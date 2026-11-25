"""HealthOps Intelligence local-development server with optional durable local persistence."""
from __future__ import annotations

import csv
import copy
import html
import importlib.util
import io
import json
import logging
import mimetypes
import os
import re
import secrets
import sqlite3
import zipfile
from collections import Counter
from email.parser import BytesParser
from email.policy import default as email_policy
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer
from http.cookies import SimpleCookie
from threading import Lock, Thread
from pathlib import Path
from urllib.parse import unquote, urlparse

from settings import load_settings

SETTINGS = load_settings()

import pandas as pd

from persistence import LocalPersistence
from healthops_db.auth import (
    AuthenticationError, auth_setup_required, bootstrap_first_owner,
    hash_session_token, login_user, logout_user,
)
from healthops_db.database import session_lookup
from analytics_engine import answer_question as answer_engine_v2
from llm_analyst import answer_with_llm

logging.basicConfig(
    level=SETTINGS.log_level,
    format="%(message)s",
)
LOGGER = logging.getLogger("healthops")
HERE = Path(__file__).parent

with (HERE / "branding.json").open(encoding="utf-8") as _brand_file:
    BRANDING = json.load(_brand_file)

MAX_UPLOAD = SETTINGS.max_upload_bytes
MAX_REQUEST_BYTES = 500 * 1024 * 1024
MAX_ARCHIVE_FILES = 200
MAX_ARCHIVE_UNCOMPRESSED = 500 * 1024 * 1024
MAX_ARCHIVE_RATIO = 200
SESSION_COOKIE_NAME = "healthops_session"
AUTH_BYPASS_FOR_TESTS = SETTINGS.environment == "test"

SESSION = {
    "tables": {},
    "source_tables": None,
    "cleaned": {},
    "clean_active": False,
    "last_analysis": None,
    "goal": "Hospital operations and patient flow",
    "clean_log": [],
    "generation": 0,
    "source_db": None,
    "refreshed_at": None,
}
JOBS = {}
PERSISTENCE = None
JOB_LOCK = Lock()
ANALYSIS_LOCK = Lock()

ID_RE = re.compile(
    r"patient|mrn|medical.?record|ssn|email|phone|address|birth|dob|name",
    re.I,
)
NON_MEASURE_RE = re.compile(
    r"(^|[^a-z])(id|npi|zip|postal|code|year)([^a-z]|$)"
    r"|provider.?id|patient.?id|encounter.?id|claim.?id",
    re.I,
)
DOMAIN_RULES = {
    "Patients": r"patient|mrn|medical.?record",
    "Encounters": r"encounter|visit|admission|appointment|outpatient|inpatient",
    "Diagnoses": r"diagnos|icd|condition|disease",
    "Insurance & claims": r"claim|payer|insurance|policy|coverage",
    "Billing & fees": r"bill|charge|fee|cost|amount|payment|revenue|price",
    "Medicines": r"medic|drug|prescri|pharmacy|dose",
    "Provider / facility": r"provider|hospital|facility",
    "Staff": r"staff|doctor|nurse|employee|clinician",
    "Time": r"date|time|month|year",
}


def safe(value):
    if pd.isna(value):
        return None
    if hasattr(value, "isoformat"):
        return value.isoformat()
    if hasattr(value, "item"):
        try:
            return value.item()
        except Exception:
            pass
    if isinstance(value, (str, int, float, bool)):
        return value
    return str(value)


def format_metric(value, max_fraction_digits=2):
    """Format analytical numbers without exposing binary floating-point noise."""
    try:
        number = float(value)
        if not pd.notna(number):
            return "—"
        if number.is_integer():
            return f"{int(number):,}"
        rendered = f"{number:,.{max_fraction_digits}f}"
        return rendered.rstrip("0").rstrip(".") if "." in rendered else rendered
    except (TypeError, ValueError, OverflowError):
        return str(value)


def numeric_series(series):
    """Parse common numeric and currency text without changing the source."""
    if pd.api.types.is_numeric_dtype(series):
        return pd.to_numeric(series, errors="coerce")

    cleaned = (
        series.astype("string")
        .str.replace(r"[$,]", "", regex=True)
        .str.replace("%", "", regex=False)
        .str.strip()
    )
    parsed = pd.to_numeric(cleaned, errors="coerce")
    nonnull = int(series.notna().sum())

    if nonnull and int(parsed.notna().sum()) / nonnull >= 0.85:
        return parsed

    return pd.to_numeric(series, errors="coerce")


def read_bytes(name, raw):
    ext = Path(name).suffix.lower()

    if ext in (".csv", ".tsv"):
        for encoding in ("utf-8-sig", "utf-8", "latin-1"):
            try:
                return {
                    Path(name).stem: pd.read_csv(
                        io.BytesIO(raw),
                        encoding=encoding,
                        low_memory=False,
                        sep="\t" if ext == ".tsv" else ",",
                    )
                }
            except UnicodeDecodeError:
                continue

    if ext in (".xlsx", ".xls"):
        try:
            return pd.read_excel(io.BytesIO(raw), sheet_name=None)
        except ImportError as exc:
            raise ValueError(
                "This Excel variant requires an optional reader. "
                "Install the engine documented for .xls/.xlsx support."
            ) from exc

    if ext == ".xlsb":
        try:
            return pd.read_excel(
                io.BytesIO(raw),
                sheet_name=None,
                engine="pyxlsb",
            )
        except ImportError as exc:
            raise ValueError(
                "XLSB support requires the optional pyxlsb package. "
                "Install it, then restart HealthOps Intelligence."
            ) from exc

    if ext == ".json":
        obj = json.loads(raw.decode("utf-8-sig"))
        if isinstance(obj, list):
            return {Path(name).stem: pd.json_normalize(obj)}
        if isinstance(obj, dict):
            tables = {
                str(key): pd.json_normalize(value)
                for key, value in obj.items()
                if isinstance(value, list)
            }
            return tables or {Path(name).stem: pd.json_normalize(obj)}

    if ext in (".jsonl", ".ndjson"):
        return {
            Path(name).stem: pd.read_json(io.BytesIO(raw), lines=True)
        }

    if ext == ".parquet":
        try:
            return {Path(name).stem: pd.read_parquet(io.BytesIO(raw))}
        except ImportError as exc:
            raise ValueError(
                "Parquet support requires an optional engine such as pyarrow. "
                "Install pyarrow, then restart HealthOps Intelligence."
            ) from exc

    if ext == ".xml":
        try:
            return {
                Path(name).stem: pd.read_xml(
                    io.BytesIO(raw),
                    parser="etree",
                )
            }
        except Exception as exc:
            raise ValueError(
                "XML must contain one tabular row set. "
                "Hierarchical clinical documents need a schema-specific connector."
            ) from exc

    raise ValueError(f"Unsupported format: {ext or 'unknown'}")


def load_upload(name, raw):
    if len(raw) > MAX_UPLOAD:
        raise ValueError(
            f"This local-development build accepts files up to "
            f"{MAX_UPLOAD // (1024 * 1024)} MB."
        )

    if name.lower().endswith(".zip"):
        tables = {}
        with zipfile.ZipFile(io.BytesIO(raw)) as archive:
            members = [item for item in archive.infolist() if not item.is_dir()]

            if len(members) > MAX_ARCHIVE_FILES:
                raise ValueError(
                    f"ZIP contains more than {MAX_ARCHIVE_FILES} files."
                )

            declared_total = sum(item.file_size for item in members)
            if declared_total > MAX_ARCHIVE_UNCOMPRESSED:
                raise ValueError(
                    "ZIP expands beyond the configured 500 MB limit."
                )

            for info in members:
                parts = Path(info.filename.replace("\\", "/")).parts
                if Path(info.filename).is_absolute() or ".." in parts:
                    raise ValueError(
                        "ZIP contains an unsafe path; no files were extracted."
                    )
                if info.flag_bits & 1:
                    raise ValueError(
                        "Password-protected ZIP members are not supported."
                    )
                if info.file_size > MAX_UPLOAD:
                    raise ValueError(
                        f"ZIP member {Path(info.filename).name} "
                        "exceeds the per-file limit."
                    )
                if (
                    info.file_size and info.compress_size == 0
                ) or (
                    info.compress_size
                    and info.file_size / info.compress_size > MAX_ARCHIVE_RATIO
                ):
                    raise ValueError(
                        "ZIP compression ratio exceeds the configured safety limit."
                    )

                if Path(info.filename).suffix.lower() not in (
                    ".csv",
                    ".tsv",
                    ".json",
                    ".jsonl",
                    ".ndjson",
                    ".xlsx",
                    ".xls",
                    ".xlsb",
                    ".parquet",
                    ".xml",
                ):
                    continue

                for table, dataframe in read_bytes(
                    Path(info.filename).name,
                    archive.read(info),
                ).items():
                    key = f"{Path(info.filename).stem} · {table}"[:100]
                    tables[key] = dataframe

        return tables

    return read_bytes(name, raw)


def read_sqlite_tables(path):
    """Read a local SQLite snapshot without enabling writes or extension loading."""
    file_path = Path(path).expanduser().resolve()

    if not file_path.is_file():
        raise ValueError("Choose an existing local SQLite database file.")
    if file_path.stat().st_size > 1024**3:
        raise ValueError("SQLite file exceeds the 1 GB local limit.")

    connection = sqlite3.connect(
        f"file:{file_path.as_posix()}?mode=ro",
        uri=True,
        timeout=10,
    )
    try:
        connection.execute("PRAGMA query_only=ON")
        names = [
            row[0]
            for row in connection.execute(
                "SELECT name FROM sqlite_master "
                "WHERE type='table' AND name NOT LIKE 'sqlite_%'"
            )
        ]
        if not names:
            raise ValueError("No tables found in this database.")

        return {
            name: pd.read_sql_query(
                'SELECT * FROM "' + name.replace('"', '""') + '"',
                connection,
            )
            for name in names
        }
    finally:
        connection.close()


def parse_upload_parts(body, content_type):
    """Parse browser multipart uploads without the deprecated cgi module."""
    headers = (
        b"Content-Type: "
        + content_type.encode("latin-1")
        + b"\r\nMIME-Version: 1.0\r\n\r\n"
    )
    message = BytesParser(policy=email_policy).parsebytes(headers + body)
    if not message.is_multipart():
        raise ValueError("Expected a multipart file upload.")

    files = []
    goal = None

    for part in message.iter_parts():
        disposition = part.get_content_disposition()
        name = part.get_param("name", header="content-disposition")
        filename = part.get_filename()
        payload = part.get_payload(decode=True) or b""

        if disposition == "form-data" and filename:
            files.append(
                (Path(filename.replace("\\", "/")).name, payload)
            )
        elif name == "goal":
            goal = payload.decode(
                part.get_content_charset() or "utf-8",
                errors="replace",
            )

    return files, goal


def persist_workspace_state():
    """Persist the local workspace when the normal server entry point is running."""
    if PERSISTENCE is None:
        return None
    return PERSISTENCE.save_workspace(SESSION)


def persist_job_state(job_id):
    if PERSISTENCE is None:
        return None

    with JOB_LOCK:
        job = copy.deepcopy(JOBS.get(job_id))

    if job is not None:
        PERSISTENCE.save_job(job_id, job)


def initialise_persistence():
    """Restore the last complete local workspace and recover interrupted jobs."""
    global PERSISTENCE

    data_root = SETTINGS.data_dir

    PERSISTENCE = LocalPersistence(data_root)
    interrupted = PERSISTENCE.recover_interrupted_jobs()
    restored = PERSISTENCE.load_workspace()

    if restored:
        SESSION.update(restored)

        # Keep defaults for fields introduced by later versions.
        SESSION.setdefault("tables", {})
        SESSION.setdefault("source_tables", None)
        SESSION.setdefault("cleaned", {})
        SESSION.setdefault("clean_active", False)
        SESSION.setdefault("last_analysis", None)
        SESSION.setdefault("goal", "Hospital operations and patient flow")
        SESSION.setdefault("clean_log", [])
        SESSION.setdefault("generation", 0)
        SESSION.setdefault("source_db", None)
        SESSION.setdefault("refreshed_at", None)

        LOGGER.info(json.dumps({
            "event": "workspace_restored",
            "table_count": len(SESSION.get("tables") or {}),
            "has_analysis": bool(SESSION.get("last_analysis")),
            "storage_mode": "durable-local",
        }, separators=(",", ":")))

    if interrupted:
        LOGGER.warning(json.dumps({
            "event": "jobs_interrupted_by_restart",
            "count": interrupted,
        }, separators=(",", ":")))

    LOGGER.info(json.dumps({
        "event": "local_persistence_ready",
        "storage_mode": "durable-local",
        "data_directory": str(data_root.resolve()),
    }, separators=(",", ":")))


def domain_for(name):
    return next(
        (
            label
            for label, pattern in DOMAIN_RULES.items()
            if re.search(pattern, str(name), re.I)
        ),
        "Other",
    )


def queue_analysis(goal):
    job_id = secrets.token_urlsafe(12)
    generation = SESSION["generation"]

    with JOB_LOCK:
        JOBS[job_id] = {
            "status": "processing",
            "analysis": None,
            "error": None,
            "created_at": pd.Timestamp.now(tz="UTC").isoformat(
                timespec="seconds"
            ),
        }

    persist_job_state(job_id)

    def run():
        try:
            with ANALYSIS_LOCK:
                if generation != SESSION["generation"]:
                    with JOB_LOCK:
                        JOBS[job_id]["status"] = "cancelled"
                    persist_job_state(job_id)
                    return

                result = build_analysis(goal)

                if generation == SESSION["generation"]:
                    SESSION["last_analysis"] = result
                    persist_workspace_state()

                    with JOB_LOCK:
                        JOBS[job_id].update({
                            "status": "complete",
                            "analysis": result,
                            "completed_at": pd.Timestamp.now(
                                tz="UTC"
                            ).isoformat(timespec="seconds"),
                        })
                else:
                    with JOB_LOCK:
                        JOBS[job_id]["status"] = "cancelled"

                persist_job_state(job_id)

        except Exception as exc:
            with JOB_LOCK:
                JOBS[job_id].update({
                    "status": "error",
                    "error": str(exc),
                    "completed_at": pd.Timestamp.now(
                        tz="UTC"
                    ).isoformat(timespec="seconds"),
                })
            try:
                persist_job_state(job_id)
            except Exception:
                LOGGER.exception(json.dumps({
                    "event": "job_persistence_failed",
                    "job_id": job_id,
                }, separators=(",", ":")))

    Thread(target=run, daemon=True).start()
    return job_id


