# Adding a Vendor (InvUpload)

This is the standard pattern for supporting a new invoice supplier. Follow it so every vendor stays consistent with the shared sheet schema.

## Architecture (do not reinvent)

| Piece | File | Role |
|-------|------|------|
| Registry | `vendors.py` | One `VendorSpec` per supplier |
| Detect | `vendors.detect_prompt()` + `ocr.detect_vendor` | Header/logo → `vendor_key` |
| Extract | `vendors.build_extract_prompt(spec)` | Vendor rules + **shared** JSON schema |
| Normalize / sheets | `ocr.py` + `sheets.py` | Same 15 columns for every vendor |

**Adding a vendor should almost always mean: edit `vendors.py` only, then restart uvicorn.**  
Do not fork prompts inside `ocr.py`. Do not add vendor-specific sheet columns unless product explicitly requires it.

## Shared output schema (stable — all vendors)

Gemini must return this shape (see `extract_schema_block()`):

```json
{
  "vendor": "printed name",
  "vendor_key": "registry_key",
  "invoice_number": "",
  "invoice_date": "YYYY-MM-DD or as printed",
  "ship_to_name": "customer / account name on ship-to block",
  "ship_to_address": "street + city/state/zip from ship-to (not vendor letterhead)",
  "ship_to_city": "city only if readable",
  "overall_confidence": 0,
  "line_items": [
    {
      "upc": "true barcode UPC/EAN only, else empty",
      "item_code": "ITEM# / SKU with leading zeros",
      "description": "product name",
      "pack_size": "e.g. 24/12oz, 2/12 NR",
      "qty_cases": "primary qty (cases when shown)",
      "cost_per_pack": "wholesale pack/case cost (PRICE, not NET)",
      "cost_per_unit": "",
      "ssp_per_pack": "suggested retail / SSP / SRP",
      "ssp_per_unit": "",
      "amount": "line extension",
      "confidence": 0
    }
  ],
  "notes": ""
}
```

`ship_to_*` is used only for a **background soft warning** when the PIN/session store differs from a known store named on the ticket. Sheet tab still follows the PIN.

Sheet columns (one row per line item) — **do not reorder existing columns when adding vendors**;
new global columns may be appended at the end (e.g. Vendor):

```
Timestamp, Store, Invoice Number, Invoice Date,
UPC, Description, Pack Size, Qty (Cases),
Cost per Pack, Cost per Unit, SSP per Pack, SSP per Unit, Amount,
OCR Confidence, Needs Review, Vendor
```

- **Vendor** = registry display name (e.g. `R.L. Lipton Distributing Company`), filled from OCR on every line row.
- Sheet **UPC** cell = real UPC if present, else `item_code` (`_product_id_for_sheet`).
- **Write mode = append** (`append_invoice_to_store_sheet` / `append_rows`). Live uploads never delete prior rows.
- Re-OCR / agent corrections also **append** unless the operator explicitly asks to replace/cleanup.
- `replace_invoice_rows_for_store` (delete by invoice # then append) is **opt-in only** — do not use by default.
- Append uses `value_input_option=RAW` so leading zeros survive.
- Confidence &lt; threshold (default 70) → `Needs Review=TRUE`.
- Header auto-upgrade: if row 1 still has the old 15-col prefix, `_ensure_headers` rewrites header to add `Vendor` at the end (legacy data rows stay column-aligned; Vendor blank until re-written).

## Checklist: new vendor

1. **Get a real sample photo**  
   Upload via `upload.gassnap.io` or drop under `uploads/samples/`. Prefer a full ticket: header + full line table + totals.

2. **Document the layout** (in the `VendorSpec.extract_rules` and optionally a short section below)  
   - Printed legal/brand name(s) and address cues  
   - Exact column headers left→right  
   - Which column is ITEM# vs UPC vs qty vs cost vs SSP vs extension  
   - Wrapped description / pack-size patterns  
   - Rows to skip (fees, deposits, payment blocks, category subtotals)

3. **Append a `VendorSpec` in `vendors.py` → `VENDORS` tuple** (priority = match order):

```python
VendorSpec(
    key="snake_case_key",           # stable id; never rename lightly
    display_name="Human Name",
    aliases=(                       # lowercase substrings for printed-name match
        "full printed name",
        "short form",
        # optional unique address fragment if logo often crops
    ),
    detect_labels=("name gemini may emit",),
    extract_rules="""...full column map + quirks...""".strip(),
    critical_rules=(                # compact-retry only — NEVER truncated
        "COLS: ... short field map; PRICE≠NET; wrap ownership; SKIP footers."
    ),
    notes="Sample id / validation note",
)
```

**`critical_rules` (required for production vendors):** short must-keep instructions injected whole into `build_compact_extract_prompt()`. Put column→field maps, PRICE vs NET, wrap/pack ownership, and footer skip lists here. Do not rely on truncating `extract_rules` for those.

4. **Detection**  
   - `detect_prompt()` auto-includes new keys from the registry.  
   - Aliases correct weak Gemini keys via `match_vendor_text`.  
   - Avoid ultra-short aliases (`"sbg"`, `"ab"`) that false-positive inside other words.

5. **Extract rules quality bar**  
   - Explicit column → JSON field map (prevents PRICE/NET/SSP swaps).  
   - Call out multi-line description packs.  
   - State whether true UPC is present or ITEM#-only.  
   - Tell the model what is *not* a product line.

6. **Restart** the upload app (vendors.py is imported at process start):

```bash
# prod pattern (adjust path if this tree is the live app root)
pid=$(ss -tlnp | sed -n 's/.*:8010 .*pid=\([0-9]\+\).*/\1/p' | head -1)
[ -n "$pid" ] && kill "$pid"
screen -S gassnap-upload -X quit 2>/dev/null || true
screen -dmS gassnap-upload bash -c 'cd /opt/gassnaptools/upload-app && exec ./venv/bin/uvicorn main:app --host 127.0.0.1 --port 8010'
curl -sS http://127.0.0.1:8010/health
```

7. **Verify**
   - Alias unit check: `match_vendor_text("…")` → expected key  
   - Live detect on sample → `vendor_key`  
   - Forced extract: `extract_invoice_line_items(path, vendor_key="…")` with **120s+** timeout  
   - Spot-check: item_code, upc, pack_size ownership, cost vs SSP, amount, item count  
   - Confirm rows on `Inv - {Store}` tab (not Invoice Log)

8. **Update `CONTEXT.md`** supported-vendor list when a vendor is production-ready.

---

## Superior Beverage (reference implementation)

**Registry key:** `superior_beverage`  
**Samples:**
- Glenwillow (DISC/NET + wrap): `uploads/20260727_002200_098f3e9d6e.jpeg`
- **Parma Glenwillow layout A:** `uploads/20260822_003027_0c3c62bec6.jpeg` — inv **3749614**, **28** lines, Total Content **$1226.22** (Invoice Total $1262.57 w/ fees)
- **Killbuck multipage PDF layout A:** `uploads/20260822_031623_ec39b6eebc.pdf` — inv **3754642**, **74** lines, **$3485.12**
- **Akron ARCO East Ave (DEP + pack-in-desc):** `uploads/20260729_223234_5d8a7de7dd.jpg` — inv **10549752**, Invoice Total / Beer$ **$1,844.69**, Cases **80**

### Structure — two DC layouts (same vendor)

**A) Glenwillow DC** — 31031 Diamond Parkway, Glenwillow, OH 44139:
```
ITEM# | QTY | DESCRIPTION | U.P.C. | SSP | PRICE | DISC | NET | AMOUNT
```
Description often wraps: line A brand, line B pack (STUBBY 2/12 NR, …).  
**cost_per_pack = NET** (or PRICE−DISC if NET blurry). List PRICE is **not** cost when DISC/NET present.

