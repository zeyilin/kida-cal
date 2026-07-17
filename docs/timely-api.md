# Timely booking API — KIDA NYC (Phase 0 recon output)

_Derived from observed traffic on 2026-07-17, not guessed. Reproduce any time with
`python3 recon/capture.py`._

## TL;DR

- The React page at `bookings.gettimely.com/kidanyc/bb/book` is just an **iframe
  wrapper** around a classic **server-rendered ASP.NET flow** on `book.gettimely.com`.
- Availability is reachable from a **plain HTTP client with a cookie jar** — **no
  headless browser, no JS execution required.** This was proven end-to-end in
  `recon/capture.py` (stdlib `urllib` only).
- The flow is **stateful**: a per-session `obg` GUID + cookies carry a chosen
  service. Staff and date, however, are plain **query params** you can vary freely
  within one session.
- Exact slot times come from an HTML partial whose radio buttons carry a base64
  token that decodes to `date, service, staff, startMin, endMin`.

**Route recommendation: Route B (public GitHub repo + Actions) with a lightweight
pure-HTTP fetcher (no Playwright).** Route A (Apps Script) is _possible_ but carries
real risk; see "Route recommendation" at the bottom.

---

## Constants

| Thing | Value |
|---|---|
| Business slug | `kidanyc` |
| Booking host (real app) | `book.gettimely.com` |
| Wrapper host (public link) | `bookings.gettimely.com/kidanyc/bb/book` |
| Location id | `10796` (KIDANYC, 369 Broome Street, New York) |
| Timezone id (`tzId`) | `80` (America/New_York) |
| Cookies required | `is-client-login-kidanyc`, `__cf_bm` (both auto-issued) |
| CSRF token | none |

---

## The funnel

Every session mints a fresh `obg` GUID (an "order booking group" id). It is the
session key threaded through every request.

### 0. Bootstrap (cold, anonymous)

```
GET https://book.gettimely.com/kidanyc/book/embed?client-login=true
  -> 302 -> /Booking/Location/10796?mobile=True&params=...
```

Sets cookies and returns the **service-selection** HTML. The `obg` is embedded in
the form action: `/Booking/Service?obg=<guid>`.

This page is also the **service + staff catalog** — see "Catalog" below.

### 1. Select service  (POST — writes session state)

```
POST /Booking/Service?obg=<obg>
Content-Type: application/x-www-form-urlencoded

LocationId=0
BookableTimeSlotItemIds=<serviceId>:SV        # the chosen service, e.g. 5319865:SV
ServiceStaffIds[5319865:SV]=287218,367833,175308   # echo ALL hidden fields from the page
... (24 ServiceStaffIds[..] hidden fields) ...
commit=
```

Response: the **staff-selection** page (302 -> `/Booking/StaffSelection?obg=<obg>`),
listing `SelectedStaffId` radios for the staff who can do this service.

> The service is now baked into the session (`obg`). To query a *different* service,
> start a new session.

### 2. Select staff  (POST — advances the funnel)

```
POST /Booking/StaffSelection?obg=<obg>
SelectedStaffId=<staffId>       # any valid staff id for this service; 0 = "any"
commit=
```

Response: 302 -> `/Booking/DateSelection?obg=<obg>`.

> You only need to POST staff **once** to reach the date step. After that, staff is a
> **query param** on the two endpoints below and overrides the POSTed value
> (verified: Masa / Satomi / Nao return distinct open-day sets in the same session).

### 3. Open dates for a month  (GET — JSON)

```
GET /Booking/GetOpenDates?obg=<obg>&month=7&year=2026&staffId=<staffId>&tzName=&tzId=80
```

```json
{
  "firstOpenDate": "2026-07-19",
  "firstAvailableDate": "2026-07-19",
  "openDates": [
    { "day": "2026-07-19", "isAvailableMorning": true,  "isAvailableAfternoon": false },
    { "day": "2026-07-20", "isAvailableMorning": true,  "isAvailableAfternoon": true  }
  ]
}
```

Morning/afternoon booleans only — **not** exact times. `staffId=0` returns the
"any staff" union. Call once per month; a 28-day lookahead spans at most 2 months.

