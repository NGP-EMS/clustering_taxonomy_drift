#!/usr/bin/env python3
"""
taxonomy_unresolved_label_resolver.py

Reads taxonomy_unresolved_label_queue (where resolver_status IS NULL) and
recommends one of: MAP_TO_EXISTING | ANOMALY | PROMOTE.

What this script does:
  - Loads active cluster centroids, medoids, and representative labels from
    taxonomy_clusters + taxonomy_cluster_names.
  - Embeds unresolved labels in a single shared batched call (one model instance).
  - Computes cosine similarity against active cluster centroids for the same field.
  - For high-similarity matches: applies actor-aware directionality guard and
    contradiction guard before recommending MAP_TO_EXISTING.
  - For labels that match known true-anomaly clusters: recommends ANOMALY.
  - For labels with no strong cluster match: recommends PROMOTE if they appear
    frequently enough, otherwise leaves resolver_status NULL.
  - Writes recommendations into taxonomy_unresolved_label_queue.
    Does NOT write to STAGE. Does NOT create clusters. Does NOT rename clusters.

Thresholds (configurable via CLI):
  --map-threshold     Cosine similarity to recommend MAP_TO_EXISTING (default 0.82).
  --anomaly-threshold Cosine similarity to recommend ANOMALY (default 0.78).
  --promote-min-occurrences   Minimum occurrence_count to recommend PROMOTE (default 20).
  --promote-min-calls         Minimum distinct_call_count to recommend PROMOTE (default 10).

Embedding:
  Uses SentenceTransformers (same model as production mapper).
  Set EMBEDDING_MODEL env var or --embedding-model to override.
  Set EMBEDDING_DEVICE to cpu/cuda/openvino (default cpu).
  One model is loaded once; all labels for all fields are embedded in one batch.

Actor guard:
  Labels that semantically point towards "agent acted aggressively / rudely" are
  flagged when the candidate cluster points in the opposite direction (e.g. caller
  was rude). Stored in actor_guard_status, does NOT block the recommendation —
  it surfaces the conflict in evidence_json for human review.

Contradiction guard:
  A label that already has a different mapping in taxonomy_label_cluster_map for the
  same field is flagged in contradiction_guard_status.

Dry run:
  Default. Pass --apply to write recommendations to DB.

Env:
  LOCAL_DATABASE_URL or LOCAL_PG_HOST/PORT/DB/USER/PASSWORD

Example:
  python taxonomy_unresolved_label_resolver.py
  python taxonomy_unresolved_label_resolver.py --apply --field call_type
"""

from __future__ import annotations

import argparse
import json
import logging
import os
import re
import sys
from collections import defaultdict
from dataclasses import dataclass, field
from datetime import datetime, timezone
from typing import Any, Dict, List, Optional, Sequence, Tuple

import numpy as np
import psycopg2
import psycopg2.extras

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


# ── Constants ─────────────────────────────────────────────────────────────────

DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_MAP_THRESHOLD = 0.82
DEFAULT_ANOMALY_THRESHOLD = 0.78
DEFAULT_PROMOTE_MIN_OCCURRENCES = 20
DEFAULT_PROMOTE_MIN_CALLS = 10
IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Actor-polarity keywords: if a label contains these, the direction of the
# actor matters. Labels that semantically invert the agent/caller direction
# relative to their candidate cluster are flagged.
AGENT_ACTOR_KEYWORDS = re.compile(
    r"\b(agent|advisor|rep|staff|employee|team member)\b",
    re.IGNORECASE,
)
CALLER_ACTOR_KEYWORDS = re.compile(
    r"\b(caller|customer|client|patient|member)\b",
    re.IGNORECASE,
)


# ── Connection ────────────────────────────────────────────────────────────────

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


def connect(dsn: str):
    return psycopg2.connect(dsn)


def get_table_columns(conn, schema: str, table: str):
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
            "SELECT EXISTS (SELECT 1 FROM information_schema.tables "
            "WHERE table_schema=%s AND table_name=%s)",
            (schema, table),
        )
        return bool(cur.fetchone()[0])


# ── Normalisation ─────────────────────────────────────────────────────────────

