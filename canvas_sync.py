#!/usr/bin/env python3
"""Turn a Canvas calendar feed into a write plan for the Assignment Desk artifact.

Reads the Canvas ICS feed, compares it against the assignments already in the
artifact's database, and emits a plan describing exactly which documents to
write. Nothing here talks to the artifact directly -- the caller applies the
plan -- so this stays deterministic and testable.

  canvas_sync.py plan --existing-dir DIR [--ics-file F] [--tz ZONE] [--out F]

The feed URL comes from $CANVAS_ICS_URL. It is a secret (anyone with it can
read your calendar), so it is never printed, logged, or written to the plan.
"""

import argparse
import hashlib
import json
import os
import re
import subprocess
import sys
from datetime import datetime, timedelta, timezone

try:
    from zoneinfo import ZoneInfo
except ImportError:                                    # pragma: no cover
    ZoneInfo = None

COLLECTION = "assignments"
STATE_COLLECTION = "sync"
STATE_DOC = "state"
DUE_SOON_HOURS = 24
RENOTIFY_AFTER_HOURS = 20      # don't repeat a due-soon reminder inside this window
FORGET_AFTER_DAYS = 30


# --------------------------------------------------------------------- fetch

def fetch_ics(url):
    """Fetch the feed. curl is already configured for this environment's proxy
    and CA bundle, so prefer it and fall back to urllib."""
    try:
        r = subprocess.run(
            ["curl", "-fsSL", "--max-time", "45", url],
            capture_output=True, text=True,
        )
        if r.returncode == 0 and r.stdout.strip():
            return r.stdout
        detail = (r.stderr or "").strip().splitlines()
        detail = detail[-1] if detail else "curl exit %d" % r.returncode
    except FileNotFoundError:
        detail = "curl not installed"

    import urllib.request
    try:
        with urllib.request.urlopen(url, timeout=45) as resp:
            return resp.read().decode("utf-8", "replace")
    except Exception as exc:                            # noqa: BLE001
        raise SystemExit(
            "Could not read the Canvas feed (%s; then %s).\n"
            "Check that CANVAS_ICS_URL is the full feed URL from "
            "Canvas > Calendar > Calendar Feed, and that it has not been reset."
            % (detail, exc)
        )


# --------------------------------------------------------------------- parse

def unfold(text):
    """RFC 5545 line unfolding: a line beginning with space or tab continues
    the previous one."""
    out = []
    for raw in text.replace("\r\n", "\n").replace("\r", "\n").split("\n"):
        if raw[:1] in (" ", "\t") and out:
            out[-1] += raw[1:]
        else:
            out.append(raw)
    return out


def unescape(value):
    return (value.replace("\\n", "\n").replace("\\N", "\n")
                 .replace("\\,", ",").replace("\\;", ";")
                 .replace("\\\\", "\\"))


def parse_events(text):
    """Yield each VEVENT as {PROPERTY: (params, value)}."""
    events, current = [], None
    for line in unfold(text):
        if line == "BEGIN:VEVENT":
            current = {}
            continue
        if line == "END:VEVENT":
            if current is not None:
                events.append(current)
            current = None
            continue
        if current is None or ":" not in line:
            continue
        head, value = line.split(":", 1)
        parts = head.split(";")
        name = parts[0].upper()
        params = {}
        for p in parts[1:]:
            if "=" in p:
                k, v = p.split("=", 1)
                params[k.upper()] = v
        current[name] = (params, unescape(value))
    return events


def parse_dt(params, value, tz):
    """Return (date 'YYYY-MM-DD', time 'HH:MM' or '') in the student's zone."""
    value = value.strip()
    if params.get("VALUE") == "DATE" or re.fullmatch(r"\d{8}", value):
        return value[:4] + "-" + value[4:6] + "-" + value[6:8], ""
    m = re.fullmatch(r"(\d{8})T(\d{6})(Z?)", value)
    if not m:
        return "", ""
    day, clock, zulu = m.groups()
    dt = datetime(int(day[:4]), int(day[4:6]), int(day[6:8]),
                  int(clock[:2]), int(clock[2:4]), int(clock[4:6]))
    if zulu:
        dt = dt.replace(tzinfo=timezone.utc).astimezone(tz)
    elif "TZID" in params and ZoneInfo:
        try:
            dt = dt.replace(tzinfo=ZoneInfo(params["TZID"])).astimezone(tz)
        except Exception:                               # noqa: BLE001
            pass
    return dt.strftime("%Y-%m-%d"), dt.strftime("%H:%M")


COURSE_RE = re.compile(r"^(.*?)\s*\[([^\]]+)\]\s*$")


def split_summary(summary):
    """Canvas writes 'Assignment title [Course Name]'."""
    m = COURSE_RE.match(summary.strip())
    if m:
        return m.group(1).strip(), m.group(2).strip()
    return summary.strip(), ""


