#!/usr/bin/env python3
"""
One-time (and safely repeatable): lay out and style the review sheet.

Writes the header, turns column A into real checkboxes, sizes everything,
hides the gridlines and the unused columns, and sets the colour rules that
tell a reviewer where to look. Run it from your laptop:

    $env:GOOGLE_SERVICE_ACCOUNT_JSON = Get-Content key.json -Raw
    $env:SHEET_ID = "..."
    python setup_sheet.py

Re-running replaces the formatting rather than stacking more on top, so it's
the right thing to run after any change to the columns in sheet.py.
"""

import os
import sys

import sheet


def rgb(hex_colour):
    h = hex_colour.lstrip("#")
    return {"red": int(h[0:2], 16) / 255,
            "green": int(h[2:4], 16) / 255,
            "blue": int(h[4:6], 16) / 255}


FONT = os.environ.get("SHEET_FONT", "Inter")
HEADER_BG = rgb("#1F2937")      # near-black slate
HEADER_FG = rgb("#FFFFFF")
BAND_A = rgb("#FFFFFF")
BAND_B = rgb("#F7F8FA")         # barely-there stripe
AMBER = rgb("#FEF0C7")          # needs a human
GREEN = rgb("#DCFCE7")          # published, stand down
MUTED = rgb("#6B7280")
SKIPPED = rgb("#F3F4F6")   # not an event: present, but quiet
LINK = rgb("#2563EB")

WIDTHS = {  # pixels, by column name
    "Approve": 72, "Repost only": 76, "Status": 118, "Archive": 72,
    "Tag": 120, "Date": 100, "Start": 78, "End": 78, "Zone": 210,
    "As written": 110, "Address": 240, "Link": 165, "Flyer": 240,
    "Check": 230, "Source": 60, "Queued": 115,
}
CENTRED = ("Approve", "Repost only", "Archive", "Date", "Start", "End", "Queued")
# Column names that were called something else in an earlier layout. Used
# only to recognise an existing sheet so its data doesn't get shifted.
RENAMED_FROM = {"Start PT": "Start", "End PT": "End"}
ROW_HEIGHT = int(os.environ.get("FLYER_ROW_HEIGHT", "240"))
HEADER_HEIGHT = 44
FROZEN_COLUMNS = 5              # the three actions, Status and Tag stay put
STYLED_ROWS = 2000


def ensure_archive(svc, existing_titles):
    """The archive is a plain dump: same columns, no checkboxes, no colour
    rules. It exists so dedupe survives rows leaving the queue."""
    if sheet.ARCHIVE_TAB not in existing_titles:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet.spreadsheet_id(),
            body={"requests": [{"addSheet": {"properties": {
                "title": sheet.ARCHIVE_TAB,
                "gridProperties": {"frozenRowCount": 1},
            }}}]},
        ).execute()
        print(f"created the '{sheet.ARCHIVE_TAB}' tab")

    # Rewrite the header every time. Rows arrive here already in the queue's
    # column order, so a stale header would describe them wrongly - and
    # read_sources looks up Source by position, so it would stop finding it.
    svc.spreadsheets().values().update(
        spreadsheetId=sheet.spreadsheet_id(),
        range=f"{sheet.ARCHIVE_TAB}!A1:{sheet.LAST_COL}1",
        valueInputOption="RAW",
        body={"values": [sheet.COLUMNS]},
    ).execute()


