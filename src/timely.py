"""
Low-level Timely booking-funnel client for KIDA NYC.

Wraps the stateful cookie + `obg` funnel documented in docs/timely-api.md. No browser
required. One TimelyClient instance == one HTTP session (its own cookie jar), so create
a fresh client per service (service is baked into the session; staff/date are params).

Politeness: a shared request delay, exponential backoff on 429/5xx (and on connection
failures and timeouts, which are retried the same way but never counted as throttles),
and a hard per-run request cap enforced across all clients via a module-level counter.
When the host asks us to back off for longer than we are willing to sleep, keeps 429ing,
stops giving usable answers (connection failures, timeouts, 5xx), or has made the run
spend its whole retry-time budget waiting out failures, we stop the run and report rather
than grinding — the MAX_* stop conditions below, and docs/compliance.md. A connection broken
mid-response (requests' ChunkedEncodingError) is NOT among the retried failures; see the
note in TimelyClient._request.
"""
from __future__ import annotations

import base64
import html as htmllib
import json
import math
import re
import time
from dataclasses import dataclass, field
from datetime import datetime, timezone
from itertools import count
from email.utils import parsedate_to_datetime
from urllib.parse import urlsplit

import requests

BASE = "https://book.gettimely.com"
EMBED = BASE + "/kidanyc/book/embed?client-login=true"
TZ_ID = 80  # Timely tz id for America/New_York
# Honest, self-identifying UA. We do not impersonate a browser (docs/compliance.md).
UA = "kida-cal/0.2 (+personal read-only availability mirror; contact via repo issues)"

# If Timely asks us to wait longer than this, stop the run instead of sleeping through it.
# The next cron tick will retry. Sleeping on an unbounded Retry-After let a single response
# hold the job open for hours.
MAX_RETRY_AFTER_SECONDS = 120
# The funnel's two POST steps are what Timely actually rate-limits — observed 429ing the
# same two /Booking/Service calls on every run, through all 5 retries, while the GET reads
# sail through. They are ~48 requests of a ~400-request run, so paying a few extra seconds
# each costs little and is the polite response to being told to slow down.
POST_EXTRA_DELAY_SECONDS = 3.0

# Consecutive fully-throttled REQUESTS (each having already spent all 5 retries) that mean
# "this endpoint does not want us right now". A single stubborn url cannot end the sweep on
# its own because this counts whole REQUESTS, not the 5 attempts inside one — that, not the
# constant's size, is what bounds one url's influence.
#
# Counted per (method, path), because Timely throttles ONE funnel step at a time while its
# neighbours stay healthy, and every throttled step has a healthy sibling immediately before
# it: bootstrap GET precedes /Booking/Service, /Booking/Service precedes
# /Booking/StaffSelection, GetOpenDates precedes gettimeslots. A counter shared by those
# siblings is reset to 0 by the sibling's success before the throttled step re-increments
# it, so it oscillates 0->1->0->1 and never reaches the threshold. That is not theory: a
# shared counter made this stop condition unreachable during the 2026-08-09 incident (16
# services' /Booking/Service POSTs 429'd through every retry while all GETs succeeded, and
# the run ground on for 12 minutes), and keying it per METHOD alone still left the same hole
# open for a StaffSelection-only or gettimeslots-only window.
MAX_CONSECUTIVE_THROTTLES = 3
# ...and a monotonic backstop that no interleaving can reset. "Consecutive" has now been
# defeated twice by call orders nobody predicted, so the per-endpoint streak above is the
# fast trip and this total is the guarantee: it counts every fully-retried 429 of the run
# and is never cleared by any success. Sized just above the per-endpoint threshold so a run
# scattering single throttles across all four endpoints still stops promptly.
MAX_THROTTLED_REQUESTS_PER_RUN = 5

