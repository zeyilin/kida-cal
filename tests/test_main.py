"""Tests for sync_calendar.main() — the decision layer.

main() had zero coverage while owning the decisions with the largest blast radius: whether
an untrusted fetch is allowed to write, what the process exits with, whether the published
.ics is replaced, and whether a run gets to claim it performed a deep sweep. Every one of
those is reachable here through the `argv` seam plus a fake calendar service.
"""
import json
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import mkconfig
from fakes import FakeCalendarService, frozen_datetime, http_error
from src import sync_calendar
from src.fetch_availability import FetchResult
from src.models import MARKER_ID, Event, ServiceOption

NY = ZoneInfo("America/New_York")


def _openings(n=4, stylist="175308", days_ahead=3, hour=None):
    """n hourly openings `days_ahead` days out, from `hour` (default: the current hour).
    Pass an hour whenever the test depends on how they merge: from the current hour, n of
    them cross midnight in the evening and split into two blocks."""
    base = datetime.now(NY).replace(minute=0, second=0, microsecond=0)
    if hour is not None:
        base = base.replace(hour=hour)
    return [Event(stylist_id=stylist, stylist="Nao", stylist_role="Stylist",
                  start=base + timedelta(days=days_ahead, hours=h),
                  book_url="https://book",
                  options=[ServiceOption("Hair Cut", "$75", 55, False)])
            for h in range(n)]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """Drive main() against a fake calendar with a scripted fetch."""
    svc = FakeCalendarService()
    state = {"service": svc, "result": None, "config": mkconfig(), "fetches": 0,
             "on_fetch": None}

    def fake_fetch(cfg, **k):
        # Counted, because "the gate skipped BEFORE any Timely request" is only provable by
        # showing this was never reached. `on_fetch` lets a test look at the calendar and
        # the workflow outputs exactly as they stood when the fetch began.
        state["fetches"] += 1
        if state["on_fetch"]:
            state["on_fetch"]()
        return state["result"]

    monkeypatch.setattr(sync_calendar, "get_service", lambda: state["service"])
    monkeypatch.setattr(sync_calendar, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(sync_calendar.Config, "load", classmethod(
        lambda cls, path="config.yaml": state["config"]))
    monkeypatch.setattr(sync_calendar, "fetch", fake_fetch)
    monkeypatch.setenv("KIDA_CALENDAR_ID", "cal-under-test")
    state["tmp"] = tmp_path
    return state


def _result(events, ok=True, staff_ok=None):
    return FetchResult(slots=[], events=list(events), notices="", ok=ok,
                       lookups_ok=60 if ok else 0, lookups_failed=0 if ok else 60,
                       lookups_expected=60, staff_ok=staff_ok)


# --------------------------------------------------------------- write refusal
def test_an_untrusted_fetch_writes_nothing_and_exits_2(rig):
    rig["result"] = _result(_openings(), ok=False)
    rig["service"].store["kidapreexisting0000000000000000000000000"] = {
        "id": "kidapreexisting0000000000000000000000000", "status": "confirmed",
        "summary": "old", "start": {"dateTime": datetime.now(NY).isoformat()},
        "end": {"dateTime": datetime.now(NY).isoformat()}}

    with pytest.raises(SystemExit) as ei:
        sync_calendar.main(["--depth", "near"])

    assert ei.value.code == 2
    # Not one write of any kind — this used to insert and patch, then report failure.
    assert [m for m, _ in rig["service"].calls
            if m in ("insert", "patch", "delete", "update")] == []


def test_a_healthy_run_exits_zero_and_writes(rig):
    rig["result"] = _result(_openings())
    sync_calendar.main(["--depth", "near"])          # no SystemExit
    assert rig["service"].methods("insert")


def test_withheld_deletes_above_the_floor_fail_the_run(rig):
    """A run whose every API call succeeded but which refused a large share of its deletes
    did not do its job; reporting green is how a partial outage stays invisible."""
    cfg = rig["config"]
    stale = _openings(n=sync_calendar.SKIPPED_DELETE_EXIT_FLOOR + 5, stylist="ghost")
    for ev in stale:
        entry = sync_calendar.build_entries([ev], cfg.event_style)[0]
        body = sync_calendar.entry_body(entry, cfg, "")
        rig["service"].store[body["id"]] = dict(body, status="confirmed")
    # 'ghost' is not in staff_ok, so all their deletes are withheld.
    rig["result"] = _result(_openings(stylist="175308"), staff_ok={"175308"})

    with pytest.raises(SystemExit) as ei:
        sync_calendar.main(["--depth", "near"])
    assert ei.value.code == 1
    assert rig["service"].methods("delete") == []


def test_a_few_withheld_deletes_do_not_fail_the_run(rig):
    cfg = rig["config"]
    ev = _openings(n=1, stylist="ghost")[0]
    entry = sync_calendar.build_entries([ev], cfg.event_style)[0]
    body = sync_calendar.entry_body(entry, cfg, "")
    rig["service"].store[body["id"]] = dict(body, status="confirmed")
    rig["result"] = _result(_openings(stylist="175308"), staff_ok={"175308"})

    sync_calendar.main(["--depth", "near"])          # routine; must not raise


# --------------------------------------------------------------- .ics gating
def test_near_tier_never_republishes_the_ics(rig):
    """A near run knows 21 days; publishing from it would truncate a 90-day feed that
    Google only refetches every 8-24h."""
    out = rig["tmp"] / "feed.ics"
    rig["result"] = _result(_openings())
    sync_calendar.main(["--depth", "near", "--ics", str(out)])
    assert not out.exists()


def test_deep_tier_publishes(rig):
    out = rig["tmp"] / "feed.ics"
    rig["result"] = _result(_openings())
    sync_calendar.main(["--depth", "full", "--ics", str(out)])
    assert out.exists() and "BEGIN:VEVENT" in out.read_text()


def test_a_deep_run_with_no_entries_refuses_to_publish(rig):
    """A zero-entry feed is a valid ~330-byte VCALENDAR, so nothing downstream would
    notice it replacing a good one. (fetch() would also mark a zero-slot run untrusted;
    this covers the belt-and-braces check in main() on its own.)"""
    out = rig["tmp"] / "feed.ics"
    out.write_text("PREVIOUS GOOD FEED")
    rig["result"] = _result([])                      # ok=True but empty
    sync_calendar.main(["--depth", "full", "--ics", str(out)])
    assert out.read_text() == "PREVIOUS GOOD FEED"


# --------------------------------------------------------------- replay
def test_a_near_capture_replayed_as_deep_is_neither_deep_nor_wide(rig):
    """`--depth full --from-json <near capture>` is the natural thing to run mid-incident.
    It must not republish a truncated .ics over the full feed, nor stamp last_deep_sweep."""
    cap = rig["tmp"] / "cap.json"
    out = rig["tmp"] / "feed.ics"
    rig["result"] = _result(_openings())
    cap.write_text(sync_calendar.result_to_json(rig["result"], lookahead_days=21))

    sync_calendar.main(["--depth", "full", "--from-json", str(cap), "--ics", str(out)])

    assert not out.exists(), "a 21-day capture must not replace the published feed"
    marker = rig["service"].live().get(MARKER_ID)
    assert marker is not None
    assert "last_deep_sweep" not in marker["extendedProperties"]["private"]


def test_fetch_json_round_trips_the_fields_the_sync_depends_on(rig):
    cap = rig["tmp"] / "cap.json"
    original = _result(_openings(), staff_ok={"175308"})
    rig["result"] = original
    sync_calendar.main(["--depth", "near", "--fetch-json", str(cap)])

    back = sync_calendar.result_from_json(cap.read_text())
    assert back.ok is original.ok
    assert back.staff_ok == original.staff_ok
    assert [e.google_event_id() for e in back.events] == \
        [e.google_event_id() for e in original.events]
    assert json.loads(cap.read_text())["lookahead_days"] == rig["config"].near_lookahead_days


def test_staff_ok_none_survives_the_round_trip(rig):
    """None (no per-stylist information) is semantically distinct from the empty set
    (nobody is covered) — collapsing them would grant or revoke delete authority wholesale."""
    cap = rig["tmp"] / "cap.json"
    rig["result"] = _result(_openings(), staff_ok=None)
    sync_calendar.main(["--depth", "near", "--fetch-json", str(cap)])
    assert sync_calendar.result_from_json(cap.read_text()).staff_ok is None


# --------------------------------------------------------------- marker
def test_a_clean_deep_run_records_the_deep_sweep(rig):
    rig["result"] = _result(_openings())
    sync_calendar.main(["--depth", "full"])
    private = rig["service"].live()[MARKER_ID]["extendedProperties"]["private"]
    assert "last_deep_sweep" in private


def test_the_marker_reports_openings_not_entries(rig):
    rig["config"] = mkconfig(event_style="blocks")
    rig["result"] = _result(_openings(n=4))          # 4 openings -> 1 block
    sync_calendar.main(["--depth", "near"])
    assert "4 open" in rig["service"].live()[MARKER_ID]["summary"]


def test_the_marker_counts_openings_outside_this_runs_window(rig):
    """Defect F, end to end: a near run used to title the marker with its own 21 days of
    openings, so a calendar showing 90 days of availability read '4 open'."""
    cfg = rig["config"] = mkconfig(event_style="blocks")
    # Only a deep sweep sees these. A fixed hour, so the six stay one block whatever time
    # of day the suite runs.
    far = _openings(n=6, stylist="24102", days_ahead=40, hour=9)
    (block,) = sync_calendar.build_entries(far, "blocks")
    body = sync_calendar.entry_body(block, cfg, "")
    rig["service"].store[body["id"]] = dict(body, status="confirmed")
    rig["result"] = _result(_openings(n=4))

    sync_calendar.main(["--depth", "near"])

    assert "10 open" in rig["service"].live()[MARKER_ID]["summary"]


def test_a_departed_stylist_no_longer_fails_every_run(rig):
    """Defect D, end to end: more than SKIPPED_DELETE_EXIT_FLOOR held entries for someone
    the fetch never looked for meant exit 1 every hour until they aged out."""
    # slots, so the departed stylist's contiguous openings stay one entry each.
    cfg = rig["config"] = mkconfig(event_style="slots")
    for ev in _openings(n=sync_calendar.SKIPPED_DELETE_EXIT_FLOOR + 5, stylist="left"):
        entry = sync_calendar.build_entries([ev], cfg.event_style)[0]
        body = sync_calendar.entry_body(entry, cfg, "")
        rig["service"].store[body["id"]] = dict(body, status="confirmed")
    rig["result"] = _result(_openings(stylist="175308"), staff_ok={"175308"})
    rig["result"].staff_planned = {"175308"}

    sync_calendar.main(["--depth", "near"])                  # exit 0: must not raise

    assert len(rig["service"].methods("delete")) == sync_calendar.SKIPPED_DELETE_EXIT_FLOOR + 5


# --------------------------------------------------------------- the skip gate
NOW = datetime.now(NY).replace(microsecond=0)   # frozen per test; events are built around it


def _freeze(monkeypatch, at=NOW):
    monkeypatch.setattr(sync_calendar, "datetime", frozen_datetime(at))


def _seed_marker(rig, **stamps):
    rig["service"].store[MARKER_ID] = {
        "id": MARKER_ID, "status": "confirmed", "summary": "✓ KIDA slots checked",
        "start": {"date": NOW.date().isoformat()},
        "end": {"date": (NOW.date() + timedelta(days=1)).isoformat()},
        "extendedProperties": {"private": {k: v.isoformat() for k, v in stamps.items()}}}


def _outputs(path):
    if not path.exists():
        return {}
    return dict(line.split("=", 1) for line in path.read_text().splitlines())


@pytest.fixture
def gh_out(monkeypatch, tmp_path):
    path = tmp_path / "github_output"
    monkeypatch.setenv("GITHUB_OUTPUT", str(path))
    return path


@pytest.mark.parametrize("ago, skipped", [
    (timedelta(0), True),
    (timedelta(minutes=49), True),
    (timedelta(minutes=50) - timedelta(seconds=1), True),
    (timedelta(minutes=50), False),                       # the boundary itself runs
    (timedelta(minutes=61), False),
])
def test_the_gate_measures_from_the_last_run_start(rig, monkeypatch, gh_out, ago, skipped):
    _freeze(monkeypatch)
    _seed_marker(rig, last_attempt=NOW - ago, last_run=NOW - timedelta(hours=5),
                 last_deep_sweep=NOW - timedelta(hours=1))
    before = dict(rig["service"].store[MARKER_ID])
    rig["result"] = _result(_openings())

    sync_calendar.main(["--depth", "auto", "--skip-if-checked-within", "50"])

    assert _outputs(gh_out)["skipped"] == ("true" if skipped else "false")
    assert rig["fetches"] == (0 if skipped else 1)
    if skipped:
        # Not one write, and the marker is untouched: a skipped tick is not an attempt.
        assert [m for m, _ in rig["service"].calls
                if m in ("insert", "patch", "update", "delete")] == []
        assert rig["service"].store[MARKER_ID] == before


@pytest.mark.parametrize("depth", ["near", "full"])
def test_an_explicit_depth_is_never_skipped(rig, monkeypatch, gh_out, depth):
    """A dispatch asking for near or full is a person asking for a run now."""
    _freeze(monkeypatch)
    _seed_marker(rig, last_attempt=NOW - timedelta(minutes=1))
    rig["result"] = _result(_openings())

    sync_calendar.main(["--depth", depth, "--skip-if-checked-within", "50"])

    assert rig["fetches"] == 1
    assert _outputs(gh_out)["skipped"] == "false"


def test_the_gate_is_off_by_default(rig, monkeypatch, gh_out):
    _freeze(monkeypatch)
    _seed_marker(rig, last_attempt=NOW - timedelta(minutes=1))
    rig["result"] = _result(_openings())
    sync_calendar.main([])
    assert rig["fetches"] == 1


def test_the_gate_falls_back_to_last_run_on_an_older_marker(rig, monkeypatch, gh_out):
    _freeze(monkeypatch)
    _seed_marker(rig, last_run=NOW - timedelta(minutes=10))
    sync_calendar.main(["--skip-if-checked-within", "50"])
    assert rig["fetches"] == 0
    assert _outputs(gh_out)["skipped"] == "true"


def test_a_last_attempt_in_the_future_does_not_silence_the_sync(rig, monkeypatch, gh_out):
    """A broken clock or a hand-edited marker must not make every tick skip until the
    wall clock catches up."""
    _freeze(monkeypatch)
    _seed_marker(rig, last_attempt=NOW + timedelta(hours=3))
    rig["result"] = _result(_openings())
    sync_calendar.main(["--skip-if-checked-within", "50"])
    assert rig["fetches"] == 1


def test_last_attempt_is_stamped_before_the_fetch_and_keeps_the_rest(rig, monkeypatch, gh_out):
    _freeze(monkeypatch)
    deep = NOW - timedelta(hours=2)
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=1), last_run=NOW - timedelta(hours=1),
                 last_deep_sweep=deep)
    seen = {}
    rig["on_fetch"] = lambda: seen.update(
        rig["service"].store[MARKER_ID]["extendedProperties"]["private"])
    rig["result"] = _result(_openings())

    sync_calendar.main(["--skip-if-checked-within", "50"])

    assert seen["last_attempt"] == NOW.isoformat()
    assert seen["last_deep_sweep"] == deep.isoformat()          # untouched by the stamp
    assert seen["last_run"] == (NOW - timedelta(hours=1)).isoformat()


