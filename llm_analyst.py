
"""LLM query planning with local, validated execution for HealthOps Intelligence.

Only schema metadata and a redacted question are sent to the model. All DataFrame
filtering, grouping, aggregation, profiling, and quality checks execute locally.
The model cannot run Python, SQL, or arbitrary tools.
"""
from __future__ import annotations

import json
import logging
import os
import re
import threading
from typing import Any, Mapping

import pandas as pd

LOGGER = logging.getLogger("healthops.llm")
_CLIENT = None
_CLIENT_LOCK = threading.Lock()

try:
    from dotenv import load_dotenv
    load_dotenv(override=False)
except ImportError:
    pass


# Direct identifiers and contact fields are excluded from the model schema.
_SENSITIVE_FIELD_RE = re.compile(
    r"\b(?:mrn|medical.?record(?:.?number)?|ssn|email|phone|telephone|"
    r"address|patient.?id|member.?id|encounter.?id|claim.?id|"
    r"patient.?name|first.?name|last.?name|full.?name|date.?of.?birth|dob)\b",
    re.I,
)
_IDENTIFIER_FIELD_RE = re.compile(
    r"\b(?:id|npi|zip|postal.?code|diagnosis.?code|icd.?code)\b|"
    r"(?:^|[_\s])code(?:$|[_\s])",
    re.I,
)
_EMAIL_RE = re.compile(r"\b[A-Z0-9._%+-]+@[A-Z0-9.-]+\.[A-Z]{2,}\b", re.I)
_PHONE_RE = re.compile(r"(?<!\w)(?:\+?\d[\d ()-]{7,}\d)(?!\w)")
_MRN_RE = re.compile(
    r"\b(?:mrn|medical\s+record(?:\s+number)?)\s*[:#-]?\s*[A-Z0-9-]{4,}\b",
    re.I,
)

_OPERATIONS = [
    "aggregate",
    "group_aggregate",
    "group_count",
    "rank",
    "distinct",
    "row_count",
    "column_types",
    "missing_values",
    "duplicate_rows",
    "outliers",
    "summary",
    "unsupported_forecast",
    "clarify",
    "unsupported",
]
_AGGREGATIONS = ["mean", "median", "sum", "min", "max", "count"]
_FILTER_OPERATORS = ["eq", "ne", "contains", "gt", "gte", "lt", "lte"]

_TOOL = {
    "type": "function",
    "name": "plan_healthcare_analysis",
    "description": (
        "Select a safe analytical operation over the supplied table schema. "
        "This is a query plan only; the application validates and executes it locally."
    ),
    "strict": True,
    "parameters": {
        "type": "object",
        "properties": {
            "operation": {"type": "string", "enum": _OPERATIONS},
            "table": {"type": ["string", "null"]},
            "measure": {"type": ["string", "null"]},
            "group_by": {
                "type": "array",
                "items": {"type": "string"},
                "maxItems": 2,
            },
            "aggregation": {
                "type": ["string", "null"],
                "enum": _AGGREGATIONS + [None],
            },
            "filters": {
                "type": "array",
                "maxItems": 4,
                "items": {
                    "type": "object",
                    "properties": {
                        "field": {"type": "string"},
                        "operator": {
                            "type": "string",
                            "enum": _FILTER_OPERATORS,
                        },
                        "value": {"type": "string"},
                    },
                    "required": ["field", "operator", "value"],
                    "additionalProperties": False,
                },
            },
            "field": {"type": ["string", "null"]},
            "limit": {
                "type": "integer",
                "minimum": 1,
                "maximum": 50,
            },
            "sort_order": {
                "type": "string",
                "enum": ["asc", "desc"],
            },
            "include_percentages": {"type": "boolean"},
            "clarification": {"type": "string"},
        },
        "required": [
            "operation",
            "table",
            "measure",
            "group_by",
            "aggregation",
            "filters",
            "field",
            "limit",
            "sort_order",
            "include_percentages",
            "clarification",
        ],
        "additionalProperties": False,
    },
}

