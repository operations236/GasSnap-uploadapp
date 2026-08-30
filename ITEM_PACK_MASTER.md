# Item Pack Master (units per case)

**Status:** Phase 1 live (manual fill → master tab). **SSP bridge + blank SSP backfill + live OCR SSP enrich (2026-08-30).** Full Phase 2 qty lookup on OCR write still optional.  
**Workbook:** DayClose-Killbuck Marathon (`1dcZufZoV7whkLiSzOkM8qarUXetrIr_KHBqusfWmb1M`)  
**Tab:** `Item Pack Master`  
**Locked decisions:** 2026-08-23 (operator); SSP from master 2026-08-30

## Locked product rules

| # | Decision |
|---|----------|
| 1 | Master lives as a **Google Sheet tab** in the DayClose workbook |
| 2 | Lookup key = **UPC only** (preserve leading zeros; sheet writes RAW) |
| 3 | No master hit / cannot compute unit fields → row stays review-worthy (**Needs Review**) when Phase 2 lands |
| 4 | **Cost per Unit** on write (Phase 2): only on master hit — prefer live `Cost per Pack ÷ Extracted Qty`; master also stores **last-seen** Cost per Unit |
| 5 | **SSP per Unit** stored on master as last-seen reference (Superior ticket SSP is usually already unit-level → often equals SSP per Pack on Inv) |
| 6 | **Unit Name** documented on master rows over time (6pk / bottle / can / 15pk / …) |
| 7 | Build path: manual Extracted Qty on Inv tabs → upsert script → later live lookup |
| 8 | Optional PDi dump later to seed/compare — not required to start |

### Formulas (when master hits)

- `Extracted Qty` = units per **one case** (from master) — stable gold field
- `Calculated Qty` = `Qty (Cases) × Extracted Qty`
- `Cost per Unit` (invoice math) = `Cost per Pack ÷ Extracted Qty`
- Master **Cost per Unit** / **SSP per Unit** = last-seen snapshot for reference (prices move; not SoT for P&L)
- No hit → do **not** invent unit fields; leave blank (Phase 2 will force review)

## Master columns

| Column | Meaning |
|--------|---------|
| UPC | Match key |
| Extracted Qty | Units per case (gold) |
| Unit Name | What “unit” means for this SKU (operator-maintained) |
| Cost per Unit | Last-seen wholesale per unit (reference) |
| SSP per Unit | Last-seen suggested retail per unit (reference) |
| Description | Last seen (info) |
| Pack Size Example | Last seen pack string (info) |
| Vendor Example | Last seen vendor (info only — **not** part of match key) |
| Source | `manual_inv_…` / `upsert_from_inv` / `pdi_dump` |
| Active | TRUE/FALSE |
| Notes | Conflicts, caveats |
| Updated At | Last upsert |
| Hit Count | How many source rows touched this UPC |

## Operator workflow (Phase 1)

1. After OCR lands on `Inv - {Store}`, manually fill **Extracted Qty** (and Cost per Unit if you want) for new SKUs — same as Killbuck Superior.
2. Upsert into master (one-liner):

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/upsert_item_pack_master.py
```

Killbuck-only:

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/upsert_item_pack_master.py --tabs "Inv - Killbuck"
```

Dry-run:

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/upsert_item_pack_master.py --dry-run
```

### Backfill already-ingested Inv rows (blanks)

Fills blank **Extracted Qty** / **Calculated Qty** / **Cost per Unit** / **SSP per Pack** / **SSP per Unit** from master UPC hit. Never overwrites filled cells. Does not invent on miss.

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/backfill_inv_qty_from_master.py --dry-run
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/backfill_inv_qty_from_master.py
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/backfill_inv_qty_from_master.py --ssp-only --tabs "Inv - ARCO"
```

First qty run 2026-08-30: **222** cells across Killbuck/ARCO/Parma/Newcomerstown (74 ext + 74 calc + 74 cpu); 93 Superior gold kept; ~855 UPC misses (other vendors / ITEM#).

### OBD dual-layout SSP seed (ITEM# bridge)

Layout A tickets print SSP (no UPC). Layout B print UPC (no SSP). Join on OBD **ITEM#** → master **UPC → SSP per Unit**.

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/seed_obd_ssp_from_layouts.py --dry-run
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/seed_obd_ssp_from_layouts.py
```

Defaults: A `uploads/20260822_190933_7df92c1520.json` (244557) + B `uploads/20260826_182318_49148774c1.json` (253838) → 14 seeds. Then `--ssp-only` backfill on Inv tabs.

### Live OCR SSP enrich (Wave 3)

`item_pack_master.py` + `ocr.py`: after vendor fixes, blank `ssp_per_pack`/`ssp_per_unit` filled from master UPC hit (never overwrite ticket SSP; skip `abarta_coke`). Health: `item_pack_ssp_enrich=true`.

3. Open tab **Item Pack Master** — confirm new UPCs, fill **Unit Name** when you care, fix Notes/CONFLICT rows.
4. Conflict default: **keep existing Extracted Qty**, note the disagreement. Overwrite only with `--force-ext` if you intend to.

## Seed (done 2026-08-23)

- Source: `Inv - Killbuck` Superior inv **3754642** manual Extracted Qty + Cost/Unit formulas
- **74** unique UPCs, **0** ext conflicts
- Cost per Unit + SSP per Unit columns added same day (74/74 filled from Inv)
- Source tag: `manual_inv_killbuck_superior`
- Upsert: blank prices fill on harvest; `--force-prices` overwrites existing Cost/SSP per Unit

## Phase 2 — live OCR path

```
extract → normalize line items → vendor fixes
       → Item Pack Master by UPC:
            blank SSP → fill ssp_per_pack + ssp_per_unit from master (SHIPPED 2026-08-30)
            Extracted/Calculated/Cost-per-unit from master ext (optional / not fully wired)
       → miss: leave blank; Needs Review per vendor policy
       → append sheet
```

Shipped for **SSP enrich** (`item_pack_master.enrich_line_items_ssp_from_master`).  
Still out of scope until asked: full auto Extracted Qty on every OCR write; pack-string SoT; vendor+item_code live key.

## PDi dump (optional)

If you export pricebook items, we can compare:

- Does export include **units per case / case pack / pack size**?
- Does UPC format match invoice UPC (check digit, leading zeros)?

Bring a sample dump when ready; we diff against `Item Pack Master` before bulk import.

## Why not pack-string rules as SoT

Industry pattern = item master / pricebook case pack, not per-invoice regex. Pack text can **suggest** while building the master; production trust = UPC → Extracted Qty.
