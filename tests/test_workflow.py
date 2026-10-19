"""The sync workflow's own decision logic, exercised rather than eyeballed.

.github/workflows/sync.yml makes real decisions — whether a failure opens an issue,
whether a skipped tick may vouch for the calendar to the dead-man's switch, which steps a
skipped tick must stay out of — and none of it ran anywhere but production. That is how
116 failure issues got filed before anyone looked at the rule that filed them.

Two techniques, both against the real file, plus a section that chains them to the real
program (main()'s published outputs fed straight into the step scripts):
  * `run:` scripts are executed with bash the way the runner executes them, with every
    `${{ }}` value supplied through env exactly as the step's env: block would (the
    workflow never interpolates into a script, so nothing is lost), and every external
    command (gh, curl, python) replaced by a stub that records its argv.
  * `if:` conditions are evaluated for the handful of scenarios that matter (a skipped
    tick, a run that ran and succeeded, one that failed, one that died before the program
    started) by a tiny evaluator covering just the expression subset this workflow uses.
"""
from __future__ import annotations

import os
import re
import subprocess
from datetime import timedelta
from pathlib import Path

import pytest
import yaml

from src import sync_calendar
from src.models import MARKER_ID
# The end-to-end section drives main() on test_main's rig; importing the fixtures is what
# makes them available here.
from test_main import (NOW, _freeze, _openings, _outputs, _result, _seed_marker,
                       gh_out, rig)  # noqa: F401

ROOT = Path(__file__).resolve().parent.parent
WORKFLOW = ROOT / ".github" / "workflows" / "sync.yml"


def _workflow() -> dict:
    return yaml.safe_load(WORKFLOW.read_text(encoding="utf-8"))


def _triggers(wf: dict) -> dict:
    # PyYAML follows YAML 1.1, where a bare `on` key loads as the boolean True.
    return wf.get("on", wf.get(True))


def _job() -> dict:
    return _workflow()["jobs"]["sync"]


def _step(name: str) -> dict:
    return next(s for s in _job()["steps"] if s.get("name") == name)


def _literal_env(block: dict | None) -> dict[str, str]:
    """The env values that are fixed in the file (thresholds, the gate window). Expression
    values are the test's to supply — they are what each scenario varies."""
    return {k: str(v) for k, v in (block or {}).items() if "${{" not in str(v)}


STUB = """#!/usr/bin/env bash
{ printf 'CALL %s\\n' "$(basename "$0")"; printf '%s\\n' "$@"; } >> "$STUB_LOG"
case "$(basename "$0") $1 $2" in
  "gh issue list") printf '%s\\n' "${FAKE_OPEN_ISSUE:-}" ;;
esac
exit "${FAKE_EXIT:-0}"
"""


def _run(name: str, tmp_path: Path, **env: str) -> tuple[subprocess.CompletedProcess, str, str]:
    """Run step `name`'s script; return (process, stub-call log, step summary)."""
    bin_dir = tmp_path / "bin"
    bin_dir.mkdir(exist_ok=True)
    for cmd in ("gh", "curl", "python"):
        stub = bin_dir / cmd
        stub.write_text(STUB, encoding="utf-8")
        stub.chmod(0o755)
    log, summary, output = (tmp_path / "calls.log", tmp_path / "summary.md",
                            tmp_path / "github_output")
    for f in (log, summary, output):
        f.write_text("", encoding="utf-8")
    step = _step(name)
    script = tmp_path / "step.sh"
    script.write_text(step["run"], encoding="utf-8")
    full_env = {
        "PATH": f"{bin_dir}{os.pathsep}{os.environ.get('PATH', '/usr/bin:/bin')}",
        "HOME": str(tmp_path),
        "GITHUB_EVENT_NAME": "schedule",
        "GITHUB_OUTPUT": str(output),
        "GITHUB_STEP_SUMMARY": str(summary),
        "RUNNER_TEMP": str(tmp_path),
        "STUB_LOG": str(log),
        **_literal_env(_job().get("env")),
        **_literal_env(step.get("env")),
        **env,
    }
    # `bash -e {0}` is what the runner uses for a step with no `shell:`.
    proc = subprocess.run(["bash", "--noprofile", "--norc", "-e", str(script)],
                          cwd=tmp_path, env=full_env, capture_output=True, text=True,
                          timeout=30)
    return proc, log.read_text(encoding="utf-8"), summary.read_text(encoding="utf-8")