# Attempts that get no USABLE answer — a connection failure, a timeout, or a 5xx — have
# stop conditions of their own. They cannot share the throttle counters above — neither
# a timeout nor a 502 is Timely asking us to slow down, and at 3-and-5 a handful of
# unlucky reads would abort a healthy sweep as untrusted — but they cannot have none
# either. Once transport failures were retried (5 attempts with backoff: ~3 min per
# request against a host that never answers, instead of the old ~30s), one hung endpoint
# — the per-step degradation the throttle notes above describe, say GetOpenDates
# read-timing-out while bootstrap and both POSTs stay healthy — cost every lookup ~3 min.
# Simulated, a deep sweep then ran ~185 min, well past the 120-min timeout-minutes at
# which GitHub kills the job (so no fetch artifact, no exit 2, and the sync concurrency
# group held for two hours), and the hung endpoint took 300 requests with nothing to stop
# them. Before transport retries the same run ended untrusted in ~34 min.
#
# 5xx were in that hole from the start, and these counters first covered only transport
# failures, so the same endpoint answering 503 instead of timing out still went
# uncounted: a request that spent all 5 attempts on 5xx just raised TimelyError, and no
# stop condition saw it. With GetOpenDates answering every request 503 and Retry-After:
# 60, each lookup sat out four waits (~4 min) and a simulated deep sweep ran ~246 min
# (~126 at Retry-After: 30, ~486 at 120) — the same job-timeout kill. MAX_RETRY_SECONDS_PER_RUN
# below now bounds that by itself; a request that ENDS on a 5xx also extends the streak
# below exactly as one that ends on a timeout does, so an endpoint that answers nothing
# but 5xx is stopped as an outage rather than ground on until the time runs out.
#
# Consecutive REQUESTS that used up every attempt with no usable answer (their last
# attempt a connection failure, a timeout or a 5xx), keyed per (method, path) and cleared
# only by that endpoint's own success, for the same reasons as MAX_CONSECUTIVE_THROTTLES.
# Even at a 10% per-attempt failure rate one request loses all five attempts about once
# in 100,000, so a streak is never ordinary flakiness. But "three in a row" alone was
# filled by two things that are not an endpoint outage, and either one aborted the whole
# run (exit 2, nothing written for any stylist):
#
#   - One stylist, or one service. The catalog's last six services are Hiroki's alone,
#     and nothing else calls GetOpenDates, gettimeslots or StaffSelection between them, so
#     a fault confined to him — Timely 500ing or timing out on anything about one corrupt
#     roster — exhausted three of his requests in a row and stopped the run. Being
#     deterministic, it then stopped every run after it, leaving booked slots advertised
#     until someone intervened. Before these stop conditions the same fault cost his 6 of
#     60 lookups: a trusted run with Hiroki alone withheld from the delete pass, the
#     trade-off MIN_LOOKUP_SUCCESS_RATIO in fetch_availability exists to make. One
#     service fills a streak the same way: a service whose own setup makes GetOpenDates
#     throw exhausts every one of its stylists' requests back to back, in one session.
#   - A blip. A 5xx with no Retry-After fails in a fraction of a second, so three
#     exhausted requests took ~105s, and a two-minute 503 window on gettimeslots (an app
#     pool recycling) aborted a sweep that otherwise lost 3 lookups and stayed trusted.
#     The streak was sized on ~9 min of 30s timeouts and never meant to see that.
#
# So a streak of MAX_CONSECUTIVE_UNANSWERED_REQUESTS trips only once it ALSO
#   - names at least two stylists — requests that name none (the bootstrap GET, the
#     /Booking/Service POST) cannot be one stylist's fault, so they always qualify;
#   - came from at least two sessions (TimelyClient instances: fetch() opens one per
#     service), so it cannot be one service's fault;
#   - has lasted MIN_UNANSWERED_STREAK_SECONDS, from its first request's first attempt to
#     its latest request's last, so it is not a blip. Three timed-out requests take ~9 min
#     on their own; it is the fast failures this holds back.
# What that costs is detection speed when the sweep order shows an outage one stylist or
# one service at a time. fetch() POSTs StaffSelection for each service's FIRST eligible
# stylist — Masa for the first nine services — so a hung StaffSelection now trips at the
# tenth request (~32 simulated min, where MAX_TRANSPORT_FAILURES_PER_RUN trips too) instead
# of the third, and a hung GetOpenDates at the fourth (the first service has three
# stylists). MAX_RETRY_SECONDS_PER_RUN keeps every such case inside the job timeout. A
# false trip has no such bound: it stops the calendar updating on every run.
MAX_CONSECUTIVE_UNANSWERED_REQUESTS = 3
MIN_UNANSWERED_STREAK_SECONDS = 5 * 60
# ...and a run-wide backstop on failed transport ATTEMPTS — connection failures and
# timeouts, NOT 5xx — never cleared by any success, because what runs out here is time.
# Each costs up to ~50s (the 30s timeout, up to 16s of backoff, pacing) whether or not its
# request then recovers, so a network losing 5% of requests at random never forms a
# streak yet still pushed a simulated deep sweep to ~126 min. 50 of them add under 45 min
# to the observed ~38-min deep sweep — inside the 120-min job timeout — and are ~12x the 4
# timeouts that cost the 2026-09-05 run its lookups, so ordinary flakiness still recovers
# rather than aborting.
#
# 5xx were counted here for a while, and a 5xx is answered in a fraction of a second, so
# 50 of them are ~2 min of backoff, not ~40 min. Counting them aborted runs that lost
# nothing: a simulated sweep in which ~3.5% of attempts got a fast 503, every request
# recovering on its retry, was stopped at the 50th 503 having spent 2 min on retries; and
# one stylist's deterministic 500 interleaved with healthy lookups (Fausto, in nine
# services: 45 attempts, never a streak) left room for only five ordinary timeouts in the
# rest of the run. What a 5xx costs is time — its own latency and any Retry-After it
# carries — and MAX_RETRY_SECONDS_PER_RUN charges exactly that.
MAX_TRANSPORT_FAILURES_PER_RUN = 50
# The backstop under every retry above, measured in the unit that actually runs out:
# seconds. Each failed attempt of any kind — 429, 5xx, connection failure, timeout — is
# charged what it cost the run: the time the attempt itself took plus the backoff or
# Retry-After wait before the next one. When the next wait would take the run's total
# past this, the run stops instead of sleeping it. The counters above bound HOW MANY
# requests or attempts fail; none of them bounds how long Timely may ask us to wait, a
# request that recovers on a late attempt reaches no streak and no throttle total, and
# the unanswered streak deliberately excuses one stylist's or one service's outage. Four
# 429s or 503s with Retry-After: 120 and then a 200 cost 8 min — counting toward nothing
# else at all — and on every gettimeslots call they ran a simulated sweep for ~8 hours.
# 40 min on top of the observed ~38-min deep sweep keeps the worst case near 80 min,
# inside the 120-min job timeout with room for setup and the calendar sync — and a
# healthy run's occasional 429 costs seconds of it, so only a host that keeps asking for
# long waits, or keeps failing slowly, ever trips it.
MAX_RETRY_SECONDS_PER_RUN = 40 * 60

