#!/usr/bin/env python3
"""
Backfill cleaned taxonomy labels into STAGE fact_call_classification.

Purpose:
- Read existing approved taxonomy mappings from the local taxonomy DB.
- Read messy historical labels from STAGE fact_call_classification.
- Replace selected fields with clean taxonomy display names.
- Set better_tags_updated_at = NOW() only when at least one selected field changes.
- Run in dry-run mode by default.
- Support parallel workers for faster coverage/backfill.

Important:
- This script does NOT recluster labels.
- This script does NOT create new clusters.
- This script does NOT create anomalies.
- This script does NOT push anything to Vespa.
- This script does NOT modify Iris.

Main behavior:
- Single-value fields map directly against taxonomy_label_cluster_map.raw_label /
  normalized_label, then taxonomy_cluster_names.display_name.
- Composite array fields like next_step, outcome_sub, call_type_sub, and
  main_reason_sub are checked as whole arrays first.
  Example:
      STAGE next_step = ['Try_Later']
      taxonomy raw_label = '{Try_Later}'
      display_name = 'Retry After Research'
      output next_step = ['Retry After Research']
- Item-level array fields like additional_tags, descriptive_keywords, and
  coaching_tags are cleaned item by item.
- Placeholder labels such as NA are left unchanged and not counted as unmapped.
- Optional supplemental mapping CSV can recover safe same-field mappings from
  unresolved_match_audit.csv.

Required env vars can be either:
    LOCAL_DATABASE_URL
    STAGE_DATABASE_URL

or your existing env format:
    LOCAL_PG_HOST, LOCAL_PG_PORT, LOCAL_PG_DB, LOCAL_PG_USER, LOCAL_PG_PASSWORD
    DWH_HOST, DWH_PORT, DWH_NAME, DWH_USER, DWH_PASS

Examples:

Small dry run:
    python backfill_clean_taxonomy_to_stage.py --dry-run true --workers 1 --limit 1000 --log-level INFO

Small dry run with supplemental mappings:
    python backfill_clean_taxonomy_to_stage.py --dry-run true --workers 1 --limit 1000 --audit-dir audit_after_recovery_test --supplemental-mapping-csv unresolved_match_audit.csv --log-level INFO

Full coverage dry run:
    python backfill_clean_taxonomy_to_stage.py --dry-run true --workers 10 --batch-size 10000 --audit-dir audit_after_recovery_full --supplemental-mapping-csv unresolved_match_audit.csv --log-level INFO

Real run:
    python backfill_clean_taxonomy_to_stage.py --dry-run false --workers 10 --batch-size 10000 --update-page-size 500 --supplemental-mapping-csv unresolved_match_audit.csv --log-level INFO
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
import sys
import time
from collections import Counter
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import psycopg2
import psycopg2.extras

try:
    from dotenv import load_dotenv

    load_dotenv()
except Exception:
    pass


DEFAULT_FIELDS = [
    "call_type",
    "call_type_sub",
    "outcome",
    "outcome_sub",
    "main_reason",
    "main_reason_sub",
    "next_step",
    "additional_tags",
    "descriptive_keywords",
    "coaching_tags",
]


MULTI_VALUE_FIELDS = {
    "call_type_sub",
    "outcome_sub",
    "main_reason_sub",
    "next_step",
    "additional_tags",
    "descriptive_keywords",
    "coaching_tags",
}



COMPOSITE_ARRAY_FIELDS = set()


DEFAULT_ACTIVE_RUN_IDS = {
    "additional_tags": "20260513_093749",
    "call_type": "20260508_161539",
    "call_type_sub": "20260512_124311",
    "coaching_tags": "20260508_120119",
    "descriptive_keywords": "20260512_142335",
    "main_reason": "20260511_141942",
    "main_reason_sub": "20260512_113801",
    "next_step": "20260508_155211",
    "outcome": "20260508_152217",
    "outcome_sub": "20260512_103057",
}


AMBIGUOUS = "__AMBIGUOUS_MAPPING__"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


IGNORED_UNMAPPED_LABELS = {
    "na",
    "n a",
    "n/a",
    "none",
    "null",
    "nan",
}


SUPPLEMENTAL_SAFE_STATUSES = {
    "SAME_FIELD_RAW_OR_NORMALIZED_EXACT",
    "SAME_FIELD_RAW_OR_NORMALIZED_LOOSE",
    "SAME_FIELD_DISPLAY_NAME_MATCH",
}


@dataclass
class FieldLookup:
    exact: Dict[str, str] = field(default_factory=dict)
    loose: Dict[str, str] = field(default_factory=dict)
    exact_conflicts: int = 0
    loose_conflicts: int = 0


@dataclass
class WorkerResult:
    worker_id: int
    rows_scanned: int = 0
    rows_changed: int = 0
    rows_unchanged: int = 0
    rows_error: int = 0
    update_batches: int = 0
    unmapped: Counter = field(default_factory=Counter)
    changed_samples: List[Dict[str, Any]] = field(default_factory=list)


def build_local_database_url_from_env() -> Optional[str]:
    host = os.getenv("LOCAL_PG_HOST")
    port = os.getenv("LOCAL_PG_PORT")
    db = os.getenv("LOCAL_PG_DB")
    user = os.getenv("LOCAL_PG_USER")
    password = os.getenv("LOCAL_PG_PASSWORD")

    if not all([host, port, db, user, password]):
        return None

    return f"host={host} port={port} dbname={db} user={user} password={password}"


def build_stage_database_url_from_env() -> Optional[str]:
    host = os.getenv("DWH_HOST")
    port = os.getenv("DWH_PORT")
    db = os.getenv("DWH_NAME")
    user = os.getenv("DWH_USER")
    password = os.getenv("DWH_PASS")

    if not all([host, port, db, user, password]):
        return None

    return f"host={host} port={port} dbname={db} user={user} password={password}"


def parse_bool(value: str | bool) -> bool:
    if isinstance(value, bool):
        return value

    value = value.strip().lower()

    if value in {"true", "1", "yes", "y"}:
        return True

    if value in {"false", "0", "no", "n"}:
        return False

    raise argparse.ArgumentTypeError(f"Invalid boolean value: {value}")


def assert_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.match(value):
        raise ValueError(f"Invalid {label}: {value}")

    return value


def split_csv_arg(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def connect(dsn: str):
    return psycopg2.connect(dsn)


def get_table_columns(conn, schema: str, table: str) -> Dict[str, Dict[str, str]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT column_name, data_type, udt_name
            FROM information_schema.columns
            WHERE table_schema = %s
              AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        rows = cur.fetchall()

    return {
        row["column_name"]: {
            "data_type": row["data_type"],
            "udt_name": row["udt_name"],
        }
        for row in rows
    }


def table_exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s
                  AND table_name = %s
            )
            """,
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def strip_wrapping_quotes(text: str) -> str:
    text = text.strip()

    if len(text) >= 2:
        if text[0] == text[-1] and text[0] in {"'", '"'}:
            return text[1:-1].strip()

    return text


