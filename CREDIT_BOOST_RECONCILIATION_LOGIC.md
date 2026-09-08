# Credit Boost (Boom) Reconciliation – Logic Explained

This document explains, in plain terms, how the app reconciles **Credit Boost powered by Boom** invoices. It's meant to be read by a teammate who needs to understand *what the app does* and *why*, not just *how the code is structured*.

There are two related but separate reconciliation flows in the app for this vendor:

1. **Invoice ↔ Cash Reconciliation** – "did we bill the right amount, and did we get paid?"
2. **Resident Charges Reconciliation** – "does every resident who was billed by Boom actually show up in Entrata, and vice versa?"

---

## 1. Invoice ↔ Cash Reconciliation

This is the main flow, and the one used from the app's home/upload screen: the **"Invoice vs Cash Report"** tab (the default option, as opposed to the "Resident Charges Check" tab). To run it, a user:

1. Picks a **Reporting Month**.
2. Selects **Credit Boost powered by Boom** from the **Vendor** dropdown.
3. Uploads the **Vendor Invoice** — the Boom transaction export (CSV/XLSX).
4. Uploads the **Entrata Cash-Received Report** — labeled in the UI as the **"Receipts by Charge Code"** report (CSV/XLSX). This is expected to already be scoped to the Credit Boost charge code when pulled from Entrata (the app does not currently filter the cash report by charge code for this flow — it treats every row in the uploaded file as relevant).

Submitting the form (`routes/reconciliation_routes.py::process_upload`) calls `run_reconciliation()` (`services/reconciliation_service.py`), which compares Boom's transaction export against the Receipts by Charge Code report, on a per-property basis, for the selected reporting month.

### Step-by-step

1. **Parse the Boom export** (`services/boom_invoice_parser.py`)
   Reads the vendor's CSV/XLSX export and turns every row into a `BoomTransactionLine` (property, transaction ID, category, transaction type, template name, subject type, amount, etc.).

2. **Determine which rows "qualify"** (`boom_strategy.normalize_vendor_data`)
   Not every row in the Boom export represents a billable resident fee. A row only counts if **all** of these match the config in [config/vendors/credit_boost_boom.yaml](config/vendors/credit_boost_boom.yaml):
   - `category` = `partner_boom_report_ongoing_fee`
   - `transaction_type` = `Invoice`
   - `template_name` = `BoomReport`
   - `subject_type` = `Boom::ReportingAccount`

   Rows that don't match all four are excluded from billing math (but are still visible in the raw export).

3. **Check for duplicate transaction IDs** (`boom_strategy.validate_vendor_data`)
   Each qualifying transaction ID must appear only once. If Boom's export contains the same transaction ID twice, this is flagged as a **BLOCKING** validation failure (`boom_duplicate_transaction_ids`) — the run will require review before anything is trusted.

4. **Load the property master** (`data/property_master.csv`)
   The internal source of truth for all properties: internal ID, PMS ID, property name, charge code, program start date, etc.

5. **Match each Boom property to an internal property** (`boom_strategy.match_to_properties`)
   Uses the **approved roll-up mapping** at [config/property_mappings/boom_property_rollups.csv](config/property_mappings/boom_property_rollups.csv), which maps `boom_property_id → internal_property_id` (+ PMS ID). Only rows marked `approved=true` are used.
   - If a manual override is supplied for a property, it wins.
   - If there's no approved roll-up entry for a Boom property name, the line is marked `UNMATCHED` and a **BLOCKING** `UNMATCHED_PROPERTY` exception is raised.
   - Unlike the Rent Plus vendor, **no fuzzy name matching** is attempted — the roll-up mapping is explicit and must be curated by hand.

6. **Aggregate quantity and amount per property** (`boom_strategy.aggregate_and_rate_map`)
   For each matched internal property, among **qualifying** lines only:
   - `QTY` = count of **distinct** qualifying transaction IDs.
   - `AMOUNT` = `QTY × flat_rate_per_resident` (currently **$6.50**, set in the YAML config).
   - There is **no rate table** for Boom (unlike Rent Plus) — every resident costs the same flat fee, so no rate-mapping exceptions are ever produced for this vendor.

7. **Parse the Entrata cash report** (`services/cash_report_parser.py`)
   Reads the Receipts by Charge Code export (CSV/XLSX), maps flexible column headings (property name/ID, charge code, cash received, adjustments) into `CashRecord` objects, one per property. The parser *supports* an optional `charge_code` filter, but the main upload route does not currently pass one — in practice, the file is trusted to already be scoped to the correct charge code because that's how the report was pulled from Entrata.

8. **Join cash to each property result** (`reconciliation_service._join_cash_to_properties`)
   Cash records are matched to property results by, in order: internal property ID → PMS property ID → normalized property name. For every property:
   - No matching cash record → `cash_received = 0`, **WARNING** `NO_CASH_RECORD`.
   - Cash received = 0 → **WARNING** `ZERO_CASH`.
   - Cash received < 0 → **BLOCKING** `NEGATIVE_CASH`.
   - Cash received < amount invoiced → **WARNING** `CASH_BELOW_OWED`.
   - Any cash record that never matched an invoice property → **INFO** `NO_INVOICE_RECORD`.

9. **Calculate per-property financials** (`boom_strategy.calculate_property_financials`)
   - `amount_to_pull` = the invoice amount owed.
   - `actual_property_revenue_share` = `cash_received − invoice_amount_owed`.
   - If cash received is less than what's owed, the property's `validation_status` is `warning`; otherwise `ok`.

