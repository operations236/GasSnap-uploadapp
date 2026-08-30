"""
Item Pack Master lookup (UPC → pack; UPC+Store → SSP).

Shared by OCR live enrich, backfill scripts, and seed tools.
- Extracted Qty: match key = UPC only
- SSP: match key = UPC + source store (Option C — no cross-store bleed).
  Empty SSP Store on master → do not auto-fill SSP (legacy untagged).
Fail soft — never raise into upload path.
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

_cache_lock = threading.Lock()
_cache_at = 0.0
# upc -> ssp string only when we ignore store (legacy callers) — prefer store map
_cache_ssp_meta: Dict[str, Dict[str, str]] = {}  # upc -> {ssp, store}
_cache_ext: Dict[str, str] = {}
_CACHE_TTL = float(os.getenv("ITEM_PACK_CACHE_TTL", "300"))

SSP_MASTER_SKIP_VENDORS = frozenset({"abarta_coke"})


def _digits(upc: str) -> str:
    return re.sub(r"\D", "", str(upc or "").strip())


def normalize_upc_key(upc: str) -> str:
    return str(upc or "").strip()


def normalize_store(store: str) -> str:
    """PIN/session store name; casefold strip. Empty → no SSP match."""
    return str(store or "").strip().casefold()


def store_from_inv_tab(tab_title: str) -> str:
    """'Inv - ARCO' → 'ARCO'."""
    t = str(tab_title or "").strip()
    if t.lower().startswith("inv - "):
        return t[6:].strip()
    return t


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


def _index_upc(dst_meta: Dict[str, Dict[str, str]], dst_ext: Dict[str, str], upc: str, ssp: str, store: str, ext: str) -> None:
    if not upc:
        return
    keys = [upc]
    d = _digits(upc)
    if d and d != upc:
        keys.append(d)
    for k in keys:
        if ssp:
            # First wins for duplicate UPC rows
            if k not in dst_meta:
                dst_meta[k] = {"ssp": ssp, "store": store}
        if ext and k not in dst_ext:
            dst_ext[k] = ext


def fetch_master_maps(
    *,
    sheet_id: str = "",
    force: bool = False,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str]]:
    """
    Return (ssp_meta_by_upc, ext_by_upc).
    ssp_meta value: {"ssp": "13.99", "store": "ARCO"} (store may be "").
    Active=FALSE skipped.
    """
    global _cache_at, _cache_ssp_meta, _cache_ext
    now = time.time()
    with _cache_lock:
        if not force and _cache_at > 0 and (now - _cache_at) < _CACHE_TTL:
            return {k: dict(v) for k, v in _cache_ssp_meta.items()}, dict(_cache_ext)

    ssp_meta: Dict[str, Dict[str, str]] = {}
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
                # Option C store tag (new); fall back to nothing if missing
                i_ss = None
                for name in ("SSP Store", "SSP Source Store", "Store Example"):
                    if name in col:
                        i_ss = col[name]
                        break
                for r in values[1:]:
                    rr = r + [""] * 24
                    upc = str(rr[i_upc]).strip()
                    if not upc:
                        continue
                    if i_act is not None:
                        act = str(rr[i_act]).strip().upper()
                        if act in ("FALSE", "0", "N", "NO"):
                            continue
                    ssp_s = _fmt_ssp(rr[i_ssp]) if i_ssp is not None else ""
                    ext_s = _norm_ext(rr[i_ext]) if i_ext is not None else ""
                    store_s = str(rr[i_ss]).strip() if i_ss is not None else ""
                    _index_upc(ssp_meta, ext, upc, ssp_s, store_s, ext_s)
        with _cache_lock:
            _cache_ssp_meta = {k: dict(v) for k, v in ssp_meta.items()}
            _cache_ext = dict(ext)
            _cache_at = time.time()
        tagged = sum(1 for v in ssp_meta.values() if v.get("store") and v.get("ssp"))
        logger.info(
            "Item Pack Master loaded ssp_meta=%d ssp_store_tagged=%d ext=%d",
            len(ssp_meta),
            tagged,
            len(ext),
        )
    except Exception as e:
        logger.warning("Item Pack Master load failed (soft): %s", e)
        with _cache_lock:
            if _cache_at > 0 and not force:
                return {k: dict(v) for k, v in _cache_ssp_meta.items()}, dict(_cache_ext)
            _cache_at = time.time()
            _cache_ssp_meta = {}
            _cache_ext = {}
        return {}, {}

    return ssp_meta, ext


def lookup_ssp(
    upc: str,
    store: str = "",
    ssp_meta: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> str:
    """
    SSP only when master row is tagged with the same store (Option C).
    Untagged master SSP (empty SSP Store) never auto-fills.
    """
    if not upc:
        return ""
    want = normalize_store(store)
    if not want:
        return ""
    meta = ssp_meta if ssp_meta is not None else fetch_master_maps()[0]
    u = normalize_upc_key(upc)
    row = meta.get(u) or meta.get(_digits(u))
    if not row:
        return ""
    got_store = normalize_store(str(row.get("store") or ""))
    if not got_store or got_store != want:
        return ""
    return str(row.get("ssp") or "").strip()


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


def enrich_line_items_ssp_from_master(
    items: list,
    *,
    vendor_key: str = "",
    store: str = "",
    ssp_meta: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> int:
    """
    Fill blank ssp_per_pack / ssp_per_unit from master UPC+store hit.
    Never overwrites non-blank ticket SSP. Requires store (Option C).
    """
    if (vendor_key or "").strip() in SSP_MASTER_SKIP_VENDORS:
        return 0
    if not items:
        return 0
    if not normalize_store(store):
        logger.debug("SSP enrich skipped: no store")
        return 0
    try:
        meta = (
            {k: dict(v) for k, v in ssp_meta.items()}
            if ssp_meta is not None
            else fetch_master_maps()[0]
        )
    except Exception as e:
        logger.warning("SSP master enrich skipped: %s", e)
        return 0
    if not meta:
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
        raw = str(it.get("upc_raw") or "").strip()
        sheet_u = str(it.get("upc") or "").strip()
        cand = raw if _digits(raw) and len(_digits(raw)) >= 10 else sheet_u
        if not cand or len(_digits(cand)) < 10:
            continue
        ssp = lookup_ssp(cand, store=store, ssp_meta=meta)
        if not ssp:
            other = sheet_u if cand == raw else raw
            if other and other != cand:
                ssp = lookup_ssp(other, store=store, ssp_meta=meta)
        if not ssp:
            continue
        it["ssp_per_pack"] = ssp
        it["ssp_per_unit"] = ssp
        it["ssp_source"] = f"item_pack_master:{normalize_store(store)}"
        filled += 1
    if filled:
        logger.info(
            "Item Pack Master SSP enrich filled=%d vendor=%s store=%s",
            filled,
            vendor_key or "-",
            store or "-",
        )
    return filled
