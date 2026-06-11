#!/usr/bin/env python3
"""
taxonomy_stage_autopilot.py

End-to-end orchestration of the STAGE taxonomy cleanup loop.

  Phase 1 — Backfill STAGE rows whose labels already exist in taxonomy_label_cluster_map
  Phase 2 — Run the unresolved label resolver (writes recommendations to queue)
  Phase 3 — Materialize resolver decisions:
               MAP_TO_EXISTING → insert/update taxonomy_label_cluster_map
               ANOMALY         → create a per-label anomaly cluster (no shared bucket)
               PROMOTE         → promote to standard cluster if thresholds met,
                                 else mark MANUAL_REVIEW_NEEDED
  Phase 4 — Targeted re-backfill for call IDs affected by Phase 3 materializations
  Phase 5 — Write audit JSON to --audit-dir

Safety:
  Default mode is dry-run — reads and proposals only, no writes.
  Pass --apply to execute writes.
  Each phase is idempotent; safe to re-run if interrupted.
  Phases can be skipped via --skip-phases (comma-separated: 1,2,3,4,5).

Example:
  python taxonomy_stage_autopilot.py
  python taxonomy_stage_autopilot.py --apply
  python taxonomy_stage_autopilot.py --apply --fields outcome_sub,call_type
  python taxonomy_stage_autopilot.py --apply --skip-phases 1,2 --fields outcome_sub

Validation case:
  outcome_sub / Discovery_Multi_Meter  →  per-label ANOMALY cluster:
    display name "Discovery Multi Meter", active in taxonomy_clusters,
    taxonomy_cluster_names row, taxonomy_label_cluster_map row,
    STAGE rows updated in Phase 4.

Env:
  LOCAL_DATABASE_URL  or  LOCAL_PG_HOST/PORT/DB/USER/PASSWORD
  STAGE_DATABASE_URL  or  DWH_PG_HOST/PORT/DB/USER/PASSWORD  (needed for phases 1/4)
"""

from __future__ import annotations

import argparse
import hashlib
import json
import logging
import os
import re
import sys
import time
from collections import defaultdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, List, Optional, Set, Tuple

import psycopg2
import psycopg2.extras

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass

log = logging.getLogger("autopilot")

# ── table name constants ───────────────────────────────────────────────────────

CLUSTER_TABLE    = "taxonomy_clusters"
CLUSTER_NAME_TABLE = "taxonomy_cluster_names"
LABEL_MAP_TABLE  = "taxonomy_label_cluster_map"
QUEUE_TABLE      = "taxonomy_unresolved_label_queue"

DEFAULT_FIELDS = [
    "call_type",
    "call_type_sub",
    "main_reason",
    "main_reason_sub",
    "outcome",
    "outcome_sub",
    "next_step",
    "coaching_tags",
    "additional_tags",
    "descriptive_keywords",
]

AUTOPILOT_CLUSTER_VERSION = f"autopilot_{datetime.now(timezone.utc).strftime('%Y%m%d')}"

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")


# ── connection helpers ─────────────────────────────────────────────────────────

def _build_dsn(host, port, db, user, password) -> Optional[str]:
    if not all([host, port, db, user, password]):
        return None
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def build_local_dsn() -> Optional[str]:
    return os.getenv("LOCAL_DATABASE_URL") or _build_dsn(
        os.getenv("LOCAL_PG_HOST"), os.getenv("LOCAL_PG_PORT"),
        os.getenv("LOCAL_PG_DB"),   os.getenv("LOCAL_PG_USER"),
        os.getenv("LOCAL_PG_PASSWORD"),
    )


def build_stage_dsn() -> Optional[str]:
    return os.getenv("STAGE_DATABASE_URL") or _build_dsn(
        os.getenv("DWH_PG_HOST"), os.getenv("DWH_PG_PORT"),
        os.getenv("DWH_PG_DB"),   os.getenv("DWH_PG_USER"),
        os.getenv("DWH_PG_PASSWORD"),
    )


# ── schema introspection ───────────────────────────────────────────────────────

def table_exists(conn, table_name: str) -> bool:
    schema, table = (table_name.split(".", 1) if "." in table_name else ("public", table_name))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT 1 FROM information_schema.tables WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        return bool(cur.fetchone())


def get_columns(conn, table_name: str) -> Set[str]:
    schema, table = (table_name.split(".", 1) if "." in table_name else ("public", table_name))
    with conn.cursor() as cur:
        cur.execute(
            "SELECT column_name FROM information_schema.columns WHERE table_schema=%s AND table_name=%s",
            (schema, table),
        )
        return {r[0] for r in cur.fetchall()}


