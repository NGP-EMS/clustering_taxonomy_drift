#!/usr/bin/env python3
"""
production_cluster_mapper_hourly.py

Production mapper for real post-classification call rows.

Purpose:
    - Read the real classification table, usually only the last 1 hour.
    - Split configured classification fields into individual labels.
    - Preserve raw labels exactly as received.
    - Normalize only for matching and embeddings.
    - First try exact approved taxonomy label mapping.
    - For unknown labels, embed and compare against active approved cluster centroids.
    - Upsert idempotent output rows for Iris/dashboard/downstream monitoring.
    - Never mutate approved taxonomy clusters, names, centroids, or label maps.

Required env values:
    APP_DB_HOST=...
    APP_DB_PORT=5432
    APP_DB_USER=...
    APP_DB_PASS=...
    APP_DB_NAME=ai-call-analysis-db

    LOCAL_PG_HOST=127.0.0.1
    LOCAL_PG_PORT=5432
    LOCAL_PG_DB=taxonomy_drift_local
    LOCAL_PG_USER=postgres
    LOCAL_PG_PASSWORD=postgres

Example hourly production run:
    python production_cluster_mapper_hourly.py ^
      --input-db app ^
      --input-table ngp_call_classification ^
      --timestamp-column created_at ^
      --lookback-hours 1 ^
      --call-id-column call_id ^
      --fields call_type,call_type_sub,main_reason,main_reason_sub,outcome,outcome_sub,next_step,coaching_tags,descriptive_keywords,additional_tags ^
      --env-file .env ^
      --cluster-table taxonomy_clusters ^
      --label-map-table taxonomy_label_cluster_map ^
      --output-table taxonomy_call_cluster_outputs ^
      --run-table taxonomy_mapper_runs ^
      --ensure-schema ^
      --write-output

Safe dry run:
    python production_cluster_mapper_hourly.py ^
      --input-db app ^
      --input-table ngp_call_classification ^
      --timestamp-column created_at ^
      --lookback-hours 1 ^
      --call-id-column call_id ^
      --fields call_type ^
      --env-file .env ^
      --ensure-schema ^
      --preview-csv taxonomy_cluster_output/production_mapper_hourly_preview.csv
"""

from __future__ import annotations

import argparse
import ast
import hashlib
import importlib.util
import json
import os
import re
import sys
import uuid
from dataclasses import dataclass
from datetime import datetime, timedelta, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Tuple

import numpy as np
import pandas as pd
import psycopg2
import psycopg2.extras
from sklearn.preprocessing import normalize as sk_normalize

try:
    from dotenv import load_dotenv
except ImportError:  # pragma: no cover
    load_dotenv = None

try:
    import torch
except ImportError:  # pragma: no cover
    torch = None


DEFAULT_CLUSTER_TABLE = "taxonomy_clusters"
DEFAULT_LABEL_MAP_TABLE = "taxonomy_label_cluster_map"
DEFAULT_OUTPUT_TABLE = "taxonomy_call_cluster_outputs"
DEFAULT_RUN_TABLE = "taxonomy_mapper_runs"
DEFAULT_EXISTING_CLUSTER_THRESHOLD = 0.82
DEFAULT_NEW_CLUSTER_CANDIDATE_THRESHOLD = 0.72
DEFAULT_TOP_K = 5
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/all-MiniLM-L6-v2"
DEFAULT_EMBEDDING_DIMENSIONS = 384
DEFAULT_NGP_INPUT_TABLE = "ngp_call_classification"
DEFAULT_NGP_FIELDS = (
    "call_type,call_type_sub,outcome,outcome_sub,main_reason,main_reason_sub,"
    "additional_tags,next_step,descriptive_keywords,coaching_tags"
)
DEFAULT_NGP_METADATA_COLUMNS = (
    "id,filename,call_id,call_date_time,created_at,batch_id,agent_extension,"
    "agent_name,business_name,contact_person_name,contact_person_role,"
    "contact_person_phone_number,is_decision_maker,duration,sales_stage,lead_status"
)

STABLE_MAPPING_STATUS = "EXISTING_CLUSTER"
KNOWN_ANOMALY_STATUS = "KNOWN_TRUE_ANOMALY"
EMERGING_MAPPING_STATUSES = {"NEW_CLUSTER_CANDIDATE", "TRUE_ANOMALY"}
CONFIG_ISSUE_STATUS = "NO_CLUSTER_REFERENCE"

FIELD_EMBEDDING_CONTEXT = {
    "call_type": "call type category",
    "call_type_sub": "secondary call type category",
    "main_reason": "main business reason for call",
    "main_reason_sub": "secondary business reason for call",
    "outcome": "call result or commercial outcome",
    "outcome_sub": "secondary call result",
    "tags": "structured call modifier",
    "additional_tags": "free-form business intelligence tag",
    "descriptive_keywords": "search keyword or notable call topic",
    "coaching_tags": "agent coaching tag indicating either a skill weakness requiring improvement (Coaching_Poor_* or Coaching_Unclear_*) or a demonstrated strength (Training_Good_* or Training_Clear_*)",
    "next_step": "next action after call",
    "tone": "customer tone or sentiment",
    "outcome_base": "broad outcome family",
    "call_type_base": "broad call type family",
}


# -----------------------------------------------------------------------------
# Dataclasses
# -----------------------------------------------------------------------------


@dataclass(frozen=True)
class ClusterReference:
    field_name: str
    cluster_id: str
    cluster_name: Optional[str]
    display_name: Optional[str]
    centroid_embedding: np.ndarray
    cluster_version: str
    similarity_threshold: float
    cluster_size: Optional[int] = None
    total_occurrences: Optional[int] = None
    medoid_label: Optional[str] = None
    is_true_anomaly_cluster: bool = False


@dataclass(frozen=True)
class ExactLabelReference:
    field_name: str
    normalized_label: str
    raw_label: str
    cluster_id: str
    display_name: Optional[str]
    cluster_name: Optional[str]
    cluster_version: str
    cluster_size: Optional[int] = None
    total_occurrences: Optional[int] = None
    is_true_anomaly_cluster: bool = False


@dataclass(frozen=True)
class ExplodedLabel:
    source_db: str
    source_table: str
    source_record_id: str
    source_created_at: Optional[datetime]
    classified_at: Optional[datetime]
    source_metadata: Dict[str, Any]
    field_name: str
    raw_label: str
    normalized_label: str


@dataclass(frozen=True)
class MappingResult:
    dedupe_key: str
    mapper_run_id: str
    source_db: str
    source_table: str
    source_record_id: str
    source_created_at: Optional[datetime]
    classified_at: Optional[datetime]
    source_metadata: Dict[str, Any]
    mapper_window_start: Optional[datetime]
    mapper_window_end: Optional[datetime]
    field_name: str
    raw_label: str
    normalized_label: str
    embedding_text: Optional[str]
    label_embedding: Optional[List[float]]
    mapped_cluster_id: Optional[str]
    mapped_cluster_name: Optional[str]
    mapped_display_name: Optional[str]
    similarity_score: Optional[float]
    existing_cluster_threshold: Optional[float]
    new_cluster_candidate_threshold: float
    mapping_status: str
    mapping_method: str
    cluster_version: Optional[str]
    top_candidates: List[Dict[str, Any]]
    embedding_model: str
    text_mode: str
    created_at: datetime


# -----------------------------------------------------------------------------
# Text handling
# -----------------------------------------------------------------------------


def normalize_label(value: Any) -> str:
    if value is None:
        return ""

    text = str(value).strip()
    if not text or text.lower() in {"nan", "none", "null", "n/a", "na", "unknown"}:
        return ""

    text = text.lower()
    text = text.replace("_", " ").replace("-", " ").replace("/", " ")
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"\s+", " ", text)
    return text.strip()



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

            between_start = min(left_idx, right_idx)
            between_end = max(left_idx, right_idx)
            action_categories = _action_categories_between(tokens, between_start, between_end)
            if not action_categories:
                # Allow compact forms like "agent customer abuse" where the action lands
                # immediately after the second actor.
                action_categories = _action_categories_between(tokens, between_start, min(len(tokens) - 1, between_end + 3))
            if not action_categories:
                continue

            actor = left_actor if left_idx < right_idx else right_actor
            target = right_actor if left_idx < right_idx else left_actor

            # Passive form: "customer insulted by agent" means agent -> customer.
            low = min(left_idx, right_idx)
            high = max(left_idx, right_idx)
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

def coaching_aware_embedding_text(label: Any) -> str:
    label_text = str(label or "")
    label_lower = label_text.lower()

    if any(w in label_lower for w in ["fraud", "compliance", "deceptive", "time_gaming"]):
        tier = "COMPLIANCE_RISK"
    elif any(w in label_lower for w in ["non_business", "personal_call", "audio_quality", "data_quality", "time_management"]):
        tier = "PROCESS_DISCIPLINE"
    else:
        tier = "AGENT_SKILL"

    if any(w in label_lower for w in ["good", "training", "clear", "strength"]):
        direction = "STRENGTH_DEMONSTRATED"
    elif any(w in label_lower for w in ["poor", "coaching", "unclear", "weakness"]):
        direction = "IMPROVEMENT_NEEDED"
    else:
        direction = "NEUTRAL"

    skill = label_text
    for prefix in [
        "Training_Good_",
        "Training_Clear_",
        "Coaching_Poor_",
        "Coaching_Unclear_",
        "Coaching_",
        "Training_",
    ]:
        if label_text.startswith(prefix):
            skill = label_text[len(prefix):]
            break

    return f"tier: {tier} | skill: {skill} | direction: {direction} | label: {normalize_label(label_text)}"


