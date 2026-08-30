#!/usr/bin/env python3
"""
Seed Item Pack Master SSP from Ohio Beverage dual layouts.

Layout A (SSP, often no UPC): ITEM# + SSP
Layout B (UPC, no SSP):       ITEM# + UPC
Join on OBD ITEM# → master row UPC + SSP per Unit (last-seen).

Does NOT invent Extracted Qty (leave blank unless --pack-as-ext and PACK is whole).
Does NOT hardcode prices in app code — writes the shared master tab only.

Default samples (ARCO):
  A: uploads/20260822_190933_7df92c1520.json  inv 244557
  B: uploads/20260826_182318_49148774c1.json  inv 253838

Usage:
  ./venv/bin/python scripts/seed_obd_ssp_from_layouts.py --dry-run
  ./venv/bin/python scripts/seed_obd_ssp_from_layouts.py
  ./venv/bin/python scripts/seed_obd_ssp_from_layouts.py --force-prices
"""

from __future__ import annotations

import argparse
import json
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
BASE_DIR = Path(__file__).resolve().parent.parent
DEFAULT_SHEET_ID = os.getenv(
    "INVOICE_WORKBOOK_ID", "1dcZufZoV7whkLiSzOkM8qarUXetrIr_KHBqusfWmb1M"
)
MASTER_TAB = "Item Pack Master"
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
    "Description",
    "Pack Size Example",
    "Vendor Example",
    "Source",
    "Active",
    "Notes",
    "Updated At",
    "Hit Count",
]

DEFAULT_A = BASE_DIR / "uploads" / "20260822_190933_7df92c1520.json"
DEFAULT_B = BASE_DIR / "uploads" / "20260826_182318_49148774c1.json"
VENDOR = "Ohio Beverage Distributing"
SOURCE = "obd_layout_ab_bridge"


def _client() -> gspread.Client:
    if not DEFAULT_CREDS.is_file():
        raise SystemExit(f"Credentials not found: {DEFAULT_CREDS}")
    creds = Credentials.from_service_account_file(str(DEFAULT_CREDS), scopes=list(SCOPES))
    return gspread.authorize(creds)


def _fmt_ssp(raw: Any) -> str:
    s = str(raw or "").strip().replace(",", "").replace("$", "")
    if not s:
        return ""
    try:
        v = float(s)
    except ValueError:
        return s
    if abs(v - round(v, 2)) < 1e-9:
        return f"{v:.2f}"
    return f"{v:.4f}".rstrip("0").rstrip(".")


def _load_items(path: Path) -> List[Dict[str, Any]]:
    data = json.loads(path.read_text(encoding="utf-8"))
    ocr = data.get("ocr") or {}
    ext = ocr.get("extraction") or data.get("extraction") or {}
    items = ext.get("line_items") or []
    return [it for it in items if isinstance(it, dict)]


def _by_item_code(items: List[Dict[str, Any]]) -> Dict[str, Dict[str, Any]]:
    out: Dict[str, Dict[str, Any]] = {}
    for it in items:
        code = str(it.get("item_code") or "").strip()
        if code:
            out[code] = it
    return out


def _bridge(
    path_a: Path,
    path_b: Path,
) -> Tuple[List[Dict[str, str]], Dict[str, int]]:
    la = _load_items(path_a)
    lb = _load_items(path_b)
    A = _by_item_code(la)
    B = _by_item_code(lb)
    stats = {
        "a_lines": len(la),
        "b_lines": len(lb),
        "a_codes": len(A),
        "b_codes": len(B),
        "overlap": 0,
        "seedable": 0,
        "skip_no_upc": 0,
        "skip_no_ssp": 0,
    }
    seeds: List[Dict[str, str]] = []
    both = sorted(set(A) & set(B))
    stats["overlap"] = len(both)
    for code in both:
        ia, ib = A[code], B[code]
        upc = str(ib.get("upc_raw") or ib.get("upc") or "").strip()
        ssp = _fmt_ssp(ia.get("ssp_per_pack") or ia.get("ssp_per_unit") or "")
        if not upc:
            stats["skip_no_upc"] += 1
            continue
        if not ssp:
            stats["skip_no_ssp"] += 1
            continue
        desc = str(ib.get("description") or ia.get("description") or "").strip()
        pack = str(ib.get("pack_size") or ia.get("pack_size") or "").strip()
        seeds.append(
            {
                "upc": upc,
                "sspu": ssp,
                "desc": desc,
                "pack": pack,
                "item_code": code,
                "vendor": VENDOR,
            }
        )
        stats["seedable"] += 1
    stats["b_only"] = len(set(B) - set(A))
    stats["a_only"] = len(set(A) - set(B))
    return seeds, stats


def _ensure_master(sh: gspread.Spreadsheet) -> gspread.Worksheet:
    try:
        ws = sh.worksheet(MASTER_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=MASTER_TAB, rows=2000, cols=len(HEADERS))
        ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
        return ws
    values = ws.get_all_values() or []
    if not values:
        ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
    elif [c.strip() for c in values[0]] != HEADERS:
        # Do not wipe — expect upsert script already normalized headers
        print(f"WARN: master header differs: {values[0][:8]}... (proceeding by name)")
    return ws


