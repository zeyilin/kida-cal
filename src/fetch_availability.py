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
Measured effect on a 90-day sweep: ~5,050 requests / 84 min → ~2,300 / ~39 min when it
landed; ~1,700 / ~40 min in the October 2026 production runs (docs/compliance.md).

`verify_dedup_samples` caps how many memo hits per run are re-fetched live and compared,
so if that invariant ever stops holding we hear about it instead of publishing wrong times.
The checks are re-drawn at random every run (see _DedupSampler) — a fixed sample proved the
same pair over and over and never looked at a later service. They go to as many different
memo-served services as the cap allows, every one of them when it covers them all, and only
then to further pairs; never to a second day of one pair. A check that cannot be made leaves
its pair unverified, not failed. A mismatch drops the memo for the rest of the run and marks
the whole fetch untrusted — unless, for a check made after the sweep, the service whose
answer the memo holds now gives the same day as the check (see _source_agrees).

Exit/guard behaviour lives here so callers get a clear success/suspicious signal:
`FetchResult.ok` is False unless a large majority of the configured (service,staff)
lookups succeeded — the caller must NOT mass-delete calendar events when it is False.
"""
from __future__ import annotations

import os
import random
import re
import sys
from dataclasses import dataclass, field
from datetime import date, datetime, timedelta
from zoneinfo import ZoneInfo, ZoneInfoNotFoundError

import requests
import yaml

from . import timely
from .cache import ResponseCache
from .catalog import SERVICES, STAFF, service_meta, staff_known, staff_name
from .models import Event, Slot, group_slots
from .timely import BudgetExhausted

KIDANYC_HOME = "https://kidanyc.com/"
_KNOWN_IDS = set(SERVICES)   # service ids we have price/deposit enrichment for

# Share of configured (service,staff) lookups that must succeed for the fetch to be trusted.
# Not 100%: one flaky 429 in 60 would freeze every delete, and a calendar still advertising
# already-booked appointments is worse than one that is briefly a little thin.
MIN_LOOKUP_SUCCESS_RATIO = 0.8

# The only zone this tool can be configured with. It is not a display preference: every
# GetOpenDates/gettimeslots request is sent with tzId=timely.TZ_ID (80, America/New_York),
# and Timely answers in KIDA's New York wall-clock minutes, which build_datetime() then
# stamps with config.timezone. Any other zone would shift every published opening.
TIMELY_TIMEZONE = "America/New_York"


@dataclass
class Config:
    # NOTE: every field needs an annotation. Un-annotated names are ordinary class
    # attributes — excluded from __init__/__repr__/asdict and shared across instances —
    # which previously made stylists/services/min_slot_hour/calendar_id invisible in
    # exactly the debug output where a misconfiguration would have shown up.
    lookahead_days: int = 90              # deep sweep window
    near_lookahead_days: int = 21         # hourly near-term window
    deep_sweep_every_hours: int = 6       # how stale a deep sweep may get before we redo it
    timezone: str = TIMELY_TIMEZONE       # fixed: see TIMELY_TIMEZONE
    # "all" or a list of names; validate() turns a single name into a one-item list.
    stylists: str | list[str] = "all"
    services: str | list[str] = "all"
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
        v = self.verify_dedup_samples
        if not isinstance(v, int) or isinstance(v, bool) or v < 0:
            raise SystemExit(f"config: verify_dedup_samples must be a whole number >= 0 "
                             f"(0 disables the spot-checks), got {v!r}")
        # Validated against the one zone the requests are made in, not merely against the
        # tz database: `Europe/London` loads fine and would have passed, then stamped every
        # New York opening with London's offset and published it hours off.
        if self.timezone != TIMELY_TIMEZONE:
            raise SystemExit(
                f"config: timezone must be {TIMELY_TIMEZONE!r}, got {self.timezone!r}. It is "
                f"not a display setting: Timely is queried with tzId={timely.TZ_ID} "
                f"(src/timely.py) and answers in KIDA's New York wall-clock times, so any "
                f"other zone would silently shift every published opening.")
        try:
            ZoneInfo(self.timezone)
        except (ZoneInfoNotFoundError, ValueError):
            raise SystemExit(f"config: cannot load timezone {self.timezone!r} — is the tz "
                             f"database (tzdata) installed?")
        self.stylists = _allowlist("stylists", self.stylists)
        self.services = _allowlist("services", self.services)
        if self.stylists != "all":
            # Names are matched as case-insensitive substrings of src/catalog.py STAFF, so a
            # name that matches nobody there can never match anyone at all — it is always a
            # typo. Fail on it: a stylist the plan never looks for is "covered by absence"
            # and their existing calendar entries are pruned, so `stylists: [Nao, Sachii]`
            # would quietly take Sachi off the calendar instead of filtering for her.
            known = [n.lower() for n in STAFF.values()]
            for want in self.stylists:
                if not any(want.lower() in n for n in known):
                    raise SystemExit(
                        f"config: stylists entry {want!r} matches no stylist in "
                        f"src/catalog.py STAFF (known: {', '.join(STAFF.values())})")

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
    # Stylists who had at least one lookup PLANNED this run. A stylist outside this set was
    # never looked for (left the salon, dropped from Timely's catalog, filtered out by
    # config) — which is not the same as one we looked for and could not see. None means
    # unknown: a capture written before this field existed, or a run throttled during the
    # catalog bootstrap, before any plan existed (see the comment on that early return).
    staff_planned: set[str] | None = None
    # The local date the fetch treated as "today": its window is window_start ..
    # window_start + lookahead_days, inclusive. The sync must judge staleness against THIS
    # window, not one recomputed from its own clock minutes later. None means unknown.
    window_start: date | None = None


def build_datetime(date_iso: str, minute_of_day: int, tz: ZoneInfo) -> datetime:
    """Compose a tz-aware datetime from a local date + minutes-since-midnight.

    Uses zoneinfo so DST offsets (e.g. the Nov 1 2026 fall-back) are correct. We never
    build naive datetimes; downstream code serializes these with their offset.
    """
    y, m, d = (int(x) for x in date_iso.split("-"))
    naive = datetime(y, m, d) + timedelta(minutes=minute_of_day)
    return naive.replace(tzinfo=tz)


def _allowlist(key: str, value) -> str | list[str]:
    """Normalise a `stylists:` / `services:` setting to "all" or a list of names.

    A bare string used to reach the matchers below as-is, where `any(... for a in allow)`
    iterated it CHARACTER BY CHARACTER: `stylists: Nao` admitted every stylist whose name
    contains an n, an a or an o — all nine — and `services: Buzz` admitted "Hair Cut" on
    its u. One name is a perfectly reasonable thing to write, so it is accepted and made a
    one-item list; anything that is not "all" or names is refused rather than guessed at.
    """
    if isinstance(value, str):
        name = value.strip()
        if name.lower() == "all":
            return "all"
        if name:
            return [name]
    elif isinstance(value, list) and value:
        names = [v.strip() if isinstance(v, str) else v for v in value]
        if all(isinstance(n, str) and n and n.lower() != "all" for n in names):
            return names
    # Everything else, refused:
    #  * None / an empty list. Both used to mean "all" by falling through `not allow`, so a
    #    list whose every name had been commented out silently re-admitted everyone.
    #  * "all" inside a list, which would be matched as the substring "all".
    #  * empty strings, which as a substring match everything.
    #  * numbers, mappings, booleans — not names at all.
    raise SystemExit(f'config: {key} must be "all", a name, or a list of names, '
                     f"got {value!r}")


def _names(allow) -> list[str]:
    # Belt and braces for a Config that skipped validate(): one name is one name, never the
    # letters of one (see _allowlist).
    return [allow] if isinstance(allow, str) else list(allow)


def _service_allowed(name: str, allow) -> bool:
    if allow == "all" or not allow:
        return True
    n = name.lower()
    return any(str(a).lower() in n for a in _names(allow))


def _stylist_allowed(staff_id: str, allow) -> bool:
    if allow == "all" or not allow:
        return True
    name = staff_name(staff_id).lower()
    return any(str(a).lower() in name for a in _names(allow))


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


# A service's memo hits, as recorded for _DedupSampler: staff_id -> {day: (duration, the
# memoized slots that pair published for that day, the service that read them live)}. The
# slots are kept by reference so a later check compares against exactly what was published,
# even if a later service's probe has since rewritten the same memo key.
_MemoHits = dict[str, dict[str, tuple[int, list, dict]]]


class _DedupSampler:
    """Decides which memo hits are re-fetched live to test the de-duplication premise.

    The first version spent its budget on the first N memo hits in plan order. The plan
    order never changes, so that was days 2..N+1 of the SAME (service, stylist) pair on
    every run, forever — the second service in the catalog was proved again and again, and
    a later service whose availability genuinely differed at the same duration was never
    compared once and was published wrong under ok=True.

    Spreading the checks is harder than it looks, because whether a pair is memo-served at
    all (does its length match one an earlier service already walked for that stylist?) is
    only known once its first response is decoded, and the real menu is full of pairs that
    look eligible up front and never are. A draft of this sampler promised checks to pairs
    ahead of time and lost every one it promised to such a pair: with the default 4 checks,
    `stylists: [Nao]` left one of its 3 memo-served services unchecked on most runs. Now:

    * at most `budget` live re-fetches per run. A check paid after the sweep also costs a
      new session's setup requests, two of them funnel POSTs (see _make_up_checks for the
      total), and a mismatch one more session before it voids the run (_source_agrees);
    * the checked SERVICES are the first `budget` memo-served services in a random priority
      order drawn once per run — so a uniformly random set of that many, and every one of
      them whenever the budget covers them all. A service's place in the plan, and how many
      never-memo-served pairs surround it, no longer change its odds. Budget beyond one per
      service goes to further pairs, never to a second day of a pair already checked;
    * which pair within a service, and which of its memo-served days, are drawn too: a
      divergence confined to one stylist, to far-out dates or to weekends is invisible to a
      sampler that always re-reads the same pair's second day;
    * all of it from `rng` — a fresh system-seeded random.Random in production, so
      successive runs rotate over the whole plan. Tests inject a seeded one.

    A service is decided when its walk ends, while its session is still open: by then it
    is known which of its pairs were memo-served, and a check costs one request. It takes
    one there unless the checks left could all be claimed by services ranked ahead of it:
    services not walked yet, which may still turn out memo-served, and earlier ones that
    did and were passed over. A service passed over is owed a check. Whatever budget is
    still unspent when the sweep ends (held for services that turned out never memo-served,
    or that failed) is paid to the owed services in priority order, then to further pairs,
    through new sessions (see _make_up_checks for why never the service's own). Since no
    service ever takes a check one ranked ahead of it could have claimed, the services
    checked in the end are exactly the first `budget` memo-served ones in the priority
    order, wherever each check was made. A check that errors still counts against the
    budget, and leaves its pair unverified.
    """

    def __init__(self, plan, budget: int, rng: random.Random):
        self.left = max(0, int(budget))
        self._rng = rng
        # This run's priority order over every service that could possibly be memo-served
        # (one of its stylists is walked by an earlier pair), by plan index. Tracked pair by
        # pair, not service by service: a stylist the catalog listed twice under one service
        # would hit the memo her first pair wrote, and must not find that service unranked.
        could: list[int] = []
        walked: set[str] = set()
        for i, (_svc, eligible) in enumerate(plan):
            memo_servable = False
            for sid in eligible:
                memo_servable = memo_servable or sid in walked
                walked.add(sid)
            if memo_servable:
                could.append(i)
        rng.shuffle(could)
        self._rank = {i: r for r, i in enumerate(could)}
        self._owed: dict[int, _MemoHits] = {}       # memo-served, walked, not yet checked
        # (idx, staff_id, hits) for a checked service's other memo-served pairs
        self._spare: list[tuple[int, str, _MemoHits]] = []

    def at_walk_end(self, idx: int, hits: _MemoHits) -> tuple[str, str] | None:
        """Service `idx` has just finished its walk with these memo hits (completed pairs
        only), and its session is still open. Returns the (staff_id, day) to re-fetch in it
        now, or None — in which case the service is owed a check (see make_up)."""
        mine = self._rank[idx]      # a memo hit means an earlier pair walked the stylist
        # Who may claim a check before this one: services not walked yet (they may still
        # turn out memo-served) and owed ones, ranked better. Services already checked, or
        # already found never memo-served, no longer claim anything.
        ahead = sum(1 for i, r in self._rank.items()
                    if r < mine and (i > idx or i in self._owed))
        if ahead >= self.left:
            self._owed[idx] = hits
            return None
        staff_id, day = self._draw(hits)
        self._spare += [(idx, s, hits) for s in sorted(hits) if s != staff_id]
        return staff_id, day

    def make_up(self) -> list[tuple[int, str, str, _MemoHits]]:
        """After the sweep: every (idx, staff_id, day, hits) still worth checking, in the
        order the remaining budget should go — one pair of each owed service, best priority
        first, then further pairs at random. The caller stops when the budget runs out."""
        picks = []
        spare = list(self._spare)
        for idx in sorted(self._owed, key=self._rank.__getitem__):
            hits = self._owed[idx]
            staff_id, day = self._draw(hits)
            picks.append((idx, staff_id, day, hits))
            spare += [(idx, s, hits) for s in sorted(hits) if s != staff_id]
        self._rng.shuffle(spare)
        for idx, staff_id, hits in spare:
            picks.append((idx, staff_id, self._rng.choice(sorted(hits[staff_id])), hits))
        return picks

    def spend(self) -> None:
        self.left -= 1

    def _draw(self, hits: _MemoHits) -> tuple[str, str]:
        staff_id = self._rng.choice(sorted(hits))
        return staff_id, self._rng.choice(sorted(hits[staff_id]))


def _starts(raw: list) -> set[int]:
    return {s.start_min for s in raw}


def _dedup_mismatch(svc: dict, staff_id: str, day: str, duration: int,
                    memo: list, live: list) -> str | None:
    """The error to report if a live re-fetch disagrees with what the memo published."""
    if _starts(live) == _starts(memo):
        return None
    return (f"dedup invariant broken for {svc['name']} / staff {staff_id} {day} "
            f"@{duration}m: memoized {sorted(_starts(memo))} vs live "
            f"{sorted(_starts(live))}")


def _open_session(make_client, svc: dict, service_staff_ids: dict, staff_id: str):
    """A new session through the funnel for `svc`, opened after the sweep for checks only.
    No response cache: the only reads it makes are live ones (see _live_read)."""
    client = make_client(cache=None, cache_ns=svc["service_id"])
    client.bootstrap()
    client.select_service(svc["bookable_item_id"], service_staff_ids)
    client.select_staff(staff_id)
    return client


def _live_read(client, staff_id: str, day: str) -> list:
    """One spot-check's live read of `day`, asked the way the sweep asks for a stylist's days:
    GetOpenDates for the stylist (the check day's month only), then gettimeslots. The first
    answer is not used; the request is made for the order's sake. Without it a check made as
    a service's walk ends would follow another stylist's requests, and one made in a new
    session would follow the bare funnel — orders no sweep, browser or recon capture has
    used — and an empty day earned that way would read as a broken invariant and void the
    run. It costs one GET per check.

    Never from the response cache (a dev aid, off in CI): a GetOpenDates answered from what
    the sweep just cached never reaches Timely, which loses the order above, and a cached
    gettimeslots is not a live read. Dropping the cache here costs the session nothing: a
    check is the last request of its service's walk, and a session opened after the sweep
    makes nothing but checks."""
    client.cache = None
    checked = datetime.fromisoformat(day)
    client.open_dates(staff_id, checked.month, checked.year)
    return client.time_slots(staff_id, day)


def _source_agrees(source: dict, svc: dict, staff_id: str, day: str, live: list,
                   make_client, service_staff_ids: dict, errors: list[str]) -> bool:
    """A check made after the sweep disagreed with the memo. That is not proof the
    invariant broke: the memo holds what `source` read live, up to the whole sweep earlier
    (~40 minutes for a deep one), and a booking or a cancellation since then changes that
    stylist's day for every service alike. So `source` is asked again, now, in a new
    session. If it gives the same day as the check, the two services agree and only the day
    changed: the pair is left unverified, like one the draw passed over, and True is
    returned. If it disagrees, or cannot be asked at all (a throttle included), the
    mismatch stands; the caller stops there, so nothing more is sent.

    Only checks made after the sweep are re-asked. One made as a walk ends trails the memo
    by the services walked in between rather than by the rest of the sweep, so the same
    false alarm is rarer there but still possible; a re-ask in the middle of the sweep
    would also need its own answer to a throttle, and is not worth that."""
    try:
        again = _live_read(_open_session(make_client, source, service_staff_ids, staff_id),
                           staff_id, day)
    except Exception as e:
        msg = f"could not re-ask {source['name']} / staff {staff_id} {day}: {e}"
        print(f"::warning::{msg}", file=sys.stderr)
        errors.append(msg)
        return False
    if _starts(again) != _starts(live):
        return False
    msg = (f"dedup spot-check for {svc['name']} / staff {staff_id} {day} disagreed with the "
           f"memo, but {source['name']} now gives the same {sorted(_starts(live))}: the day "
           f"changed during the sweep, so the pair is left unverified")
    print(f"::warning::{msg}", file=sys.stderr)
    errors.append(msg)
    return True


def _check_not_made(svc: dict, staff_id: str, day: str, e: Exception) -> str:
    msg = f"dedup spot-check not made for {svc['name']} / staff {staff_id} {day}: {e}"
    print(f"::warning::{msg}", file=sys.stderr)
    return msg


def _checks_stopped(e: Exception, when: str) -> str:
    msg = f"dedup spot-checks stopped {when}: {e}"
    print(f"::warning::{msg}", file=sys.stderr)
    return msg


def _throttling() -> bool:
    """Whether Timely is throttling an endpoint right now: its latest request spent all its
    retries on 429s and no request to it has succeeded since (the per-endpoint streak in
    src/timely.py). Short of a stop condition such a request raises a plain TimelyError,
    which on its own does not say it was a throttle. Every make-up session sends every
    endpoint the sweep does, so it would be sent straight back into that throttle."""
    return any(timely.BUDGET.throttles.values())


def _make_up_checks(plan, sampler: _DedupSampler, make_client,
                    service_staff_ids: dict, errors: list[str]) -> tuple[int, bool]:
    """Spend the checks still unspent when the sweep ends (see _DedupSampler). Returns
    (checks made, whether one found the invariant broken).

    Each goes through a NEW session — never the service's own sweep session. By now that one
    has sat idle for the rest of the sweep, nothing in docs/timely-api.md says how long an
    obg session lives, and a stale session that answered with a well-formed empty day would
    read here as a broken invariant and void the whole run. The new session makes the
    sweep's requests in the sweep's order: the funnel, then GetOpenDates for the stylist,
    then gettimeslots (see _live_read). The price is a session's three setup requests (two
    of them the funnel POSTs) per service checked here, and one GetOpenDates per check: at
    most 16 with the default 4 checks, against a ~1,700-request sweep. Simulated against the
    real menu (docs/timely-api.md), about half of all runs need one such session and a few
    need two. A mismatch costs one more session and two reads, to ask the service the memo
    came from before it voids the run (see _source_agrees).

    Every lookup is complete by now, so nothing here can fail one. A check that cannot be
    made is reported and its pair stays unverified, like any pair the draw passed over. A
    throttle (BudgetExhausted) stops the checks — it means stop sending — but does not
    abort the run: it says nothing about data that is already complete. (The last planned
    service's own check, made inside the sweep but with nothing left to walk, is treated
    the same way; see fetch().) So does a check that fails on one request throttled short
    of a stop condition (see _throttling): the next service's session would send the same
    request into the same throttle, five 429s and their Retry-After waits apiece, on a run
    whose every lookup has already landed.
    """
    sessions: dict[int, object] = {}
    dead: set[int] = set()            # services whose make-up session failed: not retried
    made = 0
    for idx, staff_id, day, hits in sampler.make_up():
        if not sampler.left:
            break
        if idx in dead:
            continue                  # no request, so no budget spent: it goes to the next
        svc = plan[idx][0]
        sampler.spend()               # before the request, as in the sweep
        try:
            client = sessions.get(idx)
            if client is None:
                client = _open_session(make_client, svc, service_staff_ids, staff_id)
                sessions[idx] = client
            live = _live_read(client, staff_id, day)
        except BudgetExhausted as e:
            errors.append(_checks_stopped(e, "after the sweep"))
            break
        except Exception as e:
            if _throttling():
                errors.append(_checks_stopped(e, "after the sweep"))
                break
            errors.append(_check_not_made(svc, staff_id, day, e))
            dead.add(idx)
            continue
        made += 1
        duration, memo, source = hits[staff_id][day]
        broken = _dedup_mismatch(svc, staff_id, day, duration, memo, live)
        if broken and _source_agrees(source, svc, staff_id, day, live, make_client,
                                     service_staff_ids, errors):
            continue
        if broken:
            print(f"::error::{broken}", file=sys.stderr)
            errors.append(broken)
            return made, True
    return made, False


def fetch(config: Config, tz: ZoneInfo | None = None, *, client_factory=None,
          rng: random.Random | None = None) -> FetchResult:
    """Walk the funnel and return normalized availability.

    `client_factory` is a test seam (called as factory(cache=..., cache_ns=...)); production
    always uses the real TimelyClient. It exists so the orchestration here — de-duplication,
    the health threshold, budget propagation — can be tested without touching a third party
    whose POST funnel rate-limits under repeated hits.

    `rng` is the other seam: the randomness that picks this run's dedup spot-checks.
    Production leaves it None and gets a fresh system-seeded random.Random, which is what
    makes the checks rotate from run to run; tests pass a seeded one to be deterministic.
    """
    tz = tz or ZoneInfo(config.timezone)
    make_client = client_factory or timely.TimelyClient
    rng = rng if rng is not None else random.Random()
    timely.BUDGET.configure(config.max_requests_per_run, config.request_delay_seconds)
    cache = ResponseCache(ttl_seconds=config.cache_ttl_seconds)

    now = datetime.now(tz)
    today = now.date()
    horizon = today + timedelta(days=config.lookahead_days)
    months = _months_in_window(now, config.lookahead_days)

    # One bootstrap to learn the live catalog (service names + staff mapping).
    probe = make_client(cache=cache, cache_ns="")
    try:
        services, service_staff_ids = probe.bootstrap()
    except (BudgetExhausted, timely.RetriesExhausted) as e:
        # Throttled (or never answered) before we could even read the catalog. There is no
        # plan to walk, but this still has to look like every other untrusted fetch — a
        # clean ok=False result the caller can artifact and exit 2 on — rather than a
        # traceback that reports a throttle window as a crash. RetriesExhausted is the
        # common form: one 429'd GET that used up its retries trips no stop condition, so
        # it used to escape here as a bare TimelyError. Other bootstrap failures stay loud:
        # a missing obg or an unparseable catalog is a shape change, not a rate limit.
        print(f"::error::fetch aborted during bootstrap: {e}", file=sys.stderr)
        return FetchResult(slots=[], events=[], notices=None, ok=False,
                           lookups_ok=0, lookups_failed=0, lookups_expected=0,
                           requests_made=timely.BUDGET.made,
                           errors=[f"run aborted: {e}"], staff_ok=set(),
                           # None, NOT set(): no plan exists, so who would have been
                           # looked for is unknown. The sync withholds a stylist's deletes
                           # when they are in staff_planned - staff_ok, and treats anyone
                           # outside staff_planned as gone from the salon (deletable). An
                           # empty set here would make that difference empty and EVERY
                           # stylist "gone" — the whole calendar deletable on the one run
                           # that saw nothing. None falls back to the legacy rule (not in
                           # staff_ok => withheld), which with staff_ok=set() withholds all.
                           staff_planned=None, window_start=today)

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
    # Everyone the plan will look for, whatever happens to the lookups. Reported even when
    # the sweep aborts: it is what lets the sync tell "looked for and could not see"
    # (withhold their deletes) from "not looked for at all" (left the salon, dropped from
    # the catalog, filtered out by config — their entries are ordinary stale entries).
    staff_planned = {sid for _, eligible in plan for sid in eligible}

    slots: list[Slot] = []
    ok_count = fail_count = 0
    errors: list[str] = []
    # (staff_id, duration_min, date_iso) -> (the service that read it live, decoded slots),
    # shared across services
    slot_memo: dict[tuple[str, int, str], tuple[dict, list]] = {}
    memo_trusted = True        # cleared for the rest of the run if a spot-check disagrees
    saved = 0
    sampler = _DedupSampler(plan, config.verify_dedup_samples, rng)
    staff_seen: set[str] = set()      # stylists with at least one complete lookup
    staff_failed: set[str] = set()    # stylists with at least one failed lookup

    # Set when a stop condition in src/timely.py (the per-endpoint throttle streak, the
    # run-wide throttle total, or the request cap) ends the sweep mid-flight. Deliberately
    # NOT folded into fail_count: the rest of the window was never looked at, so the only
    # honest verdict for the whole run is untrusted, whatever the ratio of what we did see.
    aborted: str | None = None
    # Set when a stop condition trips on a spot-check once nothing is left to walk: no more
    # requests this run, but the sweep's data stands (see the last service's check below).
    checks_stopped = False

    for idx, (svc, eligible) in enumerate(plan):
        meta = service_meta(svc["service_id"])
        # Fresh session per service (service is baked into the obg session). Cache is
        # namespaced by service id so GET reads hit across runs despite a new obg.
        client = make_client(cache=cache, cache_ns=svc["service_id"])
        try:
            client.bootstrap()
            client.select_service(svc["bookable_item_id"], service_staff_ids)
            client.select_staff(eligible[0])
        except BudgetExhausted as e:
            # The run is over. Never launder this into a per-lookup count — record it as
            # an abort, which forces ok=False below regardless of how much we did fetch.
            aborted = str(e)
            errors.append(f"run aborted: {e}")
            break
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

        svc_hits: _MemoHits = {}           # this service's memo hits, completed pairs only
        for staff_id in eligible:
            # Buffer this pair's slots. A lookup that dies on day 3 of 6 must contribute
            # NOTHING rather than a truncated truth that reads downstream as "this stylist
            # has fewer openings" — which the delete pass would act on.
            pair_slots: list[Slot] = []
            pair_hits: dict[str, tuple[int, list]] = {}
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
                        source, raw = slot_memo[memo_key]
                        pair_hits[day] = (duration, raw, source)
                        saved += 1
                    else:
                        raw = client.time_slots(staff_id, day)
                        durations = _slot_durations(raw)
                        if len(durations) == 1:
                            duration = next(iter(durations))
                            if memo_trusted:
                                slot_memo[(staff_id, duration, day)] = (svc, raw)
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
            except BudgetExhausted as e:
                aborted = str(e)
                errors.append(f"run aborted: {e}")
                break
            except Exception as e:
                fail_count += 1
                staff_failed.add(staff_id)
                errors.append(f"{svc['name']} / staff {staff_id}: {e}")
                continue
            slots.extend(pair_slots)
            if pair_hits:
                svc_hits[staff_id] = pair_hits
            staff_seen.add(staff_id)
            ok_count += 1
        if aborted:
            break

        # The spot-check, if this service gets one, is taken here: its walk is done, so it
        # is known which of its pairs were memo-served, and its session is still open.
        pick = sampler.at_walk_end(idx, svc_hits) if svc_hits else None
        if pick:
            staff_id, day = pick
            duration, raw, _source = svc_hits[staff_id][day]
            # Spent before the request, so a check that errors still counts against the cap.
            sampler.spend()
            try:
                fresh = _live_read(client, staff_id, day)
            except BudgetExhausted as e:
                if idx < len(plan) - 1:
                    # A stop condition mid-sweep, like any other: the run is over, and
                    # whatever services follow this one go unwalked.
                    aborted = str(e)
                    errors.append(f"run aborted: {e}")
                    break
                # The last service: nothing is left to walk, so this is a throttle after
                # the sweep in all but name, and is treated as one (see _make_up_checks).
                # Stop sending — no make-up session follows — but keep data that is
                # already complete. Aborting here used to void runs whose every lookup had
                # landed (exit 2, nothing written) over a request that only re-read a day
                # already published, while the same throttle one session later did not.
                errors.append(_checks_stopped(e, "as the sweep ended"))
                checks_stopped = True
            except Exception as e:
                # Not a failed lookup: the pair's own lookup already completed, and this
                # request only re-reads one of its days. It used to run inside that lookup,
                # where a single 429 on it threw the pair's slots away and cost the stylist
                # delete authority for the run. Unverified is all it is — like any pair the
                # draw passed over.
                errors.append(_check_not_made(svc, staff_id, day, e))
            else:
                saved -= 1            # this memo hit was paid for after all
                broken = _dedup_mismatch(svc, staff_id, day, duration, raw, fresh)
                if broken:
                    # The premise of the whole de-duplication is false. Do not just log it
                    # and publish the wrong times: drop the memo, stop reusing it for the
                    # rest of the run, and mark the fetch untrusted so nothing is written
                    # or deleted.
                    print(f"::error::{broken}", file=sys.stderr)
                    errors.append(broken)
                    memo_trusted = False
                    slot_memo.clear()

    # "enough" == a large majority of the planned lookups actually landed AND we actually saw
    # data. The old rule (`ok_count > 0 and fail_count <= ok_count`) called a fetch healthy
    # with 49% of the salon's availability missing, and the delete pass then removed all of
    # it. Counting only exceptions is still not enough: a Timely shape change can make every
    # lookup "succeed" with zero slots, which is the input that wipes the calendar. A salon
    # with genuinely zero openings across the whole window is not a state we should ever
    # publish silently, so require at least one slot before declaring health.
    enough = (expected > 0
              and ok_count >= MIN_LOOKUP_SUCCESS_RATIO * expected
              and bool(slots))

    # Checks promised to services that turned out never to be memo-served are not lost:
    # they go to the memo-served services that were passed over (see _DedupSampler). Not
    # on an aborted run, which is mid-back-off and untrusted anyway, nor after a stop
    # condition on the last service's check, nor once the memo has already been caught out,
    # nor on a run too thin to trust whatever they find: each check here can cost a session
    # and two of the funnel POSTs Timely rate-limits, on a run that will write nothing. Nor
    # while Timely is throttling an endpoint as the sweep ends (see _throttling): a storm
    # that starts on the last services would otherwise take more sessions into it, five
    # 429s and their Retry-After waits apiece, until a stop condition tripped. A throttle
    # the sweep has since recovered from leaves no streak, and costs no checks.
    if (enough and not aborted and not checks_stopped and memo_trusted and sampler.left
            and not _throttling()):
        made, broken = _make_up_checks(plan, sampler, make_client,
                                       service_staff_ids, errors)
        saved -= made
        if broken:
            memo_trusted = False

    events = group_slots(slots)
    # An aborted run has nothing to describe and is mid-back-off; None is the "scrape
    # failed" value, which leaves existing descriptions alone.
    notices = None if aborted else fetch_notices()

    # ...and the sweep ran to the end, and no spot-check caught the memo out.
    ok = enough and not aborted and memo_trusted
    if aborted:
        print(f"::error::fetch aborted mid-sweep: {aborted}", file=sys.stderr)
    if not ok:
        print(f"::error::fetch incomplete: {ok_count}/{expected} lookups succeeded "
              f"(need {MIN_LOOKUP_SUCCESS_RATIO:.0%}), {len(slots)} slots, "
              f"memo_trusted={memo_trusted}, aborted={bool(aborted)}", file=sys.stderr)

    return FetchResult(slots=slots, events=events, notices=notices, ok=ok,
                       lookups_ok=ok_count, lookups_failed=fail_count,
                       lookups_expected=expected, requests_made=timely.BUDGET.made,
                       requests_saved=saved, errors=errors,
                       # Only stylists every one of whose lookups landed. A stylist with any
                       # failure is withheld from the delete pass rather than being read as
                       # "no longer has openings". An aborted run grants delete authority
                       # over nobody it planned for: the stylists it never reached look
                       # identical to stylists with no openings, and an empty set (unlike
                       # None) means "nobody is covered" rather than "coverage unknown, all
                       # deletable".
                       staff_ok=set() if aborted else staff_seen - staff_failed,
                       # The FULL plan, aborted or not: staff_planned - staff_ok is exactly
                       # who the sync must withhold, so shrinking this to "who we reached"
                       # would hand the unreached back their delete authority.
                       staff_planned=staff_planned, window_start=today)


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
