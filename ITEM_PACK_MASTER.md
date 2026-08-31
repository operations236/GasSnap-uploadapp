# Item Pack Master (units per case) — Store + UPC

**Status:** Phase 2 live — **match key = Store + UPC** (2026-08-30 rebuild).  
**Workbook:** DayClose-Killbuck Marathon (`1dcZufZoV7whkLiSzOkM8qarUXetrIr_KHBqusfWmb1M`)  
**Tab:** `Item Pack Master`

## Locked product rules

| # | Decision |
|---|----------|
| 1 | Master lives as a **Google Sheet tab** in the DayClose workbook |
| 2 | Lookup key = **Store + UPC** (one row per PIN store per barcode; RAW zeros) |
| 3 | No master hit → leave unit fields blank (do not invent) |
| 4 | **Extracted Qty** = units per **one case** (operator gold per store) |
| 5 | **Calculated Qty** is **never stored on master** — always `Qty (Cases) × Extracted Qty` on Inv/OCR |
| 6 | **Cost per Unit** on Inv/OCR = live `Cost per Pack ÷ Extracted Qty` when both present; master stores last-seen CPU as fallback |
| 7 | **SSP per Unit** last-seen **per store** (same Store key). Ticket SSP always wins when printed |
| 8 | Blanks-only auto-fill — never overwrite hand-fills / ticket values |
| 9 | Build path: manual Extracted Qty on `Inv - {Store}` → upsert → live OCR enrich |

## Master columns

| Column | Meaning |
|--------|---------|
| **Store** | PIN store (part of match key) |
| **UPC** | Barcode (part of match key) |
| Extracted Qty | Units per case (gold) |
| Unit Name | 6pk / bottle / can / … (operator) |
| Cost per Unit | Last-seen wholesale per unit (reference) |
| SSP per Unit | Last-seen suggested retail per unit |
| Description | Last seen |
| Pack Size Example | Last seen |
| Vendor Example | Info only — **not** match key |
| Source | `upsert_from_inv` / `obd_layout_ab_bridge` / … |
| Active | TRUE/FALSE |
| Notes | Conflicts |
| Updated At | Last upsert |
| Hit Count | Harvest touches |

## Operator workflow

1. After OCR lands on `Inv - {Store}`, fill **Extracted Qty** for new SKUs (and Cost/Unit if you want — or let formula `Cost per Pack / Extracted Qty` apply on next backfill/OCR).
2. Upsert into master:

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/upsert_item_pack_master.py
```

Wipe + full rebuild from all Inv gold:

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/upsert_item_pack_master.py --wipe --rebuild
```

Dry-run:

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/upsert_item_pack_master.py --dry-run
```

### Telegram reminder (pending fills)

Daily **9:30 AM EST** cron scans all `Inv - *` tabs and Telegram-pings when lines still lack **Extracted Qty** and/or **Cost per Unit**, grouped by **store + vendor**. Silent when clean.

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/pending_extracted_qty_reminder.py
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/pending_extracted_qty_reminder.py --telegram
```

Cron line (user crontab):
`30 13 * * * …/pending_extracted_qty_reminder.py --telegram`

3. Backfill blank Inv cells from master (store-scoped):

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/backfill_inv_qty_from_master.py --dry-run
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/backfill_inv_qty_from_master.py
```

4. OBD layout A+B SSP seed (tags **Store**, default ARCO):

```bash
cd /opt/gassnaptools/upload-app && ./venv/bin/python scripts/seed_obd_ssp_from_layouts.py --store ARCO
```

## Live OCR path

```
extract → vendor fixes
       → Item Pack Master lookup (PIN store + UPC):
            blank Extracted Qty ← master
            blank Calculated Qty ← Qty × Extracted
            blank Cost per Unit ← Cost per Pack ÷ Extracted (else master CPU)
            blank SSP ← master SSP
       → never overwrite non-blank ticket/operator values
       → append sheet
```

Health flags: `item_pack_store_upc_key`, `item_pack_qty_enrich`, `item_pack_ssp_enrich`.

## Why Store + UPC

Counties/stores can have different shelf (SSP) and sometimes different cost.  
Extracted Qty (pack) is usually the same, but storing per-store keeps one simple key and avoids cross-store bleed.

## Why not pack-string rules as SoT

Industry pattern = item master / pricebook case pack, not per-invoice regex.