def canvas_id(event):
    """A stable id for the assignment, used as the document id so repeated
    syncs update one document instead of piling up duplicates."""
    uid = event.get("UID", ({}, ""))[1].strip()
    m = re.search(r"assignment[-_](\d+)", uid, re.I)
    if m:
        return "canvas-a" + m.group(1)
    if uid:
        return "canvas-" + hashlib.sha1(uid.encode()).hexdigest()[:16]
    summary = event.get("SUMMARY", ({}, ""))[1]
    return "canvas-" + hashlib.sha1(summary.encode()).hexdigest()[:16]


def to_assignment(event, tz):
    summary = event.get("SUMMARY", ({}, ""))[1]
    if not summary.strip():
        return None
    params, value = event.get("DTSTART", ({}, ""))
    due, time_of_day = parse_dt(params, value, tz)
    if not due:                       # undated calendar noise, not homework
        return None
    title, course = split_summary(summary)
    desc = re.sub(r"<[^>]+>", " ", event.get("DESCRIPTION", ({}, ""))[1])
    desc = re.sub(r"\s+", " ", desc).strip()
    return {
        "id": canvas_id(event),
        "title": title,
        "course": course,
        "due": due,
        "time": time_of_day,
        "notes": desc[:280],
        "link": event.get("URL", ({}, ""))[1].strip(),
        "source": "canvas",
    }


def is_assignment(event):
    """Canvas puts assignments and calendar events in one feed; assignment
    UIDs carry 'assignment'. Keep anything that looks like coursework and
    drop plain calendar entries."""
    uid = event.get("UID", ({}, ""))[1]
    return "assignment" in uid.lower() or "syllabus" in uid.lower()


# ---------------------------------------------------------------- merge/plan

def load_existing(path):
    """Read the documents dumped by the artifact's read_db --out_dir."""
    docs = {}
    if not path:
        return docs
    root = os.path.join(path, COLLECTION)
    if not os.path.isdir(root):
        root = path
    if not os.path.isdir(root):
        return docs
    for name in sorted(os.listdir(root)):
        if not name.endswith(".json"):
            continue
        try:
            with open(os.path.join(root, name), encoding="utf-8") as fh:
                body = json.load(fh)
        except (OSError, ValueError):
            continue
        if isinstance(body, dict):
            body.setdefault("id", name[:-5])
            docs[name[:-5]] = body
    return docs


def merge(incoming, existing):
    """Canvas owns the due date, the course and the link. You own everything
    else -- whether it's turned in, your priority, your estimate, and any
    notes you have written over the Canvas description."""
    merged = dict(existing)
    merged["id"] = incoming["id"]
    merged["due"] = incoming["due"]
    merged["time"] = incoming["time"]
    merged["link"] = incoming["link"]
    merged["source"] = "canvas"
    if not existing.get("edited"):
        merged["title"] = incoming["title"]
        merged["course"] = incoming["course"] or existing.get("course", "")
    if not (existing.get("notes") or "").strip():
        merged["notes"] = incoming["notes"]
    merged.setdefault("prio", "normal")
    merged.setdefault("est", None)
    merged.setdefault("done", False)
    merged.setdefault("doneAt", None)
    merged.setdefault("createdAt", existing.get("createdAt") or 0)
    return merged


def changed(a, b):
    keys = ("title", "course", "due", "time", "link", "notes", "source")
    return any((a.get(k) or "") != (b.get(k) or "") for k in keys)


def load_state(path):
    """Which due-soon reminders have already gone out, so an hourly job
    doesn't send the same one over and over."""
    for candidate in (os.path.join(path or "", STATE_COLLECTION, STATE_DOC + ".json"),
                      os.path.join(path or "", STATE_DOC + ".json")):
        if path and os.path.isfile(candidate):
            try:
                with open(candidate, encoding="utf-8") as fh:
                    body = json.load(fh)
                if isinstance(body, dict) and isinstance(body.get("notified"), dict):
                    return body
            except (OSError, ValueError):
                pass
    return {"notified": {}}


def pick_reminders(due_soon, state, now):
    """Filter due-soon items down to the ones worth waking someone for, and
    return the refreshed notification state."""
    notified = dict(state.get("notified") or {})
    fresh, cutoff = [], now - timedelta(hours=RENOTIFY_AFTER_HOURS)
    forget = now - timedelta(days=FORGET_AFTER_DAYS)

    for item in due_soon:
        last = notified.get(item["id"])
        last_dt = None
        if last:
            try:
                last_dt = datetime.fromisoformat(last)
            except ValueError:
                last_dt = None
        if last_dt is None or last_dt < cutoff:
            fresh.append(item)
            notified[item["id"]] = now.isoformat(timespec="seconds")

    for key in list(notified):
        try:
            if datetime.fromisoformat(notified[key]) < forget:
                del notified[key]
        except ValueError:
            del notified[key]
    return fresh, notified