def normalize_exact(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    text = strip_wrapping_quotes(text)
    text = text.strip()

    if not text:
        return None

    return text.lower()


def normalize_loose(value: Any) -> Optional[str]:
    if value is None:
        return None

    text = str(value).strip()

    if not text:
        return None

    text = strip_wrapping_quotes(text)
    text = text.replace("_", " ")
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"\s+", " ", text)
    text = text.strip().lower()

    if not text:
        return None

    return text


def is_ignored_unmapped_label(value: Any) -> bool:
    key = normalize_loose(value)
    return bool(key and key in IGNORED_UNMAPPED_LABELS)


def add_mapping_value(target: Dict[str, str], key: Optional[str], value: str) -> bool:
    if not key:
        return False

    existing = target.get(key)

    if existing is None:
        target[key] = value
        return False

    if existing == value:
        return False

    target[key] = AMBIGUOUS
    return True


def force_mapping_value(target: Dict[str, str], key: Optional[str], value: str) -> bool:
    if not key:
        return False

    existing = target.get(key)

    if existing == value:
        return False

    target[key] = value
    return True


def lookup_clean_value(
    field_name: str,
    raw_value: str,
    lookups: Dict[str, FieldLookup],
) -> Optional[str]:
    field_lookup = lookups.get(field_name)

    if not field_lookup:
        return None

    exact_key = normalize_exact(raw_value)

    if exact_key:
        exact_value = field_lookup.exact.get(exact_key)

        if exact_value and exact_value != AMBIGUOUS:
            return exact_value

    loose_key = normalize_loose(raw_value)

    if loose_key:
        loose_value = field_lookup.loose.get(loose_key)

        if loose_value and loose_value != AMBIGUOUS:
            return loose_value

    return None


