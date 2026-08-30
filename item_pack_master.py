"""
Item Pack Master lookup (UPC → last-seen pack/SSP).

Shared by OCR live enrich, backfill scripts, and seed tools.
Match key = UPC only (leading zeros preserved). Fail soft — never raise into upload path.
"""

from __future__ import annotations

import logging
import os
import re
import threading
import time
from pathlib import Path
from typing import Any, Dict, Mapping, Optional, Tuple

logger = logging.getLogger(__name__)

BASE_DIR = Path(__file__).resolve().parent
DEFAULT_CREDS = Path(
    os.getenv(
        "GOOGLE_CREDENTIALS",
        str(Path.home() / ".openclaw" / "google-credentials.json"),
    )
)
if not DEFAULT_CREDS.is_file():
    DEFAULT_CREDS = BASE_DIR / "google-credentials.json"

DEFAULT_SHEET_ID = os.getenv(
    "INVOICE_WORKBOOK_ID", "1dcZufZoV7whkLiSzOkM8qarUXetrIr_KHBqusfWmb1M"
)
MASTER_TAB = "Item Pack Master"
SCOPES = (
    "https://www.googleapis.com/auth/spreadsheets",
    "https://www.googleapis.com/auth/drive",
)

# Process cache (OCR may hit master once per upload; TTL avoids sheet spam)
_cache_lock = threading.Lock()
_cache_at = 0.0
_cache_ssp: Dict[str, str] = {}
_cache_ext: Dict[str, str] = {}
_CACHE_TTL = float(os.getenv("ITEM_PACK_CACHE_TTL", "300"))


def _digits(upc: str) -> str:
    return re.sub(r"\D", "", str(upc or "").strip())


def normalize_upc_key(upc: str) -> str:
    """Prefer full RAW string; also index by digits-only for loose match."""
    return str(upc or "").strip()


def _client():
    import gspread
    from google.oauth2.service_account import Credentials

    path = Path(os.getenv("GOOGLE_CREDENTIALS", str(DEFAULT_CREDS)))
    if not path.is_file():
        raise FileNotFoundError(f"credentials not found: {path}")
    creds = Credentials.from_service_account_file(str(path), scopes=list(SCOPES))
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


def _norm_ext(raw: Any) -> str:
    s = str(raw or "").strip().replace(",", "")
    if not s:
        return ""
    try:
        v = float(s)
    except ValueError:
        return ""
    if abs(v - round(v)) < 1e-9:
        return str(int(round(v)))
    return str(v)


def fetch_master_maps(
    *,
    sheet_id: str = "",
    force: bool = False,
) -> Tuple[Dict[str, str], Dict[str, str]]:
    """
    Return (ssp_by_upc, ext_by_upc).
    Keys include RAW UPC and digits-only form when different.
    Active=FALSE rows skipped.
    """
    global _cache_at, _cache_ssp, _cache_ext
    now = time.time()
    with _cache_lock:
        if (
            not force
            and _cache_ssp is not None
            and (now - _cache_at) < _CACHE_TTL
            and (_cache_ssp or _cache_ext or _cache_at > 0)
        ):
            # Allow empty cache only if we successfully loaded recently
            if _cache_at > 0:
                return dict(_cache_ssp), dict(_cache_ext)

    ssp: Dict[str, str] = {}
    ext: Dict[str, str] = {}
    sid = (sheet_id or DEFAULT_SHEET_ID).strip()
    try:
        gc = _client()
        sh = gc.open_by_key(sid)
        ws = sh.worksheet(MASTER_TAB)
        values = ws.get_all_values() or []
        if not values:
            logger.warning("Item Pack Master empty")
        else:
            header = [h.strip() for h in values[0]]
            col = {h: i for i, h in enumerate(header)}
            if "UPC" not in col:
                logger.warning("Item Pack Master missing UPC col: %s", header)
            else:
                i_upc = col["UPC"]
                i_ssp = col.get("SSP per Unit")
                i_ext = col.get("Extracted Qty")
                i_act = col.get("Active")
                for r in values[1:]:
                    rr = r + [""] * 20
                    upc = str(rr[i_upc]).strip()
                    if not upc:
                        continue
                    if i_act is not None:
                        act = str(rr[i_act]).strip().upper()
                        if act in ("FALSE", "0", "N", "NO"):
                            continue
                    ssp_s = _fmt_ssp(rr[i_ssp]) if i_ssp is not None else ""
                    ext_s = _norm_ext(rr[i_ext]) if i_ext is not None else ""
                    if ssp_s:
                        ssp[upc] = ssp_s
                        d = _digits(upc)
                        if d and d not in ssp:
                            ssp[d] = ssp_s
                    if ext_s:
                        ext[upc] = ext_s
                        d = _digits(upc)
                        if d and d not in ext:
                            ext[d] = ext_s
        with _cache_lock:
            _cache_ssp = dict(ssp)
            _cache_ext = dict(ext)
            _cache_at = time.time()
        logger.info(
            "Item Pack Master loaded ssp=%d ext=%d sheet=%s",
            len(ssp),
            len(ext),
            sid[:8],
        )
    except Exception as e:
        logger.warning("Item Pack Master load failed (soft): %s", e)
        with _cache_lock:
            # Keep prior cache if any
            if _cache_at > 0 and not force:
                return dict(_cache_ssp), dict(_cache_ext)
            _cache_at = time.time()  # negative cache brief
            _cache_ssp = {}
            _cache_ext = {}
        return {}, {}

    return ssp, ext