_PLANNER_INSTRUCTIONS = """You are the natural-language query planner for HealthOps Intelligence.
Your job is to map the user's question to ONE structured plan_healthcare_analysis call.

Rules:
- Use only exact table and column names in the supplied schema. Never invent fields.
- The schema contains no record values. Infer filter values only when explicitly stated
  in the user's question. Never invent a category value.
- Never request patient identifiers, names, contact details, or row-level records.
- For 'average X in Chennai', use aggregate, measure X, and an equality filter on the
  location/city field when present.
- For 'average X by department', use group_aggregate with aggregation mean.
- For 'record count by department', use group_count and group_by Department.
- For 'which group has the highest average X', use rank, aggregation mean, sort desc,
  and limit 1. For total/sum rankings use sum. For count rankings use group_count.
- For percentage shares of records, use group_count and include_percentages true.
- For 'compare admissions/visits/claims across hospitals', use group_aggregate; use
  sum for explicitly additive volume/count measures and mean only when the question
  asks for an average/rate. If the measure semantics are unclear, choose clarify.
- Use column_types, missing_values, duplicate_rows, outliers, or summary for those
  respective data-understanding questions.
- Requests to predict/forecast future outcomes must use unsupported_forecast. Do not
  claim that a forecast was calculated. Historical descriptive statistics are not forecasts.
- If the question is ambiguous, unsupported by the schema, or lacks a required field,
  choose clarify or unsupported and give a concise reason in clarification.
- Do not invent SQL, Python, formulas, statistics, or results. The application executes
  the plan against the original in-memory data and returns verified aggregate evidence.
"""



def _provider_name() -> str:
    return os.getenv("LLM_PROVIDER", "gemini").strip().lower()


def is_configured() -> bool:
    """Check Gemini provider, enable flag and API key."""
    flag = os.getenv("HEALTHOPS_LLM_ENABLED")
    if flag is not None and flag.strip().lower() in {"0", "false", "no", "off"}:
        return False
    return (
        _provider_name() == "gemini"
        and bool(os.getenv("GEMINI_API_KEY", "").strip())
    )


def _get_client():
    """Create and cache the Google Gemini client."""
    global _CLIENT

    if _CLIENT is not None:
        return _CLIENT

    key = os.getenv("GEMINI_API_KEY", "").strip()
    if not key:
        return None

    with _CLIENT_LOCK:
        if _CLIENT is None:
            try:
                from google import genai
                from google.genai import types
            except ImportError as exc:
                raise RuntimeError(
                    "Google Gen AI SDK is missing. "
                    "Run: python -m pip install 'google-genai>=1,<2'."
                ) from exc

            timeout_ms = int(
                max(
                    5.0,
                    float(os.getenv("HEALTHOPS_LLM_TIMEOUT_SECONDS", "30"))
                ) * 1000
            )

            _CLIENT = genai.Client(
                api_key=key,
                http_options=types.HttpOptions(timeout=timeout_ms),
            )

    return _CLIENT


def _gemini_parameters_schema() -> dict[str, Any]:
    """Build Gemini-compatible function parameters."""
    schema = json.loads(json.dumps(_TOOL["parameters"]))
    properties = schema["properties"]

    for field_name in ("table", "measure", "field"):
        properties[field_name] = {
            "type": "string",
            "description": "Use an empty string when not applicable.",
        }

    properties["aggregation"] = {
        "type": "string",
        "enum": _AGGREGATIONS + [""],
        "description": "Use an empty string when not applicable.",
    }

    return schema


def _call_gemini_for_plan(client, prompt: str, model: str):
    """Ask Gemini for one structured function call without auto-execution."""
    from google.genai import types

    declaration = types.FunctionDeclaration(
        name="plan_healthcare_analysis",
        description=(
            "Choose one safe analytical operation using the supplied schema. "
            "Return a plan only. The application validates and executes it locally."
        ),
        parameters_json_schema=_gemini_parameters_schema(),
    )

    tool = types.Tool(function_declarations=[declaration])

    config = types.GenerateContentConfig(
        system_instruction=_PLANNER_INSTRUCTIONS,
        tools=[tool],
        tool_config=types.ToolConfig(
            function_calling_config=types.FunctionCallingConfig(
                mode="ANY",
                allowed_function_names=["plan_healthcare_analysis"],
            )
        ),
        automatic_function_calling=types.AutomaticFunctionCallingConfig(
            disable=True
        ),
        temperature=0,
        max_output_tokens=1200,
    )

    return client.models.generate_content(
        model=model,
        contents=prompt,
        config=config,
    )

def _is_sensitive_field(name: str) -> bool:
    return bool(_SENSITIVE_FIELD_RE.search(str(name)))


def _is_identifier_field(name: str) -> bool:
    return bool(_IDENTIFIER_FIELD_RE.search(str(name))) or _is_sensitive_field(name)


def _numeric(series: pd.Series) -> pd.Series:
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


def _kind(series: pd.Series, name: str) -> str:
    if pd.api.types.is_bool_dtype(series):
        return "boolean"

    if pd.api.types.is_datetime64_any_dtype(series):
        return "date"

    if pd.api.types.is_numeric_dtype(series):
        return "numeric"

    nonnull = int(series.notna().sum())
    if nonnull:
        parsed = _numeric(series)
        if int(parsed.notna().sum()) / nonnull >= 0.85:
            return "numeric"

    if re.search(r"date|time|timestamp", name, re.I) and nonnull:
        parsed_dates = pd.to_datetime(series, errors="coerce")
        if int(parsed_dates.notna().sum()) / nonnull >= 0.80:
            return "date"

    return "text"


