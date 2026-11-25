"""Google Sheets access for Manikandan Bhaktha Jana Sabha.

How it works (read this once, it keeps the rest simple):

* The spreadsheet has two tabs, "Credit" and "Debit". Row 1 = title/summary,
  row 2 = headers, transactions start at row 3, columns A-D only.
* Every transaction written by this app gets *row-level developer metadata*
  (key "sabha_tx", value {"id": <uuid>, "ts": <UTC timestamp>}). Developer
  metadata is invisible in the sheet, and Google moves it together with the
  row, so the visible A-D layout never changes.
* Every write is ONE spreadsheets.batchUpdate call. Google applies a batch
  atomically (all requests succeed or none do). That includes Credit <-> Debit
  transfers: "write into the other tab" + "remove from this tab" happen in the
  same call, so a failed transfer can never lose or duplicate a transaction.
* Only cell VALUES are written (fields="userEnteredValue"), never formatting.
  Text is stored as literal strings, so a name like "=SUM(1)" cannot become a
  formula.
* A single process-wide lock serialises every spreadsheet operation.
"""

import json
import logging
import os
import re
import threading
from contextlib import contextmanager
from datetime import datetime, timezone, timedelta

import google_auth_httplib2
import httplib2
from google.auth.exceptions import GoogleAuthError
from google.oauth2 import service_account
from googleapiclient.discovery import build
from googleapiclient.errors import HttpError

log = logging.getLogger("sabha.sheets")

SCOPES = ["https://www.googleapis.com/auth/spreadsheets"]
FIRST_ROW = 3                      # first transaction row (1-based)
TABS = {"credit": "Credit", "debit": "Debit"}
META_KEY = "sabha_tx"
RECENT_LIMIT = 10
LOCK_TIMEOUT = 40                  # seconds to wait for another operation


class SheetsError(Exception):
    """An error whose message is safe to show to the administrator."""

    def __init__(self, message, status=500, duplicate=False):
        super().__init__(message)
        self.message = message
        self.status = status
        self.duplicate = duplicate


# --------------------------------------------------------------------------
# Connection
# --------------------------------------------------------------------------
_lock = threading.Lock()
_service = None


@contextmanager
def _locked():
    if not _lock.acquire(timeout=LOCK_TIMEOUT):
        raise SheetsError("The server is busy with another entry. Please try again in a moment.", 503)
    try:
        yield
    finally:
        _lock.release()


def _spreadsheet_id():
    raw = os.environ.get("SPREADSHEET_ID", "").strip()
    if not raw:
        raise SheetsError("The server is missing its spreadsheet configuration.", 500)
    match = re.search(r"/d/([A-Za-z0-9_-]+)", raw)   # allow a pasted full URL
    return match.group(1) if match else raw


def _svc():
    global _service
    if _service is None:
        raw = os.environ.get("GOOGLE_SERVICE_ACCOUNT_JSON", "").strip()
        path = os.environ.get("GOOGLE_APPLICATION_CREDENTIALS", "").strip()
        if not raw and not path:
            raise SheetsError("The server is missing its Google credentials.", 500)
        try:
            if raw:
                creds = service_account.Credentials.from_service_account_info(
                    json.loads(raw, strict=False), scopes=SCOPES)
            else:
                creds = service_account.Credentials.from_service_account_file(path, scopes=SCOPES)
            http = google_auth_httplib2.AuthorizedHttp(creds, http=httplib2.Http(timeout=25))
            _service = build("sheets", "v4", http=http, cache_discovery=False)
        except Exception:
            log.exception("Could not load Google credentials")
            raise SheetsError("The Google credentials on the server are invalid.", 500)
    return _service


def _run(request, retries=0):
    """Execute a Google API request and translate failures into SheetsError."""
    try:
        return request.execute(num_retries=retries)
    except HttpError as e:
        status = getattr(e.resp, "status", 500)
        log.error("Google Sheets API error %s: %s", status, e)
        if status == 403:
            raise SheetsError("Google denied access to the spreadsheet. Share it with the service "
                              "account email as an Editor and make sure the Sheets API is enabled.", 502)
        if status == 404:
            raise SheetsError("The spreadsheet was not found. Check the spreadsheet ID and sharing.", 502)
        if status == 429:
            raise SheetsError("Google Sheets is busy right now. Please wait a moment and try again.", 503)
        if status == 400:
            raise SheetsError("Google Sheets rejected the request. Nothing was changed.", 502)
        raise SheetsError("Google Sheets had a problem. Nothing was changed; please try again.", 502)
    except GoogleAuthError:
        log.exception("Google authentication failed")
        raise SheetsError("Could not authenticate with Google. Check the service account credentials.", 502)
    except (httplib2.HttpLib2Error, OSError):
        log.exception("Network error talking to Google")
        raise SheetsError("Could not reach Google Sheets. Check the connection and try again.", 503)


