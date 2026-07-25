"""
High-level availability fetcher: config → normalized, de-duplicated open slots.

Orchestrates the Timely funnel (src/timely.py) across the configured services and
staff for the lookahead window, normalizes each opening into a tz-aware Slot, and
groups overlapping services into Events (src/models.group_slots).

Also fetches the live notices banner from kidanyc.com (free text, surfaced verbatim).

Request de-duplication
----------------------
The funnel is walked once per (service, staff) pair — 60 pairs — but a gettimeslots
response is a pure function of (staff, date, service DURATION), not of the service's
identity. Only ~27 distinct (staff, duration) combinations exist, so most of those walks
re-fetch bytes we already have. We learn each pair's duration by decoding the first
response's booking token (never from a hardcoded table, which would silently drift and
attribute slots to the wrong service) and memoize by (staff, duration, date) from then on.
Measured effect on a 90-day sweep: ~5,050 requests / 84 min → ~2,300 / ~39 min.

`verify_dedup_samples` re-fetches a few memo hits per run and compares, so if that
invariant ever stops holding we hear about it instead of publishing wrong times.

Exit/guard behaviour lives here so callers get a clear success/suspicious signal:
`FetchResult.ok` is False unless a large majority of the configured (service,staff)
lookups succeeded — the caller must NOT mass-delete calendar events when it is False.
"""
from __future__ import annotations

import os
import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import yaml

from . import timely
from .cache import ResponseCache
from .catalog import SERVICES, service_meta, staff_known, staff_name
from .models import Event, Slot, group_slots
from .timely import BudgetExhausted

KIDANYC_HOME = "https://kidanyc.com/"
_KNOWN_IDS = set(SERVICES)   # service ids we have price/deposit enrichment for

# Share of configured (service,staff) lookups that must succeed for the fetch to be trusted.
# Not 100%: one flaky 429 in 60 would freeze every delete, and a calendar still advertising
# already-booked appointments is worse than one that is briefly a little thin.
MIN_LOOKUP_SUCCESS_RATIO = 0.8


@dataclass
class Config:
    # NOTE: every field needs an annotation. Un-annotated names are ordinary class
    # attributes — excluded from __init__/__repr__/asdict and shared across instances —
    # which previously made stylists/services/min_slot_hour/calendar_id invisible in
    # exactly the debug output where a misconfiguration would have shown up.
    lookahead_days: int = 90              # deep sweep window
    near_lookahead_days: int = 21         # hourly near-term window
    deep_sweep_every_hours: int = 6       # how stale a deep sweep may get before we redo it
    timezone: str = "America/New_York"
    stylists: object = "all"
    services: object = "all"
    min_slot_hour: int | None = None
    weekends_only: bool = False
    event_style: str = "blocks"           # "blocks" (merged) or "slots" (one per opening)
    request_delay_seconds: float = 1.0
    max_requests_per_run: int = 6000
    cache_ttl_seconds: int = 900
    verify_dedup_samples: int = 4
    calendar_id: str | None = None
    calendar_name: str = "KIDA NYC — Open Slots"
    booking_url: str = "https://bookings.gettimely.com/kidanyc/bb/book"
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        c = cls()
        known = {f.name for f in cls.__dataclass_fields__.values()} - {"raw"}
        for k, v in data.items():
            if k not in known:
                # A warning, not a hard exit: a stray key should not take the sync down.
                # But it must be visible — `calender_id` used to attach silently and let
                # ensure_calendar fall through to creating a brand-new calendar.
                print(f"::warning::unknown config key {k!r} in {path} (ignored)",
                      file=sys.stderr)
                continue
            setattr(c, k, v)
        c.raw = data

        # Per-run env overrides.
        env_look = os.environ.get("KIDA_LOOKAHEAD_DAYS")
        if env_look:
            c.lookahead_days = int(env_look)
        env_ttl = os.environ.get("KIDA_CACHE_TTL_SECONDS")
        if env_ttl:
            c.cache_ttl_seconds = int(env_ttl)

        c.validate()
        return c

    def validate(self) -> None:
        if self.event_style not in ("blocks", "slots"):
            raise SystemExit(f"config: event_style must be 'blocks' or 'slots', "
                             f"got {self.event_style!r}")
        for name in ("lookahead_days", "near_lookahead_days", "deep_sweep_every_hours",
                     "max_requests_per_run"):
            v = getattr(self, name)
            if not isinstance(v, int) or isinstance(v, bool) or v <= 0:
                raise SystemExit(f"config: {name} must be a positive integer, got {v!r}")
        if self.min_slot_hour is not None and not (0 <= int(self.min_slot_hour) <= 23):
            raise SystemExit(f"config: min_slot_hour must be 0-23, got {self.min_slot_hour!r}")
        if self.request_delay_seconds < 0:
            raise SystemExit("config: request_delay_seconds must not be negative")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"config: unknown timezone {self.timezone!r}")

    def describe(self) -> str:
        return (f"style={self.event_style} deep={self.lookahead_days}d "
                f"near={self.near_lookahead_days}d tz={self.timezone} "
                f"stylists={self.stylists} services={self.services} "
                f"delay={self.request_delay_seconds}s cap={self.max_requests_per_run} "
                f"cache_ttl={self.cache_ttl_seconds}s")


