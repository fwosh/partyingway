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

import sys

import sheet

WIDTHS = {  # pixels, by column name
    "Approve": 70, "Status": 110, "Tag": 120, "Date": 90, "Start PT": 70,
    "End PT": 70, "As written": 100, "Address": 260, "Link": 180,
    "Flyer": 120, "Notes": 300, "Source": 60, "Queued": 140,
}
FLYER_ROW_HEIGHT = 90


def tab_id(svc):
    meta = svc.spreadsheets().get(spreadsheetId=sheet.spreadsheet_id()).execute()
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == sheet.TAB:
            return s["properties"]["sheetId"]
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    sys.exit(f"No tab named {sheet.TAB!r}. Tabs present: {titles}. "
             f"Rename one, or set SHEET_TAB.")


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

    requests = [
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
        # Rows tall enough that =IMAGE() renders as something you can read.
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": 1, "endIndex": 500},
            "properties": {"pixelSize": FLYER_ROW_HEIGHT},
            "fields": "pixelSize",
        }},
        # A row the parser flagged turns amber, so it's obvious at a glance.
        {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": sid, "startRowIndex": 1, "endColumnIndex": n}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [
                    {"userEnteredValue": '=OR($E2="",$C2="",$D2="",$H2="")'}]},
                "format": {"backgroundColor": {"red": 1, "green": 0.95, "blue": 0.8}},
            },
        }}},
        # Published rows go green and stop demanding attention.
        {"addConditionalFormatRule": {"index": 0, "rule": {
            "ranges": [{"sheetId": sid, "startRowIndex": 1, "endColumnIndex": n}],
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA", "values": [
                    {"userEnteredValue": '=REGEXMATCH($B2&"","^Posted")'}]},
                "format": {"backgroundColor": {"red": 0.90, "green": 0.96, "blue": 0.90}},
            },
        }}},
    ]

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

    print(f"'{sheet.TAB}' is ready: {len(sheet.COLUMNS)} columns, checkboxes in A.")
    print(f"https://docs.google.com/spreadsheets/d/{sheet.spreadsheet_id()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
