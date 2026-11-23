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
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

import requests
from googleapiclient.discovery import build

import sheet

BSKY_HANDLE = os.environ["BSKY_HANDLE"]
BSKY_APP_PASSWORD = os.environ["BSKY_APP_PASSWORD"]

PUBLISH_MODE = os.environ.get("PUBLISH_MODE", "calendar").lower()
REPOST = os.environ.get("REPOST", "true").lower() in ("1", "true", "yes")
DEFAULT_DURATION_HOURS = int(os.environ.get("DEFAULT_DURATION_HOURS", "3"))
DRY_RUN = os.environ.get("DRY_RUN", "").lower() in ("1", "true", "yes")

PDS = "https://bsky.social"
APPVIEW = "https://public.api.bsky.app"
PACIFIC = ZoneInfo("America/Los_Angeles")
BSKY_POST_URL_RE = re.compile(
    r"https?://(?:www\.)?bsky\.app/profile/([^/\s]+)/post/([A-Za-z0-9._~-]+)"
)
REQUIRED = ("Tag", "Date", "Start PT", "Address", "Link")


class RowProblem(Exception):
    """Something the reviewer has to fix before this row can go out."""


# ---------- field checking ----------

def clean(value):
    return str(value).strip() if value not in (None, "") else ""


def parse_when(row):
    date_text = clean(row["Date"])
    try:
        day = datetime.strptime(date_text, "%Y-%m-%d").date()
    except ValueError:
        raise RowProblem(f"date should be YYYY-MM-DD, not {date_text!r}")

    def clock(value, label):
        text = clean(value)
        for fmt in ("%H:%M", "%H:%M:%S"):
            try:
                return datetime.strptime(text, fmt).time()
            except ValueError:
                continue
        raise RowProblem(f"{label} should be 24h HH:MM, not {text!r}")

    start = datetime.combine(day, clock(row["Start PT"], "start"), tzinfo=PACIFIC)
    if clean(row["End PT"]):
        end = datetime.combine(day, clock(row["End PT"], "end"), tzinfo=PACIFIC)
        if end <= start:
            end += timedelta(days=1)  # runs past midnight
    else:
        end = start + timedelta(hours=DEFAULT_DURATION_HOURS)
    return start, end


def check(row):
    missing = [name for name in REQUIRED if not clean(row[name])]
    if missing:
        raise RowProblem("needs " + ", ".join(name.lower() for name in missing))
    return parse_when(row)


# ---------- publishing ----------

def to_calendar(row, start, end):
    """What post_reminders.py will read an hour before the event, so the
    shapes have to line up: Location becomes the quote-post embed and
    Description becomes the reminder's body text."""
    tag, address = clean(row["Tag"]), clean(row["Address"])
    event = {
        "summary": f"Art Party — {tag}",
        "location": clean(row["Link"]),
        "description": f"{address} {tag}".strip(),
        "start": {"dateTime": start.isoformat(), "timeZone": "America/Los_Angeles"},
        "end": {"dateTime": end.isoformat(), "timeZone": "America/Los_Angeles"},
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
        "date": start.strftime("%Y-%m-%d"),
        "start": start.strftime("%H:%M"),
        "end": end.strftime("%H:%M"),
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

def pending(row):
    """Ticked and either never published or previously flagged - so fixing a
    flagged cell is enough to make it retry."""
    status = clean(row["Status"])
    return sheet.is_ticked(row) and (not status or status.startswith("needs"))


def main():
    if PUBLISH_MODE not in ("calendar", "sheet"):
        sys.exit(f"PUBLISH_MODE should be 'calendar' or 'sheet', not {PUBLISH_MODE!r}")

    svc = sheet.service()
    rows = [row for row in sheet.read_rows(svc) if pending(row)]
    print(f"{len(rows)} row(s) ticked and waiting ({PUBLISH_MODE} mode)")

    session = None
    for row in rows:
        label = f"row {row['_row']} ({clean(row['Tag']) or 'untagged'})"
        try:
            start, end = check(row)
        except RowProblem as problem:
            print(f"  {label}: {problem}")
            if not DRY_RUN:
                sheet.set_cell(row["_row"], "Status", str(problem), svc)
            continue

        if DRY_RUN:
            print(f"  {label}: would publish {start:%a %b %d %H:%M} – {end:%H:%M} PT")
            continue

        try:
            where = (to_calendar(row, start, end) if PUBLISH_MODE == "calendar"
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

    print("done")
    return 0


if __name__ == "__main__":
    sys.exit(main())
