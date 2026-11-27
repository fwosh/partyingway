#!/usr/bin/env python3
"""
Watch for art-party submissions on Bluesky and queue them for review.

Protocol: a host (or anyone) tags @partyingway in a REPLY to the post that
announces the party. The reply is only the signal - the details live in the
root post of that thread, which is also what eventually gets reposted. So
each mention is walked up to its thread root, read, and turned into a row
on the review sheet for a human to check.

Nothing here publishes anything. DMs are ignored by design.
"""

import json
import os
import sys
from datetime import date as date_cls, datetime, time as time_cls, timedelta, timezone
from zoneinfo import ZoneInfo

import requests

import sheet

BSKY_HANDLE = os.environ["BSKY_HANDLE"]
BSKY_APP_PASSWORD = os.environ["BSKY_APP_PASSWORD"]
ANTHROPIC_API_KEY = os.environ["ANTHROPIC_API_KEY"]

MODEL = os.environ.get("MODEL", "claude-haiku-5-5")
LOOKBACK_HOURS = int(os.environ.get("LOOKBACK_HOURS", "48"))
# Ceiling on API calls per run, so a burst of mentions can't spend a lot.
MAX_PARSES_PER_RUN = int(os.environ.get("MAX_PARSES_PER_RUN", "8"))
# Images dominate the cost, but capping too hard loses details: hosts often
# post a decorative in-character image first and the real flyer second.
# Bluesky allows 4 per post, so this is effectively "all of them".
MAX_IMAGES = int(os.environ.get("MAX_IMAGES", "4"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")
# Re-read posts already on the sheet. For comparing models against each
# other on the same posts, where dedupe would otherwise skip everything.
IGNORE_SEEN = os.environ.get("IGNORE_SEEN", "").lower() in ("1", "true", "yes")
if IGNORE_SEEN and not DRY_RUN:
    # Writing would duplicate every row, so this combination is never what
    # anyone means.
    print("IGNORE_SEEN only runs as a dry run; not writing to the sheet.")
    DRY_RUN = True

PDS = "https://bsky.social"
APPVIEW = "https://public.api.bsky.app"
PACIFIC = ZoneInfo("America/Los_Angeles")


# ---------- bluesky ----------

def create_session():
    resp = requests.post(
        f"{PDS}/xrpc/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_APP_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def list_mentions(session, since):
    """Mention notifications newer than `since`.

    listNotifications needs auth, so this goes through the PDS with the
    session token rather than the public AppView.
    """
    out, cursor = [], None
    for _ in range(5):
        params = {"limit": 50}
        if cursor:
            params["cursor"] = cursor
        resp = requests.get(
            f"{PDS}/xrpc/app.bsky.notification.listNotifications",
            headers={"Authorization": f"Bearer {session['accessJwt']}"},
            params=params, timeout=30,
        )
        resp.raise_for_status()
        payload = resp.json()
        reached_end = False
        for notif in payload.get("notifications", []):
            if notif.get("indexedAt", "") < since:
                reached_end = True
                continue
            if notif.get("reason") == "mention":
                out.append(notif)
        cursor = payload.get("cursor")
        if reached_end or not cursor:
            break
    return out


def source_uri(notification):
    """The post the details actually live in: the thread root for a reply,
    or the post itself if someone tagged us in a standalone post."""
    reply = (notification.get("record") or {}).get("reply") or {}
    return (reply.get("root") or {}).get("uri") or notification.get("uri")


def get_post(uri):
    resp = requests.get(
        f"{APPVIEW}/xrpc/app.bsky.feed.getPosts",
        params={"uris": uri}, timeout=30,
    )
    resp.raise_for_status()
    posts = resp.json().get("posts", [])
    return posts[0] if posts else None


def author_followups(uri, author_did, limit=8):
    """Text of replies the HOST added under their own announcement.

    Hosts often put the time in a reply rather than editing the post. Only
    the root author's replies count - anyone else in the thread is chatter,
    and trusting it would let a stranger set an event's time. Text only, by
    design: images down a thread are usually reaction art, and reading them
    would multiply the cost of every parse.
    """
    try:
        resp = requests.get(
            f"{APPVIEW}/xrpc/app.bsky.feed.getPostThread",
            params={"uri": uri, "depth": 6, "parentHeight": 0}, timeout=30,
        )
        resp.raise_for_status()
        thread = resp.json().get("thread") or {}
    except (requests.RequestException, ValueError) as exc:
        print(f"    couldn't read the thread: {exc}")
        return []

    found = []

    def walk(node):
        for reply in (node.get("replies") or []):
            if len(found) >= limit:
                return
            post = reply.get("post") or {}
            did = ((post.get("author") or {}).get("did") or "")
            text = ((post.get("record") or {}).get("text") or "").strip()
            if did == author_did and text:
                found.append(text)
            walk(reply)

    walk(thread)
    return found


def post_images(post):
    """Covers plain image embeds and the record-with-media shape a post gets
    when it quotes something and carries art."""
    embed = post.get("embed") or {}
    images = embed.get("images")
    if images is None and "media" in embed:
        images = (embed.get("media") or {}).get("images")
    return [
        {"url": img["fullsize"], "alt": (img.get("alt") or "").strip()}
        for img in (images or []) if img.get("fullsize")
    ]


def web_url(post):
    rkey = post["uri"].rsplit("/", 1)[-1]
    handle = (post.get("author") or {}).get("handle") or ""
    return f"https://bsky.app/profile/{handle}/post/{rkey}"


# ---------- parsing ----------

EXTRACT_SCHEMA = {
    "schema": {
        "type": "object",
        "additionalProperties": False,
        "properties": {
            "is_art_party": {"type": "boolean",
                "description": "False if the post isn't announcing an art party."},
            "tag": {"type": "string",
                "description": "Event hashtag including the leading #. Empty string if none."},
            "address": {"type": "string",
                "description": ("FFXIV location as 'Datacenter, World, District, Ward, "
                                "Plot'. Keep apartment/subdivision detail. Empty if absent.")},
            "date": {"type": "string",
                "description": ("Event date as YYYY-MM-DD, in the SAME zone as "
                                "start_local - the date the host states, not the "
                                "date it falls on elsewhere. Empty if undeterminable.")},
            "time_as_written": {"type": "string",
                "description": "Start time EXACTLY as worded, e.g. '7pm EST', '19:00 ST'."},
            "timezone": {"type": "string",
                "description": ("IANA zone the stated times are in, e.g. 'America/New_York'. "
                                "Use 'UTC' for FFXIV Server Time. Empty if the post never "
                                "says - do NOT guess.")},
            "start_local": {"type": "string",
                "description": ("Start time in the host's OWN timezone, 24h HH:MM. Do NOT "
                                "convert it to anywhere else. '5pm EST' is '17:00'.")},
            "end_local": {"type": "string",
                "description": "End time, same timezone, 24h HH:MM. Empty if none given."},
            "recurrence": {"type": "string",
                "description": ("If the post says this party happens on a repeating "
                                "schedule, describe it in the post's own words - "
                                "'weekly', 'every other Sunday', 'first Friday "
                                "monthly', 'bimonthly'. EMPTY STRING for a one-off. "
                                "A post advertising one date of a known series still "
                                "counts: say so if the post mentions the series.")},
            "warning": {"type": "string",
                "description": ("EMPTY STRING unless something is genuinely wrong. Only "
                                "fill this when the post text and the graphic disagree, "
                                "or a value is ambiguous enough that a human must decide. "
                                "Then: at most 10 words, e.g. 'text says 7pm, flyer says "
                                "8pm'. Missing fields need no warning - a blank cell "
                                "already shows that.")},
        },
        "required": ["is_art_party", "tag", "address", "date", "time_as_written",
                     "timezone", "start_local", "end_local", "recurrence",
                     "warning"],
    },
}

SYSTEM_PROMPT = """\
You extract Final Fantasy XIV art party details from Bluesky posts for a \
community calendar.

THE POST TEXT IS AUTHORITATIVE. Hosts usually write the full details out in \
the body, and that is what you should read first and trust. An attached \
graphic is normally a poster that restates the same information, so use it \
to CONFIRM what the text says, and to fill a gap only where the text is \
genuinely silent. If the graphic and the text disagree about a date, time or \
address, follow the TEXT and say plainly in notes that they disagree and what \
each one said - a human will settle it. Never let a hard-to-read flyer \
override something the text states clearly.

FFXIV addresses look like "Datacenter, World, District, Ward, Plot".
  Datacenters (NA): Aether, Primal, Crystal, Dynamis
  Datacenters (EU): Light, Chaos      (JP): Elemental, Gaia, Mana, Meteor
  Housing districts: Mist, The Lavender Beds, The Goblet, Shirogane, Empyreum
Wards and plots are usually written W# and P#. Subdivision and apartment \
numbers matter - keep them.

TIMES: do NOT convert between timezones. Report the time the host stated, \
in the host's own zone, and name that zone. The calendar does the converting.
- start_local / end_local: the stated time as 24-hour HH:MM. "5pm EST" is \
"17:00". "7:30pm" is "19:30". Nothing moves.
- timezone: the IANA name for the zone the host used - America/New_York for \
ET, America/Chicago for CT, America/Denver for MT, America/Los_Angeles for \
PT, Europe/London for GMT/BST, Asia/Tokyo for JST. Use the IANA name, not \
the abbreviation, and don't worry about daylight saving: the IANA zone \
already handles it for whatever date the event falls on.
- "ST" or "Server Time" means FFXIV Server Time, which is UTC. Always.
- "GMT" on a flier is almost always an EU host meaning their own clock, \
which is BST for half the year - so that is Europe/London, NOT UTC. Only \
use UTC when the post says Server Time or ST.
- If the post gives a time with NO zone at all, leave timezone as an EMPTY \
STRING. Do not guess, and do not assume the host posts from their own \
country. A human will resolve it.
- Put the time exactly as worded in time_as_written so the reviewer can \
check you read it correctly.

DATES: "Saturday" or "Nov 30" with no year means the next future occurrence \
relative to the current date below. If that lands more than 90 days out, say \
so in notes.

RECURRING PARTIES: many hosts run a regular series and post a fresh flier \
for each session. If the post mentions any repeating schedule, put it in \
recurrence. Do not try to work out future dates - just report the date this \
particular post is advertising, and describe the pattern in recurrence so a \
human can decide what to do about it.

Never invent a detail that isn't present. Leave any field you cannot \
determine as an empty string - that is correct and useful, whereas a \
plausible guess is harmful, because it can pass review unnoticed. A blank \
field is how the reviewer sees there was nothing to find, so it needs no \
comment. Leave `warning` empty unless something genuinely conflicts.
"""


def parse_post(post, followups=()):
    now_pt = datetime.now(PACIFIC)
    text = (post.get("record") or {}).get("text", "")
    all_images = post_images(post)
    images = all_images[:MAX_IMAGES]
    if len(all_images) > len(images):
        print(f"    {len(all_images)} images attached, sending {len(images)}")

    content = []
    for image in images:
        content.append({"type": "image",
                        "source": {"type": "url", "url": image["url"]}})
        if image["alt"]:
            content.append({"type": "text",
                            "text": f"(alt text for the image above: {image['alt']})"})
    prompt = (
        f"Current date and time in Pacific: {now_pt.strftime('%Y-%m-%d %H:%M %Z')}\n"
        f"Post author: @{(post.get('author') or {}).get('handle', '')}\n"
        f"Attached graphics: {len(images)}\n\n"
        f"POST TEXT (authoritative):\n{text or '(no text)'}"
    )
    if followups:
        joined = "\n---\n".join(followups)
        prompt += (
            "\n\nLATER REPLIES BY THE SAME HOST, oldest first. Hosts often "
            "add or correct the date, time or address here rather than "
            "editing the post. Treat these as authoritative too, and where "
            "a reply contradicts the original post, the reply wins - say so "
            "in warning. Only text was read from these; any images they "
            "carry were not.\n"
            f"{joined}"
        )
    content.append({"type": "text", "text": prompt})

    resp = requests.post(
        "https://api.anthropic.com/v1/messages",
        headers={"x-api-key": ANTHROPIC_API_KEY,
                 "anthropic-version": "2023-06-01",
                 "content-type": "application/json"},
        json={"model": MODEL, "max_tokens": 2000, "system": SYSTEM_PROMPT,
              # Structured outputs: the reply is guaranteed to be JSON
              # matching this schema, in a text block.
              "output_config": {"format": {"type": "json_schema",
                                           **EXTRACT_SCHEMA}},
              "messages": [{"role": "user", "content": content}]},
        timeout=120,
    )
    if resp.status_code != 200:
        # The body explains exactly what was wrong; raise_for_status() hides it.
        print(f"    Anthropic API {resp.status_code}: {resp.text[:1500]}")
    resp.raise_for_status()
    payload = resp.json()
    usage = payload.get("usage", {})
    print(f"    tokens in/out: {usage.get('input_tokens')}/{usage.get('output_tokens')}")
    for block in payload.get("content", []):
        if block.get("type") == "text":
            return json.loads(block["text"])
    raise RuntimeError("model returned no text block")


# ---------- rows ----------

def server_time_to_london(fields):
    """Fold a Server Time party into Europe/London, moving the clock with it.

    The sheet has no UTC option any more, so an ST party has to be stored in
    some other zone - and Server Time only equals a London clock in winter.
    From late March to late October London is BST, an hour ahead, so simply
    relabelling the zone would advertise the party an hour early. Shift the
    stated times by whatever the offset is on the event's own date and the
    instant survives the relabel.

    No-ops outside that window, which is why nothing looks odd in November.
    Returns (fields, note) - note is non-empty only when a shift happened,
    so the reviewer can see it was deliberate and not "correct" it back.
    """
    if (fields.get("timezone") or "").strip() != sheet.SERVER_TIME:
        return fields, ""

    fields = dict(fields)
    fields["timezone"] = sheet.SERVER_TIME_AS
    source, target = ZoneInfo(sheet.SERVER_TIME), ZoneInfo(sheet.SERVER_TIME_AS)

    day = parse_date(fields.get("date"))
    start = parse_clock(fields.get("start_local"))
    if day is None or start is None:
        # Nothing to anchor the offset to. Leave the times alone and let a
        # human look: a blank or unparseable date is already flagged.
        return fields, "server time, date unclear - check the hour by hand"

    def shifted(clock):
        if clock is None:
            return None
        moved = datetime.combine(day, clock, tzinfo=source).astimezone(target)
        return moved

    moved_start = shifted(start)
    if moved_start.hour == start.hour and moved_start.minute == start.minute:
        return fields, ""                      # winter: ST and London agree

    fields["start_local"] = moved_start.strftime("%H:%M")
    moved_end = shifted(parse_clock(fields.get("end_local")))
    if moved_end is not None:
        fields["end_local"] = moved_end.strftime("%H:%M")

    note = (f"{start.strftime('%H:%M')} server time = "
            f"{moved_start.strftime('%H:%M')} {moved_start.tzname()} - "
            f"do not change back")
    if moved_start.date() != day:
        # 23:00 ST in summer is 00:00 BST the next day. Move the date too,
        # and say so loudly, because it now disagrees with the flyer.
        fields["date"] = moved_start.date().isoformat()
        note += f"; date moved to {fields['date']}"
    return fields, note


def parse_date(text):
    try:
        return date_cls.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


def parse_clock(text):
    try:
        return time_cls.fromisoformat(str(text or "").strip())
    except ValueError:
        return None


def check_note(fields, extra=""):
    """What the reviewer needs flagged, recurrence first.

    A repeating party usually already has a hand-maintained event on the
    calendar, so publishing this one would duplicate it. Saying so here is
    what stops that happening, and it names the action to take rather than
    leaving the reviewer to work it out.
    """
    parts = []
    repeats = (fields.get("recurrence") or "").strip()
    if repeats:
        parts.append(f"repeats {repeats} - use Repost only, then check the "
                     f"recurring event on the calendar")
    warning = (fields.get("warning") or "").strip()
    if warning:
        parts.append(warning)
    if extra:
        parts.append(extra)
    return "; ".join(parts)


def build_row(fields, post, link):
    images = post_images(post)
    flyer = f'=IMAGE("{images[0]["url"]}", 1)' if images else ""
    fields, shift = server_time_to_london(fields)
    return {
        "Approve": False,
        "Repost only": False,
        "Status": "",
        "Archive": False,
        "Tag": fields.get("tag") or "",
        "Date": fields.get("date") or "",
        "Start": fields.get("start_local") or "",
        "End": fields.get("end_local") or "",
        "Zone": sheet.zone_label(fields.get("timezone") or ""),
        "As written": fields.get("time_as_written") or "",
        "Address": fields.get("address") or "",
        "Link": link,
        "Flyer": flyer,
        "Check": check_note(fields, shift),
        "Source": post["uri"],
        "Queued": datetime.now(PACIFIC).strftime("%Y-%m-%d %H:%M"),
    }


# ---------- main ----------

def main():
    svc = sheet.service()
    sid = sheet.tab_id(svc)
    known = set() if IGNORE_SEEN else sheet.read_sources(svc)
    print(f"model: {MODEL}")
    if IGNORE_SEEN:
        print("ignoring what's already on the sheet (comparison run)")
    else:
        print(f"{len(known)} submission(s) already seen (queue + archive)")

    since = (datetime.now(timezone.utc) - timedelta(hours=LOOKBACK_HOURS)).isoformat()
    session = create_session()
    notifications = list_mentions(session, since)
    print(f"{len(notifications)} mention(s) in the last {LOOKBACK_HOURS}h")

    seen_this_run, parsed, failed = set(), 0, 0
    for notification in notifications:
        uri = source_uri(notification)
        if uri in known or uri in seen_this_run:
            continue  # several people can tag the same party
        seen_this_run.add(uri)

        if parsed >= MAX_PARSES_PER_RUN:
            print(f"  hit the {MAX_PARSES_PER_RUN}-parse cap for this run; "
                  f"the rest will be picked up next time")
            break

        post = get_post(uri)
        if post is None:
            print(f"  source post is gone, skipping: {uri}")
            continue

        link = web_url(post)
        followups = author_followups(uri, (post.get("author") or {}).get("did", ""))
        note = f", {len(followups)} host reply/replies" if followups else ""
        print(f"  parsing {link}{note}")
        try:
            fields = parse_post(post, followups)
        except Exception as exc:
            # One unreadable post shouldn't cost us the rest of the batch.
            # It isn't recorded, so the next run tries it again.
            print(f"    could not parse: {exc}")
            failed += 1
            continue
        parsed += 1

        row = build_row(fields, post, link)
        if not fields.get("is_art_party"):
            # Still write a row: the Source column is the only thing that
            # stops this mention being parsed again on every single run.
            row = {**row, "Status": "Not an event"}
        if DRY_RUN:
            print(f"    tag={fields.get('tag') or '-'} "
                  f"date={fields.get('date') or '-'} "
                  f"start={fields.get('start_local') or '-'} "
                  f"end={fields.get('end_local') or '-'} "
                  f"zone={fields.get('timezone') or '-'}")
            print(f"    as written: {fields.get('time_as_written') or '-'}")
            print(f"    address: {fields.get('address') or '-'}")
            note = check_note(fields)
            if note:
                print(f"    check: {note}")
            if row["Status"]:
                print(f"    -> {row['Status']}")
            continue

        sheet.append_row(row, svc, sheet_id=sid)
        if row["Status"]:
            print(f"    logged as {row['Status'].lower()}")
        else:
            print(f"    added: {row['Tag'] or '(untagged)'} on {row['Date'] or '?'}")

    remaining = len([n for n in notifications
                     if source_uri(n) not in known and source_uri(n) not in seen_this_run])
    print(f"done: {parsed} parsed, {failed} failed, {remaining} left for the next run")
    return 0


if __name__ == "__main__":
    sys.exit(main())
