#!/usr/bin/env python3
"""
Backfill blank Inv qty/unit columns from Item Pack Master (Store + UPC key).

Fills ONLY blank cells (never overwrites operator-filled values):
  - Extracted Qty  ← master Extracted Qty (units/case) for this store
  - Calculated Qty ← Qty (Cases) × Extracted Qty
  - Cost per Unit  ← Cost per Pack ÷ Extracted Qty (live), else master CPU
  - SSP per Pack / Unit ← master SSP per Unit for this store

Does NOT invent values on master miss.
Does NOT delete/replace whole rows — cell-level updates only.

Usage:
  ./venv/bin/python scripts/backfill_inv_qty_from_master.py --dry-run
  ./venv/bin/python scripts/backfill_inv_qty_from_master.py
  ./venv/bin/python scripts/backfill_inv_qty_from_master.py --tabs "Inv - Killbuck"
  ./venv/bin/python scripts/backfill_inv_qty_from_master.py --ssp-only --tabs "Inv - ARCO"
"""

from __future__ import annotations

import argparse
import os
import sys
import time
from collections import defaultdict
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence, Tuple

import gspread
from google.oauth2.service_account import Credentials
from gspread.utils import rowcol_to_a1

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

# Batch size for Sheets API value updates
BATCH_CHUNK = 400


def _client() -> gspread.Client:
    if not DEFAULT_CREDS.is_file():
        raise SystemExit(f"Credentials not found: {DEFAULT_CREDS}")
    creds = Credentials.from_service_account_file(str(DEFAULT_CREDS), scopes=list(SCOPES))
    return gspread.authorize(creds)


def _fnum(raw: Any) -> Optional[float]:
    s = str(raw or "").strip().replace(",", "").replace("$", "")
    if not s:
        return None
    try:
        return float(s)
    except ValueError:
        return None


def _norm_ext(raw: Any) -> Optional[str]:
    v = _fnum(raw)
    if v is None:
        return None
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(v)


def _fmt_money(v: float) -> str:
    if abs(v - round(v, 2)) < 1e-9:
        return f"{v:.2f}"
    return f"{v:.10f}".rstrip("0").rstrip(".")


def _fmt_qty(v: float) -> str:
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return f"{v:.6f}".rstrip("0").rstrip(".")


def _blank(s: Any) -> bool:
    return not str(s or "").strip()


def _load_master(sh: gspread.Spreadsheet) -> Dict[str, Dict[str, str]]:
    """key store\\x1fupc -> {ext, ssp, cpu, store, upc}. Active rows only."""
    try:
        ws = sh.worksheet(MASTER_TAB)
    except gspread.exceptions.WorksheetNotFound:
        raise SystemExit(f"Master tab missing: {MASTER_TAB!r}")
    values = ws.get_all_values() or []
    if not values:
        raise SystemExit("Item Pack Master is empty")
    header = [h.strip() for h in values[0]]
    col = {h: i for i, h in enumerate(header)}
    if "UPC" not in col:
        raise SystemExit(f"Master missing UPC column: {header}")
    by: Dict[str, Dict[str, str]] = {}
    skipped_inactive = 0
    skipped_nostore = 0
    for r in values[1:]:
        rr = r + [""] * 28
        upc = str(rr[col["UPC"]]).strip()
        if not upc:
            continue
        if "Active" in col:
            act = str(rr[col["Active"]]).strip().upper()
            if act in ("FALSE", "0", "N", "NO"):
                skipped_inactive += 1
                continue
        store = ""
        if "Store" in col:
            store = str(rr[col["Store"]]).strip()
        elif "SSP Store" in col:
            store = str(rr[col["SSP Store"]]).strip()
        if not store:
            skipped_nostore += 1
            continue
        ext = _norm_ext(rr[col["Extracted Qty"]]) if "Extracted Qty" in col else None
        cpu = ""
        if "Cost per Unit" in col:
            cv = _fnum(rr[col["Cost per Unit"]])
            if cv is not None:
                cpu = _fmt_money(cv)
        ssp = ""
        if "SSP per Unit" in col:
            sv = _fnum(rr[col["SSP per Unit"]])
            if sv is not None:
                ssp = _fmt_money(sv)
        if ext is None and not ssp and not cpu:
            continue
        k = f"{store.casefold()}\x1f{upc}"
        if k not in by:
            by[k] = {
                "ext": ext or "",
                "ssp": ssp,
                "cpu": cpu,
                "store": store,
                "upc": upc,
            }
        # also index digits-only upc
        import re

        dig = re.sub(r"\D", "", upc)
        if dig and dig != upc:
            kd = f"{store.casefold()}\x1f{dig}"
            if kd not in by:
                by[kd] = dict(by[k])
    n_ext = sum(1 for v in by.values() if v.get("ext"))
    n_ssp = sum(1 for v in by.values() if v.get("ssp"))
    stores = {v.get("store") for v in by.values()}
    print(
        f"master loaded: {len(by)} keys stores={sorted(stores)} ext={n_ext} ssp={n_ssp} "
        f"(skipped_inactive={skipped_inactive} nostore={skipped_nostore})"
    )
    return by