_OBG_RE = re.compile(r"/Booking/Service\?obg=([0-9a-f-]{36})")
_SERVICE_RE = re.compile(
    r'<input[^>]*id="service(\d+)"[^>]*?data-staffids="([^"]*)"[^>]*?'
    r'data-name="([^"]*)"[^>]*?value="([^"]+)"', re.S)
_SERVICE_STAFF_RE = re.compile(
    r'name="(ServiceStaffIds\[\d+:SV\])"\s+[^>]*value="([^"]*)"')
_BOOKING_SELECTION_RE = re.compile(r'name="BookingSelection"[^>]*value="([^"]+)"')
# Sentinels proving a gettimeslots response really is the day partial, not a changed page,
# an error, a login interstitial or an empty body. Any ONE of them is enough.
# IsStaffRequested is the strongest: it appears in both the populated and the empty
# captured fixtures, so it marks the endpoint rather than the outcome.
_STAFF_FIELD_RE = re.compile(r'name="IsStaffRequested"', re.I)
_NO_TIMES_RE = re.compile(r"no times? (?:are )?available", re.I)
_DAY_HEADER_RE = re.compile(r"<h3[^>]*>[^<]*\d", re.I)


class TimelyError(RuntimeError):
    """Raised when the funnel shape is not what recon documented (fail loudly)."""


class BudgetExhausted(TimelyError):
    """The per-run request cap, a throttle stop condition, the unanswered-request streak
    (an endpoint that stopped answering anyone, or answers everyone 5xx), the run-wide
    transport-failure cap, or the run-wide retry-time budget tripped.

    This is NOT a per-lookup failure: it means the rest of the run's data is missing, so it
    must never be laundered into a failure count that still reports the fetch as healthy.
    fetch_availability.fetch() catches it once, at the sweep level, and returns a result
    explicitly marked untrusted (ok=False, delete authority over nobody) — which keeps every
    downstream guard working and the fetch artifact written, rather than killing the process
    with a traceback on what is now an expected outcome of a throttle window.
    """


class _UnreadableRetryAfter(ValueError):
    """Retry-After was present but is neither finite delta-seconds nor an HTTP-date."""


def parse_retry_after(raw: str | None, *, now: datetime | None = None) -> float | None:
    """Seconds to wait, from either RFC 7231 form. None when the header is absent or blank.

    RFC 7231 permits `Retry-After: 120` *and* `Retry-After: Wed, 09 Aug 2026 14:30:00 GMT`.
    Only the first was parsed, so a dated header fell back to the 2-second backoff — we
    then retried five times inside 30 seconds while holding a note asking for 30 minutes,
    and MAX_RETRY_AFTER_SECONDS could not fire no matter how distant the date. That breaks
    the promise in docs/compliance.md that we never wait *less* than we were asked to.

    Raises _UnreadableRetryAfter when the header is present but unparseable: an unreadable
    back-off instruction is not permission to retry in two seconds.
    """
    if raw is None:
        return None
    s = raw.strip()
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        pass
    else:
        # float() also accepts "inf" and "nan". NaN silently defeats every comparison that
        # guards this value — `nan > MAX_RETRY_AFTER_SECONDS` is False and `max(2.0, nan)`
        # returns 2.0 — so it must never reach the caller as a number.
        if math.isfinite(v):
            return max(0.0, v)      # a negative or past value means "retry now"
        raise _UnreadableRetryAfter(s)
    try:
        when = parsedate_to_datetime(s)
    except (TypeError, ValueError, OverflowError):
        raise _UnreadableRetryAfter(s) from None
    if when is None:                # older Pythons return None instead of raising
        raise _UnreadableRetryAfter(s)
    if when.tzinfo is None:
        # A "-0000" offset parses naive, and subtracting naive from aware is a TypeError
        # that would kill the run with a traceback. RFC 7231 dates are UTC.
        when = when.replace(tzinfo=timezone.utc)
    return max(0.0, (when - (now or datetime.now(timezone.utc))).total_seconds())


