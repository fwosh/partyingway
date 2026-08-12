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
from datetime import date, datetime, timedelta, timezone
from pathlib import Path

import icalendar
import recurring_ical_events
import regex
import requests

ICS_URL = os.environ["ICS_URL"]
BSKY_HANDLE = os.environ["BSKY_HANDLE"]
BSKY_APP_PASSWORD = os.environ["BSKY_APP_PASSWORD"]

LEAD_MINUTES = int(os.environ.get("LEAD_MINUTES", "75"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

STATE_PATH = Path(__file__).parent / "state.json"
PDS = "https://bsky.social"
APPVIEW = "https://public.api.bsky.app"
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
    r"""Bluesky's 300 limit counts grapheme clusters, so an emoji with a
    variation selector is 1, not 2. \X matches a full cluster."""
    return len(regex.findall(r"\X", text))


def truncate(text, limit):
    if grapheme_len(text) <= limit:
        return text
    clusters = regex.findall(r"\X", text)
    return "".join(clusters[: limit - 1]).rstrip() + "\u2026"


def build_post_text(event, quoted=False):
    """The post format lives here. An empty field is skipped rather than
    leaving a bare label behind. When `quoted` is true the Location URL is
    being shown as a quote-post embed, so it's left out of the text."""
    details = []

    location = field(event, "LOCATION")
    if location and not quoted:
        details.append(f"Post: {location}")

    description = strip_html(field(event, "DESCRIPTION"))
    if description:
        details.append(description)

    text = "Happening today within 1 hour! \u2728"
    if details:
        text += "\n\n" + "\n".join(details)
    return truncate(text, MAX_GRAPHEMES)


MD_LINK_RE = regex.compile(r"\[([^\]]+)\]\((https?://[^\s)]+)\)")
BARE_URL_RE = regex.compile(r"https?://[^\s\]\)]+")
HASHTAG_RE = regex.compile(r"(?<![\w/])#(\w{1,64})")


def resolve_markdown_links(text):
    """Turn [label](url) into `label` plus a link span, so the post shows a
    clean label instead of a raw URL. Returns (text, [(start, end, url)])."""
    out, spans, cursor = [], [], 0
    for match in MD_LINK_RE.finditer(text):
        out.append(text[cursor:match.start()])
        label_start = sum(len(piece) for piece in out)
        out.append(match.group(1))
        spans.append((label_start, label_start + len(match.group(1)), match.group(2)))
        cursor = match.end()
    out.append(text[cursor:])
    return "".join(out), spans


def build_facets(text, link_spans):
    """Byte offsets, not character offsets - the AT Protocol counts UTF-8."""
    def byte_offset(char_index):
        return len(text[:char_index].encode("utf-8"))

    facets, claimed = [], []

    def add(start, end, feature):
        facets.append({
            "index": {"byteStart": byte_offset(start), "byteEnd": byte_offset(end)},
            "features": [feature],
        })
        claimed.append((start, end))

    for start, end, url in link_spans:
        add(start, end, {"$type": "app.bsky.richtext.facet#link", "uri": url})

    for match in BARE_URL_RE.finditer(text):
        if any(s <= match.start() < e for s, e in claimed):
            continue
        url = match.group(0).rstrip(".,;:!?")
        add(match.start(), match.start() + len(url),
            {"$type": "app.bsky.richtext.facet#link", "uri": url})

    for match in HASHTAG_RE.finditer(text):
        if any(s <= match.start() < e for s, e in claimed):
            continue  # don't tag a fragment inside a URL
        add(match.start(), match.end(),
            {"$type": "app.bsky.richtext.facet#tag", "tag": match.group(1)})

    facets.sort(key=lambda f: f["index"]["byteStart"])
    return facets


# ---------- quote embeds ----------

BSKY_POST_URL_RE = regex.compile(
    r"https?://(?:www\.)?(?:bsky\.app|staging\.bsky\.app)"
    r"/profile/([^/\s]+)/post/([A-Za-z0-9._~-]+)"
)


def resolve_quote(url):
    """Turn a bsky.app post URL into the {uri, cid} pair an embed needs.

    Returns None if the URL isn't a Bluesky post or the post can't be
    fetched (deleted, blocked, private) - the caller then falls back to
    showing the plain link.
    """
    match = BSKY_POST_URL_RE.search(url or "")
    if not match:
        return None
    actor, rkey = match.group(1), match.group(2)

    try:
        did = actor
        if not did.startswith("did:"):
            resp = requests.get(
                f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle",
                params={"handle": actor}, timeout=20,
            )
            resp.raise_for_status()
            did = resp.json()["did"]

        at_uri = f"at://{did}/app.bsky.feed.post/{rkey}"
        resp = requests.get(
            f"{APPVIEW}/xrpc/app.bsky.feed.getPosts",
            params={"uris": at_uri}, timeout=20,
        )
        resp.raise_for_status()
        posts = resp.json().get("posts", [])
        if not posts:
            return None
        # The cid pins a specific version of the record; without it the
        # embed is rejected.
        return {"uri": posts[0]["uri"], "cid": posts[0]["cid"]}
    except (requests.RequestException, KeyError, ValueError) as exc:
        print(f"  could not resolve quote {url}: {exc}")
        return None


# ---------- bluesky ----------

def create_session():
    resp = requests.post(
        f"{PDS}/xrpc/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_APP_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def create_post(session, text, facets, embed=None):
    record = {
        "$type": "app.bsky.feed.post",
        "text": text,
        "createdAt": datetime.now(timezone.utc).isoformat().replace("+00:00", "Z"),
        "langs": ["en"],
    }
    if facets:
        record["facets"] = facets
    if embed:
        record["embed"] = embed

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
        quote = resolve_quote(field(event, "LOCATION"))
        embed = {"$type": "app.bsky.embed.record", "record": quote} if quote else None

        text, link_spans = resolve_markdown_links(
            build_post_text(event, quoted=bool(quote))
        )
        facets = build_facets(text, link_spans)
        print(f"[{minutes_out:.0f} min out] {summary}")

        if DRY_RUN:
            print("--- would post ---")
            print(text)
            print(f"--- {grapheme_len(text)}/{MAX_GRAPHEMES} graphemes, "
                  f"{len(facets)} facet(s), "
                  f"{'quoting ' + quote['uri'] if quote else 'no embed'} ---")
            continue

        if session is None:
            session = create_session()
        create_post(session, text, facets, embed)
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
