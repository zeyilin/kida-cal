"""
Sync open slots into a dedicated Google Calendar (the live path).

Idempotent: each calendar entry has a deterministic id derived from stylist + start, so an
opening maps to the same calendar event across runs. Each sync diffs the feed against the
calendar and applies the minimum changes.

Every entry of ours that the feed no longer contains ("stale") is first sorted into one of
four kinds, because they need opposite orderings:

  history   it has already ENDED                            delete in pass 1
  held      its stylist was looked for and not seen          left alone, and so are
            completely this run (see Safety)                 that stylist's writes
  booked    nothing in the feed stands in for it             delete in pass 1
  re-key    a BLOCK that a feed entry of the same stylist    delete in pass 4, and only
            overlaps (blocks key on their first opening, so  once EVERY overlapping
            booking or passing it re-keys the whole block)   replacement has landed

Only a block can be a re-key. A `slots` id IS its opening (stylist + start), so nothing in
the feed ever stands in for a stale slot — it is booked, held or history, even when an open
neighbour on Timely's 30-minute grid overlaps its 55 minutes.

An entry still in the feed under the same id is patched when its content changed. One kind
of patch is held back like a re-key: a block keeps its id while its first opening stands,
so a booking in its MIDDLE patches the head smaller and moves the tail into a new block.
That patch retires part of the old span exactly as a re-key retires all of it ("narrowing").

The passes then run in this order:

  1. delete booked + history     nothing in the feed stands in for these, so no write can
                                 make removing them safer; a booked entry left up is a
                                 publicly advertised appointment that is already taken
  2. patch                       in feed and on calendar, content changed (not narrowing)
  3. insert                      in feed, not on calendar (409 -> revive, below)
  4. narrowing patches, then     their availability IS still in the feed under another id;
     delete re-keys              shrinking or removing them before that entry is live
                                 would leave it advertised nowhere if its write failed, so
                                 each waits for EVERY overlapping entry to land (narrowing
                                 latest first, as each waits only on later entries)
  5. changeover purge            entries written under the other event_style, entry by
                                 entry under the same rule as 4, and refused outright if
                                 the openings that landed are implausibly few

Only pass 1 precedes the writes. Passes 4 and 5 retire things that something written in
2-3 replaces, so a run that aborts part-way leaves the old entries standing.

Why insert has a 409 fallback
-----------------------------
Google's events.delete is a *soft* delete: the event becomes status="cancelled" and its id
stays reserved for ~30 days. We list with showDeleted=False, so those tombstones are
invisible to us. The ordinary salon lifecycle — slot open, booked (we delete), customer
cancels, slot reopens — therefore produces an id that is absent from `existing` but still
reserved by Google, and a plain insert fails with 409 "The requested identifier already
exists.". events.update is a PUT and our bodies carry no "status", so it defaults back to
"confirmed" and revives the tombstone. This is Google's own documented remedy for 409.
The same fallback covers a retry whose original insert succeeded but whose response was lost.

Safety
------
* We only ever touch our own secondary calendar, never the primary.
* If the fetch did not clearly succeed (FetchResult.ok is False) we SKIP every delete, and
  main() refuses to write at all, so a transient Timely outage cannot wipe the calendar.
* Delete authority is per stylist. One we LOOKED FOR and could not see completely
  (in FetchResult.staff_planned but not staff_ok) is left as they are apart from entries
  that have already ended: no delete of anything current, no patch, no insert. What that
  withholds from deletion — their stale entries, and any block the partial view would
  shrink — counts toward skipped_delete and main()'s exit floor. One we never looked for
  (left the salon, dropped from Timely's catalog, excluded by config) has nothing a failed
  lookup could have hidden, so their entries are ordinary stale entries.
* Descriptions are rewritten only for stylists every one of whose lookups landed: a
  failed lookup means a service's times are unknown, not that it is gone.
* Staleness is judged against the window the FETCH used (FetchResult.window_start), never
  one recomputed from this process's clock, which may have crossed midnight since.
* Even on a clean fetch, a delete pass that would remove more than half of the upcoming
  in-window events is refused unless --allow-mass-delete is passed. The denominator is
  IN-WINDOW, not whole-calendar: a 21-day run sees only ~350 of ~3300 events, so a
  whole-calendar denominator would wave through a near-total wipe.
* Individual write failures are isolated and counted; one bad event no longer aborts the run.
  Auth/permission failures still abort immediately rather than retrying thousands of times.

Auth: two supported methods, selected automatically —
  1. Service account (preferred for unattended CI): set KIDA_SERVICE_ACCOUNT_JSON to the
     path of the SA key file. The target calendar must be shared with the SA's email with
     "Make changes to events". No token expiry. Requires config.calendar_id / KIDA_CALENDAR_ID
     (a service account can't create a calendar in your account).
  2. OAuth desktop (local/interactive): token cached at ~/.config/kida-cal/token.json,
     minted from client_secret.json (KIDA_GOOGLE_CLIENT_SECRET). Publish the OAuth app to
     avoid the 7-day refresh-token expiry if you use this in CI.
Never commit the SA key or token.
"""
from __future__ import annotations

import argparse
import json
import os
from datetime import date, datetime, timedelta
from datetime import time as dtime
from pathlib import Path
from zoneinfo import ZoneInfo

from .fetch_availability import Config, FetchResult, fetch
from .ics_export import entry_description
from .models import (BLOCK_PREFIX, ID_PREFIX, MARKER_ID, build_entries, event_from_dict,
                     event_to_dict)

SCOPES = ["https://www.googleapis.com/auth/calendar"]
LOCATION = "KIDA NYC, 369 Broome Street, New York, NY 10013"
TOKEN_PATH = Path(os.path.expanduser("~/.config/kida-cal/token.json"))
CLIENT_SECRET_ENV = "KIDA_GOOGLE_CLIENT_SECRET"       # path to client_secret.json (OAuth)
SERVICE_ACCOUNT_ENV = "KIDA_SERVICE_ACCOUNT_JSON"     # path to service-account key (preferred)

API_RETRIES = 5              # handled inside googleapiclient: jittered backoff on 429/5xx
MAX_CONSECUTIVE_FAILURES = 25   # circuit breaker: stop grinding through a systemic failure
DELETE_BLAST_RADIUS = 0.5       # refuse a delete pass larger than this share of the window
DELETE_BLAST_MIN = 150          # ...but only once the window is big enough to judge.
                                # Measured in OPENINGS: the live 21-day window is ~97
                                # block entries but ~303 openings, and openings are the
                                # only unit that does not move when event_style does.
FAILURE_EXIT_FLOOR = 5          # below this many write failures, still exit 0
FAILURE_EXIT_SHARE = 0.05
SKIPPED_DELETE_EXIT_FLOOR = 10  # withholding a few deletes is routine; withholding many
                                # means a stylist is stale on a public calendar


class WriteAborted(RuntimeError):
    """Raised when the sync stops early (auth failure or too many consecutive errors)."""


def using_service_account() -> bool:
    return bool(os.environ.get(SERVICE_ACCOUNT_ENV))


# ---------------------------------------------------------------- HTTP helpers
def _execute(request):
    """Run a Google API request.

    googleapiclient already implements jittered exponential backoff for 429, 5xx and
    403-rate-limit responses when given num_retries, so we do not hand-roll it. This
    wrapper exists only so the call site is uniform and the fake in tests has one seam.
    """
    return request.execute(num_retries=API_RETRIES)


def _status(exc) -> int | None:
    return getattr(getattr(exc, "resp", None), "status", None)


def _reasons(exc) -> set[str]:
    """Structured error reasons. Never substring-match str(exc): it contains the request
    URI, so a calendar id containing 'rate' would classify every error as rate limiting."""
    try:
        return {d.get("reason", "") for d in (exc.error_details or []) if isinstance(d, dict)}
    except Exception:
        return set()


# Short-term throttling: survivable, keep going (the consecutive-failure breaker still
# stops us if it persists).
_RATE_REASONS = {"rateLimitExceeded", "userRateLimitExceeded"}
# A exhausted daily/project quota: retrying every remaining event cannot succeed, so abort
# with a message that says so instead of blaming calendar sharing.
_QUOTA_REASONS = {"quotaExceeded", "dailyLimitExceeded"}


def _is_rate_limited(exc) -> bool:
    return bool(_reasons(exc) & _RATE_REASONS)


def _fatal_write_error(exc, what: str) -> str | None:
    """Why no write to this calendar can succeed right now, or None if `exc` is survivable.

    One classification for every write, so the marker stamp — the run's first write — and
    the sync's own writes cannot disagree about whether a 403 is worth carrying on past.
    """
    status = _status(exc)
    if status == 403 and _reasons(exc) & _QUOTA_REASONS:
        return (f"{what}: Google Calendar quota exhausted ({sorted(_reasons(exc))}). "
                f"Aborting; the quota resets on Google's schedule and the next run retries.")
    if status in (401, 403) and not _is_rate_limited(exc):
        return (f"{what}: {status} {sorted(_reasons(exc)) or exc}. Aborting rather than "
                f"repeating this for every remaining event — check calendar sharing/scopes.")
    return None


