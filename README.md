# HealthOps Intelligence

**Healthcare data analytics prototype Â· Prototype v1**

HealthOps Intelligence is a healthcare-focused, dataset-agnostic analytics prototype. It helps users upload tabular healthcare and hospital-operations datasets, inspect their structure, profile data quality, review statistical anomaly candidates, create a separate cleaned copy, build visual summaries, and ask constrained questions about aggregate results.

> **Prototype boundary:** This repository is not a clinical decision-support system and is not certified for HIPAA, GDPR, or any other regulatory regime. Use synthetic or properly de-identified data only. Do not upload identifiable patient information, credentials, or confidential hospital records.

## Current capabilities

- **File ingestion:** CSV, TSV, JSON, JSONL/NDJSON, XML in tabular form, Excel workbooks (XLSX, XLS, XLSB when optional readers are installed), Parquet when an optional reader is installed, and bounded ZIP bundles.
- **Database input:** Read-only SQLite snapshots. The PostgreSQL database foundation currently supports authentication and application metadata; it is not a general-purpose PostgreSQL analytics connector.
- **Profiling:** Table/column inventory, inferred data types, row counts, distinct counts, missingness, duplicate checks, numeric summaries, and data-quality candidates.
- **Quality review:** IQR-based outlier candidates and selected healthcare/business-rule checks. Statistical flags are candidates for investigation, not confirmed data errors.
- **Cleaning workflow:** Approved transformations create a separate derived copy; the source copy is preserved for review/reversion.
- **Analytics:** Aggregations, rankings, charts, relationship candidates, decision prompts, and Markdown reporting.
- **Ask Your Data:** A constrained analyst that plans supported aggregate operations and executes calculations locally. The optional LLM planner receives schema metadata and a redacted question rather than the uploaded DataFrame rows; answers still require review.
- **Authentication foundation:** First-owner setup and login with PostgreSQL-backed sessions, password hashing, and a tenant-aware metadata schema foundation.

See [Prototype scope](docs/PROTOTYPE_SCOPE.md), [Architecture](docs/ARCHITECTURE.md), and [Known limitations](docs/KNOWN_LIMITATIONS.md).

## Local setup (Windows PowerShell)

The current development setup uses Python 3.13 and PostgreSQL. Existing project installations also use a local `.env` file for database and optional AI-provider settings. Never commit `.env`.

```powershell
cd "$env:USERPROFILE\Downloads\healthops-intelligence-foundation\vitals_and_healthops"

# Activate the existing virtual environment
.\.venv\Scripts\Activate.ps1

# Install dependencies (safe to repeat)
python -m pip install -r requirements.txt

# Use your configured local validation database
$env:HEALTHOPS_DB_NAME = "healthops_validation_db"
python server.py
```

Open <http://127.0.0.1:8765> and sign in with the administrator account created during first-owner setup.

For a new development machine, configure a PostgreSQL database and the required database roles/credentials first, then apply the Alembic migrations using the dedicated migrator credentials. Do not reuse production credentials in a demo environment. See [Deployment readiness](docs/DEPLOYMENT_READINESS.md).

## Run checks

```powershell
$env:HEALTHOPS_ENV = "test"
$env:HEALTHOPS_LLM_ENABLED = "false"
python -m py_compile server.py settings.py persistence.py analytics_engine.py llm_analyst.py
python -m unittest discover -s tests -v
$testExitCode = $LASTEXITCODE
Remove-Item Env:HEALTHOPS_LLM_ENABLED -ErrorAction SilentlyContinue
Remove-Item Env:HEALTHOPS_ENV -ErrorAction SilentlyContinue
if ($testExitCode -ne 0) { throw "Tests failed." }
```

The test configuration disables the external LLM call so tests are deterministic and do not require an API key.

## Project structure

```text
vitals_and_healthops/
â”œâ”€â”€ server.py                 # HTTP API and application orchestration
â”œâ”€â”€ settings.py               # Runtime configuration and safety checks
â”œâ”€â”€ analytics_engine.py       # Deterministic analytical operations
â”œâ”€â”€ llm_analyst.py            # Optional constrained query planner
â”œâ”€â”€ persistence.py            # Local workspace/job persistence
â”œâ”€â”€ healthops_db/             # PostgreSQL models, auth and session helpers
â”œâ”€â”€ alembic/                  # Database migrations
â”œâ”€â”€ static/
â”‚   â”œâ”€â”€ css/app.css
â”‚   â””â”€â”€ js/
â”œâ”€â”€ tests/                    # Synthetic-data regression tests
â”œâ”€â”€ docs/                     # Architecture, scope, operations and demo notes
â””â”€â”€ instance/                 # Local runtime data; intentionally not committed
```

## Data safety and privacy

- Do not use real patient records or protected health information in this prototype.
- Use synthetic or properly de-identified datasets for demonstrations and tests.
- Keep `.env`, database passwords, API keys, uploaded datasets, local snapshots, and backups out of Git.
- The current application still has one process-wide analytics workspace. Authentication and PostgreSQL row-level-security foundations do **not** yet make all analytics requests independently tenant-isolated.
- Do not expose this build as a multi-user healthcare service. A restricted synthetic-data demo is the maximum deployment scope until the readiness gates are completed.

Read [SECURITY.md](SECURITY.md) before publishing the repository.

## Demo dataset used during prototype validation

A hospital charges dataset supplied by the project owner was uploaded and analyzed locally. The observed test run contained 163,065 rows and 12 columns. The workflow exercised schema detection, data-quality profiling, a whitespace-trimming cleaning copy, city-level average-charge aggregation, provider-ID distinct counts, and DRG-mix summaries. These results are prototype validation observations, not clinical or pricing conclusions.

The source dataset is not included in this repository. Users must independently verify licensing and redistribution rights before sharing any third-party dataset.

## Deployment status

**No hosted application URL is configured yet.** The current runtime is local-development oriented. Before deploying, the server must support the host and port assigned by the hosting platform, local file persistence must be handled appropriately, and the single-user process-wide analytics state must remain behind a restricted synthetic-data demo boundary.

See [Deployment readiness](docs/DEPLOYMENT_READINESS.md). The recommended first hosting evaluation is Render for the web service plus a separate managed PostgreSQL project for authentication/metadata. This is a prototype path, not production certification.

## License

No open-source license has been selected yet. Until a license is added, do not assume that this repository grants permission to reuse or redistribute the code.