@dataclass
class _UnansweredStreak:
    """One endpoint's run of consecutive exhausted requests, with what it takes to tell an
    outage from one stylist's fault, one service's fault, or a blip — see
    MAX_CONSECUTIVE_UNANSWERED_REQUESTS."""
    since: float                  # monotonic start of the streak's first attempt
    requests: int = 0
    staff: set = field(default_factory=set)      # stylist each request named, or None
    sessions: set = field(default_factory=set)   # TimelyClient each request came from


@dataclass
class RawSlot:
    date: str          # YYYY-MM-DD (local)
    service_id: str
    staff_id: str
    start_min: int     # minutes since local midnight
    end_min: int
    token: str         # opaque base64 (what the funnel would POST to book)


class _Budget:
    """Process-wide request budget + pacing, shared by every TimelyClient."""
    def __init__(self):
        self.made = 0
        self.cap = 10_000
        self.delay = 1.0
        # (method, path) -> consecutive fully-throttled requests for that endpoint.
        self.throttles: dict[tuple[str, str], int] = {}
        self.throttled_total = 0  # every fully-throttled request this run; never cleared
        # (method, path) -> that endpoint's run of consecutive requests that used up every
        # attempt with no usable answer (connection failure, timeout, 5xx); absent once
        # it answers again. Kept apart from `throttles`: see
        # MAX_CONSECUTIVE_UNANSWERED_REQUESTS.
        self.unanswered_streaks: dict[tuple[str, str], _UnansweredStreak] = {}
        # Every failed transport ATTEMPT this run (not 5xx); never cleared. See
        # MAX_TRANSPORT_FAILURES_PER_RUN.
        self.transport_failures = 0
        # Seconds this run has lost to failed attempts of every kind and the waits after
        # them; never cleared. See MAX_RETRY_SECONDS_PER_RUN.
        self.retry_seconds = 0.0
        self._last = 0.0

    def configure(self, cap: int, delay: float):
        self.cap = cap
        self.delay = delay
        # Reset the counters too: a long-lived process (or a test calling fetch twice)
        # would otherwise inherit the previous run's spend and trip the cap immediately.
        self.made = 0
        self.throttles = {}
        self.throttled_total = 0
        self.unanswered_streaks = {}
        self.transport_failures = 0
        self.retry_seconds = 0.0

    @staticmethod
    def _endpoint(method: str, url: str) -> tuple[str, str]:
        """Identify the endpoint a throttle or unanswered request belongs to, ignoring the
        per-session `obg`.

        Every funnel url carries a fresh `obg` query param, so keying on the full url would
        give each service its own counter and no streak could ever build. The path alone is
        stable across sessions. gettimeslots is the only path with a trailing slash, so it
        is normalized rather than left to key differently from its siblings by accident.
        """
        return method.upper(), (urlsplit(url).path.rstrip("/").lower() or "/")

    def tick(self, extra_delay: float = 0.0):
        if self.made >= self.cap:
            raise BudgetExhausted(
                f"request cap reached ({self.cap}) — the rest of this run's availability was "
                f"never fetched. Raise max_requests_per_run only if that is genuinely safe.")
        wait = (self.delay + extra_delay) - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.made += 1

    def throttled(self, method: str, url: str):
        """One fully-throttled REQUEST (all retries spent on 429s), not one attempt."""
        verb, path = self._endpoint(method, url)
        n = self.throttles.get((verb, path), 0) + 1
        self.throttles[(verb, path)] = n
        self.throttled_total += 1
        if n >= MAX_CONSECUTIVE_THROTTLES:
            raise BudgetExhausted(
                f"{n} consecutive throttled {verb} {path} requests from Timely — stopping "
                f"this run as the documented stop condition requires. Next run will retry.")
        if self.throttled_total >= MAX_THROTTLED_REQUESTS_PER_RUN:
            raise BudgetExhausted(
                f"{self.throttled_total} fully-throttled requests this run (across "
                f"{len([k for k, v in self.throttles.items() if v])} endpoints) — Timely is "
                f"throttling us broadly enough that the streak counter never trips. "
                f"Stopping; the next run will retry.")

    def unanswered(self, method: str, url: str, failure: str, *, staff: str | None,
                   session: int, since: float):
        """One REQUEST that used up every attempt with no usable answer: its last attempt
        a connection failure, a timeout or a 5xx (`failure` says which, for the stop
        message). `staff` is the stylist the request was about, None when it names none;
        `session` identifies the TimelyClient it came from; `since` is when its first
        attempt started.

        Extends the endpoint's streak, and raises BudgetExhausted once that streak is long
        enough AND can no longer be one stylist's fault, one service's fault or a blip —
        see MAX_CONSECUTIVE_UNANSWERED_REQUESTS for why each of those is ruled out first.
        Never feeds the throttle counters.
        """
        key = self._endpoint(method, url)
        streak = self.unanswered_streaks.get(key)
        if streak is None:
            streak = self.unanswered_streaks[key] = _UnansweredStreak(since=since)
        streak.requests += 1
        streak.staff.add(staff)
        streak.sessions.add(session)
        lasted = time.monotonic() - streak.since
        one_stylist = len(streak.staff) == 1 and None not in streak.staff
        one_session = len(streak.sessions) == 1
        if (streak.requests >= MAX_CONSECUTIVE_UNANSWERED_REQUESTS
                and not one_stylist and not one_session
                and lasted >= MIN_UNANSWERED_STREAK_SECONDS):
            verb, path = key
            named = sorted(s for s in streak.staff if s is not None)
            raise BudgetExhausted(
                f"{streak.requests} consecutive {verb} {path} requests "
                f"({f'for {len(named)} stylists, ' if named else ''}"
                f"from {len(streak.sessions)} sessions, over {lasted / 60:.0f} min) used "
                f"up every attempt without a usable answer (last: {failure}) — that "
                f"endpoint is not answering. Stopping this run rather than grinding "
                f"through it; the next run will retry.")

    def transport_failed(self, method: str, url: str, failure: str):
        """One ATTEMPT that got no response at all: a connection failure or a timeout.
        Counts toward MAX_TRANSPORT_FAILURES_PER_RUN whether or not its request then
        recovers. A 5xx attempt does not — see the note there."""
        self.transport_failures += 1
        if self.transport_failures >= MAX_TRANSPORT_FAILURES_PER_RUN:
            verb, path = self._endpoint(method, url)
            raise BudgetExhausted(
                f"{self.transport_failures} connection failures/timeouts this run (last: "
                f"{verb} {path}: {failure}) — each costs up to a 30s timeout plus "
                f"backoff, so a run absorbing more risks outrunning its job timeout. "
                f"Stopping; the next run will retry.")

    def retry_cost(self, method: str, url: str, failure: str, *, spent: float,
                   wait: float):
        """Charge one failed attempt of ANY kind to the run-wide retry-time budget:
        `spent`, the seconds the attempt itself took, plus `wait`, the backoff or
        Retry-After we are about to sleep before the next attempt (0 after the last one).

        Raises BudgetExhausted instead of letting that wait take the run past
        MAX_RETRY_SECONDS_PER_RUN, so the run stops at the failure that would overspend
        rather than after sleeping through it. Only the attempt itself, already spent
        when this is called, can overshoot the budget — by at most one attempt's timeout.
        """
        self.retry_seconds += max(0.0, spent)
        if self.retry_seconds + wait > MAX_RETRY_SECONDS_PER_RUN:
            verb, path = self._endpoint(method, url)
            raise BudgetExhausted(
                f"{self.retry_seconds / 60:.0f} min of this run already went on failed "
                f"attempts and the waits between retries (last: {verb} {path}: {failure}); "
                f"waiting {wait:.0f}s more would pass the "
                f"{MAX_RETRY_SECONDS_PER_RUN // 60}-min retry budget that keeps a run "
                f"inside its job timeout. Stopping; the next run will retry.")
        self.retry_seconds += wait

    def cleared(self, method: str, url: str):
        """A success only vouches for the endpoint it came from — see the note on
        MAX_CONSECUTIVE_THROTTLES for why a sibling's success must not clear this. It
        clears that endpoint's unanswered streak as well as its throttle streak: either
        way, the endpoint is answering again."""
        key = self._endpoint(method, url)
        self.throttles[key] = 0
        self.unanswered_streaks.pop(key, None)