def _stylist_of(item: dict) -> str | None:
    """The stylist id we stamped on an event when we wrote it, if present.

    Entries written by older versions have no such stamp and return None, which callers
    must treat as "unknown", never as "not covered"."""
    return ((item.get("extendedProperties") or {}).get("private") or {}).get("stylist")


def _openings_of(item: dict) -> int:
    """How many openings an existing entry represents.

    Legacy entries written before this stamp existed were always one-opening `slots`
    entries, so 1 is the correct default rather than a guess."""
    raw = ((item.get("extendedProperties") or {}).get("private") or {}).get("openings")
    try:
        return max(1, int(raw))
    except (TypeError, ValueError):
        return 1


# ---------------------------------------------------------------- auth / service
def get_service():
    from googleapiclient.discovery import build

    sa_path = os.environ.get(SERVICE_ACCOUNT_ENV)
    if sa_path:
        # Preferred path: service account. No browser, no token refresh dance.
        from google.oauth2 import service_account
        creds = service_account.Credentials.from_service_account_file(sa_path, scopes=SCOPES)
        return build("calendar", "v3", credentials=creds, cache_discovery=False)

    # Fallback: OAuth desktop flow with a cached, refreshable token.
    from google.auth.transport.requests import Request
    from google.oauth2.credentials import Credentials
    from google_auth_oauthlib.flow import InstalledAppFlow

    creds = None
    if TOKEN_PATH.exists():
        creds = Credentials.from_authorized_user_file(str(TOKEN_PATH), SCOPES)
    if not creds or not creds.valid:
        if creds and creds.expired and creds.refresh_token:
            creds.refresh(Request())
        else:
            secret = os.environ.get(CLIENT_SECRET_ENV, "client_secret.json")
            flow = InstalledAppFlow.from_client_secrets_file(secret, SCOPES)
            creds = flow.run_local_server(port=0)
        TOKEN_PATH.parent.mkdir(parents=True, exist_ok=True)
        TOKEN_PATH.write_text(creds.to_json())
    return build("calendar", "v3", credentials=creds, cache_discovery=False)


def ensure_calendar(service, config: Config, dry_run: bool = False) -> str:
    """Return the target calendar id.

    Resolution order: KIDA_CALENDAR_ID env → config.calendar_id → (OAuth only) find-or-create
    a secondary calendar by name. A service account cannot create a calendar in your account,
    so it MUST be given an explicit id (of a calendar you created and shared with it).
    """
    cal_id = os.environ.get("KIDA_CALENDAR_ID") or config.calendar_id
    if cal_id:
        return cal_id
    if using_service_account():
        raise SystemExit(
            "Service-account auth requires an explicit calendar id. Create a calendar, share "
            "it with the service account's email ('Make changes to events'), and set "
            "KIDA_CALENDAR_ID (or config.calendar_id).")
    if dry_run:
        raise SystemExit(
            "No calendar id configured. Refusing to create one during --dry-run — set "
            "KIDA_CALENDAR_ID or config.calendar_id.")
    # OAuth only: look for an existing calendar with our name before creating a duplicate.
    page_token = None
    while True:
        cal_list = _execute(service.calendarList().list(pageToken=page_token))
        for entry in cal_list.get("items", []):
            if entry.get("summary") == config.calendar_name:
                return entry["id"]
        page_token = cal_list.get("nextPageToken")
        if not page_token:
            break
    created = _execute(service.calendars().insert(body={
        "summary": config.calendar_name,
        "timeZone": config.timezone,
        "description": "Auto-generated mirror of KIDA NYC open booking slots. "
                       "Read-only; confirm every slot on KIDA's site.",
    }))
    return created["id"]


def preflight(service, cal_id: str) -> None:
    """Prove we can reach the calendar BEFORE spending ~85 minutes fetching from Timely.

    Merely building the service proves nothing — for a service account it does no network
    I/O at all, which is why 59 failed runs each burned a full paced Timely sweep before
    discovering they could not write.
    """
    from googleapiclient.errors import HttpError
    try:
        _execute(service.calendars().get(calendarId=cal_id))
    except HttpError as e:
        raise SystemExit(
            f"cannot access calendar {cal_id!r} ({_status(e)}): {e}. Check that the calendar "
            f"is shared with the service account with 'Make changes to events'.")


# ---------------------------------------------------------------- event bodies
def entry_body(entry, config: Config, notices: str | None) -> dict:
    """Build the Calendar API body for one entry.

    Deliberately absent:
      * colorId — it overrides whatever colour a subscriber chose for the calendar, and they
        can never change it back. The palette it indexes is the 11-entry EVENT palette, not
        the calendar palette the config key name implied.
      * source — visible only to the event's *creator*, i.e. the service account. It reached
        zero human readers while costing bytes on every single write.
    """
    return {
        "id": entry.google_event_id(),
        "summary": entry.summary(),
        "location": LOCATION,
        "description": entry_description(entry, notices),
        "start": {"dateTime": entry.start.isoformat(), "timeZone": config.timezone},
        "end": {"dateTime": entry.end.isoformat(), "timeZone": config.timezone},
        "transparency": "transparent",          # shows as Free
        # Suppresses notifications for the service account only. Subscribers keep whatever
        # default their own calendar applies to this calendar.
        "reminders": {"useDefault": False, "overrides": []},
        # Stamped so the delete pass can establish per-stylist authority: if a stylist's
        # lookups failed this run, we can recognise their entries and leave them alone
        # instead of deleting on information we know is incomplete. `openings` lets a
        # future changeover size what it is retiring in openings rather than entries,
        # which is the only unit comparable across event_style. Neither is compared in
        # _needs_patch — both are derived from the entry and stable for a given id.
        "extendedProperties": {"private": {
            "stylist": str(entry.stylist_id),
            "openings": str(entry.opening_count),
        }},
    }


# Fields compared to decide whether an existing event needs rewriting. Keep this list
# minimal: any field Google normalizes on write (timeZone, source, …) will fail to
# round-trip and cause a full-calendar patch storm on every single run.
_PATCH_FIELDS = ("summary", "description", "location", "transparency")


def _needs_patch(existing: dict, desired: dict, compare_description: bool = True) -> bool:
    for k in _PATCH_FIELDS:
        if k == "description" and not compare_description:
            continue
        if existing.get(k) != desired.get(k):
            return True
    for k in ("start", "end"):
        if existing.get(k, {}).get("dateTime") != desired[k]["dateTime"]:
            return True
    return False


def _scheme(event_id: str) -> str:
    """Which id scheme an existing calendar event belongs to."""
    return "blocks" if event_id.startswith(BLOCK_PREFIX) else "slots"


def _parse_when(item: dict, key: str, tz) -> datetime | None:
    """The `start` or `end` instant of an existing calendar event, or None if unparseable.

    Guards TypeError as well as ValueError: comparing a naive datetime against an aware
    horizon raises TypeError, which would otherwise kill the whole delete phase.
    """
    s = item.get(key) or {}
    raw = s.get("dateTime") or s.get("date")
    if not raw:
        return None
    try:
        d = datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None
    if d.tzinfo is None:          # all-day ("date") or an unexpected naive value
        d = d.replace(tzinfo=tz)  # assume salon-local
    return d


def _parse_start(item: dict, tz) -> datetime | None:
    return _parse_when(item, "start", tz)


def _parse_end(item: dict, tz) -> datetime | None:
    return _parse_when(item, "end", tz)


def _overlaps(start: datetime, end: datetime, other_start: datetime,
              other_end: datetime) -> bool:
    """Whether [start, end) and [other_start, other_end) share any instant.

    A legacy item with no parsable end is given end == start, and a zero-length span can
    never pass the half-open test — so it is treated as the single instant `start` instead,
    or such an item could never be recognised as superseded by the entry that replaces it.
    """
    if end <= start:
        return other_start <= start < other_end
    return other_start < end and start < other_end


def _list_all(service, cal_id: str):
    """Every event on the calendar. Deliberately unbounded in time: this listing is how
    past events get pruned, so adding timeMin would let dead history accumulate forever."""
    page_token = None
    while True:
        resp = _execute(service.events().list(
            calendarId=cal_id, showDeleted=False, singleEvents=True,
            maxResults=2500, pageToken=page_token))
        for item in resp.get("items", []):
            yield item
        page_token = resp.get("nextPageToken")
        if not page_token:
            break


