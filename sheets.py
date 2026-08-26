"""
Google Sheets integration for InvUpload.

Master Mapping (Google Sheet tab or local JSON):
  Store Name → Spreadsheet ID [→ optional Tab name]

Each store can have:
  - its own Google Spreadsheet, or
  - a dedicated tab inside a shared spreadsheet (works without Drive API create).

Append failures are logged and never raised to the upload path.
"""

from __future__ import annotations

import json
import logging
import os
import threading
import time
from dataclasses import dataclass
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

logger = logging.getLogger(__name__)

EASTERN = ZoneInfo("America/New_York")
BASE_DIR = Path(__file__).resolve().parent

DEFAULT_CREDS = Path(
    os.getenv("GOOGLE_CREDENTIALS", str(BASE_DIR / "google-credentials.json"))
)
STORE_SHEETS_FILE = Path(os.getenv("STORE_SHEETS_FILE", str(BASE_DIR / "store_sheets.json")))

# Existing ops workbook used by dayclose / invoice log (Sheets API only — no Drive create needed)
DEFAULT_WORKBOOK_ID = os.getenv(
    "INVOICE_WORKBOOK_ID",
    "1dcZufZoV7whkLiSzOkM8qarUXetrIr_KHBqusfWmb1M",
)
MASTER_MAPPING_SHEET_ID = os.getenv("MASTER_MAPPING_SHEET_ID", DEFAULT_WORKBOOK_ID).strip()
MASTER_MAPPING_TAB = os.getenv("MASTER_MAPPING_TAB", "Master Mapping")
DEFAULT_INVOICE_TAB = os.getenv("INVOICE_SHEET_TAB", "Invoices")

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)

INVOICE_HEADERS: List[str] = [
    "Timestamp",
    "Store",
    "Invoice Number",
    "Invoice Date",
    "UPC",
    "Description",
    "Pack Size",
    "Qty (Cases)",
    "Cost per Pack",
    "Cost per Unit",
    "SSP per Pack",
    "SSP per Unit",
    "Amount",
    "OCR Confidence",
    "Needs Review",
    "Vendor",  # display name from OCR registry (appended — keeps legacy rows aligned)
    # Optional trailing qty fields (Item Pack Master / ticket UNITS) — never strip if already on tab
    "Calculated Qty",
    "Extracted Qty",
]

_cache_lock = threading.Lock()
_map_cache: Dict[str, "StoreTarget"] = {}
_cache_loaded_at = 0.0
_CACHE_TTL_SEC = 300.0
_client: Optional[gspread.Client] = None
_client_lock = threading.Lock()


@dataclass(frozen=True)
class StoreTarget:
    store: str
    spreadsheet_id: str
    tab: str  # worksheet title inside that spreadsheet


class SheetsError(Exception):
    pass


def _get_client() -> gspread.Client:
    global _client
    with _client_lock:
        if _client is not None:
            return _client
        creds_path = Path(os.getenv("GOOGLE_CREDENTIALS", str(DEFAULT_CREDS)))
        if not creds_path.is_file():
            raise SheetsError(f"Google credentials not found: {creds_path}")
        creds = Credentials.from_service_account_file(str(creds_path), scopes=list(SCOPES))
        _client = gspread.authorize(creds)
        return _client


def _norm(name: str) -> str:
    return " ".join((name or "").strip().split())


def _load_local_file() -> Dict[str, Any]:
    if not STORE_SHEETS_FILE.is_file():
        return {}
    try:
        data = json.loads(STORE_SHEETS_FILE.read_text(encoding="utf-8"))
    except Exception as e:
        logger.error("Failed reading %s: %s", STORE_SHEETS_FILE, e)
        return {}
    return data if isinstance(data, dict) else {}


def save_local_mapping(targets: Mapping[str, StoreTarget], *, master_id: str = "") -> None:
    """Persist mapping locally (gitignored). Supports sheet_id + tab per store."""
    payload: Dict[str, Any] = {
        "_master_sheet_id": master_id or MASTER_MAPPING_SHEET_ID,
        "stores": {
            t.store: {"sheet_id": t.spreadsheet_id, "tab": t.tab}
            for t in targets.values()
        },
    }
    STORE_SHEETS_FILE.write_text(json.dumps(payload, indent=2, ensure_ascii=False) + "\n", encoding="utf-8")
    try:
        os.chmod(STORE_SHEETS_FILE, 0o600)
    except OSError:
        pass


