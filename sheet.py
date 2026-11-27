"""
Shared access to the review sheet.

The sheet is the only state this pipeline keeps: it's the review queue, the
record of what's been published, and the dedupe key store all at once. That
means no committing JSON back to the repo and no second source of truth to
drift out of sync.
"""

import json
import os
import re

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TAB = os.environ.get("SHEET_TAB", "Review queue")
ARCHIVE_TAB = os.environ.get("SHEET_ARCHIVE_TAB", "Archive")

# Column order. The scripts address cells by these names, so if you rearrange
# columns in the sheet, rearrange them here too (and re-run setup_sheet.py).
COLUMNS = [
    "Approve",      # A  checkbox: repost AND put it on the calendar
    "Repost only",  # B  checkbox: repost, but make no calendar event
    "Status",       # C  written back by publish.py
    "Archive",      # D  checkbox: file it away, doing nothing else
    "Tag",          # E
    "Date",         # F  YYYY-MM-DD
    "Start",        # G  HH:MM in the host's own timezone, NOT converted
    "End",          # H  HH:MM, optional
    "Zone",         # I  IANA zone those times are in
    "As written",   # J  the time exactly as the post worded it
    "Address",      # K  FFXIV location
    "Link",         # L  the post being advertised
    "Flyer",        # M  =IMAGE() so the graphic shows in the sheet
    "Check",        # N  blank unless the parser hit a real problem
    "Source",       # O  AT-URI, the dedupe key
    "Queued",       # P
]
INDEX = {name: i for i, name in enumerate(COLUMNS)}
LAST_COL = chr(ord("A") + len(COLUMNS) - 1)


def service():
    credentials = service_account.Credentials.from_service_account_info(
        json.loads(os.environ["GOOGLE_SERVICE_ACCOUNT_JSON"]), scopes=SCOPES,
    )
    return build("sheets", "v4", credentials=credentials, cache_discovery=False)


def spreadsheet_id():
    return os.environ["SHEET_ID"]


def read_rows(svc=None):
    """Every data row, as dicts, with the 1-based sheet row number attached.

    UNFORMATTED_VALUE keeps checkboxes as real booleans instead of the
    strings a formatted read would give.
    """
    svc = svc or service()
    resp = svc.spreadsheets().values().get(
        spreadsheetId=spreadsheet_id(),
        range=f"{TAB}!A2:{LAST_COL}",
        valueRenderOption="UNFORMATTED_VALUE",
    ).execute()

    rows = []
    for offset, values in enumerate(resp.get("values", [])):
        values = values + [""] * (len(COLUMNS) - len(values))
        row = {name: values[i] for name, i in INDEX.items()}
        row["_row"] = offset + 2  # +1 for the header, +1 for 1-based rows
        rows.append(row)
    return rows


ROW_HEIGHT = int(os.environ.get("FLYER_ROW_HEIGHT", "240"))


def tab_id(svc=None):
    svc = svc or service()
    meta = svc.spreadsheets().get(spreadsheetId=spreadsheet_id()).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == TAB:
            return s["properties"]["sheetId"]
    return None


def append_row(values, svc=None, sheet_id=None):
    """Put a submission at the TOP of the queue, as row 2.

    Newest-first matters here: rows are tall enough to show a flyer, so with
    newest-last a reviewer would scroll past every event they already
    handled to reach the one that needs them.

    values is a dict keyed by column name. USER_ENTERED so =IMAGE() is
    treated as a formula rather than literal text.
    """
    svc = svc or service()
    ordered = [values.get(name, "") for name in COLUMNS]

    if sheet_id is not None:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id(),
            body={"requests": [
                {"insertDimension": {
                    "range": {"sheetId": sheet_id, "dimension": "ROWS",
                              "startIndex": 1, "endIndex": 2},
                    # Take formatting from the row below, not the header.
                    "inheritFromBefore": False,
                }},
                {"updateDimensionProperties": {
                    "range": {"sheetId": sheet_id, "dimension": "ROWS",
                              "startIndex": 1, "endIndex": 2},
                    "properties": {"pixelSize": ROW_HEIGHT},
                    "fields": "pixelSize",
                }},
            ]},
        ).execute()
        svc.spreadsheets().values().update(
            spreadsheetId=spreadsheet_id(),
            range=f"{TAB}!A2:{LAST_COL}2",
            valueInputOption="USER_ENTERED",
            body={"values": [ordered]},
        ).execute()
        return

    # Without a sheet id we can't insert, so fall back to appending.
    svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id(),
        range=f"{TAB}!A:{LAST_COL}",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [ordered]},
    ).execute()


