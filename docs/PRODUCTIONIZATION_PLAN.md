# HealthOps Intelligence — Architecture Assessment and Productionization Plan

**Status:** Active engineering roadmap, not a production-readiness certification
**Assessment date:** 2026-10-29
**Current build:** Single-user, loopback-only local analytics application

## 1. Executive assessment

The project is a useful functional prototype: it can read several tabular formats, profile loaded DataFrames, calculate descriptive metrics, render charts, propose reversible in-memory cleaning operations, answer a limited set of deterministic analytical questions, and generate a Markdown report.

It is not yet a production platform for healthcare organisations. The current backend is a single `server.py` module built on `http.server`; application state lives in process-global dictionaries; analytical data is read into pandas DataFrames; background analysis uses in-process daemon threads; and there is no authentication, user or organisation isolation, durable data/job storage, persisted audit log, database migration layer, production query engine, or hardened deployment setup. The assistant is rule-based rather than a general natural-language query planner. Reports currently focus on Markdown and derived-copy CSV data.

The provided ZIP did not contain the `docs/` or `tests/` directories linked from its README. This revision adds both and makes the documented unit-test command executable.

**Safety boundary:** continue to use synthetic or properly de-identified datasets only. Keep the service bound to loopback. Do not expose it to a network or use identifiable patient data until identity, authorisation, data isolation, encryption, audit, operational controls, and a customer/jurisdiction-specific privacy and security review have been implemented.

## 2. Current architecture and data flow

```text
Browser
  ├─ index.html
  ├─ static/css/app.css
  └─ static/js/app.js
       │ HTTP/JSON + multipart uploads
       ▼
Python standard-library ThreadingHTTPServer (server.py)
  ├─ file loaders / ZIP validation / SQLite read-only snapshot
  ├─ process-global SESSION and JOBS dictionaries
  ├─ in-process analysis threads
  ├─ pandas profiling, heuristics, chart aggregation, cleaning copy
  ├─ deterministic pattern-based assistant
  └─ Markdown report builder
```

The UI and server do share real analysis results, but this is still a monolithic local application. `SESSION` is shared by all incoming requests in the process. Analysis serialization by a lock alone does not provide user/workspace isolation or durable, transactional state. Uploaded files and derived DataFrames remain memory-resident and disappear when the process exits.

## 3. Changes implemented in this foundation pass

- Extracted the stylesheet and browser JavaScript from the HTML into `static/css/app.css` and `static/js/app.js` so that visual and interaction code can be maintained independently.
- Added a guarded static-asset route that resolves requested paths under the dedicated `static/` directory and rejects path traversal.
- Tightened the Content Security Policy to permit scripts from the application's own origin rather than inline scripts. Inline style attributes remain allowed because the existing chart renderer emits computed presentation styles.
- Hardened request `Content-Length` parsing against malformed, negative, oversized, and prematurely terminated request bodies.
- Corrected a natural-language aggregation bug where a field named `Total Charges` caused a question asking for its average to be interpreted as a sum. The matched field label is removed before interpreting aggregate intent.
- Expanded the frontend upload extension allowlist to include TSV, JSONL/NDJSON, XML, XLSB, and the other formats advertised by the backend.
- Read the upload size limit from `/api/config` instead of hard-coding 100 MB in the browser. The client provides early feedback; the server remains authoritative.
- Centralised display formatting for analytical numbers at two fractional digits to avoid floating-point-looking values such as `147441.33399999997` in the visual layer.
- Added deterministic unit and HTTP-surface tests for loaders, ZIP path validation, SQLite read-only snapshots, analytics/chat regression cases, visualization fields, branding rendering, static assets, security headers, and static path traversal.

These changes improve maintainability and correctness. They do **not** add authentication, persistence, multi-user isolation, scalable data processing, a general-purpose LLM-based query planner, or production compliance controls.

## 4. Verified baseline

Run from this directory:

```powershell
python -m py_compile server.py
node --check static/js/app.js
python -m unittest discover -s tests -v
```

The test suite currently contains 24 deterministic tests. It uses synthetic fixtures and does not use patient data. See `tests/test_server.py` for the exact coverage.

## 5. Prioritised roadmap

### P0 — Local reliability and correctness

- Continue adding regression tests before changing analytical behaviour.
- Remove remaining ambiguous heuristic assumptions from chart generation and recommendation calculations.
- Validate query inputs, empty datasets, duplicate names, malformed file content, and resource limits consistently.
- Bound and expire completed job metadata; add clear job failure and cancellation semantics.
- Synchronise updates to process-global data state so concurrent requests cannot observe partially replaced datasets. This only improves local consistency; it is not a replacement for workspace isolation.
- Separate analytics logic, connectors, API handlers, and report generation into modules with stable contracts.
- Add full browser interaction tests for upload, navigation, chart building, cleaning approval, and export.

