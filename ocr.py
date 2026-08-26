"""
Gemini OCR for InvUpload — multi-vendor invoice line extraction.

Flow (background only — never blocks upload HTTP response):
  1. Detect vendor from invoice header (Gemini, short prompt)
  2. Extract line items with that vendor's prompt rules
  3. Append one Google Sheets row per line item

Vendor registry lives in vendors.py — add new distributors there.
"""

from __future__ import annotations

import json
import logging
import os
import re
import threading
from datetime import datetime
from decimal import Decimal, InvalidOperation, ROUND_HALF_UP
from pathlib import Path
from typing import Any, Dict, List, Mapping, Optional
from zoneinfo import ZoneInfo

from dotenv import load_dotenv

import vendors as vendor_registry

_BASE = Path(__file__).resolve().parent
load_dotenv(_BASE / ".env")

logger = logging.getLogger(__name__)
EASTERN = ZoneInfo("America/New_York")

GEMINI_MODEL = os.getenv("GEMINI_MODEL", "gemini-2.5-flash")
OCR_ENABLED = os.getenv("OCR_ENABLED", "1").strip().lower() not in ("0", "false", "no", "off")
LOW_CONFIDENCE_THRESHOLD = int(os.getenv("OCR_LOW_CONFIDENCE_THRESHOLD", "70"))
# Set OCR_SKIP_DETECT=1 to go straight to generic multi-hint extract (one API call)
OCR_SKIP_DETECT = os.getenv("OCR_SKIP_DETECT", "0").strip().lower() in ("1", "true", "yes", "on")
# Gemini detect keys below this conf are ignored unless printed-name alias matches.
OCR_DETECT_MIN_CONFIDENCE = int(os.getenv("OCR_DETECT_MIN_CONFIDENCE", "70"))
# Post-extract QA (invoice-level)
OCR_QA_FOOT_TOLERANCE = Decimal(os.getenv("OCR_QA_FOOT_TOLERANCE", "1.00"))
OCR_QA_REVIEW_RATE = float(os.getenv("OCR_QA_REVIEW_RATE", "0.30"))  # fraction of lines flagged
OCR_QA_MISSING_AMOUNT_RATE = float(os.getenv("OCR_QA_MISSING_AMOUNT_RATE", "0.20"))


def _mime(path: Path) -> str:
    return {
        ".jpg": "image/jpeg",
        ".jpeg": "image/jpeg",
        ".png": "image/png",
        ".webp": "image/webp",
        ".heic": "image/heic",
        ".heif": "image/heif",
        ".pdf": "application/pdf",
    }.get(path.suffix.lower(), "image/jpeg")


def _read_media(path: Path) -> tuple[bytes, str]:
    """Load upload bytes + mime. PDFs stay application/pdf (Gemini native)."""
    path = Path(path)
    data = path.read_bytes()
    mime = _mime(path)
    if path.suffix.lower() == ".pdf" or data[:4] == b"%PDF":
        mime = "application/pdf"
    return data, mime


def _get_client():
    from google import genai
    from google.genai import types

    api_key = os.getenv("GEMINI_API_KEY", "").strip()
    if not api_key:
        raise RuntimeError("GEMINI_API_KEY not set (check .env in this app's own directory)")
    return genai.Client(
        api_key=api_key,
        http_options=types.HttpOptions(timeout=90_000),
    )


def _strip_json_fence(raw: str) -> str:
    raw = (raw or "").strip()
    if raw.startswith("```"):
        parts = raw.split("```")
        if len(parts) >= 2:
            raw = parts[1]
            if raw.lstrip().startswith("json"):
                raw = raw.lstrip()[4:]
    return raw.strip()


def _gemini_json(media_bytes: bytes, mime: str, prompt: str, *, label: str) -> Dict[str, Any]:
    """Call Gemini with one image or PDF part + prompt; parse JSON object."""
    from google.genai import types

    client = _get_client()
    logger.info("OCR: %s model=%s mime=%s bytes=%d", label, GEMINI_MODEL, mime, len(media_bytes))
    response = client.models.generate_content(
        model=GEMINI_MODEL,
        contents=[
            types.Part.from_bytes(data=media_bytes, mime_type=mime),
            prompt,
        ],
    )
    raw_text = (response.text or "").strip()
    return json.loads(_strip_json_fence(raw_text))