def read_sources(svc=None):
    """Every Source ever seen, across the queue AND the archive.

    Dedupe has to span both, or archiving a row would make its mention look
    new again and the whole thing would be re-parsed and re-queued.
    """
    svc = svc or service()
    letter = chr(ord("A") + INDEX["Source"])
    ranges = [f"{TAB}!{letter}2:{letter}", f"{ARCHIVE_TAB}!{letter}2:{letter}"]
    found = set()
    for rng in ranges:
        try:
            resp = svc.spreadsheets().values().get(
                spreadsheetId=spreadsheet_id(), range=rng).execute()
        except Exception:
            continue  # archive tab may not exist yet
        for row in resp.get("values", []):
            if row and row[0]:
                found.add(row[0])
    return found


def move_to_archive(row_numbers, svc=None, sheet_id=None):
    """Move whole rows out of the queue, preserving their =IMAGE() formulas.

    Rows are deleted bottom-up: deleting row 5 first would shift every row
    below it and the later deletions would hit the wrong ones.
    """
    if not row_numbers:
        return 0
    svc = svc or service()
    rows = sorted(set(row_numbers))

    values = []
    for number in rows:
        resp = svc.spreadsheets().values().get(
            spreadsheetId=spreadsheet_id(),
            range=f"{TAB}!A{number}:{LAST_COL}{number}",
            valueRenderOption="FORMULA",  # keep =IMAGE(), don't flatten it
        ).execute()
        got = resp.get("values", [[]])
        values.append((got[0] if got else []) + [""] * len(COLUMNS))

    svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id(),
        range=f"{ARCHIVE_TAB}!A:{LAST_COL}",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [v[:len(COLUMNS)] for v in values]},
    ).execute()

    if sheet_id is not None:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=spreadsheet_id(),
            body={"requests": [
                {"deleteDimension": {"range": {
                    "sheetId": sheet_id, "dimension": "ROWS",
                    "startIndex": number - 1, "endIndex": number}}}
                for number in reversed(rows)
            ]},
        ).execute()
    return len(rows)


def set_cell(row_number, column, value, svc=None):
    svc = svc or service()
    letter = chr(ord("A") + INDEX[column])
    svc.spreadsheets().values().update(
        spreadsheetId=spreadsheet_id(),
        range=f"{TAB}!{letter}{row_number}",
        valueInputOption="USER_ENTERED",
        body={"values": [[value]]},
    ).execute()


def is_ticked(row, column="Approve"):
    value = row.get(column)
    return value is True or str(value).strip().upper() == "TRUE"


# --- timezones -------------------------------------------------------------
# Reviewers shouldn't have to type "America/Los_Angeles". The sheet shows a
# short label; everything resolves to a proper Area/Location zone, which is
# what actually tracks daylight saving. The bare tzdata abbreviations (EST,
# PST) are deliberately NOT used as zones: they are fixed offsets that ignore
# DST, so an event in July would land an hour out.
# Deliberately short: the zones FFXIV art parties actually happen in, not
# the full tzdata list. The label carries both forms so a reviewer can scan
# for the abbreviation they know while the sheet stores something precise.
ZONE_LABELS = [                       # (label, IANA) - dropdown order
    ("America/Los_Angeles (PT)", "America/Los_Angeles"),
    ("America/Denver (MT)", "America/Denver"),
    ("America/Chicago (CT)", "America/Chicago"),
    ("America/New_York (ET)", "America/New_York"),
    ("America/Anchorage (AKT)", "America/Anchorage"),
    ("Pacific/Honolulu (HT)", "Pacific/Honolulu"),
    ("Europe/London (GMT/BST)", "Europe/London"),
    ("Europe/Paris (CET)", "Europe/Paris"),
    ("Asia/Tokyo (JST)", "Asia/Tokyo"),
    ("Australia/Sydney (AET)", "Australia/Sydney"),
]
LABEL_FOR_ZONE = {iana: label for label, iana in ZONE_LABELS}

