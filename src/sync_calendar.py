"""
Sync open slots into a dedicated Google Calendar (the live path).

Idempotent: each calendar entry has a deterministic id derived from stylist + start, so an
opening maps to the same calendar event across runs. Each sync computes a diff against the
calendar and applies the minimum changes, in this order:

  on calendar, not in feed    -> delete   (it got booked)
  in feed and on calendar     -> patch if content changed
  in feed, not on calendar    -> insert
  wrong id scheme for the configured event_style -> delete (one-time changeover)

Deletes run FIRST and deliberately so. They are the correctness-critical half: an event we
fail to delete is a publicly advertised appointment that is already booked. Inserts merely
delay good news. `stale` and `desired` are disjoint by construction, so the ordering is safe.

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
* Even on a clean fetch, a delete pass that would remove more than half of the in-window
  events is refused unless --allow-mass-delete is passed. The denominator is IN-WINDOW, not
  whole-calendar: a 21-day run sees only ~350 of ~3300 events, so a whole-calendar
  denominator would wave through a near-total wipe.
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
from datetime import datetime, timedelta
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


def _parse_start(item: dict, tz) -> datetime | None:
    """Start instant of an existing calendar event, or None if unparseable.

    Guards TypeError as well as ValueError: comparing a naive datetime against an aware
    horizon raises TypeError, which would otherwise kill the whole delete phase.
    """
    s = item.get("start") or {}
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
    from googleapiclient.errors import HttpError
    try:
        return _execute(service.events().get(calendarId=cal_id, eventId=MARKER_ID))
    except HttpError as e:
        if _status(e) in (404, 410):
            return None
        raise


def marker_last_deep_sweep(marker: dict | None) -> datetime | None:
    if not marker:
        return None
    raw = ((marker.get("extendedProperties") or {}).get("private") or {}).get("last_deep_sweep")
    if not raw:
        return None
    try:
        return datetime.fromisoformat(raw)
    except (ValueError, TypeError):
        return None


def write_marker(service, cal_id: str, *, checked_at: datetime, entry_count: int,
                 last_deep: datetime | None, dry_run: bool = False) -> None:
    """Publish an all-day 'last checked' event.

    Without this the calendar looks equally healthy whether it was synced 5 minutes or 5 days
    ago — which is exactly how a 3-day outage went unnoticed. One patch per run; never stamp
    a timestamp into per-event descriptions, which would rewrite the whole calendar hourly.
    """
    from googleapiclient.errors import HttpError

    day = checked_at.date()
    age_note = ""
    if last_deep:
        hours = (checked_at - last_deep).total_seconds() / 3600
        age_note = f"Last full 90-day sweep: {hours:.0f}h ago.\n"
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
        # "go deep". last_run separately records that the run happened at all.
        "extendedProperties": {"private": dict(
            {"last_run": checked_at.isoformat()},
            **({"last_deep_sweep": last_deep.isoformat()} if last_deep else {}))},
    }
    if dry_run:
        return
    try:
        _execute(service.events().insert(calendarId=cal_id, body=body))
    except HttpError as e:
        if _status(e) != 409:
            raise
        _execute(service.events().update(calendarId=cal_id, eventId=MARKER_ID, body=body))


def resolve_depth(service, cal_id: str, config: Config, requested: str) -> tuple[int, bool]:
    """Return (lookahead_days, is_deep_sweep).

    'auto' decides from *elapsed time since the last deep sweep*, read off the marker event,
    rather than from the wall clock. The old `date -u +%H % 6` scheme assumed the cron fires:
    over one measured 180-hour window GitHub delivered 56% of hourly ticks, 00 UTC never fired
    once, and deep sweeps landed 14 times against 30 intended. An elapsed-time check is immune
    to dropped ticks, cron drift, and a long run pushing the next one past its hour.
    """
    if requested == "near":
        return config.near_lookahead_days, False
    if requested == "full":
        return config.lookahead_days, True

    last_deep = marker_last_deep_sweep(read_marker(service, cal_id)) if service else None
    if last_deep is None:
        return config.lookahead_days, True
    now = datetime.now(last_deep.tzinfo or ZoneInfo(config.timezone))
    if now - last_deep >= timedelta(hours=config.deep_sweep_every_hours):
        return config.lookahead_days, True
    return config.near_lookahead_days, False


# ---------------------------------------------------------------- diff + apply
def sync(config: Config, result: FetchResult, service=None, dry_run=False,
         allow_mass_delete=False) -> dict:
    from googleapiclient.errors import HttpError

    tz = ZoneInfo(config.timezone)
    entries = build_entries(result.events, config.event_style)

    # A failed notices scrape returns None (distinct from "" for a genuinely empty banner).
    # The banner text is an input to every description, so treating a 20-second kidanyc.com
    # blip as "banner cleared" would rewrite all ~3300 events, then rewrite them back next run.
    # Descriptions are only rewritten when we saw EVERY service. A failed service lookup
    # means we do not know whether it is bookable — not that it isn't — and dropping it
    # from the description asserts the stronger claim. Timely persistently 429s two
    # /Booking/Service POSTs, so without this ~16 entries flip-flop between two description
    # states every hour: the service vanishes on a failed run and returns on the next.
    complete = result.lookups_failed == 0
    compare_description = result.notices is not None and complete
    if not complete:
        print(f"::warning::{result.lookups_failed} service lookup(s) failed; leaving "
              f"existing descriptions alone rather than asserting those services are gone")
    desired = {e.google_event_id(): entry_body(e, config, result.notices) for e in entries}
    # Side tables keyed the same way as `desired`, so the write passes can reason about an
    # entry's stylist and day without re-deriving them from the body.
    _entry_stylist = {e.google_event_id(): str(e.stylist_id) for e in entries}
    _entry_day = {e.google_event_id(): (str(e.stylist_id), e.start.date()) for e in entries}
    _desired_days = set(_entry_day.values())

    stats = {"insert": 0, "revive": 0, "patch": 0, "delete": 0, "migrated": 0,
             "unchanged": 0, "skipped_delete": 0, "blocked_delete": 0,
             "failed_insert": 0, "failed_patch": 0, "failed_delete": 0}

    if dry_run and service is None:
        # No API access: just report what we'd publish.
        stats["insert"] = len(desired)
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

    # The horizon is the END of the last day the fetch covered. fetch() filters by DATE
    # (day_date > horizon), so a datetime horizon would leave later-in-the-day slots on the
    # final day permanently undeletable: absent from `desired`, but past the cutoff.
    last_day = (datetime.now(tz) + timedelta(days=config.lookahead_days)).date()
    horizon = datetime.combine(last_day + timedelta(days=1), dtime.min, tzinfo=tz)
    now = datetime.now(tz)

    starts = {eid: _parse_start(item, tz) for eid, item in existing.items()}
    obs_starts = {eid: _parse_start(item, tz) for eid, item in obsolete.items()}

    # Deletes are window-scoped so a short near-term run can't wipe the deep run's far-out
    # events. That applies to the obsolete bucket too: a near-tier run that meets a style
    # changeover must not purge 90 days of entries it can only replace 21 days of.
    in_window = [eid for eid, d in starts.items() if d is not None and d <= horizon]
    obsolete_in_window = [eid for eid, d in obs_starts.items()
                          if d is not None and d <= horizon]

    # Events that have already started can never be in `desired` (fetch drops past slots),
    # so they are unconditionally stale for reasons that say nothing about fetch health.
    # Keeping them out of the ratio stops routine pruning from tripping the guard.
    # `not in desired` applies to the past bucket too. A slot that merely STARTED while the
    # fetch was running is still in `desired` (the fetch saw it minutes ago), and deleting
    # it would remove an entry the same run is about to write — churn on every run, and
    # under `blocks` it can retire a block whose later openings are still bookable.
    past = [eid for eid in in_window if starts[eid] < now and eid not in desired]
    future_stale = [eid for eid in in_window
                    if eid not in desired and starts[eid] >= now]
    future_in_window = [eid for eid in in_window if starts[eid] >= now]

    # Delete authority is per stylist where we can establish it. An event we wrote carries
    # its stylist id, so if that person's lookups failed this run we simply leave their
    # entries alone rather than deleting on information we know is incomplete. Without this
    # a single flaky stylist (the busiest owns 9 of 60 lookups = 15%, inside the 20% slack
    # that still reports ok=True) would be wiped off the public calendar.
    staff_covered = result.staff_ok

    def _may_delete(item: dict) -> bool:
        """True unless the item belongs to a stylist we could not see completely.

        An unstamped item (written by an older version) reports None, which means UNKNOWN
        provenance, not "not covered" — it must stay deletable or nothing written before
        the stamp existed could ever be pruned.
        """
        if staff_covered is None:
            return True
        who = _stylist_of(item)
        return who is None or who in staff_covered

    if staff_covered is not None:
        held = {eid for eid in future_stale if not _may_delete(existing[eid])}
        if held:
            print(f"::warning::withholding {len(held)} delete(s) for stylists whose "
                  f"lookups failed this run; they keep their entries until we can see them")
            stats["skipped_delete"] += len(held)
            future_stale = [e for e in future_stale if e not in held]
        # Withholding must be SYMMETRIC. Suppressing a stylist's deletes while still
        # inserting their new entries publishes two overlapping blocks for the same
        # stylist-day, the older one still advertising an opening we know is booked.
        # If we cannot see someone completely, we leave their whole day alone.
        blind = {who for who in
                 {_stylist_of(existing[eid]) for eid in held} if who is not None}
        if blind:
            dropped = [eid for eid, b in desired.items()
                       if str(_entry_stylist[eid]) in blind]
            if dropped:
                print(f"::warning::also withholding {len(dropped)} write(s) for those "
                      f"stylists, so their day is not double-published")
                for eid in dropped:
                    desired.pop(eid, None)
        # The changeover purge needs the SAME authority. It is the one delete path that
        # runs when `existing` is empty by construction, so the check above cannot fire
        # for it — and a changeover is exactly when a half-seen stylist would otherwise be
        # erased with no replacement written.
        held_obs = [eid for eid in obsolete_in_window if not _may_delete(obsolete[eid])]
        if held_obs:
            print(f"::warning::withholding {len(held_obs)} changeover retirement(s) for "
                  f"stylists whose lookups failed this run")
            stats["skipped_delete"] += len(held_obs)
            hold = set(held_obs)
            obsolete_in_window = [e for e in obsolete_in_window if e not in hold]

    stale = past + future_stale

    # ---- guards on the delete pass ----
    blocked = False
    if not result.ok:
        stats["skipped_delete"] += len(stale) + len(obsolete_in_window)
        print(f"WARNING: fetch not ok (ok={result.lookups_ok} failed={result.lookups_failed}); "
              f"skipping {len(stale) + len(obsolete_in_window)} deletes to avoid wiping "
              f"the calendar")
        blocked = True
    # Size the floor in OPENINGS, not entries. Under `blocks` the 21-day window is ~97
    # entries but ~303 openings, so an entry floor of 50 sits only 1.9x away — and
    # weekends_only, a min_slot_hour filter, or two stylists on leave would drop it under
    # the floor, silently disabling the guard entirely on exactly the calendars where a
    # wipe is least recoverable.
    elif (not allow_mass_delete
            and sum(_openings_of(existing[e]) for e in future_in_window) >= DELETE_BLAST_MIN
            and len(future_stale) > DELETE_BLAST_RADIUS * len(future_in_window)):
        stats["blocked_delete"] = len(future_stale)
        print(f"::error::refusing to delete {len(future_stale)} of {len(future_in_window)} "
              f"upcoming in-window events (> {DELETE_BLAST_RADIUS:.0%}). Fetch reported ok "
              f"but looks partial. Re-run with --allow-mass-delete if this really is correct.")
        blocked = True

    failures: list[str] = []
    failed_ids: set[str] = set()      # entries whose write did NOT land this run
    consecutive = 0

    def guard(exc, kind: str, eid: str) -> None:
        """Record a per-item write failure, or abort if it is not survivable."""
        nonlocal consecutive
        status = _status(exc)
        if status == 403 and _reasons(exc) & _QUOTA_REASONS:
            raise WriteAborted(
                f"{kind} {eid}: Google Calendar quota exhausted ({sorted(_reasons(exc))}). "
                f"Aborting; the quota resets on Google's schedule and the next run retries.")
        if status in (401, 403) and not _is_rate_limited(exc):
            raise WriteAborted(
                f"{kind} {eid}: {status} {sorted(_reasons(exc)) or exc}. Aborting rather than "
                f"repeating this for every remaining event — check calendar sharing/scopes.")
        stats[f"failed_{kind}"] += 1
        failures.append(f"{kind} {eid}: {status} {sorted(_reasons(exc)) or ''}".strip())
        if kind in ("insert", "patch"):
            # This entry is not live, so it cannot justify retiring anything it replaces.
            failed_ids.add(eid)
        consecutive += 1
        if consecutive >= MAX_CONSECUTIVE_FAILURES:
            raise WriteAborted(
                f"{consecutive} consecutive write failures; aborting. Last: {failures[-1]}")

    def do_delete(eid: str, stat_key: str) -> None:
        nonlocal consecutive
        if dry_run:
            stats[stat_key] += 1
            return
        try:
            _execute(service.events().delete(calendarId=cal_id, eventId=eid))
        except HttpError as e:
            # Already gone is the state we wanted. googleapiclient's own retry can turn a
            # successful delete into a 404/410 on the retried call, so this is routine.
            if _status(e) in (404, 410):
                stats[stat_key] += 1
                consecutive = 0
                return
            guard(e, "delete", eid)
            return
        stats[stat_key] += 1
        consecutive = 0

    # A stale id falls into one of two very different cases, and they must not share an
    # ordering:
    #
    #   BOOKED  — nothing in the feed covers that stylist-day any more. The entry is a
    #             publicly advertised appointment that is already taken. Delete it FIRST;
    #             that urgency is the whole reason deletes precede inserts.
    #   RE-KEY  — the stylist-day IS still in the feed, under a different id. Under
    #             `blocks` this happens constantly: the id is keyed on the block's first
    #             opening, so booking (or merely passing) that opening re-keys the block.
    #             Deleting first here removes still-bookable availability, and if the
    #             replacement insert then fails the whole day is advertised nowhere while
    #             the run still exits 0.
    #
    # So re-keys are deleted LAST, after their replacement is live — the same discipline
    # the changeover purge uses.
    def _is_rekey(eid: str) -> bool:
        who = _stylist_of(existing[eid])
        d = starts.get(eid)
        return (who is not None and d is not None
                and (who, d.date()) in _desired_days)

    booked = [eid for eid in stale if not _is_rekey(eid)]
    rekeyed = [eid for eid in stale if _is_rekey(eid)]

    # ---- 1. deletes, first: a stale event is a publicly advertised booked appointment ----
    if not blocked:
        for eid in booked:
            do_delete(eid, "delete")

    # ---- 2. patches ----
    for eid, body in desired.items():
        item = existing.get(eid)
        if item is None:
            continue
        if not _needs_patch(item, body, compare_description):
            stats["unchanged"] += 1
            continue
        if dry_run:
            stats["patch"] += 1
            continue
        try:
            _execute(service.events().patch(calendarId=cal_id, eventId=eid, body=body))
        except HttpError as e:
            guard(e, "patch", eid)
            continue
        stats["patch"] += 1
        consecutive = 0

    # ---- 3. inserts, with the 409 tombstone fallback ----
    for eid, body in desired.items():
        if eid in existing:
            continue
        if dry_run:
            stats["insert"] += 1
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
            consecutive = 0
            continue
        stats["insert"] += 1
        consecutive = 0

    # ---- 3b. re-keyed entries, now that their replacement is live ----
    # Each of these is an entry whose stylist-day is still in the feed under a new id. The
    # replacement insert has already run, so deleting now can never leave the day
    # unrepresented — and if that insert failed, we keep the old entry instead of the day
    # going dark.
    if not blocked and rekeyed:
        kept = 0
        for eid in rekeyed:
            replacement = next((k for k, day in _entry_day.items()
                                if day == (_stylist_of(existing[eid]), starts[eid].date())),
                               None)
            if replacement is not None and replacement in failed_ids:
                kept += 1
                stats["skipped_delete"] += 1
                continue
            do_delete(eid, "delete")
        if kept:
            print(f"::warning::kept {kept} superseded entr(ies) whose replacement failed to "
                  f"write — better a stale entry than an unrepresented day")

    # ---- 4. the event_style changeover purge, LAST and separately guarded ----
    # These are entries written under the other style. They run after the inserts, not with
    # the deletes, so a changeover can only ever remove an entry once its replacement is
    # already live: if the write pass aborts partway (quota, auth, a failure streak) the old
    # entries are still standing. Doing this first — as the delete pass — meant an abort left
    # the public calendar emptied with nothing written back.
    if obsolete_in_window and not blocked:
        # Size BOTH sides in openings, and count only the entries that actually LANDED.
        #
        # Entries are not comparable across styles (blocks are ~3.3x fewer for the same
        # availability), so an entry-count comparison would refuse a healthy slots->blocks
        # changeover and wave through a blocks->slots one that had lost most of its data.
        # And counting entries we merely intended to write would let a run that failed 200
        # of its inserts still retire everything those inserts were meant to replace.
        live_now = sum(e.opening_count for e in entries
                       if e.google_event_id() not in failed_ids)
        retiring = sum(_openings_of(obsolete[eid]) for eid in obsolete_in_window)
        landed_entries = stats["insert"] + stats["revive"] + stats["patch"] + stats["unchanged"]
        if not allow_mass_delete and live_now < DELETE_BLAST_RADIUS * retiring:
            # The replacement set is implausibly small next to what we are about to remove.
            # This is the shape of a partial fetch on a changeover run, which would
            # otherwise wipe the calendar and still exit 0.
            stats["blocked_delete"] += len(obsolete_in_window)
            print(f"::error::refusing to retire {len(obsolete_in_window)} entries "
                  f"({retiring} openings) from the previous event_style: the "
                  f"{landed_entries} replacement entries that landed cover only "
                  f"{live_now} openings. Fix the fetch first, or re-run with "
                  f"--allow-mass-delete.")
        else:
            print(f"retiring {len(obsolete_in_window)} entries ({retiring} openings) from "
                  f"the previous event_style ({landed_entries} entries covering "
                  f"{live_now} openings are live)")
            for eid in obsolete_in_window:
                do_delete(eid, "migrated")

    if failures:
        print(f"::warning::{len(failures)} write(s) failed; first 20:")
        for f in failures[:20]:
            print(f"  {f}")

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
        "events": [event_to_dict(e) for e in result.events],
    }, indent=1)


def result_from_json(text: str) -> FetchResult:
    d = json.loads(text)
    return FetchResult(
        slots=[], events=[event_from_dict(e) for e in d.get("events", [])],
        notices=d.get("notices"), ok=bool(d.get("ok")),
        lookups_ok=d.get("lookups_ok", 0), lookups_failed=d.get("lookups_failed", 0),
        errors=d.get("errors", []),
        staff_ok=set(d["staff_ok"]) if d.get("staff_ok") is not None else None)


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
    untrusted-fetch write refusal, the exit codes, the .ics gating, the marker stamp —
    without patching sys.argv."""
    ap = argparse.ArgumentParser(description="Sync KIDA NYC open slots to Google Calendar")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the insert/patch/delete diff without writing")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--depth", choices=("auto", "near", "full"), default="auto",
                    help="auto (default) goes deep only when the last deep sweep is stale")
    ap.add_argument("--ics", default=None, help="also write an .ics file to this path")
    ap.add_argument("--fetch-json", default=None,
                    help="write the fetch result to this path (for artifacting/replay)")
    ap.add_argument("--from-json", default=None,
                    help="skip the Timely fetch and load a previous result from this path")
    ap.add_argument("--allow-mass-delete", action="store_true",
                    help="bypass the delete blast-radius guard (manual recovery only)")
    args = ap.parse_args(argv)

    config = Config.load(args.config)
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
    if service is not None:
        cal_id = ensure_calendar(service, config, dry_run=args.dry_run)
        preflight(service, cal_id)      # fail in 1 second, not after an 85-minute fetch

    # Depth first: it decides how far the fetch reaches.
    days, deep = resolve_depth(service, cal_id, config, args.depth)
    config.lookahead_days = days
    print(f"depth: {'FULL' if deep else 'near'} ({days} days)")

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
        print(f"loaded {len(result.events)} events from {args.from_json} "
              f"({config.lookahead_days}-day window)")
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
            ics_export.write(args.ics, entries, result.notices,
                             datetime.now(ZoneInfo(config.timezone)))
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
                  f"- {len(result.events)} openings → {len(entries)} entries\n"
                  f"- `{line}`\n"
                  f"- {result.requests_made} Timely requests "
                  f"({result.requests_saved} saved by dedup), "
                  f"{result.lookups_ok}/{result.lookups_expected} lookups ok")

    if service is not None and not args.dry_run:
        now = datetime.now(ZoneInfo(config.timezone))
        # Only a deep sweep that actually completed cleanly may claim one happened. A run
        # that lost writes or had its delete pass refused did NOT refresh the far window,
        # and stamping it would suppress the corrective sweep for deep_sweep_every_hours.
        clean = write_failures(stats) == 0 and not stats["blocked_delete"]
        last_deep = (now if (deep and clean)
                     else marker_last_deep_sweep(read_marker(service, cal_id)))
        if deep and not clean:
            print("::warning::deep sweep did not complete cleanly; not recording it as one, "
                  "so the next run will sweep deep again")
        write_marker(service, cal_id, checked_at=now,
                     # Report openings, not entries: under `blocks` an entry count would
                     # understate availability ~3.3x to anyone reading the calendar.
                     entry_count=sum(e.opening_count for e in entries),
                     last_deep=last_deep)

    failed = write_failures(stats)
    if failed > max(FAILURE_EXIT_FLOOR, FAILURE_EXIT_SHARE * writes_attempted(stats)):
        print(f"::error::{failed} write failures out of {writes_attempted(stats)} attempted")
        raise SystemExit(1)
    if stats["blocked_delete"]:
        raise SystemExit(1)
    # A run that refused a large share of its deletes did not do its job, even though every
    # call it made succeeded. Reporting green there is how a partial outage stays invisible.
    if stats["skipped_delete"] > SKIPPED_DELETE_EXIT_FLOOR:
        print(f"::error::{stats['skipped_delete']} deletes were withheld this run "
              f"(stylists we could not see completely, or replacements that failed to "
              f"write). The calendar is stale for those people.")
        raise SystemExit(1)


if __name__ == "__main__":
    main()
