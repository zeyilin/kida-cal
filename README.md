# KIDA NYC — Open Slots calendar

A read-only calendar that mirrors **available** hair/barber appointment slots at
[KIDA NYC](https://kidanyc.com) for the next N days as a **live Google Calendar** you can
add (and share via a public link). Each slot is a **Free**, no-notification event prefixed
`OPEN ·`, with a booking link in the description. It **never books, holds, or cancels
anything** — it only reads public availability.

> Availability is a snapshot; every event says so and links to KIDA's booking page to confirm.

## Two ways to consume it

| | **A. Live Google Calendar (default)** | **B. Subscribe by `.ics` URL (fallback)** |
|---|---|---|
| How | Writes events into a Google Calendar via the API; shareable via a public Google link | Publishes a public `.ics` at a GitHub Pages URL; add via "From URL" |
| Setup | Google service account + share a calendar with it (one-time) | Just make the repo public + enable Pages. **No Google login.** |
| Freshness | **Within the hour** (native Google sync) | **Google re-fetches on its own slow schedule (~8–24h)** |
| Cost | $0/mo | $0/mo |

**A is the live path and the default** (`.github/workflows/sync.yml`, hourly). **B**
stays available as a credential-free fallback (`.github/workflows/feed.yml`, manual) — same
data, just slower to update.

## How it works

```
config.yaml ─▶ src/fetch_availability.py ─▶ [Slot] ─▶ group ─▶ [Event]
                     │  (src/timely.py funnel)                    │
                     │                                            ├─(A)▶ src/sync_calendar.py ─▶ Google Calendar API (live)
                     └▶ kidanyc.com notices                       └─(B)▶ src/build_feed.py ─▶ public/open_slots.ics ─▶ GitHub Pages URL
```

Hourly, GitHub Actions reads KIDA's live availability by walking Timely's booking funnel
from a plain HTTP client (no browser — see `docs/timely-api.md`), normalizes each opening
into a timezone-aware slot, de-dupes overlapping services, then (A) reconciles the events
into a Google Calendar (insert/patch/delete) or (B) regenerates the `.ics`.

## Setup — Path A (live Google Calendar) — default

Uses a **Google service account** so the hourly job can write unattended (no token expiry).
The credential/account steps below are done by **you** in Google's UI — this tool never
signs in as you.

**1. Google Cloud (one-time, free, no billing):**
   - Create a project → **APIs & Services** → enable the **Google Calendar API**.
   - **Credentials → Create credentials → Service account** → create it, then **Keys → Add
     key → JSON** and download the key file. Note the service account's **email**
     (`…@….iam.gserviceaccount.com`).

**2. Create + share the calendar (in Google Calendar):**
   - **Settings → Add calendar → Create new calendar**, name it **"KIDA NYC — Open Slots"**.
   - Open its settings → **Share with specific people** → add the service account's email
     with **"Make changes to events"** (least privilege).
   - For a public link: **Access permissions → Make available to public** (read-only), then
     **Get shareable link** → this is your live-calendar link
     (`https://calendar.google.com/calendar/u/0?cid=<id>`).
   - **Settings → Integrate calendar → Calendar ID** — copy it.

**3. Try it locally:**
   ```bash
   python3 -m venv .venv && .venv/bin/pip install -r requirements-calendar.txt
   KIDA_SERVICE_ACCOUNT_JSON=sa-key.json KIDA_CALENDAR_ID='<calendar-id>' \
     .venv/bin/python -m src.sync_calendar --dry-run     # insert/patch/delete diff
   # drop --dry-run to actually write; events appear within ~1 min
   ```

**4. Deploy (hourly, $0):** add repo **secrets** `KIDA_SERVICE_ACCOUNT_JSON` (paste the
   key file's contents) and `KIDA_CALENDAR_ID`. `.github/workflows/sync.yml` runs hourly.
   Kick it off once via **Actions → sync-open-slots → Run workflow**.

Share the `cid` link from step 2 with anyone — for logged-in Google users it adds as a
native calendar that updates within the hour.

**Keep the schedule alive.** GitHub auto-disables scheduled workflows after ~60 days with
no repository activity — and commits by the Actions bot don't count. `keepalive.yml`
pushes a weekly empty commit that *does* count, using a **fine-grained Personal Access
Token**. To enable it: create a fine-grained PAT scoped to **this repo only**, with
**Contents: Read and write**, and add it as the secret **`KIDA_KEEPALIVE_PAT`**. Until
that secret exists the keepalive safely no-ops (and you'd just re-enable the workflow
manually if GitHub ever pauses it).

## Setup — Path B (subscribe by `.ics` URL) — fallback

Credential-free, but Google re-reads external `.ics` URLs slowly (~8–24h).

1. Make the repo public; **Settings → Pages → Source: GitHub Actions**.
2. **Actions → publish-ics-feed → Run workflow** (manual; re-enable its cron to auto-run).
   Publishes to `https://<user>.github.io/<repo>/open_slots.ics`.
3. Google Calendar → **Other calendars → + → From URL** → paste → **Add calendar**.

```bash
.venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.build_feed --out public/open_slots.ics   # writes the .ics
.venv/bin/python -m src.fetch_availability                       # just print what's open
```

## Configuration

See `config.yaml` — lookahead window, stylist/service allowlists, evenings-only /
weekends-only filters, politeness (request delay, per-run cap, cache TTL). Service and
staff ids are catalogued in `docs/timely-api.md`.

## Booking links

Each event links to KIDA's booking page. Timely has **no shareable per-slot deep link**
(its URLs are session-bound), so links go to the generic booking start
`https://bookings.gettimely.com/kidanyc/bb/book` and the description says so rather than
implying one-click booking. See `docs/timely-api.md`.

## Safety / guardrails

- **Read-only.** No booking, no PII, no account creation. ~1 req/sec, backoff on 429/5xx,
  hard per-run request cap.
- **No wipe on failure.** If a run's fetch doesn't clearly succeed, the calendar sync
  **skips all deletes** (and `build_feed` exits without writing), so a transient outage
  can't blank your calendar. Verified by a unit test.
- **DST-correct.** All times are tz-aware `America/New_York`, unit-tested across the
  Nov 1 2026 fall-back.
- If Timely serves a CAPTCHA or blocks the client, the run fails loudly. See `docs/compliance.md`.

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Fixtures in `tests/fixtures/` are real captured API responses (incl. a fully-booked day);
the DST boundary and the no-wipe guard are unit-tested.

## Re-deriving the API

If Timely changes, re-run recon: `python3 recon/capture.py` (stdlib only). It reproduces
the funnel and prints live slots; update `docs/timely-api.md` and `src/catalog.py` from it.