def load_supplemental_mappings(
    supplemental_csv: Optional[str],
    lookups: Dict[str, FieldLookup],
) -> Dict[str, Any]:
    stats: Dict[str, Any] = {
        "enabled": bool(supplemental_csv),
        "path": supplemental_csv,
        "rows_read": 0,
        "rows_applied": 0,
        "rows_skipped": 0,
        "forced_keys": 0,
        "statuses_used": sorted(SUPPLEMENTAL_SAFE_STATUSES),
    }

    if not supplemental_csv:
        return stats

    if not os.path.exists(supplemental_csv):
        raise FileNotFoundError(f"Supplemental mapping CSV not found: {supplemental_csv}")

    with open(supplemental_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)

        required = {
            "field_name",
            "label",
            "status",
            "matched_field_name",
            "matched_raw_label",
            "matched_normalized_label",
            "matched_display_name",
        }

        missing = required - set(reader.fieldnames or [])

        if missing:
            raise RuntimeError(f"Supplemental CSV missing required columns: {sorted(missing)}")

        for row in reader:
            stats["rows_read"] += 1

            status = str(row.get("status") or "").strip()
            field_name = str(row.get("field_name") or "").strip()
            matched_field_name = str(row.get("matched_field_name") or "").strip()
            display_name = str(row.get("matched_display_name") or "").strip()

            if status not in SUPPLEMENTAL_SAFE_STATUSES:
                stats["rows_skipped"] += 1
                continue

            if not field_name or not matched_field_name or field_name != matched_field_name:
                stats["rows_skipped"] += 1
                continue

            if field_name not in lookups:
                stats["rows_skipped"] += 1
                continue

            if not display_name:
                stats["rows_skipped"] += 1
                continue

            lookup = lookups[field_name]

            candidates = [
                row.get("label"),
                row.get("matched_raw_label"),
                row.get("matched_normalized_label"),
                display_name,
            ]

            for candidate in candidates:
                if candidate is None:
                    continue

                stats["forced_keys"] += int(
                    force_mapping_value(
                        lookup.exact,
                        normalize_exact(candidate),
                        display_name,
                    )
                )

                stats["forced_keys"] += int(
                    force_mapping_value(
                        lookup.loose,
                        normalize_loose(candidate),
                        display_name,
                    )
                )

            stats["rows_applied"] += 1

    logging.info("Supplemental mappings applied: %s", stats)
    return stats


def load_taxonomy_lookups(
    local_dsn: str,
    local_schema: str,
    map_table: str,
    names_table: str,
    selected_fields: Sequence[str],
    active_run_ids: Dict[str, str],
    include_anomaly_names: bool,
) -> Tuple[Dict[str, FieldLookup], Dict[str, Any]]:
    logging.info("Loading taxonomy mappings from local DB")

    conn = connect(local_dsn)
    conn.autocommit = True

    try:
        if not table_exists(conn, local_schema, map_table):
            raise RuntimeError(f"Local mapping table not found: {local_schema}.{map_table}")

        map_columns = get_table_columns(conn, local_schema, map_table)

        names_columns = (
            get_table_columns(conn, local_schema, names_table)
            if table_exists(conn, local_schema, names_table)
            else {}
        )

        required_map_cols = {"field_name", "raw_label", "final_cluster_id"}
        missing = required_map_cols - set(map_columns)

        if missing:
            raise RuntimeError(f"Missing columns in {map_table}: {sorted(missing)}")

        has_names_table = bool(names_columns)
        has_map_display_name = "display_name" in map_columns
        has_normalized_label = "normalized_label" in map_columns
        has_map_run_id = "run_id" in map_columns
        has_names_run_id = "run_id" in names_columns
        has_names_cluster_version = "cluster_version" in names_columns
        has_names_is_anomaly = "is_anomaly" in names_columns
        has_names_active = "active" in names_columns
        has_names_is_active = "is_active" in names_columns

        if not has_names_table and not has_map_display_name:
            raise RuntimeError(
                f"No {names_table} table found and {map_table}.display_name does not exist. "
                "Cannot derive clean display names."
            )

        selected_fields = list(selected_fields)

        select_parts = [
            "m.field_name",
            "m.raw_label",
            "m.final_cluster_id",
        ]

        if has_normalized_label:
            select_parts.append("m.normalized_label")
        else:
            select_parts.append("NULL::text AS normalized_label")

        if has_names_table:
            if has_map_display_name:
                select_parts.append("COALESCE(n.display_name, m.display_name) AS clean_display_name")
            else:
                select_parts.append("n.display_name AS clean_display_name")
        else:
            select_parts.append("m.display_name AS clean_display_name")

        query = [
            f"SELECT {', '.join(select_parts)}",
            f"FROM {local_schema}.{map_table} m",
        ]

        if has_names_table:
            join_conditions = [
                "n.field_name = m.field_name",
                "n.cluster_id = m.final_cluster_id",
            ]

            if has_map_run_id and has_names_run_id:
                join_conditions.append("n.run_id = m.run_id")

            if has_names_active:
                join_conditions.append("COALESCE(n.active, TRUE) = TRUE")

            if has_names_is_active:
                join_conditions.append("COALESCE(n.is_active, TRUE) = TRUE")

            if has_names_is_anomaly and not include_anomaly_names:
                join_conditions.append("COALESCE(n.is_anomaly, FALSE) = FALSE")

            query.append(f"LEFT JOIN {local_schema}.{names_table} n ON {' AND '.join(join_conditions)}")

        params: List[Any] = []

        where_parts = ["m.field_name = ANY(%s)"]
        params.append(selected_fields)

        if has_map_run_id:
            run_filters = []

            for field_name in selected_fields:
                run_id = active_run_ids.get(field_name)

                if run_id:
                    run_filters.append("(m.field_name = %s AND m.run_id = %s)")
                    params.extend([field_name, run_id])

            if run_filters:
                where_parts.append("(" + " OR ".join(run_filters) + ")")

        where_parts.append("m.raw_label IS NOT NULL")
        where_parts.append("m.final_cluster_id IS NOT NULL")

        query.append("WHERE " + " AND ".join(where_parts))

        sql = "\n".join(query)

        lookups: Dict[str, FieldLookup] = {
            field_name: FieldLookup()
            for field_name in selected_fields
        }

        stats: Dict[str, Any] = {
            "rows_loaded": 0,
            "rows_skipped_blank_display_name": 0,
            "exact_conflicts": 0,
            "loose_conflicts": 0,
            "fields": {},
            "has_names_table": has_names_table,
            "include_anomaly_names": include_anomaly_names,
        }

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(sql, params)

            for row in cur:
                field_name = row["field_name"]
                raw_label = row["raw_label"]
                normalized_label = row.get("normalized_label")
                clean_display_name = row.get("clean_display_name")

                if not clean_display_name or not str(clean_display_name).strip():
                    stats["rows_skipped_blank_display_name"] += 1
                    continue

                clean_display_name = str(clean_display_name).strip()
                lookup = lookups[field_name]

                exact_conflict = False
                loose_conflict = False

                candidates = [
                    raw_label,
                    normalized_label,
                    clean_display_name,
                ]

                # If taxonomy stored a singleton array label like "{Try_Later}",
                # also allow the inner item "Try_Later" to map item-by-item.
                # This restores singleton mappings without collapsing multi-item arrays.
                for candidate_source in [raw_label, normalized_label]:
                    if candidate_source is None:
                        continue

                    candidate_text = str(candidate_source).strip()

                    if candidate_text.startswith("{") and candidate_text.endswith("}"):
                        singleton_items = parse_pg_array_literal(candidate_text)

                        if len(singleton_items) == 1:
                            candidates.append(singleton_items[0])
                for candidate in candidates:
                    if candidate is None:
                        continue

                    exact_conflict = (
                        add_mapping_value(
                            lookup.exact,
                            normalize_exact(candidate),
                            clean_display_name,
                        )
                        or exact_conflict
                    )

                    loose_conflict = (
                        add_mapping_value(
                            lookup.loose,
                            normalize_loose(candidate),
                            clean_display_name,
                        )
                        or loose_conflict
                    )

                if exact_conflict:
                    lookup.exact_conflicts += 1
                    stats["exact_conflicts"] += 1

                if loose_conflict:
                    lookup.loose_conflicts += 1
                    stats["loose_conflicts"] += 1

                stats["rows_loaded"] += 1

        for field_name, lookup in lookups.items():
            stats["fields"][field_name] = {
                "exact_keys": sum(1 for v in lookup.exact.values() if v != AMBIGUOUS),
                "loose_keys": sum(1 for v in lookup.loose.values() if v != AMBIGUOUS),
                "exact_conflicts": lookup.exact_conflicts,
                "loose_conflicts": lookup.loose_conflicts,
            }

        logging.info("Loaded %s taxonomy mapping rows", stats["rows_loaded"])
        return lookups, stats

    finally:
        conn.close()