def _props():
    """Fresh tab info: sheetId, title, row count, frozen rows for Credit and Debit."""
    meta = _run(_svc().spreadsheets().get(
        spreadsheetId=_spreadsheet_id(),
        fields="sheets.properties(sheetId,title,gridProperties(rowCount,frozenRowCount))"), retries=2)
    by_title = {s["properties"]["title"]: s["properties"] for s in meta.get("sheets", [])}
    out = {}
    for key, title in TABS.items():
        p = by_title.get(title)
        if p is None:
            raise SheetsError(f'The spreadsheet has no tab named exactly "{title}".', 500)
        grid = p.get("gridProperties", {})
        out[key] = {"id": p.get("sheetId", 0), "title": title,
                    "rows": grid.get("rowCount", 1000), "frozen": grid.get("frozenRowCount", 0)}
    return out


# --------------------------------------------------------------------------
# Small helpers
# --------------------------------------------------------------------------
def _now():
    return datetime.now(timezone.utc).isoformat(timespec="microseconds")

IST = timezone(timedelta(hours=5, minutes=30))

def _ist(ts):
    """Convert our stored UTC timestamp to 'dd-mm-yyyy hh:mm:ss' in IST."""
    return datetime.fromisoformat(ts).astimezone(IST).strftime("%d-%m-%Y %H:%M:%S")


def _is_formula(v):
    return isinstance(v, str) and v.startswith("=")


def _occupied(row):
    """A row holds a transaction if Name/Amount/Description has anything in it."""
    return any(str(v).strip() != "" for v in row[1:4])


def _last_index(rows):
    for i in range(len(rows) - 1, -1, -1):
        if _occupied(rows[i]):
            return i
    return -1


def _read_rows(title, render):
    res = _run(_svc().spreadsheets().values().get(
        spreadsheetId=_spreadsheet_id(), range=f"'{title}'!A{FIRST_ROW}:E",
        valueRenderOption=render), retries=2)
    return [(list(r) + [""] * 5)[:5] for r in res.get("values", [])]


def _all_meta(props):
    """Every tracked transaction: {tab, row, id, ts, meta_id}."""
    tab_by_sheet = {p["id"]: k for k, p in props.items()}
    body = {"dataFilters": [{"developerMetadataLookup": {
        "metadataKey": META_KEY, "locationType": "ROW"}}]}
    res = _run(_svc().spreadsheets().developerMetadata().search(
        spreadsheetId=_spreadsheet_id(), body=body), retries=2)
    out = []
    for item in res.get("matchedDeveloperMetadata", []):
        md = item.get("developerMetadata", {})
        loc = md.get("location", {}).get("dimensionRange")
        if not loc:
            continue
        tab = tab_by_sheet.get(loc.get("sheetId", 0))
        row = loc.get("startIndex", 0) + 1
        try:
            val = json.loads(md.get("metadataValue", ""))
            tx_id, ts = val["id"], val["ts"]
        except (ValueError, KeyError, TypeError):
            continue
        if tab and row >= FIRST_ROW:
            out.append({"tab": tab, "row": row, "id": tx_id, "ts": ts, "meta_id": md.get("metadataId")})
    return out


def _recent_entries(props, metas, limit=RECENT_LIMIT):
    """The newest `limit` transactions (by submission time), with their cell values."""
    # look at a few extra candidates in case someone cleared a row by hand
    cands = sorted(metas, key=lambda m: m["ts"], reverse=True)[:limit + 20]
    if not cands:
        return []
    ranges = [f"'{props[m['tab']]['title']}'!A{m['row']}:E{m['row']}" for m in cands]
    res = _run(_svc().spreadsheets().values().batchGet(
        spreadsheetId=_spreadsheet_id(), ranges=ranges,
        valueRenderOption="UNFORMATTED_VALUE"), retries=2)
    out = []
    for m, vr in zip(cands, res.get("valueRanges", [])):
        row = ((vr.get("values") or [[]])[0] + [""] * 5)[:5]
        if not _occupied(row):
            continue
        out.append({"id": m["id"], "type": m["tab"], "no": row[0], "name": str(row[1]),
                    "amount": row[2], "description": str(row[3]), "timestamp": str(row[4]),
                    "ts": m["ts"],
                    "_tab": m["tab"], "_row": m["row"], "_meta_id": m["meta_id"]})
        if len(out) == limit:
            break
    return out