# ---------------------------------------------------------------- status marker
def read_marker(service, cal_id: str) -> dict | None:
    """The status marker, or None if there is none.

    events.get returns a soft-deleted event (status="cancelled") rather than a 404, so a
    marker someone deleted by hand would otherwise keep answering for the last deep sweep
    and the last run start — the opposite of what deleting it was meant to do.
    """
    from googleapiclient.errors import HttpError
    try:
        marker = _execute(service.events().get(calendarId=cal_id, eventId=MARKER_ID))
    except HttpError as e:
        if _status(e) in (404, 410):
            return None
        raise
    if not marker or marker.get("status") == "cancelled":
        return None
    return marker


def _marker_private(marker: dict | None) -> dict:
    return ((marker or {}).get("extendedProperties") or {}).get("private") or {}


def _marker_time(marker: dict | None, key: str) -> datetime | None:
    """A timestamp stored on the marker, or None if absent or unreadable. Unreadable is
    deliberately the same as absent: corrupt state must never read as 'just happened'."""
    raw = _marker_private(marker).get(key)
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def _elapsed(since: datetime | None, now: datetime) -> timedelta | None:
    if since is None:
        return None
    if since.tzinfo is None:       # hand-edited marker; assume the clock it was compared to
        since = since.replace(tzinfo=now.tzinfo)
    return now - since


def marker_last_deep_sweep(marker: dict | None) -> datetime | None:
    return _marker_time(marker, "last_deep_sweep")


def marker_last_attempt(marker: dict | None) -> datetime | None:
    """When the last non-skipped run STARTED. Markers written before last_attempt existed
    only carry last_run (when a run finished), which is the closest thing they have."""
    return _marker_time(marker, "last_attempt") or _marker_time(marker, "last_run")


def marker_last_deep_attempt(marker: dict | None) -> datetime | None:
    """When the last run that set out to sweep DEEP started, whether or not it finished.
    Compared with last_deep_sweep (stamped only by a deep sweep that completed cleanly, at
    its finish) it tells resolve_depth whether that attempt failed."""
    return _marker_time(marker, "last_deep_attempt")


def stamp_last_attempt(service, cal_id: str, marker: dict, at: datetime,
                       deep: bool = False) -> None:
    """Record on the existing marker that a run started, before it sends Timely anything.

    Stamped at the START rather than with the rest of the marker at the end, so a run that
    dies part-way — an untrusted fetch, a throttle abort, the job timeout — still counts as
    an attempt: the skip gate exists precisely so the next tick does not pile straight back
    onto Timely after one of those. The whole private map is sent back with only the stamp
    changed, so nothing else on the marker (last_run, last_deep_sweep) is lost whether
    Google merges the map or replaces it. A deep run also stamps last_deep_attempt with the
    same instant, which is how the next auto run learns that a deep sweep started and never
    completed (see resolve_depth).

    This is also the run's first WRITE, and preflight only proved a read. A refusal that
    every later write would hit too — a calendar shared read-only, an exhausted quota —
    raises WriteAborted here, before Timely is sent anything. Carrying on instead spent a
    full sweep on a run bound to fail, and because nothing was stamped the gate never
    skipped the next tick either: a sweep per tick for as long as the refusal lasted.
    Anything transient (429, 5xx, a rate-limit 403) only warns: the sync's own writes may
    well get through, and all that is lost is one tick's skip.
    """
    from googleapiclient.errors import HttpError
    private = dict(_marker_private(marker), last_attempt=at.isoformat())
    if deep:
        private["last_deep_attempt"] = at.isoformat()
    try:
        _execute(service.events().patch(calendarId=cal_id, eventId=MARKER_ID,
                                        body={"extendedProperties": {"private": private}}))
    except HttpError as e:
        fatal = _fatal_write_error(e, "stamping last_attempt on the status marker")
        if fatal:
            raise WriteAborted(fatal) from e
        print(f"::warning::could not stamp last_attempt on the status marker "
              f"({_status(e)} {sorted(_reasons(e)) or ''}); continuing — at worst the next "
              f"tick is not skipped")


def write_marker(service, cal_id: str, *, checked_at: datetime, entry_count: int,
                 last_deep: datetime | None, last_attempt: datetime | None = None,
                 last_deep_attempt: datetime | None = None,
                 succeeded: bool = True, previous_success: datetime | None = None,
                 dry_run: bool = False) -> None:
    """Publish an all-day 'last checked' event.

    Without this the calendar looks equally healthy whether it was synced 5 minutes or 5 days
    ago — which is exactly how a 3-day outage went unnoticed. Two small marker writes per
    run (main()'s start stamp, then this); never stamp a timestamp into per-event
    descriptions, which would rewrite the whole calendar hourly.

    `entry_count` is OPENINGS, despite the name, so the title reads the same under either
    event_style.

    last_run is the last SUCCESSFUL sync — a run main() is about to exit 0 from. The
    workflow opens an issue off its age and pings the heartbeat on a skipped tick only
    while it is fresh, so a run that will exit 1 passes succeeded=False and the previous
    value is carried forward. Stamping it on every run made a persistent failure read as
    an hour old forever: no issue, and the skipped ticks behind each failure fed the
    dead-man's switch.
    """
    from googleapiclient.errors import HttpError

    if last_deep is not None and last_deep.tzinfo is None:
        # A hand-edited marker. _elapsed() already reads it as salon-local; subtracting it
        # from the aware checked_at raised TypeError after every calendar write, on every
        # run, until a deep sweep replaced it. Written back aware.
        last_deep = last_deep.replace(tzinfo=checked_at.tzinfo)
    last_run = checked_at if succeeded else previous_success
    day = checked_at.date()
    age_note = ""
    if last_deep:
        hours = (checked_at - last_deep).total_seconds() / 3600
        age_note = f"Last full 90-day sweep: {hours:.0f}h ago.\n"
    if not succeeded:
        age_note += ("This run did not finish cleanly; the last clean sync was "
                     + (f"at {last_run:%Y-%m-%d %-I:%M %p %Z}.\n" if last_run else
                        "not recorded.\n"))
    body = {
        "id": MARKER_ID,
        "summary": f"✓ KIDA slots checked {checked_at:%-I:%M %p} · {entry_count} open",
        "description": (
            f"This calendar was last refreshed at {checked_at:%Y-%m-%d %-I:%M %p %Z}.\n"
            f"{age_note}"
            "If this date is not today, the sync is broken and the openings shown below "
            "are stale — confirm everything on KIDA's site."),
        "start": {"date": day.isoformat()},
        "end": {"date": (day + timedelta(days=1)).isoformat()},
        "transparency": "transparent",
        "reminders": {"useDefault": False, "overrides": []},
        # Only record a deep-sweep time we actually know. Coercing None to `checked_at`
        # meant a `--depth near` dispatch against a marker-less calendar would claim a full
        # sweep had just happened, suppressing the real one for deep_sweep_every_hours.
        # An absent key keeps meaning "unknown", which resolve_depth already reads as
        # "go deep". last_run, the same way, is only ever a success we know of (above).
        #
        # This body REPLACES the marker, so last_attempt must be written back or the stamp
        # main() put there at the start of this run is erased. It is the run's START, not
        # checked_at: the skip gate measures start-to-start, and recording the finish would
        # push every later window back by however long this run took — a 38-minute deep
        # sweep would make the next on-time tick, 22 minutes after it ended, look too soon.
        # last_deep_attempt is carried for the same reason: a near run that dropped it
        # would hide the failed deep sweep before it from the next auto decision.
        "extendedProperties": {"private": dict(
            **({"last_run": last_run.isoformat()} if last_run else {}),
            **({"last_deep_sweep": last_deep.isoformat()} if last_deep else {}),
            **({"last_attempt": last_attempt.isoformat()} if last_attempt else {}),
            **({"last_deep_attempt": last_deep_attempt.isoformat()}
               if last_deep_attempt else {}))},
    }
    if dry_run:
        return
    try:
        _execute(service.events().insert(calendarId=cal_id, body=body))
    except HttpError as e:
        if _status(e) != 409:
            raise
        _execute(service.events().update(calendarId=cal_id, eventId=MARKER_ID, body=body))


