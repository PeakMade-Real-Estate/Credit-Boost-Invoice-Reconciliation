# Copilot Context Map — Credit Boost Invoice Reconciliation

> **Purpose:** Load this file at the start of each session to orient quickly without re-exploring the project.  
> **Last updated:** 2026-07-31

---

## What This App Does

Flask web application that reconciles vendor invoices (Credit Boost / Rent Plus) against Entrata cash-received reports.  A user uploads an invoice file and a cash report, selects the vendor and reporting month, and the engine returns a per-property match with exceptions flagged at INFO / WARNING / BLOCKING severity.  Results are stored in SQLite and can be downloaded as Excel workbooks.

---

## Repo Layout at a Glance

```
app.py                          Flask application factory (create_app)
config.py                       All config classes (Config, DevelopmentConfig, ProductionConfig)
requirements.txt

config/
  vendors/
    credit_boost_boom.yaml      Qualifying filters, paths, charge code for Boom
    rentplus.yaml               Filters, rate rules path, PDF description regex for Rent Plus
  property_mappings/
    boom_property_rollups.csv   Boom property name → internal ID mapping (⚠ needs populating)
    rentplus_property_aliases.csv
  rate_mappings/
    rentplus_rates.csv
  cash_reports/
    entrata_receipts.yaml       Column map for Entrata "Receipts by Charge Code" report

data/
  property_master.csv           Master list of properties (source of truth for matching)
  property_aliases.csv          RentPlus display-name aliases
  rate_mapping.csv

models/
  reconciliation_models.py      Shared dataclasses: PropertyResult, PropertyMaster,
                                  ReconciliationResult, CashRecord, InvoiceLine,
                                  all Enums (MatchStatus, ExceptionSeverity, etc.)
  boom_models.py                Boom-specific: BoomTransactionLine, BoomPropertyRollup
  schemas.py                    result_to_json() serialiser

routes/
  reconciliation_routes.py      All HTTP routes for the main reconciliation flow
                                  (upload, review, exceptions, download)
  setup_routes.py               /setup/property-master wizard (seed property master
                                  from PDF invoice + cash report)

services/
  __init__.py                   Exposes run_reconciliation as package public API
  reconciliation_service.py     ⭐ run_reconciliation() — the single engine entry point
  validation_service.py         run_all_validations() — 5 structured checks post-reconcile
  output_generator.py           generate_outputs() → accounting summary + audit workbooks
  database.py                   SQLite CRUD: init_db, save_run, get_run, list_runs,
                                  save_overrides, get_overrides, get_known_hashes
  property_master_loader.py     load_property_master(), load_property_aliases()
  property_matching.py          6-priority match engine (used by RentPlus only)
  rate_mapping.py               load_rate_rules(), apply_rate_mapping()
  cash_report_parser.py         parse_cash_report() — Entrata CSV/XLSX
  invoice_parser.py             parse_invoice() — generic CSV/XLSX invoice
  pdf_invoice_parser.py         parse_rent_plus_pdf() — RentPlus PDF-specific
  boom_invoice_parser.py        parse_boom_file() — Boom CSV/XLSX
                                  ⭐ BOOM_COLUMN_MAP: resolves loose column headings
  utils.py                      normalize_text(), parse_decimal() ($, accounting parens),
                                  safe_divide()
  property_master_seeder.py     seed_from_invoice_and_cash_report()

  vendor_strategy/
    __init__.py                 Registry: get_vendor_strategy(), normalize_vendor_code()
                                  _STRATEGY_REGISTRY maps codes to classes
                                  _VENDOR_CODE_ALIASES maps display names to codes
    base.py                     VendorReconciliationStrategy ABC — defines the interface
    boom_strategy.py            BoomReconciliationStrategy (vendor_code: credit_boost_boom)
    rentplus_strategy.py        RentPlusReconciliationStrategy (vendor_code: rent_plus)

templates/
  upload.html                   Main upload page
  review.html                   Exception review / manual override UI
  results.html                  Final reconciliation results
  exceptions.html               Exception detail view
  base.html
  setup/seed_upload.html        Property master wizard — upload step
  setup/seed_review.html        Property master wizard — review/approve step
  errors/404.html  413.html  500.html

static/
  css/main.css
  js/main.js

Test Files/
  Boom Sample export.csv        ⭐ Single-sheet CSV exported from the "Boom Sample export Test"
                                  sheet in the original Excel workbook.  Property names have
                                  been replaced with real portfolio names for local testing.

tests/                          pytest suite (conftest.py has fixtures)
outputs/                        Generated Excel reconciliation files land here
uploads/invoices/               Uploaded invoice files (UUIDs)
uploads/cash_reports/           Uploaded cash report files (UUIDs)
```