def _cell(v):
    if v is None or v == "":
        return {}                                   # with fields=userEnteredValue => clears the cell
    if isinstance(v, (int, float)):
        return {"userEnteredValue": {"numberValue": v}}
    return {"userEnteredValue": {"stringValue": str(v)}}   # literal text, never a formula


def _write_req(sheet_id, row, serial, tx, include_a):
    cells = [_cell(tx["name"]), _cell(tx["amount"]), _cell(tx["description"])]
    if tx.get("stamp"):
        cells.append(_cell(tx["stamp"]))          # column E: date/time typed by the administrator
    if include_a:
        cells.insert(0, _cell(serial))
    return {"updateCells": {
        "rows": [{"values": cells}],
        "fields": "userEnteredValue",
        "start": {"sheetId": sheet_id, "rowIndex": row - 1, "columnIndex": 0 if include_a else 1}}}


def _meta_req(sheet_id, row, tx_id, ts):
    return {"createDeveloperMetadata": {"developerMetadata": {
        "metadataKey": META_KEY,
        "metadataValue": json.dumps({"id": tx_id, "ts": ts}),
        "visibility": "DOCUMENT",
        "location": {"dimensionRange": {"sheetId": sheet_id, "dimension": "ROWS",
                                        "startIndex": row - 1, "endIndex": row}}}}}


def _append_requests(p, rows, metas, key, tx, tx_id, ts):
    """Requests that put a transaction in the first free row of a tab."""
    row = FIRST_ROW + _last_index(rows) + 1          # dynamic, never hardcoded
    reqs = []
    if row > p["rows"]:
        reqs.append({"appendDimension": {"sheetId": p["id"], "dimension": "ROWS", "length": row - p["rows"]}})
    for m in metas:                                   # stale metadata on an emptied row
        if m["tab"] == key and m["row"] == row:
            reqs.append({"deleteDeveloperMetadata": {"dataFilter": {
                "developerMetadataLookup": {"metadataId": m["meta_id"]}}}})
    idx = row - FIRST_ROW
    include_a = not (idx < len(rows) and _is_formula(rows[idx][0]))   # never overwrite a formula
    reqs.append(_write_req(p["id"], row, row - FIRST_ROW + 1, tx, include_a))
    reqs.append(_meta_req(p["id"], row, tx_id, ts))
    return reqs, row


def _remove_requests(p, row, meta_id):
    """Requests that remove one row. Returns (requests, rows_shifted_up)."""
    if p["rows"] - 1 > p["frozen"]:
        return [{"deleteDimension": {"range": {"sheetId": p["id"], "dimension": "ROWS",
                                               "startIndex": row - 1, "endIndex": row}}}], True
    # Tiny grid: Google refuses to delete the last non-frozen row, so just empty it.
    return [{"updateCells": {"rows": [{"values": [{}, {}, {}, {}]}], "fields": "userEnteredValue",
                             "start": {"sheetId": p["id"], "rowIndex": row - 1, "columnIndex": 1}}},
            {"deleteDeveloperMetadata": {"dataFilter": {
                "developerMetadataLookup": {"metadataId": meta_id}}}}], False


def _renumber_requests(sheet_id, rows, start_row):
    """Rewrite column A (No.) from start_row downwards; formulas in A are left alone."""
    reqs, seg_start, seg = [], None, []

    def flush():
        nonlocal seg_start, seg
        if seg:
            reqs.append({"updateCells": {"rows": seg, "fields": "userEnteredValue",
                                         "start": {"sheetId": sheet_id, "rowIndex": seg_start - 1,
                                                   "columnIndex": 0}}})
        seg_start, seg = None, []

    for i in range(max(start_row - FIRST_ROW, 0), len(rows)):
        r = FIRST_ROW + i
        if not _occupied(rows[i]) or _is_formula(rows[i][0]):
            flush()
            continue
        if seg_start is None:
            seg_start = r
        seg.append({"values": [{"userEnteredValue": {"numberValue": r - FIRST_ROW + 1}}]})
    flush()
    return reqs


def _batch(reqs):
    # No automatic retry on writes: a retry after a lost response could duplicate a row.
    return _run(_svc().spreadsheets().batchUpdate(
        spreadsheetId=_spreadsheet_id(), body={"requests": reqs}))


