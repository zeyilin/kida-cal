"""What the owner-facing docs promise, exercised against the code that keeps the promise.

config.yaml and docs/compliance.md are where an owner learns what a setting does and what
load Timely sees. Each claim below was once written from how the code was meant to work,
and was wrong: narrowing `stylists` to one name was said to delete the others' entries
(it froze every delete instead), a throttled or timed-out run was said to keep the ticks
behind it off Timely (not once it has run 50+ minutes), and a failed start stamp was said
to leave the next tick ungated (the end-of-run marker rewrite records it anyway). Each
test pins the claim as the doc now states it, so a code change that falsifies it turns
red here rather than in production.
"""
from __future__ import annotations

import re
from datetime import datetime, timedelta
from pathlib import Path
from zoneinfo import ZoneInfo

import pytest

from conftest import mkconfig
from fakes import FakeCalendarService, frozen_datetime, http_error
from src import fetch_availability, sync_calendar, timely
from src.fetch_availability import FetchResult
from src.models import MARKER_ID, Event, ServiceOption
# The gate and stamp claims are about main(); test_main's rig drives it against a fake
# calendar with a scripted fetch. Importing the fixtures is what makes them available here.
from test_main import (NOW, _freeze, _openings, _outputs, _result, _seed_marker,
                       gh_out, rig)  # noqa: F401

NY = ZoneInfo("America/New_York")


# ------------------------------------------------------------------ config.yaml: stylists

T0 = datetime(2026, 10, 14, 11, 30, tzinfo=NY)       # a Wednesday, mid-morning
# The nine in src/catalog.py STAFF, by the ids these tests stamp on their entries.
STAFF = ("masa", "satomi", "nao", "sachi", "taka", "yohei", "fausto", "shin", "hiroki")
DAYS = range(1, 21)                                    # inside even the near window


def _opening(day: int, hour: int, stylist: str) -> Event:
    return Event(stylist_id=stylist, stylist=stylist.title(), stylist_role="Stylist",
                 start=T0.replace(hour=hour, minute=0) + timedelta(days=day),
                 book_url="https://book",
                 options=[ServiceOption("Hair Cut", "$75", 55, False)])


def _block(day: int, stylist: str, hours=(10, 11, 12)) -> list[Event]:
    return [_opening(day, h, stylist) for h in hours]


def _salon(cfg):
    """A calendar shaped like the real one: all nine stylists, a morning block each on
    each of 20 days (540 openings, well over the guard's 150-opening floor), plus one
    afternoon block of Nao's that has since been BOOKED — it is on the calendar and no
    longer in Timely's feed, so it must go. Returns the service, the booked block's id,
    and the feed a run that looks only for `planned` stylists would get back."""
    groups = [_block(d, s) for s in STAFF for d in DAYS]
    booked = _block(5, "nao", hours=(15, 16))
    store = []
    for g in groups + [booked]:
        (entry,) = sync_calendar.build_entries(g, cfg.event_style)
        store.append(dict(sync_calendar.entry_body(entry, cfg, ""), status="confirmed"))
    (booked_entry,) = sync_calendar.build_entries(booked, cfg.event_style)

    def feed(planned: set[str]) -> FetchResult:
        events = [ev for g in groups for ev in g if ev.stylist_id in planned]
        return FetchResult(slots=[], events=events, notices="", ok=True, lookups_ok=60,
                           lookups_failed=0, lookups_expected=60, staff_ok=set(planned),
                           staff_planned=set(planned), window_start=T0.date())

    return FakeCalendarService(events=store), booked_entry.google_event_id(), feed


@pytest.fixture
def at_t0(monkeypatch):
    monkeypatch.setattr(sync_calendar, "datetime", frozen_datetime(T0))


def _live_of(svc, stylist: str) -> list[str]:
    return [eid for eid, item in svc.live().items()
            if sync_calendar._stylist_of(item) == stylist]


def test_leaving_one_stylist_out_deletes_their_entries(at_t0):
    """config.yaml: 'a stylist the run never looks for is treated like one who has left the
    salon, so their existing entries count as stale and are deleted on the next run.'"""
    cfg = mkconfig(event_style="blocks", stylists=[s for s in STAFF if s != "hiroki"])
    svc, booked, feed = _salon(cfg)
    stats = sync_calendar.sync(cfg, feed(set(STAFF) - {"hiroki"}), service=svc)

    assert stats["blocked_delete"] == 0
    assert _live_of(svc, "hiroki") == []
    assert booked not in svc.live()