def get_not_null_columns(conn, table_name: str) -> Set[str]:
    """Return column names with a NOT NULL constraint and no server-side default."""
    schema, table = (table_name.split(".", 1) if "." in table_name else ("public", table_name))
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
              AND is_nullable = 'NO'
              AND column_default IS NULL
            """,
            (schema, table),
        )
        return {r[0] for r in cur.fetchall()}


def preflight_not_null_check(
    conn,
    table_name: str,
    proposed: Dict[str, Any],
    context: str = "",
) -> List[str]:
    """
    Returns a list of NOT NULL columns that are absent or None in proposed.
    An empty list means the insert is safe to attempt.
    """
    if not table_exists(conn, table_name):
        return []
    violations = [c for c in get_not_null_columns(conn, table_name) if proposed.get(c) is None]
    if violations:
        log.warning(
            "Preflight NOT NULL violation in %s%s: %s",
            table_name, f" [{context}]" if context else "", violations,
        )
    return violations


def safe_id(name: str) -> str:
    if not IDENTIFIER_RE.match(name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def safe_table(name: str) -> str:
    parts = str(name).split(".")
    if not parts or any(not p for p in parts):
        raise ValueError(f"Unsafe SQL table name: {name!r}")
    return ".".join(f'"{p}"' for p in parts)


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


# ── cluster ID helpers ─────────────────────────────────────────────────────────

def stable_hash(*parts: Any, length: int = 18) -> str:
    payload = "||".join(str(p or "") for p in parts)
    return hashlib.sha256(payload.encode()).hexdigest()[:length]


def make_anomaly_cluster_id(field_name: str, normalized_label: str) -> str:
    return f"autopilot_anom_{stable_hash(field_name, normalized_label)}"


def normalize_label(value: Any) -> str:
    text = str(value or "").strip()
    if not text or text.lower() in {"nan", "none", "null", "na", "unknown"}:
        return ""
    text = re.sub(r"[\s_\-/]+", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip().lower()


def display_name_from_label(value: Any, max_words: int = 6) -> str:
    normalized = normalize_label(value)
    words = [w for w in normalized.split() if w]
    if not words:
        return "Unknown"
    words = words[:max_words]
    out = []
    for word in words:
        if word.isupper() and len(word) <= 5:
            out.append(word)
        else:
            out.append(word.capitalize())
    return " ".join(out)


# ── active run_id resolution ───────────────────────────────────────────────────

def resolve_active_run_ids(conn, fields: List[str]) -> Dict[str, str]:
    """
    Resolve the active run_id per field using the same 3-attempt strategy as
    backfill_clean_taxonomy_to_stage_v2.resolve_active_run_ids():

      1. taxonomy_mapper_runs — latest active, finished run per field.
      2. taxonomy_run_metadata — latest run per field.
      3. Majority vote in taxonomy_label_cluster_map itself.
      4. Fall back to AUTOPILOT_CLUSTER_VERSION so run_id is never NULL.

    Returns {field_name: run_id}.
    """
    run_ids: Dict[str, str] = {}
    if not fields:
        return run_ids

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
                (fields,),
            )
            for row in cur.fetchall():
                if row["run_id"]:
                    run_ids[row["field_name"]] = row["run_id"]
        except Exception:
            conn.rollback()

        # Attempt 2: taxonomy_run_metadata
        missing = [f for f in fields if f not in run_ids]
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

        # Attempt 3: majority vote in taxonomy_label_cluster_map
        missing = [f for f in fields if f not in run_ids]
        if missing and table_exists(conn, LABEL_MAP_TABLE):
            try:
                cur.execute(
                    f"""
                    SELECT field_name, run_id
                    FROM (
                        SELECT field_name, run_id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY field_name
                                   ORDER BY COUNT(*) DESC
                               ) AS rn
                        FROM {safe_table(LABEL_MAP_TABLE)}
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

    # Fallback: any field still missing gets AUTOPILOT_CLUSTER_VERSION so run_id is never NULL
    for f in fields:
        if f not in run_ids:
            log.warning(
                "Could not resolve active run_id for field '%s' — falling back to %s",
                f, AUTOPILOT_CLUSTER_VERSION,
            )
            run_ids[f] = AUTOPILOT_CLUSTER_VERSION

    log.info("Resolved active run_ids: %s", run_ids)
    return run_ids


# ── label_map upsert (mirrors weekly_taxonomy_maintenance.upsert_label_map) ────

def upsert_label_map(
    conn,
    *,
    field_name: str,
    raw_label: str,
    normalized_label: str,
    cluster_id: str,
    run_id: str,
    cluster_version: str,
    display_name: str,
    value_count: int,
    is_true_anomaly: bool,
    final_cluster_source: Optional[str] = None,
    dry_run: bool = True,
) -> bool:
    """Returns True if the row was written (or would be in dry-run)."""
    if not table_exists(conn, LABEL_MAP_TABLE):
        log.warning("taxonomy_label_cluster_map does not exist — skipping upsert")
        return False
    cols = get_columns(conn, LABEL_MAP_TABLE)
    source = final_cluster_source or ("true_anomaly" if is_true_anomaly else "standard_cluster")
    base_id = "-1" if is_true_anomaly else cluster_id

    if dry_run:
        log.info(
            "[DRY-RUN] label_map: field=%s raw=%r norm=%r run_id=%s cluster=%s display=%r",
            field_name, raw_label, normalized_label, run_id, cluster_id, display_name,
        )
        return True

    update_assignments: Dict[str, Any] = {
        "raw_label": raw_label,
        "final_cluster_id": cluster_id,
        "final_cluster_source": source,
        "base_cluster_id": base_id,
        "run_id": run_id,
        "cluster_version": cluster_version,
        "display_name": display_name,
        "value_count": value_count,
        "final_is_true_anomaly": is_true_anomaly,
        "updated_at": utcnow(),
    }
    set_parts = []
    params: List[Any] = []
    for col, val in update_assignments.items():
        if col in cols:
            set_parts.append(f"{safe_id(col)} = %s")
            params.append(val)

    if set_parts:
        params += [field_name, normalized_label]
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {safe_table(LABEL_MAP_TABLE)} SET {', '.join(set_parts)} "
                f"WHERE field_name = %s AND normalized_label = %s",
                params,
            )
            if cur.rowcount and cur.rowcount > 0:
                return True

    insert_vals: Dict[str, Any] = {
        "field_name": field_name,
        "raw_label": raw_label,
        "normalized_label": normalized_label,
        "final_cluster_id": cluster_id,
        "final_cluster_source": source,
        "base_cluster_id": base_id,
        "run_id": run_id,
        "cluster_version": cluster_version,
        "display_name": display_name,
        "value_count": value_count,
        "final_is_true_anomaly": is_true_anomaly,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    # Preflight: catch NOT NULL violations before the INSERT fires
    violations = preflight_not_null_check(
        conn, LABEL_MAP_TABLE, insert_vals,
        context=f"{field_name}/{normalized_label}",
    )
    if violations:
        raise ValueError(
            f"NOT NULL violation in {LABEL_MAP_TABLE} for {field_name}/{normalized_label}: "
            f"missing columns {violations}"
        )

    icols = [c for c in insert_vals if c in cols]
    ivals = [insert_vals[c] for c in icols]
    if icols:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {safe_table(LABEL_MAP_TABLE)} "
                f"({', '.join(safe_id(c) for c in icols)}) "
                f"VALUES ({', '.join(['%s'] * len(ivals))})",
                ivals,
            )
    return True


# ── cluster_name upsert (mirrors weekly_taxonomy_maintenance.upsert_cluster_name) ─

