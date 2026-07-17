# KIDA NYC — Open Slots calendar

A read-only calendar that mirrors **available** hair/barber appointment slots at
[KIDA NYC](https://kidanyc.com) for the next N days, publishing them as a **subscribable
`.ics` URL** you add to Google Calendar. Each slot is a **Free**, no-notification event
prefixed `OPEN ·`, with a booking link in the description. It **never books, holds, or
cancels anything** — it only reads public availability.

> Availability is a snapshot; every event says so and links to KIDA's booking page to confirm.

## Two ways to consume it

| | **A. Subscribe by URL (default)** | **B. Google Calendar API (optional)** |
|---|---|---|
| How | Publishes a public `.ics` at a GitHub Pages URL; you add it via "From URL" | Writes events straight into a calendar in your account |
| Setup | Just make the repo public + enable Pages. **No Google login.** | One-time Google OAuth + repo secrets |
| Freshness | **Google re-fetches on its own slow schedule (~8–24h)** | Updates within the hour |
| Cost | $0/mo | $0/mo |

You asked for **A** — that's what's set up by default (`.github/workflows/feed.yml`).
**B** stays available as the low-latency option (`.github/workflows/sync.yml`) if the
refresh lag bugs you later.

## How it works

```
config.yaml ─▶ src/fetch_availability.py ─▶ [Slot] ─▶ group ─▶ [Event]
                     │  (src/timely.py funnel)                    │
                     │                                            ├─(A)▶ src/build_feed.py ─▶ public/open_slots.ics ─▶ GitHub Pages URL
                     └▶ kidanyc.com notices                       └─(B)▶ src/sync_calendar.py ─▶ Google Calendar API
```

Hourly, GitHub Actions reads KIDA's live availability by walking Timely's booking funnel
from a plain HTTP client (no browser — see `docs/timely-api.md`), normalizes each opening
into a timezone-aware slot, de-dupes overlapping services, and regenerates the `.ics`.

## Setup — Path A (subscribe by URL)

1. **Make the repo public** (public repos get unlimited free Actions minutes → $0/mo).
2. **Enable GitHub Pages:** repo **Settings → Pages → Build and deployment → Source: GitHub Actions**.
3. **Run the workflow:** Actions tab → **publish-ics-feed** → *Run workflow* (it also runs hourly).
   It publishes the calendar to:
   ```
   https://<your-user>.github.io/<repo-name>/open_slots.ics
   ```
4. **Subscribe in Google Calendar:** on desktop, next to **Other calendars** click **+**
   → **From URL** → paste that URL → **Add calendar**.

That's it — no credentials anywhere.

> **Freshness caveat:** Google controls how often it re-reads a subscribed URL — commonly
> every several hours to a day. We regenerate hourly, but Google won't pull that fast, so a
> just-booked slot can linger in your view for a while. If that matters, use Path B.

### Try it locally

```bash
python3 -m venv .venv && .venv/bin/pip install -r requirements.txt
.venv/bin/python -m src.build_feed --out public/open_slots.ics   # writes the .ics
.venv/bin/python -m src.fetch_availability                       # just print what's open
```

## Setup — Path B (Google Calendar API, optional, faster)

Needs `pip install -r requirements-calendar.txt` and a one-time Google **Desktop app**
OAuth client (no billing account). Authorize once locally:

```bash
KIDA_GOOGLE_CLIENT_SECRET=client_secret.json \
  .venv/bin/python -m src.sync_calendar --dry-run
```

This creates the **"KIDA NYC — Open Slots"** calendar and never touches your primary one.
For CI, add repo secrets `KIDA_TOKEN_JSON` + `KIDA_CLIENT_SECRET_JSON`, then enable the
schedule in `sync.yml` (and disable `feed.yml` so Timely isn't hit twice). Credential steps
(creating the OAuth client, signing in, granting consent) are done by **you** in Google's
UI — this tool never enters your Google password.

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
- **No empty-feed wipe on failure.** If a run's fetch doesn't clearly succeed, `build_feed`
  exits non-zero **without** writing, so the previously published feed stays live (Path B
  likewise skips all deletes). A transient outage can't blank your calendar.
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
