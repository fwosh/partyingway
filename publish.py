#!/usr/bin/env python3
"""
Publish rows a reviewer ticked: repost on Bluesky, then make the event real.

Two ways to make it real, set by PUBLISH_MODE:

  calendar  (default)  write the Google Calendar event directly. IFTTT is
                       not involved and can be switched off.
  sheet                append a row to the Form Responses sheet that IFTTT
                       already watches, and let IFTTT build the event as it
                       does today. Use this to migrate in two steps.

A row with a problem gets its Status set to a "needs ..." note and is left
ticked, so fixing the cell is enough to make the next run retry it.
"""

import json
import os
import re
import sys
from datetime import date, datetime, time, timedelta
from zoneinfo import ZoneInfo

import requests
from googleapiclient.discovery import build

import sheet

BSKY_HANDLE = os.environ["BSKY_HANDLE"]
BSKY_APP_PASSWORD = os.environ["BSKY_APP_PASSWORD"]

PUBLISH_MODE = os.environ.get("PUBLISH_MODE", "calendar").lower()
REPOST = os.environ.get("REPOST", "true").lower() in ("1", "true", "yes")
DEFAULT_DURATION_HOURS = int(os.environ.get("DEFAULT_DURATION_HOURS", "3"))
# How long a finished row stays in the queue before moving to the archive.
# The reminder reads the calendar, not the sheet, so archiving early is safe:
# the row's only remaining job is to stop the mention being re-queued.
ARCHIVE_AFTER_DAYS = int(os.environ.get("ARCHIVE_AFTER_DAYS", "14"))
# Used only when a row's Zone cell is blank. Leave it unset and a row with no
# timezone is reported rather than guessed at.
DEFAULT_TIMEZONE = os.environ.get("DEFAULT_TIMEZONE", "").strip()
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

PDS = "https://bsky.social"
APPVIEW = "https://public.api.bsky.app"
PACIFIC = ZoneInfo("America/Los_Angeles")
BSKY_POST_URL_RE = re.compile(
    r"https?://(?:www\.)?bsky\.app/profile/([^/\s]+)/post/([A-Za-z0-9._~-]+)"
)
REQUIRED = ("Tag", "Date", "Start", "Address", "Link")


class RowProblem(Exception):
    """Something the reviewer has to fix before this row can go out."""


# ---------- field checking ----------

def clean(value):
    return str(value).strip() if value not in (None, "") else ""


# Sheets counts days from this date, so a formatted date cell reads back as
# a plain number. Times come back as a fraction of a day.
SHEETS_EPOCH = date(1899, 12, 30)


