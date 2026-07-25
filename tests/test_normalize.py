"""Unit tests for parsing/normalization against real captured fixtures.

Run: .venv/bin/python -m pytest tests/ -q
"""
import os
from datetime import datetime
from zoneinfo import ZoneInfo

import pytest

from src import catalog, timely
from src.fetch_availability import build_datetime
from src.models import (Event, ServiceOption, Slot, build_entries, group_blocks,
                        group_slots)

FIX = os.path.join(os.path.dirname(__file__), "fixtures")
NY = ZoneInfo("America/New_York")


def _read(name):
    with open(os.path.join(FIX, name), encoding="utf-8") as f:
        return f.read()


# --------------------------------------------------------------- funnel parsing
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
    """A genuinely fully-booked day must still parse as 'no slots', not as an error."""
    assert timely.parse_time_slots(_read("gettimeslots_empty_2026-07-22.html")) == []


@pytest.mark.parametrize("body,label", [
    ("", "empty body"),
    ("<html><body>Please sign in</body></html>", "login interstitial"),
    ("<div class='slots'></div>", "markup changed"),
])
def test_parse_time_slots_rejects_unrecognized_shape(body, label):
    """The silent-wipe guard: if Timely changes this partial, every regex misses and we
    would return [] — indistinguishable from a fully-booked day, after which the sync
    happily deletes real availability."""
    with pytest.raises(timely.TimelyError):
        timely.parse_time_slots(body)


def test_decode_token_roundtrip():
    slots = timely.parse_time_slots(_read("gettimeslots_nao_2026-07-20.html"))
    rs = slots[0]
    again = timely.decode_booking_selection(rs.token)
    assert again.date == rs.date and again.start_min == rs.start_min


# --------------------------------------------------------------- time handling
def test_build_datetime_is_tz_aware_and_unambiguous_dst():
    """Named for what it actually checks. It only covers unambiguous wall-clock times —
    it does NOT exercise the repeated 1-2am hour on the fall-back date. Measured exposure
    to that gap is nil (every real slot observed falls between 09:00 and 17:00), so the
    fold-handling it would take is not worth carrying."""
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
    assert after.hour == 9 and before.hour == 9


# --------------------------------------------------------------- grouping
def _slot(service, end_min, price, start_min=540, day="2026-07-20", staff="175308"):
    return Slot(stylist_id=staff, stylist="Nao", service_id="x", service=service,
                start=build_datetime(day, start_min, NY),
                end=build_datetime(day, end_min, NY),
                duration_min=end_min - start_min, price_display=price,
                deposit_required=False, book_url="u")


def test_titled_service_decides_the_block_length():
    """Regression for a real defect: the event ran as long as the LONGEST bookable service
    while the title named the SHORTEST, so a 55-minute haircut rendered as a multi-hour
    block and anyone scanning for a one-hour gap saw none."""
    events = group_slots([
        _slot("Hair Cut", 595, "Varies"),                 # 55 min
        _slot("Hair Cut & Beard Trim", 625, "$95"),       # 85 min
        _slot("Color", 780, "$200"),                      # 4 hours
    ])
    assert len(events) == 1
    ev = events[0]
    assert ev.primary_service() == "Hair Cut"
    assert ev.duration_min == 55                       # titled service, not the longest
    assert ev.end == build_datetime("2026-07-20", 595, NY)
    # The longer options are still discoverable, just not by stretching the block.
    assert ev.longest_end == build_datetime("2026-07-20", 780, NY)
    assert set(ev.services) == {"Hair Cut", "Hair Cut & Beard Trim", "Color"}


def test_description_pairs_each_service_with_its_own_price():
    """The old format kept services and a de-duplicated price list in parallel, so a
    stylist with 9 services and 6 distinct prices rendered them unpaired."""
    ev = group_slots([
        _slot("Hair Cut", 595, "$75"),
        _slot("Beard Trim", 570, "$40"),
    ])[0]
    body = "\n".join(ev.description_lines())
    assert "Hair Cut (55m) — $75" in body
    assert "Beard Trim (30m) — $40" in body