def parse_pg_array_literal(value: str) -> List[str]:
    text = value.strip()

    if not (text.startswith("{") and text.endswith("}")):
        return [text] if text else []

    inner = text[1:-1].strip()

    if not inner:
        return []

    values: List[str] = []
    current: List[str] = []
    in_quotes = False
    escape = False

    for char in inner:
        if escape:
            current.append(char)
            escape = False
            continue

        if char == "\\":
            escape = True
            continue

        if char == '"':
            in_quotes = not in_quotes
            continue

        if char == "," and not in_quotes:
            item = "".join(current).strip()

            if item:
                values.append(strip_wrapping_quotes(item))

            current = []
            continue

        current.append(char)

    item = "".join(current).strip()

    if item:
        values.append(strip_wrapping_quotes(item))

    return values


def parse_multi_value(value: Any) -> Tuple[List[str], str]:
    if value is None:
        return [], "none"

    if isinstance(value, list):
        return [str(v).strip() for v in value if str(v).strip()], "array"

    if isinstance(value, tuple):
        return [str(v).strip() for v in value if str(v).strip()], "array"

    text = str(value).strip()

    if not text:
        return [], "empty"

    if text.startswith("{") and text.endswith("}"):
        return parse_pg_array_literal(text), "pg_array_literal"

    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)

            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip()], "json_array"

        except Exception:
            pass

    if "," in text:
        return [part.strip() for part in text.split(",") if part.strip()], "comma_string"

    return [text], "single_string"


def to_pg_array_literal(values: Sequence[str]) -> str:
    escaped = []

    for value in values:
        item = str(value).replace("\\", "\\\\").replace('"', '\\"')
        escaped.append(f'"{item}"')

    return "{" + ",".join(escaped) + "}"


