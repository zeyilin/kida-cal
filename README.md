# KIDA NYC — Open Slots calendar

A read-only calendar that mirrors **available** hair/barber appointment slots at
[KIDA NYC](https://kidanyc.com) up to 90 days out as a **live Google Calendar** you can
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

GitHub Actions reads KIDA's live availability by walking Timely's booking funnel from a
plain HTTP client (no browser — see `docs/timely-api.md`), normalizes each opening into a
timezone-aware slot, de-dupes overlapping services, then (A) reconciles the events into a
Google Calendar (insert/patch/delete) or (B) regenerates the `.ics`.

### Scheduling & freshness

The sync runs **every hour**, with a two-tier depth so it's fresh *and* polite to Timely:

| When | Window | Why |
|---|---|---|
| Every hour | Next ~3 weeks (`KIDA_LOOKAHEAD_DAYS=21`) | Fast (~15 min). Cancellations/new openings surface **within the hour** |
| Every 6th hour (00/06/12/18 UTC) | Full 90 days (`config.lookahead_days`) | The complete view; ~1h run, too heavy to do hourly |

A single 90-day sweep of every service × stylist is a ~1-hour, request-heavy run, so doing
it every hour would run near-continuously and get rate-limited/blocked. Deletes are
**window-scoped** — each run only prunes stale events inside its own lookahead — so the
short and deep sweeps share one calendar without fighting.

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
   key file's contents) and `KIDA_CALENDAR_ID`. `.github/workflows/sync.yml` then runs on
   the hourly two-tier schedule above. Kick it off once via **Actions → sync-open-slots →
   Run workflow**. Until both secrets exist, the workflow safely no-ops.

## Sharing the calendar

Once the calendar is set to public (Path A, step 2), share it via links from its Google
Calendar settings — the repo never stores these:

- **Add to Google Calendar** (native, updates within the hour): *Settings → Integrate
  calendar → **Get shareable link*** (`…/calendar/u/0?cid=<id>`).
- **Browser view** (no Google account): the **Public URL** / embed link in the same panel.
- **Apple Calendar / Outlook**: the **Public address in iCal format** (`…/ical/<id>/public/basic.ics`).

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

See `config.yaml`. Current defaults track **every online service** for **every stylist &
barber** (`services: all`, `stylists: all`) over a **90-day** deep window; the hourly job
overrides the window to 21 days via `KIDA_LOOKAHEAD_DAYS` (see Scheduling). Also there:
evenings-only / weekends-only filters and politeness knobs (request delay, per-run cap,
cache TTL). Service and staff ids (all 9 staff, incl. master barbers Sachi / Taka (Aki) /
Yohei / Fausto / Shin) are catalogued in `docs/timely-api.md`.

> A stylist with no openings inside the window simply has no events — that's accurate, not
> a gap. Barbers who book further out only appear once the 90-day sweep reaches them.

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
  can't blank your calendar. Deletes are also **window-scoped**. Both verified by unit tests.
- **DST-correct.** All times are tz-aware `America/New_York`, unit-tested across the
  Nov 1 2026 fall-back.
- If Timely serves a CAPTCHA or blocks the client, the run fails loudly. See `docs/compliance.md`.

## Privacy

- **Secrets stay secret.** The service-account key and calendar ID live only in encrypted
  GitHub Actions secrets — never in the code or git history (`.gitignore` blocks key/token
  files; `config.yaml` keeps `calendar_id: null`). Secrets aren't exposed by a public repo,
  aren't given to fork PRs, and can't be read back after saving.
- **No personal data.** The tool reads only KIDA's *public* availability — no bookings, no
  PII. Staff names are public info from KIDA's booking page. Commits use GitHub's `noreply`
  email, not your real one.
- **Why the repo is public.** The Calendar API path doesn't need Pages, but the hourly +
  90-day schedule uses ~16k Actions minutes/month — free only on a **public** repo (private
  repos get 2,000/mo). Public exposes the *code* (no secrets) and that you track KIDA; going
  private would mean paying for minutes or dropping to a few runs/day.
- **The calendar is public by choice** so the share links work; it shows open-slot times
  only. To restrict it, share with specific Google accounts instead of "Make public."

## Tests

```bash
.venv/bin/python -m pytest tests/ -q
```

Fixtures in `tests/fixtures/` are real captured API responses (incl. a fully-booked day);
the DST boundary and the no-wipe guard are unit-tested.

## Re-deriving the API

If Timely changes, re-run recon: `python3 recon/capture.py` (stdlib only). It reproduces
the funnel and prints live slots; update `docs/timely-api.md` and `src/catalog.py` from it.