def resolve_depth(config: Config, requested: str, marker: dict | None,
                  now: datetime | None = None) -> tuple[int, bool]:
    """Return (lookahead_days, is_deep_sweep).

    'auto' decides from *elapsed time since the last deep sweep*, read off the marker event,
    rather than from the wall clock. The old `date -u +%H % 6` scheme assumed the cron fires:
    over one measured 180-hour window GitHub delivered 56% of hourly ticks, 00 UTC never fired
    once, and deep sweeps landed 14 times against 30 intended. An elapsed-time check is immune
    to dropped ticks, cron drift, and a long run pushing the next one past its hour.

    Takes the marker main() already read rather than fetching its own: the skip gate, this
    decision and the final deep-sweep stamp must all judge the SAME copy.

    A deep sweep that is due is still put off for ONE run when the run before this one was
    a deep sweep that never completed (last_deep_attempt is the latest attempt, and no
    last_deep_sweep followed it). Only a clean deep sweep records itself, so re-picking
    deep every time meant that while deep sweeps kept failing no near sweep ran at all —
    and the condition that fails a ~1575-request deep sweep (the fetch aborts a run after
    a handful of fully-throttled requests) often spares a ~240-request near one. Days 0-21
    then went unrefreshed, booked openings stayed advertised, and every run exited 2.
    Alternating keeps the near window fresh every other run while deep keeps being retried.
    """
    if requested == "near":
        return config.near_lookahead_days, False
    if requested == "full":
        return config.lookahead_days, True

    now = now or datetime.now(ZoneInfo(config.timezone))
    since = _elapsed(marker_last_deep_sweep(marker), now)
    if since is not None and since < timedelta(hours=config.deep_sweep_every_hours):
        return config.near_lookahead_days, False
    if _last_attempt_was_a_failed_deep_sweep(marker, now):
        print("depth: a deep sweep is due, but the last run was one that did not complete; "
              "sweeping near this time so days 0-"
              f"{config.near_lookahead_days} stay fresh, and deep again on the next run")
        return config.near_lookahead_days, False
    return config.lookahead_days, True


def _last_attempt_was_a_failed_deep_sweep(marker: dict | None, now: datetime) -> bool:
    """Whether the most recent run set out to sweep deep and recorded no deep sweep after.

    A clean deep sweep stamps last_deep_sweep at its FINISH, never before its start, so
    one at or after last_deep_attempt means that attempt completed. Absent or unreadable
    stamps read as "no failed deep attempt known", which leaves the old rule — go deep
    whenever it is due — in charge.
    """
    def aware(d: datetime | None) -> datetime | None:
        return d.replace(tzinfo=now.tzinfo) if d is not None and d.tzinfo is None else d

    tried = aware(marker_last_deep_attempt(marker))
    if tried is None:
        return False
    latest = aware(marker_last_attempt(marker))
    if latest is not None and latest > tried:
        return False                     # a run since then: deep has its turn again
    done = aware(marker_last_deep_sweep(marker))
    return done is None or done < tried