def next_step_aware_embedding_text(label: Any) -> str:
    label_text = str(label or "")
    label_lower = label_text.lower()

    if any(w in label_lower for w in ["sent", "scheduled", "confirmed", "received", "quote", "proposal", "pending", "closed", "date_found"]):
        polarity = "FORWARD_PROGRESS"
    elif any(w in label_lower for w in ["blocked", "do_not_call", "not_interested", "wrong_number", "ivr_failure"]):
        polarity = "DEAD_END"
    elif any(w in label_lower for w in ["research", "try_later", "internal", "transfer", "admin", "message", "na"]):
        polarity = "HOLDING_PATTERN"
    else:
        polarity = "UNKNOWN"

    return f"next_step_outcome: {polarity} | label: {normalize_label(label_text)}"


def embedding_text(source_column: str, raw_value: Any, mode: str = "field_label") -> str:
    cleaned = normalize_label(raw_value)
    source_column_clean = str(source_column or "").strip()

    if mode == "label_only":
        return cleaned

    if mode == "field_label":
        if source_column_clean == "coaching_tags":
            return coaching_aware_embedding_text(raw_value)

        if source_column_clean == "next_step":
            return next_step_aware_embedding_text(raw_value)

        short_context = FIELD_EMBEDDING_CONTEXT.get(
            source_column_clean,
            "call classification field",
        )
        return (
            f"field: {source_column_clean}; "
            f"meaning: {short_context}; "
            f"label: {cleaned}"
        )

    raise ValueError(f"Unknown embedding text mode: {mode}")


def split_labels(value: Any) -> List[str]:
    if value is None:
        return []
    if isinstance(value, float) and np.isnan(value):
        return []
    if isinstance(value, (list, tuple, set)):
        items = list(value)
    else:
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "null", "n/a", "na"}:
            return []

        parsed = None
        if (text.startswith("[") and text.endswith("]")) or (text.startswith("(") and text.endswith(")")):
            try:
                parsed = json.loads(text)
            except Exception:
                try:
                    parsed = ast.literal_eval(text)
                except Exception:
                    parsed = None

        if isinstance(parsed, (list, tuple, set)):
            items = list(parsed)
        else:
            if "|" in text:
                items = text.split("|")
            elif ";" in text:
                items = text.split(";")
            elif "," in text:
                items = text.split(",")
            else:
                items = [text]

    cleaned: List[str] = []
    seen = set()
    for item in items:
        raw = str(item).strip()
        normalized = normalize_label(raw)
        if normalized and normalized not in seen:
            cleaned.append(raw)
            seen.add(normalized)
    return cleaned


# -----------------------------------------------------------------------------
# Utility helpers
# -----------------------------------------------------------------------------


def stable_record_id(row_index: int, row: pd.Series) -> str:
    payload = json.dumps(row.fillna("").to_dict(), sort_keys=True, default=str)
    digest = hashlib.sha256(f"{row_index}:{payload}".encode("utf-8")).hexdigest()[:24]
    return f"generated_{digest}"


def utc_now() -> datetime:
    return datetime.now(timezone.utc)


def to_utc_datetime(value: Any) -> Optional[datetime]:
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        dt = value.to_pydatetime()
    elif isinstance(value, datetime):
        dt = value
    else:
        text = str(value).strip()
        if not text or text.lower() in {"nan", "none", "null"}:
            return None
        try:
            dt = pd.to_datetime(text, utc=False).to_pydatetime()
        except Exception:
            return None

    if dt.tzinfo is None:
        return dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def parse_iso_utc(value: Optional[str]) -> Optional[datetime]:
    if not value:
        return None
    text = value.strip().replace("Z", "+00:00")
    dt = datetime.fromisoformat(text)
    if dt.tzinfo is None:
        dt = dt.replace(tzinfo=timezone.utc)
    return dt.astimezone(timezone.utc)


def compute_window(args: argparse.Namespace) -> Tuple[Optional[datetime], Optional[datetime]]:
    if not args.timestamp_column:
        return None, None

    end = parse_iso_utc(args.window_end_utc) or utc_now()
    start = parse_iso_utc(args.window_start_utc)
    if start is None:
        start = end - timedelta(hours=float(args.lookback_hours))

    if start >= end:
        raise ValueError(f"Invalid mapper window: start {start.isoformat()} must be before end {end.isoformat()}.")

    return start, end


def json_default(value: Any) -> Any:
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.ndarray):
        return value.tolist()
    return str(value)


def serialize_metadata_value(value: Any) -> Any:
    if value is None:
        return None
    if isinstance(value, float) and np.isnan(value):
        return None
    if isinstance(value, pd.Timestamp):
        if pd.isna(value):
            return None
        return value.isoformat()
    if isinstance(value, datetime):
        return value.isoformat()
    if isinstance(value, np.integer):
        return int(value)
    if isinstance(value, np.floating):
        return float(value)
    if isinstance(value, np.bool_):
        return bool(value)
    return value


def row_metadata(row: pd.Series, metadata_columns: Sequence[str]) -> Dict[str, Any]:
    metadata: Dict[str, Any] = {}
    for col in metadata_columns:
        if col in row.index:
            metadata[col] = serialize_metadata_value(row.get(col))
    return metadata


def stable_dedupe_key(
    *,
    source_db: str,
    source_table: str,
    source_record_id: str,
    field_name: str,
    normalized_label: str,
    cluster_version: Optional[str],
) -> str:
    payload = "|".join(
        [
            source_db or "",
            source_table or "",
            source_record_id or "",
            field_name or "",
            normalized_label or "",
            cluster_version or "",
        ]
    )
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()


# -----------------------------------------------------------------------------
# Embeddings
# -----------------------------------------------------------------------------


class EmbeddingProvider:
    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        raise NotImplementedError


class SentenceTransformerEmbeddingProvider(EmbeddingProvider):
    def __init__(self, model_name: str, device: str):
        try:
            from sentence_transformers import SentenceTransformer
        except ImportError as exc:
            raise RuntimeError(
                "sentence-transformers is not installed. Install it with: pip install sentence-transformers"
            ) from exc

        self.model_name = model_name
        self.device = device
        if device in {"npu", "openvino-gpu", "openvino-cpu"}:
            ov_device = {"npu": "NPU", "openvino-gpu": "GPU", "openvino-cpu": "CPU"}[device]
            try:
                self.model = SentenceTransformer(
                    model_name,
                    backend="openvino",
                    model_kwargs={"device": ov_device},
                )
            except Exception as exc:
                raise RuntimeError(
                    f"Failed to load embedding model on OpenVINO {ov_device}. "
                    "Install with: pip install \"sentence-transformers[openvino]\". "
                    "If that fails, use --device cpu or --device cuda."
                ) from exc
        else:
            self.model = SentenceTransformer(model_name, device=device)

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        if not texts:
            return []
        vectors = self.model.encode(
            list(texts),
            convert_to_numpy=True,
            normalize_embeddings=True,
            show_progress_bar=False,
        )
        return sk_normalize(np.asarray(vectors, dtype=np.float32)).astype(float).tolist()


class DeterministicTestEmbeddingProvider(EmbeddingProvider):
    def __init__(self, dimensions: int = DEFAULT_EMBEDDING_DIMENSIONS):
        self.dimensions = dimensions

    def embed(self, texts: Sequence[str]) -> List[List[float]]:
        vectors: List[List[float]] = []
        for text in texts:
            digest = hashlib.sha256(text.encode("utf-8")).digest()
            values: List[float] = []
            while len(values) < self.dimensions:
                digest = hashlib.sha256(digest).digest()
                values.extend([(byte / 255.0) - 0.5 for byte in digest])
            arr = np.array(values[: self.dimensions], dtype=float)
            norm = np.linalg.norm(arr)
            if norm > 0:
                arr = arr / norm
            vectors.append(arr.tolist())
        return vectors


def resolve_torch_device(device: str) -> str:
    requested = str(device or "auto").strip().lower()

    if requested == "auto":
        if torch is not None and torch.cuda.is_available():
            return "cuda"
        return "cpu"

    if requested in {"npu", "openvino-gpu", "openvino-cpu"}:
        if importlib.util.find_spec("openvino") is None or importlib.util.find_spec("optimum.intel") is None:
            raise RuntimeError(
                f"{requested} was requested but OpenVINO/Optimum Intel is not installed. "
                "Install with: pip install \"sentence-transformers[openvino]\""
            )
        return requested

    if requested.startswith("cuda"):
        if torch is None:
            raise RuntimeError("PyTorch is not installed, so CUDA embeddings cannot be used.")
        if not torch.cuda.is_available():
            raise RuntimeError("CUDA was requested but this environment cannot see a CUDA GPU. Use --device cpu.")
        return requested

    if requested != "cpu":
        raise ValueError("Unsupported --device value. Use auto, cpu, cuda, cuda:N, npu, openvino-gpu, or openvino-cpu.")

    return "cpu"


def print_acceleration_status(device: str) -> None:
    if torch is None:
        print("PyTorch not installed; embedding acceleration unavailable.")
    elif device in {"npu", "openvino-gpu", "openvino-cpu"}:
        ov_device = {"npu": "NPU", "openvino-gpu": "GPU", "openvino-cpu": "CPU"}[device]
        print(f"Embedding device: {ov_device} via OpenVINO")
    elif device.startswith("cuda"):
        device_index = int(device.split(":", 1)[1]) if ":" in device else torch.cuda.current_device()
        print(f"Embedding device: {device} ({torch.cuda.get_device_name(device_index)})")
    else:
        print("Embedding device: cpu")


def build_embedding_provider(args: argparse.Namespace) -> EmbeddingProvider:
    if args.embedding_provider == "sentence-transformers":
        device = resolve_torch_device(args.device)
        print_acceleration_status(device)
        return SentenceTransformerEmbeddingProvider(args.embedding_model, device=device)
    if args.embedding_provider == "deterministic-test":
        return DeterministicTestEmbeddingProvider(args.embedding_dimensions)
    raise ValueError(f"Unsupported embedding provider: {args.embedding_provider}")


def validate_embedding_dimension(vectors: Sequence[Sequence[float]], expected_dim: int) -> None:
    for vec in vectors:
        if len(vec) != expected_dim:
            raise ValueError(f"Embedding dimension mismatch. Expected {expected_dim}, got {len(vec)}.")


