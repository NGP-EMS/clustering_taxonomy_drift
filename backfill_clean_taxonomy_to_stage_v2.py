#!/usr/bin/env python3
"""
backfill_clean_taxonomy_to_stage_v2.py

Column-level auditable backfill of STAGE fact_call_classification
with clean taxonomy display names.

Rules:
  - Replace a raw STAGE label only when an exact/normalized match exists in the
    same field's approved taxonomy_label_cluster_map (no similarity matching).
  - Similarity candidates are written to taxonomy_unresolved_label_queue as
    recommendations only — no STAGE mutation, no cluster creation.
  - Only fields that actually changed are written; better_tags_updated_at is
    set only when at least one field changes.
  - Default scope: rows where better_tags_updated_at IS NULL.
    Use --include-already-updated to include already-updated rows.
  - Dry-run is the default. Pass --dry-run false to apply.
  - Active run IDs are resolved dynamically from the active cluster/run tables,
    not from a hard-coded dict. Use --run-id-overrides-json only for exceptions.
  - Supplemental mapping CSV is still supported because existing approved
    supplemental mappings have not yet been migrated into taxonomy_label_cluster_map.
    When that migration is complete, this path can be removed.

Audit tables written to the LOCAL taxonomy DB:
  taxonomy_backfill_runs
  taxonomy_backfill_row_audit
  taxonomy_backfill_field_audit
  taxonomy_unresolved_label_queue

STAGE DB is only written when --dry-run false AND at least one field changed.

Env vars (.env or environment):
  LOCAL_DATABASE_URL  or  LOCAL_PG_HOST/PORT/DB/USER/PASSWORD
  STAGE_DATABASE_URL  or  DWH_HOST/PORT/NAME/USER/PASS

Example dry run:
  python backfill_clean_taxonomy_to_stage_v2.py

Example apply:
  python backfill_clean_taxonomy_to_stage_v2.py --dry-run false --workers 4

Single-row test:
  python backfill_clean_taxonomy_to_stage_v2.py --call-id <uuid> --workers 1
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import os
import re
import sys
import threading
import time
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import psycopg2
import psycopg2.extras

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_FIELDS: List[str] = [
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

MULTI_VALUE_FIELDS: Set[str] = {
    "call_type_sub",
    "outcome_sub",
    "main_reason_sub",
    "next_step",
    "additional_tags",
    "descriptive_keywords",
    "coaching_tags",
}

# Fields whose entire array is the lookup key (one display name per array).
COMPOSITE_ARRAY_FIELDS: Set[str] = set()

IGNORED_UNMAPPED_LABELS: Set[str] = {
    "na", "n a", "n/a", "none", "null", "nan",
}

SUPPLEMENTAL_SAFE_STATUSES: Set[str] = {
    "SAME_FIELD_RAW_OR_NORMALIZED_EXACT",
    "SAME_FIELD_RAW_OR_NORMALIZED_LOOSE",
    "SAME_FIELD_DISPLAY_NAME_MATCH",
}

AMBIGUOUS = "__AMBIGUOUS_MAPPING__"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# execute_batch page size for audit INSERT statements.
AUDIT_INSERT_PAGE = 1000


# ── Data classes ──────────────────────────────────────────────────────────────

@dataclass
class FieldLookup:
    exact: Dict[str, str] = field(default_factory=dict)
    loose: Dict[str, str] = field(default_factory=dict)
    exact_conflicts: int = 0
    loose_conflicts: int = 0


@dataclass
class FieldResult:
    field_name: str
    old_value: Any
    new_value: Any
    changed: bool
    field_status: str           # CHANGED | UNCHANGED | UNMAPPED | AMBIGUOUS | EMPTY | ERROR
    mapping_method: Optional[str]
    mapped_display_names: List[str]
    unmapped_labels: List[str]
    ambiguous_labels: List[str]
    notes: str = ""


@dataclass
class WorkerResult:
    worker_id: int
    rows_scanned: int = 0
    rows_changed: int = 0
    rows_unchanged: int = 0
    rows_error: int = 0
    update_batches: int = 0
    unresolved: Counter = field(default_factory=Counter)


class UnresolvedAccumulator:
    """
    In-memory accumulator for unresolved labels within a single worker.
    Keyed by (field_name, normalized_label) — same as the DB UNIQUE constraint.
    Aggregates occurrence + distinct-call counts so we do exactly ONE upsert
    per unique key at end-of-worker instead of one per occurrence.
    """

    def __init__(self) -> None:
        # key -> {raw_label, occ, calls: set, examples: list}
        self._data: Dict[Tuple[str, str], Dict[str, Any]] = {}

    def add(self, field_name: str, raw_label: str, normalized_label: str, call_id: Optional[str]) -> None:
        key = (field_name, normalized_label)
        if key not in self._data:
            self._data[key] = {
                "raw_label": raw_label,
                "occ": 0,
                "calls": set(),
                "examples": [],
            }
        entry = self._data[key]
        entry["occ"] += 1
        if call_id:
            entry["calls"].add(call_id)
            if len(entry["examples"]) < 5:
                entry["examples"].append(call_id)

    def to_batch(self) -> List[Dict[str, Any]]:
        result = []
        for (field_name, norm), entry in self._data.items():
            result.append({
                "field_name": field_name,
                "raw_label": entry["raw_label"],
                "normalized_label": norm,
                "occurrence_count": entry["occ"],
                "distinct_call_count": len(entry["calls"]),
                "source_examples": entry["examples"],
            })
        return result

    def __len__(self) -> int:
        return len(self._data)


class SharedProgress:
    """Thread-safe counter so the progress thread can read live totals."""

    def __init__(self) -> None:
        self._lock = threading.Lock()
        self.rows_scanned: int = 0
        self.rows_changed: int = 0
        self.rows_unchanged: int = 0
        self.rows_error: int = 0
        self.started_at: float = time.monotonic()

    def add(self, scanned: int, changed: int, unchanged: int, error: int) -> None:
        with self._lock:
            self.rows_scanned += scanned
            self.rows_changed += changed
            self.rows_unchanged += unchanged
            self.rows_error += error

    def snapshot(self) -> Dict[str, Any]:
        with self._lock:
            elapsed = time.monotonic() - self.started_at
            rate = self.rows_scanned / elapsed if elapsed > 0 else 0
            return {
                "rows_scanned": self.rows_scanned,
                "rows_changed": self.rows_changed,
                "rows_unchanged": self.rows_unchanged,
                "rows_error": self.rows_error,
                "elapsed_seconds": round(elapsed, 1),
                "rate_rows_per_sec": round(rate, 1),
            }


# ── Connection helpers ────────────────────────────────────────────────────────

def _build_dsn(host, port, db, user, password) -> Optional[str]:
    if not all([host, port, db, user, password]):
        return None
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def build_local_dsn() -> Optional[str]:
    return (
        os.getenv("LOCAL_DATABASE_URL")
        or _build_dsn(
            os.getenv("LOCAL_PG_HOST"),
            os.getenv("LOCAL_PG_PORT"),
            os.getenv("LOCAL_PG_DB"),
            os.getenv("LOCAL_PG_USER"),
            os.getenv("LOCAL_PG_PASSWORD"),
        )
    )


def build_stage_dsn() -> Optional[str]:
    return (
        os.getenv("STAGE_DATABASE_URL")
        or _build_dsn(
            os.getenv("DWH_HOST"),
            os.getenv("DWH_PORT"),
            os.getenv("DWH_NAME"),
            os.getenv("DWH_USER"),
            os.getenv("DWH_PASS"),
        )
    )


def connect(dsn: str):
    return psycopg2.connect(dsn)


def get_table_columns(conn, schema: str, table: str) -> Dict[str, Dict[str, str]]:
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT column_name, data_type, udt_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return {r["column_name"]: dict(r) for r in cur.fetchall()}


def table_exists(conn, schema: str, table: str) -> bool:
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1 FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
            )
            """,
            (schema, table),
        )
        return bool(cur.fetchone()[0])