**B) Akron DC / thermal house ticket** — 1267 S. Main St, Akron, OH 44301, (330) 535-3103:
```
ITEM# | QTY | DESCRIPTION | UPC | SSP | PRICE | DEP | AMOUNT
```
Pack tokens usually sit **in** the single description line (`MIKE BLK CHERRY 6PK CAN`).  
DEP is deposit (almost always 0.00) — **not** DISC/NET.  
**cost_per_pack = PRICE** on layout B only. Never put DEP into cost.  
Ship-to example: VET RETAIL OPS LLC / ARCO / 2275 EAST AVE / AKRON OH 44314.

### Schema mapping (Superior → shared fields)

| Ticket column | JSON / sheet field | Notes |
|---------------|--------------------|--------|
| ITEM# | `item_code` | Keep leading zeros; **per row** (never reuse prior ITEM#) |
| U.P.C. / UPC | `upc` | Usually real 11–12 digit barcode |
| QTY | `qty_cases` | Cases (0 OK for OOS still listed); leading `/3` → 3 |
| DESCRIPTION | `description` + `pack_size` | Wrap lineB **or** trailing pack token on single line |
| SSP | `ssp_per_pack` | Suggested sell — **never** into cost |
| **NET (A)** / **PRICE (B)** | **`cost_per_pack`** | Layout A: **NET** (never list PRICE when DISC/NET present). Layout B: **PRICE** |
| AMOUNT | `amount` | Line extension (=QTY×cost) |
| DISC (layout A) | (not mapped) | Baked into NET |
| DEP (layout B) | (not mapped) | Deposit only |

Sheet UPC column receives true UPC when present (Superior usually has it).  
Shared sheet columns unchanged — one row per line item. Foot product sum ≈ **Total Content**, not fee-inflated Invoice Total (Parma 3749614: $1226.22 content / $1262.57 invoice).

### Detection

- Header `SUPERIOR BEVERAGE` / `Superior Beverage Group`
- Aliases: superior beverage/group; Glenwillow `diamond parkway` / `31031 diamond` (unique DC — OK for identity)
- Akron `1267 s. main` / `(330) 535-3103` remain on the VendorSpec for extract context / weak cues, but **detect code requires letterhead NAME** on the shared Akron DC (same warehouse as Tramonte) — address/phone alone → `generic` / `no_letterhead`, not a locked `superior_beverage`
- Customer block (ARCO / VET RETAIL OPS / Pearl Rd Parma) is **ship-to**, not vendor
- Shared schema: optional per-line `invoice_number` (full + compact extract) for multi-invoice packets

### Gemini prompt

Built automatically:

`build_extract_prompt(get_vendor("superior_beverage"))`