# -----------------------------------------------------------------------------
# PostgreSQL helpers
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


def get_app_db_connection():
    conn_str = os.getenv("APP_DB_CONN_STR")
    if conn_str:
        return psycopg2.connect(conn_str)

    required = {
        "APP_DB_HOST": os.getenv("APP_DB_HOST"),
        "APP_DB_USER": os.getenv("APP_DB_USER"),
        "APP_DB_PASS": os.getenv("APP_DB_PASS"),
        "APP_DB_NAME": os.getenv("APP_DB_NAME"),
    }
    missing = [key for key, value in required.items() if not value]
    if missing:
        raise ValueError(f"Missing APP DB env values: {missing}")

    return psycopg2.connect(
        host=os.getenv("APP_DB_HOST"),
        port=int(os.getenv("APP_DB_PORT", "5432")),
        dbname=os.getenv("APP_DB_NAME"),
        user=os.getenv("APP_DB_USER"),
        password=os.getenv("APP_DB_PASS"),
    )


def safe_pg_identifier(name: str) -> str:
    if not re.match(r"^[A-Za-z_][A-Za-z0-9_]*$", name):
        raise ValueError(f"Unsafe SQL identifier: {name}")
    return f'"{name}"'


def safe_pg_qualified_name(name: str) -> str:
    parts = name.split(".")
    if not parts or any(not part for part in parts):
        raise ValueError(f"Unsafe SQL table name: {name}")
    return ".".join(safe_pg_identifier(part) for part in parts)


def pg_object_name(prefix: str, table_name: str, suffix: str) -> str:
    cleaned = re.sub(r"[^A-Za-z0-9_]+", "_", table_name).strip("_")
    name = f"{prefix}_{cleaned}_{suffix}"
    return name[:60]