def test_the_final_marker_keeps_the_run_start_not_its_finish(rig, monkeypatch, gh_out):
    """Start-to-start: if the marker written at the end recorded the finish, a 38-minute
    deep sweep would make the next on-time tick look 22 minutes stale and skip it."""
    _freeze(monkeypatch)
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=1))
    finished = NOW + timedelta(minutes=38)
    rig["on_fetch"] = lambda: _freeze(monkeypatch, finished)    # the fetch takes 38 minutes
    rig["result"] = _result(_openings())

    sync_calendar.main(["--skip-if-checked-within", "50"])

    private = rig["service"].live()[MARKER_ID]["extendedProperties"]["private"]
    assert private["last_attempt"] == NOW.isoformat()
    assert private["last_run"] == finished.isoformat()


def test_a_fresh_calendar_gets_no_stamp_but_its_first_marker_records_the_start(
        rig, monkeypatch, gh_out):
    _freeze(monkeypatch)
    rig["result"] = _result(_openings())
    sync_calendar.main(["--skip-if-checked-within", "50"])

    assert ("patch", MARKER_ID) not in rig["service"].calls
    private = rig["service"].live()[MARKER_ID]["extendedProperties"]["private"]
    assert private["last_attempt"] == NOW.isoformat()


def test_a_dry_run_never_stamps_last_attempt(rig, monkeypatch, gh_out):
    _freeze(monkeypatch)
    # Credentials present, so the dry run really does build a service and read the marker;
    # without them it never reaches the stamp decision and this would prove nothing.
    monkeypatch.setenv("KIDA_SERVICE_ACCOUNT_JSON", "/dev/null")
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=2))
    before = dict(rig["service"].store[MARKER_ID])
    rig["result"] = _result(_openings())

    sync_calendar.main(["--dry-run", "--skip-if-checked-within", "50"])

    assert ("get", MARKER_ID) in rig["service"].calls
    assert rig["fetches"] == 1
    assert ("patch", MARKER_ID) not in rig["service"].calls
    assert rig["service"].store[MARKER_ID] == before


