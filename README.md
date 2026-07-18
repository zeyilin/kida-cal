# KIDA NYC — Open Slots

A live Google Calendar of **open** haircut & barber appointment slots at
[KIDA NYC](https://kidanyc.com), refreshed every hour. Add it to your calendar to see
what's free at a glance, or share the link with friends.

Each opening shows up as a **Free** event like `OPEN · Haircut w/ Nao`, with a link to
book it on KIDA's site. It only reads public availability — it never books, holds, or
cancels anything. Availability is a snapshot, so always confirm on KIDA's page before you go.

## View & share it

The calendar is public, so you can hand the link to anyone. In the calendar's Google
settings (**Settings → Integrate calendar**):

- **Add to Google Calendar** → use **Get shareable link**.
- **View in a browser** or **subscribe in Apple / Outlook** → use the **Public URL** /
  **iCal** links there.

## How it stays updated

A scheduled job runs every hour on GitHub's servers, checks KIDA's live availability, and
updates a dedicated calendar ("KIDA NYC — Open Slots") — adding new openings and removing
ones that got booked. Nothing needs to stay running on your computer. Cost: **$0**.

## Setup (one-time)

To point this at your own Google Calendar:

1. **Google Cloud** (free, no billing): create a project, enable the **Google Calendar
   API**, add a **service account**, and download its JSON key.
2. **Google Calendar**: create a calendar named **KIDA NYC — Open Slots**, share it with
   the service account's email (*Make changes to events*), and make it public to share it.
3. **This repo → Settings → Secrets → Actions**: add `KIDA_SERVICE_ACCOUNT_JSON` (the key
   file's contents) and `KIDA_CALENDAR_ID` (from the calendar's settings).

The hourly sync then turns on by itself.

## Good to know

- **Private-safe.** No bookings, no personal data. Your key stays an encrypted GitHub
  secret — never in the code. The repo is public only so the hourly automation stays free.
- **Tuning.** Which stylists and services to include, and how far ahead to look, live in
  `config.yaml`.
- **Details.** The technical write-up and tests are in `docs/`.