@pytest.mark.parametrize("stylists, kept", [
    ("Nao", {"nao"}),                                            # 'one name'
    (["Masa", "Satomi", "Nao"], {"masa", "satomi", "nao"}),      # 'just Masa, Satomi and Nao'
])
def test_leaving_most_stylists_out_freezes_every_delete(at_t0, stylists, kept):
    """config.yaml: 'a refused run deletes NOTHING — not even the booked openings of the
    stylists you kept, which stay advertised.' Either example on the real nine-stylist
    calendar makes most entries stale; the comment used to promise they would just go."""
    cfg = mkconfig(event_style="blocks", stylists=stylists)
    svc, booked, feed = _salon(cfg)
    stats = sync_calendar.sync(cfg, feed(kept), service=svc)

    assert stats["blocked_delete"] > 0 and stats["delete"] == 0
    assert len(_live_of(svc, "hiroki")) == len(DAYS)
    assert booked in svc.live()                  # already taken, still advertised


def test_the_one_off_allow_mass_delete_run_clears_them(at_t0):
    """config.yaml: '...until a one-off local `--depth full --allow-mass-delete` clears
    their entries'."""
    cfg = mkconfig(event_style="blocks", stylists="Nao")
    svc, booked, feed = _salon(cfg)
    stats = sync_calendar.sync(cfg, feed({"nao"}), service=svc, allow_mass_delete=True)

    assert stats["blocked_delete"] == 0
    assert {sync_calendar._stylist_of(i) for i in svc.live().values()} == {"nao"}
    assert booked not in svc.live()


def test_a_frozen_delete_pass_fails_every_run(rig, monkeypatch, gh_out):
    """config.yaml: '...and exits 1 ... trips it on every run'. Two production ticks, six
    hours apart, through main(): both refuse, both exit 1 — nothing converges by itself."""
    cfg = mkconfig(event_style="blocks", stylists="Nao")
    svc, booked, feed = _salon(cfg)
    rig.update(service=svc, config=cfg, result=feed({"nao"}))
    for hours in (0, 6):
        monkeypatch.setattr(sync_calendar, "datetime",
                            frozen_datetime(T0 + timedelta(hours=hours)))
        with pytest.raises(SystemExit) as ei:
            sync_calendar.main(["--depth", "full"])
        assert ei.value.code == 1
    assert booked in svc.live()


# ------------------------------------------------------------------ config.yaml: depth

def _marker(**hours_ago) -> dict:
    """A status marker whose stamps are `hours_ago` before T0."""
    return {"id": MARKER_ID, "status": "confirmed", "summary": "checked",
            "start": {"date": T0.date().isoformat()},
            "end": {"date": (T0.date() + timedelta(days=1)).isoformat()},
            "extendedProperties": {"private": {
                k: (T0 - timedelta(hours=h)).isoformat() for k, h in hours_ago.items()}}}


@pytest.mark.parametrize("stamps, deep", [
    # '...it goes deep once the last deep sweep is older than deep_sweep_every_hours'
    (dict(last_deep_sweep=3, last_attempt=1), False),
    (dict(last_deep_sweep=7, last_attempt=1), True),
    # 'One exception: straight after a deep sweep that did not complete ... the next run
    # sweeps near ... and the one after retries deep.' It used to say 'whenever'.
    (dict(last_deep_sweep=7, last_attempt=1, last_deep_attempt=1), False),
    (dict(last_deep_sweep=8, last_attempt=1, last_deep_attempt=2), True),
    # 'That holds only until the last clean deep sweep is 12h old.'
    (dict(last_deep_sweep=12, last_attempt=1, last_deep_attempt=1), True),
])
def test_auto_depth_goes_deep_once_due_with_one_exception(stamps, deep):
    cfg = mkconfig()
    assert cfg.deep_sweep_every_hours == 6
    days, got = sync_calendar.resolve_depth(cfg, "auto", _marker(**stamps), now=T0)
    assert (days, got) == ((cfg.lookahead_days, True) if deep
                           else (cfg.near_lookahead_days, False))


# ------------------------------------------------------------------ compliance.md: the gate

def _tick(monkeypatch, gh_out, at, *argv) -> tuple[int, dict[str, str]]:
    """One scheduled tick at `at` with the gate the workflow passes."""
    _freeze(monkeypatch, at)
    gh_out.write_text("", encoding="utf-8")
    try:
        sync_calendar.main(["--depth", "auto", "--skip-if-checked-within", "50", *argv])
        code = 0
    except SystemExit as e:
        code = e.code
    return code, _outputs(gh_out)


