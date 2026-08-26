# InvUpload validation notes

Ad-hoc evidence (not a pytest suite). Refresh after meaningful OCR/vendor changes.

## Google Sheets write policy

- **Default: append only** — live upload path and re-OCR corrections call `append_invoice_to_store_sheet`.
- **Do not replace** existing invoice rows unless the operator explicitly asks to delete/cleanup duplicates.
- `replace_invoice_rows_for_store` remains for intentional cleanup only (match invoice #s → delete → append).
- Historical notes below may still mention replace from earlier correction passes; new work should append.

## Post-extract QA (all vendors)

Runs in `process_upload_ocr` after extract + store_check, before sheets:

| Check | Reason code |
|-------|-------------|
| extract `ok=false` | `extract_failed` |
| 0 line items | `empty_extract` |
| vendor `generic` / weak detect source | `weak_detect` |
| detect conf &lt; 70 | `low_detect_conf` |
| overall conf &lt; 70 | `low_overall_conf` |
| ≥30% lines already needs_review | `high_line_review_rate` |
| ≥20% lines missing amount | `many_missing_amounts` |
| \|sum(amount) − total_content/invoice_total\| &gt; $1 | `foot_mismatch` |

On any reason: `force_needs_review=True` (all rows Needs Review=TRUE), `ocr.qa` in metadata, log:
`OCR qa invoice_needs_review=True reasons=... sum=... foot_target=...`

Env knobs: `OCR_QA_FOOT_TOLERANCE` (default 1.00), `OCR_QA_REVIEW_RATE` (0.30), `OCR_QA_MISSING_AMOUNT_RATE` (0.20).

## Tramonte — ARCO East Ave multipage PDF `20260822_234211_f8d6a5a95a`

| Field | Value |
|-------|--------|
| File | `uploads/20260822_234211_f8d6a5a95a.pdf` (orig `Tramonte Arco akron .pdf`) |
| Store PIN | ARCO |
| Ship-to | VET RETAIL OPS LLC / ARCO / 2215 EAST AVE AKRON OH 44314 |
| Vendor detect | `tramonte` @ 100 (letterhead TRAMONTE DISTRIBUTING CO.) |
| Layout | `ITEM#\|QTY\|DESC\|UPC\|SSP\|PRICE\|DEP\|AMOUNT` (Akron DEP thermal; no per-line DISC/NET) |
| Pages | 2 picklists same warehouse as Superior Akron (1267 S Main / 330-535-3103) — letterhead name decides |
| Inv p1 | **10554909** · 19 lines · Cases **34** · Content$/Beer$/Picksheet **$713.58** |
| Inv p2 | **10554910** · 13 lines · Cases **20** · Content$/Beer$/Picksheet **$600.86** (incl 41013 qty0 OOS) |
| Forced extract | **32** lines · sum **$1314.44** · conf 98 · **0** MM · **0** NR · per-line `invoice_number` split 19+13 |
| Money map | cost=**PRICE**; ssp=SSP; amount=AMOUNT (=QTY×PRICE); do not subtract footer Discount$ |
| Schema | Full `extract_schema_block` + compact skeleton both include optional line `invoice_number` (multipage) |
| Shared DC detect | Code requires letterhead name for tramonte/superior on shared Akron warehouse — address/phone alone does not lock |
| Live first pass | page1-only 19 rows / $713.58 (stub rules) → sheet `Inv - ARCO` |
| Backfill | append inv **10554910** only (**13** rows) after multipage harden — no replace |
| Date checked | 2026-08-22 |

Anchors p1: 30210 cost 33.58 amt 33.58; 43011 4×27.19=108.76; 41341 28.79.  
Anchors p2: 76211 28.79; 41013 qty0 amt0; 41051 2×28.75=57.50; 46050 2×26.82=53.64.

**Re-spot-check** Tramonte anchors above if extract path / schema / compact prompt changes.

## Ohio Beverage — ARCO East Ave PDF `20260822_190933_7df92c1520`

| Field | Value |
|-------|--------|
| Store | ARCO (PIN) |
| Ship-to | ARCO / 2215 East Ave Akron OH 44314 |
| Inv | **244557** · Wed Aug 19, 2026 |
| Vendor | OHIO BEVERAGE DISTRIBUTING · 6745 Southpointe Pkwy Brecksville |
| Layout | `ITEM#\|QTY\|PACK\|DESC\|SSP\|PRICE\|DISC\|DEP\|EXT` |
| Lines | **35** |
| Sum EXT | **$1738.62** = Total Content = Invoice Total = check |
| Cases | **61** |
| Money map | cost = PRICE − DISC; amount=EXT; ssp=SSP |
| qty×cost MM | **0** (after harden) |
| QA | ok, foot_delta **0.00** |
| Sheet | `Inv - ARCO` live 35 (list-cost noise) + **append clean 35** |

First live pass used list PRICE as cost → high_line_review_rate. Fix: VendorSpec PRICE−DISC (like Lipton).

## R.L. Lipton — ARCO East Ave PDF `20260822_185810_49eed16da1`

| Field | Value |
|-------|--------|
| Store | ARCO (PIN) |
| Ship-to | ARCO EAST AVE / 2215 EAST AVE AKRON OH 44314 |
| Inv | **443513** · Wed Aug 19, 2026 |
| Vendor | R.L. Lipton Distributing (Valley View) |
| Lines | **18** soft drinks |
| Sum TOTAL | **$454.35** = Picksheet Total = Total Content |
| Soft Drink cases | **31** (= sum qty) |
| Money map | cost = PRICE − DISC (e.g. 17.00−1.25=**15.75**); amount=TOTAL; S.S.→ssp |
| qty×cost MM | **0** |
| QA | ok, foot_delta **0.00**, invoice_needs_review=false |
| Sheet | `Inv - ARCO` **18 rows** (live append) |
| Fix after live | ssp often landed in ssp_per_unit only → rules + unit→pack normalize |

No sheet replace (append-only). SSP harden needs `sudo systemctl restart gassnap-upload` for next uploads.

## Superior Beverage — Killbuck multipage PDF `20260822_031623_ec39b6eebc`

| Field | Value |
|-------|--------|
| File | `uploads/20260822_031623_ec39b6eebc.pdf` (orig `Superior killbuck .pdf`) |
| Store PIN | Killbuck |
| Vendor detect | `superior_beverage` @ 100 (SUPERIOR BEVERAGE / Glenwillow letterhead) |
| Invoice# | **3754642** |
| Layout | A — `ITEM#\|QTY\|DESC\|UPC\|SSP\|PRICE\|DISC\|NET\|AMOUNT` |
| Pages | 1=check payee Superior $3485.12; 2–3=product table (image-only PDF) |
| Extract | **74** lines, conf 98, sum AMOUNT **$3485.12**, qty 122 |
| Foot | Total Content $3485.12 = Invoice Total = check amount |
| Math | **0** qty×cost mismatches, **0** needs_review |
| cost policy | **NET** (not list PRICE); e.g. 15803 cost 35.18 not 38.38; 03541 cost 19.19 |
| Sheets | live append `Inv - Killbuck` **74** rows (no re-write needed) |
| Date checked | 2026-08-22 |

Anchors: 15803 NET 35.18; 109582 55.95; 03004 2×23.99=47.98; 32420 10×15.99=159.90; 13753 8×28.79=230.32.

## Superior Beverage — sample `20260727_002200_098f3e9d6e.jpeg`

| Field | Value |
|-------|--------|
| Photo | `uploads/20260727_002200_098f3e9d6e.jpeg` |
| Vendor detect | `superior_beverage` (header SUPERIOR BEVERAGE) |
| Forced extract | `vendor_key=superior_beverage` |
| Result | ok, **15** line items, overall_confidence **98** |
| Date checked | 2026-07-27 |

### Example lines (spot-check vs ticket)

Ticket columns: `ITEM# QTY DESCRIPTION U.P.C. SSP PRICE DISC NET AMOUNT`  
Mapping: cost=`PRICE`, amount=`AMOUNT`, ssp=`SSP`, pack from wrapped desc line B.

| item_code | upc | pack_size | cost_per_pack (PRICE) | ssp_per_pack | amount | description |
|-----------|-----|-----------|------------------------|--------------|--------|-------------|
| 03000 | 071990170370 | STUBBY 2/12 NR | 23.19 | 13.99 | 22.39 | COORS BANQUET |
| 03541 | 071990316006 | 24 PK CAN FLAT #104 | 19.99 | 23.99 | 19.19 | COORS LT |
| 93048 | 635985258919 | 23.5 CN | 28.75 | 2.99 | 57.50 | MIKES HARDER MANGO |

Anchors: all three match printed PRICE (not NET), UPC, pack token, SSP, amount.

### Store soft-warning (PIN vs ship-to)

Invoice ship-to (OCR):

- name: `VET RETAIL OPS LLC`
- city: `NEWCOMERSTOWN`
- address includes `550 E STATE ST` / Duchess 1220

Simulated upload store **Killbuck** (PIN session):

```json
{
  "upload_store": "Killbuck",
  "matched_store": "Newcomerstown",
  "mismatch": true,
  "sheet_store": "Killbuck",
  "warning": "Ship-to looks like 'Newcomerstown' but upload PIN store is 'Killbuck'. Rows stayed on the PIN store tab — confirm login store next time."
}
```

- Sheet tab stays **Killbuck** (PIN authoritative)
- `force_needs_review` → all 15 sheet rows `Needs Review=TRUE`
- No frontend delay (background OCR only)

### Compact-prompt retry safeguards

`build_compact_extract_prompt` always includes (not only truncated vendor tips):

- `cost_per_pack=PRICE … NEVER use NET/DISC`
- wrapped description → `pack_size` ownership
- `ship_to_*` customer block
- shared JSON shape including `ship_to_name/address/city`

### Metadata audit fields

After OCR pipeline, `uploads/{id}.json` → `ocr.extraction` includes:

- `line_items` (full normalized list)
- `ship_to_*`
- `store_check` sibling on `ocr`

---

## Superior Beverage — Akron ARCO East Ave `20260729_223234_5d8a7de7dd.jpg`

| Field | Value |
|-------|--------|
| Photo | `uploads/20260729_223234_5d8a7de7dd.jpg` (sideways phone; upright crop used for re-extract) |
| Store / PIN | **ARCO** → tab `Inv - ARCO` |
| Invoice # | **10549752** (operator-entered) |
| Invoice date | 2026-07-27 |
| Vendor detect | `superior_beverage` @ ~95 (Akron address 1267 S. Main + (330) 535-3103) |
| Layout | **B) DEP thermal:** `ITEM# \| QTY \| DESCRIPTION \| UPC \| SSP \| PRICE \| DEP \| AMOUNT` |
| Result | **40** line items, sum amount **$1844.69**, sum qty **80** (= Beer$/Invoice Total / Cases) |
| Sheet fix | `replace_invoice_rows_for_store` deleted **37** first-pass rows, wrote **40** clean |
| Date checked | 2026-07-30 |

