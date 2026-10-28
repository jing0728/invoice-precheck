# Invoice Precheck

A deterministic pre-invoice validation system that compares NetSuite sales orders, a billing queue, and Excel reference rules to produce auditable reports. The rule engine does not require an LLM.

## Features

- Order eligibility, discounts, price levels, unit prices, fulfillment scope, and EDI address checks.
- Decimal-based discount calculation and rounding, with effective-date customer rules.
- Four outcomes: `INVOICE-READY`, `DO-NOT-INVOICE`, `NEEDS-REVIEW`, and `CHECK-INCOMPLETE`.
- Local web interface, manual order checks, daily queue scans at 15:00 local time, and report archives.
- Missing, conflicting, or unrecognized evidence routes to review or incomplete status.

## Technology and Structure

Python standard-library HTTP server, openpyxl, Playwright/CDP, and HTML/JavaScript.

| Path | Purpose |
| --- | --- |
| `invoice-precheck-tool/engine.py` | Pure rule evaluation |
| `invoice-precheck-tool/sources.py` | Billing queue and workbook loading |
| `invoice-precheck-tool/netsuite_scraper.py` | NetSuite session and structured-data access |
| `invoice-precheck-tool/app.py`, `static/` | Web application and scheduling |
| `invoice-precheck-tool/backfill*.py` | Invoice backfill and preview tools |
| `netsuite-invoice-pre-check/references/` | Workbook schema, report contract, and business rules |

## Local Setup

```powershell
python -m venv .venv
.venv\Scripts\python.exe -m pip install openpyxl playwright
cd invoice-precheck-tool
..\.venv\Scripts\python.exe app.py
```

Open http://127.0.0.1:3100 . Scheduling is active while the service is running.

For a complete scan, prepare your own reference workbook using `references/workbook-schema.md`, set `WORKBOOK` in `sources.py`, and review all billing URLs, NetSuite account placeholders, and form mappings. Supply an authenticated Chrome CDP session on port 9222. Backfill also requires an authenticated billing application tab.

## Write Behavior

Queue scans include real write paths: completed checks may change an SO form field from 211 to 219, and invoice backfill may update invoice numbers and amounts in the billing application. Manual SO checks use a read-only path. Review mappings and validate in a test environment before connecting live systems.

Keep the default loopback binding. The application does not provide complete endpoint authentication and should not be exposed directly to the public internet.

## Release Limitations

Customer workbooks, credentials, browser sessions, historical reports, and the original test suite are excluded. The sanitized snapshot cannot perform complete business checks without replacement configuration and data. Historical validation notes in source documents are not test results for this release. No full application test suite was rerun during publication preparation. See [SANITIZATION.md](SANITIZATION.md).

No license file is included; confirm authorization before reuse or redistribution.