@pytest.mark.parametrize("lasted, skipped", [
    (timedelta(minutes=20), "true"),     # a throttle abort 20 min in: the tick behind waits
    (timedelta(minutes=55), "false"),    # a throttled deep sweep that ran long
    (timedelta(minutes=120), "false"),   # cut off by timeout-minutes: 120
])
def test_the_gate_bounds_sweep_starts_not_the_rest_between_sweeps(
        rig, monkeypatch, gh_out, lasted, skipped):
    """compliance.md: 'Start-to-start bounds how often a sweep starts, not the rest Timely
    gets between sweeps. A run that itself lasts 50 minutes or more ... can be followed at
    once by the run queued behind it.' It used to say a burst of ticks after a throttled
    or timed-out run 'cannot pile straight back onto Timely'. The queued tick starts the
    moment the failed run ends — `lasted` after that run stamped its start."""
    _seed_marker(rig, last_run=NOW - timedelta(hours=5), last_attempt=NOW - lasted,
                 last_deep_sweep=NOW - timedelta(hours=1))
    rig["result"] = _result(_openings())
    code, out = _tick(monkeypatch, gh_out, NOW)
    assert (code, out["skipped"]) == (0, skipped)
    assert rig["fetches"] == (skipped == "false")


def _flaky_stamp(rig):
    rig["service"].fail_plan = {("patch", MARKER_ID): [
        http_error(500, "Backend Error", "backendError")]}


def test_a_transient_stamp_failure_still_gates_the_next_tick(rig, monkeypatch, gh_out):
    """compliance.md: 'Its start is still recorded when the marker is rewritten at the end,
    so the next tick is gated as usual.' It used to say the next tick is not skipped."""
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=2), last_run=NOW - timedelta(hours=2),
                 last_deep_sweep=NOW - timedelta(hours=1))
    _flaky_stamp(rig)
    rig["result"] = _result(_openings())
    code, out = _tick(monkeypatch, gh_out, NOW)
    assert (code, out["skipped"], rig["fetches"]) == (0, "false", 1)    # it carried on

    code, out = _tick(monkeypatch, gh_out, NOW + timedelta(minutes=15))
    assert (code, out["skipped"], rig["fetches"]) == (0, "true", 1)


def test_a_transient_stamp_failure_then_a_dead_run_leaves_the_next_tick_ungated(
        rig, monkeypatch, gh_out):
    """compliance.md: '...unless the run dies before that rewrite (an untrusted or
    throttled fetch, ...), which leaves the next tick ungated.'"""
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=2), last_run=NOW - timedelta(hours=2),
                 last_deep_sweep=NOW - timedelta(hours=1))
    _flaky_stamp(rig)
    rig["result"] = _result(_openings(), ok=False)          # e.g. a throttle abort
    code, _ = _tick(monkeypatch, gh_out, NOW)
    assert code == 2

    code, out = _tick(monkeypatch, gh_out, NOW + timedelta(minutes=15))
    assert out["skipped"] == "false" and rig["fetches"] == 2


def test_a_refused_stamp_stops_the_run_before_timely(rig, monkeypatch, gh_out):
    """compliance.md: 'A refusal every write would hit (a read-only share, an exhausted
    quota) is not transient: it stops the run before any Timely request.' It used to say
    the run carries on."""
    _seed_marker(rig, last_attempt=NOW - timedelta(hours=2), last_run=NOW - timedelta(hours=2),
                 last_deep_sweep=NOW - timedelta(hours=1))
    rig["service"].fail_plan = {("patch", MARKER_ID): [http_error(403, "Forbidden",
                                                                  "forbidden")]}
    rig["result"] = _result(_openings())
    code, _ = _tick(monkeypatch, gh_out, NOW)
    assert (code, rig["fetches"]) == (1, 0)


# ------------------------------------------------------- compliance.md: the stop conditions

def test_every_constant_compliance_md_names_exists_with_the_value_it_quotes():
    """compliance.md lists the stop conditions by constant name and value. It went on
    describing MAX_CONSECUTIVE_UNANSWERED_REQUESTS (3) and MIN_UNANSWERED_STREAK_SECONDS
    (5 minutes) after src/timely.py had dropped both, so this pins each name it quotes to a
    constant in src/ and each `NAME` (N) to that constant's value."""
    text = (Path(__file__).resolve().parent.parent / "docs" / "compliance.md").read_text(
        encoding="utf-8")
    modules = (timely, fetch_availability, sync_calendar)
    names = set(re.findall(r"`([A-Z][A-Z0-9_]{3,})`", text)) - {
        "KIDA_SERVICE_ACCOUNT_JSON", "KIDA_CALENDAR_ID", "KIDA_HEARTBEAT_URL", "GITHUB_TOKEN"}
    assert names, "compliance.md names no constants; this test is looking in the wrong place"
    for name in sorted(names):
        assert any(hasattr(m, name) for m in modules), f"compliance.md names {name}, gone"
    for name, number, minutes in re.findall(
            r"`([A-Z][A-Z0-9_]{3,})` \((\d+)( minutes)?\)", text):
        actual = next(getattr(m, name) for m in modules if hasattr(m, name))
        assert actual == int(number) * (60 if minutes else 1), (name, number, actual)
