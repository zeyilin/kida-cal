"""
Secondary output: write open slots to an .ics file (RFC 5545).

Useful for Apple Calendar / debugging and as the GitHub Actions keepalive artifact.
The Google Calendar API path (src/sync_calendar.py) is the primary output because
Google re-fetches subscribed ICS feeds only every several hours — too slow for salon
slots that vanish within hours.

Events are VEVENTs marked TRANSP:TRANSPARENT (Free) with no VALARM (no alerts).
"""
from __future__ import annotations

from datetime import datetime, timezone

from .models import Event

PRODID = "-//kida-cal//KIDA NYC Open Slots//EN"


def _fold(line: str) -> str:
    """RFC 5545 line folding at 75 octets (approximate on chars; ASCII content)."""
    out, s = [], line
    limit = 75
    while len(s) > limit:
        out.append(s[:limit])
        s = " " + s[limit:]
        limit = 74  # continuation lines start with a space
    out.append(s)
    return "\r\n".join(out)


def _esc(text: str) -> str:
    return (text.replace("\\", "\\\\").replace(";", "\\;")
                .replace(",", "\\,").replace("\n", "\\n"))


def _utc(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).strftime("%Y%m%dT%H%M%SZ")


def event_description(ev: Event, notices: str, checked_at: datetime) -> str:
    lines = []
    if len(ev.services) > 1:
        lines.append("Bookable as: " + ", ".join(ev.services))
    price = " / ".join(ev.price_displays) if ev.price_displays else ""
    if price:
        lines.append(f"Price: {price}")
    if ev.deposit_required:
        lines.append("A deposit may be required (confirm on KIDA's site).")
    lines.append(f"Book: {ev.book_url}")
    if notices:
        lines.append(f"Salon notice: {notices}")
    lines.append(f"Last checked: {checked_at:%Y-%m-%d %H:%M %Z}. "
                 "This is a snapshot — the slot may already be gone; confirm on KIDA's site.")
    return "\n".join(lines)


def render(events: list[Event], notices: str, checked_at: datetime,
           location: str = "KIDA NYC, 369 Broome Street, New York, NY") -> str:
    out = ["BEGIN:VCALENDAR", "VERSION:2.0", f"PRODID:{PRODID}", "CALSCALE:GREGORIAN",
           "METHOD:PUBLISH", "X-WR-CALNAME:KIDA NYC — Open Slots",
           "X-WR-CALDESC:Open booking slots at KIDA NYC. Snapshot — confirm on KIDA's site.",
           # Refresh hints (honored by Apple/Outlook; Google uses its own schedule).
           "REFRESH-INTERVAL;VALUE=DURATION:PT1H", "X-PUBLISHED-TTL:PT1H"]
    stamp = _utc(checked_at)
    for ev in events:
        out += [
            "BEGIN:VEVENT",
            f"UID:{ev.google_event_id()}@kida-cal",
            f"DTSTAMP:{stamp}",
            f"DTSTART:{_utc(ev.start)}",
            f"DTEND:{_utc(ev.end)}",
            _fold("SUMMARY:" + _esc(ev.summary())),
            _fold("DESCRIPTION:" + _esc(event_description(ev, notices, checked_at))),
            _fold("LOCATION:" + _esc(location)),
            _fold("URL:" + _esc(ev.book_url)),
            "TRANSP:TRANSPARENT",
            "END:VEVENT",
        ]
    out.append("END:VCALENDAR")
    return "\r\n".join(out) + "\r\n"


def write(path: str, events: list[Event], notices: str, checked_at: datetime) -> None:
    with open(path, "w", encoding="utf-8", newline="") as f:
        f.write(render(events, notices, checked_at))
