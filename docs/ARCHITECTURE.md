# Architecture overview

## Runtime path

```text
Browser
  â”œâ”€ index.html
  â”œâ”€ static/css/app.css
  â””â”€ static/js/app.js + static/js/auth.js
        â”‚ HTTP / JSON / multipart upload
        â–¼
Python ThreadingHTTPServer (server.py)
  â”œâ”€ Authentication/session HTTP integration
  â”œâ”€ Upload parsers and SQLite read-only snapshot connector
  â”œâ”€ Process-wide SESSION and JOBS state
  â”œâ”€ Pandas DataFrames and profiling/quality functions
  â”œâ”€ Cleaning proposal and derived-copy workflow
  â”œâ”€ Chart/report/decision aggregation
  â””â”€ Ask Your Data -> constrained planner -> local validated execution
        â”‚
        â”œâ”€ Local instance/ snapshots and recent-job persistence
        â””â”€ PostgreSQL healthops schema for auth, sessions and metadata foundations
```

## Main components

| Component | Responsibility |
|---|---|
| `server.py` | HTTP routes, upload handling, workspace orchestration, job execution, analysis response formatting and page API. |
| `settings.py` | Runtime configuration, validation, host/port and environment safety checks. |
| `analytics_engine.py` | Deterministic, schema-aware aggregate analytics. |
| `llm_analyst.py` | Optional constrained natural-language query planning; structured plans are validated and executed locally. |
| `persistence.py` | Local durable snapshots/recent-job metadata under the configured instance directory. |
| `healthops_db/` | PostgreSQL connection helpers, authentication functions and SQLAlchemy metadata models. |
| `alembic/` | PostgreSQL schema migrations. |
| `static/` | Browser JavaScript and stylesheet. |

## Important isolation boundary

The analytics engine currently uses a process-wide workspace/session. A single server process is shared by all browser requests. The PostgreSQL tenant schema and row-level-security foundations are not yet wired through every analytics request, upload, job, cached response and report. Consequently, the current application is a single-user prototype and must not be scaled horizontally or offered as a multi-tenant service.

## Data flow

1. A user uploads a supported file or requests a read-only SQLite snapshot.
2. The parser loads the tabular content into in-memory pandas DataFrames.
3. Profiling builds table/column metadata and aggregate quality findings.
4. Visualization and reporting endpoints use aggregate results derived from the loaded tables.
5. Approved cleaning transformations create a separate copy and do not silently overwrite the source.
6. Ask Your Data maps a request to a constrained plan, validates fields/operations, and runs the supported calculation locally. The optional planner is not permitted to execute arbitrary SQL or Python.

## State and persistence

- Analytics DataFrames and active workspace state live in the running application process.
- Local snapshots and recent-job metadata are written under `instance/` when local persistence is enabled.
- Authentication sessions and metadata are handled through PostgreSQL.
- A hosted service must not assume that its container filesystem is persistent. Workspace file persistence requires a persistent disk or object-storage design, and the in-memory single-user model remains a separate limitation.