def migrate_columns(svc, sid):
    """Make an existing sheet's columns match sheet.COLUMNS.

    Renames are harmless - the header gets overwritten anyway. A genuinely
    NEW column has to be inserted at its proper index, or every column after
    it shifts and the rows already on the sheet silently misalign: Source
    would stop matching, dedupe would break, and posts could be queued twice.
    """
    resp = svc.spreadsheets().values().get(
        spreadsheetId=sheet.spreadsheet_id(),
        range=f"{sheet.TAB}!A1:1").execute()
    header = (resp.get("values") or [[]])[0]
    if not header:
        return  # fresh sheet, nothing to preserve

    header = [RENAMED_FROM.get(name, name) for name in header]
    requests = []
    for index, name in enumerate(sheet.COLUMNS):
        if index < len(header) and header[index] == name:
            continue
        if name in header:
            continue  # present, just in a different place; header write fixes it
        requests.append({"insertDimension": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": index, "endIndex": index + 1},
            "inheritFromBefore": False,
        }})
        header.insert(index, name)

    if requests:
        svc.spreadsheets().batchUpdate(
            spreadsheetId=sheet.spreadsheet_id(),
            body={"requests": requests}).execute()
        added = [sheet.COLUMNS[r["insertDimension"]["range"]["startIndex"]]
                 for r in requests]
        print(f"inserted {len(requests)} new column(s): {', '.join(added)} "
              f"- existing rows shifted to match")


def find_tab(svc):
    meta = svc.spreadsheets().get(
        spreadsheetId=sheet.spreadsheet_id(),
        fields=("sheets(properties(sheetId,title,gridProperties),"
                "conditionalFormats,bandedRanges)"),
    ).execute()
    titles = [s["properties"]["title"] for s in meta.get("sheets", [])]
    ensure_archive(svc, titles)
    for s in meta.get("sheets", []):
        if s["properties"]["title"] == sheet.TAB:
            return s
    sys.exit(f"No tab named {sheet.TAB!r}. Tabs present: {titles}. "
             f"Rename one, or set SHEET_TAB.")


def clear_existing(tab, sid):
    """Re-running should replace the styling, not pile more on top. Both
    conditional formats and banded ranges reject overlapping duplicates."""
    out = [{"deleteConditionalFormatRule": {"sheetId": sid, "index": 0}}
           for _ in tab.get("conditionalFormats", [])]
    out += [{"deleteBanding": {"bandedRangeId": b["bandedRangeId"]}}
            for b in tab.get("bandedRanges", [])]
    return out


