# Invoice Upload System — Current Context

**Last Updated**: August 23, 2026

## Current Status
- Upload portal is live at `upload.gassnap.io` (this tree: `/opt/gassnaptools/upload-app`, uvicorn :8010)
- Backend saves photo **or PDF** + metadata and appends line items to Google Sheets
- Google Sheets integration is working (one workbook with tabs per store: `Inv - {Store}`)
- Background Gemini OCR is implemented (detect vendor → vendor-specific extract → sheets)
- PDF invoices: Gallery/Files accepts `.pdf`; OCR sends `application/pdf` natively to Gemini (no local rasterize)
- One row per line item is written to the correct store tab
- Confidence scoring + "Needs Review" flag exists
- Multi-vendor registry in `vendors.py` (not a single hard-coded prompt)
- Compact retry uses `VendorSpec.critical_rules` (never truncated) + optional tips budget
- Ship-to vs PIN soft warning is background-only (no frontend delay; PIN still routes the tab)
- **Item Pack Master** (Phase 1): DayClose tab `Item Pack Master` — UPC → Extracted Qty + last-seen Cost/SSP per Unit. Seeded Killbuck Superior 3754642 (74 UPCs). Upsert: `scripts/upsert_item_pack_master.py`. Live OCR auto-fill = **Phase 2**. Docs: `ITEM_PACK_MASTER.md`

## Supported / registered vendors
| key | status |
|-----|--------|
| `tramonte` | **Production-ready** — ARCO multipage PDF `20260822_234211_f8d6a5a95a` inv **10554909** ($713.58/19) + **10554910** ($600.86/13) = **$1314.44** / 32 lines; cols SSP\|PRICE\|DEP\|AMOUNT; cost=PRICE; shared Akron DC address with Superior — letterhead decides |
| `ohio_beverage` | **Production-ready** — Killbuck + **ARCO** PDF `20260822_190933_7df92c1520` inv **244557** (35 lines, $1738.62; cols SSP\|PRICE\|DISC\|DEP\|EXT; cost=PRICE−DISC) |
| `cavalier` | Registered |
| `house_of_larose` | Registered |
| `superior_beverage` | **Production-ready** — Glenwillow + Akron + Parma 3749614 + **Killbuck PDF** `20260822_031623_ec39b6eebc` inv **3754642** (74 lines, $3485.12; multipage check+invoice; cost=NET) |
| `heidelberg` | **Production-ready** — Killbuck picksheet `20260727_013531_844932cf53` (RETAIL vs PRICE, wrap, short brand `99`) |
| `red_bull` | **Production-ready** — Killbuck fractional load sheet + **Loudonville** photo `20260824_180544_7bb9a02bb7` inv **2037214470** (4 lines, TOTAL DUE **$183.79**; case PRICE−DISC) |
| `esber` | **Production-ready** — multipage PDF beer+wine $654.95 + **Killbuck dual-sheet photo** `20260824_235655_c84021191d` inv **564942** (wine $53.32 + beer $392.37 = check **$445.69** / 8 lines) |
| `seven_up` | **Production-ready** — Newcomerstown PDF sales+damage $208.75 + photo `20260824_150436_99fd482f9b` inv **4012228305** (20 lines, TOT SALE **$623.21** / 31 cases; multi-flavor packs; cost=NET) |
| `abarta_coke` | **Production-ready** — ABARTA Coke PDF `20260728_195542_4926bd4c5b` Parma INV 5349998035 (36 lines, $1188.36) |
| `beverage_distributors` | **Production-ready** — BDI Cleveland PDF `20260821_153032_ae66fc2717` Parma inv 787548 (28 lines, EXT $1220.64 / 48 cases; cost=NET) |
| `rl_lipton` | **Production-ready** — Parma 443756/443755 + **ARCO** PDF `20260822_185810_49eed16da1` inv **443513** (18 lines, $454.35, Soft Drink 31; cost=PRICE−DISC; QA foot OK) |
| `southeast_beverage` | **Production-ready** — Newcomerstown EAGLE BP photo `20260824_233516_46e8959b51` inv **162891** (10 lines, **$240.86**; cost=UNIT PRICE; **SSP required** — operator gold on sheet; empty ssp→NR) |
| `coremark` | Registered |
| `generic` | Fallback |

How to add the next vendor: see **`VENDORS.md`**. Spot-check bar: **`VALIDATION.md`**.

**Deploy note:** after editing `vendors.py` / `ocr.py` / `main.py`, restart uvicorn on :8010 (or `sudo systemctl restart gassnap-upload`). Live is vendor-aware only when `/health` → `ocr.vendors` lists the new key.

## Expanded Scope
The system handles **multiple beverage vendors** (beer/wine/spirits DSD, energy drink DSD, and later Coke/Pepsi/etc.).  
Each vendor can use different invoice formats → **vendor-specific extract_rules + critical_rules**, shared JSON schema + sheet columns.