def lookup_ssp(upc: str, ssp_map: Optional[Mapping[str, str]] = None) -> str:
    if not upc:
        return ""
    m = ssp_map if ssp_map is not None else fetch_master_maps()[0]
    u = normalize_upc_key(upc)
    if u in m:
        return m[u]
    d = _digits(u)
    if d and d in m:
        return m[d]
    return ""


def lookup_ext(upc: str, ext_map: Optional[Mapping[str, str]] = None) -> str:
    if not upc:
        return ""
    m = ext_map if ext_map is not None else fetch_master_maps()[1]
    u = normalize_upc_key(upc)
    if u in m:
        return m[u]
    d = _digits(u)
    if d and d in m:
        return m[d]
    return ""


def invalidate_cache() -> None:
    global _cache_at
    with _cache_lock:
        _cache_at = 0.0


# Vendors that must keep SSP empty even if master has a value
SSP_MASTER_SKIP_VENDORS = frozenset({"abarta_coke"})


def enrich_line_items_ssp_from_master(
    items: list,
    *,
    vendor_key: str = "",
    ssp_map: Optional[Mapping[str, str]] = None,
) -> int:
    """
    Fill blank ssp_per_pack / ssp_per_unit from master UPC hit.
    Never overwrites non-blank ticket SSP. Returns count of lines filled.
    """
    if (vendor_key or "").strip() in SSP_MASTER_SKIP_VENDORS:
        return 0
    if not items:
        return 0
    try:
        m = dict(ssp_map) if ssp_map is not None else fetch_master_maps()[0]
    except Exception as e:
        logger.warning("SSP master enrich skipped: %s", e)
        return 0
    if not m:
        return 0

    filled = 0
    for it in items:
        if not isinstance(it, dict):
            continue
        pack = str(it.get("ssp_per_pack") or "").strip()
        unit = str(it.get("ssp_per_unit") or "").strip()
        if pack and unit:
            continue
        if pack and not unit:
            it["ssp_per_unit"] = pack
            continue
        if unit and not pack:
            it["ssp_per_pack"] = unit
            continue
        # both blank — try master
        upc = str(it.get("upc_raw") or it.get("upc") or "").strip()
        # Prefer barcode-looking field
        raw = str(it.get("upc_raw") or "").strip()
        sheet_u = str(it.get("upc") or "").strip()
        cand = raw if _digits(raw) and len(_digits(raw)) >= 10 else sheet_u
        if not cand or len(_digits(cand)) < 10:
            # item_code-only rows (layout A OBD) — no UPC key
            continue
        ssp = lookup_ssp(cand, m)
        if not ssp:
            # try other field
            other = sheet_u if cand == raw else raw
            if other and other != cand:
                ssp = lookup_ssp(other, m)
        if not ssp:
            continue
        it["ssp_per_pack"] = ssp
        it["ssp_per_unit"] = ssp
        it["ssp_source"] = "item_pack_master"
        filled += 1
    if filled:
        logger.info(
            "Item Pack Master SSP enrich filled=%d vendor=%s",
            filled,
            vendor_key or "-",
        )
    return filled
