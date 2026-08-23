# Invoice Upload System — Vision

## Purpose
Build a scalable invoice processing system that allows gas station operators to upload physical invoices from multiple beverage vendors (Beer, Coke, Pepsi, etc.) and automatically extract structured data for import into PDi.

## Scope
The system will initially focus on:
- Beer distributors (e.g., Tramonte / Ohio Beverage)
- Coke vendors
- Pepsi vendors

Each vendor uses different invoice formats, layouts, and data structures. Therefore, the system must support **vendor-specific schema and prompts**.

## Core Goals
- Eliminate manual entry of line items from beverage invoices.
- Accurately extract UPCs, descriptions, pack sizes, quantities, costs, and suggested selling prices (SSP).
- Support multiple vendors with different invoice formats.
- Maintain high data quality through confidence scoring and human review.
- Enable daily invoice processing with minimal operator effort.

## Key Principles

### 1. Vendor-Aware Architecture
- Each vendor will have its own **extraction schema** and **Gemini prompt**.
- The system must detect or accept the vendor type and apply the correct parsing logic.
- Adding a new vendor should be straightforward (new prompt + schema definition).

### 2. Async-First Processing
- Upload should succeed immediately.
- OCR and line item extraction happen in the background.

### 3. Structured Output
- One row per line item in Google Sheets.
- Consistent column structure across vendors where possible, with vendor-specific fields allowed when needed.

### 4. Human-in-the-Loop
- Low-confidence extractions are flagged with `Needs Review = TRUE`.
- Operators or staff can review and correct data before it reaches PDi.

### 5. Station-Scoped Data
- Each store has its own tab/sheet in Google Sheets.
- Data remains organized per location.

### 6. AI-Heavy Approach
- Leverage Gemini for OCR and structured extraction.
- Use AI agents for building, reviewing, and maintaining vendor-specific logic.

## Future Considerations
- Expand beyond Coke, Pepsi, and Beer to other product categories.
- Move from tab-based sheets to separate spreadsheets per store if volume grows.
- Build a dedicated review dashboard for low-confidence items.
- Eventually support direct PDi import.

## Success Metrics (Next 90 Days)
- Support at least 4 vendors (1 Beer + Coke + Pepsi + 1 more).
- Average OCR confidence ≥ 85% on supported vendors.
- All 10 internal stations actively using the system.
- Clear process defined for adding new vendors.
- Data flows reliably into Google Sheets with review flags.