def _parse_local_targets(data: Dict[str, Any]) -> Dict[str, StoreTarget]:
    out: Dict[str, StoreTarget] = {}
    if not data:
        return out

    # Format A: { "Killbuck": "SHEET_ID" }
    # Format B: { "Killbuck": {"sheet_id": "...", "tab": "Inv - Killbuck"} }
    # Format C: { "stores": { ... }, "_master_sheet_id": "..." }
    stores_obj = data.get("stores")
    if not isinstance(stores_obj, dict):
        stores_obj = data

    for k, v in stores_obj.items():
        if str(k).startswith("_"):
            continue
        store = _norm(str(k))
        if not store:
            continue
        if isinstance(v, str) and v.strip():
            out[store] = StoreTarget(store=store, spreadsheet_id=v.strip(), tab=DEFAULT_INVOICE_TAB)
        elif isinstance(v, dict):
            sid = str(v.get("sheet_id") or v.get("spreadsheet_id") or v.get("id") or "").strip()
            tab = str(v.get("tab") or v.get("worksheet") or DEFAULT_INVOICE_TAB).strip() or DEFAULT_INVOICE_TAB
            if sid:
                out[store] = StoreTarget(store=store, spreadsheet_id=sid, tab=tab)
    return out


def _load_master_mapping_from_sheet() -> Dict[str, StoreTarget]:
    sheet_id = (MASTER_MAPPING_SHEET_ID or DEFAULT_WORKBOOK_ID).strip()
    if not sheet_id:
        return {}

    client = _get_client()
    sh = client.open_by_key(sheet_id)
    try:
        ws = sh.worksheet(MASTER_MAPPING_TAB)
    except gspread.exceptions.WorksheetNotFound as e:
        raise SheetsError(f"Master Mapping tab '{MASTER_MAPPING_TAB}' missing in {sheet_id}") from e

    rows = ws.get_all_values()
    if not rows:
        return {}

    header = [c.strip().lower() for c in rows[0]]

    def col(*names: str) -> Optional[int]:
        for n in names:
            if n in header:
                return header.index(n)
        return None

    store_i = col("store", "store name", "name")
    id_i = col("sheet id", "spreadsheet id", "sheet_id", "google sheet id", "id")
    tab_i = col("tab", "worksheet", "sheet tab", "tab name")

    # default positions if headers missing
    if store_i is None:
        store_i = 0
    if id_i is None:
        id_i = 1

    out: Dict[str, StoreTarget] = {}
    for row in rows[1:]:
        if len(row) <= max(store_i, id_i):
            continue
        store = _norm(row[store_i])
        sid = (row[id_i] or "").strip()
        tab = DEFAULT_INVOICE_TAB
        if tab_i is not None and len(row) > tab_i and row[tab_i].strip():
            tab = row[tab_i].strip()
        if store and sid and not store.startswith("#"):
            out[store] = StoreTarget(store=store, spreadsheet_id=sid, tab=tab)
    return out


def refresh_store_map(*, force: bool = False) -> Dict[str, StoreTarget]:
    global _map_cache, _cache_loaded_at
    now = time.time()
    with _cache_lock:
        if not force and _map_cache and (now - _cache_loaded_at) < _CACHE_TTL_SEC:
            return dict(_map_cache)

    mapping: Dict[str, StoreTarget] = {}
    try:
        mapping = _load_master_mapping_from_sheet()
        if mapping:
            logger.info("Sheets: loaded %d store targets from Master Mapping", len(mapping))
    except Exception as e:
        logger.warning("Sheets: Master Mapping unavailable (%s); trying local file", e)

    if not mapping:
        mapping = _parse_local_targets(_load_local_file())
        if mapping:
            logger.info("Sheets: loaded %d store targets from %s", len(mapping), STORE_SHEETS_FILE.name)

    with _cache_lock:
        _map_cache = dict(mapping)
        _cache_loaded_at = now
    return dict(mapping)


def get_target_for_store(store: str) -> Optional[StoreTarget]:
    store_n = _norm(store)
    m = refresh_store_map()
    if store_n in m:
        return m[store_n]
    lower = {k.lower(): v for k, v in m.items()}
    return lower.get(store_n.lower())


