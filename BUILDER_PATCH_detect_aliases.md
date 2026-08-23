# Builder patch — detect_prompt + alias hardening

**Source:** Reviewer Agent (2026-08-21)  
**Goal:** Stop false vendor keys (driver-name Esber, bare `midvale`, cropped-header overconfidence) without changing sheet schema or extract schemas for production vendors.

**Files to edit:**
1. `vendors.py` — aliases + `detect_prompt()` (+ optional OBD address anchors)
2. `ocr.py` — confidence floor + safer key selection in `detect_vendor()`

**After apply:**
```bash
# restart uvicorn on :8010 (user’s usual pattern)
curl -sS http://127.0.0.1:8010/health | python3 -c "import sys,json; print([v['key'] for v in json.load(sys.stdin)['ocr']['vendors']])"
cd /opt/gassnaptools/upload-app && ./venv/bin/python - <<'PY'
from vendors import match_vendor_text, detect_prompt
assert match_vendor_text('D. ESPER') is None
assert match_vendor_text('customer midvale ohio') is None
assert match_vendor_text('7Up Midvale') and match_vendor_text('7Up Midvale').key == 'seven_up'
assert match_vendor_text('gundy drive').key == 'seven_up'
assert match_vendor_text('OHIO BEVERAGE DISTRIBUTING').key == 'ohio_beverage'
assert match_vendor_text('brecksville, oh 44141').key == 'ohio_beverage'
assert match_vendor_text('440-746-7500').key == 'ohio_beverage'
p = detect_prompt()
assert 'letterhead' in p.lower() and 'driver' in p.lower()
assert 'generic' in p
print('alias + detect_prompt self-check OK')
print('detect_prompt len', len(p))
PY
```

**Do not change:** shared extract schema, sheet columns, `critical_rules` for deep vendors (except OBD aliases/notes if you touch that spec).

**Live tickets to re-test after restart (forced extract if detect still generic):**
- `uploads/20260819_121010_56a4802690.jpg` — must NOT be `esber` from “D. ESPER”; prefer `ohio_beverage` if letterhead/Brecksville visible
- `uploads/20260819_173127_655c7394e2.jpg` — if no letterhead → `generic` @ low conf OK; do not invent Esber/Superior without header (forced `superior_beverage` only for sheet repair after human confirm)

---

## 1) `vendors.py` — `ohio_beverage` aliases

Replace the `ohio_beverage` `aliases=` / `detect_labels=` / optionally strengthen first lines of extract_rules header cues.

**OLD:**
```python
        aliases=(
            "ohio beverage",
            "ohio beverage distributing",
            "ohio bev",
            "obd",
        ),
        detect_labels=("ohio beverage", "ohio beverage distributing", "obd"),
```

**NEW:**
```python
        aliases=(
            "ohio beverage",
            "ohio beverage distributing",
            "ohio bev",
            # Prefer longer forms; bare "obd" is short — keep but never add "esper"
            "ohio beverage dist",
            # Brecksville DC anchors (Parma live ticket 20260819_121010_56a4802690 notes)
            "brecksville, oh 44141",
            "brecksville oh 44141",
            "brecksville, oh",
            "(440) 746-7500",
            "440-746-7500",
            "440 746 7500",
        ),
        detect_labels=(
            "ohio beverage",
            "ohio beverage distributing",
            "ohio bev",
        ),
```

Also drop bare `"obd"` from **detect_labels** (Gemini catalog) so the model is less tempted to guess from 3 letters. Keep `"obd"` **out of aliases too** in the NEW block above (removed) — uploader hint “OBD” can still match via `"ohio bev"` if they type a fuller hint; if you want hint “obd” to work, add only:

```python
# optional: hint-only short form — OK in aliases, NOT ideal alone in detect_labels
"obd",
```

**Reviewer recommendation:** keep `"obd"` in `aliases` for uploader hints, omit from `detect_labels`. Final aliases block:

```python
        aliases=(
            "ohio beverage",
            "ohio beverage distributing",
            "ohio bev",
            "ohio beverage dist",
            "obd",  # short; match_vendor_text only — not listed in detect_labels
            "brecksville, oh 44141",
            "brecksville oh 44141",
            "brecksville, oh",
            "(440) 746-7500",
            "440-746-7500",
            "440 746 7500",
        ),
        detect_labels=(
            "ohio beverage",
            "ohio beverage distributing",
            "ohio bev",
        ),
```