### Example anchors (spot-check vs ticket)

| item_code | upc | qty | pack | cost (PRICE) | ssp | amount | description |
|-----------|-----|-----|------|--------------|-----|--------|-------------|
| 30210 | 635985804598 | 2 | 6PK CAN | 33.58 | 10.49 | 67.16 | MIKE BLK CHERRY |
| 43011 | 635985800262 | 2 | 12PK CAN | 28.79 | 17.99 | 57.58 | WC #3 VARIETY |
| 41013 | 080660957159 | 5 | 12PK CAN | 27.19 | 16.99 | 135.95 | MOD ESP |
| 43073 | 080660957111 | 6 | 18PK CANS | 17.59 | 21.99 | 105.54 | MOD ESP |
| 37024 | 635985260684 | 2 | 16OZ CAN | 38.18 | 1.99 | 76.36 | MXD LIIT |
| 41057 | 080660954011 | 1 | 7OZ 6PK | 22.38 | 6.99 | 22.38 | CORONITA EXTRA |
| 41051 | 080660956411 | 1 | 24Z NR | 28.75 | 2.99 | 28.75 | CORONA EXTRA |

Ship-to: VET RETAIL OPS LLC ARCO / 2275 EAST AVE / AKRON — matches PIN store ARCO (no mismatch flag).

