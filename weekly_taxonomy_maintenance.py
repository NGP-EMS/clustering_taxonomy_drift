#!/usr/bin/env python3
"""
weekly_taxonomy_maintenance.py

Weekly taxonomy maintenance script for Sheezu AI taxonomy production flow.

Scope for weekly v1:
  1. Config issue solver for NO_CLUSTER_REFERENCE.
  2. Unresolved label resolver for TRUE_ANOMALY and NEW_CLUSTER_CANDIDATE.

This script is intentionally NOT a full cleanup script. It does not attempt
cluster stretching, weak medoid cleanup, broad duplicate cleanup, or full drift.
Those are deferred.

Default behavior is DRY RUN. Use --apply to write repairs/resolutions.

Expected local DB env values in .env:
  LOCAL_PG_HOST=127.0.0.1
  LOCAL_PG_PORT=5432
  LOCAL_PG_DB=taxonomy_drift_local
  LOCAL_PG_USER=postgres
  LOCAL_PG_PASSWORD=postgres

Example dry run:
  python weekly_taxonomy_maintenance.py --env-file .env

Example apply:
  python weekly_taxonomy_maintenance.py --env-file .env --apply

Useful options:
  --lookback-days 7
  --approved-fields call_type,call_type_sub,outcome,outcome_sub,main_reason,main_reason_sub,additional_tags,next_step,descriptive_keywords,coaching_tags
  --config-only
  --unresolved-only
"""

from __future__ import annotations

import argparse
import hashlib
import json
import math
import os
import re
import sys
from collections import Counter, defaultdict
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import psycopg2
import psycopg2.extras

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None


DEFAULT_APPROVED_FIELDS = [
    "call_type",
    "call_type_sub",
    "outcome",
    "outcome_sub",
    "main_reason",
    "main_reason_sub",
    "additional_tags",
    "next_step",
    "descriptive_keywords",
    "coaching_tags",
]

DEFAULT_OUTPUT_TABLE = "taxonomy_call_cluster_outputs"
DEFAULT_CLUSTER_TABLE = "taxonomy_clusters"
DEFAULT_CLUSTER_NAME_TABLE = "taxonomy_cluster_names"
DEFAULT_LABEL_MAP_TABLE = "taxonomy_label_cluster_map"
DEFAULT_EMBEDDINGS_TABLE = "taxonomy_label_embeddings"
DEFAULT_WEEKLY_RUN_TABLE = "taxonomy_weekly_runs"
DEFAULT_WEEKLY_REPAIR_LOG_TABLE = "taxonomy_weekly_repair_log"
DEFAULT_WEEKLY_ACTION_TABLE = "taxonomy_weekly_unresolved_actions"
DEFAULT_FIELD_CONFIG_TABLE = "taxonomy_production_mapper_field_config"

DEFAULT_EXISTING_THRESHOLD = 0.82
DEFAULT_NEW_CANDIDATE_THRESHOLD = 0.72

# For NEW_CLUSTER_CANDIDATE -> MAP_TO_EXISTING_CLUSTER deterministic safe-map.
DEFAULT_SAFE_MAP_CLOSE_MARGIN = 0.025
DEFAULT_SAFE_MAP_MIN_TOP_MARGIN = 0.015
DEFAULT_SAFE_MAP_STABILITY_RATIO = 0.80

# For NEW_CLUSTER_CANDIDATE -> attach to existing true-anomaly cluster.
DEFAULT_ANOMALY_ATTACH_THRESHOLD = 0.88

# Promotion threshold after anomaly cluster is created/updated.
DEFAULT_GENERAL_PROMOTION_CALLS = 10
DEFAULT_GENERAL_PROMOTION_OCCURRENCES = 25
DEFAULT_GENERAL_PROMOTION_WEEKS = 2

DEFAULT_STANDARD_NAMING_METHOD = "gpt"
DEFAULT_GPT_NAMING_MODEL = os.getenv("OPENAI_NAMING_MODEL", "gpt-4o-mini")
DEFAULT_GPT_NAME_MAX_WORDS = 6
DEFAULT_GPT_NAME_MAX_RETRIES = 5

FIELD_PROMOTION_THRESHOLDS = {
    "coaching_tags": {"distinct_call_count": 3, "total_occurrences": 5, "weeks_seen": 2},
    "additional_tags": {"distinct_call_count": 20, "total_occurrences": 50, "weeks_seen": 2},
}

STOPWORDS = {
    "a", "an", "and", "are", "as", "at", "be", "by", "for", "from", "has", "have",
    "in", "is", "it", "of", "on", "or", "the", "to", "with", "without", "unknown",
    "general", "misc", "miscellaneous", "other", "unspecified", "none", "na", "n/a",
    "good", "bad", "poor", "training", "coaching", "clear", "unclear", "tag", "label",
}

ACRONYMS = {
    "dm": "DM",
    "loa": "LOA",
    "ivr": "IVR",
    "mpan": "MPAN",
    "mprn": "MPRN",
    "tps": "TPS",
    "3cx": "3CX",
}

CONTRADICTION_PAIRS = [
    ("good", "poor"),
    ("clear", "unclear"),
    ("sent", "not sent"),
    ("received", "not received"),
    ("interested", "not interested"),
    ("available", "unavailable"),
    ("answer", "no answer"),
    ("callback", "do not call"),
    ("approved", "rejected"),
    ("agreed", "declined"),
]


# -----------------------------------------------------------------------------
# Utilities
# -----------------------------------------------------------------------------


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def normalize_label(value: Any) -> str:
    if value is None:
        return ""
    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "n/a", "na", "unknown"}:
        return ""
    text = text.replace("_", " ").replace("-", " ").replace("/", " ")
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
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
        low = word.lower()
        if low in ACRONYMS:
            out.append(ACRONYMS[low])
        elif re.fullmatch(r"\d+[a-z]*", low):
            out.append(low.upper())
        else:
            out.append(low.capitalize())
    return " ".join(out)


def stable_hash(*parts: Any, length: int = 16) -> str:
    payload = "||".join(str(p or "") for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def make_weekly_run_id() -> str:
    return f"weekly_{datetime.utcnow().strftime('%Y%m%d_%H%M%S')}_{stable_hash(datetime.utcnow().isoformat(), length=8)}"


def make_anomaly_cluster_id(field_name: str, normalized_label: str) -> str:
    digest = stable_hash(field_name, normalized_label, length=18)
    return f"weekly_anom_{digest}"


def safe_pg_identifier(name: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name}")
    return f'"{name}"'


def safe_pg_qualified_name(name: str) -> str:
    parts = str(name).split(".")
    if not parts or any(not p for p in parts):
        raise ValueError(f"Unsafe SQL table name: {name}")
    return ".".join(safe_pg_identifier(p) for p in parts)


def parse_jsonish(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, (dict, list)):
        return value
    if isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            return json.loads(text)
        except Exception:
            return None
    return value


def parse_vector(value: Any) -> Optional[np.ndarray]:
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        arr = value.astype(np.float32)
    elif isinstance(value, list):
        arr = np.array(value, dtype=np.float32)
    elif isinstance(value, str):
        if not value.strip():
            return None
        try:
            arr = np.array(json.loads(value), dtype=np.float32)
        except Exception:
            return None
    else:
        try:
            arr = np.array(value, dtype=np.float32)
        except Exception:
            return None
    if arr.ndim != 1 or arr.size == 0:
        return None
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    return arr.astype(np.float32)


def vector_to_jsonb(value: Optional[np.ndarray]) -> psycopg2.extras.Json:
    if value is None:
        return psycopg2.extras.Json(None)
    return psycopg2.extras.Json([float(x) for x in value.tolist()])


def cosine_similarity(a: Optional[np.ndarray], b: Optional[np.ndarray]) -> Optional[float]:
    if a is None or b is None:
        return None
    if len(a) != len(b):
        return None
    an = np.linalg.norm(a)
    bn = np.linalg.norm(b)
    if an == 0 or bn == 0:
        return None
    return float(np.dot(a / an, b / bn))


def centroid(vectors: Sequence[np.ndarray]) -> Optional[np.ndarray]:
    valid = [v for v in vectors if v is not None and v.ndim == 1 and v.size > 0]
    if not valid:
        return None
    dim = valid[0].shape[0]
    valid = [v for v in valid if v.shape[0] == dim]
    if not valid:
        return None
    c = np.mean(np.vstack(valid), axis=0).astype(np.float32)
    norm = np.linalg.norm(c)
    if norm > 0:
        c = c / norm
    return c


def medoid_label(labels_and_vectors: Sequence[Tuple[str, np.ndarray]], center: Optional[np.ndarray]) -> Tuple[Optional[str], Optional[float]]:
    if center is None:
        return None, None
    best_label = None
    best_score = -1.0
    for label, vec in labels_and_vectors:
        sim = cosine_similarity(center, vec)
        if sim is not None and sim > best_score:
            best_score = sim
            best_label = label
    if best_label is None:
        return None, None
    return best_label, float(best_score)


def token_set(text: Any) -> set[str]:
    normalized = normalize_label(text)
    return {t for t in normalized.split() if t and t not in STOPWORDS and len(t) > 1}


def has_contradiction(candidate: str, target: str) -> bool:
    cand = normalize_label(candidate)
    tgt = normalize_label(target)
    for left, right in CONTRADICTION_PAIRS:
        if left in cand and right in tgt:
            return True
        if right in cand and left in tgt:
            return True
    return False



# -----------------------------------------------------------------------------
# Actor-aware directionality guard
# -----------------------------------------------------------------------------

ACTOR_ALIAS_TO_CANONICAL = {
    "agent": "agent",
    "advisor": "agent",
    "adviser": "agent",
    "rep": "agent",
    "representative": "agent",
    "consultant": "agent",
    "salesperson": "agent",
    "customer": "customer",
    "client": "customer",
    "prospect": "customer",
    "lead": "customer",
    "contact": "customer",
    "caller": "customer",
    "gatekeeper": "customer",
    "dm": "customer",
    "decision maker": "customer",
    "decision-maker": "customer",
    "business owner": "customer",
}

ACTOR_ACTION_KEYWORDS = {
    "rudeness_or_abuse": {
        "abuse", "abused", "abusive", "accuse", "accused", "argue", "argued",
        "aggressive", "aggression", "blame", "blamed", "curse", "cursed", "cussed",
        "hostile", "insult", "insulted", "insulting", "rude", "rudeness",
        "shout", "shouted", "shouting", "swear", "swore", "swearing",
        "threat", "threaten", "threatened", "yell", "yelled", "yelling",
    },
    "disconnect_or_hangup": {
        "abandon", "abandoned", "cut", "disconnected", "disconnect", "dropped",
        "ended", "hang", "hanged", "hung", "terminate", "terminated",
    },
    "refusal_or_rejection": {
        "decline", "declined", "reject", "rejected", "refusal", "refuse", "refused",
    },
}


def _actor_token_positions(normalized_text: str) -> list[tuple[int, str, str]]:
    tokens = normalized_text.split()
    positions: list[tuple[int, str, str]] = []
    used_positions: set[int] = set()

    phrase_aliases = sorted(
        [alias for alias in ACTOR_ALIAS_TO_CANONICAL if " " in alias or "-" in alias],
        key=lambda value: len(value.split()),
        reverse=True,
    )
    for alias in phrase_aliases:
        alias_tokens = normalize_label(alias).split()
        size = len(alias_tokens)
        if not alias_tokens:
            continue
        for idx in range(0, max(0, len(tokens) - size + 1)):
            if any((idx + offset) in used_positions for offset in range(size)):
                continue
            if tokens[idx : idx + size] == alias_tokens:
                positions.append((idx, ACTOR_ALIAS_TO_CANONICAL[alias], alias))
                used_positions.update(idx + offset for offset in range(size))

    for idx, token in enumerate(tokens):
        if idx in used_positions:
            continue
        if token in ACTOR_ALIAS_TO_CANONICAL:
            positions.append((idx, ACTOR_ALIAS_TO_CANONICAL[token], token))

    positions.sort(key=lambda item: item[0])
    return positions


def _action_categories_between(tokens: list[str], start: int, end: int) -> set[str]:
    if start > end:
        start, end = end, start
    window = tokens[max(0, start): min(len(tokens), end + 1)]
    found: set[str] = set()
    for category, words in ACTOR_ACTION_KEYWORDS.items():
        if any(token in words for token in window):
            found.add(category)
    return found


def extract_actor_relations(text: Any) -> list[dict[str, str]]:
    normalized = normalize_label(text)
    if not normalized:
        return []

    tokens = normalized.split()
    actors = _actor_token_positions(normalized)
    relations: list[dict[str, str]] = []
    seen: set[tuple[str, str, str]] = set()

    for left_idx, left_actor, _left_alias in actors:
        for right_idx, right_actor, _right_alias in actors:
            if left_idx == right_idx or left_actor == right_actor:
                continue
            distance = abs(right_idx - left_idx)
            if distance > 10:
                continue

            low = min(left_idx, right_idx)
            high = max(left_idx, right_idx)
            action_categories = _action_categories_between(tokens, low, high)
            if not action_categories:
                action_categories = _action_categories_between(tokens, low, min(len(tokens) - 1, high + 3))
            if not action_categories:
                continue

            actor = left_actor if left_idx < right_idx else right_actor
            target = right_actor if left_idx < right_idx else left_actor
            if "by" in tokens[low: high + 1]:
                actor = right_actor if right_idx > left_idx else left_actor
                target = left_actor if right_idx > left_idx else right_actor

            for action in action_categories:
                key = (actor, target, action)
                if key not in seen:
                    seen.add(key)
                    relations.append({"actor": actor, "target": target, "action": action})

    return relations


def actor_directionality_check(candidate_text: Any, target_text: Any) -> dict[str, Any]:
    candidate_relations = extract_actor_relations(candidate_text)
    target_relations = extract_actor_relations(target_text)
    conflict_pairs = []

    for cand in candidate_relations:
        for target in target_relations:
            if cand["action"] != target["action"]:
                continue
            if cand["actor"] == target["target"] and cand["target"] == target["actor"]:
                conflict_pairs.append({"candidate": cand, "target": target})

    return {
        "candidate_relations": candidate_relations,
        "target_relations": target_relations,
        "actor_direction_conflict": bool(conflict_pairs),
        "conflict_pairs": conflict_pairs,
    }


def has_actor_direction_conflict(candidate_text: Any, target_text: Any) -> bool:
    return bool(actor_directionality_check(candidate_text, target_text)["actor_direction_conflict"])

def to_iso(dt: datetime) -> str:
    return dt.astimezone(timezone.utc).isoformat()


# -----------------------------------------------------------------------------
# DB helpers
# -----------------------------------------------------------------------------


def load_environment(env_file: Optional[str]) -> None:
    if load_dotenv is None:
        return
    if env_file:
        load_dotenv(env_file)
    else:
        load_dotenv()


def get_local_pg_connection():
    conn_str = os.getenv("LOCAL_PG_CONN_STR") or os.getenv("PG_CONN_STR")
    if conn_str:
        return psycopg2.connect(conn_str)
    return psycopg2.connect(
        host=os.getenv("LOCAL_PG_HOST", "127.0.0.1"),
        port=int(os.getenv("LOCAL_PG_PORT", "5432")),
        dbname=os.getenv("LOCAL_PG_DB", "taxonomy_drift_local"),
        user=os.getenv("LOCAL_PG_USER", "postgres"),
        password=os.getenv("LOCAL_PG_PASSWORD", "postgres"),
    )


def table_exists(conn, table_name: str) -> bool:
    if "." in table_name:
        schema, table = table_name.split(".", 1)
    else:
        schema, table = "public", table_name
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
                SELECT 1
                FROM information_schema.tables
                WHERE table_schema = %s AND table_name = %s
            )
            """,
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def get_columns(conn, table_name: str) -> set[str]:
    if "." in table_name:
        schema, table = table_name.split(".", 1)
    else:
        schema, table = "public", table_name
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT column_name
            FROM information_schema.columns
            WHERE table_schema = %s AND table_name = %s
            """,
            (schema, table),
        )
        return {r[0] for r in cur.fetchall()}