def _when(cond: str | None, *, status: str, outputs: dict[str, str]) -> bool:
    """Evaluate a step `if:` for one scenario. Covers only what this workflow uses: the
    status functions, steps.<id>.outputs.<key>, string ==/!=, &&, || and parentheses. As
    on the runner, a condition with no status function is implicitly ANDed with success();
    a missing output reads as ''."""
    expr = (cond or "").strip()
    if expr.startswith("${{") and expr.endswith("}}"):
        expr = expr[3:-2].strip()
    if not expr:
        return status == "success"
    status_fns = {"success()": status == "success", "failure()": status == "failure",
                  "cancelled()": status == "cancelled", "always()": True}
    has_status = any(fn in expr for fn in status_fns)
    py = expr
    for fn, val in status_fns.items():
        py = py.replace(fn, f" {val} ")
    py = re.sub(r"steps\.[\w-]+\.outputs\.([\w-]+)",
                lambda m: repr(outputs.get(m.group(1), "")), py)
    py = py.replace("&&", " and ").replace("||", " or ")
    py = re.sub(r"!(?!=)", " not ", py)
    value = bool(eval(py, {"__builtins__": {}}, {}))   # noqa: S307 — our own file, no names
    return value if has_status else (value and status == "success")


# ------------------------------------------------------------------ schedule + the gate

def test_schedule_gives_four_independent_chances_an_hour():
    """GitHub delivered ~4 of 24 hourly ticks a day from late Sep 2026 on. Four separate
    cron entries are four separate chances, none on the overloaded top of the hour."""
    crons = [entry["cron"] for entry in _triggers(_workflow())["schedule"]]
    assert sorted(crons, key=lambda c: int(c.split()[0])) == [
        "8 * * * *", "23 * * * *", "38 * * * *", "53 * * * *"]


def _program_argv(calls: str) -> list[str]:
    """The argv the stubbed `python` received from the sync step, after `-m <module>`."""
    argv = calls.split("CALL python\n", 1)[1].splitlines()
    assert argv[:2] == ["-m", "src.sync_calendar"]
    return argv[2:]


def test_every_run_passes_the_50_minute_freshness_gate(tmp_path):
    """Without the gate, four ticks an hour would mean up to four Timely sweeps an hour.
    Run the real sync step and look at the argv the program actually receives."""
    proc, calls, _ = _run("Sync calendar", tmp_path, DEPTH="auto", GITHUB_RUN_ATTEMPT="1")
    assert proc.returncode == 0, proc.stderr
    argv = _program_argv(calls)
    i = argv.index("--skip-if-checked-within")
    assert argv[i + 1] == "50"
    assert argv[argv.index("--depth") + 1] == "auto"


@pytest.mark.parametrize("attempt", ["2", "3"])
def test_a_rerun_is_not_gated(tmp_path, attempt):
    """'Re-run jobs' reuses the original run's event, so re-running a failed scheduled run
    is depth auto again. Gated, it read the failed attempt's own start stamp and became a
    green no-op: no Timely request, the calendar still stale, the failure issue still open.
    GitHub never re-runs by itself, so attempt > 1 is a person asking, like near/full."""
    proc, calls, _ = _run("Sync calendar", tmp_path, DEPTH="auto", GITHUB_RUN_ATTEMPT=attempt)
    assert proc.returncode == 0, proc.stderr
    argv = _program_argv(calls)
    assert argv[argv.index("--skip-if-checked-within") + 1] == "0"
    assert f"Re-run (attempt {attempt})" in proc.stdout


def test_workflow_level_concurrency_group_is_kept():
    """Two syncs at once would race on the same calendar events."""
    wf = _workflow()
    assert wf["concurrency"] == {"group": "sync-open-slots", "cancel-in-progress": False}


# ------------------------------------------------------------------ which steps run when

SKIPPED = {"skipped": "true", "exit-code": "0", "last_success_age_hours": "0.4"}
RAN = {"skipped": "false", "exit-code": "0", "last_success_age_hours": "1.1"}
FAILED = {"skipped": "false", "exit-code": "1", "last_success_age_hours": "1.1"}