def _tab_store(ws_title: str) -> str:
    t = (ws_title or "").strip()
    if t.lower().startswith("inv - "):
        return t[6:].strip()
    return t


def _inv_tabs(sh: gspread.Spreadsheet, only: Optional[Sequence[str]]) -> List[gspread.Worksheet]:
    if only:
        return [sh.worksheet(name.strip()) for name in only]
    return [ws for ws in sh.worksheets() if ws.title.startswith("Inv - ")]


def _col_index(header: List[str], *names: str) -> Optional[int]:
    for n in names:
        if n in header:
            return header.index(n)
    return None


def _ensure_qty_headers(ws: gspread.Worksheet, header: List[str], dry_run: bool) -> List[str]:
    """Ensure Calculated Qty + Extracted Qty exist. Append at end if missing (before nothing else)."""
    h = list(header)
    changed = False
    for name in ("Calculated Qty", "Extracted Qty"):
        if name not in h:
            h.append(name)
            changed = True
            print(f"  header will add: {name!r} on {ws.title}")
    if changed and not dry_run:
        # Write full header row
        ws.update(values=[h], range_name="A1", value_input_option="RAW")
    return h


def backfill_tab(
    ws: gspread.Worksheet,
    master: Dict[str, Dict[str, str]],
    *,
    dry_run: bool,
    ssp_only: bool = False,
    qty_only: bool = False,
) -> Dict[str, int]:
    stats = defaultdict(int)
    values = ws.get_all_values() or []
    if not values:
        print(f"{ws.title}: empty")
        return dict(stats)

    header = [c.strip() for c in values[0]]
    do_qty = not ssp_only
    do_ssp = not qty_only
    if do_qty:
        header = _ensure_qty_headers(ws, header, dry_run=dry_run)

    tab_store = _tab_store(ws.title)
    tab_store_cf = tab_store.casefold()

    i_upc = _col_index(header, "UPC")
    i_cases = _col_index(header, "Qty (Cases)", "Qty")
    i_cpp = _col_index(header, "Cost per Pack")
    i_cpu = _col_index(header, "Cost per Unit")
    i_ssp_p = _col_index(header, "SSP per Pack")
    i_ssp_u = _col_index(header, "SSP per Unit")
    i_calc = _col_index(header, "Calculated Qty")
    i_ext = _col_index(header, "Extracted Qty")
    i_vendor = _col_index(header, "Vendor")
    i_inv = _col_index(header, "Invoice Number")
    i_desc = _col_index(header, "Description")

    if i_upc is None:
        print(f"{ws.title}: no UPC column — skip")
        stats["skip_no_upc_col"] = 1
        return dict(stats)
    if do_qty and (i_ext is None or i_calc is None):
        print(f"{ws.title}: missing Extracted/Calculated cols after ensure — skip qty")
        do_qty = False
        stats["skip_no_qty_cols"] = 1
        if not do_ssp:
            return dict(stats)

    updates: List[Dict[str, Any]] = []
    samples_fill: List[str] = []
    samples_miss: List[str] = []

    for r_i, r in enumerate(values[1:], start=2):  # 1-based sheet row
        rr = list(r) + [""] * (len(header) - len(r) + 4)
        upc = str(rr[i_upc]).strip() if i_upc < len(rr) else ""
        if not upc:
            stats["skip_no_upc"] += 1
            continue

        stats["rows_with_upc"] += 1
        mrow = master.get(f"{tab_store_cf}\x1f{upc}")
        if mrow is None:
            # digit-only fallback
            import re as _re

            dig = _re.sub(r"\D", "", upc)
            if dig and dig != upc:
                mrow = master.get(f"{tab_store_cf}\x1f{dig}")
        if mrow is None:
            stats["master_miss"] += 1
            if len(samples_miss) < 8:
                vend = rr[i_vendor] if i_vendor is not None else ""
                invn = rr[i_inv] if i_inv is not None else ""
                desc = (rr[i_desc] if i_desc is not None else "")[:28]
                samples_miss.append(f"  miss row{r_i} upc={upc} inv={invn} {vend} {desc}")
            continue

        stats["master_hit"] += 1
        ext_m = mrow.get("ext") or ""
        ssp_m = mrow.get("ssp") or ""
        # Store already baked into master key — no cross-store SSP bleed
        row_did_fill = False
        effective_ext_s = ext_m
        effective_ext = float(ext_m) if ext_m else 0.0

        # --- qty path (needs master Extracted Qty) ---
        if do_qty and ext_m and i_ext is not None and i_calc is not None:
            cur_ext = rr[i_ext] if i_ext < len(rr) else ""
            if _blank(cur_ext):
                stats["fill_extracted"] += 1
                updates.append(
                    {"range": rowcol_to_a1(r_i, i_ext + 1), "values": [[ext_m]]}
                )
                effective_ext = float(ext_m)
                effective_ext_s = ext_m
                row_did_fill = True
            else:
                stats["keep_extracted"] += 1
                existing = _fnum(cur_ext)
                if existing is not None and existing > 0:
                    effective_ext = existing
                    effective_ext_s = _norm_ext(cur_ext) or str(existing)
                else:
                    effective_ext = float(ext_m)
                    effective_ext_s = ext_m

            cur_calc = rr[i_calc] if i_calc < len(rr) else ""
            if _blank(cur_calc):
                cases = _fnum(rr[i_cases]) if i_cases is not None else None
                if cases is not None and effective_ext:
                    calc_s = _fmt_qty(cases * effective_ext)
                    stats["fill_calculated"] += 1
                    updates.append(
                        {"range": rowcol_to_a1(r_i, i_calc + 1), "values": [[calc_s]]}
                    )
                    row_did_fill = True
                else:
                    stats["skip_calc_no_cases"] += 1
            else:
                stats["keep_calculated"] += 1

            if i_cpu is not None:
                cur_cpu = rr[i_cpu] if i_cpu < len(rr) else ""
                if _blank(cur_cpu):
                    cpp = _fnum(rr[i_cpp]) if i_cpp is not None else None
                    cpu_s = ""
                    if cpp is not None and effective_ext:
                        cpu_s = _fmt_money(cpp / effective_ext)
                    if not cpu_s:
                        cpu_s = mrow.get("cpu") or ""
                    if cpu_s:
                        stats["fill_cost_per_unit"] += 1
                        updates.append(
                            {"range": rowcol_to_a1(r_i, i_cpu + 1), "values": [[cpu_s]]}
                        )
                        row_did_fill = True
                    else:
                        stats["skip_cpu_no_pack"] += 1
                else:
                    stats["keep_cost_per_unit"] += 1
        elif do_qty and not ext_m:
            stats["skip_qty_no_master_ext"] += 1

        # --- SSP path (master SSP per Unit → pack + unit blanks) ---
        if do_ssp and ssp_m:
            if i_ssp_p is not None:
                cur_sp = rr[i_ssp_p] if i_ssp_p < len(rr) else ""
                if _blank(cur_sp):
                    stats["fill_ssp_pack"] += 1
                    updates.append(
                        {"range": rowcol_to_a1(r_i, i_ssp_p + 1), "values": [[ssp_m]]}
                    )
                    row_did_fill = True
                else:
                    stats["keep_ssp_pack"] += 1
            if i_ssp_u is not None:
                cur_su = rr[i_ssp_u] if i_ssp_u < len(rr) else ""
                if _blank(cur_su):
                    # Prefer pack if already filled (same-row promote); else master
                    fill_su = ssp_m
                    if i_ssp_p is not None:
                        sp_now = (
                            ssp_m
                            if _blank(rr[i_ssp_p] if i_ssp_p < len(rr) else "")
                            else str(rr[i_ssp_p]).strip()
                        )
                        # After queued pack fill, use master value for unit too
                        if not _blank(rr[i_ssp_p] if i_ssp_p < len(rr) else ""):
                            fill_su = str(rr[i_ssp_p]).strip() or ssp_m
                        else:
                            fill_su = ssp_m
                    stats["fill_ssp_unit"] += 1
                    updates.append(
                        {"range": rowcol_to_a1(r_i, i_ssp_u + 1), "values": [[fill_su]]}
                    )
                    row_did_fill = True
                else:
                    stats["keep_ssp_unit"] += 1
        elif do_ssp and not ssp_m:
            stats["skip_ssp_no_master"] += 1

        if row_did_fill and len(samples_fill) < 6:
            vend = rr[i_vendor] if i_vendor is not None else ""
            invn = rr[i_inv] if i_inv is not None else ""
            samples_fill.append(
                f"  fill row{r_i} upc={upc} ext={effective_ext_s or '-'} "
                f"ssp={ssp_m or '-'} inv={invn} {vend}"
            )

    print(f"\n=== {ws.title} (store={tab_store!r}) ===")
    print(f"  rows_with_upc={stats['rows_with_upc']} hit={stats['master_hit']} miss={stats['master_miss']}")
    print(
        f"  fill extracted={stats['fill_extracted']} calculated={stats['fill_calculated']} "
        f"cpu={stats['fill_cost_per_unit']} ssp_p={stats['fill_ssp_pack']} ssp_u={stats['fill_ssp_unit']}"
    )
    print(
        f"  keep extracted={stats['keep_extracted']} calculated={stats['keep_calculated']} "
        f"cpu={stats['keep_cost_per_unit']} ssp_p={stats['keep_ssp_pack']} ssp_u={stats['keep_ssp_unit']}"
    )
    print(
        f"  skip calc_no_cases={stats['skip_calc_no_cases']} cpu_no_pack={stats['skip_cpu_no_pack']} "
        f"no_upc_row={stats['skip_no_upc']} qty_no_ext={stats['skip_qty_no_master_ext']} "
        f"ssp_no_m={stats['skip_ssp_no_master']}"
    )
    print(f"  cell updates queued={len(updates)} dry_run={dry_run}")
    if samples_fill:
        print("  sample fills:")
        for s in samples_fill:
            print(s)
    if samples_miss:
        print("  sample misses:")
        for s in samples_miss:
            print(s)

    if dry_run or not updates:
        stats["updates_queued"] = len(updates)
        return dict(stats)

    for i in range(0, len(updates), BATCH_CHUNK):
        chunk = updates[i : i + BATCH_CHUNK]
        ws.batch_update(chunk, value_input_option="RAW")
        time.sleep(0.5)
    stats["updates_written"] = len(updates)
    print(f"  wrote {len(updates)} cells")
    return dict(stats)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Backfill blank Inv qty/SSP cols from Item Pack Master")
    p.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    p.add_argument(
        "--tabs",
        default="",
        help='Comma-separated Inv tabs (default: all "Inv - *")',
    )
    p.add_argument("--dry-run", action="store_true")
    p.add_argument(
        "--ssp-only",
        action="store_true",
        help="Only fill blank SSP per Pack / SSP per Unit",
    )
    p.add_argument(
        "--qty-only",
        action="store_true",
        help="Only fill Extracted/Calculated/Cost per Unit (skip SSP)",
    )
    args = p.parse_args(list(argv) if argv is not None else None)
    tabs = [t.strip() for t in args.tabs.split(",") if t.strip()] or None
    if args.ssp_only and args.qty_only:
        raise SystemExit("use only one of --ssp-only / --qty-only")

    gc = _client()
    sh = gc.open_by_key(args.sheet_id)
    master = _load_master(sh)
    invs = _inv_tabs(sh, tabs)
    print(f"tabs: {[w.title for w in invs]}")
    mode = "DRY-RUN" if args.dry_run else "WRITE blanks-only"
    if args.ssp_only:
        mode += " ssp-only"
    elif args.qty_only:
        mode += " qty-only"
    print(f"mode: {mode}")

    totals: Dict[str, int] = defaultdict(int)
    for ws in invs:
        st = backfill_tab(
            ws,
            master,
            dry_run=args.dry_run,
            ssp_only=args.ssp_only,
            qty_only=args.qty_only,
        )
        for k, v in st.items():
            totals[k] += int(v)

    print("\n========== TOTALS ==========")
    for k in sorted(totals):
        print(f"  {k}={totals[k]}")
    print(f"dry_run={args.dry_run}")
    return 0


if __name__ == "__main__":
    sys.exit(main())