Add one line near the top of OBD `extract_rules` (after VENDOR = …):

```
Letterhead: "OHIO BEVERAGE DISTRIBUTING". DC may show Brecksville, OH 44141 / (440) 746-7500.
Driver / salesman names (e.g. D. ESPER) are NOT the vendor — never map Esper→Esber.
```

---

## 2) `vendors.py` — `seven_up` aliases (drop bare midvale)

**OLD:**
```python
        aliases=(
            "7up",
            "7-up",
            "7 up",
            "seven up",
            "7up midvale",
            "7-up midvale",
            "midvale",
            "gundy dr",
            "gundy drive",
            "splash transport",
        ),
```

**NEW:**
```python
        aliases=(
            "7up",
            "7-up",
            "7 up",
            "seven up",
            "7up midvale",
            "7-up midvale",
            "7 up midvale",
            # Do NOT use bare "midvale" — matches customer city / ship-to noise
            "5554 gundy",
            "gundy dr",
            "gundy drive",
            "midvale, oh 44653",
            "midvale oh 44653",
            "(740) 922-5253",
            "740-922-5253",
            "splash transport",
        ),
```

`detect_labels` stay brand-focused (already OK):
```python
        detect_labels=(
            "7up",
            "7-up",
            "7up midvale",
            "seven up",
        ),
```

---

## 3) `vendors.py` — other alias nits (small, do together)

### `house_of_larose`
Bare `"larose"` / `"la rose"` are OK for letterhead; trailing `"hol "` is fragile.

**Replace aliases with:**
```python
        aliases=(
            "house of larose",
            "house of la rose",
            "house of larose, inc",
            "larose",
            "la rose",
            # avoid bare "hol" word-boundary issues; require fuller form
            "h.o.l.",
            "hol distributing",
        ),
```

### `tramonte`
Ensure short TDI only with beverage context (already `"tdi beverage"`). Add common print forms if missing:
```python
        aliases=(
            "tramonte",
            "tramonte distributing",
            "t.d.i",
            "t.d.i.",
            "tdi beverage",
            "tramonte bev",
            "tramonte distributing inc",
        ),
```
Keep `detect_labels` without bare `"tdi"` only if you currently have it — current is `("tramonte", "tramonte distributing", "tdi")`. **Change detect_labels to drop bare tdi:**
```python
        detect_labels=("tramonte", "tramonte distributing", "tdi beverage"),
```

### `esber` — explicit anti-confusion in extract_rules header (one line)
At top of esber extract_rules after letterhead block, ensure:
```
Do not choose esber from driver/salesman names that merely look like "Esper" / "D. ESPER".
Vendor requires Esber Beverage Company letterhead, Bolivar Road, or esberbeverage.com.
```

No alias change needed for esber (alias match on “D. ESPER” already returns None). Failure was **Gemini detect key**, fixed by detect_prompt + ocr conf floor.

---

## 4) `vendors.py` — replace entire `detect_prompt()` 

**Replace function with:**

```python
def detect_prompt() -> str:
    """Short Gemini prompt: classify invoice vendor from letterhead only."""
    catalog = []
    for v in VENDORS:
        labels = ", ".join(v.detect_labels or v.aliases[:3])
        catalog.append(f'- "{v.key}": {v.display_name} (aka: {labels})')
    catalog.append(f'- "{GENERIC.key}": any other supplier, or letterhead unreadable')
    catalog_txt = "\n".join(catalog)
    return f"""You classify which SUPPLIER issued this invoice / delivery ticket.

LOOK ONLY AT:
- Company logo, letterhead, REMIT TO, sold-from / warehouse address on the vendor side
- Vendor phone/web printed in the header or footer brand block

DO NOT USE (these are NOT the vendor):
- Driver, salesman, route rep, or checker names (e.g. "D. ESPER", "ESPER" ≠ Esber)
- Ship-to / sold-to / customer / store / account name (EAGLE BP, ARCO, VET RETAIL OPS, etc.)
- Product brand names on line items (Bud, Coke, Red Bull product rows alone)
- Bank, check, or payment stub payee unless it clearly matches a catalog supplier letterhead
- Guessing from partial similarity of a person name to a distributor name

RULES:
1. Choose vendor_key ONLY from the catalog below.
2. If letterhead/logo/remit-to is missing, cropped, blurry, or ambiguous → vendor_key="generic" and confidence ≤ 40.
3. confidence ≥ 80 only when the printed supplier name or a unique vendor address/phone clearly matches.
4. confidence 50–79 = partial header cues (address fragment) but name not fully readable.
5. Never pick "esber" unless Esber Beverage / Bolivar / esberbeverage.com (or clear Esber letterhead) is visible — not because a driver name looks similar.
6. Never pick "seven_up" from customer city "Midvale" alone — need 7Up / Gundy Dr / Splash Transport vendor cues.
7. Customer block is ship-to only.

Catalog:
{catalog_txt}

Return JSON only, no markdown:
{{
  "vendor_key": "one of the keys above",
  "vendor_name_printed": "exact supplier name from letterhead/logo, else empty",
  "confidence": integer 0-100,
  "reason": "short — cite letterhead/address evidence, or say unreadable"
}}
"""
```