def normalize_loose(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip().replace("_", " ")
    text = re.sub(r"([a-z])([A-Z])", r"\1 \2", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text if text else None


# ── Embedding ─────────────────────────────────────────────────────────────────

def load_embedding_model(model_name: str, device: str):
    try:
        from sentence_transformers import SentenceTransformer
    except ImportError:
        raise RuntimeError(
            "sentence-transformers is required. "
            "Install with: pip install sentence-transformers"
        )
    logging.info("Loading embedding model %s on %s", model_name, device)
    model = SentenceTransformer(model_name, device=device)
    logging.info("Model loaded.")
    return model


def embed_batch(model, texts: List[str]) -> np.ndarray:
    if not texts:
        return np.zeros((0, 0), dtype=np.float32)
    embeddings = model.encode(
        texts,
        batch_size=256,
        show_progress_bar=len(texts) > 200,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    return embeddings


def parse_embedding(value: Any) -> Optional[np.ndarray]:
    if value is None:
        return None
    if isinstance(value, (list, tuple)):
        arr = np.array(value, dtype=np.float32)
        return arr if arr.size else None
    if isinstance(value, str):
        text = value.strip()
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                arr = np.array(parsed, dtype=np.float32)
                return arr if arr.size else None
        except Exception:
            pass
        body = text.strip("[]{}").strip()
        try:
            arr = np.array([float(x) for x in body.split(",")], dtype=np.float32)
            return arr if arr.size else None
        except Exception:
            return None
    return None


def cosine_similarity(a: np.ndarray, b: np.ndarray) -> float:
    na = np.linalg.norm(a)
    nb = np.linalg.norm(b)
    if na == 0 or nb == 0:
        return 0.0
    return float(np.dot(a, b) / (na * nb))


# ── Cluster data loading ──────────────────────────────────────────────────────

@dataclass
class ClusterRecord:
    cluster_id: str
    field_name: str
    display_name: Optional[str]
    medoid_label: Optional[str]
    representative_labels: List[str]
    centroid: Optional[np.ndarray]
    is_anomaly: bool
    run_id: Optional[str]


def load_active_clusters(conn, schema: str = "public") -> Dict[str, List[ClusterRecord]]:
    """
    Returns a dict mapping field_name -> list of ClusterRecord.
    Loads centroid_embedding, medoid_label, representative_labels, and display_name.
    Compares against centroids AND medoid/representative text for fields without centroids.
    """
    tc_cols = set(get_table_columns(conn, schema, "taxonomy_clusters").keys())
    tcn_cols = set(get_table_columns(conn, schema, "taxonomy_cluster_names").keys())

    anom_col = (
        "tc.is_true_anomaly_cluster" if "is_true_anomaly_cluster" in tc_cols
        else "tc.is_anomaly" if "is_anomaly" in tc_cols
        else "FALSE"
    )
    centroid_col = (
        "tc.centroid_embedding" if "centroid_embedding" in tc_cols
        else "tc.centroid" if "centroid" in tc_cols
        else "NULL"
    )
    medoid_col = "tc.medoid_label" if "medoid_label" in tc_cols else "NULL::text"
    rep_col = "tc.representative_labels::text" if "representative_labels" in tc_cols else "NULL::text"
    run_expr = "COALESCE(tc.run_id, '')" if "run_id" in tc_cols else "''"

    name_join = "n.field_name = tc.field_name AND n.cluster_id = tc.cluster_id"
    if "run_id" in tc_cols and "run_id" in tcn_cols:
        name_join += " AND n.run_id = tc.run_id"

    active_filter = "COALESCE(tc.active, TRUE) = TRUE" if "active" in tc_cols else "TRUE"

    sql = f"""
        SELECT
            tc.field_name,
            tc.cluster_id,
            {run_expr} AS run_id,
            {anom_col} AS is_anomaly,
            {centroid_col}::text AS centroid_embedding,
            {medoid_col} AS medoid_label,
            {rep_col} AS representative_labels,
            n.display_name
        FROM {schema}.taxonomy_clusters tc
        LEFT JOIN {schema}.taxonomy_cluster_names n ON {name_join}
        WHERE {active_filter}
        ORDER BY tc.field_name, tc.cluster_id
    """

    clusters_by_field: Dict[str, List[ClusterRecord]] = defaultdict(list)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql)
        for row in cur:
            centroid = parse_embedding(row.get("centroid_embedding"))

            rep_labels: List[str] = []
            rep_raw = row.get("representative_labels")
            if rep_raw:
                try:
                    parsed = json.loads(rep_raw) if isinstance(rep_raw, str) else rep_raw
                    if isinstance(parsed, list):
                        rep_labels = [str(v) for v in parsed if v]
                    elif isinstance(parsed, dict):
                        rep_labels = list(parsed.keys())[:10]
                except Exception:
                    rep_labels = [rep_raw[:200]] if rep_raw else []

            record = ClusterRecord(
                cluster_id=row["cluster_id"],
                field_name=row["field_name"],
                display_name=row.get("display_name"),
                medoid_label=row.get("medoid_label"),
                representative_labels=rep_labels,
                centroid=centroid,
                is_anomaly=bool(row.get("is_anomaly")),
                run_id=row.get("run_id") or None,
            )
            clusters_by_field[row["field_name"]].append(record)

    total = sum(len(v) for v in clusters_by_field.values())
    logging.info(
        "Loaded %d active cluster records across %d fields",
        total, len(clusters_by_field),
    )
    return clusters_by_field


def load_unresolved_labels(
    conn,
    fields: Optional[List[str]],
    limit: Optional[int],
) -> List[Dict[str, Any]]:
    where = ["resolver_status IS NULL"]
    params: List[Any] = []
    if fields:
        where.append("field_name = ANY(%s)")
        params.append(fields)
    sql = (
        "SELECT id, field_name, raw_label, normalized_label, "
        "occurrence_count, distinct_call_count, source_examples "
        "FROM taxonomy_unresolved_label_queue "
        f"WHERE {' AND '.join(where)} "
        "ORDER BY occurrence_count DESC, distinct_call_count DESC"
    )
    if limit:
        sql += " LIMIT %s"
        params.append(limit)

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params or None)
        return [dict(r) for r in cur.fetchall()]


def load_existing_map_labels(conn, schema: str, field_name: str) -> Dict[str, str]:
    """
    Returns normalized_label -> cluster_id for all approved map rows of a field.
    Used for contradiction detection.
    """
    cols = set(get_table_columns(conn, schema, "taxonomy_label_cluster_map").keys())
    norm_col = "normalized_label" if "normalized_label" in cols else "raw_label"
    cluster_col = "final_cluster_id" if "final_cluster_id" in cols else "cluster_id"
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT {norm_col} AS norm, {cluster_col} AS cluster_id
            FROM {schema}.taxonomy_label_cluster_map
            WHERE field_name = %s
              AND {norm_col} IS NOT NULL
              AND {cluster_col} IS NOT NULL
            """,
            (field_name,),
        )
        return {row[0].lower().strip(): row[1] for row in cur.fetchall()}


# ── Guards ────────────────────────────────────────────────────────────────────

def actor_guard(label_text: str, cluster: ClusterRecord) -> Optional[str]:
    """
    Returns a status string if the label and candidate cluster describe
    opposite actor directions, or None if no conflict detected.
    """
    label_has_agent = bool(AGENT_ACTOR_KEYWORDS.search(label_text))
    label_has_caller = bool(CALLER_ACTOR_KEYWORDS.search(label_text))

    candidate_text = " ".join(filter(None, [
        cluster.display_name,
        cluster.medoid_label,
        *cluster.representative_labels[:5],
    ]))
    cluster_has_agent = bool(AGENT_ACTOR_KEYWORDS.search(candidate_text))
    cluster_has_caller = bool(CALLER_ACTOR_KEYWORDS.search(candidate_text))

    if label_has_agent and cluster_has_caller:
        return "ACTOR_CONFLICT:label=agent,cluster=caller"
    if label_has_caller and cluster_has_agent:
        return "ACTOR_CONFLICT:label=caller,cluster=agent"
    return None


def contradiction_guard(
    normalized_label: str,
    candidate_cluster_id: str,
    existing_map: Dict[str, str],
) -> Optional[str]:
    """
    Returns a status string if normalized_label is already approved-mapped
    to a DIFFERENT cluster in this field.
    """
    existing_cluster = existing_map.get(normalized_label.lower().strip())
    if existing_cluster and existing_cluster != candidate_cluster_id:
        return f"CONTRADICTION:already_mapped_to={existing_cluster}"
    return None


# ── Recommendation engine ─────────────────────────────────────────────────────

@dataclass
class Recommendation:
    queue_id: int
    field_name: str
    normalized_label: str
    resolver_status: Optional[str]       # MAP_TO_EXISTING | ANOMALY | PROMOTE | None
    target_cluster_id: Optional[str]
    target_display_name: Optional[str]
    similarity_score: Optional[float]
    actor_guard_status: Optional[str]
    contradiction_guard_status: Optional[str]
    evidence: Dict[str, Any]


def recommend(
    label_row: Dict[str, Any],
    label_embedding: np.ndarray,
    clusters: List[ClusterRecord],
    existing_map: Dict[str, str],
    map_threshold: float,
    anomaly_threshold: float,
    promote_min_occurrences: int,
    promote_min_calls: int,
) -> Recommendation:
    queue_id = label_row["id"]
    field_name = label_row["field_name"]
    norm = label_row["normalized_label"] or ""
    raw = label_row["raw_label"] or norm
    occurrences = label_row.get("occurrence_count", 0)
    distinct_calls = label_row.get("distinct_call_count", 0)

    # Compute similarity to all clusters that have centroids
    scored: List[Tuple[float, ClusterRecord]] = []
    for cluster in clusters:
        if cluster.centroid is not None:
            sim = cosine_similarity(label_embedding, cluster.centroid)
            scored.append((sim, cluster))

    # Sort descending
    scored.sort(key=lambda x: x[0], reverse=True)

    top_candidates = [
        {
            "cluster_id": c.cluster_id,
            "display_name": c.display_name,
            "medoid_label": c.medoid_label,
            "is_anomaly": c.is_anomaly,
            "similarity": round(sim, 4),
        }
        for sim, c in scored[:5]
    ]

    evidence: Dict[str, Any] = {
        "top_candidates": top_candidates,
        "occurrences": occurrences,
        "distinct_calls": distinct_calls,
    }

    if not scored:
        # No centroids available — cannot score. Fall through to PROMOTE check.
        if occurrences >= promote_min_occurrences and distinct_calls >= promote_min_calls:
            return Recommendation(
                queue_id=queue_id,
                field_name=field_name,
                normalized_label=norm,
                resolver_status="PROMOTE",
                target_cluster_id=None,
                target_display_name=None,
                similarity_score=None,
                actor_guard_status=None,
                contradiction_guard_status=None,
                evidence={**evidence, "note": "no_centroids_available"},
            )
        return Recommendation(
            queue_id=queue_id,
            field_name=field_name,
            normalized_label=norm,
            resolver_status=None,
            target_cluster_id=None,
            target_display_name=None,
            similarity_score=None,
            actor_guard_status=None,
            contradiction_guard_status=None,
            evidence={**evidence, "note": "no_centroids_and_below_promote_threshold"},
        )

    best_sim, best_cluster = scored[0]

    # Check true-anomaly clusters first
    if best_cluster.is_anomaly and best_sim >= anomaly_threshold:
        return Recommendation(
            queue_id=queue_id,
            field_name=field_name,
            normalized_label=norm,
            resolver_status="ANOMALY",
            target_cluster_id=best_cluster.cluster_id,
            target_display_name=best_cluster.display_name,
            similarity_score=round(best_sim, 4),
            actor_guard_status=None,
            contradiction_guard_status=None,
            evidence={**evidence, "note": "matched_anomaly_cluster"},
        )

    # Check standard clusters for MAP_TO_EXISTING
    for sim, cluster in scored:
        if cluster.is_anomaly:
            continue
        if sim < map_threshold:
            break

        a_status = actor_guard(raw, cluster)
        c_status = contradiction_guard(norm, cluster.cluster_id, existing_map)

        return Recommendation(
            queue_id=queue_id,
            field_name=field_name,
            normalized_label=norm,
            resolver_status="MAP_TO_EXISTING",
            target_cluster_id=cluster.cluster_id,
            target_display_name=cluster.display_name,
            similarity_score=round(sim, 4),
            actor_guard_status=a_status,
            contradiction_guard_status=c_status,
            evidence={
                **evidence,
                "map_threshold": map_threshold,
                "note": "approved_cluster_match",
            },
        )

    # No strong cluster match — check PROMOTE threshold
    if occurrences >= promote_min_occurrences and distinct_calls >= promote_min_calls:
        return Recommendation(
            queue_id=queue_id,
            field_name=field_name,
            normalized_label=norm,
            resolver_status="PROMOTE",
            target_cluster_id=None,
            target_display_name=None,
            similarity_score=round(best_sim, 4),
            actor_guard_status=None,
            contradiction_guard_status=None,
            evidence={**evidence, "note": "below_map_threshold_above_promote_threshold"},
        )

    # Not enough signal — leave NULL
    return Recommendation(
        queue_id=queue_id,
        field_name=field_name,
        normalized_label=norm,
        resolver_status="ANOMALY",
        target_cluster_id=None,
        target_display_name=None,
        similarity_score=round(best_sim, 4),
        actor_guard_status=None,
        contradiction_guard_status=None,
        evidence={
            **evidence,
            "note": "below_all_thresholds_low_signal_anomaly",
            "decision": "create_or_keep_as_true_anomaly",
        },
    )


# ── Write recommendations ─────────────────────────────────────────────────────

def write_recommendations(
    conn,
    recommendations: List[Recommendation],
    dry_run: bool,
) -> Dict[str, int]:
    counts: Dict[str, int] = {
        "MAP_TO_EXISTING": 0,
        "ANOMALY": 0,
        "PROMOTE": 0,
        "skipped_null": 0,
        "total": len(recommendations),
    }

    to_write = [r for r in recommendations if r.resolver_status is not None]
    for r in recommendations:
        if r.resolver_status is None:
            counts["skipped_null"] += 1
        else:
            counts[r.resolver_status] = counts.get(r.resolver_status, 0) + 1

    if dry_run:
        logging.info("[DRY RUN] Would write %d recommendations", len(to_write))
        for r in to_write:
            logging.debug(
                "  [DRY RUN] %s field=%s label=%s -> %s cluster=%s sim=%.4f",
                r.queue_id, r.field_name, r.normalized_label,
                r.resolver_status, r.target_cluster_id, r.similarity_score or 0,
            )
        return counts

    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(
            cur,
            """
            UPDATE taxonomy_unresolved_label_queue SET
                resolver_status            = %s,
                target_cluster_id          = %s,
                target_display_name        = %s,
                similarity_score           = %s,
                actor_guard_status         = %s,
                contradiction_guard_status = %s,
                evidence_json              = %s,
                updated_at                 = now()
            WHERE id = %s
            """,
            [
                (
                    r.resolver_status,
                    r.target_cluster_id,
                    r.target_display_name,
                    r.similarity_score,
                    r.actor_guard_status,
                    r.contradiction_guard_status,
                    json.dumps(r.evidence, default=str),
                    r.queue_id,
                )
                for r in to_write
            ],
            page_size=500,
        )
    conn.commit()
    logging.info("Wrote %d recommendations", len(to_write))
    return counts


# ── Main ──────────────────────────────────────────────────────────────────────

def run_resolver(args) -> None:
    dsn = args.local_database_url
    if not dsn:
        raise RuntimeError("Missing local DB. Set LOCAL_DATABASE_URL or LOCAL_PG_* env vars.")

    conn = connect(dsn)
    conn.autocommit = False

    try:
        # Load unresolved labels
        labels = load_unresolved_labels(
            conn,
            fields=args.fields or None,
            limit=args.limit,
        )
        logging.info("Found %d unresolved labels to process", len(labels))

        if not labels:
            logging.info("Nothing to resolve.")
            return

        # Load active clusters (centroids + medoids + representative labels)
        clusters_by_field = load_active_clusters(conn)

        # Build contradiction maps per field
        fields_seen = list({lbl["field_name"] for lbl in labels})
        map_table_cols = set(get_table_columns(conn, "public", "taxonomy_label_cluster_map").keys())
        existing_maps: Dict[str, Dict[str, str]] = {}
        for fn in fields_seen:
            try:
                existing_maps[fn] = load_existing_map_labels(conn, "public", fn)
            except Exception as exc:
                logging.warning("Could not load existing map for field %s: %s", fn, exc)
                existing_maps[fn] = {}

        # Embed all labels in one batch
        label_texts = [
            lbl["normalized_label"] or lbl["raw_label"] or ""
            for lbl in labels
        ]

        model = load_embedding_model(args.embedding_model, args.embedding_device)
        logging.info("Embedding %d labels...", len(label_texts))
        embeddings = embed_batch(model, label_texts)
        del model  # release VRAM / memory

        # Generate recommendations
        recommendations: List[Recommendation] = []
        for i, lbl in enumerate(labels):
            fn = lbl["field_name"]
            field_clusters = clusters_by_field.get(fn, [])
            existing_map = existing_maps.get(fn, {})
            emb = embeddings[i]

            rec = recommend(
                label_row=lbl,
                label_embedding=emb,
                clusters=field_clusters,
                existing_map=existing_map,
                map_threshold=args.map_threshold,
                anomaly_threshold=args.anomaly_threshold,
                promote_min_occurrences=args.promote_min_occurrences,
                promote_min_calls=args.promote_min_calls,
            )
            recommendations.append(rec)

        # Write (or dry-run) recommendations
        counts = write_recommendations(conn, recommendations, dry_run=args.dry_run)

        summary = {
            "dry_run": args.dry_run,
            "labels_processed": len(labels),
            "MAP_TO_EXISTING": counts.get("MAP_TO_EXISTING", 0),
            "ANOMALY": counts.get("ANOMALY", 0),
            "PROMOTE": counts.get("PROMOTE", 0),
            "skipped_null": counts.get("skipped_null", 0),
            "map_threshold": args.map_threshold,
            "anomaly_threshold": args.anomaly_threshold,
            "promote_min_occurrences": args.promote_min_occurrences,
            "promote_min_calls": args.promote_min_calls,
        }
        print(json.dumps(summary, indent=2))

    finally:
        conn.close()


def parse_args(argv=None):
    parser = argparse.ArgumentParser(
        description="Recommend resolver_status for taxonomy_unresolved_label_queue entries."
    )
    parser.add_argument(
        "--local-database-url",
        default=build_local_dsn(),
    )
    parser.add_argument(
        "--fields",
        nargs="*",
        default=None,
        help="Limit to these field names (space-separated). Default: all fields.",
    )
    parser.add_argument(
        "--dry-run",
        action="store_true",
        default=True,
        help="Default true. Use --apply to write to DB.",
    )
    parser.add_argument(
        "--apply",
        action="store_true",
        default=False,
        help="Write recommendations to DB.",
    )
    parser.add_argument("--limit", type=int, default=None)
    parser.add_argument(
        "--embedding-model",
        default=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL),
    )
    parser.add_argument(
        "--embedding-device",
        default=os.getenv("EMBEDDING_DEVICE", "cpu"),
        choices=["cpu", "cuda", "openvino", "npu"],
    )
    parser.add_argument("--map-threshold",     type=float, default=DEFAULT_MAP_THRESHOLD)
    parser.add_argument("--anomaly-threshold", type=float, default=DEFAULT_ANOMALY_THRESHOLD)
    parser.add_argument("--promote-min-occurrences", type=int, default=DEFAULT_PROMOTE_MIN_OCCURRENCES)
    parser.add_argument("--promote-min-calls",        type=int, default=DEFAULT_PROMOTE_MIN_CALLS)
    parser.add_argument(
        "--log-level",
        default="INFO",
        choices=["DEBUG", "INFO", "WARNING", "ERROR"],
    )

    args = parser.parse_args(argv)
    if args.apply:
        args.dry_run = False
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(message)s",
    )
    run_resolver(args)
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