= Superior `extract_rules` (dual layout) + shared schema + global rules.  
Compact retry always keeps full `critical_rules` (layout-A NET≠list PRICE; layout-B PRICE≠DEP; pack ownership; footer skips).

### Known OCR pitfalls (Superior)

1. **Dual layout** — do not invent DISC/NET on Akron DEP tickets; do not put DEP into cost.  
2. **Wrapped pack lines (Glenwillow)** — pack_size stays with its ITEM#, not the next row.  
3. **Pack-in-desc (Akron)** — split trailing `6PK CAN` / `18PK CANS` / `19.2OZ CAN` into pack_size.  
4. **Layout A PRICE vs NET** — cost_per_pack = **NET** (or PRICE−DISC), **never list PRICE** when DISC/NET present (barefoot 8.66 not 11.33; high noon 45.10 not 58.63).  
5. **Long thermals** — extract every product row until PAYMENT; foot near Total Content / Invoice Total as appropriate (ARCO layout B $1844.69 / 80 cases). Sideways phone photos are harder — upright helps.  
6. **ITEM# reuse across rows** — each physical line has its own ITEM#; never copy the previous code.  
7. **PAYMENT / Beer$ / Cases count / SIN TAX / SPLIT CASE fee footers** — not product lines.  
8. **Operator invoice #** — prefer uploader-entered value over OCR when non-empty.  
9. **Sheet dual cohorts (Parma 3749614)** — historical list-PRICE appends may sit beside clean NET rows; default **append-only** — replace/cleanup only on explicit operator ask.

---

## Red Bull Distribution Company (reference)

**Samples:**
- Killbuck load sheet `uploads/20260727_011518_ec9b2466a3.jpeg` — fractional QTY + unit PRICE
- Loudonville photo `uploads/20260824_180544_7bb9a02bb7.jpeg` — inv **2037214470**, 4 lines, TOTAL DUE **$183.79**, case PRICE−DISC

**Registry key:** `red_bull`

### Structure

```
Red Bull Distribution Company Inc.     [customer ship-to top-right]
...
ID  QTY  UNITS  DESCRIPTION  PRICE  DEP  DISC  SUGAR  TOTAL
RB234435  1  24  RED 12OZ LS   $58.71  $0  $8.00  $0  $50.71
                      611269…          [UPC under desc]
...
Units Picked Up / DISCOUNT / INVOICE / TOTAL DUE   ← not product lines
```

Banner may say **NOT AN INVOICE** — still extract products.

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| ID (`RB…`) | `item_code` |
| UPC under description | `upc` |
| QTY (fractional or whole cases) | `qty_cases` — never UNITS |
| **UNITS** | `units` → sheet **Calculated Qty** (total pieces); required |
| UNITS ÷ whole QTY | sheet **Extracted Qty** (units per case; QTY=1 → same as UNITS) |
| UNITS `(n)` legacy note | ignore UNITS as qty_cases only |
| DESCRIPTION | `description` + pack → `pack_size` |
| **cost** | dual: fractional/unit → PRICE; full-case with DISC>0 → **PRICE−DISC** (TOTAL often = net) |
| TOTAL | `amount` |
| DEP/SUGAR | not mapped |
| SSP | usually empty |

Operator Loudonville inv **2037214470** hand-filled Calculated Qty **24/24/24/12** from ticket UNITS (footer Units Delivered 84). Empty units → needs_review.

### Detection

Aliases: red bull, redbull, RBDC, distribution company name.  
Live Loudonville 2026-08-24: `red_bull` @ 95 detect+alias.

### Pitfalls

1. Do not put RB ID into `upc` — UPC is the 61126… digits under the name.  
2. QTY is cases (fractional or whole); UNITS is unit count — never qty_cases=UNITS (24).  
3. Case delivery with DISC: cost must be net (PRICE−DISC), not list PRICE — else qty×cost floods Needs Review.  
4. Pickup/return tickets still have product rows (parens on TOTAL).  
5. Footer DISCOUNT is sum of line DISC already in TOTALS — do not subtract again from packet foot.  
6. Trust upload store PIN (this sample = **Loudonville**, even if operator said Newcomerstown).

### Sample totals (2026-08-24 Loudonville)

Inv **2037214470**: **4** lines · sum **$183.79** = TOTAL DUE · 0 MM after PRICE−DISC harden · check $183.79.

---

## Heidelberg Distributing (reference)

**Sample:** `uploads/20260727_013531_844932cf53.jpeg` (Killbuck Marathon picksheet).  
**Registry key:** `heidelberg`

### Structure

```
HEIDELBERG CLEVELAND
9101 E. PLEASANT VALLEY RD, INDEPENDENCE, OH 44131
...
ITEM#  QTY  DESCRIPTION   U.P.C.   RETAIL  PRICE  DEP  AMOUNT
65704  1    CALYPSO       07958…    2.39   17.40  0.00  17.40
            16OZ 12PK NR PARADISE PUNCH LEMO
...
Beer / Wine & Liq / Soft Drink subtotals …
Total Sales / Discount → Picksheet Total
```

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| ITEM# | `item_code` |
| U.P.C. | `upc` |
| QTY | `qty_cases` (0 allowed) |
| DESCRIPTION line A | `description` |
| DESCRIPTION line B | `pack_size` |
| RETAIL | `ssp_per_pack` |
| PRICE | `cost_per_pack` (wholesale — never swap with RETAIL) |
| AMOUNT | `amount` |
| DEP | not mapped |

