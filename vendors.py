"""
Vendor registry for InvUpload OCR.

Add a new distributor by appending a VendorSpec to VENDORS — no other files
need changes unless you want custom post-processing.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional, Sequence, Tuple


@dataclass(frozen=True)
class VendorSpec:
    """One supported invoice vendor / distributor family."""

    key: str
    display_name: str
    # Lowercase substrings matched against OCR/header vendor text
    aliases: Tuple[str, ...]
    # Full Gemini instructions for this vendor's invoice layout (full extract prompt)
    extract_rules: str
    # Short labels Gemini may return during detection
    detect_labels: Tuple[str, ...] = ()
    # Must-keep rules for compact retry — NEVER truncated (column maps, skip rows, wrap quirks)
    critical_rules: str = ""
    notes: str = ""

    def matches(self, text: str) -> bool:
        t = (text or "").lower()
        if not t:
            return False
        return any(a in t for a in self.aliases)


# ── Registry (order = match priority) ───────────────────────────────────────
# Ohio c-store beer/wine + major wholesalers we see most often.

VENDORS: Tuple[VendorSpec, ...] = (
    VendorSpec(
        key="tramonte",
        display_name="Tramonte Distributing",
        aliases=(
            "tramonte",
            "tramonte distributing",
            "tramonte distributing co",
            "tramonte distributing co.",
            "t.d.i",
            "t.d.i.",
            "tdi beverage",
            "tramonte bev",
            "tramonte distributing inc",
        ),
        detect_labels=(
            "tramonte",
            "tramonte distributing",
            "tramonte distributing co",
            "tdi beverage",
        ),
        extract_rules="""
VENDOR = Tramonte Distributing Co. (NE Ohio beer/RTD wholesaler).
Letterhead: "TRAMONTE DISTRIBUTING CO." / "Tramonte Distributing".
Akron house ticket (validated ARCO East Ave sample):
  1267 S. MAIN ST, AKRON, OH 44301, (330) 535-3103
IMPORTANT — same warehouse address/phone is ALSO used by SUPERIOR BEVERAGE tickets.
Letterhead NAME decides the vendor: TRAMONTE… → tramonte; SUPERIOR… → superior_beverage.
Never use address/phone alone to override a clear letterhead name.
Customer / ship-to (VET RETAIL OPS LLC, ARCO, 2215 EAST AVE AKRON OH 44314) is NOT the vendor.
Driver / salesrep names (e.g. TEAIR A LARAMOR, KENDALL BRYTE) are NOT the vendor.
Banner "PICKLIST - THIS IS NOT AN INVOICE" and "Not Final" still have product rows — extract them.

COLUMN LAYOUT (left → right) — Akron thermal DEP house style (same shape as Superior layout B):
  ITEM# | QTY | DESCRIPTION | UPC | SSP | PRICE | DEP | AMOUNT
There is NO per-line DISC/NET column on this layout. Footer Discount$/Total Discount is a summary
only — do NOT invent per-line discounts and do NOT subtract footer Discount from line amounts.

FIELD MAPPING (critical):
- item_code = ITEM# (keep leading zeros; full digits as printed, e.g. 30210, 43011, 41341).
  Each physical product row has its OWN ITEM# — never copy the previous row's code.
- upc = UPC barcode digits (11–12). Always capture both upc and item_code when both print.
- qty_cases = QTY (cases). Leading slash "/2" → integer 2, not a fraction. Qty 0 is valid
  (Out of Stock still listed; amount often 0.00) — still extract the row.
- ssp_per_pack = **SSP** (suggested sell / retail). NEVER put SSP into cost.
- cost_per_pack = **PRICE** (wholesale case/pack). NEVER DEP; NEVER SSP.
- amount = **AMOUNT** (line extension). Must equal QTY × PRICE when DEP is 0.00
  (e.g. QTY 4 × PRICE 27.19 = AMOUNT 108.76; QTY 2 × 33.58 = 67.16).
- DEP = deposit — almost always 0.00; unmapped (never into cost/ssp/amount).
- cost_per_unit / ssp_per_unit: leave empty unless explicitly per-unit.

DESCRIPTION + PACK SIZE (pack-in-desc, single thermal line — common):
Pack tokens sit IN the description, e.g.:
  "MIKE BLK CHERRY 6PK CAN", "MOD ESP 12PK CAN", "MOD ESP 24PK CANS", "MOD ESP 18PK CANS",
  "MXD MARG 16OZ CAN", "WC SURGE BL ORNG 19.2Z CAN", "STRES STRWBERRY 24Z CAN",
  "ICEHOUSE EDGE 24OZ CAN", "HEINEKEN 6PK NR", "MOD ESP 6NR", "COR FAMILIAR 12NR",
  "CORONA EXTRA 18PK CAN", "WC SURGE #1VARIETY 12PK CAN".
- description = brand/flavor words (strip trailing pack token when clear)
- pack_size = trailing pack token (6PK CAN, 12PK CAN, 18PK CANS, 24PK CANS, 16OZ CAN,
  19.2OZ CAN / 19.2Z CAN, 24OZ CAN / 24Z CAN, 6PK NR, 6NR, 12NR, 12PK NR, 24Z NR, …)
- If unsure where brand ends, put full text in description and still fill pack_size from the token.
Wrapped line-B packs under the same ITEM# (if present) bind pack_size to that ITEM# only.

MULTI-PAGE / MULTI-INVOICE PDFs (critical — phone scans staple 2+ picklists):
- Scan EVERY page. Page 1 and page 2 often have DIFFERENT Invoice# values under the same
  Tramonte letterhead (validated sample: inv **10554909** Picksheet Total **$713.58** /
  Cases **34**, then inv **10554910** Picksheet Total **$600.86** / Cases **20**).
- Extract EVERY product row from EVERY picklist table on every page.
- Set top-level invoice_number to the first/top Invoice# when multiple.
- On EACH line_item, set "invoice_number" to that row's own picklist Invoice# so sheet rows split.
- notes should list all invoice numbers found (e.g. "invoices: 10554909, 10554910").
- Prefer more product rows over stopping after the first table / first page.
- "Out of Stock" / "-2 Out of Stock" annotation under a product is still the same product row
  (use printed QTY, often 0).

COMPLETENESS + FOOT:
- Extract every ITEM# product row top→bottom until Credits/Cases footer on that picklist.
- Foot product sum(amount) ≈ **Content$** / **Beer$** / **Picksheet Total** for that ticket
  (sample p1: Content$/Beer$/Picksheet Total **713.58**; p2: **600.86**).
- Do NOT force-match footer Discount$ into line costs (p1 Discount$ 72.96 / p2 24.00 are
  summary lines — product AMOUNT already equals QTY×PRICE).
- Packet money across multi-invoice PDF ≈ sum of each picksheet Content$ (713.58+600.86=1314.44).

OTHER RULES:
- Invoice# near header is numeric (e.g. 10554909, 10554910). Account# / Load / PO# / License
  / Driver / Salesrep are not invoice numbers.
- ship_to_* = left customer block (VET RETAIL OPS / ARCO / East Ave), not 1267 S Main letterhead.
- SKIP (not product lines): Credits: header, Cases/Kegs/Misc/Credits counts, Gallons/Ltrs,
  Beer$/NA$/Misc$/Content$/Deposit$/Discount$ footer money blocks,
  Total Deposit / Total Discount / Total Credits / Picksheet Total,
  PAYMENT blocks, "Not Final" banner, "PICKLIST - THIS IS NOT AN INVOICE" banner text alone.
- One JSON object per product table row only.
""".strip(),
        critical_rules=(
            "VENDOR=Tramonte Distributing Co. (letterhead TRAMONTE…) — NOT Superior; "
            "same 1267 S Main Akron/(330)535-3103 warehouse as Superior — letterhead NAME wins. "
            "Ship-to/driver/salesrep NOT vendor. Extract even if banner says PICKLIST NOT AN INVOICE / Not Final. "
            "COLS: ITEM#|QTY|DESC|UPC|SSP|PRICE|DEP|AMOUNT (no per-line DISC/NET). "
            "item_code=ITEM# per row full digits; upc=UPC barcode; qty_cases=QTY (slash /2→2; qty 0 OOS OK); "
            "ssp_per_pack=SSP (never cost); cost_per_pack=PRICE (never DEP/SSP); amount=AMOUNT (=QTY×PRICE). "
            "PACK: trailing token in single-line desc (6PK CAN, 12PK CAN, 19.2Z CAN, 6NR, 12NR…). "
            "MULTI-PAGE MULTI-INVOICE: extract EVERY picklist on EVERY page; set line_item.invoice_number "
            "per header Invoice# (e.g. 10554909 then 10554910); notes list all inv #s. "
            "Foot sum(amount)≈Content$/Beer$/Picksheet Total per ticket — do NOT subtract footer Discount$. "
            "SKIP: Cases/Kegs/Gallons counts, Beer$/Content$/Discount$ blocks, Total Deposit/Discount/Credits, "
            "Picksheet Total, PAYMENT, banner-only lines."
        ),
        notes=(
            "ARCO East Ave multipage PDF 20260822_234211_f8d6a5a95a "
            "(orig Tramonte Arco akron .pdf): inv 10554909 19 lines Content$713.58/Cases34 + "
            "inv 10554910 13 lines Content$600.86/Cases20 (incl qty0 OOS); cost=PRICE; pack-in-desc; "
            "shared Akron DC address with Superior — letterhead decides."
        ),
    ),
    VendorSpec(
        key="ohio_beverage",
        display_name="Ohio Beverage Distributing",
        aliases=(
            "ohio beverage",
            "ohio beverage distributing",
            "ohio bev",
            "ohio beverage dist",
            "obd",  # short; match_vendor_text / hints only — not in detect_labels
            # Brecksville DC
            "brecksville, oh 44141",
            "brecksville oh 44141",
            "brecksville, oh",
            "6745 southpointe",
            "southpointe parkway",
            "(440) 746-7500",
            "440-746-7500",
            "440 746 7500",
        ),
        detect_labels=(
            "ohio beverage",
            "ohio beverage distributing",
            "ohio bev",
        ),
        extract_rules="""