---

## Reconciliation Engine — Execution Order

`run_reconciliation()` in `services/reconciliation_service.py` orchestrates these steps in order.  Steps 2–5 are delegated to the active strategy.

```
1. Resolve vendor strategy   vendor_strategy/__init__.py → get_vendor_strategy()
2. Load reference data       strategy.load_vendor_reference_data()
3. Parse invoice file        strategy.parse_vendor_file()
4. Normalise / filter rows   strategy.normalize_vendor_data()   ← qualifying filter applied here
5. Validate vendor data      strategy.validate_vendor_data()
6. Parse cash report         cash_report_parser.parse_cash_report()
7. Match to properties       strategy.match_to_properties()
8. Aggregate + rate map      strategy.aggregate_and_rate_map()
9. Join cash records         reconciliation_service (shared)
10. Portfolio totals          reconciliation_service (shared)
11. Run validations           validation_service.run_all_validations()
12. Determine status          reconciliation_service (shared)
```

---

## Vendor Profiles

### Credit Boost powered by Boom (`credit_boost_boom`)

| Item | Detail |
|---|---|
| Strategy class | `BoomReconciliationStrategy` in `services/vendor_strategy/boom_strategy.py` |
| Config file | `config/vendors/credit_boost_boom.yaml` |
| Invoice parser | `services/boom_invoice_parser.py` → `parse_boom_file()` |
| File formats | CSV, XLSX |
| Property matching | Explicit rollup table — `config/property_mappings/boom_property_rollups.csv` |
| QTY calculation | Count of **distinct qualifying transaction IDs** per property |
| AMOUNT calculation | Sum of transaction amounts for qualifying rows |
| Rate multipliers | None — no rate exceptions raised |
| Qualifying filter | All four dimensions must match (AND logic): `category`, `transaction_type`, `template_name`, `subject_type` |

**Actual Boom export column values used by qualifying filters (as of 2026-07):**

| Filter | Value |
|---|---|
| `category` | `partner_boom_report_ongoing_fee` |
| `transaction_type` | `Invoice` |
| `template_name` | `BoomReport` |
| `subject_type` | `Boom::ReportingAccount` |

**Boom export CSV column layout (relevant columns):**

| CSV column | Maps to | Notes |
|---|---|---|
| `ID` | `transaction_id` | Unique transaction identifier |
| `Property Name` | `boom_property_id` | Used for rollup lookup ← intentional (see session notes) |
| `Property ID` | _(ignored)_ | Numeric Boom internal ID — not yet mapped |
| `Amount` | `transaction_amount` | Accounting format e.g. `($2)` → `-2.00` |
| `Category` | `category` | |
| `Transaction Type` | `transaction_type` | |
| `Template Name` | `template_name` | |
| `Subject Type` | `subject_type` | |

### Rent Plus (`rent_plus`)

| Item | Detail |
|---|---|
| Strategy class | `RentPlusReconciliationStrategy` in `services/vendor_strategy/rentplus_strategy.py` |
| Config file | `config/vendors/rentplus.yaml` |
| Invoice parser | `services/invoice_parser.py` (CSV/XLSX) or `services/pdf_invoice_parser.py` (PDF) |
| File formats | CSV, XLSX, PDF |
| Property matching | 6-priority engine in `services/property_matching.py` + alias table |
| QTY / AMOUNT | Rate multiplier driven — see `config/rate_mappings/rentplus_rates.csv` |

---

## Key Data Files

| File | Purpose |
|---|---|
| `data/property_master.csv` | Source of truth — all active properties with internal IDs |
| `config/property_mappings/boom_property_rollups.csv` | Boom property name → internal ID → PMS ID |
| `config/property_mappings/rentplus_property_aliases.csv` | RentPlus display-name variants |
| `config/rate_mappings/rentplus_rates.csv` | Rate rules (multiplier + charge code per property) |