def _ensure_headers(ws: gspread.Worksheet) -> None:
    existing = ws.row_values(1)
    if not existing:
        ws.append_row(INVOICE_HEADERS, value_input_option="RAW")
        return
    normalized = [c.strip() for c in existing]
    if normalized == INVOICE_HEADERS:
        return
    # Safe upgrade: same leading columns (or prefix) → rewrite header row only.
    # Never drop trailing optional cols the tab already has beyond our contract.
    lead = ["Timestamp", "Store", "Invoice Number", "Invoice Date"]
    if normalized[:4] != lead:
        logger.warning(
            "Sheets: header mismatch on '%s' (left as-is). Expected %s got %s",
            ws.title,
            INVOICE_HEADERS,
            normalized,
        )
        return
    # If tab already has our full header or a longer compatible prefix, only append missing tails.
    if normalized[: len(INVOICE_HEADERS)] == INVOICE_HEADERS:
        return
    # Build target = INVOICE_HEADERS, then any extra existing columns after that stay.
    extras = []
    if len(normalized) > len(INVOICE_HEADERS):
        extras = normalized[len(INVOICE_HEADERS) :]
    # If existing is a prefix of new headers (e.g. 16-col before Calculated/Extracted), upgrade.
    base16 = INVOICE_HEADERS[:16]  # through Vendor
    if normalized == base16 or normalized[:16] == base16:
        target = list(INVOICE_HEADERS) + [c for c in extras if c and c not in INVOICE_HEADERS]
        try:
            ws.update(range_name="A1", values=[target], value_input_option="RAW")
            logger.info(
                "Sheets: upgraded header on '%s' → %d cols", ws.title, len(target)
            )
            return
        except Exception as e:
            logger.warning("Sheets: header upgrade failed on '%s': %s", ws.title, e)
            return
    # Partial match: if Vendor present and missing qty tails, append header cells only.
    if "Vendor" in normalized:
        missing = [h for h in ("Calculated Qty", "Extracted Qty") if h not in normalized]
        if missing:
            start_col = len(normalized) + 1
            try:
                # Column index → A1 without gspread.utils (keep deps thin)
                def _col_a1(c: int) -> str:
                    s = ""
                    while c:
                        c, r = divmod(c - 1, 26)
                        s = chr(65 + r) + s
                    return s

                cell = f"{_col_a1(start_col)}1"
                ws.update(range_name=cell, values=[missing], value_input_option="RAW")
                logger.info(
                    "Sheets: appended header cols %s on '%s'", missing, ws.title
                )
                return
            except Exception as e:
                logger.warning("Sheets: append header cols failed on '%s': %s", ws.title, e)
                return
    logger.warning(
        "Sheets: header mismatch on '%s' (left as-is). Expected %s got %s",
        ws.title,
        INVOICE_HEADERS,
        normalized,
    )


def _open_invoice_ws(target: StoreTarget) -> gspread.Worksheet:
    client = _get_client()
    sh = client.open_by_key(target.spreadsheet_id)
    try:
        return sh.worksheet(target.tab)
    except gspread.exceptions.WorksheetNotFound:
        return sh.add_worksheet(title=target.tab, rows=2000, cols=len(INVOICE_HEADERS) + 2)


def build_invoice_rows(
    *,
    store: str,
    invoice_number: str,
    invoice_date: str,
    timestamp: Optional[str] = None,
    line_items: Optional[Sequence[Mapping[str, Any]]] = None,
    vendor: str = "",
) -> List[List[Any]]:
    """Build sheet rows. One row per line item when provided; includes Vendor name."""
    ts = timestamp or datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    store_n = _norm(store)
    inv_no_default = (invoice_number or "").strip()
    inv_date = (invoice_date or "").strip()
    vendor_default = (vendor or "").strip()

    def one(item: Optional[Mapping[str, Any]] = None) -> List[Any]:
        item = item or {}
        g = item.get
        conf = g("ocr_confidence", g("confidence", ""))
        needs = g("needs_review", g("Needs Review", ""))
        if needs == "" and conf != "":
            try:
                needs = int(conf) < 70
            except (TypeError, ValueError):
                needs = ""
        if isinstance(needs, bool):
            needs = "TRUE" if needs else "FALSE"
        inv_no = str(g("invoice_number") or g("Invoice Number") or inv_no_default).strip()
        vendor_name = str(
            g("vendor")
            or g("Vendor")
            or g("vendor_display")
            or g("vendor_name")
            or vendor_default
            or ""
        ).strip()
        return [
            ts,
            store_n,
            inv_no,
            inv_date,
            g("upc", g("UPC", "")) or "",
            g("description", g("Description", "")) or "",
            g("pack_size", g("Pack Size", "")) or "",
            g("qty_cases", g("Qty (Cases)", g("qty", ""))) or "",
            g("cost_per_pack", g("Cost per Pack", "")) or "",
            g("cost_per_unit", g("Cost per Unit", "")) or "",
            g("ssp_per_pack", g("SSP per Pack", "")) or "",
            g("ssp_per_unit", g("SSP per Unit", "")) or "",
            g("amount", g("Amount", "")) or "",
            conf if conf != "" else "",
            needs,
            vendor_name,
            g("calculated_qty", g("Calculated Qty", g("units", ""))) or "",
            g("extracted_qty", g("Extracted Qty", "")) or "",
        ]

    if line_items:
        return [one(it) for it in line_items]
    return [one()]