def upsert_cluster_name(
    conn,
    *,
    field_name: str,
    cluster_id: str,
    cluster_version: str,
    display_name: str,
    is_anomaly: bool,
    naming_method: str,
    naming_reason: str,
    dry_run: bool = True,
) -> None:
    if not table_exists(conn, CLUSTER_NAME_TABLE):
        return
    cols = get_columns(conn, CLUSTER_NAME_TABLE)
    if not {"field_name", "cluster_id", "display_name"}.issubset(cols):
        return

    if dry_run:
        log.info(
            "[DRY-RUN] would upsert cluster_name: %s / %s → %r (anomaly=%s)",
            field_name, cluster_id, display_name, is_anomaly,
        )
        return

    set_vals: Dict[str, Any] = {
        "display_name": display_name,
        "is_anomaly": is_anomaly,
        "naming_method": naming_method,
        "naming_reason": naming_reason,
        "active": True,
        "updated_at": utcnow(),
    }
    set_parts = []
    params: List[Any] = []
    for col, val in set_vals.items():
        if col in cols:
            set_parts.append(f"{safe_id(col)} = %s")
            params.append(val)

    where = "field_name = %s AND cluster_id = %s"
    params += [field_name, cluster_id]
    if "cluster_version" in cols:
        where += " AND cluster_version = %s"
        params.append(cluster_version)

    with conn.cursor() as cur:
        if set_parts:
            cur.execute(
                f"UPDATE {safe_table(CLUSTER_NAME_TABLE)} SET {', '.join(set_parts)} WHERE {where}",
                params,
            )
            if cur.rowcount and cur.rowcount > 0:
                return

    insert_vals: Dict[str, Any] = {
        "field_name": field_name,
        "run_id": cluster_version,
        "cluster_version": cluster_version,
        "cluster_id": cluster_id,
        "is_anomaly": is_anomaly,
        "display_name": display_name,
        "naming_method": naming_method,
        "naming_reason": naming_reason,
        "active": True,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    icols = [c for c in insert_vals if c in cols]
    ivals = [insert_vals[c] for c in icols]
    if icols:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {safe_table(CLUSTER_NAME_TABLE)} "
                f"({', '.join(safe_id(c) for c in icols)}) "
                f"VALUES ({', '.join(['%s'] * len(ivals))})",
                ivals,
            )


# ── anomaly cluster upsert ─────────────────────────────────────────────────────

def create_anomaly_cluster(
    conn,
    *,
    field_name: str,
    raw_label: str,
    normalized_label: str,
    cluster_id: str,
    run_id: str,
    cluster_version: str,
    display_name: str,
    occurrence_count: int,
    distinct_call_count: int,
    source_examples: List[str],
    dry_run: bool = True,
) -> Dict[str, Any]:
    """
    Idempotently creates or updates a per-label anomaly cluster.
    run_id   — the active clustering run for this field (used in all three tables).
    cluster_version — the autopilot run version (used for taxonomy_clusters metadata).
    """
    if not table_exists(conn, CLUSTER_TABLE):
        log.warning("taxonomy_clusters does not exist — skipping anomaly cluster creation")
        return {"cluster_id": cluster_id, "display_name": display_name, "action": "skipped_no_table"}

    cols = get_columns(conn, CLUSTER_TABLE)

    if dry_run:
        log.info(
            "[DRY-RUN] anomaly cluster: field=%s norm=%r cluster_id=%s run_id=%s display=%r",
            field_name, normalized_label, cluster_id, run_id, display_name,
        )
        upsert_cluster_name(
            conn, field_name=field_name, cluster_id=cluster_id,
            cluster_version=cluster_version, display_name=display_name,
            is_anomaly=True, naming_method="autopilot_deterministic_anomaly",
            naming_reason="Autopilot anomaly cluster from resolver queue (dry-run).",
            dry_run=True,
        )
        upsert_label_map(
            conn, field_name=field_name, raw_label=raw_label,
            normalized_label=normalized_label, cluster_id=cluster_id,
            run_id=run_id, cluster_version=cluster_version, display_name=display_name,
            value_count=max(1, occurrence_count), is_true_anomaly=True,
            dry_run=True,
        )
        return {"cluster_id": cluster_id, "display_name": display_name, "action": "dry_run"}

    # taxonomy_clusters: use cluster_version as run_id for the cluster row itself
    base_values: Dict[str, Any] = {
        "field_name": field_name,
        "cluster_id": cluster_id,
        "cluster_version": cluster_version,
        "run_id": cluster_version,
        "display_name": display_name,
        "cluster_name": display_name,
        "cluster_source": "true_anomaly",
        "medoid_label": raw_label or normalized_label,
        "cluster_size": 1,
        "total_occurrences": max(1, occurrence_count),
        "is_true_anomaly_cluster": True,
        "active": True,
        "promotion_status": "ACTIVE_TRUE_ANOMALY",
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }

    update_cols = [
        "display_name", "cluster_name", "cluster_source", "medoid_label",
        "cluster_size", "total_occurrences", "is_true_anomaly_cluster", "active",
        "promotion_status", "updated_at",
    ]
    set_parts: List[str] = []
    params: List[Any] = []
    for col in update_cols:
        if col in cols:
            set_parts.append(f"{safe_id(col)} = %s")
            params.append(base_values[col])

    action = "updated"
    with conn.cursor() as cur:
        if set_parts:
            cur.execute(
                f"UPDATE {safe_table(CLUSTER_TABLE)} SET {', '.join(set_parts)} "
                f"WHERE field_name = %s AND cluster_id = %s",
                params + [field_name, cluster_id],
            )
            if not (cur.rowcount and cur.rowcount > 0):
                icols = [c for c in base_values if c in cols]
                ivals = [base_values[c] for c in icols]
                if icols:
                    cur.execute(
                        f"INSERT INTO {safe_table(CLUSTER_TABLE)} "
                        f"({', '.join(safe_id(c) for c in icols)}) "
                        f"VALUES ({', '.join(['%s'] * len(ivals))})",
                        ivals,
                    )
                action = "created"

    upsert_cluster_name(
        conn, field_name=field_name, cluster_id=cluster_id,
        cluster_version=cluster_version, display_name=display_name,
        is_anomaly=True, naming_method="autopilot_deterministic_anomaly",
        naming_reason="Autopilot anomaly cluster materialized from unresolved resolver queue.",
        dry_run=False,
    )
    # taxonomy_label_cluster_map: use the resolved active run_id for the field
    upsert_label_map(
        conn, field_name=field_name, raw_label=raw_label,
        normalized_label=normalized_label, cluster_id=cluster_id,
        run_id=run_id, cluster_version=cluster_version, display_name=display_name,
        value_count=max(1, occurrence_count), is_true_anomaly=True,
        dry_run=False,
    )
    return {"cluster_id": cluster_id, "display_name": display_name, "action": action}


