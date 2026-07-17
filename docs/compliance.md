# Compliance findings — Timely / KIDA NYC

_Recorded during Phase 0 recon on 2026-07-17. Re-check before deploy._

## robots.txt

`https://bookings.gettimely.com/robots.txt`:

```
User-Agent: *
Disallow: /cdn-cgi/
```

- The **only** disallowed path is `/cdn-cgi/` (Cloudflare internals).
- The booking funnel we read (`/kidanyc/bb/book`, and the underlying
  `book.gettimely.com/Booking/*` + `/booking/gettimeslots/`) is **not disallowed**.
- `book.gettimely.com/robots.txt` should be re-checked too; the booking app is
  actually served from that host. (Not yet captured — do before first scheduled run.)

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
  enter PII, never create an account, never touch the payment/deposit step.
- **Low volume.** Sequential requests, ~1 req/sec, descriptive User-Agent
  (`kida-cal-recon/...`), exponential backoff on 429/5xx, hard per-run request cap,
  short-TTL response cache during development.
- **No PII stored.** We persist availability (times), not people.
- Estimated volume at hourly sync for a haircut-only feed: low hundreds of requests
  per run (see `docs/timely-api.md`), far under any rate that would look abusive.

## Stop conditions (hard guardrails)

If any of the following occur, **stop and report** — do not work around them:

- A CAPTCHA or bot-check challenge appears (none seen during recon).
- The client is IP-blocked or rate-limited persistently.
- The ToS review turns up an explicit prohibition on automated access.
- Timely starts requiring login/auth to view availability.

## Observations relevant to compliance

- No authentication is required to read availability — the funnel works from a
  cold, anonymous session.
- A `__cf_bm` (Cloudflare bot-management) cookie is set on first request and must be
  carried by the cookie jar. It is issued automatically to any normal client; we do
  not attempt to evade or forge bot detection.
- No `__RequestVerificationToken` / CSRF token is present on the funnel forms.
