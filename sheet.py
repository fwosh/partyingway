"""
Shared access to the review sheet.

The sheet is the only state this pipeline keeps: it's the review queue, the
record of what's been published, and the dedupe key store all at once. That
means no committing JSON back to the repo and no second source of truth to
drift out of sync.
"""

import json
import os

from google.oauth2 import service_account
from googleapiclient.discovery import build

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
TAB = os.environ.get("SHEET_TAB", "Review queue")

# Column order. The scripts address cells by these names, so if you rearrange
# columns in the sheet, rearrange them here too (and re-run setup_sheet.py).
COLUMNS = [
    "Approve",      # A  checkbox - the only thing a reviewer touches
    "Status",       # B  written back by publish.py
    "Tag",          # C
    "Date",         # D  YYYY-MM-DD, Pacific
    "Start PT",     # E  HH:MM
    "End PT",       # F  HH:MM, optional
    "As written",   # G  the time exactly as the post worded it
    "Address",      # H  FFXIV location
    "Link",         # I  the post being advertised
    "Flyer",        # J  =IMAGE() so the graphic shows in the sheet
    "Notes",        # K  parser's remarks: ambiguity, conflicts
    "Source",       # L  AT-URI, the dedupe key
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


def append_row(values, svc=None):
    """values: a dict keyed by column name. USER_ENTERED so =IMAGE() is
    treated as a formula rather than literal text."""
    svc = svc or service()
    ordered = [values.get(name, "") for name in COLUMNS]
    svc.spreadsheets().values().append(
        spreadsheetId=spreadsheet_id(),
        range=f"{TAB}!A:{LAST_COL}",
        valueInputOption="USER_ENTERED",
        insertDataOption="INSERT_ROWS",
        body={"values": [ordered]},
    ).execute()


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