### P1 — Application state and tenancy foundation

- Introduce a durable metadata database with explicit migrations.
- Define organisations, users, workspaces, data sources, dataset versions, jobs, saved dashboards, semantic mappings, cleaning runs, and audit-event entities.
- Implement authentication and server-side authorisation before multi-user use.
- Enforce tenant scoping in every data-source, dataset, query, cache, report, and background-job access path.
- Persist dataset metadata and job status; store large source files in a separately configured object/file store where appropriate.
- Introduce explicit lifecycle management, retention and deletion policies, backups, and restoration tests.

### P2 — Scalable ingestion and execution

- Define connector interfaces for files, relational databases, REST APIs, and healthcare interoperability sources.
- Implement PostgreSQL and MySQL connectors with read-only, least-privilege credentials as the initial database integrations.
- Store source configuration securely; do not put secrets in the browser or ordinary application logs.
- Use background tasks for ingestion, profiling, exports, scheduled refresh, and long-running queries.
- Use a columnar query engine, query pushdown, or a warehouse/distributed backend when data size exceeds single-node memory constraints.
- Define and test explicit single-node resource limits. Do not promise arbitrary terabyte processing on a laptop.
- Support incremental refresh only where the source offers reliable change tracking or an appropriate watermark.

### P3 — Schema-aware analytical assistant

- Create a semantic layer with data dictionary entries, field synonyms, data types, units, valid aggregations, and user-approved business metric definitions.
- Translate natural-language requests to structured query plans, validate the plan, execute it with a deterministic engine, and ground responses in returned results.
- Enforce table/column allowlists, read-only execution, parameter binding, timeouts, result limits, and query-cost controls.
- Keep unsupported or ambiguous requests explicit rather than guessing.
- Expand deterministic evaluation coverage for aggregates, distinct counts, filters, grouping, date ranges, joins, missingness, multi-turn follow-ups, permissions, and refusals.
- Introduce an LLM provider only through a configured adapter. Do not send identifiable patient data externally by default.

### P4 — Trustworthy cleaning and quality

- Represent proposed cleaning operations as versioned, structured transformations with preview, approval, execution, audit, and reversal states.
- Add field-level and cross-table validation rules, with configurable thresholds and documented provenance.
- Distinguish a confirmed rule violation from an anomaly candidate. Never auto-delete outliers or silently impute missing healthcare values.
- Store before/after aggregates and the transformation manifest; protect the source dataset as immutable.
- Add tests for idempotency, reversibility, type preservation, null preservation, key constraints, and audit completeness.

### P5 — Dashboards and reports

- Persist saved dashboard definitions and their semantic/query dependencies.
- Add real filter state, drill-down, chart export, table export, and reproducible query execution.
- Ensure all pages use the same metric definitions and number-formatting policy.
- Generate PDF, DOCX, XLSX, CSV, HTML and Markdown only when the relevant export is actually implemented and tested.
- Include report scope, source versions, calculation definitions, filters, missingness, limitations, and audit provenance.
- Make reports reproducible from the same underlying analysis result rather than regenerating unsupported estimates in the presentation layer.

### P6 — Production operations and security review

- Containerise application services and configure separate development, staging and production environments.
- Add HTTPS termination, secrets management, authentication, rate limiting, CSRF protections where applicable, security headers, health/readiness checks, structured logs, alerting, metrics, and dependency vulnerability scanning.
- Define backup/restore, disaster recovery, resource and concurrency limits, retention and deletion, incident response, and logging policies.
- Complete a security threat model and a jurisdiction/customer-specific healthcare privacy assessment before handling regulated or identifiable information.
- Verify the production deployment with clean-install, migration, backup/restore, cross-tenant isolation, load, failure, and security tests.

## 6. Deployment gate

Do not call the application production-ready until all of the following are demonstrated in the target deployment:

- Authenticated access and server-enforced tenant boundaries.
- Durable and permission-scoped storage for datasets, jobs, dashboards, reports, and audit records.
- Supported connectors and formats tested against representative input files and failure cases.
- Resource-bounded analysis and long-running job execution.
- Query-plan validation and correct deterministic results for the supported analytics catalogue.
- Reversible transformations with complete audit evidence.
- Correct report/export outputs and consistent metric definitions.
- Backup/restore, observability, incident handling, and a documented operations runbook.
- Independent security/privacy review appropriate to the organisation, deployment, and data handled.

The present build is a local development foundation only.