def relation_candidates(tables):
    out = []
    keys = {
        name: {str(column).strip().lower(): column for column in df.columns}
        for name, df in tables.items()
    }

    for left_name, left_keys in keys.items():
        for right_name, right_keys in keys.items():
            if left_name >= right_name:
                continue

            for normalized, left_column in left_keys.items():
                if normalized in right_keys and (
                    normalized.endswith("_id")
                    or normalized in ("patientid", "encounterid", "claimid")
                ):
                    out.append({
                        "left": left_name,
                        "left_column": str(left_column),
                        "right": right_name,
                        "right_column": str(right_keys[normalized]),
                        "confidence": "candidate",
                    })

    return out[:40]


def profile():
    result = []

    for name, dataframe in SESSION["tables"].items():
        columns = []

        for column in dataframe.columns:
            series = dataframe[column]
            missing = int(series.isna().sum())
            unique = int(series.nunique(dropna=True))
            dtype = str(series.dtype)

            if pd.api.types.is_datetime64_any_dtype(series):
                kind = "date"
            elif pd.api.types.is_bool_dtype(series):
                kind = "boolean"
            elif pd.api.types.is_numeric_dtype(series) or (
                int(series.notna().sum())
                and int(numeric_series(series).notna().sum())
                / int(series.notna().sum()) >= 0.85
            ):
                kind = "numeric"
            else:
                kind = "text"

            columns.append({
                "name": str(column),
                "type": kind,
                "domain": domain_for(column),
                "missing": missing,
                "missing_pct": round(
                    100 * missing / max(len(series), 1), 4
                ),
                "unique": unique,
                "masked": bool(ID_RE.search(str(column))),
            })

        result.append({
            "name": name,
            "rows": int(len(dataframe)),
            "columns": columns,
            "duplicate_rows": (
                int(dataframe.duplicated().sum()) if len(dataframe) else 0
            ),
        })

    return result