@pytest.mark.parametrize("name, status, outputs, runs", [
    # A skipped tick fetched nothing: no artifact warning, no .ics check, and above all it
    # must not close a failure issue as "Recovered" — the run it deferred to may have failed.
    ("Upload fetch result", "success", SKIPPED, False),
    ("Check .ics before publishing", "success", SKIPPED, False),
    ("Clear failure issue on recovery", "success", SKIPPED, False),
    ("Report failure", "success", SKIPPED, False),
    # ...but it still reports itself, and still gets to the heartbeat (which then decides).
    ("Run summary", "success", SKIPPED, True),
    ("Heartbeat", "success", SKIPPED, True),
    # A run that really ran and succeeded keeps every behaviour it had.
    ("Upload fetch result", "success", RAN, True),
    ("Check .ics before publishing", "success", RAN, True),
    ("Clear failure issue on recovery", "success", RAN, True),
    ("Heartbeat", "success", RAN, True),
    ("Report failure", "success", RAN, False),
    # Failures and cancellations: keep the fetch, report, never "recover".
    ("Upload fetch result", "failure", FAILED, True),
    ("Report failure", "failure", FAILED, True),
    ("Report failure", "cancelled", FAILED, True),
    ("Clear failure issue on recovery", "failure", FAILED, False),
    ("Heartbeat", "failure", FAILED, False),
    # Died before the program could write any output (e.g. a missing secret).
    ("Report failure", "failure", {}, True),
    ("Run summary", "failure", {}, True),
])
def test_step_conditions(name, status, outputs, runs):
    assert _when(_step(name).get("if"), status=status, outputs=outputs) is runs


def test_skipped_run_summary_says_it_was_skipped_and_why(tmp_path):
    proc, _, summary = _run("Run summary", tmp_path, DEPTH="auto", EXIT_CODE="0",
                            SKIPPED="true", LAST_OK_AGE="0.4", ICS_EVENTS="", PUBLISH="")
    assert proc.returncode == 0, proc.stderr
    assert "Skipped by the freshness gate" in summary
    assert "less than 50 minutes ago" in summary
    assert "| last successful sync | 0.4 h before this run |" in summary


def test_run_summary_for_a_real_run_does_not_claim_a_skip(tmp_path):
    proc, _, summary = _run("Run summary", tmp_path, DEPTH="auto", EXIT_CODE="0",
                            SKIPPED="false", LAST_OK_AGE="", ICS_EVENTS="12",
                            PUBLISH="true")
    assert proc.returncode == 0, proc.stderr
    assert "Skipped by the freshness gate" not in summary
    assert "| last successful sync | unknown |" in summary


# ------------------------------------------------------------------ failure reporting

def _report(tmp_path, *, age: str, open_issue: str = "", status: str = "failure",
            exit_code: str = "1", skipped: str = "false"):
    return _run("Report failure", tmp_path, GH_TOKEN="x", JOB_STATUS=status,
                RUN_URL="https://example.invalid/run/1", EXIT_CODE=exit_code,
                SKIPPED=skipped, LAST_OK_AGE=age, FAKE_OPEN_ISSUE=open_issue)


@pytest.mark.parametrize("age", ["0.4", "3.5", "11.9"])
def test_transient_failure_warns_instead_of_opening_an_issue(tmp_path, age):
    """Failures alternating with successes used to open (and then close) a fresh issue
    every time — 116 of them in ten weeks. Under the threshold it is a warning."""
    proc, calls, _ = _report(tmp_path, age=age)
    assert proc.returncode == 0, proc.stderr
    assert "CALL gh\nissue\ncreate" not in calls
    assert "CALL gh\nissue\ncomment" not in calls
    assert "::warning::Transient failure" in proc.stdout
    assert f"{age} h" in proc.stdout


@pytest.mark.parametrize("age", ["12.0", "12.1", "40.0"])
def test_stale_calendar_opens_an_issue(tmp_path, age):
    proc, calls, _ = _report(tmp_path, age=age)
    assert proc.returncode == 0, proc.stderr
    assert "CALL gh\nissue\ncreate" in calls
    assert f"last successful sync: {age} h" in calls


def test_unknown_age_is_loud(tmp_path):
    """No age means the run died before it could even read the marker — a missing secret,
    a revoked key, Google down — or the marker records no success at all. Never a passing
    Timely blip; open the issue."""
    proc, calls, _ = _report(tmp_path, age="")
    assert proc.returncode == 0, proc.stderr
    assert "CALL gh\nissue\ncreate" in calls
    assert "last successful sync: unknown" in calls