**`boom_property_rollups.csv` schema:**
```
boom_property_id, internal_property_id, pms_property_id, boom_property_name, approved
```
- `boom_property_id` = the `Property Name` as it appears in the Boom export CSV
- `approved` must be `true` for the row to be used in matching

---

## SQLite Schema (reconciliation.db)

Tables: `reconciliation_runs`, `manual_overrides`  
Functions in `services/database.py`: `init_db`, `save_run`, `get_run`, `list_runs`, `delete_run`, `save_overrides`, `get_overrides`, `get_known_hashes`, `get_next_sequence`, `register_file_hash`, `update_run_outputs`

---

## Adding a New Vendor

1. Create `config/vendors/<vendor_code>.yaml`
2. Subclass `VendorReconciliationStrategy` in `services/vendor_strategy/<name>_strategy.py`
3. Register in `services/vendor_strategy/__init__.py` under `_STRATEGY_REGISTRY`
4. Add display-name aliases to `_VENDOR_CODE_ALIASES` in the same file
5. Add vendor code to `SUPPORTED_VENDORS` in `config.py`

---

## Session History — Context Carried Forward

### Session: 2026-07-29 / 2026-07-30 / 2026-07-31

**What we did:**

Reviewed the Boom export test file (`Test Files/Boom Sample export.csv`) against the reconciliation logic and found three issues.

---

**Issue 1 — Qualifying filters (FIXED 2026-07-30)**  
`config/vendors/credit_boost_boom.yaml` had placeholder filter values (`Credit Boost`, `Positive`, `Credit Boost Template`, `Resident`) that matched nothing in the actual Boom export. All four filters were updated to match the real export values (`partner_boom_report_ongoing_fee`, `Invoice`, `BoomReport`, `Boom::ReportingAccount`).

---

**Issue 2 — Column resolver picked numeric Property ID over Property Name (FIXED 2026-07-30)**  
`BOOM_COLUMN_MAP` in `services/boom_invoice_parser.py` listed `"property id"` before `"property name"` as candidates for the `boom_property_id` field.  The Boom CSV has both columns; the resolver was picking the numeric `Property ID` (e.g., `109`) instead of the human-readable `Property Name` (e.g., `48 West`).  Since the rollup table is keyed by name, every property would have raised an `UNMATCHED_PROPERTY` blocking exception.  Fixed by reordering candidates so `"property name"` / `"property_name"` resolve first.

---

**Issue 3 — boom_property_rollups.csv is empty (PENDING)**  
`config/property_mappings/boom_property_rollups.csv` only has two placeholder rows (`Summit Ridge`, `Maple Grove`).  The test file uses these portfolio properties:

| Boom `Property Name` | Internal ID | PMS ID |
|---|---|---|
| 48 West | _TBD_ | _TBD_ |
| 555 Boulevard | _TBD_ | _TBD_ |
| 698 Prospect | _TBD_ | _TBD_ |
| Auden Upstate | _TBD_ | _TBD_ |
| Beach Club | _TBD_ | _TBD_ |
| Buckeye Hall | _TBD_ | _TBD_ |

**Action needed:** Confirm internal IDs and PMS IDs for each property, then populate the rollup CSV.  Also follow up with Boom about whether the `Property ID` (numeric) column in their export can be used as a stable identifier in the future — if so, `boom_property_id` in the rollup should use those numeric IDs and the column map ordering would need to be revisited.

---

**What is working correctly (confirmed):**

- `parse_decimal()` in `services/utils.py` correctly handles Boom's `($2)` accounting-notation amounts (converts to `-2.00`)
- Duplicate transaction ID validation in `BoomReconciliationStrategy.validate_vendor_data()` is correct
- QTY = distinct transaction IDs per property / AMOUNT = sum logic is correct
- The overall strategy pipeline, property rollup matching, and aggregation are structurally sound

---

## Authentication

Phase 1 uses a session placeholder (`session["user"] = "web_user"` in `app.py`).  The app is structured so Azure Easy Auth or Microsoft Entra ID can be injected into `flask.g` / `flask.session` later without touching business logic.