def build_analysis(goal=None):
    goal = (
        goal
        or SESSION.get("goal")
        or "Hospital operations and patient flow"
    )
    SESSION["goal"] = goal
    tables = SESSION["tables"]
    cache = SESSION.get("_analysis_static")

    if cache:
        prof = cache["prof"]
        issues = [dict(item) for item in cache["issues"]]
        clean = [dict(item) for item in cache["clean"]]
        rows = cache["rows"]
        fields = cache["fields"]
        missing_total = cache["missing_total"]
        cells_total = cache["cells_total"]
        duplicate_total = cache["duplicate_total"]
    else:
        prof = profile()
        issues = []
        clean = []
        charts = []
        domains = Counter()
        rows = sum(len(df) for df in tables.values())
        fields = sum(len(df.columns) for df in tables.values())
        missing_total = 0
        cells_total = 0
        duplicate_total = 0

        for item in prof:
            df = tables[item["name"]]
            duplicate_total += item["duplicate_rows"]
            domains.update(column["domain"] for column in item["columns"])

            for column in item["columns"]:
                missing_total += column["missing"]
                cells_total += item["rows"]

                if column["missing"]:
                    issues.append({
                        "kind": "missing",
                        "severity": "medium",
                        "table": item["name"],
                        "column": column["name"],
                        "count": column["missing"],
                        "eligible": item["rows"],
                        "pct": round(
                            100 * column["missing"] / max(item["rows"], 1),
                            4,
                        ),
                        "title": (
                            f"{column['name']}: "
                            f"{column['missing']:,} missing values"
                        ),
                        "detail": (
                            f"{column['missing']:,} of {item['rows']:,} "
                            f"rows are blank "
                            f"({100 * column['missing'] / max(item['rows'], 1):.4f}%)."
                        ),
                        "action": (
                            f"Resolution: determine whether {column['name']} "
                            "is required for each record type. Compare with "
                            "related/source fields and fill only from an "
                            "authoritative source. Preserve unknowns as null; "
                            "do not blanket-impute. Document any approved "
                            "exclusion or imputation rule."
                        ),
                    })

                series = df[column["name"]]

                if column["type"] == "text":
                    trimmed = series.dropna().astype(str)
                    trim_count = int(
                        (trimmed != trimmed.str.strip()).sum()
                    )
                    if trim_count:
                        clean.append({
                            "kind": "whitespace",
                            "table": item["name"],
                            "column": column["name"],
                            "count": trim_count,
                            "title": (
                                f"{column['name']}: {trim_count:,} values "
                                "have leading/trailing spaces"
                            ),
                            "action": (
                                "Trim surrounding spaces in the derived copy; "
                                "this does not change category meaning."
                            ),
                        })

                    if column["unique"] > 1 and series.notna().any():
                        parsed = numeric_series(series)
                        if (
                            parsed.notna().sum()
                            / max(int(series.notna().sum()), 1) >= 0.85
                            and series.astype(str).str.contains(
                                r"[$,]", regex=True
                            ).any()
                        ):
                            clean.append({
                                "kind": "currency",
                                "table": item["name"],
                                "column": column["name"],
                                "count": int(parsed.notna().sum()),
                                "title": (
                                    f"{column['name']}: numeric values "
                                    "stored as formatted text"
                                ),
                                "action": (
                                    "Parse into a numeric field in the derived "
                                    "copy; retain the original text column for audit."
                                ),
                            })

                if column["type"] == "date":
                    parsed = pd.to_datetime(series, errors="coerce")
                    invalid = int(
                        (series.notna() & parsed.isna()).sum()
                    )
                    if invalid:
                        clean.append({
                            "kind": "date",
                            "table": item["name"],
                            "column": column["name"],
                            "count": invalid,
                            "title": (
                                f"{column['name']}: {invalid:,} values "
                                "are not parseable as dates"
                            ),
                            "action": (
                                "Inspect date format and locale before "
                                "normalizing; ambiguous values need source-owner confirmation."
                            ),
                        })

                if column["type"] == "numeric" and column["unique"] > 5:
                    numeric_values = numeric_series(series).dropna()
                    if len(numeric_values) > 10:
                        q1 = numeric_values.quantile(0.25)
                        q3 = numeric_values.quantile(0.75)
                        iqr = q3 - q1
                        lower = q1 - 1.5 * iqr
                        upper = q3 + 1.5 * iqr
                        mask = (
                            (numeric_values < lower)
                            | (numeric_values > upper)
                        )
                        count = int(mask.sum())

                        if count:
                            pct = 100 * count / len(numeric_values)
                            issues.append({
                                "kind": "outlier",
                                "severity": "review",
                                "table": item["name"],
                                "column": column["name"],
                                "count": count,
                                "eligible": len(numeric_values),
                                "pct": round(pct, 4),
                                "lower_bound": safe(lower),
                                "upper_bound": safe(upper),
                                "title": (
                                    f"{column['name']}: {count:,} IQR "
                                    f"outlier candidates ({pct:.2f}% of "
                                    f"{len(numeric_values):,} numeric values)"
                                ),
                                "detail": (
                                    f"Tukey 1.5×IQR screening bounds: "
                                    f"{safe(lower)} to {safe(upper)}. "
                                    f"Observed non-missing range: "
                                    f"{safe(numeric_values.min())} to "
                                    f"{safe(numeric_values.max())}. "
                                    "These are statistical flags, not confirmed errors."
                                ),
                                "action": (
                                    f"Resolution: (1) inspect the {count:,} "
                                    "flagged records in the source system, "
                                    "(2) compare like-for-like service/DRG and "
                                    "units, (3) correct only source-confirmed "
                                    "entry or unit errors, (4) retain clinically "
                                    "or operationally valid extremes, and "
                                    "(5) document the decision. If no source "
                                    "rule confirms an error, leave the values unchanged."
                                ),
                            })

            if item["duplicate_rows"]:
                issues.append({
                    "kind": "duplicate",
                    "severity": "medium",
                    "table": item["name"],
                    "title": f"{item['duplicate_rows']:,} exact duplicate rows",
                    "detail": (
                        "Rows match across all loaded fields; this may be "
                        "repeated extraction or valid repeated events."
                    ),
                    "action": (
                        "Check source event identifiers and extraction "
                        "timestamps. Remove only confirmed accidental duplicates, "
                        "and retain a reversible audit copy."
                    ),
                })

        # Cross-field financial checks provide actionable domain rules where supported.
        for item in prof:
            dataframe = tables[item["name"]]
            by_name = {
                re.sub(r"[^a-z0-9]", "", column["name"].lower()): column["name"]
                for column in item["columns"]
            }
            total_column = next(
                (
                    value for key, value in by_name.items()
                    if "totalpayment" in key or "totalpaid" in key
                ),
                None,
            )
            medicare_column = next(
                (
                    value for key, value in by_name.items()
                    if "medicarepayment" in key
                ),
                None,
            )

            if total_column and medicare_column:
                first = numeric_series(dataframe[medicare_column])
                second = numeric_series(dataframe[total_column])
                valid = first.notna() & second.notna()
                count = int((first[valid] > second[valid]).sum())

                if count:
                    issues.append({
                        "kind": "business_rule",
                        "severity": "high",
                        "table": item["name"],
                        "title": (
                            f"{count:,} rows where Medicare payment "
                            "exceeds total payment"
                        ),
                        "detail": (
                            f"Compared {medicare_column} with "
                            f"{total_column} where both values were parseable."
                        ),
                        "action": (
                            "Verify field definitions, currency units, and "
                            "column mapping with the source owner. This is a "
                            "rule-based inconsistency candidate, not an automatic correction."
                        ),
                    })

        if duplicate_total:
            clean.append({
                "kind": "duplicates",
                "count": duplicate_total,
                "title": (
                    f"{duplicate_total:,} exact duplicate rows across tables"
                ),
                "action": (
                    "Review keys and source extraction semantics before "
                    "approving removal."
                ),
            })

        if missing_total:
            clean.append({
                "kind": "missing",
                "count": missing_total,
                "title": (
                    f"{missing_total:,} missing cells across loaded tables"
                ),
                "action": (
                    "Review by field and business meaning; no blanket "
                    "imputation is recommended."
                ),
            })

        SESSION["_analysis_static"] = {
            "prof": prof,
            "issues": [dict(item) for item in issues],
            "clean": [dict(item) for item in clean],
            "rows": rows,
            "fields": fields,
            "missing_total": missing_total,
            "cells_total": cells_total,
            "duplicate_total": duplicate_total,
        }

    charts = []
    domains = Counter()
    for item in prof:
        domains.update(column["domain"] for column in item["columns"])

    goal_lower = goal.lower()
    dimension_patterns = (
        r"payer|insurance|drg|diagnos|service|procedure|state|region|provider"
        if any(word in goal_lower for word in ("revenue", "billing", "insurance"))
        else r"clinic|department|location|region|state|drg|diagnos|visit|encounter|provider|type"
    )
    measure_patterns = (
        r"payment|charge|cost|amount|revenue|fee"
        if any(word in goal_lower for word in ("revenue", "billing", "insurance"))
        else r"discharge|volume|count|encounter|visit|payment|charge|amount|cost"
    )

    for item in prof:
        dataframe = tables[item["name"]]
        date_column = next(
            (
                column["name"] for column in item["columns"]
                if column["domain"] == "Time"
            ),
            None,
        )

        if date_column:
            dates = pd.to_datetime(
                dataframe[date_column],
                errors="coerce",
            ).dropna()
            if len(dates) > 2:
                periods = (
                    dates.dt.to_period("M")
                    .astype(str)
                    .value_counts()
                    .sort_index()
                    .tail(18)
                )
                if len(periods) > 1:
                    charts.append({
                        "kind": "trend",
                        "title": f"Monthly records by {date_column}",
                        "labels": periods.index.tolist(),
                        "values": [int(value) for value in periods.values],
                        "table": item["name"],
                        "unit": "records",
                    })

        categories = [
            column for column in item["columns"]
            if column["type"] == "text"
            and not column["masked"]
            and 2 <= column["unique"] <= 10000
        ]
        numbers = [
            column for column in item["columns"]
            if column["type"] == "numeric"
            and column["unique"] > 1
        ]

        if categories and numbers:
            category = sorted(
                categories,
                key=lambda column: (
                    bool(re.search(dimension_patterns, column["name"], re.I)),
                    column["unique"],
                ),
                reverse=True,
            )[0]
            measure = sorted(
                numbers,
                key=lambda column: (
                    bool(re.search(measure_patterns, column["name"], re.I)),
                    column["unique"],
                ),
                reverse=True,
            )[0]

            work = pd.DataFrame({
                "category": dataframe[category["name"]]
                    .astype("string").fillna("Unknown"),
                "value": numeric_series(dataframe[measure["name"]]),
            }).dropna(subset=["value"])

            grouped = (
                work.groupby("category")["value"]
                .agg(["mean", "count"])
                .sort_values("mean", ascending=False)
                .head(12)
            )

            if len(grouped) > 1:
                charts.append({
                    "kind": "comparison",
                    "title": (
                        f"Mean {measure['name']} by {category['name']}"
                    ),
                    "labels": [str(value) for value in grouped.index],
                    "values": [float(value) for value in grouped["mean"].values],
                    "group_counts": [
                        int(value) for value in grouped["count"].values
                    ],
                    "table": item["name"],
                    "unit": measure["name"],
                    "measure": measure["name"],
                    "dimension": category["name"],
                })

        measure = next(
            (
                column for column in item["columns"]
                if column["type"] == "numeric"
                and column["unique"] >= 10
                and re.search(
                    r"charge|payment|amount|cost|fee|price|discharge|wait|length|age",
                    column["name"],
                    re.I,
                )
            ),
            None,
        )

        if measure:
            values = numeric_series(
                dataframe[measure["name"]]
            ).dropna()
            if len(values) > 10:
                histogram = pd.cut(
                    values,
                    bins=10,
                    duplicates="drop",
                ).value_counts(sort=False)
                charts.append({
                    "kind": "distribution",
                    "display_type": "column",
                    "title": f"Distribution of {measure['name']}",
                    "labels": [str(value) for value in histogram.index],
                    "values": [int(value) for value in histogram.values],
                    "table": item["name"],
                    "unit": "records",
                    "measure": measure["name"],
                })

        if categories:
            category = sorted(
                categories,
                key=lambda column: (
                    column["unique"] <= 100,
                    bool(re.search(
                        r"diagnos|drg|department|clinic|city|region|state|type|status|payer",
                        column["name"],
                        re.I,
                    )),
                    column["unique"] <= 15,
                ),
                reverse=True,
            )[0]

            counts = (
                dataframe[category["name"]]
                .astype("string")
                .fillna("Unknown")
                .value_counts()
                .head(12)
            )

            if len(counts) > 1:
                charts.append({
                    "kind": "category_count",
                    "display_type": "bar",
                    "title": f"Records by {category['name']}",
                    "labels": [str(value) for value in counts.index],
                    "values": [int(value) for value in counts.values],
                    "table": item["name"],
                    "unit": "records",
                    "dimension": category["name"],
                })

                share_counts = counts.head(7)
                remainder = int(len(dataframe) - share_counts.sum())
                share_labels = [str(value) for value in share_counts.index]
                share_values = [int(value) for value in share_counts.values]

                if remainder:
                    share_labels.append("Other categories")
                    share_values.append(remainder)

                charts.append({
                    "kind": "category_share",
                    "display_type": "donut",
                    "title": f"Top category share · {category['name']}",
                    "labels": share_labels,
                    "values": share_values,
                    "total": int(len(dataframe)),
                    "table": item["name"],
                    "unit": "records",
                    "dimension": category["name"],
                })

                charts.append({
                    "kind": "category_table",
                    "display_type": "table",
                    "title": f"Category detail · {category['name']}",
                    "labels": [str(value) for value in counts.index],
                    "values": [int(value) for value in counts.values],
                    "table": item["name"],
                    "unit": "records",
                    "dimension": category["name"],
                })

    if "quality" in goal_lower or "data quality" in goal_lower:
        missing = [
            (column["name"], column["missing"])
            for item in prof
            for column in item["columns"]
            if column["missing"]
        ]
        if missing:
            charts.insert(0, {
                "kind": "quality",
                "title": "Missing cells by field",
                "labels": [item[0] for item in missing[:15]],
                "values": [int(item[1]) for item in missing[:15]],
                "table": "All tables",
                "unit": "missing cells",
            })

    unique_charts = []
    seen = set()
    for chart in charts:
        signature = (chart["title"], chart["table"])
        if signature not in seen:
            unique_charts.append(chart)
            seen.add(signature)
    charts = unique_charts[:6]

    has_finance = any(
        re.search(
            r"charge|payment|amount|claim|payer|insurance|bill|cost",
            column["name"],
            re.I,
        )
        for item in prof
        for column in item["columns"]
    )
    has_flow = any(
        re.search(
            r"encounter|visit|admission|discharge|appointment|wait",
            column["name"],
            re.I,
        )
        for item in prof
        for column in item["columns"]
    )
    has_quality = any(
        re.search(
            r"diagnos|icd|outcome|readmission|complication",
            column["name"],
            re.I,
        )
        for item in prof
        for column in item["columns"]
    )
    has_staff = any(
        re.search(
            r"staff|doctor|nurse|employee|clinician",
            column["name"],
            re.I,
        )
        for item in prof
        for column in item["columns"]
    )

    recommendations = []

    def display_number(value):
        return format_metric(value, 2)

    def comparison_evidence(chart):
        if not chart or len(chart.get("values", [])) < 2:
            return None

        values = chart["values"]
        counts = chart.get("group_counts", [])
        top = 0
        lowest = len(values) - 1
        difference = float(values[top]) - float(values[lowest])

        if counts and len(counts) == len(values):
            return (
                f"In the {len(values)} displayed {chart['dimension']} groups, "
                f"{chart['labels'][top]} has the highest mean "
                f"{chart['measure']} ({display_number(values[top])}; "
                f"n={counts[top]:,} rows) and "
                f"{chart['labels'][lowest]} the lowest "
                f"({display_number(values[lowest])}; "
                f"n={counts[lowest]:,} rows). Difference: "
                f"{display_number(difference)} {chart['measure']} units."
            )

        return (
            f"In the {len(values)} displayed {chart['dimension']} groups, "
            f"{chart['labels'][top]} has the highest mean "
            f"{chart['measure']} ({display_number(values[top])}) and "
            f"{chart['labels'][lowest]} the lowest "
            f"({display_number(values[lowest])}). Difference: "
            f"{display_number(difference)} {chart['measure']} units."
        )

    if any(word in goal_lower for word in ("revenue", "billing", "insurance")):
        comparison = next(
            (
                chart for chart in charts
                if chart["kind"] == "comparison"
                and re.search(
                    r"charge|payment|amount|cost|fee|revenue",
                    chart.get("measure", ""),
                    re.I,
                )
            ),
            None,
        )
        evidence = comparison_evidence(comparison)

        recommendation = (
            "Measured comparison: " + evidence
            + " Review case mix, service definitions, and units before "
              "interpreting or changing charges; a mean charge is not collected revenue."
            if evidence
            else
            "No supported financial measure/category comparison was calculated "
            "from the loaded schema; do not infer revenue-cycle performance."
        )

        if not any(
            re.search(r"denial|claim.?status|payment.?date", column["name"], re.I)
            for item in prof
            for column in item["columns"]
        ):
            recommendation += (
                " No denial-status or payment-date field was detected, "
                "so denial rates and payment lag cannot be calculated."
            )

        recommendations.append({
            "category": "REVENUE & BILLING",
            "text": recommendation,
        })

    elif any(word in goal_lower for word in ("quality", "outcome")):
        recommendations.append({
            "category": "CARE QUALITY",
            "text": (
                "Stratify documented outcomes by diagnosis and care setting, "
                "then validate coding and cohort definitions with clinical informatics."
                if has_quality
                else
                "No recognizable diagnosis or outcome fields were found. "
                "Clinical quality comparisons are not supported by the loaded schema."
            ),
        })

    elif "staff" in goal_lower or "capacity" in goal_lower:
        recommendations.append({
            "category": "STAFFING & CAPACITY",
            "text": (
                "Compare staffing coverage with time- and service-level demand, "
                "accounting for role and shift definitions."
                if has_staff and has_flow
                else
                "This schema lacks staff rosters, roles, shifts, or "
                "encounter-time demand fields needed to assess staffing against demand."
            ),
        })

    elif "data quality" in goal_lower:
        recommendations.append({
            "category": "DATA QUALITY",
            "text": (
                f"Review {len(issues)} findings by severity. Resolve documented "
                "source-rule violations first; retain plausible high-value "
                "observations and all original values."
            ),
        })

    else:
        count_chart = next(
            (
                chart for chart in charts
                if chart["kind"] == "category_count"
            ),
            None,
        )
        comparison = next(
            (
                chart for chart in charts
                if chart["kind"] == "comparison"
            ),
            None,
        )

        if count_chart:
            leader = count_chart["values"][0]
            table_row_count = next(
                (
                    item["rows"] for item in prof
                    if item["name"] == count_chart["table"]
                ),
                rows,
            )
            share = 100 * leader / max(table_row_count, 1)
            recommendation = (
                f"Measured workload: {count_chart['labels'][0]} is the most "
                f"frequent {count_chart['dimension']} category with "
                f"{leader:,} of {table_row_count:,} records in "
                f"{count_chart['table']} ({share:.2f}%). Review this category's "
                "service mix and capacity using local operational context."
            )
        elif comparison_evidence(comparison):
            recommendation = (
                "Measured comparison: "
                + comparison_evidence(comparison)
                + " This is a descriptive group comparison; it does not "
                  "establish staffing need or a cause."
            )
        elif has_flow:
            recommendation = (
                "Encounter/flow fields are present, but no compatible category "
                "comparison was calculated. Map a valid date or group field "
                "before drawing a workload conclusion."
            )
        else:
            recommendation = (
                "No encounter, visit, admission, discharge, or wait-time field "
                "was detected, so patient-flow performance cannot be calculated "
                "from this dataset."
            )

        if not any(
            column["domain"] == "Time"
            for item in prof
            for column in item["columns"]
        ):
            recommendation += (
                " No usable time field was detected; arrival peaks, waiting-time "
                "trends, and monthly change cannot be measured."
            )

        recommendations.append({
            "category": "OPERATIONS & PATIENT FLOW",
            "text": recommendation,
        })

    if has_finance and not any(
        "REVENUE" in item["category"] for item in recommendations
    ):
        comparison = next(
            (
                chart for chart in charts
                if chart["kind"] == "comparison"
                and re.search(
                    r"charge|payment|amount|cost|fee|revenue",
                    chart.get("measure", ""),
                    re.I,
                )
            ),
            None,
        )
        evidence = comparison_evidence(comparison)
        recommendations.append({
            "category": "FINANCIAL VARIATION",
            "text": (
                "Measured comparison: " + evidence
                + " Validate case mix and units before interpretation; "
                  "this is not collected revenue."
                if evidence
                else
                "Financial fields are present, but no supported numeric "
                "comparison was calculated; revenue performance cannot be concluded."
            ),
        })

    if not charts:
        recommendations.append({
            "category": "NEXT STEP",
            "text": (
                "No chartable measure/dimension pair was found. Map a date, "
                "category, or numeric measure in the schema before drawing conclusions."
            ),
        })

    relations = relation_candidates(tables)
    kpis = []

    entity_rules = [
        ("Patient records", r"patient|mrn", r"patient.?id|mrn|medical.?record"),
        ("Encounter records", r"encounter|visits?|admissions?", r"encounter.?id|visit.?id|admission.?id"),
        ("Diagnosis records", r"diagnos", r"diagnos(?:is)?_?(?:id|code)|icd.?code"),
        ("Claims & insurance", r"claims?", r"claim.?id|claim.?status|payer|insurance"),
        ("Medication records", r"medication|prescription|drug.?orders?", r"medication.?id|drug.?id|prescription.?id"),
        ("Staff records", r"staff|workforce", r"staff.?id|employee.?id|clinician.?id|nurse.?id|doctor.?id"),
    ]

    for label, table_pattern, field_pattern in entity_rules:
        matched = [
            item for item in prof
            if re.search(table_pattern, item["name"], re.I)
            or any(
                re.search(field_pattern, column["name"], re.I)
                for column in item["columns"]
            )
        ]
        if matched:
            if label == "Patient records":
                keys = [
                    column["name"]
                    for item in matched
                    for column in item["columns"]
                    if re.search(
                        r"patient.?id|mrn|medical.?record",
                        column["name"],
                        re.I,
                    )
                ]
                value = (
                    sum(
                        int(tables[item["name"]][column].nunique(dropna=True))
                        for item in matched
                        for column in keys
                        if column in tables[item["name"]]
                    )
                    if keys
                    else sum(item["rows"] for item in matched)
                )
                basis = (
                    "distinct patient identifiers"
                    if keys else "rows in matched tables"
                )
            else:
                value = sum(item["rows"] for item in matched)
                basis = "rows in matched tables"

            kpis.append({
                "label": label,
                "value": value,
                "tables": len(matched),
                "basis": basis,
            })

    completeness = round(
        100 * (1 - missing_total / max(cells_total, 1)),
        6,
    )
    focus = (
        "Revenue cycle"
        if any(word in goal_lower for word in ("revenue", "billing", "insurance"))
        else "Care quality"
        if "quality" in goal_lower or "outcome" in goal_lower
        else "Staffing and capacity"
        if "staff" in goal_lower or "capacity" in goal_lower
        else "Data quality"
        if "data quality" in goal_lower
        else "Operations and patient flow"
    )

    metric_patterns = {
        "Revenue cycle": r"charge|payment|amount|cost|fee|revenue|claim",
        "Operations and patient flow": r"discharge|encounter|visit|admission|wait|length|volume",
        "Care quality": r"outcome|readmission|mortality|complication|diagnos|icd",
        "Staffing and capacity": r"staff|doctor|nurse|employee|clinician",
        "Data quality": r"missing|duplicate|quality|complete",
    }

    numeric_fields = []
    for item in prof:
        for column in item["columns"]:
            if (
                column["type"] == "numeric"
                and column["unique"] > 1
                and not NON_MEASURE_RE.search(column["name"])
            ):
                numeric_fields.append((
                    bool(re.search(
                        metric_patterns[focus],
                        column["name"],
                        re.I,
                    )),
                    item,
                    column,
                ))

    numeric_fields.sort(key=lambda item: item[0], reverse=True)
    goal_kpis = []

    for _, item, column in numeric_fields[:5]:
        values = numeric_series(
            tables[item["name"]][column["name"]]
        ).dropna()
        if len(values) > 1:
            for label, value in (
                ("Mean", values.mean()),
                ("Median", values.median()),
                ("90th percentile", values.quantile(0.9)),
            ):
                goal_kpis.append({
                    "label": f"{label} {column['name']}",
                    "value": float(value),
                    "unit": column["name"],
                    "table": item["name"],
                    "n": len(values),
                    "statistic": label.lower(),
                })

    workflow = [
        {
            "label": "Profile complete",
            "detail": (
                f"{len(prof)} tables · {rows:,} rows · {fields} fields"
            ),
        },
        {
            "label": "Goal selected",
            "detail": (
                f"{focus}: analysis filters and recommendations use the selected question"
            ),
        },
        {
            "label": "Quality review",
            "detail": (
                f"{len(issues)} findings; see recommended actions before cleaning"
            ),
        },
        {
            "label": "Explore & document",
            "detail": (
                f"{len(charts)} data-backed visualizations ready; "
                "report includes this analysis goal"
            ),
        },
    ]

    visual_fields = []
    for item in prof:
        visual_fields.append({
            "table": item["name"],
            "dimensions": [
                column["name"] for column in item["columns"]
                if column["type"] == "text"
                and not column["masked"]
                and 2 <= column["unique"] <= 10000
            ],
            "measures": [
                column["name"] for column in item["columns"]
                if column["type"] == "numeric"
                and column["unique"] > 1
                and not NON_MEASURE_RE.search(column["name"])
            ],
            "dates": [
                column["name"] for column in item["columns"]
                if column["type"] == "date"
            ],
        })

    return {
        "goal": goal,
        "focus": focus,
        "workflow": workflow,
        "tables": prof,
        "relationships": relations,
        "issues": issues[:120],
        "cleaning_suggestions": clean[:120],
        "recommendations": recommendations,
        "charts": charts,
        "chart": charts[0] if charts else None,
        "domains": dict(domains),
        "kpis": kpis,
        "goal_kpis": goal_kpis,
        "visual_fields": visual_fields,
        "table_count": len(prof),
        "row_count": rows,
        "field_count": fields,
        "missing_cells": missing_total,
        "completeness_pct": completeness,
        "duplicate_rows": duplicate_total,
        "refreshed_at": SESSION.get("refreshed_at"),
        "clean_active": bool(SESSION.get("clean_active")),
    }


