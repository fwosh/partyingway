#!/usr/bin/env python3
"""
One-time: prepare the review sheet.

Writes the header row, turns column A into real checkboxes, sizes the
columns so the flyer and address are readable, freezes the header, and
colours the status column. Run it once from your laptop after creating an
empty spreadsheet and sharing it with the service account.

    export GOOGLE_SERVICE_ACCOUNT_JSON="$(cat key.json)"
    export SHEET_ID=...
    python setup_sheet.py

Safe to re-run: it overwrites the header and formatting, and leaves any
data rows alone.
"""

import os
import sys

import sheet

WIDTHS = {  # pixels, by column name
    "Approve": 70, "Status": 110, "Tag": 120, "Date": 90, "Start PT": 70,
    "End PT": 70, "As written": 100, "Address": 240, "Link": 170,
    "Flyer": 240, "Check": 180, "Source": 60, "Queued": 120,
}
# Tall enough to actually read a flyer without opening the post.
FLYER_ROW_HEIGHT = int(os.environ.get("FLYER_ROW_HEIGHT", "240"))
AMBER = {"red": 1, "green": 0.90, "blue": 0.70}
GREEN = {"red": 0.87, "green": 0.95, "blue": 0.87}
DATA_ROWS = 2000


def tab_id(svc):
    meta = svc.spreadsheets().get(spreadsheetId=sheet.spreadsheet_id()).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == sheet.TAB:
            return s["properties"]["sheetId"]
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    sys.exit(f"No tab named {sheet.TAB!r}. Tabs present: {titles}. "
             f"Rename one, or set SHEET_TAB.")


def clear_rules(svc, sid):
    """Re-running should replace the formatting, not pile more on top."""
    meta = svc.spreadsheets().get(
        spreadsheetId=sheet.spreadsheet_id(),
        fields="sheets(properties/sheetId,conditionalFormats)").execute()
    existing = 0
    for s in meta.get("sheets", []):
        if s["properties"]["sheetId"] == sid:
            existing = len(s.get("conditionalFormats", []))
    return [{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}}
            for _ in range(existing)]


def main():
    svc = sheet.service()
    sid = tab_id(svc)
    n = len(sheet.COLUMNS)

    svc.spreadsheets().values().update(
        spreadsheetId=sheet.spreadsheet_id(),
        range=f"{sheet.TAB}!A1:{sheet.LAST_COL}1",
        valueInputOption="RAW",
        body={"values": [sheet.COLUMNS]},
    ).execute()

    requests = clear_rules(svc, sid) + [
        # Header: bold, frozen, so it survives sorting and scrolling.
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1},
            "cell": {"userEnteredFormat": {
                "textFormat": {"bold": True},
                "backgroundColor": {"red": 0.95, "green": 0.95, "blue": 0.95},
                "verticalAlignment": "MIDDLE",
            }},
            "fields": "userEnteredFormat(textFormat,backgroundColor,verticalAlignment)",
        }},
        {"updateSheetProperties": {
            "properties": {"sheetId": sid, "gridProperties": {"frozenRowCount": 1}},
            "fields": "gridProperties.frozenRowCount",
        }},
        # Real checkboxes in column A, so reviewers click rather than type.
        {"setDataValidation": {
            "range": {"sheetId": sid, "startRowIndex": 1,
                      "startColumnIndex": sheet.INDEX["Approve"],
                      "endColumnIndex": sheet.INDEX["Approve"] + 1},
            "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True},
        }},
        # Addresses and notes wrap instead of spilling across neighbours.
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1,
                      "startColumnIndex": sheet.INDEX["Address"],
                      "endColumnIndex": sheet.INDEX["Notes"] + 1},
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP",
                                           "verticalAlignment": "MIDDLE"}},
            "fields": "userEnteredFormat(wrapStrategy,verticalAlignment)",
        }},
        # Source is bookkeeping, not for humans.
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": sheet.INDEX["Source"],
                      "endIndex": sheet.INDEX["Source"] + 1},
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser",
        }},
    ]

    # Make the rows that already hold data tall enough to read a flyer. New
    # rows size themselves as intake.py writes them, so the sheet never ends
    # up with a long tail of tall empty rows.
    existing = len(sheet.read_rows(svc))
    if existing:
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": 1, "endIndex": 1 + existing},
            "properties": {"pixelSize": FLYER_ROW_HEIGHT},
            "fields": "pixelSize",
        }})

    def col_range(name):
        return {"sheetId": sid, "startRowIndex": 1, "endRowIndex": DATA_ROWS,
                "startColumnIndex": sheet.INDEX[name],
                "endColumnIndex": sheet.INDEX[name] + 1}

    def rule(index, ranges, formula, colour):
        return {"addConditionalFormatRule": {"index": index, "rule": {
            "ranges": ranges,
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": formula}]},
                "format": {"backgroundColor": colour},
            },
        }}}

    # $L is Source: only rows that actually hold a submission. Without this
    # every empty row in the sheet matches and the whole thing turns amber.
    HAS_DATA = '$L2<>""'
    rules = [
        # Published rows go green and stop asking for attention. First, so a
        # published row doesn't also light up amber.
        (0, [{"sheetId": sid, "startRowIndex": 1, "endRowIndex": DATA_ROWS,
              "endColumnIndex": n}],
         f'=AND({HAS_DATA},REGEXMATCH($B2&"","^Posted"))', GREEN),
        # A required field the parser couldn't fill: flag that cell only.
        (1, [col_range("Tag")], f'=AND({HAS_DATA},$C2="")', AMBER),
        (2, [col_range("Date")], f'=AND({HAS_DATA},$D2="")', AMBER),
        (3, [col_range("Start PT")], f'=AND({HAS_DATA},$E2="")', AMBER),
        # Address empty, or missing the comma-separated shape an FFXIV
        # address has (datacenter, world, district, ward, plot).
        (4, [col_range("Address")],
         f'=AND({HAS_DATA},OR($H2="",LEN($H2)-LEN(SUBSTITUTE($H2,",",""))<3))',
         AMBER),
        # Anything the parser explicitly flagged.
        (5, [col_range("Check")], f'=AND({HAS_DATA},$K2<>"")', AMBER),
    ]
    for index, ranges, formula, colour in rules:
        requests.append(rule(index, ranges, formula, colour))

    for name, width in WIDTHS.items():
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": sheet.INDEX[name],
                      "endIndex": sheet.INDEX[name] + 1},
            "properties": {"pixelSize": width},
            "fields": "pixelSize",
        }})

    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet.spreadsheet_id(), body={"requests": requests},
    ).execute()

    print(f"'{sheet.TAB}' is ready: {len(sheet.COLUMNS)} columns, "
          f"checkboxes in A, {existing} data row(s) resized.")
    print(f"https://docs.google.com/spreadsheets/d/{sheet.spreadsheet_id()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