VENDOR = Ohio Beverage Distributing (beer/wine/seltzer wholesale picksheet).
Letterhead: "OHIO BEVERAGE DISTRIBUTING", 6745 Southpointe Parkway, Brecksville, OH 44141,
Phone 440-746-7500.
Driver / salesman / salesrep names (e.g. D. ESPER, J. COTTRELL, SNOWBERGER) are NOT the vendor —
never map Esper→Esber.
Customer / ship-to (ARCO, VET RETAIL OPS, Eagle, etc.) is NOT the vendor.

COLUMN LAYOUT (left → right) on thermal picksheets (ARCO inv 244557 and similar):
  ITEM# | QTY | PACK | DESCRIPTION | SSP | PRICE | DISC | DEP | EXT

FIELD MAPPING (critical):
- item_code = ITEM# (keep leading zeros; e.g. 00504, 02542, 93835). REQUIRED.
- upc: usually absent — leave empty; never put ITEM# into upc.
- qty_cases = QTY (cases ordered/shipped). NEVER use the PACK column as qty.
- pack_size = PACK column (units in case: 2, 3, 12, 15, 24, 35, etc.) plus pack tokens from DESC (12NR, 12CAN, SUIT, 12/16OZ CAN, 5/3/25OZ CAN, 6/4CAN).
- description = brand + pack form (BUD 12NR, BL SUIT, CW MGO MARG 6/4CAN). Abbreviations OK as printed.
- ssp_per_pack = **SSP** column (suggested sell / retail shelf; e.g. 13.99, 2.49, 12.99).
  NEVER put SSP into cost.
- PRICE = list wholesale case cost BEFORE discount.
- DISC = **per-case** dollars off PRICE (e.g. 1.60, 0.80, 4.32), not a line-total lump unless QTY=1.
- DEP = deposit — usually 0.00; do NOT add into cost or amount unless product is deposit-only (rare).
- cost_per_pack = **net case cost after discount**:
    * If DISC > 0: cost_per_pack = PRICE − DISC
      Examples: PRICE 23.99 DISC 1.60 → cost **22.39**; PRICE 19.99 DISC 0.80 → **19.19**;
      PRICE 31.00 DISC 4.32 → **26.68**; PRICE 25.13 DISC 3.60 → **21.53**.
    * If DISC is 0.00 / blank: cost_per_pack = PRICE.
  NEVER use list PRICE as cost when DISC is non-zero.
  NEVER use SSP as cost.
- amount = **EXT** (line extension). Must equal QTY × cost_per_pack (= QTY × (PRICE − DISC)).
  Example: QTY 3, PRICE 19.99, DISC 0.80 → cost 19.19, EXT 57.57.

OTHER RULES:
- Invoice# near header (e.g. 244557). Account# / Load / PO# / License are not invoice numbers.
- Extract EVERY product ITEM# row until Cases/Bottles/Gallons footer block.
- SKIP (not product lines): Cases/Bottles/Gallons/Kegs/Misc/Credits summary counts,
  Total Sales, Total Discount, Total Content, Total Deposit, Total Credits, Over/Short,
  Invoice Total, PAYMENT block, signatures, service-fee banner, Final banner.
- Foot product sum to **Total Content** / Invoice Total when equal (payment check amount).
- ship_to_* = left customer block (ARCO address), not Southpointe letterhead.
- One JSON object per product ITEM# row only.
""".strip(),
        critical_rules=(
            "VENDOR=Ohio Beverage Distributing (6745 Southpointe / Brecksville) — ship-to/driver NOT vendor. "
            "COLS: ITEM#|QTY|PACK|DESC|SSP|PRICE|DISC|DEP|EXT. "
            "item_code=ITEM# (leading zeros); upc usually empty; qty_cases=QTY not PACK; "
            "ssp_per_pack=SSP (never cost); "
            "cost_per_pack=PRICE−DISC when DISC>0 else PRICE — NEVER SSP; NEVER list PRICE if DISC nonzero; "
            "amount=EXT (=QTY×(PRICE−DISC)). "
            "SKIP: Cases/Bottles totals, Total Sales/Discount/Content, payment block, signatures."
        ),
        notes=(
            "Killbuck validated historically; ARCO East Ave PDF 20260822_190933_7df92c1520 inv 244557 "
            "(35 lines, Total Content $1738.62; cost=PRICE−DISC; Soft/beer+CW)."
        ),
    ),
    VendorSpec(
        key="cavalier",
        display_name="Cavalier Distributing",
        aliases=(
            "cavalier",
            "cavalier distributing",
            "cavalier dist",
        ),
        detect_labels=("cavalier", "cavalier distributing"),
        extract_rules="""
VENDOR = Cavalier Distributing (Ohio craft/import beer distributor).
- Header may say Cavalier Distributing / Cavalier.
- Capture item/product codes and any UPC/EAN if printed.
- Descriptions often include brewery + brand + pack (cans/bottles).
- Watch for mixed-case and keg lines; still one sheet row per line.
- Prefer case quantity + unit cost + extension when both pack and unit prices appear.
""".strip(),
        critical_rules=(
            "item_code=product code; upc if EAN/UPC printed. "
            "One row per line incl. keg/mixed-case. "
            "Prefer case qty + cost + extension; pack in description."
        ),
        notes="Common Ohio craft beer house.",
    ),
    VendorSpec(
        key="house_of_larose",
        display_name="House of LaRose",
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
        detect_labels=("house of larose", "larose", "house of la rose"),
        extract_rules="""
VENDOR = House of LaRose (Anheuser-Busch wholesaler, Ohio).
- AB / Budweiser family invoices are common.
- Capture route/invoice #, item numbers, case qty, pricing, and deposits if present.
- ITEM#/product code → item_code; UPC only if a real barcode number is printed.
- Pack configs often 6/4, 2/12, 24/12oz LOOSE, etc.
- Separate product lines from empty returns / deposit credit lines when possible.
""".strip(),
        critical_rules=(
            "item_code=ITEM#/product code; upc only if real barcode. "
            "Pack in desc (6/4, 2/12, LOOSE). "
            "Skip empty-return/deposit-credit-only rows when separable."
        ),
        notes="Major AB wholesaler in Ohio.",
    ),
    VendorSpec(
        key="superior_beverage",
        display_name="Superior Beverage",
        aliases=(
            "superior beverage",
            "superior beverage group",
            "superior bev",
            "superior bev group",
            # Glenwillow DC (logo often crops)
            "31031 diamond",
            "diamond parkway",
            "glenwillow, oh",
            # Akron DC / house ticket (ARCO East Ave sample)
            "1267 s. main",
            "1267 s main",
            "s. main st akron",
            "main st akron",
            "(330) 535-3103",
            "330-535-3103",
        ),
        detect_labels=(
            "superior beverage",
            "superior beverage group",
            "superior bev",
        ),
        extract_rules="""
VENDOR = Superior Beverage / Superior Beverage Group (NE Ohio beer/wine/spirits wholesaler).
Letterhead: "SUPERIOR BEVERAGE" / "Superior Beverage Group".
Warehouse addresses vary by DC — both are the same vendor when letterhead says Superior:
  - Glenwillow: 31031 Diamond Parkway, Glenwillow, OH 44139
  - Akron: 1267 S. Main St, Akron, OH 44301, (330) 535-3103
NOTE: Tramonte Distributing Co. also prints the SAME Akron address/phone on its picklists.
If letterhead says TRAMONTE DISTRIBUTING CO. this is NOT Superior — wrong vendor_key.
Customer / ship-to (e.g. VET RETAIL OPS LLC, ARCO, 2275 East Ave) is NOT the vendor.

TWO COLUMN LAYOUTS (detect which is printed; map accordingly):

A) Glenwillow-style (has DISC + NET):
  ITEM# | QTY | DESCRIPTION | U.P.C. | SSP | PRICE | DISC | NET | AMOUNT
  - cost_per_pack = **NET** (charged wholesale after discount). When DISC is 0.00, NET equals PRICE.
  - If NET is unreadable: cost_per_pack = PRICE − DISC when DISC > 0, else PRICE.
  - amount = AMOUNT (must equal QTY × NET / cost). Never use list PRICE as cost when DISC/NET present.
  - PRICE = list before discount — do not put PRICE into cost_per_pack when NET is printed.
  - DISC unmapped (already baked into NET).

