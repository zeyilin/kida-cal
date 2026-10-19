# KIDA NYC — Open Slots

A calendar that shows the **open** haircut and barber appointments at
[KIDA NYC](https://kidanyc.com), so you can see what's free at a glance instead of clicking
through the booking site. It updates on its own in the background.

Each stylist's free time shows up as a **Free** event like `Nao · 3 open`, and opening it
lists the exact times you can book, what each service costs, and a link to go book it.
It only shows availability — it never books, holds, or cancels anything. Slots can fill up
fast, so always confirm on KIDA's site before you go.

There's also a single all-day entry like `✓ KIDA slots checked 2:15 PM · 57 open`, sitting
on the day of the last time it managed to check. It moves forward with every check, so it
tells you how fresh everything else is. If it's more than half a day old, something is
broken and the openings are stale. If the last check ran into trouble, opening the entry
says so, and when the last clean one was.

## Viewing and sharing

The calendar is public, so you can add it to your own calendar or send the link to friends.
Open its settings in Google Calendar to grab the shareable link — there are also links for
viewing it in a browser or subscribing from Apple Calendar or Outlook.

## Good to know

- It runs in the cloud, so nothing has to stay on your computer — and it's free to run.
  It gets four chances an hour to check, but never checks more often than about once every
  50 minutes, so it doesn't pester KIDA's booking site. How often it really runs is up to
  GitHub, which lately has only started it about 4 times a day. The "checked" entry always
  tells you the truth.
- Quick checks look about three weeks ahead; every six hours or so one goes the full 90
  days, so far-out openings can appear a little later than near-term ones.
- It only reads KIDA's public availability and doesn't collect or store anything personal.

## If you're running your own copy

Two repository secrets are required: `KIDA_SERVICE_ACCOUNT_JSON` (a Google service-account
key) and `KIDA_CALENDAR_ID` (the calendar it writes to, shared with that service account
with "Make changes to events"). The sync fails loudly if either is missing.

One more is optional but strongly recommended: `KIDA_HEARTBEAT_URL`, pointing at a
dead-man's-switch service like [healthchecks.io](https://healthchecks.io). Nothing else can
tell you that a scheduled run never *started* — which is how this calendar once went stale
for three days without a single alert.

When the sync keeps failing, it opens a GitHub issue — but only once the calendar has gone
12 hours or more without a successful refresh, or when it can't tell how long that's been
(a missing secret, say, or a new setup that has never synced cleanly). A one-off failure
just leaves a warning on that run, and the issue closes itself after the next successful
sync. To retry a failed run straight away, use **Re-run all jobs** on it: a re-run always
checks, even within the 50 minutes.

### Getting a reliable hourly refresh

GitHub's scheduler is the weak link. For a run every hour, have an outside cron service
such as [cron-job.org](https://cron-job.org) start the sync for you:

- **Request:** `POST https://api.github.com/repos/<owner>/<repo>/actions/workflows/sync.yml/dispatches`
- **Headers:** `Authorization: Bearer <token>` and `Accept: application/vnd.github+json`
- **Body:** `{"ref":"main","inputs":{"depth":"auto"}}`
- **Token:** a fine-grained personal access token for this one repository, with only
  **Actions: read and write** and nothing else.

Schedule it hourly. An extra trigger never costs KIDA's site anything — a run that starts
within 50 minutes of the last one stops straight away — but more often than hourly buys
nothing either, and it gets in the way of runs you start by hand (below). GitHub answers a
good request with `204 No Content`. Tokens expire, so note the date.

Only one run happens at a time, and GitHub keeps just one more waiting behind it: any
newer run takes the waiting one's place. So a run you start by hand — `near`, `full`, or a
re-run — while another is in progress can be replaced by the next automatic one, which
then usually stops straight away. Start those when nothing is running, and if one shows up
as cancelled, start it again.