# ── MAP_TO_EXISTING materialization ───────────────────────────────────────────

def materialize_map_to_existing(
    conn,
    row: Dict[str, Any],
    run_id: str,
    cluster_version: str,
    dry_run: bool,
) -> Dict[str, Any]:
    """
    Verify the recommended cluster_id + field_name exists, then insert/update
    taxonomy_label_cluster_map so the label is resolved on the next backfill.
    run_id is the resolved active run for this field.
    """
    field_name       = row["field_name"]
    normalized_label = row["normalized_label"]
    raw_label        = row.get("raw_label") or normalized_label
    # Accept both real schema name (target_*) and legacy alias (recommended_*)
    rec_cluster_id   = row.get("target_cluster_id") or row.get("recommended_cluster_id")
    rec_display_name = (
        row.get("target_display_name") or row.get("recommended_display_name")
        or display_name_from_label(raw_label)
    )
    occurrence_count = int(row.get("occurrence_count") or 1)

    if not rec_cluster_id:
        return {"status": "skipped", "reason": "missing target_cluster_id"}

    if not table_exists(conn, CLUSTER_TABLE):
        return {"status": "skipped", "reason": "taxonomy_clusters missing"}

    # Verify cluster exists for this field
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT cluster_id,
                   COALESCE(n.display_name, c.display_name, c.cluster_id) AS display_name,
                   c.cluster_version, c.run_id, c.is_true_anomaly_cluster, c.active
            FROM {safe_table(CLUSTER_TABLE)} c
            LEFT JOIN {safe_table(CLUSTER_NAME_TABLE)} n
              ON n.field_name = c.field_name AND n.cluster_id = c.cluster_id
            WHERE c.field_name = %s AND c.cluster_id = %s
            ORDER BY COALESCE(c.active, TRUE) DESC
            LIMIT 1
            """,
            (field_name, rec_cluster_id),
        )
        cluster_row = cur.fetchone()

    if not cluster_row:
        return {"status": "skipped", "reason": f"cluster_id {rec_cluster_id} not found for field {field_name}"}

    if cluster_row.get("is_true_anomaly_cluster"):
        return {"status": "skipped", "reason": "recommended cluster is an anomaly cluster — use ANOMALY action instead"}

    display_name = rec_display_name or str(cluster_row.get("display_name") or rec_cluster_id)
    # Prefer the cluster's own run_id; fall back to the field-resolved run_id
    effective_run_id = str(cluster_row.get("run_id") or run_id)
    cv = str(cluster_row.get("cluster_version") or cluster_version)

    upsert_label_map(
        conn, field_name=field_name, raw_label=raw_label,
        normalized_label=normalized_label, cluster_id=rec_cluster_id,
        run_id=effective_run_id, cluster_version=cv, display_name=display_name,
        value_count=occurrence_count, is_true_anomaly=False,
        final_cluster_source="autopilot_map_to_existing",
        dry_run=dry_run,
    )
    return {
        "status": "dry_run" if dry_run else "applied",
        "cluster_id": rec_cluster_id,
        "display_name": display_name,
        "run_id": effective_run_id,
        "field_name": field_name,
        "normalized_label": normalized_label,
    }


# ── PROMOTE materialization ────────────────────────────────────────────────────

_PROMOTE_THRESHOLDS: Dict[str, Dict[str, int]] = {
    "call_type":     {"distinct_call_count": 50, "total_occurrences": 200, "weeks_seen": 4},
    "outcome_sub":   {"distinct_call_count": 30, "total_occurrences": 100, "weeks_seen": 3},
    "main_reason":   {"distinct_call_count": 40, "total_occurrences": 150, "weeks_seen": 3},
}
_DEFAULT_PROMOTE_THRESHOLD = {"distinct_call_count": 30, "total_occurrences": 100, "weeks_seen": 2}


def check_promotion_threshold(field_name: str, row: Dict[str, Any]) -> Tuple[bool, str]:
    cfg = _PROMOTE_THRESHOLDS.get(field_name, _DEFAULT_PROMOTE_THRESHOLD)
    calls       = int(row.get("distinct_call_count") or 0)
    occurrences = int(row.get("occurrence_count")    or 0)
    # weeks_seen not tracked in queue; use occurrence proxy
    reasons = []
    if calls >= cfg["distinct_call_count"]:
        reasons.append(f"distinct_call_count {calls} >= {cfg['distinct_call_count']}")
    if occurrences >= cfg["total_occurrences"]:
        reasons.append(f"occurrence_count {occurrences} >= {cfg['total_occurrences']}")
    met = len(reasons) >= 1
    return met, "; ".join(reasons) if reasons else "below threshold"


def materialize_promote(
    conn,
    row: Dict[str, Any],
    run_id: str,
    cluster_version: str,
    dry_run: bool,
) -> Dict[str, Any]:
    field_name       = row["field_name"]
    normalized_label = row["normalized_label"]
    raw_label        = row.get("raw_label") or normalized_label
    occurrence_count = int(row.get("occurrence_count") or 1)

    met, reason = check_promotion_threshold(field_name, row)

    if not met:
        if not dry_run:
            if table_exists(conn, QUEUE_TABLE):
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        UPDATE {safe_table(QUEUE_TABLE)}
                        SET resolver_notes = %s,
                            updated_at = NOW()
                        WHERE field_name = %s AND normalized_label = %s
                          AND resolver_status = 'PROMOTE'
                        """,
                        (f"MANUAL_REVIEW_NEEDED: {reason}", field_name, normalized_label),
                    )
        return {
            "status": "manual_review_needed",
            "reason": reason,
            "field_name": field_name,
            "normalized_label": normalized_label,
        }

    # Threshold met — create a standard (non-anomaly) cluster
    cluster_id   = f"autopilot_promo_{stable_hash(field_name, normalized_label)}"
    display_name = display_name_from_label(raw_label)

    if not table_exists(conn, CLUSTER_TABLE):
        return {"status": "skipped", "reason": "taxonomy_clusters missing"}

    cols = get_columns(conn, CLUSTER_TABLE)

    if dry_run:
        log.info(
            "[DRY-RUN] promote: field=%s norm=%r cluster_id=%s run_id=%s display=%r (%s)",
            field_name, normalized_label, cluster_id, run_id, display_name, reason,
        )
        upsert_cluster_name(
            conn, field_name=field_name, cluster_id=cluster_id,
            cluster_version=cluster_version, display_name=display_name,
            is_anomaly=False, naming_method="autopilot_deterministic_promoted",
            naming_reason=f"Autopilot promoted standard cluster. {reason}",
            dry_run=True,
        )
        upsert_label_map(
            conn, field_name=field_name, raw_label=raw_label,
            normalized_label=normalized_label, cluster_id=cluster_id,
            run_id=run_id, cluster_version=cluster_version, display_name=display_name,
            value_count=occurrence_count, is_true_anomaly=False,
            final_cluster_source="autopilot_promoted",
            dry_run=True,
        )
        return {"status": "dry_run", "cluster_id": cluster_id, "display_name": display_name, "run_id": run_id, "reason": reason}

    base_values: Dict[str, Any] = {
        "field_name": field_name,
        "cluster_id": cluster_id,
        "cluster_version": cluster_version,
        "run_id": cluster_version,
        "display_name": display_name,
        "cluster_name": display_name,
        "cluster_source": "autopilot_promoted",
        "medoid_label": raw_label or normalized_label,
        "cluster_size": 1,
        "total_occurrences": occurrence_count,
        "is_true_anomaly_cluster": False,
        "active": True,
        "promotion_status": "PROMOTED_TO_STANDARD",
        "promotion_candidate_reason": reason,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }

    update_cols = [
        "display_name", "cluster_name", "cluster_source", "medoid_label",
        "cluster_size", "total_occurrences", "is_true_anomaly_cluster", "active",
        "promotion_status", "promotion_candidate_reason", "updated_at",
    ]
    set_parts: List[str] = []
    params: List[Any] = []
    for col in update_cols:
        if col in cols:
            set_parts.append(f"{safe_id(col)} = %s")
            params.append(base_values[col])

    with conn.cursor() as cur:
        if set_parts:
            cur.execute(
                f"UPDATE {safe_table(CLUSTER_TABLE)} SET {', '.join(set_parts)} "
                f"WHERE field_name = %s AND cluster_id = %s",
                params + [field_name, cluster_id],
            )
            if not (cur.rowcount and cur.rowcount > 0):
                icols = [c for c in base_values if c in cols]
                ivals = [base_values[c] for c in icols]
                if icols:
                    cur.execute(
                        f"INSERT INTO {safe_table(CLUSTER_TABLE)} "
                        f"({', '.join(safe_id(c) for c in icols)}) "
                        f"VALUES ({', '.join(['%s'] * len(ivals))})",
                        ivals,
                    )

    upsert_cluster_name(
        conn, field_name=field_name, cluster_id=cluster_id,
        cluster_version=cluster_version, display_name=display_name,
        is_anomaly=False, naming_method="autopilot_deterministic_promoted",
        naming_reason=f"Autopilot promoted standard cluster. {reason}",
        dry_run=False,
    )
    upsert_label_map(
        conn, field_name=field_name, raw_label=raw_label,
        normalized_label=normalized_label, cluster_id=cluster_id,
        run_id=run_id, cluster_version=cluster_version, display_name=display_name,
        value_count=occurrence_count, is_true_anomaly=False,
        final_cluster_source="autopilot_promoted",
        dry_run=False,
    )
    return {
        "status": "applied",
        "cluster_id": cluster_id,
        "display_name": display_name,
        "run_id": run_id,
        "reason": reason,
    }