@dataclass
class FetchResult:
    slots: list[Slot]
    events: list[Event]
    notices: str | None            # None == the scrape FAILED (vs "" == banner is empty)
    ok: bool                       # True if the fetch clearly succeeded
    lookups_ok: int = 0
    lookups_failed: int = 0
    lookups_expected: int = 0
    requests_made: int = 0
    requests_saved: int = 0
    errors: list[str] = field(default_factory=list)
    # Stylists whose lookups ALL succeeded this run. The delete pass only has authority over
    # these people: global health is too coarse, because the busiest stylist owns ~15% of
    # the lookups and could fail entirely while the run still reported ok=True — and every
    # one of their events would then be deleted as "no longer offered".
    staff_ok: set[str] | None = None


def build_datetime(date_iso: str, minute_of_day: int, tz: ZoneInfo) -> datetime:
    """Compose a tz-aware datetime from a local date + minutes-since-midnight.

    Uses zoneinfo so DST offsets (e.g. the Nov 1 2026 fall-back) are correct. We never
    build naive datetimes; downstream code serializes these with their offset.
    """
    y, m, d = (int(x) for x in date_iso.split("-"))
    naive = datetime(y, m, d) + timedelta(minutes=minute_of_day)
    return naive.replace(tzinfo=tz)


def _service_allowed(name: str, allow) -> bool:
    if allow == "all" or not allow:
        return True
    n = name.lower()
    return any(str(a).lower() in n for a in allow)


def _stylist_allowed(staff_id: str, allow) -> bool:
    if allow == "all" or not allow:
        return True
    name = staff_name(staff_id).lower()
    return any(str(a).lower() in name for a in allow)


def _months_in_window(start: datetime, days: int):
    """Yield (year, month) covering [start, start+days]."""
    seen, cur, end = [], start, start + timedelta(days=days)
    while cur <= end:
        key = (cur.year, cur.month)
        if key not in seen:
            seen.append(key)
        # jump to first of next month
        if cur.month == 12:
            cur = cur.replace(year=cur.year + 1, month=1, day=1)
        else:
            cur = cur.replace(month=cur.month + 1, day=1)
    return seen


def fetch_notices(session: requests.Session | None = None) -> str | None:
    """Best-effort scrape of the kidanyc.com notices banner (free text).

    Returns None when the scrape FAILED, which is deliberately distinct from "" (the banner
    is genuinely empty). This string is an input to every event description, so treating a
    20-second outage as "the banner was cleared" would rewrite all ~3300 events and then
    rewrite them back on the next run.
    """
    try:
        sess = session or requests.Session()
        r = sess.get(KIDANYC_HOME, headers={"User-Agent": timely.UA}, timeout=20)
        r.raise_for_status()
        html = r.text
        # The home page renders a notices/alert banner; grab visible text from likely
        # containers, else fall back to empty. Kept deliberately loose — it's free text.
        chunks = re.findall(
            r'<(?:div|p|span)[^>]*class="[^"]*(?:notice|alert|banner|announcement)[^"]*"[^>]*>(.*?)</(?:div|p|span)>',
            html, re.S | re.I)
        text = " ".join(re.sub(r"<[^>]+>", " ", c) for c in chunks)
        text = re.sub(r"\s+", " ", text).strip()
        return text[:500]
    except Exception as e:
        print(f"::warning::notices scrape failed ({e}); leaving existing descriptions alone",
              file=sys.stderr)
        return None


def _slot_durations(raw) -> set[int]:
    return {r.end_min - r.start_min for r in raw}


