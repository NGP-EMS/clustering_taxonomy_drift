#!/usr/bin/env python3
"""
seed_historical_unresolved_queue.py

Imports historical unresolved labels from backfill_unmapped_labels.csv into
public.taxonomy_unresolved_label_queue so the normal resolver/autopilot can
materialize them and then re-backfill already-updated STAGE rows.

CSV supported columns:
  field_name,label,count
or:
  field_name,raw_label,occurrence_count

Default is dry-run. Use --apply to write to the local taxonomy DB.
"""

from __future__ import annotations

import argparse
import csv
import json
import logging
import os
import re
from collections import defaultdict
from datetime import datetime, timezone
from typing import Any, Dict, Optional, Tuple

import psycopg2
import psycopg2.extras

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

QUEUE_TABLE = "public.taxonomy_unresolved_label_queue"
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


def _build_dsn(host, port, db, user, password) -> Optional[str]:
    if not all([host, port, db, user, password]):
        return None
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def build_local_dsn() -> Optional[str]:
    return os.getenv("LOCAL_DATABASE_URL") or _build_dsn(
        os.getenv("LOCAL_PG_HOST"), os.getenv("LOCAL_PG_PORT"),
        os.getenv("LOCAL_PG_DB"), os.getenv("LOCAL_PG_USER"),
        os.getenv("LOCAL_PG_PASSWORD"),
    )