### Detection

Header `HEIDELBERG CLEVELAND` / aliases `heidelberg`, Pleasant Valley Rd.  
Live detect on sample → `heidelberg` @ 100.

### Pitfalls

1. **RETAIL vs PRICE** — retail is small SSP; PRICE is wholesale (e.g. Fireball 3.99 vs 134.00).  
2. Wrapped pack lines under ITEM#.  
3. Category + Picksheet Total footers are not products.  
4. Qty 0 product rows still extract.  
5. Short numeric brands (`99`) after QTY belong in **description**, not `qty_cases`.

---

## Esber Beverage Company (reference)

**Samples:**
- PDF `uploads/20260727_233821_9312ed9e4c.pdf` — Killbuck multipage check + beer **559759** + wine **559649** = **$654.95**
- Photo `uploads/20260824_235655_c84021191d.jpeg` — Killbuck dual-sheet inv **564942** wine **$53.32** + beer **$392.37** = check **$445.69** (8 lines)

**Registry key:** `esber`

### Structure

Phone-scan PDFs / table photos often stack:
1. Payment check (ignore for products; foot cross-check only)
2. Wine/spirits ticket (blue headers)
3. Beer/RTD ticket (red headers) — may be a second sheet under the wine page

**Beer (red):**
```
UPC | Prod# | Case | Kegs | Description | Retail Price | Price | Dep | Ext Total
```

**Wine (blue):**
```
UPC | Prod# | Case | Bottle | Size | Description | Retail Price |
Wholesale Price Case | Price Bottle | LLC | Total
```

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| UPC | `upc` |
| Prod# | `item_code` |
| Case (or Bottle if Case empty) | `qty_cases` |
| Size / desc pack token | `pack_size` |
| Retail Price | `ssp_per_pack` (may be &lt; wholesale — not a swap) |
| Price / WS Case / Price Bottle | `cost_per_pack` (beer Price; wine WS Case if case qty else Price Bottle) |
| Ext Total / Total | `amount` (= Case×Price; else cost=Ext/Case) |
| LLC | ignore (not a sheet column) |

### Detection

Aliases: `esber`, `esber beverage company`, Bolivar Rd / esberbeverage.com.  
Live Killbuck dual-sheet 2026-08-24 → `esber` @ 95. Driver “ESPER” alone must not lock esber.

### Pitfalls

1. **Multi-sheet photo** — extract every wine+beer product table; packet foot = sum of sheet SUBTOTALS (53.32+392.37=445.69).  
2. **Retail vs Price** — retail can be unit and/or less than wholesale; amount tracks Case×Price.  
3. Wine bottle picks: qty = Bottle, cost = Price Bottle (not WS Case).  
4. Skip EMPTY keg deposit rates and check pages.  
5. Same-row only — do not mix brand/desc across beer rows.  
6. First-pass incomplete extract: append corrected full packet (do not delete old rows unless operator asks).

---

## 7UP Midvale (reference)

**Samples:**
- PDF `uploads/20260728_030651_8d9c43069c.pdf` — Newcomerstown / EAGLE BP; sales **4012630832** $237.55 + damage **4012630833** (−$28.80) = net **$208.75**
- Photo `uploads/20260824_150436_99fd482f9b.jpeg` — Newcomerstown / EAGLE BP; inv **4012228305** sales-only **TOT SALE $623.21 / 31 cases / ~20 lines** (check $623.31 ignore)

**Registry key:** `seven_up`

### Structure
Tall thermal PDF/photo: **SALES** (required) then optional **DAMAGE**, then payment receipt.
- Vendor: **7Up Midvale**, 5554 Gundy Dr / Splash Transport — customer **EAGLE BP** is ship-to only.
- SALES: WHSLE | CASES | UNITS | UPC/SKU NET | TAX | AMOUNT
- Multi-line **pack groups**: pack header → **many** flavor lines → PKG subtotal (checksum only; skip as product).
- `upc` = left of slash; `item_code` = right SKU; `cost_per_pack` = **NET**; `ssp` = **WHSLE**; `qty_cases` = **CASES**.
- Never put CASES×NET into the NET field (Ghost CASES 4 × 20.66 = AMT 82.64 → cost stays 20.66).
- DAMAGE: negative amount if parentheses; skip PALLET/SHELL lines.
- Shared schema optional per-line `invoice_number` when sales+damage dual inv.

### Detection
- Letterhead `7Up Midvale` / Gundy Dr / (740) 922-5253 / Splash Transport
- Do **not** use bare city `midvale` or customer `EAGLE BP` as vendor aliases
- Live Newcomerstown 2026-08-24: `seven_up` @ 95 detect+alias

### Sample totals (2026-08-24)
Inv **4012228305**: **20** product lines · sum **$623.21** · cases **31** · Ghost pack 1+4+2 @ NET 20.66 = $144.62 · QA foot OK after completeness harden.

---

## ABARTA Coca-Cola (reference)

**Sample:** `uploads/20260728_195542_4926bd4c5b.pdf` (Parma / VET RETAIL OPS).  
**Registry key:** `abarta_coke`

