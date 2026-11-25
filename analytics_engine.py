
"""Deterministic, data-grounded natural-language analytics for HealthOps.

This module converts a constrained set of natural-language questions into a
validated in-memory query plan. It never executes generated SQL or Python,
never mutates source DataFrames, and returns None when intent cannot be mapped
confidently so the caller can use its existing fallback/clarification path.
"""
from __future__ import annotations

from dataclasses import dataclass
import re
from typing import Any, Mapping

import pandas as pd


# Do not expose patient-level identifiers in answers or group on them.
_SENSITIVE_FIELD_RE = re.compile(
    r"patient|mrn|medical.?record|ssn|email|phone|address|birth|dob|"
    r"(^|[^a-z])(patient.?name|first.?name|last.?name|full.?name)([^a-z]|$)",
    re.I,
)
_IDENTIFIER_FIELD_RE = re.compile(
    r"(^|[^a-z])(id|npi|zip|postal|code)([^a-z]|$)|"
    r"provider.?id|patient.?id|encounter.?id|claim.?id|member.?id",
    re.I,
)
_NUMERIC_ALIASES = {
    "charge": r"charge|billing|billed",
    "payment": r"payment|paid|reimbursement",
    "cost": r"cost|expense",
    "amount": r"amount|value",
    "fee": r"fee|tariff",
    "revenue": r"revenue|income|charge|payment",
    "admission": r"admission|admissions",
    "visit": r"visit|visits|encounter|encounters",
    "claim": r"claim|claims",
    "age": r"age",
    "length of stay": r"length.?of.?stay|los",
    "waiting time": r"wait(?:ing)?.?time|wait.?minutes",
}
_DIMENSION_ALIASES = {
    "city": r"city|town",
    "location": r"location|city|town|site",
    "hospital": r"hospital|facility|provider",
    "department": r"department|speciality|specialty|clinic|ward",
    "diagnosis": r"diagnos|condition|disease|icd",
    "payer": r"payer|insurance|insurer",
    "insurance": r"insurance|payer|insurer",
    "state": r"state|region",
    "status": r"status",
    "medicine": r"medic|drug|pharmacy",
    "staff": r"staff|employee|clinician|nurse|doctor",
}


@dataclass(frozen=True)
class Field:
    table: str
    name: str
    kind: str
    sensitive: bool
    non_measure: bool


@dataclass(frozen=True)
class Filter:
    field: str
    value: str


def _has_phrase(text: str, phrase: str) -> bool:
    phrase = str(phrase).strip()
    if not phrase:
        return False
    return bool(re.search(
        r"(?<![a-z0-9])" + re.escape(phrase) + r"(?![a-z0-9])",
        text,
        re.I,
    ))


def _format(value: Any) -> str:
    try:
        number = float(value)
        if pd.isna(number):
            return "—"
        if number.is_integer():
            return f"{int(number):,}"
        return f"{number:,.2f}".rstrip("0").rstrip(".")
    except (TypeError, ValueError, OverflowError):
        return str(value)


def _numeric(series: pd.Series) -> pd.Series:
    """Interpret numeric-like source text for analysis without mutating it."""
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


def _field_kind(series: pd.Series, name: str) -> str:
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

    if re.search(r"date|time|timestamp|month|year", name, re.I) and nonnull:
        parsed_dates = pd.to_datetime(series, errors="coerce")
        if int(parsed_dates.notna().sum()) / nonnull >= 0.80:
            return "date"

    return "text"


def _is_sensitive(name: str) -> bool:
    return bool(_SENSITIVE_FIELD_RE.search(name))


def _is_non_measure(name: str) -> bool:
    return bool(_IDENTIFIER_FIELD_RE.search(name)) or _is_sensitive(name)


