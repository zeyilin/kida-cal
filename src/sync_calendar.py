"""
Sync open slots into a dedicated Google Calendar (the live path).

Idempotent: each Event has a deterministic id (sha1 of stylist|start), so a slot maps
to the same calendar event across runs. Each sync computes an insert/patch/delete diff
against the calendar and applies the minimum changes:

  in feed, not on calendar   -> insert
  in feed and on calendar    -> patch if content changed
  on calendar, not in feed   -> delete (it got booked)

Safety: we only ever touch our own secondary calendar, never the primary. If the fetch
did not clearly succeed (FetchResult.ok is False, e.g. every lookup 429'd), we SKIP all
deletes so a transient outage can't wipe the calendar.

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
import os
import time
from datetime import datetime
from pathlib import Path
from zoneinfo import ZoneInfo

from .fetch_availability import Config, FetchResult, fetch
from .ics_export import event_description
from .models import Event

SCOPES = ["https://www.googleapis.com/auth/calendar"]
LOCATION = "KIDA NYC, 369 Broome Street, New York, NY 10013"
TOKEN_PATH = Path(os.path.expanduser("~/.config/kida-cal/token.json"))
CLIENT_SECRET_ENV = "KIDA_GOOGLE_CLIENT_SECRET"       # path to client_secret.json (OAuth)
SERVICE_ACCOUNT_ENV = "KIDA_SERVICE_ACCOUNT_JSON"     # path to service-account key (preferred)


def using_service_account() -> bool:
    return bool(os.environ.get(SERVICE_ACCOUNT_ENV))


def _execute(request, *, tries=6):
    """Run a Google API request, backing off on rate-limit / transient errors.

    A large first sync (rewriting all events + inserting the 90-day backlog) can burst
    past Google's per-100-seconds write limit; this retries instead of failing the run.
    """
    from googleapiclient.errors import HttpError
    backoff = 1.5
    for attempt in range(tries):
        try:
            return request.execute()
        except HttpError as e:
            status = getattr(getattr(e, "resp", None), "status", None)
            reason = str(e).lower()
            transient = status in (429, 500, 502, 503) or (
                status == 403 and ("rate" in reason or "quota" in reason))
            if transient and attempt < tries - 1:
                time.sleep(backoff)
                backoff *= 2
                continue
            raise


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


def ensure_calendar(service, config: Config) -> str:
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


# ---------------------------------------------------------------- event bodies
def event_body(ev: Event, config: Config, notices: str) -> dict:
    return {
        "id": ev.google_event_id(),
        "summary": ev.summary(),
        "location": LOCATION,
        "description": event_description(ev, notices),
        "start": {"dateTime": ev.start.isoformat(), "timeZone": config.timezone},
        "end": {"dateTime": ev.end.isoformat(), "timeZone": config.timezone},
        "transparency": "transparent",          # shows as Free
        "reminders": {"useDefault": False, "overrides": []},  # no notifications
        "colorId": str(config.calendar_color_id),
        "source": {"title": "Book on KIDA NYC", "url": ev.book_url},
    }


def _needs_patch(existing: dict, desired: dict) -> bool:
    for k in ("summary", "description", "location", "transparency", "colorId"):
        if existing.get(k) != desired.get(k):
            return True
    for k in ("start", "end"):
        if (existing.get(k, {}).get("dateTime") != desired[k]["dateTime"]):
            return True
    return False


# ---------------------------------------------------------------- diff + apply
def sync(config: Config, result: FetchResult, service=None, dry_run=False) -> dict:
    desired = {ev.google_event_id(): event_body(ev, config, result.notices)
               for ev in result.events}

    stats = {"insert": 0, "patch": 0, "delete": 0, "unchanged": 0, "skipped_delete": 0}

    if dry_run and service is None:
        # No API access: just report what we'd publish.
        stats["insert"] = len(desired)
        print(f"[dry-run] would publish {len(desired)} events "
              f"(no calendar access to diff against)")
        for ev in list(result.events)[:10]:
            print(f"  INSERT {ev.start:%a %m-%d %H:%M} {ev.summary()}")
        if len(result.events) > 10:
            print(f"  ... and {len(result.events) - 10} more")
        return stats

    cal_id = ensure_calendar(service, config)

    # Load our existing events (only ones we created carry the 'kida' id prefix).
    existing: dict[str, dict] = {}
    page_token = None
    while True:
        resp = _execute(service.events().list(
            calendarId=cal_id, showDeleted=False, singleEvents=True,
            maxResults=2500, pageToken=page_token))
        for item in resp.get("items", []):
            if item["id"].startswith("kida"):
                existing[item["id"]] = item
        page_token = resp.get("nextPageToken")
        if not page_token:
            break

    # inserts / patches
    for eid, body in desired.items():
        if eid not in existing:
            stats["insert"] += 1
            if not dry_run:
                _execute(service.events().insert(calendarId=cal_id, body=body))
        elif _needs_patch(existing[eid], body):
            stats["patch"] += 1
            if not dry_run:
                _execute(service.events().patch(calendarId=cal_id, eventId=eid, body=body))
        else:
            stats["unchanged"] += 1

    # deletes — but NEVER when the fetch was suspicious (guard against mass-wipe)
    stale = [eid for eid in existing if eid not in desired]
    if not result.ok:
        stats["skipped_delete"] = len(stale)
        print(f"WARNING: fetch not ok (ok={result.lookups_ok} failed={result.lookups_failed}); "
              f"skipping {len(stale)} deletes to avoid wiping the calendar")
    else:
        for eid in stale:
            stats["delete"] += 1
            if not dry_run:
                _execute(service.events().delete(calendarId=cal_id, eventId=eid))

    return stats


# ---------------------------------------------------------------- CLI
def main():
    ap = argparse.ArgumentParser(description="Sync KIDA NYC open slots to Google Calendar")
    ap.add_argument("--dry-run", action="store_true",
                    help="print the insert/patch/delete diff without writing")
    ap.add_argument("--config", default="config.yaml")
    ap.add_argument("--ics", default=None, help="also write an .ics file to this path")
    args = ap.parse_args()

    config = Config.load(args.config)
    result = fetch(config)
    print(f"fetched: ok={result.ok} lookups ok={result.lookups_ok} "
          f"failed={result.lookups_failed} → {len(result.events)} events")

    if args.ics:
        from . import ics_export
        ics_export.write(args.ics, result.events, result.notices,
                         datetime.now(ZoneInfo(config.timezone)))
        print(f"wrote {args.ics}")

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

    stats = sync(config, result, service=service, dry_run=args.dry_run)
    print(("[dry-run] " if args.dry_run else "") + "sync: " +
          " ".join(f"{k}={v}" for k, v in stats.items()))

    # Fail loudly for CI if auth/shape broke and we got nothing while not in dry-run.
    if not args.dry_run and not result.ok:
        raise SystemExit(2)


if __name__ == "__main__":
    main()