def ensure_output_schema(
    conn,
    *,
    output_table: str,
    run_table: str,
    create_iris_views: bool,
) -> None:
    output_t = safe_pg_qualified_name(output_table)
    run_t = safe_pg_qualified_name(run_table)

    with conn.cursor() as cur:
        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {run_t} (
                mapper_run_id TEXT PRIMARY KEY,
                source_db TEXT,
                source_table TEXT,
                input_row_count INTEGER,
                exploded_label_count INTEGER,
                result_count INTEGER,
                mapper_window_start TIMESTAMPTZ,
                mapper_window_end TIMESTAMPTZ,
                fields JSONB,
                status_counts JSONB,
                config JSONB,
                started_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                finished_at TIMESTAMPTZ,
                status TEXT,
                error_message TEXT
            );
            """
        )

        cur.execute(
            f"""
            CREATE TABLE IF NOT EXISTS {output_t} (
                id BIGSERIAL PRIMARY KEY,
                dedupe_key TEXT NOT NULL,
                mapper_run_id TEXT NOT NULL,
                source_db TEXT,
                source_table TEXT,
                source_record_id TEXT NOT NULL,
                source_created_at TIMESTAMPTZ,
                classified_at TIMESTAMPTZ,
                source_metadata JSONB,
                mapper_window_start TIMESTAMPTZ,
                mapper_window_end TIMESTAMPTZ,
                field_name TEXT NOT NULL,
                raw_label TEXT NOT NULL,
                normalized_label TEXT NOT NULL,
                embedding_text TEXT,
                label_embedding JSONB,
                mapped_cluster_id TEXT,
                mapped_cluster_name TEXT,
                mapped_display_name TEXT,
                similarity_score NUMERIC,
                existing_cluster_threshold NUMERIC,
                new_cluster_candidate_threshold NUMERIC,
                mapping_status TEXT NOT NULL,
                mapping_method TEXT,
                cluster_version TEXT,
                top_candidates JSONB,
                embedding_model TEXT,
                text_mode TEXT,
                created_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP,
                updated_at TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP
            );
            """
        )

        columns = {
            "dedupe_key": "TEXT",
            "source_db": "TEXT",
            "source_table": "TEXT",
            "source_created_at": "TIMESTAMPTZ",
            "classified_at": "TIMESTAMPTZ",
            "source_metadata": "JSONB",
            "mapper_window_start": "TIMESTAMPTZ",
            "mapper_window_end": "TIMESTAMPTZ",
            "embedding_text": "TEXT",
            "mapped_display_name": "TEXT",
            "existing_cluster_threshold": "NUMERIC",
            "new_cluster_candidate_threshold": "NUMERIC",
            "mapping_method": "TEXT",
            "top_candidates": "JSONB",
            "embedding_model": "TEXT",
            "text_mode": "TEXT",
            "updated_at": "TIMESTAMPTZ DEFAULT CURRENT_TIMESTAMP",
        }
        for column, definition in columns.items():
            cur.execute(f"ALTER TABLE {output_t} ADD COLUMN IF NOT EXISTS {safe_pg_identifier(column)} {definition};")

        cur.execute(f"ALTER TABLE {output_t} ALTER COLUMN label_embedding DROP NOT NULL;")
        cur.execute(
            f"UPDATE {output_t} SET dedupe_key = md5(COALESCE(source_db, '') || '|' || COALESCE(source_table, '') || '|' || source_record_id || '|' || field_name || '|' || normalized_label || '|' || COALESCE(cluster_version, '')) WHERE dedupe_key IS NULL;"
        )
        cur.execute(f"ALTER TABLE {output_t} ALTER COLUMN dedupe_key SET NOT NULL;")

        cur.execute(
            f"CREATE UNIQUE INDEX IF NOT EXISTS {safe_pg_identifier(pg_object_name('ux', output_table, 'dedupe'))} ON {output_t}(dedupe_key);"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {safe_pg_identifier(pg_object_name('idx', output_table, 'record_field'))} ON {output_t}(source_record_id, field_name);"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {safe_pg_identifier(pg_object_name('idx', output_table, 'status'))} ON {output_t}(mapping_status);"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {safe_pg_identifier(pg_object_name('idx', output_table, 'run'))} ON {output_t}(mapper_run_id);"
        )
        cur.execute(
            f"CREATE INDEX IF NOT EXISTS {safe_pg_identifier(pg_object_name('idx', output_table, 'field_seen'))} ON {output_t}(field_name, source_created_at);"
        )

        if create_iris_views:
            cur.execute(
                f"""
                CREATE OR REPLACE VIEW iris_taxonomy_canonical_feed AS
                SELECT
                    source_db,
                    source_table,
                    source_record_id,
                    source_created_at,
                    classified_at,
                    source_metadata,
                    field_name,
                    raw_label,
                    normalized_label,
                    mapped_cluster_id,
                    mapped_display_name,
                    similarity_score,
                    cluster_version,
                    mapping_method,
                    mapper_run_id,
                    created_at,
                    updated_at
                FROM {output_t}
                WHERE mapping_status IN ('EXISTING_CLUSTER', 'KNOWN_TRUE_ANOMALY');
                """
            )
            cur.execute(
                f"""
                CREATE OR REPLACE VIEW iris_taxonomy_emerging_feed AS
                SELECT
                    source_db,
                    source_table,
                    source_record_id,
                    source_created_at,
                    classified_at,
                    source_metadata,
                    field_name,
                    raw_label,
                    normalized_label,
                    similarity_score,
                    existing_cluster_threshold,
                    new_cluster_candidate_threshold,
                    mapping_status,
                    top_candidates,
                    cluster_version,
                    mapper_run_id,
                    created_at,
                    updated_at
                FROM {output_t}
                WHERE mapping_status IN ('NEW_CLUSTER_CANDIDATE', 'TRUE_ANOMALY');
                """
            )
            cur.execute(
                f"""
                CREATE OR REPLACE VIEW iris_taxonomy_config_issue_feed AS
                SELECT
                    source_db,
                    source_table,
                    source_record_id,
                    source_created_at,
                    classified_at,
                    source_metadata,
                    field_name,
                    raw_label,
                    normalized_label,
                    mapping_status,
                    mapper_run_id,
                    created_at,
                    updated_at
                FROM {output_t}
                WHERE mapping_status = 'NO_CLUSTER_REFERENCE';
                """
            )

    conn.commit()


def read_input_from_postgres(
    conn,
    *,
    input_table: str,
    call_id_column: str,
    fields: Sequence[str],
    timestamp_column: Optional[str],
    classified_at_column: Optional[str],
    metadata_columns: Sequence[str],
    window_start: Optional[datetime],
    window_end: Optional[datetime],
    where_clause: Optional[str],
    limit: Optional[int],
) -> pd.DataFrame:
    selected_columns: List[str] = []
    for col in [call_id_column, timestamp_column, classified_at_column, *metadata_columns, *fields]:
        if col and col not in selected_columns:
            selected_columns.append(col)

    column_sql = ", ".join([safe_pg_identifier(col) for col in selected_columns])
    table_sql = safe_pg_qualified_name(input_table)

    clauses: List[str] = []
    params: List[Any] = []
    if timestamp_column and window_start and window_end:
        ts_col = safe_pg_identifier(timestamp_column)
        clauses.append(f"{ts_col} >= %s")
        params.append(window_start)
        clauses.append(f"{ts_col} < %s")
        params.append(window_end)
    if where_clause:
        clauses.append(f"({where_clause})")

    sql = f"SELECT {column_sql} FROM {table_sql}"
    if clauses:
        sql += " WHERE " + " AND ".join(clauses)
    if timestamp_column:
        sql += f" ORDER BY {safe_pg_identifier(timestamp_column)} ASC"
    if limit:
        sql += f" LIMIT {int(limit)}"

    print(f"Reading input rows from table: {input_table}")
    print(f"Input SQL: {sql}")
    if params:
        print(f"Input SQL params: {[p.isoformat() if isinstance(p, datetime) else p for p in params]}")
    return pd.read_sql_query(sql, conn, params=params)


def parse_embedding(value: Any) -> np.ndarray:
    if value is None:
        raise ValueError("centroid_embedding is null")
    if isinstance(value, np.ndarray):
        arr = value.astype(float)
    elif isinstance(value, list):
        arr = np.array(value, dtype=float)
    elif isinstance(value, str):
        arr = np.array(json.loads(value), dtype=float)
    else:
        arr = np.array(value, dtype=float)
    if arr.ndim != 1:
        raise ValueError("centroid_embedding must be a 1D vector")
    norm = np.linalg.norm(arr)
    if norm > 0:
        arr = arr / norm
    return arr.astype(np.float32)


def load_active_clusters(
    conn,
    *,
    cluster_table: str,
    fields: Sequence[str],
    default_existing_threshold: float,
    cluster_version: Optional[str],
    include_anomaly_clusters: bool = False,
) -> Dict[str, List[ClusterReference]]:
    cluster_t = safe_pg_qualified_name(cluster_table)
    anomaly_filter = "TRUE" if include_anomaly_clusters else "FALSE"

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        params: List[Any] = [list(fields)]
        version_filter = ""
        if cluster_version:
            version_filter = "AND COALESCE(c.cluster_version, 'v1') = %s"
            params.append(cluster_version)

        cur.execute(
            f"""
            SELECT
                c.field_name,
                c.cluster_id,
                COALESCE(NULLIF(n.display_name, ''), NULLIF(c.display_name, ''), c.cluster_id) AS cluster_name,
                COALESCE(NULLIF(n.display_name, ''), NULLIF(c.display_name, ''), c.cluster_id) AS display_name,
                c.centroid_embedding,
                COALESCE(c.cluster_version, 'v1') AS cluster_version,
                c.similarity_threshold,
                c.cluster_size,
                c.total_occurrences,
                c.medoid_label,
                COALESCE(c.is_true_anomaly_cluster, FALSE) AS is_true_anomaly_cluster
            FROM {cluster_t} c
            LEFT JOIN taxonomy_cluster_names n
                ON c.field_name = n.field_name
                AND c.cluster_id = n.cluster_id
                AND (n.run_id = c.run_id OR n.run_id IS NULL OR c.run_id IS NULL)
                AND (n.cluster_version = c.cluster_version OR n.cluster_version IS NULL OR c.cluster_version IS NULL)
            WHERE COALESCE(c.active, TRUE) = TRUE
              AND COALESCE(c.is_true_anomaly_cluster, FALSE) = {anomaly_filter}
              AND c.field_name = ANY(%s)
              AND c.centroid_embedding IS NOT NULL
              {version_filter}
            ORDER BY c.field_name, c.cluster_id;
            """,
            params,
        )
        rows = cur.fetchall()

    by_field: Dict[str, List[ClusterReference]] = {field: [] for field in fields}
    for row in rows:
        try:
            centroid = parse_embedding(row["centroid_embedding"])
        except Exception as exc:
            print(
                f"Skipping invalid centroid field={row.get('field_name')} cluster_id={row.get('cluster_id')}: {exc}",
                file=sys.stderr,
            )
            continue

        threshold = row.get("similarity_threshold")
        if threshold is None:
            threshold = default_existing_threshold

        ref = ClusterReference(
            field_name=row["field_name"],
            cluster_id=str(row["cluster_id"]),
            cluster_name=row.get("cluster_name"),
            display_name=row.get("display_name"),
            centroid_embedding=centroid,
            cluster_version=row.get("cluster_version") or "v1",
            similarity_threshold=float(threshold),
            cluster_size=row.get("cluster_size"),
            total_occurrences=row.get("total_occurrences"),
            medoid_label=row.get("medoid_label"),
            is_true_anomaly_cluster=bool(row.get("is_true_anomaly_cluster")),
        )
        by_field.setdefault(ref.field_name, []).append(ref)

    return by_field


def load_exact_label_map(
    conn,
    *,
    label_map_table: str,
    fields: Sequence[str],
    cluster_version: Optional[str],
    include_anomaly_labels: bool = False,
) -> Dict[Tuple[str, str], ExactLabelReference]:
    label_map_t = safe_pg_qualified_name(label_map_table)

    params: List[Any] = [list(fields)]
    version_filter = ""
    if cluster_version:
        version_filter = "AND COALESCE(m.cluster_version, 'v1') = %s"
        params.append(cluster_version)

    # The current cluster row is the source of truth. A label-map row can be stale
    # after a true anomaly is promoted to a standard cluster, so do not let
    # m.final_is_true_anomaly override c.is_true_anomaly_cluster.
    if include_anomaly_labels:
        anomaly_filter = "AND COALESCE(c.is_true_anomaly_cluster, FALSE) = TRUE"
    else:
        anomaly_filter = "AND COALESCE(c.is_true_anomaly_cluster, FALSE) = FALSE"

    sql = f"""
        SELECT
            m.field_name,
            m.normalized_label,
            m.raw_label,
            m.final_cluster_id,
            COALESCE(m.cluster_version, c.cluster_version, 'v1') AS cluster_version,
            COALESCE(NULLIF(n.display_name, ''), NULLIF(c.display_name, ''), c.cluster_id) AS display_name,
            COALESCE(NULLIF(n.display_name, ''), NULLIF(c.display_name, ''), c.cluster_id) AS cluster_name,
            c.cluster_size,
            c.total_occurrences,
            COALESCE(c.is_true_anomaly_cluster, FALSE) AS is_true_anomaly_cluster,
            COALESCE(m.value_count, 0) AS value_count
        FROM {label_map_t} m
        JOIN taxonomy_clusters c
          ON c.field_name = m.field_name
         AND c.cluster_id = m.final_cluster_id
         AND COALESCE(c.cluster_version, 'v1') = COALESCE(m.cluster_version, 'v1')
         AND COALESCE(c.active, TRUE) = TRUE
        LEFT JOIN taxonomy_cluster_names n
          ON n.field_name = c.field_name
         AND n.cluster_id = c.cluster_id
         AND (n.run_id = c.run_id OR n.run_id IS NULL OR c.run_id IS NULL)
         AND (n.cluster_version = c.cluster_version OR n.cluster_version IS NULL OR c.cluster_version IS NULL)
        WHERE m.field_name = ANY(%s)
          AND m.normalized_label IS NOT NULL
          AND m.normalized_label <> ''
          AND m.final_cluster_id IS NOT NULL
          {anomaly_filter}
          {version_filter}
        ORDER BY m.field_name, m.normalized_label, COALESCE(m.value_count, 0) DESC;
    """

    exact: Dict[Tuple[str, str], ExactLabelReference] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(sql, params)
        for row in cur.fetchall():
            field_name = str(row["field_name"])
            normalized_label = normalize_label(row["normalized_label"])
            key = (field_name, normalized_label)
            if key in exact:
                continue
            exact[key] = ExactLabelReference(
                field_name=field_name,
                normalized_label=normalized_label,
                raw_label=str(row.get("raw_label") or ""),
                cluster_id=str(row["final_cluster_id"]),
                display_name=row.get("display_name"),
                cluster_name=row.get("cluster_name"),
                cluster_version=row.get("cluster_version") or "v1",
                cluster_size=row.get("cluster_size"),
                total_occurrences=row.get("total_occurrences"),
                is_true_anomaly_cluster=bool(row.get("is_true_anomaly_cluster")),
            )

    return exact


# -----------------------------------------------------------------------------
# Mapping
# -----------------------------------------------------------------------------


def cosine_similarity_matrix(query: np.ndarray, candidates: np.ndarray) -> np.ndarray:
    query = np.asarray(query, dtype=np.float32)
    q_norm = np.linalg.norm(query)
    if q_norm > 0:
        query = query / q_norm
    candidates = np.asarray(candidates, dtype=np.float32)
    c_norms = np.linalg.norm(candidates, axis=1, keepdims=True)
    c_norms[c_norms == 0] = 1.0
    return (candidates / c_norms) @ query


def parse_field_threshold_overrides(value: Optional[str]) -> Dict[str, float]:
    overrides: Dict[str, float] = {}
    if not value:
        return overrides

    for part in str(value).split(","):
        part = part.strip()
        if not part:
            continue
        if "=" not in part:
            raise ValueError(
                f"Invalid --field-existing-thresholds entry: {part}. Expected format field=value."
            )
        field_name, threshold_text = part.split("=", 1)
        field_name = field_name.strip()
        threshold = float(threshold_text.strip())
        if not field_name:
            raise ValueError(f"Invalid empty field name in --field-existing-thresholds: {part}")
        if threshold < 0 or threshold > 1:
            raise ValueError(f"Threshold must be between 0 and 1 for {field_name}: {threshold}")
        overrides[field_name] = threshold

    return overrides


def effective_existing_threshold(
    *,
    field_name: str,
    cluster_threshold: float,
    field_threshold_overrides: Dict[str, float],
) -> float:
    return float(field_threshold_overrides.get(field_name, cluster_threshold))


def explode_call_rows(
    df: pd.DataFrame,
    *,
    source_db: str,
    source_table: str,
    call_id_column: str,
    fields: Sequence[str],
    timestamp_column: Optional[str],
    classified_at_column: Optional[str],
    metadata_columns: Sequence[str],
) -> List[ExplodedLabel]:
    records: List[ExplodedLabel] = []
    seen = set()

    for row_index, row in df.iterrows():
        source_record_id = row.get(call_id_column)
        if source_record_id is None or str(source_record_id).strip() == "" or str(source_record_id).lower() == "nan":
            source_record_id = stable_record_id(int(row_index), row)
        else:
            source_record_id = str(source_record_id)

        source_created_at = to_utc_datetime(row.get(timestamp_column)) if timestamp_column and timestamp_column in df.columns else None
        classified_at = to_utc_datetime(row.get(classified_at_column)) if classified_at_column and classified_at_column in df.columns else None
        if classified_at is None:
            classified_at = source_created_at
        source_metadata = row_metadata(row, metadata_columns)

        for field_name in fields:
            if field_name not in df.columns:
                continue
            raw_values = split_labels(row.get(field_name))
            for raw_label in raw_values:
                normalized_label = normalize_label(raw_label)
                if not normalized_label:
                    continue

                dedupe = (source_table, source_record_id, field_name, normalized_label)
                if dedupe in seen:
                    continue
                seen.add(dedupe)

                records.append(
                    ExplodedLabel(
                        source_db=source_db,
                        source_table=source_table,
                        source_record_id=source_record_id,
                        source_created_at=source_created_at,
                        classified_at=classified_at,
                        source_metadata=source_metadata,
                        field_name=field_name,
                        raw_label=raw_label,
                        normalized_label=normalized_label,
                    )
                )

    return records


def build_result(
    *,
    item: ExplodedLabel,
    mapper_run_id: str,
    mapper_window_start: Optional[datetime],
    mapper_window_end: Optional[datetime],
    embedding_text_value: Optional[str],
    label_embedding: Optional[List[float]],
    mapped_cluster_id: Optional[str],
    mapped_cluster_name: Optional[str],
    mapped_display_name: Optional[str],
    similarity_score: Optional[float],
    existing_cluster_threshold: Optional[float],
    new_cluster_candidate_threshold: float,
    mapping_status: str,
    mapping_method: str,
    cluster_version: Optional[str],
    top_candidates: List[Dict[str, Any]],
    embedding_model: str,
    text_mode: str,
) -> MappingResult:
    created_at = utc_now()
    dedupe_key = stable_dedupe_key(
        source_db=item.source_db,
        source_table=item.source_table,
        source_record_id=item.source_record_id,
        field_name=item.field_name,
        normalized_label=item.normalized_label,
        cluster_version=cluster_version,
    )
    return MappingResult(
        dedupe_key=dedupe_key,
        mapper_run_id=mapper_run_id,
        source_db=item.source_db,
        source_table=item.source_table,
        source_record_id=item.source_record_id,
        source_created_at=item.source_created_at,
        classified_at=item.classified_at,
        source_metadata=item.source_metadata,
        mapper_window_start=mapper_window_start,
        mapper_window_end=mapper_window_end,
        field_name=item.field_name,
        raw_label=item.raw_label,
        normalized_label=item.normalized_label,
        embedding_text=embedding_text_value,
        label_embedding=label_embedding,
        mapped_cluster_id=mapped_cluster_id,
        mapped_cluster_name=mapped_cluster_name,
        mapped_display_name=mapped_display_name,
        similarity_score=similarity_score,
        existing_cluster_threshold=existing_cluster_threshold,
        new_cluster_candidate_threshold=new_cluster_candidate_threshold,
        mapping_status=mapping_status,
        mapping_method=mapping_method,
        cluster_version=cluster_version,
        top_candidates=top_candidates,
        embedding_model=embedding_model,
        text_mode=text_mode,
        created_at=created_at,
    )


def result_from_exact_match(
    *,
    item: ExplodedLabel,
    exact_ref: ExactLabelReference,
    mapper_run_id: str,
    mapper_window_start: Optional[datetime],
    mapper_window_end: Optional[datetime],
    new_cluster_candidate_threshold: float,
    embedding_model: str,
    text_mode: str,
) -> MappingResult:
    is_anomaly = bool(exact_ref.is_true_anomaly_cluster)
    mapping_status = KNOWN_ANOMALY_STATUS if is_anomaly else STABLE_MAPPING_STATUS
    mapping_method = "exact_anomaly_label_map" if is_anomaly else "exact_label_map"
    top_candidates = [
        {
            "cluster_id": exact_ref.cluster_id,
            "cluster_name": exact_ref.cluster_name,
            "display_name": exact_ref.display_name,
            "similarity_score": 1.0,
            "cluster_version": exact_ref.cluster_version,
            "cluster_size": exact_ref.cluster_size,
            "total_occurrences": exact_ref.total_occurrences,
            "source": "taxonomy_label_cluster_map",
            "is_true_anomaly_cluster": is_anomaly,
        }
    ]
    return build_result(
        item=item,
        mapper_run_id=mapper_run_id,
        mapper_window_start=mapper_window_start,
        mapper_window_end=mapper_window_end,
        embedding_text_value=None,
        label_embedding=None,
        mapped_cluster_id=exact_ref.cluster_id,
        mapped_cluster_name=exact_ref.cluster_name,
        mapped_display_name=exact_ref.display_name,
        similarity_score=1.0,
        existing_cluster_threshold=1.0,
        new_cluster_candidate_threshold=new_cluster_candidate_threshold,
        mapping_status=mapping_status,
        mapping_method=mapping_method,
        cluster_version=exact_ref.cluster_version,
        top_candidates=top_candidates,
        embedding_model=embedding_model,
        text_mode=text_mode,
    )


def result_no_cluster_reference(
    *,
    item: ExplodedLabel,
    mapper_run_id: str,
    mapper_window_start: Optional[datetime],
    mapper_window_end: Optional[datetime],
    new_cluster_candidate_threshold: float,
    embedding_model: str,
    text_mode: str,
) -> MappingResult:
    return build_result(
        item=item,
        mapper_run_id=mapper_run_id,
        mapper_window_start=mapper_window_start,
        mapper_window_end=mapper_window_end,
        embedding_text_value=None,
        label_embedding=None,
        mapped_cluster_id=None,
        mapped_cluster_name=None,
        mapped_display_name=None,
        similarity_score=None,
        existing_cluster_threshold=None,
        new_cluster_candidate_threshold=new_cluster_candidate_threshold,
        mapping_status=CONFIG_ISSUE_STATUS,
        mapping_method="no_active_cluster_reference",
        cluster_version=None,
        top_candidates=[],
        embedding_model=embedding_model,
        text_mode=text_mode,
    )


def _rank_cluster_candidates(
    *,
    item: ExplodedLabel,
    label_embedding: List[float],
    clusters: Sequence[ClusterReference],
    top_k: int,
    field_existing_thresholds: Dict[str, float],
    source: str,
) -> tuple[list[dict[str, Any]], Optional[ClusterReference], Optional[float], Optional[float]]:
    if not clusters:
        return [], None, None, None

    query = np.array(label_embedding, dtype=np.float32)
    centroids = np.vstack([cluster.centroid_embedding for cluster in clusters])
    scores = cosine_similarity_matrix(query, centroids)
    order = np.argsort(scores)[::-1]

    label_text = " ".join([item.raw_label, item.normalized_label])
    top_candidates: List[Dict[str, Any]] = []
    best_allowed_cluster: Optional[ClusterReference] = None
    best_allowed_score: Optional[float] = None
    best_allowed_threshold: Optional[float] = None

    for rank, idx in enumerate(order):
        cluster = clusters[int(idx)]
        threshold = effective_existing_threshold(
            field_name=item.field_name,
            cluster_threshold=cluster.similarity_threshold,
            field_threshold_overrides=field_existing_thresholds,
        )
        target_text = " ".join(
            str(value or "")
            for value in [cluster.display_name, cluster.cluster_name, cluster.medoid_label]
        )
        actor_check = actor_directionality_check(label_text, target_text)
        actor_conflict = bool(actor_check["actor_direction_conflict"])

        if rank < top_k:
            top_candidates.append(
                {
                    "cluster_id": cluster.cluster_id,
                    "cluster_name": cluster.cluster_name,
                    "display_name": cluster.display_name,
                    "similarity_score": float(scores[int(idx)]),
                    "cluster_version": cluster.cluster_version,
                    "cluster_size": cluster.cluster_size,
                    "total_occurrences": cluster.total_occurrences,
                    "medoid_label": cluster.medoid_label,
                    "similarity_threshold": threshold,
                    "is_true_anomaly_cluster": cluster.is_true_anomaly_cluster,
                    "source": source,
                    "actor_direction_conflict": actor_conflict,
                    "actor_directionality_check": actor_check,
                }
            )

        if actor_conflict:
            continue

        if best_allowed_cluster is None:
            best_allowed_cluster = cluster
            best_allowed_score = float(scores[int(idx)])
            best_allowed_threshold = threshold

    return top_candidates, best_allowed_cluster, best_allowed_score, best_allowed_threshold


def map_embedded_label_to_cluster(
    *,
    item: ExplodedLabel,
    label_embedding: List[float],
    mapper_run_id: str,
    mapper_window_start: Optional[datetime],
    mapper_window_end: Optional[datetime],
    clusters: Sequence[ClusterReference],
    anomaly_clusters: Sequence[ClusterReference],
    anomaly_cluster_threshold: float,
    new_cluster_candidate_threshold: float,
    top_k: int,
    field_existing_thresholds: Dict[str, float],
    embedding_model: str,
    text_mode: str,
) -> MappingResult:
    if not clusters and not anomaly_clusters:
        return result_no_cluster_reference(
            item=item,
            mapper_run_id=mapper_run_id,
            mapper_window_start=mapper_window_start,
            mapper_window_end=mapper_window_end,
            new_cluster_candidate_threshold=new_cluster_candidate_threshold,
            embedding_model=embedding_model,
            text_mode=text_mode,
        )

    standard_candidates, best_cluster, best_score, best_threshold = _rank_cluster_candidates(
        item=item,
        label_embedding=label_embedding,
        clusters=clusters,
        top_k=top_k,
        field_existing_thresholds=field_existing_thresholds,
        source="standard_cluster",
    )

    if best_cluster is not None and best_score is not None and best_threshold is not None and best_score >= best_threshold:
        return build_result(
            item=item,
            mapper_run_id=mapper_run_id,
            mapper_window_start=mapper_window_start,
            mapper_window_end=mapper_window_end,
            embedding_text_value=embedding_text(item.field_name, item.raw_label, mode=text_mode),
            label_embedding=label_embedding,
            mapped_cluster_id=best_cluster.cluster_id,
            mapped_cluster_name=best_cluster.cluster_name,
            mapped_display_name=best_cluster.display_name,
            similarity_score=best_score,
            existing_cluster_threshold=best_threshold,
            new_cluster_candidate_threshold=new_cluster_candidate_threshold,
            mapping_status=STABLE_MAPPING_STATUS,
            mapping_method="centroid_similarity",
            cluster_version=best_cluster.cluster_version,
            top_candidates=standard_candidates,
            embedding_model=embedding_model,
            text_mode=text_mode,
        )

    anomaly_candidates, best_anomaly, best_anomaly_score, _best_anomaly_threshold = _rank_cluster_candidates(
        item=item,
        label_embedding=label_embedding,
        clusters=anomaly_clusters,
        top_k=top_k,
        field_existing_thresholds={},
        source="true_anomaly_cluster",
    )

    if best_anomaly is not None and best_anomaly_score is not None and best_anomaly_score >= anomaly_cluster_threshold:
        return build_result(
            item=item,
            mapper_run_id=mapper_run_id,
            mapper_window_start=mapper_window_start,
            mapper_window_end=mapper_window_end,
            embedding_text_value=embedding_text(item.field_name, item.raw_label, mode=text_mode),
            label_embedding=label_embedding,
            mapped_cluster_id=best_anomaly.cluster_id,
            mapped_cluster_name=best_anomaly.cluster_name,
            mapped_display_name=best_anomaly.display_name,
            similarity_score=best_anomaly_score,
            existing_cluster_threshold=anomaly_cluster_threshold,
            new_cluster_candidate_threshold=new_cluster_candidate_threshold,
            mapping_status=KNOWN_ANOMALY_STATUS,
            mapping_method="anomaly_centroid_similarity",
            cluster_version=best_anomaly.cluster_version,
            top_candidates=anomaly_candidates + standard_candidates,
            embedding_model=embedding_model,
            text_mode=text_mode,
        )

    if best_cluster is not None and best_score is not None and best_threshold is not None:
        if best_score >= new_cluster_candidate_threshold:
            status = "NEW_CLUSTER_CANDIDATE"
            method = "near_existing_below_threshold"
        else:
            status = "TRUE_ANOMALY"
            method = "low_similarity_unresolved"
        similarity_score = best_score
        existing_threshold = best_threshold
        cluster_version = best_cluster.cluster_version
    else:
        status = "TRUE_ANOMALY"
        method = "low_similarity_unresolved"
        similarity_score = best_anomaly_score
        existing_threshold = anomaly_cluster_threshold if best_anomaly_score is not None else None
        cluster_version = best_anomaly.cluster_version if best_anomaly is not None else None

    return build_result(
        item=item,
        mapper_run_id=mapper_run_id,
        mapper_window_start=mapper_window_start,
        mapper_window_end=mapper_window_end,
        embedding_text_value=embedding_text(item.field_name, item.raw_label, mode=text_mode),
        label_embedding=label_embedding,
        mapped_cluster_id=None,
        mapped_cluster_name=None,
        mapped_display_name=None,
        similarity_score=similarity_score,
        existing_cluster_threshold=existing_threshold,
        new_cluster_candidate_threshold=new_cluster_candidate_threshold,
        mapping_status=status,
        mapping_method=method,
        cluster_version=cluster_version,
        top_candidates=standard_candidates + anomaly_candidates,
        embedding_model=embedding_model,
        text_mode=text_mode,
    )


def run_mapping(
    *,
    df: pd.DataFrame,
    source_db: str,
    source_table: str,
    call_id_column: str,
    fields: Sequence[str],
    timestamp_column: Optional[str],
    classified_at_column: Optional[str],
    metadata_columns: Sequence[str],
    exact_label_map: Dict[Tuple[str, str], ExactLabelReference],
    exact_anomaly_label_map: Dict[Tuple[str, str], ExactLabelReference],
    embedding_provider: EmbeddingProvider,
    clusters_by_field: Dict[str, List[ClusterReference]],
    anomaly_clusters_by_field: Dict[str, List[ClusterReference]],
    mapper_run_id: str,
    mapper_window_start: Optional[datetime],
    mapper_window_end: Optional[datetime],
    new_cluster_candidate_threshold: float,
    anomaly_cluster_threshold: float,
    top_k: int,
    embedding_batch_size: int,
    text_mode: str,
    embedding_model: str,
    expected_embedding_dimensions: int,
    field_existing_thresholds: Dict[str, float],
) -> Tuple[List[MappingResult], int]:
    exploded = explode_call_rows(
        df,
        source_db=source_db,
        source_table=source_table,
        call_id_column=call_id_column,
        fields=fields,
        timestamp_column=timestamp_column,
        classified_at_column=classified_at_column,
        metadata_columns=metadata_columns,
    )

    if not exploded:
        return [], 0

    print(f"Exploded call-field labels: {len(exploded):,}")
    results: List[MappingResult] = []
    embedding_items: List[ExplodedLabel] = []
    embedding_texts: List[str] = []

    for item in exploded:
        exact_ref = exact_label_map.get((item.field_name, item.normalized_label))
        if exact_ref is None:
            exact_ref = exact_anomaly_label_map.get((item.field_name, item.normalized_label))
        if exact_ref is not None:
            results.append(
                result_from_exact_match(
                    item=item,
                    exact_ref=exact_ref,
                    mapper_run_id=mapper_run_id,
                    mapper_window_start=mapper_window_start,
                    mapper_window_end=mapper_window_end,
                    new_cluster_candidate_threshold=new_cluster_candidate_threshold,
                    embedding_model=embedding_model,
                    text_mode=text_mode,
                )
            )
            continue

        clusters = clusters_by_field.get(item.field_name, [])
        anomaly_clusters = anomaly_clusters_by_field.get(item.field_name, [])
        if not clusters and not anomaly_clusters:
            results.append(
                result_no_cluster_reference(
                    item=item,
                    mapper_run_id=mapper_run_id,
                    mapper_window_start=mapper_window_start,
                    mapper_window_end=mapper_window_end,
                    new_cluster_candidate_threshold=new_cluster_candidate_threshold,
                    embedding_model=embedding_model,
                    text_mode=text_mode,
                )
            )
            continue

        embedding_items.append(item)
        embedding_texts.append(embedding_text(item.field_name, item.raw_label, mode=text_mode))

    print(f"Exact known standard-label matches: {sum(1 for r in results if r.mapping_method == 'exact_label_map'):,}")
    print(f"Exact known anomaly-label matches: {sum(1 for r in results if r.mapping_method == 'exact_anomaly_label_map'):,}")
    print(f"Labels requiring embeddings: {len(embedding_items):,}")
    print(f"Embedding text mode: {text_mode}")

    embedding_lookup: Dict[str, List[float]] = {}
    unique_texts = sorted(set(embedding_texts))
    print(f"Unique embedding texts to embed/map: {len(unique_texts):,}")

    for start in range(0, len(unique_texts), embedding_batch_size):
        batch_texts = unique_texts[start : start + embedding_batch_size]
        batch_embeddings = embedding_provider.embed(batch_texts)
        validate_embedding_dimension(batch_embeddings, expected_embedding_dimensions)
        for text, vector in zip(batch_texts, batch_embeddings):
            embedding_lookup[text] = vector

    for item, text in zip(embedding_items, embedding_texts):
        results.append(
            map_embedded_label_to_cluster(
                item=item,
                label_embedding=embedding_lookup[text],
                mapper_run_id=mapper_run_id,
                mapper_window_start=mapper_window_start,
                mapper_window_end=mapper_window_end,
                clusters=clusters_by_field.get(item.field_name, []),
                anomaly_clusters=anomaly_clusters_by_field.get(item.field_name, []),
                anomaly_cluster_threshold=anomaly_cluster_threshold,
                new_cluster_candidate_threshold=new_cluster_candidate_threshold,
                top_k=top_k,
                field_existing_thresholds=field_existing_thresholds,
                embedding_model=embedding_model,
                text_mode=text_mode,
            )
        )

    return results, len(exploded)


# -----------------------------------------------------------------------------
# Output helpers
# -----------------------------------------------------------------------------


def result_rows(results: Sequence[MappingResult]) -> List[Tuple[Any, ...]]:
    rows = []
    for result in results:
        rows.append(
            (
                result.dedupe_key,
                result.mapper_run_id,
                result.source_db,
                result.source_table,
                result.source_record_id,
                result.source_created_at,
                result.classified_at,
                psycopg2.extras.Json(result.source_metadata),
                result.mapper_window_start,
                result.mapper_window_end,
                result.field_name,
                result.raw_label,
                result.normalized_label,
                result.embedding_text,
                psycopg2.extras.Json(result.label_embedding) if result.label_embedding is not None else None,
                result.mapped_cluster_id,
                result.mapped_cluster_name,
                result.mapped_display_name,
                result.similarity_score,
                result.existing_cluster_threshold,
                result.new_cluster_candidate_threshold,
                result.mapping_status,
                result.mapping_method,
                result.cluster_version,
                psycopg2.extras.Json(result.top_candidates),
                result.embedding_model,
                result.text_mode,
                result.created_at,
            )
        )
    return rows


def upsert_mapping_results(conn, *, output_table: str, results: Sequence[MappingResult], batch_size: int = 1000) -> int:
    if not results:
        return 0

    output_t = safe_pg_qualified_name(output_table)
    rows = result_rows(results)

    sql = f"""
        INSERT INTO {output_t} (
            dedupe_key,
            mapper_run_id,
            source_db,
            source_table,
            source_record_id,
            source_created_at,
            classified_at,
            source_metadata,
            mapper_window_start,
            mapper_window_end,
            field_name,
            raw_label,
            normalized_label,
            embedding_text,
            label_embedding,
            mapped_cluster_id,
            mapped_cluster_name,
            mapped_display_name,
            similarity_score,
            existing_cluster_threshold,
            new_cluster_candidate_threshold,
            mapping_status,
            mapping_method,
            cluster_version,
            top_candidates,
            embedding_model,
            text_mode,
            created_at
        ) VALUES %s
        ON CONFLICT (dedupe_key) DO UPDATE SET
            mapper_run_id = EXCLUDED.mapper_run_id,
            source_db = EXCLUDED.source_db,
            source_table = EXCLUDED.source_table,
            source_record_id = EXCLUDED.source_record_id,
            source_created_at = EXCLUDED.source_created_at,
            classified_at = EXCLUDED.classified_at,
            source_metadata = EXCLUDED.source_metadata,
            mapper_window_start = EXCLUDED.mapper_window_start,
            mapper_window_end = EXCLUDED.mapper_window_end,
            raw_label = EXCLUDED.raw_label,
            embedding_text = EXCLUDED.embedding_text,
            label_embedding = EXCLUDED.label_embedding,
            mapped_cluster_id = EXCLUDED.mapped_cluster_id,
            mapped_cluster_name = EXCLUDED.mapped_cluster_name,
            mapped_display_name = EXCLUDED.mapped_display_name,
            similarity_score = EXCLUDED.similarity_score,
            existing_cluster_threshold = EXCLUDED.existing_cluster_threshold,
            new_cluster_candidate_threshold = EXCLUDED.new_cluster_candidate_threshold,
            mapping_status = EXCLUDED.mapping_status,
            mapping_method = EXCLUDED.mapping_method,
            cluster_version = EXCLUDED.cluster_version,
            top_candidates = EXCLUDED.top_candidates,
            embedding_model = EXCLUDED.embedding_model,
            text_mode = EXCLUDED.text_mode,
            updated_at = CURRENT_TIMESTAMP;
    """

    with conn.cursor() as cur:
        for start in range(0, len(rows), batch_size):
            batch = rows[start : start + batch_size]
            psycopg2.extras.execute_values(cur, sql, batch, page_size=batch_size)
    conn.commit()
    return len(rows)


def summarize_results(results: Sequence[MappingResult]) -> pd.DataFrame:
    if not results:
        return pd.DataFrame(columns=["field_name", "mapping_status", "mapping_method", "count"])
    df = pd.DataFrame(
        [
            {
                "field_name": result.field_name,
                "mapping_status": result.mapping_status,
                "mapping_method": result.mapping_method,
                "mapped_display_name": result.mapped_display_name,
            }
            for result in results
        ]
    )
    return (
        df.groupby(["field_name", "mapping_status", "mapping_method"])
        .size()
        .reset_index(name="count")
        .sort_values(["field_name", "count"], ascending=[True, False])
    )


def status_counts(results: Sequence[MappingResult]) -> Dict[str, Any]:
    out: Dict[str, Dict[str, int]] = {}
    for result in results:
        field_map = out.setdefault(result.field_name, {})
        field_map[result.mapping_status] = field_map.get(result.mapping_status, 0) + 1
    return out


def write_preview_csv(results: Sequence[MappingResult], output_path: str) -> None:
    path = Path(output_path)
    path.parent.mkdir(parents=True, exist_ok=True)
    rows = []
    for result in results:
        rows.append(
            {
                "dedupe_key": result.dedupe_key,
                "mapper_run_id": result.mapper_run_id,
                "source_db": result.source_db,
                "source_table": result.source_table,
                "source_record_id": result.source_record_id,
                "source_created_at": result.source_created_at.isoformat() if result.source_created_at else None,
                "classified_at": result.classified_at.isoformat() if result.classified_at else None,
                "source_metadata": json.dumps(result.source_metadata, ensure_ascii=False, default=json_default),
                "mapper_window_start": result.mapper_window_start.isoformat() if result.mapper_window_start else None,
                "mapper_window_end": result.mapper_window_end.isoformat() if result.mapper_window_end else None,
                "field_name": result.field_name,
                "raw_label": result.raw_label,
                "normalized_label": result.normalized_label,
                "mapped_cluster_id": result.mapped_cluster_id,
                "mapped_cluster_name": result.mapped_cluster_name,
                "mapped_display_name": result.mapped_display_name,
                "similarity_score": result.similarity_score,
                "existing_cluster_threshold": result.existing_cluster_threshold,
                "new_cluster_candidate_threshold": result.new_cluster_candidate_threshold,
                "mapping_status": result.mapping_status,
                "mapping_method": result.mapping_method,
                "cluster_version": result.cluster_version,
                "top_candidates": json.dumps(result.top_candidates, ensure_ascii=False, default=json_default),
                "embedding_model": result.embedding_model,
                "text_mode": result.text_mode,
                "created_at": result.created_at.isoformat(),
            }
        )
    pd.DataFrame(rows).to_csv(path, index=False, encoding="utf-8")


def write_mapper_run(
    conn,
    *,
    run_table: str,
    mapper_run_id: str,
    source_db: str,
    source_table: str,
    input_row_count: int,
    exploded_label_count: int,
    result_count: int,
    mapper_window_start: Optional[datetime],
    mapper_window_end: Optional[datetime],
    fields: Sequence[str],
    counts: Dict[str, Any],
    config: Dict[str, Any],
    started_at: datetime,
    finished_at: datetime,
    status: str,
    error_message: Optional[str] = None,
) -> None:
    run_t = safe_pg_qualified_name(run_table)
    sql = f"""
        INSERT INTO {run_t} (
            mapper_run_id,
            source_db,
            source_table,
            input_row_count,
            exploded_label_count,
            result_count,
            mapper_window_start,
            mapper_window_end,
            fields,
            status_counts,
            config,
            started_at,
            finished_at,
            status,
            error_message
        ) VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s)
        ON CONFLICT (mapper_run_id) DO UPDATE SET
            input_row_count = EXCLUDED.input_row_count,
            exploded_label_count = EXCLUDED.exploded_label_count,
            result_count = EXCLUDED.result_count,
            status_counts = EXCLUDED.status_counts,
            config = EXCLUDED.config,
            finished_at = EXCLUDED.finished_at,
            status = EXCLUDED.status,
            error_message = EXCLUDED.error_message;
    """
    with conn.cursor() as cur:
        cur.execute(
            sql,
            (
                mapper_run_id,
                source_db,
                source_table,
                input_row_count,
                exploded_label_count,
                result_count,
                mapper_window_start,
                mapper_window_end,
                psycopg2.extras.Json(list(fields)),
                psycopg2.extras.Json(counts),
                psycopg2.extras.Json(config),
                started_at,
                finished_at,
                status,
                error_message,
            ),
        )
    conn.commit()


# -----------------------------------------------------------------------------
# CLI
# -----------------------------------------------------------------------------


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(
        description="Hourly production mapper for real post-classification call field labels."
    )

    input_group = parser.add_mutually_exclusive_group(required=False)
    input_group.add_argument("--input-file", help="CSV file containing post-classification call rows.")
    input_group.add_argument("--input-table", help="PostgreSQL table containing post-classification call rows. Defaults to ngp_call_classification when neither input source is provided.")

    parser.add_argument(
        "--input-db",
        choices=["local", "app"],
        default="app",
        help="Database to read --input-table from. Use app for ai-call-analysis-db; local for taxonomy_drift_local.",
    )
    parser.add_argument("--where", help="Optional SQL WHERE clause for --input-table. Do not include WHERE.")
    parser.add_argument("--limit", type=int, help="Optional row limit for PostgreSQL input.")
    parser.add_argument("--call-id-column", default="call_id", help="Column containing the call/source record id.")
    parser.add_argument("--timestamp-column", default="created_at", help="Timestamp column used for the production lookback window. Use empty string to disable.")
    parser.add_argument("--classified-at-column", help="Optional separate classified-at timestamp column to store in output.")
    parser.add_argument("--lookback-hours", type=float, default=1.0, help="How many hours back to read when --timestamp-column is set.")
    parser.add_argument("--window-start-utc", help="Optional explicit UTC window start, e.g. 2026-05-20T08:00:00Z.")
    parser.add_argument("--window-end-utc", help="Optional explicit UTC window end, e.g. 2026-05-20T09:00:00Z. Defaults to now.")
    parser.add_argument("--fields", default=DEFAULT_NGP_FIELDS, help="Comma-separated classified fields to process. Defaults to NGP taxonomy fields.")
    parser.add_argument("--metadata-columns", default=DEFAULT_NGP_METADATA_COLUMNS, help="Comma-separated non-taxonomy columns to store in source_metadata JSONB.")

    parser.add_argument("--env-file", help="Optional .env file path.")
    parser.add_argument("--cluster-table", default=DEFAULT_CLUSTER_TABLE)
    parser.add_argument("--label-map-table", default=DEFAULT_LABEL_MAP_TABLE)
    parser.add_argument("--output-table", default=DEFAULT_OUTPUT_TABLE)
    parser.add_argument("--run-table", default=DEFAULT_RUN_TABLE)
    parser.add_argument("--cluster-version", help="Optional cluster version to use. If omitted, all active versions are eligible.")
    parser.add_argument("--ensure-schema", action=argparse.BooleanOptionalAction, default=True, help="Create/patch mapper output tables and Iris views. Default: true.")
    parser.add_argument("--skip-iris-views", action="store_true", help="Do not create Iris feed views when --ensure-schema is used.")
    parser.add_argument("--write-output", action=argparse.BooleanOptionalAction, default=True, help="Upsert mapping results into local PostgreSQL output table. Default: true.")
    parser.add_argument("--preview-csv", help="Optional path to save mapping results as CSV.")

    parser.add_argument("--disable-exact-label-map", action="store_true", help="Disable fast exact lookup from taxonomy_label_cluster_map.")
    parser.add_argument("--existing-cluster-threshold", type=float, default=DEFAULT_EXISTING_CLUSTER_THRESHOLD)
    parser.add_argument(
        "--field-existing-thresholds",
        help="Optional comma-separated per-field existing-cluster thresholds, e.g. next_step=0.75,coaching_tags=0.82",
    )
    parser.add_argument("--new-cluster-candidate-threshold", type=float, default=DEFAULT_NEW_CLUSTER_CANDIDATE_THRESHOLD)
    parser.add_argument("--anomaly-cluster-threshold", type=float, default=0.88, help="Similarity threshold for mapping to existing true anomaly clusters after standard mapping fails.")
    parser.add_argument("--top-k", type=int, default=DEFAULT_TOP_K)
    parser.add_argument("--embedding-batch-size", type=int, default=128)
    parser.add_argument(
        "--text-mode",
        choices=["field_label", "label_only"],
        default="field_label",
        help="Embedding text format. Default field_label matches pipeline.py default.",
    )

    parser.add_argument(
        "--embedding-provider",
        choices=["sentence-transformers", "deterministic-test"],
        default="sentence-transformers",
    )
    parser.add_argument(
        "--embedding-model",
        default=os.getenv("EMBEDDING_MODEL_NAME", DEFAULT_EMBEDDING_MODEL),
        help="Must match the model used by offline clustering.",
    )
    parser.add_argument(
        "--device",
        default="openvino-gpu",
        help="Embedding device: auto, cpu, cuda, cuda:N, npu, openvino-gpu, or openvino-cpu.",
    )
    parser.add_argument("--embedding-dimensions", type=int, default=DEFAULT_EMBEDDING_DIMENSIONS)

    args = parser.parse_args()
    if not args.input_file and not args.input_table:
        args.input_table = DEFAULT_NGP_INPUT_TABLE
    if args.timestamp_column is not None and str(args.timestamp_column).strip() == "":
        args.timestamp_column = None
    if args.classified_at_column is not None and str(args.classified_at_column).strip() == "":
        args.classified_at_column = None
    return args


def main() -> int:
    args = parse_args()
    started_at = utc_now()
    load_environment(args.env_file)

    fields = [field.strip() for field in args.fields.split(",") if field.strip()]
    metadata_columns = [col.strip() for col in str(args.metadata_columns or "").split(",") if col.strip()]
    if not fields:
        raise ValueError("At least one field is required in --fields.")

    mapper_window_start, mapper_window_end = compute_window(args)
    field_existing_thresholds = parse_field_threshold_overrides(args.field_existing_thresholds)
    if field_existing_thresholds:
        print(f"Per-field existing-cluster thresholds: {field_existing_thresholds}")

    mapper_run_id = f"mapper_{started_at.strftime('%Y%m%d_%H%M%S')}_{uuid.uuid4().hex[:8]}"
    source_db = args.input_db if args.input_table else "file"
    source_table = args.input_table or str(Path(args.input_file).name)

    local_conn = get_local_pg_connection()
    input_conn = None
    input_row_count = 0
    exploded_label_count = 0
    results: List[MappingResult] = []

    try:
        if args.ensure_schema:
            ensure_output_schema(
                local_conn,
                output_table=args.output_table,
                run_table=args.run_table,
                create_iris_views=not args.skip_iris_views,
            )
            print(f"Ensured output schema: {args.output_table}, {args.run_table}")

        if args.input_file:
            df = pd.read_csv(args.input_file)
        else:
            if args.input_db == "app":
                input_conn = get_app_db_connection()
                print("Input DB: APP_DB / ai-call-analysis-db")
            else:
                input_conn = local_conn
                print("Input DB: LOCAL_PG / taxonomy_drift_local")

            if mapper_window_start and mapper_window_end:
                print(f"Mapper window UTC: {mapper_window_start.isoformat()} -> {mapper_window_end.isoformat()}")

            df = read_input_from_postgres(
                input_conn,
                input_table=args.input_table,
                call_id_column=args.call_id_column,
                fields=fields,
                timestamp_column=args.timestamp_column,
                classified_at_column=args.classified_at_column,
                metadata_columns=metadata_columns,
                window_start=mapper_window_start,
                window_end=mapper_window_end,
                where_clause=args.where,
                limit=args.limit,
            )

        input_row_count = len(df)
        print(f"Loaded input rows: {input_row_count:,}")

        missing_fields = [field for field in fields if field not in df.columns]
        if missing_fields:
            print(f"Warning: fields not found in input and skipped: {missing_fields}", file=sys.stderr)

        clusters_by_field = load_active_clusters(
            local_conn,
            cluster_table=args.cluster_table,
            fields=fields,
            default_existing_threshold=args.existing_cluster_threshold,
            cluster_version=args.cluster_version,
            include_anomaly_clusters=False,
        )
        anomaly_clusters_by_field = load_active_clusters(
            local_conn,
            cluster_table=args.cluster_table,
            fields=fields,
            default_existing_threshold=args.anomaly_cluster_threshold,
            cluster_version=args.cluster_version,
            include_anomaly_clusters=True,
        )
        for field in fields:
            print(f"Loaded active approved clusters for {field}: {len(clusters_by_field.get(field, [])):,}")
            print(f"Loaded active true anomaly clusters for {field}: {len(anomaly_clusters_by_field.get(field, [])):,}")

        exact_label_map: Dict[Tuple[str, str], ExactLabelReference] = {}
        exact_anomaly_label_map: Dict[Tuple[str, str], ExactLabelReference] = {}
        if not args.disable_exact_label_map:
            exact_label_map = load_exact_label_map(
                local_conn,
                label_map_table=args.label_map_table,
                fields=fields,
                cluster_version=args.cluster_version,
                include_anomaly_labels=False,
            )
            exact_anomaly_label_map = load_exact_label_map(
                local_conn,
                label_map_table=args.label_map_table,
                fields=fields,
                cluster_version=args.cluster_version,
                include_anomaly_labels=True,
            )
            print(f"Loaded exact approved label mappings: {len(exact_label_map):,}")
            print(f"Loaded exact true anomaly label mappings: {len(exact_anomaly_label_map):,}")
        else:
            print("Exact approved/anomaly label map lookup: disabled")

        embedding_provider = build_embedding_provider(args)
        results, exploded_label_count = run_mapping(
            df=df,
            source_db=source_db,
            source_table=source_table,
            call_id_column=args.call_id_column,
            fields=fields,
            timestamp_column=args.timestamp_column,
            classified_at_column=args.classified_at_column,
            metadata_columns=metadata_columns,
            exact_label_map=exact_label_map,
            exact_anomaly_label_map=exact_anomaly_label_map,
            embedding_provider=embedding_provider,
            clusters_by_field=clusters_by_field,
            anomaly_clusters_by_field=anomaly_clusters_by_field,
            mapper_run_id=mapper_run_id,
            mapper_window_start=mapper_window_start,
            mapper_window_end=mapper_window_end,
            new_cluster_candidate_threshold=args.new_cluster_candidate_threshold,
            anomaly_cluster_threshold=args.anomaly_cluster_threshold,
            top_k=args.top_k,
            embedding_batch_size=args.embedding_batch_size,
            text_mode=args.text_mode,
            embedding_model=args.embedding_model,
            expected_embedding_dimensions=args.embedding_dimensions,
            field_existing_thresholds=field_existing_thresholds,
        )

        summary = summarize_results(results)
        print("\nMapper run id:", mapper_run_id)
        print("\nMapping summary:")
        if summary.empty:
            print("No labels were mapped.")
        else:
            print(summary.to_string(index=False))

        if args.write_output:
            if not args.ensure_schema:
                print("Warning: --write-output was used without --ensure-schema. Existing DB schema must already be patched.", file=sys.stderr)
            inserted_or_updated = upsert_mapping_results(local_conn, output_table=args.output_table, results=results)
            print(f"\nUpserted {inserted_or_updated:,} rows into local table: {args.output_table}")
        else:
            print("\nOutput not written. Add --write-output to upsert results.")

        if args.preview_csv:
            write_preview_csv(results, args.preview_csv)
            print(f"Saved preview CSV: {args.preview_csv}")

        if args.write_output:
            config = {
                "cluster_table": args.cluster_table,
                "label_map_table": args.label_map_table,
                "output_table": args.output_table,
                "cluster_version": args.cluster_version,
                "existing_cluster_threshold": args.existing_cluster_threshold,
                "field_existing_thresholds": field_existing_thresholds,
                "new_cluster_candidate_threshold": args.new_cluster_candidate_threshold,
                "anomaly_cluster_threshold": args.anomaly_cluster_threshold,
                "top_k": args.top_k,
                "text_mode": args.text_mode,
                "embedding_provider": args.embedding_provider,
                "embedding_model": args.embedding_model,
                "embedding_dimensions": args.embedding_dimensions,
                "exact_label_map_enabled": not args.disable_exact_label_map,
                "metadata_columns": metadata_columns,
            }
            write_mapper_run(
                local_conn,
                run_table=args.run_table,
                mapper_run_id=mapper_run_id,
                source_db=source_db,
                source_table=source_table,
                input_row_count=input_row_count,
                exploded_label_count=exploded_label_count,
                result_count=len(results),
                mapper_window_start=mapper_window_start,
                mapper_window_end=mapper_window_end,
                fields=fields,
                counts=status_counts(results),
                config=config,
                started_at=started_at,
                finished_at=utc_now(),
                status="SUCCESS",
            )
            print(f"Wrote mapper run metadata into: {args.run_table}")

        return 0

    except Exception as exc:
        if args.write_output:
            try:
                write_mapper_run(
                    local_conn,
                    run_table=args.run_table,
                    mapper_run_id=mapper_run_id,
                    source_db=source_db,
                    source_table=source_table,
                    input_row_count=input_row_count,
                    exploded_label_count=exploded_label_count,
                    result_count=len(results),
                    mapper_window_start=mapper_window_start,
                    mapper_window_end=mapper_window_end,
                    fields=fields,
                    counts=status_counts(results),
                    config={"failed_args": vars(args)},
                    started_at=started_at,
                    finished_at=utc_now(),
                    status="FAILED",
                    error_message=str(exc),
                )
            except Exception:
                pass
        raise

    finally:
        if input_conn is not None and input_conn is not local_conn:
            input_conn.close()
        local_conn.close()


if __name__ == "__main__":
    raise SystemExit(main())