def to_pg_array_literal_unquoted(values: Sequence[str]) -> str:
    clean_values = [str(v).strip() for v in values if str(v).strip()]
    return "{" + ",".join(clean_values) + "}"


def build_composite_array_candidates(values: Sequence[str]) -> List[str]:
    clean_values = [str(v).strip() for v in values if str(v).strip()]

    if not clean_values:
        return []

    candidates = [
        to_pg_array_literal_unquoted(clean_values),
        to_pg_array_literal(clean_values),
    ]

    normalized_values = []

    for value in clean_values:
        normalized = normalize_loose(value)

        if normalized:
            normalized_values.append(normalized)

    if normalized_values:
        candidates.append("{" + ",".join(normalized_values) + "}")

    return dedupe_preserve_order(candidates)


def serialize_multi_value(values: Sequence[str], original_style: str, data_type: str) -> Any:
    if data_type == "ARRAY":
        return list(values)

    if original_style == "json_array":
        return json.dumps(list(values), ensure_ascii=False)

    if original_style == "pg_array_literal":
        return to_pg_array_literal(values)

    if original_style == "comma_string":
        return ", ".join(values)

    if original_style == "single_string":
        return values[0] if values else None

    if original_style == "array":
        return list(values)

    return ", ".join(values)


def dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []

    for value in values:
        clean_value = str(value).strip()

        if not clean_value:
            continue

        key = normalize_loose(clean_value) or clean_value.lower()

        if key in seen:
            continue

        seen.add(key)
        result.append(clean_value)

    return result


def values_equal(left: Any, right: Any) -> bool:
    if left is None and right is None:
        return True

    if isinstance(left, list) or isinstance(right, list) or isinstance(left, tuple) or isinstance(right, tuple):
        left_values, _ = parse_multi_value(left)
        right_values, _ = parse_multi_value(right)
        return left_values == right_values

    return str(left).strip() == str(right).strip()


def transform_single_value(
    field_name: str,
    value: Any,
    lookups: Dict[str, FieldLookup],
    unmapped: Counter,
) -> Tuple[Any, bool]:
    if value is None:
        return value, False

    text = str(value).strip()

    if not text:
        return value, False

    clean_value = lookup_clean_value(field_name, text, lookups)

    if not clean_value:
        if not is_ignored_unmapped_label(text):
            unmapped[(field_name, text)] += 1
        return value, False

    changed = not values_equal(value, clean_value)
    return clean_value, changed


def transform_multi_value(
    field_name: str,
    value: Any,
    data_type: str,
    lookups: Dict[str, FieldLookup],
    unmapped: Counter,
) -> Tuple[Any, bool]:
    values, original_style = parse_multi_value(value)

    if not values:
        return value, False

    if field_name in COMPOSITE_ARRAY_FIELDS:
        for candidate in build_composite_array_candidates(values):
            clean_value = lookup_clean_value(field_name, candidate, lookups)

            if clean_value:
                new_value = serialize_multi_value(
                    [clean_value],
                    "array" if data_type == "ARRAY" else original_style,
                    data_type,
                )
                changed = not values_equal(value, new_value)
                return new_value, changed

    transformed: List[str] = []
    any_label_changed = False

    for raw_label in values:
        clean_value = lookup_clean_value(field_name, raw_label, lookups)

        if clean_value:
            transformed.append(clean_value)

            if not values_equal(raw_label, clean_value):
                any_label_changed = True

        else:
            transformed.append(raw_label)

            if not is_ignored_unmapped_label(raw_label):
                unmapped[(field_name, raw_label)] += 1

    transformed = dedupe_preserve_order(transformed)
    new_value = serialize_multi_value(transformed, original_style, data_type)

    changed = any_label_changed or not values_equal(value, new_value)
    return new_value, changed


def transform_row(
    row: Dict[str, Any],
    selected_fields: Sequence[str],
    field_types: Dict[str, Dict[str, str]],
    lookups: Dict[str, FieldLookup],
) -> Tuple[Dict[str, Any], bool, Counter, Dict[str, Tuple[Any, Any]]]:
    updated_values: Dict[str, Any] = {}
    changed_fields: Dict[str, Tuple[Any, Any]] = {}
    unmapped: Counter = Counter()

    for field_name in selected_fields:
        original_value = row.get(field_name)
        data_type = field_types[field_name]["data_type"]

        if field_name in MULTI_VALUE_FIELDS:
            new_value, changed = transform_multi_value(
                field_name=field_name,
                value=original_value,
                data_type=data_type,
                lookups=lookups,
                unmapped=unmapped,
            )
        else:
            new_value, changed = transform_single_value(
                field_name=field_name,
                value=original_value,
                lookups=lookups,
                unmapped=unmapped,
            )

        updated_values[field_name] = new_value

        if changed:
            changed_fields[field_name] = (original_value, new_value)

    return updated_values, bool(changed_fields), unmapped, changed_fields


