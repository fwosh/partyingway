#!/usr/bin/env python3
"""
Post a Bluesky reminder roughly an hour before each event on a public
Google Calendar.

Reads the calendar's public .ics feed, so no Google API key or OAuth.
Runs on a cron every 30 minutes; anything starting within LEAD_MINUTES
that hasn't been announced yet gets a post. Dedupe lives in state.json.
"""

import json
import os
import re
import sys
import unicodedata
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import icalendar
import recurring_ical_events
import requests

ICS_URL = os.environ["ICS_URL"]
BSKY_HANDLE = os.environ["BSKY_HANDLE"]
BSKY_APP_PASSWORD = os.environ["BSKY_APP_PASSWORD"]

LEAD_MINUTES = int(os.environ.get("LEAD_MINUTES", "75"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

STATE_PATH = Path(__file__).parent / "state.json"
PDS = "https://bsky.social"
MAX_GRAPHEMES = 300
RETENTION_DAYS = 30


# ---------- state ----------

def load_state():
    if not STATE_PATH.exists():
        return {}
    try:
        return json.loads(STATE_PATH.read_text())
    except json.JSONDecodeError:
        return {}


def save_state(state):
    cutoff = (datetime.now(timezone.utc) - timedelta(days=RETENTION_DAYS)).isoformat()
    pruned = {k: v for k, v in state.items() if v >= cutoff}
    STATE_PATH.write_text(json.dumps(pruned, indent=2, sort_keys=True) + "\n")


# ---------- calendar ----------

def fetch_events(window_start, window_end):
    """Fetch the public ICS feed and expand recurrences into the window."""
    resp = requests.get(
        ICS_URL,
        headers={
            "Cache-Control": "no-cache",
            "User-Agent": "bsky-event-reminder/1.0",
        },
        # Cache-buster: the feed sits behind a CDN and a stale copy would
        # silently skip today's events.
        params={"_": int(datetime.now(timezone.utc).timestamp())},
        timeout=30,
    )
    resp.raise_for_status()
    if not resp.content.lstrip().startswith(b"BEGIN:VCALENDAR"):
        raise RuntimeError(
            "Feed did not return an iCalendar document. Check that the "
            "calendar is public and the URL is the /public/basic.ics form."
        )
    calendar = icalendar.Calendar.from_ical(resp.content)
    return recurring_ical_events.of(calendar).between(window_start, window_end)


def usable_start(event):
    """Return an aware datetime, or None for all-day and cancelled events."""
    if str(event.get("STATUS", "")).upper() == "CANCELLED":
        return None
    start = event["DTSTART"].dt
    if not isinstance(start, datetime) or isinstance(start, date) and not isinstance(start, datetime):
        return None  # all-day event: no meaningful "1 hour before"
    if start.tzinfo is None:
        return start.replace(tzinfo=timezone.utc)
    return start


def field(event, name):
    value = event.get(name)
    return str(value).strip() if value else ""


# ---------- text ----------

TAG_RE = re.compile(r"<[^>]+>")
ENTITIES = {
    "&nbsp;": " ", "&amp;": "&", "&lt;": "<",
    "&gt;": ">", "&quot;": '"', "&#39;": "'",
}


def strip_html(text):
    """Descriptions carry HTML when the event was edited in the rich-text UI."""
    if not text:
        return ""
    text = re.sub(r"<br\s*/?>|</p>|</div>", "\n", text, flags=re.I)
    text = TAG_RE.sub("", text)
    for entity, char in ENTITIES.items():
        text = text.replace(entity, char)
    return re.sub(r"\n{3,}", "\n\n", text).strip()


def grapheme_len(text):
    """Bluesky counts graphemes, not codepoints. Dropping combining marks
    approximates this closely enough for emoji and accented text."""
    return len([c for c in unicodedata.normalize("NFC", text)
                if not unicodedata.combining(c)])


def truncate(text, limit):
    if grapheme_len(text) <= limit:
        return text
    out = ""
    for char in text:
        if grapheme_len(out + char) > limit - 1:
            break
        out += char
    return out.rstrip() + "\u2026"


def build_post_text(event):
    parts = ["Happening today in 1 hour! \u2728"]
    description = strip_html(field(event, "DESCRIPTION"))
    location = field(event, "LOCATION")
    if description:
        parts.append(description)
    if location:
        parts.append(location)
    return truncate("\n\n".join(parts), MAX_GRAPHEMES)


def detect_links(text):
    """Without facets, URLs render as inert plain text."""
    facets = []
    for match in re.finditer(r"https?://[^\s\]\)]+", text):
        url = match.group(0).rstrip(".,;:!?")
        start = len(text[: match.start()].encode("utf-8"))
        facets.append({
            "index": {"byteStart": start, "byteEnd": start + len(url.encode("utf-8"))},
            "features": [{"$type": "app.bsky.richtext.facet#link", "uri": url}],
        })
    return facets


# ---------- bluesky ----------

def create_session():
    resp = requests.post(
        f"{PDS}/xrpc/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_APP_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def create_post(session, text):
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "langs": ["en"],
    }
    facets = detect_links(text)
    if facets:
        record["facets"] = facets

    resp = requests.post(
        f"{PDS}/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {session['accessJwt']}"},
        json={
            "repo": session["did"],
            "collection": "app.bsky.feed.post",
            "record": record,
        },
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


# ---------- main ----------

def main():
    now = datetime.now(timezone.utc)
    state = load_state()
    session = None
    posted_any = False

    events = fetch_events(now, now + timedelta(minutes=LEAD_MINUTES + 30))
    print(f"{len(events)} event(s) in the lookahead window")

    for event in events:
        start = usable_start(event)
        if start is None:
            continue

        uid = field(event, "UID")
        # Recurring instances share a UID, so the start time disambiguates.
        key = f"{uid}@{start.astimezone(timezone.utc).isoformat()}"
        if key in state:
            continue

        minutes_out = (start - now).total_seconds() / 60
        if not (0 < minutes_out <= LEAD_MINUTES):
            continue

        summary = field(event, "SUMMARY") or "(untitled)"
        text = build_post_text(event)
        print(f"[{minutes_out:.0f} min out] {summary}")

        if DRY_RUN:
            print("--- would post ---")
            print(text)
            print("------------------")
            continue

        if session is None:
            session = create_session()
        create_post(session, text)
        state[key] = now.isoformat()
        posted_any = True
        print(f"posted: {summary}")

    if posted_any:
        save_state(state)
    else:
        print("nothing to post")
    return 0


if __name__ == "__main__":
    sys.exit(main())