### 4. Exact time slots for one day  (GET — HTML partial)

```
GET /booking/gettimeslots/?obg=<obg>&dateSelected=2026-07-21&staffId=<staffId>&tzName=&tzId=80
```

Returns an **HTML fragment** (~12 KB), not JSON. Each bookable slot is a radio:

```html
<input type="radio" name="BookingSelection" value="<base64>">
```

There is **no per-day XHR when the user clicks around the calendar** in the browser
— the SPA prefetches, but the endpoint above is the real source and is what we call.

#### Decoding the `BookingSelection` token

`base64decode(value)` →

```
2026-07-21,,5319865:SV;5319865;-1;175308;540;595;0
   date   , , itemId  ;svcId ; grp;staff ;startMin;endMin;n
```

- `startMin`/`endMin` are **minutes since local midnight**: `540` = 09:00, `595` = 09:55.
- So this token = Hair Cut with staff 175308 (Nao), 2026-07-21 09:00–09:55, **local
  (America/New_York) wall time**. Build tz-aware datetimes from `date` + `startMin`
  in `America/New_York`; do **not** store naive times.

The same partial also embeds a rich JSON per slot (in a hidden field) with ISO local
datetimes and human labels, e.g.:

```json
{"staff":{"staffId":175308,"aliasOrFirstName":"Nao ","frequencyInMinutes":60},
 "service":{"serviceId":5319865,"name":"Hair Cut"},
 "clientTimeOfService":"2026-07-21T09:00:00","endOfClientService":"2026-07-21T09:55:00"}
```

Either source works; the compact token is easiest and most stable to parse.

---

## Catalog (services)

24 bookable services across 3 categories. `staff` = staff ids able to perform it.
Prices below are from the service list; **note master-barber price can vary by
staff** (each master barber shows their own price on the staff page — all currently
$60 for a Haircut).

### Hair Salon (staff: Masa 287218, Satomi 367833, Nao 175308)

| serviceId | name | duration | price |
|---|---|---|---|
| 5319865 | Hair Cut | 55m | Varies (from $75) |
| 5319877 | Blow Out | 55m | Varies |
| 71794 | Roots Color / Re-touch | 1h30 | Varies |
| 71795 | Single Process Color | 2h | Varies |
| 72465 | Full Head Foil Highlight | 3h | Varies |
| 72464 | Half Head Foil Highlight | 3h | Varies |
| 72466 | Two+ Process / Bleach / Corrective | 4h | Varies |
| 245259 | Ombre / balayage | 3h | Varies |
| 72467 | Deep Conditioner | 30m | Varies |

(Roots/Single/Foil/Ombre are staffed by Masa + Nao only, not Satomi.)

### Barber Shop / Master Barber (staff: 24102, 173532, 24107, 24105, 24104)

Names seen on the staff page: **Sachi, Taka (Aki), Yohei, Fausto** (+ one more of
the five ids — exact id↔name pairing to be nailed in Phase 1).

| serviceId | name | duration | price |
|---|---|---|---|
| 71521 | Haircut | 25m | $60 |
| 71522 | Portion Haircut | 25m | $50 |
| 71523 | Buzz | 25m | $50 |
| 71525 | Head Shave (Razor) | 30m | $60 _(staff 24105 only)_ |
| 71527 | Beard Trim | 25m | $40 |
| 71528 | Beard Shave (Razor) | 30m | $60 _(24102, 24105)_ |
| 3142646 | Hair Cut & Beard Trim | 55m | $95 |
| 3142652 | Portion Cut & Beard Trim | 55m | $85 |
| 71529 | Haircut & Beard Shave | 55m | $115 |

### Barber Shop / Junior Barber (staff: Hiroki 686999)

| serviceId | name | duration | price |
|---|---|---|---|
| 5319831 | Hair Cut | 55m | $50 |
| 5319834 | Portion Hair Cut | 55m | $40 |
| 5319836 | Buzz | 55m | $40 |
| 5319837 | Beard Trim | 55m | $30 |
| 5319841 | Hair Cut & Beard Trim | 1h25 | $75 |
| 5319844 | Portion Hair Cut & Beard Trim | 1h25 | $65 |

