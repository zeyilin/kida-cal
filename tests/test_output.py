"""Tests for .ics rendering (RFC 5545 conformance and the folding/escaping rules)."""
from datetime import datetime
from zoneinfo import ZoneInfo

from src import ics_export
from src.fetch_availability import build_datetime
from src.models import Slot, build_entries, group_slots

NY = ZoneInfo("America/New_York")
CHECKED = datetime(2026, 7, 17, 12, 0, tzinfo=NY)


def _slot(service, start_min, end_min, price="Varies", staff="175308", name="Nao"):
    return Slot(stylist_id=staff, stylist=name, service_id="x", service=service,
                start=build_datetime("2026-07-20", start_min, NY),
                end=build_datetime("2026-07-20", end_min, NY),
                duration_min=end_min - start_min, price_display=price,
                deposit_required=False, book_url="https://book.example/a?b=1,2;3")


def _render(style="slots", slots=None):
    slots = slots or [_slot("Hair Cut", 540, 595), _slot("Hair Cut & Beard Trim", 540, 625)]
    entries = build_entries(group_slots(slots), style)
    return ics_export.render(entries, "Closed July 4", CHECKED)


def test_ics_render_is_wellformed_and_free_with_no_alarm():
    ics = _render()
    assert ics.startswith("BEGIN:VCALENDAR")
    assert ics.rstrip().endswith("END:VCALENDAR")
    assert ics.count("BEGIN:VEVENT") == 1                 # dedup → one entry
    assert ics.count("BEGIN:VEVENT") == ics.count("END:VEVENT")
    assert "TRANSP:TRANSPARENT" in ics                    # Free
    assert "BEGIN:VALARM" not in ics                      # no notifications
    assert "SUMMARY:Nao · Hair Cut" in ics
    assert "DTSTART:20260720T130000Z" in ics              # 09:00 EDT == 13:00 UTC
    assert "DTEND:20260720T135500Z" in ics                # titled service: 55 min, not 85
    assert "\r\n" in ics                                  # CRLF line endings


def test_ics_has_no_method_property():
    """METHOD makes this an iTIP message, and clients then use SEQUENCE to decide whether
    a re-fetch supersedes their copy. This feed is full-state and carries no SEQUENCE, so
    advertising METHOD invited clients to sit on a stale copy under a stable UID."""
    ics = _render()
    assert "METHOD:" not in ics
    assert "\r\nSEQUENCE" not in ics


def test_lines_are_folded_on_octets_not_characters():
    """Summaries carry '·' (2 bytes) and the calendar name an em dash, so counting
    characters lets a line exceed 75 octets — and can split a character across the fold."""
    long_name = "Bartholomew Fitzgerald-Montgomery"
    slots = [_slot(f"Deluxe Signature Hair Cut and Beard Sculpt with Hot Towel {i}",
                   540, 595, name=long_name) for i in range(3)]
    ics = ics_export.render(build_entries(group_slots(slots), "slots"),
                            "A" * 400, CHECKED)
    for line in ics.split("\r\n"):
        assert len(line.encode("utf-8")) <= 75, f"line exceeds 75 octets: {line!r}"
    # Folding must not corrupt the content: unfolding restores valid UTF-8 and the text.
    unfolded = ics.replace("\r\n ", "")
    assert long_name in unfolded
    assert "·" in unfolded


def test_url_value_is_not_text_escaped():
    """URL takes a URI value, not TEXT, so its commas and semicolons must stay literal."""
    ics = _render()
    unfolded = ics.replace("\r\n ", "")
    assert "URL:https://book.example/a?b=1,2;3" in unfolded
    # ...while genuinely TEXT-valued properties still are escaped.
    assert "DESCRIPTION:" in ics


def test_description_is_escaped_and_carries_the_honest_caveats():
    ics = _render()
    unfolded = ics.replace("\r\n ", "")
    assert "\\n" in unfolded                     # newlines escaped, not literal
    assert "Salon notice: Closed July 4" in unfolded
    assert "Prices as of" in unfolded
    assert "confirm on KIDA's site" in unfolded


def test_blocks_style_renders_one_vevent_per_block():
    slots = [_slot("Hair Cut", m, m + 55) for m in (540, 600, 660)]
    ics = _render(style="blocks", slots=slots)
    assert ics.count("BEGIN:VEVENT") == 1
    unfolded = ics.replace("\r\n ", "")
    assert "SUMMARY:Nao · 3 open" in unfolded
    assert "DTSTART:20260720T130000Z" in unfolded   # 9:00 am
    assert "DTEND:20260720T155500Z" in unfolded     # through the 11:00 opening


def test_uid_matches_the_calendar_event_id():
    """The .ics and the Google Calendar must agree on identity, or a subscriber who has
    both sees every opening twice."""
    entries = build_entries(group_slots([_slot("Hair Cut", 540, 595)]), "slots")
    ics = ics_export.render(entries, "", CHECKED)
    assert f"UID:{entries[0].google_event_id()}@kida-cal" in ics


def test_empty_feed_is_still_valid():
    ics = ics_export.render([], "", CHECKED)
    assert ics.startswith("BEGIN:VCALENDAR")
    assert "BEGIN:VEVENT" not in ics
    assert ics.rstrip().endswith("END:VCALENDAR")


def test_notices_none_omits_the_notice_line():
    entries = build_entries(group_slots([_slot("Hair Cut", 540, 595)]), "slots")
    ics = ics_export.render(entries, None, CHECKED)
    assert "Salon notice" not in ics
