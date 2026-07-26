"""
Low-level Timely booking-funnel client for KIDA NYC.

Wraps the stateful cookie + `obg` funnel documented in docs/timely-api.md. No browser
required. One TimelyClient instance == one HTTP session (its own cookie jar), so create
a fresh client per service (service is baked into the session; staff/date are params).

Politeness: a shared request delay, exponential backoff on 429/5xx, and a hard per-run
request cap enforced across all clients via a module-level counter. When the host asks us
to back off for longer than we are willing to sleep, or keeps 429ing, we stop the run and
report rather than grinding — see docs/compliance.md.
"""
from __future__ import annotations

import base64
import html as htmllib
import json
import re
import time
from dataclasses import dataclass

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

# Consecutive fully-throttled REQUESTS (each having already spent all 5 retries) across the
# whole run that mean "we are not welcome right now". Must stay above the retry-loop length
# so that a single stubborn url cannot end the sweep on its own.
MAX_CONSECUTIVE_THROTTLES = 3

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
    """The per-run request cap or the throttle stop-condition tripped.

    This is NOT a per-lookup failure: it means the rest of the run's data is missing, so
    callers must re-raise it past their per-lookup `except Exception` handlers instead of
    laundering it into a failure count that still reports the fetch as healthy.
    """


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
        self.throttles = 0        # consecutive 429s seen this run
        self._last = 0.0

    def configure(self, cap: int, delay: float):
        self.cap = cap
        self.delay = delay
        # Reset the counters too: a long-lived process (or a test calling fetch twice)
        # would otherwise inherit the previous run's spend and trip the cap immediately.
        self.made = 0
        self.throttles = 0

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

    def throttled(self):
        """One fully-throttled REQUEST (all retries spent on 429s), not one attempt."""
        self.throttles += 1
        if self.throttles >= MAX_CONSECUTIVE_THROTTLES:
            raise BudgetExhausted(
                f"{self.throttles} consecutive throttled requests from Timely — stopping "
                f"this run as the documented stop condition requires. Next run will retry.")

    def cleared(self):
        self.throttles = 0


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


class TimelyClient:
    def __init__(self, cache=None, cache_ns=""):
        self.session = requests.Session()
        self.session.headers["User-Agent"] = UA
        self.obg: str | None = None
        self.cache = cache  # optional ResponseCache
        # Cache namespace: availability depends on the selected service + staff + date,
        # NOT on the per-session obg. Key on the service so reads cache across runs.
        self.cache_ns = cache_ns

    # -- HTTP with backoff -------------------------------------------------
    def _request(self, method, url, *, xhr=False, data=None, cache_key=None,
                 extra_delay=0.0):
        if cache_key and self.cache:
            hit = self.cache.get(cache_key)
            if hit is not None:
                return hit
        headers = {}
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
        backoff = 2.0
        for attempt in range(5):
            BUDGET.tick(extra_delay)
            resp = self.session.request(method, url, data=data, headers=headers, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504):
                retry_after = None
                raw = resp.headers.get("Retry-After")
                if raw:
                    try:
                        retry_after = float(raw)
                    except ValueError:
                        retry_after = None
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
                if attempt == 4:
                    if resp.status_code == 429:
                        # Count the exhausted REQUEST, not each attempt: incrementing per
                        # attempt made a single stubborn url trip the run-wide stop
                        # condition by itself, and made this branch unreachable for 429.
                        BUDGET.throttled()
                    raise TimelyError(f"{resp.status_code} from {url} after retries")
                wait = max(backoff, retry_after) if retry_after is not None else backoff
                time.sleep(wait)
                backoff *= 2
                continue
            resp.raise_for_status()
            BUDGET.cleared()
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
        self._request("POST", f"{BASE}/Booking/Service?obg={self.obg}", data=form,
                      extra_delay=POST_EXTRA_DELAY_SECONDS)

    def select_staff(self, staff_id: str):
        self._request("POST", f"{BASE}/Booking/StaffSelection?obg={self.obg}",
                      data={"SelectedStaffId": str(staff_id), "commit": ""},
                      extra_delay=POST_EXTRA_DELAY_SECONDS)

    def open_dates(self, staff_id: str, month: int, year: int) -> list[dict]:
        url = (f"{BASE}/Booking/GetOpenDates?obg={self.obg}&month={month}&year={year}"
               f"&staffId={staff_id}&tzName=&tzId={TZ_ID}")
        text = self._request("GET", url, xhr=True,
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
        text = self._request("GET", url, xhr=True,
                             cache_key=f"ts:{self.cache_ns}:{staff_id}:{date_iso}")
        return parse_time_slots(text)