---

## 5) `ocr.py` — `detect_vendor()` confidence floor + safer selection

Add near top of file with other constants (after `OCR_SKIP_DETECT`):

```python
# Gemini detect keys below this conf are ignored unless printed-name alias matches.
OCR_DETECT_MIN_CONFIDENCE = int(os.getenv("OCR_DETECT_MIN_CONFIDENCE", "70"))
```

**Replace the resolution block inside `detect_vendor` try** (from `printed = ...` through `out = {...}`) with:

```python
        printed = str(data.get("vendor_name_printed") or "").strip()
        key_raw = str(data.get("vendor_key") or "").strip().lower()
        try:
            conf = int(data.get("confidence") or 0)
        except (TypeError, ValueError):
            conf = 0
        conf = max(0, min(100, conf))
        reason = str(data.get("reason") or "").strip()

        from_name = vendor_registry.match_vendor_text(printed)
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
                reason = (reason + f"; alias overrides detect key {from_key.key}").strip("; ")
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
```

### Optional follow-up (same PR if small): extract-time re-resolve when detect was weak

In `extract_invoice_line_items`, after building `printed_vendor`, the existing `resolve_vendor_key(detected_key=vendor.key, ...)` prefers detected_key first. When detection source was `low_conf` / `generic` / `error`, prefer alias on extract `vendor` name.

Minimal change — when calling resolve, if detection source is weak, pass detected_key empty so name can win:

```python
        det_source = str(detection.get("source") or "")
        det_conf = int(detection.get("confidence") or 0)
        weak_detect = det_source in ("generic", "low_conf", "error", "skip") or (
            det_conf < OCR_DETECT_MIN_CONFIDENCE and det_source == "detect"
        )
        resolved = vendor_registry.resolve_vendor_key(
            detected_key="" if weak_detect else vendor.key,
            vendor_name=printed_vendor,
            model_vendor_key=str(data.get("vendor_key") or ""),
        )
```

If `resolve_vendor_key` still prioritizes `model_vendor_key` when detected_key is empty, verify order in `vendors.resolve_vendor_key` — current code tries `detected_key` then `model_vendor_key` then name. For weak detect, also clear weak model key unless conf high:

Actually current resolve:
```python
    for key in (detected_key, model_vendor_key):
        ...
```
So if extract JSON still says `vendor_key: esber` wrongly, it could re-stick. Safer resolve call:

```python
        extract_key = str(data.get("vendor_key") or "").strip().lower()
        # Only honor extract vendor_key when detect was already trusted or name aliases agree
        resolved = vendor_registry.resolve_vendor_key(
            detected_key="" if weak_detect else vendor.key,
            vendor_name=printed_vendor,
            model_vendor_key="" if weak_detect else extract_key,
        )
        # If still generic, allow extract key only via alias on printed_vendor (already done)
```

**Reviewer: include this weak_detect block** — it fixes the “wrong detect sticks through extract” path.

---

## 6) Out of scope for this patch (do not bundle)
- Full OBD/Superior extract_rules expansion
- Sheet replace for the two Parma invoices (separate verify pass)
- New Pepsi/McLane vendors
- Layout fingerprint classifier

---

## 7) Acceptance checks