# ---------------------------------------------------------------- diff + apply
def sync(config: Config, result: FetchResult, service=None, dry_run=False,
         allow_mass_delete=False) -> dict:
    from googleapiclient.errors import HttpError

    tz = ZoneInfo(config.timezone)
    entries = build_entries(result.events, config.event_style)
    staff_ok, staff_planned = result.staff_ok, result.staff_planned

    # A failed notices scrape returns None (distinct from "" for a genuinely empty banner).
    # The banner text is an input to every description, so treating a 20-second kidanyc.com
    # blip as "banner cleared" would rewrite all ~3300 events, then rewrite them back next run.
    # A description is only rewritten when we saw EVERY service its stylist offers. A
    # failed service lookup means we do not know whether it is bookable — not that it isn't
    # — and dropping it from the description asserts the stronger claim. Timely
    # persistently 429s two /Booking/Service POSTs, so without this ~16 entries flip-flop
    # between two description states every hour: the service vanishes on a failed run and
    # returns on the next.
    #
    # Judged per STYLIST wherever the fetch says whose lookups failed. Every failure is
    # charged to the stylists it hid (fetch() puts all of a failed service's eligible staff
    # in staff_failed), and those stylists are withheld whole below — so the descriptions
    # still compared all belong to someone every one of whose lookups landed. Freezing
    # everyone's instead, as a single global "any lookup failed" did, kept a covered
    # stylist's calendar offering a service at a time it had moved away from on every run
    # where anything anywhere failed. Only a result that does not say whose (staff_ok None,
    # a capture from before it existed) still freezes them all.
    def _compare_description(who: str) -> bool:
        if result.notices is None:
            return False
        if result.lookups_failed == 0:
            return True
        return staff_ok is not None and who in staff_ok

    if result.lookups_failed and staff_ok is None:
        print(f"::warning::{result.lookups_failed} service lookup(s) failed and this result "
              f"does not say whose; leaving existing descriptions alone rather than "
              f"asserting those services are gone")
    desired = {e.google_event_id(): entry_body(e, config, result.notices) for e in entries}
    # Side tables keyed the same way as `desired`, so the write passes can reason about an
    # entry's stylist, size and time span without re-deriving them from the body.
    _entry_stylist = {e.google_event_id(): str(e.stylist_id) for e in entries}
    compare_desc = {eid: _compare_description(who) for eid, who in _entry_stylist.items()}
    _entry_openings = {e.google_event_id(): e.opening_count for e in entries}
    _spans: dict[str, list[tuple[datetime, datetime, str]]] = {}
    for e in entries:
        _spans.setdefault(str(e.stylist_id), []).append((e.start, e.end, e.google_event_id()))

    stats = {"insert": 0, "revive": 0, "patch": 0, "delete": 0, "migrated": 0,
             "unchanged": 0, "skipped_delete": 0, "blocked_delete": 0,
             "failed_insert": 0, "failed_patch": 0, "failed_delete": 0,
             # Not a write counter: openings across EVERYTHING of ours still live on the
             # calendar once this run is done, which is what the status marker reports.
             "live_openings": 0}

    if dry_run and service is None:
        # No API access: just report what we'd publish.
        stats["insert"] = len(desired)
        stats["live_openings"] = sum(_entry_openings.values())
        print(f"[dry-run] would publish {len(desired)} entries "
              f"(no calendar access to diff against)")
        for e in entries[:10]:
            print(f"  INSERT {e.start:%a %m-%d %H:%M} {e.summary()}")
        if len(entries) > 10:
            print(f"  ... and {len(entries) - 10} more")
        return stats

    cal_id = ensure_calendar(service, config, dry_run=dry_run)

    # Load our existing events. Only ids carrying our prefix are ours; the marker is state,
    # not a slot; and entries written under the *other* event_style are obsolete by
    # construction (an event_style changeover) and are handled by a separate, later pass.
    existing: dict[str, dict] = {}
    obsolete: dict[str, dict] = {}
    for item in _list_all(service, cal_id):
        eid = item.get("id", "")
        if not eid.startswith(ID_PREFIX) or eid == MARKER_ID:
            continue
        (existing if _scheme(eid) == config.event_style else obsolete)[eid] = item

    now = datetime.now(tz)

    # The horizon is the END of the last day the FETCH covered, measured from the day the
    # fetch called "today" — not from this clock. A near run whose fetch starts at 23:50
    # and whose sync runs at 00:10 otherwise reaches one day further than anything it
    # looked at, and deletes that whole unfetched day as "no longer offered". And it is a
    # date boundary, not a datetime: fetch() filters by DATE (day_date > horizon), so a
    # datetime horizon would leave later-in-the-day slots on the final day permanently
    # undeletable — absent from `desired`, but past the cutoff.
    window_start: date | None = result.window_start
    if window_start is None:
        window_start = now.date()
        print(f"::warning::fetch result does not say which day its window started (a "
              f"capture from before window_start was recorded); assuming {window_start}. "
              f"If the fetch began before midnight, entries on the day after its window "
              f"will be judged stale.")
    last_day = window_start + timedelta(days=config.lookahead_days)
    horizon = datetime.combine(last_day + timedelta(days=1), dtime.min, tzinfo=tz)

    starts = {eid: _parse_start(item, tz) for eid, item in existing.items()}
    obs_starts = {eid: _parse_start(item, tz) for eid, item in obsolete.items()}
    # An entry's END is what makes it history. A legacy item with no parsable end is given
    # its start, i.e. it becomes history the moment it begins — which is how every entry
    # was treated before ends were consulted at all.
    ends = {eid: _parse_end(item, tz) or starts[eid] for eid, item in existing.items()}
    obs_ends = {eid: _parse_end(item, tz) or obs_starts[eid]
                for eid, item in obsolete.items()}

    # Deletes are window-scoped so a short near-term run can't wipe the deep run's far-out
    # events. That applies to the obsolete bucket too: a near-tier run that meets a style
    # changeover must not purge 90 days of entries it can only replace 21 days of.
    # Strictly before: `horizon` is the first instant of the first day the fetch did NOT
    # cover, so an entry starting exactly then was never looked for.
    in_window = [eid for eid, d in starts.items() if d is not None and d < horizon]
    obsolete_in_window = [eid for eid, d in obs_starts.items()
                          if d is not None and d < horizon]

    # Delete authority is per stylist where we can establish it. An event we wrote carries
    # its stylist id, so if that person's lookups failed this run we simply leave their
    # entries alone rather than deleting on information we know is incomplete. Without this
    # a single flaky stylist (the busiest owns 9 of 60 lookups = 15%, inside the 20% slack
    # that still reports ok=True) would be wiped off the public calendar.
    def _uncovered(who: str | None) -> bool:
        """True for a stylist we LOOKED FOR this run and could not see completely.

        Absence from staff_ok alone is not that. staff_ok can only ever contain stylists
        the fetch planned a lookup for, so someone who left the salon, was dropped from
        Timely's catalog, or is excluded by config.stylists is never in it — and reading
        that as "failed" held their entries forever, with skipped_delete over the exit
        floor failing every hourly run for the 90 days it took them to age out. Only when
        the plan is unknown (a capture from before staff_planned existed) do we fall back
        to the old, stricter rule.

        An unstamped item (written by an older version) reports None, which means UNKNOWN
        provenance, not "not covered" — it must stay deletable or nothing written before
        the stamp existed could ever be pruned.
        """
        if staff_ok is None or who is None:
            return False
        if staff_planned is not None:
            return who in staff_planned and who not in staff_ok
        return who not in staff_ok

    # Sort every stale in-window entry into its kind (the module docstring has the table).
    # `wanted` is the feed BEFORE any withholding below: an uncovered stylist's unchanged
    # entry is not stale just because we decline to touch it.
    wanted = set(desired)
    history: list[str] = []
    held: list[str] = []
    booked: list[str] = []
    rekeyed: dict[str, list[str]] = {}     # stale id -> every desired id that overlaps it
    for eid in in_window:
        if eid in wanted:
            continue
        if ends[eid] < now:
            # Over. Nothing the fetch did or did not see can make it bookable again, so it
            # is deletable whoever it belongs to.
            history.append(eid)
            continue
        who = _stylist_of(existing[eid])
        # From here on, an entry that has STARTED is treated exactly like a future one.
        # Under `blocks` an in-progress block can still hold bookable openings (10:00-14:55
        # at 11:30 still offers 12:00 onward), and exempting it from authority and re-key
        # handling deleted a half-seen stylist's day with nothing written in its place.
        if _uncovered(who):
            held.append(eid)
            continue
        # Only a block is ever re-keyed. A slot's id is sha1(stylist|start) — the opening
        # itself — so nothing in the feed stands in for a stale one. On Timely's 30-minute
        # grid of 55-minute services an open neighbour overlaps a booked slot, and calling
        # that a re-key deferred the delete behind every insert, then kept an appointment
        # already taken advertised whenever the neighbour's own, unrelated write failed.
        over = ([k for s, e, k in _spans.get(who, ())
                 if _overlaps(starts[eid], ends[eid], s, e)]
                if who is not None and _scheme(eid) == "blocks" else [])
        if over:
            rekeyed[eid] = over
        else:
            booked.append(eid)

    # Withholding must be SYMMETRIC. Suppressing a stylist's deletes while still writing
    # their new entries publishes two overlapping blocks for the same stylist-day, the
    # older one still advertising an opening we know is booked — and PATCHING one of their
    # existing entries rewrites it from data we know is partial (a block keeps its id while
    # its first opening stands, so it shrinks to whatever this run saw, then grows back next
    # run). If we cannot see someone completely, we leave their whole day alone.
    blind_writes = [eid for eid in desired if _uncovered(_entry_stylist[eid])]
    withheld_writes = sum(1 for eid in blind_writes
                          if eid not in existing
                          or _needs_patch(existing[eid], desired[eid], compare_desc[eid]))
    # A withheld patch that would SHRINK an entry is a withheld delete of the openings it
    # gives up, and counts toward skipped_delete like one — exactly as pass 4 counts a split
    # block it keeps wide. Counting only stale entries let a stylist uncovered on every run
    # (one of their lookups failing persistently) keep a booked mid-block opening up for
    # good: the head stays and keeps its id, so nothing is stale, and every run exited 0.
    # A withheld insert or a growing patch only under-advertises, and stays a warning.
    withheld_shrinks = sum(1 for eid in blind_writes if eid in existing
                           and _openings_of(existing[eid]) > _entry_openings[eid])
    for eid in blind_writes:
        desired.pop(eid)
    # The changeover purge needs the SAME authority. It is the one delete path that runs
    # when `existing` is empty by construction, so the classification above cannot reach
    # it — and a changeover is exactly when a half-seen stylist would otherwise be erased
    # with no replacement written. History is exempt here too: it is over either way.
    held_obs = {eid for eid in obsolete_in_window
                if obs_ends[eid] >= now and _uncovered(_stylist_of(obsolete[eid]))}
    obsolete_in_window = [e for e in obsolete_in_window if e not in held_obs]
    # ...and the same per-ENTRY rule as a re-key. An obsolete entry that a feed entry of
    # the same stylist overlaps is that entry's predecessor in the other style, and may go
    # only once every one of them landed. The purge's own guard is an aggregate over the
    # whole calendar, so on its own it let one stylist's failed block be paid for by
    # everyone else's: the rest of the salon cleared the bar, and that stylist's
    # still-bookable openings were retired with nothing in their place. Unlike a stale
    # slot, an obsolete entry IS replaceable even when it is a slot: its successor is
    # written under the other style's id (the block a slot's opening now sits in, or the
    # slots a block breaks into).
    obs_rekeyed: dict[str, list[str]] = {}
    for eid in obsolete_in_window:
        who = _stylist_of(obsolete[eid])
        if who is None or obs_ends[eid] < now:
            continue                       # unknown provenance, or history: deletable
        over = [k for s, e, k in _spans.get(who, ())
                if _overlaps(obs_starts[eid], obs_ends[eid], s, e)]
        if over:
            obs_rekeyed[eid] = over
    if held or held_obs or withheld_writes:
        blind = sorted({_entry_stylist[eid] for eid in blind_writes}
                       | {_stylist_of(existing[eid]) for eid in held}
                       | {_stylist_of(obsolete[eid]) for eid in held_obs})
        shrinks = (f" ({withheld_shrinks} of them a block that would shrink)"
                   if withheld_shrinks else "")
        print(f"::warning::{len(blind)} stylist(s) could not be seen completely this run "
              f"({', '.join(blind)}); leaving them as they are: withholding "
              f"{len(held) + len(held_obs) + withheld_shrinks} delete(s){shrinks} and "
              f"{withheld_writes - withheld_shrinks} write(s)")
    stats["skipped_delete"] += len(held) + len(held_obs) + withheld_shrinks

    # Narrowing patches: an entry the feed still has under the same id, whose STORED span
    # other feed entries of the same stylist now share. A block keeps its id while its
    # first opening stands, so a booking mid-block patches the head smaller and moves the
    # tail into a new block — and patching first meant one failed tail insert (or a quota
    # abort before it) left the tail's openings advertised nowhere, with the old block
    # never stale for the re-key rule to protect. Feed blocks of one stylist never overlap
    # each other, so a shared stored span is exactly a span this patch would give away.
    # Blocks only, for the reason above: a slot's span is its own opening's.
    narrowing: dict[str, list[str]] = {}   # existing id -> desired ids taking over its span
    for eid, body in desired.items():
        item = existing.get(eid)
        if (item is None or _scheme(eid) != "blocks" or starts[eid] is None
                or not _needs_patch(item, body, compare_desc[eid])):
            continue
        others = [k for s, e, k in _spans.get(_entry_stylist[eid], ())
                  if k != eid and _overlaps(starts[eid], ends[eid], s, e)]
        if others:
            narrowing[eid] = others

    # ---- guards on the delete pass ----
    # The blast-radius ratio is over UPCOMING entries only. Anything that has already
    # started is stale for reasons that say nothing about fetch health — fetch() drops
    # started slots, and a block re-keys as its openings pass — so counting it would let
    # routine pruning trip the guard.
    upcoming = [eid for eid in in_window if starts[eid] >= now]
    upcoming_stale = [eid for eid in booked + list(rekeyed) if starts[eid] >= now]
    blocked = False
    if not result.ok:
        n = len(booked) + len(history) + len(rekeyed) + len(obsolete_in_window)
        stats["skipped_delete"] += n
        print(f"WARNING: fetch not ok (ok={result.lookups_ok} failed={result.lookups_failed}); "
              f"skipping {n} deletes to avoid wiping the calendar")
        blocked = True
    # Size the floor in OPENINGS, not entries. Under `blocks` the 21-day window is ~97
    # entries but ~303 openings, so an entry floor of 50 sits only 1.9x away — and
    # weekends_only, a min_slot_hour filter, or two stylists on leave would drop it under
    # the floor, silently disabling the guard entirely on exactly the calendars where a
    # wipe is least recoverable.
    elif (not allow_mass_delete
            and sum(_openings_of(existing[e]) for e in upcoming) >= DELETE_BLAST_MIN
            and len(upcoming_stale) > DELETE_BLAST_RADIUS * len(upcoming)):
        stats["blocked_delete"] = len(upcoming_stale)
        print(f"::error::refusing to delete {len(upcoming_stale)} of {len(upcoming)} "
              f"upcoming in-window events (> {DELETE_BLAST_RADIUS:.0%}). Fetch reported ok "
              f"but looks partial. Re-run with --allow-mass-delete if this really is correct.")
        blocked = True

    failures: list[str] = []
    landed: set[str] = set()     # desired ids that are on the calendar as written this run
    gone: set[str] = set()       # ids this run removed (or found already removed)
    consecutive = 0

    def guard(exc, kind: str, eid: str) -> None:
        """Record a per-item write failure, or abort if it is not survivable."""
        nonlocal consecutive
        status = _status(exc)
        fatal = _fatal_write_error(exc, f"{kind} {eid}")
        if fatal:
            raise WriteAborted(fatal)
        stats[f"failed_{kind}"] += 1
        failures.append(f"{kind} {eid}: {status} {sorted(_reasons(exc)) or ''}".strip())
        consecutive += 1
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            raise WriteAborted(
                f"{consecutive} consecutive write failures; aborting. Last: {failures[-1]}")

    def do_delete(eid: str, stat_key: str) -> None:
        nonlocal consecutive
        if dry_run:
            stats[stat_key] += 1
            gone.add(eid)
            return
        try:
            _execute(service.events().delete(calendarId=cal_id, eventId=eid))
        except HttpError as e:
            # Already gone is the state we wanted. googleapiclient's own retry can turn a
            # successful delete into a 404/410 on the retried call, so this is routine.
            if _status(e) in (404, 410):
                stats[stat_key] += 1
                gone.add(eid)
                consecutive = 0
                return
            guard(e, "delete", eid)
            return
        stats[stat_key] += 1
        gone.add(eid)
        consecutive = 0

    def do_patch(eid: str) -> None:
        nonlocal consecutive
        if dry_run:
            stats["patch"] += 1
            landed.add(eid)
            return
        try:
            _execute(service.events().patch(calendarId=cal_id, eventId=eid,
                                            body=desired[eid]))
        except HttpError as e:
            guard(e, "patch", eid)
            return
        stats["patch"] += 1
        landed.add(eid)
        consecutive = 0

    # ---- 1. deletes, first: nothing in the feed stands in for these ----
    # Booked before history: a booked entry is a publicly advertised appointment that is
    # already taken, so it must not be starved by a failure streak among mere clutter.
    if not blocked:
        for eid in booked + history:
            do_delete(eid, "delete")

    # ---- 2. patches ----
    # Every id in `landed` is an entry that is on the calendar exactly as this run wants it:
    # patched, inserted, revived, or already right. Only these may justify retiring anything
    # they replace. A write that failed is absent, and so is one never attempted because
    # its stylist was withheld above — no write, so nothing failed, but nothing landed.
    for eid, body in desired.items():
        item = existing.get(eid)
        if item is None:
            continue
        if not _needs_patch(item, body, compare_desc[eid]):
            stats["unchanged"] += 1
            landed.add(eid)
            continue
        if eid in narrowing:
            continue                 # pass 4, once what takes over its old span is live
        do_patch(eid)

    # ---- 3. inserts, with the 409 tombstone fallback ----
    for eid, body in desired.items():
        if eid in existing:
            continue
        if dry_run:
            stats["insert"] += 1
            landed.add(eid)
            continue
        try:
            _execute(service.events().insert(calendarId=cal_id, body=body))
        except HttpError as e:
            if _status(e) != 409:
                guard(e, "insert", eid)
                continue
            # The id is reserved by a tombstone (or by our own retried insert). update() is
            # a PUT and the body carries no "status", so it revives the event as confirmed.
            try:
                _execute(service.events().update(calendarId=cal_id, eventId=eid, body=body))
            except HttpError as e2:
                guard(e2, "insert", eid)
                continue
            stats["revive"] += 1
            landed.add(eid)
            consecutive = 0
            continue
        stats["insert"] += 1
        landed.add(eid)
        consecutive = 0

    # ---- 4. what the writes above supersede, now that they have had their chance ----
    # Each of these overlaps one or more entries of the same stylist that the feed still
    # wants, and may be shrunk or removed only once EVERY one of them landed. Checking a
    # single "replacement" per stylist-day picked the first entry of that day, so with a
    # morning and an evening block, a failed evening insert was excused by the untouched
    # morning one and the evening's still-bookable openings were advertised nowhere.
    #
    # A narrowing patch that has to wait keeps the wider old block, and a re-key keeps the
    # whole entry: both still advertise the opening that was booked out of them, which is
    # the lesser harm, and both count toward skipped_delete — retiring part of a span is a
    # delete of that part. Narrowing patches go first: a block kept whole by an earlier run
    # can overlap one being narrowed now, and its delete waits for that patch to land.
    # Unlike the deletes they are not stopped by the guards above, any more than pass 2's
    # patches are: everything that takes over their span has already landed.
    #
    # ...and they go LATEST first. A narrowing entry only ever waits on entries that start
    # after it — what overlaps its stored span, which begins at its own first opening, since
    # feed blocks of one stylist never overlap — and some of those may be narrowing too.
    # Walked earliest-first, X was checked before the later Y it waits on had been patched,
    # so a run where nothing failed still kept X wide: two booked openings advertised, and
    # a warning blaming a write failure that never happened. Latest-first, every narrowing
    # entry is settled before anything that waits on it is checked.
    kept_wide = 0
    for eid, others in sorted(narrowing.items(), key=lambda kv: starts[kv[0]], reverse=True):
        if all(r in landed for r in others):
            do_patch(eid)
        else:
            kept_wide += 1
            stats["skipped_delete"] += 1
    kept = 0
    if not blocked:
        for eid, replacements in rekeyed.items():
            if not all(r in landed for r in replacements):
                kept += 1
                stats["skipped_delete"] += 1
                continue
            do_delete(eid, "delete")
    if kept or kept_wide:
        # Not "failed to write": one failed insert can keep a chain of blocks waiting on
        # each other, so the count kept here is not the count of writes that failed.
        print(f"::warning::kept {kept} superseded entr(ies) whole and {kept_wide} split "
              f"block(s) at their old size: what takes over their span did not land this "
              f"run ({len(failures)} write(s) failed; one can hold back a chain of blocks "
              f"waiting on each other) — better a stale entry than unrepresented "
              f"availability")

    # ---- 5. the event_style changeover purge, LAST and separately guarded ----
    # These are entries written under the other style. They run after the inserts, not with
    # the deletes, so a changeover can only ever remove an entry once its replacement is
    # already live: if the write pass aborts partway (quota, auth, a failure streak) the old
    # entries are still standing. Doing this first — as the delete pass — meant an abort left
    # the public calendar emptied with nothing written back. Two checks, because they catch
    # different failures: the aggregate below catches a fetch that lost most of the salon,
    # and the per-entry one (obs_rekeyed) catches a single stylist's replacement failing
    # while everyone else's landed.
    if obsolete_in_window and not blocked:
        # Size BOTH sides in openings, and count only the entries that actually LANDED.
        #
        # Entries are not comparable across styles (blocks are ~3.3x fewer for the same
        # availability), so an entry-count comparison would refuse a healthy slots->blocks
        # changeover and wave through a blocks->slots one that had lost most of its data.
        # And counting entries we merely intended to write would let a run that failed 200
        # of its inserts still retire everything those inserts were meant to replace — or
        # credit it with a withheld stylist's openings, which were never attempted at all
        # (the busiest stylist alone is ~15% of the salon).
        live_now = sum(_entry_openings[eid] for eid in landed)
        retiring = sum(_openings_of(obsolete[eid]) for eid in obsolete_in_window)
        if not allow_mass_delete and live_now < DELETE_BLAST_RADIUS * retiring:
            # The replacement set is implausibly small next to what we are about to remove.
            # This is the shape of a partial fetch on a changeover run, which would
            # otherwise wipe the calendar and still exit 0.
            stats["blocked_delete"] += len(obsolete_in_window)
            print(f"::error::refusing to retire {len(obsolete_in_window)} entries "
                  f"({retiring} openings) from the previous event_style: the "
                  f"{len(landed)} replacement entries that landed cover only "
                  f"{live_now} openings. Fix the fetch first, or re-run with "
                  f"--allow-mass-delete.")
        else:
            print(f"retiring up to {len(obsolete_in_window)} entries ({retiring} openings) "
                  f"from the previous event_style ({len(landed)} entries covering "
                  f"{live_now} openings are live)")
            kept_obs = 0
            for eid in obsolete_in_window:
                replacements = obs_rekeyed.get(eid)
                if replacements and not all(r in landed for r in replacements):
                    kept_obs += 1
                    stats["skipped_delete"] += 1
                    continue
                do_delete(eid, "migrated")
            if kept_obs:
                print(f"::warning::kept {kept_obs} entr(ies) of the previous event_style "
                      f"whose replacement did not land this run; the next run retires them")

    if failures:
        print(f"::warning::{len(failures)} write(s) failed; first 20:")
        for f in failures[:20]:
            print(f"  {f}")

    # What a subscriber can now see, in openings: everything that landed, plus every entry
    # of ours this run left standing and that has not ended — the out-of-window days a near
    # run never looked at, a withheld stylist's day, a superseded entry kept because its
    # replacement failed. Counting only this run's own entries reported 21 days of a 90-day
    # calendar on every near run, understating it several-fold and jumping at each deep
    # sweep. Landed entries count at their new size; the rest at their stamped one, less
    # whatever their own landed replacements already advertise — a kept re-key and the
    # replacements that DID land show the same openings twice, and on a run whose deletes
    # the blast-radius guard refused, that is every re-keyed stylist-day.
    left = {**existing, **obsolete}
    left_ends = {**ends, **obs_ends}
    superseded = {**rekeyed, **narrowing, **obs_rekeyed}

    def still_shown(eid: str, item: dict) -> int:
        shared = sum(_entry_openings[r] for r in superseded.get(eid, ()) if r in landed)
        return max(0, _openings_of(item) - shared)

    stats["live_openings"] = (
        sum(_entry_openings[eid] for eid in landed)
        + sum(still_shown(eid, item) for eid, item in left.items()
              if eid not in landed and eid not in gone
              and left_ends[eid] is not None and left_ends[eid] >= now))

    # Patch-churn alarm. _PATCH_FIELDS is deliberately minimal because any field Google
    # normalizes on write will fail to compare equal on the next run and rewrite the entire
    # calendar, forever, at ~1 write per event per hour. Tests can only prove our body
    # round-trips against ITSELF; whether it round-trips against Google's normalization is
    # only observable in production, so watch for the signature here.
    settled = stats["patch"] + stats["unchanged"]
    if settled >= 50 and stats["patch"] > 0.2 * settled:
        print(f"::warning::{stats['patch']} of {settled} existing events needed a patch "
              f"({stats['patch'] / settled:.0%}). Above ~20% this usually means a field in "
              f"_PATCH_FIELDS does not round-trip from Google and every run is rewriting "
              f"the whole calendar. Diff one event's stored body against entry_body().")
    return stats