B) Akron/thermal house-style (has DEP, no DISC/NET) — sample inv 10549752:
  ITEM# | QTY | DESCRIPTION | UPC | SSP | PRICE | DEP | AMOUNT
  - cost_per_pack = PRICE (case wholesale).
  - amount = AMOUNT (typically PRICE × QTY when DEP is 0.00).
  - DEP is deposit — NOT a product field; never put DEP into cost/ssp/amount.
  - Do NOT invent DISC/NET columns when the ticket only has DEP.

FIELD MAPPING (shared):
- item_code = ITEM# only (4–6 digits, keep leading zeros). Each printed product row has its OWN ITEM# —
  never copy the previous row's ITEM# onto the next product (common fail on dense thermals).
- upc = UPC / U.P.C. barcode digits (11–12). Always capture both upc and item_code.
- qty_cases = QTY (cases; 0 allowed for out-of-stock still listed). Thermal tickets often print qty as
  "/2", "/3", "/6" (leading slash) — that means integer 2/3/6 cases, NOT a fraction.
- ssp_per_pack = SSP (suggested sell / retail). NEVER put SSP into cost.
- amount = AMOUNT.
- cost_per_unit / ssp_per_unit: leave empty unless explicitly per-unit labeled.

DESCRIPTION + PACK SIZE:
1) Wrapped (common Glenwillow): line A brand → description; line B under SAME ITEM# → pack_size
   (STUBBY 2/12 NR, 24 PK CAN FLAT #104, 1.5 L CHARDONNAY #80, …). Never steal the next row's pack line.
2) Single-line (common Akron thermal): pack tokens sit IN the description
   (e.g. "MIKE BLK CHERRY 6PK CAN", "MOD ESP 12PK CAN", "CORONA EXTRA 18PK CANS",
   "WC SURGE BLUEBERRY 19.2OZ CAN", "CORONITA EXTRA 7OZ 6PK").
   - description = brand/flavor words (strip trailing pack token when clear)
   - pack_size = trailing pack token (6PK CAN, 12PK CAN, 18PK CANS, 24OZ CAN, 19.2OZ CAN,
     7OZ 6PK, 6PK NR, 12PK NR, 24Z NR, 16OZ 12CAN, …)
   - If unsure where brand ends, put full text in description and still fill pack_size from the pack token.

COMPLETENESS (critical on long thermals):
- Extract EVERY product row from ITEM# table top→bottom until category/PAYMENT footers.
- Do not stop early; do not drop middle rows when QTY is high or description is long.
- Qty-0 / "Out of Stock" product rows with ITEM# + UPC still extract (amount may be 0.00).
- After extraction, sum(AMOUNT) should foot near **Total Content** (product $), not Invoice Total
  when Invoice Total adds BEER/WINE SIN TAX / SPLIT CASE CHARGE fees — skip those fee rows.
  Example Parma inv 3749614: Total Content ~1226.22; Invoice Total 1262.57 includes tax/case charges.

OTHER RULES:
- Header Invoice# is numeric (e.g. 3629771, 3749614, 10549752); ignore payment "Check N" noise.
- Ship-to may be EAGLE ENERGY GROUP / EAGLE STORES / PARMA Pearl Rd — still not the vendor.
- SKIP (not product lines): Beer/NAS/Misc/RTD$/Ltrs blocks, Cases/Kegs/Gallons counts,
  Beer/Wine/Soft Drink/Units/Credits subtotals, Total Deposit/Credits/Invoice Total,
  BEER SIN TAX CHARGE / WINE SIN TAX CHARGE / SPLIT CASE CHARGE,
  PAYMENT / PAYMENT TOTALS / TOTAL CASH/CHECK blocks, fee-only rows.
- Handwritten qty/price overrides beat printed values when clearly marked.
- One JSON object per product table row (unique physical line), not per unique ITEM# value only.
""".strip(),
        critical_rules=(
            "SUPERIOR dual layout: (A) ITEM#|QTY|DESC|UPC|SSP|PRICE|DISC|NET|AMOUNT "
            "or (B) ITEM#|QTY|DESC|UPC|SSP|PRICE|DEP|AMOUNT. "
            "item_code=ITEM# per row full digits as printed (e.g. 107378/262260 — never drop leading digits); upc=UPC; "
            "qty_cases=QTY digits — leading slash /3 means 3 cases NOT a fraction; ssp_per_pack=SSP; "
            "LAYOUT A: cost_per_pack=NET (or PRICE−DISC) — NEVER list PRICE when DISC/NET present; "
            "LAYOUT B: cost_per_pack=PRICE; never DEP. amount=AMOUNT (=QTY×cost). "
            "PACK: wrapped lineB under ITEM# OR trailing token in single-line desc. "
            "ALL product rows until PAYMENT/category footers; foot sum(AMOUNT)≈Total Content not fee-inflated Invoice Total. "
            "SKIP: Beer$/PAYMENT/Invoice Total/Cases counts/SIN TAX/SPLIT CASE fee rows."
        ),
        notes=(
            "Glenwillow 20260727_002200_098f3e9d6e (DISC/NET wrap); "
            "Akron ARCO East Ave 20260729_223234_5d8a7de7dd inv 10549752 (DEP, pack-in-desc, total 1844.69); "
            "Parma 20260822_003027_0c3c62bec6 inv 3749614 Total Content ~1226; "
            "Killbuck multipage PDF 20260822_031623_ec39b6eebc inv 3754642 "
            "(check page + 2 inv pages, 74 lines, Total Content/Invoice/check $3485.12, cost=NET)."
        ),
    ),
    VendorSpec(
        key="heidelberg",
        display_name="Heidelberg Distributing",
        aliases=(
            "heidelberg",
            "heidelberg cleveland",
            "heidelberg distributing",
            "heidelberg dist",
            "pleasant valley rd",  # Independence OH letterhead anchor
            "independence, oh 44131",
            "9101 e. pleasant valley",
            "(216) 520-2626",
            "216-520-2626",
            "216 520 2626",
        ),
        detect_labels=(
            "heidelberg",
            "heidelberg cleveland",
            "heidelberg distributing",
        ),
        extract_rules="""
VENDOR = Heidelberg / Heidelberg Cleveland (Ohio beer, wine, spirits, soft-drink wholesaler).
Letterhead typically: "HEIDELBERG CLEVELAND", 9101 E. Pleasant Valley Rd, Independence, OH 44131,
phone (216) 520-2626. Check stubs may say "Heidelberg".
Customer / ship-to (PAR MAR OIL CO, PAR MAR 85, Eagle, ARCO, VET RETAIL OPS, Pearl Rd Parma, etc.) is NOT the vendor.
Driver / salesrep names are NOT the vendor.

COLUMN LAYOUT (left → right) on delivery / picksheet tickets:
  ITEM# | QTY | DESCRIPTION | U.P.C. | RETAIL | PRICE | DEP | AMOUNT

FIELD MAPPING (critical — do not swap columns):
- item_code = ITEM# (numeric, keep leading zeros if any). Read the printed ITEM# carefully — do not invent/swap codes.
- upc = U.P.C. column — real barcode digits (11–12). Always capture both upc and item_code.
- qty_cases = QTY (cases). Thermal tickets often print qty as "/2", "/3", "/4" (leading slash) — that means integer 2/3/4, NOT a fraction.
  Copy the integer case count only (0 OK for not-delivered lines).
- ssp_per_pack = RETAIL column (suggested / shelf retail) — NOT the PRICE column.
- cost_per_pack = PRICE column (wholesale case/pack cost). Do NOT put RETAIL here.
- amount = AMOUNT (line extension). Strip currency symbols. Prefer AMOUNT = QTY × PRICE when DEP is 0.
- DEP (deposit) is usually 0.00 — ignore for sheet fields unless needed to resolve blurry amount.
- cost_per_unit / ssp_per_unit: leave empty unless explicitly per-unit.

DESCRIPTION + PACK SIZE (wrapped rows — common):
- DESCRIPTION often spans TWO lines under one ITEM#:
    line A: brand/name (e.g. ESSENTIA, CALYPSO, NESQUIK, CUPCAKE, WOODBRIDGE, 19 CRIMES, VODKADE, or short numeric brands like "99")
    line B: size/pack/flavor (e.g. 750ML CAB 12, 16OZ 12PK NR, 1L 12PK PET, 19.2OZ 15PK CN BERRY MONKEY)
- description = brand + key flavor words; pack_size = size/pack token from line B.
- Short numeric brands (e.g. "99") sit in DESCRIPTION after QTY — do NOT merge them into qty_cases.
- NEVER attach the next product's pack line to the previous ITEM#.

MULTI-PICKSHEET PHOTOS (common — two HEIDELBERG headers on one image):
- Each header block has its own Invoice# (e.g. 700010271 then 700010272).
- Extract EVERY product row from EVERY picksheet table on the image.
- Set top-level invoice_number to the first/top Invoice# when multiple.
- On EACH line_item, set "invoice_number" to that row's picksheet Invoice# so sheet rows split correctly.
- Category footers / Picksheet Total / bottle tax lines belong only to their own picksheet — skip as products.
- Product money often foots to Total Content per picksheet; Picksheet Total may add SPLIT CS CHG / bottle tax / county tax — those fee lines are NOT products.