# ── mark queue rows as materialized ───────────────────────────────────────────

def mark_queue_row_materialized(
    conn,
    field_name: str,
    normalized_label: str,
    resolver_status: str,
    materialized_cluster_id: Optional[str],
    materialized_display_name: Optional[str],
    dry_run: bool,
) -> None:
    """
    Record materialisation results back into taxonomy_unresolved_label_queue.

    Uses only columns that actually exist (schema-introspected):
    - target_cluster_id    → materialized cluster_id  (if column present)
    - target_display_name  → materialized display name (if column present)
    - evidence_json        → merged with materialization metadata (if column present)
    - updated_at           → NOW()  (always)

    resolver_status is intentionally left unchanged (stays ANOMALY / MAP_TO_EXISTING / PROMOTE).
    There is no materialized_at column in this schema.
    """
    if dry_run or not table_exists(conn, QUEUE_TABLE):
        return
    cols = get_columns(conn, QUEUE_TABLE)

    set_parts: List[str] = []
    params: List[Any] = []

    if "target_cluster_id" in cols and materialized_cluster_id:
        set_parts.append("target_cluster_id = %s")
        params.append(materialized_cluster_id)

    if "target_display_name" in cols and materialized_display_name:
        set_parts.append("target_display_name = %s")
        params.append(materialized_display_name)

    if "evidence_json" in cols:
        patch = {
            "materialized": True,
            "materialized_cluster_id": materialized_cluster_id,
            "materialized_display_name": materialized_display_name,
            "materialized_action": resolver_status,
            "materialized_at": utcnow().isoformat(),
        }
        # Merge patch into existing evidence_json via PostgreSQL jsonb concatenation
        set_parts.append("evidence_json = COALESCE(evidence_json, '{}'::jsonb) || %s::jsonb")
        params.append(json.dumps(patch))

    set_parts.append("updated_at = NOW()")

    params += [field_name, normalized_label, resolver_status]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {safe_table(QUEUE_TABLE)}
            SET {', '.join(set_parts)}
            WHERE field_name = %s
              AND normalized_label = %s
              AND resolver_status = %s
            """,
            params,
        )


# ── Phase 1 — STAGE backfill ──────────────────────────────────────────────────

def phase1_backfill(args, phase_results: Dict[str, Any]) -> Dict[str, Any]:
    log.info("=== Phase 1: STAGE backfill ===")
    t0 = time.monotonic()

    try:
        import backfill_clean_taxonomy_to_stage_v2 as bv2
    except ImportError as exc:
        return {"status": "skipped", "reason": f"cannot import backfill_clean_taxonomy_to_stage_v2: {exc}"}

    argv = [
        "--dry-run", "false" if args.apply else "true",
        "--workers", str(args.workers),
        "--fields", ",".join(args.fields),
        "--progress-interval", "15",
        "--skip-unchanged-audit",
        "--audit-dir", str(args.audit_dir),
    ]
    if args.local_database_url:
        argv += ["--local-database-url", args.local_database_url]
    if args.stage_database_url:
        argv += ["--stage-database-url", args.stage_database_url]

    try:
        bargs = bv2.parse_args(argv)
        bv2.run_backfill(bargs)
        elapsed = time.monotonic() - t0
        return {"status": "ok", "elapsed_sec": round(elapsed, 1)}
    except SystemExit as exc:
        return {"status": "error", "reason": f"SystemExit({exc.code})", "elapsed_sec": round(time.monotonic() - t0, 1)}
    except Exception as exc:
        log.exception("Phase 1 error: %s", exc)
        return {"status": "error", "reason": str(exc), "elapsed_sec": round(time.monotonic() - t0, 1)}


# ── Phase 2 helpers ───────────────────────────────────────────────────────────

def _recs_to_queue_rows(
    recommendations: List[Any],
    labels: List[Dict[str, Any]],
) -> List[Dict[str, Any]]:
    """
    Convert Phase 2 Recommendation objects + parallel label dicts into the same
    shape as rows returned by _load_queue_rows(), so Phase 3 can process them
    in dry-run mode without requiring a DB write from Phase 2.
    """
    rows = []
    for rec, lbl in zip(recommendations, labels):
        if rec.resolver_status is None:
            continue
        rows.append({
            "field_name":         rec.field_name,
            "normalized_label":   rec.normalized_label,
            "raw_label":          lbl.get("raw_label") or rec.normalized_label,
            "resolver_status":    rec.resolver_status,
            "target_cluster_id":  rec.target_cluster_id,
            "target_display_name": rec.target_display_name,
            "occurrence_count":   lbl.get("occurrence_count") or 1,
            "distinct_call_count": lbl.get("distinct_call_count") or 0,
            "source_examples":    lbl.get("source_examples") or [],
            "evidence_json":      rec.evidence or {},
            "similarity_score":   rec.similarity_score,
        })
    return rows


# ── Phase 2 — Resolver ────────────────────────────────────────────────────────

def phase2_resolve(args, phase_results: Dict[str, Any]) -> Dict[str, Any]:
    log.info("=== Phase 2: Unresolved label resolver ===")
    t0 = time.monotonic()

    try:
        import taxonomy_unresolved_label_resolver as resolver
    except ImportError as exc:
        return {"status": "skipped", "reason": f"cannot import taxonomy_unresolved_label_resolver: {exc}"}

    argv = []
    if args.apply:
        argv += ["--apply"]
    if args.fields:
        argv += ["--fields"] + list(args.fields)
    if args.local_database_url:
        argv += ["--local-database-url", args.local_database_url]
    if args.embedding_device:
        argv += ["--embedding-device", args.embedding_device]

    try:
        rargs = resolver.parse_args(argv)
        result = resolver.run_resolver(rargs)
        elapsed = time.monotonic() - t0
        if isinstance(result, tuple) and len(result) == 2:
            recs, lbls = result
            phase_results["phase2_queue_rows"] = _recs_to_queue_rows(recs, lbls)
            log.info(
                "Phase 2 captured %d in-memory rows for dry-run Phase 3",
                len(phase_results["phase2_queue_rows"]),
            )
        return {"status": "ok", "elapsed_sec": round(elapsed, 1)}
    except SystemExit as exc:
        return {"status": "error", "reason": f"SystemExit({exc.code})", "elapsed_sec": round(time.monotonic() - t0, 1)}
    except Exception as exc:
        log.exception("Phase 2 error: %s", exc)
        return {"status": "error", "reason": str(exc), "elapsed_sec": round(time.monotonic() - t0, 1)}


# ── Phase 3 — Materialize ─────────────────────────────────────────────────────

def _load_queue_rows(conn, fields: List[str]) -> List[Dict[str, Any]]:
    if not table_exists(conn, QUEUE_TABLE):
        return []
    cols = get_columns(conn, QUEUE_TABLE)

    # Use actual column names from the real schema.
    # target_cluster_id / target_display_name are the resolver output columns;
    # recommended_cluster_id / recommended_display_name are aliases kept for
    # compatibility if a future schema renames them back.
    select_cols = [
        "field_name", "normalized_label", "raw_label", "resolver_status",
        "occurrence_count", "distinct_call_count", "source_examples",
        "evidence_json",
        # real schema names
        "target_cluster_id", "target_display_name",
        # legacy / alternate names — included only if present
        "recommended_cluster_id", "recommended_display_name",
    ]
    available = [c for c in select_cols if c in cols]
    field_filter = "AND field_name = ANY(%s)" if fields else ""
    params: List[Any] = []
    if fields:
        params.append(fields)

    # Skip rows already materialized: evidence_json already contains "materialized": true.
    # This is idempotency without requiring a dedicated column.
    already_done_guard = (
        "AND (evidence_json IS NULL OR NOT (evidence_json ? 'materialized'))"
        if "evidence_json" in cols else ""
    )

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {', '.join(safe_id(c) for c in available)}
            FROM {safe_table(QUEUE_TABLE)}
            WHERE resolver_status IN ('MAP_TO_EXISTING', 'ANOMALY', 'PROMOTE')
              {field_filter}
              {already_done_guard}
            ORDER BY field_name, normalized_label
            """,
            params,
        )
        return [dict(r) for r in cur.fetchall()]