def detect_vendor(
    image_path: str | Path,
    *,
    vendor_hint: str = "",
) -> Dict[str, Any]:
    """
    Classify invoice vendor via Gemini + registry alias match.

    Returns {vendor_key, vendor_name_printed, confidence, reason, source}.
    Never raises.
    """
    path = Path(image_path)
    # Hint wins if it clearly matches a known vendor
    if vendor_hint:
        hinted = vendor_registry.match_vendor_text(vendor_hint)
        if hinted:
            return {
                "vendor_key": hinted.key,
                "vendor_name_printed": vendor_hint,
                "confidence": 90,
                "reason": "uploader hint alias match",
                "source": "hint",
            }

    if OCR_SKIP_DETECT:
        return {
            "vendor_key": vendor_registry.GENERIC.key,
            "vendor_name_printed": vendor_hint or "",
            "confidence": 0,
            "reason": "OCR_SKIP_DETECT",
            "source": "skip",
        }

    try:
        media_bytes, mime = _read_media(path)
        data = _gemini_json(
            media_bytes,
            mime,
            vendor_registry.detect_prompt(),
            label="vendor-detect",
        )
        printed = str(data.get("vendor_name_printed") or "").strip()
        key_raw = str(data.get("vendor_key") or "").strip().lower()
        try:
            conf = int(data.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0
        conf = max(0, min(100, conf))
        reason = str(data.get("reason") or "").strip()

        # Letterhead NAME match (excludes shared Akron DC address/phone alone).
        from_name = vendor_registry.letterhead_name_vendor(printed)
        from_key = vendor_registry.get_vendor(key_raw)
        key_is_known = from_key.key != vendor_registry.GENERIC.key
        # Trust model key only if confident enough; alias on printed letterhead always OK.
        key_ok = key_is_known and conf >= OCR_DETECT_MIN_CONFIDENCE

        if from_name and key_ok and from_name.key == from_key.key:
            chosen = from_name
            source = "detect+alias"
        elif from_name:
            # Printed letterhead alias wins over a conflicting/low-conf model key
            chosen = from_name
            source = "alias"
            if key_is_known and from_name.key != from_key.key:
                reason = (reason + f"; alias overrides detect key {from_key.key}").strip(
                    "; "
                )
        elif key_ok:
            chosen = from_key
            source = "detect"
        elif key_is_known and not key_ok:
            # Model guessed a catalog key without enough conf and no printed alias
            chosen = vendor_registry.GENERIC
            source = "low_conf"
            reason = (
                reason
                + f"; detect key {from_key.key} conf {conf}<{OCR_DETECT_MIN_CONFIDENCE} → generic"
            ).strip("; ")
        else:
            chosen = vendor_registry.GENERIC
            source = "generic"

        # Refuse catalog keys with no printed letterhead name (kills driver-name hallucinations)
        if (
            chosen.key != vendor_registry.GENERIC.key
            and not printed
            and source == "detect"
        ):
            reason_l = reason.lower()
            if conf < 90 or any(
                w in reason_l
                for w in ("driver", "salesman", "sales rep", "route rep", "esper")
            ):
                chosen = vendor_registry.GENERIC
                source = "no_letterhead"
                reason = (
                    reason + "; no vendor_name_printed — refused key without letterhead"
                ).strip("; ")

        # Tramonte vs Superior shared Akron DC: name required (code enforces prompt rule 8)
        g_key, g_source, g_reason, g_conf = vendor_registry.guard_shared_akron_dc_detect(
            chosen_key=chosen.key,
            printed_name=printed,
            source=source,
            reason=reason,
            confidence=conf,
        )
        if g_key != chosen.key or g_source != source:
            chosen = vendor_registry.get_vendor(g_key)
            source = g_source
            reason = g_reason
            conf = g_conf

        out = {
            "vendor_key": chosen.key,
            "vendor_name_printed": printed,
            "confidence": conf,
            "reason": reason,
            "source": source,
        }
        logger.info(
            "OCR: detect vendor_key=%s printed=%r conf=%s source=%s",
            out["vendor_key"],
            printed,
            conf,
            source,
        )
        return out
    except Exception as e:
        logger.warning("OCR: vendor detect failed: %s", e)
        # Fall back to hint aliases only
        hinted = vendor_registry.match_vendor_text(vendor_hint) if vendor_hint else None
        return {
            "vendor_key": (hinted.key if hinted else vendor_registry.GENERIC.key),
            "vendor_name_printed": vendor_hint or "",
            "confidence": 0,
            "reason": f"detect error: {e}",
            "source": "error",
        }


def _looks_like_upc(value: str) -> bool:
    digits = re.sub(r"\D", "", value or "")
    return len(digits) in (11, 12, 13, 14)


def _upc_a_check_digit(d11: str) -> str:
    """UPC-A check digit for an 11-digit body (GS1)."""
    s = sum(int(d11[i]) * (3 if i % 2 == 0 else 1) for i in range(11))
    return str((10 - (s % 10)) % 10)


def _normalize_upc_digits(value: str) -> str:
    """
    Normalize barcode digits for sheet/PDi match.

    BDI (and some thermals) print 11-digit UPC bodies without the check digit.
    Item Pack Master / other vendors use full 12-digit UPC-A. Append check digit
    when we have exactly 11 digits. Leave 12/13/14 as-is (digits only).
    """
    digits = re.sub(r"\D", "", value or "")
    if len(digits) == 11:
        return digits + _upc_a_check_digit(digits)
    return digits


def _product_id_for_sheet(upc: str, item_code: str) -> str:
    """Sheet 'UPC' column: prefer true UPC/EAN, else vendor ITEM#."""
    u = (upc or "").strip()
    c = (item_code or "").strip()
    if u and _looks_like_upc(u):
        return _normalize_upc_digits(u)
    if u and not c:
        return u
    if c:
        return c
    return u
def _parse_money(value: Any) -> Optional[Decimal]:
    if value is None:
        return None
    s = str(value).strip()
    if not s:
        return None
    s = s.replace("$", "").replace(",", "").strip()
    # parentheses negatives
    if s.startswith("(") and s.endswith(")"):
        s = "-" + s[1:-1].strip()
    try:
        return Decimal(s)
    except (InvalidOperation, ValueError):
        return None


def _sum_line_amounts(items: List[Dict[str, Any]]) -> Decimal:
    total = Decimal("0")
    for it in items:
        a = _parse_money(it.get("amount"))
        if a is not None:
            total += a
    return total


def _reconcile_qty_cost_amount(items: List[Dict[str, Any]]) -> None:
    """
    Shared money-row repair when qty×cost≠amount (tall tickets often misread QTY).
    Prefer integer qty from amount/cost; else net cost from amount/qty.
    """
    for it in items:
        q = _parse_money(it.get("qty_cases"))
        c = _parse_money(it.get("cost_per_pack"))
        a = _parse_money(it.get("amount"))
        if q is None or c is None or a is None:
            continue
        if abs(q * c - a) < Decimal("0.02"):
            continue
        if c != 0:
            implied = (a / c).quantize(Decimal("0.0001"))
            nearest = implied.to_integral_value(rounding=ROUND_HALF_UP)
            if nearest > 0 and abs(implied - nearest) < Decimal("0.02"):
                if abs(nearest * c - a) < Decimal("0.02"):
                    it["qty_cases"] = str(int(nearest))
                    continue
        if q != 0:
            net = (a / q).quantize(Decimal("0.01"))
            if abs(q * net - a) < Decimal("0.02"):
                it["cost_per_pack"] = f"{net}"


def _apply_vendor_item_fixes(vendor: Any, items: List[Dict[str, Any]]) -> None:
    """Deterministic per-vendor line cleanup after Gemini normalize."""
    if vendor.key == "abarta_coke":
        for it in items:
            it["ssp_per_pack"] = ""
            it["ssp_per_unit"] = ""
    if vendor.key == "rl_lipton":
        for it in items:
            pack = str(it.get("ssp_per_pack") or "").strip()
            unit = str(it.get("ssp_per_unit") or "").strip()
            if not pack and unit:
                it["ssp_per_pack"] = unit
    if vendor.key == "red_bull":
        for it in items:
            q = _parse_money(str(it.get("qty_cases") or ""))
            c = _parse_money(str(it.get("cost_per_pack") or ""))
            a = _parse_money(str(it.get("amount") or ""))
            if q is not None and c is not None and a is not None and q != 0:
                if abs(q * c - a) >= Decimal("0.02"):
                    if c > abs(a / q) and abs(a) > 0:
                        net = (a / q).quantize(Decimal("0.01"))
                        it["cost_per_pack"] = f"{net}"
                        conf_i = int(it.get("confidence") or 0)
                        needs = conf_i < LOW_CONFIDENCE_THRESHOLD
                        if not str(it.get("item_code") or "").strip():
                            needs = True
                        if abs(q * net - a) >= Decimal("0.02"):
                            needs = True
                        it["needs_review"] = needs
            units_s = str(it.get("units") or it.get("calculated_qty") or "").strip()
            if units_s.startswith("(") and units_s.endswith(")"):
                units_s = units_s[1:-1].strip()
            if units_s:
                it["units"] = units_s
                it["calculated_qty"] = units_s
                q_int = None
                try:
                    qf = float(str(it.get("qty_cases") or "").strip() or "nan")
                    if qf == qf and qf > 0 and abs(qf - round(qf)) < 1e-9:
                        q_int = int(round(qf))
                except (TypeError, ValueError):
                    q_int = None
                u_val = _parse_money(units_s)
                if q_int and q_int > 0 and u_val is not None:
                    per = (u_val / Decimal(q_int)).quantize(Decimal("1"))
                    if per == per.to_integral_value():
                        it["extracted_qty"] = str(int(per))
                    else:
                        it["extracted_qty"] = f"{per}"
            else:
                if (
                    str(it.get("item_code") or "").strip()
                    or str(it.get("amount") or "").strip()
                ):
                    it["needs_review"] = True
    if vendor.key == "southeast_beverage":
        for it in items:
            pack = str(it.get("ssp_per_pack") or "").strip()
            unit = str(it.get("ssp_per_unit") or "").strip()
            if not pack and unit:
                it["ssp_per_pack"] = unit
                pack = unit
            if not pack and (
                str(it.get("cost_per_pack") or "").strip()
                or str(it.get("amount") or "").strip()
                or str(it.get("item_code") or "").strip()
            ):
                it["needs_review"] = True
    _reconcile_qty_cost_amount(items)


def _foot_target_from_extraction(extraction: Mapping[str, Any]) -> Optional[Decimal]:
    """
    Best-effort invoice product total for footing checks.
    Prefer total_content / picksheet_total (excludes tax/fees) over invoice_total; also scan notes.
    """
    for key in (
        "total_content",
        "Total Content",
        "picksheet_total",
        "Picksheet Total",
        "invoice_total",
        "Invoice Total",
        "amount_due",
        "Amount Due",
    ):
        v = _parse_money(extraction.get(key))
        if v is not None and v > 0:
            return v

    notes = str(extraction.get("notes") or "")
    if not notes:
        return None
    patterns = [
        r"total\s*content\s*[:=]?\s*\$?\s*([0-9,]+\.\d{2})",
        r"picksheet\s*total\s*[:=]?\s*\$?\s*([0-9,]+\.\d{2})",
        r"amount\s*due\s*[:=]?\s*\$?\s*([0-9,]+\.\d{2})",
        r"invoice\s*total\s*[:=]?\s*\$?\s*([0-9,]+\.\d{2})",
    ]
    for pat in patterns:
        m = re.search(pat, notes, flags=re.I)
        if m:
            v = _parse_money(m.group(1))
            if v is not None and v > 0:
                return v
    return None


def qa_check_extraction(extraction: Mapping[str, Any]) -> Dict[str, Any]:
    """
    Shared post-extract quality gate (all vendors).

    Returns a dict suitable for metadata + logging. Does not raise.
    When invoice_needs_review is True, callers should force Needs Review on sheet rows.
    """
    reasons: List[str] = []
    items = [it for it in (extraction.get("line_items") or []) if isinstance(it, dict)]
    n = len(items)

    try:
        overall = int(extraction.get("overall_confidence") or 0)
    except (TypeError, ValueError):
        overall = 0
    overall = max(0, min(100, overall))

    det = extraction.get("detection") if isinstance(extraction.get("detection"), Mapping) else {}
    try:
        det_conf = int(det.get("confidence") or 0)
    except (TypeError, ValueError):
        det_conf = 0
    det_source = str(det.get("source") or "").strip().lower()
    vendor_key = str(extraction.get("vendor_key") or "").strip() or "generic"

    line_review = 0
    missing_amount = 0
    sum_amount = Decimal("0")
    summed_any = False
    for it in items:
        if it.get("needs_review"):
            line_review += 1
        amt_s = str(it.get("amount") or "").strip()
        if not amt_s:
            missing_amount += 1
        a = _parse_money(amt_s)
        if a is not None:
            sum_amount += a
            summed_any = True

    line_review_rate = float(line_review) / n if n else (1.0 if not extraction.get("ok") else 0.0)
    missing_amount_rate = float(missing_amount) / n if n else 0.0

    if not extraction.get("ok"):
        reasons.append("extract_failed")
    if n == 0:
        reasons.append("empty_extract")

    weak_sources = {"generic", "low_conf", "error", "skip", "no_letterhead"}
    if vendor_key == "generic" or det_source in weak_sources:
        reasons.append("weak_detect")
    elif det_conf and det_conf < OCR_DETECT_MIN_CONFIDENCE:
        reasons.append("low_detect_conf")

    if overall and overall < LOW_CONFIDENCE_THRESHOLD:
        reasons.append("low_overall_conf")

    if n and line_review_rate + 1e-9 >= OCR_QA_REVIEW_RATE:
        reasons.append("high_line_review_rate")

    if n and missing_amount_rate + 1e-9 >= OCR_QA_MISSING_AMOUNT_RATE:
        reasons.append("many_missing_amounts")

    foot_target = _foot_target_from_extraction(extraction)
    foot_delta: Optional[Decimal] = None
    if foot_target is not None and summed_any:
        foot_delta = abs(sum_amount - foot_target)
        if foot_delta > OCR_QA_FOOT_TOLERANCE:
            reasons.append("foot_mismatch")

    # De-dupe while preserving order
    seen = set()
    reasons_u: List[str] = []
    for r in reasons:
        if r not in seen:
            seen.add(r)
            reasons_u.append(r)

    invoice_needs_review = bool(reasons_u)
    return {
        "ok": not invoice_needs_review,
        "invoice_needs_review": invoice_needs_review,
        "reasons": reasons_u,
        "item_count": n,
        "line_review_count": line_review,
        "line_review_rate": round(line_review_rate, 4),
        "missing_amount_count": missing_amount,
        "sum_amount": f"{sum_amount:.2f}" if summed_any else None,
        "foot_target": f"{foot_target:.2f}" if foot_target is not None else None,
        "foot_delta": f"{foot_delta:.2f}" if foot_delta is not None else None,
        "vendor_key": vendor_key,
        "detect_confidence": det_conf,
        "detect_source": det_source,
        "overall_confidence": overall,
    }


def _normalize_line_items(data: Mapping[str, Any]) -> List[Dict[str, Any]]:
    items_in = data.get("line_items") or []
    items: List[Dict[str, Any]] = []
    for it in items_in:
        if not isinstance(it, dict):
            continue
        conf = it.get("confidence", data.get("overall_confidence", 50))
        try:
            conf_i = int(conf)
        except (TypeError, ValueError):
            conf_i = 50
        conf_i = max(0, min(100, conf_i))
        upc_raw = str(it.get("upc") or "").strip()
        item_code = str(
            it.get("item_code")
            or it.get("item_no")
            or it.get("item#")
            or it.get("sku")
            or ""
        ).strip()
        if upc_raw and not _looks_like_upc(upc_raw) and not item_code:
            item_code = upc_raw
            upc_raw = ""
        desc = str(it.get("description") or "").strip()
        if not any(
            str(it.get(k) or "").strip()
            for k in ("upc", "item_code", "description", "qty_cases", "amount", "pack_size")
        ) and not item_code and not upc_raw:
            continue
        product_id = _product_id_for_sheet(upc_raw, item_code)
        qty_s = str(it.get("qty_cases") or it.get("quantity") or "").strip()
        cost_s = str(it.get("cost_per_pack") or it.get("unit_cost") or "").strip()
        amount_s = str(it.get("amount") or it.get("total") or "").strip()
        needs = conf_i < LOW_CONFIDENCE_THRESHOLD
        # Empty vendor identity is always review-worthy when the line has product data.
        if not item_code:
            needs = True
        q = _parse_money(qty_s)
        c = _parse_money(cost_s)
        a = _parse_money(amount_s)
        if q is not None and c is not None and a is not None and abs(q * c - a) >= Decimal("0.02"):
            needs = True
        row = {
            "upc": product_id,
            "upc_raw": upc_raw,
            "item_code": item_code,
            "description": desc,
            "pack_size": str(it.get("pack_size") or "").strip(),
            "qty_cases": qty_s,
            "cost_per_pack": cost_s,
            "cost_per_unit": str(it.get("cost_per_unit") or "").strip(),
            "ssp_per_pack": str(it.get("ssp_per_pack") or it.get("suggested_retail") or "").strip(),
            "ssp_per_unit": str(it.get("ssp_per_unit") or "").strip(),
            "amount": amount_s,
            "confidence": conf_i,
            "needs_review": needs,
        }
        # Ticket UNITS / piece count (e.g. Red Bull) → Calculated Qty on sheet
        units_s = str(
            it.get("units")
            or it.get("qty_units")
            or it.get("unit_count")
            or it.get("calculated_qty")
            or ""
        ).strip()
        if units_s:
            row["units"] = units_s
        extracted_s = str(it.get("extracted_qty") or it.get("Extracted Qty") or "").strip()
        if extracted_s:
            row["extracted_qty"] = extracted_s
        # Preserve optional multi-invoice tagging when extract provides it.
        inv_no = str(it.get("invoice_number") or it.get("Invoice Number") or "").strip()
        if inv_no:
            row["invoice_number"] = inv_no
        items.append(row)
    return items


def line_items_for_sheets(extraction: Mapping[str, Any]) -> List[Dict[str, Any]]:
    """Map OCR extraction → sheets.append line_items (incl. confidence / needs_review / vendor)."""
    items = list(extraction.get("line_items") or [])
    overall = int(extraction.get("overall_confidence") or 0)
    force_review = bool(extraction.get("force_needs_review"))
    # Prefer human display name; fall back to printed vendor text then registry key.
    vendor_name = str(
        extraction.get("vendor_display")
        or extraction.get("vendor")
        or extraction.get("vendor_key")
        or ""
    ).strip()
    if items:
        out = []
        for it in items:
            conf = int(it.get("confidence") or overall or 0)
            row = {
                "upc": it.get("upc", ""),
                "description": it.get("description", ""),
                "pack_size": it.get("pack_size", ""),
                "qty_cases": it.get("qty_cases", ""),
                "cost_per_pack": it.get("cost_per_pack", ""),
                "cost_per_unit": it.get("cost_per_unit", ""),
                "ssp_per_pack": it.get("ssp_per_pack", ""),
                "ssp_per_unit": it.get("ssp_per_unit", ""),
                "amount": it.get("amount", ""),
                "ocr_confidence": conf,
                "needs_review": bool(it.get("needs_review", conf < LOW_CONFIDENCE_THRESHOLD))
                or overall < LOW_CONFIDENCE_THRESHOLD
                or force_review,
                "vendor": vendor_name,
            }
            inv_no = str(it.get("invoice_number") or "").strip()
            if inv_no:
                row["invoice_number"] = inv_no
            # Calculated Qty = ticket units / total pieces when present
            calc = str(
                it.get("calculated_qty")
                or it.get("units")
                or it.get("Calculated Qty")
                or ""
            ).strip()
            if calc:
                row["calculated_qty"] = calc
            extq = str(it.get("extracted_qty") or it.get("Extracted Qty") or "").strip()
            if extq:
                row["extracted_qty"] = extq
            out.append(row)
        return out

    return [
        {
            "upc": "",
            "description": extraction.get("notes") or "OCR: no line items extracted",
            "pack_size": "",
            "qty_cases": "",
            "cost_per_pack": "",
            "cost_per_unit": "",
            "ssp_per_pack": "",
            "ssp_per_unit": "",
            "amount": "",
            "ocr_confidence": overall,
            "needs_review": True,
            "vendor": vendor_name,
        }
    ]


def extract_invoice_line_items(
    image_path: str | Path,
    *,
    invoice_number: str = "",
    invoice_date: str = "",
    vendor_hint: str = "",
    vendor_key: str = "",
) -> Dict[str, Any]:
    """
    Detect vendor (unless vendor_key forced) + extract line items.

    Returns normalized dict with vendor_key, detection meta, line_items, etc.
    """
    path = Path(image_path)
    if not path.is_file():
        return {
            "ok": False,
            "error": f"image not found: {path}",
            "line_items": [],
            "overall_confidence": 0,
            "vendor_key": vendor_registry.GENERIC.key,
        }

    detection: Dict[str, Any]
    if vendor_key:
        forced = vendor_registry.get_vendor(vendor_key)
        detection = {
            "vendor_key": forced.key,
            "vendor_name_printed": forced.display_name,
            "confidence": 100,
            "reason": "forced vendor_key",
            "source": "forced",
        }
    else:
        detection = detect_vendor(path, vendor_hint=vendor_hint)

    vendor = vendor_registry.get_vendor(detection.get("vendor_key"))
    media_bytes, mime = _read_media(path)

    full_prompt = vendor_registry.build_extract_prompt(
        vendor,
        invoice_number=invoice_number,
        invoice_date=invoice_date,
        vendor_hint=vendor_hint,
        printed_name=str(detection.get("vendor_name_printed") or ""),
    )
    compact_prompt = vendor_registry.build_compact_extract_prompt(vendor)
    # Full → compact → full again when foot fails (tall tickets drop money lines).
    prompts = [full_prompt, compact_prompt, full_prompt]

    try:
        last_err: Optional[str] = None
        data: Optional[Dict[str, Any]] = None
        items: List[Dict[str, Any]] = []
        best: Optional[tuple] = None  # (score, data, items)
        for i, prompt in enumerate(prompts):
            try:
                cand = _gemini_json(
                    media_bytes,
                    mime,
                    prompt,
                    label=f"extract[{vendor.key}]#{i + 1}",
                )
            except Exception as e:
                last_err = str(e)
                logger.warning("OCR: extract attempt %d (%s) failed: %s", i + 1, vendor.key, e)
                continue
            cand_items = _normalize_line_items(cand)
            _apply_vendor_item_fixes(vendor, cand_items)
            sm = _sum_line_amounts(cand_items)
            ft = _foot_target_from_extraction(cand)
            if ft is not None and ft > 0:
                delta = abs(sm - ft)
            else:
                # No foot target — still accept; prefer larger line count when comparing
                delta = Decimal("999999")
            score = (delta, -len(cand_items))
            if best is None or score < best[0]:
                best = (score, cand, cand_items)
            if ft is not None and ft > 0 and delta <= OCR_QA_FOOT_TOLERANCE:
                logger.info(
                    "OCR: foot ok attempt=%d vendor=%s n=%d sum=%s target=%s delta=%s",
                    i + 1,
                    vendor.key,
                    len(cand_items),
                    sm,
                    ft,
                    delta,
                )
                break
            if i + 1 < len(prompts) and ft is not None and ft > 0:
                logger.warning(
                    "OCR: foot retry vendor=%s attempt=%d n=%d sum=%s target=%s delta=%s",
                    vendor.key,
                    i + 1,
                    len(cand_items),
                    sm,
                    ft,
                    delta,
                )
        if best is None:
            raise RuntimeError(last_err or "Gemini extract failed")
        data = best[1]
        items = best[2]
        assert data is not None

        try:
            overall = int(data.get("overall_confidence") or 0)
        except (TypeError, ValueError):
            overall = 0
        overall = max(0, min(100, overall))

        printed_vendor = str(data.get("vendor") or "").strip() or str(
            detection.get("vendor_name_printed") or ""
        )
        # Re-resolve if extract clearly names a different known vendor.
        # Weak detect must not stick a bad key through extract model_vendor_key.
        det_source = str(detection.get("source") or "")
        try:
            det_conf = int(detection.get("confidence") or 0)
        except (TypeError, ValueError):
            det_conf = 0
        weak_detect = det_source in (
            "generic",
            "low_conf",
            "error",
            "skip",
            "no_letterhead",
        ) or (
            det_conf < OCR_DETECT_MIN_CONFIDENCE
            and det_source in ("detect", "detect+alias")
        )
        resolved = vendor_registry.resolve_vendor_key(
            detected_key="" if weak_detect else vendor.key,
            vendor_name=printed_vendor,
            model_vendor_key="" if weak_detect else str(data.get("vendor_key") or ""),
        )

        inv_no = str(data.get("invoice_number") or "").strip() or invoice_number
        inv_date = str(data.get("invoice_date") or "").strip() or invoice_date

        ship_to_name = str(data.get("ship_to_name") or data.get("customer_name") or "").strip()
        ship_to_address = str(data.get("ship_to_address") or data.get("customer_address") or "").strip()
        ship_to_city = str(data.get("ship_to_city") or "").strip()

        result = {
            "ok": True,
            "vendor": printed_vendor or resolved.display_name,
            "vendor_key": resolved.key,
            "vendor_display": resolved.display_name,
            "detection": detection,
            "invoice_number": inv_no,
            "invoice_date": inv_date,
            "ship_to_name": ship_to_name,
            "ship_to_address": ship_to_address,
            "ship_to_city": ship_to_city,
            "overall_confidence": overall,
            "line_items": items,
            "notes": str(data.get("notes") or "").strip(),
            "model": GEMINI_MODEL,
            # Optional footing anchors when Gemini fills them (or notes parser later)
            "total_content": str(
                data.get("total_content") or data.get("Total Content") or ""
            ).strip(),
            "invoice_total": str(
                data.get("invoice_total") or data.get("Invoice Total") or ""
            ).strip(),
            "picksheet_total": str(
                data.get("picksheet_total") or data.get("Picksheet Total") or ""
            ).strip(),
        }
        logger.info(
            "OCR: done vendor_key=%s printed=%r items=%d conf=%s ship_to_city=%r",
            resolved.key,
            result["vendor"],
            len(items),
            overall,
            ship_to_city or ship_to_address[:40],
        )
        return result
    except Exception as e:
        logger.exception("OCR: Gemini extraction failed: %s", e)
        return {
            "ok": False,
            "error": str(e),
            "line_items": [],
            "overall_confidence": 0,
            "vendor": str(detection.get("vendor_name_printed") or ""),
            "vendor_key": vendor.key,
            "vendor_display": vendor.display_name,
            "detection": detection,
            "invoice_number": invoice_number,
            "invoice_date": invoice_date,
            "ship_to_name": "",
            "ship_to_address": "",
            "ship_to_city": "",
            "notes": "",
        }


def match_store_from_ship_to(
    *,
    ship_to_name: str = "",
    ship_to_address: str = "",
    ship_to_city: str = "",
    known_stores: Optional[List[str]] = None,
) -> Optional[str]:
    """
    Best-effort match of invoice ship-to text to a known store name.
    Longest store name wins (New Concord before Concord-like false hits).
    Does not change routing — callers use this for soft warnings only.
    """
    stores = [str(s).strip() for s in (known_stores or []) if str(s).strip()]
    if not stores:
        return None
    blob = " ".join(
        p for p in (ship_to_name, ship_to_address, ship_to_city) if (p or "").strip()
    ).lower()
    if not blob:
        return None
    for store in sorted(stores, key=lambda s: len(s), reverse=True):
        name = store.lower()
        if name and name in blob:
            return store
    return None


def evaluate_store_routing(
    *,
    upload_store: str,
    extraction: Mapping[str, Any],
    known_stores: Optional[List[str]] = None,
) -> Dict[str, Any]:
    """
    Compare PIN/session store vs OCR ship-to. PIN remains authoritative for sheet tab.
    Soft warning only when ship-to clearly names a *different* known store.
    """
    upload = (upload_store or "").strip()
    matched = match_store_from_ship_to(
        ship_to_name=str(extraction.get("ship_to_name") or ""),
        ship_to_address=str(extraction.get("ship_to_address") or ""),
        ship_to_city=str(extraction.get("ship_to_city") or ""),
        known_stores=known_stores,
    )
    mismatch = bool(matched and upload and matched.lower() != upload.lower())
    warning = ""
    if mismatch:
        warning = (
            f"Ship-to looks like '{matched}' but upload PIN store is '{upload}'. "
            "Rows stayed on the PIN store tab — confirm login store next time."
        )
    return {
        "upload_store": upload,
        "ship_to_name": str(extraction.get("ship_to_name") or "").strip(),
        "ship_to_address": str(extraction.get("ship_to_address") or "").strip(),
        "ship_to_city": str(extraction.get("ship_to_city") or "").strip(),
        "matched_store": matched or "",
        "mismatch": mismatch,
        "warning": warning,
        "sheet_store": upload,  # PIN wins
    }


def _update_metadata(meta_path: Path, patch: Dict[str, Any]) -> None:
    try:
        data = {}
        if meta_path.is_file():
            data = json.loads(meta_path.read_text(encoding="utf-8"))
        data.update(patch)
        meta_path.write_text(json.dumps(data, indent=2, ensure_ascii=False), encoding="utf-8")
    except Exception as e:
        logger.error("OCR: failed to update metadata %s: %s", meta_path, e)


def process_upload_ocr(
    *,
    photo_path: str | Path,
    meta_path: str | Path | None,
    store: str,
    invoice_number: str,
    invoice_date: str,
    timestamp: str | None = None,
    vendor_hint: str = "",
    known_stores: Optional[List[str]] = None,
    skip_placeholder_sheet_row: bool = True,
) -> Dict[str, Any]:
    """Full post-upload pipeline: detect vendor → OCR → Sheets. Never raises."""
    import sheets as sheets_integration

    photo_path = Path(photo_path)
    meta_path_p = Path(meta_path) if meta_path else None
    ts = timestamp or datetime.now(EASTERN).strftime("%Y-%m-%d %H:%M:%S %Z")
    upload_store = (store or "").strip()

    if not OCR_ENABLED:
        logger.info("OCR: disabled via OCR_ENABLED")
        result = {"ok": False, "skipped": True, "reason": "OCR_ENABLED=0"}
        if meta_path_p:
            _update_metadata(meta_path_p, {"ocr": result})
        return result

    try:
        extraction = extract_invoice_line_items(
            photo_path,
            invoice_number=invoice_number,
            invoice_date=invoice_date,
            vendor_hint=vendor_hint,
        )
        store_check = evaluate_store_routing(
            upload_store=upload_store,
            extraction=extraction,
            known_stores=known_stores,
        )
        if store_check.get("mismatch"):
            # Soft flag only — do not reroute sheet tab away from PIN store.
            extraction = dict(extraction)
            extraction["force_needs_review"] = True
            note = store_check.get("warning") or ""
            prev = str(extraction.get("notes") or "").strip()
            extraction["notes"] = f"{prev}; {note}".strip("; ").strip() if prev else note
            logger.warning(
                "OCR: store mismatch upload=%s ship_to_match=%s — PIN routing kept",
                upload_store,
                store_check.get("matched_store"),
            )

        # Invoice-level QA (all vendors) — footing, empty, weak detect, review rate
        qa = qa_check_extraction(extraction)
        if qa.get("invoice_needs_review"):
            extraction = dict(extraction)
            extraction["force_needs_review"] = True
            reasons = ",".join(qa.get("reasons") or []) or "qa"
            qa_note = f"QA review: {reasons}"
            prev = str(extraction.get("notes") or "").strip()
            if qa_note not in prev:
                extraction["notes"] = f"{prev}; {qa_note}".strip("; ").strip() if prev else qa_note
        logger.info(
            "OCR qa invoice_needs_review=%s reasons=%s sum=%s foot_target=%s foot_delta=%s "
            "lines=%s line_review=%s vendor=%s detect_conf=%s overall_conf=%s",
            qa.get("invoice_needs_review"),
            ",".join(qa.get("reasons") or []) or "-",
            qa.get("sum_amount"),
            qa.get("foot_target"),
            qa.get("foot_delta"),
            qa.get("item_count"),
            qa.get("line_review_count"),
            qa.get("vendor_key"),
            qa.get("detect_confidence"),
            qa.get("overall_confidence"),
        )

        line_items = line_items_for_sheets(extraction)
        inv_no = (invoice_number or "").strip() or str(extraction.get("invoice_number") or "").strip()
        inv_date = (invoice_date or "").strip() or str(extraction.get("invoice_date") or "").strip()
        vendor_name = str(
            extraction.get("vendor_display")
            or extraction.get("vendor")
            or extraction.get("vendor_key")
            or ""
        ).strip()

        sheet_res = sheets_integration.append_invoice_to_store_sheet(
            store=upload_store,
            invoice_number=str(inv_no),
            invoice_date=str(inv_date),
            timestamp=ts,
            line_items=line_items,
            vendor=vendor_name,
        )

        out = {
            "ok": bool(extraction.get("ok")) and bool(sheet_res.get("ok")),
            "extraction": {
                "ok": extraction.get("ok"),
                "vendor": extraction.get("vendor"),
                "vendor_key": extraction.get("vendor_key"),
                "vendor_display": extraction.get("vendor_display"),
                "detection": extraction.get("detection"),
                "overall_confidence": extraction.get("overall_confidence"),
                "item_count": len(extraction.get("line_items") or []),
                # Persist normalized lines for audit/regression (uploads/*.json is local).
                "line_items": list(extraction.get("line_items") or []),
                "notes": extraction.get("notes"),
                "error": extraction.get("error"),
                "model": extraction.get("model"),
                "ship_to_name": extraction.get("ship_to_name"),
                "ship_to_address": extraction.get("ship_to_address"),
                "ship_to_city": extraction.get("ship_to_city"),
                "total_content": extraction.get("total_content"),
                "invoice_total": extraction.get("invoice_total"),
                "picksheet_total": extraction.get("picksheet_total"),
                "force_needs_review": bool(extraction.get("force_needs_review")),
            },
            "qa": qa,
            "store_check": store_check,
            "sheets": sheet_res,
            "rows_written": sheet_res.get("rows", 0),
            "finished_at": datetime.now(EASTERN).isoformat(),
        }
        if meta_path_p:
            _update_metadata(meta_path_p, {"ocr": out, "store": upload_store})
        logger.info(
            "OCR pipeline done store=%s inv=%s vendor=%s rows=%s sheet_ok=%s mismatch=%s qa_review=%s",
            upload_store,
            inv_no,
            extraction.get("vendor_key"),
            out.get("rows_written"),
            sheet_res.get("ok"),
            store_check.get("mismatch"),
            qa.get("invoice_needs_review"),
        )
        return out
    except Exception as e:
        logger.exception("OCR pipeline crashed: %s", e)
        out = {"ok": False, "error": str(e)}
        if meta_path_p:
            _update_metadata(meta_path_p, {"ocr": out})
        return out


def enqueue_ocr(**kwargs: Any) -> None:
    t = threading.Thread(
        target=process_upload_ocr,
        kwargs=kwargs,
        name="invupload-ocr",
        daemon=True,
    )
    t.start()


def ocr_status() -> Dict[str, Any]:
    return {
        "enabled": OCR_ENABLED,
        "model": GEMINI_MODEL,
        "low_confidence_threshold": LOW_CONFIDENCE_THRESHOLD,
        "detect_min_confidence": OCR_DETECT_MIN_CONFIDENCE,
        "qa_foot_tolerance": str(OCR_QA_FOOT_TOLERANCE),
        "qa_review_rate": OCR_QA_REVIEW_RATE,
        "api_key_configured": bool(os.getenv("GEMINI_API_KEY", "").strip()),
        "skip_detect": OCR_SKIP_DETECT,
        "vendors": vendor_registry.list_vendors(),
    }