def append_invoice_to_store_sheet(
    *,
    store: str,
    invoice_number: str,
    invoice_date: str,
    timestamp: Optional[str] = None,
    line_items: Optional[Sequence[Mapping[str, Any]]] = None,
    vendor: str = "",
) -> Dict[str, Any]:
    """Append invoice row(s) for store. Never raises."""
    store_n = _norm(store)
    try:
        target = get_target_for_store(store_n)
        if not target:
            msg = f"No Google Sheet mapped for store '{store_n}'"
            logger.error("Sheets: %s", msg)
            return {"ok": False, "error": msg, "store": store_n}

        ws = _open_invoice_ws(target)
        _ensure_headers(ws)
        rows = build_invoice_rows(
            store=store_n,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            timestamp=timestamp,
            line_items=line_items,
            vendor=vendor,
        )
        if len(rows) > 1 and hasattr(ws, "append_rows"):
            # RAW keeps leading zeros on ITEM#/UPC (USER_ENTERED turns "00503" → 503)
            ws.append_rows(rows, value_input_option="RAW")
        else:
            for r in rows:
                ws.append_row(r, value_input_option="RAW")

        logger.info(
            "Sheets: appended %d row(s) store=%s sheet=%s tab=%s inv=%s vendor=%s",
            len(rows),
            store_n,
            target.spreadsheet_id,
            target.tab,
            invoice_number,
            (vendor or "")[:40],
        )
        return {
            "ok": True,
            "store": store_n,
            "sheet_id": target.spreadsheet_id,
            "tab": target.tab,
            "rows": len(rows),
        }
    except Exception as e:
        logger.exception("Sheets append failed for store=%s: %s", store_n, e)
        return {"ok": False, "error": str(e), "store": store_n}


def replace_invoice_rows_for_store(
    *,
    store: str,
    match_invoice_numbers: Sequence[str],
    invoice_number: str,
    invoice_date: str,
    timestamp: Optional[str] = None,
    line_items: Optional[Sequence[Mapping[str, Any]]] = None,
    vendor: str = "",
) -> Dict[str, Any]:
    """
    Delete existing sheet rows whose Invoice Number is in match_invoice_numbers,
    then append line_items. Used to correct a bad OCR write without manual cleanup.
    Never raises.
    """
    store_n = _norm(store)
    match_set = {str(x).strip() for x in (match_invoice_numbers or []) if str(x).strip()}
    if not match_set:
        return {"ok": False, "error": "match_invoice_numbers empty", "store": store_n}
    try:
        target = get_target_for_store(store_n)
        if not target:
            msg = f"No Google Sheet mapped for store '{store_n}'"
            logger.error("Sheets: %s", msg)
            return {"ok": False, "error": msg, "store": store_n}

        ws = _open_invoice_ws(target)
        _ensure_headers(ws)
        values = ws.get_all_values() or []
        if not values:
            deleted = 0
        else:
            header = [str(h).strip().lower() for h in values[0]]
            try:
                inv_col = header.index("invoice number")
            except ValueError:
                inv_col = 2  # default position in INVOICE_HEADERS
            # 1-based sheet row numbers to delete (skip header row 1)
            to_delete = []
            for idx, row in enumerate(values[1:], start=2):
                inv = str(row[inv_col]).strip() if inv_col < len(row) else ""
                if inv in match_set:
                    to_delete.append(idx)
            # delete bottom-up so indices stay valid
            for row_num in reversed(to_delete):
                ws.delete_rows(row_num)
            deleted = len(to_delete)
            logger.info(
                "Sheets: deleted %d row(s) store=%s tab=%s inv_match=%s",
                deleted,
                store_n,
                target.tab,
                sorted(match_set),
            )

        # Prefer explicit vendor arg; else first line_item vendor fields
        vendor_name = (vendor or "").strip()
        if not vendor_name and line_items:
            first = line_items[0] if line_items else {}
            vendor_name = str(
                first.get("vendor")
                or first.get("Vendor")
                or first.get("vendor_display")
                or ""
            ).strip()

        append_res = append_invoice_to_store_sheet(
            store=store_n,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            timestamp=timestamp,
            line_items=line_items,
            vendor=vendor_name,
        )
        out = {
            "ok": bool(append_res.get("ok")),
            "store": store_n,
            "sheet_id": target.spreadsheet_id,
            "tab": target.tab,
            "deleted": deleted,
            "rows": append_res.get("rows", 0),
            "match_invoice_numbers": sorted(match_set),
        }
        if not append_res.get("ok"):
            out["error"] = append_res.get("error")
            out["ok"] = False
        return out
    except Exception as e:
        logger.exception("Sheets replace failed for store=%s: %s", store_n, e)
        return {"ok": False, "error": str(e), "store": store_n}