def test_open_issue_gets_a_comment_with_the_age_even_when_young(tmp_path):
    """One issue per streak: once open, every failure lands on it, whatever the age."""
    proc, calls, _ = _report(tmp_path, age="0.4", open_issue="7")
    assert proc.returncode == 0, proc.stderr
    assert "CALL gh\nissue\ncomment\n7\n" in calls
    assert "CALL gh\nissue\ncreate" not in calls
    assert "last successful sync: 0.4 h" in calls


def test_cancelled_run_goes_through_the_same_rule(tmp_path):
    proc, calls, _ = _report(tmp_path, age="2.0", status="cancelled")
    assert proc.returncode == 0, proc.stderr
    assert "CALL gh\nissue\ncreate" not in calls
    assert "::warning::" in proc.stdout


@pytest.mark.parametrize("age", ["13.0", "40.0", ""])
def test_a_step_failing_after_a_successful_sync_never_opens_an_issue(tmp_path, age):
    """The age is measured BEFORE the sweep. A run that synced fine after a 13h delivery gap
    (or the very first good run of a new setup: age unknown) and then lost an artifact
    upload used to open 'sync-open-slots is failing' about a calendar it had just refreshed."""
    proc, calls, _ = _report(tmp_path, age=age, exit_code="0", skipped="false")
    assert proc.returncode == 0, proc.stderr
    assert "CALL gh\nissue\ncreate" not in calls
    assert "::warning::The sync itself succeeded" in proc.stdout
    assert "Transient failure" not in proc.stdout     # nor a made-up age in the warning


def test_a_step_failing_after_a_successful_sync_still_lands_on_an_open_issue(tmp_path):
    """One issue per streak, as for any failure — but the comment must not call a calendar
    this run just refreshed 13h stale."""
    proc, calls, _ = _report(tmp_path, age="13.0", exit_code="0", skipped="false",
                             open_issue="7")
    assert proc.returncode == 0, proc.stderr
    assert "CALL gh\nissue\ncomment\n7\n" in calls
    assert "last successful sync: this run" in calls
    assert "13.0 h" not in calls


def test_a_skipped_tick_that_fails_later_still_gets_the_age_rule(tmp_path):
    """A skip also exits 0, but it refreshed nothing: the age is the calendar's real age."""
    proc, calls, _ = _report(tmp_path, age="13.0", exit_code="0", skipped="true")
    assert proc.returncode == 0, proc.stderr
    assert "CALL gh\nissue\ncreate" in calls
    assert "last successful sync: 13.0 h" in calls


# ------------------------------------------------------------------ heartbeat

def _heartbeat(tmp_path, *, skipped: str, age: str, url: str = "https://hc.invalid/ping"):
    return _run("Heartbeat", tmp_path, HEARTBEAT_URL=url, SKIPPED=skipped,
                LAST_OK_AGE=age)


@pytest.mark.parametrize("skipped, age, pings", [
    ("false", "30.0", True),     # it ran and succeeded just now: that IS fresh
    ("false", "", True),
    ("true", "0.1", True),       # deferred to a run that succeeded minutes ago
    ("true", "0.7", True),       # 39-45 min: certainly inside the 50-min window
    # The age is published to one decimal, so "0.8" is anywhere from 45 to 51 minutes. The
    # check takes the top of that range: a skip vouches only for a calendar that is
    # certainly under 50 minutes old, never one that may be just over.
    ("true", "0.8", False),
    # Deferred to a run that FAILED: the gate counts attempts, so the surplus ticks after a
    # failing run would otherwise ping for it and the switch would never fire.
    ("true", "1.1", False),
    ("true", "26.5", False),
    ("true", "", False),
])
def test_heartbeat_pings_only_for_a_fresh_calendar(tmp_path, skipped, age, pings):
    proc, calls, _ = _heartbeat(tmp_path, skipped=skipped, age=age)
    assert proc.returncode == 0, proc.stderr
    assert ("CALL curl" in calls) is pings
    if not pings:
        assert "not pinging" in proc.stdout


def test_heartbeat_without_url_is_a_clean_notice(tmp_path):
    proc, calls, _ = _heartbeat(tmp_path, skipped="false", age="0.1", url="")
    assert proc.returncode == 0, proc.stderr
    assert "CALL curl" not in calls
    assert "KIDA_HEARTBEAT_URL not set" in proc.stdout


