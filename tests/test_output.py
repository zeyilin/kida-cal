"""Tests for ICS rendering and the calendar-sync safety guard (no network)."""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import ics_export, sync_calendar
from src.fetch_availability import Config, FetchResult
from src.models import Slot, group_slots

NY = ZoneInfo("America/New_York")


def _events():
    a = datetime(2026, 7, 20, 9, 0, tzinfo=NY)
    slots = [
        Slot("175308", "Nao", "5319865", "Hair Cut", a,
             datetime(2026, 7, 20, 9, 55, tzinfo=NY), 55, "Varies (from $75)", True, "https://x"),
        Slot("175308", "Nao", "3142646", "Hair Cut & Beard Trim", a,
             datetime(2026, 7, 20, 10, 25, tzinfo=NY), 85, "$95", False, "https://x"),
    ]
    return group_slots(slots)


def test_ics_render_is_wellformed_and_free_no_alarm():
    evs = _events()
    ics = ics_export.render(evs, "Closed July 4", datetime(2026, 7, 17, 12, 0, tzinfo=NY))
    assert ics.startswith("BEGIN:VCALENDAR")
    assert ics.rstrip().endswith("END:VCALENDAR")
    assert ics.count("BEGIN:VEVENT") == 1                 # dedup → one event
    assert "TRANSP:TRANSPARENT" in ics                    # Free
    assert "BEGIN:VALARM" not in ics                      # no notifications
    assert "SUMMARY:OPEN · Hair Cut w/ Nao (Stylist)" in ics
    assert "DTSTART:20260720T130000Z" in ics              # 09:00 EDT == 13:00 UTC
    assert "\r\n" in ics                                  # CRLF line endings


def test_sync_skips_deletes_when_fetch_failed():
    """The core safety property: a failed fetch must never delete existing events."""
    cfg = Config.load(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))

    class FakeEvents:
        def __init__(self): self.deleted = []
        def list(self, **k):
            class R:
                def execute(self_):
                    return {"items": [
                        {"id": "kidaDEADBEEF", "summary": "OPEN · old",
                         "start": {"dateTime": "2026-07-20T09:00:00-04:00"},
                         "end": {"dateTime": "2026-07-20T09:55:00-04:00"}}]}
            return R()
        def delete(self, **k):
            self.deleted.append(k.get("eventId"))
            class R:
                def execute(self_): return {}
            return R()

    class FakeService:
        def __init__(self): self._ev = FakeEvents()
        def events(self): return self._ev
    svc = FakeService()

    # Fetch clearly failed → no events, ok=False. Existing 'kidaDEADBEEF' is stale.
    bad = FetchResult(slots=[], events=[], notices="", ok=False, lookups_ok=0, lookups_failed=3)
    cfg.calendar_id = "cal123"        # skip calendar creation path
    stats = sync_calendar.sync(cfg, bad, service=svc, dry_run=False)
    assert stats["delete"] == 0
    assert stats["skipped_delete"] == 1
    assert svc._ev.deleted == []       # nothing actually deleted


def test_window_scoped_delete_protects_far_out_events():
    """A short near-term run must delete stale in-window events but NOT the deep run's
    far-out events (so the hourly + 6-hourly sweeps can share one calendar)."""
    from datetime import timedelta
    cfg = Config.load(os.path.join(os.path.dirname(__file__), "..", "config.yaml"))
    cfg.calendar_id = "cal123"
    cfg.lookahead_days = 21
    now = datetime.now(NY)
    near = (now + timedelta(days=5)).isoformat()      # inside the 21-day window
    far = (now + timedelta(days=40)).isoformat()      # beyond it (deep run's territory)

    class FakeEvents:
        def __init__(self): self.deleted = []
        def list(self, **k):
            class R:
                def execute(self_):
                    return {"items": [
                        {"id": "kidaNEAR", "summary": "OPEN · near",
                         "start": {"dateTime": near}, "end": {"dateTime": near}},
                        {"id": "kidaFAR", "summary": "OPEN · far",
                         "start": {"dateTime": far}, "end": {"dateTime": far}}]}
            return R()
        def delete(self, **k):
            self.deleted.append(k.get("eventId"))
            class R:
                def execute(self_): return {}
            return R()

    class FakeService:
        def __init__(self): self._ev = FakeEvents()
        def events(self): return self._ev
    svc = FakeService()

    # Successful fetch with NO events (everything "booked"). Only the in-window one
    # should be pruned; the far-out one is left for the deep sweep.
    good = FetchResult(slots=[], events=[], notices="", ok=True, lookups_ok=5, lookups_failed=0)
    stats = sync_calendar.sync(cfg, good, service=svc, dry_run=False)
    assert svc._ev.deleted == ["kidaNEAR"]
    assert stats["delete"] == 1