### Structure
- REMIT TO: ABARTA Coca Cola Beverages LLC, Pittsburgh PA
- INV# / barcode = true invoice id (e.g. **5349998035**); OUTLET STORE# is not the invoice #
- SALES cols: `DESCRIPTION | MAT# | QTY | PRICE | CONN | RATE | NET | EXTENDED`
- UPC under description line; category band headers + right-side category totals are not products

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| MAT# (numeric only) | `item_code` (NK… in desc is not MAT#) |
| UPC under desc | `upc` |
| QTY | `qty_cases` |
| NET | `cost_per_pack` (after deal) — PDi cost |
| EXTENDED | `amount` (= QTY × NET) |
| PRICE | **not** mapped to SSP — list wholesale only (foots to TOTAL PRODUCTS) |
| RATE / CONN | ignore |

**SSP policy (decided):** leave `ssp_per_pack` / `ssp_per_unit` **empty** on ABARTA. PRICE is pre-deal list cost, not shelf retail. Pipeline also clears SSP for `abarta_coke` after extract.

**Review flags:** empty `item_code` → `needs_review=TRUE`; `|QTY×NET−EXTENDED| > $0.02` → review.

### Sample totals
36 product lines · sum EXTENDED **$1188.36** = AMOUNT DUE · sum QTY **38** = NET PRODUCT QTY.  
TOTAL PRODUCTS (list) **1898.12** = sum(PRICE×QTY) — not written to sheet. Skip SHELL deposits + DELIVERY RECAP.

---

## Beverage Distributors Inc (reference)

**Sample:** `uploads/20260821_153032_ae66fc2717.pdf` (Parma / Pearl&Bradley Sunoco — check + invoice).  
**Registry key:** `beverage_distributors`

### Structure
Phone-scan PDF often staples:
1. Payment **CHECK** (Pay to BEVERAGE DISTRIBUTORS INC) — ignore for products
2. Invoice page with letterhead **BEVERAGE DISTRIBUTORS INC**, 3800 King Ave, Cleveland, OH 44114, (216) 431-1600

```
ITEM# | QTY | DESCRIPTION | UPC | SSP | PRICE | DISC | DEP | NET | EXT
```

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| ITEM# | `item_code` (leading zeros) |
| UPC | `upc` — BDI often **11 digits** on ticket; pipeline normalizes to **12-digit UPC-A** (check digit). Gold first lines inv 787548: 034100576530 / 576363 / 573065 / 015091 |
| QTY | `qty_cases` |
| DESCRIPTION (+ wrap) | `description` + `pack_size` |
| SSP | `ssp_per_pack` |
| **NET** | `cost_per_pack` (after DISC) — not PRICE |
| EXT | `amount` (= QTY × NET) |
| PRICE / DISC / DEP | not mapped (PRICE=list; DISC=$ off; DEP=deposit) |

### Detection
Aliases: beverage distributors inc; King Ave Cleveland; (216) 431-1600.  
Live detect sample → `beverage_distributors` @ 100.  
Ship-to / Eagle Store / check payor is **not** the vendor.

### Sample totals
Inv **787548** · **28** lines · sum EXT **$1220.64** · Cases **48**.  
Stapled check **$1238.70** (may include extras beyond line EXT — do not force-match).  
First-pass `generic` used PRICE as cost → qty×cost mismatches; forced BDI rules → **0** mismatches / **0** needs_review on anchors.

### Pitfalls
1. Multipage: check page first — skip products there.  
2. **PRICE vs NET** — cost = NET; EXT tracks NET not list PRICE.  
3. Wrapped description lines under same ITEM#.  
4. Customer Pearl Rd / Eagle Store is ship-to only.

---

## R.L. Lipton Distributing Company (reference)

**Sample:** `uploads/20260821_154840_f0a8f586bc.jpeg` (Parma / Eagle Stores picksheet).  
**Registry key:** `rl_lipton`

### Structure
Thermal picksheet, often banner **"Not Final Picksheet"** (still extract products).

Letterhead: **R.L. Lipton Distributing Company**, 9797 Sweet Valley Drive, Valley View, OH 44125, (216) 475-4150.

```
ITEM# | QTY | DESCRIPTION | U.P.C. | S.S. PRICE | PRICE | DISC. | TOTAL
```

Description wraps: pack line (ARIZ 24/22 CAN) + flavor line (KIWI STRAWBERY).

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| ITEM# | `item_code` (leading zeros) |
| U.P.C. | `upc` |
| QTY | `qty_cases` |
| DESCRIPTION (+ wrap) | `description` + `pack_size` |
| S.S. PRICE | `ssp_per_pack` (often unit shelf e.g. 0.99) |
| **PRICE** | `cost_per_pack` (wholesale case) |
| TOTAL | `amount` (= QTY × PRICE when DISC 0) |
| DISC. | not mapped |

### Detection
Aliases: r.l. lipton / lipton distributing; Sweet Valley Dr; (216) 475-4150.  
Live detect sample → `rl_lipton` @ 100.  
Eagle Stores / BP# / driver names are **not** the vendor.

### Sample totals
Inv **443756** (user typed 443755) · **4** lines · sum TOTAL **$119.00** · qty **7**  
Soft Drink category $119.00 = Picksheet Total. Handwritten $496.16 is payment memo — ignore for product foot.