def bootstrap_master_and_store_tabs(
    stores: Sequence[str],
    *,
    workbook_id: str = DEFAULT_WORKBOOK_ID,
    share_with: Optional[str] = None,
) -> Dict[str, StoreTarget]:
    """
    Ensure Master Mapping tab + one invoice tab per store on `workbook_id`.

    Uses Spreadsheets API only (add_worksheet). Suitable when Drive API is disabled
    and separate spreadsheet files cannot be created by the service account.

    Tab naming: "Inv - {Store}"
    """
    client = _get_client()
    sh = client.open_by_key(workbook_id)

    # Master Mapping tab
    try:
        master = sh.worksheet(MASTER_MAPPING_TAB)
    except gspread.exceptions.WorksheetNotFound:
        master = sh.add_worksheet(title=MASTER_MAPPING_TAB, rows=50, cols=4)
        logger.info("Sheets: created tab '%s'", MASTER_MAPPING_TAB)

    if not master.row_values(1):
        master.append_row(["Store", "Sheet ID", "Tab", "Notes"], value_input_option="RAW")

    targets: Dict[str, StoreTarget] = {}
    existing_tabs = {w.title for w in sh.worksheets()}

    for store in stores:
        store_n = _norm(store)
        tab = f"Inv - {store_n}"
        if tab not in existing_tabs:
            ws = sh.add_worksheet(title=tab, rows=2000, cols=len(INVOICE_HEADERS) + 2)
            ws.append_row(INVOICE_HEADERS, value_input_option="RAW")
            existing_tabs.add(tab)
            logger.info("Sheets: created store tab '%s'", tab)
        else:
            ws = sh.worksheet(tab)
            _ensure_headers(ws)

        targets[store_n] = StoreTarget(store=store_n, spreadsheet_id=workbook_id, tab=tab)

    # Rewrite master mapping body (keep header)
    body = [["Store", "Sheet ID", "Tab", "Notes"]]
    for store_n in sorted(targets.keys()):
        t = targets[store_n]
        body.append([t.store, t.spreadsheet_id, t.tab, "InvUpload per-store tab"])
    master.clear()
    master.update(range_name="A1", values=body, value_input_option="RAW")

    save_local_mapping(targets, master_id=workbook_id)

    # optional share — needs Drive API; ignore failures
    if share_with:
        try:
            sh.share(share_with, perm_type="user", role="writer", notify=False)
        except Exception as e:
            logger.warning("Sheets: could not share workbook with %s: %s", share_with, e)

    # bust cache
    refresh_store_map(force=True)
    return targets


def sheets_status() -> Dict[str, Any]:
    try:
        mapping = refresh_store_map()
        return {
            "enabled": True,
            "credentials": Path(os.getenv("GOOGLE_CREDENTIALS", str(DEFAULT_CREDS))).is_file(),
            "master_sheet_id": MASTER_MAPPING_SHEET_ID or DEFAULT_WORKBOOK_ID,
            "mapped_stores": len(mapping),
            "stores": {
                k: {"sheet_id": v.spreadsheet_id, "tab": v.tab}
                for k, v in sorted(mapping.items())
            },
        }
    except Exception as e:
        return {"enabled": True, "error": str(e)}