# ------------------------------------------------------------------ program → workflow
#
# The workflow's rules are only as true as the number it is fed. Each test below runs the
# real main() through a scenario, takes the GITHUB_OUTPUT values it actually published, and
# hands them to the real step script — the chain production runs, with nothing assumed
# about what last_success_age_hours means. That is the gap the 12-hour rule first shipped
# through: every step test above passed while main() stamped last_run on runs about to
# exit 1, so a persistent failure read as ~6h old on every run and never opened an issue.

def _main(monkeypatch, gh_out, at, *argv) -> tuple[int, dict[str, str]]:
    """One production tick at `at`: the argv sync.yml passes, plus `argv`. Returns the exit
    code and the outputs main() published for the steps after it."""
    _freeze(monkeypatch, at)
    gh_out.write_text("", encoding="utf-8")
    try:
        sync_calendar.main(["--depth", "auto", "--skip-if-checked-within", "50", *argv])
        code = 0
    except SystemExit as e:
        code = e.code
    return code, _outputs(gh_out)


def _hold_deletes_for_a_stylist_we_cannot_see(rig):
    """Production-shaped exit 1: a stylist who is PLANNED but whose lookups all fail holds
    more entries than SKIPPED_DELETE_EXIT_FLOOR, so their deletes are withheld every run.
    test_main's departed-stylist test records this every-run-exits-1 state happening."""
    cfg = rig["config"]
    for ev in _openings(n=sync_calendar.SKIPPED_DELETE_EXIT_FLOOR + 5, stylist="unseen"):
        (entry,) = sync_calendar.build_entries([ev], cfg.event_style)
        body = sync_calendar.entry_body(entry, cfg, "")
        rig["service"].store[body["id"]] = dict(body, status="confirmed")
    rig["result"] = _result(_openings(stylist="175308"), staff_ok={"175308"})
    rig["result"].staff_planned = {"175308", "unseen"}


def _step_dir(tmp_path, name: str) -> Path:
    # Not tmp_path itself: gh_out already owns tmp_path/github_output.
    d = tmp_path / name
    d.mkdir()
    return d


def test_a_persistent_exit_1_opens_an_issue_once_the_calendar_is_12h_stale(
        rig, monkeypatch, gh_out, tmp_path):
    """README: 'it opens a GitHub issue — but only once the calendar has gone 12 hours or
    more without a successful refresh'. Last real success 1h ago, then every run exits 1,
    6h apart (GitHub's recent cadence). The issue must open on the first run that finds
    the calendar 12h+ stale, and the streak must land on it from then on."""
    _seed_marker(rig, last_run=NOW - timedelta(hours=1),
                 last_attempt=NOW - timedelta(hours=1),
                 last_deep_sweep=NOW - timedelta(hours=1))
    _hold_deletes_for_a_stylist_we_cannot_see(rig)

    open_issue, timeline = "", []
    for i in range(4):
        code, out = _main(monkeypatch, gh_out, NOW + timedelta(hours=6 * i))
        assert code == 1
        age = out["last_success_age_hours"]
        proc, calls, _ = _run("Report failure", _step_dir(tmp_path, f"run{i}"),
                              GH_TOKEN="x", JOB_STATUS="failure", RUN_URL="u",
                              EXIT_CODE=str(code), LAST_OK_AGE=age,
                              FAKE_OPEN_ISSUE=open_issue)
        assert proc.returncode == 0, proc.stderr
        if "CALL gh\nissue\ncreate" in calls:
            timeline.append((age, "opened"))
            open_issue = "41"
        elif "CALL gh\nissue\ncomment\n41\n" in calls:
            timeline.append((age, "commented"))
        else:
            assert "::warning::Transient failure" in proc.stdout
            timeline.append((age, "warned"))

    # Before the fix this was 1.0 / 6.0 / 6.0 / 6.0, all "warned": 19h of a failing
    # calendar and not one issue.
    assert timeline == [("1.0", "warned"), ("7.0", "warned"),
                        ("13.0", "opened"), ("19.0", "commented")]