OTHER RULES:
- Wine, spirits, RTD, soft drink, and beer can appear on the same ticket — extract all product ITEM# rows.
- Qty 0 lines with a product description are still product rows (amount often 0.00).
- Skip category subtotals (Beer / Wine & Liq / Soft Drink / Misc / Credits / Gallons) and
  Total Sales / Discount / Content / Deposit / SPLIT CS CHG / BTL WINE BTL / AAA-ADMIN / county tax /
  Picksheet Total / signature blocks — NOT product lines.
- ship_to_* = account/ship-to block (left header), not Heidelberg Independence letterhead.
- One JSON object per product ITEM# row only.
""".strip(),
        critical_rules=(
            "VENDOR=Heidelberg Cleveland (9101 E Pleasant Valley / Independence) — ship-to NOT vendor. "
            "COLS: ITEM#|QTY|DESC|U.P.C.|RETAIL|PRICE|DEP|AMOUNT. "
            "item_code=ITEM# only (as printed); upc=U.P.C.; qty_cases=QTY digits — leading slash /2=/3 means 2/3 cases NOT fraction; "
            "short brands like '99' belong in description NOT qty; "
            "ssp_per_pack=RETAIL; cost_per_pack=PRICE (wholesale) — NEVER swap RETAIL/PRICE; amount=AMOUNT; ignore DEP. "
            "MULTI-TICKET photo: extract ALL picksheets; set line_item.invoice_number per header Invoice#. "
            "WRAP: lineA=brand→description; lineB under SAME ITEM#=pack_size; never steal next pack line. "
            "SKIP: Beer/Wine&Liq/Soft Drink/Misc/Credits/Gallons, Total Sales/Discount/Picksheet Total, "
            "bottle tax/SPLIT CS CHG fee lines, signatures."
        ),
        notes=(
            "Killbuck 20260727_013531_844932cf53; Parma dual-ticket photo 20260821_182354_90740be04e "
            "inv 700010271 ($88.86 soft) + 700010272 (wine/spirits Total Content $568.14) product sum $657.00 / 21 lines."
        ),
    ),
    VendorSpec(
        key="red_bull",
        display_name="Red Bull Distribution Company",
        aliases=(
            "red bull",
            "redbull",
            "red bull distribution",
            "red bull distribution company",
            "rbdc",
            "redbulldistributioncompany",
            "rbdc.ar@redbull.com",
        ),
        detect_labels=(
            "red bull",
            "red bull distribution",
            "red bull distribution company",
            "rbdc",
        ),
        extract_rules="""
VENDOR = Red Bull Distribution Company Inc. (RBDC) — energy drink DSD invoice / load sheet.
Letterhead: "Red Bull Distribution Company Inc.", often Dallas TX PO Box, rbdc.ar@redbull.com,
www.redbulldistributioncompany.com. Route / Load Sheet / Salesman near top.
Invoice# is long numeric (e.g. 2036701920). Cust# and Terms (often COD) in header.

COLUMN LAYOUT (left → right) on thermal delivery tickets:
  ID | QTY | UNITS | DESCRIPTION | PRICE | DEP | DISC | SUGAR | TOTAL

FIELD MAPPING (critical):
- item_code = ID column (alphanumeric SKU, e.g. RB247584, RB2746). Keep letters + digits exactly.
- upc = barcode digits printed under the description (usually 12 digits starting 61126…). 
  Do NOT put the RB… ID into upc. If barcode digits missing, leave upc "".
- description = flavor/size text (PINK 12OZ LS, SF SEABLUE 12OZ LS, SUGARFREE 8.4OZ LS, etc.).
- pack_size = size/pack token from description (12OZ LS, 12OZ 4PK, 8.4OZ LS, 8.4OZ 4PK).
  LS = loose singles; 4PK = 4-pack. Put pack form in pack_size, full text can stay in description.
- qty_cases = QTY column (often fractional cases, e.g. 0.87, 1.33) — copy as printed.
  UNITS in parentheses (21) is unit count — do NOT use UNITS as qty_cases; optional note only.
- cost_per_pack = PRICE column (per-unit/pack wholesale, e.g. 2.28, 1.75). Strip $.
- amount = TOTAL column line extension. Strip $ and parentheses; use absolute value if shown as (41.73).
- ssp_per_pack / ssp_per_unit / cost_per_unit: usually blank (not printed).
- Ignore DEP, DISC, SUGAR columns for sheet fields (unless needed to recover a blurry TOTAL).

OTHER RULES:
- Tickets may be delivery OR pickup/return ("Units Picked Up", "Cases Delivered 0"). Still extract every product ID row.
- Parentheses around TOTAL often mean credit/return formatting — still one product line per ID; amount as positive digits unless a clear minus sign is printed.
- Skip footer blocks: Units Picked Up, Number of SKU's, Cases/Units Delivered, Subtotal, Sales Tax, SSB/Sugar Tax, Can Deposit, Fees, Invoice Total, signatures.
- ship_to_* = customer block (often top-right store name/address), not Red Bull Dallas letterhead.
- One JSON object per product ID row only.
""".strip(),
        critical_rules=(
            "COLS: ID|QTY|UNITS|DESC|PRICE|DEP|DISC|SUGAR|TOTAL. "
            "item_code=ID (RB… exactly); upc=61126… under desc — NEVER put RB id in upc; "
            "qty_cases=QTY (fractional OK) NOT UNITS; pack_size from desc (12OZ LS, 8.4OZ 4PK); "
            "cost_per_pack=PRICE; amount=TOTAL (strip $ and parens, abs value). "
            "SKIP: Units Picked Up, SKU counts, Cases/Units Delivered, Subtotal/Tax/Deposit/Fees/Invoice Total."
        ),
        notes="Validated on Killbuck load sheet 20260727_011518_ec9b2466a3 (RB ID + UPC under desc).",
    ),
    VendorSpec(
        key="esber",
        display_name="Esber Beverage Company",
        aliases=(
            "esber",
            "esber beverage",
            "esber beverage company",
            "esberbeverage",
            "bolivar road",  # Canton letterhead anchor
            "2217 bolivar",
            "esberbeverage.com",
            "sales@esberbeverage.com",
        ),
        detect_labels=(
            "esber",
            "esber beverage",
            "esber beverage company",
        ),
        extract_rules="""
VENDOR = Esber Beverage Company (Canton OH beer / wine / spirits DSD).
Letterhead: "Esber Beverage Company", 2217 Bolivar Road S.W., Canton, OH 44706,
phone 330-456-4361, www.esberbeverage.com. Customer copy invoices are common.
Do not choose esber from driver/salesman names that merely look like "Esper" / "D. ESPER".
Vendor requires Esber Beverage Company letterhead, Bolivar Road, or esberbeverage.com.

MULTIPAGE / MULTI-INVOICE PDF (critical — phone scans often staple several docs):
1. Scan EVERY page of the document.
2. IGNORE payment CHECKs, paystubs, deposit slips, photo-of-check pages (no product table).
3. Extract EVERY product table on EVERY invoice page. Multiple invoice numbers in one PDF
   are normal (e.g. beer inv 559759 + wine inv 559649 paid by one check).
4. Prefer MORE product rows over stopping after the first table. Never stop after beer
   if a wine/spirits table appears later on the same page or another page.
5. In notes, list all invoice numbers found (e.g. "invoices: 559759, 559649").
6. Handwritten circled check totals (e.g. $654.95) are payment memos — not a reason to
   skip a second invoice; often check = sum of all invoice subtotals.

=== LAYOUT A — BEER / RTD (often RED column headers) ===
  UPC | Prod# | Case | Kegs | Description | Retail Price | Price | Dep | Ext Total

FIELD MAP A:
- upc = UPC (keep leading zeros).
- item_code = Prod#.
- qty_cases = Case (case count). Kegs only if Case empty and Kegs has qty.
- description = Description; pack_size from desc (30PK, C24, 9PK, 15PK, C12, 24OZ).
- ssp_per_pack = Retail Price. May be pack OR unit retail (e.g. 1.29 on 24oz).
  Retail may be LESS than Price (e.g. 14.99 retail / 23.99 wholesale on 9PK) — still map
  Retail→ssp and Price→cost; do NOT swap; trust Ext Total = Case × Price.
- cost_per_pack = Price (wholesale). NEVER put Retail Price here.
- amount = Ext Total (= Case × Price when both clear).
- Ignore Dep. Skip EMPTY 1/2 KEG / EMPTY 1/4 KEG footer rate lines.

=== LAYOUT B — WINE / SPIRITS (often BLUE column headers) ===
  UPC | Prod# | Case | Bottle | Size | Description | Retail Price |
  Wholesale Price Case | Price Bottle | LLC | Total

FIELD MAP B:
- upc = UPC; item_code = Prod#.
- qty_cases = Case if Case has a number; else Bottle count (bottle-pick lines).
- pack_size = Size column (5L, 750ML) or from description.
- description = Description (FRANZIA…, MON AMI…, JACOBS CREEK…).
- ssp_per_pack = Retail Price (as printed).
- cost_per_pack =
    * Wholesale Price Case when the line is sold by Case (Case qty filled), OR
    * Price Bottle when the line is sold by Bottle only (Case empty, Bottle filled).
  Never put Retail or LLC into cost.
- amount = Total column (line extension). Case lines: often = Case × WS Case.
  Bottle lines: often = Bottle × Price Bottle (e.g. 6 × 6.66 = 42.96).
- LLC = Ohio liquor/tax-style fee column — NOT a shared sheet field. Ignore LLC for
  cost/ssp/amount (do not add LLC into amount unless Total already includes it —
  use printed Total as amount).

