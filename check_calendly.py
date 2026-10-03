#!/usr/bin/env python3
"""
Check a public Calendly booking link for open slots on specific dates and
send a phone push via ntfy (https://ntfy.sh) when new slots appear.

Uses only the Python standard library. Configured with environment variables:

  CALENDLY_URL   required  The Calendly link, e.g. https://calendly.com/some-org/ceremony
                           (a profile link like https://calendly.com/some-org checks every
                           event type on it)
  NTFY_TOPIC     required  Your private ntfy topic name (pick something unguessable)
  TARGET_DATES   optional  Comma-separated YYYY-MM-DD dates (default 2026-12-16)
  TIMEZONE       optional  Default America/Los_Angeles
  NTFY_SERVER    optional  Default https://ntfy.sh
  STATE_FILE     optional  Default .state/seen.json (remembers slots already announced)
  TEST_NOTIFY    optional  "true" sends a status push even if nothing is open

Note: this uses the same JSON endpoints Calendly's own booking page calls. They are
not an official API, so if Calendly changes them the run will fail loudly (and the
workflow sends you a "checker failed" push) rather than silently missing slots.
"""
import http.client
import json
import os
import re
import sys
import time
import urllib.error
import urllib.parse
import urllib.request
from datetime import date, datetime
from zoneinfo import ZoneInfo

BASE = "https://calendly.com/api/booking"
UA = "Mozilla/5.0 (X11; Linux x86_64) personal-availability-checker/1.0"


def env(name, default=None, required=False):
    val = os.environ.get(name, "")
    val = val.strip() if val else ""
    if not val:
        if required:
            sys.exit(f"Missing required environment variable {name}")
        return default
    return val


CALENDLY_URL = env("CALENDLY_URL", required=True)
NTFY_TOPIC = env("NTFY_TOPIC", required=True)
TARGET_DATES = [d.strip() for d in env("TARGET_DATES", "2026-12-16").split(",") if d.strip()]
TZ_NAME = env("TIMEZONE", "America/Los_Angeles")
NTFY_SERVER = env("NTFY_SERVER", "https://ntfy.sh").rstrip("/")
STATE_FILE = env("STATE_FILE", ".state/seen.json")
TEST = env("TEST_NOTIFY", "false").lower() in ("1", "true", "yes")
TZ = ZoneInfo(TZ_NAME)


class Transient(Exception):
    """A temporary hiccup (rate limit, server error, timeout) worth retrying later."""


def http_get(url, want_json=True, attempts=4):
    headers = {"User-Agent": UA}
    if want_json:
        headers["Accept"] = "application/json"
    last = None
    for i in range(attempts):
        if i:
            time.sleep(5 * 2 ** (i - 1))  # 5s, 10s, 20s
        req = urllib.request.Request(url, headers=headers)
        try:
            with urllib.request.urlopen(req, timeout=30) as r:
                body = r.read().decode("utf-8", errors="replace")
            return json.loads(body) if want_json else body
        except urllib.error.HTTPError as e:
            if e.code in (408, 425, 429) or e.code >= 500:
                last = f"HTTP {e.code}"
                print(f"  attempt {i + 1}: GET {url} -> {last}, retrying")
                continue
            raise RuntimeError(f"GET {url} -> HTTP {e.code}") from e
        except (urllib.error.URLError, TimeoutError, ConnectionError, http.client.HTTPException, json.JSONDecodeError) as e:
            last = f"{type(e).__name__}: {e}"
            print(f"  attempt {i + 1}: GET {url} -> {last}, retrying")
    raise Transient(f"GET {url} kept failing ({last})")


def as_list(obj):
    """Calendly responses are sometimes a bare list, sometimes wrapped."""
    if isinstance(obj, list):
        return obj
    if isinstance(obj, dict):
        for key in ("collection", "data", "event_types", "results"):
            if isinstance(obj.get(key), list):
                return obj[key]
        return [obj]
    return []


def resolve_event_types(url):
    """Return a list of (uuid, name) for the event type(s) behind a Calendly link."""
    parsed = urllib.parse.urlparse(url)
    parts = [p for p in parsed.path.split("/") if p]
    if not parts:
        raise RuntimeError(f"Can't read a Calendly path from {url}")

    # https://calendly.com/<profile>/<event>
    if len(parts) >= 2 and parts[0] != "d":
        q = urllib.parse.urlencode({"event_type_slug": parts[1], "profile_slug": parts[0]})
        data = http_get(f"{BASE}/event_types/lookup?{q}")
        item = as_list(data)[0]
        return [(item["uuid"], item.get("name") or parts[1])]

    # https://calendly.com/<profile>  -> check every active event type on the page
    if len(parts) == 1:
        data = http_get(f"{BASE}/profiles/{parts[0]}/event_types")
        types = [
            (t["uuid"], t.get("name") or t.get("slug") or t["uuid"])
            for t in as_list(data)
            if t.get("uuid") and t.get("active", True) is not False
        ]
        if types:
            return types
        raise RuntimeError(f"No event types found on profile {parts[0]}")

    # Other link shapes (e.g. /d/abc-def-ghi): read the page and pull the uuid out of it
    html = http_get(url, want_json=False)
    m = re.search(r'"event_type"\s*:\s*\{[^{}]*?"uuid"\s*:\s*"([0-9a-fA-F-]{16,})"', html) or \
        re.search(r'event_types/([0-9a-fA-F-]{16,})', html)
    if not m:
        raise RuntimeError(f"Couldn't find an event type id in {url}")
    return [(m.group(1), "event")]


