# Compliance findings — Timely / KIDA NYC

_Recorded during Phase 0 recon on 2026-07-17. Usage profile re-measured 2026-07-25._

## robots.txt

`https://bookings.gettimely.com/robots.txt` (2026-07-17) and
`https://book.gettimely.com/robots.txt` (2026-07-25) are identical:

```
User-Agent: *
Disallow: /cdn-cgi/
```

- The **only** disallowed path is `/cdn-cgi/` (Cloudflare internals).
- The booking funnel we read (`/kidanyc/bb/book`, and the underlying
  `book.gettimely.com/Booking/*` + `/booking/gettimeslots/`) is **not disallowed**.
- Both hosts now captured. `book.gettimely.com` is the one that actually serves the
  booking app, and it is the host every scheduled run talks to.

## Terms of Service

- `https://www.gettimely.com/terms-of-service/` is a JS-rendered SPA; the text
  could **not** be retrieved programmatically (curl/WebFetch return an empty shell).
  **Action for the user:** skim it manually before deploying an automated reader.
- Nothing in robots.txt forbids this access. The relevant question for ToS is
  usually a general "no automated/bulk access" clause. Our usage profile is
  deliberately minimal (see below) and is meant to be indistinguishable from a
  single person periodically checking availability.

## Our usage profile (self-imposed limits)

- **Read-only.** We only read public availability. We never POST a booking, never
  enter PII, never create an account, never touch the payment/deposit step. The two
  POSTs we do make (`/Booking/Service`, `/Booking/StaffSelection`) only move an
  anonymous session through the funnel; nothing is reserved or submitted.
- **Low volume.** Sequential requests, ~1 req/sec (`request_delay_seconds`), honest
  self-identifying User-Agent, exponential backoff on 429/5xx, hard per-run request cap.
- **No PII stored.** We persist availability (times), not people.

### Measured volume (not estimated)

The earlier "low hundreds of requests per run" figure described a haircut-only feed.
The deployed config is all services × all stylists × 90 days, so it was wrong by an
order of magnitude. Measured on a real deep sweep, and after the de-duplication
described in `src/fetch_availability.py`:

| | requests / deep run | wall clock |
| --- | --- | --- |
| before de-duplication | ~5,050 | ~84 min |
| after | ~2,400 | ~39 min |
| near-term (21-day) run | ~410 | ~7 min |

Every run now prints its own `requests_made`, so this table can be checked rather than
trusted. Deep sweeps run roughly every 6 hours, not hourly.

**The response cache is a development aid only.** It cannot hit in CI (runners start
clean, `.cache/` is gitignored, each key is requested once per run), so it was never a
production politeness measure and is no longer described as one — the sync workflow
sets `KIDA_CACHE_TTL_SECONDS=0`. Politeness in production comes from the pacer and from
cutting requests at the source; caching availability would also serve stale times into
a product whose entire value is freshness.

## Stop conditions (hard guardrails)

If any of the following occur, **stop and report** — do not work around them:

- A CAPTCHA or bot-check challenge appears (none seen during recon).
- The client is IP-blocked or rate-limited persistently.
- The ToS review turns up an explicit prohibition on automated access.
- Timely starts requiring login/auth to view availability.

These are enforced in code, not just documented — see `src/timely.py`:

- `MAX_CONSECUTIVE_THROTTLES` (5): five consecutive 429s raise `BudgetExhausted` and
  end the run. The next scheduled run retries; we do not grind.
- `MAX_RETRY_AFTER_SECONDS` (120): if Timely asks us to wait longer than this, we stop
  rather than sleep through it. We never wait *less* than a `Retry-After` asks.
- `max_requests_per_run`: a hard cap. Hitting it aborts the run and — importantly —
  marks the fetch untrusted, so a truncated run can never trigger calendar deletions.
- `BudgetExhausted` deliberately bypasses the per-lookup `except` handlers, so a
  throttled run fails loudly instead of being reported as healthy-but-thin.

The User-Agent is `kida-cal/0.2 (+personal read-only availability mirror; contact via
repo issues)`. It previously carried a `Mozilla/5.0 (Macintosh; ...)` prefix, which
contradicted the "we do not attempt to evade or forge bot detection" claim below; it
has been removed so the string is honest about what we are.

## Observations relevant to compliance

- No authentication is required to read availability — the funnel works from a
  cold, anonymous session.
- A `__cf_bm` (Cloudflare bot-management) cookie is set on first request and must be
  carried by the cookie jar. It is issued automatically to any normal client; we do
  not attempt to evade or forge bot detection.
- No `__RequestVerificationToken` / CSRF token is present on the funnel forms.