def build_plan(events, existing, tz, now, state=None):
    seen, writes, new, updated, due_soon = set(), [], [], [], []
    horizon = now + timedelta(hours=DUE_SOON_HOURS)

    for event in events:
        if not is_assignment(event):
            continue
        item = to_assignment(event, tz)
        if not item or item["id"] in seen:
            continue
        seen.add(item["id"])

        prior = existing.get(item["id"])
        if prior is None:
            body = merge(item, {"createdAt": int(now.timestamp() * 1000)})
            writes.append({"op": "set", "collection": COLLECTION,
                           "doc_id": item["id"], "data": strip_id(body)})
            new.append(body)
            record = body
        else:
            body = merge(item, prior)
            if changed(body, prior):
                writes.append({"op": "set", "collection": COLLECTION,
                               "doc_id": item["id"], "data": strip_id(body)})
                if (prior.get("due"), prior.get("time")) != (body["due"], body["time"]):
                    updated.append(body)
            record = body

        if not record.get("done"):
            when = due_datetime(record, tz)
            if when and now <= when <= horizon:
                due_soon.append(record)

    for item in existing.values():
        if item.get("source") == "canvas" and item["id"] not in seen and not item.get("done"):
            when = due_datetime(item, tz)
            if when and now <= when <= horizon:
                due_soon.append(item)

    due_soon.sort(key=lambda i: (i.get("due", ""), i.get("time") or "23:59"))
    reminders, notified = pick_reminders(due_soon, state or {"notified": {}}, now)
    writes.append({"op": "set", "collection": STATE_COLLECTION, "doc_id": STATE_DOC,
                   "data": {"notified": notified, "lastRun": now.isoformat(timespec="seconds")}})
    return {
        "writes": writes,
        "new": [brief(i) for i in new],
        "updated": [brief(i) for i in updated],
        "due_soon": [brief(i) for i in due_soon],
        "remind": [brief(i) for i in reminders],
        "checked_at": now.isoformat(timespec="seconds"),
    }


def strip_id(body):
    return {k: v for k, v in body.items() if k != "id"}


def due_datetime(item, tz):
    if not item.get("due"):
        return None
    try:
        y, m, d = (int(x) for x in item["due"].split("-"))
        hh, mm = (23, 59)
        if item.get("time"):
            hh, mm = (int(x) for x in item["time"].split(":"))
        return datetime(y, m, d, hh, mm, tzinfo=tz)
    except (ValueError, TypeError):
        return None


def brief(item):
    return {k: item.get(k) for k in ("id", "title", "course", "due", "time", "link")}


def summarize(plan):
    lines = []
    for label, key in (("New", "new"), ("Due within 24h", "remind"), ("Due date changed", "updated")):
        rows = plan[key]
        if not rows:
            continue
        lines.append("%s (%d):" % (label, len(rows)))
        for r in rows:
            when = r["due"] + (" " + r["time"] if r["time"] else "")
            course = (" [%s]" % r["course"]) if r["course"] else ""
            lines.append("  - %s%s - due %s" % (r["title"], course, when))
    if not lines:
        return "NOTHING-TO-REPORT"
    return "\n".join(lines)


# ----------------------------------------------------------------------- cli

def main(argv=None):
    ap = argparse.ArgumentParser(description=__doc__,
                                 formatter_class=argparse.RawDescriptionHelpFormatter)
    sub = ap.add_subparsers(dest="cmd", required=True)
    p = sub.add_parser("plan", help="build a write plan from the Canvas feed")
    p.add_argument("--existing-dir", help="directory of documents dumped by read_db --out_dir")
    p.add_argument("--ics-file", help="read a saved feed instead of fetching (for testing)")
    p.add_argument("--tz", default=os.environ.get("CANVAS_TZ", "UTC"),
                   help="your timezone, e.g. America/New_York (default: $CANVAS_TZ or UTC)")
    p.add_argument("--now", help="ISO timestamp to treat as now (for testing)")
    p.add_argument("--out", help="write the plan JSON here instead of stdout")
    args = ap.parse_args(argv)

    tz = timezone.utc
    if ZoneInfo and args.tz and args.tz.upper() != "UTC":
        try:
            tz = ZoneInfo(args.tz)
        except Exception:                               # noqa: BLE001
            print("Unknown timezone %r; using UTC. Due times may be off by "
                  "your UTC offset." % args.tz, file=sys.stderr)

    if args.ics_file:
        with open(args.ics_file, encoding="utf-8") as fh:
            text = fh.read()
    else:
        url = os.environ.get("CANVAS_ICS_URL", "").strip()
        if not url:
            raise SystemExit(
                "CANVAS_ICS_URL is not set. Add your Canvas calendar feed URL "
                "as an environment variable (Canvas > Calendar > Calendar Feed)."
            )
        text = fetch_ics(url)

    now = datetime.fromisoformat(args.now).replace(tzinfo=tz) if args.now else datetime.now(tz)
    plan = build_plan(parse_events(text), load_existing(args.existing_dir), tz, now,
                      load_state(args.existing_dir))
    plan["summary"] = summarize(plan)

    payload = json.dumps(plan, indent=2)
    if args.out:
        with open(args.out, "w", encoding="utf-8") as fh:
            fh.write(payload + "\n")
        print(plan["summary"])
        print("\nPlan written to %s (%d document write(s))." % (args.out, len(plan["writes"])))
    else:
        print(payload)
    return 0


if __name__ == "__main__":
    sys.exit(main())