def as_date(value):
    """Accept either a Sheets date serial or a typed-in string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        return SHEETS_EPOCH + timedelta(days=int(value))
    text = clean(value)
    for fmt in ("%Y-%m-%d", "%B %d, %Y", "%b %d, %Y", "%m/%d/%Y", "%m/%d/%y"):
        try:
            return datetime.strptime(text, fmt).date()
        except ValueError:
            continue
    return None


def as_time(value):
    """Accept either a Sheets time fraction or a typed-in string."""
    if isinstance(value, bool):
        return None
    if isinstance(value, (int, float)):
        minutes = round((float(value) % 1) * 24 * 60)
        return time((minutes // 60) % 24, minutes % 60)
    text = clean(value).upper().replace(".", "")
    for fmt in ("%H:%M", "%H:%M:%S", "%I:%M %p", "%I:%M%p", "%I %p"):
        try:
            return datetime.strptime(text, fmt).time()
        except ValueError:
            continue
    return None


def event_zone(row):
    """The timezone the host stated their times in.

    Times are stored exactly as the host gave them and handed to Google with
    this zone attached, so Calendar does the converting - correctly, for
    every viewer, with daylight saving handled for the event's own date.
    """
    written = clean(row["Zone"]) or DEFAULT_TIMEZONE
    if not written:
        raise RowProblem("needs zone")
    # "PT", "EST", "Server Time" and America/New_York all land on a proper
    # Area/Location zone. That matters: the bare tzdata abbreviations are
    # fixed offsets that ignore daylight saving, so EST in July would be an
    # hour out. Mapping them beats both rejecting and trusting them.
    name = sheet.resolve_zone(written)
    if not name:
        raise RowProblem(f"{written!r} isn't a timezone we recognise - "
                         f"pick one from the dropdown")
    try:
        return name, ZoneInfo(name)
    except Exception:
        raise RowProblem(f"{name!r} isn't a known timezone")


def parse_when(row):
    day = as_date(row["Date"])
    if day is None:
        raise RowProblem(f"couldn't read the date {clean(row['Date'])!r}")

    start_time = as_time(row["Start"])
    if start_time is None:
        raise RowProblem(f"couldn't read the start time {clean(row['Start'])!r}")

    zone_name, zone = event_zone(row)
    start = datetime.combine(day, start_time, tzinfo=zone)
    end_time = as_time(row["End"]) if clean(row["End"]) else None
    if end_time is not None:
        end = datetime.combine(day, end_time, tzinfo=zone)
        if end <= start:
            end += timedelta(days=1)  # runs past midnight
    else:
        end = start + timedelta(hours=DEFAULT_DURATION_HOURS)
    return start, end, zone_name


def check(row):
    missing = [name for name in REQUIRED if not clean(row[name])]
    if missing:
        raise RowProblem("needs " + ", ".join(name.lower() for name in missing))
    return parse_when(row)


# ---------- publishing ----------

def hashtag_url(tag):
    """A Bluesky hashtag feed URL. Strips the leading # and anything that
    can't appear in a tag, so a stray character can't break the link."""
    bare = re.sub(r"[^\w]", "", clean(tag).lstrip("#"))
    return f"https://bsky.app/hashtag/{bare}" if bare else ""


def to_calendar(row, start, end, zone_name):
    """What post_reminders.py reads an hour before the event, so the shapes
    have to line up: Location becomes the quote-post embed, and Description
    becomes the reminder's body text once its HTML is stripped."""
    tag, address = clean(row["Tag"]), clean(row["Address"])
    lines = []
    if address:
        lines.append(f"\U0001f3d8\ufe0fLocation: {address}")
    if tag:
        url = hashtag_url(tag)
        # Calendar descriptions render HTML, so the tag can link out to the
        # Bluesky feed for it. post_reminders.py strips the markup, leaving
        # a plain #tag that its own facet logic makes tappable again.
        linked = f'<a href="{url}">{tag}</a>' if url else tag
        lines.append(f"\U0001f3f7\ufe0fTag: {linked}")

    event = {
        "summary": tag or "Art party",
        "location": clean(row["Link"]),
        "description": "\n".join(lines),
        "start": {"dateTime": start.isoformat(), "timeZone": zone_name},
        "end": {"dateTime": end.isoformat(), "timeZone": zone_name},
    }
    from google.oauth2 import service_account
    credentials = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]),
        scopes=["https://www.googleapis.com/auth/calendar"],
    )
    svc = build("calendar", "v3", credentials=credentials, cache_discovery=False)
    created = svc.events().insert(
        calendarId=os.environ["CALENDAR_ID"], body=event).execute()
    return created.get("htmlLink", "")


def to_responses_sheet(row, start, end, svc):
    """Append a row shaped like a form submission, for IFTTT to pick up.

    RESPONSES_COLUMNS must match the left-to-right column order of the
    Form Responses tab.
    """
    order = os.environ.get(
        "RESPONSES_COLUMNS", "timestamp,tag,link,address,date,start,end"
    ).split(",")
    values = {
        "timestamp": datetime.now(PACIFIC).strftime("%m/%d/%Y %H:%M:%S"),
        "tag": clean(row["Tag"]),
        "link": clean(row["Link"]),
        "address": clean(row["Address"]),
        # The old form collected Pacific, so convert for this path only.
        "date": start.astimezone(PACIFIC).strftime("%Y-%m-%d"),
        "start": start.astimezone(PACIFIC).strftime("%H:%M"),
        "end": end.astimezone(PACIFIC).strftime("%H:%M"),
    }
    tab = os.environ.get("RESPONSES_TAB", "Form Responses 1")
    svc.spreadsheets().values().append(
        spreadsheetId=os.environ["RESPONSES_SHEET_ID"],
        range=f"{tab}!A:Z",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [[values.get(name.strip(), "") for name in order]]},
    ).execute()
    return "handed to IFTTT"