def ensure_weekly_schema(conn, args: argparse.Namespace) -> None:
    run_t = safe_pg_qualified_name(args.weekly_run_table)
    repair_t = safe_pg_qualified_name(args.weekly_repair_log_table)
    action_t = safe_pg_qualified_name(args.weekly_action_table)
    cfg_t = safe_pg_qualified_name(args.field_config_table)
    cluster_t = safe_pg_qualified_name(args.cluster_table)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {run_t} (
                id BIGSERIAL PRIMARY KEY,
                weekly_run_id TEXT NOT NULL UNIQUE,
                window_start TIMESTAMPTZ NOT NULL,
                window_end TIMESTAMPTZ NOT NULL,
                mode TEXT NOT NULL,
                apply_changes BOOLEAN NOT NULL DEFAULT FALSE,
                status TEXT NOT NULL DEFAULT 'RUNNING',
                summary_json JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW(),
                completed_at TIMESTAMPTZ
            );
            """
        )
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {repair_t} (
                id BIGSERIAL PRIMARY KEY,
                weekly_run_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                issue_type TEXT NOT NULL,
                repair_action TEXT NOT NULL,
                repair_status TEXT NOT NULL,
                rows_affected INTEGER NOT NULL DEFAULT 0,
                notes TEXT,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{args.weekly_repair_log_table}_run ON {repair_t}(weekly_run_id);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{args.weekly_repair_log_table}_field ON {repair_t}(field_name);")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {action_t} (
                id BIGSERIAL PRIMARY KEY,
                weekly_run_id TEXT NOT NULL,
                field_name TEXT NOT NULL,
                normalized_label TEXT,
                raw_label_examples JSONB,
                source_mapper_run_ids JSONB,
                source_record_ids JSONB,
                original_mapping_status TEXT,
                recommended_action TEXT NOT NULL,
                resolution_status TEXT NOT NULL,
                target_cluster_id TEXT,
                target_display_name TEXT,
                similarity_score NUMERIC,
                evidence_json JSONB,
                created_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{args.weekly_action_table}_run ON {action_t}(weekly_run_id);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{args.weekly_action_table}_field ON {action_t}(field_name);")
        cur.execute(f"CREATE INDEX IF NOT EXISTS idx_{args.weekly_action_table}_action ON {action_t}(recommended_action);")

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {cfg_t} (
                field_name TEXT PRIMARY KEY,
                approved_for_production BOOLEAN NOT NULL DEFAULT FALSE,
                enabled_for_mapper BOOLEAN NOT NULL DEFAULT FALSE,
                reason TEXT,
                updated_at TIMESTAMPTZ NOT NULL DEFAULT NOW()
            );
            """
        )

        # Promotion tracking columns on taxonomy_clusters. Safe additive schema.
        cur.execute(f"ALTER TABLE {cluster_t} ADD COLUMN IF NOT EXISTS promotion_status TEXT;")
        cur.execute(f"ALTER TABLE {cluster_t} ADD COLUMN IF NOT EXISTS weekly_first_seen TIMESTAMPTZ;")
        cur.execute(f"ALTER TABLE {cluster_t} ADD COLUMN IF NOT EXISTS weekly_last_seen TIMESTAMPTZ;")
        cur.execute(f"ALTER TABLE {cluster_t} ADD COLUMN IF NOT EXISTS weekly_occurrence_count INTEGER DEFAULT 0;")
        cur.execute(f"ALTER TABLE {cluster_t} ADD COLUMN IF NOT EXISTS weekly_distinct_call_count INTEGER DEFAULT 0;")
        cur.execute(f"ALTER TABLE {cluster_t} ADD COLUMN IF NOT EXISTS weekly_weeks_seen INTEGER DEFAULT 0;")
        cur.execute(f"ALTER TABLE {cluster_t} ADD COLUMN IF NOT EXISTS promotion_candidate_reason TEXT;")

    conn.commit()


def insert_weekly_run(conn, args: argparse.Namespace, weekly_run_id: str, window_start: datetime, window_end: datetime) -> None:
    run_t = safe_pg_qualified_name(args.weekly_run_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {run_t} (weekly_run_id, window_start, window_end, mode, apply_changes, status, summary_json)
            VALUES (%s, %s, %s, %s, %s, 'RUNNING', %s)
            ON CONFLICT (weekly_run_id) DO UPDATE SET
                window_start = EXCLUDED.window_start,
                window_end = EXCLUDED.window_end,
                mode = EXCLUDED.mode,
                apply_changes = EXCLUDED.apply_changes,
                status = 'RUNNING',
                summary_json = EXCLUDED.summary_json,
                completed_at = NULL
            """,
            (
                weekly_run_id,
                window_start,
                window_end,
                args.mode,
                bool(args.apply),
                psycopg2.extras.Json({"started_at": to_iso(utcnow())}),
            ),
        )
    conn.commit()


def complete_weekly_run(conn, args: argparse.Namespace, weekly_run_id: str, status: str, summary: dict[str, Any]) -> None:
    run_t = safe_pg_qualified_name(args.weekly_run_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {run_t}
            SET status = %s,
                summary_json = %s,
                completed_at = NOW()
            WHERE weekly_run_id = %s
            """,
            (status, psycopg2.extras.Json(summary), weekly_run_id),
        )
    conn.commit()


def log_repair(
    conn,
    args: argparse.Namespace,
    *,
    weekly_run_id: str,
    field_name: str,
    issue_type: str,
    repair_action: str,
    repair_status: str,
    rows_affected: int = 0,
    notes: Optional[str] = None,
) -> None:
    repair_t = safe_pg_qualified_name(args.weekly_repair_log_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {repair_t} (
                weekly_run_id, field_name, issue_type, repair_action,
                repair_status, rows_affected, notes
            ) VALUES (%s, %s, %s, %s, %s, %s, %s)
            """,
            (weekly_run_id, field_name, issue_type, repair_action, repair_status, int(rows_affected or 0), notes),
        )


def log_action(
    conn,
    args: argparse.Namespace,
    *,
    weekly_run_id: str,
    field_name: str,
    normalized_label: str,
    raw_label_examples: list[str],
    source_mapper_run_ids: list[str],
    source_record_ids: list[str],
    original_mapping_status: str,
    recommended_action: str,
    resolution_status: str,
    target_cluster_id: Optional[str],
    target_display_name: Optional[str],
    similarity_score: Optional[float],
    evidence: dict[str, Any],
) -> None:
    action_t = safe_pg_qualified_name(args.weekly_action_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {action_t} (
                weekly_run_id, field_name, normalized_label, raw_label_examples,
                source_mapper_run_ids, source_record_ids, original_mapping_status,
                recommended_action, resolution_status, target_cluster_id,
                target_display_name, similarity_score, evidence_json
            ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
            """,
            (
                weekly_run_id,
                field_name,
                normalized_label,
                psycopg2.extras.Json(raw_label_examples[:25]),
                psycopg2.extras.Json(source_mapper_run_ids[:100]),
                psycopg2.extras.Json(source_record_ids[:100]),
                original_mapping_status,
                recommended_action,
                resolution_status,
                target_cluster_id,
                target_display_name,
                similarity_score,
                psycopg2.extras.Json(evidence),
            ),
        )
    print(f"Logged weekly unresolved action: {field_name} / {normalized_label} -> {recommended_action} ({resolution_status})")


def count_weekly_actions(conn, args: argparse.Namespace, weekly_run_id: str) -> int:
    action_t = safe_pg_qualified_name(args.weekly_action_table)
    with conn.cursor() as cur:
        cur.execute(f"SELECT COUNT(*) FROM {action_t} WHERE weekly_run_id = %s", (weekly_run_id,))
        return int(cur.fetchone()[0] or 0)


# -----------------------------------------------------------------------------
# Config issue solver
# -----------------------------------------------------------------------------


def seed_approved_field_config(conn, args: argparse.Namespace, approved_fields: Sequence[str]) -> None:
    cfg_t = safe_pg_qualified_name(args.field_config_table)
    with conn.cursor() as cur:
        for field in approved_fields:
            cur.execute(
                f"""
                INSERT INTO {cfg_t} (field_name, approved_for_production, enabled_for_mapper, reason, updated_at)
                VALUES (%s, TRUE, TRUE, 'Approved default taxonomy production field', NOW())
                ON CONFLICT (field_name) DO UPDATE SET
                    approved_for_production = TRUE,
                    enabled_for_mapper = TRUE,
                    updated_at = NOW()
                """,
                (field,),
            )
    conn.commit()


def get_no_cluster_reference_fields(conn, args: argparse.Namespace, window_start: datetime, window_end: datetime) -> list[dict[str, Any]]:
    output_t = safe_pg_qualified_name(args.output_table)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                field_name,
                COUNT(*)::int AS row_count,
                COUNT(DISTINCT source_record_id)::int AS distinct_call_count,
                MIN(classified_at) AS first_seen,
                MAX(classified_at) AS last_seen
            FROM {output_t}
            WHERE mapping_status = 'NO_CLUSTER_REFERENCE'
              AND COALESCE(classified_at, created_at) >= %s
              AND COALESCE(classified_at, created_at) < %s
            GROUP BY field_name
            ORDER BY row_count DESC, field_name
            """,
            (window_start, window_end),
        )
        return list(cur.fetchall())


def cluster_counts_for_field(conn, args: argparse.Namespace, field_name: str) -> dict[str, int]:
    cluster_t = safe_pg_qualified_name(args.cluster_table)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                COUNT(*)::int AS total_clusters,
                COUNT(*) FILTER (WHERE COALESCE(active, TRUE) = TRUE)::int AS active_clusters,
                COUNT(*) FILTER (
                    WHERE COALESCE(active, TRUE) = TRUE
                      AND COALESCE(is_true_anomaly_cluster, FALSE) = FALSE
                )::int AS active_standard_clusters,
                COUNT(*) FILTER (
                    WHERE COALESCE(is_true_anomaly_cluster, FALSE) = FALSE
                )::int AS standard_clusters,
                COUNT(*) FILTER (
                    WHERE COALESCE(is_true_anomaly_cluster, FALSE) = FALSE
                      AND centroid_embedding IS NULL
                )::int AS standard_missing_centroids
            FROM {cluster_t}
            WHERE field_name = %s
            """,
            (field_name,),
        )
        row = cur.fetchone() or {}
        return {k: int(row.get(k) or 0) for k in row.keys()}


def reactivate_latest_valid_standard_clusters(conn, args: argparse.Namespace, field_name: str) -> int:
    cluster_t = safe_pg_qualified_name(args.cluster_table)
    cols = get_columns(conn, args.cluster_table)
    version_expr = "COALESCE(cluster_version, run_id, 'v1')" if "run_id" in cols else "COALESCE(cluster_version, 'v1')"
    run_filter_cols = []
    if "cluster_version" in cols:
        run_filter_cols.append("cluster_version")
    if "run_id" in cols:
        run_filter_cols.append("run_id")

    with conn.cursor() as cur:
        cur.execute(
            f"""
            WITH latest AS (
                SELECT {version_expr} AS latest_version
                FROM {cluster_t}
                WHERE field_name = %s
                  AND COALESCE(is_true_anomaly_cluster, FALSE) = FALSE
                  AND centroid_embedding IS NOT NULL
                GROUP BY {version_expr}
                ORDER BY MAX(COALESCE(updated_at, created_at, NOW())) DESC NULLS LAST
                LIMIT 1
            )
            UPDATE {cluster_t} c
            SET active = TRUE,
                updated_at = NOW()
            FROM latest
            WHERE c.field_name = %s
              AND COALESCE(c.is_true_anomaly_cluster, FALSE) = FALSE
              AND c.centroid_embedding IS NOT NULL
              AND {version_expr.replace('cluster_version', 'c.cluster_version').replace('run_id', 'c.run_id')} = latest.latest_version
            """,
            (field_name, field_name),
        )
        return int(cur.rowcount or 0)


