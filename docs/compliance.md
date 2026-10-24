# Compliance findings — Timely / KIDA NYC

_Recorded during Phase 0 recon on 2026-07-17. Usage profile re-measured 2026-07-25; run
cadence and the freshness gate described 2026-10-12; stop conditions brought in line with
`src/timely.py` 2026-10-19; volume table updated from the October 2026 production runs
2026-10-24._

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
  self-identifying User-Agent, exponential backoff on 429/5xx and on connection failures
  and timeouts (never sooner than a `Retry-After` asks), hard per-run request cap,
  and at most one sweep started per ~50 minutes however often the workflow is
  triggered, with the few exceptions listed under "How often we look" below.
- **No PII stored.** We persist availability (times), not people.

### Measured volume (not estimated)

The earlier "low hundreds of requests per run" figure described a haircut-only feed.
The deployed config is all services × all stylists × 90 days, so it was wrong by an
order of magnitude. Measured, not estimated: the first row on a real deep sweep before
the de-duplication described in `src/fetch_availability.py`, the other two from the
October 2026 production runs (near-term sync steps took 9m50s–11m08s):

| | requests / deep run | wall clock |
| --- | --- | --- |
| before de-duplication | ~5,050 | ~84 min |
| after | ~1,700 | ~40 min |
| near-term (21-day) run | ~385 | ~10 min |

