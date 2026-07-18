"""Canonical data models for open availability slots."""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime


@dataclass(frozen=True)
class Slot:
    """One open opening for one (stylist, service, start).

    `start`/`end` are timezone-aware (America/New_York). Never construct these from
    naive local times — see fetch_availability.build_datetime.
    """
    stylist_id: str
    stylist: str
    service_id: str
    service: str
    start: datetime
    end: datetime
    duration_min: int
    price_display: str
    deposit_required: bool
    book_url: str

    @property
    def wall_key(self) -> tuple[str, str]:
        """Identity for de-duplication: same stylist + same wall-clock start."""
        return (self.stylist_id, self.start.isoformat())


@dataclass
class Event:
    """A de-duplicated calendar event: one stylist, one start time, possibly several
    eligible services collapsed together (e.g. the same 3pm opening bookable as either
    'Hair Cut' or 'Hair Cut & Beard Trim')."""
    stylist_id: str
    stylist: str
    stylist_role: str
    start: datetime
    end: datetime
    services: list[str] = field(default_factory=list)
    price_displays: list[str] = field(default_factory=list)
    deposit_required: bool = False
    book_url: str = ""

    @property
    def duration_min(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    def google_event_id(self) -> str:
        """Deterministic, idempotent id: sha1(stylist_id|start_iso), hex-lowercased.

        Google event ids must be base32hex (0-9a-v), 5–1024 chars. Hex qualifies.
        Same opening → same id across runs → insert/patch/delete instead of dupes.
        """
        raw = f"{self.stylist_id}|{self.start.isoformat()}"
        return "kida" + hashlib.sha1(raw.encode()).hexdigest()

    def primary_service(self) -> str:
        """The service shown in the title. A slot usually supports several services;
        prefer a haircut so the title reads 'Haircut w/ Sachi', not the alphabetically
        first option like 'Beard Shave'. Full list still shows in the description."""
        if not self.services:
            return "Appointment"

        def rank(name: str):
            n = name.strip().lower()
            if n in ("hair cut", "haircut"):     # the plain full haircut
                return (0, len(name), name)
            if "cut" in n:                       # other cut variants / combos
                return (1, len(name), name)
            return (2, len(name), name)          # beard-only, color, blowout, etc.

        return min(self.services, key=rank)

    def summary(self) -> str:
        role = f" ({self.stylist_role})" if self.stylist_role else ""
        return f"OPEN · {self.primary_service()} w/ {self.stylist}{role}"


def group_slots(slots: list[Slot]) -> list[Event]:
    """Collapse raw Slots into Events keyed by (stylist_id, start). Eligible services
    for the same opening are listed together rather than emitted as overlapping events.
    """
    from .catalog import staff_role

    by_key: dict[tuple[str, str], Event] = {}
    for s in sorted(slots, key=lambda x: (x.start, x.stylist_id, x.service)):
        key = s.wall_key
        ev = by_key.get(key)
        if ev is None:
            ev = Event(
                stylist_id=s.stylist_id,
                stylist=s.stylist,
                stylist_role=staff_role(s.stylist_id),
                start=s.start,
                end=s.end,
                book_url=s.book_url,
            )
            by_key[key] = ev
        if s.service not in ev.services:
            ev.services.append(s.service)
        if s.price_display and s.price_display not in ev.price_displays:
            ev.price_displays.append(s.price_display)
        ev.deposit_required = ev.deposit_required or s.deposit_required
        # An opening's end is the longest service bookable at that start.
        if s.end > ev.end:
            ev.end = s.end
    return sorted(by_key.values(), key=lambda e: (e.start, e.stylist))