def test_skipped_ticks_behind_a_failing_run_do_not_feed_the_heartbeat(
        rig, monkeypatch, gh_out, tmp_path):
    """The gate counts attempts, so the surplus ticks after a failing run skip. If they
    pinged, three pings an hour would cover an outage and the switch would never fire."""
    _seed_marker(rig, last_run=NOW - timedelta(hours=6),
                 last_attempt=NOW - timedelta(hours=6),
                 last_deep_sweep=NOW - timedelta(hours=1))
    _hold_deletes_for_a_stylist_we_cannot_see(rig)
    code, _ = _main(monkeypatch, gh_out, NOW)
    assert code == 1

    for minutes in (15, 30, 45):
        code, out = _main(monkeypatch, gh_out, NOW + timedelta(minutes=minutes))
        assert (code, out["skipped"]) == (0, "true")
        proc, calls, _ = _run("Heartbeat", _step_dir(tmp_path, f"hb{minutes}"),
                              HEARTBEAT_URL="https://hc.invalid/ping", SKIPPED="true",
                              LAST_OK_AGE=out["last_success_age_hours"])
        assert proc.returncode == 0, proc.stderr
        # Before the fix the age read 0.2-0.8 here and every one of these pinged.
        assert "CALL curl" not in calls, out
        assert "not pinging" in proc.stdout


def test_a_skip_pings_only_while_the_last_success_is_under_the_gate_window(
        rig, monkeypatch, gh_out, tmp_path):
    """What the heartbeat's skip rule does and does not promise. It is about the CALENDAR,
    not the run the tick deferred to: a quick failure right behind a long sweep can be
    followed by a ping while that sweep's result is still under 50 minutes old. What it
    guarantees is that an outage stops the pings no later than 50 minutes after the last
    success, because nothing but a success makes that age younger."""
    # Success A started 60 min ago and finished 22 min ago (a 38-minute deep sweep).
    _seed_marker(rig, last_attempt=NOW - timedelta(minutes=60),
                 last_run=NOW - timedelta(minutes=22),
                 last_deep_sweep=NOW - timedelta(minutes=22))
    rig["result"] = _result(_openings(), ok=False)

    def failing_run_then_skip(at, label):
        code, _ = _main(monkeypatch, gh_out, at)
        assert code == 2                                   # untrusted fetch, writes nothing
        code, out = _main(monkeypatch, gh_out, at + timedelta(minutes=15))
        assert (code, out["skipped"]) == (0, "true")
        proc, calls, _ = _run("Heartbeat", _step_dir(tmp_path, label),
                              HEARTBEAT_URL="https://hc.invalid/ping", SKIPPED="true",
                              LAST_OK_AGE=out["last_success_age_hours"])
        assert proc.returncode == 0, proc.stderr
        return out["last_success_age_hours"], "CALL curl" in calls

    # Run B fails at once; the tick behind it sees A's result 37 minutes old: fresh.
    assert failing_run_then_skip(NOW, "first") == ("0.6", True)
    # The outage goes on. An hour later A's result is 97 minutes old, and nothing pings.
    assert failing_run_then_skip(NOW + timedelta(hours=1), "second") == ("1.6", False)


def test_rerunning_a_failed_scheduled_run_syncs_instead_of_skipping(
        rig, monkeypatch, gh_out, tmp_path):
    """The owner sees a red scheduled run and clicks 'Re-run all jobs' ten minutes later.
    The re-run's argv comes from the real step (attempt 2, same schedule event, so depth
    auto). Before the fix the program read the failed attempt's start stamp, skipped, and
    the re-run went green having done nothing — the calendar stale and the issue open."""
    _seed_marker(rig, last_run=NOW - timedelta(hours=3),
                 last_attempt=NOW - timedelta(hours=3),
                 last_deep_sweep=NOW - timedelta(hours=1))
    rig["result"] = _result(_openings(), ok=False)           # attempt 1: a Timely blip
    code, out = _main(monkeypatch, gh_out, NOW)
    assert (code, out["skipped"]) == (2, "false")

    rig["result"] = _result(_openings())                     # the blip is over
    d = _step_dir(tmp_path, "rerun")
    proc, calls, _ = _run("Sync calendar", d, DEPTH="auto", GITHUB_RUN_ATTEMPT="2")
    assert proc.returncode == 0, proc.stderr
    monkeypatch.chdir(d)            # the step's relative --ics / --fetch-json paths
    _freeze(monkeypatch, NOW + timedelta(minutes=10))
    gh_out.write_text("", encoding="utf-8")
    sync_calendar.main(_program_argv(calls))                 # exits 0: no SystemExit
    out = _outputs(gh_out)

    assert out["skipped"] == "false"
    assert rig["fetches"] == 2
    assert out["last_success_age_hours"] == "3.2"            # measured before this sweep
    # ...and this run, not a later one, closes the failure issue.
    assert _when(_step("Clear failure issue on recovery").get("if"), status="success",
                 outputs={**out, "exit-code": "0"})