# ---------- bluesky ----------

def create_session():
    resp = requests.post(
        f"{PDS}/xrpc/com.atproto.server.createSession",
        json={"identifier": BSKY_HANDLE, "password": BSKY_APP_PASSWORD},
        timeout=30,
    )
    resp.raise_for_status()
    return resp.json()


def resolve_post(url):
    """bsky.app URL -> the {uri, cid} strongref a repost record needs."""
    match = BSKY_POST_URL_RE.search(url or "")
    if not match:
        return None
    actor, rkey = match.group(1), match.group(2)
    did = actor
    if not did.startswith("did:"):
        resp = requests.get(f"{APPVIEW}/xrpc/com.atproto.identity.resolveHandle",
                            params={"handle": actor}, timeout=20)
        resp.raise_for_status()
        did = resp.json()["did"]
    resp = requests.get(f"{APPVIEW}/xrpc/app.bsky.feed.getPosts",
                        params={"uris": f"at://{did}/app.bsky.feed.post/{rkey}"},
                        timeout=20)
    resp.raise_for_status()
    posts = resp.json().get("posts", [])
    return {"uri": posts[0]["uri"], "cid": posts[0]["cid"]} if posts else None


def repost(session, subject):
    resp = requests.post(
        f"{PDS}/xrpc/com.atproto.repo.createRecord",
        headers={"Authorization": f"Bearer {session['accessJwt']}"},
        json={"repo": session["did"], "collection": "app.bsky.feed.repost",
              "record": {"$type": "app.bsky.feed.repost", "subject": subject,
                         "createdAt": datetime.now(ZoneInfo("UTC")).isoformat()
                                               .replace("+00:00", "Z")}},
        timeout=30,
    )
    resp.raise_for_status()


# ---------- main ----------

def action_for(row):
    """Which of the three actions a row is asking for, or None.

    The boxes are separate so each is one click, which means a row can carry
    more than one tick. Precedence runs least-destructive first:

      Archive      file it, do nothing else
      Repost only  repost, make NO calendar event
      Approve      repost and create the calendar event

    Repost only beats Approve deliberately. Regulars like CanvasAP already
    have a hand-maintained recurring event, so the costly mistake is adding
    a duplicate, not omitting one: a missing event is a row you tick again,
    a duplicate is one somebody has to notice and delete.
    """
    if sheet.is_ticked(row, "Archive"):
        return None
    status = clean(row["Status"])
    if status and not status.startswith("needs"):
        return None
    if sheet.is_ticked(row, "Repost only"):
        return "repost"
    if sheet.is_ticked(row, "Approve"):
        return "publish"
    return None


def pending(row):
    return action_for(row) is not None


def finished(row):
    """Nothing further will happen to this row."""
    status = clean(row["Status"])
    return bool(status) and not status.startswith("needs")


def stale(row, now):
    queued = clean(row["Queued"])
    if not queued:
        return False
    for fmt in ("%Y-%m-%d %H:%M", "%Y-%m-%d"):
        try:
            when = datetime.strptime(queued[:16], fmt).replace(tzinfo=PACIFIC)
        except ValueError:
            continue
        return (now - when).days >= ARCHIVE_AFTER_DAYS
    return False


