# KIDA NYC — Open Slots

A calendar that shows the **open** haircut and barber appointments at
[KIDA NYC](https://kidanyc.com), so you can see what's free at a glance instead of clicking
through the booking site. It updates on its own in the background.

Each stylist's free time shows up as a **Free** event like `Nao · 3 open`, and opening it
lists the exact times you can book, what each service costs, and a link to go book it.
It only shows availability — it never books, holds, or cancels anything. Slots can fill up
fast, so always confirm on KIDA's site before you go.

There's also a `✓ KIDA slots checked …` entry at the top of each day. That's the calendar
telling you when it last managed to look. If that date isn't today, something is broken and
everything below it is stale.

## Viewing and sharing

The calendar is public, so you can add it to your own calendar or send the link to friends.
Open its settings in Google Calendar to grab the shareable link — there are also links for
viewing it in a browser or subscribing from Apple Calendar or Outlook.

## Good to know

- It aims to refresh every hour and runs in the cloud, so nothing has to stay on your
  computer — and it's free to run. In practice GitHub delivers only about half of those
  hourly attempts, so the real gap is usually 1–2 hours. The "checked" entry always tells
  you the truth.
- It looks about three weeks ahead most of the time, and does a deeper 90-day sweep every
  few hours, so far-out openings appear a little later than near-term ones.
- It only reads KIDA's public availability and doesn't collect or store anything personal.

## If you're running your own copy

Two repository secrets are required: `KIDA_SERVICE_ACCOUNT_JSON` (a Google service-account
key) and `KIDA_CALENDAR_ID` (the calendar it writes to, shared with that service account
with "Make changes to events"). The sync fails loudly if either is missing.

One more is optional but strongly recommended: `KIDA_HEARTBEAT_URL`, pointing at a
dead-man's-switch service like [healthchecks.io](https://healthchecks.io). Nothing else can
tell you that a scheduled run never *started* — which is how this calendar once went stale
for three days without a single alert.