def test_a_run_that_syncs_after_a_long_gap_then_fails_a_later_step_opens_no_issue(
        rig, monkeypatch, gh_out, tmp_path):
    """GitHub delivered nothing for 13h; the next run syncs cleanly (exit 0) and then an
    artifact upload fails. main() published the age it found BEFORE the sweep — 13.0 — and
    Report failure used to open 'sync-open-slots is failing' off it."""
    _seed_marker(rig, last_run=NOW - timedelta(hours=13),
                 last_attempt=NOW - timedelta(hours=13),
                 last_deep_sweep=NOW - timedelta(hours=1))
    rig["result"] = _result(_openings())
    code, out = _main(monkeypatch, gh_out, NOW)
    assert (code, out["skipped"], out["last_success_age_hours"]) == (0, "false", "13.0")

    assert _when(_step("Report failure").get("if"), status="failure",
                 outputs={**out, "exit-code": "0"})
    proc, calls, _ = _run("Report failure", _step_dir(tmp_path, "report"), GH_TOKEN="x",
                          JOB_STATUS="failure", RUN_URL="u", EXIT_CODE=str(code),
                          SKIPPED=out["skipped"], LAST_OK_AGE=out["last_success_age_hours"])
    assert proc.returncode == 0, proc.stderr
    assert "CALL gh\nissue\ncreate" not in calls
    assert "::warning::The sync itself succeeded" in proc.stdout


def test_a_new_calendar_whose_runs_all_fail_says_why_the_age_is_unknown(
        rig, monkeypatch, gh_out, tmp_path):
    """A new setup's first run exits 1 and creates the status marker with no success on it.
    The next failing run reads that marker and still publishes an unknown age. The issue
    used to blame 'failed before it could read the calendar's status marker, or there is no
    marker yet' — both false here — and send the owner hunting for a credentials problem."""
    _hold_deletes_for_a_stylist_we_cannot_see(rig)
    code, _ = _main(monkeypatch, gh_out, NOW)
    assert code == 1
    assert MARKER_ID in rig["service"].live()                # the marker now exists...

    code, out = _main(monkeypatch, gh_out, NOW + timedelta(hours=6))
    assert (code, out["last_success_age_hours"]) == (1, "")  # ...and was read: still unknown
    proc, calls, _ = _run("Report failure", _step_dir(tmp_path, "report"), GH_TOKEN="x",
                          JOB_STATUS="failure", RUN_URL="u", EXIT_CODE=str(code),
                          SKIPPED=out["skipped"], LAST_OK_AGE="")
    assert proc.returncode == 0, proc.stderr
    assert "CALL gh\nissue\ncreate" in calls
    assert "no successful sync is recorded on the calendar's status marker" in calls
    assert "none of its runs has succeeded" in calls


def test_the_outputs_the_steps_read_are_the_ones_the_program_publishes(
        rig, monkeypatch, gh_out):
    """The sync step's comment lists what main() writes to GITHUB_OUTPUT and when: two
    before Timely is contacted (so they survive a fetch that dies), `deep` after. Every
    steps.sync.outputs.X the workflow reads must be one of those or the step's own
    exit-code — a name that drifts reads as '' and silently flips a step's decision."""
    _seed_marker(rig, last_run=NOW - timedelta(hours=2),
                 last_attempt=NOW - timedelta(hours=2),
                 last_deep_sweep=NOW - timedelta(hours=1))
    rig["result"] = _result(_openings())
    at_fetch = {}
    rig["on_fetch"] = lambda: at_fetch.update(_outputs(gh_out))
    code, out = _main(monkeypatch, gh_out, NOW)
    assert code == 0
    assert set(at_fetch) == {"skipped", "last_success_age_hours"}
    assert set(out) == {"skipped", "last_success_age_hours", "deep"}

    read = set(re.findall(r"steps\.sync\.outputs\.([\w-]+)", WORKFLOW.read_text("utf-8")))
    assert read <= set(out) | {"exit-code"}, read - set(out)