def sweep(svc):
    """Clear the queue of anything that doesn't need a reviewer any more.

    Two ways a row leaves: a human ticked Archive, which is immediate and
    needs no reason; or it finished on its own and has since aged out.
    """
    now = datetime.now(PACIFIC)
    by_hand, aged = [], []
    for row in sheet.read_rows(svc):
        if sheet.is_ticked(row, "Archive"):
            by_hand.append(row)
        elif finished(row) and stale(row, now):
            aged.append(row)

    # A hand-archived row usually has no Status; say why it left, so the
    # archive reads as a record rather than a pile.
    for row in by_hand:
        if not clean(row["Status"]):
            sheet.set_cell(row["_row"], "Status", "Archived by hand", svc)

    rows = by_hand + aged
    if not rows:
        return
    moved = sheet.move_to_archive(
        [r["_row"] for r in rows], svc, sheet_id=sheet.tab_id(svc))
    parts = []
    if by_hand:
        parts.append(f"{len(by_hand)} ticked for archive")
    if aged:
        parts.append(f"{len(aged)} finished over {ARCHIVE_AFTER_DAYS} days ago")
    print(f"archived {moved} row(s): {', '.join(parts)}")


def main():
    if PUBLISH_MODE not in ("calendar", "sheet"):
        sys.exit(f"PUBLISH_MODE should be 'calendar' or 'sheet', not {PUBLISH_MODE!r}")

    svc = sheet.service()
    rows = [(row, action_for(row)) for row in sheet.read_rows(svc)]
    rows = [(row, act) for row, act in rows if act]
    counts = {a: sum(1 for _, x in rows if x == a) for a in ("publish", "repost")}
    print(f"{len(rows)} row(s) ticked and waiting "
          f"({counts['publish']} to publish, {counts['repost']} repost-only, "
          f"{PUBLISH_MODE} mode)")

    session = None
    for row, action in rows:
        label = f"row {row['_row']} ({clean(row['Tag']) or 'untagged'})"

        if action == "repost":
            # No calendar event, so the date and time don't have to make
            # sense - only the link matters.
            if not clean(row["Link"]):
                print(f"  {label}: needs link")
                if not DRY_RUN:
                    sheet.set_cell(row["_row"], "Status", "needs link", svc)
                continue
            if DRY_RUN:
                print(f"  {label}: would repost only, no calendar event")
                continue
            subject = resolve_post(clean(row["Link"]))
            if not subject:
                print(f"  {label}: link isn't a Bluesky post")
                sheet.set_cell(row["_row"], "Status",
                               "needs a Bluesky link to repost", svc)
                continue
            session = session or create_session()
            try:
                repost(session, subject)
            except Exception as exc:
                print(f"  {label}: repost failed: {exc}")
                sheet.set_cell(row["_row"], "Status",
                               f"needs retry: {exc}"[:200], svc)
                continue
            status = f"Reposted {datetime.now(PACIFIC):%m-%d %H:%M} (no event)"
            sheet.set_cell(row["_row"], "Status", status, svc)
            print(f"  {label}: {status}")
            continue

        try:
            start, end, zone_name = check(row)
        except RowProblem as problem:
            print(f"  {label}: {problem}")
            if not DRY_RUN:
                sheet.set_cell(row["_row"], "Status", str(problem), svc)
            continue

        if DRY_RUN:
            print(f"  {label}: would publish {start:%a %b %d %H:%M}–{end:%H:%M} "
                  f"{zone_name} ({start.astimezone(PACIFIC):%H:%M} PT)")
            continue

        try:
            where = (to_calendar(row, start, end, zone_name)
                     if PUBLISH_MODE == "calendar"
                     else to_responses_sheet(row, start, end, svc))
        except Exception as exc:  # leave it ticked so the next run retries
            print(f"  {label}: publish failed: {exc}")
            sheet.set_cell(row["_row"], "Status", f"needs retry: {exc}"[:200], svc)
            continue

        reposted = False
        if REPOST:
            subject = resolve_post(clean(row["Link"]))
            if subject:
                session = session or create_session()
                try:
                    repost(session, subject)
                    reposted = True
                except Exception as exc:
                    # The event exists; a failed repost shouldn't undo that.
                    print(f"  {label}: repost failed: {exc}")
            else:
                print(f"  {label}: link isn't a Bluesky post, no repost")

        status = f"Posted {datetime.now(PACIFIC):%m-%d %H:%M}"
        if REPOST and not reposted:
            status += " (no repost)"
        sheet.set_cell(row["_row"], "Status", status, svc)
        print(f"  {label}: {status} — {where}")

    if not DRY_RUN:
        sweep(svc)
    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