def _redact_question(
    question: str,
    tables: Mapping[str, pd.DataFrame],
) -> str:
    """Remove obvious direct identifiers before sending the question to the API."""
    cleaned = _EMAIL_RE.sub("[redacted email]", question)
    cleaned = _PHONE_RE.sub("[redacted phone]", cleaned)
    cleaned = _MRN_RE.sub("[redacted record identifier]", cleaned)

    # Redact exact values from small identifier columns without exporting them.
    for dataframe in tables.values():
        if not isinstance(dataframe, pd.DataFrame):
            continue

        for column in dataframe.columns:
            if not _is_sensitive_field(str(column)):
                continue

            series = dataframe[column]
            if not isinstance(series, pd.Series) or len(series) > 100_000:
                continue

            values = series.dropna().astype(str).unique()
            if len(values) > 2_000:
                continue

            for value in sorted(values, key=len, reverse=True):
                value = value.strip()
                if len(value) < 3:
                    continue
                cleaned = re.sub(
                    re.escape(value),
                    "[redacted]",
                    cleaned,
                    flags=re.I,
                )

    return cleaned


def _schema(tables: Mapping[str, pd.DataFrame]) -> dict[str, Any]:
    catalog = []

    for table_name, dataframe in tables.items():
        if not isinstance(dataframe, pd.DataFrame):
            continue

        fields = []
        hidden = 0

        for column in dataframe.columns:
            name = str(column)
            series = dataframe[column]

            if not isinstance(series, pd.Series):
                continue

            if _is_sensitive_field(name):
                hidden += 1
                continue

            kind = _kind(series, name)
            fields.append({
                "name": name,
                "type": kind,
                "can_group": (
                    kind == "text" and not _is_identifier_field(name)
                ),
                "can_measure": (
                    kind == "numeric" and not _is_identifier_field(name)
                ),
            })

        catalog.append({
            "table": str(table_name),
            "columns": fields,
            "excluded_sensitive_field_count": hidden,
        })

    return {"tables": catalog}


def _fmt(value: Any) -> str:
    try:
        number = float(value)
        if pd.isna(number):
            return "—"
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _failure(
    message: str,
    operation: str,
    table: str | None = None,
) -> dict[str, Any]:
    return {
        "answer": message,
        "sources": [],
        "executed": False,
        "query_plan": {
            "operation": operation,
            "table": table,
        },
    }


def _forecast_refusal(question: str) -> dict[str, Any] | None:
    future = re.search(
        r"\b(?:will|shall|going\s+to|predict(?:ion)?|forecast|project(?:ion)?|"
        r"expected\s+to|next\s+(?:year|month|quarter|week)|in\s+\d+\s+"
        r"(?:years?|months?|quarters?|weeks?)|future)\b",
        question,
        re.I,
    )

    outcome = re.search(
        r"\b(?:admissions?|visits?|encounters?|patients?|costs?|expenses?|"
        r"charges?|revenue|profit|workload|staffing|occupancy|demand|"
        r"readmissions?|mortality|claims?|payments?)\b",
        question,
        re.I,
    )

    if not (future and outcome):
        return None

    target = ""
    percentage = re.search(r"\b(\d+(?:\.\d+)?)\s*%", question)

    if percentage and re.search(
        r"\b(reduc\w*|decreas\w*|cut|lower\w*)\b",
        question,
        re.I,
    ):
        target = (
            f" A {percentage.group(1)}% reduction is a target, not a forecast."
        )

    metric = outcome.group(0).lower()

    if re.search(r"cost|expense|charge|fee", question, re.I):
        lead = (
            "I cannot reliably predict whether future hospital costs "
            "will decrease from the currently loaded data."
        )
    else:
        lead = (
            f"I cannot reliably predict future {metric} "
            "from the currently loaded data."
        )

    return _failure(
        f"{lead}{target} A defensible forecast needs time-indexed historical "
        "observations, a validated model, and an evaluation of forecast error. "
        "No forecast was run.",
        "unsupported_forecast",
    )


def _table_and_frame(
    plan: dict[str, Any],
    tables: Mapping[str, pd.DataFrame],
):
    requested = plan.get("table")

    if requested is None:
        if len(tables) != 1:
            return None, None, _failure(
                "Please specify which table to analyse; multiple tables are loaded.",
                "clarify",
            )
        requested = next(iter(tables))

    if requested not in tables or not isinstance(tables[requested], pd.DataFrame):
        return None, None, _failure(
            "The selected table is not present in the current workspace. "
            "Please choose an available table.",
            "clarify",
        )

    return str(requested), tables[requested], None