Earlier measurements ran lower: on 2026-08-09, after the 3-second POST pacing landed, a
deep sweep made 1,575 requests in 37m50s and a near-term run 240 requests in 6m59s.
Request counts scale with how much availability actually exists, so they drift from week
to week, and a near-term run's wall clock moves with them — it went from ~7 to ~10
minutes as its request count rose. Every run prints its own `requests_made`, so this
table can be checked rather than trusted. Deep sweeps run about every 6 hours
(`deep_sweep_every_hours`), not on every run — but that is a target, not a ceiling. A deep
sweep that fails the run for any reason (an untrusted fetch, more lost calendar writes or
more withheld deletes than the run's exit floors allow, a refused delete pass) is not
recorded as one, so it stays due. A few lost writes under that floor are left to the next
scheduled deep sweep: the days 22–90 openings they carried stay off Google Calendar, and
a far block they were replacing stays up, until then — up to about 10 hours at the
current cadence — though the `.ics` feed that sweep published already has them. After a
deep sweep that is not recorded, the next run that gets past the gate sweeps near
instead, so that days 0–21 stay fresh, and the one after it retries deep: while such a
condition lasts, every other run can be a ~40-minute deep sweep. That only holds until the
last clean deep sweep is 12 hours old (`NEAR_FALLBACK_HOURS` in `src/sync_calendar.py`);
from then on every run is a deep sweep until one completes. That is deliberate: the near
runs in between succeed, so alternating indefinitely would keep a deep sweep that never
completes from ever reaching the workflow's 12-hour failure-issue rule. A manual `full`
dispatch is always deep.

### How often we look: the freshness gate

The sync workflow is scheduled four times an hour (`:08`, `:23`, `:38`, `:53`). That is not
four sweeps an hour. GitHub delivers only a fraction of scheduled ticks — since late
September 2026 about 4 a day, with a median gap of 5–6 hours — and the extra cron lines
exist only to give a run more chances to be delivered at all. What bounds the load on
Timely is a gate in the program itself:

- Every run the workflow starts passes `--skip-if-checked-within 50` (a re-run passes 0;
  see the exceptions below). Before sending Timely anything it reads the calendar's
  status marker, and if the last run **started** less than 50 minutes ago it exits
  without making a single Timely request.
- The gate measures start-to-start, and a run records its start on the marker *before*
  its first Timely request, so a run that fails or is throttled still counts: ticks that
  land within 50 minutes of its start skip.
- Start-to-start bounds how often a sweep *starts*, not the rest Timely gets between
  sweeps. A run that itself lasts 50 minutes or more — a throttled deep sweep that ran
  long, or one cut off by the job's 120-minute timeout — can be followed at once by the
  run queued behind it (the workflow's concurrency group holds at most one), which finds
  the start stamp old enough and sweeps. When the long run was a deep sweep that did not
  complete, that follow-on sweep is the short near-term one (`resolve_depth` in
  `src/sync_calendar.py`) — unless the last clean deep sweep is 12 or more hours old, when
  it is another deep sweep (see above).
- Net effect: at most one sweep starts per ~50 minutes however many ticks (or external
  triggers, see the README) arrive, outside the exceptions below. With every tick on time
  that works out to hourly. The ceiling is ~29 sweeps a day, against the 24 the old single
  hourly cron line allowed in principle; in practice GitHub has been delivering about 4.

The exceptions are known and rare:

- A manual dispatch with depth `near` or `full` is a person asking for a run now, and is
  not gated. Depth `auto` — which is what the README's external trigger sends — is.
- A re-run (GitHub's "Re-run jobs") is likewise a person asking, and is not gated. It is
  almost always the retry of a run that just failed, whose own start stamp would
  otherwise turn the retry into a no-op. GitHub never re-runs anything by itself.
- A calendar with no status marker — a new setup, or one whose marker someone deleted —
  has nothing to measure from, and keeps having nothing until a run gets far enough to
  write one (past the fetch, to the calendar writes). The start is only ever recorded on
  an existing marker, so until then every tick sweeps, including straight after a
  throttled or failed run, and with no record of a deep sweep each one goes deep. If a new
  setup's first runs are being throttled, disable the workflow until it is resolved.
- Recording the start is best-effort for transient errors (429, 5xx, a rate-limit 403):
  the run warns and carries on. Its start is still recorded when the marker is rewritten
  at the end, so the next tick is gated as usual — unless the run dies before that
  rewrite (an untrusted or throttled fetch, an aborted write pass, the timeout), which
  leaves the next tick ungated. A refusal every write would hit (a read-only share, an
  exhausted quota) is not transient: it stops the run before any Timely request.

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

- `MAX_CONSECUTIVE_THROTTLES` (3): three consecutive *fully-retried* requests to the same
  endpoint — each having already spent all 5 attempts, so ~15 actual 429 responses — raise
  `BudgetExhausted`, which stops every further Timely request this run (what that does to
  the run's result is under `BudgetExhausted` below). The next scheduled run retries — no
  sooner than 50 minutes after this one started, because a throttled run still counts
  toward the freshness gate above (once the calendar has a status marker; see the
  exceptions there) — so we do not grind.

  Counted per `(method, path)`, which matters more than it looks. Timely throttles one
  funnel step at a time while its neighbours stay healthy, and every step we make has a
  healthy sibling immediately before it (`bootstrap` → `/Booking/Service` →
  `/Booking/StaffSelection`; `GetOpenDates` → `gettimeslots`). A counter shared across
  siblings gets reset to zero by the sibling's success before the throttled step
  re-increments it, so it oscillates and never reaches the threshold. That is not
  hypothetical: it is why this stop condition did **not** fire on 2026-08-09, when 16
  services' `/Booking/Service` POSTs 429'd through every retry for 12 minutes.
- `MAX_THROTTLED_REQUESTS_PER_RUN` (5): a monotonic backstop, never cleared by any
  success. "Consecutive" has now been defeated twice by call orders nobody predicted, so
  the streak above is the fast trip and this total is the guarantee: broad throttling that
  never forms a streak on any single endpoint still stops the run.
- `MAX_RETRY_AFTER_SECONDS` (120): if Timely asks us to wait longer than this — on a 429 or
  on a 5xx, since an overloaded edge says "back off" with a 503 too — we stop rather than
  sleep through it. We never wait *less* than a `Retry-After` asks — including the RFC 7231
  HTTP-date form, which we previously could not parse and silently replaced with a 2-second
  backoff. A `Retry-After` we cannot read at all also stops the run rather than being
  treated as permission to retry immediately.
- An endpoint that stops answering — connection failures, timeouts or 5xx — is bounded by
  the two backstops below. Each request that uses up its 5 attempts on such failures fails
  only its own lookup, and `MIN_LOOKUP_SUCCESS_RATIO` (in `src/fetch_availability.py`)
  decides whether the run is still trusted. There is deliberately no streak stop for
  these: one stylist's or one service's fault, or a short window of 503s, tripped it on
  sweeps that would have finished trusted — the comment above
  `MAX_TRANSPORT_FAILURES_PER_RUN` in `src/timely.py` has the cases.
- `MAX_TRANSPORT_FAILURES_PER_RUN` (50): a run-wide total of failed connection attempts and
  timeouts (not 5xx), never cleared by any success. Each costs up to ~50 seconds, so this is
  what stops a network that drops requests at random, without ever forming a streak, from
  dragging a run past its job timeout.
- `MAX_RETRY_SECONDS_PER_RUN` (40 minutes): the backstop under all of the above, in time.
  Every failed attempt of any kind is charged what it cost — the attempt itself plus the
  backoff or `Retry-After` wait before the next one — and the run stops rather than sleep
  past 40 minutes of retrying in total. A host that keeps asking for long waits, or keeps
  failing slowly, is therefore stopped even when every request eventually succeeds.
- `max_requests_per_run`: a hard cap. Hitting it aborts the run and — importantly —
  marks the fetch untrusted, so a truncated run can never trigger calendar deletions.
- `BudgetExhausted` deliberately bypasses the per-lookup `except` handlers, so a throttled
  run can never be reported as healthy-but-thin. `fetch()` catches it once at the sweep
  level and returns a result explicitly marked untrusted — `ok=False`, and delete authority
  over nobody — so the run still writes its fetch artifact and exits 2 ("nothing was written
  to the calendar") instead of dying with a traceback on what is an expected outcome.

  The one exception is a stop condition that trips once every lookup has already landed:
  during the de-duplication spot-checks made after the sweep, or on the last planned
  service's own spot-check. It still stops every further Timely request, but it voids
  nothing, because the data is already complete: the result stays `ok=True`, its errors
  record `dedup spot-checks stopped …`, and the run writes to the calendar as usual (exit 0
  unless something else fails). A throttle on the re-ask that a spot-check mismatch
  triggers is not covered by that exception: the mismatch it was checking stands, so that
  fetch is untrusted and exits 2.

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