BUDGET = _Budget()


def parse_service_catalog(html: str):
    """Return (services, service_staff_ids) from the landing page.

    services: [{service_id, name, staff_ids:[...], bookable_item_id}]
    service_staff_ids: {"ServiceStaffIds[..:SV]": "id,id,...", ...} to echo on POST.
    """
    services = []
    for sid, staffids, name, value in _SERVICE_RE.findall(html):
        services.append({
            "service_id": sid,
            "name": htmllib.unescape(name).strip(),
            "staff_ids": [s for s in staffids.split(",") if s],
            "bookable_item_id": value,
        })
    service_staff_ids = dict(_SERVICE_STAFF_RE.findall(html))
    if not services:
        raise TimelyError("no services parsed from landing page — catalog shape changed")
    return services, service_staff_ids


def decode_booking_selection(value: str) -> RawSlot | None:
    """Decode a BookingSelection base64 token.

    Layout: DATE,,<svc>:SV;<svc>;<groupId>;<staffId>;<startMin>;<endMin>;<n>
    """
    try:
        dec = base64.b64decode(value).decode("utf-8", "replace")
    except Exception:
        return None
    date = dec.split(",", 1)[0]
    parts = dec.split(";")
    if len(parts) < 6:
        return None
    try:
        return RawSlot(
            date=date,
            service_id=parts[1],
            staff_id=parts[3],
            start_min=int(parts[4]),
            end_min=int(parts[5]),
            token=value,
        )
    except (ValueError, IndexError):
        return None