def _apply_filters(
    dataframe: pd.DataFrame,
    filters: list[dict[str, str]],
):
    filtered = dataframe
    applied = []

    for item in filters:
        field = str(item.get("field", ""))
        operator = str(item.get("operator", "eq"))
        value = str(item.get("value", ""))

        if field not in dataframe.columns:
            raise ValueError(
                f"Filter field '{field}' is not available in this table."
            )

        if _is_identifier_field(field):
            raise ValueError(
                "Filtering by patient or identifier fields is not supported."
            )

        if operator not in _FILTER_OPERATORS:
            raise ValueError("The requested filter operator is not supported.")

        series = filtered[field]
        kind = _kind(series, field)

        if operator in {"eq", "ne", "contains"} and kind == "text":
            text_series = series.astype("string")

            if operator == "eq":
                mask = (
                    text_series.str.strip().str.casefold()
                    == value.strip().casefold()
                )
            elif operator == "ne":
                mask = (
                    text_series.str.strip().str.casefold()
                    != value.strip().casefold()
                )
            else:
                mask = text_series.str.contains(
                    re.escape(value),
                    case=False,
                    na=False,
                )

        elif kind == "numeric":
            numeric = _numeric(series)

            try:
                target = float(
                    value.replace(",", "").replace("$", "").strip()
                )
            except ValueError as exc:
                raise ValueError(
                    f"Filter value for '{field}' must be numeric."
                ) from exc

            mask = {
                "eq": numeric == target,
                "ne": numeric != target,
                "gt": numeric > target,
                "gte": numeric >= target,
                "lt": numeric < target,
                "lte": numeric <= target,
            }.get(operator)

            if mask is None:
                raise ValueError(
                    f"Operator '{operator}' is not valid for numeric field '{field}'."
                )

        elif kind == "date":
            dates = pd.to_datetime(series, errors="coerce")
            target_date = pd.to_datetime(value, errors="coerce")

            if pd.isna(target_date):
                raise ValueError(
                    f"Filter value for '{field}' is not a valid date."
                )

            mask = {
                "eq": dates == target_date,
                "ne": dates != target_date,
                "gt": dates > target_date,
                "gte": dates >= target_date,
                "lt": dates < target_date,
                "lte": dates <= target_date,
            }.get(operator)

            if mask is None:
                raise ValueError(
                    f"Operator '{operator}' is not valid for date field '{field}'."
                )

        else:
            raise ValueError(
                f"Filter operator '{operator}' is not supported for field '{field}'."
            )

        filtered = filtered.loc[mask.fillna(False)]
        applied.append({
            "field": field,
            "operator": operator,
            "value": value,
        })

    return filtered, applied


