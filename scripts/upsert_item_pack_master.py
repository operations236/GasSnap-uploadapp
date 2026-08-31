#!/usr/bin/env python3
"""
Upsert Item Pack Master from filled Inv - {Store} rows.

Match key: **Store + UPC** (one row per store per UPC).
Source rows: non-empty UPC + Extracted Qty on any Inv - {Store} tab.

Harvested:
  Extracted Qty (gold units/case)
  Cost per Unit (Inv col or Cost per Pack ÷ Extracted Qty)
  SSP per Unit (Inv SSP per Unit or SSP per Pack)

Calculated Qty is NOT stored on master (invoice-line: Qty×Extracted).

Usage:
  ./venv/bin/python scripts/upsert_item_pack_master.py --wipe --rebuild
  ./venv/bin/python scripts/upsert_item_pack_master.py --dry-run
  ./venv/bin/python scripts/upsert_item_pack_master.py --tabs "Inv - Killbuck,Inv - Parma"
"""
from __future__ import annotations

import argparse
import os
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

EASTERN = ZoneInfo("America/New_York")
DEFAULT_SHEET_ID = os.getenv(
    "INVOICE_WORKBOOK_ID", "1dcZufZoV7whkLiSzOkM8qarUXetrIr_KHBqusfWmb1M"
)
MASTER_TAB = "Item Pack Master"
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_CREDS = Path(
    os.getenv(
        "GOOGLE_CREDENTIALS",
        str(Path.home() / ".openclaw" / "google-credentials.json"),
    )
)
if not DEFAULT_CREDS.is_file():
    DEFAULT_CREDS = BASE_DIR / "google-credentials.json"

SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)

HEADERS = [
    "Store",
    "UPC",
    "Extracted Qty",
    "Unit Name",
    "Cost per Unit",
    "SSP per Unit",
    "Description",
    "Pack Size Example",
    "Vendor Example",
    "Source",
    "Active",
    "Notes",
    "Updated At",
    "Hit Count",
]


def _client() -> gspread.Client:
    if not DEFAULT_CREDS.is_file():
        raise SystemExit(f"Credentials not found: {DEFAULT_CREDS}")
    creds = Credentials.from_service_account_file(str(DEFAULT_CREDS), scopes=list(SCOPES))
    return gspread.authorize(creds)