def open_slots(uuid, day):
    q = urllib.parse.urlencode({
        "timezone": TZ_NAME,
        "diagnostics": "false",
        "range_start": day,
        "range_end": day,
    })
    data = http_get(f"{BASE}/event_types/{uuid}/calendar/range?{q}")
    if "days" not in data:
        raise RuntimeError(f"Unexpected availability response for {uuid}: {str(data)[:300]}")
    spots = []
    for d in data["days"]:
        if d.get("date") != day:
            continue
        for s in d.get("spots", []):
            if s.get("status", "available") == "available" and s.get("start_time"):
                spots.append(s["start_time"])
    return sorted(spots)


def pretty_time(iso):
    try:
        return datetime.fromisoformat(iso.replace("Z", "+00:00")).astimezone(TZ).strftime("%-I:%M %p")
    except ValueError:
        return iso


def pretty_date(day):
    return date.fromisoformat(day).strftime("%a %b %-d")


def notify(title, message, priority="high", tags="bell"):
    req = urllib.request.Request(
        f"{NTFY_SERVER}/{urllib.parse.quote(NTFY_TOPIC)}",
        data=message.encode("utf-8"),
        method="POST",
        headers={
            "Title": title,
            "Priority": priority,
            "Tags": tags,
            "Click": CALENDLY_URL,
        },
    )
    with urllib.request.urlopen(req, timeout=30) as r:
        r.read()


def load_state():
    try:
        with open(STATE_FILE) as f:
            return set(json.load(f))
    except (FileNotFoundError, ValueError):
        return set()


def save_state(seen):
    os.makedirs(os.path.dirname(STATE_FILE) or ".", exist_ok=True)
    with open(STATE_FILE, "w") as f:
        json.dump(sorted(seen), f)


def main():
    today = datetime.now(TZ).date()
    dates = [d for d in TARGET_DATES if date.fromisoformat(d) >= today]
    seen = load_state()
    if not dates:
        print("All target dates have passed; nothing to check. You can disable the workflow.")
        save_state(seen)
        return

    event_types = resolve_event_types(CALENDLY_URL)
    print(f"Checking {len(event_types)} event type(s) for {', '.join(dates)}")

    current = {}  # key -> human line
    for uuid, name in event_types:
        for day in dates:
            for start in open_slots(uuid, day):
                key = f"{uuid}|{start}"
                label = f"{pretty_date(day)} {pretty_time(start)}"
                if len(event_types) > 1:
                    label += f" ({name})"
                current[key] = label
            print(f"  {name} on {day}: {sum(1 for k in current if k.startswith(uuid) and day in k)} open")

    new = [current[k] for k in current if k not in seen]

    if new:
        lines = "\n".join(new[:15]) + ("\n..." if len(new) > 15 else "")
        notify(
            f"Courthouse opening: {len(new)} new slot{'s' if len(new) != 1 else ''}!",
            f"{lines}\nTap to book before someone else does.",
            priority="urgent",
            tags="rotating_light,wedding",
        )
        print(f"Notified about {len(new)} new slot(s)")
    elif TEST:
        status = f"{len(current)} open slot(s) right now." if current else "No open slots right now."
        notify("Courthouse checker is working", f"Test run. {status}", priority="default", tags="white_check_mark")
        print("Sent test notification")
    else:
        print("No new slots")

    # Remember only slots that are still open, so a slot that closes and re-opens alerts again.
    save_state(set(current))


FAIL_FILE = os.path.join(os.path.dirname(STATE_FILE) or ".", "failures.txt")
FAIL_LIMIT = 3  # only report a breakage after this many hiccup runs in a row


def failure_count(n=None):
    if n is None:
        try:
            with open(FAIL_FILE) as f:
                return int(f.read().strip() or 0)
        except (FileNotFoundError, ValueError):
            return 0
    os.makedirs(os.path.dirname(FAIL_FILE) or ".", exist_ok=True)
    with open(FAIL_FILE, "w") as f:
        f.write(str(n))
    return n


if __name__ == "__main__":
    try:
        main()
        failure_count(0)
    except Transient as e:
        n = failure_count(failure_count() + 1)
        print(f"::warning::Calendly hiccup ({n} in a row): {e}")
        if n >= FAIL_LIMIT:
            sys.exit(f"Calendly has failed {n} runs in a row: {e}")
        # A one-off blip: stay green; the next run will check again.