10. **Roll up portfolio totals** (`reconciliation_service._calculate_portfolio_totals`)
    Sums quantities, invoice amounts, cash received, and revenue share across every property. `balance_difference = total_cash_received − total_invoice_amount_owed − total_actual_property_revenue_share` (this should always be ~0 by construction — it's a sanity check).

11. **Run shared validations** (`services/validation_service.py`)
    - **Invoice total balance** — does the calculated invoice total match the source file's stated total (within tolerance, default $0.01)?
    - **Duplicate file detection** — has this exact file (by SHA-256 hash) been uploaded/processed before?
    - **Portfolio balance control** — is the portfolio-level balance difference within tolerance (default $0.05)?
    - **Duplicate property records** — did the same property end up with more than one result row?
    - **Reporting month present** — sanity check that a month was supplied.
    - **Cash period consistency** — do the cash records align with the reporting month?

12. **Determine overall status** (`reconciliation_service._determine_status`)
    - Any **BLOCKING** exception or failed validation → `REQUIRES_REVIEW`.
    - Otherwise, any **WARNING** → `PASSED_WITH_WARNINGS`.
    - Otherwise → `PASSED`.

13. **(Optional) Generate output files** — an accounting workbook, an audit workbook, and a reconciliation CSV are written to `outputs/` via `services/output_generator.py`.

### Key inputs/outputs at a glance

| Input | UI label | Purpose |
|---|---|---|
| Boom transaction export (CSV/XLSX) | "Vendor Invoice" | Source of billable resident fees |
| Entrata cash-received report | "Entrata Cash-Received Report" / "Receipts by Charge Code" | Source of actual cash collected per property |
| `data/property_master.csv` | n/a (server-side reference data) | Internal property reference data |
| `config/property_mappings/boom_property_rollups.csv` | n/a (server-side reference data) | Approved Boom → internal property mapping |
| `config/vendors/credit_boost_boom.yaml` | n/a (server-side config) | Qualifying filters, flat rate, tolerances, charge code |

| Output | Meaning |
|---|---|
| `PropertyResult` (per property) | qty billed, amount owed, cash received, revenue share, status |
| `PropertyException` | Property-level problems (unmatched property, no cash, negative cash, etc.) |
| `ValidationResult` | Portfolio/file-level checks |
| Overall `ReconciliationStatus` | PASSED / PASSED_WITH_WARNINGS / REQUIRES_REVIEW / FAILED |

---

## 2. Resident Charges Reconciliation

This is a separate, finer-grained check (`services/resident_charges_reconciliation_service.py`) that drills down to the **individual resident** level rather than property totals. It answers: *"Is every resident Boom billed us for actually reflected in Entrata, and vice versa?"*

### How it works

1. **Parse the Entrata Resident Charges report** — a multi-sheet XLSX (one sheet per property), giving each resident's charged amount.

2. **Parse the Boom invoice export again**, applying the **same qualifying filters** as the main strategy (category/type/template/subject must match), to get a clean list of billed residents per property.

3. **Normalize names for matching:**
   - Entrata names come as `"Last, First"` and are converted to `"First Last"`.
   - Boom names have parenthetical suffixes stripped (e.g. `"John Smith (Co-signer)"` → `"John Smith"`).
   - Both are then run through the shared `normalize_text` normalizer (case/whitespace/punctuation insensitive) before comparison.
   - Properties are matched by normalized property name.

4. **Compare the two resident lists per property** and produce discrepancies:
   - **`charged_not_billed`** — Entrata shows a resident with a charge amount > 0, but there's no matching qualifying Boom invoice line for that property. (We charged the resident but never billed the vendor, or the vendor missed them.)
   - **`billed_not_charged`** — Boom billed for a resident at that property, but the resident either doesn't appear in Entrata or has a $0 charge there. (The vendor invoiced us for someone we don't have a matching charge for.)

5. The result (`ResidentChargesReconciliationResult`) lists these discrepancies per property so someone can investigate — e.g. a resident who moved out, a data-entry mismatch, or a genuine billing error.

### Why this exists separately

The property-level reconciliation (flow #1) can *pass* even if individual residents are mismatched, as long as the aggregate quantity and dollar amounts happen to line up. The resident-level check exists specifically to catch those cases — it's a more granular audit trail, not a replacement for the main flow.

---

## Glossary

- **Qualifying line/transaction** — a Boom export row that matches all four configured filters (category, transaction type, template name, subject type). Only qualifying lines count toward billing.
- **Roll-up mapping** — the approved, hand-curated CSV mapping Boom's property naming to our internal property IDs. Unlike Rent Plus, Boom does not use fuzzy name matching.
- **Flat rate** — Boom charges a fixed $ amount per qualifying resident (currently $6.50); there is no per-property or per-unit-type rate table like Rent Plus has.
- **Exception severities** — `INFO` (no action needed), `WARNING` (review recommended, doesn't block), `BLOCKING` (must be resolved before the run can be trusted — forces `REQUIRES_REVIEW`).
- **Charge code** — the Entrata AR code used to filter cash-received rows to only those relevant to Credit Boost (`CREDITBOOST`).

## Where to look in the code

- Orchestration: [services/reconciliation_service.py](services/reconciliation_service.py)
- Boom-specific logic: [services/vendor_strategy/boom_strategy.py](services/vendor_strategy/boom_strategy.py)
- Vendor config (filters, flat rate, tolerances): [config/vendors/credit_boost_boom.yaml](config/vendors/credit_boost_boom.yaml)
- Property roll-up mapping: [config/property_mappings/boom_property_rollups.csv](config/property_mappings/boom_property_rollups.csv)
- Cash report parsing: [services/cash_report_parser.py](services/cash_report_parser.py)
- Shared validations: [services/validation_service.py](services/validation_service.py)
- Resident-level cross-check: [services/resident_charges_reconciliation_service.py](services/resident_charges_reconciliation_service.py)