def build_visualization(request):
    """Aggregate a user-selected visual from loaded data; never return source rows."""
    table = str(request.get("table", ""))
    dimension = str(request.get("dimension", ""))
    measure = str(request.get("measure", ""))
    series_column = str(request.get("series", ""))
    visual_type = str(request.get("visual_type", "bar")).lower()
    aggregation = str(request.get("aggregation", "mean")).lower()

    if visual_type not in {
        "bar", "column", "line", "area", "clustered",
        "pie", "donut", "treemap", "table", "card",
    }:
        raise ValueError("Choose a supported visualization type.")

    if aggregation not in {"count", "sum", "mean", "median", "min", "max"}:
        raise ValueError("Choose a supported aggregation.")

    if (
        visual_type in {"pie", "donut", "treemap"}
        and measure
        and aggregation not in {"sum", "count"}
    ):
        aggregation = "sum"

    if table not in SESSION["tables"]:
        raise ValueError("Choose an available table.")

    dataframe = SESSION["tables"][table]
    columns = {str(column): column for column in dataframe.columns}

    for selected in (dimension, measure, series_column):
        if selected and selected not in columns:
            raise ValueError(
                "A selected field is no longer available. "
                "Refresh the dataset and choose again."
            )

    if dimension and ID_RE.search(dimension):
        raise ValueError(
            "Visualizations cannot group by likely patient identifiers."
        )

    title_measure = measure or "records"
    visual_total = None

    date_fields = {
        column["name"]
        for item in profile()
        if item["name"] == table
        for column in item["columns"]
        if column["type"] == "date"
    }

    if dimension and dimension in columns and dimension in date_fields:
        dates = pd.to_datetime(
            dataframe[columns[dimension]],
            errors="coerce",
        )
        work = pd.DataFrame({
            "period": dates.dt.to_period("M").astype("string")
        })

        if measure:
            work["value"] = numeric_series(
                dataframe[columns[measure]]
            )
            work = work.dropna(subset=["period", "value"])
        else:
            work = work.dropna(subset=["period"])
            work["value"] = 1

        grouped_all = work.groupby("period")["value"].agg(
            "count" if aggregation == "count" else aggregation
        ).sort_index()

        visual_total = (
            float(grouped_all.sum())
            if aggregation in {"sum", "count"}
            else None
        )
        grouped = grouped_all.tail(36)

        if visual_type in {"pie", "donut", "treemap"} and len(grouped_all) > 7:
            grouped = grouped_all.tail(7)

        labels = [str(value) for value in grouped.index]
        values = [safe(value) for value in grouped.values]
        datasets = None

    elif dimension:
        categories = (
            dataframe[columns[dimension]]
            .astype("string")
            .fillna("Unknown")
        )

        if measure:
            numeric_values = numeric_series(
                dataframe[columns[measure]]
            )
            work = pd.DataFrame({
                "category": categories,
                "value": numeric_values,
            }).dropna(subset=["value"])

            function = "count" if aggregation == "count" else aggregation

            if (
                series_column
                and series_column != dimension
                and visual_type not in {"pie", "donut", "treemap"}
            ):
                work["series"] = (
                    dataframe.loc[work.index, columns[series_column]]
                    .astype("string")
                    .fillna("Unknown")
                )

                grouped = work.groupby(
                    ["category", "series"]
                )["value"].agg(function).unstack(fill_value=0)

                order = (
                    grouped.sum(axis=1)
                    .sort_values(ascending=False)
                    .head(12)
                    .index
                )
                grouped = grouped.loc[order]

                series_order = (
                    grouped.sum(axis=0)
                    .sort_values(ascending=False)
                    .head(6)
                    .index
                )
                grouped = grouped[series_order]
                labels = [str(value) for value in grouped.index]
                datasets = [
                    {
                        "name": str(name),
                        "values": [safe(value) for value in grouped[name].values],
                    }
                    for name in grouped.columns
                ]
                values = [
                    safe(value)
                    for value in grouped.sum(axis=1).values
                ]
            else:
                grouped_all = (
                    work.groupby("category")["value"]
                    .agg(function)
                    .sort_values(ascending=False)
                )
                visual_total = (
                    float(grouped_all.sum())
                    if aggregation in {"sum", "count"}
                    else None
                )
                grouped = grouped_all.head(
                    7 if visual_type in {"pie", "donut", "treemap"} else 12
                )

                if (
                    visual_type in {"pie", "donut", "treemap"}
                    and len(grouped_all) > len(grouped)
                ):
                    grouped = pd.concat([
                        grouped,
                        pd.Series({
                            "Other categories":
                                grouped_all.iloc[len(grouped):].sum()
                        }),
                    ])

                labels = [str(value) for value in grouped.index]
                values = [safe(value) for value in grouped.values]
                datasets = None
        else:
            grouped_all = categories.value_counts()
            visual_total = int(len(categories))
            grouped = grouped_all.head(
                7 if visual_type in {"pie", "donut", "treemap"} else 12
            )

            if (
                visual_type in {"pie", "donut", "treemap"}
                and len(grouped_all) > len(grouped)
            ):
                grouped = pd.concat([
                    grouped,
                    pd.Series({
                        "Other categories":
                            grouped_all.iloc[len(grouped):].sum()
                    }),
                ])

            labels = [str(value) for value in grouped.index]
            values = [int(value) for value in grouped.values]
            datasets = None
            aggregation = "count"

    elif measure:
        numeric_values = numeric_series(
            dataframe[columns[measure]]
        ).dropna()

        if not len(numeric_values):
            raise ValueError(
                "The selected measure has no parseable numeric values."
            )

        if visual_type == "card":
            value = (
                numeric_values.count()
                if aggregation == "count"
                else getattr(numeric_values, aggregation)()
            )
            return {
                "visualization": {
                    "kind": "card",
                    "display_type": "card",
                    "title": f"{aggregation.title()} {measure}",
                    "table": table,
                    "unit": measure,
                    "measure": measure,
                    "value": float(value),
                    "count": len(numeric_values),
                }
            }

        histogram = pd.cut(
            numeric_values,
            bins=min(12, max(3, int(numeric_values.nunique() ** 0.5))),
            duplicates="drop",
        ).value_counts(sort=False)

        labels = [str(value) for value in histogram.index]
        values = [int(value) for value in histogram.values]
        datasets = None
        dimension = "Value range"
        aggregation = "count"
        visual_total = int(len(numeric_values))

    else:
        raise ValueError(
            "Select a category or date field, or select a numeric measure "
            "for its distribution."
        )

    if not labels:
        raise ValueError("No aggregate values were available for this selection.")

    if visual_type == "card":
        card_values = (
            numeric_series(dataframe[columns[measure]]).dropna()
            if measure
            else pd.Series([len(dataframe)])
        )
        value = (
            card_values.count()
            if aggregation == "count"
            else getattr(card_values, aggregation)()
        )
        return {
            "visualization": {
                "kind": "card",
                "display_type": "card",
                "title": f"{aggregation.title()} {title_measure}",
                "table": table,
                "unit": title_measure,
                "measure": measure,
                "value": float(value),
                "count": len(card_values),
            }
        }

    total = (
        visual_total
        if visual_total is not None
        else float(sum(float(value or 0) for value in values))
    )

    return {
        "visualization": {
            "kind": "aggregate",
            "display_type": visual_type,
            "title": (
                f"{aggregation.title()} {title_measure} by "
                f"{dimension or 'value range'}"
            ),
            "table": table,
            "unit": title_measure if measure else "records",
            "measure": measure,
            "dimension": dimension,
            "aggregation": aggregation,
            "labels": labels,
            "values": values,
            "series": datasets,
            "total": round(total, 2),
        }
    }


_CHAT_HISTORY = {
    "generation": None,
    "questions": [],
    "last_outlier_result": None,
    "last_analytical_result": None,
}


def _format_outlier_followup_number(value):
    if value is None:
        return "not available"

    number = float(value)
    if number.is_integer():
        return f"{int(number):,}"
    return f"{number:,.6f}".rstrip("0").rstrip(".")


def _answer_outlier_followup(question, previous_result):
    """Answer outlier follow-ups from previously verified local results."""
    if not isinstance(previous_result, dict):
        return None

    previous_plan = previous_result.get("query_plan") or {}
    results = previous_plan.get("outlier_results") or []

    if previous_plan.get("operation") != "outliers" or not results:
        return None

    normalized = re.sub(r"[^a-z0-9\s]", " ", str(question).lower())
    normalized = " ".join(normalized.split())

    # Only intercept questions that clearly request a detail of the
    # immediately preceding outlier analysis.
    asks_method = bool(
        re.search(r"\b(method|algorithm|technique|rule)\b", normalized)
    )
    asks_upper = "upper bound" in normalized
    asks_lower = "lower bound" in normalized
    asks_q1 = bool(re.search(r"\bq1\b|first quartile", normalized))
    asks_q3 = bool(re.search(r"\bq3\b|third quartile", normalized))
    asks_iqr = bool(re.search(r"\biqr\b|interquartile range", normalized))
    asks_count = bool(
        re.search(
            r"how many|number of|count of|outliers? detected",
            normalized,
        )
    )
    asks_values = (
        "which value" in normalized
        or "what value" in normalized
        or "which one" in normalized
        or "what is it" in normalized
        or "outlier value" in normalized
        or "detected value" in normalized
        or ("outlier" in normalized and "value" in normalized)
    )

    if not any((
        asks_method, asks_upper, asks_lower, asks_q1,
        asks_q3, asks_iqr, asks_count, asks_values,
    )):
        return None

    # Select a field explicitly named in the follow-up, or use the sole
    # field from the previous result.
    selected = []
    for item in results:
        field = str(item.get("field", ""))
        normalized_field = re.sub(r"[^a-z0-9]", "", field.lower())
        question_field = re.sub(r"[^a-z0-9]", "", normalized)
        if normalized_field and normalized_field in question_field:
            selected.append(item)

    if len(selected) == 1:
        item = selected[0]
    elif len(results) == 1:
        item = results[0]
    else:
        return None

    field = item.get("field", "the selected field")
    count = item.get("outlier_candidates")
    values = item.get("outlier_values") or []

    if asks_method:
        method = str(item.get("method") or "Tukey 1.5 * IQR")
        method = method.replace("?", "*").replace("?", "*")
        answer = f"The outlier detection method used for {field} was {method} rule."
    elif asks_upper:
        answer = (
            f"The upper IQR outlier bound for {field} is "
            f"{_format_outlier_followup_number(item.get('upper_bound'))}."
        )
    elif asks_lower:
        answer = (
            f"The lower IQR outlier bound for {field} is "
            f"{_format_outlier_followup_number(item.get('lower_bound'))}."
        )
    elif asks_q1:
        answer = (
            f"Q1 (the first quartile) for {field} is "
            f"{_format_outlier_followup_number(item.get('q1'))}."
        )
    elif asks_q3:
        answer = (
            f"Q3 (the third quartile) for {field} is "
            f"{_format_outlier_followup_number(item.get('q3'))}."
        )
    elif asks_iqr:
        answer = (
            f"The interquartile range (IQR) for {field} is "
            f"{_format_outlier_followup_number(item.get('iqr'))}."
        )
    elif asks_count:
        if count is None:
            answer = f"Outlier screening for {field} was not performed."
        else:
            answer = (
                f"{count} IQR outlier candidate(s) were detected in {field} "
                f"among {item.get('n', 0)} numeric observations."
            )
    elif asks_values:
        if count is None:
            answer = f"Outlier screening for {field} was not performed."
        elif values:
            formatted = ", ".join(
                _format_outlier_followup_number(value)
                for value in values
            )
            answer = f"The detected outlier candidate(s) in {field} are: {formatted}."
        else:
            answer = f"No IQR outlier candidates were detected in {field}."
    else:
        return None

    return {
        "answer": answer,
        "sources": previous_result.get("sources", []),
        "executed": True,
        "query_plan": {
            "operation": "outliers_followup",
            "table": previous_plan.get("table"),
            "outlier_results": results,
        },
    }