def safe_id(name: str) -> str:
    if not IDENTIFIER_RE.match(name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def safe_table(name: str) -> str:
    parts = str(name).split(".")
    if not parts or any(not p for p in parts):
        raise ValueError(f"Unsafe SQL table name: {name!r}")
    for p in parts:
        if not IDENTIFIER_RE.match(p):
            raise ValueError(f"Unsafe SQL table name: {name!r}")
    return ".".join(f'"{p}"' for p in parts)


def normalize_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null", "na", "unknown"}:
        return ""
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"[\s_\-/]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def get_columns(conn, table_name: str) -> Dict[str, Dict[str, str]]:
    schema, table = table_name.split(".", 1) if "." in table_name else ("public", table_name)
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


def json_value_for_column(value: Any, col_meta: Dict[str, str]) -> Any:
    udt = (col_meta or {}).get("udt_name", "")
    data_type = (col_meta or {}).get("data_type", "")
    if udt in {"json", "jsonb"} or data_type in {"json", "jsonb"}:
        return psycopg2.extras.Json(value)
    return json.dumps(value, default=str)


def load_csv(path: str, min_count: int = 1) -> Dict[Tuple[str, str], Dict[str, Any]]:
    rows: Dict[Tuple[str, str], Dict[str, Any]] = {}
    with open(path, "r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            field = (row.get("field_name") or "").strip()
            raw = (row.get("label") or row.get("raw_label") or row.get("normalized_label") or "").strip()
            norm = normalize_label(raw)
            try:
                count = int(row.get("count") or row.get("occurrence_count") or 0)
            except Exception:
                count = 0
            if not field or not norm or count < min_count:
                continue
            key = (field, norm)
            if key not in rows:
                rows[key] = {"field_name": field, "raw_label": raw, "normalized_label": norm, "count": 0}
            rows[key]["count"] += count
            if raw and len(raw) < len(rows[key].get("raw_label") or "" ) or not rows[key].get("raw_label"):
                rows[key]["raw_label"] = raw
    return rows


def seed_queue(conn, rows: Dict[Tuple[str, str], Dict[str, Any]], dry_run: bool) -> Dict[str, int]:
    cols = get_columns(conn, QUEUE_TABLE)
    if not cols:
        raise RuntimeError(f"Could not read columns for {QUEUE_TABLE}")

    counts = defaultdict(int)
    qtable = safe_table(QUEUE_TABLE)

    for (_field, _norm), row in rows.items():
        field = row["field_name"]
        norm = row["normalized_label"]
        raw = row["raw_label"] or norm
        count = int(row["count"] or 0)

        evidence = {
            "seeded_from": "historical_backfill_unmapped_labels",
            "historical_count": count,
            "seeded_at": datetime.now(timezone.utc).isoformat(),
        }
        source_examples = []

        if dry_run:
            counts["would_upsert"] += 1
            continue

        # First update an existing non-materialized queue row.
        set_parts = []
        params = []
        if "raw_label" in cols:
            set_parts.append("raw_label = COALESCE(raw_label, %s)")
            params.append(raw)
        if "occurrence_count" in cols:
            set_parts.append("occurrence_count = GREATEST(COALESCE(occurrence_count, 0), %s)")
            params.append(count)
        if "distinct_call_count" in cols:
            # Historical CSV does not contain real distinct calls. Keep existing value if present.
            set_parts.append("distinct_call_count = COALESCE(distinct_call_count, 0)")
        if "resolver_status" in cols:
            # Re-open only unresolved/review rows for resolver. Already materialized rows are skipped by evidence guard below.
            set_parts.append("resolver_status = NULL")
        if "target_cluster_id" in cols:
            set_parts.append("target_cluster_id = NULL")
        if "target_display_name" in cols:
            set_parts.append("target_display_name = NULL")
        if "similarity_score" in cols:
            set_parts.append("similarity_score = NULL")
        if "actor_guard_status" in cols:
            set_parts.append("actor_guard_status = NULL")
        if "contradiction_guard_status" in cols:
            set_parts.append("contradiction_guard_status = NULL")
        if "evidence_json" in cols:
            if cols["evidence_json"].get("udt_name") in {"json", "jsonb"}:
                set_parts.append("evidence_json = COALESCE(evidence_json, '{}'::jsonb) || %s::jsonb")
                params.append(json.dumps(evidence))
            else:
                set_parts.append("evidence_json = %s")
                params.append(json.dumps(evidence))
        if "updated_at" in cols:
            set_parts.append("updated_at = NOW()")

        # Avoid re-opening rows that are already materialized.
        evidence_guard = ""
        if "evidence_json" in cols:
            evidence_guard = "AND (evidence_json IS NULL OR NOT (evidence_json::text LIKE '%\"materialized\": true%'))"

        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {qtable}
                SET {', '.join(set_parts)}
                WHERE field_name = %s
                  AND normalized_label = %s
                  {evidence_guard}
                """,
                params + [field, norm],
            )
            if cur.rowcount and cur.rowcount > 0:
                counts["updated"] += 1
                continue

        # Insert new row.
        insert_vals: Dict[str, Any] = {}
        for col, val in {
            "field_name": field,
            "raw_label": raw,
            "normalized_label": norm,
            "occurrence_count": count,
            "distinct_call_count": 0,
            "resolver_status": None,
            "target_cluster_id": None,
            "target_display_name": None,
            "similarity_score": None,
            "actor_guard_status": None,
            "contradiction_guard_status": None,
            "source_examples": source_examples,
            "evidence_json": evidence,
            "created_at": datetime.now(timezone.utc),
            "updated_at": datetime.now(timezone.utc),
        }.items():
            if col not in cols:
                continue
            if col in {"source_examples", "evidence_json"}:
                insert_vals[col] = json_value_for_column(val, cols[col])
            else:
                insert_vals[col] = val

        icols = list(insert_vals.keys())
        ivals = [insert_vals[c] for c in icols]
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {qtable} ({', '.join(safe_id(c) for c in icols)}) VALUES ({', '.join(['%s'] * len(ivals))})",
                ivals,
            )
        counts["inserted"] += 1

    if dry_run:
        conn.rollback()
    else:
        conn.commit()
    counts["total_csv_pairs"] = len(rows)
    return dict(counts)


def parse_args(argv=None):
    parser = argparse.ArgumentParser(description="Seed historical unresolved labels into taxonomy_unresolved_label_queue.")
    parser.add_argument("--csv", required=True, help="Path to backfill_unmapped_labels.csv")
    parser.add_argument("--local-database-url", default=build_local_dsn())
    parser.add_argument("--min-count", type=int, default=1, help="Only seed labels with count >= this value.")
    parser.add_argument("--apply", action="store_true", default=False, help="Write rows. Default is dry-run.")
    parser.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    return parser.parse_args(argv)


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(level=getattr(logging, args.log_level), format="%(asctime)s | %(levelname)s | %(message)s")
    if not args.local_database_url:
        raise RuntimeError("Missing local DB. Set LOCAL_DATABASE_URL or LOCAL_PG_* env vars.")

    rows = load_csv(args.csv, args.min_count)
    logging.info("Loaded %d unique historical unresolved field/label pairs from %s", len(rows), args.csv)
    conn = psycopg2.connect(args.local_database_url)
    conn.autocommit = False
    try:
        counts = seed_queue(conn, rows, dry_run=not args.apply)
        logging.info("Seed result: %s", counts)
        print(json.dumps({"dry_run": not args.apply, **counts}, indent=2))
    finally:
        conn.close()
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