def build_select_sql(
    schema: str,
    table: str,
    selected_fields: Sequence[str],
    worker_count: int,
    worker_id: int,
    include_already_updated: bool,
    limit: Optional[int],
    call_id: Optional[str],
    row_id: Optional[str],
) -> Tuple[str, List[Any]]:
    columns = ["id", "call_id", *selected_fields]
    select_cols = ", ".join(columns)

    sql = [
        f"SELECT {select_cols}",
        f"FROM {schema}.{table}",
    ]

    where = []
    params: List[Any] = []

    if not include_already_updated:
        where.append("better_tags_updated_at IS NULL")

    if call_id:
        where.append("call_id::text = %s")
        params.append(call_id)

    if row_id:
        where.append("id::text = %s")
        params.append(row_id)

    if worker_count > 1 and not call_id and not row_id:
        where.append(
            "mod((hashtext(COALESCE(call_id::text, id::text))::bigint + 2147483648), %s) = %s"
        )
        params.extend([worker_count, worker_id])

    if where:
        sql.append("WHERE " + " AND ".join(where))

    sql.append("ORDER BY id")

    if limit is not None:
        sql.append("LIMIT %s")
        params.append(limit)

    return "\n".join(sql), params


def build_update_sql(schema: str, table: str, selected_fields: Sequence[str]) -> str:
    set_parts = [f"{field_name} = %s" for field_name in selected_fields]
    set_parts.append("better_tags_updated_at = NOW()")

    return f"""
        UPDATE {schema}.{table}
        SET {", ".join(set_parts)}
        WHERE id = %s
    """