def phase3_materialize(args, phase_results: Dict[str, Any]) -> Dict[str, Any]:
    log.info("=== Phase 3: Materialize resolver decisions ===")
    t0 = time.monotonic()

    local_dsn = args.local_database_url
    if not local_dsn:
        return {"status": "skipped", "reason": "no local_database_url"}

    conn = psycopg2.connect(local_dsn)
    conn.autocommit = False  # explicit transaction for entire phase
    try:
        if args.apply:
            rows = _load_queue_rows(conn, list(args.fields))
        else:
            # Dry-run: Phase 2 didn't write to DB, so seed rows from its in-memory
            # proposals first, then append any DB rows from prior apply runs.
            p2_rows: List[Dict[str, Any]] = list(phase_results.get("phase2_queue_rows") or [])
            db_rows = _load_queue_rows(conn, list(args.fields))
            seen: Set[Tuple[str, str]] = {
                (r.get("field_name", ""), r.get("normalized_label", ""))
                for r in p2_rows
            }
            for r in db_rows:
                key = (r.get("field_name", ""), r.get("normalized_label", ""))
                if key not in seen:
                    p2_rows.append(r)
                    seen.add(key)
            rows = p2_rows
        log.info("Phase 3: %d queue rows to materialize", len(rows))

        if not rows:
            conn.rollback()
            phase_results["phase3_affected_call_ids"] = []
            return {"status": "ok", "rows_processed": 0, "counts": {}, "elapsed_sec": 0.0, "detail": []}

        # Resolve active run_id once per field — used consistently across all three tables
        unique_fields = list({r.get("field_name", "") for r in rows if r.get("field_name")})
        active_run_ids = resolve_active_run_ids(conn, unique_fields)
        log.info("Active run_ids for Phase 3: %s", active_run_ids)

        counts: Dict[str, int] = defaultdict(int)
        materialized_call_ids: Set[str] = set()
        results_detail: List[Dict[str, Any]] = []

        for row in rows:
            status   = str(row.get("resolver_status") or "")
            field    = row.get("field_name", "")
            norm     = row.get("normalized_label", "")
            run_id   = active_run_ids.get(field, AUTOPILOT_CLUSTER_VERSION)
            src_exs  = row.get("source_examples") or []
            if isinstance(src_exs, str):
                try:
                    src_exs = json.loads(src_exs)
                except Exception:
                    src_exs = []

            result: Dict[str, Any] = {}

            if status == "MAP_TO_EXISTING":
                result = materialize_map_to_existing(
                    conn, row,
                    run_id=run_id,
                    cluster_version=AUTOPILOT_CLUSTER_VERSION,
                    dry_run=not args.apply,
                )

            elif status == "ANOMALY":
                cluster_id   = make_anomaly_cluster_id(field, norm)
                display_name = display_name_from_label(row.get("raw_label") or norm)
                result = create_anomaly_cluster(
                    conn,
                    field_name=field,
                    raw_label=row.get("raw_label") or norm,
                    normalized_label=norm,
                    cluster_id=cluster_id,
                    run_id=run_id,
                    cluster_version=AUTOPILOT_CLUSTER_VERSION,
                    display_name=display_name,
                    occurrence_count=int(row.get("occurrence_count") or 1),
                    distinct_call_count=int(row.get("distinct_call_count") or 0),
                    source_examples=src_exs,
                    dry_run=not args.apply,
                )

            elif status == "PROMOTE":
                result = materialize_promote(
                    conn, row,
                    run_id=run_id,
                    cluster_version=AUTOPILOT_CLUSTER_VERSION,
                    dry_run=not args.apply,
                )

            result["resolver_status"] = status
            result["field_name"]      = field
            result["normalized_label"] = norm
            result["run_id"]          = run_id
            results_detail.append(result)

            outcome = result.get("status") or result.get("action", "unknown")
            counts[f"{status}_{outcome}"] += 1

            if outcome in ("applied", "dry_run") and src_exs:
                for ex in src_exs[:50]:
                    materialized_call_ids.add(str(ex))

            if args.apply:
                mark_queue_row_materialized(
                    conn, field, norm, status,
                    result.get("cluster_id"),
                    result.get("display_name"),
                    dry_run=False,
                )

        if args.apply:
            conn.commit()
            log.info("Phase 3 committed (%d rows)", len(rows))
        else:
            conn.rollback()

        phase_results["phase3_affected_call_ids"] = list(materialized_call_ids)
        elapsed = time.monotonic() - t0
        return {
            "status": "ok",
            "rows_processed": len(rows),
            "counts": dict(counts),
            "active_run_ids": active_run_ids,
            "elapsed_sec": round(elapsed, 1),
            "detail": results_detail,
        }

    except Exception as exc:
        conn.rollback()
        log.exception("Phase 3 error — rolled back: %s", exc)
        return {"status": "error", "reason": str(exc), "elapsed_sec": round(time.monotonic() - t0, 1)}
    finally:
        conn.close()