def write_failures(stats: dict) -> int:
    return stats["failed_insert"] + stats["failed_patch"] + stats["failed_delete"]


def writes_attempted(stats: dict) -> int:
    return (stats["insert"] + stats["revive"] + stats["patch"] + stats["delete"]
            + stats["migrated"] + write_failures(stats))


# ---------------------------------------------------------------- fetch (de)serialization
def result_to_json(result: FetchResult, lookahead_days: int | None = None) -> str:
    return json.dumps({
        # The window this capture covers. A replay must not be judged against a different
        # one, or events outside the captured window read as "no longer offered".
        "lookahead_days": lookahead_days,
        "ok": result.ok,
        "notices": result.notices,
        "lookups_ok": result.lookups_ok,
        "lookups_failed": result.lookups_failed,
        "errors": result.errors,
        "staff_ok": sorted(result.staff_ok) if result.staff_ok is not None else None,
        # Both decide what a replay may delete, so a replay that dropped them would judge
        # the capture by different rules than the run that made it: without staff_planned
        # every departed stylist reads as a failed lookup again, and without window_start
        # the window is re-anchored on the day of the REPLAY.
        "staff_planned": (sorted(result.staff_planned)
                          if result.staff_planned is not None else None),
        "window_start": result.window_start.isoformat() if result.window_start else None,
        "events": [event_to_dict(e) for e in result.events],
    }, indent=1)