def test_title_leads_with_the_discriminator():
    """Every title used to begin 'OPEN · ', so the first 15 characters — all a truncated
    calendar chip shows — were identical across the entire calendar."""
    ev = Event(stylist_id="24102", stylist="Sachi", stylist_role="Master Barber",
               start=datetime(2026, 8, 1, 15, 0, tzinfo=NY),
               options=[ServiceOption(n, "", 30, False) for n in
                        ["Beard Shave (Razor)", "Beard Trim", "Buzz", "Haircut",
                         "Portion Haircut"]])
    assert ev.primary_service() == "Haircut"
    assert ev.summary() == "Sachi · Haircut"
    assert not ev.summary().startswith("OPEN")
    # A slot that genuinely only fits beard services still titles honestly.
    ev2 = Event(stylist_id="24102", stylist="Sachi", stylist_role="Master Barber",
                start=ev.start,
                options=[ServiceOption("Beard Trim", "", 30, False)])
    assert "Haircut" not in ev2.summary()


def test_group_slots_distinct_starts_stay_separate():
    events = group_slots([_slot("Hair Cut", 595, "x", start_min=540),
                          _slot("Hair Cut", 655, "x", start_min=600)])
    assert len(events) == 2


# --------------------------------------------------------------- blocks
def test_blocks_merge_contiguous_openings():
    """Hourly openings with 55-minute services leave a 5-minute gap; that is one
    continuous stretch of availability, not four separate events."""
    events = group_slots([_slot("Hair Cut", m + 55, "$75", start_min=m)
                          for m in (540, 600, 660, 720)])
    blocks = group_blocks(events)
    assert len(blocks) == 1
    b = blocks[0]
    assert len(b.openings) == 4
    assert b.start == build_datetime("2026-07-20", 540, NY)
    assert b.end == build_datetime("2026-07-20", 775, NY)
    assert b.summary() == "Nao · 4 open"
    body = "\n".join(b.description_lines())
    # Each service is listed with the starts where THAT service is bookable, rather than a
    # flat list of times above a union of services (a mostly-false cross-product).
    assert "Bookable start times:" in body
    assert "Hair Cut (55m) — $75: 9:00 AM, 10:00 AM, 11:00 AM, 12:00 PM" in body


def test_blocks_split_on_a_real_gap():
    events = group_slots([_slot("Hair Cut", m + 55, "$75", start_min=m)
                          for m in (540, 600, 960, 1020)])   # morning, then 4pm
    blocks = group_blocks(events)
    assert len(blocks) == 2
    assert [len(b.openings) for b in blocks] == [2, 2]


def test_blocks_split_per_stylist_and_per_day():
    events = group_slots(
        [_slot("Hair Cut", 595, "$75", staff="175308")]
        + [_slot("Hair Cut", 595, "$75", staff="24102")]
        + [_slot("Hair Cut", 595, "$75", day="2026-07-21")])
    assert len(group_blocks(events)) == 3


def test_block_id_is_stable_and_cannot_collide_with_a_slot_id():
    events = group_slots([_slot("Hair Cut", m + 55, "$75", start_min=m)
                          for m in (540, 600)])
    block = group_blocks(events)[0]
    assert block.google_event_id() == group_blocks(events)[0].google_event_id()
    assert block.google_event_id().startswith("kidav")
    # sha1 hex is [0-9a-f], so a legacy id can never begin 'kidav'.
    assert not events[0].google_event_id().startswith("kidav")


def test_build_entries_switches_style_and_rejects_nonsense():
    events = group_slots([_slot("Hair Cut", m + 55, "$75", start_min=m)
                          for m in (540, 600)])
    assert len(build_entries(events, "slots")) == 2
    assert len(build_entries(events, "blocks")) == 1
    with pytest.raises(ValueError):
        build_entries(events, "chunks")


# --------------------------------------------------------------- catalog drift
def test_catalog_covers_every_id_in_the_captured_fixture():
    """Unknown ids used to reach the public calendar as 'Staff 991234' with no price,
    announced only by one stderr line inside an 85-minute log."""
    services, _ = timely.parse_service_catalog(_read("service_catalog.html"))
    missing_services = [s["service_id"] for s in services
                        if s["service_id"] not in catalog.SERVICES]
    assert missing_services == []
    staff_ids = {sid for s in services for sid in s["staff_ids"]}
    assert [s for s in sorted(staff_ids) if not catalog.staff_known(s)] == []
    # Every named person needs a role, or the description renders a blank line.
    assert set(catalog.STAFF) == set(catalog.STAFF_ROLE)


def test_unknown_staff_is_reported_as_unknown():
    assert catalog.staff_known("175308")
    assert not catalog.staff_known("999999")