def parse_time_slots(partial_html: str, *, strict: bool = True) -> list[RawSlot]:
    """Decode the bookable openings in a gettimeslots partial.

    `strict` guards the failure mode that silently empties the calendar: if Timely changes
    this partial's markup, every regex misses and we return [] — indistinguishable from a
    genuinely fully-booked day, and the sync would then delete real availability. So an
    empty result is only believed when the response still *looks* like the empty-day
    response we captured (a day header and/or the apology line).
    """
    slots, seen = [], set()
    for value in _BOOKING_SELECTION_RE.findall(partial_html):
        rs = decode_booking_selection(value)
        if rs is None:
            continue
        key = (rs.date, rs.start_min, rs.staff_id)
        if key in seen:
            continue
        seen.add(key)
        slots.append(rs)
    if not slots and strict and not _looks_like_empty_day(partial_html):
        at_endpoint = bool(_STAFF_FIELD_RE.search(partial_html))
        raise TimelyError(
            "gettimeslots returned neither bookable slots nor a recognizable empty-day "
            f"response ({len(partial_html)} bytes, "
            f"{'right endpoint — slot markup changed' if at_endpoint else 'not the slots partial at all'}). "
            "Refusing to report this as 'no availability'.")
    return slots


def _looks_like_empty_day(html: str) -> bool:
    """Does this response positively assert 'this day has no openings'?

    Only OUTCOME-specific markers count. `IsStaffRequested` deliberately does NOT appear
    here: it is the first line of the *populated* fixture too, so accepting it would mean
    accepting any response from this endpoint — including one whose slot markup changed
    out from under our regex, which is the exact failure this guard exists to catch.
    It is used separately, as a precondition, to tell "right endpoint, no slots" apart
    from "wrong page entirely".
    """
    return bool(_NO_TIMES_RE.search(html) or _DAY_HEADER_RE.search(html))


def _on_service_selection_page(html: str) -> bool:
    """Is this step 1 of the funnel — the service-selection form?

    Keyed ONLY on that form's own fields (the service checkboxes and the ServiceStaffIds
    hidden inputs), never on a /Booking/Service?obg= link: later pages link back to step
    1, so keying on the link would refuse every advanced page. Both funnel POSTs use this
    as their "did not advance" check; see the notes in select_service and select_staff
    for what each can and cannot prove.
    """
    return bool(_SERVICE_STAFF_RE.search(html) or _SERVICE_RE.search(html))


_SESSION_IDS = count(1)