@pytest.mark.parametrize("depth", ["near", "full"])
def test_an_explicit_depth_still_records_its_start(rig, monkeypatch, gh_out, depth):
    """near/full are never gated, but they are still attempts on Timely: an auto tick
    landing ten minutes behind one must see it and stand down."""
    _freeze(monkeypatch)
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=3))
    seen = {}
    rig["on_fetch"] = lambda: seen.update(
        rig["service"].store[MARKER_ID]["extendedProperties"]["private"])
    rig["result"] = _result(_openings())

    sync_calendar.main(["--depth", depth])

    assert seen["last_attempt"] == NOW.isoformat()
    _freeze(monkeypatch, NOW + timedelta(minutes=10))
    sync_calendar.main(["--skip-if-checked-within", "50"])
    assert rig["fetches"] == 1
    assert _outputs(gh_out)["skipped"] == "true"


def test_a_stamp_the_calendar_refuses_stops_before_timely(rig, monkeypatch, gh_out):
    """A calendar shared read-only passes preflight (a read) and fails its first write,
    the stamp. Warning and carrying on spent a full Timely sweep per tick — and since the
    stamp never landed, the gate never skipped one."""
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=2), last_run=NOW - timedelta(hours=2),
                 last_deep_sweep=NOW - timedelta(hours=1))
    rig["service"].fail_plan = {(m, None): [http_error(403, "Forbidden", "forbidden")] * 50
                                for m in ("patch", "insert", "update", "delete")}
    rig["result"] = _result(_openings())

    for minutes in (0, 15):                      # two ticks of the four-an-hour schedule
        _freeze(monkeypatch, NOW + timedelta(minutes=minutes))
        with pytest.raises(SystemExit) as ei:
            sync_calendar.main(["--skip-if-checked-within", "50"])
        assert ei.value.code == 1

    assert rig["fetches"] == 0


