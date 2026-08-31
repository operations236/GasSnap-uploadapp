#!/usr/bin/env python3
"""
Seed Item Pack Master SSP from Ohio Beverage dual layouts (Store + UPC key).

Layout A (SSP, often no UPC): ITEM# + SSP
Layout B (UPC, no SSP):       ITEM# + UPC
Join on OBD ITEM# → master row (Store, UPC) + SSP per Unit.

Does NOT invent Extracted Qty.
Merges into existing master (does not wipe other stores).

Default samples (ARCO):
  A: uploads/20260822_190933_7df92c1520.json  inv 244557
  B: uploads/20260826_182318_49148774c1.json  inv 253838

Usage:
  ./venv/bin/python scripts/seed_obd_ssp_from_layouts.py --dry-run
  ./venv/bin/python scripts/seed_obd_ssp_from_layouts.py --store ARCO
"""
from __future__ import annotations

import argparse
import json
import os
import sys
from collections import OrderedDict
from datetime import datetime
from pathlib import Path
from typing import Any, Dict, List, Optional, Sequence
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

# Must match upsert_item_pack_master.HEADERS
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

DEFAULT_A = BASE_DIR / "uploads" / "20260822_190933_7df92c1520.json"
DEFAULT_B = BASE_DIR / "uploads" / "20260826_182318_49148774c1.json"
VENDOR = "Ohio Beverage Distributing"
SOURCE = "obd_layout_ab_bridge"
DEFAULT_STORE = "ARCO"


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


def _items(path: Path) -> List[Dict[str, Any]]:
    d = json.loads(path.read_text())
    e = (d.get("ocr") or {}).get("extraction") or d.get("extraction") or d
    return list(e.get("line_items") or [])


def _item_code(it: Dict[str, Any]) -> str:
    return str(it.get("item_code") or it.get("ITEM#") or "").strip()


def _bridge(path_a: Path, path_b: Path) -> tuple[List[Dict[str, str]], Dict[str, int]]:
    A: Dict[str, Dict[str, str]] = {}
    B: Dict[str, Dict[str, str]] = {}
    st = {
        "a_lines": 0,
        "b_lines": 0,
        "a_codes": 0,
        "b_codes": 0,
        "overlap": 0,
        "seedable": 0,
        "skip_no_ssp": 0,
        "skip_no_upc": 0,
        "a_only": 0,
        "b_only": 0,
    }
    for it in _items(path_a):
        st["a_lines"] += 1
        code = _item_code(it)
        if not code:
            continue
        ssp = _fmt_ssp(it.get("ssp_per_pack") or it.get("ssp_per_unit") or "")
        if not ssp:
            st["skip_no_ssp"] += 1
            continue
        A[code] = {
            "item_code": code,
            "sspu": ssp,
            "desc": str(it.get("description") or "")[:80],
            "pack": str(it.get("pack_size") or ""),
        }
    st["a_codes"] = len(A)
    for it in _items(path_b):
        st["b_lines"] += 1
        code = _item_code(it)
        if not code:
            continue
        upc = str(it.get("upc_raw") or it.get("upc") or "").strip()
        if not upc or len("".join(c for c in upc if c.isdigit())) < 10:
            st["skip_no_upc"] += 1
            continue
        B[code] = {
            "item_code": code,
            "upc": upc,
            "desc": str(it.get("description") or "")[:80],
            "pack": str(it.get("pack_size") or ""),
            "vendor": VENDOR,
        }
    st["b_codes"] = len(B)
    seeds: List[Dict[str, str]] = []
    for code in sorted(set(A) & set(B)):
        st["overlap"] += 1
        a, b = A[code], B[code]
        seeds.append(
            {
                "item_code": code,
                "upc": b["upc"],
                "sspu": a["sspu"],
                "desc": a.get("desc") or b.get("desc") or "",
                "pack": a.get("pack") or b.get("pack") or "",
                "vendor": VENDOR,
            }
        )
        st["seedable"] += 1
    st["b_only"] = len(set(B) - set(A))
    st["a_only"] = len(set(A) - set(B))
    return seeds, st


def _rk(store: str, upc: str) -> str:
    return f"{store.strip().casefold()}\x1f{upc.strip()}"


