#!/usr/bin/env python3
"""
Upsert Item Pack Master from filled Inv - {Store} rows.

Workbook: DayClose-Killbuck Marathon (default INVOICE_WORKBOOK_ID)
Master tab: "Item Pack Master"
Match key: UPC only (leading zeros preserved; RAW writes)

Source rows must have non-empty UPC + Extracted Qty.
Optional harvest: Cost per Unit, SSP per Unit (last-seen reference; prices move).
  - Cost per Unit: prefer Inv col; else Cost per Pack ÷ Extracted Qty
  - SSP per Unit: prefer Inv col; else SSP per Pack (Superior SSP is usually already unit-level)

On UPC conflict (different Extracted Qty): keep existing master value,
append CONFLICT note, do not overwrite gold ext unless --force-ext.
--force-prices overwrites Cost/SSP per Unit from newest harvest.

Usage:
  ./venv/bin/python scripts/upsert_item_pack_master.py
  ./venv/bin/python scripts/upsert_item_pack_master.py --tabs "Inv - Killbuck"
  ./venv/bin/python scripts/upsert_item_pack_master.py --dry-run
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
    "UPC",
    "Extracted Qty",
    "Unit Name",
    "Cost per Unit",
    "SSP per Unit",
    "SSP Store",
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
    s = (raw or "").strip().replace(",", "")
    if not s:
        return None
    try:
        v = float(s)
    except ValueError:
        return None
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(v)


def _fnum(raw: Any) -> Optional[float]:
    s = str(raw or "").strip().replace(",", "")
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
    return f"{v:.10f}".rstrip("0").rstrip(".")


def _ensure_master(sh: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        ws = sh.worksheet(MASTER_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=MASTER_TAB, rows=2000, cols=len(HEADERS))
    values = ws.get_all_values() or []
    if not values or [c.strip() for c in values[0]] != HEADERS:
        # Preserve existing data under old header if present
        old_rows = values[1:] if values else []
        old_h = [c.strip() for c in values[0]] if values else []
        ws.clear()
        if old_rows and old_h:
            oc = {h: i for i, h in enumerate(old_h)}
            migrated: List[List[str]] = []
            for r in old_rows:
                rr = r + [""] * 20

                def get(name: str, default: str = "") -> str:
                    i = oc.get(name)
                    if i is None or i >= len(rr):
                        return default
                    return str(rr[i]).strip()

                migrated.append(
                    [
                        get("UPC"),
                        get("Extracted Qty"),
                        get("Unit Name"),
                        get("Cost per Unit"),
                        get("SSP per Unit"),
                        get("SSP Store"),
                        get("Description"),
                        get("Pack Size Example"),
                        get("Vendor Example"),
                        get("Source"),
                        get("Active") or "TRUE",
                        get("Notes"),
                        get("Updated At"),
                        get("Hit Count") or "0",
                    ]
                )
            ws.update(values=[HEADERS] + migrated, range_name="A1", value_input_option="RAW")
        else:
            ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
        try:
            ws.freeze(rows=1)
        except Exception:
            pass
    return ws


def _load_master(ws: gspread.Worksheet) -> Tuple[List[str], Dict[str, Dict[str, Any]]]:
    values = ws.get_all_values() or []
    if not values:
        return HEADERS, {}
    header = values[0]
    col = {h: i for i, h in enumerate(header)}

    def cell(rr: List[str], name: str, default: str = "") -> str:
        i = col.get(name)
        if i is None or i >= len(rr):
            return default
        return str(rr[i]).strip()

    by_upc: Dict[str, Dict[str, Any]] = OrderedDict()
    for r in values[1:]:
        rr = r + [""] * len(HEADERS)
        upc = cell(rr, "UPC")
        if not upc:
            continue
        hit_raw = cell(rr, "Hit Count", "0")
        try:
            hit = int(float(hit_raw or 0))
        except ValueError:
            hit = 0
        by_upc[upc] = {
            "upc": upc,
            "ext": cell(rr, "Extracted Qty"),
            "unit": cell(rr, "Unit Name"),
            "cpu": cell(rr, "Cost per Unit"),
            "sspu": cell(rr, "SSP per Unit"),
            "ssp_store": cell(rr, "SSP Store"),
            "desc": cell(rr, "Description"),
            "pack": cell(rr, "Pack Size Example"),
            "vendor": cell(rr, "Vendor Example"),
            "source": cell(rr, "Source") or "manual",
            "active": cell(rr, "Active") or "TRUE",
            "notes": cell(rr, "Notes"),
            "updated": cell(rr, "Updated At"),
            "hit": hit,
        }
    return header, by_upc


def _inv_tabs(sh: gspread.Spreadsheet, only: Optional[Sequence[str]]) -> List[gspread.Worksheet]:
    if only:
        return [sh.worksheet(name.strip()) for name in only]
    return [ws for ws in sh.worksheets() if ws.title.startswith("Inv - ")]


def _harvest_tab(ws: gspread.Worksheet) -> List[Dict[str, str]]:
    values = ws.get_all_values() or []
    if not values:
        return []
    header = [h.strip() for h in values[0]]
    col = {h: i for i, h in enumerate(header)}
    if "UPC" not in col or "Extracted Qty" not in col:
        return []

    def cell(rr: List[str], name: str) -> str:
        i = col.get(name)
        if i is None or i >= len(rr):
            return ""
        return str(rr[i]).strip()

    # Option C: SSP Store from Inv tab name
    tab = ws.title
    store = tab[6:].strip() if tab.lower().startswith("inv - ") else tab

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
                "upc": upc,
                "ext": ext,
                "cpu": _fmt_money(cpu_n),
                "sspu": _fmt_money(sspu_n),
                "ssp_store": store if _fmt_money(sspu_n) else "",
                "desc": cell(rr, "Description"),
                "pack": cell(rr, "Pack Size"),
                "vendor": cell(rr, "Vendor"),
                "tab": ws.title,
            }
        )
    return found


def upsert(
    *,
    sheet_id: str,
    tabs: Optional[Sequence[str]],
    dry_run: bool,
    force_ext: bool,
    force_prices: bool,
    source_label: str,
) -> int:
    gc = _client()
    sh = gc.open_by_key(sheet_id)
    master_ws = _ensure_master(sh)
    _, by_upc = _load_master(master_ws)

    harvested: List[Dict[str, str]] = []
    for inv_ws in _inv_tabs(sh, tabs):
        rows = _harvest_tab(inv_ws)
        print(f"harvest {inv_ws.title}: {len(rows)} rows with UPC+Extracted Qty")
        harvested.extend(rows)

    if not harvested:
        print("nothing to upsert")
        return 0

    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    added = updated_meta = conflicts = 0

    for h in harvested:
        upc = h["upc"]
        if upc not in by_upc:
            by_upc[upc] = {
                "upc": upc,
                "ext": h["ext"],
                "unit": "",
                "cpu": h.get("cpu") or "",
                "sspu": h.get("sspu") or "",
                "ssp_store": h.get("ssp_store") or "",
                "desc": h["desc"],
                "pack": h["pack"],
                "vendor": h["vendor"],
                "source": source_label,
                "active": "TRUE",
                "notes": "",
                "updated": now,
                "hit": 1,
            }
            added += 1
            continue

        cur = by_upc[upc]
        cur["hit"] = int(cur.get("hit") or 0) + 1
        cur["updated"] = now
        if h["desc"]:
            cur["desc"] = h["desc"]
        if h["pack"]:
            cur["pack"] = h["pack"]
        if h["vendor"]:
            cur["vendor"] = h["vendor"]

        # prices: fill blanks always; overwrite only with --force-prices
        # SSP store-scoped (Option C): only write ssp when store matches or master ssp empty
        if h.get("cpu"):
            if force_prices or not str(cur.get("cpu") or "").strip():
                cur["cpu"] = h["cpu"]
        if h.get("sspu"):
            cur_store = str(cur.get("ssp_store") or "").strip()
            new_store = str(h.get("ssp_store") or "").strip()
            if force_prices:
                cur["sspu"] = h["sspu"]
                if new_store:
                    cur["ssp_store"] = new_store
            elif not str(cur.get("sspu") or "").strip():
                cur["sspu"] = h["sspu"]
                if new_store:
                    cur["ssp_store"] = new_store
            elif new_store and cur_store.casefold() == new_store.casefold():
                # same store may refresh blank only already handled; keep value unless force
                if not cur_store and new_store:
                    cur["ssp_store"] = new_store
            elif new_store and not cur_store:
                cur["ssp_store"] = new_store
            # else different store owns SSP — leave alone

        cur_ext = _norm_ext(str(cur.get("ext") or "")) or str(cur.get("ext") or "").strip()
        if cur_ext != h["ext"]:
            conflicts += 1
            note = f"CONFLICT saw ext {h['ext']} on {h['tab']}; kept {cur_ext};"
            prev_notes = (cur.get("notes") or "").strip()
            if note not in prev_notes:
                cur["notes"] = (prev_notes + " " + note).strip()
            if force_ext:
                cur["ext"] = h["ext"]
                cur["source"] = source_label + "+force_ext"
                cur["notes"] = (cur["notes"] + f" FORCE set ext={h['ext']};").strip()
        else:
            updated_meta += 1
            if not cur.get("source"):
                cur["source"] = source_label

    out_rows: List[List[str]] = []
    for _upc, d in by_upc.items():
        out_rows.append(
            [
                d["upc"],
                str(d.get("ext") or ""),
                str(d.get("unit") or ""),
                str(d.get("cpu") or ""),
                str(d.get("sspu") or ""),
                str(d.get("ssp_store") or ""),
                str(d.get("desc") or ""),
                str(d.get("pack") or ""),
                str(d.get("vendor") or ""),
                str(d.get("source") or source_label),
                str(d.get("active") or "TRUE"),
                str(d.get("notes") or ""),
                str(d.get("updated") or now),
                str(int(d.get("hit") or 0)),
            ]
        )

    print(
        f"master size={len(out_rows)} added={added} meta_touch≈{updated_meta} "
        f"conflicts={conflicts} dry_run={dry_run}"
    )
    if dry_run:
        return 0

    values = master_ws.get_all_values() or []
    if len(values) > 1:
        master_ws.delete_rows(2, len(values))
    if out_rows:
        master_ws.append_rows(out_rows, value_input_option="RAW")
    print(f"wrote {len(out_rows)} rows → tab '{MASTER_TAB}'")
    return 0


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Upsert Item Pack Master from Inv tabs")
    p.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    p.add_argument(
        "--tabs",
        default="",
        help='Comma-separated Inv tabs (default: all tabs starting with "Inv - ")',
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--force-ext",
        action="store_true",
        help="On UPC conflict, overwrite Extracted Qty with newest harvest value",
    )
    p.add_argument(
        "--force-prices",
        action="store_true",
        help="Overwrite Cost/SSP per Unit even when master already has values",
    )
    p.add_argument("--source", default="upsert_from_inv")
    args = p.parse_args(list(argv) if argv is not None else None)
    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()] or None
    return upsert(
        sheet_id=args.sheet_id,
        tabs=tabs,
        dry_run=args.dry_run,
        force_ext=args.force_ext,
        force_prices=args.force_prices,
        source_label=args.source,
    )


if __name__ == "__main__":
    sys.exit(main())