def result_from_json(text: str) -> FetchResult:
    """Inverse of result_to_json. Captures written before a key existed load it as None —
    'unknown' — which sync() handles with its legacy rule for each, never as an empty set
    or today's date, which would each assert something the capture never recorded."""
    d = json.loads(text)
    window_start = None
    if d.get("window_start"):
        try:
            window_start = date.fromisoformat(d["window_start"])
        except (ValueError, TypeError):
            print(f"::warning::ignoring unreadable window_start {d['window_start']!r} "
                  f"in the capture")
    return FetchResult(
        slots=[], events=[event_from_dict(e) for e in d.get("events", [])],
        notices=d.get("notices"), ok=bool(d.get("ok")),
        lookups_ok=d.get("lookups_ok", 0), lookups_failed=d.get("lookups_failed", 0),
        errors=d.get("errors", []),
        staff_ok=set(d["staff_ok"]) if d.get("staff_ok") is not None else None,
        staff_planned=(set(d["staff_planned"])
                       if d.get("staff_planned") is not None else None),
        window_start=window_start)


def _github_output(key: str, value: str) -> None:
    """Expose a value to later workflow steps (no-op outside Actions)."""
    path = os.environ.get("GITHUB_OUTPUT")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(f"{key}={value}\n")
    except OSError:
        pass


def _summary_line(text: str) -> None:
    """Mirror a line into the Actions run summary, so the run list stops being 100
    indistinguishable rows."""
    path = os.environ.get("GITHUB_STEP_SUMMARY")
    if not path:
        return
    try:
        with open(path, "a", encoding="utf-8") as f:
            f.write(text + "\n")
    except OSError:
        pass


