"""
Item Pack Master lookup — key = (Store, UPC).

Shared by OCR live enrich, backfill scripts, and upsert tools.

Master row fields (per store):
  Extracted Qty (units/case gold)
  Cost per Unit (last-seen reference; live prefers Cost per Pack ÷ Extracted Qty)
  SSP per Unit (last-seen shelf; ticket SSP wins when printed)

Calculated Qty is NOT stored on master — always Qty(Cases) × Extracted Qty on Inv/OCR.

Blanks-only enrich — never overwrite operator/ticket values.
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
# composite key "store\\x1fupc" and digit variants → row dict
_cache_rows: Dict[str, Dict[str, str]] = {}
_CACHE_TTL = float(os.getenv("ITEM_PACK_CACHE_TTL", "300"))

SSP_MASTER_SKIP_VENDORS = frozenset({"abarta_coke"})
# qty enrich can skip same if needed later
QTY_MASTER_SKIP_VENDORS = frozenset()


def _digits(upc: str) -> str:
    return re.sub(r"\D", "", str(upc or "").strip())


def normalize_upc_key(upc: str) -> str:
    return str(upc or "").strip()


def normalize_store(store: str) -> str:
    """PIN/session store name; preserve display casing for writes via canonical map."""
    return str(store or "").strip()


def normalize_store_key(store: str) -> str:
    return str(store or "").strip().casefold()


def store_from_inv_tab(tab_title: str) -> str:
    """'Inv - ARCO' → 'ARCO'."""
    t = str(tab_title or "").strip()
    if t.lower().startswith("inv - "):
        return t[6:].strip()
    return t


def _ck(store: str, upc: str) -> str:
    return f"{normalize_store_key(store)}\x1f{normalize_upc_key(upc)}"


def _client():
    import gspread
    from google.oauth2.service_account import Credentials

    path = Path(os.getenv("GOOGLE_CREDENTIALS", str(DEFAULT_CREDS)))
    if not path.is_file():
        raise FileNotFoundError(f"credentials not found: {path}")
    creds = Credentials.from_service_account_file(str(path), scopes=list(SCOPES))
    return gspread.authorize(creds)


def _fmt_money(raw: Any) -> str:
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


def _index_row(
    dst: Dict[str, Dict[str, str]],
    *,
    store: str,
    upc: str,
    ext: str,
    cpu: str,
    ssp: str,
    unit: str = "",
    desc: str = "",
) -> None:
    if not store or not upc:
        return
    payload = {
        "store": normalize_store(store),
        "upc": normalize_upc_key(upc),
        "ext": ext or "",
        "cpu": cpu or "",
        "ssp": ssp or "",
        "unit": unit or "",
        "desc": desc or "",
    }
    keys = [normalize_upc_key(upc)]
    d = _digits(upc)
    if d and d not in keys:
        keys.append(d)
    for u in keys:
        k = _ck(store, u)
        # first wins on duplicate harvest
        if k not in dst:
            dst[k] = dict(payload)


def fetch_master_rows(
    *,
    sheet_id: str = "",
    force: bool = False,
) -> Dict[str, Dict[str, str]]:
    """
    Load master keyed by store\\x1fupc (and digit upc variants).
    Each value: store, upc, ext, cpu, ssp, unit, desc.
    """
    global _cache_at, _cache_rows
    now = time.time()
    with _cache_lock:
        if not force and _cache_at > 0 and (now - _cache_at) < _CACHE_TTL:
            return {k: dict(v) for k, v in _cache_rows.items()}

    rows: Dict[str, Dict[str, str]] = {}
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
                i_store = col.get("Store")
                # legacy: SSP Store when Store missing
                if i_store is None:
                    i_store = col.get("SSP Store")
                i_ext = col.get("Extracted Qty")
                i_cpu = col.get("Cost per Unit")
                i_ssp = col.get("SSP per Unit")
                i_unit = col.get("Unit Name")
                i_desc = col.get("Description")
                i_act = col.get("Active")
                for r in values[1:]:
                    rr = r + [""] * 28
                    upc = str(rr[i_upc]).strip()
                    if not upc:
                        continue
                    if i_act is not None:
                        act = str(rr[i_act]).strip().upper()
                        if act in ("FALSE", "0", "N", "NO"):
                            continue
                    store = str(rr[i_store]).strip() if i_store is not None else ""
                    if not store:
                        # skip storeless legacy during transition
                        continue
                    ext_s = _norm_ext(rr[i_ext]) if i_ext is not None else ""
                    cpu_s = _fmt_money(rr[i_cpu]) if i_cpu is not None else ""
                    ssp_s = _fmt_money(rr[i_ssp]) if i_ssp is not None else ""
                    unit_s = str(rr[i_unit]).strip() if i_unit is not None else ""
                    desc_s = str(rr[i_desc]).strip() if i_desc is not None else ""
                    if not ext_s and not cpu_s and not ssp_s:
                        continue
                    _index_row(
                        rows,
                        store=store,
                        upc=upc,
                        ext=ext_s,
                        cpu=cpu_s,
                        ssp=ssp_s,
                        unit=unit_s,
                        desc=desc_s,
                    )
        with _cache_lock:
            _cache_rows = {k: dict(v) for k, v in rows.items()}
            _cache_at = time.time()
        logger.info(
            "Item Pack Master loaded rows=%d stores=%d",
            len(rows),
            len({v.get("store") for v in rows.values()}),
        )
    except Exception as e:
        logger.warning("Item Pack Master load failed (soft): %s", e)
        with _cache_lock:
            if _cache_at > 0 and not force:
                return {k: dict(v) for k, v in _cache_rows.items()}
            _cache_at = time.time()
            _cache_rows = {}
        return {}

    return rows


# Back-compat alias used by older scripts
def fetch_master_maps(
    *,
    sheet_id: str = "",
    force: bool = False,
) -> Tuple[Dict[str, Dict[str, str]], Dict[str, str]]:
    """
    Deprecated shape for tests: returns (ssp_meta_by_flat_upc, ext_by_flat_upc)
    WITHOUT store filter — prefer lookup_* with store.
    """
    rows = fetch_master_rows(sheet_id=sheet_id, force=force)
    ssp_meta: Dict[str, Dict[str, str]] = {}
    ext: Dict[str, str] = {}
    for v in rows.values():
        u = v.get("upc") or ""
        if not u:
            continue
        if v.get("ssp") and u not in ssp_meta:
            ssp_meta[u] = {"ssp": v["ssp"], "store": v.get("store") or ""}
        if v.get("ext") and u not in ext:
            ext[u] = v["ext"]
    return ssp_meta, ext


def lookup_row(
    upc: str,
    store: str,
    rows: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> Dict[str, str]:
    if not upc or not normalize_store_key(store):
        return {}
    m = rows if rows is not None else fetch_master_rows()
    u = normalize_upc_key(upc)
    hit = m.get(_ck(store, u))
    if hit:
        return dict(hit)
    d = _digits(u)
    if d and d != u:
        hit = m.get(_ck(store, d))
        if hit:
            return dict(hit)
    return {}


def lookup_ext(
    upc: str,
    store: str = "",
    rows: Optional[Mapping[str, Mapping[str, str]]] = None,
    ext_map: Optional[Mapping[str, str]] = None,
) -> str:
    """Extracted Qty for (store, upc). store required for Store+UPC master."""
    if store:
        return str(lookup_row(upc, store, rows).get("ext") or "")
    # legacy fallback
    if ext_map is not None:
        u = normalize_upc_key(upc)
        return str(ext_map.get(u) or ext_map.get(_digits(u)) or "")
    return ""


def lookup_ssp(
    upc: str,
    store: str = "",
    ssp_meta: Optional[Mapping[str, Mapping[str, str]]] = None,
    rows: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> str:
    if store and rows is not None:
        return str(lookup_row(upc, store, rows).get("ssp") or "")
    if store:
        return str(lookup_row(upc, store).get("ssp") or "")
    # legacy Option C path via ssp_meta
    if not upc or not normalize_store_key(store):
        return ""
    meta = ssp_meta if ssp_meta is not None else fetch_master_maps()[0]
    u = normalize_upc_key(upc)
    row = meta.get(u) or meta.get(_digits(u))
    if not row:
        return ""
    if normalize_store_key(str(row.get("store") or "")) != normalize_store_key(store):
        return ""
    return str(row.get("ssp") or "").strip()


def lookup_cpu(upc: str, store: str, rows: Optional[Mapping[str, Mapping[str, str]]] = None) -> str:
    return str(lookup_row(upc, store, rows).get("cpu") or "")


def invalidate_cache() -> None:
    global _cache_at
    with _cache_lock:
        _cache_at = 0.0


def _item_upc_candidates(it: Mapping[str, Any]) -> list[str]:
    raw = str(it.get("upc_raw") or "").strip()
    sheet_u = str(it.get("upc") or "").strip()
    out = []
    for c in (raw, sheet_u):
        if c and c not in out and len(_digits(c)) >= 8:
            out.append(c)
    return out


def _blank(s: Any) -> bool:
    return not str(s or "").strip()


def _f(s: Any) -> Optional[float]:
    t = str(s or "").strip().replace(",", "").replace("$", "")
    if not t:
        return None
    try:
        return float(t)
    except ValueError:
        return None


def enrich_line_items_from_master(
    items: list,
    *,
    vendor_key: str = "",
    store: str = "",
    rows: Optional[Mapping[str, Mapping[str, str]]] = None,
    fill_ssp: bool = True,
    fill_qty: bool = True,
) -> Dict[str, int]:
    """
    Blanks-only enrich from (store, UPC) master:
      - Extracted Qty from master.ext
      - Calculated Qty = qty_cases × extracted (live)
      - Cost per Unit = cost_per_pack ÷ extracted when both present (live math);
        else master.cpu last-seen if still blank
      - SSP pack+unit from master.ssp (skip abarta_coke)
    Never overwrites non-blank ticket/operator values.
    """
    stats = {"ssp": 0, "ext": 0, "calc": 0, "cpu": 0, "rows_hit": 0}
    if not items or not normalize_store_key(store):
        return stats
    vk = (vendor_key or "").strip()
    try:
        master = (
            {k: dict(v) for k, v in rows.items()}
            if rows is not None
            else fetch_master_rows()
        )
    except Exception as e:
        logger.warning("Item Pack Master enrich skipped: %s", e)
        return stats
    if not master:
        return stats

    do_ssp = fill_ssp and vk not in SSP_MASTER_SKIP_VENDORS
    do_qty = fill_qty and vk not in QTY_MASTER_SKIP_VENDORS

    for it in items:
        if not isinstance(it, dict):
            continue
        hit: Dict[str, str] = {}
        for cand in _item_upc_candidates(it):
            hit = lookup_row(cand, store, master)
            if hit:
                break
        if not hit:
            continue
        stats["rows_hit"] += 1

        # --- SSP ---
        if do_ssp:
            pack = str(it.get("ssp_per_pack") or "").strip()
            unit = str(it.get("ssp_per_unit") or "").strip()
            if pack and not unit:
                it["ssp_per_unit"] = pack
            elif unit and not pack:
                it["ssp_per_pack"] = unit
            elif not pack and not unit:
                ssp = str(hit.get("ssp") or "").strip()
                if ssp:
                    it["ssp_per_pack"] = ssp
                    it["ssp_per_unit"] = ssp
                    it["ssp_source"] = f"item_pack_master:{normalize_store_key(store)}"
                    stats["ssp"] += 1

        # --- Extracted Qty ---
        ext_m = str(hit.get("ext") or "").strip()
        if do_qty and ext_m and _blank(it.get("extracted_qty")):
            it["extracted_qty"] = ext_m
            it["ext_source"] = f"item_pack_master:{normalize_store_key(store)}"
            stats["ext"] += 1

        ext_use = str(it.get("extracted_qty") or ext_m or "").strip()
        ext_n = _f(ext_use)

        # --- Calculated Qty = cases × extracted ---
        if do_qty and _blank(it.get("calculated_qty")) and ext_n and ext_n > 0:
            qn = _f(it.get("qty_cases"))
            if qn is not None:
                calc = qn * ext_n
                if abs(calc - round(calc)) < 1e-9:
                    it["calculated_qty"] = str(int(round(calc)))
                else:
                    it["calculated_qty"] = f"{calc:.4f}".rstrip("0").rstrip(".")
                stats["calc"] += 1

        # --- Cost per Unit: prefer live pack÷ext ---
        if do_qty and _blank(it.get("cost_per_unit")):
            cpu = ""
            pack_c = _f(it.get("cost_per_pack"))
            if pack_c is not None and ext_n and ext_n > 0:
                cpu = _fmt_money(pack_c / ext_n)
            if not cpu:
                cpu = str(hit.get("cpu") or "").strip()
            if cpu:
                it["cost_per_unit"] = cpu
                stats["cpu"] += 1

    if any(stats[k] for k in ("ssp", "ext", "calc", "cpu")):
        logger.info(
            "Item Pack Master enrich store=%s vendor=%s hit=%d ssp=%d ext=%d calc=%d cpu=%d",
            store,
            vk or "-",
            stats["rows_hit"],
            stats["ssp"],
            stats["ext"],
            stats["calc"],
            stats["cpu"],
        )
    return stats


def enrich_line_items_ssp_from_master(
    items: list,
    *,
    vendor_key: str = "",
    store: str = "",
    ssp_meta: Optional[Mapping[str, Mapping[str, str]]] = None,
    rows: Optional[Mapping[str, Mapping[str, str]]] = None,
) -> int:
    """Back-compat: SSP-only enrich. Prefer enrich_line_items_from_master."""
    st = enrich_line_items_from_master(
        items,
        vendor_key=vendor_key,
        store=store,
        rows=rows,
        fill_ssp=True,
        fill_qty=False,
    )
    return int(st.get("ssp") or 0)