def fetch(config: Config, tz: ZoneInfo | None = None, *, client_factory=None) -> FetchResult:
    """Walk the funnel and return normalized availability.

    `client_factory` is a test seam (called as factory(cache=..., cache_ns=...)); production
    always uses the real TimelyClient. It exists so the orchestration here — de-duplication,
    the health threshold, budget propagation — can be tested without touching a third party
    whose POST funnel rate-limits under repeated hits.
    """
    tz = tz or ZoneInfo(config.timezone)
    make_client = client_factory or timely.TimelyClient
    timely.BUDGET.configure(config.max_requests_per_run, config.request_delay_seconds)
    cache = ResponseCache(ttl_seconds=config.cache_ttl_seconds)

    now = datetime.now(tz)
    today = now.date()
    horizon = today + timedelta(days=config.lookahead_days)
    months = _months_in_window(now, config.lookahead_days)

    # One bootstrap to learn the live catalog (service names + staff mapping).
    probe = make_client(cache=cache, cache_ns="")
    services, service_staff_ids = probe.bootstrap()

    for s in services:
        if s["service_id"] not in _KNOWN_IDS:
            msg = (f"unknown service id {s['service_id']} ({s['name']}) — "
                   f"add it to src/catalog.py")
            print(f"::warning::{msg}", file=sys.stderr)

    # Build the work plan up front so we know the denominator for the health check.
    plan: list[tuple[dict, list[str]]] = []
    for svc in services:
        if not _service_allowed(svc["name"], config.services):
            continue
        eligible = [sid for sid in svc["staff_ids"]
                    if _stylist_allowed(sid, config.stylists) and staff_known(sid)]
        unknown = [sid for sid in svc["staff_ids"] if not staff_known(sid)]
        if unknown:
            # Publishing "Nao · Hair Cut" is fine; publishing "Staff 991234 · Hair Cut"
            # with no role and no price is worse than publishing nothing.
            print(f"::warning::skipping unknown staff {unknown} on {svc['name']} — "
                  f"add them to src/catalog.py STAFF", file=sys.stderr)
        if eligible:
            plan.append((svc, eligible))

    expected = sum(len(e) for _, e in plan)

    slots: list[Slot] = []
    ok_count = fail_count = 0
    errors: list[str] = []
    # (staff_id, duration_min, date_iso) -> decoded slots, shared across services
    slot_memo: dict[tuple[str, int, str], list] = {}
    memo_trusted = True        # cleared for the rest of the run if a spot-check disagrees
    saved = 0
    checks_left = max(0, int(config.verify_dedup_samples))
    staff_seen: set[str] = set()      # stylists with at least one complete lookup
    staff_failed: set[str] = set()    # stylists with at least one failed lookup

    for svc, eligible in plan:
        meta = service_meta(svc["service_id"])
        # Fresh session per service (service is baked into the obg session). Cache is
        # namespaced by service id so GET reads hit across runs despite a new obg.
        client = make_client(cache=cache, cache_ns=svc["service_id"])
        try:
            client.bootstrap()
            client.select_service(svc["bookable_item_id"], service_staff_ids)
            client.select_staff(eligible[0])
        except BudgetExhausted:
            raise                     # the run is over; never launder this into a count
        except Exception as e:
            # One failure here costs us every staff lookup for this service, so charge
            # all of them. Charging 1 biased `ok` toward True exactly when the outage
            # was broadest.
            fail_count += len(eligible)
            # ...and they must lose delete authority too, or the two counters disagree:
            # every one of these stylists also appears in some OTHER service, so without
            # this they land in staff_ok, keep full delete authority, and have the events
            # only this service could see deleted off the public calendar.
            staff_failed.update(eligible)
            errors.append(f"{svc['name']}: funnel setup failed: {e}")
            continue

        for staff_id in eligible:
            # Buffer this pair's slots. A lookup that dies on day 3 of 6 must contribute
            # NOTHING rather than a truncated truth that reads downstream as "this stylist
            # has fewer openings" — which the delete pass would act on.
            pair_slots: list[Slot] = []
            try:
                duration: int | None = None
                open_days = set()
                for (yr, mo) in months:
                    for od in client.open_dates(staff_id, mo, yr):
                        open_days.add(od["day"])

                for day in sorted(open_days):
                    day_date = datetime.fromisoformat(day).date()
                    if day_date < today or day_date > horizon:
                        continue
                    if config.weekends_only and day_date.weekday() < 5:
                        continue

                    memo_key = ((staff_id, duration, day)
                                if duration is not None and memo_trusted else None)
                    if memo_key is not None and memo_key in slot_memo:
                        raw = slot_memo[memo_key]
                        saved += 1
                        if checks_left:
                            # Spot-check the invariant against live data a few times a run.
                            checks_left -= 1
                            fresh = client.time_slots(staff_id, day)
                            if {s.start_min for s in fresh} != {s.start_min for s in raw}:
                                # The premise of the whole de-duplication is false. Do not
                                # just log it and publish the wrong times: drop the memo,
                                # stop reusing it for the rest of the run, and mark the
                                # fetch untrusted so nothing is written or deleted.
                                msg = (f"dedup invariant broken for staff {staff_id} {day} "
                                       f"@{duration}m: memoized "
                                       f"{sorted(s.start_min for s in raw)} vs live "
                                       f"{sorted(s.start_min for s in fresh)}")
                                print(f"::error::{msg}", file=sys.stderr)
                                errors.append(msg)
                                memo_trusted = False
                                slot_memo.clear()
                                raw = fresh
                    else:
                        raw = client.time_slots(staff_id, day)
                        durations = _slot_durations(raw)
                        if len(durations) == 1:
                            duration = next(iter(durations))
                            if memo_trusted:
                                slot_memo[(staff_id, duration, day)] = raw
                        elif durations:
                            # Mixed durations in one response: the memo key would be a lie,
                            # so don't cache and don't claim to know this pair's duration.
                            duration = None

                    for rs in raw:
                        start = build_datetime(rs.date, rs.start_min, tz)
                        if start < now:
                            continue
                        if (config.min_slot_hour is not None
                                and start.hour < int(config.min_slot_hour)):
                            continue
                        end = build_datetime(rs.date, rs.end_min, tz)
                        pair_slots.append(Slot(
                            stylist_id=staff_id,
                            stylist=staff_name(staff_id),
                            service_id=svc["service_id"],
                            service=svc["name"],
                            start=start,
                            end=end,
                            duration_min=rs.end_min - rs.start_min,
                            price_display=meta["price_display"],
                            deposit_required=meta["deposit_required"],
                            book_url=config.booking_url,
                        ))
            except BudgetExhausted:
                raise
            except Exception as e:
                fail_count += 1
                staff_failed.add(staff_id)
                errors.append(f"{svc['name']} / staff {staff_id}: {e}")
                continue
            slots.extend(pair_slots)
            staff_seen.add(staff_id)
            ok_count += 1

    events = group_slots(slots)
    notices = fetch_notices()

    # "ok" == a large majority of the planned lookups actually landed AND we actually saw
    # data. The old rule (`ok_count > 0 and fail_count <= ok_count`) called a fetch healthy
    # with 49% of the salon's availability missing, and the delete pass then removed all of
    # it. Counting only exceptions is still not enough: a Timely shape change can make every
    # lookup "succeed" with zero slots, which is the input that wipes the calendar. A salon
    # with genuinely zero openings across the whole window is not a state we should ever
    # publish silently, so require at least one slot before declaring health.
    ok = (expected > 0
          and ok_count >= MIN_LOOKUP_SUCCESS_RATIO * expected
          and memo_trusted
          and bool(slots))
    if not ok:
        print(f"::error::fetch incomplete: {ok_count}/{expected} lookups succeeded "
              f"(need {MIN_LOOKUP_SUCCESS_RATIO:.0%}), {len(slots)} slots, "
              f"memo_trusted={memo_trusted}", file=sys.stderr)

    return FetchResult(slots=slots, events=events, notices=notices, ok=ok,
                       lookups_ok=ok_count, lookups_failed=fail_count,
                       lookups_expected=expected, requests_made=timely.BUDGET.made,
                       requests_saved=saved, errors=errors,
                       # Only stylists every one of whose lookups landed. A stylist with any
                       # failure is withheld from the delete pass rather than being read as
                       # "no longer has openings".
                       staff_ok=staff_seen - staff_failed)


if __name__ == "__main__":
    cfg = Config.load()
    result = fetch(cfg)
    print(f"ok={result.ok} lookups ok={result.lookups_ok}/{result.lookups_expected} "
          f"failed={result.lookups_failed}")
    print(f"{len(result.slots)} raw slots → {len(result.events)} events "
          f"({result.requests_made} requests, {result.requests_saved} saved by dedup)")
    if result.notices:
        print(f"notices: {result.notices}")
    for ev in result.events[:25]:
        print(f"  {ev.start:%a %m-%d %H:%M} {ev.summary()}  [{', '.join(ev.services)}]")
    if result.errors:
        print("errors:", *result.errors[:5], sep="\n  ")