# ---------------------------------------------------------------- CLI
def main(argv=None):
    """Entry point. `argv` is a test seam: tests drive the decision points here — the
    untrusted-fetch write refusal, the exit codes, the .ics gating, the marker stamp, the
    skip gate — without patching sys.argv.

    Order matters and is deliberate: read the marker once, publish the health output, run
    the skip gate, resolve depth, stamp last_attempt (and last_deep_attempt on a deep
    run), and only then contact Timely. A skipped tick therefore costs a couple of
    calendar reads (preflight's calendars.get and the marker) and zero Timely requests,
    and a calendar we can read but not write stops at the stamp, before Timely is sent
    anything.
    """
    ap = argparse.ArgumentParser(description="Sync KIDA NYC open slots to Google Calendar")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the insert/patch/delete diff without writing")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--depth", choices=("auto", "near", "full"), default="auto",
                    help="auto (default) goes deep when the last deep sweep is stale, "
                         "unless the run before was a deep sweep that did not complete")
    ap.add_argument("--ics", default=None, help="also write an .ics file to this path")
    ap.add_argument("--fetch-json", default=None,
                    help="write the fetch result to this path (for artifacting/replay)")
    ap.add_argument("--from-json", default=None,
                    help="skip the Timely fetch and load a previous result from this path")
    ap.add_argument("--allow-mass-delete", action="store_true",
                    help="bypass the delete blast-radius guard (manual recovery only)")
    ap.add_argument("--skip-if-checked-within", type=int, default=0, metavar="MINUTES",
                    help="with --depth auto only: exit 0 without contacting Timely if the "
                         "last run STARTED less than this many minutes ago (0 = never skip)")
    args = ap.parse_args(argv)

    config = Config.load(args.config)
    tz = ZoneInfo(config.timezone)
    # The configured DEEP window, captured before resolve_depth overwrites lookahead_days
    # with this run's tier. Anything asking "was this really a full sweep?" must compare
    # against this, not against the already-resolved value.
    deep_window = config.lookahead_days
    print(f"config: {config.describe()}")

    service = None
    if (not args.dry_run or using_service_account()
            or TOKEN_PATH.exists() or os.environ.get(CLIENT_SECRET_ENV)):
        try:
            service = get_service()
        except Exception as e:
            if args.dry_run:
                print(f"(no calendar credentials: {e}; dry-run will report inserts only)")
            else:
                raise

    cal_id = None
    marker = None
    if service is not None:
        cal_id = ensure_calendar(service, config, dry_run=args.dry_run)
        preflight(service, cal_id)      # fail in 1 second, not after an 85-minute fetch
        # Read ONCE. The health output, the skip gate, the depth decision and the final
        # deep-sweep stamp all need it, and each must judge the same copy.
        marker = read_marker(service, cal_id)
    started = datetime.now(tz)

    # Published before anything slow or fallible, so the workflow's failure handling can
    # always tell a one-off blip from a calendar that has been stale for hours — even when
    # this run dies in the fetch. last_run is only ever written by a run that exits 0 (see
    # write_marker), so this is the age of the last SUCCESS, not of the last attempt. Empty
    # means unknown: no marker yet, a marker no run has succeeded on, or a last_run in the
    # future, which only a broken clock or a hand-edited marker can produce.
    since_ok = _elapsed(_marker_time(marker, "last_run"), started)
    _github_output("last_success_age_hours",
                   f"{since_ok.total_seconds() / 3600:.1f}"
                   if since_ok is not None and since_ok >= timedelta(0) else "")

    # The skip gate. Hourly ticks are not hourly: GitHub delays and bunches them, and a
    # manual dispatch lands wherever it lands, so two runs can start minutes apart and walk
    # the same Timely funnel twice. Measured START-TO-START off last_attempt, so a long
    # deep sweep does not make the next on-time tick look premature. Only `auto` is gated:
    # an explicit near/full is a person asking for a run now. A last_attempt in the future
    # is a broken clock, not a recent run, and must not silence the sync indefinitely.
    if args.depth == "auto" and args.skip_if_checked_within > 0:
        since = _elapsed(marker_last_attempt(marker), started)
        if (since is not None
                and timedelta(0) <= since < timedelta(minutes=args.skip_if_checked_within)):
            msg = (f"skip: the last run started {int(since.total_seconds() // 60)} min ago, "
                   f"within --skip-if-checked-within {args.skip_if_checked_within}; not "
                   f"contacting Timely this tick")
            print(msg)
            _github_output("skipped", "true")
            _summary_line(f"⏭ {msg}")
            return
    _github_output("skipped", "false")

    # Depth next: it decides how far the fetch reaches, and the stamp below records a deep
    # attempt so the next auto run can tell one that never completed (see resolve_depth).
    # Judged on the marker as read, before this run's own stamp lands on it.
    days, deep = resolve_depth(config, args.depth, marker, now=started)
    config.lookahead_days = days
    print(f"depth: {'FULL' if deep else 'near'} ({days} days)")
    last_deep_attempt = started if deep else marker_last_deep_attempt(marker)

    # Only an existing marker is stamped. Creating one here would publish a 'checked'
    # title for a run that has not checked anything yet; write_marker() at the end carries
    # `started` into the marker it creates instead. Every non-skipped run stamps, near and
    # full included: they are not gated, but they are attempts, and an auto tick landing
    # behind one must see it.
    if marker is not None and not args.dry_run:
        try:
            stamp_last_attempt(service, cal_id, marker, started, deep=deep)
        except WriteAborted as e:
            print(f"::error::{e}")
            _summary_line(f"❌ cannot write to the calendar: {e}")
            raise SystemExit(1)

    if args.from_json:
        payload = Path(args.from_json).read_text(encoding="utf-8")
        result = result_from_json(payload)
        # The window the JSON was CAPTURED at governs, not the depth resolved at replay
        # time. Replaying a 21-day capture under a 90-day depth would mark every event in
        # days 22-90 stale — a mass delete driven purely by the replay flag.
        captured = json.loads(payload).get("lookahead_days")
        if captured and captured != config.lookahead_days:
            print(f"::warning::{args.from_json} was captured over {captured} days but this "
                  f"run resolved {config.lookahead_days}; using the captured window so the "
                  f"replay cannot delete events it never looked for")
            config.lookahead_days = int(captured)
        # ...and it is only a DEEP sweep if the capture actually covered the deep window.
        # Otherwise a `--depth full --from-json <near capture>` replay — the natural thing
        # to run while debugging an incident — republishes a truncated .ics over the full
        # feed and stamps last_deep_sweep, suppressing the real sweep for hours.
        if captured:
            deep = int(captured) >= deep_window
            # The final marker follows suit, so a replay that turned out near does not
            # leave a deep attempt behind for the next auto run to read as a failed one.
            last_deep_attempt = started if deep else marker_last_deep_attempt(marker)
        # The capture's own window_start rides along in `result`, so sync() anchors the
        # window on the day the capture was FETCHED. Re-anchoring it on the replay day
        # would put every day after the captured window inside it, and delete it unseen.
        print(f"loaded {len(result.events)} events from {args.from_json} "
              f"({config.lookahead_days}-day window from "
              f"{result.window_start or 'an unrecorded start'})")
    else:
        result = fetch(config)

    print(f"fetched: ok={result.ok} lookups ok={result.lookups_ok} "
          f"failed={result.lookups_failed} → {len(result.events)} events "
          f"({result.requests_made} requests)")
    if result.errors:
        from collections import Counter
        # These were collected and then never printed on the production path, so every
        # Timely 429 was invisible.
        for kind, n in Counter(e.split(":")[0] for e in result.errors).most_common(10):
            print(f"  fetch error x{n}: {kind}")
        for e in result.errors[:20]:
            print(f"    {e}")

    entries = build_entries(result.events, config.event_style)
    _github_output("deep", "true" if deep else "false")

    if args.fetch_json:
        Path(args.fetch_json).write_text(
            result_to_json(result, config.lookahead_days), encoding="utf-8")
        print(f"wrote {args.fetch_json}")

    if args.ics:
        # Only a deep sweep may republish the feed. A near-term run only knows about the
        # next ~3 weeks, so writing the .ics from it would truncate the published feed's
        # horizon to 21 days — and since Google re-fetches an .ics only every 8-24h,
        # subscribers would almost always be served the short version. Skipping the write
        # leaves the previous, complete deployment live; the workflow's "is the .ics
        # non-empty" check turns that into a skipped publish rather than a failure.
        if not (deep and result.ok):
            print(f"skipping {args.ics}: "
                  f"{'near-term sweep' if not deep else 'fetch not ok'} — "
                  f"the published feed keeps its full-horizon copy")
        elif not entries:
            # A zero-entry feed is still a syntactically valid ~330-byte VCALENDAR, so
            # nothing downstream would notice it replacing a good one — and Google refetches
            # a subscribed .ics only every 8-24h, so subscribers would sit on the empty
            # version for most of a day.
            print(f"::warning::skipping {args.ics}: 0 entries. Refusing to replace the "
                  f"published feed with an empty one.")
        else:
            from . import ics_export
            ics_export.write(args.ics, entries, result.notices, datetime.now(tz))
            print(f"wrote {args.ics} ({len(entries)} entries)")

    # An untrusted fetch must write NOTHING. Previously this check ran after sync(), so a
    # suspicious run still inserted and patched, then reported failure.
    if not result.ok:
        print("::error::fetch not ok — refusing to write anything to the calendar")
        _summary_line(f"❌ fetch not ok ({result.lookups_ok} ok / {result.lookups_failed} failed) "
                      f"— nothing written")
        raise SystemExit(2)

    try:
        stats = sync(config, result, service=service, dry_run=args.dry_run,
                     allow_mass_delete=args.allow_mass_delete)
    except WriteAborted as e:
        print(f"::error::sync aborted: {e}")
        _summary_line(f"❌ sync aborted: {e}")
        raise SystemExit(1)

    line = ("[dry-run] " if args.dry_run else "") + "sync: " + " ".join(
        f"{k}={v}" for k, v in stats.items() if v)
    print(line)
    _summary_line(f"### {'FULL' if deep else 'near'} sweep · {days}d\n"
                  f"- {len(result.events)} openings fetched → {len(entries)} entries; "
                  f"{stats['live_openings']} openings live on the calendar\n"
                  f"- `{line}`\n"
                  f"- {result.requests_made} Timely requests "
                  f"({result.requests_saved} saved by dedup), "
                  f"{result.lookups_ok}/{result.lookups_expected} lookups ok")

    # The exit decision comes BEFORE the marker, because the marker records it: last_run is
    # the last run that exits 0. Deciding afterwards stamped last_run on runs about to exit
    # 1, so a persistent failure published last_success_age_hours of ~1 on every run — the
    # workflow's 12-hour issue threshold was never reached, and the skipped ticks behind
    # each failure looked fresh enough to ping the heartbeat.
    failed = write_failures(stats)
    errors = []
    if failed > max(FAILURE_EXIT_FLOOR, FAILURE_EXIT_SHARE * writes_attempted(stats)):
        errors.append(f"::error::{failed} write failures out of {writes_attempted(stats)} "
                      f"attempted")
    # A refused delete pass already said why, as an ::error:: from sync().
    failing = bool(errors) or bool(stats["blocked_delete"])
    # A run that refused a large share of its deletes did not do its job, even though every
    # call it made succeeded. Reporting green there is how a partial outage stays invisible.
    if stats["skipped_delete"] > SKIPPED_DELETE_EXIT_FLOOR:
        failing = True
        errors.append(f"::error::{stats['skipped_delete']} deletes were withheld this run "
                      f"(stylists we could not see completely, or replacements that did not "
                      f"land). The calendar is stale for those people.")

    if service is not None and not args.dry_run:
        now = datetime.now(tz)
        # Only a deep sweep that actually completed cleanly may claim one happened. A run
        # that lost writes or had its delete pass refused did NOT refresh the far window,
        # and stamping it would suppress the corrective sweep for deep_sweep_every_hours.
        clean = write_failures(stats) == 0 and not stats["blocked_delete"]
        last_deep = now if (deep and clean) else marker_last_deep_sweep(marker)
        if deep and not clean:
            print("::warning::deep sweep did not complete cleanly; not recording it as one, "
                  "so the next run will sweep deep again")
        write_marker(service, cal_id, checked_at=now,
                     # Openings, not entries: under `blocks` an entry count would understate
                     # availability ~3.3x. And everything of ours left live, not just this
                     # run's window: a near run sees 21 of 90 days. See sync().
                     entry_count=stats["live_openings"],
                     last_deep=last_deep, last_attempt=started,
                     last_deep_attempt=last_deep_attempt,
                     succeeded=not failing,
                     previous_success=_marker_time(marker, "last_run"))

    for msg in errors:
        print(msg)
    if failing:
        raise SystemExit(1)


if __name__ == "__main__":
    main()