def worker_backfill(
    worker_id: int,
    args,
    selected_fields: Sequence[str],
    field_types: Dict[str, Dict[str, str]],
    lookups: Dict[str, FieldLookup],
) -> WorkerResult:
    result = WorkerResult(worker_id=worker_id)

    read_conn = connect(args.stage_database_url)
    write_conn = connect(args.stage_database_url)

    read_conn.autocommit = False
    write_conn.autocommit = False

    cursor_name = f"backfill_worker_{worker_id}_{int(time.time())}"
    update_sql = build_update_sql(args.stage_schema, args.stage_table, selected_fields)

    try:
        select_sql, select_params = build_select_sql(
            schema=args.stage_schema,
            table=args.stage_table,
            selected_fields=selected_fields,
            worker_count=args.workers,
            worker_id=worker_id,
            include_already_updated=args.include_already_updated,
            limit=args.limit,
            call_id=args.call_id,
            row_id=args.id,
        )

        read_cursor = read_conn.cursor(
            name=cursor_name,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        read_cursor.itersize = args.batch_size
        read_cursor.execute(select_sql, select_params)

        write_cursor = write_conn.cursor()

        while True:
            rows = read_cursor.fetchmany(args.batch_size)

            if not rows:
                break

            update_params: List[Tuple[Any, ...]] = []

            for row in rows:
                result.rows_scanned += 1

                try:
                    updated_values, changed, row_unmapped, changed_fields = transform_row(
                        row=row,
                        selected_fields=selected_fields,
                        field_types=field_types,
                        lookups=lookups,
                    )

                    result.unmapped.update(row_unmapped)

                    if not changed:
                        result.rows_unchanged += 1
                        continue

                    result.rows_changed += 1

                    params = [updated_values[field_name] for field_name in selected_fields]
                    params.append(row["id"])
                    update_params.append(tuple(params))

                    if len(result.changed_samples) < args.sample_limit:
                        result.changed_samples.append(
                            {
                                "id": row.get("id"),
                                "call_id": row.get("call_id"),
                                "changed_fields": {
                                    field_name: {
                                        "old": str(old_value),
                                        "new": str(new_value),
                                    }
                                    for field_name, (old_value, new_value) in changed_fields.items()
                                },
                            }
                        )

                except Exception as exc:
                    result.rows_error += 1
                    logging.exception(
                        "Worker %s failed transforming row id=%s call_id=%s: %s",
                        worker_id,
                        row.get("id"),
                        row.get("call_id"),
                        exc,
                    )

            if update_params and not args.dry_run:
                psycopg2.extras.execute_batch(
                    write_cursor,
                    update_sql,
                    update_params,
                    page_size=args.update_page_size,
                )
                write_conn.commit()
                result.update_batches += 1

            elif update_params and args.dry_run:
                result.update_batches += 1

            logging.info(
                "Worker %s progress: scanned=%s changed=%s unchanged=%s errors=%s",
                worker_id,
                result.rows_scanned,
                result.rows_changed,
                result.rows_unchanged,
                result.rows_error,
            )

        if args.dry_run:
            write_conn.rollback()
        else:
            write_conn.commit()

        read_cursor.close()

    except Exception:
        write_conn.rollback()
        raise

    finally:
        read_conn.close()
        write_conn.close()

    return result


def write_unmapped_csv(path: str, unmapped: Counter) -> None:
    with open(path, "w", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=["field_name", "label", "count"],
        )
        writer.writeheader()

        for (field_name, label), count in unmapped.most_common():
            writer.writerow(
                {
                    "field_name": field_name,
                    "label": label,
                    "count": count,
                }
            )


def write_changed_samples_json(path: str, samples: List[Dict[str, Any]]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(samples, f, indent=2, ensure_ascii=False, default=str)


def write_summary_json(path: str, summary: Dict[str, Any]) -> None:
    with open(path, "w", encoding="utf-8") as f:
        json.dump(summary, f, indent=2, ensure_ascii=False, default=str)


def run_backfill(args) -> None:
    selected_fields = args.fields

    for name in [
        args.stage_schema,
        args.stage_table,
        args.local_schema,
        args.map_table,
        args.names_table,
    ]:
        assert_identifier(name, "identifier")

    local_run_ids = dict(DEFAULT_ACTIVE_RUN_IDS)

    if args.run_id_overrides_json:
        with open(args.run_id_overrides_json, "r", encoding="utf-8") as f:
            overrides = json.load(f)

        local_run_ids.update(overrides)

    lookups, mapping_stats = load_taxonomy_lookups(
        local_dsn=args.local_database_url,
        local_schema=args.local_schema,
        map_table=args.map_table,
        names_table=args.names_table,
        selected_fields=selected_fields,
        active_run_ids=local_run_ids,
        include_anomaly_names=args.include_anomaly_names,
    )

    supplemental_stats = load_supplemental_mappings(
        supplemental_csv=args.supplemental_mapping_csv,
        lookups=lookups,
    )
    mapping_stats["supplemental_mappings"] = supplemental_stats
    mapping_stats["ignored_unmapped_labels"] = sorted(IGNORED_UNMAPPED_LABELS)

    stage_conn = connect(args.stage_database_url)
    stage_conn.autocommit = True

    try:
        stage_columns = get_table_columns(stage_conn, args.stage_schema, args.stage_table)

        required_columns = {"id", "call_id", "better_tags_updated_at", *selected_fields}
        missing = required_columns - set(stage_columns)

        if missing:
            raise RuntimeError(
                f"Missing required STAGE columns in {args.stage_schema}.{args.stage_table}: {sorted(missing)}"
            )

        field_types = {
            field_name: stage_columns[field_name]
            for field_name in selected_fields
        }

    finally:
        stage_conn.close()

    logging.info("Selected fields: %s", ", ".join(selected_fields))
    logging.info("Dry run: %s", args.dry_run)
    logging.info("Workers: %s", args.workers)

    started_at = datetime.utcnow()

    worker_results: List[WorkerResult] = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                worker_backfill,
                worker_id,
                args,
                selected_fields,
                field_types,
                lookups,
            )
            for worker_id in range(args.workers)
        ]

        for future in as_completed(futures):
            worker_results.append(future.result())

    finished_at = datetime.utcnow()

    total_unmapped = Counter()
    changed_samples: List[Dict[str, Any]] = []

    summary: Dict[str, Any] = {
        "started_at_utc": started_at.isoformat(),
        "finished_at_utc": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "dry_run": args.dry_run,
        "stage_table": f"{args.stage_schema}.{args.stage_table}",
        "selected_fields": selected_fields,
        "workers": args.workers,
        "batch_size": args.batch_size,
        "update_page_size": args.update_page_size,
        "mapping_stats": mapping_stats,
        "rows_scanned": 0,
        "rows_changed": 0,
        "rows_unchanged": 0,
        "rows_error": 0,
        "update_batches": 0,
        "worker_results": [],
    }

    for worker_result in sorted(worker_results, key=lambda r: r.worker_id):
        total_unmapped.update(worker_result.unmapped)
        changed_samples.extend(worker_result.changed_samples)

        summary["rows_scanned"] += worker_result.rows_scanned
        summary["rows_changed"] += worker_result.rows_changed
        summary["rows_unchanged"] += worker_result.rows_unchanged
        summary["rows_error"] += worker_result.rows_error
        summary["update_batches"] += worker_result.update_batches

        summary["worker_results"].append(
            {
                "worker_id": worker_result.worker_id,
                "rows_scanned": worker_result.rows_scanned,
                "rows_changed": worker_result.rows_changed,
                "rows_unchanged": worker_result.rows_unchanged,
                "rows_error": worker_result.rows_error,
                "update_batches": worker_result.update_batches,
                "unmapped_label_count": sum(worker_result.unmapped.values()),
            }
        )

    summary["unmapped_total_count"] = sum(total_unmapped.values())
    summary["unmapped_unique_count"] = len(total_unmapped)

    os.makedirs(args.audit_dir, exist_ok=True)

    summary_path = os.path.join(args.audit_dir, "backfill_summary.json")
    unmapped_path = os.path.join(args.audit_dir, "backfill_unmapped_labels.csv")
    samples_path = os.path.join(args.audit_dir, "backfill_changed_samples.json")

    write_summary_json(summary_path, summary)
    write_unmapped_csv(unmapped_path, total_unmapped)
    write_changed_samples_json(samples_path, changed_samples[: args.sample_limit])

    logging.info("Backfill summary written to: %s", summary_path)
    logging.info("Unmapped labels written to: %s", unmapped_path)
    logging.info("Changed row samples written to: %s", samples_path)

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))