# ── Phase 4 — Targeted re-backfill ────────────────────────────────────────────

def _fields_materialized_in_phase3(phase_results: Dict[str, Any]) -> List[str]:
    """Return the distinct fields that had successful materializations in Phase 3."""
    p3 = phase_results.get("phase3", {})
    detail = p3.get("detail") or []
    fields = sorted({
        r.get("field_name", "")
        for r in detail
        if r.get("status") in ("applied", "dry_run") and r.get("field_name")
    })
    return fields or []


def phase4_rebackfill(args, phase_results: Dict[str, Any]) -> Dict[str, Any]:
    """
    Re-backfill STAGE rows for fields touched in Phase 3 with --include-already-updated
    and --include-anomaly-names so the newly materialized labels resolve correctly.

    This is intentionally a full-field sweep rather than per-call-ID, because the
    backfill script's --call-id flag only supports a single row. The field scope
    naturally limits the blast radius to only affected fields.
    """
    log.info("=== Phase 4: Targeted re-backfill ===")
    t0 = time.monotonic()

    # Only re-backfill fields that had successful materializations
    affected_fields = _fields_materialized_in_phase3(phase_results)
    if not affected_fields:
        return {"status": "skipped", "reason": "no fields had successful materializations in Phase 3"}

    try:
        import backfill_clean_taxonomy_to_stage_v2 as bv2
    except ImportError as exc:
        return {"status": "skipped", "reason": f"cannot import backfill_clean_taxonomy_to_stage_v2: {exc}"}

    log.info("Phase 4: re-backfilling fields: %s", affected_fields)

    argv = [
        "--dry-run", "false" if args.apply else "true",
        "--workers", str(args.workers),
        "--fields", ",".join(affected_fields),
        "--include-already-updated",   # pick up rows already touched in Phase 1
        "--include-anomaly-names",     # allow anomaly cluster display names
        "--skip-unchanged-audit",
        "--progress-interval", "15",
        "--audit-dir", str(args.audit_dir),
    ]
    if args.local_database_url:
        argv += ["--local-database-url", args.local_database_url]
    if args.stage_database_url:
        argv += ["--stage-database-url", args.stage_database_url]

    try:
        bargs = bv2.parse_args(argv)
        bv2.run_backfill(bargs)
        elapsed = time.monotonic() - t0
        return {
            "status": "ok",
            "fields_targeted": affected_fields,
            "elapsed_sec": round(elapsed, 1),
        }
    except SystemExit as exc:
        return {"status": "error", "reason": f"SystemExit({exc.code})", "elapsed_sec": round(time.monotonic() - t0, 1)}
    except Exception as exc:
        log.exception("Phase 4 error: %s", exc)
        return {"status": "error", "reason": str(exc), "elapsed_sec": round(time.monotonic() - t0, 1)}