def restore_missing_names(conn, args: argparse.Namespace, field_name: str) -> int:
    """Restore missing taxonomy_cluster_names rows from cluster display_name/cluster_name if available."""
    cluster_t = safe_pg_qualified_name(args.cluster_table)
    names_t = safe_pg_qualified_name(args.cluster_name_table)
    cluster_cols = get_columns(conn, args.cluster_table)
    name_cols = get_columns(conn, args.cluster_name_table)

    if not table_exists(conn, args.cluster_name_table):
        return 0
    required_name_cols = {"field_name", "cluster_id", "display_name"}
    if not required_name_cols.issubset(name_cols):
        return 0

    display_expr = None
    if "display_name" in cluster_cols:
        display_expr = "c.display_name"
    elif "cluster_name" in cluster_cols:
        display_expr = "c.cluster_name"
    else:
        return 0

    insert_cols = []
    select_exprs = []
    for col in ["field_name", "run_id", "cluster_version", "cluster_id", "is_anomaly", "display_name", "naming_method", "naming_reason", "active", "created_at", "updated_at"]:
        if col not in name_cols:
            continue
        insert_cols.append(col)
        if col == "field_name":
            select_exprs.append("c.field_name")
        elif col == "run_id":
            select_exprs.append("c.run_id" if "run_id" in cluster_cols else "COALESCE(c.cluster_version, 'weekly')")
        elif col == "cluster_version":
            select_exprs.append("COALESCE(c.cluster_version, c.run_id, 'weekly')" if "run_id" in cluster_cols else "COALESCE(c.cluster_version, 'weekly')")
        elif col == "cluster_id":
            select_exprs.append("c.cluster_id")
        elif col == "is_anomaly":
            select_exprs.append("COALESCE(c.is_true_anomaly_cluster, FALSE)")
        elif col == "display_name":
            select_exprs.append(display_expr)
        elif col == "naming_method":
            select_exprs.append("'weekly_restored_from_cluster'")
        elif col == "naming_reason":
            select_exprs.append("'Restored missing name from cluster metadata'")
        elif col == "active":
            select_exprs.append("TRUE")
        elif col == "created_at":
            select_exprs.append("NOW()")
        elif col == "updated_at":
            select_exprs.append("NOW()")

    if not insert_cols:
        return 0

    join_run = ""
    if "run_id" in name_cols and "run_id" in cluster_cols:
        join_run += " AND n.run_id = c.run_id"
    if "cluster_version" in name_cols and "cluster_version" in cluster_cols:
        join_run += " AND n.cluster_version = c.cluster_version"

    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {names_t} ({', '.join(safe_pg_identifier(c) for c in insert_cols)})
            SELECT {', '.join(select_exprs)}
            FROM {cluster_t} c
            LEFT JOIN {names_t} n
              ON n.field_name = c.field_name
             AND n.cluster_id = c.cluster_id
             {join_run}
            WHERE c.field_name = %s
              AND COALESCE(c.active, TRUE) = TRUE
              AND COALESCE(c.is_true_anomaly_cluster, FALSE) = FALSE
              AND n.cluster_id IS NULL
              AND {display_expr} IS NOT NULL
              AND {display_expr} <> ''
            """,
            (field_name,),
        )
        return int(cur.rowcount or 0)


def rebuild_missing_centroids(conn, args: argparse.Namespace, field_name: str) -> int:
    """
    Rebuild missing centroids using taxonomy_label_cluster_map joined to taxonomy_label_embeddings.

    This is intentionally conservative and only updates active standard clusters with missing centroids.
    It tries common embedding column names dynamically.
    """
    if not table_exists(conn, args.embeddings_table):
        return 0

    cluster_t = safe_pg_qualified_name(args.cluster_table)
    label_map_t = safe_pg_qualified_name(args.label_map_table)
    emb_t = safe_pg_qualified_name(args.embeddings_table)

    cluster_cols = get_columns(conn, args.cluster_table)
    map_cols = get_columns(conn, args.label_map_table)
    emb_cols = get_columns(conn, args.embeddings_table)

    if "centroid_embedding" not in cluster_cols:
        return 0
    if not {"field_name", "final_cluster_id", "normalized_label"}.issubset(map_cols):
        return 0
    if "normalized_label" not in emb_cols:
        return 0

    emb_col = None
    for candidate in ["embedding", "label_embedding", "embedding_vector", "vector"]:
        if candidate in emb_cols:
            emb_col = candidate
            break
    if not emb_col:
        return 0

    raw_col = "raw_label" if "raw_label" in map_cols else "normalized_label"
    value_col = "value_count" if "value_count" in map_cols else None

    join_field = "AND e.field_name = m.field_name" if "field_name" in emb_cols else ""
    version_join = ""
    if "cluster_version" in map_cols and "cluster_version" in cluster_cols:
        version_join = "AND COALESCE(m.cluster_version, 'v1') = COALESCE(c.cluster_version, 'v1')"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                c.cluster_id,
                COALESCE(c.cluster_version, 'weekly') AS cluster_version,
                m.{raw_col} AS raw_label,
                m.normalized_label,
                {('COALESCE(m.' + value_col + ', 1)') if value_col else '1'} AS value_count,
                e.{emb_col} AS embedding
            FROM {cluster_t} c
            JOIN {label_map_t} m
              ON m.field_name = c.field_name
             AND m.final_cluster_id = c.cluster_id
             {version_join}
            JOIN {emb_t} e
              ON e.normalized_label = m.normalized_label
             {join_field}
            WHERE c.field_name = %s
              AND COALESCE(c.active, TRUE) = TRUE
              AND COALESCE(c.is_true_anomaly_cluster, FALSE) = FALSE
              AND c.centroid_embedding IS NULL
            """,
            (field_name,),
        )
        rows = cur.fetchall()

    grouped: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        grouped[str(row["cluster_id"])].append(row)

    updated = 0
    with conn.cursor() as cur:
        for cluster_id, members in grouped.items():
            vectors = []
            labels_and_vectors = []
            label_counts = Counter()
            for m in members:
                vec = parse_vector(m.get("embedding"))
                if vec is None:
                    continue
                vectors.append(vec)
                label = str(m.get("raw_label") or m.get("normalized_label") or "")
                labels_and_vectors.append((label, vec))
                label_counts[label] += int(m.get("value_count") or 1)
            if not vectors:
                continue
            cvec = centroid(vectors)
            medoid, medoid_sim = medoid_label(labels_and_vectors, cvec)
            reps = [label for label, _cnt in label_counts.most_common(10)]
            assignments = ["centroid_embedding = %s", "updated_at = NOW()"]
            params: list[Any] = [vector_to_jsonb(cvec)]
            if "medoid_label" in cluster_cols:
                assignments.append("medoid_label = %s")
                params.append(medoid)
            if "medoid_similarity_to_centroid" in cluster_cols:
                assignments.append("medoid_similarity_to_centroid = %s")
                params.append(medoid_sim)
            if "representative_labels" in cluster_cols:
                assignments.append("representative_labels = %s")
                params.append(psycopg2.extras.Json(reps))
            if "cluster_size" in cluster_cols:
                assignments.append("cluster_size = %s")
                params.append(len(label_counts))
            if "total_occurrences" in cluster_cols:
                assignments.append("total_occurrences = %s")
                params.append(sum(label_counts.values()))
            params.extend([field_name, cluster_id])
            cur.execute(
                f"""
                UPDATE {cluster_t}
                SET {', '.join(assignments)}
                WHERE field_name = %s AND cluster_id = %s
                """,
                params,
            )
            updated += int(cur.rowcount or 0)
    return updated


def weekly_config_repair(conn, args: argparse.Namespace, weekly_run_id: str, window_start: datetime, window_end: datetime) -> dict[str, Any]:
    approved_fields = parse_approved_fields(args.approved_fields)
    seed_approved_field_config(conn, args, approved_fields)

    fields = get_no_cluster_reference_fields(conn, args, window_start, window_end)
    summary = {"no_cluster_reference_fields": len(fields), "repairs_logged": 0, "repairs_applied": 0}

    if not fields:
        print("Config repair: no NO_CLUSTER_REFERENCE fields found in window.")
        return summary

    print(f"Config repair: found {len(fields)} fields with NO_CLUSTER_REFERENCE.")

    for item in fields:
        field = str(item["field_name"])
        if field not in approved_fields:
            status = "DONE" if args.apply else "PLANNED"
            if args.apply:
                cfg_t = safe_pg_qualified_name(args.field_config_table)
                with conn.cursor() as cur:
                    cur.execute(
                        f"""
                        INSERT INTO {cfg_t} (field_name, approved_for_production, enabled_for_mapper, reason, updated_at)
                        VALUES (%s, FALSE, FALSE, 'Field produced NO_CLUSTER_REFERENCE but is not approved for production taxonomy mapping', NOW())
                        ON CONFLICT (field_name) DO UPDATE SET
                            approved_for_production = FALSE,
                            enabled_for_mapper = FALSE,
                            reason = EXCLUDED.reason,
                            updated_at = NOW()
                        """,
                        (field,),
                    )
            log_repair(
                conn,
                args,
                weekly_run_id=weekly_run_id,
                field_name=field,
                issue_type="FIELD_NOT_APPROVED_FOR_PRODUCTION",
                repair_action="DISABLE_FROM_PRODUCTION_MAPPER_FIELD_CONFIG",
                repair_status=status,
                rows_affected=1 if args.apply else 0,
                notes="Not treated as taxonomy drift. Production mapper field list/config should exclude this field.",
            )
            summary["repairs_logged"] += 1
            if args.apply:
                summary["repairs_applied"] += 1
            continue

        counts = cluster_counts_for_field(conn, args, field)
        if counts["total_clusters"] == 0:
            log_repair(
                conn,
                args,
                weekly_run_id=weekly_run_id,
                field_name=field,
                issue_type="NO_TAXONOMY_HISTORY",
                repair_action="INITIAL_TAXONOMY_BUILD_REQUIRED",
                repair_status="BLOCKED",
                rows_affected=0,
                notes="Field is approved but has no taxonomy_clusters rows. Weekly script will not invent a taxonomy from scratch.",
            )
            summary["repairs_logged"] += 1
            continue

        if counts["active_standard_clusters"] == 0 and counts["standard_clusters"] > 0:
            rows = reactivate_latest_valid_standard_clusters(conn, args, field) if args.apply else 0
            log_repair(
                conn,
                args,
                weekly_run_id=weekly_run_id,
                field_name=field,
                issue_type="NO_ACTIVE_STANDARD_CLUSTERS",
                repair_action="REACTIVATE_LATEST_VALID_STANDARD_CLUSTERS",
                repair_status="DONE" if args.apply and rows else ("PLANNED" if not args.apply else "NOOP"),
                rows_affected=rows,
                notes=f"Counts before repair: {counts}",
            )
            summary["repairs_logged"] += 1
            if rows:
                summary["repairs_applied"] += 1

        if counts["standard_missing_centroids"] > 0:
            rows = rebuild_missing_centroids(conn, args, field) if args.apply else 0
            log_repair(
                conn,
                args,
                weekly_run_id=weekly_run_id,
                field_name=field,
                issue_type="MISSING_CENTROIDS",
                repair_action="REBUILD_MISSING_CENTROIDS_AND_MEDOIDS",
                repair_status="DONE" if args.apply and rows else ("PLANNED" if not args.apply else "NOOP"),
                rows_affected=rows,
                notes=f"Missing standard centroids before repair: {counts['standard_missing_centroids']}",
            )
            summary["repairs_logged"] += 1
            if rows:
                summary["repairs_applied"] += 1

        rows = restore_missing_names(conn, args, field) if args.apply else 0
        log_repair(
            conn,
            args,
            weekly_run_id=weekly_run_id,
            field_name=field,
            issue_type="NAME_COVERAGE_CHECK",
            repair_action="RESTORE_MISSING_NAMES_FROM_APPROVED_CLUSTER_METADATA",
            repair_status="DONE" if args.apply and rows else ("PLANNED" if not args.apply else "NOOP"),
            rows_affected=rows,
            notes="Safe name restore only. Does not invent new display names.",
        )
        summary["repairs_logged"] += 1
        if rows:
            summary["repairs_applied"] += 1

    if args.apply:
        conn.commit()
    else:
        conn.rollback()
        # Recreate run row/logs after rollback would be lost; we do not want that.
        # Therefore dry-run logs are printed and not persisted unless --persist-dry-run-logs.
        if args.persist_dry_run_logs:
            ensure_weekly_schema(conn, args)
            seed_approved_field_config(conn, args, approved_fields)
            conn.commit()

    return summary


# -----------------------------------------------------------------------------
# Unresolved resolver
# -----------------------------------------------------------------------------


@dataclass
class UnresolvedGroup:
    field_name: str
    normalized_label: str
    statuses: list[str]
    raw_labels: list[str]
    source_record_ids: list[str]
    mapper_run_ids: list[str]
    row_count: int
    distinct_call_count: int
    avg_similarity: Optional[float]
    max_similarity: Optional[float]
    label_vectors: list[np.ndarray]
    top_candidates: list[dict[str, Any]]
    first_seen: Optional[datetime]
    last_seen: Optional[datetime]

    @property
    def primary_status(self) -> str:
        c = Counter(self.statuses)
        if c.get("TRUE_ANOMALY"):
            return "TRUE_ANOMALY"
        if c.get("NEW_CLUSTER_CANDIDATE"):
            return "NEW_CLUSTER_CANDIDATE"
        return self.statuses[0] if self.statuses else "UNKNOWN"

    @property
    def representative_raw_label(self) -> str:
        if not self.raw_labels:
            return self.normalized_label
        return Counter(self.raw_labels).most_common(1)[0][0]

    @property
    def centroid_vector(self) -> Optional[np.ndarray]:
        return centroid(self.label_vectors)


@dataclass
class KnownAnomalyEvidenceGroup:
    field_name: str
    cluster_id: str
    cluster_version: Optional[str]
    display_name: Optional[str]
    group: UnresolvedGroup