OTHER RULES:
- Extract beer AND wine/spirits product Prod# rows from the same PDF.
- Skip TAX, SUBTOTAL, TOTAL, CREDITS, Case/Bottle footer counts, signatures, logos.
- ship_to_* = customer block (VET RETAIL OPS / store address), not Esber Canton letterhead.
- One JSON object per product Prod# row (across all invoice pages/tables).
""".strip(),
        critical_rules=(
            "MULTIPAGE: scan EVERY page; IGNORE checks/paystubs only; "
            "extract EVERY product table on EVERY invoice page; multiple invoice #s OK; "
            "prefer more product rows — do not stop after first table; "
            "notes list all invoice #s found. "
            "BEER(red): UPC|Prod#|Case|Kegs|Desc|Retail|Price|Dep|ExtTotal → "
            "upc,item_code,qty=Case,ssp=Retail,cost=Price,amount=ExtTotal. "
            "Retail may be < Price — still Retail→ssp Price→cost; trust Case×Price=Ext. "
            "WINE(blue): UPC|Prod#|Case|Bottle|Size|Desc|Retail|WS Case|Price Bottle|LLC|Total → "
            "qty=Case else Bottle; pack_size=Size; ssp=Retail; "
            "cost=WS Case if Case qty else Price Bottle; amount=Total; IGNORE LLC in cost/ssp. "
            "SKIP: EMPTY keg rates, TAX/SUBTOTAL/TOTAL/CREDITS, signatures, check pages."
        ),
        notes="Sample 20260727_233821_9312ed9e4c.pdf: beer 559759 (9@$435.45) + wine 559649 (6@$219.50); check $654.95.",
    ),
    VendorSpec(
        key="seven_up",
        display_name="7UP Midvale",
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
        detect_labels=(
            "7up",
            "7-up",
            "7up midvale",
            "seven up",
        ),
        extract_rules="""
VENDOR = 7UP Midvale (soft-drink DSD / bottler delivery ticket).
Letterhead top: "7Up Midvale", 5554 Gundy Dr, Midvale, OH 44653, (740) 922-5253.
Customer block is often a c-store name (e.g. EAGLE BP) — that is ship-to, NOT the vendor.
Do not classify vendor as Eagle BP.

MULTIPAGE / MULTI-TICKET PDF (one tall scan often stacks two tickets):
1. Scan the entire page top to bottom (sales ticket then damage/credit ticket).
2. Extract product lines from SALES invoice AND DAMAGE / credit invoice when present.
3. Multiple invoice numbers are normal (e.g. SALES 4012630832 + DAMAGE/ASSOC 4012630833).
4. notes: list all invoice numbers found.
5. Skip COD notices, RECEIVED BY, legal NOTICE, signatures, handwritten check memos.

=== SALES ticket layout ===
Header: INVOICE <number>, DATE, TIME, ASSOC CREDIT/DEBIT INVOICE#.
Customer: name + address + ACCOUNT #.
Column band under "SALES":
  DESCRIPTION / WHSLE | CASES | SALES UNITS | UPC/SKU  NET | TAX | AMOUNT

Product blocks are multi-line:
  line A (pack header): e.g. "12 OZ ALUM CAN 12PK X 2", "16 OZ ALUM CAN LS 12", "67.6OZ/2L PLASTIC BTL PP149 LS 8"
  line B (flavor/SKU): e.g. "12z12P SK Orange", "16z12GhstEn OrnCrm", UPC/SKU pair "078000113167/10000865"
  line C (numbers): WHSLE, CASES, UNITS, NET, TAX, AMOUNT
  optional PKG subtotal row — SKIP PKG rows (they repeat cases/units/amount only).

FIELD MAP (SALES product flavor lines only — one JSON object per flavor/SKU line):
- upc = LEFT side of UPC/SKU before slash (barcode, keep leading zeros; e.g. 078000113167).
- item_code = RIGHT side after slash (internal SKU; e.g. 10000865).
- description = pack header + flavor text (e.g. "12 OZ ALUM CAN 12PK X 2 SK Orange").
- pack_size = pack token from header (12PK X 2, LS 12, LS 24, LS 8, SLEEK LS 12, etc.).
- qty_cases = CASES column (not SALES UNITS).
- cost_per_pack = NET column (actual charged pack/case price). amount should equal CASES × NET.
- ssp_per_pack = WHSLE column when printed (list/wholesale reference) — may differ from NET.
- amount = AMOUNT column. Strip $.
- Put source invoice number in notes or description only if needed; prefer notes listing invoice #s.
- Do NOT invent UPC from account # or route #.

=== DAMAGE / credit ticket layout ===
Header may say DAMAGE (not SALES). Columns often:
  DESCRIPTION | WHSLE | UNITS | PIECE | UPC/SKU NET | TAX | AMOUNT
AMOUNT may be in parentheses meaning credit/return, e.g. (28.80).
- Still one product object per damage SKU line.
- qty_cases = UNITS or PIECE count as printed (damage is often unit-based).
- cost_per_pack = NET; amount = signed extension (negative if parentheses/credit).
- Same upc/item_code split on UPC/SKU.

SKIP entirely:
- PKG subtotal lines
- TOT SALE / TOT DMGD summary lines
- AMOUNT DUE
- PALLET PLASTIC / SHELL PLASTIC equipment lines (not sellable product)
- COD-CHECK OR MONEY ORDER, RECEIVED BY, NOTICE blocks
""".strip(),
        critical_rules=(
            "VENDOR=7Up Midvale (Gundy Dr) — customer EAGLE BP is ship-to NOT vendor. "
            "Scan full tall ticket: SALES invoice + DAMAGE/credit invoice if stacked. "
            "SALES cols: WHSLE|CASES|UNITS|UPC/SKU NET|TAX|AMOUNT. "
            "upc=LEFT of UPC/SKU slash; item_code=RIGHT SKU; qty_cases=CASES not UNITS; "
            "cost_per_pack=NET; ssp_per_pack=WHSLE; amount=AMOUNT (=CASES×NET). "
            "One object per flavor/SKU line under pack headers; SKIP PKG subtotals, "
            "TOT SALE/TOT DMGD, AMOUNT DUE, PALLET/SHELL lines, signatures. "
            "DAMAGE: amount negative if (parens); qty from UNITS/PIECE. "
            "notes list all invoice #s (e.g. 4012630832, 4012630833)."
        ),
        notes="Sample 20260728_030651_8d9c43069c.pdf Newcomerstown: sales 4012630832 $237.55 + damage 4012630833 ($28.80).",
    ),
    VendorSpec(
        key="abarta_coke",
        display_name="ABARTA Coca-Cola Beverages",
        aliases=(
            "abarta",
            "abarta coca cola",
            "abarta coca-cola",
            "abarta coca cola beverages",
            "abarta coca-cola beverages",
            "coca cola beverages llc",
            "coca-cola beverages llc",
            "abarta coca cola beverages llc",
            "pittsburgh, pa 15253",
            "po box 536675",
        ),
        detect_labels=(
            "abarta",
            "abarta coca cola",
            "coca cola beverages",
            "coca-cola beverages",
        ),
        extract_rules="""
VENDOR = ABARTA Coca-Cola Beverages LLC (Coca-Cola bottler / DSD).
REMIT TO: ABARTA Coca Cola Beverages LLC, PO Box 536675, Pittsburgh, PA 15253-5908.
Customer/ship-to is the store (e.g. VET RETAIL OPS LLC, Parma) — NOT the vendor.
Do not use store name as vendor_key.

INVOICE IDENTITY:
- Primary invoice # is the long INV# / barcode (e.g. 5349998035), often also labeled VENDOR#.
- OUTLET STORE# is the Coke outlet id (e.g. 501784359) — not the invoice number.
- DEL DATE is delivery datetime. User-typed short numbers may be wrong — prefer printed INV#.

COLUMN LAYOUT under SALES (left → right):
  DESCRIPTION | MAT# | QTY | PRICE | CONN | RATE | NET | EXTENDED

Multi-line product rows (common):
  line A: short desc + MAT# + QTY + PRICE + CONN(e.g. ZDCS) + RATE(discount, often negative) + NET + EXTENDED
  line B under desc: 12-digit UPC (0490…, 0708…, 8116…) and pack units count (12, 24, …)
Category band headers (e.g. "12 OZ 12-Pk 24 SPARKLING", "16 OZ 1-Ls 24 ENERGY DR") and
right-side category subtotals (e.g. 156.12, 180.88) are NOT product lines — skip them.

FIELD MAPPING (critical):
- item_code = MAT# only when printed in the MAT# column and numeric (e.g. 115586, 133145).
  Tokens like NK0414141 glued into description are NOT MAT# — leave item_code empty and still
  extract the line (upc/amount). Empty MAT# lines must be review-flagged by the pipeline.
- upc = 12-digit UPC under the description (keep leading zeros). Never put MAT# into upc.
- description = product text (12ZCAN12FP SPRITE, 16ZCSK24LS MON NRG, etc.).
- pack_size = from category header or desc (12/12oz, 24/16oz, 8/2L, 12/20oz, …).
- qty_cases = QTY column (cases).
- cost_per_pack = NET column (net case price AFTER deal/discount). NEVER use PRICE for cost when NET is present.
- amount = EXTENDED (line extension). Must equal QTY × NET when both clear.
- ssp_per_pack = ALWAYS leave empty / null. PRICE is list wholesale BEFORE deal (foots to TOTAL
  PRODUCTS), NOT shelf suggested retail. Do not put PRICE into ssp_per_pack or ssp_per_unit.
  Optional notes may say list/PRICE totals if useful.