### Pitfalls
1. **S.S. PRICE vs PRICE** — never put 0.99 into cost; cost is case PRICE (17.00).  
2. Wrapped flavor lines under ITEM#.  
3. Skip category $ blocks and Picksheet Total footers.  
4. Prefer printed Invoice# over operator typo / handwritten multi-stop totals.

---

## Southeast Beverage Co. (reference)

**Sample:** `uploads/20260824_233516_46e8959b51.jpeg` (Newcomerstown / EAGLE BP).  
**Registry key:** `southeast_beverage`

### Structure

```
SOUTHEAST BEVERAGE CO.
P.O. BOX 180  ATHENS, OH 45701  (740) 593-3353
Customer: EAGLE BP / NEW CUMBERLAND OH   ← ship-to, not vendor

ITEM# QTY DESCRIPTION  U.P.C.  SSP  PRICE  DISC  UNIT PRICE  DEP  EXT
11600 1 SEVENTH SON… 4/6CN  858439006380  12.99  54.38  0  54.38  0  54.38
80083 4 ALP SPRINGS 24/16OZ NR  …  5.99  4.99  0  4.99  0  19.96
…
Beer / Soft Drink category $ rows   ← skip
Total Content / Invoice Total 240.86
```

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| ITEM# | `item_code` |
| U.P.C. | `upc` |
| QTY | `qty_cases` |
| pack token in DESCRIPTION | `pack_size` (4/6CN, 24/16OZ NR, …) |
| **SSP** (after UPC, before PRICE) | `ssp_per_pack` — **required**; small shelf $ (0.89–12.99) |
| UNIT PRICE (or PRICE−DISC) | `cost_per_pack` |
| EXT | `amount` |
| DEP | unmapped |

After UPC the money band is always **SSP | PRICE | DISC | UNIT PRICE | DEP | EXT**.  
First OCR passes foots money but dropped SSP entirely — operator filled **SSP per Pack** on `Inv - Newcomerstown` inv **162891**; those values are gold anchors in `VendorSpec` (see VALIDATION.md). Empty ssp → line `needs_review` in pipeline.

### Detection

Aliases: southeast beverage, southeast beverage co, Athens P.O. Box 180, (740) 593-3353.  
Do **not** alias EAGLE BP / driver names. Live detect after registry → `southeast_beverage` @ 95–100.

### Sample totals (2026-08-24 Newcomerstown)

Inv **162891** (user typed 162892 — prefer printed): **10** lines · sum EXT **$240.86** = Total Content = Invoice Total = check.  
Beer $54.38 + Soft Drink $186.48. 0 MM. Operator SSP gold: 12.99, 5.99, 0.89, 2.89, 2.89, 2.99, 3.99, 3.99, 0.99, 0.99.

### Pitfalls

1. Ship-to EAGLE BP is not the vendor.  
2. cost = UNIT PRICE (net case), not SSP; SSP is **not** optional on this layout.  
3. Do not stop after reading UNIT/EXT — SSP is the first money field after UPC.  
4. Skip category Beer/Soft Drink $ summaries and Selling Units Total.  
5. Prefer printed Invoice# over operator one-digit typo.  
6. Manual sheet SSP corrections are training signal — keep anchors in rules; do not hardcode SSP by UPC in Python (next ticket prices differ).

---

## Matesich Distributing Co. (reference)

**Samples (Newcomerstown / Duchess 1220):**
- Beer picksheet `uploads/20260826_142154_68d26bcd38.jpeg` — inv **624530**, **25** lines, Picksheet Total **$2033.70**
- RTD picksheet `uploads/20260826_142319_2fabd190b2.jpeg` — inv **624531**, **4** lines, Picksheet Total **$195.65**
- Handwritten **$2229.35** on tall ticket = 2033.70 + 195.65 (two picksheets, one payment)

**Registry key:** `matesich`

### Structure

```
MATESICH DISTRIBUTING CO.     [banner: NOT A FINAL INVOICE — still extract]
Customer: ENGLEFIELD INC / DUCHESS 1220 NEWCOMERSTOWN  ← ship-to

ITEM#  QTY  DESCRIPTION           SSP   PRICE  DISC  NET   AMOUNT
00133  7    B LT CAN TL 12PK12OZ  13.99 23.99  1.60  22.39 156.73
207640 2    SUPERLYTE C3/8 …      17.99 45.00  2.25  42.75  85.50
…
Cases: N   Total Sales / Picksheet Total
```

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| ITEM# | `item_code` (keep leading zeros) |
| true barcode digits if any | `upc` (else empty; sheet uses item_code) |
| QTY | `qty_cases` |
| pack token in DESCRIPTION | `pack_size` |
| SSP | `ssp_per_pack` |
| **NET** (or PRICE−DISC) | `cost_per_pack` — never list PRICE when NET present |
| AMOUNT | `amount` (= QTY × NET) |

### Detection

Aliases: matesich, matesich distributing, OCR variants matisch/matesch.  
Not ENGLEFIELD / DUCHESS / driver. Live detect after registry → `matesich` @ 95–100.

### Pitfalls

1. cost=NET not PRICE (Superlyte DISC 2.25 → NET 42.75).  
2. NOT A FINAL INVOICE still extract.  
3. Handwritten multi-ticket $ is payment memo — foot each picksheet to its Picksheet Total.  
4. Cases/Gallons footer counts are not products.