def parse_args(argv: Optional[Sequence[str]] = None):
    parser = argparse.ArgumentParser(
        description="Backfill cleaned taxonomy tags into STAGE fact_call_classification."
    )

    parser.add_argument(
        "--local-database-url",
        default=os.getenv("LOCAL_DATABASE_URL") or build_local_database_url_from_env(),
        help="Local taxonomy DB connection string. Defaults to LOCAL_DATABASE_URL or LOCAL_PG_* env vars.",
    )

    parser.add_argument(
        "--stage-database-url",
        default=os.getenv("STAGE_DATABASE_URL") or build_stage_database_url_from_env(),
        help="STAGE DWH DB connection string. Defaults to STAGE_DATABASE_URL or DWH_* env vars.",
    )

    parser.add_argument("--local-schema", default="public")
    parser.add_argument("--stage-schema", default="public")
    parser.add_argument("--stage-table", default="fact_call_classification")
    parser.add_argument("--map-table", default="taxonomy_label_cluster_map")
    parser.add_argument("--names-table", default="taxonomy_cluster_names")

    parser.add_argument(
        "--fields",
        type=split_csv_arg,
        default=DEFAULT_FIELDS,
        help="Comma-separated fields to backfill.",
    )

    parser.add_argument(
        "--dry-run",
        type=parse_bool,
        default=True,
        help="Default true. Set false to update STAGE.",
    )

    parser.add_argument(
        "--include-already-updated",
        action="store_true",
        help="Also process rows where better_tags_updated_at is already set.",
    )

    parser.add_argument(
        "--include-anomaly-names",
        action="store_true",
        help="Allow mapping to anomaly cluster names if taxonomy_cluster_names.is_anomaly exists.",
    )

    parser.add_argument(
        "--supplemental-mapping-csv",
        default=None,
        help=(
            "Optional unresolved_match_audit.csv file. "
            "Uses safe SAME_FIELD exact/display/loose matches as supplemental mappings."
        ),
    )

    parser.add_argument(
        "--workers",
        type=int,
        default=4,
        help="Parallel worker count. Each worker processes a hash partition of rows.",
    )

    parser.add_argument(
        "--batch-size",
        type=int,
        default=5000,
        help="Rows fetched per worker batch.",
    )

    parser.add_argument(
        "--update-page-size",
        type=int,
        default=500,
        help="execute_batch page size for UPDATE statements.",
    )

    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Optional test limit per worker. For testing, use --workers 1 if you want an exact limit.",
    )

    parser.add_argument(
        "--call-id",
        default=None,
        help="Optional single call_id test.",
    )

    parser.add_argument(
        "--id",
        default=None,
        help="Optional single row id test.",
    )

    parser.add_argument(
        "--audit-dir",
        default="audit_backfill_clean_taxonomy",
        help="Directory for summary/unmapped/sample audit files.",
    )

    parser.add_argument(
        "--sample-limit",
        type=int,
        default=100,
        help="Maximum changed row samples to store.",
    )

    parser.add_argument(
        "--run-id-overrides-json",
        default=None,
        help="Optional JSON file mapping field_name to active run_id.",
    )

    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args(argv)

    if not args.local_database_url:
        raise RuntimeError("Missing local DB connection. Set LOCAL_DATABASE_URL or --local-database-url.")

    if not args.stage_database_url:
        raise RuntimeError("Missing STAGE DB connection. Set STAGE_DATABASE_URL or --stage-database-url.")

    if args.workers < 1:
        raise RuntimeError("--workers must be >= 1")

    if args.batch_size < 1:
        raise RuntimeError("--batch-size must be >= 1")

    if args.update_page_size < 1:
        raise RuntimeError("--update-page-size must be >= 1")

    invalid_fields = [field for field in args.fields if field not in DEFAULT_FIELDS]

    if invalid_fields:
        raise RuntimeError(
            f"Unsupported fields requested: {invalid_fields}. "
            f"Allowed fields: {DEFAULT_FIELDS}"
        )

    return args


def main(argv: Optional[Sequence[str]] = None) -> int:
    args = parse_args(argv)

    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )

    run_backfill(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