def _catalog(tables: Mapping[str, pd.DataFrame]) -> list[Field]:
    result: list[Field] = []

    for table_name, dataframe in tables.items():
        if not isinstance(dataframe, pd.DataFrame):
            continue

        for column in dataframe.columns:
            name = str(column)
            series = dataframe[column]

            # Duplicate column labels can make pandas return a DataFrame here.
            # Skip them rather than misclassifying or crashing the query planner.
            if not isinstance(series, pd.Series):
                continue

            result.append(Field(
                table=str(table_name),
                name=name,
                kind=_field_kind(series, name),
                sensitive=_is_sensitive(name),
                non_measure=_is_non_measure(name),
            ))

    return result


def _alias_candidates(fields: list[Field], aliases: Mapping[str, str],
                      question: str, kind: str) -> list[Field]:

    inflections = {
        "city": r"\bcit(?:y|ies)\b",
        "diagnosis": r"\bdiagnos(?:is|es)\b",
        "hospital": r"\bhospitals?\b",
        "department": r"\bdepartments?\b",
        "provider": r"\bproviders?\b",
    }

    wanted = [
        alias for alias in aliases
        if re.search(
            inflections.get(
                alias,
                r"(?<![a-z0-9])" + re.escape(alias) + r"s?(?![a-z0-9])"
            ),
            question,
            re.I,
        )
    ]

    candidates: list[Field] = []

    for field in fields:
        if field.kind != kind or field.sensitive:
            continue

        if kind == "numeric" and field.non_measure:
            continue

        if any(re.search(aliases[alias], field.name, re.I) for alias in wanted):
            candidates.append(field)

    return candidates


def _mentioned_fields(fields: list[Field], question: str) -> list[Field]:
    return [
        field for field in fields
        if _has_phrase(question, field.name)
    ]


def _choose_table(tables: Mapping[str, pd.DataFrame],
                  fields: list[Field],
                  mentioned: list[Field],
                  question: str) -> tuple[str | None, str | None]:
    """Return a table or a user-facing clarification reason."""

    named_tables = [
        name for name in tables
        if _has_phrase(question, str(name))
    ]

    if len(named_tables) == 1:
        return named_tables[0], None

    if len(named_tables) > 1:
        return None, "Your question names more than one table. Please specify one table."

    if mentioned:
        relevant_names = {
            field.table for field in mentioned
        }

        if len(relevant_names) == 1:
            return next(iter(relevant_names)), None

        # Prefer a table containing both a mentioned text dimension and measure.
        mentioned_names = {
            (item.table, item.name) for item in mentioned
        }

        matching = []

        for name in tables:
            local = [
                field for field in fields
                if field.table == name
            ]

            has_dimension = any(
                (field.table, field.name) in mentioned_names
                and field.kind == "text"
                for field in local
            )

            has_measure = any(
                (field.table, field.name) in mentioned_names
                and field.kind == "numeric"
                and not field.non_measure
                for field in local
            )

            if has_dimension and has_measure:
                matching.append(name)

        if len(matching) == 1:
            return matching[0], None

        return None, (
            "I found matching fields in multiple tables. Please name the table "
            "or identify which source should be analysed."
        )

    if len(tables) == 1:
        return next(iter(tables)), None

    return None, None


def _select_field(local_fields: list[Field],
                  mentioned: list[Field],
                  kind: str,
                  aliases: Mapping[str, str],
                  question: str) -> tuple[Field | None, bool]:

    exact = [
        field for field in mentioned
        if field in local_fields
        and field.kind == kind
        and not field.sensitive
    ]

    if kind == "numeric":
        exact = [
            field for field in exact
            if not field.non_measure
        ]

    if len(exact) == 1:
        return exact[0], False

    if len(exact) > 1:
        return None, True

    aliases_found = _alias_candidates(
        local_fields,
        aliases,
        question,
        kind,
    )

    if len(aliases_found) == 1:
        return aliases_found[0], False

    if len(aliases_found) > 1:
        return None, True

    return None, False