### First-pass failures (why dual-layout rules matter)

1. Glenwillow-only rules assumed DISC/NET; Akron has **DEP**.
2. Sideways thermal dropped MOD ESP 18PK ($105.54) and mis-read MXD LIIT qty (1 vs 2 → $38.18 vs $76.36).
3. Dense ITEM# column: model reused prior ITEM# across adjacent products.
4. Pack tokens live inside DESCRIPTION (no wrap line B).

### How to re-run spot-check

```bash
cd /opt/gassnaptools/upload-app
./venv/bin/python - <<'PY'
import json
from pathlib import Path
meta=json.loads(Path('uploads/20260729_223234_5d8a7de7dd.json').read_text())
items=meta['ocr']['extraction']['line_items']
total=sum(float(i.get('amount') or 0) for i in items)
qty=sum(float(i.get('qty_cases') or 0) for i in items)
print(meta['ocr']['extraction']['vendor_key'], len(items), round(total,2), qty)
assert abs(total-1844.69)<0.02 and abs(qty-80)<0.02
for code in ('30210','43011','43073','37024','41057'):
    it=next(x for x in items if x.get('item_code')==code)
    print(code, it.get('qty_cases'), it.get('cost_per_pack'), it.get('amount'), it.get('upc'))
PY
```

Restart uvicorn after `vendors.py` edits before trusting **new** live Superior uploads.

## How to re-run spot-check (Glenwillow)

```bash
cd /opt/gassnaptools/upload-app
./venv/bin/python - <<'PY'
from ocr import extract_invoice_line_items, evaluate_store_routing
r = extract_invoice_line_items(
    "uploads/20260727_002200_098f3e9d6e.jpeg",
    vendor_key="superior_beverage",
)
print(r["ok"], len(r["line_items"]), r.get("ship_to_city"))
print(evaluate_store_routing(
    upload_store="Killbuck",
    extraction=r,
    known_stores=["Killbuck","Newcomerstown","ARCO","Loudonville","Parma","New Concord","Shreve"],
))
for code in ("03000","03541","93048"):
    it = next(x for x in r["line_items"] if x.get("item_code")==code)
    print(code, it.get("pack_size"), it.get("cost_per_pack"), it.get("ssp_per_pack"), it.get("amount"), it.get("upc_raw") or it.get("upc"))
PY
```

Restart uvicorn after `vendors.py` / `ocr.py` / `main.py` edits before trusting live uploads.

---

## Red Bull Distribution — sample `20260727_011518_ec9b2466a3.jpeg`

| Field | Value |
|-------|--------|
| Photo | `uploads/20260727_011518_ec9b2466a3.jpeg` |
| Vendor detect | `red_bull` @ 100 (Red Bull Distribution Company Inc.) |
| Forced extract | 11 lines, overall_confidence **98** |
| Store check | ship-to Killbuck + PIN Killbuck → mismatch false |
| Date checked | 2026-07-27 |

Ticket columns: `ID QTY UNITS DESCRIPTION PRICE DEP DISC SUGAR TOTAL`  
UPC sits under description; QTY is fractional cases.

### Example lines

| item_code | upc | pack_size | qty_cases | cost_per_pack | amount | description |
|-----------|-----|-----------|-----------|---------------|--------|-------------|
| RB247584 | 611269003123 | 12OZ LS | 0.87 | 2.28 | 41.73 | PINK 12OZ LS |
| RB243201 | 611269002140 | 12OZ LS | 0.12 | 2.28 | 5.96 | SF AMBER 12OZ LS |
| RB247585 | 611269003147 | 8.4OZ 4PK | 0.66 | 1.75 | 23.33 | PINK 8.4OZ 4PK |