def _execute_plan(
    plan: dict[str, Any],
    tables: Mapping[str, pd.DataFrame],
) -> dict[str, Any]:
    operation = plan.get("operation")

    if operation == "unsupported_forecast":
        return _failure(
            "A future prediction is not supported by a descriptive analytics "
            "query. No forecast was run.",
            "unsupported_forecast",
        )

    if operation in {"clarify", "unsupported"}:
        reason = str(
            plan.get("clarification")
            or "Please clarify the field or analysis you want."
        )
        return _failure(reason, str(operation))

    table, dataframe, error = _table_and_frame(plan, tables)
    if error:
        return error

    if operation == "column_types":
        requested_field = plan.get("field")
        records = []

        for column in dataframe.columns:
            name = str(column)

            if _is_sensitive_field(name):
                continue

            if requested_field and name != requested_field:
                continue

            records.append({
                "field": name,
                "type": _kind(dataframe[column], name),
                "pandas_dtype": str(dataframe[column].dtype),
            })

        if requested_field and not records:
            return _failure(
                f"Field '{requested_field}' is not available as a "
                "non-identifying field.",
                "clarify",
                table,
            )

        answer = (
            "Column data types in "
            + table
            + ": "
            + "; ".join(
                f"{item['field']} = {item['type']} "
                f"({item['pandas_dtype']})"
                for item in records
            )
            + "."
        )

        return {
            "answer": answer,
            "sources": [
                f"{table}.{item['field']}" for item in records
            ],
            "executed": True,
            "query_plan": {
                "operation": operation,
                "table": table,
                "fields_returned": len(records),
            },
        }

    if operation == "duplicate_rows":
        count = int(dataframe.duplicated().sum())
        answer = (
            f"Exact duplicate row count in {table}: "
            f"{count:,} across {len(dataframe):,} rows."
        )

        return {
            "answer": answer,
            "sources": [table],
            "executed": True,
            "query_plan": {
                "operation": operation,
                "table": table,
                "duplicate_rows": count,
                "rows": len(dataframe),
            },
        }

    if operation == "summary":
        missing = int(dataframe.isna().sum().sum())
        duplicates = int(dataframe.duplicated().sum())

        safe_fields = [
            str(column)
            for column in dataframe.columns
            if not _is_sensitive_field(str(column))
        ]

        type_counts: dict[str, int] = {}

        for column in safe_fields:
            kind = _kind(dataframe[column], column)
            type_counts[kind] = type_counts.get(kind, 0) + 1

        answer = (
            f"Dataset summary for {table}: {len(dataframe):,} rows, "
            f"{len(dataframe.columns)} fields, {missing:,} missing cells, "
            f"and {duplicates:,} exact duplicate rows. "
            "Non-identifying field types: "
            + ", ".join(
                f"{key}: {value}"
                for key, value in sorted(type_counts.items())
            )
            + "."
        )

        return {
            "answer": answer,
            "sources": [table],
            "executed": True,
            "query_plan": {
                "operation": operation,
                "table": table,
                "rows": len(dataframe),
                "fields": len(dataframe.columns),
                "missing_cells": missing,
                "duplicate_rows": duplicates,
                "field_type_counts": type_counts,
            },
        }

    if operation == "missing_values":
        requested_field = plan.get("field")

        selected = (
            [str(requested_field)]
            if requested_field
            else [
                str(column)
                for column in dataframe.columns
                if not _is_sensitive_field(str(column))
            ]
        )

        if any(
            name not in dataframe.columns or _is_sensitive_field(name)
            for name in selected
        ):
            return _failure(
                "The requested field is unavailable or is a protected identifier field.",
                "clarify",
                table,
            )

        values = {
            name: int(dataframe[name].isna().sum())
            for name in selected
        }

        answer = (
            "Missing values in "
            + table
            + ": "
            + "; ".join(
                f"{name} = {count:,}"
                for name, count in values.items()
            )
            + "."
        )

        return {
            "answer": answer,
            "sources": [
                f"{table}.{name}" for name in selected
            ],
            "executed": True,
            "query_plan": {
                "operation": operation,
                "table": table,
                "missing_by_field": values,
            },
        }

    if operation == "outliers":
        requested_field = plan.get("field")

        candidates = (
            [str(requested_field)]
            if requested_field
            else [
                str(column)
                for column in dataframe.columns
                if (
                    not _is_sensitive_field(str(column))
                    and _kind(dataframe[column], str(column)) == "numeric"
                )
            ]
        )

        results = []

        def format_outlier_number(value):
            number = float(value)
            if number.is_integer():
                return f"{int(number):,}"
            return f"{number:,.6f}".rstrip("0").rstrip(".")

        for name in candidates:
            if name not in dataframe.columns or _is_identifier_field(name):
                return _failure(
                    f"Field '{name}' is unavailable for outlier screening.",
                    "clarify",
                    table,
                )

            if _kind(dataframe[name], name) != "numeric":
                return _failure(
                    f"Field '{name}' is not numeric and cannot be IQR-screened.",
                    "clarify",
                    table,
                )

            values = _numeric(dataframe[name]).dropna()

            if len(values) < 10:
                results.append({
                    "field": name,
                    "n": len(values),
                    "method": "Tukey 1.5 * IQR",
                    "q1": None,
                    "q3": None,
                    "iqr": None,
                    "lower_bound": None,
                    "upper_bound": None,
                    "outlier_candidates": None,
                    "outlier_values": [],
                    "note": (
                        "At least 10 numeric observations are required "
                        "for this screening."
                    ),
                })
                continue

            q1 = float(values.quantile(0.25))
            q3 = float(values.quantile(0.75))
            iqr = q3 - q1
            lower = q1 - 1.5 * iqr
            upper = q3 + 1.5 * iqr

            outlier_mask = (values < lower) | (values > upper)
            outlier_series = values.loc[outlier_mask]
            count = int(outlier_mask.sum())

            outlier_values = []
            for value in outlier_series.tolist():
                numeric_value = float(value)
                outlier_values.append(
                    int(numeric_value)
                    if numeric_value.is_integer()
                    else numeric_value
                )

            results.append({
                "field": name,
                "n": len(values),
                "method": "Tukey 1.5 * IQR",
                "q1": q1,
                "q3": q3,
                "iqr": iqr,
                "lower_bound": float(lower),
                "upper_bound": float(upper),
                "outlier_candidates": count,
                "outlier_percentage": round(100 * count / len(values), 2),
                "outlier_values": outlier_values[:50],
                "outlier_values_truncated": count > 50,
            })

        parts = []

        for item in results:
            if item["outlier_candidates"] is None:
                parts.append(
                    f"{item['field']}: not screened ({item['note']})"
                )
                continue

            detected = item["outlier_values"]

            if detected:
                detected_text = ", ".join(
                    format_outlier_number(value)
                    for value in detected
                )
                if item["outlier_values_truncated"]:
                    detected_text += ", ... (showing first 50)"
            else:
                detected_text = "none"

            parts.append(
                f"{item['field']}: "
                f"Q1={format_outlier_number(item['q1'])}; "
                f"Q3={format_outlier_number(item['q3'])}; "
                f"IQR={format_outlier_number(item['iqr'])}; "
                f"lower bound={format_outlier_number(item['lower_bound'])}; "
                f"upper bound={format_outlier_number(item['upper_bound'])}; "
                f"{item['outlier_candidates']} IQR candidate(s) among "
                f"{item['n']} numeric values "
                f"({item['outlier_percentage']:.2f}%); "
                f"detected value(s): {detected_text}"
            )

        answer = (
            "IQR outlier screening in "
            + table
            + ": "
            + (
                "; ".join(parts)
                if parts
                else "no eligible numeric fields"
            )
            + ". Method: Tukey's 1.5?IQR rule. "
            "Statistical flags are candidates, not confirmed errors."
        )

        return {
            "answer": answer,
            "sources": [
                f"{table}.{item['field']}"
                for item in results
            ],
            "executed": True,
            "query_plan": {
                "operation": operation,
                "table": table,
                "outlier_results": results,
            },
        }

    allowed_operations = {
        "aggregate",
        "group_aggregate",
        "group_count",
        "rank",
        "distinct",
        "row_count",
    }

    if operation not in allowed_operations:
        return _failure(
            "I could not map that request to a supported local analytical operation.",
            "unsupported",
            table,
        )

    try:
        filtered, applied_filters = _apply_filters(
            dataframe,
            plan.get("filters") or [],
        )
    except ValueError as exc:
        return _failure(str(exc), "clarify", table)

    group_by = plan.get("group_by") or []

    if len(group_by) > 2:
        return _failure(
            "At most two grouping fields are supported in this version.",
            "clarify",
            table,
        )

    for name in group_by:
        if name not in dataframe.columns or _is_identifier_field(name):
            return _failure(
                f"Grouping field '{name}' is unavailable or protected.",
                "clarify",
                table,
            )

        if _kind(dataframe[name], name) not in {"text", "boolean"}:
            return _failure(
                f"Grouping field '{name}' must be categorical.",
                "clarify",
                table,
            )

    measure = plan.get("measure")
    aggregation = plan.get("aggregation")

    if operation in {"aggregate", "group_aggregate", "rank"}:
        if (
            not measure
            or measure not in dataframe.columns
            or _is_identifier_field(str(measure))
        ):
            return _failure(
                "Please specify an available, non-identifying numeric measure.",
                "clarify",
                table,
            )

        if _kind(dataframe[measure], measure) != "numeric":
            return _failure(
                f"Field '{measure}' is not numeric, so this calculation cannot be performed.",
                "clarify",
                table,
            )

        if aggregation not in _AGGREGATIONS:
            return _failure(
                "Please specify a supported aggregation: average, median, sum, "
                "minimum, maximum, or count.",
                "clarify",
                table,
            )

        if aggregation == "sum" and re.search(
            r"average|rate|ratio|percentage|percent",
            str(measure),
            re.I,
        ):
            return _failure(
                f"'{measure}' appears to be an average or rate field. "
                "Summing it would not produce a valid total.",
                "blocked_unsafe_sum",
                table,
            )

    if operation in {
        "group_aggregate",
        "rank",
        "group_count",
        "distinct",
    } and not group_by:
        return _failure(
            "Please specify a non-identifying category field for this grouped analysis.",
            "clarify",
            table,
        )

    if operation in {"aggregate", "row_count"} and group_by:
        return _failure(
            "This operation does not use a grouping field. Please clarify the question.",
            "clarify",
            table,
        )

    rows_before = len(dataframe)
    rows_after = len(filtered)

    source_fields = [f"{table}.{name}" for name in group_by]

    if measure:
        source_fields.append(f"{table}.{measure}")

    source_fields.extend(
        f"{table}.{item['field']}"
        for item in applied_filters
    )

    limit = max(1, min(int(plan.get("limit") or 10), 50))
    sort_ascending = plan.get("sort_order", "desc") == "asc"

    query_plan = {
        "operation": operation,
        "table": table,
        "measure": measure,
        "group_by": group_by,
        "aggregation": aggregation,
        "filters": applied_filters,
        "rows_before_filter": rows_before,
        "rows_after_filter": rows_after,
        "rows_excluded_by_filter": rows_before - rows_after,
        "source_fields": source_fields,
    }

    if operation == "row_count":
        answer = (
            f"Matching record count in {table}: {rows_after:,} rows."
        )

        if applied_filters:
            answer += " Applied filters: " + ", ".join(
                f"{item['field']} {item['operator']} {item['value']}"
                for item in applied_filters
            ) + "."

        query_plan["result_count"] = rows_after

    elif operation == "aggregate":
        values = _numeric(filtered[measure]).dropna()

        if not len(values):
            answer = (
                f"No interpretable numeric values for {measure} "
                "remain after filtering."
            )
            query_plan["value"] = None
        else:
            value = getattr(values, aggregation)()

            label = {
                "mean": "Average",
                "median": "Median",
                "sum": "Sum",
                "min": "Minimum",
                "max": "Maximum",
                "count": "Count",
            }[aggregation]

            answer = (
                f"{label} {measure} in {table}: {_fmt(value)} across "
                f"{len(values):,} non-missing numeric values "
                f"(of {rows_after:,} matching rows)."
            )

            if aggregation == "sum":
                answer += (
                    " Treat this as a total only if the source field "
                    "is additive at the row level."
                )

            query_plan.update({
                "value": float(value),
                "valid_numeric_values": len(values),
            })

    elif operation in {"group_aggregate", "rank"}:
        work = filtered[group_by].copy()
        work["__metric__"] = _numeric(filtered[measure])
        work = work.dropna(subset=["__metric__"])
        valid = len(work)

        if not valid:
            answer = (
                f"No numeric values for {measure} were available after filtering."
            )
            query_plan["groups_returned"] = 0
        else:
            grouped = (
                work.groupby(group_by, dropna=False)["__metric__"]
                .agg(aggregation)
            )

            counts = (
                work.groupby(group_by, dropna=False)["__metric__"]
                .count()
            )

            grouped = grouped.sort_values(
                ascending=sort_ascending,
                kind="stable",
            )

            if operation == "rank":
                selected = grouped.head(limit)

                if len(grouped) > len(selected) and len(selected):
                    boundary = selected.iloc[-1]
                    tied = grouped[grouped == boundary]

                    selected = pd.concat([
                        selected,
                        tied.loc[~tied.index.isin(selected.index)],
                    ])
                    selected = selected.iloc[:50]
            else:
                selected = grouped.head(100)

            label = {
                "mean": "average",
                "median": "median",
                "sum": "sum",
                "min": "minimum",
                "max": "maximum",
                "count": "count",
            }[aggregation]

            def display_key(key):
                return (
                    " | ".join(str(value) for value in key)
                    if isinstance(key, tuple)
                    else str(key)
                )

            details = "; ".join(
                f"{display_key(key)} = {_fmt(value)} "
                f"(n={int(counts.loc[key]):,})"
                for key, value in selected.items()
            )

            answer = (
                f"{label.title()} {measure} by {' and '.join(group_by)} "
                f"in {table}: {details}."
            )

            if aggregation == "sum":
                answer += (
                    " Summed values are meaningful as totals only when "
                    "the measure is additive."
                )

            query_plan.update({
                "groups_returned": len(selected),
                "groups_available": len(grouped),
                "results": [
                    {
                        "group": display_key(key),
                        "value": float(value),
                        "n": int(counts.loc[key]),
                    }
                    for key, value in selected.items()
                ],
            })

    elif operation in {"group_count", "distinct"}:
        if operation == "distinct":
            name = group_by[0]
            count = int(filtered[name].nunique(dropna=True))
            answer = (
                f"Distinct {name} values in {table}: "
                f"{count:,}; missing values excluded."
            )
            query_plan["distinct_count"] = count
        else:
            work = filtered[group_by].copy()

            for name in group_by:
                work[name] = (
                    work[name]
                    .astype("string")
                    .fillna("Unknown")
                )

            grouped = (
                work.groupby(group_by, dropna=False)
                .size()
                .sort_values(
                    ascending=sort_ascending,
                    kind="stable",
                )
                .head(limit)
            )

            denominator = max(rows_after, 1)
            parts = []

            for key, value in grouped.items():
                label = (
                    " | ".join(str(item) for item in key)
                    if isinstance(key, tuple)
                    else str(key)
                )

                piece = f"{label} = {int(value):,}"

                if plan.get("include_percentages"):
                    piece += (
                        f" ({100 * int(value) / denominator:.2f}%)"
                    )

                parts.append(piece)

            answer = (
                f"Record count by {' and '.join(group_by)} in {table}: "
                + "; ".join(parts)
                + "."
            )
            answer += " Missing category values are grouped as Unknown."
            query_plan["groups_returned"] = len(grouped)

    return {
        "answer": answer,
        "sources": list(dict.fromkeys(source_fields)) or [table],
        "executed": True,
        "query_plan": query_plan,
    }