| Test | Expect |
|------|--------|
| `match_vendor_text("D. ESPER")` | `None` |
| `match_vendor_text("midvale")` | `None` |
| `match_vendor_text("7up midvale")` | `seven_up` |
| `match_vendor_text("440-746-7500")` | `ohio_beverage` |
| `detect_prompt()` | contains driver/letterhead rules |
| Unit: mock detect JSON `{vendor_key:esber, confidence:95, vendor_name_printed:"", reason:"driver D. ESPER"}` | With new logic: key_ok true would still accept esber if conf≥70 — **prompt must stop the model**; conf floor alone does not fix high-conf hallucinations. Rely on prompt rules 5 + empty printed name. Optional stricter rule below. |

### Stricter optional rule (recommended)

If `printed` is empty and `source` would be `detect` only, demote to generic unless conf ≥ 90 **and** reason does not mention driver/salesman:

```python
        if (
            chosen.key != vendor_registry.GENERIC.key
            and not printed
            and source == "detect"
        ):
            reason_l = reason.lower()
            if conf < 90 or any(
                w in reason_l for w in ("driver", "salesman", "sales rep", "route rep", "esper")
            ):
                chosen = vendor_registry.GENERIC
                source = "no_letterhead"
                reason = (
                    reason
                    + "; no vendor_name_printed — refused key without letterhead"
                ).strip("; ")
```

**Include this block** after `chosen`/`source` are set and before building `out`. This is what kills the exact Aug 19 Esber failure (conf 95, printed empty, reason cited driver).

---

## 8) Patch checklist for Builder

- [ ] `seven_up` aliases: remove bare `midvale`; add Gundy/phone/zip anchors  
- [ ] `ohio_beverage` aliases: Brecksville phone/address; detect_labels without relying on bare obd only  
- [ ] `tramonte` detect_labels: no bare `tdi`  
- [ ] `house_of_larose` aliases: drop `"hol "` trailing-space hack  
- [ ] esber extract_rules one-liner: Esper ≠ Esber  
- [ ] Replace `detect_prompt()` with letterhead-only + negatives  
- [ ] `ocr.py`: `OCR_DETECT_MIN_CONFIDENCE`, low-conf → generic, no_letterhead guard, weak_detect re-resolve  
- [ ] Self-check script above green  
- [ ] Restart :8010; `/health` vendors unchanged in count  
- [ ] Spot-check detect on: Esber PDF sample, 7UP PDF, Superior samples, Red Bull, Heidelberg, ABARTA  
- [ ] Spot-check detect on Parma `56a4802690` and `655c7394e2` — log vendor_key + reason  
- [ ] Note results in VALIDATION.md short “Detect hardening 2026-08-21” section (optional but preferred)

---

## 9) Exact combined `detect_vendor` body (copy-paste target)

Full function for Builder convenience (replaces existing `detect_vendor`):

```python
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

        from_name = vendor_registry.match_vendor_text(printed)
        from_key = vendor_registry.get_vendor(key_raw)
        key_is_known = from_key.key != vendor_registry.GENERIC.key
        key_ok = key_is_known and conf >= OCR_DETECT_MIN_CONFIDENCE

        if from_name and key_ok and from_name.key == from_key.key:
            chosen = from_name
            source = "detect+alias"
        elif from_name:
            chosen = from_name
            source = "alias"
            if key_is_known and from_name.key != from_key.key:
                reason = (reason + f"; alias overrides detect key {from_key.key}").strip("; ")
        elif key_ok:
            chosen = from_key
            source = "detect"
        elif key_is_known and not key_ok:
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
        hinted = vendor_registry.match_vendor_text(vendor_hint) if vendor_hint else None
        return {
            "vendor_key": (hinted.key if hinted else vendor_registry.GENERIC.key),
            "vendor_name_printed": vendor_hint or "",
            "confidence": 0,
            "reason": f"detect error: {e}",
            "source": "error",
        }
```

And in `extract_invoice_line_items` after `printed_vendor = ...`:

```python
        det_source = str(detection.get("source") or "")
        try:
            det_conf = int(detection.get("confidence") or 0)
        except (TypeError, ValueError):
            det_conf = 0
        weak_detect = det_source in ("generic", "low_conf", "error", "skip", "no_letterhead") or (
            det_conf < OCR_DETECT_MIN_CONFIDENCE and det_source in ("detect", "detect+alias")
        )
        resolved = vendor_registry.resolve_vendor_key(
            detected_key="" if weak_detect else vendor.key,
            vendor_name=printed_vendor,
            model_vendor_key="" if weak_detect else str(data.get("vendor_key") or ""),
        )
```

(Replace the existing single `resolve_vendor_key(...)` call.)

---

End of Builder patch.