def _answer_methodology_followup(question, previous_result, tables):
    """Answer grouped-analysis follow-ups by intent using verified local data."""
    if not isinstance(previous_result, dict) or not isinstance(tables, dict):
        return None

    plan = previous_result.get("query_plan") or {}
    if plan.get("operation") not in {"rank", "group_aggregate"}:
        return None

    results = plan.get("results") or []
    group_by = plan.get("group_by") or (
        [plan.get("dimension")] if plan.get("dimension") else []
    )
    table_name = plan.get("table")
    measure = plan.get("measure")
    if not results or not group_by or not table_name or table_name not in tables:
        return None

    dataframe = tables.get(table_name)
    if not isinstance(dataframe, pd.DataFrame):
        return None

    normalized = re.sub(r"[^a-z0-9\s]", " ", str(question).lower())
    normalized = " ".join(normalized.split())

    asks_n = bool(re.search(
        r"\b(?:n|sample size|observation count|record count|number of records|"
        r"what does n represent|n represent|contributing records?)\b",
        normalized,
    ))
    asks_unique = bool(re.search(
        r"\b(?:unique|distinct)\b.*\b(?:hospitals?|providers?|facilities?)\b|"
        r"\b(?:hospitals?|providers?|facilities?)\b.*\b(?:unique|distinct|count|many)\b|"
        r"records?\s+(?:versus|vs|and)\s+(?:unique|distinct)",
        normalized,
    ))
    asks_drg = bool(re.search(
        r"\b(?:drg|case mix|case composition|category mix|composition)\b",
        normalized,
    ))
    asks_limits = bool(re.search(
        r"\b(?:limitation|limitations|caveat|caveats|interpret|pricing|"
        r"like for like|risk adjust|adjusted comparison|what might this indicate)\b",
        normalized,
    ))

    # Do not replay a broad methodology report for a narrow, explicit follow-up.
    # A unique-provider-only request receives only provider counts; a DRG-only
    # request receives the category-mix comparison and case-mix explanation.
    if not any((asks_n, asks_unique, asks_drg, asks_limits)):
        return None

    city_field = next((
        str(name) for name in group_by
        if re.search(r"city|town|location|municipality", str(name), re.I)
    ), None)
    if not city_field or city_field not in dataframe.columns:
        return None

    provider_field = next((
        str(column) for column in dataframe.columns
        if re.search(
            r"(?:provider|facility|hospital)\s*(?:id|identifier|number|code)|\bnpi\b",
            str(column), re.I,
        )
    ), None)
    drg_field = next((
        str(column) for column in dataframe.columns
        if re.search(r"\bdrg\b", str(column), re.I)
        and not re.search(r"(?:id|identifier|code)$", str(column), re.I)
    ), None)

    summary_rows = []
    drg_sets = {}
    source_fields = [f"{table_name}.{city_field}"]
    if measure and measure in dataframe.columns:
        source_fields.append(f"{table_name}.{measure}")
    if provider_field:
        source_fields.append(f"{table_name}.{provider_field}")
    if drg_field:
        source_fields.append(f"{table_name}.{drg_field}")

    for item in results[:10]:
        raw_city = item.get("group", "")
        city = " | ".join(str(part) for part in raw_city) if isinstance(raw_city, tuple) else str(raw_city)
        city = city.strip()
        if not city:
            continue

        city_values = dataframe[city_field].astype("string").str.strip()
        mask = city_values.str.casefold().eq(city.casefold()).fillna(False)
        subset = dataframe.loc[mask]

        # Match the rows that contributed to the previous grouped measure.
        contributing = subset
        if measure and measure in subset.columns:
            contributing = subset.loc[numeric_series(subset[measure]).notna()]

        row = {
            "city": city,
            "record_count": int(item.get("n") or len(contributing)),
            "unique_provider_ids": None,
            "drg_categories": None,
            "drg_rows": None,
            "largest_drg_share_pct": None,
            "top_drg_categories": [],
        }

        if provider_field:
            providers = (
                contributing[provider_field]
                .astype("string")
                .str.strip()
                .replace("", pd.NA)
                .dropna()
            )
            row["unique_provider_ids"] = int(providers.nunique(dropna=True))

        if drg_field:
            drgs = (
                contributing[drg_field]
                .astype("string")
                .str.strip()
                .replace("", pd.NA)
                .dropna()
            )
            counts = drgs.value_counts(dropna=True)
            row["drg_categories"] = int(drgs.nunique(dropna=True))
            row["drg_rows"] = int(len(drgs))
            if len(drgs):
                row["largest_drg_share_pct"] = round(
                    100.0 * float(counts.iloc[0]) / len(drgs), 2
                )
                row["top_drg_categories"] = (
                    [
                        {
                            "category": str(category),
                            "records": int(count),
                            "share_pct": round(100.0 * int(count) / len(drgs), 2),
                        }
                        for category, count in counts.head(3).items()
                    ]
                    if int(counts.iloc[0]) > 1 else []
                )
                row["all_drg_categories_once"] = int(counts.iloc[0]) == 1
                drg_sets[city] = set(drgs.astype(str).tolist())

        summary_rows.append(row)

    if not summary_rows:
        return None

    paragraphs = []

    if asks_n:
        paragraphs.append(
            "WHAT n MEANS: n is the number of dataset rows with a valid numeric "
            f"value for {measure or 'the selected measure'} that contributed to "
            "each city's average. It is a count of contributing records, not a "
            "count of unique hospitals or patients. In this dataset, each counted "
            "row is an observation carrying the city, provider and DRG fields; "
            "the source documentation should be checked before treating rows as "
            "discharges or individual patients."
        )

    if asks_unique:
        if provider_field:
            pieces = []
            for row in summary_rows:
                count = row["unique_provider_ids"]
                if count is not None:
                    pieces.append(
                        f"{row['city']}: {count:,} distinct provider IDs "
                        f"across {row['record_count']:,} contributing records"
                    )
            answer = (
                "UNIQUE PROVIDERS IN THE PREVIOUS CITY COMPARISON: "
                + "; ".join(pieces)
                + ". Provider ID values are intentionally not displayed. "
                "These are distinct identifiers in the contributing rows; they "
                "represent unique physical hospitals only if the source data "
                "dictionary confirms a one-to-one mapping."
            )
        else:
            answer = (
                "I could not find a provider, facility, hospital ID or NPI field "
                "in the loaded table, so unique-provider counts cannot be verified."
            )
        paragraphs.append(answer)

    if asks_drg:
        if drg_field:
            mix_parts = []
            for row in summary_rows:
                if row["drg_categories"] is None:
                    continue
                item = (
                    f"{row['city']}: {row['drg_categories']:,} distinct DRG "
                    f"categories across {row['drg_rows']:,} non-missing DRG records"
                )
                if row.get("all_drg_categories_once"):
                    item += "; every DRG category occurs once in these records"
                elif row["top_drg_categories"]:
                    top = ", ".join(
                        f"{entry['category']} ({entry['records']:,}; "
                        f"{entry['share_pct']:.2f}%)"
                        for entry in row["top_drg_categories"]
                    )
                    item += f"; most frequent categories: {top}"
                mix_parts.append(item)

            sets = list(drg_sets.values())
            common_count = len(set.intersection(*sets)) if sets else 0
            union_count = len(set.union(*sets)) if sets else 0
            overlap = (
                f"Across the five city groups, {common_count:,} DRG categories "
                f"appear in every city; {union_count:,} distinct categories appear "
                "in at least one city."
            ) if len(sets) > 1 else "A cross-city DRG overlap could not be calculated."

            paragraphs.append(
                "DRG CATEGORY MIX: " + "; ".join(mix_parts) + ". " + overlap
                + " These are record-frequency summaries of the DRG Definition "
                "field, not patient counts and not a risk-adjusted comparison."
            )
        else:
            paragraphs.append(
                "DRG MIX: No DRG Definition field was detected in the loaded "
                "table, so the current dataset cannot quantify case-mix differences."
            )

        paragraphs.append(
            "HOW CASE MIX CAN AFFECT COVERED CHARGES: DRGs represent different "
            "diagnosis/procedure groupings and resource needs. A city with a larger "
            "share of higher-resource or more complex DRGs may show a higher overall "
            "mean even when covered charges for the same DRG are similar. The "
            "unadjusted city average therefore mixes service composition with "
            "provider-level charge differences. A like-for-like comparison should "
            "compare covered charges within the same DRG across cities/providers."
        )

    if asks_limits:
        paragraphs.append(
            "LIMITATIONS: The city comparison is descriptive and unadjusted. "
            "The contributing n values are records, not hospitals or patients; "
            "small groups such as Vacaville (n=3) are especially sensitive to "
            "individual rows. Provider IDs are identifiers, not verified hospital "
            "names. DRG frequency alone does not measure severity or resource use, "
            "and these data do not establish causation or prove that one city is "
            "more expensive for the same service. Average Covered Charges is also "
            "different from Average Total Payments and Average Medicare Payments."
        )

    # Keep the provider-only and DRG-only answers concise and intent-specific.
    if asks_unique and not any((asks_n, asks_drg, asks_limits)):
        paragraphs = [paragraphs[-1]]
    elif asks_drg and not any((asks_n, asks_unique, asks_limits)):
        paragraphs = paragraphs[-2:]

    return {
        "answer": "\n\n".join(paragraphs),
        "sources": list(dict.fromkeys(source_fields)),
        "executed": True,
        "query_plan": {
            "operation": "methodology_followup",
            "table": table_name,
            "based_on_operation": plan.get("operation"),
            "group_by": group_by,
            "measure": measure,
            "city_summaries": summary_rows,
            "provider_identifier_counted_not_values": bool(provider_field),
            "drg_mix_summarized": bool(drg_field),
            "followup_intents": {
                "n": asks_n,
                "unique_providers": asks_unique,
                "drg_mix": asks_drg,
                "limitations": asks_limits,
            },
        },
    }

