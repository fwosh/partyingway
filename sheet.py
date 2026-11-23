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
    "Approve",      # A  checkbox - the only thing a reviewer touches
    "Status",       # B  written back by publish.py
    "Tag",          # C
    "Date",         # D  YYYY-MM-DD, Pacific
    "Start",        # E  HH:MM in the host's own timezone, NOT converted
    "End",          # F  HH:MM, optional
    "Zone",         # G  IANA zone those times are in
    "As written",   # H  the time exactly as the post worded it
    "Address",      # I  FFXIV location
    "Link",         # I  the post being advertised
    "Flyer",        # J  =IMAGE() so the graphic shows in the sheet
    "Check",        # K  blank unless the parser hit a real problem
    "Source",       # M  AT-URI, the dedupe key
    "Queued",       # M
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


def move_to_archive(row_numbers, svc=None, sheet_id=None, archive_id=None):
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


def is_ticked(row):
    value = row.get("Approve")
    return value is True or str(value).strip().upper() == "TRUE"
