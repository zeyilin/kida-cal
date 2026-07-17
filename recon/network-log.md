# Observed network traffic (Phase 0)

Captured 2026-07-17 by driving the live funnel in Chrome (network panel) and then
reproducing it headlessly with `recon/capture.py`. Requests are reproducible; this is
a curated summary rather than a raw dump (recon used the browser + a stdlib HTTP
client, not Playwright's logger).

## Booking-relevant requests, in funnel order

| # | Method | URL | Purpose | Response |
|---|---|---|---|---|
| 1 | GET | `book.gettimely.com/kidanyc/book/embed?client-login=true` → 302 → `/Booking/Location/10796` | bootstrap; sets cookies + mints `obg`; renders service catalog | HTML |
| 2 | POST | `book.gettimely.com/Booking/Service?obg=<obg>` | select service (`BookableTimeSlotItemIds=<id>:SV` + echoed `ServiceStaffIds[..]`) | 302 → StaffSelection HTML |
| 3 | POST | `book.gettimely.com/Booking/StaffSelection?obg=<obg>` | select staff (`SelectedStaffId=<id>`) | 302 → DateSelection HTML |
| 4 | GET | `book.gettimely.com/Booking/GetOpenDates?obg=<obg>&month=&year=&staffId=&tzName=&tzId=80` | month availability (morning/afternoon booleans) | **JSON** |
| 5 | GET | `book.gettimely.com/booking/gettimeslots/?obg=<obg>&dateSelected=YYYY-MM-DD&staffId=&tzName=&tzId=80` | exact slots for one day | **HTML partial** (base64 `BookingSelection` tokens) |

## Notes captured during recon

- Cookies set on request 1: `is-client-login-kidanyc`, `__cf_bm` (Cloudflare bot mgmt).
- No `__RequestVerificationToken` / CSRF on any funnel form.
- `staffId` on #4/#5 is a query param and **overrides** the staff POSTed at #3
  (distinct open-day sets per stylist confirmed); `staffId=0` = "any staff".
- Clicking a different calendar day in the browser fired **one** request to
  `/booking/gettimeslots/` (endpoint #5) with `dateSelected` — that is the real
  per-day source.
- Third-party/noise requests seen and ignored: newrelic, google-analytics,
  facebook (503), fonts, tui.esm.js assets.

## Reproduce

```
python3 recon/capture.py            # slots for Nao / Hair Cut
python3 recon/capture.py --catalog  # full service/staff catalog
python3 recon/capture.py --service <id> --staff <id> --month M --year Y
```