Notes: first live upload hit **generic** (before registry entry). After `red_bull` VendorSpec, detect+extract both resolve correctly. Minor OCR digit noise possible on dense thermal lines (e.g. one UPC/amount off-by-one) — structure/mapping is solid.

```bash
cd /opt/gassnaptools/upload-app
./venv/bin/python - <<'PY'
from ocr import detect_vendor, extract_invoice_line_items
print(detect_vendor("uploads/20260727_011518_ec9b2466a3.jpeg"))
r = extract_invoice_line_items(
    "uploads/20260727_011518_ec9b2466a3.jpeg", vendor_key="red_bull"
)
print(r["ok"], r["vendor_key"], len(r["line_items"]), r["overall_confidence"])
for it in r["line_items"][:3]:
    print(it["item_code"], it.get("upc_raw") or it["upc"], it["pack_size"], it["qty_cases"], it["cost_per_pack"], it["amount"], it["description"])
PY
```

## Red Bull Distribution — Loudonville photo `20260824_180544_7bb9a02bb7.jpeg`

| Field | Value |
|-------|--------|
| File | `uploads/20260824_180544_7bb9a02bb7.jpeg` |
| Store PIN | **Loudonville** (operator prompt may say Newcomerstown — trust upload) |
| Ship-to | Loudonville Marathon / 236 N Union St |
| Detect | `red_bull` @ 95 (Red Bull Distribution Company Inc.) |
| Invoice | **2037214470** · 08/24/2026 · banner NOT AN INVOICE (still extract) |
| Layout | ID\|QTY\|**UNITS**\|DESC\|PRICE\|DEP\|DISC\|SUGAR\|TOTAL — full-case QTY=1 + DISC |
| Lines | **4** |
| Sum TOTAL | **$183.79** = INVOICE = TOTAL DUE (footer DISCOUNT $25 already in line nets) |
| Units | ticket UNITS **24+24+24+12=84** = footer Units Delivered |
| Money map | amount=TOTAL; cost=**PRICE−DISC**; qty=QTY not UNITS |
| Qty map | **UNITS → Calculated Qty**; when QTY whole, **Extracted Qty = UNITS÷QTY** (QTY=1 → same as UNITS) |
| Live first pass | 4 lines sum OK but cost=list PRICE → NR; **UNITS not written to sheet** |
| Operator gold | Manual **Calculated Qty** 24/24/24/12 on inv 2037214470 (matches ticket UNITS) |
| Pipeline harden | extract `units` + map to Calculated Qty; empty units → needs_review; sheets 18-col write |
| Date checked | 2026-08-24; units operator-verify 2026-08-26 |

### Anchors (cost + **operator UNITS / Calculated Qty**)

| item_code | qty | UNITS (=Calculated Qty) | list PRICE | DISC | cost net | amount |
|-----------|-----|-------------------------|------------|------|----------|--------|
| RB234435 | 1 | **24** | 58.71 | 8.00 | **50.71** | 50.71 |
| RB248998 | 1 | **24** | 58.71 | 8.00 | **50.71** | 50.71 |
| RB1718 | 1 | **24** | 45.99 | 8.00 | **37.99** | 37.99 |
| RB36463 | 1 | **12** | 45.38 | 1.00 | **44.38** | 44.38 |
| (packet) | 4 | **84** | — | — | — | **183.79** |

Note: Item Pack Master says Extracted Qty = units/case. For QTY=1 full cases, Extracted Qty should be **24/24/24/12** (same as UNITS), not 1. Operator sheet had Extracted=1 on some rows — treat Calculated Qty as the UNITS gold; pipeline sets both when QTY is whole.

---

## Heidelberg Distributing — sample `20260727_013531_844932cf53.jpeg`

| Field | Value |
|-------|--------|
| Photo | `uploads/20260727_013531_844932cf53.jpeg` |
| Vendor detect | `heidelberg` @ 100 (HEIDELBERG CLEVELAND) |
| Forced extract | **17** lines, overall_confidence **98** |
| Store check | Killbuck ship-to + PIN → no mismatch |
| Date checked | 2026-07-27 |

Ticket columns: `ITEM# QTY DESCRIPTION U.P.C. RETAIL PRICE DEP AMOUNT`  
Critical: **RETAIL → ssp_per_pack**, **PRICE → cost_per_pack** (do not swap).

### Example lines

| item_code | pack_size | qty | cost (PRICE) | ssp (RETAIL) | amount | description |
|-----------|-----------|-----|--------------|--------------|--------|-------------|
| 65704 | 16OZ 12PK NR | 1 | 17.40 | 2.39 | 17.40 | CALYPSO PARADISE PUNCH LEMO |
| 65710 | 16OZ 12PK NR | 0 | 17.40 | 2.39 | 0.00 | CALYPSO GRAPEBERRY… (OOS-style) |
| 101842 | 200ML 48PK | 1 | 134.00 | 3.99 | 134.00 | FIREBALL BLAZIN APPLE |

Notes: first live upload hit **generic**. After `heidelberg` VendorSpec, detect+extract resolve correctly. UPC digits may OCR-noise on dense thermal print; PRICE/RETAIL mapping verified (Fireball 134 cost vs 3.99 retail).