## Key Decisions Made
- OCR runs asynchronously after upload (does not block the user)
- One row per line item is written to Google Sheets
- Start with own stations before opening to external operators
- Use Gemini for OCR (consistent with other parts of the stack)
- Soft-fail on OCR errors (upload still succeeds)
- Low-confidence items are flagged for review instead of auto-accepted
- Each vendor has its own `VendorSpec` (aliases + detect labels + extract_rules + **critical_rules** for compact retry)
- Compact OCR retry never truncates `critical_rules` (PRICE≠NET / RETAIL≠PRICE, wrap ownership, footer skips stay intact)
- Sheet column structure stays identical across vendors
- **Store tab = PIN/session store** (authoritative). OCR ship-to is compared in the background only: if it matches a *different* known store, metadata gets `store_check.mismatch` + rows get `Needs Review=TRUE` — no frontend delay, no auto-reroute.

## Current Focus
- **Item Pack Master Phase 1 (2026-08-23):** manual Extracted Qty on Inv tabs → upsert master by UPC; grow coverage across vendors; optional PDi dump compare later. Phase 2 = post-OCR lookup + Cost per Unit / Calculated Qty on write.
- Prior: ARCO Tramonte dual inv; OBD 244557; Lipton 443513; Killbuck Superior 3754642; post-extract QA live.
- Sheets: **append-only** unless operator asks to replace.
- After registry edits: `sudo systemctl restart gassnap-upload` then confirm `/health` qa_foot=1.00

## Working Rules
- Always read **`AGENTS.md`** (development rules) + `VISION.md` before starting work on this project.
- Prefer async/background processing.
- Keep the system simple and maintainable.
- Flag uncertain extractions for human review.
- Document how to add new vendors clearly (`VENDORS.md` + one `VendorSpec` + `VALIDATION.md` spot-check).
- Do not assume one universal prompt will work for all vendors.
- Paths: vision/context live next to the app at `/opt/gassnaptools/upload-app/` (not a separate `invoice-upload/` tree).
- **Google Sheets: append-only by default** (live path + agent re-runs). Never replace/delete sheet rows unless the user explicitly asks.
- **Git:** private repo https://github.com/operations236/GasSnap-uploadapp — standing commit/push to `origin/main` after verified work (AGENTS.md Rule 14); never stage secrets.

## Open Questions
- Should we eventually move from one workbook (tabs per store) to separate spreadsheets per store?
- Do we need a dedicated review dashboard, or is the "Needs Review" flag in Google Sheets sufficient for the first phase?
- What is the target accuracy threshold per vendor before considering it production-ready?
- How will we handle vendors with very different layouts (e.g., multi-page invoices, different units)?

## Important Constraints
- Must work reliably with real beverage invoices (tables, pack sizes, UPCs, mixed pricing).
- Should not require operators to manually enter line items.
- Must handle OCR failures gracefully without breaking the upload flow.
- Adding a new vendor should be relatively straightforward (new `VendorSpec` only).
- The system should remain maintainable even as the number of supported vendors grows.

## Notes
- Shared workbook with tabs per store (`Inv - {Store}`). May evolve later.
- Validated production-ready samples:
  - **Tramonte ARCO:** `uploads/20260822_234211_f8d6a5a95a.pdf` — inv 10554909 ($713.58) + 10554910 ($600.86) = $1314.44 / 32 lines
  - Superior Glenwillow: `uploads/20260727_002200_098f3e9d6e.jpeg`
  - Superior Akron ARCO East Ave: `uploads/20260729_223234_5d8a7de7dd.jpg` — inv 10549752, 40 lines, $1844.69 / 80 cases
  - Red Bull: `uploads/20260727_011518_ec9b2466a3.jpeg`
  - Heidelberg: `uploads/20260727_013531_844932cf53.jpeg`
  - Esber: `uploads/20260727_233821_9312ed9e4c.pdf` — beer 559759 ($435.45) + wine 559649 ($219.50) = check $654.95
  - 7UP Midvale: `uploads/20260728_030651_8d9c43069c.pdf` — sales 4012630832 ($237.55) + damage 4012630833 (−$28.80) = $208.75
  - ABARTA Coke: `uploads/20260728_195542_4926bd4c5b.pdf` — INV 5349998035, 36 lines, AMOUNT DUE $1188.36 (user typed 1267 corrected)
  - Beverage Distributors Inc: `uploads/20260821_153032_ae66fc2717.pdf` — Parma inv 787548, 28 lines, EXT $1220.64 / 48 cases (cost=NET)
  - R.L. Lipton (latest): `uploads/20260821_154840_f0a8f586bc.jpeg` — Parma inv 443756, 4 lines, Picksheet Total $119.00
- Early uploads of a new vendor may still show `vendor_key=generic` in metadata until the registry entry exists and uvicorn is restarted; re-OCR + `replace_invoice_rows_for_store` when generic wrote bad rows.