def test_a_naive_last_deep_sweep_does_not_crash_the_run(rig, monkeypatch, gh_out):
    _freeze(monkeypatch)
    naive = (NOW - timedelta(hours=1)).replace(tzinfo=None)
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=2))
    rig["service"].store[MARKER_ID]["extendedProperties"]["private"]["last_deep_sweep"] = \
        naive.isoformat()
    rig["result"] = _result(_openings())

    sync_calendar.main(["--skip-if-checked-within", "50"])          # must not raise

    assert rig["fetches"] == 1
    assert "1h ago" in rig["service"].live()[MARKER_ID]["description"]


def test_the_marker_is_read_once_per_run(rig, monkeypatch, gh_out):
    _freeze(monkeypatch)
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=2),
                 last_deep_sweep=NOW - timedelta(hours=1))
    rig["result"] = _result(_openings())
    sync_calendar.main(["--skip-if-checked-within", "50"])
    assert rig["service"].calls.count(("get", MARKER_ID)) == 1


# --------------------------------------------------------------- last_success_age_hours
def test_last_success_age_is_published_before_the_fetch(rig, monkeypatch, gh_out):
    """The workflow reads this on FAILURE to decide whether to open an issue, so it has to
    be out before the step that is most likely to fail."""
    _freeze(monkeypatch)
    _seed_marker(rig, last_run=NOW - timedelta(hours=2, minutes=30))
    seen = {}
    rig["on_fetch"] = lambda: seen.update(_outputs(gh_out))
    rig["result"] = _result(_openings(), ok=False)

    with pytest.raises(SystemExit):
        sync_calendar.main(["--depth", "near"])

    assert seen["last_success_age_hours"] == "2.5"