def load_unresolved_groups(conn, args: argparse.Namespace, window_start: datetime, window_end: datetime) -> list[UnresolvedGroup]:
    output_t = safe_pg_qualified_name(args.output_table)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                field_name,
                normalized_label,
                raw_label,
                source_record_id,
                mapper_run_id,
                mapping_status,
                similarity_score,
                label_embedding,
                top_candidates,
                COALESCE(classified_at, created_at) AS event_time
            FROM {output_t}
            WHERE mapping_status IN ('TRUE_ANOMALY', 'NEW_CLUSTER_CANDIDATE')
              AND normalized_label IS NOT NULL
              AND normalized_label <> ''
              AND COALESCE(classified_at, created_at) >= %s
              AND COALESCE(classified_at, created_at) < %s
            ORDER BY field_name, normalized_label, event_time
            """,
            (window_start, window_end),
        )
        rows = cur.fetchall()

    grouped: dict[tuple[str, str], list[dict[str, Any]]] = defaultdict(list)
    for row in rows:
        field = str(row["field_name"])
        norm = normalize_label(row["normalized_label"])
        if norm:
            grouped[(field, norm)].append(row)

    out: list[UnresolvedGroup] = []
    for (field, norm), members in grouped.items():
        sims = [float(m["similarity_score"]) for m in members if m.get("similarity_score") is not None]
        vectors = [parse_vector(m.get("label_embedding")) for m in members]
        vectors = [v for v in vectors if v is not None]
        candidates: list[dict[str, Any]] = []
        for m in members:
            parsed = parse_jsonish(m.get("top_candidates"))
            if isinstance(parsed, list):
                candidates.extend([x for x in parsed if isinstance(x, dict)])
        event_times = [m.get("event_time") for m in members if m.get("event_time") is not None]
        out.append(
            UnresolvedGroup(
                field_name=field,
                normalized_label=norm,
                statuses=[str(m.get("mapping_status") or "") for m in members],
                raw_labels=[str(m.get("raw_label") or "") for m in members if str(m.get("raw_label") or "").strip()],
                source_record_ids=sorted({str(m.get("source_record_id") or "") for m in members if m.get("source_record_id")}),
                mapper_run_ids=sorted({str(m.get("mapper_run_id") or "") for m in members if m.get("mapper_run_id")}),
                row_count=len(members),
                distinct_call_count=len({str(m.get("source_record_id") or "") for m in members if m.get("source_record_id")}),
                avg_similarity=float(np.mean(sims)) if sims else None,
                max_similarity=float(max(sims)) if sims else None,
                label_vectors=vectors,
                top_candidates=candidates,
                first_seen=min(event_times) if event_times else None,
                last_seen=max(event_times) if event_times else None,
            )
        )
    return out


def load_known_anomaly_evidence_groups(conn, args: argparse.Namespace, window_start: datetime, window_end: datetime) -> list[KnownAnomalyEvidenceGroup]:
    """
    Load already-known anomaly rows created by the hourly mapper.

    These rows are not unresolved; they already matched active true-anomaly
    clusters. Weekly still has to attach the new raw/normalized labels to the
    anomaly cluster, refresh counters, and promote the cluster if recurring
    evidence crosses the configured threshold.
    """
    output_t = safe_pg_qualified_name(args.output_table)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                field_name,
                mapped_cluster_id,
                mapped_display_name,
                cluster_version,
                normalized_label,
                raw_label,
                source_record_id,
                mapper_run_id,
                mapping_status,
                similarity_score,
                label_embedding,
                top_candidates,
                COALESCE(classified_at, created_at) AS event_time
            FROM {output_t}
            WHERE mapping_status = 'KNOWN_TRUE_ANOMALY'
              AND mapped_cluster_id IS NOT NULL
              AND normalized_label IS NOT NULL
              AND normalized_label <> ''
              AND COALESCE(classified_at, created_at) >= %s
              AND COALESCE(classified_at, created_at) < %s
            ORDER BY field_name, mapped_cluster_id, normalized_label, event_time
            """,
            (window_start, window_end),
        )
        rows = cur.fetchall()

    grouped: dict[tuple[str, str, str], list[dict[str, Any]]] = defaultdict(list)
    cluster_meta: dict[tuple[str, str], dict[str, Any]] = {}
    for row in rows:
        field = str(row.get("field_name") or "")
        cluster_id = str(row.get("mapped_cluster_id") or "")
        norm = normalize_label(row.get("normalized_label"))
        if not field or not cluster_id or not norm:
            continue
        grouped[(field, cluster_id, norm)].append(row)
        cluster_meta.setdefault(
            (field, cluster_id),
            {
                "cluster_version": row.get("cluster_version"),
                "display_name": row.get("mapped_display_name"),
            },
        )

    out: list[KnownAnomalyEvidenceGroup] = []
    for (field, cluster_id, norm), members in grouped.items():
        sims = [float(m["similarity_score"]) for m in members if m.get("similarity_score") is not None]
        vectors = [parse_vector(m.get("label_embedding")) for m in members]
        vectors = [v for v in vectors if v is not None]
        candidates: list[dict[str, Any]] = []
        for m in members:
            parsed = parse_jsonish(m.get("top_candidates"))
            if isinstance(parsed, list):
                candidates.extend([x for x in parsed if isinstance(x, dict)])
        event_times = [m.get("event_time") for m in members if m.get("event_time") is not None]
        meta = cluster_meta.get((field, cluster_id), {})
        group = UnresolvedGroup(
            field_name=field,
            normalized_label=norm,
            statuses=["KNOWN_TRUE_ANOMALY" for _ in members],
            raw_labels=[str(m.get("raw_label") or "") for m in members if str(m.get("raw_label") or "").strip()],
            source_record_ids=sorted({str(m.get("source_record_id") or "") for m in members if m.get("source_record_id")}),
            mapper_run_ids=sorted({str(m.get("mapper_run_id") or "") for m in members if m.get("mapper_run_id")}),
            row_count=len(members),
            distinct_call_count=len({str(m.get("source_record_id") or "") for m in members if m.get("source_record_id")}),
            avg_similarity=float(np.mean(sims)) if sims else None,
            max_similarity=float(max(sims)) if sims else None,
            label_vectors=vectors,
            top_candidates=candidates,
            first_seen=min(event_times) if event_times else None,
            last_seen=max(event_times) if event_times else None,
        )
        out.append(
            KnownAnomalyEvidenceGroup(
                field_name=field,
                cluster_id=cluster_id,
                cluster_version=str(meta.get("cluster_version") or args.weekly_cluster_version or "weekly"),
                display_name=meta.get("display_name"),
                group=group,
            )
        )
    return out


def choose_top_standard_candidate(group: UnresolvedGroup) -> Optional[dict[str, Any]]:
    candidates = [c for c in group.top_candidates if c.get("cluster_id")]
    if not candidates:
        return None
    by_cluster: dict[str, list[dict[str, Any]]] = defaultdict(list)
    for c in candidates:
        by_cluster[str(c.get("cluster_id"))].append(c)
    ranked = []
    for cid, rows in by_cluster.items():
        scores = [float(r.get("similarity_score")) for r in rows if r.get("similarity_score") is not None]
        ranked.append(
            {
                **rows[0],
                "cluster_id": cid,
                "avg_score": float(np.mean(scores)) if scores else None,
                "max_score": float(max(scores)) if scores else None,
                "count": len(rows),
                "stability_ratio": len(rows) / max(1, len(candidates)),
            }
        )
    ranked.sort(key=lambda x: (x.get("avg_score") or -1, x.get("count") or 0), reverse=True)
    return ranked[0] if ranked else None


def top_candidate_margin(group: UnresolvedGroup) -> Optional[float]:
    candidates = [c for c in group.top_candidates if c.get("similarity_score") is not None]
    if len(candidates) < 2:
        return None
    # Use the best score per cluster, then margin between top two clusters.
    best_by_cluster: dict[str, float] = {}
    for c in candidates:
        cid = str(c.get("cluster_id") or "")
        if not cid:
            continue
        score = float(c.get("similarity_score"))
        best_by_cluster[cid] = max(best_by_cluster.get(cid, -1.0), score)
    if len(best_by_cluster) < 2:
        return None
    scores = sorted(best_by_cluster.values(), reverse=True)
    return float(scores[0] - scores[1])


def deterministic_safe_map_check(group: UnresolvedGroup, top_candidate: dict[str, Any], args: argparse.Namespace) -> tuple[bool, dict[str, Any]]:
    threshold = float(top_candidate.get("similarity_threshold") or DEFAULT_EXISTING_THRESHOLD)
    score = top_candidate.get("avg_score") or top_candidate.get("similarity_score") or group.avg_similarity
    score = float(score) if score is not None else None
    margin = top_candidate_margin(group)
    stability = float(top_candidate.get("stability_ratio") or 0.0)

    candidate_text = " ".join([group.normalized_label] + group.raw_labels[:5])
    target_text = " ".join(
        str(top_candidate.get(k) or "")
        for k in ["display_name", "cluster_name", "medoid_label"]
    )

    cand_tokens = token_set(candidate_text)
    target_tokens = token_set(target_text)
    overlap = sorted(cand_tokens & target_tokens)

    actor_check = actor_directionality_check(candidate_text, target_text)

    checks = {
        "score": score,
        "threshold": threshold,
        "threshold_gap": None if score is None else float(threshold - score),
        "close_to_threshold": False if score is None else (score >= threshold - args.safe_map_close_margin),
        "stability_ratio": stability,
        "stable_top_candidate": stability >= args.safe_map_stability_ratio,
        "top_margin": margin,
        "strong_margin": True if margin is None else margin >= args.safe_map_min_top_margin,
        "component_overlap": overlap,
        "shares_key_components": bool(overlap),
        "contradiction": has_contradiction(candidate_text, target_text),
        "actor_direction_conflict": bool(actor_check["actor_direction_conflict"]),
        "actor_directionality_check": actor_check,
    }

    passed = all(
        [
            checks["close_to_threshold"],
            checks["stable_top_candidate"],
            checks["strong_margin"],
            checks["shares_key_components"],
            not checks["contradiction"],
            not checks["actor_direction_conflict"],
        ]
    )
    return bool(passed), checks


def load_active_anomaly_clusters(conn, args: argparse.Namespace, field_name: str) -> list[dict[str, Any]]:
    cluster_t = safe_pg_qualified_name(args.cluster_table)
    cols = get_columns(conn, args.cluster_table)

    select_exprs = [
        "c.field_name",
        "c.cluster_id",
        "c.centroid_embedding",
        "COALESCE(NULLIF(n.display_name, ''), NULLIF(c.display_name, ''), c.cluster_id) AS display_name",
    ]
    for c in ["cluster_version", "run_id", "medoid_label", "total_occurrences", "cluster_size", "promotion_status"]:
        if c in cols:
            select_exprs.append(f"c.{safe_pg_identifier(c)}")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {', '.join(select_exprs)}
            FROM {cluster_t} c
            LEFT JOIN taxonomy_cluster_names n
              ON n.field_name = c.field_name
             AND n.cluster_id = c.cluster_id
             AND (n.run_id = c.run_id OR n.run_id IS NULL OR c.run_id IS NULL)
             AND (n.cluster_version = c.cluster_version OR n.cluster_version IS NULL OR c.cluster_version IS NULL)
            WHERE c.field_name = %s
              AND COALESCE(c.active, TRUE) = TRUE
              AND COALESCE(c.is_true_anomaly_cluster, FALSE) = TRUE
              AND c.centroid_embedding IS NOT NULL
            ORDER BY COALESCE(c.updated_at, c.created_at, NOW()) DESC NULLS LAST
            """,
            (field_name,),
        )
        return list(cur.fetchall())


def nearest_anomaly_cluster(conn, args: argparse.Namespace, group: UnresolvedGroup) -> tuple[Optional[dict[str, Any]], Optional[float]]:
    gvec = group.centroid_vector
    if gvec is None:
        return None, None
    best = None
    best_score = -1.0
    candidate_text = " ".join([group.normalized_label] + group.raw_labels[:5])
    for row in load_active_anomaly_clusters(conn, args, group.field_name):
        target_text = " ".join(
            str(row.get(k) or "")
            for k in ["display_name", "cluster_name", "medoid_label"]
        )
        actor_check = actor_directionality_check(candidate_text, target_text)
        if actor_check["actor_direction_conflict"]:
            row["actor_direction_conflict"] = True
            row["actor_directionality_check"] = actor_check
            continue
        vec = parse_vector(row.get("centroid_embedding"))
        sim = cosine_similarity(gvec, vec)
        if sim is not None and sim > best_score:
            row["actor_direction_conflict"] = False
            row["actor_directionality_check"] = actor_check
            best = row
            best_score = sim
    if best is None:
        return None, None
    return best, float(best_score)


def get_cluster_identity(conn, args: argparse.Namespace, field_name: str, cluster_id: str) -> Optional[dict[str, Any]]:
    cluster_t = safe_pg_qualified_name(args.cluster_table)
    cols = get_columns(conn, args.cluster_table)

    select_exprs = [
        "c.field_name",
        "c.cluster_id",
        "COALESCE(NULLIF(n.display_name, ''), NULLIF(c.display_name, ''), c.cluster_id) AS display_name",
    ]
    for c in ["cluster_version", "run_id", "is_true_anomaly_cluster", "active", "promotion_status", "total_occurrences", "cluster_size"]:
        if c in cols:
            select_exprs.append(f"c.{safe_pg_identifier(c)}")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {', '.join(select_exprs)}
            FROM {cluster_t} c
            LEFT JOIN taxonomy_cluster_names n
              ON n.field_name = c.field_name
             AND n.cluster_id = c.cluster_id
             AND (n.run_id = c.run_id OR n.run_id IS NULL OR c.run_id IS NULL)
             AND (n.cluster_version = c.cluster_version OR n.cluster_version IS NULL OR c.cluster_version IS NULL)
            WHERE c.field_name = %s AND c.cluster_id = %s
            ORDER BY COALESCE(c.active, TRUE) DESC, COALESCE(c.updated_at, c.created_at, NOW()) DESC NULLS LAST
            LIMIT 1
            """,
            (field_name, cluster_id),
        )
        return cur.fetchone()


