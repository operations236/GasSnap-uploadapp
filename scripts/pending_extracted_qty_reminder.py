#!/usr/bin/env python3
"""
Scan Inv - {Store} tabs for lines still missing Extracted Qty and/or Cost per Unit.

Prints a compact summary when anything is pending.
With --telegram: also send to owner chat (openclaw telegram_config).
Empty stdout when nothing pending (unless --always).

Usage:
  ./venv/bin/python scripts/pending_extracted_qty_reminder.py
  ./venv/bin/python scripts/pending_extracted_qty_reminder.py --telegram
  ./venv/bin/python scripts/pending_extracted_qty_reminder.py --telegram --always
"""
from __future__ import annotations

import argparse
import json
import os
import sys
import urllib.request
from collections import defaultdict
from datetime import datetime
from pathlib import Path
from typing import Dict, List, Tuple
from zoneinfo import ZoneInfo

import gspread
from google.oauth2.service_account import Credentials

EASTERN = ZoneInfo("America/New_York")
DEFAULT_SHEET_ID = os.getenv(
    "INVOICE_WORKBOOK_ID", "1dcZufZoV7whkLiSzOkM8qarUXetrIr_KHBqusfWmb1M"
)
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

# Invoice-tracking bot (separate from ops morning_dashboard bot)
_INVOICE_TG_ENV = Path(
    os.getenv(
        "INVOICE_TELEGRAM_ENV",
        str(Path.home() / ".openclaw" / "telegram-invoice.env"),
    )
)


def _load_invoice_telegram() -> tuple[str, str]:
    """Load TELEGRAM_TOKEN + TELEGRAM_CHAT_ID from invoice env file."""
    if not _INVOICE_TG_ENV.is_file():
        raise FileNotFoundError(
            f"Invoice Telegram env missing: {_INVOICE_TG_ENV} "
            "(create with TELEGRAM_TOKEN= and TELEGRAM_CHAT_ID=)"
        )
    vals: dict[str, str] = {}
    for line in _INVOICE_TG_ENV.read_text().splitlines():
        line = line.strip()
        if not line or line.startswith("#") or "=" not in line:
            continue
        k, v = line.split("=", 1)
        vals[k.strip()] = v.strip()
    token = vals.get("TELEGRAM_TOKEN") or ""
    chat = vals.get("TELEGRAM_CHAT_ID") or ""
    if not token or not chat:
        raise RuntimeError(f"TELEGRAM_TOKEN/CHAT_ID empty in {_INVOICE_TG_ENV}")
    return token, chat


def send_telegram(text: str) -> None:
    token, chat_id = _load_invoice_telegram()
    url = f"https://api.telegram.org/bot{token}/sendMessage"
    payload = json.dumps(
        {
            "chat_id": chat_id,
            "text": text,
            "disable_web_page_preview": True,
        }
    ).encode()
    req = urllib.request.Request(
        url, data=payload, headers={"Content-Type": "application/json"}
    )
    with urllib.request.urlopen(req, timeout=20) as resp:
        if resp.status >= 300:
            raise RuntimeError(f"telegram HTTP {resp.status}")


def _client() -> gspread.Client:
    if not DEFAULT_CREDS.is_file():
        raise SystemExit(f"credentials missing: {DEFAULT_CREDS}")
    creds = Credentials.from_service_account_file(str(DEFAULT_CREDS), scopes=list(SCOPES))
    return gspread.authorize(creds)


def _blank(s: object) -> bool:
    return not str(s or "").strip()


def _store_from_tab(title: str) -> str:
    t = (title or "").strip()
    if t.lower().startswith("inv - "):
        return t[6:].strip()
    return t