def _detect_filters(dataframe: pd.DataFrame,
                    fields: list[Field],
                    question: str) -> tuple[list[Filter], str | None]:
    """Infer simple equality filters only when a value and filter preposition exist."""

    if re.search(r"\bfor\s+(?:each|every)\b", question, re.I):
        question_for_filter = re.sub(
            r"\bfor\s+(?:each|every)\b",
            " ",
            question,
            flags=re.I,
        )
    else:
        question_for_filter = question

    if not re.search(
        r"\b(?:where|in|at|for)\b|(?:=|\bis\b|\bequals\b)",
        question_for_filter,
        re.I,
    ):
        return [], None

    candidates: list[tuple[int, Field, str]] = []

    for field in fields:
        if (
            field.kind != "text"
            or field.sensitive
            or field.name not in dataframe.columns
        ):
            continue

        if len(dataframe) > 250_000:
            # Avoid materialising a potentially huge distinct-value list for
            # automatic filter discovery. Explicit filter planning comes later.
            continue

        values = (
            dataframe[field.name]
            .dropna()
            .astype(str)
            .str.strip()
            .unique()
        )

        if len(values) > 5000:
            continue

        for value in values:
            value = str(value).strip()

            if (
                len(value) < 2
                or value.casefold() in {"unknown", "none", "null", "nan"}
            ):
                continue

            if _has_phrase(question_for_filter, value):
                candidates.append((len(value), field, value))

    if not candidates:
        return [], None

    # De-duplicate, then discard short matches contained in a longer exact
    # category value (for example "City" inside "City Hospital").
    distinct = {
        (item[1].name, item[2].casefold()): item
        for item in candidates
    }

    candidates = list(distinct.values())

    candidates = [
        item for item in candidates
        if not any(
            len(other[2]) > len(item[2])
            and item[2].casefold() in other[2].casefold()
            for other in candidates
        )
    ]

    if len(candidates) > 1:
        return [], (
            "I detected more than one possible category filter. This version "
            "supports one inferred equality filter at a time; specify one filter "
            "or use the exact field and value in a single condition."
        )

    _, field, value = candidates[0]

    return [Filter(field=field.name, value=value)], None


def _aggregation(question: str,
                 mentioned: list[Field]) -> str | None:

    intent = question

    for field in sorted(
        mentioned,
        key=lambda item: len(item.name),
        reverse=True,
    ):
        intent = re.sub(
            re.escape(field.name),
            " ",
            intent,
            flags=re.I,
        )

    if re.search(r"\bmedian\b", intent):
        return "median"

    if re.search(r"\b(mean|average|avg)\b", intent):
        return "mean"

    if re.search(r"\b(minimum|min|lowest|smallest)\b", intent):
        return "min"

    if re.search(r"\b(maximum|max|highest|largest)\b", intent):
        return "max"

    if re.search(r"\b(sum|total|combined|aggregate)\b", intent):
        return "sum"

    if re.search(r"\b(count|number of)\b", intent):
        return "count"

    return None


def _rank_limit(question: str) -> int:
    match = re.search(
        r"\b(?:top|bottom)\s+(\d{1,2})\b",
        question,
        re.I,
    )

    return max(1, min(int(match.group(1)), 50)) if match else 10


def _row_count_intent(question: str) -> bool:
    return bool(re.search(
        r"\b(how many|number of|count of|record count|row count|total records|"
        r"total rows|how many records|how many rows)\b",
        question,
        re.I,
    ))


def _distinct_intent(question: str) -> bool:
    return bool(
        re.search(r"\b(distinct|unique|different)\b", question, re.I)
        or re.search(
            r"\b(?:how many|total recorded) (?:cities|towns|locations|regions|states|"
            r"hospitals|providers|diagnoses|departments)\b",
            question,
            re.I,
        )
    )


def _group_intent(question: str) -> bool:
    # Do not mistake phrases such as "reduce costs by 20%" for a group-by.
    return bool(re.search(
        r"\b(?:per|for each|across|group(?:ed)? by)\b|\bby(?!\s*\d)",
        question,
        re.I,
    ))


def _rank_intent(question: str) -> bool:
    return bool(re.search(
        r"\b(top|bottom|highest|lowest|largest|smallest|most common)\b",
        question,
        re.I,
    ))