def upsert_label_embedding(conn, args: argparse.Namespace, *, field_name: str, raw_label: str, normalized_label: str, vector: Optional[np.ndarray]) -> None:
    if vector is None or not table_exists(conn, args.embeddings_table):
        return
    cols = get_columns(conn, args.embeddings_table)
    if "normalized_label" not in cols:
        return
    emb_col = None
    for candidate in ["embedding", "label_embedding", "embedding_vector", "vector"]:
        if candidate in cols:
            emb_col = candidate
            break
    if not emb_col:
        return

    insert_cols = []
    values = []
    for col in ["field_name", "raw_label", "normalized_label", emb_col, "model_name", "text_mode", "created_at", "updated_at"]:
        if col not in cols:
            continue
        insert_cols.append(col)
        if col == "field_name":
            values.append(field_name)
        elif col == "raw_label":
            values.append(raw_label)
        elif col == "normalized_label":
            values.append(normalized_label)
        elif col == emb_col:
            values.append(vector_to_jsonb(vector))
        elif col == "model_name":
            values.append(args.embedding_model)
        elif col == "text_mode":
            values.append(args.text_mode)
        elif col in {"created_at", "updated_at"}:
            values.append(utcnow())

    if not insert_cols:
        return

    conflict = ""
    if "field_name" in cols:
        conflict = "ON CONFLICT DO NOTHING"
    else:
        conflict = "ON CONFLICT DO NOTHING"

    emb_t = safe_pg_qualified_name(args.embeddings_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {emb_t} ({', '.join(safe_pg_identifier(c) for c in insert_cols)})
            VALUES ({', '.join(['%s'] * len(values))})
            {conflict}
            """,
            values,
        )


def upsert_label_map(
    conn,
    args: argparse.Namespace,
    *,
    field_name: str,
    raw_label: str,
    normalized_label: str,
    cluster_id: str,
    cluster_version: str,
    display_name: str,
    value_count: int,
    is_true_anomaly: bool,
    final_cluster_source: Optional[str] = None,
    base_cluster_id: Optional[str] = None,
) -> None:
    label_map_t = safe_pg_qualified_name(args.label_map_table)
    cols = get_columns(conn, args.label_map_table)
    if not {"field_name", "normalized_label"}.issubset(cols):
        return

    # Update first to avoid assuming a unique constraint.
    set_parts = []
    params: list[Any] = []
    assignments = {
        "raw_label": raw_label,
        "final_cluster_id": cluster_id,
        "final_cluster_source": final_cluster_source or ("true_anomaly" if is_true_anomaly else "standard_cluster"),
        "base_cluster_id": base_cluster_id if base_cluster_id is not None else ("-1" if is_true_anomaly else cluster_id),
        "cluster_version": cluster_version,
        "display_name": display_name,
        "value_count": value_count,
        "final_is_true_anomaly": is_true_anomaly,
        "updated_at": utcnow(),
    }
    for col, val in assignments.items():
        if col in cols:
            set_parts.append(f"{safe_pg_identifier(col)} = %s")
            params.append(val)
    if set_parts:
        params.extend([field_name, normalized_label])
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {label_map_t}
                SET {', '.join(set_parts)}
                WHERE field_name = %s AND normalized_label = %s
                """,
                params,
            )
            if cur.rowcount and cur.rowcount > 0:
                return

    insert_values = {
        "field_name": field_name,
        "raw_label": raw_label,
        "normalized_label": normalized_label,
        "final_cluster_id": cluster_id,
        "final_cluster_source": final_cluster_source or ("true_anomaly" if is_true_anomaly else "standard_cluster"),
        "base_cluster_id": base_cluster_id if base_cluster_id is not None else ("-1" if is_true_anomaly else cluster_id),
        "cluster_version": cluster_version,
        "display_name": display_name,
        "value_count": value_count,
        "final_is_true_anomaly": is_true_anomaly,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }
    insert_cols = [c for c in insert_values.keys() if c in cols]
    values = [insert_values[c] for c in insert_cols]
    if not insert_cols:
        return
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {label_map_t} ({', '.join(safe_pg_identifier(c) for c in insert_cols)})
            VALUES ({', '.join(['%s'] * len(values))})
            """,
            values,
        )


def upsert_cluster_name(
    conn,
    args: argparse.Namespace,
    *,
    field_name: str,
    cluster_id: str,
    cluster_version: str,
    display_name: str,
    is_anomaly: bool,
    naming_method: str,
    naming_reason: str,
) -> None:
    if not table_exists(conn, args.cluster_name_table):
        return
    names_t = safe_pg_qualified_name(args.cluster_name_table)
    cols = get_columns(conn, args.cluster_name_table)
    required = {"field_name", "cluster_id", "display_name"}
    if not required.issubset(cols):
        return

    set_values = {
        "display_name": display_name,
        "is_anomaly": is_anomaly,
        "naming_method": naming_method,
        "naming_reason": naming_reason,
        "active": True,
        "updated_at": utcnow(),
    }
    set_parts = []
    params = []
    for col, value in set_values.items():
        if col in cols:
            set_parts.append(f"{safe_pg_identifier(col)} = %s")
            params.append(value)
    where = "field_name = %s AND cluster_id = %s"
    params.extend([field_name, cluster_id])
    if "cluster_version" in cols:
        where += " AND cluster_version = %s"
        params.append(cluster_version)
    with conn.cursor() as cur:
        if set_parts:
            cur.execute(f"UPDATE {names_t} SET {', '.join(set_parts)} WHERE {where}", params)
            if cur.rowcount and cur.rowcount > 0:
                return

    insert_values = {
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
    insert_cols = [c for c in insert_values.keys() if c in cols]
    values = [insert_values[c] for c in insert_cols]
    if insert_cols:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {names_t} ({', '.join(safe_pg_identifier(c) for c in insert_cols)}) VALUES ({', '.join(['%s'] * len(values))})",
                values,
            )


def compute_cluster_counters(conn, args: argparse.Namespace, field_name: str, cluster_id: str) -> dict[str, Any]:
    label_map_t = safe_pg_qualified_name(args.label_map_table)
    output_t = safe_pg_qualified_name(args.output_table)
    map_cols = get_columns(conn, args.label_map_table)
    if not {"field_name", "normalized_label", "final_cluster_id"}.issubset(map_cols):
        return {"row_count": 0, "distinct_call_count": 0, "weeks_seen": 0, "label_occurrence_sum": 0, "first_seen": None, "last_seen": None}
    value_expr = "COALESCE(SUM(value_count), 0)::int" if "value_count" in map_cols else "COUNT(*)::int"
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            WITH labels AS (
                SELECT
                    normalized_label,
                    {value_expr} AS label_occurrence_sum
                FROM {label_map_t}
                WHERE field_name = %s
                  AND final_cluster_id = %s
                  AND normalized_label IS NOT NULL
                  AND normalized_label <> ''
                GROUP BY normalized_label
            )
            SELECT
                COUNT(o.*)::int AS row_count,
                COUNT(DISTINCT o.source_record_id)::int AS distinct_call_count,
                COUNT(DISTINCT date_trunc('week', COALESCE(o.classified_at, o.created_at)))::int AS weeks_seen,
                COALESCE(MAX(l.label_occurrence_sum), 0)::int AS label_occurrence_sum,
                MIN(COALESCE(o.classified_at, o.created_at)) AS first_seen,
                MAX(COALESCE(o.classified_at, o.created_at)) AS last_seen
            FROM {output_t} o
            JOIN labels l ON l.normalized_label = o.normalized_label
            WHERE o.field_name = %s
            """,
            (field_name, cluster_id, field_name),
        )
        row = cur.fetchone() or {}
        return dict(row)


def promotion_threshold_met(field_name: str, counters: dict[str, Any]) -> tuple[bool, str]:
    cfg = FIELD_PROMOTION_THRESHOLDS.get(
        field_name,
        {
            "distinct_call_count": DEFAULT_GENERAL_PROMOTION_CALLS,
            "total_occurrences": DEFAULT_GENERAL_PROMOTION_OCCURRENCES,
            "weeks_seen": DEFAULT_GENERAL_PROMOTION_WEEKS,
        },
    )
    calls = int(counters.get("distinct_call_count") or 0)
    rows = int(counters.get("row_count") or 0)
    label_occurrences = int(counters.get("label_occurrence_sum") or 0)
    occurrences = max(rows, label_occurrences)
    weeks = int(counters.get("weeks_seen") or 0)
    reasons = []
    if calls >= cfg["distinct_call_count"]:
        reasons.append(f"distinct_call_count {calls} >= {cfg['distinct_call_count']}")
    if occurrences >= cfg["total_occurrences"]:
        reasons.append(f"occurrence_count {occurrences} >= {cfg['total_occurrences']}")
    if weeks >= cfg["weeks_seen"]:
        reasons.append(f"weeks_seen {weeks} >= {cfg['weeks_seen']}")
    return bool(reasons), "; ".join(reasons)


def normalize_display_name_key(value: Any) -> str:
    """Normalize display names for duplicate validation across GPT/deterministic naming."""
    text = str(value or "").lower().strip()
    text = text.replace("_", " ").replace("-", " ").replace("/", " ")
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def has_adjacent_repeated_words(value: Any) -> bool:
    words = str(value or "").split()
    for idx in range(1, len(words)):
        if words[idx].lower() == words[idx - 1].lower():
            return True
    return False


def validate_promoted_display_name(
    value: Any,
    *,
    existing_names: set[str],
    rejected_names: Optional[set[str]] = None,
    max_words: int = DEFAULT_GPT_NAME_MAX_WORDS,
) -> list[str]:
    """Return validation notes for a promoted standard display name."""
    notes: list[str] = []
    cleaned = sanitize_display_name_candidate(value, max_words=max_words)
    if not cleaned:
        return ["blank_name"]
    key = normalize_display_name_key(cleaned)
    if len(cleaned.split()) > max_words:
        notes.append("too_many_words")
    if key in existing_names:
        notes.append("existing_name_conflict")
    if rejected_names and key in rejected_names:
        notes.append("duplicate_against_rejected_name")
    if has_adjacent_repeated_words(cleaned):
        notes.append("adjacent_repeated_words")
    return notes


def duplicate_standard_display_name_exists(
    conn,
    args: argparse.Namespace,
    *,
    field_name: str,
    display_name: str,
    excluding_cluster_id: str,
) -> tuple[bool, list[dict[str, Any]]]:
    """Check duplicate standard display names before promotion."""
    if not table_exists(conn, args.cluster_name_table):
        return False, []
    names_t = safe_pg_qualified_name(args.cluster_name_table)
    cluster_t = safe_pg_qualified_name(args.cluster_table)
    name_cols = get_columns(conn, args.cluster_name_table)
    if not {"field_name", "cluster_id", "display_name"}.issubset(name_cols):
        return False, []

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                n.field_name,
                n.cluster_id,
                n.display_name,
                COALESCE(c.is_true_anomaly_cluster, FALSE) AS is_true_anomaly_cluster,
                COALESCE(c.active, TRUE) AS active,
                c.cluster_size,
                c.total_occurrences,
                c.medoid_label,
                c.promotion_status
            FROM {names_t} n
            LEFT JOIN {cluster_t} c
              ON c.field_name = n.field_name
             AND c.cluster_id = n.cluster_id
            WHERE n.field_name = %s
              AND LOWER(TRIM(n.display_name)) = LOWER(TRIM(%s))
              AND n.cluster_id <> %s
              AND COALESCE(c.active, TRUE) = TRUE
              AND COALESCE(c.is_true_anomaly_cluster, FALSE) = FALSE
            ORDER BY c.total_occurrences DESC NULLS LAST, n.cluster_id
            """,
            (field_name, display_name, excluding_cluster_id),
        )
        rows = [dict(r) for r in cur.fetchall()]
    return bool(rows), rows



def existing_standard_display_names(
    conn,
    args: argparse.Namespace,
    *,
    field_name: str,
    excluding_cluster_id: Optional[str] = None,
) -> set[str]:
    """Return normalized active standard display names for duplicate-safe GPT naming."""
    if not table_exists(conn, args.cluster_name_table):
        return set()
    names_t = safe_pg_qualified_name(args.cluster_name_table)
    cluster_t = safe_pg_qualified_name(args.cluster_table)
    name_cols = get_columns(conn, args.cluster_name_table)
    if not {"field_name", "cluster_id", "display_name"}.issubset(name_cols):
        return set()

    params: list[Any] = [field_name]
    exclude_sql = ""
    if excluding_cluster_id:
        exclude_sql = "AND n.cluster_id <> %s"
        params.append(excluding_cluster_id)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT LOWER(TRIM(n.display_name))
            FROM {names_t} n
            LEFT JOIN {cluster_t} c
              ON c.field_name = n.field_name
             AND c.cluster_id = n.cluster_id
            WHERE n.field_name = %s
              {exclude_sql}
              AND n.display_name IS NOT NULL
              AND TRIM(n.display_name) <> ''
              AND COALESCE(c.active, TRUE) = TRUE
              AND COALESCE(c.is_true_anomaly_cluster, FALSE) = FALSE
            """,
            params,
        )
        return {normalize_display_name_key(r[0]) for r in cur.fetchall() if r and r[0]}