class TimelyClient:
    def __init__(self, cache=None, cache_ns=""):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        # Which session a failed request came from, for the unanswered streak's "not one
        # service's fault" rule. A counter, not id(self): fetch() drops each service's
        # client for the next, and CPython reuses a freed object's id.
        self.session_no = next(_SESSION_IDS)
        self.obg: str | None = None
        self.cache = cache  # optional ResponseCache
        # Cache namespace: availability depends on the selected service + staff + date,
        # NOT on the per-session obg. Key on the service so reads cache across runs.
        self.cache_ns = cache_ns

    # -- HTTP with backoff -------------------------------------------------
    def _request(self, method, url, *, xhr=False, data=None, cache_key=None,
                 extra_delay=0.0, staff=None):
        """`staff` names the stylist the request is about (None if it names none), so
        a stylist-specific fault can be told apart from an endpoint outage — see
        MAX_CONSECUTIVE_UNANSWERED_REQUESTS."""
        if cache_key and self.cache:
            hit = self.cache.get(cache_key)
            if hit is not None:
                return hit
        headers = {}
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
        backoff = 2.0
        first_started = None
        for attempt in range(5):
            BUDGET.tick(extra_delay)
            last = attempt == 4
            # Measured from after the pacing sleep: what a FAILED attempt costs the run is
            # charged to BUDGET.retry_cost() below, and pacing is spent either way.
            started = time.monotonic()
            if first_started is None:
                first_started = started      # where an unanswered streak's clock starts
            try:
                resp = self.session.request(method, url, data=data, headers=headers,
                                            timeout=30)
            except (requests.ConnectionError, requests.Timeout) as e:
                # A connection that fails before any response arrives (refused, reset, or
                # closed before the status line) or a connect/read timeout is the most
                # ordinary transient failure there is, and it used to escape on the FIRST
                # attempt — only 429/5xx were retried. The 2026-09-05 run lost 4 lookups to
                # "Read timed out. (read timeout=30)", each charged as a failed lookup
                # (costing that stylist delete authority), and exited 1. So: the same
                # backoff and the same 5-attempt limit as a 5xx. There is no Retry-After on
                # a response we never received, so the plain backoff applies.
                #
                # Deliberately NOT reported to BUDGET.throttled(): the throttle stop
                # conditions mean "Timely asked us to slow down", and a timeout says no
                # such thing — at a streak of 3 and a run-wide total of 5, a few unlucky
                # reads would abort a healthy sweep as untrusted. BUDGET.transport_failed()
                # and, once the request is exhausted, BUDGET.unanswered() count them
                # instead, against thresholds sized for a network rather than a rate
                # limiter, and raise BudgetExhausted when one trips. That is not optional:
                # one request against a black-holed host now takes up to 5 x 30s + 30s of
                # backoff instead of 30s, and without those stop conditions one hung
                # endpoint pushed a deep sweep past the job's 120-min timeout (see
                # MAX_CONSECUTIVE_UNANSWERED_REQUESTS). Each attempt also still ticks the
                # budget above, so these retries can never take the run past the request
                # cap. Re-sending one of the two funnel POSTs books nothing — it re-selects
                # the same service or staff on an anonymous session (docs/compliance.md) —
                # and the "did not advance" checks in select_service/select_staff still vet
                # whatever page the retry lands on.
                #
                # Only these two families (requests files SSLError and ProxyError under
                # ConnectionError, so those retry too). NOT retried: a connection broken
                # MID-BODY, after the status line arrived — requests raises
                # ChunkedEncodingError ("Connection broken: IncompleteRead") for that,
                # which is not a ConnectionError. Waiting might well fix it, but widening
                # the retry set is a separate decision, so for now it fails its lookup on
                # the first attempt exactly as before. The rest of what requests raises —
                # an invalid url, a redirect loop — fails identically on every attempt, so
                # retrying it would only spend requests.
                failure = f"{type(e).__name__}: {e}"
                if last:
                    BUDGET.unanswered(method, url, failure, staff=staff,
                                      session=self.session_no, since=first_started)
                BUDGET.transport_failed(method, url, failure)
                wait = 0.0 if last else backoff
                BUDGET.retry_cost(method, url, failure,
                                  spent=time.monotonic() - started, wait=wait)
                if last:
                    raise TimelyError(
                        f"{method} {url} failed after 5 attempts: {failure}") from e
                time.sleep(wait)
                backoff *= 2
                continue
            if resp.status_code in (429, 500, 502, 503, 504):
                raw = resp.headers.get("Retry-After")
                try:
                    retry_after = parse_retry_after(raw)
                except _UnreadableRetryAfter:
                    raise BudgetExhausted(
                        f"{resp.status_code} from {url} with an unreadable Retry-After: "
                        f"{raw!r}. Stopping rather than inventing a shorter wait than we "
                        f"were asked for; the next run will retry.") from None
                # An over-long Retry-After is a stop condition on ANY status that carries
                # one, not just 429. A 503 with Retry-After: 3600 is what a Cloudflare
                # overload/rate-limit page looks like, and clamping that down to 120s would
                # mean retrying four times sooner than we were asked to — the opposite of
                # what docs/compliance.md promises.
                if retry_after is not None and retry_after > MAX_RETRY_AFTER_SECONDS:
                    raise BudgetExhausted(
                        f"{resp.status_code} from {url} with Retry-After: {retry_after:.0f}s "
                        f"(> {MAX_RETRY_AFTER_SECONDS}s). Stopping rather than holding the "
                        f"job open or retrying sooner than asked; the next run will retry.")
                failure = f"HTTP {resp.status_code}"
                if last and resp.status_code == 429:
                    # Count the exhausted REQUEST, not each attempt: incrementing per
                    # attempt made a single stubborn url trip the run-wide stop condition
                    # by itself, and made this branch unreachable for 429.
                    BUDGET.throttled(method, url)
                elif last:
                    # A request that ends on a 5xx got no usable answer, exactly like one
                    # that ends on a timeout, and extends the same streak. It used to count
                    # toward nothing, so an endpoint answering 503 to every request — with
                    # a Retry-After each lookup sat out four times — ran a deep sweep for
                    # hours, past the job timeout. Its ATTEMPTS are deliberately not
                    # transport failures: a 5xx arrives in a fraction of a second, so its
                    # cost is time, which retry_cost() below charges. See
                    # MAX_TRANSPORT_FAILURES_PER_RUN.
                    BUDGET.unanswered(method, url, failure, staff=staff,
                                      session=self.session_no, since=first_started)
                if last:
                    wait = 0.0
                elif retry_after is not None:
                    wait = max(backoff, retry_after)
                else:
                    wait = backoff
                # Every failed attempt, 429 included, is also charged to the run-wide
                # retry-time budget: no counter bounds how long the Retry-After waits take,
                # and a 429 or 5xx that recovers on a late attempt reaches no counter at
                # all (MAX_RETRY_SECONDS_PER_RUN).
                BUDGET.retry_cost(method, url, failure,
                                  spent=time.monotonic() - started, wait=wait)
                if last:
                    raise TimelyError(f"{resp.status_code} from {url} after retries")
                time.sleep(wait)
                backoff *= 2
                continue
            resp.raise_for_status()
            BUDGET.cleared(method, url)
            text = resp.text
            if cache_key and self.cache:
                self.cache.put(cache_key, text)
            return text
        raise TimelyError(f"unreachable: {url}")

    # -- Funnel ------------------------------------------------------------
    def bootstrap(self):
        """Cold GET the embed entry; capture obg + service catalog."""
        html = self._request("GET", EMBED)
        m = _OBG_RE.search(html)
        if not m:
            raise TimelyError("could not find obg on landing page")
        self.obg = m.group(1)
        return parse_service_catalog(html)

    def select_service(self, bookable_item_id: str, service_staff_ids: dict):
        form = {"LocationId": "0", "BookableTimeSlotItemIds": bookable_item_id, "commit": ""}
        form.update(service_staff_ids)
        html = self._request("POST", f"{BASE}/Booking/Service?obg={self.obg}", data=form,
                             extra_delay=POST_EXTRA_DELAY_SECONDS)
        # A 200 does not prove the funnel moved. Under load the edge can serve an
        # interstitial, or the session can reset to step 1 — and then GetOpenDates answers
        # a well-formed, empty {"openDates": []} for every staff id. Each lookup "succeeds"
        # with zero openings, those stylists keep full delete authority, and the sync
        # removes genuinely bookable times. Every GET in this module has a fail-loudly
        # shape guard; the two steps that actually mutate session state had none.
        #
        # Detected by what ONLY the service-selection page carries: its service checkboxes
        # and the ServiceStaffIds hidden inputs we just echoed back. Captured against the
        # live funnel 2026-08-09: both are absent once it advances. A link back to
        # /Booking/Service?obg=... IS still present on the advanced page, so `_OBG_RE` must
        # NOT be used here — it would fail every service on every run.
        if _on_service_selection_page(html):
            raise TimelyError(
                "POST /Booking/Service came back on the service-selection page — the funnel "
                "did not advance (session reset, or an edge interstitial). Refusing to read "
                "availability from a session still parked at step 1.")

    def select_staff(self, staff_id: str):
        html = self._request("POST", f"{BASE}/Booking/StaffSelection?obg={self.obg}",
                             data={"SelectedStaffId": str(staff_id), "commit": ""},
                             extra_delay=POST_EXTRA_DELAY_SECONDS, staff=str(staff_id))
        # The same hole select_service closed, one step later, and it had no guard at all:
        # if the session was reset between the two POSTs, this one lands back on step 1,
        # GetOpenDates answers a well-formed empty {"openDates": []} for every stylist, and
        # each lookup "succeeds" with zero openings while keeping delete authority.
        #
        # What this DOES detect: a reset to step 1, by the same service-form markup as
        # above. The DateSelection page this POST 302s to has never been captured, so this
        # rests on the pattern the step-2 capture shows
        # (tests/fixtures/post_service_advanced_2026-08-09.html): Timely keeps a back-LINK
        # to an earlier step but drops that step's form fields. Step 1's checkboxes and
        # ServiceStaffIds inputs are already gone one step on; only step 1 itself would
        # bring them back.
        #
        # What it does NOT detect, on purpose: this POST bouncing back to the staff page
        # itself (a rejected SelectedStaffId), or an edge interstitial that is neither
        # page. The staff page does have distinctive markup — `staff-selection-form`, the
        # `SelectedStaffId` radios, a form posting to /Booking/StaffSelection — but nothing
        # in this repo proves that markup is ABSENT from the DateSelection page. That same
        # back-link pattern makes a /Booking/StaffSelection?obg= link there likely, and a
        # "change stylist" control named SelectedStaffId is entirely plausible; a guard
        # keyed on either would fail every lookup of every run and take the calendar
        # down. Capture a live DateSelection response into tests/fixtures before adding a
        # positive "advanced" check, or a "still on step 2" check, here.
        if _on_service_selection_page(html):
            raise TimelyError(
                "POST /Booking/StaffSelection came back on the service-selection page — the "
                "funnel did not advance (the session was reset to step 1). Refusing to read "
                "availability from a session that has no service selected.")

    def open_dates(self, staff_id: str, month: int, year: int) -> list[dict]:
        url = (f"{BASE}/Booking/GetOpenDates?obg={self.obg}&month={month}&year={year}"
               f"&staffId={staff_id}&tzName=&tzId={TZ_ID}")
        text = self._request("GET", url, xhr=True, staff=str(staff_id),
                             cache_key=f"od:{self.cache_ns}:{staff_id}:{year}-{month}")
        try:
            data = json.loads(text)
        except json.JSONDecodeError:
            raise TimelyError(f"GetOpenDates did not return JSON for staff {staff_id}")
        # A present-but-empty openDates is legitimate (that month is fully booked). A
        # MISSING key is a shape change, and silently reading it as "no open days" would
        # skip every gettimeslots call — so the strict slot parser never runs, every lookup
        # "succeeds" with zero data, and the calendar gets emptied.
        if not isinstance(data, dict) or "openDates" not in data:
            raise TimelyError(
                f"GetOpenDates response has no 'openDates' key for staff {staff_id} "
                f"({sorted(data)[:8] if isinstance(data, dict) else type(data).__name__}) "
                f"— the API shape changed.")
        return data["openDates"]

    def time_slots(self, staff_id: str, date_iso: str) -> list[RawSlot]:
        url = (f"{BASE}/booking/gettimeslots/?obg={self.obg}&dateSelected={date_iso}"
               f"&staffId={staff_id}&tzName=&tzId={TZ_ID}")
        text = self._request("GET", url, xhr=True, staff=str(staff_id),
                             cache_key=f"ts:{self.cache_ns}:{staff_id}:{date_iso}")
        return parse_time_slots(text)