```bash
cd /opt/gassnaptools/upload-app
./venv/bin/python - <<'PY'
from ocr import detect_vendor, extract_invoice_line_items
print(detect_vendor("uploads/20260727_013531_844932cf53.jpeg"))
r = extract_invoice_line_items(
    "uploads/20260727_013531_844932cf53.jpeg", vendor_key="heidelberg"
)
print(r["ok"], r["vendor_key"], len(r["line_items"]), r["overall_confidence"])
for code in ("65704", "65710", "101842"):
    it = next(x for x in r["line_items"] if str(x.get("item_code")) == code)
    print(code, it.get("pack_size"), it.get("qty_cases"), it.get("cost_per_pack"), it.get("ssp_per_pack"), it.get("amount"), it.get("description"))
PY
```

---

## Esber Beverage — sample `20260727_233821_9312ed9e4c.pdf`

| Field | Value |
|-------|--------|
| PDF | `uploads/20260727_233821_9312ed9e4c.pdf` (check + beer + wine) |
| Vendor detect | `esber` @ ~99–100 |
| Extract | **15** lines (9 beer + 6 wine), conf ≥95 |
| Beer inv 559759 | sum **435.45** |
| Wine inv 559649 | sum **219.50** |
| Packet total | **654.95** (= check) |
| Sheet | beer from first upload; wine backfilled inv **559649** only (6 rows) |
| Date checked | 2026-07-27 |

### Beer anchors (559759)

| item_code | qty | cost | ssp | amount | description |
|-----------|-----|------|-----|--------|-------------|
| 11 | 2 | 18.39 | 22.99 | 36.78 | HIGH LIFE 30PK/CAN |
| 70 | 4 | 15.99 | 19.99 | 63.96 | MILLER LITE 16OZ 15PK |
| 1067 | 7 | 22.39 | 13.99 | 156.73 | MILLER LITE C12 |

### Wine anchors (559649)

| item_code | qty | cost | ssp | amount | description |
|-----------|-----|------|-----|--------|-------------|
| 20966 | 1 | 53.32 (WS Case) | 19.99 | 53.32 | FRANZIA CHILLABLE RED 5L |
| 21500 | 6 | 6.66 (Price Bottle) | 9.99 | 42.96 | MON AMI PINK MOSCATO 750ML |
| 24954 | 3 | 3.99 (Price Bottle) | 5.99 | 13.47 | JACOBS CREEK CAB SAUV |

```bash
cd /opt/gassnaptools/upload-app
./venv/bin/python - <<'PY'
from ocr import detect_vendor, extract_invoice_line_items
from decimal import Decimal
print(detect_vendor("uploads/20260727_233821_9312ed9e4c.pdf"))
r = extract_invoice_line_items("uploads/20260727_233821_9312ed9e4c.pdf", vendor_key="esber")
print(r["ok"], r["vendor_key"], len(r["line_items"]), r["overall_confidence"])
print("sum", sum(Decimal(i["amount"]) for i in r["line_items"]))
print([i["item_code"] for i in r["line_items"]])
PY
```

## Esber Beverage — Killbuck dual-sheet photo `20260824_235655_c84021191d.jpeg`

| Field | Value |
|-------|--------|
| File | `uploads/20260824_235655_c84021191d.jpeg` |
| Store PIN | **Killbuck** · ship-to VET RETAIL OPS / 205 W FRONT ST |
| Detect | `esber` @ 95 (Esber Beverage Company letterhead) |
| Invoice | **564942** (wine blue page + beer red page + check) |
| First live | 6 lines · sum **$288.54** · foot_mismatch δ 157.15 · high_line_review_rate (missed High Life; row mix) |
| Forced post-harden | **8** lines · sum **$445.69** · **0** MM · QA foot OK |
| Packet | wine SUBTOTAL **53.32** + beer SUBTOTAL **392.37** = check **445.69** |
| Sheet | first 6 NR rows + append clean **8** (append-only) |
| Date checked | 2026-08-24 |

### Anchors (564942)

| item_code | qty | cost | amount | description |
|-----------|-----|------|--------|-------------|
| 20966 | 1 | 53.32 | 53.32 | FRANZIA CHILLABLE RED 5L (wine) |
| 11 | 7 | 18.39 | 128.73 | HIGH LIFE 30PK CAN |
| 51 | 2 | 19.19 | 38.38 | MILLER LITE SUITCASE C24 |
| 70 | 1 | 15.99 | 15.99 | MILLER LITE 16OZ 15PK |
| 254 | 2 | 12.43 | 24.86 | ICEHOUSE 24OZ C12 |
| 267 | 3 | 12.43 | 37.29 | EDGE 24OZ C12 |
| 364 | 2 | 28.78 | 57.56 | HEINEKEN 2/12PK NR |
| 1067 | 4 | 22.39 | 89.56 | MILLER LITE C12 |

---

## 7UP Midvale — sample `20260728_030651_8d9c43069c.pdf`

| Field | Value |
|-------|--------|
| PDF | `uploads/20260728_030651_8d9c43069c.pdf` |
| Store | Newcomerstown (EAGLE BP ship-to) |
| Detect | `seven_up` @ 99 (7Up Midvale — not Eagle) |
| Extract | **11** lines (10 sales + 1 damage), conf 95 |
| Sales 4012630832 | **$237.55** |
| Damage 4012630833 | **−$28.80** |
| Net | **$208.75** |
| Sheet | **replaced** bad generic: deleted **19** → wrote **11** on `Inv - Newcomerstown` |