# ── Normalisation ─────────────────────────────────────────────────────────────

def strip_wrapping_quotes(text: str) -> str:
    text = text.strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        return text[1:-1].strip()
    return text


def normalize_exact(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = strip_wrapping_quotes(str(value).strip())
    return text.lower() if text else None


def normalize_loose(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = strip_wrapping_quotes(str(value).strip())
    text = text.replace("_", " ")
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text if text else None


def is_ignored_unmapped(value: Any) -> bool:
    key = normalize_loose(value)
    return bool(key and key in IGNORED_UNMAPPED_LABELS)


# ── Lookup map construction ───────────────────────────────────────────────────

def _add_mapping(target: Dict[str, str], key: Optional[str], value: str) -> bool:
    """Returns True if a conflict was introduced."""
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


def _force_mapping(target: Dict[str, str], key: Optional[str], value: str) -> bool:
    if not key:
        return False
    if target.get(key) == value:
        return False
    target[key] = value
    return True


def resolve_active_run_ids(
    conn,
    schema: str,
    map_table: str,
    selected_fields: Sequence[str],
) -> Dict[str, str]:
    """
    Determine the active run_id for each selected field by inspecting the
    actual cluster/run tables — not a hard-coded dict.

    Strategy (tried in order until a result is found):
      1. taxonomy_mapper_runs: latest finished_at run per field that is active.
      2. taxonomy_run_metadata: latest run per field.
      3. taxonomy_label_cluster_map itself: most common run_id per field.
      4. Fall back to no run_id filter (empty string means "no filter").
    """
    run_ids: Dict[str, str] = {}
    map_cols = set(get_table_columns(conn, schema, map_table).keys())
    if "run_id" not in map_cols:
        return {}

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        # Attempt 1: taxonomy_mapper_runs
        try:
            cur.execute(
                """
                SELECT field_name, run_id
                FROM (
                    SELECT field_name, run_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY field_name
                               ORDER BY finished_at DESC NULLS LAST, started_at DESC NULLS LAST
                           ) AS rn
                    FROM public.taxonomy_mapper_runs
                    WHERE field_name = ANY(%s)
                      AND COALESCE(active, TRUE) = TRUE
                ) sub
                WHERE rn = 1
                """,
                (list(selected_fields),),
            )
            for row in cur.fetchall():
                if row["run_id"]:
                    run_ids[row["field_name"]] = row["run_id"]
        except Exception:
            conn.rollback()

        # Attempt 2: taxonomy_run_metadata for fields still missing
        missing = [f for f in selected_fields if f not in run_ids]
        if missing:
            try:
                cur.execute(
                    """
                    SELECT field_name, run_id
                    FROM (
                        SELECT field_name, run_id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY field_name
                                   ORDER BY created_at DESC NULLS LAST
                               ) AS rn
                        FROM public.taxonomy_run_metadata
                        WHERE field_name = ANY(%s)
                    ) sub
                    WHERE rn = 1
                    """,
                    (missing,),
                )
                for row in cur.fetchall():
                    if row["run_id"] and row["field_name"] not in run_ids:
                        run_ids[row["field_name"]] = row["run_id"]
            except Exception:
                conn.rollback()

        # Attempt 3: majority run_id in the map table itself
        missing = [f for f in selected_fields if f not in run_ids]
        if missing:
            try:
                cur.execute(
                    f"""
                    SELECT field_name, run_id
                    FROM (
                        SELECT field_name, run_id,
                               COUNT(*) AS cnt,
                               ROW_NUMBER() OVER (
                                   PARTITION BY field_name
                                   ORDER BY COUNT(*) DESC
                               ) AS rn
                        FROM {schema}.{map_table}
                        WHERE field_name = ANY(%s)
                          AND run_id IS NOT NULL
                        GROUP BY field_name, run_id
                    ) sub
                    WHERE rn = 1
                    """,
                    (missing,),
                )
                for row in cur.fetchall():
                    if row["run_id"] and row["field_name"] not in run_ids:
                        run_ids[row["field_name"]] = row["run_id"]
            except Exception:
                conn.rollback()

    logging.info("Resolved active run_ids: %s", run_ids)
    return run_ids


def load_taxonomy_lookups(
    local_dsn: str,
    local_schema: str,
    map_table: str,
    names_table: str,
    selected_fields: Sequence[str],
    run_id_overrides: Dict[str, str],
    include_anomaly_names: bool,
) -> Tuple[Dict[str, FieldLookup], Dict[str, Any]]:
    logging.info("Loading taxonomy mappings from local DB")
    conn = connect(local_dsn)
    conn.autocommit = False

    try:
        if not table_exists(conn, local_schema, map_table):
            raise RuntimeError(f"Local mapping table not found: {local_schema}.{map_table}")

        map_cols = get_table_columns(conn, local_schema, map_table)
        names_cols = (
            get_table_columns(conn, local_schema, names_table)
            if table_exists(conn, local_schema, names_table)
            else {}
        )

        for col in ("field_name", "raw_label", "final_cluster_id"):
            if col not in map_cols:
                raise RuntimeError(f"Missing column {col} in {map_table}")

        has_names = bool(names_cols)
        has_map_display_name = "display_name" in map_cols
        has_normalized_label = "normalized_label" in map_cols
        has_map_run_id = "run_id" in map_cols
        has_names_run_id = "run_id" in names_cols
        has_names_is_anomaly = "is_anomaly" in names_cols
        has_names_active = "active" in names_cols
        has_names_is_active = "is_active" in names_cols

        if not has_names and not has_map_display_name:
            raise RuntimeError(
                f"No {names_table} table and no display_name in {map_table}; "
                "cannot derive clean display names."
            )

        # Resolve active run IDs dynamically, then apply overrides on top.
        active_run_ids = resolve_active_run_ids(conn, local_schema, map_table, selected_fields)
        active_run_ids.update(run_id_overrides)

        sel = [
            "m.field_name",
            "m.raw_label",
            "m.final_cluster_id",
            "m.normalized_label" if has_normalized_label else "NULL::text AS normalized_label",
        ]

        if has_names:
            if has_map_display_name:
                sel.append("COALESCE(n.display_name, m.display_name) AS clean_display_name")
            else:
                sel.append("n.display_name AS clean_display_name")
        else:
            sel.append("m.display_name AS clean_display_name")

        sql_parts = [
            f"SELECT {', '.join(sel)}",
            f"FROM {local_schema}.{map_table} m",
        ]

        if has_names:
            join_conds = [
                "n.field_name = m.field_name",
                "n.cluster_id = m.final_cluster_id",
            ]
            if has_map_run_id and has_names_run_id:
                join_conds.append("n.run_id = m.run_id")
            if has_names_active:
                join_conds.append("COALESCE(n.active, TRUE) = TRUE")
            if has_names_is_active:
                join_conds.append("COALESCE(n.is_active, TRUE) = TRUE")
            if has_names_is_anomaly and not include_anomaly_names:
                join_conds.append("COALESCE(n.is_anomaly, FALSE) = FALSE")
            sql_parts.append(
                f"LEFT JOIN {local_schema}.{names_table} n ON {' AND '.join(join_conds)}"
            )

        params: List[Any] = []
        where: List[str] = ["m.field_name = ANY(%s)"]
        params.append(list(selected_fields))

        if has_map_run_id and active_run_ids:
            run_filters = []
            for fn in selected_fields:
                rid = active_run_ids.get(fn)
                if rid:
                    run_filters.append("(m.field_name = %s AND m.run_id = %s)")
                    params.extend([fn, rid])
            if run_filters:
                where.append("(" + " OR ".join(run_filters) + ")")

        where += ["m.raw_label IS NOT NULL", "m.final_cluster_id IS NOT NULL"]
        sql_parts.append("WHERE " + " AND ".join(where))

        lookups: Dict[str, FieldLookup] = {f: FieldLookup() for f in selected_fields}
        stats: Dict[str, Any] = {
            "rows_loaded": 0,
            "rows_skipped_blank_display_name": 0,
            "exact_conflicts": 0,
            "loose_conflicts": 0,
            "resolved_run_ids": active_run_ids,
            "fields": {},
        }

        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute("\n".join(sql_parts), params)
            for row in cur:
                fn = row["field_name"]
                raw = row["raw_label"]
                norm = row.get("normalized_label")
                display = row.get("clean_display_name")

                if not display or not str(display).strip():
                    stats["rows_skipped_blank_display_name"] += 1
                    continue

                display = str(display).strip()
                lk = lookups[fn]
                exact_c = False
                loose_c = False

                candidates = [raw, norm, display]

                # Allow singleton pg-array labels like "{Try_Later}" to also
                # match item-by-item as "Try_Later".
                for src in [raw, norm]:
                    if src is None:
                        continue
                    txt = str(src).strip()
                    if txt.startswith("{") and txt.endswith("}"):
                        items = parse_pg_array_literal(txt)
                        if len(items) == 1:
                            candidates.append(items[0])

                for candidate in candidates:
                    if candidate is None:
                        continue
                    exact_c = _add_mapping(lk.exact, normalize_exact(candidate), display) or exact_c
                    loose_c = _add_mapping(lk.loose, normalize_loose(candidate), display) or loose_c

                if exact_c:
                    lk.exact_conflicts += 1
                    stats["exact_conflicts"] += 1
                if loose_c:
                    lk.loose_conflicts += 1
                    stats["loose_conflicts"] += 1
                stats["rows_loaded"] += 1

        for fn, lk in lookups.items():
            stats["fields"][fn] = {
                "exact_keys": sum(1 for v in lk.exact.values() if v != AMBIGUOUS),
                "loose_keys": sum(1 for v in lk.loose.values() if v != AMBIGUOUS),
                "exact_conflicts": lk.exact_conflicts,
                "loose_conflicts": lk.loose_conflicts,
            }

        logging.info("Loaded %s taxonomy mapping rows", stats["rows_loaded"])
        return lookups, stats

    finally:
        conn.close()


def load_supplemental_mappings(
    supplemental_csv: Optional[str],
    lookups: Dict[str, FieldLookup],
) -> Dict[str, Any]:
    """
    Supplemental mapping CSV support is kept because existing approved
    supplemental mappings have not yet been migrated into taxonomy_label_cluster_map.
    Only SAME_FIELD safe statuses are applied, and only as force-overwrites on
    the same-field lookup (never cross-field).
    """
    stats: Dict[str, Any] = {
        "enabled": bool(supplemental_csv),
        "path": supplemental_csv,
        "rows_read": 0,
        "rows_applied": 0,
        "rows_skipped": 0,
        "forced_keys": 0,
    }

    if not supplemental_csv:
        return stats

    if not os.path.exists(supplemental_csv):
        raise FileNotFoundError(f"Supplemental mapping CSV not found: {supplemental_csv}")

    required_cols = {
        "field_name", "label", "status",
        "matched_field_name", "matched_raw_label",
        "matched_normalized_label", "matched_display_name",
    }

    with open(supplemental_csv, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        missing = required_cols - set(reader.fieldnames or [])
        if missing:
            raise RuntimeError(
                f"Supplemental CSV missing required columns: {sorted(missing)}"
            )

        for row in reader:
            stats["rows_read"] += 1
            status = str(row.get("status") or "").strip()
            fn = str(row.get("field_name") or "").strip()
            matched_fn = str(row.get("matched_field_name") or "").strip()
            display = str(row.get("matched_display_name") or "").strip()

            if (
                status not in SUPPLEMENTAL_SAFE_STATUSES
                or not fn
                or not matched_fn
                or fn != matched_fn
                or fn not in lookups
                or not display
            ):
                stats["rows_skipped"] += 1
                continue

            lk = lookups[fn]
            for candidate in [
                row.get("label"),
                row.get("matched_raw_label"),
                row.get("matched_normalized_label"),
                display,
            ]:
                if candidate is None:
                    continue
                stats["forced_keys"] += int(
                    _force_mapping(lk.exact, normalize_exact(candidate), display)
                )
                stats["forced_keys"] += int(
                    _force_mapping(lk.loose, normalize_loose(candidate), display)
                )

            stats["rows_applied"] += 1

    logging.info("Supplemental mappings applied: %s", stats)
    return stats


# ── PG array parsing / serialisation ─────────────────────────────────────────

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
    if isinstance(value, (list, tuple)):
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
        return [p.strip() for p in text.split(",") if p.strip()], "comma_string"
    return [text], "single_string"


def to_pg_array_literal(values: Sequence[str]) -> str:
    escaped = ['"' + str(v).replace("\\", "\\\\").replace('"', '\\"') + '"' for v in values]
    return "{" + ",".join(escaped) + "}"


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
    return list(values)


def dedupe_preserve_order(values: Iterable[str]) -> List[str]:
    seen: Set[str] = set()
    result: List[str] = []
    for v in values:
        v = str(v).strip()
        if not v:
            continue
        key = normalize_loose(v) or v.lower()
        if key in seen:
            continue
        seen.add(key)
        result.append(v)
    return result


def values_equal(left: Any, right: Any) -> bool:
    if left is None and right is None:
        return True
    if isinstance(left, (list, tuple)) or isinstance(right, (list, tuple)):
        lv, _ = parse_multi_value(left)
        rv, _ = parse_multi_value(right)
        return lv == rv
    return str(left).strip() == str(right).strip()


def row_hash(row: Dict[str, Any], fields: Sequence[str]) -> str:
    parts = {f: str(row.get(f)) for f in fields}
    return hashlib.sha256(json.dumps(parts, sort_keys=True, default=str).encode()).hexdigest()[:16]


# ── Per-field transformation ──────────────────────────────────────────────────

def _lookup(field_name: str, raw: str, lookups: Dict[str, FieldLookup]) -> Optional[str]:
    lk = lookups.get(field_name)
    if not lk:
        return None
    k = normalize_exact(raw)
    if k:
        v = lk.exact.get(k)
        if v and v != AMBIGUOUS:
            return v
    k = normalize_loose(raw)
    if k:
        v = lk.loose.get(k)
        if v and v != AMBIGUOUS:
            return v
    return None


def _is_ambiguous(field_name: str, raw: str, lookups: Dict[str, FieldLookup]) -> bool:
    lk = lookups.get(field_name)
    if not lk:
        return False
    for norm_fn in (normalize_exact, normalize_loose):
        k = norm_fn(raw)
        if k and lk.exact.get(k) == AMBIGUOUS:
            return True
        if k and lk.loose.get(k) == AMBIGUOUS:
            return True
    return False


def transform_field(
    field_name: str,
    original_value: Any,
    data_type: str,
    lookups: Dict[str, FieldLookup],
) -> FieldResult:
    """
    Transform one field value.  Returns a FieldResult describing what happened.
    No side-effects; unmapped labels are reported back to the caller.
    """
    if original_value is None or (isinstance(original_value, str) and not original_value.strip()):
        return FieldResult(
            field_name=field_name,
            old_value=original_value,
            new_value=original_value,
            changed=False,
            field_status="EMPTY",
            mapping_method=None,
            mapped_display_names=[],
            unmapped_labels=[],
            ambiguous_labels=[],
        )

    is_multi = field_name in MULTI_VALUE_FIELDS

    if not is_multi:
        raw = str(original_value).strip()
        if _is_ambiguous(field_name, raw, lookups):
            return FieldResult(
                field_name=field_name,
                old_value=original_value,
                new_value=original_value,
                changed=False,
                field_status="AMBIGUOUS",
                mapping_method=None,
                mapped_display_names=[],
                unmapped_labels=[],
                ambiguous_labels=[raw],
            )
        display = _lookup(field_name, raw, lookups)
        if display:
            changed = not values_equal(original_value, display)
            return FieldResult(
                field_name=field_name,
                old_value=original_value,
                new_value=display,
                changed=changed,
                field_status="CHANGED" if changed else "UNCHANGED",
                mapping_method="exact_or_loose",
                mapped_display_names=[display],
                unmapped_labels=[],
                ambiguous_labels=[],
            )
        if is_ignored_unmapped(raw):
            return FieldResult(
                field_name=field_name,
                old_value=original_value,
                new_value=original_value,
                changed=False,
                field_status="UNCHANGED",
                mapping_method=None,
                mapped_display_names=[],
                unmapped_labels=[],
                ambiguous_labels=[],
            )
        return FieldResult(
            field_name=field_name,
            old_value=original_value,
            new_value=original_value,
            changed=False,
            field_status="UNMAPPED",
            mapping_method=None,
            mapped_display_names=[],
            unmapped_labels=[raw],
            ambiguous_labels=[],
        )

    # Multi-value path
    items, original_style = parse_multi_value(original_value)
    if not items:
        return FieldResult(
            field_name=field_name,
            old_value=original_value,
            new_value=original_value,
            changed=False,
            field_status="EMPTY",
            mapping_method=None,
            mapped_display_names=[],
            unmapped_labels=[],
            ambiguous_labels=[],
        )

    # Composite array: treat the whole array as one lookup key
    if field_name in COMPOSITE_ARRAY_FIELDS:
        for candidate in _build_composite_candidates(items):
            display = _lookup(field_name, candidate, lookups)
            if display:
                new_val = serialize_multi_value([display], "array", data_type)
                changed = not values_equal(original_value, new_val)
                return FieldResult(
                    field_name=field_name,
                    old_value=original_value,
                    new_value=new_val,
                    changed=changed,
                    field_status="CHANGED" if changed else "UNCHANGED",
                    mapping_method="composite_array",
                    mapped_display_names=[display],
                    unmapped_labels=[],
                    ambiguous_labels=[],
                )

    transformed: List[str] = []
    mapped_names: List[str] = []
    unmapped: List[str] = []
    ambiguous: List[str] = []
    any_changed = False

    for raw in items:
        if _is_ambiguous(field_name, raw, lookups):
            transformed.append(raw)
            ambiguous.append(raw)
            continue
        display = _lookup(field_name, raw, lookups)
        if display:
            transformed.append(display)
            mapped_names.append(display)
            if not values_equal(raw, display):
                any_changed = True
        else:
            transformed.append(raw)
            if not is_ignored_unmapped(raw):
                unmapped.append(raw)

    transformed = dedupe_preserve_order(transformed)
    new_val = serialize_multi_value(transformed, original_style, data_type)
    changed = any_changed or not values_equal(original_value, new_val)

    if ambiguous and not mapped_names and not unmapped:
        status = "AMBIGUOUS"
    elif unmapped and not mapped_names:
        status = "UNMAPPED"
    elif changed:
        status = "CHANGED"
    else:
        status = "UNCHANGED"

    return FieldResult(
        field_name=field_name,
        old_value=original_value,
        new_value=new_val,
        changed=changed,
        field_status=status,
        mapping_method="item_by_item" if (mapped_names or not unmapped) else None,
        mapped_display_names=mapped_names,
        unmapped_labels=unmapped,
        ambiguous_labels=ambiguous,
    )


def _build_composite_candidates(items: List[str]) -> List[str]:
    clean = [str(v).strip() for v in items if str(v).strip()]
    if not clean:
        return []
    candidates = [
        "{" + ",".join(clean) + "}",
        to_pg_array_literal(clean),
    ]
    norms = [normalize_loose(v) for v in clean if normalize_loose(v)]
    if norms:
        candidates.append("{" + ",".join(norms) + "}")
    return dedupe_preserve_order(candidates)


# ── SQL builders ──────────────────────────────────────────────────────────────

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
    cols = ["id", "call_id", *selected_fields]
    sql = [f"SELECT {', '.join(cols)}", f"FROM {schema}.{table}"]
    where: List[str] = []
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


def build_grouped_update_sql(
    schema: str,
    table: str,
    changed_field_set: Tuple[str, ...],
) -> str:
    """
    Build an UPDATE that touches only the fields that actually changed for this
    batch group, plus better_tags_updated_at.
    """
    set_parts = [f"{f} = %s" for f in changed_field_set]
    set_parts.append("better_tags_updated_at = NOW()")
    return f"UPDATE {schema}.{table} SET {', '.join(set_parts)} WHERE id = %s"


# ── Audit INSERT helpers ──────────────────────────────────────────────────────

def _jsonb(value: Any) -> Optional[str]:
    """Serialize value to a JSONB-compatible string, or None for null."""
    if value is None:
        return None
    return json.dumps(value, ensure_ascii=False, default=str)


def insert_row_audit_batch(
    cur,
    run_id: str,
    batch: List[Dict[str, Any]],
) -> None:
    if not batch:
        return
    psycopg2.extras.execute_batch(
        cur,
        """
        INSERT INTO taxonomy_backfill_row_audit
            (backfill_run_id, stage_row_id, call_id, row_status,
             changed_fields, unchanged_fields, unmapped_fields,
             error_message, old_row_hash, new_row_hash)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        [
            (
                run_id,
                r["stage_row_id"],
                r.get("call_id"),
                r["row_status"],
                _jsonb(r.get("changed_fields")),
                _jsonb(r.get("unchanged_fields")),
                _jsonb(r.get("unmapped_fields")),
                r.get("error_message"),
                r.get("old_row_hash"),
                r.get("new_row_hash"),
            )
            for r in batch
        ],
        page_size=AUDIT_INSERT_PAGE,
    )


def insert_field_audit_batch(
    cur,
    run_id: str,
    batch: List[Dict[str, Any]],
) -> None:
    if not batch:
        return
    psycopg2.extras.execute_batch(
        cur,
        """
        INSERT INTO taxonomy_backfill_field_audit
            (backfill_run_id, stage_row_id, call_id, field_name,
             old_value, new_value, changed, field_status,
             mapping_method, mapped_display_names, unmapped_labels,
             ambiguous_labels, notes)
        VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s,%s)
        """,
        [
            (
                run_id,
                r["stage_row_id"],
                r.get("call_id"),
                r["field_name"],
                _jsonb(r["old_value"]),
                _jsonb(r["new_value"]),
                r["changed"],
                r["field_status"],
                r.get("mapping_method"),
                _jsonb(r.get("mapped_display_names")),
                _jsonb(r.get("unmapped_labels")),
                _jsonb(r.get("ambiguous_labels")),
                r.get("notes"),
            )
            for r in batch
        ],
        page_size=AUDIT_INSERT_PAGE,
    )


def upsert_unresolved_batch(
    cur,
    batch: List[Dict[str, Any]],
) -> None:
    """
    Merge pre-aggregated unresolved labels into taxonomy_unresolved_label_queue.
    Input is already de-duped by (field_name, normalized_label) by UnresolvedAccumulator,
    so the ON CONFLICT clause is a plain increment — no subquery required.
    """
    if not batch:
        return
    psycopg2.extras.execute_batch(
        cur,
        """
        INSERT INTO taxonomy_unresolved_label_queue
            (field_name, raw_label, normalized_label,
             occurrence_count, distinct_call_count,
             first_seen_at, last_seen_at, source_examples)
        VALUES (%s, %s, %s, %s, %s, now(), now(), %s)
        ON CONFLICT (field_name, normalized_label) DO UPDATE SET
            raw_label           = EXCLUDED.raw_label,
            occurrence_count    = taxonomy_unresolved_label_queue.occurrence_count
                                  + EXCLUDED.occurrence_count,
            distinct_call_count = taxonomy_unresolved_label_queue.distinct_call_count
                                  + EXCLUDED.distinct_call_count,
            last_seen_at        = now(),
            source_examples     = CASE
                WHEN jsonb_array_length(
                         COALESCE(taxonomy_unresolved_label_queue.source_examples, '[]'::jsonb)
                     ) >= 10
                THEN taxonomy_unresolved_label_queue.source_examples
                ELSE (
                    taxonomy_unresolved_label_queue.source_examples
                    || EXCLUDED.source_examples
                )
            END,
            updated_at          = now()
        """,
        [
            (
                r["field_name"],
                r["raw_label"],
                r["normalized_label"],
                r["occurrence_count"],
                r["distinct_call_count"],
                _jsonb(r.get("source_examples", [])),
            )
            for r in batch
        ],
        page_size=AUDIT_INSERT_PAGE,
    )


# ── Progress updater thread ───────────────────────────────────────────────────

def _progress_updater(
    local_dsn: str,
    run_id: str,
    shared: SharedProgress,
    rows_pending_before: Optional[int],
    stop_event: threading.Event,
    interval: float,
) -> None:
    """
    Background thread: writes live progress to taxonomy_backfill_runs every
    `interval` seconds so the dashboard can poll without waiting for the run
    to finish.
    """
    while not stop_event.wait(interval):
        try:
            snap = shared.snapshot()
            eta: Optional[float] = None
            if rows_pending_before and snap["rate_rows_per_sec"] > 0:
                remaining = rows_pending_before - snap["rows_scanned"]
                eta = max(0.0, remaining / snap["rate_rows_per_sec"])

            progress_json = {**snap, "live": True}
            if eta is not None:
                progress_json["eta_seconds"] = round(eta)

            conn = connect(local_dsn)
            conn.autocommit = True
            with conn.cursor() as cur:
                cur.execute(
                    """
                    UPDATE taxonomy_backfill_runs SET
                        rows_scanned  = %s,
                        rows_changed  = %s,
                        rows_unchanged = %s,
                        rows_error    = %s,
                        summary_json  = %s
                    WHERE backfill_run_id = %s
                    """,
                    (
                        snap["rows_scanned"],
                        snap["rows_changed"],
                        snap["rows_unchanged"],
                        snap["rows_error"],
                        json.dumps(progress_json, default=str),
                        run_id,
                    ),
                )
            conn.close()
        except Exception as exc:
            logging.debug("Progress update failed: %s", exc)


# ── Worker ────────────────────────────────────────────────────────────────────

def worker_backfill(
    worker_id: int,
    args,
    run_id: str,
    selected_fields: Sequence[str],
    field_types: Dict[str, Dict[str, str]],
    lookups: Dict[str, FieldLookup],
    shared_progress: SharedProgress,
) -> WorkerResult:
    result = WorkerResult(worker_id=worker_id)

    stage_read_conn  = connect(args.stage_database_url)
    stage_write_conn = connect(args.stage_database_url)
    local_conn       = connect(args.local_database_url)

    stage_read_conn.autocommit  = False
    stage_write_conn.autocommit = False
    local_conn.autocommit       = False

    # Per-worker in-memory accumulator: one entry per unique (field, norm_label).
    # Flushed once at end of worker — single upsert batch, no ON CONFLICT subquery.
    unresolved_accum = UnresolvedAccumulator()

    cursor_name = f"backfill_v2_w{worker_id}_{int(time.time())}"

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

    # Separate flush thresholds:
    #   update_page_size  — how often to commit STAGE writes (small: 500)
    #   audit_flush_size  — how often to commit audit rows to local DB (larger: 5000)
    audit_flush_size = args.audit_flush_size

    # Whether to skip audit rows for UNCHANGED fields (dramatically cuts write volume).
    skip_unchanged_audit: bool = args.skip_unchanged_audit

    try:
        read_cur = stage_read_conn.cursor(
            name=cursor_name,
            cursor_factory=psycopg2.extras.RealDictCursor,
        )
        read_cur.itersize = args.batch_size
        read_cur.execute(select_sql, select_params)

        stage_write_cur = stage_write_conn.cursor()
        local_write_cur = local_conn.cursor()

        row_audit_buf:   List[Dict[str, Any]] = []
        field_audit_buf: List[Dict[str, Any]] = []

        # STAGE UPDATE groups — keyed by the exact tuple of changed field names.
        update_groups: Dict[Tuple[str, ...], List[Tuple[Any, ...]]] = defaultdict(list)

        # Progress delta since last shared_progress.add() call.
        _delta_scanned = _delta_changed = _delta_unchanged = _delta_error = 0

        def flush_updates() -> None:
            if args.dry_run:
                update_groups.clear()
                return
            for changed_field_set, param_list in update_groups.items():
                sql = build_grouped_update_sql(
                    args.stage_schema, args.stage_table, changed_field_set
                )
                psycopg2.extras.execute_batch(
                    stage_write_cur, sql, param_list, page_size=args.update_page_size
                )
            stage_write_conn.commit()
            result.update_batches += 1
            update_groups.clear()

        def flush_audit() -> None:
            insert_row_audit_batch(local_write_cur, run_id, row_audit_buf)
            insert_field_audit_batch(local_write_cur, run_id, field_audit_buf)
            local_conn.commit()
            row_audit_buf.clear()
            field_audit_buf.clear()

        rows_since_stage_flush = 0
        rows_since_audit_flush = 0

        while True:
            rows = read_cur.fetchmany(args.batch_size)
            if not rows:
                break

            for row in rows:
                result.rows_scanned += 1
                _delta_scanned += 1
                row_id  = str(row["id"])
                call_id = str(row["call_id"]) if row.get("call_id") else None

                try:
                    old_hash = row_hash(dict(row), selected_fields)
                    field_results: List[FieldResult] = []

                    for fn in selected_fields:
                        fr = transform_field(
                            field_name=fn,
                            original_value=row.get(fn),
                            data_type=field_types[fn]["data_type"],
                            lookups=lookups,
                        )
                        field_results.append(fr)

                    changed_frs  = [fr for fr in field_results if fr.changed]
                    unchanged_frs = [fr for fr in field_results if not fr.changed]
                    unmapped_frs  = [fr for fr in field_results if fr.unmapped_labels]

                    if changed_frs:
                        result.rows_changed += 1
                        _delta_changed += 1
                        new_values = {fr.field_name: fr.new_value for fr in changed_frs}
                        new_hash_row = dict(row)
                        new_hash_row.update(new_values)
                        new_hash = row_hash(new_hash_row, selected_fields)

                        changed_set = tuple(fr.field_name for fr in changed_frs)
                        update_groups[changed_set].append(
                            tuple(fr.new_value for fr in changed_frs) + (row["id"],)
                        )

                        row_audit_buf.append({
                            "stage_row_id":   row_id,
                            "call_id":        call_id,
                            "row_status":     "CHANGED",
                            "changed_fields":  [fr.field_name for fr in changed_frs],
                            "unchanged_fields":[fr.field_name for fr in unchanged_frs],
                            "unmapped_fields": [fr.field_name for fr in unmapped_frs],
                            "old_row_hash":   old_hash,
                            "new_row_hash":   new_hash,
                        })
                        # For CHANGED rows, always write field audit for every field.
                        for fr in field_results:
                            field_audit_buf.append({
                                "stage_row_id":       row_id,
                                "call_id":            call_id,
                                "field_name":         fr.field_name,
                                "old_value":          fr.old_value,
                                "new_value":          fr.new_value,
                                "changed":            fr.changed,
                                "field_status":       fr.field_status,
                                "mapping_method":     fr.mapping_method,
                                "mapped_display_names": fr.mapped_display_names,
                                "unmapped_labels":    fr.unmapped_labels,
                                "ambiguous_labels":   fr.ambiguous_labels,
                            })
                    else:
                        result.rows_unchanged += 1
                        _delta_unchanged += 1
                        row_audit_buf.append({
                            "stage_row_id":   row_id,
                            "call_id":        call_id,
                            "row_status":     "UNCHANGED",
                            "changed_fields":  [],
                            "unchanged_fields":[fr.field_name for fr in unchanged_frs],
                            "unmapped_fields": [fr.field_name for fr in unmapped_frs],
                            "old_row_hash":   old_hash,
                            "new_row_hash":   old_hash,
                        })
                        # For UNCHANGED rows, only write field audit for noteworthy
                        # statuses (UNMAPPED, AMBIGUOUS, ERROR) unless skip is off.
                        for fr in field_results:
                            if not skip_unchanged_audit or fr.field_status not in ("UNCHANGED", "EMPTY"):
                                field_audit_buf.append({
                                    "stage_row_id":       row_id,
                                    "call_id":            call_id,
                                    "field_name":         fr.field_name,
                                    "old_value":          fr.old_value,
                                    "new_value":          fr.new_value,
                                    "changed":            False,
                                    "field_status":       fr.field_status,
                                    "mapping_method":     fr.mapping_method,
                                    "mapped_display_names": fr.mapped_display_names,
                                    "unmapped_labels":    fr.unmapped_labels,
                                    "ambiguous_labels":   fr.ambiguous_labels,
                                })

                    # Accumulate unresolved labels in memory (no DB write here).
                    for fr in field_results:
                        for raw in fr.unmapped_labels:
                            norm = normalize_loose(raw) or normalize_exact(raw) or raw
                            result.unresolved[(fr.field_name, raw)] += 1
                            unresolved_accum.add(fr.field_name, raw, norm, call_id)

                except Exception as exc:
                    result.rows_error += 1
                    _delta_error += 1
                    logging.exception(
                        "Worker %s error on row id=%s call_id=%s: %s",
                        worker_id, row_id, call_id, exc,
                    )
                    row_audit_buf.append({
                        "stage_row_id": row_id,
                        "call_id":      call_id,
                        "row_status":   "ERROR",
                        "error_message": str(exc)[:500],
                    })

                rows_since_stage_flush += 1
                rows_since_audit_flush += 1

                if rows_since_stage_flush >= args.update_page_size:
                    flush_updates()
                    rows_since_stage_flush = 0

                if rows_since_audit_flush >= audit_flush_size:
                    flush_audit()
                    rows_since_audit_flush = 0
                    # Push live progress to the shared counter
                    shared_progress.add(_delta_scanned, _delta_changed, _delta_unchanged, _delta_error)
                    _delta_scanned = _delta_changed = _delta_unchanged = _delta_error = 0
                    logging.info(
                        "Worker %s: scanned=%s changed=%s unchanged=%s errors=%s unresolved_keys=%s",
                        worker_id,
                        result.rows_scanned, result.rows_changed,
                        result.rows_unchanged, result.rows_error,
                        len(unresolved_accum),
                    )

        # Final flushes
        flush_updates()
        flush_audit()

        # Flush all accumulated unresolved labels in a single batch.
        upsert_unresolved_batch(local_write_cur, unresolved_accum.to_batch())
        local_conn.commit()

        # Push remaining progress delta
        shared_progress.add(_delta_scanned, _delta_changed, _delta_unchanged, _delta_error)

        if args.dry_run:
            stage_write_conn.rollback()
        else:
            stage_write_conn.commit()

        read_cur.close()

    except Exception:
        stage_write_conn.rollback()
        local_conn.rollback()
        raise
    finally:
        stage_read_conn.close()
        stage_write_conn.close()
        local_conn.close()

    return result


# ── Run orchestration ─────────────────────────────────────────────────────────

def count_pending_rows(args) -> Optional[int]:
    try:
        conn = connect(args.stage_database_url)
        conn.autocommit = True
        with conn.cursor() as cur:
            if args.include_already_updated:
                cur.execute(
                    f"SELECT COUNT(*) FROM {args.stage_schema}.{args.stage_table}"
                )
            else:
                cur.execute(
                    f"SELECT COUNT(*) FROM {args.stage_schema}.{args.stage_table} "
                    f"WHERE better_tags_updated_at IS NULL"
                )
            return cur.fetchone()[0]
    except Exception as exc:
        logging.warning("Could not count pending rows: %s", exc)
        return None
    finally:
        conn.close()


def create_run_record(local_dsn: str, run_id: str, args, selected_fields: List[str]) -> None:
    conn = connect(local_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            INSERT INTO taxonomy_backfill_runs
                (backfill_run_id, dry_run, source_schema, source_table,
                 selected_fields, worker_count, batch_size, update_page_size,
                 include_already_updated, status)
            VALUES (%s,%s,%s,%s,%s,%s,%s,%s,%s,'RUNNING')
            """,
            (
                run_id,
                args.dry_run,
                args.stage_schema,
                args.stage_table,
                selected_fields,
                args.workers,
                args.batch_size,
                args.update_page_size,
                args.include_already_updated,
            ),
        )
    conn.close()


def finalize_run_record(
    local_dsn: str,
    run_id: str,
    status: str,
    rows_scanned: int,
    rows_changed: int,
    rows_unchanged: int,
    rows_error: int,
    rows_pending_before: Optional[int],
    rows_pending_after: Optional[int],
    summary: Dict[str, Any],
    error_message: Optional[str],
) -> None:
    conn = connect(local_dsn)
    conn.autocommit = True
    with conn.cursor() as cur:
        cur.execute(
            """
            UPDATE taxonomy_backfill_runs SET
                finished_at          = now(),
                status               = %s,
                rows_scanned         = %s,
                rows_changed         = %s,
                rows_unchanged       = %s,
                rows_error           = %s,
                rows_pending_before  = %s,
                rows_pending_after   = %s,
                summary_json         = %s,
                error_message        = %s
            WHERE backfill_run_id = %s
            """,
            (
                status,
                rows_scanned,
                rows_changed,
                rows_unchanged,
                rows_error,
                rows_pending_before,
                rows_pending_after,
                json.dumps(summary, default=str),
                error_message,
                run_id,
            ),
        )
    conn.close()


def run_backfill(args) -> None:
    for name in [args.stage_schema, args.stage_table, args.local_schema,
                 args.map_table, args.names_table]:
        if not IDENTIFIER_RE.match(name):
            raise ValueError(f"Invalid SQL identifier: {name!r}")

    selected_fields = list(args.fields)

    # Verify STAGE table has required columns
    stage_conn = connect(args.stage_database_url)
    stage_conn.autocommit = True
    try:
        stage_cols = get_table_columns(stage_conn, args.stage_schema, args.stage_table)
        required = {"id", "call_id", "better_tags_updated_at", *selected_fields}
        missing = required - set(stage_cols)
        if missing:
            raise RuntimeError(
                f"Missing columns in {args.stage_schema}.{args.stage_table}: {sorted(missing)}"
            )
        field_types = {f: stage_cols[f] for f in selected_fields}
    finally:
        stage_conn.close()

    # Load taxonomy lookups + optional supplemental mappings
    run_id_overrides: Dict[str, str] = {}
    if args.run_id_overrides_json:
        with open(args.run_id_overrides_json, "r", encoding="utf-8") as f:
            run_id_overrides = json.load(f)

    lookups, mapping_stats = load_taxonomy_lookups(
        local_dsn=args.local_database_url,
        local_schema=args.local_schema,
        map_table=args.map_table,
        names_table=args.names_table,
        selected_fields=selected_fields,
        run_id_overrides=run_id_overrides,
        include_anomaly_names=args.include_anomaly_names,
    )

    supplemental_stats = load_supplemental_mappings(
        supplemental_csv=args.supplemental_mapping_csv,
        lookups=lookups,
    )
    mapping_stats["supplemental_mappings"] = supplemental_stats

    # Count pending rows before run
    rows_pending_before = count_pending_rows(args)

    # Create run record
    run_id = datetime.now(timezone.utc).strftime("bfr_%Y%m%d_%H%M%S") + f"_w{args.workers}"
    create_run_record(args.local_database_url, run_id, args, selected_fields)

    logging.info("Run ID: %s  dry_run=%s  workers=%s  fields=%s",
                 run_id, args.dry_run, args.workers, selected_fields)

    started_at = datetime.now(timezone.utc)
    error_message: Optional[str] = None
    worker_results: List[WorkerResult] = []
    final_status = "DONE"

    shared_progress = SharedProgress()
    stop_progress   = threading.Event()
    progress_thread = threading.Thread(
        target=_progress_updater,
        args=(
            args.local_database_url,
            run_id,
            shared_progress,
            rows_pending_before,
            stop_progress,
            args.progress_interval,
        ),
        daemon=True,
        name="backfill-progress",
    )
    progress_thread.start()

    try:
        with ThreadPoolExecutor(max_workers=args.workers) as executor:
            futures = [
                executor.submit(
                    worker_backfill,
                    worker_id,
                    args,
                    run_id,
                    selected_fields,
                    field_types,
                    lookups,
                    shared_progress,
                )
                for worker_id in range(args.workers)
            ]
            for future in as_completed(futures):
                try:
                    worker_results.append(future.result())
                except Exception as exc:
                    logging.exception("Worker failed: %s", exc)
                    error_message = str(exc)[:500]
                    final_status = "FAILED"

    except Exception as exc:
        error_message = str(exc)[:500]
        final_status = "FAILED"
    finally:
        stop_progress.set()
        progress_thread.join(timeout=5)

    if args.dry_run and final_status != "FAILED":
        final_status = "DRY_RUN_DONE"

    finished_at = datetime.now(timezone.utc)

    total_scanned = sum(r.rows_scanned for r in worker_results)
    total_changed = sum(r.rows_changed for r in worker_results)
    total_unchanged = sum(r.rows_unchanged for r in worker_results)
    total_error = sum(r.rows_error for r in worker_results)

    rows_pending_after = count_pending_rows(args)

    summary: Dict[str, Any] = {
        "run_id": run_id,
        "dry_run": args.dry_run,
        "status": final_status,
        "started_at": started_at.isoformat(),
        "finished_at": finished_at.isoformat(),
        "duration_seconds": (finished_at - started_at).total_seconds(),
        "stage_table": f"{args.stage_schema}.{args.stage_table}",
        "selected_fields": selected_fields,
        "workers": args.workers,
        "batch_size": args.batch_size,
        "update_page_size": args.update_page_size,
        "rows_scanned": total_scanned,
        "rows_changed": total_changed,
        "rows_unchanged": total_unchanged,
        "rows_error": total_error,
        "rows_pending_before": rows_pending_before,
        "rows_pending_after": rows_pending_after,
        "mapping_stats": mapping_stats,
        "workers_detail": [
            {
                "worker_id": r.worker_id,
                "rows_scanned": r.rows_scanned,
                "rows_changed": r.rows_changed,
                "rows_unchanged": r.rows_unchanged,
                "rows_error": r.rows_error,
                "unresolved_label_count": sum(r.unresolved.values()),
            }
            for r in sorted(worker_results, key=lambda r: r.worker_id)
        ],
    }

    finalize_run_record(
        local_dsn=args.local_database_url,
        run_id=run_id,
        status=final_status,
        rows_scanned=total_scanned,
        rows_changed=total_changed,
        rows_unchanged=total_unchanged,
        rows_error=total_error,
        rows_pending_before=rows_pending_before,
        rows_pending_after=rows_pending_after,
        summary=summary,
        error_message=error_message,
    )

    # Still write audit dir files for offline review
    if args.audit_dir:
        os.makedirs(args.audit_dir, exist_ok=True)
        summary_path = os.path.join(args.audit_dir, f"backfill_v2_summary_{run_id}.json")
        with open(summary_path, "w", encoding="utf-8") as f:
            json.dump(summary, f, indent=2, ensure_ascii=False, default=str)
        logging.info("Summary written to: %s", summary_path)

    print(json.dumps(summary, indent=2, ensure_ascii=False, default=str))


# ── CLI ───────────────────────────────────────────────────────────────────────

def parse_bool(value: Any) -> bool:
    if isinstance(value, bool):
        return value
    v = str(value).strip().lower()
    if v in {"true", "1", "yes", "y"}:
        return True
    if v in {"false", "0", "no", "n"}:
        return False
    raise argparse.ArgumentTypeError(f"Invalid boolean: {value!r}")


def split_csv_arg(value: str) -> List[str]:
    return [v.strip() for v in value.split(",") if v.strip()]


def assert_identifier(value: str, label: str) -> str:
    if not IDENTIFIER_RE.match(value):
        raise ValueError(f"Invalid {label}: {value!r}")
    return value


def parse_args(argv: Optional[Sequence[str]] = None):
    parser = argparse.ArgumentParser(
        description="Column-level auditable backfill of STAGE taxonomy tags (v2)."
    )

    parser.add_argument(
        "--local-database-url",
        default=build_local_dsn(),
        help="Local taxonomy DB DSN. Defaults to LOCAL_DATABASE_URL or LOCAL_PG_* env vars.",
    )
    parser.add_argument(
        "--stage-database-url",
        default=build_stage_dsn(),
        help="STAGE DWH DSN. Defaults to STAGE_DATABASE_URL or DWH_* env vars.",
    )

    parser.add_argument("--local-schema",  default="public")
    parser.add_argument("--stage-schema",  default="public")
    parser.add_argument("--stage-table",   default="fact_call_classification")
    parser.add_argument("--map-table",     default="taxonomy_label_cluster_map")
    parser.add_argument("--names-table",   default="taxonomy_cluster_names")

    parser.add_argument(
        "--fields",
        type=split_csv_arg,
        default=DEFAULT_FIELDS,
        help="Comma-separated fields to process.",
    )
    parser.add_argument(
        "--dry-run",
        type=parse_bool,
        default=True,
        help="Default true. Pass false to write to STAGE.",
    )
    parser.add_argument(
        "--include-already-updated",
        action="store_true",
        help="Process rows where better_tags_updated_at is already set.",
    )
    parser.add_argument(
        "--include-anomaly-names",
        action="store_true",
        help="Allow mapping to anomaly cluster display names.",
    )
    parser.add_argument(
        "--supplemental-mapping-csv",
        default=None,
        help=(
            "Optional unresolved_match_audit.csv with SAME_FIELD safe mappings. "
            "Remove this flag once those mappings are migrated into taxonomy_label_cluster_map."
        ),
    )
    parser.add_argument("--workers",           type=int, default=4)
    parser.add_argument("--batch-size",        type=int, default=5000)
    parser.add_argument("--update-page-size",  type=int, default=500)
    parser.add_argument(
        "--audit-flush-size",
        type=int,
        default=5000,
        help=(
            "Flush row/field audit rows to the local DB every N rows per worker. "
            "Larger values mean fewer round-trips. Default 5000."
        ),
    )
    parser.add_argument(
        "--skip-unchanged-audit",
        action="store_true",
        default=True,
        help=(
            "Skip writing field_audit rows for UNCHANGED/EMPTY fields. "
            "UNMAPPED and AMBIGUOUS are always written. Default on."
        ),
    )
    parser.add_argument(
        "--no-skip-unchanged-audit",
        dest="skip_unchanged_audit",
        action="store_false",
        help="Write field_audit rows for all fields including UNCHANGED.",
    )
    parser.add_argument(
        "--progress-interval",
        type=float,
        default=10.0,
        help="Seconds between live progress writes to taxonomy_backfill_runs. Default 10.",
    )
    parser.add_argument(
        "--limit",
        type=int,
        default=None,
        help="Row limit per worker (for testing).",
    )
    parser.add_argument("--call-id",  default=None, help="Single call_id test.")
    parser.add_argument("--id",       default=None, help="Single row id test.")
    parser.add_argument(
        "--audit-dir",
        default="audit_backfill_v2",
        help="Directory for offline summary JSON files.",
    )
    parser.add_argument(
        "--run-id-overrides-json",
        default=None,
        help="JSON file mapping field_name -> run_id to override dynamic resolution.",
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args(argv)

    if not args.local_database_url:
        raise RuntimeError("Missing local DB. Set LOCAL_DATABASE_URL or LOCAL_PG_* env vars.")
    if not args.stage_database_url:
        raise RuntimeError("Missing STAGE DB. Set STAGE_DATABASE_URL or DWH_* env vars.")
    if args.workers < 1:
        raise RuntimeError("--workers must be >= 1")
    if args.batch_size < 1:
        raise RuntimeError("--batch-size must be >= 1")
    if args.update_page_size < 1:
        raise RuntimeError("--update-page-size must be >= 1")

    invalid = [f for f in args.fields if f not in DEFAULT_FIELDS]
    if invalid:
        raise RuntimeError(
            f"Unknown fields: {invalid}. Allowed: {DEFAULT_FIELDS}"
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