def answer_with_llm(
    question: str,
    tables: Mapping[str, pd.DataFrame],
    analysis: Mapping[str, Any] | None = None,
    conversation_history: list[str] | None = None,
) -> dict[str, Any] | None:
    """Use Gemini for query planning and Pandas for local calculations."""

    question = str(question or "").strip()

    if not question or not tables:
        return None

    forecast = _forecast_refusal(question)
    if forecast is not None:
        return forecast

    if not is_configured():
        return None

    try:
        client = _get_client()
        if client is None:
            return None

        table_aliases = {
            f"table_{index}": name
            for index, name in enumerate(tables.keys(), start=1)
            if isinstance(tables[name], pd.DataFrame)
        }

        aliased_tables = {
            alias: tables[original]
            for alias, original in table_aliases.items()
        }

        schema = _schema(aliased_tables)
        safe_question = _redact_question(question, tables)

        # Previous user questions provide context for resolving follow-ups.
        # Each question is independently redacted before reaching Gemini.
        safe_history = [
            _redact_question(str(item).strip()[:500], tables)
            for item in (conversation_history or [])[-5:]
            if str(item).strip()
        ]

        if safe_history:
            safe_question = (
                "The following are previous USER QUESTIONS only. Use them "
                "only to resolve references in the CURRENT QUESTION. Do not "
                "answer previous questions again or treat them as new requests. "
                "If the current question is a short follow-up, use the history "
                "to resolve its omitted subject, field, or method. The CURRENT "
                "QUESTION is the request to execute.\n\n"
                "PREVIOUS USER QUESTIONS:\n"
                + "\n".join(f"- {item}" for item in safe_history)
                + "\n\nCURRENT QUESTION:\n"
                + safe_question
            )

        for alias, original in sorted(
            table_aliases.items(),
            key=lambda item: len(str(item[1])),
            reverse=True,
        ):
            safe_question = re.sub(
                re.escape(str(original)),
                alias,
                safe_question,
                flags=re.I,
            )

        prompt = json.dumps(
            {
                "question": safe_question,
                "schema": schema,
            },
            ensure_ascii=False,
        )

        model = os.getenv(
            "GEMINI_MODEL",
            "gemini-3.5-flash-lite",
        ).strip()

        response = _call_gemini_for_plan(client, prompt, model)
        calls = list(getattr(response, "function_calls", None) or [])

        if (
            len(calls) != 1
            or getattr(calls[0], "name", None)
            != "plan_healthcare_analysis"
        ):
            return _failure(
                "Gemini did not return exactly one valid analysis plan. "
                "Please rephrase the question.",
                "invalid_llm_plan",
            )

        raw_args = getattr(calls[0], "args", None)

        if isinstance(raw_args, str):
            try:
                plan = json.loads(raw_args)
            except json.JSONDecodeError:
                return _failure(
                    "Gemini returned an invalid analysis plan.",
                    "invalid_llm_plan",
                )
        elif isinstance(raw_args, Mapping):
            plan = dict(raw_args)
        else:
            return _failure(
                "Gemini returned an invalid analysis plan.",
                "invalid_llm_plan",
            )

        for key in ("table", "measure", "field", "aggregation"):
            if plan.get(key) == "":
                plan[key] = None

        requested_alias = plan.get("table")

        if requested_alias in table_aliases:
            plan["table"] = table_aliases[requested_alias]
        elif requested_alias is not None:
            return _failure(
                "Gemini selected a table that is not in the current workspace.",
                "invalid_llm_plan",
            )

        result = _execute_plan(plan, tables)

        result.setdefault("query_plan", {})["model"] = model
        result["query_plan"]["planner"] = "gemini_function_calling"

        return result

    except Exception as exc:
        LOGGER.exception(
            "Gemini analyst request failed (%s)",
            type(exc).__name__,
        )

        return {
            "answer": (
                "The Gemini AI analyst could not complete this request. "
                "No new analysis was returned. Check the Gemini API key, "
                "model availability, API quota, and network connection."
            ),
            "sources": [],
            "executed": False,
            "error": "llm_unavailable",
            "query_plan": {
                "operation": "llm_unavailable",
                "provider": "gemini",
            },
        }