# FFXIV Server Time is UTC. The sheet no longer offers UTC as a choice, so
# intake converts ST times into this zone instead. Named rather than
# inlined because getting the pair wrong is a silent one-hour error.
SERVER_TIME = "UTC"
SERVER_TIME_AS = "Europe/London"

# Everything a host or reviewer might plausibly write, folded to one zone.
ZONE_ALIASES = {}
for _label, _iana in ZONE_LABELS:
    ZONE_ALIASES[_label.upper()] = _iana
    ZONE_ALIASES[_iana.upper()] = _iana
ZONE_ALIASES.update({
    "PT": "America/Los_Angeles", "PST": "America/Los_Angeles",
    "PDT": "America/Los_Angeles", "PACIFIC": "America/Los_Angeles",
    "MT": "America/Denver", "MST": "America/Denver",
    "MDT": "America/Denver", "MOUNTAIN": "America/Denver",
    "CT": "America/Chicago", "CST": "America/Chicago",
    "CDT": "America/Chicago", "CENTRAL": "America/Chicago",
    "ET": "America/New_York", "EST": "America/New_York",
    "EDT": "America/New_York", "EASTERN": "America/New_York",
    "AKT": "America/Anchorage", "AKST": "America/Anchorage",
    "AKDT": "America/Anchorage",
    "HT": "Pacific/Honolulu", "HST": "Pacific/Honolulu",
    # No UTC in the sheet, by request: a feed consumer that renders a UTC
    # event without converting shows EU readers the wrong hour all summer,
    # and two near-identical zones in one dropdown invited mis-picks.
    # Everything UTC-ish therefore lands on Europe/London. Server Time is
    # still genuinely UTC, so intake.py SHIFTS the clock time when it folds
    # an ST party into London - see server_time_to_london() there. Relabel
    # without shifting and ST parties run an hour early from April to
    # October.
    "ST": "Europe/London", "SERVER TIME": "Europe/London",
    "SERVERTIME": "Europe/London",
    "UTC": "Europe/London", "ZULU": "Europe/London",
    "GMT": "Europe/London", "BST": "Europe/London", "JST": "Asia/Tokyo",
    "CET": "Europe/Paris", "CEST": "Europe/Paris",
    "AEST": "Australia/Sydney", "AEDT": "Australia/Sydney",
})


def zone_label(iana):
    """The short form to show in the sheet, falling back to the full name."""
    return LABEL_FOR_ZONE.get((iana or "").strip(), (iana or "").strip())


def resolve_zone(value):
    """A sheet cell -> an IANA zone name, or None if it means nothing.

    Accepts the dropdown's full label ("America/New_York (ET)"), either half
    of it on its own, and the shorthand a host might type ("EST", "Server
    Time"). The abbreviations resolve to Area/Location zones rather than
    being used as zones themselves, since the bare tzdata ones are fixed
    offsets that ignore daylight saving.
    """
    text = str(value or "").strip()
    if not text:
        return None

    candidates = [text]
    if "(" in text:
        candidates.append(text.split("(", 1)[0].strip())          # before the paren
        candidates.append(text.split("(", 1)[1].rstrip(") ").strip())  # inside it
    for candidate in candidates:
        if not candidate:
            continue
        hit = ZONE_ALIASES.get(candidate.upper())
        if hit:
            return hit
    for candidate in candidates:
        if "/" in candidate:
            return candidate
    return None