def answer_question(question: str,
                    tables: Mapping[str, pd.DataFrame],
                    analysis: Mapping[str, Any] | None = None
                    ) -> dict[str, Any] | None:
    """Plan and execute a supported question; return None when not confidently mapped."""

    question = str(question or "").strip()

    if not question or not tables:
        return None

    fields = _catalog(tables)
    mentioned = _mentioned_fields(fields, question)

    table, table_error = _choose_table(
        tables,
        fields,
        mentioned,
        question,
    )

    if table_error:
        return {
            "answer": table_error,
            "sources": [],
            "goal": (analysis or {}).get("goal"),
        }

    if table is None:
        return None

    dataframe = tables[table]

    local_fields = [
        field for field in fields
        if field.table == table
    ]

    local_mentioned = [
        field for field in mentioned
        if field.table == table
    ]

    # Infer the dimension and measure from exact names first, then conservative aliases.
    dimension, dimension_ambiguous = _select_field(
        local_fields,
        local_mentioned,
        "text",
        _DIMENSION_ALIASES,
        question,
    )

    measure, measure_ambiguous = _select_field(
        local_fields,
        local_mentioned,
        "numeric",
        _NUMERIC_ALIASES,
        question,
    )

    if dimension_ambiguous or measure_ambiguous:
        return {
            "answer": (
                "I found more than one possible field for that calculation. "
                "Please specify the exact category and/or numeric field names."
            ),
            "sources": [],
            "goal": (analysis or {}).get("goal"),
        }

    distinct = _distinct_intent(question)
    row_count = _row_count_intent(question)
    grouped = _group_intent(question)
    ranked = _rank_intent(question)
    statistic = _aggregation(question, local_mentioned)

    # Infer category from common plural phrasing such as "how many cities".
    if dimension is None and (
        distinct or row_count or ranked or grouped
    ):
        alias_dimension, ambiguous = _select_field(
            local_fields,
            [],
            "text",
            _DIMENSION_ALIASES,
            question,
        )

        if ambiguous:
            return {
                "answer": "Please specify which category field you want to analyse.",
                "sources": [],
                "goal": (analysis or {}).get("goal"),
            }

        dimension = alias_dimension

    # Ranking defaults to summing the selected measure, with an explicit
    # additivity warning in the result. Other grouped metrics require a stated
    # aggregation so the engine does not silently choose mean versus sum.
    if grouped and measure and dimension is None:
        return {
            "answer": (
                "I found a numeric measure but no safe category field for the "
                "requested grouping. Specify a non-identifying category field."
            ),
            "sources": [],
            "goal": (analysis or {}).get("goal"),
        }

    if ranked and dimension and measure:
        operation = "rank"

        if statistic is None:
            statistic = (
                "mean"
                if re.search(
                    r"\b(average|avg|mean|rate|ratio|percent|percentage)\b",
                    measure.name,
                    re.I,
                )
                else "sum"
            )

    elif distinct and dimension:
        operation = "distinct"

    elif row_count and dimension and not measure:
        operation = "group_count" if grouped or ranked else "row_count"

    elif row_count and not measure:
        operation = "row_count"

    elif dimension and measure and grouped:
        if statistic is None:
            return {
                "answer": (
                    f"I can group {measure.name} by {dimension.name}, but the "
                    "aggregation is ambiguous. Please specify sum, average, "
                    "median, minimum or maximum."
                ),
                "sources": [],
                "goal": (analysis or {}).get("goal"),
            }

        operation = "group_aggregate"

    elif measure and statistic:
        operation = "aggregate"

    elif dimension and (grouped or ranked):
        operation = "group_count"

    else:
        # Let the existing handler answer other established question types.
        return None

    # A bare "how many cities" is a distinct-value question, not a row count.
    if (
        row_count
        and dimension
        and re.search(
            r"\b(?:cities|towns|locations|regions|states|hospitals|providers|"
            r"departments|diagnoses)\b",
            question,
            re.I,
        )
        and not re.search(r"\b(?:records|rows)\b", question, re.I)
    ):
        operation = "distinct"

    filters, filter_error = _detect_filters(
        dataframe,
        local_fields,
        question,
    )

    if filter_error:
        return {
            "answer": filter_error,
            "sources": [],
            "goal": (analysis or {}).get("goal"),
        }

    goal = (analysis or {}).get("goal") or ""

    return _execute_with_statistic(
        dataframe,
        table,
        operation,
        measure,
        dimension,
        filters,
        _rank_limit(question),
        question,
        goal,
        statistic,
    )