def _load(ws: gspread.Worksheet) -> "OrderedDict[str, Dict[str, Any]]":
    values = ws.get_all_values() or []
    by: "OrderedDict[str, Dict[str, Any]]" = OrderedDict()
    if not values:
        return by
    header = [h.strip() for h in values[0]]
    col = {h: i for i, h in enumerate(header)}

    def cell(rr: List[str], name: str) -> str:
        i = col.get(name)
        if i is None or i >= len(rr):
            return ""
        return str(rr[i]).strip()

    for r in values[1:]:
        rr = r + [""] * 28
        store = cell(rr, "Store") or cell(rr, "SSP Store")
        upc = cell(rr, "UPC")
        if not store or not upc:
            continue
        try:
            hit = int(float(cell(rr, "Hit Count") or 0))
        except ValueError:
            hit = 0
        by[_rk(store, upc)] = {
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


def _write(ws: gspread.Worksheet, by: "OrderedDict[str, Dict[str, Any]]") -> int:
    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    out: List[List[str]] = []
    items = sorted(
        by.values(),
        key=lambda d: (str(d.get("store") or "").casefold(), str(d.get("upc") or "")),
    )
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
                str(d.get("source") or SOURCE),
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
    p = argparse.ArgumentParser(description="Seed OBD SSP into Store+UPC Item Pack Master")
    p.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    p.add_argument("--layout-a", default=str(DEFAULT_A))
    p.add_argument("--layout-b", default=str(DEFAULT_B))
    p.add_argument("--store", default=DEFAULT_STORE)
    p.add_argument("--dry-run", action="store_true")
    p.add_argument("--force-prices", action="store_true")
    p.add_argument("--source", default=SOURCE)
    args = p.parse_args(list(argv) if argv is not None else None)

    path_a, path_b = Path(args.layout_a), Path(args.layout_b)
    if not path_a.is_file() or not path_b.is_file():
        raise SystemExit(f"missing layout JSON A={path_a} B={path_b}")

    seeds, st = _bridge(path_a, path_b)
    print(f"=== OBD bridge store={args.store} ===")
    for k in sorted(st):
        print(f"  {k}={st[k]}")
    print(f"seeds={len(seeds)}")
    for s in seeds[:6]:
        print(f"  {s['item_code']} upc={s['upc']} ssp={s['sspu']}")
    if not seeds:
        return 1

    gc = _client()
    sh = gc.open_by_key(args.sheet_id)
    try:
        ws = sh.worksheet(MASTER_TAB)
    except gspread.exceptions.WorksheetNotFound:
        ws = sh.add_worksheet(title=MASTER_TAB, rows=4000, cols=len(HEADERS))
        ws.update(values=[HEADERS], range_name="A1", value_input_option="RAW")

    by = _load(ws)
    print(f"master before={len(by)}")
    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    store = args.store.strip()
    added = fill = keep = force = 0
    for s in seeds:
        k = _rk(store, s["upc"])
        note = f"OBD ITEM#{s['item_code']};"
        if k not in by:
            by[k] = {
                "store": store,
                "upc": s["upc"],
                "ext": "",
                "unit": "",
                "cpu": "",
                "sspu": s["sspu"],
                "desc": s.get("desc") or "",
                "pack": s.get("pack") or "",
                "vendor": VENDOR,
                "source": args.source,
                "active": "TRUE",
                "notes": note,
                "updated": now,
                "hit": 1,
            }
            added += 1
            fill += 1
            continue
        cur = by[k]
        cur["hit"] = int(cur.get("hit") or 0) + 1
        cur["updated"] = now
        if s.get("desc"):
            cur["desc"] = s["desc"]
        if note not in (cur.get("notes") or ""):
            cur["notes"] = ((cur.get("notes") or "") + " " + note).strip()
        if args.force_prices or not str(cur.get("sspu") or "").strip():
            cur["sspu"] = s["sspu"]
            if args.force_prices:
                force += 1
            else:
                fill += 1
            if not cur.get("source") or cur.get("source") == "manual":
                cur["source"] = args.source
        else:
            keep += 1
    print(f"apply added={added} ssp_fill={fill} ssp_force={force} ssp_keep={keep} after={len(by)}")
    if args.dry_run:
        return 0
    n = _write(ws, by)
    print(f"wrote {n} rows")
    try:
        sys.path.insert(0, str(BASE_DIR))
        import item_pack_master as ipm

        ipm.invalidate_cache()
    except Exception as e:
        print("cache skip", e)
    return 0


if __name__ == "__main__":
    sys.exit(main())