### Anchors

| item_code | upc | qty | cost NET | amount | note |
|-----------|-----|-----|----------|--------|------|
| 10000865 | 078000113167 | 1 | 14.58 | 14.58 | 12PK X 2 SK Orange |
| 20042082 | 842595139266 | 3 | 22.10 | 66.30 | LS 12 PAL160 |
| 10001130 | 078000113402 | 24 | | −28.80 | damage credit |

No PKG / pallet / shell rows. First live pass was `generic`/EAGLE BP with 19 noisy rows — corrected via `replace_invoice_rows_for_store`.

## 7UP Midvale — Newcomerstown photo `20260824_150436_99fd482f9b.jpeg`

| Field | Value |
|-------|--------|
| File | `uploads/20260824_150436_99fd482f9b.jpeg` |
| Store PIN | Newcomerstown |
| Ship-to | EAGLE BP / 550 E State St Newcomerstown OH 43832 |
| Detect | `seven_up` @ 95 (letterhead 7up Midvale + Gundy Dr — not Eagle) |
| Invoice | **4012228305** · 08/24/26 · sales-only (no DAMAGE stack) |
| Layout | SALES pack groups: header → many flavors → PKG checksum |
| TOT SALE | cases **31** · units **436** · amount **$623.21** = AMOUNT DUE |
| Check | $623.31 (payment receipt) — ignore for product foot |
| Forced extract (post-harden) | **20** lines · sum **$623.21** · qty **31** · **0** MM · QA foot δ **0.00** |
| Live first pass | 20 lines / sum **$427.03** · foot_mismatch (Ghost pack under-read) → Needs Review |
| Money map | cost=**NET**; ssp=**WHSLE**; qty=**CASES**; upc left / SKU right of slash |
| Sheet | first pass appended 20 incomplete rows on `Inv - Newcomerstown`; append clean 20 after harden (append-only) |
| Date checked | 2026-08-24 |

### Anchors (Ghost pack + foot)

| item_code | qty | NET | amount | note |
|-----------|-----|-----|--------|------|
| 10175089 | 1 | 20.66 | 20.66 | Ghost ElecLimd |
| 10174169 | 4 | 20.66 | 82.64 | Ghost WlchGrap — do not stuff NET=41.32 |
| 10175283 | 2 | 20.66 | 41.32 | Ghost Red berry-ish |
| 20042078 | 3 | 22.00 | 66.00 | Bloom PearScr |
| 10161426 | 2 | 9.72 | 19.44 | 2L RC Cola |
| (packet) | 31 | — | **623.21** | TOT SALE |

---

## ABARTA Coca-Cola — sample `20260728_195542_4926bd4c5b.pdf`

| Field | Value |
|-------|--------|
| PDF | `uploads/20260728_195542_4926bd4c5b.pdf` |
| Store | Parma (VET RETAIL OPS) |
| Detect | `abarta_coke` @ 98 |
| Extract | **36** lines, conf 98 |
| Invoice # | **5349998035** (user typed 1267 — corrected) |
| Sum EXTENDED | **$1188.36** = AMOUNT DUE |
| Sum QTY | **38** = NET PRODUCT QTY |
| TOTAL PRODUCTS (list) | **1898.12** = sum(PRICE×QTY) — not a sheet column |
| SSP policy | **blank** — PRICE is list wholesale, not shelf retail |
| Sheet | replaced generic → clean on `Inv - Parma` as inv **5349998035** |

### Anchors

