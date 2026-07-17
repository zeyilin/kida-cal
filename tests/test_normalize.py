"""Unit tests for parsing/normalization against real captured fixtures.

Run: .venv/bin/python -m pytest tests/ -q
"""
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from src import timely
from src.fetch_availability import build_datetime
from src.models import Slot, group_slots

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
NY = ZoneInfo("America/New_York")


def _read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


def test_parse_service_catalog():
    services, ssids = timely.parse_service_catalog(_read("service_catalog.html"))
    assert len(services) == 24
    by_id = {s["service_id"]: s for s in services}
    hair_cut = by_id["5319865"]
    assert hair_cut["name"] == "Hair Cut"
    assert hair_cut["bookable_item_id"] == "5319865:SV"
    assert hair_cut["staff_ids"] == ["287218", "367833", "175308"]
    # Every service contributes a ServiceStaffIds hidden field to echo on POST.
    assert any(k.startswith("ServiceStaffIds[5319865:SV]") for k in ssids)


def test_parse_time_slots_populated():
    slots = timely.parse_time_slots(_read("gettimeslots_nao_2026-07-20.html"))
    starts = sorted(s.start_min for s in slots)
    # Whatever the fixture captured (availability drifts as slots get booked): all
    # slots must be daytime, on the hour, and 55-min Hair Cut openings.
    assert starts == [540, 600, 660, 720, 780]  # 9,10,11am,12,1pm at capture time
    assert all(540 <= m <= 1200 for m in starts)
    one = next(s for s in slots if s.start_min == 540)
    assert one.date == "2026-07-20"
    assert one.staff_id == "175308"
    assert one.end_min - one.start_min == 55  # Hair Cut duration


def test_parse_time_slots_empty_day():
    slots = timely.parse_time_slots(_read("gettimeslots_empty_2026-07-22.html"))
    assert slots == []


def test_decode_token_roundtrip():
    slots = timely.parse_time_slots(_read("gettimeslots_nao_2026-07-20.html"))
    rs = slots[0]
    # token must decode back to same fields
    again = timely.decode_booking_selection(rs.token)
    assert again.date == rs.date and again.start_min == rs.start_min


def test_build_datetime_is_tz_aware_and_dst_correct():
    # Summer (EDT, UTC-4)
    summer = build_datetime("2026-07-20", 540, NY)  # 09:00
    assert summer.tzinfo is not None
    assert summer.utcoffset().total_seconds() == -4 * 3600
    assert (summer.hour, summer.minute) == (9, 0)

    # DST fall-back is 2026-11-01. A 09:00 slot that day is EST (UTC-5),
    # while 09:00 the day before is still EDT (UTC-4).
    before = build_datetime("2026-10-31", 540, NY)
    after = build_datetime("2026-11-01", 540, NY)
    assert before.utcoffset().total_seconds() == -4 * 3600
    assert after.utcoffset().total_seconds() == -5 * 3600
    # Same wall-clock hour, different absolute instant → offsets differ by 1h.
    assert after.hour == 9 and before.hour == 9


def test_group_slots_merges_overlapping_services():
    start = build_datetime("2026-07-20", 540, NY)
    end = build_datetime("2026-07-20", 595, NY)
    end_long = build_datetime("2026-07-20", 625, NY)
    common = dict(stylist_id="175308", stylist="Nao", start=start,
                  deposit_required=False, book_url="u")
    s1 = Slot(service_id="5319865", service="Hair Cut", end=end, duration_min=55,
              price_display="Varies", **common)
    s2 = Slot(service_id="3142646", service="Hair Cut & Beard Trim", end=end_long,
              duration_min=85, price_display="$95", **common)
    events = group_slots([s1, s2])
    assert len(events) == 1
    ev = events[0]
    assert set(ev.services) == {"Hair Cut", "Hair Cut & Beard Trim"}
    assert ev.end == end_long          # longest service wins the end time
    assert ev.summary().startswith("OPEN · ")
    # Deterministic id is stable and hex.
    assert ev.google_event_id() == events[0].google_event_id()
    assert ev.google_event_id().startswith("kida")


def test_group_slots_distinct_starts_stay_separate():
    a = build_datetime("2026-07-20", 540, NY)
    b = build_datetime("2026-07-20", 600, NY)
    mk = lambda start, end: Slot(stylist_id="175308", stylist="Nao", service_id="5319865",
                                 service="Hair Cut", start=start, end=end, duration_min=55,
                                 price_display="Varies", deposit_required=False, book_url="u")
    events = group_slots([mk(a, a), mk(b, b)])
    assert len(events) == 2
