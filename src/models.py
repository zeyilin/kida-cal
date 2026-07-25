"""Canonical data models for open availability slots.

Three layers, narrowest first:

    Slot    one (stylist, service, start) opening as Timely reports it
    Event   one (stylist, start) opening, with every service bookable at it collapsed in
    Block   contiguous Events for one stylist on one local date, merged into one calendar entry

Events and Blocks are interchangeable as far as the calendar sync is concerned — both expose
``google_event_id()``, ``summary()``, ``description_lines()``, ``start`` and ``end`` — so
``config.event_style`` can pick either without the sync path caring which it got.

Calendar-id schemes (they must stay mutually unambiguous, because the sync classifies an
existing event by its id prefix alone):

    kida<40 hex>    per-Event, legacy "slots" style
    kidav<40 hex>   per-Block, "blocks" style
    kidastatus      the single status/freshness marker

sha1 hex only ever contains [0-9a-f], so 'v' and 's' can never appear at that position in a
legacy id. Do not pick a hex character as a future discriminator.
"""
from __future__ import annotations

import hashlib
from dataclasses import dataclass, field
from datetime import datetime, timedelta

ID_PREFIX = "kida"           # every event this project owns
BLOCK_PREFIX = "kidav"       # blocks style
MARKER_ID = "kidastatus"     # the freshness / last-deep-sweep marker

# Openings this far apart or closer are treated as one continuous stretch of availability.
# Timely's grid is hourly with 55-minute services, so back-to-back openings leave a 5-minute
# gap; 20 minutes absorbs that without welding genuinely separate morning/evening stretches.
BLOCK_GAP_TOLERANCE = timedelta(minutes=20)


@dataclass(frozen=True)
class ServiceOption:
    """One service bookable at a given opening, with its own duration and price."""
    name: str
    price_display: str
    duration_min: int
    deposit_required: bool


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


def _rank(name: str):
    """Order services so the title names a haircut rather than the alphabetically first
    option. Ties break on the shorter, plainer name."""
    n = name.strip().lower()
    if n in ("hair cut", "haircut"):     # the plain full haircut
        return (0, len(name), name)
    if "cut" in n:                       # other cut variants / combos
        return (1, len(name), name)
    return (2, len(name), name)          # beard-only, color, blowout, etc.


@dataclass
class Event:
    """A de-duplicated opening: one stylist, one start time, possibly several eligible
    services collapsed together (e.g. the same 3pm opening bookable as either 'Hair Cut'
    or 'Hair Cut & Beard Trim')."""
    stylist_id: str
    stylist: str
    stylist_role: str
    start: datetime
    options: list[ServiceOption] = field(default_factory=list)
    book_url: str = ""

    # -- the titled service decides the block length -------------------------
    @property
    def primary(self) -> ServiceOption | None:
        """The service shown in the title. Its duration — not the longest service's —
        defines `end`, so a 55-minute haircut does not render as a 4-hour block."""
        if not self.options:
            return None
        return min(self.options, key=lambda o: _rank(o.name))

    @property
    def end(self) -> datetime:
        p = self.primary
        return self.start + timedelta(minutes=p.duration_min if p else 30)

    @property
    def longest_end(self) -> datetime:
        if not self.options:
            return self.end
        return self.start + timedelta(minutes=max(o.duration_min for o in self.options))

    @property
    def duration_min(self) -> int:
        return int((self.end - self.start).total_seconds() // 60)

    @property
    def services(self) -> list[str]:
        return [o.name for o in self.options]

    @property
    def deposit_required(self) -> bool:
        p = self.primary
        return bool(p and p.deposit_required)

    # -- calendar surface ----------------------------------------------------
    def google_event_id(self) -> str:
        """Deterministic, idempotent id: sha1(stylist_id|start_iso), hex-lowercased.

        Google event ids must be base32hex (0-9a-v), 5–1024 chars. Hex qualifies.
        Same opening → same id across runs → insert/patch/delete instead of dupes.
        """
        raw = f"{self.stylist_id}|{self.start.isoformat()}"
        return ID_PREFIX + hashlib.sha1(raw.encode()).hexdigest()

    def primary_service(self) -> str:
        p = self.primary
        return p.name if p else "Appointment"

    def summary(self) -> str:
        """Lead with the discriminator. Every event used to begin 'OPEN · Hair Cut w/ …',
        so the first 15 characters — all a truncated calendar chip shows — were identical
        across the whole calendar."""
        return f"{self.stylist} · {self.primary_service()}"

    def description_lines(self) -> list[str]:
        lines = []
        if self.stylist_role:
            lines.append(self.stylist_role)
        lines.append(f"{self.start:%-I:%M %p} – {self.end:%-I:%M %p}")
        lines.append("")
        lines.extend(_option_lines(self.options))
        return lines


@dataclass
class Block:
    """Contiguous openings for one stylist on one local date, as a single calendar entry.

    One event per 30-minute opening puts ~37 events a day (peaking at 81, with 16 mutually
    overlapping) on the calendar, which Google renders as '+34 more'. A block collapses a
    run of openings into one readable entry and lists the exact bookable starts in the
    description — the grid is hourly, so the block itself does NOT mean any start is valid.
    """
    stylist_id: str
    stylist: str
    stylist_role: str
    openings: list[Event]

    @property
    def start(self) -> datetime:
        return self.openings[0].start

    @property
    def end(self) -> datetime:
        return max(o.end for o in self.openings)

    @property
    def book_url(self) -> str:
        return self.openings[0].book_url

    @property
    def options(self) -> list[ServiceOption]:
        """Union of services bookable somewhere in this block, best-titled first."""
        seen: dict[str, ServiceOption] = {}
        for o in self.openings:
            for opt in o.options:
                seen.setdefault(opt.name, opt)
        return sorted(seen.values(), key=lambda o: _rank(o.name))

    def google_event_id(self) -> str:
        """Keyed on the block's first opening, so a block keeps its id for as long as its
        earliest slot stays open. 'v' never appears in sha1 hex, so this can never collide
        with a legacy per-Event id."""
        raw = f"{self.stylist_id}|{self.start.isoformat()}"
        return BLOCK_PREFIX + hashlib.sha1(raw.encode()).hexdigest()

    def summary(self) -> str:
        n = len(self.openings)
        return f"{self.stylist} · {n} open" if n > 1 else f"{self.stylist} · 1 open"

    def description_lines(self) -> list[str]:
        """List each service with the start times where THAT service is bookable.

        Printing a flat list of starts above a union of every service bookable at any of
        them invites the reader to pair any service with any time — a cross-product that is
        mostly false. A 25-minute haircut may be bookable at 9:30, 10:00 and 10:30 while the
        55-minute cut-and-beard only fits at 9:30, and the old layout advertised both at all
        three. This groups by service so every pair printed is real.
        """
        lines = []
        if self.stylist_role:
            lines.append(self.stylist_role)
        lines.append("Bookable start times:")
        lines.append("")
        for opt in self.options:
            times = [o.start for o in self.openings
                     if any(x.name == opt.name for x in o.options)]
            when = ", ".join(f"{t:%-I:%M %p}" for t in times)
            price = f" — {opt.price_display}" if opt.price_display else ""
            lines.append(f"{opt.name} ({opt.duration_min}m){price}: {when}")
        if any(o.deposit_required for o in self.options):
            lines.append("")
            lines.append("Some of these take a deposit — confirm on KIDA's site.")
        return lines


def _option_lines(options: list[ServiceOption]) -> list[str]:
    """Render services with their own price and duration.

    The old format kept `services` and a de-duplicated `price_displays` as two parallel
    lists, so a stylist with 9 services and 6 distinct prices rendered them unpaired — you
    could see that *something* cost $40 but not what.
    """
    lines = []
    for o in options:
        price = f" — {o.price_display}" if o.price_display else ""
        lines.append(f"{o.name} ({o.duration_min}m){price}")
    if any(o.deposit_required for o in options):
        lines.append("")
        lines.append("Some of these take a deposit — confirm on KIDA's site.")
    return lines


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
                book_url=s.book_url,
            )
            by_key[key] = ev
        if not any(o.name == s.service for o in ev.options):
            ev.options.append(ServiceOption(
                name=s.service,
                price_display=s.price_display,
                duration_min=s.duration_min,
                deposit_required=s.deposit_required,
            ))
    return sorted(by_key.values(), key=lambda e: (e.start, e.stylist))


