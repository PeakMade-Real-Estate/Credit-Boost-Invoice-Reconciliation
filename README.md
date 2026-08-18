# Invoice Reconciliation App – Phase 1

Internal tool for reconciling vendor invoices (Rent Plus) against Entrata cash-received reports.

---

## Architecture Overview

```
┌─────────────────────────────────────────────────────────────────┐
│                     Flask Web Application                        │
│  routes/reconciliation_routes.py  –  file upload, display only │
└────────────────────────────┬────────────────────────────────────┘
                             │ calls
┌────────────────────────────▼────────────────────────────────────┐
│              Reusable Reconciliation Engine                      │
│  services/reconciliation_service.py  ←  run_reconciliation()   │
│                                                                  │
│  invoice_parser  →  property_matching  →  rate_mapping          │
│  cash_report_parser  →  validation_service  →  output_generator │
└─────────────────────────────────────────────────────────────────┘
```

The reconciliation engine is **completely independent of Flask**.  
It can be called from Power Automate, an Azure Function, a scheduled script,  
or the CLI — without changing any business logic.

---

## Project Structure

```
invoice_reconciliation_app/
│
├── app.py                   ← Flask application factory
├── config.py                ← Configuration (env vars)
├── requirements.txt
├── README.md
├── .env.example
│
├── routes/
│   └── reconciliation_routes.py   ← Flask routes ONLY (no business logic)
│
├── services/                      ← Reusable engine (no Flask dependency)
│   ├── reconciliation_service.py  ← Main entry point: run_reconciliation()
│   ├── invoice_parser.py
│   ├── cash_report_parser.py
│   ├── property_master_loader.py
│   ├── property_matching.py
│   ├── rate_mapping.py
│   ├── validation_service.py
│   ├── output_generator.py
│   ├── database.py
│   └── utils.py
│
├── models/
│   ├── reconciliation_models.py   ← All dataclasses
│   └── schemas.py                 ← JSON serialisation
│
├── data/
│   ├── property_master.csv        ← Reference data (edit to add properties)
│   ├── rate_mapping.csv           ← Rate rules (edit to add new rates)
│   └── property_aliases.csv       ← Approved property name aliases
│
├── templates/                     ← Jinja2 HTML templates
├── static/                        ← CSS, JS
├── uploads/                       ← Uploaded files (git-ignored)
├── outputs/                       ← Generated Excel files (git-ignored)
└── tests/
    ├── conftest.py
    ├── fixtures/                  ← Sample CSV files for tests
    ├── test_property_matching.py
    ├── test_rate_mapping.py
    ├── test_validations.py
    └── test_reconciliation_service.py
```

---

## Running Locally (VS Code)

### 1. Create a virtual environment

```powershell
cd "z:\Shared\Technology\AI Projects\Credit Boost Invoice Reconciliation"
python -m venv .venv
.venv\Scripts\Activate.ps1
```

### 2. Install dependencies

```powershell
pip install -r requirements.txt
```

### 3. Configure environment

```powershell
Copy-Item .env.example .env
# Edit .env to set SECRET_KEY and any custom paths
```

### 4. Run the app

```powershell
# If using system Python (no venv active):
$env:PYTHONPATH = "."
python app.py
```

Open http://127.0.0.1:5000 in your browser.

> **Note for this machine:** The workspace lives on a network drive (Egnyte).
> Use the system Python directly rather than a venv inside the workspace:
> `C:\Users\pbatson\AppData\Local\Python\pythoncore-3.14-64\python.exe`
> A local venv (e.g. in `C:\dev\reconciliation-venv`) works better than one
> on the network share.

### 5. Run tests

```powershell
$env:PYTHONPATH = "."
python -m pytest tests/ -v
```

With coverage:

```powershell
python -m pytest tests/ -v --cov=services --cov=models --cov-report=term-missing
```

---

## Using the Reconciliation Engine Without Flask

The engine can be called directly from any Python context:

```python
from services.reconciliation_service import run_reconciliation
from decimal import Decimal

result = run_reconciliation(
    invoice_file_path="invoice.xlsx",
    cash_report_file_path="cash_report.csv",
    reporting_month="2026-07",
    vendor="Rent Plus",
    property_master_path="data/property_master.csv",
    rate_mapping_path="data/rate_mapping.csv",
    property_aliases_path="data/property_aliases.csv",
)

print(result.status)                         # ReconciliationStatus.PASSED
print(result.portfolio_totals)
print(result.property_results)
print(result.property_exceptions)

# Generate Excel files
from services.output_generator import generate_outputs
accounting_path, audit_path = generate_outputs(result, output_folder="outputs")
```

This same call pattern works from:
- A **scheduled Python script** (Task Scheduler, cron)
- An **Azure Function** (wrap in an HTTP trigger)
- **Power Automate** (call via an API endpoint that wraps `run_reconciliation`)
- A **background job** (Celery, APScheduler)

---

## Workflow

1. Open the app and go to **New Reconciliation**.
2. Select the reporting month and vendor.
3. Upload the vendor invoice (CSV or XLSX).
4. Upload the Entrata cash-received report (CSV or XLSX).
5. Click **Start Reconciliation**.
6. If there are blocking exceptions (unmatched properties, unknown rates), you are
   redirected to the **Exceptions** page.
   - Use the dropdowns to map invoice properties to known Property IDs.
   - Use the rate-rule dropdowns to assign a rate for unknown prices.
   - Click **Apply Overrides & Re-run**.
7. Review the **Review** page: invoice total, cash totals, balance difference,
   validation checks.
8. Go to **Downloads** to generate and download:
   - **Accounting Summary** – one row per property for the accounting team.
   - **Detailed Audit File** – every invoice line, all exceptions, run metadata.

---

## Reference Data

### `data/property_master.csv`

Add a row for each active property.  Key fields:

| Field | Description |
|---|---|
| `internal_property_id` | Your internal ID (e.g. `CB-0001`) |
| `vendor_property_id` | The ID the vendor uses on their invoice |
| `pms_property_id` | Entrata / PMS property ID |
| `program_start_date` | YYYY-MM-DD |
| `program_end_date` | YYYY-MM-DD or blank if still active |
| `active` | `true` / `false` |

### `data/rate_mapping.csv`

One row per rate.  Leave `property_id` blank for general rates.  Populate it for
property-specific (legacy) rates.

### `data/property_aliases.csv`

Map common alternate names that appear on invoices to internal IDs.  
Set `approved = true` for aliases that should be auto-resolved.

---

## Security Notes

- Uploaded files are stored with random names outside `static/`.
- File types are validated (CSV and XLSX only; PDF not yet supported).
- `werkzeug.utils.secure_filename` is used for all uploads.
- Secrets are loaded from environment variables only.
- Internal file paths are never exposed in the browser.

---

## Future Roadmap

- [ ] Azure Easy Auth / Entra ID authentication
- [ ] PDF invoice parsing adapter
- [ ] SharePoint property master integration
- [ ] Azure SQL migration (schema already compatible)
- [ ] Automated scheduled reconciliation (Azure Function)
- [ ] Email notification on completion
