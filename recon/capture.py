#!/usr/bin/env python3
"""
recon/capture.py  --  Phase 0 recon / re-derivation tool for KIDA NYC on Timely.

This is a THROWAWAY discovery script, kept so the API map in docs/timely-api.md
can be re-derived if Timely changes. It is also the working proof-of-concept for
the real fetcher: it drives the Timely booking funnel end-to-end from a plain HTTP
client with a cookie jar and prints real open slots.

KEY FINDING: no headless browser is required. The React wrapper at
bookings.gettimely.com is just an iframe around a classic server-rendered
ASP.NET flow on book.gettimely.com. A cookie jar + a per-session "obg" GUID is
all that is needed. Stdlib only (urllib) -- no pip installs.

Run:
    python3 recon/capture.py
    python3 recon/capture.py --catalog     # dump service/staff catalog only
    python3 recon/capture.py --service 5319865 --staff 175308 --month 7 --year 2026
"""
import argparse
import base64
import html as htmllib
import http.cookiejar
import json
import re
import sys
import urllib.parse
import urllib.request

BASE = "https://book.gettimely.com"
EMBED = BASE + "/kidanyc/book/embed?client-login=true"
LOCATION_ID = 10796          # KIDANYC, 369 Broome Street, New York
TZ_ID = 80                   # Timely tz id observed for America/New_York
UA = ("Mozilla/5.0 (Macintosh; Intel Mac OS X 10_15_7) "
      "kida-cal-recon/0.1 (+personal read-only availability mirror)")


def make_session():
    cj = http.cookiejar.CookieJar()
    return urllib.request.build_opener(urllib.request.HTTPCookieProcessor(cj)), cj


def get(opener, url, xhr=False):
    req = urllib.request.Request(url, headers={"User-Agent": UA})
    if xhr:
        req.add_header("X-Requested-With", "XMLHttpRequest")
    with opener.open(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def post(opener, url, data):
    body = urllib.parse.urlencode(data, doseq=True).encode()
    req = urllib.request.Request(url, data=body, headers={
        "User-Agent": UA,
        "Content-Type": "application/x-www-form-urlencoded",
    })
    with opener.open(req, timeout=30) as r:
        return r.read().decode("utf-8", "replace")


def parse_catalog(service_html):
    """Every service checkbox on the landing form carries data-name + data-staffids."""
    services = []
    for m in re.finditer(
        r'<input[^>]*id="service(\d+)"[^>]*?data-staffids="([^"]*)"[^>]*?'
        r'data-name="([^"]*)"[^>]*?value="([^"]+)"', service_html, re.S):
        sid, staffids, name, value = m.groups()
        services.append({
            "service_id": sid,
            "name": htmllib.unescape(name),
            "staff_ids": staffids.split(",") if staffids else [],
            "bookable_item_id": value,      # e.g. "5319865:SV"
        })
    # ServiceStaffIds[<id>:SV] hidden fields must be echoed back on the POST.
    service_staff_ids = dict(re.findall(
        r'name="(ServiceStaffIds\[\d+:SV\])"\s+[^>]*value="([^"]*)"', service_html))
    return services, service_staff_ids


def bootstrap(opener):
    """Cold GET the embed entry -> sets cookies, mints obg, renders service form."""
    html = get(opener, EMBED)
    m = re.search(r"/Booking/Service\?obg=([0-9a-f-]{36})", html)
    if not m:
        sys.exit("Could not find obg on landing page -- funnel shape may have changed.")
    return m.group(1), html


def select_service(opener, obg, bookable_item_id, service_staff_ids):
    form = {"LocationId": "0", "BookableTimeSlotItemIds": bookable_item_id, "commit": ""}
    form.update(service_staff_ids)
    return post(opener, f"{BASE}/Booking/Service?obg={obg}", form)


def select_staff(opener, obg, staff_id):
    return post(opener, f"{BASE}/Booking/StaffSelection?obg={obg}",
                {"SelectedStaffId": str(staff_id), "commit": ""})


def get_open_dates(opener, obg, staff_id, month, year):
    url = (f"{BASE}/Booking/GetOpenDates?obg={obg}&month={month}&year={year}"
           f"&staffId={staff_id}&tzName=&tzId={TZ_ID}")
    return json.loads(get(opener, url, xhr=True))


def get_time_slots(opener, obg, staff_id, date_iso):
    """Returns the HTML partial; decode BookingSelection tokens into slots."""
    url = (f"{BASE}/booking/gettimeslots/?obg={obg}&dateSelected={date_iso}"
           f"&staffId={staff_id}&tzName=&tzId={TZ_ID}")
    partial = get(opener, url, xhr=True)
    slots, seen = [], set()
    for val in re.findall(r'name="BookingSelection"[^>]*value="([^"]+)"', partial):
        try:
            dec = base64.b64decode(val).decode("utf-8", "replace")
        except Exception:
            continue
        # token: DATE,,<svc>:SV;<svc>;<groupId>;<staffId>;<startMin>;<endMin>;<n>
        date = dec.split(",")[0]
        parts = dec.split(";")
        start_min, end_min = int(parts[4]), int(parts[5])
        key = (date, start_min, parts[3])
        if key in seen:
            continue
        seen.add(key)
        slots.append({
            "date": date,
            "start": f"{start_min // 60:02d}:{start_min % 60:02d}",
            "end": f"{end_min // 60:02d}:{end_min % 60:02d}",
            "duration_min": end_min - start_min,
            "staff_id": parts[3],
            "token": val,          # opaque base64 the funnel would POST to book
        })
    return slots


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--catalog", action="store_true", help="dump service/staff catalog and exit")
    ap.add_argument("--service", default="5319865", help="service_id (default: Hair Salon Hair Cut)")
    ap.add_argument("--staff", default="175308", help="staff_id to query (default: Nao); 0 = any")
    ap.add_argument("--month", type=int, default=7)
    ap.add_argument("--year", type=int, default=2026)
    ap.add_argument("--days", type=int, default=4, help="how many open days to expand")
    args = ap.parse_args()

    opener, cj = make_session()
    obg, service_html = bootstrap(opener)
    services, service_staff_ids = parse_catalog(service_html)

    print(f"# obg={obg}  cookies={[c.name for c in cj]}")
    print(f"# {len(services)} services discovered\n")

    if args.catalog:
        for s in services:
            print(f'{s["service_id"]:>8}  {s["bookable_item_id"]:>12}  '
                  f'staff=[{",".join(s["staff_ids"])}]  {s["name"]}')
        return

    svc = next((s for s in services if s["service_id"] == args.service), None)
    if not svc:
        sys.exit(f"service {args.service} not found; run --catalog to list ids")

    select_service(opener, obg, svc["bookable_item_id"], service_staff_ids)
    # POST any valid staff once to advance to DateSelection; staffId is then a GET param.
    select_staff(opener, obg, svc["staff_ids"][0])

    print(f"# service {svc['service_id']} ({svc['name']})  querying staffId={args.staff}\n")
    od = get_open_dates(opener, obg, args.staff, args.month, args.year)
    open_days = [d["day"] for d in od["openDates"]]
    print(f"firstOpenDate={od.get('firstOpenDate')}  openDays={open_days}\n")

    for day in open_days[:args.days]:
        slots = get_time_slots(opener, obg, args.staff, day)
        print(f"{day}: {len(slots)} slots -> " + ", ".join(s["start"] for s in slots))


if __name__ == "__main__":
    main()