def _execute_with_statistic(dataframe: pd.DataFrame,
                            table: str,
                            operation: str,
                            measure: Field | None,
                            dimension: Field | None,
                            filters: list[Filter],
                            limit: int,
                            question: str,
                            goal: str,
                            statistic: str | None) -> dict[str, Any]:
    """Execute a validated plan and return aggregate-only evidence."""

    filtered = dataframe

    for item in filters:
        values = (
            filtered[item.field]
            .astype("string")
            .str.strip()
            .str.casefold()
        )
        filtered = filtered.loc[values == item.value.casefold()]

    rows_before = len(dataframe)
    rows_after = len(filtered)

    sources = []

    if dimension:
        sources.append(f"{table}.{dimension.name}")

    if measure:
        sources.append(f"{table}.{measure.name}")

    sources.extend(
        f"{table}.{item.field}"
        for item in filters
    )

    sources = list(dict.fromkeys(sources))

    if (
        statistic == "sum"
        and measure is not None
        and re.search(
            r"\b(average|avg|mean|rate|ratio|percent|percentage)\b",
            measure.name,
            re.I,
        )
    ):
        return {
            "answer": (
                f"{measure.name} in {table} appears to be an average, rate or "
                "ratio-valued field. Summing those row values would not produce "
                "a valid total. Ask for an average or select a source field "
                "documented as additive."
            ),
            "sources": [f"{table}.{measure.name}"],
            "goal": goal,
            "query_plan": {
                "table": table,
                "operation": "blocked_unsafe_sum",
                "measure": measure.name,
                "dimension": dimension.name if dimension else None,
                "statistic": statistic,
                "filters": [],
            },
        }

    plan = {
        "table": table,
        "operation": operation,
        "measure": measure.name if measure else None,
        "dimension": dimension.name if dimension else None,
        "statistic": statistic,
        "filters": [
            {
                "field": item.field,
                "operator": "equals",
                "value": item.value,
            }
            for item in filters
        ],
        "limit": (
            limit
            if operation in {"rank", "group_count", "group_aggregate"}
            else None
        ),
        "rows_before_filter": rows_before,
        "rows_after_filter": rows_after,
        "rows_excluded_by_filter": rows_before - rows_after,
        "source_fields": sources,
    }

    if operation == "aggregate":
        if statistic is None:
            raise ValueError("The analytical plan is missing an aggregation.")

        values = _numeric(filtered[measure.name])
        valid = int(values.notna().sum())
        missing = int(len(values) - valid)

        if not valid:
            answer = (
                f"{table}.{measure.name} has no interpretable numeric values "
                "after filtering."
            )
            plan.update({
                "valid_numeric_values": 0,
                "missing_or_unparsed_values": missing,
            })
        else:
            value = {
                "mean": values.mean,
                "median": values.median,
                "sum": values.sum,
                "min": values.min,
                "max": values.max,
                "count": values.count,
            }[statistic]()

            label = {
                "mean": "average",
                "median": "median",
                "sum": "sum",
                "min": "minimum",
                "max": "maximum",
                "count": "count",
            }[statistic]

            answer = (
                f"{label} {measure.name} in {table}: {_format(value)} across "
                f"{valid:,} non-missing numeric values "
                f"(of {rows_after:,} matching rows)."
            )

            if statistic == "sum":
                answer += (
                    " Treat this as a total only if the source field is "
                    "additive at the row level."
                )

            plan.update({
                "value": float(value),
                "valid_numeric_values": valid,
                "missing_or_unparsed_values": missing,
            })

    elif operation in {"group_aggregate", "rank"}:
        if statistic is None:
            raise ValueError("The analytical plan is missing an aggregation.")

        values = _numeric(filtered[measure.name])
        categories = (
            filtered[dimension.name]
            .astype("string")
            .fillna("Unknown")
        )

        work = pd.DataFrame({
            "category": categories,
            "value": values,
        }, index=filtered.index)

        work = work.dropna(subset=["value"])
        valid = len(work)
        missing = len(filtered) - valid

        if not valid:
            answer = (
                f"No numeric {measure.name} values were available after filtering."
            )
            plan.update({
                "valid_numeric_values": 0,
                "missing_or_unparsed_values": missing,
            })
        else:
            grouped = (
                work.groupby("category", dropna=False)["value"]
                .agg(statistic)
            )

            counts = (
                work.groupby("category", dropna=False)["value"]
                .count()
            )

            ascending = bool(re.search(
                r"\b(bottom|lowest|smallest)\b",
                question,
                re.I,
            ))

            grouped = grouped.sort_values(
                ascending=ascending,
                kind="stable",
            )

            if operation == "rank":
                selected = grouped.head(limit)

                # Retain every group tied at the ranking boundary.
                if len(grouped) > len(selected) and len(selected):
                    boundary = selected.iloc[-1]
                    tied = grouped[grouped == boundary]

                    selected = pd.concat([
                        selected,
                        tied.loc[~tied.index.isin(selected.index)],
                    ])

                    selected = selected.iloc[:50]
            else:
                selected = grouped

            label = {
                "mean": "average",
                "median": "median",
                "sum": "sum",
                "min": "minimum",
                "max": "maximum",
                "count": "count",
            }[statistic]

            answer = (
                f"{label.title()} {measure.name} by {dimension.name} in {table} "
                "(non-missing numeric values only): "
                + "; ".join(
                    f"{key} = {_format(value)} (n={int(counts[key]):,})"
                    for key, value in selected.items()
                )
                + f". Excluded {missing:,} rows with missing or non-numeric "
                  "measure values."
            )

            if statistic == "sum":
                answer += (
                    " Summed values are meaningful as totals only when "
                    "the measure is additive."
                )

            plan.update({
                "groups_returned": len(selected),
                "groups_available": len(grouped),
                "valid_numeric_values": valid,
                "missing_or_unparsed_values": missing,
            })

    elif operation == "distinct":
        series = filtered[dimension.name]
        count = int(series.nunique(dropna=True))
        nonmissing = int(series.notna().sum())
        missing = len(series) - nonmissing

        answer = (
            f"Distinct {dimension.name} values in {table}: {count:,}, "
            f"counted across {nonmissing:,} non-missing rows "
            f"({missing:,} missing values excluded). "
            f"Operation: exact distinct count; source: {table}.{dimension.name}."
        )

        plan.update({
            "result_count": count,
            "non_missing_values": nonmissing,
            "missing_values_excluded": missing,
        })

    elif operation == "row_count":
        answer = (
            f"Matching record count in {table}: {rows_after:,} rows."
        )

        if filters:
            answer += " Applied filters: " + ", ".join(
                f"{item.field} = {item.value}"
                for item in filters
            ) + "."

        plan["result_count"] = rows_after

    elif operation == "group_count":
        grouped = (
            filtered[dimension.name]
            .astype("string")
            .fillna("Unknown")
            .value_counts(dropna=False)
        )

        ascending = bool(re.search(
            r"\b(bottom|lowest|smallest)\b",
            question,
            re.I,
        ))

        grouped = grouped.sort_values(
            ascending=ascending,
            kind="stable",
        ).head(limit)

        answer = (
            f"Record count by {dimension.name} in {table}: "
            + "; ".join(
                f"{key} = {int(value):,}"
                for key, value in grouped.items()
            )
            + ". Missing category values are grouped as Unknown."
        )

        plan.update({
            "groups_returned": len(grouped),
        })

    else:
        raise ValueError("Unsupported analytical operation.")

    return {
        "answer": answer,
        "sources": sources,
        "goal": goal,
        "query_plan": plan,
    }