def main():
    svc = sheet.service()
    tab = find_tab(svc)
    sid = tab["properties"]["sheetId"]
    grid = tab["properties"].get("gridProperties", {})
    total_columns = grid.get("columnCount", 26)
    n = len(sheet.COLUMNS)

    migrate_columns(svc, sid)

    svc.spreadsheets().values().update(
        spreadsheetId=sheet.spreadsheet_id(),
        range=f"{sheet.TAB}!A1:{sheet.LAST_COL}1",
        valueInputOption="RAW",
        body={"values": [sheet.COLUMNS]},
    ).execute()

    whole = {"sheetId": sid, "startRowIndex": 0, "endRowIndex": STYLED_ROWS,
             "startColumnIndex": 0, "endColumnIndex": n}
    data = dict(whole, startRowIndex=1)

    def column(name, **extra):
        return {"sheetId": sid, "startRowIndex": extra.pop("from_row", 1),
                "endRowIndex": STYLED_ROWS,
                "startColumnIndex": sheet.INDEX[name],
                "endColumnIndex": sheet.INDEX[name] + 1}

    requests = clear_existing(tab, sid)

    requests += [
        # Gridlines off. With banding for separation this reads as a designed
        # table rather than an infinite spreadsheet.
        {"updateSheetProperties": {
            "properties": {"sheetId": sid,
                           "gridProperties": {"hideGridlines": True,
                                              "frozenRowCount": 1,
                                              "frozenColumnCount": FROZEN_COLUMNS}},
            "fields": ("gridProperties.hideGridlines,"
                       "gridProperties.frozenRowCount,"
                       "gridProperties.frozenColumnCount"),
        }},
        # One typeface, one size, vertically centred - rows are tall enough
        # that top-aligned text would float oddly above the flyer.
        {"repeatCell": {
            "range": whole,
            "cell": {"userEnteredFormat": {
                "textFormat": {"fontFamily": FONT, "fontSize": 10},
                "verticalAlignment": "MIDDLE",
                "horizontalAlignment": "LEFT",
            }},
            "fields": ("userEnteredFormat(textFormat,verticalAlignment,"
                       "horizontalAlignment)"),
        }},
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 0, "endRowIndex": 1,
                      "startColumnIndex": 0, "endColumnIndex": n},
            "cell": {"userEnteredFormat": {
                "backgroundColor": HEADER_BG,
                "textFormat": {"fontFamily": FONT, "fontSize": 10,
                               "bold": True, "foregroundColor": HEADER_FG},
                "verticalAlignment": "MIDDLE",
                "horizontalAlignment": "LEFT",
            }},
            "fields": ("userEnteredFormat(backgroundColor,textFormat,"
                       "verticalAlignment,horizontalAlignment)"),
        }},
        {"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": 0, "endIndex": 1},
            "properties": {"pixelSize": HEADER_HEIGHT},
            "fields": "pixelSize",
        }},
        # Quiet stripes so the eye can follow a row across 13 columns.
        {"addBanding": {"bandedRange": {
            "range": data,
            "rowProperties": {"firstBandColor": BAND_A,
                              "secondBandColor": BAND_B},
        }}},
        # Long values wrap instead of spilling over their neighbours.
        {"repeatCell": {
            "range": {"sheetId": sid, "startRowIndex": 1,
                      "endRowIndex": STYLED_ROWS,
                      "startColumnIndex": sheet.INDEX["Address"],
                      "endColumnIndex": sheet.INDEX["Check"] + 1},
            "cell": {"userEnteredFormat": {"wrapStrategy": "WRAP"}},
            "fields": "userEnteredFormat.wrapStrategy",
        }},
        {"repeatCell": {
            "range": column("Tag"),
            "cell": {"userEnteredFormat": {
                "textFormat": {"fontFamily": FONT, "fontSize": 10, "bold": True}}},
            "fields": "userEnteredFormat.textFormat",
        }},
        {"repeatCell": {
            "range": column("Link"),
            "cell": {"userEnteredFormat": {
                "textFormat": {"fontFamily": FONT, "fontSize": 9,
                               "foregroundColor": LINK}}},
            "fields": "userEnteredFormat.textFormat",
        }},
        {"repeatCell": {
            "range": column("Queued"),
            "cell": {"userEnteredFormat": {
                "textFormat": {"fontFamily": FONT, "fontSize": 9,
                               "foregroundColor": MUTED}}},
            "fields": "userEnteredFormat.textFormat",
        }},
        # Real checkboxes, so reviewing is one click. Status sits between
        # them deliberately: Approve posts, Archive files away, and they
        # should not be adjacent targets.
        {"setDataValidation": {
            "range": column("Approve"),
            "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True},
        }},
        {"setDataValidation": {
            "range": column("Repost only"),
            "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True},
        }},
        {"setDataValidation": {
            "range": column("Archive"),
            "rule": {"condition": {"type": "BOOLEAN"}, "showCustomUi": True},
        }},
        # A dropdown, so nobody has to remember "America/Los_Angeles".
        # strict=False: a zone not on the list still goes through with a
        # warning rather than being blocked, since publish.py understands
        # plenty of spellings the list doesn't show.
        {"setDataValidation": {
            "range": column("Zone"),
            "rule": {
                "condition": {"type": "ONE_OF_LIST", "values": [
                    {"userEnteredValue": label} for label, _ in sheet.ZONE_LABELS]},
                "showCustomUi": True,
                "strict": False,
            },
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

    # Store real dates and times, show them the way a person reads them.
    requests += [
        {"repeatCell": {
            "range": column("Date"),
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "DATE", "pattern": "mmmm d, yyyy"}}},
            "fields": "userEnteredFormat.numberFormat",
        }},
        {"repeatCell": {
            "range": column("Start"),
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "TIME", "pattern": "h:mm am/pm"}}},
            "fields": "userEnteredFormat.numberFormat",
        }},
        {"repeatCell": {
            "range": column("End"),
            "cell": {"userEnteredFormat": {
                "numberFormat": {"type": "TIME", "pattern": "h:mm am/pm"}}},
            "fields": "userEnteredFormat.numberFormat",
        }},
    ]

    for name in CENTRED:
        requests.append({"repeatCell": {
            "range": column(name),
            "cell": {"userEnteredFormat": {"horizontalAlignment": "CENTER"}},
            "fields": "userEnteredFormat.horizontalAlignment",
        }})

    for name, width in WIDTHS.items():
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": sheet.INDEX[name],
                      "endIndex": sheet.INDEX[name] + 1},
            "properties": {"pixelSize": width},
            "fields": "pixelSize",
        }})

    # Hide the empty columns past the last real one, so the sheet ends where
    # the data ends instead of trailing off into the alphabet.
    if total_columns > n:
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "COLUMNS",
                      "startIndex": n, "endIndex": total_columns},
            "properties": {"hiddenByUser": True},
            "fields": "hiddenByUser",
        }})

    # --- colour rules -----------------------------------------------------
    # $L is Source. Every rule is gated on it so that empty rows, of which a
    # new sheet has a thousand, stay plain.
    def ref(name):
        """$-prefixed cell reference for a column, by name.

        Worked out from sheet.COLUMNS rather than written by hand, so
        inserting a column can't silently point a rule at its neighbour.
        """
        return f"${chr(ord('A') + sheet.INDEX[name])}2"

    HAS_DATA = f'{ref("Source")}<>""'
    # Still waiting on a reviewer: no status yet, or publish asked for a fix.
    AWAITING = (f'AND({HAS_DATA},OR({ref("Status")}="",'
                f'LEFT({ref("Status")},5)="needs"))')

    def rule(index, ranges, formula, colour):
        return {"addConditionalFormatRule": {"index": index, "rule": {
            "ranges": ranges,
            "booleanRule": {
                "condition": {"type": "CUSTOM_FORMULA",
                              "values": [{"userEnteredValue": formula}]},
                "format": {"backgroundColor": colour},
            },
        }}}

    for index, ranges, formula, colour in [
        # Published first, so a finished row doesn't also light up amber.
        (0, [data],
         f'=AND({HAS_DATA},REGEXMATCH({ref("Status")}&"","^(Posted|Reposted)"))',
         GREEN),
        # Not an art party: kept so the mention is never re-parsed, but
        # greyed so it doesn't read as work waiting to be done.
        (1, [data], f'=OR(AND({HAS_DATA},{ref("Status")}="Not an event"),'
                    f'AND({HAS_DATA},{ref("Archive")}=TRUE))', SKIPPED),
        (2, [column("Tag")], f'=AND({AWAITING},{ref("Tag")}="")', AMBER),
        (3, [column("Date")], f'=AND({AWAITING},{ref("Date")}="")', AMBER),
        (4, [column("Start")], f'=AND({AWAITING},{ref("Start")}="")', AMBER),
        # No timezone means publish can't place the event on the calendar.
        (5, [column("Zone")], f'=AND({AWAITING},{ref("Zone")}="")', AMBER),
        # Empty, or lacking the comma-separated shape of an FFXIV address.
        (6, [column("Address")],
         f'=AND({AWAITING},OR({ref("Address")}="",'
         f'LEN({ref("Address")})-LEN(SUBSTITUTE({ref("Address")},",",""))<3))',
         AMBER),
        (7, [column("Check")], f'=AND({AWAITING},{ref("Check")}<>"")', AMBER),
    ]:
        requests.append(rule(index, ranges, formula, colour))

    # Rows already holding data get the taller height. New rows size
    # themselves as intake.py writes them, so there's no tall empty tail.
    existing = len(sheet.read_rows(svc))
    if existing:
        requests.append({"updateDimensionProperties": {
            "range": {"sheetId": sid, "dimension": "ROWS",
                      "startIndex": 1, "endIndex": 1 + existing},
            "properties": {"pixelSize": ROW_HEIGHT},
            "fields": "pixelSize",
        }})

    svc.spreadsheets().batchUpdate(
        spreadsheetId=sheet.spreadsheet_id(), body={"requests": requests},
    ).execute()

    print(f"'{sheet.TAB}' styled: {n} columns, checkboxes in A, "
          f"{existing} data row(s) resized.")
    print(f"https://docs.google.com/spreadsheets/d/{sheet.spreadsheet_id()}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