def chat_answer(question, analysis, conversation_history=None):
    """Use LLM query planning with validated local execution, then Engine v2 fallback."""

    question_text = str(question or "").strip()

    # The LLM sees schema metadata and a redacted question, never raw DataFrame rows.
    # It returns a structured plan; llm_analyst validates and executes that plan locally.
    llm_result = answer_with_llm(
        question_text,
        SESSION.get("tables") or {},
        analysis,
        conversation_history=conversation_history,
    )

    if llm_result is not None:
        return llm_result

    # Keep the deterministic engine available when the API is not configured.
    engine_result = answer_engine_v2(
        question_text,
        SESSION.get("tables") or {},
        analysis,
    )

    if engine_result is not None:
        return engine_result

    # Preserve the existing rule-based fallback for other question types.
    question_lower = question.lower()
    sources = []
    tables = SESSION["tables"]

    def fmt(value):
        return format_metric(value, 2)

    def find_field(pattern):
        return next(
            (
                (table, column)
                for table in analysis["tables"]
                for column in table["columns"]
                if re.search(pattern, column["name"], re.I)
            ),
            None,
        )

    def find_mentioned_field():
        normalized_question = re.sub(r"[^a-z0-9]", "", question_lower)
        candidates = []

        for table in analysis["tables"]:
            for column in table["columns"]:
                normalized_column = re.sub(
                    r"[^a-z0-9]", "", column["name"].lower()
                )
                if len(normalized_column) > 2 and normalized_column in normalized_question:
                    candidates.append((len(normalized_column), table, column))

        if not candidates:
            return None

        _, table, column = max(candidates, key=lambda item: item[0])
        return table, column

    def find_semantic_field(pattern):
        return next(
            (
                (table, column)
                for table in analysis["tables"]
                for column in table["columns"]
                if re.search(pattern, column["name"], re.I)
                and not column["masked"]
                and not NON_MEASURE_RE.search(column["name"])
            ),
            None,
        )

    if re.search(
        r"which .*?(city|town|location|region|state|department|hospital|provider)"
        r".*?(highest|most|largest|maximum)|"
        r"(?:highest|most|largest|maximum).*?(?:records|rows|count).*?"
        r"(city|town|location|region|state|department|hospital|provider)",
        question_lower,
    ):
        requested = next(
            (
                term for term in (
                    "city", "town", "location", "region", "state",
                    "department", "hospital", "provider",
                )
                if re.search(r"\b" + term + r"\b", question_lower)
            ),
            None,
        )

        field = next(
            (
                (table, column)
                for table in analysis["tables"]
                for column in table["columns"]
                if requested
                and re.search(requested, column["name"], re.I)
                and column["type"] == "text"
                and not column["masked"]
                and not NON_MEASURE_RE.search(column["name"])
            ),
            None,
        )

        if field is None and requested is None:
            field = next(
                (
                    (table, column)
                    for table in analysis["tables"]
                    for column in table["columns"]
                    if re.search(
                        r"city|town|location|region|state|department|hospital|provider",
                        column["name"],
                        re.I,
                    )
                    and column["type"] == "text"
                    and not column["masked"]
                    and not NON_MEASURE_RE.search(column["name"])
                ),
                None,
            )

        if field:
            table, column = field
            counts = (
                tables[table["name"]][column["name"]]
                .astype("string")
                .fillna("Unknown")
                .value_counts(dropna=False)
            )
            top_count = counts.max()
            winners = counts[counts == top_count]
            answer = (
                f"{', '.join(str(value) for value in winners.index)} has the "
                f"most records in {table['name']}.{column['name']}: "
                f"{fmt(top_count)} of {table['rows']:,} rows "
                f"({100 * top_count / table['rows']:.4f}%). "
                "Counted one row as one record; null values are grouped as Unknown."
            )
            sources = [f"{table['name']}.{column['name']}"]
        else:
            answer = (
                "I can't answer that from the current schema: no city, location, "
                "region, state, department, hospital, or provider field was detected."
            )

    elif (
        re.search(r"\b(distinct|unique|different)\b", question_lower)
        or re.search(
            r"total recorded (?:cities|towns|locations|regions|states|"
            r"hospitals|providers|diagnoses|departments)",
            question_lower,
        )
        or re.search(
            r"how many (?:cities|towns|locations|regions|states|hospitals|"
            r"providers|diagnoses|departments)\b",
            question_lower,
        )
    ):
        matched = find_mentioned_field()

        if not matched:
            alias = next(
                (
                    pattern
                    for word, pattern in [
                        ("cities", r"city|town"),
                        ("city", r"city|town"),
                        ("towns", r"town|city"),
                        ("locations", r"location|city|town"),
                        ("regions", r"region|state"),
                        ("states", r"state|region"),
                        ("hospitals", r"hospital|facility"),
                        ("providers", r"provider"),
                        ("diagnoses", r"diagnos|condition|disease"),
                        ("departments", r"department|clinic"),
                    ]
                    if re.search(r"\b" + word + r"\b", question_lower)
                ),
                None,
            )
            matched = find_semantic_field(alias) if alias else None

        if matched:
            table, column = matched
            series = tables[table["name"]][column["name"]]
            count = int(series.nunique(dropna=True))
            nonnull = int(series.notna().sum())
            missing = len(series) - nonnull

            answer = (
                f"Distinct {column['name']} values in {table['name']}: "
                f"{count:,}, counted across {nonnull:,} non-missing rows "
                f"({missing:,} missing values excluded). Operation: exact "
                f"distinct count; source: {table['name']}.{column['name']}."
            )
            sources = [f"{table['name']}.{column['name']}"]
        else:
            answer = (
                "I couldn't identify a matching category field for a distinct "
                "count. No count was estimated. Check the schema or specify the field name."
            )

    elif (
        re.search(
            r"\b(top|bottom|highest|lowest|largest|smallest)\b",
            question_lower,
        )
        and re.search(r"\b(by|per|for each|across)\b", question_lower)
    ):
        candidates = []
        normalized_question = re.sub(r"[^a-z0-9]", "", question_lower)

        for table in analysis["tables"]:
            for column in table["columns"]:
                normalized_column = re.sub(
                    r"[^a-z0-9]", "", column["name"].lower()
                )
                if len(normalized_column) > 2 and normalized_column in normalized_question:
                    candidates.append((table, column))

        measure = next(
            (
                (table, column)
                for table, column in candidates
                if column["type"] == "numeric"
                and not NON_MEASURE_RE.search(column["name"])
            ),
            None,
        )

        if not measure:
            term = next(
                (
                    word for word in (
                        "charge", "payment", "cost", "amount", "fee",
                        "revenue", "discharge", "wait", "length",
                    )
                    if re.search(r"\b" + word + r"s?\b", question_lower)
                ),
                None,
            )
            matches = [
                (table, column)
                for table in analysis["tables"]
                for column in table["columns"]
                if term
                and column["type"] == "numeric"
                and re.search(term, column["name"], re.I)
                and not NON_MEASURE_RE.search(column["name"])
            ]
            if len(matches) == 1:
                measure = matches[0]

        dimension = next(
            (
                (table, column)
                for table, column in candidates
                if column["type"] == "text"
                and not column["masked"]
                and not NON_MEASURE_RE.search(column["name"])
                and (measure is None or table["name"] == measure[0]["name"])
            ),
            None,
        )

        if not dimension:
            alias = next(
                (
                    pattern
                    for word, pattern in [
                        ("cities", r"city|town"),
                        ("city", r"city|town"),
                        ("locations", r"location|city"),
                        ("hospitals", r"hospital|facility"),
                        ("providers", r"provider"),
                        ("departments", r"department|clinic"),
                        ("diagnoses", r"diagnos|drg"),
                        ("regions", r"region|state"),
                    ]
                    if re.search(r"\b" + word + r"\b", question_lower)
                ),
                None,
            )
            dimension = find_semantic_field(alias) if alias else None

        if (
            measure
            and dimension
            and measure[0]["name"] == dimension[0]["name"]
        ):
            table, measure_column = measure
            _, dimension_column = dimension
            dataframe = tables[table["name"]]
            values = numeric_series(dataframe[measure_column["name"]])
            categories = dataframe[dimension_column["name"]].astype("string")
            work = pd.DataFrame({
                "category": categories,
                "value": values,
            }).dropna(subset=["category", "value"])

            operation = (
                "mean"
                if re.search(r"\b(mean|average|avg)\b", question_lower)
                else "sum"
            )

            if (
                operation == "sum"
                and re.search(r"\baverage\b", measure_column["name"], re.I)
            ):
                answer = (
                    f"{measure_column['name']} in {table['name']} is already "
                    "an average-valued measure. Summing those row values would "
                    "not establish total charges. Ask for average "
                    f"{measure_column['name']} by {dimension_column['name']}, "
                    "or select a source field documented as additive."
                )
                sources = [
                    f"{table['name']}.{dimension_column['name']}",
                    f"{table['name']}.{measure_column['name']}",
                ]
                return {
                    "answer": answer,
                    "sources": sources,
                    "goal": analysis["goal"],
                }

            grouped = (
                work.groupby("category")["value"]
                .agg(operation)
                .sort_values(
                    ascending=bool(
                        re.search(
                            r"\b(bottom|lowest|smallest)\b",
                            question_lower,
                        )
                    )
                )
            )
            amount_match = re.search(r"\btop\s+(\d+)\b", question_lower)
            amount = int(amount_match.group(1)) if amount_match else 10
            selected = grouped.head(min(amount, 50))
            counts = work.groupby("category")["value"].count()

            answer = (
                f"{operation.title()} {measure_column['name']} by "
                f"{dimension_column['name']} in {table['name']} "
                "(non-missing numeric values only), "
                f"{'lowest' if re.search(r'\\b(bottom|lowest|smallest)\\b', question_lower) else 'highest'} "
                f"{len(selected)}: "
                + "; ".join(
                    f"{key} = {fmt(value)} (n={int(counts[key]):,})"
                    for key, value in selected.items()
                )
                + f". Excluded {len(dataframe) - len(work):,} rows missing "
                  f"{dimension_column['name']} or an interpretable "
                  f"{measure_column['name']} value."
            )
            sources = [
                f"{table['name']}.{dimension_column['name']}",
                f"{table['name']}.{measure_column['name']}",
            ]
        else:
            answer = (
                "To rank categories by a measure, specify both a category field "
                "and a numeric measure (for example: ‘top 10 cities by total "
                "charges’). No calculation was run."
            )

    elif re.search(
        r"summar|overview|key findings|what did you find|analysis results",
        question_lower,
    ):
        findings = analysis["issues"]
        counts = Counter(item["kind"] for item in findings)
        parts = [
            (
                f"Loaded {analysis['table_count']} table(s), "
                f"{analysis['row_count']:,} rows, and "
                f"{analysis['field_count']} fields. Completeness is "
                f"{analysis['completeness_pct']:.1f}% "
                f"({analysis['missing_cells']:,} missing cells; "
                f"{analysis['duplicate_rows']:,} exact duplicate rows)."
            ),
            (
                f"Quality audit flags: {len(findings)} total — "
                + ", ".join(
                    f"{count} {kind.replace('_', ' ')}"
                    for kind, count in sorted(counts.items())
                )
                + ". Statistical outliers are candidates, not confirmed errors."
            ),
            (
                f"Analysis goal: {analysis['goal']}. Available calculated "
                "indicators: "
                + (
                    "; ".join(
                        f"{item['label']} = {fmt(item['value'])} "
                        f"({item['n']:,} non-missing values)"
                        for item in analysis["goal_kpis"][:6]
                    )
                    or "none for the selected goal"
                )
                + "."
            ),
        ]
        answer = "\n\n".join(parts)
        sources = [table["name"] for table in analysis["tables"]]

    elif re.search(r"missing|null|blank|completeness|quality|audit|clean", question_lower):
        field = find_mentioned_field()

        if field:
            table, column = field
            found = [
                item for item in analysis["issues"]
                if item["kind"] == "missing"
                and item["table"] == table["name"]
                and item.get("column") == column["name"]
            ]

            if found:
                item = found[0]
                answer = (
                    f"{table['name']}.{column['name']}: "
                    f"{column['missing']:,} missing of "
                    f"{table['rows']:,} rows "
                    f"({100 * column['missing'] / max(table['rows'], 1):.4f}%). "
                    "Resolution: determine whether this field is required for "
                    "each record type; compare against source/related fields; "
                    "correct only from an authoritative source. Keep unknowns "
                    "null and document any approved exclusion or imputation rule."
                )
            else:
                answer = (
                    f"{table['name']}.{column['name']}: 0 missing of "
                    f"{table['rows']:,} rows (100.0% populated)."
                )

            sources = [f"{table['name']}.{column['name']}"]
        else:
            found = [
                item for item in analysis["issues"]
                if item["kind"] in (
                    "missing", "duplicate", "business_rule", "outlier"
                )
            ]

            if found:
                answer = "\n\n".join(
                    f"{item['table']} — {item['title']}. "
                    f"Measured evidence: {item['detail']} "
                    f"Resolution: {item['action']}"
                    for item in found[:12]
                )
            else:
                answer = (
                    f"Quality audit: 0 missing cells, 0 exact duplicate rows, "
                    f"and 0 IQR/business-rule flags across "
                    f"{analysis['row_count']:,} rows and "
                    f"{analysis['field_count']} fields. This covers the checks "
                    "currently implemented; it is not a guarantee of source correctness."
                )

            sources = list(dict.fromkeys(item["table"] for item in found))

    elif re.search(r"outlier|anomal|unusual|extreme", question_lower):
        field = find_mentioned_field()
        found = [
            item for item in analysis["issues"]
            if item["kind"] in ("outlier", "business_rule")
            and (
                not field
                or (
                    item["table"] == field[0]["name"]
                    and item.get("column") == field[1]["name"]
                )
            )
        ]

        answer = "\n\n".join(
            f"{item['table']} — {item['title']}. "
            f"{item['detail']} Resolution: {item['action']}"
            for item in found[:12]
        ) or (
            "No IQR outlier candidates or implemented cross-field business-rule "
            "violations were found for that question. This does not prove every "
            "value is valid; rules depend on source definitions."
        )
        sources = list(dict.fromkeys(item["table"] for item in found))

    elif re.search(
        r"how many|row count|record count|total records|total rows|tables|columns|fields",
        question_lower,
    ):
        answer = (
            f"{analysis['table_count']} table(s), "
            f"{analysis['row_count']:,} rows, "
            f"{analysis['field_count']} fields, "
            f"{analysis['missing_cells']:,} missing cells, "
            f"{analysis['duplicate_rows']:,} exact duplicate rows. "
            f"Completeness: {analysis['completeness_pct']:.1f}% across all cells."
        )
        sources = [table["name"] for table in analysis["tables"]]

    else:
        matched = find_mentioned_field()

        if matched:
            table, column = matched
            series = tables[table["name"]][column["name"]]
            sources = [f"{table['name']}.{column['name']}"]

            if re.search(r"missing|null|blank|complete|quality", question_lower):
                answer = (
                    f"{table['name']}.{column['name']}: "
                    f"{column['missing']:,} missing of "
                    f"{table['rows']:,} rows "
                    f"({column['missing_pct']:.4f}% missing; "
                    f"{100 - column['missing_pct']:.4f}% populated)."
                )

            elif (
                column["type"] == "numeric"
                and re.search(
                    r"mean|average|avg|median|min|max|sum|total|highest|lowest",
                    question_lower,
                )
            ):
                values = numeric_series(series).dropna()

                # Resolve aggregation intent after removing the matched field name.
                # This prevents "average of Total Charges" from becoming a sum
                # because the field happens to contain the word "Total".
                intent_text = re.sub(
                    re.escape(str(column["name"])),
                    " ",
                    question_lower,
                    flags=re.I,
                )

                if re.search(r"\bmedian\b", intent_text):
                    operation = "median"
                elif re.search(r"\b(mean|average|avg)\b", intent_text):
                    operation = "average"
                elif re.search(r"\b(minimum|min|lowest)\b", intent_text):
                    operation = "minimum"
                elif re.search(r"\b(maximum|max|highest)\b", intent_text):
                    operation = "maximum"
                elif re.search(r"\b(sum|total|combined|aggregate)\b", intent_text):
                    operation = "sum"
                else:
                    operation = "average"

                value = {
                    "median": values.median(),
                    "sum": values.sum(),
                    "minimum": values.min(),
                    "maximum": values.max(),
                    "average": values.mean(),
                }[operation]

                answer = (
                    f"{operation} {column['name']} in {table['name']}: "
                    f"{fmt(value)} across {len(values):,} non-missing numeric "
                    f"values (of {table['rows']:,} rows). This is the "
                    f"{operation} of row-level values; use a sum as a total "
                    "only if the field's definition makes rows additive."
                )

            elif (
                column["type"] == "text"
                and re.search(
                    r"top|most common|frequent|distribution|highest|most records",
                    question_lower,
                )
                and not column["masked"]
            ):
                counts = (
                    series.astype("string")
                    .fillna("Unknown")
                    .value_counts(dropna=False)
                    .head(10)
                )
                answer = (
                    f"Most frequent {column['name']} values in {table['name']}: "
                    + ", ".join(
                        f"{key} ({value:,}; {100 * value / table['rows']:.4f}%)"
                        for key, value in counts.items()
                    )
                    + "."
                )

            else:
                answer = (
                    f"{table['name']}.{column['name']} is a {column['type']} "
                    f"field with {column['unique']:,} distinct non-null values, "
                    f"{column['missing']:,} missing rows, and "
                    f"{column['missing_pct']:.1f}% missing. Ask for its average, "
                    "median, minimum, maximum, top values, or missingness."
                )

        elif re.search(r"goal|focus|recommend|suggest|what should", question_lower):
            answer = (
                f"Current analysis goal: {analysis['goal']} "
                f"(focus: {analysis['focus']}). "
                + " ".join(item["text"] for item in analysis["recommendations"])
            )
            sources = [table["name"] for table in analysis["tables"]]

        else:
            fields = ", ".join(
                f"{table['name']}: "
                + ", ".join(
                    column["name"]
                    for column in table["columns"]
                    if not column["masked"]
                )
                for table in analysis["tables"][:6]
            )
            answer = (
                "I couldn't map that question to a supported calculation, "
                "so I won't guess. Available non-identifying fields: "
                f"{fields}. Try “Which city has the most records?”, "
                "“How many missing values are in [field]?”, "
                "“What is the average of [numeric field]?”, "
                "“Summarize the analysis”, or “Show outlier findings.”"
            )
            sources = [table["name"] for table in analysis["tables"]]

    return {
        "answer": answer,
        "sources": sources,
        "goal": analysis["goal"],
    }


