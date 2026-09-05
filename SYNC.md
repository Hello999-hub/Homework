# Syncing homework from Canvas

Assignment Desk can fill itself from your Canvas calendar feed on a schedule, and
notify you when new homework appears or something is due within 24 hours.

## How it fits together

The tracker page **cannot** call Canvas itself — published artifacts are blocked from
making network requests to outside sites. So the sync runs somewhere else:

1. A scheduled job wakes up every few hours in a fresh session.
2. It runs `canvas_sync.py`, which reads your Canvas calendar feed and compares it
   against the assignments already in the tracker's database.
3. It applies the resulting write plan to that database, so the next time you open the
   page the new assignments are there.
4. If the run turned up anything worth knowing, it sends a push notification and email.

## Setup

### 1. Get your Canvas feed URL

In Canvas: **Calendar → Calendar Feed** (bottom right of the calendar sidebar). Copy the
`https://…/feeds/calendars/user_….ics` URL.

This URL is a secret — anyone who has it can read your Canvas calendar. It is read-only:
it cannot submit work, change grades, or act as you. If it leaks, Canvas can reset it
from the same dialog, which invalidates the old one.

### 2. Add two environment variables

In **claude.ai/code → your environment → Environment variables**, not in a chat message
and not in this repository:

| Variable | Value | Notes |
| --- | --- | --- |
| `CANVAS_ICS_URL` | the feed URL from step 1 | required |
| `CANVAS_TZ` | e.g. `America/New_York` | **required for correct dates** |

`CANVAS_TZ` matters more than it looks. Canvas publishes deadlines in UTC, so an
assignment due 11:59 PM Thursday your time is `Friday 03:59Z` in the feed. Without your
timezone the sync defaults to UTC and files that assignment on the wrong day.

### 3. Let the schedule run

The scheduled job is already configured. Once the variables above exist, the next run
picks them up. Until then every run exits quietly without notifying you.

## What the sync will and won't touch

Canvas owns the **due date, time, course, and link**. You own everything else. Once you
edit an assignment by hand in the tracker, the sync stops overwriting its title and
course. It never un-checks something you marked as turned in, and it never deletes
anything — an assignment removed from Canvas stays on your list until you delete it.

Because the calendar feed carries due dates but not submission status, the sync cannot
check things off for you. That needs a Canvas API token instead of the feed — a bigger
credential, and a change worth making deliberately.

## Running it by hand

```sh
# against your real feed (needs CANVAS_ICS_URL in the environment)
python3 canvas_sync.py plan --existing-dir /tmp/hwsync/existing --tz America/New_York

# against the test fixture, no credentials needed
python3 canvas_sync.py plan --ics-file tests/sample_canvas.ics --tz America/New_York
```

The `plan` command only reads. It prints the documents it *would* write; something else
applies them, so you can always look before anything changes.