def _load_master(ws: gspread.Worksheet) -> Dict[str, Dict[str, Any]]:
    values = ws.get_all_values() or []
    if not values:
        return OrderedDict()
    header = [h.strip() for h in values[0]]
    col = {h: i for i, h in enumerate(header)}

    def cell(rr: List[str], name: str, default: str = "") -> str:
        i = col.get(name)
        if i is None or i >= len(rr):
            return default
        return str(rr[i]).strip()

    by: Dict[str, Dict[str, Any]] = OrderedDict()
    for r in values[1:]:
        rr = r + [""] * 20
        upc = cell(rr, "UPC")
        if not upc:
            continue
        try:
            hit = int(float(cell(rr, "Hit Count", "0") or 0))
        except ValueError:
            hit = 0
        by[upc] = {
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


def apply_seeds(
    by_upc: Dict[str, Dict[str, Any]],
    seeds: List[Dict[str, str]],
    *,
    force_prices: bool,
    source: str,
) -> Dict[str, int]:
    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    st = {"added": 0, "ssp_fill": 0, "ssp_keep": 0, "ssp_force": 0, "meta": 0}
    for s in seeds:
        upc = s["upc"]
        ssp = s["sspu"]
        note_bit = f"OBD ITEM#{s.get('item_code')};"
        if upc not in by_upc:
            by_upc[upc] = {
                "upc": upc,
                "ext": "",
                "unit": "",
                "cpu": "",
                "sspu": ssp,
                "desc": s.get("desc") or "",
                "pack": s.get("pack") or "",
                "vendor": s.get("vendor") or VENDOR,
                "source": source,
                "active": "TRUE",
                "notes": note_bit,
                "updated": now,
                "hit": 1,
            }
            st["added"] += 1
            st["ssp_fill"] += 1
            continue
        cur = by_upc[upc]
        cur["hit"] = int(cur.get("hit") or 0) + 1
        cur["updated"] = now
        if s.get("desc"):
            cur["desc"] = s["desc"]
        if s.get("pack"):
            cur["pack"] = s["pack"]
        if s.get("vendor"):
            cur["vendor"] = s["vendor"]
        prev_notes = (cur.get("notes") or "").strip()
        if note_bit not in prev_notes:
            cur["notes"] = (prev_notes + " " + note_bit).strip()
        cur_ssp = _fmt_ssp(cur.get("sspu") or "")
        if force_prices:
            cur["sspu"] = ssp
            st["ssp_force"] += 1
            cur["source"] = source + "+force_prices"
        elif not cur_ssp:
            cur["sspu"] = ssp
            st["ssp_fill"] += 1
            if not cur.get("source") or cur.get("source") == "manual":
                cur["source"] = source
        else:
            st["ssp_keep"] += 1
        st["meta"] += 1
    return st


def write_master(ws: gspread.Worksheet, by_upc: Dict[str, Dict[str, Any]], source: str) -> int:
    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    out_rows: List[List[str]] = []
    for _u, d in by_upc.items():
        out_rows.append(
            [
                d["upc"],
                str(d.get("ext") or ""),
                str(d.get("unit") or ""),
                str(d.get("cpu") or ""),
                str(d.get("sspu") or ""),
                str(d.get("desc") or ""),
                str(d.get("pack") or ""),
                str(d.get("vendor") or ""),
                str(d.get("source") or source),
                str(d.get("active") or "TRUE"),
                str(d.get("notes") or ""),
                str(d.get("updated") or now),
                str(int(d.get("hit") or 0)),
            ]
        )
    values = ws.get_all_values() or []
    # Ensure header
    if not values or [c.strip() for c in values[0]] != HEADERS:
        ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")
        values = [HEADERS]
    if len(values) > 1:
        ws.delete_rows(2, len(values))
    if out_rows:
        ws.append_rows(out_rows, value_input_option="RAW")
    return len(out_rows)


def main(argv: Optional[Sequence[str]] = None) -> int:
    p = argparse.ArgumentParser(description="Seed Item Pack Master SSP from OBD layout A+B")
    p.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    p.add_argument("--layout-a", default=str(DEFAULT_A), help="JSON with SSP (layout A)")
    p.add_argument("--layout-b", default=str(DEFAULT_B), help="JSON with UPC (layout B)")
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-prices", action="store_true")
    p.add_argument("--source", default=SOURCE)
    args = p.parse_args(list(argv) if argv is not None else None)

    path_a = Path(args.layout_a)
    path_b = Path(args.layout_b)
    if not path_a.is_file():
        raise SystemExit(f"layout A missing: {path_a}")
    if not path_b.is_file():
        raise SystemExit(f"layout B missing: {path_b}")

    seeds, st = _bridge(path_a, path_b)
    print("=== OBD layout A+B bridge ===")
    print(f"A={path_a.name} B={path_b.name}")
    for k in sorted(st):
        print(f"  {k}={st[k]}")
    print(f"seeds={len(seeds)}")
    for s in seeds[:8]:
        print(f"  {s['item_code']} upc={s['upc']} ssp={s['sspu']} {s['desc'][:28]}")
    if len(seeds) > 8:
        print(f"  ... +{len(seeds) - 8} more")

    if not seeds:
        print("nothing to seed")
        return 1

    gc = _client()
    sh = gc.open_by_key(args.sheet_id)
    ws = _ensure_master(sh)
    by_upc = _load_master(ws)
    print(f"master before={len(by_upc)}")
    apply_st = apply_seeds(
        by_upc, seeds, force_prices=args.force_prices, source=args.source
    )
    print(
        f"apply added={apply_st['added']} ssp_fill={apply_st['ssp_fill']} "
        f"ssp_keep={apply_st['ssp_keep']} ssp_force={apply_st['ssp_force']} "
        f"master_after={len(by_upc)} dry_run={args.dry_run}"
    )
    if args.dry_run:
        return 0
    n = write_master(ws, by_upc, args.source)
    print(f"wrote {n} rows → '{MASTER_TAB}'")
    # Invalidate OCR cache if importable
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
