# Assignment Desk

A homework tracker in a single HTML file. Open `index.html` in a browser — no build,
no install, no server.

## What it does

- **Add assignments** with a course, due date, optional time, priority, an estimate in
  minutes, and notes.
- **Groups by urgency** — Overdue, Due today, Tomorrow, Later this week, Coming up,
  No due date — sorted by when things are actually due.
- **Glance strip** at the top counts overdue / due today / next 7 days / turned in.
  Click a count to filter down to it.
- **Week progress** bar: how much of what's due in the next seven days is turned in.
- **Search, filter by course, and switch** between To do / Turned in / Everything.
- **Edit in place** by clicking an assignment's title; delete with an undo toast.
- Light and dark themes, keyboard shortcuts (`/` search, `n` new assignment), and a
  layout that works on a phone.

## Where your data lives

By default everything is stored in the browser's `localStorage` — private to that
browser, and it survives closing the tab.

When the page is published as a Claude Artifact, it also declares the `db` capability:
on load it asks for the artifact's document store and, if granted, syncs assignments
there instead, so the same list shows up on any device you sign in from. The indicator
in the top right says which mode is active ("Saved in this browser" vs "Synced to your
account"). If the store isn't available the page falls back to local storage without
losing anything, and local assignments are pushed up the first time cloud storage
becomes available.

## Examples on first run

An empty desk shows three example assignments (marked `Example`, on dashed rows) so the
layout isn't a blank page. Adding your own assignment — or pressing "Clear examples" —
removes them for good; they're never written to storage.