def test_last_success_age_is_empty_when_unknown(rig, monkeypatch, gh_out):
    _freeze(monkeypatch)
    rig["result"] = _result(_openings())
    sync_calendar.main(["--depth", "near"])
    assert _outputs(gh_out)["last_success_age_hours"] == ""


def test_last_success_age_is_published_on_a_skipped_tick_too(rig, monkeypatch, gh_out):
    _freeze(monkeypatch)
    _seed_marker(rig, last_attempt=NOW - timedelta(minutes=5),
                 last_run=NOW - timedelta(hours=26))
    sync_calendar.main(["--skip-if-checked-within", "50"])
    assert _outputs(gh_out) == {"last_success_age_hours": "26.0", "skipped": "true"}


def test_last_success_age_is_empty_when_last_run_is_in_the_future(rig, monkeypatch, gh_out):
    """A negative age would read as 'fresh' to both the issue opener and the heartbeat;
    only a broken clock or a hand edit produces one, so it is reported as unknown."""
    _freeze(monkeypatch)
    _seed_marker(rig, last_run=NOW + timedelta(hours=2))
    rig["result"] = _result(_openings())
    sync_calendar.main(["--depth", "near"])
    assert _outputs(gh_out)["last_success_age_hours"] == ""


def _hold_15_for_a_stylist_we_cannot_see(rig):
    """A calendar on which every run exits 1: 15 entries for a stylist whose lookups keep
    failing, so their deletes are withheld — over SKIPPED_DELETE_EXIT_FLOOR."""
    cfg = rig["config"]
    for ev in _openings(n=sync_calendar.SKIPPED_DELETE_EXIT_FLOOR + 5, stylist="sick"):
        (entry,) = sync_calendar.build_entries([ev], cfg.event_style)
        body = sync_calendar.entry_body(entry, cfg, "")
        rig["service"].store[body["id"]] = dict(body, status="confirmed")
    rig["result"] = _result(_openings(stylist="175308"), staff_ok={"175308"})
    rig["result"].staff_planned = {"175308", "sick"}


