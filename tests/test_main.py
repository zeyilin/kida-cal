"""Tests for sync_calendar.main() — the decision layer.

main() had zero coverage while owning the decisions with the largest blast radius: whether
an untrusted fetch is allowed to write, what the process exits with, whether the published
.ics is replaced, and whether a run gets to claim it performed a deep sweep. Every one of
those is reachable here through the `argv` seam plus a fake calendar service.
"""
import json
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import pytest

from conftest import mkconfig
from fakes import FakeCalendarService
from src import sync_calendar
from src.fetch_availability import FetchResult
from src.models import MARKER_ID, Event, ServiceOption

NY = ZoneInfo("America/New_York")


def _openings(n=4, stylist="175308", days_ahead=3):
    base = datetime.now(NY).replace(minute=0, second=0, microsecond=0)
    return [Event(stylist_id=stylist, stylist="Nao", stylist_role="Stylist",
                  start=base + timedelta(days=days_ahead, hours=h),
                  book_url="https://book",
                  options=[ServiceOption("Hair Cut", "$75", 55, False)])
            for h in range(n)]


@pytest.fixture
def rig(monkeypatch, tmp_path):
    """Drive main() against a fake calendar with a scripted fetch."""
    svc = FakeCalendarService()
    state = {"service": svc, "result": None, "config": mkconfig()}

    monkeypatch.setattr(sync_calendar, "get_service", lambda: state["service"])
    monkeypatch.setattr(sync_calendar, "preflight", lambda *a, **k: None)
    monkeypatch.setattr(sync_calendar.Config, "load", classmethod(
        lambda cls, path="config.yaml": state["config"]))
    monkeypatch.setattr(sync_calendar, "fetch", lambda cfg, **k: state["result"])
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
