#!/usr/bin/env python3
"""
Watch a Calendly event for ANY open slot on ANY date in an upcoming window, and push
to your phone (ntfy.sh) when new slots appear. Companion to check_calendly.py, which
watches specific dates; this one reuses its Calendly + ntfy helpers.

Environment variables (same as check_calendly.py, plus):

  CALENDLY_URL   required  e.g. https://calendly.com/sb-marriages/sb-license-ceremony-eng
  NTFY_TOPIC     required
  DAYS_AHEAD     optional  How far ahead to look, default 120 (county books ~90 days out)
  EARLIEST       optional  Ignore dates before this YYYY-MM-DD
  LATEST         optional  Ignore dates after this YYYY-MM-DD
  ANY_STATE_DIR  optional  Default .state/any
  TEST_NOTIFY    optional  "true" sends a status push even if nothing new is open
"""
import json
import os
import sys
import urllib.parse
from datetime import date, datetime, timedelta

# Keep this checker's memory separate from the specific-date checker's.
STATE_DIR = (os.environ.get("ANY_STATE_DIR") or ".state/any").rstrip("/")
os.environ["STATE_FILE"] = f"{STATE_DIR}/seen.json"

import check_calendly as cc  # noqa: E402  (shared HTTP retries, ntfy, Calendly lookup)

DAYS_AHEAD = int(cc.env("DAYS_AHEAD", "120"))
EARLIEST = cc.env("EARLIEST")
LATEST = cc.env("LATEST")
FAIL_FILE = f"{STATE_DIR}/failures.txt"
FAIL_LIMIT = 3


def month_chunks(start, end):
    """Yield (first, last) date pairs no longer than a calendar month (Calendly's limit)."""
    cur = start
    while cur <= end:
        nxt = (cur.replace(day=1) + timedelta(days=32)).replace(day=1)
        yield cur, min(end, nxt - timedelta(days=1))
        cur = nxt


def open_slots_between(uuid, start, end):
    found = []
    for a, b in month_chunks(start, end):
        q = urllib.parse.urlencode({
            "timezone": cc.TZ_NAME, "diagnostics": "false",
            "range_start": a.isoformat(), "range_end": b.isoformat(),
        })
        data = cc.http_get(f"{cc.BASE}/event_types/{uuid}/calendar/range?{q}")
        if "days" not in data:
            raise RuntimeError(f"Unexpected availability response: {str(data)[:300]}")
        for d in data["days"]:
            for s in d.get("spots", []):
                if s.get("status", "available") == "available" and s.get("start_time"):
                    found.append((d["date"], s["start_time"]))
    return found


def main():
    today = datetime.now(cc.TZ).date()
    start = max(today, date.fromisoformat(EARLIEST)) if EARLIEST else today
    end = today + timedelta(days=DAYS_AHEAD)
    if LATEST:
        end = min(end, date.fromisoformat(LATEST))
    if start > end:
        print("Date window is empty; nothing to check.")
        return

    seen = cc.load_state()
    event_types = cc.resolve_event_types(cc.CALENDLY_URL)
    print(f"Checking {len(event_types)} event type(s) from {start} to {end}")

    current = {}   # key -> (date, time label)
    for uuid, name in event_types:
        for day, start_time in open_slots_between(uuid, start, end):
            label = cc.pretty_time(start_time) + (f" ({name})" if len(event_types) > 1 else "")
            current[f"{uuid}|{start_time}"] = (day, label)
    open_days = sorted({d for d, _ in current.values()})
    print(f"  {len(current)} open slot(s) on {len(open_days)} date(s)" +
          (f": {', '.join(open_days)}" if open_days else ""))

    new = {k: v for k, v in current.items() if k not in seen}
    if new:
        by_day = {}
        for day, label in sorted(new.values()):
            by_day.setdefault(day, []).append(label)
        lines = [f"{cc.pretty_date(d)}: {', '.join(t[:6])}{' …' if len(t) > 6 else ''}"
                 for d, t in sorted(by_day.items())]
        n_days = len(by_day)
        cc.notify(
            f"Courthouse: {len(new)} slot{'s' if len(new) != 1 else ''} open on "
            f"{n_days} date{'s' if n_days != 1 else ''}!",
            "\n".join(lines[:12]) + ("\n…" if len(lines) > 12 else "") + "\nTap to book.",
            priority="urgent", tags="rotating_light,calendar",
        )
        print(f"Notified about {len(new)} new slot(s)")
    elif cc.TEST:
        status = (f"{len(current)} open slot(s) on {len(open_days)} date(s) right now."
                  if current else f"Nothing open between {cc.pretty_date(start.isoformat())} "
                                  f"and {cc.pretty_date(end.isoformat())}.")
        cc.notify("Any-date checker is working", f"Test run. {status}",
                  priority="default", tags="white_check_mark")
        print("Sent test notification")
    else:
        print("No new slots")

    cc.save_state(set(current))


def failures(n=None):
    if n is None:
        try:
            with open(FAIL_FILE) as f:
                return int(f.read().strip() or 0)
        except (FileNotFoundError, ValueError):
            return 0
    os.makedirs(STATE_DIR, exist_ok=True)
    with open(FAIL_FILE, "w") as f:
        f.write(str(n))
    return n


if __name__ == "__main__":
    try:
        main()
        failures(0)
    except cc.Transient as e:
        n = failures(failures() + 1)
        print(f"::warning::Calendly hiccup ({n} in a row): {e}")
        if n >= FAIL_LIMIT:
            sys.exit(f"Calendly has failed {n} runs in a row: {e}")