def test_a_failing_run_does_not_advance_the_last_success(rig, monkeypatch, gh_out):
    """main() wrote last_run before deciding to exit 1, so through a persistent failure
    every run published an age of ~1h: the workflow (which opens an issue at 12h) only ever
    warned, and the outage stayed silent."""
    _hold_15_for_a_stylist_we_cannot_see(rig)
    _seed_marker(rig, last_run=NOW - timedelta(hours=11))

    ages = []
    for hour in range(3):                                # three hourly runs, all failing
        _freeze(monkeypatch, NOW + timedelta(hours=hour))
        gh_out.write_text("")
        with pytest.raises(SystemExit) as ei:
            sync_calendar.main(["--depth", "near"])
        assert ei.value.code == 1
        ages.append(_outputs(gh_out)["last_success_age_hours"])

    assert ages == ["11.0", "12.0", "13.0"]


def test_a_skipped_tick_behind_a_failed_run_does_not_look_fresh(rig, monkeypatch, gh_out):
    """The heartbeat pings on a skip only while the last SUCCESS is inside the gate
    window. A failed run that reset last_run let the ticks behind it feed the switch."""
    _hold_15_for_a_stylist_we_cannot_see(rig)
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=1),
                 last_run=NOW - timedelta(hours=30))
    _freeze(monkeypatch)
    with pytest.raises(SystemExit):
        sync_calendar.main(["--skip-if-checked-within", "50"])

    _freeze(monkeypatch, NOW + timedelta(minutes=20))
    gh_out.write_text("")
    sync_calendar.main(["--skip-if-checked-within", "50"])

    out = _outputs(gh_out)
    assert out["skipped"] == "true"
    assert float(out["last_success_age_hours"]) >= 30