def group_blocks(events: list[Event]) -> list[Block]:
    """Merge each stylist's contiguous same-day openings into Blocks."""
    by_day: dict[tuple[str, str], list[Event]] = {}
    for ev in events:
        by_day.setdefault((ev.stylist_id, ev.start.date().isoformat()), []).append(ev)

    blocks: list[Block] = []
    for (_sid, _day), evs in by_day.items():
        evs.sort(key=lambda e: e.start)
        run: list[Event] = []
        run_end: datetime | None = None
        for ev in evs:
            if run and ev.start - run_end > BLOCK_GAP_TOLERANCE:
                blocks.append(_mk_block(run))
                run, run_end = [], None
            run.append(ev)
            run_end = max(run_end, ev.end) if run_end else ev.end
        if run:
            blocks.append(_mk_block(run))
    return sorted(blocks, key=lambda b: (b.start, b.stylist))


def _mk_block(run: list[Event]) -> Block:
    head = run[0]
    return Block(stylist_id=head.stylist_id, stylist=head.stylist,
                 stylist_role=head.stylist_role, openings=list(run))


def build_entries(events: list[Event], style: str):
    """Return the list of calendar entries for the configured style."""
    if style == "blocks":
        return group_blocks(events)
    if style == "slots":
        return list(events)
    raise ValueError(f"unknown event_style {style!r} (expected 'blocks' or 'slots')")


# ---------------------------------------------------------------- (de)serialization
# Used by --fetch-json / --from-json so a write-side failure does not cost another
# ~5,000-request, 85-minute Timely sweep to reproduce.
def event_to_dict(ev: Event) -> dict:
    return {
        "stylist_id": ev.stylist_id,
        "stylist": ev.stylist,
        "stylist_role": ev.stylist_role,
        "start": ev.start.isoformat(),
        "book_url": ev.book_url,
        "options": [{"name": o.name, "price_display": o.price_display,
                     "duration_min": o.duration_min,
                     "deposit_required": o.deposit_required} for o in ev.options],
    }


def event_from_dict(d: dict) -> Event:
    return Event(
        stylist_id=d["stylist_id"],
        stylist=d["stylist"],
        stylist_role=d.get("stylist_role", ""),
        start=datetime.fromisoformat(d["start"]),
        book_url=d.get("book_url", ""),
        options=[ServiceOption(**o) for o in d.get("options", [])],
    )
