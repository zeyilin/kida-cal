"""
Low-level Timely booking-funnel client for KIDA NYC.

Wraps the stateful cookie + `obg` funnel documented in docs/timely-api.md. No browser
required. One TimelyClient instance == one HTTP session (its own cookie jar), so create
a fresh client per service (service is baked into the session; staff/date are params).

Politeness: a shared request delay, exponential backoff on 429/5xx, and a hard per-run
request cap enforced across all clients via a module-level counter.
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
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "kida-cal/0.1 (+personal read-only availability mirror)")

_OBG_RE = re.compile(r"/Booking/Service\?obg=([0-9a-f-]{36})")
_SERVICE_RE = re.compile(
    r'<input[^>]*id="service(\d+)"[^>]*?data-staffids="([^"]*)"[^>]*?'
    r'data-name="([^"]*)"[^>]*?value="([^"]+)"', re.S)
_SERVICE_STAFF_RE = re.compile(
    r'name="(ServiceStaffIds\[\d+:SV\])"\s+[^>]*value="([^"]*)"')
_BOOKING_SELECTION_RE = re.compile(r'name="BookingSelection"[^>]*value="([^"]+)"')


class TimelyError(RuntimeError):
    """Raised when the funnel shape is not what recon documented (fail loudly)."""


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
        self._last = 0.0

    def configure(self, cap: int, delay: float):
        self.cap = cap
        self.delay = delay

    def tick(self):
        if self.made >= self.cap:
            raise TimelyError(f"request cap reached ({self.cap}); aborting to stay polite")
        wait = self.delay - (time.monotonic() - self._last)
        if wait > 0:
            time.sleep(wait)
        self._last = time.monotonic()
        self.made += 1


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


def parse_time_slots(partial_html: str) -> list[RawSlot]:
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
    return slots


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
    def _request(self, method, url, *, xhr=False, data=None, cache_key=None):
        if cache_key and self.cache:
            hit = self.cache.get(cache_key)
            if hit is not None:
                return hit
        headers = {}
        if xhr:
            headers["X-Requested-With"] = "XMLHttpRequest"
        backoff = 2.0
        for attempt in range(5):
            BUDGET.tick()
            resp = self.session.request(method, url, data=data, headers=headers, timeout=30)
            if resp.status_code in (429, 500, 502, 503, 504):
                if attempt == 4:
                    raise TimelyError(f"{resp.status_code} from {url} after retries")
                retry_after = resp.headers.get("Retry-After")
                try:
                    wait = max(backoff, float(retry_after)) if retry_after else backoff
                except ValueError:
                    wait = backoff
                time.sleep(wait)
                backoff *= 2
                continue
            resp.raise_for_status()
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
        self._request("POST", f"{BASE}/Booking/Service?obg={self.obg}", data=form)

    def select_staff(self, staff_id: str):
        self._request("POST", f"{BASE}/Booking/StaffSelection?obg={self.obg}",
                      data={"SelectedStaffId": str(staff_id), "commit": ""})

    def open_dates(self, staff_id: str, month: int, year: int) -> list[dict]:
        url = (f"{BASE}/Booking/GetOpenDates?obg={self.obg}&month={month}&year={year}"
               f"&staffId={staff_id}&tzName=&tzId={TZ_ID}")
        text = self._request("GET", url, xhr=True,
                             cache_key=f"od:{self.cache_ns}:{staff_id}:{year}-{month}")
        try:
            return json.loads(text).get("openDates", [])
        except json.JSONDecodeError:
            raise TimelyError(f"GetOpenDates did not return JSON for staff {staff_id}")

    def time_slots(self, staff_id: str, date_iso: str) -> list[RawSlot]:
        url = (f"{BASE}/booking/gettimeslots/?obg={self.obg}&dateSelected={date_iso}"
               f"&staffId={staff_id}&tzName=&tzId={TZ_ID}")
        text = self._request("GET", url, xhr=True,
                             cache_key=f"ts:{self.cache_ns}:{staff_id}:{date_iso}")
        return parse_time_slots(text)
