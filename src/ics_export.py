"""
Secondary output: write open slots to an .ics file (RFC 5545).

Useful for Apple Calendar / Outlook and as the public GitHub Pages feed. The Google
Calendar API path (src/sync_calendar.py) is the primary output because Google re-fetches
subscribed ICS feeds only every several hours — too slow for salon slots that vanish
within hours.

Entries are VEVENTs marked TRANSP:TRANSPARENT (Free) with no VALARM (no alerts).

Note there is deliberately no METHOD property. METHOD makes a calendar an iTIP *message*
(an invitation/update to be applied), and clients then lean on SEQUENCE to decide whether
a re-fetched VEVENT supersedes the copy they hold. This feed is a full-state document
republished in its entirety, and it carries no SEQUENCE, so advertising METHOD:PUBLISH
invited clients to keep a stale copy under a stable UID.
"""
from __future__ import annotations

from datetime import datetime, timezone

from .catalog import PRICES_AS_OF

PRODID = "-//kida-cal//KIDA NYC Open Slots//EN"


def _fold(line: str) -> str:
    """RFC 5545 line folding at 75 OCTETS.

    Folding on characters is wrong for this feed: summaries contain '·' (2 bytes in UTF-8),
    names like 'Taka (Aki)' and the calendar name carries an em dash, so a
    character-counted line can exceed 75 octets and a multi-byte character can be split
    across the fold. We measure in bytes and never break inside a character.
    """
    raw = line.encode("utf-8")
    if len(raw) <= 75:
        return line
    out, start, limit = [], 0, 75
    while start < len(raw):
        end = min(start + limit, len(raw))
        # Back off until we are on a UTF-8 character boundary (continuation bytes are 10xxxxxx).
        while end < len(raw) and (raw[end] & 0xC0) == 0x80:
            end -= 1
        out.append(raw[start:end].decode("utf-8"))
        start = end
        limit = 74          # continuation lines are prefixed with a single space
    return "\r\n ".join(out)


def _esc(text: str) -> str:
    """Escape a TEXT-valued property. Not for URI values — those are not TEXT and must
    not have their commas or semicolons backslash-escaped."""
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def entry_description(entry, notices: str | None) -> str:
    """The shared description body for a calendar event and its .ics twin."""
    lines = list(entry.description_lines())
    lines.append("")
    lines.append(f"Book: {entry.book_url}")
    if notices:
        lines.append(f"Salon notice: {notices}")
    lines.append(f"Prices as of {PRICES_AS_OF}; confirm on KIDA's site.")
    lines.append("Snapshot of open availability — the slot may already be gone.")
    return "\n".join(lines)


def render(entries: list, notices: str | None, checked_at: datetime,
           location: str = "KIDA NYC, 369 Broome Street, New York, NY") -> str:
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", _fold(f"PRODID:{PRODID}"), "CALSCALE:GREGORIAN",
           _fold("X-WR-CALNAME:" + _esc("KIDA NYC — Open Slots")),
           _fold("X-WR-CALDESC:" + _esc(
               f"Open booking slots at KIDA NYC, last checked {checked_at:%Y-%m-%d %H:%M %Z}. "
               "Snapshot — confirm on KIDA's site.")),
           # Refresh hints (honored by Apple/Outlook; Google uses its own schedule).
           "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    stamp = _utc(checked_at)
    for ev in entries:
        out += [
            "BEGIN:VEVENT",
            f"UID:{ev.google_event_id()}@kida-cal",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_utc(ev.start)}",
            f"DTEND:{_utc(ev.end)}",
            _fold("SUMMARY:" + _esc(ev.summary())),
            _fold("DESCRIPTION:" + _esc(entry_description(ev, notices))),
            _fold("LOCATION:" + _esc(location)),
            _fold("URL:" + ev.book_url),          # URI value: not TEXT, so not escaped
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def write(path: str, entries: list, notices: str | None, checked_at: datetime) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(render(entries, notices, checked_at))