def test_a_blast_radius_refusal_does_not_advance_the_last_success(rig, monkeypatch, gh_out):
    cfg = rig["config"] = mkconfig(event_style="slots")
    held = [ev for i in range(20) for ev in _openings(n=10, stylist=f"s{i}", hour=9,
                                                      days_ahead=1 + i % 18)]
    for ev in held:
        body = sync_calendar.entry_body(ev, cfg, "")
        rig["service"].store[body["id"]] = dict(body, status="confirmed")
    ok_at = NOW - timedelta(hours=3)
    _seed_marker(rig, last_run=ok_at)
    _freeze(monkeypatch)
    rig["result"] = _result(held[:50])                   # 'ok', yet 150 of 200 gone

    with pytest.raises(SystemExit) as ei:
        sync_calendar.main(["--depth", "near"])

    assert ei.value.code == 1
    private = rig["service"].live()[MARKER_ID]["extendedProperties"]["private"]
    assert private["last_run"] == ok_at.isoformat()
    assert private["last_attempt"] == NOW.isoformat()


# --------------------------------------------------------------- capture round-trip
def test_the_capture_round_trips_plan_and_window(rig):
    cap = rig["tmp"] / "cap.json"
    original = _result(_openings(), staff_ok={"175308"})
    original.staff_planned = {"175308", "24102"}
    original.window_start = date(2026, 10, 14)
    rig["result"] = original
    sync_calendar.main(["--depth", "near", "--fetch-json", str(cap)])

    raw = json.loads(cap.read_text())
    assert raw["staff_planned"] == ["175308", "24102"]          # sorted, stable diffs
    assert raw["window_start"] == "2026-10-14"
    back = sync_calendar.result_from_json(cap.read_text())
    assert back.staff_planned == original.staff_planned
    assert back.window_start == original.window_start


def test_unknown_plan_and_window_round_trip_as_unknown(rig):
    cap = rig["tmp"] / "cap.json"
    rig["result"] = _result(_openings())
    sync_calendar.main(["--depth", "near", "--fetch-json", str(cap)])
    back = sync_calendar.result_from_json(cap.read_text())
    assert back.staff_planned is None and back.window_start is None


def test_a_capture_from_before_these_fields_still_loads():
    old = json.dumps({"lookahead_days": 21, "ok": True, "notices": "", "lookups_ok": 60,
                      "lookups_failed": 0, "errors": [], "staff_ok": ["175308"],
                      "events": []})
    back = sync_calendar.result_from_json(old)
    assert back.staff_ok == {"175308"}
    assert back.staff_planned is None and back.window_start is None


def test_a_replay_is_judged_against_the_window_it_was_captured_in(rig):
    """A capture fetched yesterday covers yesterday..today+20. Replayed today without its
    window_start, sync() re-anchors on today and deletes everything on today+21 — a day
    the capture never looked at."""
    cfg = rig["config"]
    cap = rig["tmp"] / "cap.json"
    # Sachi is fully covered, so only the window can protect her entry — not a held delete.
    captured = _result(_openings(stylist="175308", days_ahead=3),
                       staff_ok={"175308", "24102"})
    captured.staff_planned = {"175308", "24102"}
    captured.window_start = datetime.now(NY).date() - timedelta(days=1)
    cap.write_text(sync_calendar.result_to_json(captured, lookahead_days=21))
    # 10:00 rather than _openings()' current hour, which would land exactly on the window
    # boundary when the suite runs between midnight and 1am.
    ten = datetime.now(NY).replace(hour=10, minute=0, second=0, microsecond=0)
    unseen = Event(stylist_id="24102", stylist="Sachi", stylist_role="Master Barber",
                   start=ten + timedelta(days=21), book_url="https://book",
                   options=[ServiceOption("Hair Cut", "$75", 55, False)])
    (entry,) = sync_calendar.build_entries([unseen], cfg.event_style)
    body = sync_calendar.entry_body(entry, cfg, "")
    rig["service"].store[body["id"]] = dict(body, status="confirmed")

    sync_calendar.main(["--depth", "near", "--from-json", str(cap)])

    assert body["id"] in rig["service"].live()