# ── Phase 5 — Audit output ────────────────────────────────────────────────────

def phase5_audit(args, phase_results: Dict[str, Any], total_elapsed: float) -> Dict[str, Any]:
    log.info("=== Phase 5: Writing audit output ===")
    t0 = time.monotonic()

    audit_dir = Path(args.audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)

    run_ts = datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")
    summary = {
        "run_timestamp":    run_ts,
        "apply":            args.apply,
        "fields":           list(args.fields),
        "skipped_phases":   list(args.skip_phases),
        "total_elapsed_sec": round(total_elapsed, 1),
        "cluster_version":  AUTOPILOT_CLUSTER_VERSION,
        "phases":           {k: v for k, v in phase_results.items() if not k.startswith("phase3_affected")},
    }

    summary_path = audit_dir / f"autopilot_summary_{run_ts}.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    log.info("Audit written to: %s", summary_path)

    elapsed = time.monotonic() - t0
    return {"status": "ok", "path": str(summary_path), "elapsed_sec": round(elapsed, 1)}


# ── CLI & orchestrator ────────────────────────────────────────────────────────

def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="End-to-end autopilot: backfill -> resolve -> materialize -> re-backfill -> audit."
    )
    parser.add_argument("--apply",  action="store_true", default=False,
                        help="Execute writes. Default is dry-run.")
    parser.add_argument(
        "--fields",
        default=",".join(DEFAULT_FIELDS),
        help="Comma-separated taxonomy fields to process.",
    )
    parser.add_argument(
        "--skip-phases",
        default="",
        help="Comma-separated phases to skip (e.g. 2,4).",
    )
    parser.add_argument("--workers",       type=int, default=8)
    parser.add_argument("--audit-dir",     default="audit_autopilot")
    parser.add_argument(
        "--local-database-url",
        default=build_local_dsn(),
    )
    parser.add_argument(
        "--stage-database-url",
        default=build_stage_dsn(),
    )
    parser.add_argument(
        "--embedding-device",
        default=os.getenv("EMBEDDING_DEVICE", "cpu"),
        choices=["cpu", "cuda", "openvino"],
    )
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args(argv)
    args.fields = [f.strip() for f in args.fields.split(",") if f.strip()]
    args.skip_phases = {int(p.strip()) for p in args.skip_phases.split(",") if p.strip().isdigit()}

    if not args.fields:
        parser.error("--fields must contain at least one field name")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    run_start = time.monotonic()

    if not args.apply:
        log.info("DRY-RUN mode. Pass --apply to write to DB.")
    else:
        log.info("APPLY mode — writes are LIVE.")

    log.info("Fields: %s", ", ".join(args.fields))
    if args.skip_phases:
        log.info("Skipping phases: %s", sorted(args.skip_phases))

    phase_results: Dict[str, Any] = {}

    def run_phase(n: int, fn, label: str):
        if n in args.skip_phases:
            log.info("Skipping Phase %d (%s)", n, label)
            phase_results[f"phase{n}"] = {"status": "skipped", "reason": "skip_phases"}
            return
        result = fn(args, phase_results)
        phase_results[f"phase{n}"] = result
        status = result.get("status", "unknown")
        elapsed = result.get("elapsed_sec", "?")
        log.info("Phase %d done: status=%s elapsed=%ss", n, status, elapsed)

    try:
        run_phase(1, phase1_backfill,    "STAGE backfill")
        run_phase(2, phase2_resolve,     "resolver")
        run_phase(3, phase3_materialize, "materialize")
        run_phase(4, phase4_rebackfill,  "targeted re-backfill")
    finally:
        # Phase 5 always runs so audit is written even when an earlier phase fails.
        total = time.monotonic() - run_start
        if 5 not in args.skip_phases:
            p5 = phase5_audit(args, phase_results, total)
            phase_results["phase5"] = p5
            log.info("Phase 5 done: status=%s elapsed=%ss", p5.get("status"), p5.get("elapsed_sec"))
        else:
            log.info("Skipping Phase 5 (audit output)")
            phase_results["phase5"] = {"status": "skipped", "reason": "skip_phases"}

    log.info("Autopilot complete in %.1fs (apply=%s)", total, args.apply)

    # Print compact summary table
    for i in range(1, 6):
        r = phase_results.get(f"phase{i}", {})
        print(f"  Phase {i}: {r.get('status','?'):30s}  {r.get('elapsed_sec','')}")

    return 0


if __name__ == "__main__":
    sys.exit(main())