| MAT# | UPC | qty | NET (cost) | EXTENDED | SSP |
|------|-----|-----|------------|----------|-----|
| 115586 | 049000028928 | 1 | 15.00 | 15.00 | *(empty)* |
| 133145 | 070847811244 | 2 | 34.80 | 69.60 | *(empty)* |
| 146858 | 049000053418 | 1 | 30.00 | 30.00 | *(empty)* |
| *(empty)* | 815154027175 | 1 | 45.22 | 45.22 | Needs Review (no MAT#) |

Map: cost=NET not PRICE; ssp left empty; empty MAT# and |QTY×NET−EXT|>$0.02 → Needs Review.

---

## Detect hardening — 2026-08-21

Patch: `BUILDER_PATCH_detect_aliases.md` applied to `vendors.py` + `ocr.py`.

### Changes
- `detect_prompt()`: letterhead/REMIT TO only; explicit negatives (driver ≠ vendor, ship-to ≠ vendor); Esber/7UP special rules
- `OCR_DETECT_MIN_CONFIDENCE` (default 70): low-conf catalog keys → `generic` (`source=low_conf`)
- No printed letterhead + detect-only: refuse key when conf&lt;90 or reason mentions driver/salesman/esper (`source=no_letterhead`)
- Weak detect must not re-stick via extract `vendor_key` resolve
- Aliases: drop bare `midvale`; OBD Brecksville phone/address; tramonte detect_labels no bare `tdi`; HOL drop `"hol "` hack

### Unit checks (no Gemini)
| Case | Result |
|------|--------|
| mock detect esber conf 95, printed empty, reason driver D. ESPER | `generic` / `no_letterhead` |
| mock esber + printed "Esber Beverage Company" | `esber` |
| mock superior conf 55, printed empty | `generic` / `low_conf` |
| mock key esber + printed OHIO BEVERAGE DISTRIBUTING | `ohio_beverage` / `alias` |
| `match_vendor_text("D. ESPER")` | None |
| `match_vendor_text("midvale")` | None |
| `match_vendor_text("440-746-7500")` | `ohio_beverage` |

### Live re-check after uvicorn restart
- `uploads/20260819_121010_56a4802690.jpg` — must not be esber from driver name
- `uploads/20260819_173127_655c7394e2.jpg` — generic OK if no letterhead
- Regression detect on validated samples: Superior, Heidelberg, Red Bull, Esber PDF, 7UP, ABARTA

---

## Beverage Distributors Inc — sample `20260821_153032_ae66fc2717.pdf`

| Field | Value |
|-------|--------|
| PDF | `uploads/20260821_153032_ae66fc2717.pdf` (check + invoice) |
| Store / PIN | **Parma** → tab `Inv - Parma` |
| Invoice # | **787548** |
| Invoice date | 2026-08-21 |
| Vendor detect | `beverage_distributors` @ 100 (BEVERAGE DISTRIBUTORS INC letterhead) |
| Layout | `ITEM# \| QTY \| DESC \| UPC \| SSP \| PRICE \| DISC \| DEP \| NET \| EXT` |
| Forced extract | **28** lines, conf 98, sum EXT **$1220.64**, sum qty **48** (= Cases footer) |
| Cost policy | **NET** after DISC (not list PRICE); EXT = QTY × NET |
| Check stub | $1238.70 payee BDI (not forced equal to line sum) |
| First pass | `generic` used PRICE→cost → many needs_review; BDI rules → **0** qty×cost mismatches |
| Date checked | 2026-08-21 |

### Anchors (spot-check vs ticket)

| item_code | qty | cost NET | ssp | amount | note |
|-----------|-----|----------|-----|--------|------|
| 00119 | 4 | 23.99 | 14.99 | 95.96 | not PRICE 25.59 |
| 00244 | 6 | 19.19 | 23.99 | 115.14 | DISC 0.80 |
| 00215 | 3 | 12.43 | 1.29 | 37.29 | ICE EDGE |
| 00671 | 2 | 35.18 | 10.99 | 70.36 | HKN 6PK |
| 01017 | 2 | 28.79 | 17.99 | 57.58 | White Claw |
| 02401 | 1 | 28.46 | 2.96 | 28.46 | last line |

Ship-to: PEARL&BRADLEY SUNOCO / EAGLE STORE #85 / 5385 Pearl Rd — matches PIN Parma.

```bash
cd /opt/gassnaptools/upload-app
./venv/bin/python - <<'PY'
from ocr import detect_vendor, extract_invoice_line_items
from decimal import Decimal
print(detect_vendor("uploads/20260821_153032_ae66fc2717.pdf"))
r = extract_invoice_line_items(
    "uploads/20260821_153032_ae66fc2717.pdf", vendor_key="beverage_distributors"
)
print(r["ok"], r["vendor_key"], len(r["line_items"]), r["overall_confidence"])
print("sum", sum(Decimal(i["amount"]) for i in r["line_items"]))
print("qty", sum(Decimal(i["qty_cases"]) for i in r["line_items"]))
it = next(x for x in r["line_items"] if x["item_code"]=="00119")
assert it["cost_per_pack"] == "23.99" and it["amount"] == "95.96"
print("anchor 00119 OK", it["cost_per_pack"], it["ssp_per_pack"], it["amount"])
PY
```

Restart uvicorn after `vendors.py` edits before trusting **new** live BDI uploads. To fix Parma sheet rows from the generic first pass:

```bash
# after restart — replace inv 787548 on Inv - Parma with forced extract
cd /opt/gassnaptools/upload-app && ./venv/bin/python - <<'PY'
from ocr import extract_invoice_line_items, line_items_for_sheets
import sheets
r = extract_invoice_line_items(
    "uploads/20260821_153032_ae66fc2717.pdf",
    vendor_key="beverage_distributors",
    invoice_number="787548",
    invoice_date="2026-08-21",
)
rows = line_items_for_sheets(r)
print(sheets.replace_invoice_rows_for_store(
    store="Parma",
    match_invoice_numbers=["787548"],
    line_items=rows,
    invoice_date="2026-08-21",
    invoice_number="787548",
))
PY
```

---

## R.L. Lipton Distributing — sample `20260821_154840_f0a8f586bc.jpeg`

| Field | Value |
|-------|--------|
| Photo | `uploads/20260821_154840_f0a8f586bc.jpeg` |
| Store / PIN | **Parma** → tab `Inv - Parma` |
| Invoice # printed | **443756** (user typed 443755) |
| Invoice date | 2026-08-20 on ticket / upload 2026-08-21 |
| Vendor detect | `rl_lipton` @ 100 (R.L. Lipton Distributing Company letterhead) |
| Layout | `ITEM# \| QTY \| DESC \| U.P.C. \| S.S. PRICE \| PRICE \| DISC. \| TOTAL` |
| Forced extract | **4** lines, conf 95, sum TOTAL **$119.00**, sum qty **7** |
| Cost policy | **PRICE** wholesale; ssp = **S.S. PRICE** (0.99 unit) — never swap |
| Foot | Soft Drink / Picksheet Total **$119.00**; handwritten $496.16 ignored |
| Date checked | 2026-08-21 |

### Anchors

| item_code | qty | cost PRICE | ssp S.S. | amount | description |
|-----------|-----|------------|----------|--------|-------------|
| 02903 | 3 | 17.00 | 0.99 | 51.00 | ARIZ 24/22 CAN KIWI STRAWBERY |
| 02904 | 1 | 17.00 | 0.99 | 17.00 | ARIZ 24/22 CAN SWEET TEA |
| 02915 | 2 | 17.00 | 0.99 | 34.00 | ARIZ 24/22 CAN DRAGNFRUIT MANG |
| 02925 | 1 | 17.00 | 0.99 | 17.00 | ARIZ 24/22 CAN RIZZLER BERRY |

```bash
cd /opt/gassnaptools/upload-app
./venv/bin/python - <<'PY'
from ocr import detect_vendor, extract_invoice_line_items
from decimal import Decimal
print(detect_vendor("uploads/20260821_154840_f0a8f586bc.jpeg"))
r = extract_invoice_line_items(
    "uploads/20260821_154840_f0a8f586bc.jpeg", vendor_key="rl_lipton"
)
print(r["ok"], r["vendor_key"], len(r["line_items"]), r.get("invoice_number"))
print("sum", sum(Decimal(i["amount"]) for i in r["line_items"]))
it = next(x for x in r["line_items"] if x["item_code"]=="02903")
assert it["cost_per_pack"]=="17.00" and it["ssp_per_pack"]=="0.99" and it["amount"]=="51.00"
print("anchor 02903 OK")
PY
```

After restart — replace generic sheet rows (match both typed + printed inv #):

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python - <<'PY'
from ocr import extract_invoice_line_items, line_items_for_sheets
import sheets
r = extract_invoice_line_items(
    "uploads/20260821_154840_f0a8f586bc.jpeg",
    vendor_key="rl_lipton",
    invoice_number="443756",
    invoice_date="2026-08-20",
)
rows = line_items_for_sheets(r)
print(sheets.replace_invoice_rows_for_store(
    store="Parma",
    match_invoice_numbers=["443755", "443756"],
    line_items=rows,
    invoice_date="2026-08-20",
    invoice_number="443756",
))
PY
```

## Southeast Beverage — Newcomerstown photo `20260824_233516_46e8959b51.jpeg`

| Field | Value |
|-------|--------|
| File | `uploads/20260824_233516_46e8959b51.jpeg` |
| Store PIN | **Newcomerstown** |
| Ship-to | EAGLE BP / NEW CUMBERLAND OH 43832 |
| Letterhead | SOUTHEAST BEVERAGE CO. · P.O. BOX 180 · ATHENS OH 45701 · (740) 593-3353 |
| Invoice | printed **162891** (user typed 162892 — prefer printed) |
| Layout | ITEM#\|QTY\|DESC\|UPC\|**SSP**\|PRICE\|DISC\|UNIT PRICE\|DEP\|EXT |
| First live | `generic` / weak_detect · 10 lines · sum $240.86 · **SSP blank** · Vendor generic |
| Clean append | Vendor **Southeast Beverage Co.** inv **162891** · costs/amounts OK · **SSP still blank from OCR** |
| Operator gold | Manual **SSP per Pack** on inv 162891 sheet rows (2026-08-25) — matches ticket; locked into VendorSpec anchors |
| Pipeline harden | extract_rules + critical_rules SSP column geometry + gold table; empty ssp → `needs_review` for `southeast_beverage` |
| Money map | amount=EXT; cost=**UNIT PRICE**; qty=QTY; ssp=**SSP** (required; small shelf $ left of PRICE) |
| Sheet | generic 10 (162892 NR) + Southeast 10 (162891) append-only — keep operator SSP on 162891 |
| Date checked | 2026-08-24; SSP operator-verify 2026-08-25 |

### Anchors (cost + **operator SSP**)

| item_code | UPC | qty | **SSP** | cost UNIT | amount |
|-----------|-----|-----|---------|-----------|--------|
| 11600 | 858439006380 | 1 | **12.99** | 54.38 | 54.38 |
| 80083 | 075140245147 | 4 | **5.99** | 4.99 | 19.96 |
| 80086 | 075140707027 | 1 | **0.89** | 9.60 | 9.60 |
| 81064 | 850031700260 | 1 | **2.89** | 21.57 | 21.57 |
| 81120 | 840442200893 | 1 | **2.89** | 21.57 | 21.57 |
| 91137 | 810113512884 | 1 | **2.99** | 24.00 | 24.00 |
| 80003 | 883990661006 | 1 | **3.99** | 31.40 | 31.40 |
| 80004 | 883990651205 | 1 | **3.99** | 31.40 | 31.40 |
| 80023 | 074806001615 | 1 | **0.99** | 13.49 | 13.49 |
| 80024 | 074806001622 | 1 | **0.99** | 13.49 | 13.49 |
| (packet) | — | — | — | — | **240.86** |

Future extract gate: all 10 ssp_per_pack filled + 0 MM + foot $240.86. Do not treat money-only foot as complete for this vendor.

---