def _public(e):
    return {k: v for k, v in e.items() if not k.startswith("_")}


# --------------------------------------------------------------------------
# Public operations
# --------------------------------------------------------------------------
def list_recent():
    with _locked():
        props = _props()
        return [_public(e) for e in _recent_entries(props, _all_meta(props))]


def add_transaction(tx_type, name, amount, description, tx_id, stamp=""):
    """Append a transaction to the Credit or Debit tab. tx_id doubles as a duplicate-submit guard."""
    tx = {"name": name, "amount": amount, "description": description, "stamp": stamp}
    with _locked():
        props = _props()
        metas = _all_meta(props)
        if any(m["id"] == tx_id for m in metas):
            raise SheetsError("This entry was already recorded.", 409, duplicate=True)
        rows = _read_rows(props[tx_type]["title"], "FORMULA")
        reqs, row = _append_requests(props[tx_type], rows, metas, tx_type, tx, tx_id, _now())
        _batch(reqs)
    return {"id": tx_id, "type": tx_type, "no": row - FIRST_ROW + 1}


def update_transaction(tx_id, tx_type, name, amount, description, stamp=""):
    """Edit one of the 10 most recent transactions; moves it between tabs if the type changed."""
    tx = {"name": name, "amount": amount, "description": description, "stamp": stamp}
    with _locked():
        props = _props()
        metas = _all_meta(props)
        target = next((m for m in metas if m["id"] == tx_id), None)
        if target is None:
            raise SheetsError("That transaction could not be found. It may have been removed.", 404)
        recent_ids = {e["id"] for e in _recent_entries(props, metas)}
        if tx_id not in recent_ids:
            raise SheetsError("Only the 10 most recent transactions can be edited.", 403)

        src_key, src_row = target["tab"], target["row"]

        # --- same type: update the existing row in place -----------------------------
        if tx_type == src_key:
            _batch([_write_req(props[src_key]["id"], src_row, None, tx, include_a=False)])
            return {"id": tx_id, "type": src_key, "no": src_row - FIRST_ROW + 1}

        # --- type changed: move to the other tab in ONE atomic batch ------------------
        dst_key = tx_type
        src_p, dst_p = props[src_key], props[dst_key]
        src_rows = _read_rows(src_p["title"], "FORMULA")
        dst_rows = _read_rows(dst_p["title"], "FORMULA")

        reqs, dst_row = _append_requests(dst_p, dst_rows, metas, dst_key, tx, tx_id, target["ts"])
        remove, shifted = _remove_requests(src_p, src_row, target["meta_id"])
        reqs += remove
        if shifted:
            after = src_rows[:src_row - FIRST_ROW] + src_rows[src_row - FIRST_ROW + 1:]
            reqs += _renumber_requests(src_p["id"], after, src_row)
        _batch(reqs)      # all-or-nothing: no lost rows, no duplicates, no rollback needed
        return {"id": tx_id, "type": dst_key, "no": dst_row - FIRST_ROW + 1}

def delete_transaction(tx_id):
    """Delete one of the 10 most recent transactions and renumber the rows below it."""
    with _locked():
        props = _props()
        metas = _all_meta(props)
        target = next((m for m in metas if m["id"] == tx_id), None)
        if target is None:
            raise SheetsError("That transaction could not be found. It may have been removed.", 404)
        if tx_id not in {e["id"] for e in _recent_entries(props, metas)}:
            raise SheetsError("Only the 10 most recent transactions can be deleted.", 403)

        p, row = props[target["tab"]], target["row"]
        rows = _read_rows(p["title"], "FORMULA")
        reqs, shifted = _remove_requests(p, row, target["meta_id"])
        if shifted:
            after = rows[:row - FIRST_ROW] + rows[row - FIRST_ROW + 1:]
            reqs += _renumber_requests(p["id"], after, row)
        _batch(reqs)      # one all-or-nothing call: delete row + renumber
    return {"id": tx_id}


# --------------------------------------------------------------------------
# `python sheets.py` = quick connection check (Phase 1)
# --------------------------------------------------------------------------
if __name__ == "__main__":
    from dotenv import load_dotenv
    load_dotenv()
    logging.basicConfig(level=logging.INFO)
    try:
        info = _props()
        print("Connected to the spreadsheet.")
        for key, p in info.items():
            rows = _read_rows(p["title"], "UNFORMATTED_VALUE")
            print(f'  Tab "{p["title"]}": found, {_last_index(rows) + 1} transaction row(s)')
    except SheetsError as e:
        print("FAILED:", e.message)