def sanitize_display_name_candidate(value: Any, *, max_words: int = DEFAULT_GPT_NAME_MAX_WORDS) -> str:
    """Clean an LLM/deterministic cluster name into a safe short display name."""
    text = str(value or "").strip()
    if not text:
        return ""

    text = text.splitlines()[0]

    # Remove common artifact wrappers before punctuation stripping.
    text = text.replace("{", " ").replace("}", " ")
    text = text.replace("[", " ").replace("]", " ")
    text = text.replace("(", " ").replace(")", " ")

    # Convert separators into spaces so "{Discovery,Admin Routing}" does not become "DiscoveryAdmin Routing".
    text = re.sub(r"[_|,;:]+", " ", text)
    text = text.replace("-", " ").replace("/", " ")

    # Split camel case after wrapper/separator cleanup.
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)

    # Remove trailing hash/id artifacts such as cc1904, 00456c, 4c0753.
    text = re.sub(r"\b[0-9a-f]{5,}\b$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[a-z]{1,8}[0-9a-f]{4,}\b$", "", text, flags=re.IGNORECASE)

    # Do not keep artifact suffixes used only to avoid duplicate names.
    text = re.sub(r"\banomaly\s+review\b", " ", text, flags=re.IGNORECASE)

    text = text.strip('"\'` .,:;|-_')
    text = re.sub(r"[^A-Za-z0-9 &+]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()

    words = [w for w in text.split() if w]
    if not words:
        return ""

    words = words[:max_words]
    out = []
    for word in words:
        low = word.lower()
        if low in ACRONYMS:
            out.append(ACRONYMS[low])
        elif re.fullmatch(r"\d+[a-z]*", low):
            out.append(low.upper())
        elif word.isupper() and len(word) <= 5:
            out.append(word)
        else:
            out.append(low.capitalize())

    return " ".join(out).strip()


def gpt_standard_cluster_name(
    args: argparse.Namespace,
    group: UnresolvedGroup,
    *,
    proposed_display_name: str,
    existing_names: set[str],
) -> tuple[Optional[str], str]:
    """
    Use GPT for promoted standard-cluster naming with duplicate repair retries.

    Returns (name, reason). GPT is retried for validation failures such as
    duplicate names, blank names, adjacent repeated words, or previously rejected
    names. After --gpt-name-max-retries attempts, this returns None and the
    caller can fall back or block promotion.
    """
    if getattr(args, "standard_naming_method", DEFAULT_STANDARD_NAMING_METHOD) != "gpt":
        return None, "GPT naming disabled by --standard-naming-method."

    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None, "OPENAI_API_KEY is not set."

    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:  # pragma: no cover - optional dependency
        return None, f"openai package unavailable: {exc}"

    max_words = int(getattr(args, "gpt_name_max_words", DEFAULT_GPT_NAME_MAX_WORDS))
    max_retries = max(1, int(getattr(args, "gpt_name_max_retries", DEFAULT_GPT_NAME_MAX_RETRIES)))
    existing_names = {normalize_display_name_key(name) for name in existing_names if name}
    rejected_names: set[str] = set()
    rejected_display_names: list[str] = []
    validation_history: list[str] = []
    raw_examples = list(dict.fromkeys(group.raw_labels))[:12]

    client = OpenAI(api_key=api_key)

    for attempt in range(1, max_retries + 1):
        forbidden_names = sorted(existing_names | rejected_names)
        prompt = {
            "field_name": group.field_name,
            "normalized_label": group.normalized_label,
            "raw_label_examples": raw_examples,
            "current_or_proposed_name": proposed_display_name,
            "attempt": attempt,
            "max_attempts": max_retries,
            "names_to_avoid_normalized": forbidden_names[:350],
            "rejected_names_for_this_cluster": rejected_display_names[-20:],
            "previous_validation_failures": validation_history[-20:],
            "rules": [
                "Return only one display name.",
                f"Maximum {max_words} words.",
                "Use Title Case.",
                "Preserve acronyms exactly when present, e.g. DM, LOA, IVR, TPS, CRM, VAT, MOP, KVA, MPAN, MHHS, CCL, DNC, CoT, 3CX.",
                "Do not use quotes, bullets, IDs, counts, punctuation-only suffixes, underscores, or explanations.",
                "Do not invent concepts not supported by the labels.",
                "Avoid generic names unless the labels are truly broad.",
                "Do not add words like Review just to make the name unique.",
                "Do not return any name listed in names_to_avoid_normalized or rejected_names_for_this_cluster.",
                "If the current/proposed name is already exact and unique, return it unchanged.",
                "If it duplicates an existing standard name, make a more specific name using only supported label evidence.",
            ],
        }

        try:
            response = client.chat.completions.create(
                model=getattr(args, "gpt_naming_model", DEFAULT_GPT_NAMING_MODEL),
                messages=[
                    {
                        "role": "system",
                        "content": (
                            "You name call-classification taxonomy clusters. "
                            "Return a concise, supported cluster display name only."
                        ),
                    },
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                temperature=0,
                max_tokens=32,
            )
            raw_name = response.choices[0].message.content if response.choices else ""
        except Exception as exc:  # pragma: no cover - external API
            validation_history.append(f"attempt_{attempt}_gpt_error:{exc}")
            continue

        cleaned = sanitize_display_name_candidate(raw_name, max_words=max_words)
        notes = validate_promoted_display_name(
            cleaned,
            existing_names=existing_names,
            rejected_names=rejected_names,
            max_words=max_words,
        )
        if cleaned and not notes:
            retry_note = "" if attempt == 1 else f" after {attempt} GPT attempts"
            return cleaned, f"GPT-generated promoted standard cluster name{retry_note}."

        rejected_key = normalize_display_name_key(cleaned or raw_name)
        if rejected_key:
            rejected_names.add(rejected_key)
        if cleaned:
            rejected_display_names.append(cleaned)
        validation_history.append(
            f"attempt_{attempt}: proposed='{cleaned or str(raw_name or '').strip()}' notes={','.join(notes) or 'invalid'}"
        )

    reason = "; ".join(validation_history[-5:]) if validation_history else "no valid GPT response"
    return None, f"GPT could not generate a unique clean name after {max_retries} attempts. {reason}"


def resolve_promoted_standard_display_name(
    conn,
    args: argparse.Namespace,
    group: UnresolvedGroup,
    *,
    cluster_id: str,
    proposed_display_name: str,
) -> dict[str, Any]:
    """
    Resolve the final standard display name before promotion.

    GPT is the preferred path. Deterministic naming is only a fallback when the
    proposed name is already unique or GPT is disabled/unavailable. If a duplicate
    exists and GPT cannot provide a unique replacement, promotion remains blocked.
    """
    proposed = sanitize_display_name_candidate(
        proposed_display_name or display_name_from_label(group.representative_raw_label),
        max_words=int(getattr(args, "gpt_name_max_words", DEFAULT_GPT_NAME_MAX_WORDS)),
    ) or display_name_from_label(group.representative_raw_label)
    existing_names = existing_standard_display_names(
        conn,
        args,
        field_name=group.field_name,
        excluding_cluster_id=cluster_id,
    )

    # Always let GPT name promoted standard clusters when enabled. This handles
    # both normal naming and duplicate-name avoidance.
    gpt_name, gpt_reason = gpt_standard_cluster_name(
        args,
        group,
        proposed_display_name=proposed,
        existing_names=existing_names,
    )
    if gpt_name:
        return {
            "display_name": gpt_name,
            "naming_method": "gpt_promoted_standard_name",
            "naming_reason": gpt_reason,
            "gpt_reason": gpt_reason,
            "used_gpt": True,
            "duplicate_before_gpt": normalize_display_name_key(proposed) in existing_names,
        }

    if normalize_display_name_key(proposed) not in existing_names:
        return {
            "display_name": proposed,
            "naming_method": "deterministic_promoted_standard_name_fallback",
            "naming_reason": f"GPT unavailable; deterministic proposed name was unique. {gpt_reason}",
            "gpt_reason": gpt_reason,
            "used_gpt": False,
            "duplicate_before_gpt": False,
        }

    return {
        "display_name": None,
        "naming_method": None,
        "naming_reason": f"Duplicate standard display name exists and GPT could not provide a unique name. {gpt_reason}",
        "gpt_reason": gpt_reason,
        "used_gpt": False,
        "duplicate_before_gpt": True,
    }


def update_label_map_for_promoted_cluster(
    conn,
    args: argparse.Namespace,
    *,
    field_name: str,
    cluster_id: str,
) -> int:
    label_map_t = safe_pg_qualified_name(args.label_map_table)
    cols = get_columns(conn, args.label_map_table)
    if not {"field_name", "final_cluster_id"}.issubset(cols):
        return 0

    assignments = {
        "final_cluster_source": "promoted_from_true_anomaly",
        "final_is_true_anomaly": False,
        "base_cluster_id": cluster_id,
        "updated_at": utcnow(),
    }
    set_parts = []
    params: list[Any] = []
    for col, value in assignments.items():
        if col in cols:
            set_parts.append(f"{safe_pg_identifier(col)} = %s")
            params.append(value)
    if not set_parts:
        return 0

    params.extend([field_name, cluster_id])
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {label_map_t}
            SET {', '.join(set_parts)}
            WHERE field_name = %s
              AND final_cluster_id = %s
            """,
            params,
        )
        return int(cur.rowcount or 0)


def update_mapper_output_to_standard_cluster(
    conn,
    args: argparse.Namespace,
    group: UnresolvedGroup,
    *,
    cluster_id: str,
    display_name: str,
    mapping_method: str = "weekly_promoted_from_true_anomaly",
) -> int:
    if not args.update_mapper_output:
        return 0

    output_t = safe_pg_qualified_name(args.output_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {output_t}
            SET mapped_cluster_id = %s,
                mapped_cluster_name = %s,
                mapped_display_name = %s,
                mapping_status = 'EXISTING_CLUSTER',
                mapping_method = %s,
                updated_at = NOW()
            WHERE field_name = %s
              AND normalized_label = %s
              AND mapping_status IN (
                    'TRUE_ANOMALY',
                    'NEW_CLUSTER_CANDIDATE',
                    'KNOWN_TRUE_ANOMALY',
                    'ATTACHED_TO_EXISTING_ANOMALY_CLUSTER'
              )
            """,
            (cluster_id, display_name, display_name, mapping_method, group.field_name, group.normalized_label),
        )
        return int(cur.rowcount or 0)


def update_mapper_output_cluster_to_standard(
    conn,
    args: argparse.Namespace,
    *,
    field_name: str,
    cluster_id: str,
    display_name: str,
    mapping_method: str = "weekly_promoted_known_anomaly_cluster",
) -> int:
    """Update every mapper output row currently pointing at a promoted anomaly cluster."""
    if not args.update_mapper_output:
        return 0

    output_t = safe_pg_qualified_name(args.output_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {output_t}
            SET mapped_cluster_id = %s,
                mapped_cluster_name = %s,
                mapped_display_name = %s,
                mapping_status = 'EXISTING_CLUSTER',
                mapping_method = %s,
                updated_at = NOW()
            WHERE field_name = %s
              AND mapped_cluster_id = %s
              AND mapping_status = 'KNOWN_TRUE_ANOMALY'
            """,
            (cluster_id, display_name, display_name, mapping_method, field_name, cluster_id),
        )
        return int(cur.rowcount or 0)


def promote_anomaly_to_standard_cluster(
    conn,
    args: argparse.Namespace,
    group: UnresolvedGroup,
    *,
    cluster_id: str,
    cluster_version: str,
    display_name: str,
    threshold_reason: str,
) -> dict[str, Any]:
    """Promote an individual anomaly cluster to a standard cluster with GPT naming and duplicate protection."""
    cluster_t = safe_pg_qualified_name(args.cluster_table)
    cluster_cols = get_columns(conn, args.cluster_table)

    name_resolution = resolve_promoted_standard_display_name(
        conn,
        args,
        group,
        cluster_id=cluster_id,
        proposed_display_name=display_name,
    )
    resolved_display_name = name_resolution.get("display_name")
    if not resolved_display_name:
        duplicate_rows = duplicate_standard_display_name_exists(
            conn,
            args,
            field_name=group.field_name,
            display_name=display_name,
            excluding_cluster_id=cluster_id,
        )[1]
        assignments = {
            "promotion_status": "STANDARD_CLUSTER_PROMOTION_BLOCKED_DUPLICATE_NAME",
            "promotion_candidate_reason": f"{threshold_reason}; {name_resolution.get('naming_reason')}",
            "updated_at": utcnow(),
        }
        set_parts = []
        params: list[Any] = []
        for col, value in assignments.items():
            if col in cluster_cols:
                set_parts.append(f"{safe_pg_identifier(col)} = %s")
                params.append(value)
        if set_parts:
            params.extend([group.field_name, cluster_id])
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {cluster_t} SET {', '.join(set_parts)} WHERE field_name = %s AND cluster_id = %s",
                    params,
                )
        return {
            "promotion_status": "STANDARD_CLUSTER_PROMOTION_BLOCKED_DUPLICATE_NAME",
            "promotion_reason": threshold_reason,
            "duplicate_display_name_rows": duplicate_rows,
            "label_map_rows_updated": 0,
            "mapper_rows_updated": 0,
            "name_resolution": name_resolution,
        }

    # Final duplicate guard after GPT/deterministic resolution.
    has_duplicate, duplicate_rows = duplicate_standard_display_name_exists(
        conn,
        args,
        field_name=group.field_name,
        display_name=resolved_display_name,
        excluding_cluster_id=cluster_id,
    )
    if has_duplicate:
        assignments = {
            "promotion_status": "STANDARD_CLUSTER_PROMOTION_BLOCKED_DUPLICATE_NAME",
            "promotion_candidate_reason": f"{threshold_reason}; resolved name still duplicates an existing standard cluster",
            "updated_at": utcnow(),
        }
        set_parts = []
        params: list[Any] = []
        for col, value in assignments.items():
            if col in cluster_cols:
                set_parts.append(f"{safe_pg_identifier(col)} = %s")
                params.append(value)
        if set_parts:
            params.extend([group.field_name, cluster_id])
            with conn.cursor() as cur:
                cur.execute(
                    f"UPDATE {cluster_t} SET {', '.join(set_parts)} WHERE field_name = %s AND cluster_id = %s",
                    params,
                )
        return {
            "promotion_status": "STANDARD_CLUSTER_PROMOTION_BLOCKED_DUPLICATE_NAME",
            "promotion_reason": threshold_reason,
            "duplicate_display_name_rows": duplicate_rows,
            "label_map_rows_updated": 0,
            "mapper_rows_updated": 0,
            "name_resolution": name_resolution,
        }

    assignments = {
        "is_true_anomaly_cluster": False,
        "cluster_source": "promoted_from_true_anomaly",
        "display_name": resolved_display_name,
        "cluster_name": resolved_display_name,
        "promotion_status": "PROMOTED_TO_STANDARD",
        "promotion_candidate_reason": threshold_reason,
        "active": True,
        "updated_at": utcnow(),
    }
    set_parts = []
    params: list[Any] = []
    for col, value in assignments.items():
        if col in cluster_cols:
            set_parts.append(f"{safe_pg_identifier(col)} = %s")
            params.append(value)
    if set_parts:
        params.extend([group.field_name, cluster_id])
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {cluster_t} SET {', '.join(set_parts)} WHERE field_name = %s AND cluster_id = %s",
                params,
            )

    upsert_cluster_name(
        conn,
        args,
        field_name=group.field_name,
        cluster_id=cluster_id,
        cluster_version=cluster_version,
        display_name=resolved_display_name,
        is_anomaly=False,
        naming_method=name_resolution.get("naming_method") or "gpt_promoted_standard_name",
        naming_reason=name_resolution.get("naming_reason") or "Promoted recurring true anomaly to standard cluster using GPT naming.",
    )
    label_map_rows_updated = update_label_map_for_promoted_cluster(
        conn,
        args,
        field_name=group.field_name,
        cluster_id=cluster_id,
    )
    mapper_rows_updated = update_mapper_output_to_standard_cluster(
        conn,
        args,
        group,
        cluster_id=cluster_id,
        display_name=resolved_display_name,
    )
    return {
        "promotion_status": "PROMOTED_TO_STANDARD",
        "promotion_reason": threshold_reason,
        "duplicate_display_name_rows": [],
        "label_map_rows_updated": label_map_rows_updated,
        "mapper_rows_updated": mapper_rows_updated,
        "name_resolution": name_resolution,
    }

def create_or_update_anomaly_cluster(conn, args: argparse.Namespace, group: UnresolvedGroup, cluster_id: Optional[str] = None, *, promote_if_threshold: bool = True) -> tuple[str, str, dict[str, Any]]:
    cluster_t = safe_pg_qualified_name(args.cluster_table)
    cluster_cols = get_columns(conn, args.cluster_table)
    field = group.field_name
    norm = group.normalized_label
    raw = group.representative_raw_label
    cluster_id = cluster_id or make_anomaly_cluster_id(field, norm)
    cluster_version = args.weekly_cluster_version
    display_name = display_name_from_label(raw or norm)

    # Persist embedding for each raw label if possible.
    group_center = group.centroid_vector
    for raw_label in set(group.raw_labels or [raw]):
        upsert_label_embedding(conn, args, field_name=field, raw_label=raw_label, normalized_label=norm, vector=group_center)

    # Existing labels/vectors for this anomaly cluster.
    label_map_t = safe_pg_qualified_name(args.label_map_table)
    map_cols = get_columns(conn, args.label_map_table)
    existing_labels: list[tuple[str, str, int]] = []
    if {"field_name", "final_cluster_id", "normalized_label"}.issubset(map_cols):
        raw_col = "raw_label" if "raw_label" in map_cols else "normalized_label"
        value_col = "value_count" if "value_count" in map_cols else None
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT {raw_col} AS raw_label, normalized_label, {('COALESCE(value_count,1)' if value_col else '1')} AS value_count
                FROM {label_map_t}
                WHERE field_name = %s AND final_cluster_id = %s
                """,
                (field, cluster_id),
            )
            existing_labels = [(str(r.get("raw_label") or ""), normalize_label(r.get("normalized_label")), int(r.get("value_count") or 1)) for r in cur.fetchall()]

    all_label_counts = Counter()
    labels_and_vectors: list[tuple[str, np.ndarray]] = []
    vectors = []
    for label, _n, cnt in existing_labels:
        if label:
            all_label_counts[label] += cnt
    for label in group.raw_labels or [raw]:
        all_label_counts[label] += max(1, group.row_count)
    # Use group vectors for the current group. If there are existing vectors in embedding table, attempt to load them.
    for vec in group.label_vectors:
        vectors.append(vec)
        labels_and_vectors.append((raw, vec))

    # Try to include existing label embeddings.
    if table_exists(conn, args.embeddings_table):
        emb_t = safe_pg_qualified_name(args.embeddings_table)
        emb_cols = get_columns(conn, args.embeddings_table)
        emb_col = next((c for c in ["embedding", "label_embedding", "embedding_vector", "vector"] if c in emb_cols), None)
        if emb_col and "normalized_label" in emb_cols:
            existing_norms = sorted({n for _r, n, _c in existing_labels if n})
            if existing_norms:
                field_join = "AND field_name = %s" if "field_name" in emb_cols else ""
                params: list[Any] = [existing_norms]
                if "field_name" in emb_cols:
                    params.append(field)
                with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                    cur.execute(
                        f"SELECT normalized_label, {emb_col} AS embedding FROM {emb_t} WHERE normalized_label = ANY(%s) {field_join}",
                        params,
                    )
                    for er in cur.fetchall():
                        vec = parse_vector(er.get("embedding"))
                        if vec is not None:
                            vectors.append(vec)
                            labels_and_vectors.append((str(er.get("normalized_label")), vec))

    cvec = centroid(vectors) or group_center
    medoid, medoid_sim = medoid_label(labels_and_vectors, cvec)
    if not medoid:
        medoid = raw or norm
    reps = [label for label, _count in all_label_counts.most_common(10)] or [raw or norm]

    # Upsert label map for the current unresolved label into this anomaly cluster.
    upsert_label_map(
        conn,
        args,
        field_name=field,
        raw_label=raw,
        normalized_label=norm,
        cluster_id=cluster_id,
        cluster_version=cluster_version,
        display_name=display_name,
        value_count=max(1, group.row_count),
        is_true_anomaly=True,
        final_cluster_source="true_anomaly",
        base_cluster_id="-1",
    )

    counters = compute_cluster_counters(conn, args, field, cluster_id)
    threshold_met, threshold_reason = promotion_threshold_met(field, counters)
    promotion_status = "STANDARD_CLUSTER_PROMOTION_CANDIDATE" if threshold_met else "ACTIVE_TRUE_ANOMALY"

    base_values = {
        "field_name": field,
        "cluster_id": cluster_id,
        "cluster_version": cluster_version,
        "run_id": cluster_version,
        "display_name": display_name,
        "cluster_name": display_name,
        "cluster_source": "true_anomaly",
        "centroid_embedding": vector_to_jsonb(cvec),
        "medoid_label": medoid,
        "medoid_similarity_to_centroid": medoid_sim,
        "representative_labels": psycopg2.extras.Json(reps),
        "cluster_size": len(all_label_counts),
        "total_occurrences": sum(all_label_counts.values()),
        "is_true_anomaly_cluster": True,
        "active": True,
        "promotion_status": promotion_status,
        "weekly_first_seen": counters.get("first_seen") or group.first_seen,
        "weekly_last_seen": counters.get("last_seen") or group.last_seen,
        "weekly_occurrence_count": int(counters.get("row_count") or group.row_count),
        "weekly_distinct_call_count": int(counters.get("distinct_call_count") or group.distinct_call_count),
        "weekly_weeks_seen": int(counters.get("weeks_seen") or 1),
        "promotion_candidate_reason": threshold_reason if threshold_met else None,
        "created_at": utcnow(),
        "updated_at": utcnow(),
    }

    # UPDATE if exists.
    update_cols = [
        "display_name", "cluster_name", "cluster_source", "centroid_embedding", "medoid_label", "medoid_similarity_to_centroid",
        "representative_labels", "cluster_size", "total_occurrences", "is_true_anomaly_cluster", "active",
        "promotion_status", "weekly_first_seen", "weekly_last_seen", "weekly_occurrence_count",
        "weekly_distinct_call_count", "weekly_weeks_seen", "promotion_candidate_reason", "updated_at",
    ]
    set_parts = []
    params = []
    for col in update_cols:
        if col in cluster_cols:
            set_parts.append(f"{safe_pg_identifier(col)} = %s")
            params.append(base_values[col])
    params.extend([field, cluster_id])
    with conn.cursor() as cur:
        if set_parts:
            cur.execute(
                f"UPDATE {cluster_t} SET {', '.join(set_parts)} WHERE field_name = %s AND cluster_id = %s",
                params,
            )
            if cur.rowcount and cur.rowcount > 0:
                upsert_cluster_name(
                    conn,
                    args,
                    field_name=field,
                    cluster_id=cluster_id,
                    cluster_version=cluster_version,
                    display_name=display_name,
                    is_anomaly=True,
                    naming_method="weekly_deterministic_anomaly_label",
                    naming_reason="Anomaly cluster name generated from normalized unresolved label.",
                )
                mapper_rows_updated = update_mapper_output_to_anomaly_cluster(
                    conn,
                    args,
                    group,
                    cluster_id=cluster_id,
                    display_name=display_name,
                    mapping_method="weekly_updated_anomaly_cluster",
                )
                meta = {
                    "promotion_status": promotion_status,
                    "promotion_reason": threshold_reason,
                    "counters": counters,
                    "mapper_rows_updated": mapper_rows_updated,
                }
                if threshold_met and promote_if_threshold:
                    promotion_meta = promote_anomaly_to_standard_cluster(
                        conn,
                        args,
                        group,
                        cluster_id=cluster_id,
                        cluster_version=cluster_version,
                        display_name=display_name,
                        threshold_reason=threshold_reason,
                    )
                    meta.update(promotion_meta)
                return cluster_id, display_name, meta

    insert_cols = [c for c in base_values.keys() if c in cluster_cols]
    insert_values = [base_values[c] for c in insert_cols]
    if insert_cols:
        with conn.cursor() as cur:
            cur.execute(
                f"INSERT INTO {cluster_t} ({', '.join(safe_pg_identifier(c) for c in insert_cols)}) VALUES ({', '.join(['%s'] * len(insert_values))})",
                insert_values,
            )

    upsert_cluster_name(
        conn,
        args,
        field_name=field,
        cluster_id=cluster_id,
        cluster_version=cluster_version,
        display_name=display_name,
        is_anomaly=True,
        naming_method="weekly_deterministic_anomaly_label",
        naming_reason="Anomaly cluster name generated from normalized unresolved label.",
    )
    mapper_rows_updated = update_mapper_output_to_anomaly_cluster(
        conn,
        args,
        group,
        cluster_id=cluster_id,
        display_name=display_name,
        mapping_method="weekly_created_anomaly_cluster",
    )
    meta = {
        "promotion_status": promotion_status,
        "promotion_reason": threshold_reason,
        "counters": counters,
        "mapper_rows_updated": mapper_rows_updated,
    }
    if threshold_met and promote_if_threshold:
        promotion_meta = promote_anomaly_to_standard_cluster(
            conn,
            args,
            group,
            cluster_id=cluster_id,
            cluster_version=cluster_version,
            display_name=display_name,
            threshold_reason=threshold_reason,
        )
        meta.update(promotion_meta)
    return cluster_id, display_name, meta


def map_to_existing_cluster(conn, args: argparse.Namespace, group: UnresolvedGroup, target: dict[str, Any]) -> tuple[str, str]:
    target_cluster_id = str(target.get("cluster_id"))
    target_display_name = str(target.get("display_name") or target.get("cluster_name") or target_cluster_id)
    target_version = str(target.get("cluster_version") or args.weekly_cluster_version)
    upsert_label_map(
        conn,
        args,
        field_name=group.field_name,
        raw_label=group.representative_raw_label,
        normalized_label=group.normalized_label,
        cluster_id=target_cluster_id,
        cluster_version=target_version,
        display_name=target_display_name,
        value_count=max(1, group.row_count),
        is_true_anomaly=False,
        final_cluster_source="standard_cluster",
        base_cluster_id=target_cluster_id,
    )
    if args.update_mapper_output:
        output_t = safe_pg_qualified_name(args.output_table)
        with conn.cursor() as cur:
            cur.execute(
                f"""
                UPDATE {output_t}
                SET mapped_cluster_id = %s,
                    mapped_cluster_name = %s,
                    mapped_display_name = %s,
                    mapping_status = 'EXISTING_CLUSTER',
                    mapping_method = 'weekly_safe_map_to_existing',
                    updated_at = NOW()
                WHERE field_name = %s
                  AND normalized_label = %s
                  AND mapping_status = 'NEW_CLUSTER_CANDIDATE'
                """,
                (target_cluster_id, target_display_name, target_display_name, group.field_name, group.normalized_label),
            )
    return target_cluster_id, target_display_name




def update_mapper_output_to_anomaly_cluster(
    conn,
    args: argparse.Namespace,
    group: UnresolvedGroup,
    *,
    cluster_id: str,
    display_name: str,
    mapping_method: str = "weekly_resolved_to_anomaly_cluster",
) -> int:
    if not args.update_mapper_output:
        return 0

    output_t = safe_pg_qualified_name(args.output_table)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            UPDATE {output_t}
            SET mapped_cluster_id = %s,
                mapped_cluster_name = %s,
                mapped_display_name = %s,
                mapping_status = 'KNOWN_TRUE_ANOMALY',
                mapping_method = %s,
                updated_at = NOW()
            WHERE field_name = %s
              AND normalized_label = %s
              AND mapping_status IN ('TRUE_ANOMALY', 'NEW_CLUSTER_CANDIDATE', 'KNOWN_TRUE_ANOMALY')
            """,
            (cluster_id, display_name, display_name, mapping_method, group.field_name, group.normalized_label),
        )
        return int(cur.rowcount or 0)


def maybe_promote_known_anomaly_cluster(
    conn,
    args: argparse.Namespace,
    evidence: KnownAnomalyEvidenceGroup,
) -> dict[str, Any]:
    """Refresh counters for an existing known anomaly cluster and promote it if thresholds are met."""
    info = get_cluster_identity(conn, args, evidence.field_name, evidence.cluster_id)
    if not info:
        return {"promotion_status": "KNOWN_ANOMALY_CLUSTER_NOT_FOUND", "cluster_id": evidence.cluster_id}
    if not bool(info.get("is_true_anomaly_cluster")):
        return {"promotion_status": "ALREADY_STANDARD_CLUSTER", "cluster_id": evidence.cluster_id}

    counters = compute_cluster_counters(conn, args, evidence.field_name, evidence.cluster_id)
    threshold_met, threshold_reason = promotion_threshold_met(evidence.field_name, counters)
    promotion_status = "STANDARD_CLUSTER_PROMOTION_CANDIDATE" if threshold_met else "ACTIVE_TRUE_ANOMALY"

    cluster_t = safe_pg_qualified_name(args.cluster_table)
    cluster_cols = get_columns(conn, args.cluster_table)
    assignments = {
        "promotion_status": promotion_status,
        "weekly_first_seen": counters.get("first_seen") or evidence.group.first_seen,
        "weekly_last_seen": counters.get("last_seen") or evidence.group.last_seen,
        "weekly_occurrence_count": int(counters.get("row_count") or evidence.group.row_count),
        "weekly_distinct_call_count": int(counters.get("distinct_call_count") or evidence.group.distinct_call_count),
        "weekly_weeks_seen": int(counters.get("weeks_seen") or 1),
        "promotion_candidate_reason": threshold_reason if threshold_met else None,
        "updated_at": utcnow(),
    }
    set_parts = []
    params: list[Any] = []
    for col, value in assignments.items():
        if col in cluster_cols:
            set_parts.append(f"{safe_pg_identifier(col)} = %s")
            params.append(value)
    if set_parts:
        params.extend([evidence.field_name, evidence.cluster_id])
        with conn.cursor() as cur:
            cur.execute(
                f"UPDATE {cluster_t} SET {', '.join(set_parts)} WHERE field_name = %s AND cluster_id = %s",
                params,
            )

    meta: dict[str, Any] = {
        "promotion_status": promotion_status,
        "promotion_reason": threshold_reason,
        "counters": counters,
    }
    if threshold_met:
        display_name = str(info.get("display_name") or evidence.display_name or evidence.cluster_id)
        cluster_version = str(info.get("cluster_version") or evidence.cluster_version or args.weekly_cluster_version or "weekly")
        promotion_meta = promote_anomaly_to_standard_cluster(
            conn,
            args,
            evidence.group,
            cluster_id=evidence.cluster_id,
            cluster_version=cluster_version,
            display_name=display_name,
            threshold_reason=threshold_reason,
        )
        meta.update(promotion_meta)
        if promotion_meta.get("promotion_status") == "PROMOTED_TO_STANDARD":
            cluster_wide_rows = update_mapper_output_cluster_to_standard(
                conn,
                args,
                field_name=evidence.field_name,
                cluster_id=evidence.cluster_id,
                display_name=str(promotion_meta.get("name_resolution", {}).get("display_name") or display_name),
            )
            meta["cluster_wide_mapper_rows_updated"] = cluster_wide_rows
    return meta


def weekly_unresolved_label_resolver(conn, args: argparse.Namespace, weekly_run_id: str, window_start: datetime, window_end: datetime) -> dict[str, Any]:
    groups = load_unresolved_groups(conn, args, window_start, window_end)
    known_anomaly_groups = load_known_anomaly_evidence_groups(conn, args, window_start, window_end)
    summary = {
        "unresolved_groups": len(groups),
        "known_anomaly_evidence_groups": len(known_anomaly_groups),
        "true_anomaly_groups": 0,
        "new_cluster_candidate_groups": 0,
        "known_anomaly_clusters_refreshed": 0,
        "created_or_updated_anomaly_clusters": 0,
        "mapped_to_existing_clusters": 0,
        "attached_to_existing_anomaly_clusters": 0,
        "promotion_candidates": 0,
        "promoted_to_standard_clusters": 0,
        "promotion_blocked_duplicate_names": 0,
    }

    def count_promotion_result(meta: dict[str, Any]) -> None:
        status = meta.get("promotion_status")
        if status == "STANDARD_CLUSTER_PROMOTION_CANDIDATE":
            summary["promotion_candidates"] += 1
        elif status == "PROMOTED_TO_STANDARD":
            summary["promoted_to_standard_clusters"] += 1
        elif status == "STANDARD_CLUSTER_PROMOTION_BLOCKED_DUPLICATE_NAME":
            summary["promotion_blocked_duplicate_names"] += 1
    if not groups and not known_anomaly_groups:
        print("Weekly resolver: no TRUE_ANOMALY, NEW_CLUSTER_CANDIDATE, or KNOWN_TRUE_ANOMALY evidence groups found in window.")
        return summary

    if groups:
        print(f"Unresolved resolver: processing {len(groups)} unresolved label groups.")
    else:
        print("Unresolved resolver: no TRUE_ANOMALY or NEW_CLUSTER_CANDIDATE groups found in window.")

    for group in groups:
        status = group.primary_status
        if status == "TRUE_ANOMALY":
            summary["true_anomaly_groups"] += 1
            if args.apply:
                cluster_id, display_name, meta = create_or_update_anomaly_cluster(conn, args, group)
                count_promotion_result(meta)
                summary["created_or_updated_anomaly_clusters"] += 1
                resolution_status = "DONE"
            else:
                cluster_id = make_anomaly_cluster_id(group.field_name, group.normalized_label)
                display_name = display_name_from_label(group.representative_raw_label)
                meta = {"dry_run": True}
                resolution_status = "PLANNED"

            log_action(
                conn,
                args,
                weekly_run_id=weekly_run_id,
                field_name=group.field_name,
                normalized_label=group.normalized_label,
                raw_label_examples=list(dict.fromkeys(group.raw_labels))[:10],
                source_mapper_run_ids=group.mapper_run_ids,
                source_record_ids=group.source_record_ids,
                original_mapping_status=status,
                recommended_action="CREATE_OR_UPDATE_INDIVIDUAL_ANOMALY_CLUSTER",
                resolution_status=resolution_status,
                target_cluster_id=cluster_id,
                target_display_name=display_name,
                similarity_score=group.avg_similarity,
                evidence={
                    "row_count": group.row_count,
                    "distinct_call_count": group.distinct_call_count,
                    "avg_similarity": group.avg_similarity,
                    "max_similarity": group.max_similarity,
                    "meta": meta,
                },
            )
            continue

        if status == "NEW_CLUSTER_CANDIDATE":
            summary["new_cluster_candidate_groups"] += 1
            nearest_anom, anomaly_score = nearest_anomaly_cluster(conn, args, group)
            if nearest_anom and anomaly_score is not None and anomaly_score >= args.anomaly_attach_threshold:
                target_cluster_id = str(nearest_anom["cluster_id"])
                if args.apply:
                    cluster_id, display_name, meta = create_or_update_anomaly_cluster(conn, args, group, cluster_id=target_cluster_id)
                    summary["attached_to_existing_anomaly_clusters"] += 1
                    count_promotion_result(meta)
                    resolution_status = "DONE"
                else:
                    cluster_id = target_cluster_id
                    display_name = str(nearest_anom.get("display_name") or nearest_anom.get("cluster_name") or target_cluster_id)
                    meta = {"dry_run": True, "nearest_anomaly_similarity": anomaly_score}
                    resolution_status = "PLANNED"
                log_action(
                    conn,
                    args,
                    weekly_run_id=weekly_run_id,
                    field_name=group.field_name,
                    normalized_label=group.normalized_label,
                    raw_label_examples=list(dict.fromkeys(group.raw_labels))[:10],
                    source_mapper_run_ids=group.mapper_run_ids,
                    source_record_ids=group.source_record_ids,
                    original_mapping_status=status,
                    recommended_action="ATTACH_TO_EXISTING_ANOMALY_CLUSTER",
                    resolution_status=resolution_status,
                    target_cluster_id=cluster_id,
                    target_display_name=display_name,
                    similarity_score=anomaly_score,
                    evidence={"nearest_anomaly": dict(nearest_anom), "meta": meta},
                )
                continue

            top_standard = choose_top_standard_candidate(group)
            safe_map_passed = False
            safe_map_checks = {}
            if top_standard:
                cluster_info = get_cluster_identity(conn, args, group.field_name, str(top_standard.get("cluster_id")))
                if cluster_info and not bool(cluster_info.get("is_true_anomaly_cluster")):
                    safe_map_passed, safe_map_checks = deterministic_safe_map_check(group, top_standard, args)

            if top_standard and safe_map_passed:
                if args.apply:
                    target_cluster_id, target_display_name = map_to_existing_cluster(conn, args, group, top_standard)
                    summary["mapped_to_existing_clusters"] += 1
                    resolution_status = "DONE"
                else:
                    target_cluster_id = str(top_standard.get("cluster_id"))
                    target_display_name = str(top_standard.get("display_name") or top_standard.get("cluster_name") or target_cluster_id)
                    resolution_status = "PLANNED"
                log_action(
                    conn,
                    args,
                    weekly_run_id=weekly_run_id,
                    field_name=group.field_name,
                    normalized_label=group.normalized_label,
                    raw_label_examples=list(dict.fromkeys(group.raw_labels))[:10],
                    source_mapper_run_ids=group.mapper_run_ids,
                    source_record_ids=group.source_record_ids,
                    original_mapping_status=status,
                    recommended_action="MAP_TO_EXISTING_CLUSTER",
                    resolution_status=resolution_status,
                    target_cluster_id=target_cluster_id,
                    target_display_name=target_display_name,
                    similarity_score=top_standard.get("avg_score") or group.avg_similarity,
                    evidence={"safe_map_checks": safe_map_checks, "top_standard_candidate": top_standard},
                )
                continue

            # Default for unresolved new candidates that are not safe to map.
            if args.apply:
                cluster_id, display_name, meta = create_or_update_anomaly_cluster(conn, args, group)
                summary["created_or_updated_anomaly_clusters"] += 1
                count_promotion_result(meta)
                resolution_status = "DONE"
            else:
                cluster_id = make_anomaly_cluster_id(group.field_name, group.normalized_label)
                display_name = display_name_from_label(group.representative_raw_label)
                meta = {"dry_run": True, "safe_map_checks": safe_map_checks, "top_standard_candidate": top_standard}
                resolution_status = "PLANNED"
            log_action(
                conn,
                args,
                weekly_run_id=weekly_run_id,
                field_name=group.field_name,
                normalized_label=group.normalized_label,
                raw_label_examples=list(dict.fromkeys(group.raw_labels))[:10],
                source_mapper_run_ids=group.mapper_run_ids,
                source_record_ids=group.source_record_ids,
                original_mapping_status=status,
                recommended_action="CREATE_OR_UPDATE_INDIVIDUAL_ANOMALY_CLUSTER",
                resolution_status=resolution_status,
                target_cluster_id=cluster_id,
                target_display_name=display_name,
                similarity_score=group.avg_similarity,
                evidence={
                    "reason": "NEW_CLUSTER_CANDIDATE was not safe to map to standard cluster and did not attach to existing anomaly cluster.",
                    "safe_map_checks": safe_map_checks,
                    "top_standard_candidate": top_standard,
                    "meta": meta,
                },
            )


    if known_anomaly_groups:
        print(f"Known-anomaly resolver: processing {len(known_anomaly_groups)} known anomaly evidence groups.")

    for evidence in known_anomaly_groups:
        group = evidence.group
        if args.apply:
            cluster_id, display_name, meta = create_or_update_anomaly_cluster(
                conn,
                args,
                group,
                cluster_id=evidence.cluster_id,
                promote_if_threshold=False,
            )
            promotion_meta = maybe_promote_known_anomaly_cluster(conn, args, evidence)
            meta.update({f"known_anomaly_{k}": v for k, v in promotion_meta.items()})
            summary["known_anomaly_clusters_refreshed"] += 1
            count_promotion_result(promotion_meta)
            resolution_status = "DONE"
        else:
            cluster_id = evidence.cluster_id
            display_name = str(evidence.display_name or evidence.cluster_id)
            meta = {
                "dry_run": True,
                "known_anomaly_cluster_id": evidence.cluster_id,
                "note": "Would refresh known anomaly evidence, update anomaly counters, and promote if thresholds are met.",
            }
            resolution_status = "PLANNED"

        log_action(
            conn,
            args,
            weekly_run_id=weekly_run_id,
            field_name=group.field_name,
            normalized_label=group.normalized_label,
            raw_label_examples=list(dict.fromkeys(group.raw_labels))[:10],
            source_mapper_run_ids=group.mapper_run_ids,
            source_record_ids=group.source_record_ids,
            original_mapping_status="KNOWN_TRUE_ANOMALY",
            recommended_action="REFRESH_KNOWN_ANOMALY_AND_PROMOTE_IF_RECURRING",
            resolution_status=resolution_status,
            target_cluster_id=cluster_id,
            target_display_name=display_name,
            similarity_score=group.avg_similarity,
            evidence={
                "row_count": group.row_count,
                "distinct_call_count": group.distinct_call_count,
                "avg_similarity": group.avg_similarity,
                "max_similarity": group.max_similarity,
                "meta": meta,
            },
        )

    if args.apply:
        conn.commit()
        print(f"Persisted weekly unresolved action rows: {count_weekly_actions(conn, args, weekly_run_id)}")
    else:
        # In unresolved dry-run mode, no taxonomy tables are mutated; only PLANNED
        # action rows are inserted. Keep them only when explicitly requested.
        if args.persist_dry_run_logs:
            conn.commit()
            persisted_count = count_weekly_actions(conn, args, weekly_run_id)
            print(f"Persisted dry-run weekly unresolved action rows: {persisted_count}")
            summary["persisted_dry_run_action_rows"] = persisted_count
        else:
            conn.rollback()

    return summary


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def parse_approved_fields(value: Optional[str]) -> list[str]:
    if not value:
        return list(DEFAULT_APPROVED_FIELDS)
    return [x.strip() for x in value.split(",") if x.strip()]


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Weekly taxonomy maintenance: config repair + unresolved label resolver.")
    parser.add_argument("--env-file", default=".env")
    parser.add_argument("--mode", default="weekly_v1", choices=["weekly_v1"])
    parser.add_argument("--apply", action="store_true", help="Apply repairs/resolutions. Default is dry run.")
    parser.add_argument("--persist-dry-run-logs", action="store_true", help="Persist dry-run logs. Default dry run prints summary only.")
    parser.add_argument("--config-only", action="store_true")
    parser.add_argument("--unresolved-only", action="store_true")

    parser.add_argument("--lookback-days", type=int, default=7)
    parser.add_argument("--window-start", default=None, help="UTC ISO timestamp override.")
    parser.add_argument("--window-end", default=None, help="UTC ISO timestamp override.")

    parser.add_argument("--approved-fields", default=",".join(DEFAULT_APPROVED_FIELDS))

    parser.add_argument("--output-table", default=DEFAULT_OUTPUT_TABLE)
    parser.add_argument("--cluster-table", default=DEFAULT_CLUSTER_TABLE)
    parser.add_argument("--cluster-name-table", default=DEFAULT_CLUSTER_NAME_TABLE)
    parser.add_argument("--label-map-table", default=DEFAULT_LABEL_MAP_TABLE)
    parser.add_argument("--embeddings-table", default=DEFAULT_EMBEDDINGS_TABLE)
    parser.add_argument("--weekly-run-table", default=DEFAULT_WEEKLY_RUN_TABLE)
    parser.add_argument("--weekly-repair-log-table", default=DEFAULT_WEEKLY_REPAIR_LOG_TABLE)
    parser.add_argument("--weekly-action-table", default=DEFAULT_WEEKLY_ACTION_TABLE)
    parser.add_argument("--field-config-table", default=DEFAULT_FIELD_CONFIG_TABLE)

    parser.add_argument("--weekly-cluster-version", default=None, help="Cluster version for weekly anomaly clusters. Defaults to weekly run id.")
    parser.add_argument("--embedding-model", default="sentence-transformers/all-MiniLM-L6-v2")
    parser.add_argument("--text-mode", default="field_label")

    parser.add_argument("--safe-map-close-margin", type=float, default=DEFAULT_SAFE_MAP_CLOSE_MARGIN)
    parser.add_argument("--safe-map-min-top-margin", type=float, default=DEFAULT_SAFE_MAP_MIN_TOP_MARGIN)
    parser.add_argument("--safe-map-stability-ratio", type=float, default=DEFAULT_SAFE_MAP_STABILITY_RATIO)
    parser.add_argument("--anomaly-attach-threshold", type=float, default=DEFAULT_ANOMALY_ATTACH_THRESHOLD)
    parser.add_argument("--update-mapper-output", action=argparse.BooleanOptionalAction, default=True, help="When resolving labels, also update mapper output rows. Default: true.")
    parser.add_argument("--standard-naming-method", choices=["gpt", "deterministic"], default=DEFAULT_STANDARD_NAMING_METHOD, help="Naming method for promoted standard clusters. Default: gpt with deterministic fallback.")
    parser.add_argument("--gpt-naming-model", default=DEFAULT_GPT_NAMING_MODEL, help="OpenAI model for GPT cluster naming. Can also be set with OPENAI_NAMING_MODEL.")
    parser.add_argument("--gpt-name-max-words", type=int, default=DEFAULT_GPT_NAME_MAX_WORDS, help="Maximum words allowed in GPT-generated cluster display names.")
    parser.add_argument("--gpt-name-max-retries", type=int, default=DEFAULT_GPT_NAME_MAX_RETRIES, help="Maximum GPT renaming attempts when a promoted standard name is blank, invalid, or duplicate. Default: 5.")

    return parser.parse_args()


def parse_window(args: argparse.Namespace) -> tuple[datetime, datetime]:
    if args.window_end:
        window_end = datetime.fromisoformat(args.window_end.replace("Z", "+00:00"))
    else:
        window_end = utcnow()
    if window_end.tzinfo is None:
        window_end = window_end.replace(tzinfo=timezone.utc)

    if args.window_start:
        window_start = datetime.fromisoformat(args.window_start.replace("Z", "+00:00"))
        if window_start.tzinfo is None:
            window_start = window_start.replace(tzinfo=timezone.utc)
    else:
        window_start = window_end - timedelta(days=int(args.lookback_days))
    return window_start.astimezone(timezone.utc), window_end.astimezone(timezone.utc)


def main() -> int:
    args = parse_args()
    load_environment(args.env_file)

    weekly_run_id = make_weekly_run_id()
    if not args.weekly_cluster_version:
        args.weekly_cluster_version = weekly_run_id

    window_start, window_end = parse_window(args)

    conn = get_local_pg_connection()
    conn.autocommit = False

    summary: dict[str, Any] = {
        "weekly_run_id": weekly_run_id,
        "apply": bool(args.apply),
        "window_start": to_iso(window_start),
        "window_end": to_iso(window_end),
        "config_repair": {},
        "unresolved_resolver": {},
    }

    try:
        ensure_weekly_schema(conn, args)
        insert_weekly_run(conn, args, weekly_run_id, window_start, window_end)

        print(f"Weekly run id: {weekly_run_id}")
        print(f"Mode: {args.mode}")
        print(f"Apply changes: {args.apply}")
        print(f"Window UTC: {to_iso(window_start)} -> {to_iso(window_end)}")

        if not args.unresolved_only:
            summary["config_repair"] = weekly_config_repair(conn, args, weekly_run_id, window_start, window_end)

        if not args.config_only:
            summary["unresolved_resolver"] = weekly_unresolved_label_resolver(conn, args, weekly_run_id, window_start, window_end)

        # If dry-run rolled back, run row may no longer exist. Recreate only summary row for traceability.
        if not args.apply:
            ensure_weekly_schema(conn, args)
            insert_weekly_run(conn, args, weekly_run_id, window_start, window_end)

        complete_weekly_run(conn, args, weekly_run_id, "COMPLETED", summary)
        conn.commit()

        print("\nWeekly maintenance summary:")
        print(json.dumps(summary, indent=2, default=str))
        if not args.apply:
            print("\nDRY RUN ONLY. Re-run with --apply to write taxonomy repairs/resolutions.")
        return 0
    except Exception as exc:
        conn.rollback()
        try:
            ensure_weekly_schema(conn, args)
            insert_weekly_run(conn, args, weekly_run_id, window_start, window_end)
            complete_weekly_run(conn, args, weekly_run_id, "FAILED", {**summary, "error": str(exc)})
            conn.commit()
        except Exception:
            conn.rollback()
        print(f"ERROR: {exc}", file=sys.stderr)
        raise
    finally:
        conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