def scan(sheet_id: str) -> Tuple[Dict[Tuple[str, str], Dict[str, int]], Dict[str, int]]:
    gc = _client()
    sh = gc.open_by_key(sheet_id)
    by_sv: Dict[Tuple[str, str], Dict[str, int]] = defaultdict(
        lambda: {"missing_ext": 0, "missing_cpu": 0, "missing_both": 0, "lines": 0}
    )
    totals = {"rows_upc": 0, "missing_ext": 0, "missing_cpu": 0, "pending_lines": 0}

    for ws in sh.worksheets():
        if not ws.title.lower().startswith("inv - "):
            continue
        store = _store_from_tab(ws.title)
        values = ws.get_all_values() or []
        if len(values) < 2:
            continue
        header = [h.strip() for h in values[0]]
        col = {h: i for i, h in enumerate(header)}
        if "UPC" not in col:
            continue
        i_upc = col["UPC"]
        i_ext = col.get("Extracted Qty")
        i_cpu = col.get("Cost per Unit")
        i_vend = col.get("Vendor")

        for r in values[1:]:
            rr = r + [""] * 24
            upc = str(rr[i_upc]).strip() if i_upc < len(rr) else ""
            if not upc:
                continue
            totals["rows_upc"] += 1
            ext_blank = i_ext is None or _blank(rr[i_ext] if i_ext < len(rr) else "")
            cpu_blank = i_cpu is None or _blank(rr[i_cpu] if i_cpu < len(rr) else "")
            if not ext_blank and not cpu_blank:
                continue
            vend = (
                str(rr[i_vend]).strip()
                if i_vend is not None and i_vend < len(rr)
                else ""
            ) or "(no vendor)"
            if len(vend) > 40:
                vend = vend[:37] + "…"
            key = (store, vend)
            by_sv[key]["lines"] += 1
            totals["pending_lines"] += 1
            if ext_blank:
                by_sv[key]["missing_ext"] += 1
                totals["missing_ext"] += 1
            if cpu_blank:
                by_sv[key]["missing_cpu"] += 1
                totals["missing_cpu"] += 1
            if ext_blank and cpu_blank:
                by_sv[key]["missing_both"] += 1

    return by_sv, totals


def format_message(
    by_sv: Dict[Tuple[str, str], Dict[str, int]],
    totals: Dict[str, int],
    *,
    min_lines: int,
    top_n: int,
) -> str:
    items = [(k, v) for k, v in by_sv.items() if v["lines"] >= min_lines]
    items.sort(key=lambda kv: (-kv[1]["lines"], kv[0][0], kv[0][1]))
    if not items:
        return ""

    now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M %Z")
    lines: List[str] = []
    lines.append("Item Pack Master — pending fills")
    lines.append(now)
    lines.append(
        f"Total pending lines: {totals['pending_lines']} "
        f"(blank Extracted Qty: {totals['missing_ext']}, "
        f"blank Cost/Unit: {totals['missing_cpu']})"
    )
    lines.append("")
    lines.append("By store + vendor:")

    shown = 0
    for (store, vend), st in items:
        if shown >= top_n:
            break
        lines.append(
            f"• {store} · {vend}: {st['lines']} lines "
            f"(Ext qty blank {st['missing_ext']}, "
            f"Cost/unit blank {st['missing_cpu']})"
        )
        shown += 1
    rest = len(items) - shown
    if rest > 0:
        lines.append(f"… +{rest} more store·vendor groups")

    lines.append("")
    lines.append("Fill Extracted Qty on Inv, then run upsert:")
    lines.append(
        "cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/upsert_item_pack_master.py"
    )
    return "\n".join(lines)


def main(argv: List[str] | None = None) -> int:
    p = argparse.ArgumentParser(
        description="Reminder: pending Extracted Qty / Cost per Unit by store+vendor"
    )
    p.add_argument("--sheet-id", default=DEFAULT_SHEET_ID)
    p.add_argument("--min-lines", type=int, default=1)
    p.add_argument("--top", type=int, default=25)
    p.add_argument(
        "--always",
        action="store_true",
        help="Message even when nothing pending (default: silent if clean)",
    )
    p.add_argument(
        "--telegram",
        action="store_true",
        help="Send via invoice bot (~/.openclaw/telegram-invoice.env)",
    )
    args = p.parse_args(argv)

    by_sv, totals = scan(args.sheet_id)
    msg = format_message(by_sv, totals, min_lines=args.min_lines, top_n=args.top)
    if not msg:
        if args.always:
            now = datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M %Z")
            msg = f"Item Pack Master — no pending Extracted Qty / Cost per Unit ({now})"
        else:
            return 0

    print(msg)
    if args.telegram:
        try:
            send_telegram(msg)
            print("telegram: sent", file=sys.stderr)
        except Exception as e:
            print(f"telegram: FAIL {e}", file=sys.stderr)
            return 2
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