- RATE is the discount dollars (often negative) — do not put RATE into cost/ssp/amount.
- CONN is deal code (ZDCS etc.) — ignore for sheet fields.

Also extract every product MAT# row across ALL pages of multipage invoices.

SKIP entirely:
- Category headers and category total amounts on the right
- DEPOSITS ON SALES / SHELL lines (<<IMPLIED>>, 0.00)
- DELIVERY RECAP block
- NET PRODUCT QTY / SINGLES / CONSUMER QTY counts
- TOTAL PRODUCTS, TOTAL ADJUSTMENTS, AMOUNT DUE, AMOUNT PAID, TERMS
- Barcodes-only headers, "Scanned with CamScanner", customer service footers

ship_to_* = SHIP TO / PAYER store block (Parma address), not ABARTA Pittsburgh remit-to.
notes: include printed INV# and DEL DATE; mention TOTAL ADJUSTMENTS if large.
""".strip(),
        critical_rules=(
            "VENDOR=ABARTA Coca-Cola Beverages LLC (Pittsburgh) — store/ship-to is NOT vendor. "
            "Invoice # = long INV#/barcode (e.g. 5349998035), not outlet store#. "
            "COLS: DESC|MAT#|QTY|PRICE|CONN|RATE|NET|EXTENDED. "
            "item_code=numeric MAT# only (NK… in desc is NOT MAT# — leave item_code empty); "
            "upc=12-digit under desc (not MAT#); qty_cases=QTY; "
            "cost_per_pack=NET (after discount) — NEVER use PRICE as cost when NET present; "
            "amount=EXTENDED (=QTY×NET). "
            "ssp_per_pack=EMPTY always — PRICE is list wholesale not shelf SSP/retail. "
            "Ignore RATE/CONN for money fields. One object per product row all pages. "
            "SKIP: category headers/subtotals, SHELL deposits, DELIVERY RECAP, TOTAL/AMOUNT DUE footers."
        ),
        notes="Sample 20260728_195542_4926bd4c5b.pdf Parma: INV 5349998035, 36 lines, AMOUNT DUE 1188.36. SSP left blank (PRICE=list).",
    ),
    VendorSpec(
        key="beverage_distributors",
        display_name="Beverage Distributors Inc",
        aliases=(
            "beverage distributors",
            "beverage distributors inc",
            "beverage distributors inc.",
            "beverage distributor",
            # Cleveland letterhead anchors (Parma sample 20260821_153032_ae66fc2717)
            "3800 king",
            "3800 king ave",
            "king ave cleveland",
            "(216) 431-1600",
            "216-431-1600",
            "216 431 1600",
        ),
        detect_labels=(
            "beverage distributors",
            "beverage distributors inc",
            "beverage distributors inc.",
        ),
        extract_rules="""
VENDOR = Beverage Distributors Inc (Cleveland OH beer/wine/RTD wholesaler).
Letterhead top: "BEVERAGE DISTRIBUTORS INC", 3800 King Ave, Cleveland, OH 44114,
phone (216) 431-1600. Timestamp line e.g. "Fri Aug 21, 2026 10:36 AM".
Customer / ship-to (PEARL&BRADLEY SUNOCO, EAGLE STORE, ARCO, VET RETAIL OPS, etc.) is NOT the vendor.
Payee on a stapled CHECK may also say "BEVERAGE DISTRIBUTORS INC" — that confirms vendor but the check page has NO product table.

MULTIPAGE PHONE-SCAN PDF (common):
1. Scan EVERY page.
2. IGNORE payment CHECK pages (Pay to the Order Of, bank MICR, handwritten amount) — no product lines.
3. Extract the invoice page(s) with ITEM# table only.
4. notes: mention check amount if visible (e.g. $1238.70) and invoice #.

COLUMN LAYOUT (left → right) under dashed header:
  ITEM# | QTY | DESCRIPTION | UPC | SSP | PRICE | DISC | DEP | NET | EXT

FIELD MAPPING (critical — do not swap money columns):
- item_code = ITEM# (5 digits common, keep leading zeros: 00119, 00241).
- upc = UPC column barcode digits (often 11–12; keep leading zeros). Never put ITEM# in upc.
- qty_cases = QTY (cases).
- description = DESCRIPTION brand/flavor words.
- pack_size = pack token from description (12PK CN, 24PK CN, 6NR, 12NR, 30PK CN, 240Z CN,
  23.5OZ CAN, 19.2, 160Z CN LS, etc.). Description often wraps to a second line under the same ITEM#
  (e.g. "MK MXD 160Z STR" + "DAQ"; "WHITE CLAW 12PK" + "BLK CHE") — merge pack/flavor into
  description/pack_size; never attach wrap lines to the next ITEM#.
- ssp_per_pack = SSP (suggested sell / shelf).
- cost_per_pack = NET column (wholesale AFTER line discount). amount should equal QTY × NET.
  PRICE is list wholesale BEFORE DISC — do NOT put PRICE into cost_per_pack when NET is present.
  DISC is dollars-off list (e.g. 1.60); DEP is deposit (often 0.00) — neither maps to sheet money fields.
- amount = EXT (line extension). Strip currency symbols.
- cost_per_unit / ssp_per_unit: leave empty unless explicitly per-unit.

OTHER RULES:
- Invoice# is numeric (e.g. 787548). Account#, Load, Driver, Salesrep are not invoice numbers.
- Extract EVERY product ITEM# row top→bottom until Cases/Bottles/Kegs footer counts.
- Qty 0 product rows still extract if present.
- SKIP: Cases/Bottles/Kegs count footers, COD terms blocks, check pages, signatures, CamScanner marks.
- ship_to_* = account/customer block (left header), not King Ave Cleveland letterhead.
- One JSON object per product ITEM# row only.
""".strip(),
        critical_rules=(
            "VENDOR=Beverage Distributors Inc (3800 King Ave Cleveland) — store/ship-to NOT vendor. "
            "MULTIPAGE: IGNORE check/pay pages; extract ITEM# table pages only. "
            "COLS: ITEM#|QTY|DESC|UPC|SSP|PRICE|DISC|DEP|NET|EXT. "
            "item_code=ITEM# (leading zeros); upc=UPC barcode; qty_cases=QTY; "
            "ssp_per_pack=SSP; cost_per_pack=NET (after DISC) — NEVER PRICE when NET present; "
            "amount=EXT (=QTY×NET). DISC/DEP unmapped. "
            "WRAP: continuation under same ITEM# → same row pack/desc. "
            "SKIP: Cases/Bottles/Kegs footers, check pages, signatures."
        ),
        notes=(
            "Sample 20260821_153032_ae66fc2717.pdf Parma inv 787548: "
            "check $1238.70 + invoice 28 lines / 48 cases (NET cost, EXT=QTY×NET)."
        ),
    ),
    VendorSpec(
        key="rl_lipton",
        display_name="R.L. Lipton Distributing Company",
        aliases=(
            "r.l. lipton",
            "r.l lipton",
            "rl lipton",
            "lipton distributing",
            "lipton distributing company",
            "r.l. lipton distributing",
            "r.l. lipton distributing company",
            # Valley View OH letterhead anchors (Parma sample 20260821_154840_f0a8f586bc)
            "9797 sweet valley",
            "sweet valley drive",
            "valley view, ohio 44125",
            "valley view, oh 44125",
            "(216) 475-4150",
            "216-475-4150",
            "216 475 4150",
            "(216) 475-6256",
            "216-475-6256",
        ),
        detect_labels=(
            "r.l. lipton",
            "rl lipton",
            "lipton distributing",
            "r.l. lipton distributing company",
        ),
        extract_rules="""