### Staff id → name (known so far)

| staffId | name | role |
|---|---|---|
| 287218 | Masa | Stylist |
| 367833 | Satomi | Stylist |
| 175308 | Nao | Stylist |
| 24102 / 173532 / 24107 / 24105 / 24104 | Sachi, Taka (Aki), Yohei, Fausto, (+1) | Master Barber |
| 686999 | Hiroki | Junior Barber |

The plan's example stylist "Shin" does **not** appear in the current roster — use the
ids above, not hardcoded names from the plan.

---

## Deep links (for the "book this slot" URL)

- All in-funnel URLs are `obg`-session-bound (`/Booking/DateSelection?obg=<guid>`) and
  are **not shareable** — they die with the session.
- The only stable public link is the funnel start:
  **`https://bookings.gettimely.com/kidanyc/bb/book`**.
- **No per-slot deep link was found.** Whether Timely supports a service-preselect
  param (e.g. `?serviceId=` / `?bookableItemId=`) on the wrapper URL is **untested** —
  worth a quick check in Phase 2. Until then, event links point at the generic booking
  start and the description must say so (don't imply one-click booking).

---

## Request-volume estimate (per sync run)

Per **service** in scope: 1 bootstrap + 1 POST service + 1 POST staff, then per
**staff** you care about: 1 `GetOpenDates` per month (×2 for a 28-day window) + 1
`gettimeslots` per open day (~13 open days/month observed for one stylist).

Example — haircut-only feed = 3 cut services (salon / master / junior) × their staff
(3 + 5 + 1 = 9 staff-service combos), ~2 months, ~13 open days each:

- ≈ 3 × (3 funnel POSTs) + 9 × (2 GetOpenDates) + 9 × (~26 gettimeslots)
- ≈ **~250 requests/run**. Hourly ⇒ ~6k/day. Comfortably under Apps Script's
  20k UrlFetch/day and trivial for GitHub Actions.

Optimizations: cache `GetOpenDates`, only fetch `gettimeslots` for days flagged
available, and dedupe overlapping services by `(staff, start)` as the plan specifies.

---

## Route recommendation

Phase 0's job is to pick A/B/C. The deciding facts:

1. **No browser is required** — so the costly "private repo + Playwright" trap is
   irrelevant, and Route C's headless-browser justification disappears.
2. But this is **not a clean JSON API**. It's a stateful cookie+`obg` funnel with
   two POST steps, an HTML-partial response that needs base64+regex decoding, and a
   Cloudflare `__cf_bm` cookie to carry.

### → Recommended: **Route B — public GitHub repo + GitHub Actions, pure-HTTP fetcher (no Playwright)**

- Unlimited free Actions minutes on public repos ⇒ guaranteed **$0/mo**.
- The funnel is easy and debuggable in Python/`requests` (or Node) with a cookie jar —
  `recon/capture.py` is already 90% of the fetcher.
- OAuth token + calendar id live in **repo secrets**; keep the Free plan's $0 spend
  limit so nothing can ever bill.
- Mitigate the 60-day scheduled-workflow auto-disable with a keepalive commit (e.g.
  commit the regenerated `.ics`).

### Route A (Apps Script) — possible, but risk-flagged

Attractive (no secrets, no OAuth, native `CalendarApp`, truly serverless). Feasible
because no browser is needed. **But** `UrlFetchApp` has no automatic cookie jar — you
must read `Set-Cookie` off each response and re-send it by hand across the multi-step
funnel, including the Cloudflare `__cf_bm` cookie, and parse HTML partials with regex
in GAS. Doable, more fragile, and much harder to debug than Route B. Reasonable if the
user strongly prefers zero-infra/zero-secrets and accepts the added fragility.

### Route C (local cron/launchd) — not recommended

$0 and private, but only syncs while the Mac is awake, which defeats a
"did-a-cancellation-just-open" calendar.

**Suggested path:** build and validate the fetcher logic locally (it already runs),
ship it as Route B. If the user later wants zero secrets, port the proven logic to
Apps Script as Route A.
