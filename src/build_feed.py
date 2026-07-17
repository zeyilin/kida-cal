"""
Build the public .ics feed (URL-subscribe path — no Google credentials needed).

Fetches current availability and writes an iCalendar file that gets published to a
public URL (GitHub Pages). Subscribe to that URL in Google Calendar via
"Other calendars → From URL".

Note on freshness: we regenerate hourly, but Google re-fetches external ICS URLs on
its own slow schedule (commonly 8–24h). For near-real-time updates use the Google
Calendar API path instead (src/sync_calendar.py).

Safety: if the fetch does not clearly succeed, we exit non-zero WITHOUT writing, so a
transient outage can't replace a good feed with an empty one (the previous published
feed stays live because the deploy step won't run).
"""
from __future__ import annotations

import argparse
import os
import sys
from datetime import datetime
from zoneinfo import ZoneInfo

from . import ics_export
from .fetch_availability import Config, fetch


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--out", default="public/open_slots.ics", help="output .ics path")
    ap.add_argument("--config", default="config.yaml")
    args = ap.parse_args()

    cfg = Config.load(args.config)
    result = fetch(cfg)
    print(f"fetched: ok={result.ok} lookups ok={result.lookups_ok} "
          f"failed={result.lookups_failed} → {len(result.events)} events")
    if result.errors:
        for e in result.errors[:5]:
            print("  err:", e, file=sys.stderr)

    if not result.ok:
        # Do NOT overwrite a good feed with an empty/broken one.
        print("fetch not ok — leaving existing feed untouched (exit 2)", file=sys.stderr)
        raise SystemExit(2)

    out_dir = os.path.dirname(args.out)
    if out_dir:
        os.makedirs(out_dir, exist_ok=True)
    checked_at = datetime.now(ZoneInfo(cfg.timezone))
    ics_export.write(args.out, result.events, result.notices, checked_at)
    print(f"wrote {args.out} with {len(result.events)} events (checked {checked_at:%Y-%m-%d %H:%M %Z})")


if __name__ == "__main__":
    main()