class Handler(BaseHTTPRequestHandler):
    server_version = "HealthOpsHTTP/1.0"
    sys_version = ""

    def log_message(self, fmt, *args):
        LOGGER.info(json.dumps({
            "event": "http_access",
            "method": self.command,
            "path": urlparse(self.path).path,
            "status": (
                int(args[1])
                if len(args) > 1 and str(args[1]).isdigit()
                else None
            ),
        }, separators=(",", ":")))

    def send_json(self, obj, code=200, extra_headers=None):
        raw = json.dumps(obj, default=safe).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(raw)))
        self.send_header("Cache-Control", "no-store")
        self.send_header("X-Content-Type-Options", "nosniff")
        self.send_header("X-Frame-Options", "DENY")
        self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
        self.send_header(
            "Content-Security-Policy",
            "default-src 'self' data: blob:; "
            "style-src 'self' 'unsafe-inline'; "
            "script-src 'self' 'unsafe-inline'; "
            "img-src 'self' data: blob:; connect-src 'self'; "
            "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
        )
        for header_name, header_value in (extra_headers or {}).items():
            self.send_header(header_name, header_value)
        self.end_headers()
        self.wfile.write(raw)

    def _session_token(self):
        cookie = SimpleCookie()
        try:
            cookie.load(self.headers.get("Cookie", ""))
        except Exception:
            return None
        morsel = cookie.get(SESSION_COOKIE_NAME)
        return morsel.value if morsel else None

    def _cookie_header(self, token=None, clear=False):
        if clear:
            return (
                f"{SESSION_COOKIE_NAME}=; Path=/; Max-Age=0; "
                "Expires=Thu, 01 Jan 1970 00:00:00 GMT; HttpOnly; SameSite=Strict"
            )
        return (
            f"{SESSION_COOKIE_NAME}={token}; Path=/; Max-Age=43200; "
            "HttpOnly; SameSite=Strict"
        )

    def _identity_from_session(self):
        token = self._session_token()
        if not token:
            return None
        try:
            token_hash = hash_session_token(token)
        except ValueError:
            return None
        rows = session_lookup(token_hash)
        identity = next(
            (dict(row) for row in rows if row.get("organization_id") is not None),
            None,
        )
        if identity is None:
            return None
        return {"token_hash": token_hash, "identity": identity}

    def _run_authenticated(self, callback):
        try:
            auth = self._identity_from_session()
        except Exception as exc:
            LOGGER.warning(json.dumps({
                "event": "session_validation_failed",
                "error_type": type(exc).__name__,
            }, separators=(",", ":")))
            return self.send_json({"error": "Authentication service unavailable. Check PostgreSQL and retry."}, 503)
        if auth is None:
            return self.send_json({"error": "Please sign in to continue."}, 401)
        self.healthops_identity = auth["identity"]
        return callback()

    def do_GET(self):
        path = urlparse(self.path).path
        public_paths = {"/api/health", "/api/config", "/api/auth/state"}
        if AUTH_BYPASS_FOR_TESTS or not path.startswith("/api/") or path in public_paths:
            return self._do_GET_inner()
        return self._run_authenticated(self._do_GET_inner)

    def _do_GET_inner(self):
        path = urlparse(self.path).path

        if path == "/":
            raw = (HERE / "index.html").read_text(encoding="utf-8")
            for key, value in BRANDING.items():
                raw = raw.replace(
                    "{{" + key.upper() + "}}",
                    html.escape(str(value)),
                )
            raw = raw.encode("utf-8")
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self' data: blob:; "
                "style-src 'self' 'unsafe-inline'; script-src 'self'; "
                "img-src 'self' data: blob:; connect-src 'self'; "
                "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(raw)

        elif path.startswith("/static/"):
            # Resolve after URL decoding and reject traversal/symlink escape attempts.
            static_root = (HERE / "static").resolve()
            relative = unquote(path[len("/static/"):])
            candidate = (static_root / relative).resolve()

            if not candidate.is_relative_to(static_root) or not candidate.is_file():
                return self.send_error(404)

            raw = candidate.read_bytes()
            content_type = (
                mimetypes.guess_type(candidate.name)[0]
                or "application/octet-stream"
            )
            if content_type in ("text/css", "application/javascript"):
                content_type += "; charset=utf-8"

            self.send_response(200)
            self.send_header("Content-Type", content_type)
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-cache")
            self.send_header("X-Content-Type-Options", "nosniff")
            self.send_header("X-Frame-Options", "DENY")
            self.send_header("Referrer-Policy", "strict-origin-when-cross-origin")
            self.send_header(
                "Content-Security-Policy",
                "default-src 'self' data: blob:; style-src 'self' 'unsafe-inline'; "
                "script-src 'self'; img-src 'self' data: blob:; connect-src 'self'; "
                "object-src 'none'; base-uri 'self'; frame-ancestors 'none'",
            )
            self.end_headers()
            self.wfile.write(raw)

        elif path == "/favicon.svg":
            monogram = html.escape(str(BRANDING.get("monogram", "HO"))[:3])
            raw = (
                '<svg xmlns="http://www.w3.org/2000/svg" viewBox="0 0 64 64">'
                '<rect width="64" height="64" rx="16" fill="#6d52cf"/>'
                f'<text x="32" y="40" text-anchor="middle" font-family="Arial" '
                f'font-size="24" font-weight="700" fill="white">{monogram}</text>'
                "</svg>"
            ).encode()

            self.send_response(200)
            self.send_header("Content-Type", "image/svg+xml")
            self.send_header("Content-Length", str(len(raw)))
            self.send_header("Cache-Control", "no-store")
            self.end_headers()
            self.wfile.write(raw)

        elif path == "/api/auth/state":
            if AUTH_BYPASS_FOR_TESTS:
                return self.send_json({
                    "setup_required": False,
                    "authenticated": True,
                    "test_mode": True,
                    "identity": {"display_name": "Test User", "organization_name": "Test Organization"},
                })
            try:
                setup_required = auth_setup_required()
                auth = self._identity_from_session()
                return self.send_json({
                    "setup_required": setup_required,
                    "authenticated": auth is not None,
                    "identity": auth["identity"] if auth else None,
                })
            except Exception as exc:
                LOGGER.warning(json.dumps({
                    "event": "auth_state_failed",
                    "error_type": type(exc).__name__,
                }, separators=(",", ":")))
                return self.send_json({"error": "Authentication database unavailable. Check PostgreSQL and restart the application."}, 503)

        elif path == "/api/health":
            self.send_json({
                "status": "ok",
                "service": "healthops-intelligence",
            })

        elif path == "/api/ready":
            ready = bool(
                SESSION.get("tables") and SESSION.get("last_analysis")
            )
            self.send_json({
                "ready": ready,
                "analysis_ready": bool(SESSION.get("last_analysis")),
            }, 200 if ready else 503)

        elif path == "/api/config":
            try:
                parquet = importlib.util.find_spec("pyarrow") is not None
            except (ImportError, ValueError):
                parquet = False
            try:
                xlsb = importlib.util.find_spec("pyxlsb") is not None
            except (ImportError, ValueError):
                xlsb = False
            try:
                xls = importlib.util.find_spec("xlrd") is not None
            except (ImportError, ValueError):
                xls = False
            try:
                xlsx = importlib.util.find_spec("openpyxl") is not None
            except (ImportError, ValueError):
                xlsx = False

            formats = [
                {
                    "format": "CSV",
                    "available": True,
                    "status": "available",
                    "notes": "Parsed as a comma-delimited table.",
                },
                {
                    "format": "TSV",
                    "available": True,
                    "status": "available",
                    "notes": "Parsed as a tab-delimited table.",
                },
                {
                    "format": "XLSX",
                    "available": xlsx,
                    "status": "available" if xlsx else "optional",
                    "notes": "Each workbook sheet becomes a table; requires openpyxl.",
                },
                {
                    "format": "XLS",
                    "available": xls,
                    "status": "available" if xls else "optional",
                    "notes": "Legacy Excel; requires xlrd.",
                },
                {
                    "format": "XLSB",
                    "available": xlsb,
                    "status": "available" if xlsb else "optional",
                    "notes": "Requires optional pyxlsb.",
                },
                {
                    "format": "JSON",
                    "available": True,
                    "status": "available",
                    "notes": "Arrays or named arrays become tables.",
                },
                {
                    "format": "JSONL / NDJSON",
                    "available": True,
                    "status": "available",
                    "notes": "One JSON record per line.",
                },
                {
                    "format": "Parquet",
                    "available": parquet,
                    "status": "available" if parquet else "optional",
                    "notes": "Requires optional pyarrow or fastparquet.",
                },
                {
                    "format": "XML",
                    "available": True,
                    "status": "available",
                    "notes": (
                        "One tabular row set only; hierarchical clinical "
                        "documents require a dedicated connector."
                    ),
                },
                {
                    "format": "ZIP",
                    "available": True,
                    "status": "available",
                    "notes": (
                        f"Max {MAX_ARCHIVE_FILES} members, 500 MB expanded, "
                        "bounded compression ratio."
                    ),
                },
                {
                    "format": "FHIR / HL7 / PDF / DOCX",
                    "available": False,
                    "status": "unsupported",
                    "notes": (
                        "Not implemented; dedicated clinical/document adapters "
                        "are required."
                    ),
                },
            ]

            config = {
                "branding": BRANDING,
                "environment": SETTINGS.environment,
                "persistence": {
                    "enabled": PERSISTENCE is not None,
                    "mode": (
                        "durable-local"
                        if PERSISTENCE is not None
                        else "process-memory-test-mode"
                    ),
                    "scope": "single-user local workspace",
                },
                "formats": formats,
                "upload_limit_bytes": MAX_UPLOAD,
                "request_limit_bytes": MAX_REQUEST_BYTES,
                "database_connectors": ["SQLite read-only"],
                "report_formats": [
                    "Markdown",
                    "CSV export for an approved derived copy",
                ],
            }
            self.send_json(config)

        elif path == "/api/status":
            self.send_json({
                "ready": bool(SESSION["tables"]),
                "analysis_ready": bool(SESSION.get("last_analysis")),
                "tables": len(SESSION["tables"]),
                "source_type": (
                    "sqlite"
                    if SESSION.get("source_db")
                    else "upload" if SESSION["tables"] else None
                ),
                "environment": SETTINGS.environment,
                "storage_mode": (
                    "durable-local"
                    if PERSISTENCE is not None
                    else "process-memory-test-mode"
                ),
                "clean_active": bool(SESSION.get("clean_active")),
                "refreshed_at": SESSION.get("refreshed_at"),
            })

        elif path == "/api/analysis":
            self.send_json({"analysis": SESSION.get("last_analysis")})

        elif path.startswith("/api/jobs/"):
            job_id = path.rsplit("/", 1)[-1]
            with JOB_LOCK:
                job = JOBS.get(job_id)

            if job is None and PERSISTENCE is not None:
                job = PERSISTENCE.get_job(job_id)

            self.send_json(
                job or {
                    "status": "missing",
                    "error": "Analysis job not found.",
                },
                404 if not job else 200,
            )

        else:
            self.send_error(404)

    def do_POST(self):
        path = urlparse(self.path).path
        public_paths = {"/api/auth/setup", "/api/auth/login", "/api/auth/logout"}
        if not AUTH_BYPASS_FOR_TESTS and path.startswith("/api/"):
            origin = self.headers.get("Origin")
            host = self.headers.get("Host", "").lower()
            if origin:
                parsed_origin = urlparse(origin)
                if parsed_origin.scheme not in ("http", "https") or parsed_origin.netloc.lower() != host:
                    return self.send_json({"error": "Cross-origin request blocked."}, 403)
        if AUTH_BYPASS_FOR_TESTS or path in public_paths or not path.startswith("/api/"):
            return self._do_POST_inner()
        return self._run_authenticated(self._do_POST_inner)

    def _do_POST_inner(self):
        path = urlparse(self.path).path
        raw_length = self.headers.get("Content-Length")

        try:
            length = int(raw_length or "0")
        except (TypeError, ValueError):
            return self.send_json(
                {"error": "A valid Content-Length header is required."},
                411,
            )

        if length < 0:
            return self.send_json(
                {"error": "Content-Length cannot be negative."},
                400,
            )

        if length > MAX_REQUEST_BYTES:
            return self.send_json(
                {"error": "Upload request exceeds the configured 500 MB request limit."},
                413,
            )

        body = self.rfile.read(length)
        if len(body) != length:
            return self.send_json(
                {"error": "Request body ended before the declared Content-Length."},
                400,
            )

        try:
            if path == "/api/auth/setup":
                request = json.loads(body or b"{}")
                if request.get("password") != request.get("confirm_password"):
                    raise ValueError("The passwords do not match.")
                token, identity = bootstrap_first_owner(
                    request.get("organization_name", ""),
                    request.get("display_name", ""),
                    request.get("email", ""),
                    request.get("password", ""),
                )
                return self.send_json(
                    {"ok": True, "identity": identity},
                    201,
                    {"Set-Cookie": self._cookie_header(token)},
                )

            if path == "/api/auth/login":
                request = json.loads(body or b"{}")
                token, identity = login_user(
                    request.get("email", ""),
                    request.get("password", ""),
                )
                return self.send_json(
                    {"ok": True, "identity": identity},
                    200,
                    {"Set-Cookie": self._cookie_header(token)},
                )

            if path == "/api/auth/logout":
                token = self._session_token()
                if token:
                    try:
                        logout_user(hash_session_token(token))
                    except ValueError:
                        pass
                return self.send_json(
                    {"ok": True},
                    200,
                    {"Set-Cookie": self._cookie_header(clear=True)},
                )

            if path == "/api/upload":
                files, goal = parse_upload_parts(
                    body,
                    self.headers.get("Content-Type", ""),
                )
                incoming = {}

                for filename, raw in files:
                    incoming.update(load_upload(filename, raw))

                if not incoming:
                    raise ValueError("No supported files found.")

                SESSION["tables"] = incoming
                SESSION["source_tables"] = None
                SESSION["cleaned"] = {}
                SESSION["clean_active"] = False
                SESSION["source_db"] = None
                SESSION["refreshed_at"] = pd.Timestamp.now(
                    tz="UTC"
                ).isoformat(timespec="seconds")
                SESSION.pop("_analysis_static", None)
                SESSION["clean_log"] = []
                SESSION["last_analysis"] = None
                SESSION["generation"] += 1
                SESSION["goal"] = goal or SESSION.get("goal")

                persist_workspace_state()
                job_id = queue_analysis(SESSION["goal"])

                return self.send_json({
                    "ok": True,
                    "job_id": job_id,
                    "status": "processing",
                }, 202)

            if path == "/api/refresh":
                if not SESSION.get("source_db"):
                    raise ValueError(
                        "Live refresh is available for a connected read-only "
                        "SQLite source. Re-upload files to analyze a new snapshot."
                    )

                incoming = read_sqlite_tables(SESSION["source_db"])
                SESSION["tables"] = incoming
                SESSION["source_tables"] = None
                SESSION.pop("_analysis_static", None)
                SESSION["cleaned"] = {}
                SESSION["clean_active"] = False
                SESSION["clean_log"] = []
                SESSION["last_analysis"] = None
                SESSION["generation"] += 1
                SESSION["refreshed_at"] = pd.Timestamp.now(
                    tz="UTC"
                ).isoformat(timespec="seconds")

                persist_workspace_state()
                job_id = queue_analysis(SESSION.get("goal"))

                return self.send_json({
                    "ok": True,
                    "job_id": job_id,
                    "status": "processing",
                    "refreshed_at": SESSION["refreshed_at"],
                }, 202)

            if path == "/api/analyze":
                request = json.loads(body or b"{}")
                if not SESSION["tables"]:
                    raise ValueError("Upload data before running an analysis.")

                SESSION["goal"] = request.get(
                    "goal",
                    SESSION.get("goal"),
                )
                SESSION["last_analysis"] = None
                persist_workspace_state()
                job_id = queue_analysis(SESSION["goal"])

                return self.send_json({
                    "ok": True,
                    "job_id": job_id,
                    "status": "processing",
                }, 202)

            if path == "/api/chat":
                request = json.loads(body or b"{}")
                question = str(request.get("question", "")).strip()

                if not question:
                    raise ValueError(
                        "Enter a question about your loaded dataset."
                    )

                if not SESSION["tables"]:
                    return self.send_json({
                        "answer": (
                            "Upload a dataset first. I can then calculate "
                            "aggregate answers from its tables."
                        ),
                        "sources": [],
                    })

                analysis = SESSION.get("last_analysis")
                if not analysis:
                    return self.send_json({
                        "error": (
                            "Analysis is still processing. Try again in a moment."
                        )
                    }, 409)

                generation = SESSION.get("generation", 0)

                if _CHAT_HISTORY["generation"] != generation:
                    _CHAT_HISTORY["generation"] = generation
                    _CHAT_HISTORY["questions"] = []
                    _CHAT_HISTORY["last_outlier_result"] = None
                    _CHAT_HISTORY["last_analytical_result"] = None

                previous_questions = list(_CHAT_HISTORY["questions"])

                # Prefer exact, previously calculated outlier evidence for
                # short follow-up questions. Do not call Gemini unnecessarily.
                result = _answer_outlier_followup(
                    question,
                    _CHAT_HISTORY.get("last_outlier_result"),
                )

                if result is None:
                    result = _answer_methodology_followup(
                        question,
                        _CHAT_HISTORY.get("last_analytical_result"),
                        SESSION.get("tables") or {},
                    )

                if result is None:
                    result = chat_answer(
                        question,
                        analysis,
                        conversation_history=previous_questions,
                    )

                _CHAT_HISTORY["questions"] = (
                    previous_questions + [question]
                )[-5:]

                result_plan = result.get("query_plan") or {}
                result_operation = result_plan.get("operation")

                if (
                    result.get("executed")
                    and result_operation in {"rank", "group_aggregate"}
                    and result_plan.get("results")
                ):
                    _CHAT_HISTORY["last_analytical_result"] = result
                elif (
                    result.get("executed")
                    and result_operation not in {
                        "methodology_followup", "outliers_followup"
                    }
                ):
                    _CHAT_HISTORY["last_analytical_result"] = None

                if result.get("executed") and result_operation == "outliers":
                    _CHAT_HISTORY["last_outlier_result"] = result
                elif (
                    result.get("executed")
                    and result_operation
                    and result_operation != "outliers_followup"
                ):
                    _CHAT_HISTORY["last_outlier_result"] = None

                return self.send_json(result)

            if path == "/api/visualization":
                if not SESSION.get("last_analysis"):
                    raise ValueError(
                        "Wait for data analysis to finish before creating a visualization."
                    )
                return self.send_json(
                    build_visualization(json.loads(body or b"{}"))
                )

            if path == "/api/clean":
                request = json.loads(body)
                cleaned = {}

                for name, dataframe in SESSION["tables"].items():
                    output = dataframe.copy()

                    if request.get("trim", False):
                        for column in output.select_dtypes(
                            include=["object", "string"]
                        ).columns:
                            output[column] = output[column].map(
                                lambda value: (
                                    value.strip()
                                    if isinstance(value, str)
                                    else value
                                )
                            )

                    if request.get("normalize_dates", False):
                        for column in output.columns:
                            if re.search(
                                r"date|time|dob|birth",
                                str(column),
                                re.I,
                            ):
                                parsed = pd.to_datetime(
                                    output[column],
                                    errors="coerce",
                                )
                                valid = output[column].notna()
                                if (
                                    valid.any()
                                    and parsed[valid].notna().mean() > 0.85
                                ):
                                    output[column] = parsed

                    if request.get("normalize_numbers", False):
                        for column in output.select_dtypes(
                            include=["object", "string"]
                        ).columns:
                            parsed = numeric_series(output[column])
                            valid = output[column].notna()
                            if (
                                valid.any()
                                and parsed[valid].notna().mean() >= 0.85
                                and output[column].astype(str).str.contains(
                                    r"[$,]", regex=True
                                ).any()
                            ):
                                output[column] = parsed

                    if request.get("drop_exact_duplicates", False):
                        output = output.drop_duplicates()

                    cleaned[name] = output

                if SESSION.get("source_tables") is None:
                    SESSION["source_tables"] = {
                        name: dataframe.copy()
                        for name, dataframe in SESSION["tables"].items()
                    }

                SESSION["cleaned"] = cleaned
                SESSION["clean_log"] = [
                    action
                    for action, enabled in [
                        ("trim text whitespace", request.get("trim")),
                        (
                            "normalize unambiguous date fields",
                            request.get("normalize_dates"),
                        ),
                        (
                            "parse currency-like text as numeric",
                            request.get("normalize_numbers"),
                        ),
                        (
                            "remove exact duplicates",
                            request.get("drop_exact_duplicates"),
                        ),
                    ]
                    if enabled
                ]

                persist_workspace_state()

                return self.send_json({
                    "ok": True,
                    "message": (
                        "Approved transformations applied to a separate copy. "
                        "Choose whether to analyze it; the source remains available."
                    ),
                    "tables": [
                        {
                            "name": name,
                            "rows_before": len(SESSION["tables"][name]),
                            "rows_after": len(dataframe),
                            "removed_duplicates": (
                                len(SESSION["tables"][name]) - len(dataframe)
                            ),
                        }
                        for name, dataframe in cleaned.items()
                    ],
                    "cleaning_actions": SESSION["clean_log"],
                })

            if path == "/api/activate-cleaned":
                if not SESSION.get("cleaned"):
                    raise ValueError(
                        "Create and approve a cleaned copy before analyzing it."
                    )

                if SESSION.get("source_tables") is None:
                    SESSION["source_tables"] = {
                        name: dataframe.copy()
                        for name, dataframe in SESSION["tables"].items()
                    }

                SESSION["tables"] = {
                    name: dataframe.copy()
                    for name, dataframe in SESSION["cleaned"].items()
                }
                SESSION["clean_active"] = True
                SESSION.pop("_analysis_static", None)
                SESSION["last_analysis"] = None
                SESSION["generation"] += 1
                SESSION["refreshed_at"] = pd.Timestamp.now(
                    tz="UTC"
                ).isoformat(timespec="seconds")

                persist_workspace_state()
                job_id = queue_analysis(SESSION.get("goal"))

                return self.send_json({
                    "ok": True,
                    "job_id": job_id,
                    "status": "processing",
                }, 202)

            if path == "/api/revert-cleaned":
                if SESSION.get("source_tables") is None:
                    raise ValueError(
                        "No preserved source copy is available to restore."
                    )

                SESSION["tables"] = {
                    name: dataframe.copy()
                    for name, dataframe in SESSION["source_tables"].items()
                }
                SESSION["clean_active"] = False
                SESSION.pop("_analysis_static", None)
                SESSION["last_analysis"] = None
                SESSION["generation"] += 1
                SESSION["refreshed_at"] = pd.Timestamp.now(
                    tz="UTC"
                ).isoformat(timespec="seconds")

                persist_workspace_state()
                job_id = queue_analysis(SESSION.get("goal"))

                return self.send_json({
                    "ok": True,
                    "job_id": job_id,
                    "status": "processing",
                }, 202)

            if path == "/api/report":
                analysis = SESSION.get("last_analysis")
                if not analysis:
                    raise ValueError(
                        "Wait for analysis to finish before exporting the report."
                    )

                goal = json.loads(body or b"{}").get("goal", "")
                lines = [
                    f"# {BRANDING['product_name']} — Healthcare Analytics Report",
                    "",
                    f"Generated locally: {pd.Timestamp.now().isoformat(timespec='minutes')}",
                    f"Analysis question: {goal or analysis.get('goal') or 'Not specified'}",
                    f"Analysis focus: {analysis.get('focus', 'Not determined')}",
                    "",
                    "> Privacy: patient-level values are excluded from this report.",
                    "",
                    "## Dataset inventory",
                ]

                for table in analysis["tables"]:
                    lines.append(
                        f"- **{table['name']}** — {table['rows']:,} rows, "
                        f"{len(table['columns'])} columns; exact duplicates: "
                        f"{table['duplicate_rows']:,}."
                    )

                lines += ["", "## Field profile"]
                for table in analysis["tables"]:
                    lines += [
                        f"### {table['name']}",
                        "| Field | Type | Healthcare domain | Missing | Unique |",
                        "|---|---|---|---:|---:|",
                    ]
                    for column in table["columns"]:
                        lines.append(
                            f"| {column['name']} | {column['type']} | "
                            f"{column['domain']} | {column['missing']:,} "
                            f"({column['missing_pct']}%) | {column['unique']:,} |"
                        )

                lines += ["", "## Relationship candidates"]
                lines += [
                    f"- `{item['left']}.{item['left_column']}` ↔ "
                    f"`{item['right']}.{item['right_column']}` "
                    "(candidate; confirm before analysis)."
                    for item in analysis["relationships"]
                ] or ["- No matching key candidates detected."]

                lines += ["", "## Data quality findings"]
                lines += [
                    f"- **{item['table']} — {item['title']}.** "
                    f"{item['detail']} Recommended next step: "
                    f"{item.get('action', 'Review with the source owner before changing data.')}"
                    for item in analysis["issues"]
                ] or [
                    "- No missingness, exact duplicates, or numeric outlier "
                    "candidates detected in the loaded tables."
                ]

                lines += ["", "## Cleaning suggestions"]
                lines += [
                    f"- **{item.get('table', 'All tables')} — {item['title']}.** "
                    f"{item['action']}"
                    for item in analysis.get("cleaning_suggestions", [])
                ] or ["- No specific cleaning candidates were detected."]

                lines += ["", "## Approved cleaning actions"]
                lines += [
                    f"- {action}"
                    for action in SESSION.get("clean_log", [])
                ] or ["- No cleaning actions were approved."]

                lines += ["", "## Goal-specific indicators"]
                lines += [
                    f"- {item['label']}: {format_metric(item['value'])} "
                    f"{item['unit']} across {item['n']:,} non-missing values "
                    f"in {item['table']}."
                    for item in analysis.get("goal_kpis", [])
                ] or ["- No numeric fields matched the selected goal."]

                lines += ["", "## Suggested operational questions"]
                lines += [
                    f"- **{item['category']}:** {item['text']}"
                    for item in analysis["recommendations"]
                ]

                lines += [
                    "",
                    "## Limitations",
                    "These are descriptive, heuristic findings. Statistical outliers "
                    "are not necessarily errors. No clinical conclusions or causal "
                    "claims are made. Validate definitions, completeness, "
                    "representativeness, and any proposed action with qualified teams.",
                    "",
                ]

                return self.send_json({
                    "markdown": "\n".join(lines),
                    "filename": BRANDING["report_filename"],
                    "csv": {
                        name: dataframe.to_csv(index=False)
                        for name, dataframe in SESSION["cleaned"].items()
                    },
                })

            if path == "/api/sqlite":
                request = json.loads(body)
                db_path = Path(
                    request.get("path", "")
                ).expanduser().resolve()
                SESSION["goal"] = request.get(
                    "goal",
                    SESSION.get("goal"),
                )

                incoming = read_sqlite_tables(db_path)

                SESSION["source_db"] = str(db_path)
                SESSION["tables"] = incoming
                SESSION["source_tables"] = None
                SESSION["cleaned"] = {}
                SESSION["clean_active"] = False
                SESSION["clean_log"] = []
                SESSION.pop("_analysis_static", None)
                SESSION["last_analysis"] = None
                SESSION["generation"] += 1
                SESSION["refreshed_at"] = pd.Timestamp.now(
                    tz="UTC"
                ).isoformat(timespec="seconds")

                persist_workspace_state()
                job_id = queue_analysis(SESSION.get("goal"))

                return self.send_json({
                    "ok": True,
                    "job_id": job_id,
                    "status": "processing",
                    "refreshed_at": SESSION["refreshed_at"],
                }, 202)

            return self.send_json({"error": "Unknown endpoint."}, 404)

        except AuthenticationError as exc:
            return self.send_json({"error": str(exc)}, 401)
        except Exception as exc:
            LOGGER.warning(json.dumps({
                "event": "request_failed",
                "method": self.command,
                "path": path,
                "error_type": type(exc).__name__,
            }, separators=(",", ":")))
            return self.send_json({"error": str(exc)}, 400)


if __name__ == "__main__":
    host = SETTINGS.host
    port = SETTINGS.port

    initialise_persistence()

    print(
        f"{BRANDING['product_name']} is running in local-development mode: "
        f"http://{host}:{port}"
    )
    print(
        "Workspace snapshots and recent jobs are saved locally under: "
        f"{PERSISTENCE.root}"
    )
    print(
        "This remains a single-user local-development build. "
        "Use synthetic or properly de-identified data only."
    )

    ThreadingHTTPServer((host, port), Handler).serve_forever()