VENDOR = R.L. Lipton Distributing Company (NE Ohio beverage wholesaler / picksheet).
Letterhead: "R.L. Lipton Distributing Company", 9797 Sweet Valley Drive, Valley View, Ohio 44125,
Phone (216) 475-4150, Fax (216) 475-6256.
Banner may say "Not Final Picksheet" — still extract all product rows (final vs not-final does not change mapping).
Customer / ship-to (EAGLE STORES, BP # 85, Pearl Rd, ARCO, VET RETAIL OPS, etc.) is NOT the vendor.
Driver / salesrep names (e.g. Jason Madden, Roy Strump) are NOT the vendor.

COLUMN LAYOUT (left → right) on thermal picksheet tickets:
  ITEM# | QTY | DESCRIPTION | U.P.C. | S.S. PRICE | PRICE | DISC. | TOTAL

FIELD MAPPING (critical):
- item_code = ITEM# (keep leading zeros; e.g. 02903, 02904).
- upc = U.P.C. barcode digits (11–12). Never put ITEM# into upc.
- qty_cases = QTY (cases).
- description = brand + flavor (ARIZ … KIWI STRAWBERY). DESCRIPTION often wraps:
    line A: pack form (ARIZ 24/22 CAN)
    line B: flavor (KIWI STRAWBERY, SWEET TEA, DRAGNFRUIT MANG, RIZZLER BERRY)
  Merge both lines under the same ITEM# into description; pack_size from pack token (24/22 CAN, 24/16OZ, etc.).
  Never attach the next row's flavor line to the previous ITEM#.
- ssp_per_pack = S.S. PRICE column (suggested sell; often unit shelf e.g. 0.99 / 1.00 or pack 17.99). **Always copy S.S. PRICE into ssp_per_pack as printed** — do not leave ssp_per_pack empty when S.S. PRICE is on the ticket.
  Also set ssp_per_unit to the same digits when S.S. is clearly per-unit (< ~5.00 on multi-pack / multi-bottle lines).
- DISC. is **per-case** dollars off list PRICE (e.g. 3.20), not a total-line lump unless QTY=1.
- cost_per_pack = **net case cost after discount**:
    * If DISC > 0: cost_per_pack = PRICE − DISC (e.g. 32.35 − 3.20 = 29.15).
    * If DISC is 0.00 / blank: cost_per_pack = PRICE.
  NEVER put S.S. PRICE into cost. NEVER use list PRICE as cost when DISC is non-zero.
- amount = TOTAL (line extension). Must equal QTY × cost_per_pack (i.e. QTY × (PRICE − DISC)).
  Example: QTY 2, PRICE 32.35, DISC 3.20 → cost 29.15, TOTAL 58.30.
- cost_per_unit: leave empty unless explicitly per-unit wholesale.

OTHER RULES:
- Invoice# is numeric near header (e.g. 443755, 443756). Prefer printed Invoice# over handwritten memos.
- Account# / BP# / Load / PO# are not invoice numbers.
- Extract EVERY product ITEM# row until category/totals block — beer, soft drink, wine, etc. all product rows.
- Multiple picksheets per stop are normal (different Invoice#). One photo = one invoice table.
- SKIP (not product lines): Beer/Wine & Liq/Soft Drink/Misc/Credits category $ lines,
  Total Sales, Total Discount, Total Content, Total Deposit, Total Credits, Over/Short,
  Picksheet Total, Customer/Driver Signature, "Not Final Picksheet" banners, barcodes-only strips.
- Handwritten circled totals (e.g. $496.16) are often multi-invoice payment memos (sum of several picksheets) —
  NOT this ticket's product total. Prefer printed Picksheet Total / category Beer$ or Soft Drink$.
- ship_to_* = left customer block (Eagle/Parma address), not Sweet Valley Drive letterhead.
- One JSON object per product ITEM# row only.
""".strip(),
        critical_rules=(
            "VENDOR=R.L. Lipton Distributing (9797 Sweet Valley Dr Valley View) — store/ship-to NOT vendor. "
            "COLS: ITEM#|QTY|DESC|U.P.C.|S.S.PRICE|PRICE|DISC|TOTAL. "
            "item_code=ITEM# (leading zeros); upc=U.P.C.; qty_cases=QTY; ssp_per_pack=S.S.PRICE (always fill when printed; mirror to ssp_per_unit if unit shelf); "
            "cost_per_pack=PRICE−DISC when DISC>0 else PRICE — NEVER S.S.; NEVER list PRICE if DISC nonzero; "
            "amount=TOTAL (=QTY×(PRICE−DISC)). "
            "WRAP: pack line A + flavor line B under same ITEM#. "
            "ALL product rows until category footers. "
            "SKIP: Beer/Wine/Soft Drink/Misc category $ rows, Picksheet Total block, signatures, "
            "Not Final Picksheet banners; ignore handwritten multi-invoice $ memos."
        ),
        notes=(
            "Samples: Parma 443756 Arizona $119; Parma 443755 beer $377.16 (DISC→net); "
            "ARCO East Ave PDF 20260822_185810_49eed16da1 inv 443513 soft drinks 18 lines $454.35 "
            "(Soft Drink 31 cases; cost=PRICE−DISC; S.S.→ssp)."
        ),
    ),
    # Non-beer wholesalers still hit c-stores — keep generic rules strong
    VendorSpec(
        key="coremark",
        display_name="Core-Mark",
        aliases=(
            "core-mark",
            "coremark",
            "core mark",
        ),
        detect_labels=("core-mark", "coremark"),
        extract_rules="""
VENDOR = Core-Mark (c-store wholesale).
- Often has UPC and item # both — prefer UPC in upc when 11–13 digits; still store short code in item_code.
- Dense multi-page invoices: extract every visible product line from the photo.
- Qty may be units not cases — still put primary ordered qty in qty_cases field (best effort).
- Extended price → amount; unit cost → cost_per_unit or cost_per_pack as labeled.
""".strip(),
        critical_rules=(
            "Prefer upc when 11–13 digit barcode; item_code=short SKU. "
            "qty_cases=primary qty (units OK if no cases). amount=extension; cost as labeled. "
            "Extract every product line; skip pure fee/tax totals."
        ),
    ),
)

GENERIC = VendorSpec(
    key="generic",
    display_name="Generic vendor",
    aliases=(),
    detect_labels=("unknown", "generic", "other"),
    extract_rules="""
VENDOR = unknown / generic c-store supplier invoice.
- Identify supplier name from header/logo if possible.
- Prefer true UPC/EAN when present; otherwise capture ITEM#/SKU as item_code.
- One JSON object per product line; never invent prices or codes.
- qty_cases = primary quantity column (cases if shown, else units).
- amount = line extension/total when available.
""".strip(),
    critical_rules=(
        "upc=real barcode only; else item_code=ITEM#/SKU. "
        "qty_cases=primary qty; amount=line total. Skip payment/tax-only footers."
    ),
    notes="Fallback when no registry vendor matches.",
)

_BY_KEY: Dict[str, VendorSpec] = {v.key: v for v in VENDORS}
_BY_KEY[GENERIC.key] = GENERIC


def list_vendors() -> List[Dict[str, str]]:
    return [
        {"key": v.key, "display_name": v.display_name, "notes": v.notes}
        for v in VENDORS
    ] + [{"key": GENERIC.key, "display_name": GENERIC.display_name, "notes": GENERIC.notes}]


def get_vendor(key: Optional[str]) -> VendorSpec:
    if not key:
        return GENERIC
    return _BY_KEY.get(key.strip().lower(), GENERIC)


def match_vendor_text(text: str) -> Optional[VendorSpec]:
    """Match registry vendor from free-text name (alias scan, priority order)."""
    t = (text or "").strip().lower()
    if not t:
        return None
    for v in VENDORS:
        if v.matches(t):
            return v
    return None


# Tramonte + Superior share the Akron DC ticket header. Address/phone alone must not
# lock either key — letterhead company NAME is required (detect_prompt rule 8 + code).
SHARED_AKRON_DC_VENDOR_KEYS = frozenset({"tramonte", "superior_beverage"})
# Exact superior.aliases entries that are warehouse cues, not company names.
_SHARED_AKRON_DC_ALIAS_SET = frozenset(
    {
        "1267 s. main",
        "1267 s main",
        "s. main st akron",
        "main st akron",
        "(330) 535-3103",
        "330-535-3103",
    }
)
_SHARED_AKRON_DC_REASON_MARKERS = (
    "1267",
    "535-3103",
    "535.3103",
    "5353103",
    "main st akron",
    "s. main",
    "s main st",
)


def _name_aliases_for_detect(vendor: VendorSpec) -> Tuple[str, ...]:
    """Company-name aliases/labels — excludes shared Akron DC address/phone cues."""
    names = tuple(a for a in vendor.aliases if a not in _SHARED_AKRON_DC_ALIAS_SET)
    labels = tuple(lbl for lbl in (vendor.detect_labels or ()) if lbl)
    # Preserve order, drop dups
    seen = set()
    out: List[str] = []
    for a in names + labels:
        if a not in seen:
            seen.add(a)
            out.append(a)
    return tuple(out)


def letterhead_name_vendor(text: str) -> Optional[VendorSpec]:
    """
    Match vendor from printed letterhead/company name.

    Unlike match_vendor_text, shared Akron DC address/phone aliases alone never
    select tramonte or superior_beverage (would steal the other brand's tickets).
    Glenwillow-only Superior address aliases still count as Superior identity cues.
    """
    t = (text or "").strip().lower()
    if not t:
        return None
    for v in VENDORS:
        if v.key in SHARED_AKRON_DC_VENDOR_KEYS:
            if any(a in t for a in _name_aliases_for_detect(v)):
                return v
        elif v.matches(t):
            return v
    return None


def guard_shared_akron_dc_detect(
    *,
    chosen_key: str,
    printed_name: str,
    source: str,
    reason: str,
    confidence: int,
) -> Tuple[str, str, str, int]:
    """
    Enforce letterhead NAME when finalizing tramonte vs superior_beverage.

    - Clear printed name → that vendor (alias wins).
    - Empty/ambiguous printed name, or only shared Akron address/phone cues →
      do not lock tramonte/superior (generic + no_letterhead, conf capped).
    Returns (vendor_key, source, reason, confidence).
    """
    printed = (printed_name or "").strip()
    name_hit = letterhead_name_vendor(printed)
    key = (chosen_key or "").strip().lower() or GENERIC.key

    if name_hit and name_hit.key in SHARED_AKRON_DC_VENDOR_KEYS:
        if name_hit.key != key:
            reason2 = (
                reason + f"; letterhead name → {name_hit.key} (shared Akron DC)"
            ).strip("; ")
            return name_hit.key, "alias", reason2, confidence
        return key, source, reason, confidence

    if key not in SHARED_AKRON_DC_VENDOR_KEYS:
        return key, source, reason, confidence

    # Chosen tramonte/superior without a clear company-name letterhead match.
    reason_l = (reason or "").lower()
    printed_l = printed.lower()
    shared_cue_in_printed = bool(printed) and (
        any(c in printed_l for c in _SHARED_AKRON_DC_ALIAS_SET)
        or any(m in printed_l for m in _SHARED_AKRON_DC_REASON_MARKERS)
    )
    # Printed text that only matched via shared cues (letterhead_name_vendor missed)
    only_shared_printed = bool(printed) and shared_cue_in_printed and name_hit is None
    empty_or_ambiguous = (not printed) or only_shared_printed
    shared_in_reason = any(m in reason_l for m in _SHARED_AKRON_DC_REASON_MARKERS)

    if empty_or_ambiguous or shared_in_reason or name_hit is None:
        reason2 = (
            reason
            + f"; shared Akron DC — letterhead name required (refused {key})"
        ).strip("; ")
        return GENERIC.key, "no_letterhead", reason2, min(int(confidence or 0), 40)

    return key, source, reason, confidence


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
8. TRAMONTE vs SUPERIOR share 1267 S. Main St Akron / (330) 535-3103 warehouse tickets.
   Letterhead NAME wins: "TRAMONTE DISTRIBUTING…" → tramonte; "SUPERIOR BEVERAGE…" → superior_beverage.
   Do NOT pick superior_beverage from address/phone alone when the printed name clearly says Tramonte (or vice versa).

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


def extract_schema_block() -> str:
    """Shared JSON schema for all vendors (stable sheet mapping)."""
    return """
Return ONLY valid JSON (no markdown) with this shape:

{
  "vendor": "string — supplier name as printed",
  "vendor_key": "string — registry key if known, else empty",
  "invoice_number": "string — invoice/delivery # if readable, else empty",
  "invoice_date": "string — YYYY-MM-DD if possible, else as printed",
  "ship_to_name": "string — customer / account name on ship-to block, else empty",
  "ship_to_address": "string — street + city/state/zip from ship-to (not vendor letterhead), else empty",
  "ship_to_city": "string — city only if readable, else empty",
  "overall_confidence": integer 0-100,
  "total_content": "string — printed Total Content / product $ total if shown (prefer over Invoice Total when fees exist), else empty",
  "invoice_total": "string — printed Invoice Total / Amount Due if shown, else empty",
  "picksheet_total": "string — printed Picksheet Total if shown, else empty",
  "line_items": [
    {
      "upc": "string — 12-digit UPC barcode if printed on the line, else empty",
      "item_code": "string — vendor ITEM# / product code / SKU (keep leading zeros). REQUIRED when present even if no UPC",
      "description": "string — product name verbatim",
      "pack_size": "string — e.g. 24/12oz, 4/6pk, else empty",
      "qty_cases": "string — cases/qty ordered or shipped",
      "cost_per_pack": "string — wholesale cost per pack/case if shown",
      "cost_per_unit": "string — cost per unit/each if shown",
      "ssp_per_pack": "string — suggested selling price per pack if shown (SRP/SSP/Reg Retail)",
      "ssp_per_unit": "string — suggested price per unit if shown",
      "amount": "string — line extension / line total",
      "invoice_number": "string — this row's picklist/invoice # when the packet has multiple invoices/pages; else empty or omit",
      "confidence": integer 0-100
    }
  ],
  "notes": "string — issues, blurry regions, handwritten marks"
}
""".strip()


def base_extract_rules() -> str:
    return """
GLOBAL RULES (all vendors):
- Include EVERY readable product line. Do not invent UPCs or prices.
- item_code: copy ITEM# / Item No / Prod # / Code exactly (leading zeros matter).
- upc: only a true UPC/EAN barcode (usually 11–13 digits). Do NOT put ITEM# in upc.
- If only ITEM# exists (common on beer distributor invoices), set item_code and leave upc "".
- If unsure of a value, leave it "" and lower that line's confidence.
- overall_confidence reflects image quality + completeness.
- qty_cases is the primary quantity column (cases when shown).
- Money fields: digits only when possible (e.g. 12.99), no currency symbols.
- ship_to_*: customer/delivery block only (sold-to / ship-to / account address) — never the vendor letterhead address.
- If the image is not an invoice, return line_items=[] and low overall_confidence.
""".strip()


def build_extract_prompt(
    vendor: VendorSpec,
    *,
    invoice_number: str = "",
    invoice_date: str = "",
    vendor_hint: str = "",
    printed_name: str = "",
) -> str:
    context_bits: List[str] = []
    if vendor_hint:
        context_bits.append(f"Uploader hint: {vendor_hint}")
    if printed_name:
        context_bits.append(f"Detected printed vendor name: {printed_name}")
    if invoice_number:
        context_bits.append(f"User-entered invoice #: {invoice_number}")
    if invoice_date:
        context_bits.append(f"User-entered invoice date: {invoice_date}")
    context = ("\n".join(context_bits) + "\n") if context_bits else ""

    return f"""You are reading a photo of a gas-station convenience-store vendor invoice / delivery ticket.

DETECTED VENDOR KEY: {vendor.key}
DISPLAY NAME: {vendor.display_name}

{vendor.extract_rules}

{context}
{extract_schema_block()}

Set "vendor_key" to "{vendor.key}" unless the image clearly shows a different supplier.

{base_extract_rules()}
"""


# Shared always-on rules for compact retries (never truncated).
_GLOBAL_COMPACT_CRITICAL = (
    "GLOBAL: item_code=ITEM#/SKU (leading zeros); upc=true barcode only (not ITEM#); "
    "qty_cases=primary qty; money digits only no $; "
    "ship_to_*=customer block not vendor letterhead; "
    "skip payment/tax/fee-only/total footers; every product row; no markdown."
)

# Soft cap so compact retries stay smaller than the full extract prompt.
_COMPACT_PROMPT_SOFT_MAX = 2200


def build_compact_extract_prompt(vendor: VendorSpec) -> str:
    """
    Short retry prompt used when the full extract times out or fails to parse.

    Critical content is assembled in priority order and is never mid-truncated:
      1) JSON shape skeleton
      2) global compact critical map
      3) vendor.critical_rules (must-keep column/skip/wrap rules)
      4) optional head of extract_rules only if budget remains

    Do NOT rely on truncating extract_rules alone — that drops PRICE≠NET,
    wrap ownership, and footer skip rules mid-sentence.
    """
    schema = (
        "Extract invoice line items as JSON only:\n"
        '{"vendor":"","vendor_key":"'
        + vendor.key
        + '","invoice_number":"","invoice_date":"",'
        '"ship_to_name":"","ship_to_address":"","ship_to_city":"",'
        '"overall_confidence":0,"line_items":[{"upc":"","item_code":"","description":"","pack_size":"",'
        '"qty_cases":"","cost_per_pack":"","cost_per_unit":"","ssp_per_pack":"",'
        '"ssp_per_unit":"","amount":"","invoice_number":"","confidence":0}],"notes":""}\n'
    )
    header = f"Vendor focus: {vendor.display_name} ({vendor.key}).\n"
    global_crit = f"{_GLOBAL_COMPACT_CRITICAL}\n"
    vendor_crit = (vendor.critical_rules or "").strip()
    if vendor_crit:
        vendor_crit_block = f"VENDOR CRITICAL (do not ignore):\n{vendor_crit}\n"
    else:
        vendor_crit_block = ""

    core = schema + header + global_crit + vendor_crit_block
    remaining = _COMPACT_PROMPT_SOFT_MAX - len(core)

    # Optional context from full rules — only whole budget leftover, never the sole carrier of critical maps.
    tips = (vendor.extract_rules or "").strip()
    tips_block = ""
    if tips and remaining > 120:
        # Prefer not to re-copy text already present in critical_rules
        budget = min(remaining - 20, 400)
        if len(tips) > budget:
            # Break on newline when possible so we don't cut mid-rule
            chunk = tips[:budget]
            nl = chunk.rfind("\n")
            if nl >= 80:
                chunk = chunk[:nl]
            tips_snip = chunk.rstrip() + "…"
        else:
            tips_snip = tips
        tips_block = f"More layout context:\n{tips_snip}\n"

    return (core + tips_block).rstrip() + "\n"


def resolve_vendor_key(
    *,
    detected_key: str = "",
    vendor_name: str = "",
    model_vendor_key: str = "",
) -> VendorSpec:
    """
    Pick registry vendor from detect call + extract payload + alias text.
    Priority: explicit known key → alias match on name → generic.
    """
    for key in (detected_key, model_vendor_key):
        k = (key or "").strip().lower()
        if k in _BY_KEY and k != GENERIC.key:
            return _BY_KEY[k]
        if k == GENERIC.key:
            # still try name aliases before accepting generic
            break
    matched = match_vendor_text(vendor_name)
    if matched:
        return matched
    if (detected_key or "").strip().lower() == GENERIC.key:
        return GENERIC
    if (model_vendor_key or "").strip().lower() == GENERIC.key:
        return GENERIC
    return GENERIC