def _norm_ext(raw: str) -> Optional[str]:
    s = str(raw or "").strip().replace(",", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(v)


def _fnum(raw: str) -> Optional[float]:
    s = str(raw or "").strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _fmt_money(v: Optional[float]) -> str:
    if v is None:
        return ""
    if abs(v - round(v, 2)) < 1e-9:
        return f"{v:.2f}"
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _store_from_tab(title: str) -> str:
    t = (title or "").strip()
    if t.lower().startswith("inv - "):
        return t[6:].strip()
    return t


def _row_key(store: str, upc: str) -> str:
    return f"{store.strip().casefold()}\x1f{upc.strip()}"


def _ensure_master(sh: gspread.Spreadsheet, *, wipe: bool) -> gspread.Worksheet:
    try:
        ws = sh.worksheet(MASTER_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=MASTER_TAB, rows=4000, cols=len(HEADERS))
        ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
        return ws

    values = ws.get_all_values() or []
    if wipe:
        if len(values) > 1:
            ws.delete_rows(2, len(values))
        ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
        return ws

    if not values:
        ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
        return ws

    header = [h.strip() for h in values[0]]
    if header != HEADERS:
        # migrate: rewrite header + keep what we can under new schema
        print(f"WARN: migrating header {header[:6]}… → Store+UPC schema")
        col = {h: i for i, h in enumerate(header)}
        migrated: List[List[str]] = []
        for r in values[1:]:
            rr = r + [""] * 28

            def get(*names: str) -> str:
                for n in names:
                    i = col.get(n)
                    if i is not None and i < len(rr) and str(rr[i]).strip():
                        return str(rr[i]).strip()
                return ""

            upc = get("UPC")
            if not upc:
                continue
            store = get("Store", "SSP Store")
            if not store:
                continue  # drop storeless legacy on migrate path without wipe
            migrated.append(
                [
                    store,
                    upc,
                    get("Extracted Qty"),
                    get("Unit Name"),
                    get("Cost per Unit"),
                    get("SSP per Unit"),
                    get("Description"),
                    get("Pack Size Example"),
                    get("Vendor Example"),
                    get("Source") or "migrated",
                    get("Active") or "TRUE",
                    get("Notes"),
                    get("Updated At"),
                    get("Hit Count") or "0",
                ]
            )
        if len(values) > 1:
            ws.delete_rows(2, len(values))
        ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
        if migrated:
            ws.append_rows(migrated, value_input_option="RAW")
        print(f"migrated kept {len(migrated)} store-tagged rows")
    return ws


def _load_master(ws: gspread.Worksheet) -> "OrderedDict[str, Dict[str, Any]]":
    values = ws.get_all_values() or []
    by: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    if not values:
        return by
    header = [h.strip() for h in values[0]]
    col = {h: i for i, h in enumerate(header)}

    def cell(rr: List[str], name: str, default: str = "") -> str:
        i = col.get(name)
        if i is None or i >= len(rr):
            return default
        return str(rr[i]).strip()

    for r in values[1:]:
        rr = r + [""] * 28
        store = cell(rr, "Store")
        upc = cell(rr, "UPC")
        if not store or not upc:
            continue
        try:
            hit = int(float(cell(rr, "Hit Count", "0") or 0))
        except ValueError:
            hit = 0
        k = _row_key(store, upc)
        by[k] = {
            "store": store,
            "upc": upc,
            "ext": cell(rr, "Extracted Qty"),
            "unit": cell(rr, "Unit Name"),
            "cpu": cell(rr, "Cost per Unit"),
            "sspu": cell(rr, "SSP per Unit"),
            "desc": cell(rr, "Description"),
            "pack": cell(rr, "Pack Size Example"),
            "vendor": cell(rr, "Vendor Example"),
            "source": cell(rr, "Source") or "manual",
            "active": cell(rr, "Active") or "TRUE",
            "notes": cell(rr, "Notes"),
            "updated": cell(rr, "Updated At"),
            "hit": hit,
        }
    return by


def _harvest_tab(ws: gspread.Worksheet) -> List[Dict[str, str]]:
    values = ws.get_all_values() or []
    if not values:
        return []
    header = [h.strip() for h in values[0]]
    col = {h: i for i, h in enumerate(header)}
    if "UPC" not in col or "Extracted Qty" not in col:
        return []

    store = _store_from_tab(ws.title)

    def cell(rr: List[str], name: str) -> str:
        i = col.get(name)
        if i is None or i >= len(rr):
            return ""
        return str(rr[i]).strip()

    found: List[Dict[str, str]] = []
    for r in values[1:]:
        rr = r + [""] * 32
        upc = cell(rr, "UPC")
        ext = _norm_ext(cell(rr, "Extracted Qty"))
        if not upc or ext is None:
            continue

        cpu_n = _fnum(cell(rr, "Cost per Unit"))
        sspu_n = _fnum(cell(rr, "SSP per Unit"))
        cpp_n = _fnum(cell(rr, "Cost per Pack"))
        ssp_p_n = _fnum(cell(rr, "SSP per Pack"))
        ext_n = float(ext)

        if cpu_n is None and cpp_n is not None and ext_n:
            cpu_n = cpp_n / ext_n
        if sspu_n is None and ssp_p_n is not None:
            sspu_n = ssp_p_n

        found.append(
            {
                "store": store,
                "upc": upc,
                "ext": ext,
                "cpu": _fmt_money(cpu_n),
                "sspu": _fmt_money(sspu_n),
                "desc": cell(rr, "Description"),
                "pack": cell(rr, "Pack Size"),
                "vendor": cell(rr, "Vendor"),
                "tab": ws.title,
            }
        )
    return found


def _inv_tabs(sh: gspread.Spreadsheet, only: Optional[Sequence[str]]) -> List[gspread.Worksheet]:
    tabs = [ws for ws in sh.worksheets() if ws.title.lower().startswith("inv - ")]
    if only:
        want = {t.strip().casefold() for t in only if t.strip()}
        tabs = [ws for ws in tabs if ws.title.strip().casefold() in want]
    return tabs


def apply_harvest(
    by: "OrderedDict[str, Dict[str, Any]]",
    harvest: List[Dict[str, str]],
    *,
    force_ext: bool,
    force_prices: bool,
    source_label: str,
) -> Dict[str, int]:
    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    st = {
        "added": 0,
        "ext_keep": 0,
        "ext_force": 0,
        "ext_conflict": 0,
        "cpu_fill": 0,
        "ssp_fill": 0,
        "meta": 0,
    }
    for h in harvest:
        store = h["store"]
        upc = h["upc"]
        k = _row_key(store, upc)
        if k not in by:
            by[k] = {
                "store": store,
                "upc": upc,
                "ext": h["ext"],
                "unit": "",
                "cpu": h.get("cpu") or "",
                "sspu": h.get("sspu") or "",
                "desc": h.get("desc") or "",
                "pack": h.get("pack") or "",
                "vendor": h.get("vendor") or "",
                "source": source_label,
                "active": "TRUE",
                "notes": "",
                "updated": now,
                "hit": 1,
            }
            st["added"] += 1
            continue

        cur = by[k]
        cur["hit"] = int(cur.get("hit") or 0) + 1
        cur["updated"] = now
        if h.get("desc"):
            cur["desc"] = h["desc"]
        if h.get("pack"):
            cur["pack"] = h["pack"]
        if h.get("vendor"):
            cur["vendor"] = h["vendor"]

        cur_ext = _norm_ext(str(cur.get("ext") or "")) or str(cur.get("ext") or "").strip()
        new_ext = h["ext"]
        if force_ext:
            cur["ext"] = new_ext
            st["ext_force"] += 1
        elif not cur_ext:
            cur["ext"] = new_ext
        elif cur_ext != new_ext:
            note = f"CONFLICT ext {cur_ext} vs {new_ext} from {h.get('tab')};"
            if note not in (cur.get("notes") or ""):
                cur["notes"] = ((cur.get("notes") or "") + " " + note).strip()
            st["ext_conflict"] += 1
        else:
            st["ext_keep"] += 1

        if h.get("cpu"):
            if force_prices or not str(cur.get("cpu") or "").strip():
                cur["cpu"] = h["cpu"]
                st["cpu_fill"] += 1
        if h.get("sspu"):
            if force_prices or not str(cur.get("sspu") or "").strip():
                cur["sspu"] = h["sspu"]
                st["ssp_fill"] += 1
        st["meta"] += 1
    return st


def write_master(ws: gspread.Worksheet, by: "OrderedDict[str, Dict[str, Any]]") -> int:
    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    out: List[List[str]] = []
    # stable sort: store then upc
    items = sorted(by.values(), key=lambda d: (str(d.get("store") or "").casefold(), str(d.get("upc") or "")))
    for d in items:
        out.append(
            [
                str(d.get("store") or ""),
                str(d.get("upc") or ""),
                str(d.get("ext") or ""),
                str(d.get("unit") or ""),
                str(d.get("cpu") or ""),
                str(d.get("sspu") or ""),
                str(d.get("desc") or ""),
                str(d.get("pack") or ""),
                str(d.get("vendor") or ""),
                str(d.get("source") or "upsert_from_inv"),
                str(d.get("active") or "TRUE"),
                str(d.get("notes") or ""),
                str(d.get("updated") or now),
                str(int(d.get("hit") or 0)),
            ]
        )
    values = ws.get_all_values() or []
    ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
    if len(values) > 1:
        ws.delete_rows(2, len(values))
    if out:
        ws.append_rows(out, value_input_option="RAW")
    return len(out)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Upsert Item Pack Master (Store+UPC) from Inv tabs")
    p.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    p.add_argument("--tabs", default="", help="Comma list e.g. 'Inv - Killbuck,Inv - Parma'")
    p.add_argument("--wipe", action="store_true", help="Clear master tab before rebuild")
    p.add_argument("--rebuild", action="store_true", help="Alias: same as harvest write (use with --wipe)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-ext", action="store_true")
    p.add_argument("--force-prices", action="store_true")
    p.add_argument("--source", default="upsert_from_inv")
    args = p.parse_args(list(argv) if argv is not None else None)

    only = [t.strip() for t in args.tabs.split(",") if t.strip()] or None
    gc = _client()
    sh = gc.open_by_key(args.sheet_id)
    ws = _ensure_master(sh, wipe=args.wipe)
    by = OrderedDict() if args.wipe else _load_master(ws)
    print(f"master before={len(by)} wipe={args.wipe}")

    tabs = _inv_tabs(sh, only)
    if not tabs:
        raise SystemExit("No Inv - * tabs found")
    harvest: List[Dict[str, str]] = []
    for t in tabs:
        part = _harvest_tab(t)
        print(f"  harvest {t.title}: {len(part)} rows with UPC+Extracted Qty")
        harvest.extend(part)
    print(f"harvest total={len(harvest)}")

    st = apply_harvest(
        by,
        harvest,
        force_ext=args.force_ext,
        force_prices=args.force_prices,
        source_label=args.source,
    )
    print(
        f"apply added={st['added']} ext_keep={st['ext_keep']} ext_force={st['ext_force']} "
        f"ext_conflict={st['ext_conflict']} cpu_fill={st['cpu_fill']} ssp_fill={st['ssp_fill']} "
        f"master_after={len(by)} dry_run={args.dry_run}"
    )
    # per-store counts
    from collections import Counter

    c = Counter(str(v.get("store") or "") for v in by.values())
    print("  by_store:", dict(c))

    if args.dry_run:
        return 0
    n = write_master(ws, by)
    print(f"wrote {n} rows → '{MASTER_TAB}'")
    try:
        sys.path.insert(0, str(BASE_DIR))
        import item_pack_master as ipm

        ipm.invalidate_cache()
        print("item_pack_master cache invalidated")
    except Exception as e:
        print(f"cache invalidate skip: {e}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
