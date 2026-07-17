"""
High-level availability fetcher: config → normalized, de-duplicated open slots.

Orchestrates the Timely funnel (src/timely.py) across the configured services and
staff for the lookahead window, normalizes each opening into a tz-aware Slot, and
groups overlapping services into Events (src/models.group_slots).

Also fetches the live notices banner from kidanyc.com (free text, surfaced verbatim).

Exit/guard behaviour lives here so callers get a clear success/suspicious signal:
`FetchResult.ok` is False when *every* configured (service,staff) lookup failed —
the caller must NOT mass-delete calendar events in that case.
"""
from __future__ import annotations

import re
import sys
from dataclasses import dataclass, field
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
import yaml

from . import timely
from .cache import ResponseCache
from .catalog import service_meta, staff_name
from .models import Event, Slot, group_slots

KIDANYC_HOME = "https://kidanyc.com/"


@dataclass
class Config:
    lookahead_days: int = 28
    timezone: str = "America/New_York"
    stylists = "all"
    services = "all"
    exclude_phone_only: bool = True
    min_slot_hour = None
    weekends_only: bool = False
    request_delay_seconds: float = 1.0
    max_requests_per_run: int = 1500
    cache_ttl_seconds: int = 900
    calendar_id = None
    calendar_name: str = "KIDA NYC — Open Slots"
    calendar_color_id: str = "5"
    booking_url: str = "https://bookings.gettimely.com/kidanyc/bb/book"
    raw: dict = field(default_factory=dict)

    @classmethod
    def load(cls, path: str = "config.yaml") -> "Config":
        with open(path, encoding="utf-8") as f:
            data = yaml.safe_load(f) or {}
        c = cls()
        for k, v in data.items():
            setattr(c, k, v)
        c.raw = data
        return c


@dataclass
class FetchResult:
    slots: list[Slot]
    events: list[Event]
    notices: str
    ok: bool                       # True if the fetch clearly succeeded
    lookups_ok: int = 0
    lookups_failed: int = 0
    errors: list[str] = field(default_factory=list)


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


def fetch_notices(session: requests.Session | None = None) -> str:
    """Best-effort scrape of the kidanyc.com notices banner (free text)."""
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
    except Exception:
        return ""


def fetch(config: Config, tz: ZoneInfo | None = None) -> FetchResult:
    tz = tz or ZoneInfo(config.timezone)
    timely.BUDGET.configure(config.max_requests_per_run, config.request_delay_seconds)
    cache = ResponseCache(ttl_seconds=config.cache_ttl_seconds)

    now = datetime.now(tz)
    today = now.date()
    horizon = today + timedelta(days=config.lookahead_days)
    months = _months_in_window(now, config.lookahead_days)

    # One bootstrap to learn the live catalog (service names + staff mapping).
    probe = timely.TimelyClient(cache=cache)
    services, service_staff_ids = probe.bootstrap()

    wanted = [s for s in services if _service_allowed(s["name"], config.services)]
    for s in services:
        if s["service_id"] not in _KNOWN_IDS:
            print(f"WARN: unknown service id {s['service_id']} ({s['name']}) — "
                  f"add it to src/catalog.py", file=sys.stderr)

    slots: list[Slot] = []
    ok_count = fail_count = 0
    errors: list[str] = []

    for svc in wanted:
        # Skip a service entirely if none of its staff pass the stylist filter —
        # avoids burning a bootstrap + 2 POSTs (and rate-limit risk) on services
        # nobody we care about performs.
        eligible_staff = [sid for sid in svc["staff_ids"]
                          if _stylist_allowed(sid, config.stylists)]
        if not eligible_staff:
            continue
        meta = service_meta(svc["service_id"])
        # Fresh session per service (service is baked into the obg session). Cache is
        # namespaced by service id so GET reads hit across runs despite a new obg.
        client = timely.TimelyClient(cache=cache, cache_ns=svc["service_id"])
        try:
            client.bootstrap()
            client.select_service(svc["bookable_item_id"], service_staff_ids)
            if not svc["staff_ids"]:
                continue
            client.select_staff(svc["staff_ids"][0])
        except Exception as e:  # this whole service failed — record, keep going
            fail_count += 1
            errors.append(f"{svc['name']}: funnel setup failed: {e}")
            continue

        for staff_id in eligible_staff:
            try:
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
                    for rs in client.time_slots(staff_id, day):
                        start = build_datetime(rs.date, rs.start_min, tz)
                        if start < now:
                            continue
                        if config.min_slot_hour is not None and start.hour < int(config.min_slot_hour):
                            continue
                        end = build_datetime(rs.date, rs.end_min, tz)
                        slots.append(Slot(
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
                ok_count += 1
            except Exception as e:
                fail_count += 1
                errors.append(f"{svc['name']} / staff {staff_id}: {e}")

    events = group_slots(slots)
    notices = fetch_notices()
    # "ok" == the fetch clearly worked: at least one lookup succeeded. If everything
    # failed, treat as suspicious so the caller won't mass-delete.
    ok = ok_count > 0 and fail_count <= ok_count
    return FetchResult(slots=slots, events=events, notices=notices, ok=ok,
                       lookups_ok=ok_count, lookups_failed=fail_count, errors=errors)


from .catalog import SERVICES as _CAT
_KNOWN_IDS = set(_CAT.keys())


if __name__ == "__main__":
    cfg = Config.load()
    result = fetch(cfg)
    print(f"ok={result.ok} lookups ok={result.lookups_ok} failed={result.lookups_failed}")
    print(f"{len(result.slots)} raw slots → {len(result.events)} events")
    if result.notices:
        print(f"notices: {result.notices}")
    for ev in result.events[:25]:
        print(f"  {ev.start:%a %m-%d %H:%M} {ev.summary()}  [{', '.join(ev.services)}]")
    if result.errors:
        print("errors:", *result.errors[:5], sep="\n  ")