---

## Current registry keys

See `vendors.list_vendors()` or `/health` → `ocr.vendors`:

- `tramonte` — **Production-ready** (ARCO multipage 10554909+10554910)
- `ohio_beverage`
- `cavalier`
- `house_of_larose`
- `superior_beverage`
- `heidelberg`
- `red_bull`
- `esber`
- `seven_up`
- `abarta_coke`
- `beverage_distributors`
- `rl_lipton`
- `southeast_beverage` — **Production-ready** (Newcomerstown 162891 $240.86)
- `matesich` — **Production-ready** (Newcomerstown 624530+624531 / $2229.35 packet)
- `mansfield` — **Production-ready** (Killbuck 3552801 / $2288.27)
- `coremark`
- `generic` (fallback)

---

## Mansfield Distributing (reference)

**Sample:** `uploads/20260826_183001_c95ef6b993.jpeg` (Killbuck Marathon).  
**Registry key:** `mansfield` (operator may type “Mainsfield”)

### Structure

```
MANSFIELD DISTRIBUTING
1245 LONGVIEW AVENUE  MANSFIELD, OH 44906  (419) 747-4777
Customer: VET RETAIL OPS / MARATHON KILLBUCK  ← ship-to

ITEM# QTY DESCRIPTION  UPC  SSP  PRICE  DISC  NET  EXT
70049 1 SURF LEM VAR …  24.99 40.00 0.00 40.00 40.00
00135 1 BUD 24/16 …     1.39  31.00 4.32 26.68 26.68
…
Total Sales 2411.54 − Total Discount 123.27 = Total Content/Invoice **2288.27** (= check)
```

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| ITEM# | `item_code` |
| UPC | `upc` |
| QTY | `qty_cases` |
| SSP | `ssp_per_pack` |
| **NET** | `cost_per_pack` (never list PRICE when NET present) |
| EXT | `amount` |

### Detection

Aliases: mansfield, mainsfield (typo), Longview Ave, (419) 747-4777.  
Not ship-to Killbuck / driver. Live detect after registry → `mansfield` @ 100.

### Pitfalls

1. cost=NET not PRICE (DISC per case already in NET).  
2. Foot **Total Content** not pre-discount Total Sales (2411.54 ≠ gold).  
3. Tall ticket: drop/merge/hallucinate lines — must hit **38** ITEM# gold set; often drop 00453/03125.  
4. BREAKAGE notes under rows — prefer main QTY/EXT; don’t invent bare breakage rows or phantom ITEM#s.  
5. Wrong-line traps: 01325 EXT must be 79.06; 06212 EXT 124.74; 03125 qty **2**.  
6. Verify foot **$2288.27** before trusting a single extract; 3× reproducibility is the production bar.

---

## Tramonte Distributing (reference)

**Registry key:** `tramonte`
**Sample:** `uploads/20260822_234211_f8d6a5a95a.pdf` (orig `Tramonte Arco akron .pdf`) — ARCO East Ave / VET RETAIL OPS.

### Structure

Banner: **PICKLIST - THIS IS NOT AN INVOICE** (still extract products). Phone-scan PDF often staples **two picklists**:

| Page | Invoice# | Lines | Cases | Content$ / Beer$ / Picksheet Total |
|------|----------|-------|-------|-------------------------------------|
| 1 | **10554909** | 19 | 34 | **$713.58** |
| 2 | **10554910** | 13 (incl qty-0 OOS) | 20 | **$600.86** |

Packet product $ = **$1314.44**. Letterhead:

```
TRAMONTE DISTRIBUTING CO.
1267 S. MAIN ST
AKRON, OH 44301
(330) 535-3103
```

**Same Akron address/phone as Superior Beverage.** Letterhead **NAME** decides vendor (`tramonte` vs `superior_beverage`).

```
ITEM# | QTY | DESCRIPTION | UPC | SSP | PRICE | DEP | AMOUNT
```

No per-line DISC/NET. Footer Discount$/Total Discount is summary only — product AMOUNT = QTY × PRICE.

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| ITEM# | `item_code` |
| UPC | `upc` |
| QTY | `qty_cases` (0 OOS OK) |
| DESCRIPTION (+ pack token) | `description` + `pack_size` |
| SSP | `ssp_per_pack` |
| **PRICE** | `cost_per_pack` |
| AMOUNT | `amount` |
| DEP | unmapped |
| per-picklist Invoice# | `line_item.invoice_number` on multipage |

### Detection

- Letterhead `TRAMONTE DISTRIBUTING CO.` / aliases `tramonte`, `tramonte distributing co`
- Live detect sample → `tramonte` @ 100 when printed name is clear
- **Shared Akron DC with Superior** (1267 S Main / 330-535-3103): letterhead **NAME** wins in prompt rule 8 **and** in code (`letterhead_name_vendor` + `guard_shared_akron_dc_detect`). Empty/ambiguous printed name or address/phone-only cues → do **not** lock `tramonte` or `superior_beverage` (generic / no_letterhead). Do **not** add shared address/phone as Tramonte aliases.
- Ship-to ARCO / VET RETAIL OPS / driver names are not the vendor
- Shared schema + compact retry both carry optional per-line `invoice_number` for multipage split

### Gemini prompt

`build_extract_prompt(get_vendor("tramonte"))` = Tramonte `extract_rules` + shared schema.  
Compact retry keeps full `critical_rules` (PRICE cost, multipage inv #, pack-in-desc, footer skips).

### Pitfalls

1. **Multipage dual invoice** — first live pass often stops after page1; must extract both tables + per-line `invoice_number`.  
2. **Shared warehouse with Superior** — letterhead name wins over address aliases.  
3. **cost=PRICE** (DEP layout) — never subtract footer Discount$ from lines.  
4. Pack-in-desc (`6PK CAN`, `19.2Z CAN`, `6NR`) must split into `pack_size`.  
5. Qty 0 **Out of Stock** rows still extract.  
6. PICKLIST / Not Final banners are not a reason to skip products.  
7. Sheet: append-only; after partial first pass backfill **missing inv only**.

### Sample anchors (forced extract 2026-08-22)

- Detect `tramonte` @ 100; **32** lines; sum **$1314.44**; **0** qty×cost MM; **0** NR  
- 10554909: 19 lines / $713.58 / qty 34 — 30210 cost 33.58 amt 33.58; 43011 4×27.19=108.76; 41341 28.79  
- 10554910: 13 lines / $600.86 / qty 20 — 76211 28.79; 41013 qty0 amt0 OOS; 41051 2×28.75=57.50; 46050 2×26.82=53.64  

Full recipe: skill `references/tramonte.md` (when present) + app VALIDATION.md.

## What not to do

- Hardcode a single-vendor mega-prompt in `ocr.py`
- Change sheet column order per vendor
- Fail the HTTP upload if OCR/Sheets errors
- Use `USER_ENTERED` sheet append (strips leading zeros)
- Commit real invoice photos with customer PII to public git without review


---

## Austintown Dairy (reference)

**Sample:** `uploads/20260827_180649_ff5063b5d8.pdf` (Parma / BP Pearl Rd — CamScanner PDF, often rotated).  
**Registry key:** `austintown_dairy`  
**Operator spelling:** AustinTown / Austin Town — aliases accepted (official **Austintown Dairy**).

### Structure

Continuous-form dairy ticket. Letterhead often cropped on phone scans — rely on **(330) 629-6170** + Youngstown **44513** / Bev Rd when name is missing.

```
Austintown Dairy … Youngstown, OH 44513  (330) 629-6170
Invoice Date / Invoice Number / Route
Sold To / Ship To = customer (BP Pearl / Parma) — NOT vendor

Product U.P.C. | Description | Case Quantity | Units Quantity | Total Units | Price | Amount
14059 007654500187  GAL SWISS PREM SWEET TEA   4   (blank)   16   2.9693   47.51
6241 7480600161     BIG HUG FRUIT 16oz 24pk    1   (blank)    1  11.1900   11.19
999979              DELIVERY CHARGE            —      —       —      —      5.00
…
Total:  cases 17   units 44   $364.98
```

Product U.P.C. column = **ITEM# + barcode** on one line.

### Schema mapping

| Ticket | JSON / sheet |
|--------|----------------|
| Leading ITEM# in Product U.P.C. | `item_code` |
| Trailing barcode | `upc` |
| Case Quantity | `qty_cases` |
| **Total Units** | `units` → sheet **Calculated Qty** |
| Total Units ÷ Case Qty (when whole) | sheet **Extracted Qty** (units per case) |
| Description (+ pack token) | `description` + `pack_size` |
| **cost_per_pack** | **Amount ÷ Case Quantity** when Case Qty > 0 |
| **cost_per_unit** | **Price** only when Total Units > Case Qty (per-gal); else empty |
| Amount | `amount` (= Total Units × Price) |
| SSP | empty (not printed) |
| DELIVERY CHARGE 999979 | keep as line so packet foots Total |

### Detection

Aliases: austintown dairy / austin town; **(330) 629-6170** / 629-6170; 780 bev rd; youngstown oh 44513; OCR garble **uwn, ohio 44513**.  
**Not** aliased (too broad): bare `ohio 44513`, bare `bev rd` / `bev road`.  
Crop path still locks: `UWN, OHIO 44513 (330)629-6170` → `austintown_dairy` via phone or UWN+zip.  
Do **not** alias BP Pearl / Parma customer.

### Sample totals (Parma 2026-08-27)

Inv **897375** · **12** lines (11 product + delivery) · sum **$364.98** · cases **17** · units **44**.  
Gold: 14059 4/16/$47.51; 514153 3/12/$37.26; 1489581 $78.80; delivery $5.00.

### Pitfalls

1. Letterhead name cropped — phone **629-6170** or OCR **UWN+44513** identify vendor; bare zip alone is **not** an alias.
1b. Big Hug UPC is often **10 digits** (74806…) — must survive into sheet UPC (not ITEM# only).  
2. Case Quantity ≠ Total Units (gallons 4 cases / 16 units) — never put 16 into qty_cases.  
3. cost_per_pack = Amount÷Case, not raw Price when Price is per-gallon.  
4. Include DELIVERY CHARGE so sum matches Total $364.98.  
5. No SSP column — leave empty.  
6. Rotated CamScanner PDF — still extract full table.

