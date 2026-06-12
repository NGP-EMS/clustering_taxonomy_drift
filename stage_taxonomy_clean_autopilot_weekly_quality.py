#!/usr/bin/env python3
"""
stage_taxonomy_clean_autopilot_final.py

Standalone STAGE taxonomy cleanup script.

This file does NOT import/call the old autopilot, backfill, resolver, or weekly scripts.

Required behavior implemented:
  1. Scan STAGE rows for raw taxonomy values in the configured fields.
  2. For each raw value:
       A. exact match to existing STANDARD cluster -> map to that cluster display_name
       B. exact match to existing ANOMALY cluster -> promote that anomaly to STANDARD, then map
       C. no exact match, strong semantic match to existing STANDARD cluster -> map to standard
       D. no exact match, strong semantic match to existing ANOMALY cluster -> promote anomaly to STANDARD, then map
       E. no exact/semantic match but present in historical unresolved backlog -> create STANDARD cluster, then map
       F. no exact/semantic match and not historical -> create TRUE ANOMALY cluster, then map
  3. Write/refresh taxonomy_label_cluster_map for every decided value.
  4. Update STAGE values to English display names.
  5. Preserve/compute cluster metadata for created clusters:
       - display_name / cluster_name
       - centroid_embedding
       - medoid_label
       - medoid_similarity_to_centroid
       - representative_labels
       - cluster_size / total_occurrences
       - active / is_true_anomaly_cluster
  6. Use CUDA for batched embedding when available/requested.
  7. Use vectorized semantic matching against existing cluster centroids per field.
  8. Use parallel workers for scanning and updating STAGE.

Default is DRY RUN. Add --apply to write to LOCAL taxonomy DB and STAGE DB.

Env:
  LOCAL_DATABASE_URL or LOCAL_PG_HOST/PORT/DB/USER/PASSWORD
  STAGE_DATABASE_URL or DWH_PG_HOST/PORT/DB/USER/PASSWORD
"""

from __future__ import annotations

import argparse
import csv
import hashlib
import json
import logging
import math
import os
import re
import sys
import time
import threading
from collections import Counter, defaultdict
from concurrent.futures import ThreadPoolExecutor, as_completed
from dataclasses import dataclass, asdict
from datetime import datetime, timezone
from pathlib import Path
from typing import Any, Dict, Iterable, List, Optional, Sequence, Set, Tuple

import numpy as np
import psycopg2
import psycopg2.extras

try:
    from dotenv import load_dotenv
    load_dotenv()
except Exception:
    pass


log = logging.getLogger("clean_autopilot")

DEFAULT_FIELDS = [
    "additional_tags",
    "call_type",
    "call_type_sub",
    "coaching_tags",
    "descriptive_keywords",
    "main_reason",
    "main_reason_sub",
    "next_step",
    "outcome",
    "outcome_sub",
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

LOCAL_CLUSTER_TABLE = "public.taxonomy_clusters"
LOCAL_CLUSTER_NAME_TABLE = "public.taxonomy_cluster_names"
LOCAL_LABEL_MAP_TABLE = "public.taxonomy_label_cluster_map"
LOCAL_EMBEDDINGS_TABLE = "public.taxonomy_label_embeddings"

DEFAULT_STAGE_TABLE = "public.fact_call_classification"
DEFAULT_ID_COLUMN = "id"
DEFAULT_AUDIT_DIR = "audit_clean_autopilot"
DEFAULT_EMBEDDING_MODEL = "sentence-transformers/paraphrase-multilingual-MiniLM-L12-v2"
DEFAULT_EMBEDDING_BATCH_SIZE = 512
DEFAULT_SEMANTIC_THRESHOLD = 0.82
DEFAULT_SEMANTIC_TOP_MARGIN = 0.015
DEFAULT_WORKERS = 8
DEFAULT_BATCH_SIZE = 5000
DEFAULT_UPDATE_PAGE_SIZE = 500

DEFAULT_STANDARD_NAMING_METHOD = "gpt"
DEFAULT_GPT_NAMING_MODEL = os.getenv("OPENAI_NAMING_MODEL", "gpt-4o-mini")
DEFAULT_GPT_NAME_MAX_WORDS = 6
DEFAULT_GPT_NAME_MAX_RETRIES = 5

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
    "coaching_tags": "agent coaching tag indicating either a skill weakness requiring improvement or a demonstrated strength",
    "next_step": "next action after call",
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


INVALID_LABELS = {"", "nan", "none", "null", "n/a", "na", "unknown"}

ACRONYMS = {
    "dm": "DM",
    "loa": "LOA",
    "ivr": "IVR",
    "mpan": "MPAN",
    "mprn": "MPRN",
    "tps": "TPS",
    "3cx": "3CX",
    "vat": "VAT",
    "dno": "DNO",
    "mop": "MOP",
    "dc": "DC",
    "hh": "HH",
    "mhhs": "MHHS",
    "bics": "BICS",
    "bsa": "BSA",
    "ncc": "NCC",
}

IDENTIFIER_RE = re.compile(r"^[A-Za-z_][A-Za-z0-9_]*$")

# Actor-aware guard helpers retained inline so this remains one standalone file.
ACTOR_ALIAS_TO_CANONICAL = {
    "agent": "agent", "advisor": "agent", "adviser": "agent", "rep": "agent",
    "representative": "agent", "consultant": "agent", "salesperson": "agent",
    "customer": "customer", "client": "customer", "prospect": "customer",
    "lead": "customer", "contact": "customer", "caller": "customer",
    "gatekeeper": "customer", "dm": "customer", "decision maker": "customer",
    "decision-maker": "customer", "business owner": "customer",
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


@dataclass
class ClusterRef:
    field_name: str
    cluster_id: str
    display_name: str
    run_id: Optional[str]
    cluster_version: Optional[str]
    is_anomaly: bool
    active: bool
    cluster_size: int
    total_occurrences: int
    medoid_label: Optional[str]
    cluster_source: Optional[str]
    representative_labels: List[str]
    centroid_embedding: Optional[List[float]]


@dataclass
class LabelDecision:
    field_name: str
    raw_label: str
    normalized_label: str
    display_name: str
    action: str
    cluster_id: str
    run_id: str
    cluster_version: str
    is_true_anomaly: bool
    occurrence_count: int
    historical_count: int
    source: str
    previous_cluster_was_anomaly: bool = False
    embedding: Optional[List[float]] = None
    embedding_text: Optional[str] = None
    naming_method: str = "clean_autopilot_deterministic"
    naming_reason: str = "deterministic fallback"
    evidence: Optional[Dict[str, Any]] = None


@dataclass
class DiscoverResult:
    worker_id: int
    rows_scanned: int = 0
    unique_label_keys: int = 0
    label_counts: Dict[Tuple[str, str, str], int] = None

    def __post_init__(self):
        if self.label_counts is None:
            self.label_counts = {}


@dataclass
class UpdateResult:
    worker_id: int
    rows_scanned: int = 0
    rows_changed: int = 0
    rows_unchanged: int = 0
    rows_error: int = 0


def utcnow() -> datetime:
    return datetime.now(timezone.utc)


def now_stamp() -> str:
    return datetime.now(timezone.utc).strftime("%Y%m%d_%H%M%S")


def stable_hash(*parts: Any, length: int = 18) -> str:
    payload = "||".join(str(p or "") for p in parts)
    return hashlib.sha256(payload.encode("utf-8")).hexdigest()[:length]


def safe_id(name: str) -> str:
    if not IDENTIFIER_RE.match(name or ""):
        raise ValueError(f"Unsafe SQL identifier: {name!r}")
    return f'"{name}"'


def safe_table(name: str) -> str:
    parts = str(name).split(".")
    if not parts or any(not p for p in parts):
        raise ValueError(f"Unsafe SQL table name: {name!r}")
    return ".".join(safe_id(p) for p in parts)


def split_schema_table(name: str) -> Tuple[str, str]:
    if "." in name:
        return tuple(name.split(".", 1))  # type: ignore
    return "public", name


def _build_dsn(host, port, db, user, password) -> Optional[str]:
    if not all([host, port, db, user, password]):
        return None
    return f"host={host} port={port} dbname={db} user={user} password={password}"


def build_local_dsn() -> Optional[str]:
    return (
        os.getenv("LOCAL_DATABASE_URL")
        or os.getenv("LOCAL_PG_CONN_STR")
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
        or os.getenv("DWH_PG_CONN_STR")
        or _build_dsn(
            os.getenv("DWH_PG_HOST") or os.getenv("DWH_HOST"),
            os.getenv("DWH_PG_PORT") or os.getenv("DWH_PORT"),
            os.getenv("DWH_PG_DB") or os.getenv("DWH_NAME"),
            os.getenv("DWH_PG_USER") or os.getenv("DWH_USER"),
            os.getenv("DWH_PG_PASSWORD") or os.getenv("DWH_PASS"),
        )
    )


def connect(dsn: str):
    return psycopg2.connect(dsn)


def table_exists(conn, table_name: str) -> bool:
    schema, table = split_schema_table(table_name)
    with conn.cursor() as cur:
        cur.execute(
            """
            SELECT EXISTS (
              SELECT 1
              FROM information_schema.tables
              WHERE table_schema=%s AND table_name=%s
            )
            """,
            (schema, table),
        )
        return bool(cur.fetchone()[0])


def get_columns(conn, table_name: str) -> Dict[str, Dict[str, Any]]:
    schema, table = split_schema_table(table_name)
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            """
            SELECT column_name, data_type, udt_name, is_nullable, column_default
            FROM information_schema.columns
            WHERE table_schema=%s AND table_name=%s
            ORDER BY ordinal_position
            """,
            (schema, table),
        )
        return {r["column_name"]: dict(r) for r in cur.fetchall()}


def normalize_exact(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    return text.lower() if text else None


def normalize_loose(value: Any) -> Optional[str]:
    if value is None:
        return None
    text = str(value).strip()
    if len(text) >= 2 and text[0] == text[-1] and text[0] in {"'", '"'}:
        text = text[1:-1].strip()
    if not text:
        return None
    text = text.replace("_", " ").replace("-", " ").replace("/", " ")
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"\s+", " ", text).strip().lower()
    return text if text and text not in INVALID_LABELS else None


def display_name_from_label(value: Any, max_words: int = 6) -> str:
    normalized = normalize_loose(value) or ""
    words = [w for w in normalized.split() if w]
    if not words:
        return "Unknown"
    out = []
    for word in words[:max_words]:
        low = word.lower()
        if low in ACRONYMS:
            out.append(ACRONYMS[low])
        elif re.fullmatch(r"\d+[a-z]*", low):
            out.append(low.upper())
        else:
            out.append(low.capitalize())
    return " ".join(out)



def normalize_display_name_key(value: Any) -> str:
    text = str(value or "").lower().strip()
    text = text.replace("_", " ").replace("-", " ").replace("/", " ")
    text = re.sub(r"[^a-z0-9\s]", "", text)
    text = re.sub(r"\s+", " ", text).strip()
    return text


def has_adjacent_repeated_words(value: Any) -> bool:
    words = str(value or "").split()
    return any(words[i].lower() == words[i - 1].lower() for i in range(1, len(words)))


def sanitize_display_name_candidate(value: Any, max_words: int = DEFAULT_GPT_NAME_MAX_WORDS) -> str:
    text = str(value or "").strip()
    if not text:
        return ""
    text = text.splitlines()[0]
    text = text.replace("{", " ").replace("}", " ").replace("[", " ").replace("]", " ")
    text = text.replace("(", " ").replace(")", " ")
    text = re.sub(r"[_|,;:]+", " ", text)
    text = text.replace("-", " ").replace("/", " ")
    text = re.sub(r"(?<=[a-z])(?=[A-Z])", " ", text)
    text = re.sub(r"\b[0-9a-f]{5,}\b$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\b[a-z]{1,8}[0-9a-f]{4,}\b$", "", text, flags=re.IGNORECASE)
    text = re.sub(r"\banomaly\s+review\b", " ", text, flags=re.IGNORECASE)
    text = text.strip('"\'` .,:;|-_')
    text = re.sub(r"[^A-Za-z0-9 &+]", " ", text)
    text = re.sub(r"\s+", " ", text).strip()
    words = [w for w in text.split() if w]
    if not words:
        return ""
    out = []
    for word in words[:max_words]:
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


def validate_display_name(value: Any, existing_names: Set[str], max_words: int = DEFAULT_GPT_NAME_MAX_WORDS) -> List[str]:
    notes: List[str] = []
    cleaned = sanitize_display_name_candidate(value, max_words=max_words)
    if not cleaned:
        return ["blank_name"]
    if len(cleaned.split()) > max_words:
        notes.append("too_many_words")
    if normalize_display_name_key(cleaned) in existing_names:
        notes.append("existing_name_conflict")
    if has_adjacent_repeated_words(cleaned):
        notes.append("adjacent_repeated_words")
    return notes


def has_contradiction(candidate: str, target: str) -> bool:
    cand = normalize_loose(candidate) or ""
    tgt = normalize_loose(target) or ""
    for left, right in CONTRADICTION_PAIRS:
        if left in cand and right in tgt:
            return True
        if right in cand and left in tgt:
            return True
    return False


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
    for prefix in ["Training_Good_", "Training_Clear_", "Coaching_Poor_", "Coaching_Unclear_", "Coaching_", "Training_"]:
        if label_text.startswith(prefix):
            skill = label_text[len(prefix):]
            break
    return f"tier: {tier} | skill: {skill} | direction: {direction} | label: {normalize_loose(label_text) or ''}"


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
    return f"next_step_outcome: {polarity} | label: {normalize_loose(label_text) or ''}"


def build_embedding_text(field_name: str, raw_label: Any, mode: str = "field_label") -> str:
    cleaned = normalize_loose(raw_label) or ""
    field = str(field_name or "").strip()
    if mode == "label_only":
        return cleaned
    if mode != "field_label":
        raise ValueError(f"Unknown text mode: {mode}")
    if field == "coaching_tags":
        return coaching_aware_embedding_text(raw_label)
    if field == "next_step":
        return next_step_aware_embedding_text(raw_label)
    short_context = FIELD_EMBEDDING_CONTEXT.get(field, "call classification field")
    return f"field: {field}; meaning: {short_context}; label: {cleaned}"


def weighted_centroid(rows: Sequence[Tuple[str, np.ndarray, int]]) -> Tuple[Optional[np.ndarray], Optional[str], Optional[float], List[str], int, int]:
    valid: List[Tuple[str, np.ndarray, int]] = []
    for label, vec, weight in rows:
        if vec is None or vec.ndim != 1 or vec.size == 0:
            continue
        valid.append((label, vec.astype(np.float32), max(1, int(weight or 1))))
    if not valid:
        return None, None, None, [], 0, 0
    dim = valid[0][1].shape[0]
    valid = [(label, vec, weight) for label, vec, weight in valid if vec.shape[0] == dim]
    if not valid:
        return None, None, None, [], 0, 0
    matrix = np.vstack([v for _label, v, _weight in valid]).astype(np.float32)
    weights = np.asarray([w for _label, _vec, w in valid], dtype=np.float32)
    center = np.average(matrix, axis=0, weights=weights).astype(np.float32)
    norm = float(np.linalg.norm(center))
    if norm > 0:
        center = (center / norm).astype(np.float32)
    matrix_norm = matrix / np.maximum(np.linalg.norm(matrix, axis=1, keepdims=True), 1e-12)
    sims = matrix_norm @ center
    best_idx = int(np.argmax(sims))
    medoid = valid[best_idx][0]
    medoid_sim = float(sims[best_idx])
    counter = Counter()
    for label, _vec, weight in valid:
        counter[label] += weight
    reps = [label for label, _count in counter.most_common(12)]
    return center, medoid, medoid_sim, reps, len(counter), int(sum(counter.values()))

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


def parse_embedding(value: Any) -> Optional[np.ndarray]:
    if value is None:
        return None
    if isinstance(value, np.ndarray):
        arr = value.astype(np.float32)
    elif isinstance(value, list):
        arr = np.array(value, dtype=np.float32)
    elif isinstance(value, tuple):
        arr = np.array(list(value), dtype=np.float32)
    elif isinstance(value, str):
        text = value.strip()
        if not text:
            return None
        try:
            if text.startswith("["):
                arr = np.array(json.loads(text), dtype=np.float32)
            else:
                body = text.strip("{}")
                arr = np.array([float(x) for x in body.split(",") if x.strip()], dtype=np.float32)
        except Exception:
            return None
    else:
        try:
            arr = np.array(value, dtype=np.float32)
        except Exception:
            return None
    if arr.ndim != 1 or arr.size == 0 or not np.all(np.isfinite(arr)):
        return None
    norm = float(np.linalg.norm(arr))
    if norm > 0:
        arr = (arr / norm).astype(np.float32)
    return arr.astype(np.float32)


def vector_to_list(value: Optional[np.ndarray]) -> Optional[List[float]]:
    if value is None:
        return None
    return [float(x) for x in value.astype(np.float32).tolist()]


def extract_representative_labels(value: Any) -> List[str]:
    parsed = parse_jsonish(value)
    out: List[str] = []
    if parsed is None:
        return out
    if isinstance(parsed, list):
        for item in parsed:
            if isinstance(item, str):
                if item.strip():
                    out.append(item.strip())
            elif isinstance(item, dict):
                for key in ("raw_label", "label", "medoid_label", "name"):
                    if item.get(key):
                        out.append(str(item[key]).strip())
                        break
    elif isinstance(parsed, dict):
        for key in ("labels", "representative_labels", "raw_labels"):
            val = parsed.get(key)
            if isinstance(val, list):
                out.extend(str(x).strip() for x in val if str(x).strip())
    return list(dict.fromkeys(out))


def token_positions(normalized_text: str) -> List[Tuple[int, str, str]]:
    tokens = normalized_text.split()
    positions: List[Tuple[int, str, str]] = []
    used: Set[int] = set()
    phrases = sorted([a for a in ACTOR_ALIAS_TO_CANONICAL if " " in a or "-" in a], key=lambda v: len(v.split()), reverse=True)
    for alias in phrases:
        alias_tokens = (normalize_loose(alias) or "").split()
        size = len(alias_tokens)
        if not alias_tokens:
            continue
        for idx in range(max(0, len(tokens) - size + 1)):
            if any(idx + o in used for o in range(size)):
                continue
            if tokens[idx:idx + size] == alias_tokens:
                positions.append((idx, ACTOR_ALIAS_TO_CANONICAL[alias], alias))
                used.update(idx + o for o in range(size))
    for idx, token in enumerate(tokens):
        if idx in used:
            continue
        if token in ACTOR_ALIAS_TO_CANONICAL:
            positions.append((idx, ACTOR_ALIAS_TO_CANONICAL[token], token))
    return sorted(positions, key=lambda x: x[0])


def action_categories_between(tokens: List[str], start: int, end: int) -> Set[str]:
    if start > end:
        start, end = end, start
    window = tokens[max(0, start): min(len(tokens), end + 1)]
    found: Set[str] = set()
    for category, words in ACTOR_ACTION_KEYWORDS.items():
        if any(t in words for t in window):
            found.add(category)
    return found


def extract_actor_relations(text: Any) -> List[Dict[str, str]]:
    norm = normalize_loose(text) or ""
    if not norm:
        return []
    tokens = norm.split()
    actors = token_positions(norm)
    out: List[Dict[str, str]] = []
    seen: Set[Tuple[str, str, str]] = set()
    for left_i, left_actor, _ in actors:
        for right_i, right_actor, _ in actors:
            if left_i == right_i or left_actor == right_actor:
                continue
            if abs(right_i - left_i) > 10:
                continue
            low = min(left_i, right_i)
            high = max(left_i, right_i)
            cats = action_categories_between(tokens, low, high)
            if not cats:
                cats = action_categories_between(tokens, low, min(len(tokens) - 1, high + 3))
            if not cats:
                continue
            actor = left_actor if left_i < right_i else right_actor
            target = right_actor if left_i < right_i else left_actor
            if "by" in tokens[low: high + 1]:
                actor = right_actor if right_i > left_i else left_actor
                target = left_actor if right_i > left_i else right_actor
            for cat in cats:
                key = (actor, target, cat)
                if key not in seen:
                    seen.add(key)
                    out.append({"actor": actor, "target": target, "action": cat})
    return out


def has_actor_direction_conflict(candidate_text: Any, target_text: Any) -> bool:
    c_rel = extract_actor_relations(candidate_text)
    t_rel = extract_actor_relations(target_text)
    for c in c_rel:
        for t in t_rel:
            if c["action"] == t["action"] and c["actor"] == t["target"] and c["target"] == t["actor"]:
                return True
    return False


def parse_stage_labels(value: Any, field_name: str) -> List[str]:
    if value is None:
        return []
    if isinstance(value, (list, tuple)):
        return [str(v).strip() for v in value if str(v).strip() and str(v).strip().lower() not in INVALID_LABELS]

    text = str(value).strip()
    if not text or text.lower() in INVALID_LABELS:
        return []

    # JSON array text
    if text.startswith("[") and text.endswith("]"):
        try:
            parsed = json.loads(text)
            if isinstance(parsed, list):
                return [str(v).strip() for v in parsed if str(v).strip() and str(v).strip().lower() not in INVALID_LABELS]
        except Exception:
            pass

    # PostgreSQL array-ish text
    if text.startswith("{") and text.endswith("}"):
        body = text[1:-1].strip()
        if not body:
            return []
        return [x.strip().strip('"').strip("'") for x in body.split(",") if x.strip().strip('"').strip("'")]

    if field_name in MULTI_VALUE_FIELDS:
        # Common separators emitted by classifiers/backfill flows.
        parts = re.split(r"\s*(?:,|;|\|)\s*", text)
        clean = [p.strip().strip('"').strip("'") for p in parts if p.strip().strip('"').strip("'")]
        return [p for p in clean if p.lower() not in INVALID_LABELS]

    return [text]


def assemble_stage_value(original_value: Any, mapped_labels: List[str], field_name: str) -> Any:
    if original_value is None:
        return None
    if isinstance(original_value, list):
        return mapped_labels
    if isinstance(original_value, tuple):
        return mapped_labels
    if field_name in MULTI_VALUE_FIELDS:
        return ", ".join(mapped_labels)
    return mapped_labels[0] if mapped_labels else original_value


def label_key(field_name: str, raw_label: str) -> Tuple[str, str]:
    return (field_name, normalize_loose(raw_label) or "")


def decision_cluster_id(prefix: str, field_name: str, normalized_label: str) -> str:
    return f"{prefix}_{stable_hash(field_name, normalized_label, length=18)}"


def load_historical_counts(path: Optional[str]) -> Dict[Tuple[str, str], int]:
    if not path:
        return {}
    p = Path(path)
    if not p.exists():
        raise FileNotFoundError(f"Historical unresolved CSV not found: {p}")

    counts: Dict[Tuple[str, str], int] = defaultdict(int)
    with p.open("r", encoding="utf-8-sig", newline="") as f:
        reader = csv.DictReader(f)
        for row in reader:
            field = (row.get("field_name") or row.get("field") or "").strip()
            raw = (
                row.get("label")
                or row.get("raw_label")
                or row.get("normalized_label")
                or row.get("unmapped_label")
                or ""
            ).strip()
            if not field or not raw:
                continue
            norm = normalize_loose(raw)
            if not norm:
                continue
            try:
                count = int(float(row.get("count") or row.get("occurrence_count") or row.get("occurrences") or 1))
            except Exception:
                count = 1
            if count > 0:
                counts[(field, norm)] += count
    return dict(counts)


def resolve_active_run_ids(conn, fields: Sequence[str]) -> Dict[str, str]:
    run_ids: Dict[str, str] = {}
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        try:
            cur.execute(
                """
                SELECT field_name, run_id
                FROM (
                    SELECT field_name, run_id,
                           ROW_NUMBER() OVER (
                               PARTITION BY field_name
                               ORDER BY finished_at DESC NULLS LAST, started_at DESC NULLS LAST
                           ) rn
                    FROM public.taxonomy_mapper_runs
                    WHERE field_name = ANY(%s)
                      AND COALESCE(active, TRUE) = TRUE
                ) x
                WHERE rn = 1
                """,
                (list(fields),),
            )
            for r in cur.fetchall():
                if r["run_id"]:
                    run_ids[r["field_name"]] = r["run_id"]
        except Exception:
            conn.rollback()

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
                               ) rn
                        FROM public.taxonomy_run_metadata
                        WHERE field_name = ANY(%s)
                    ) x
                    WHERE rn = 1
                    """,
                    (missing,),
                )
                for r in cur.fetchall():
                    if r["run_id"]:
                        run_ids[r["field_name"]] = r["run_id"]
            except Exception:
                conn.rollback()

        missing = [f for f in fields if f not in run_ids]
        if missing and table_exists(conn, LOCAL_LABEL_MAP_TABLE):
            try:
                cur.execute(
                    f"""
                    SELECT field_name, run_id
                    FROM (
                        SELECT field_name, run_id,
                               ROW_NUMBER() OVER (
                                   PARTITION BY field_name
                                   ORDER BY COUNT(*) DESC
                               ) rn
                        FROM {safe_table(LOCAL_LABEL_MAP_TABLE)}
                        WHERE field_name = ANY(%s)
                          AND run_id IS NOT NULL
                        GROUP BY field_name, run_id
                    ) x
                    WHERE rn = 1
                    """,
                    (missing,),
                )
                for r in cur.fetchall():
                    if r["run_id"]:
                        run_ids[r["field_name"]] = r["run_id"]
            except Exception:
                conn.rollback()

    today = f"clean_auto_{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    for field in fields:
        run_ids.setdefault(field, today)
    return run_ids


def load_cluster_refs(conn, fields: Sequence[str]) -> Dict[Tuple[str, str], ClusterRef]:
    if not table_exists(conn, LOCAL_CLUSTER_TABLE):
        raise RuntimeError(f"Missing table {LOCAL_CLUSTER_TABLE}")

    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT
                c.field_name,
                c.cluster_id,
                COALESCE(n.display_name, c.display_name, c.cluster_name, c.cluster_id) AS display_name,
                c.run_id,
                c.cluster_version,
                COALESCE(c.is_true_anomaly_cluster, FALSE) AS is_true_anomaly_cluster,
                COALESCE(c.active, TRUE) AS active,
                COALESCE(c.cluster_size, 0) AS cluster_size,
                COALESCE(c.total_occurrences, 0) AS total_occurrences,
                c.medoid_label,
                c.cluster_source,
                c.representative_labels,
                c.centroid_embedding
            FROM {safe_table(LOCAL_CLUSTER_TABLE)} c
            LEFT JOIN {safe_table(LOCAL_CLUSTER_NAME_TABLE)} n
            ON n.field_name = c.field_name
            AND n.run_id = c.run_id
            AND n.cluster_version = c.cluster_version
            AND n.cluster_id = c.cluster_id
            WHERE c.field_name = ANY(%s)
            AND COALESCE(c.active, TRUE) = TRUE
            """,

            (list(fields),),
        )
        refs: Dict[str, ClusterRef] = {}
        for r in cur.fetchall():
            is_anom = bool(r["is_true_anomaly_cluster"]) or str(r.get("cluster_source") or "").lower() == "true_anomaly"
            reps = extract_representative_labels(r.get("representative_labels"))
            emb = parse_embedding(r.get("centroid_embedding"))
            refs[(r["field_name"], str(r["cluster_id"]))] = ClusterRef(
                field_name=r["field_name"],
                cluster_id=str(r["cluster_id"]),
                display_name=str(r["display_name"] or r["cluster_id"]),
                run_id=str(r["run_id"]) if r.get("run_id") else None,
                cluster_version=str(r["cluster_version"]) if r.get("cluster_version") else None,
                is_anomaly=is_anom,
                active=bool(r["active"]),
                cluster_size=int(r["cluster_size"] or 0),
                total_occurrences=int(r["total_occurrences"] or 0),
                medoid_label=r.get("medoid_label"),
                cluster_source=r.get("cluster_source"),
                representative_labels=reps,
                centroid_embedding=vector_to_list(emb),
            )
        return refs


def cluster_rank(ref: ClusterRef) -> Tuple[int, int, int, str]:
    # Standard clusters first, then higher evidence.
    standard_priority = 1 if not ref.is_anomaly else 0
    return (standard_priority, ref.total_occurrences or 0, ref.cluster_size or 0, ref.cluster_id)


def add_index_candidate(
    index: Dict[Tuple[str, str], ClusterRef],
    field_name: str,
    raw_key: Any,
    ref: ClusterRef,
) -> None:
    for key in (normalize_exact(raw_key), normalize_loose(raw_key)):
        if not key:
            continue
        idx_key = (field_name, key)
        existing = index.get(idx_key)
        if existing is None or cluster_rank(ref) > cluster_rank(existing):
            index[idx_key] = ref


def build_exact_cluster_index(
    conn,
    fields: Sequence[str],
    cluster_refs: Dict[Tuple[str, str], ClusterRef],
) -> Dict[Tuple[str, str], ClusterRef]:
    index: Dict[Tuple[str, str], ClusterRef] = {}

    # Index cluster-level names/medoids/representatives.
    for ref in cluster_refs.values():
        if ref.field_name not in fields:
            continue
        add_index_candidate(index, ref.field_name, ref.display_name, ref)
        add_index_candidate(index, ref.field_name, ref.medoid_label, ref)
        for label in ref.representative_labels:
            add_index_candidate(index, ref.field_name, label, ref)

    # Index label-map raw and normalized labels.
    if table_exists(conn, LOCAL_LABEL_MAP_TABLE):
        with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
            cur.execute(
                f"""
                SELECT
                    lm.field_name,
                    lm.raw_label,
                    lm.normalized_label,
                    lm.final_cluster_id,
                    lm.run_id,
                    lm.cluster_version,
                    COALESCE(lm.final_is_true_anomaly, FALSE) AS final_is_true_anomaly
                FROM {safe_table(LOCAL_LABEL_MAP_TABLE)} lm
                WHERE lm.field_name = ANY(%s)
                AND lm.final_cluster_id IS NOT NULL
                """,
                (list(fields),),
            )
            for r in cur.fetchall():
                cid = str(r["final_cluster_id"])
                ref = cluster_refs.get((r["field_name"], cid))
                if ref is None:
                    ref = ClusterRef(
                        field_name=r["field_name"],
                        cluster_id=cid,
                        display_name=cid,
                        run_id=str(r["run_id"]) if r.get("run_id") else None,
                        cluster_version=str(r["cluster_version"]) if r.get("cluster_version") else None,
                        is_anomaly=bool(r.get("final_is_true_anomaly")),
                        active=True,
                        cluster_size=0,
                        total_occurrences=0,
                        medoid_label=None,
                        cluster_source=None,
                        representative_labels=[],
                        centroid_embedding=None,
                    )
                add_index_candidate(index, r["field_name"], r.get("raw_label"), ref)
                add_index_candidate(index, r["field_name"], r.get("normalized_label"), ref)
                add_index_candidate(index, r["field_name"], r.get("display_name"), ref)

    return index


def load_embedding_model(model_name: str, device: str):
    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:
        raise RuntimeError("sentence-transformers is required. pip install sentence-transformers") from exc

    log.info("Loading embedding model %s on %s", model_name, device)
    model = SentenceTransformer(model_name, device=device)
    log.info("Embedding model loaded.")
    return model


def embed_texts(model, texts: List[str], batch_size: int) -> Dict[str, List[float]]:
    if not texts:
        return {}
    embeddings = model.encode(
        texts,
        batch_size=batch_size,
        show_progress_bar=len(texts) > 500,
        normalize_embeddings=True,
        convert_to_numpy=True,
    )
    out: Dict[str, List[float]] = {}
    for text, emb in zip(texts, embeddings):
        arr = np.array(emb, dtype=np.float32)
        norm = float(np.linalg.norm(arr))
        if norm > 0:
            arr = (arr / norm).astype(np.float32)
        out[text] = [float(x) for x in arr.tolist()]
    return out




def build_semantic_cluster_index(cluster_refs: Dict[str, ClusterRef]) -> Dict[str, Dict[str, Any]]:
    """
    Build one normalized centroid matrix per field for fast semantic lookup.
    Existing active standard and anomaly clusters are both included. The caller decides
    whether a matched anomaly should be promoted.
    """
    grouped: Dict[str, List[Tuple[ClusterRef, np.ndarray]]] = defaultdict(list)
    for ref in cluster_refs.values():
        emb = parse_embedding(ref.centroid_embedding)
        if emb is None:
            continue
        norm = float(np.linalg.norm(emb))
        if norm <= 0 or not np.isfinite(norm):
            continue
        grouped[ref.field_name].append((ref, (emb / norm).astype(np.float32)))

    out: Dict[str, Dict[str, Any]] = {}
    for field, items in grouped.items():
        refs = [x[0] for x in items]
        matrix = np.vstack([x[1] for x in items]).astype(np.float32)
        out[field] = {"refs": refs, "matrix": matrix}
    return out


def semantic_candidate_for_label(
    field_name: str,
    raw_label: str,
    embedding: Optional[List[float]],
    semantic_index: Dict[str, Dict[str, Any]],
    threshold: float,
    top_margin: float,
) -> Tuple[Optional[ClusterRef], Dict[str, Any]]:
    """
    Return the best existing cluster only when the semantic evidence is strong enough.
    Safety rules:
      - same field only
      - top similarity must meet threshold
      - if there is a close runner-up, reject to avoid unsafe broad mapping
      - actor direction conflicts reject the match
    """
    evidence: Dict[str, Any] = {
        "semantic_threshold": threshold,
        "semantic_top_margin": top_margin,
        "semantic_checked": False,
        "semantic_match_accepted": False,
    }
    field_idx = semantic_index.get(field_name)
    if not field_idx or embedding is None:
        evidence["reason"] = "missing_field_semantic_index_or_label_embedding"
        return None, evidence

    vec = np.asarray(embedding, dtype=np.float32)
    if vec.ndim != 1 or vec.size == 0:
        evidence["reason"] = "invalid_label_embedding"
        return None, evidence
    norm = float(np.linalg.norm(vec))
    if norm <= 0 or not np.isfinite(norm):
        evidence["reason"] = "zero_label_embedding"
        return None, evidence
    vec = (vec / norm).astype(np.float32)

    matrix = field_idx["matrix"]
    refs: List[ClusterRef] = field_idx["refs"]
    sims = matrix @ vec
    if sims.size == 0:
        evidence["reason"] = "empty_semantic_index"
        return None, evidence

    top_count = min(5, int(sims.size))
    top_idx = np.argpartition(-sims, top_count - 1)[:top_count]
    top_idx = top_idx[np.argsort(-sims[top_idx])]
    top_candidates = []
    for idx in top_idx:
        ref = refs[int(idx)]
        top_candidates.append({
            "cluster_id": ref.cluster_id,
            "display_name": ref.display_name,
            "is_anomaly": ref.is_anomaly,
            "similarity": round(float(sims[int(idx)]), 4),
            "medoid_label": ref.medoid_label,
        })

    best_i = int(top_idx[0])
    best_ref = refs[best_i]
    best_sim = float(sims[best_i])
    second_sim = float(sims[int(top_idx[1])]) if len(top_idx) > 1 else None
    margin = (best_sim - second_sim) if second_sim is not None else None

    evidence.update({
        "semantic_checked": True,
        "top_candidates": top_candidates,
        "best_similarity": round(best_sim, 4),
        "second_similarity": round(second_sim, 4) if second_sim is not None else None,
        "top_margin_actual": round(margin, 4) if margin is not None else None,
    })

    if best_sim < threshold:
        evidence["reason"] = "best_similarity_below_threshold"
        return None, evidence

    if margin is not None and margin < top_margin:
        evidence["reason"] = "semantic_top_candidate_not_stable"
        return None, evidence

    target_text = " ".join(x for x in [best_ref.display_name, best_ref.medoid_label or ""] if x)
    if has_actor_direction_conflict(raw_label, target_text):
        evidence["reason"] = "actor_direction_conflict"
        evidence["actor_guard"] = "blocked"
        return None, evidence
    if has_contradiction(raw_label, target_text):
        evidence["reason"] = "contradiction_guard_blocked"
        evidence["contradiction_guard"] = "blocked"
        return None, evidence

    evidence["semantic_match_accepted"] = True
    evidence["reason"] = "accepted_semantic_match"
    return best_ref, evidence

def get_stage_bounds(stage_dsn: str, stage_table: str, id_column: str) -> Tuple[int, int, int]:
    with connect(stage_dsn) as conn:
        with conn.cursor() as cur:
            cur.execute(
                f"SELECT MIN({safe_id(id_column)}), MAX({safe_id(id_column)}), COUNT(*) FROM {safe_table(stage_table)}"
            )
            min_id, max_id, count = cur.fetchone()
    if min_id is None or max_id is None:
        return 0, 0, 0
    return int(min_id), int(max_id), int(count)


def make_id_ranges(min_id: int, max_id: int, workers: int) -> List[Tuple[int, int]]:
    if workers <= 1 or max_id <= min_id:
        return [(min_id, max_id)]
    span = max_id - min_id + 1
    step = max(1, math.ceil(span / workers))
    ranges = []
    start = min_id
    while start <= max_id:
        end = min(max_id, start + step - 1)
        ranges.append((start, end))
        start = end + 1
    return ranges


def discover_worker(
    worker_id: int,
    stage_dsn: str,
    stage_table: str,
    id_column: str,
    fields: Sequence[str],
    id_range: Tuple[int, int],
    batch_size: int,
    progress_every: int,
) -> DiscoverResult:
    start_id, end_id = id_range
    result = DiscoverResult(worker_id=worker_id, label_counts={})
    counter: Counter = Counter()

    conn = connect(stage_dsn)
    conn.autocommit = True
    try:
        last_id = start_id - 1
        select_cols = [id_column] + list(fields)
        while True:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT {", ".join(safe_id(c) for c in select_cols)}
                    FROM {safe_table(stage_table)}
                    WHERE {safe_id(id_column)} > %s
                      AND {safe_id(id_column)} <= %s
                    ORDER BY {safe_id(id_column)}
                    LIMIT %s
                    """,
                    (last_id, end_id, batch_size),
                )
                rows = cur.fetchall()

            if not rows:
                break

            for row in rows:
                rid = int(row[id_column])
                last_id = rid
                result.rows_scanned += 1

                for field in fields:
                    for raw in parse_stage_labels(row.get(field), field):
                        norm = normalize_loose(raw)
                        if not norm:
                            continue
                        counter[(field, raw, norm)] += 1

            if progress_every and result.rows_scanned % progress_every < batch_size:
                log.info("discover worker=%s scanned=%s unique_keys=%s", worker_id, result.rows_scanned, len(counter))

    finally:
        conn.close()

    result.label_counts = dict(counter)
    result.unique_label_keys = len(counter)
    return result


def merge_discover_results(results: Sequence[DiscoverResult]) -> Dict[Tuple[str, str, str], int]:
    merged: Counter = Counter()
    for result in results:
        merged.update(result.label_counts)
    return dict(merged)


def build_decisions(
    label_counts: Dict[Tuple[str, str, str], int],
    exact_index: Dict[Tuple[str, str], ClusterRef],
    semantic_index: Dict[str, Dict[str, Any]],
    semantic_embeddings: Dict[Tuple[str, str], List[float]],
    historical_counts: Dict[Tuple[str, str], int],
    active_run_ids: Dict[str, str],
    clean_version: str,
    semantic_threshold: float,
    semantic_top_margin: float,
    text_mode: str = "field_label",
) -> Tuple[Dict[Tuple[str, str], LabelDecision], List[LabelDecision]]:
    decisions: Dict[Tuple[str, str], LabelDecision] = {}
    detail: List[LabelDecision] = []

    # Merge raw variants by normalized label, keeping the most frequent raw form.
    grouped: Dict[Tuple[str, str], Dict[str, Any]] = {}
    for (field, raw, norm), count in label_counts.items():
        key = (field, norm)
        entry = grouped.setdefault(key, {"occ": 0, "raw_counts": Counter()})
        entry["occ"] += int(count)
        entry["raw_counts"][raw] += int(count)

    for (field, norm), entry in grouped.items():
        raw = entry["raw_counts"].most_common(1)[0][0]
        occurrence_count = int(entry["occ"])
        historical_count = int(historical_counts.get((field, norm), 0))
        active_run = active_run_ids.get(field, clean_version)
        label_embedding = semantic_embeddings.get((field, norm))
        label_embedding_text = None

        # 1. Exact / normalized / loose existing taxonomy match.
        ref = exact_index.get((field, normalize_exact(raw) or "")) or exact_index.get((field, norm))
        matched_by = None

        # 2. If no exact match, try semantic match against existing standard/anomaly centroids.
        semantic_evidence: Dict[str, Any] = {}
        if ref is None:
            ref, semantic_evidence = semantic_candidate_for_label(
                field_name=field,
                raw_label=raw,
                embedding=label_embedding,
                semantic_index=semantic_index,
                threshold=semantic_threshold,
                top_margin=semantic_top_margin,
            )
            if ref is not None:
                matched_by = "semantic_existing_cluster_centroid"
        else:
            matched_by = "exact_or_loose_existing_taxonomy_key"

        if ref is not None:
            run_id = ref.run_id or active_run
            cv = ref.cluster_version or run_id
            if ref.is_anomaly:
                action = "PROMOTE_EXISTING_ANOMALY_TO_STANDARD"
                is_true_anomaly = False
                previous_anom = True
                source = "semantic_existing_anomaly_promoted" if matched_by == "semantic_existing_cluster_centroid" else "exact_existing_anomaly_promoted"
            else:
                action = "MAP_TO_EXISTING_STANDARD"
                is_true_anomaly = False
                previous_anom = False
                source = "semantic_existing_standard" if matched_by == "semantic_existing_cluster_centroid" else "exact_existing_standard"

            evidence = {
                "matched_cluster_id": ref.cluster_id,
                "matched_cluster_is_anomaly": ref.is_anomaly,
                "matched_by": matched_by,
            }
            if semantic_evidence:
                evidence.update(semantic_evidence)

            decision = LabelDecision(
                field_name=field,
                raw_label=raw,
                normalized_label=norm,
                display_name=ref.display_name or display_name_from_label(raw),
                action=action,
                cluster_id=ref.cluster_id,
                run_id=run_id,
                cluster_version=cv,
                is_true_anomaly=is_true_anomaly,
                occurrence_count=occurrence_count,
                historical_count=historical_count,
                source=source,
                previous_cluster_was_anomaly=previous_anom,
                embedding=label_embedding or ref.centroid_embedding,
                embedding_text=build_embedding_text(field, raw, text_mode),
                evidence=evidence,
            )

        # 3. No exact or safe semantic existing match, but the label exists historically.
        elif historical_count > 0:
            cluster_id = decision_cluster_id("clean_std", field, norm)
            display_name = display_name_from_label(raw)
            decision = LabelDecision(
                field_name=field,
                raw_label=raw,
                normalized_label=norm,
                display_name=display_name,
                action="CREATE_STANDARD_FROM_HISTORICAL_UNRESOLVED",
                cluster_id=cluster_id,
                run_id=active_run,
                cluster_version=active_run,
                is_true_anomaly=False,
                occurrence_count=occurrence_count,
                historical_count=historical_count,
                source="historical_unresolved_promoted_to_standard",
                embedding=label_embedding,
                embedding_text=build_embedding_text(field, raw, text_mode),
                naming_method="pending_standard_gpt_or_deterministic",
                naming_reason="historical unresolved label becomes standard cluster",
                evidence={
                    "historical_count": historical_count,
                    "rule": "no_exact_or_safe_semantic_existing_match_but_present_in_historical_unresolved_csv",
                    "semantic_evidence": semantic_evidence,
                },
            )

        # 4. No exact match, no safe semantic match, and not historical: true new anomaly.
        else:
            cluster_id = decision_cluster_id("clean_anom", field, norm)
            display_name = display_name_from_label(raw)
            decision = LabelDecision(
                field_name=field,
                raw_label=raw,
                normalized_label=norm,
                display_name=display_name,
                action="CREATE_TRUE_ANOMALY_NEW_LABEL",
                cluster_id=cluster_id,
                run_id=active_run,
                cluster_version=active_run,
                is_true_anomaly=True,
                occurrence_count=occurrence_count,
                historical_count=0,
                source="new_unseen_label_true_anomaly",
                embedding=label_embedding,
                embedding_text=build_embedding_text(field, raw, text_mode),
                naming_method="deterministic_true_anomaly_name",
                naming_reason="new unseen label becomes true anomaly",
                evidence={
                    "rule": "no_exact_match_no_safe_semantic_match_and_not_found_in_historical_unresolved",
                    "semantic_evidence": semantic_evidence,
                },
            )

        decisions[(field, norm)] = decision
        detail.append(decision)

    return decisions, detail

def write_decision_csv(path: Path, decisions: Sequence[LabelDecision]) -> None:
    path.parent.mkdir(parents=True, exist_ok=True)
    with path.open("w", encoding="utf-8", newline="") as f:
        writer = csv.DictWriter(
            f,
            fieldnames=[
                "field_name", "raw_label", "normalized_label", "display_name", "action",
                "cluster_id", "run_id", "cluster_version", "is_true_anomaly",
                "occurrence_count", "historical_count", "source", "previous_cluster_was_anomaly", "naming_method", "naming_reason", "embedding_text",
            ],
        )
        writer.writeheader()
        for d in decisions:
            row = asdict(d)
            row.pop("embedding", None)
            row.pop("evidence", None)
            writer.writerow(row)


def default_for_column(col: str, meta: Dict[str, Any]):
    dtype = str(meta.get("data_type") or "").lower()
    if "timestamp" in dtype or "date" in dtype:
        return utcnow()
    if "integer" in dtype or "numeric" in dtype or "double" in dtype or "real" in dtype:
        return 0
    if "boolean" in dtype:
        return False
    if "json" in dtype:
        return psycopg2.extras.Json({})
    if col.endswith("_at"):
        return utcnow()
    if col.endswith("_count"):
        return 0
    return ""


def filtered_insert_values(columns: Dict[str, Dict[str, Any]], values: Dict[str, Any]) -> Dict[str, Any]:
    out: Dict[str, Any] = {k: v for k, v in values.items() if k in columns}
    for col, meta in columns.items():
        if meta.get("is_nullable") == "NO" and meta.get("column_default") is None and col not in out:
            out[col] = default_for_column(col, meta)
    return out


def execute_insert(conn, table_name: str, columns: Dict[str, Dict[str, Any]], values: Dict[str, Any]) -> None:
    vals = filtered_insert_values(columns, values)
    cols = list(vals.keys())
    params = [vals[c] for c in cols]
    with conn.cursor() as cur:
        cur.execute(
            f"""
            INSERT INTO {safe_table(table_name)}
            ({", ".join(safe_id(c) for c in cols)})
            VALUES ({", ".join(["%s"] * len(params))})
            """,
            params,
        )


def execute_update_by_keys(
    conn,
    table_name: str,
    columns: Dict[str, Dict[str, Any]],
    values: Dict[str, Any],
    key_values: Dict[str, Any],
) -> int:
    set_parts = []
    params = []
    for col, val in values.items():
        if col in columns and col not in key_values:
            set_parts.append(f"{safe_id(col)} = %s")
            params.append(val)
    if not set_parts:
        return 0
    where_parts = []
    for col, val in key_values.items():
        where_parts.append(f"{safe_id(col)} = %s")
        params.append(val)
    with conn.cursor() as cur:
        cur.execute(
            f"UPDATE {safe_table(table_name)} SET {', '.join(set_parts)} WHERE {' AND '.join(where_parts)}",
            params,
        )
        return cur.rowcount or 0


def upsert_cluster_name(conn, decision: LabelDecision, dry_run: bool) -> None:
    if not table_exists(conn, LOCAL_CLUSTER_NAME_TABLE):
        return
    cols = get_columns(conn, LOCAL_CLUSTER_NAME_TABLE)
    now = utcnow()

    values = {
        "field_name": decision.field_name,
        "run_id": decision.run_id,
        "cluster_version": decision.cluster_version,
        "cluster_id": decision.cluster_id,
        "is_anomaly": decision.is_true_anomaly,
        "display_name": decision.display_name,
        "naming_method": decision.naming_method or "clean_autopilot_deterministic",
        "naming_reason": decision.naming_reason or decision.source,
        "active": True,
        "created_at": now,
        "updated_at": now,
    }

    if dry_run:
        return

    updated = execute_update_by_keys(
        conn,
        LOCAL_CLUSTER_NAME_TABLE,
        cols,
        {**values, "updated_at": now},
        {"field_name": decision.field_name, "cluster_id": decision.cluster_id},
    )
    if updated <= 0:
        execute_insert(conn, LOCAL_CLUSTER_NAME_TABLE, cols, values)


def upsert_label_map(conn, decision: LabelDecision, dry_run: bool) -> None:
    if not table_exists(conn, LOCAL_LABEL_MAP_TABLE):
        return
    cols = get_columns(conn, LOCAL_LABEL_MAP_TABLE)
    now = utcnow()

    values = {
        "field_name": decision.field_name,
        "raw_label": decision.raw_label,
        "normalized_label": decision.normalized_label,
        "final_cluster_id": decision.cluster_id,
        "final_cluster_source": decision.source,
        "base_cluster_id": "-1" if decision.is_true_anomaly else decision.cluster_id,
        "run_id": decision.run_id,
        "cluster_version": decision.cluster_version,
        "display_name": decision.display_name,
        "value_count": max(1, int(decision.occurrence_count)),
        "final_is_true_anomaly": decision.is_true_anomaly,
        "created_at": now,
        "updated_at": now,
    }

    if dry_run:
        return

    updated = execute_update_by_keys(
        conn,
        LOCAL_LABEL_MAP_TABLE,
        cols,
        {**values, "updated_at": now},
        {"field_name": decision.field_name, "normalized_label": decision.normalized_label},
    )
    if updated <= 0:
        execute_insert(conn, LOCAL_LABEL_MAP_TABLE, cols, values)


def promote_existing_anomaly_cluster(conn, decision: LabelDecision, dry_run: bool) -> None:
    if dry_run:
        return
    cols = get_columns(conn, LOCAL_CLUSTER_TABLE)
    now = utcnow()
    values = {
        "display_name": decision.display_name,
        "cluster_name": decision.display_name,
        "run_id": decision.run_id,
        "cluster_version": decision.cluster_version,
        "is_true_anomaly_cluster": False,
        "cluster_source": "promoted_existing_anomaly_to_standard",
        "promotion_status": "PROMOTED_TO_STANDARD",
        "promotion_candidate_reason": "Exact raw label matched existing true anomaly; promoted by clean autopilot.",
        "active": True,
        "updated_at": now,
    }
    execute_update_by_keys(
        conn,
        LOCAL_CLUSTER_TABLE,
        cols,
        values,
        {"field_name": decision.field_name, "cluster_id": decision.cluster_id},
    )


def create_cluster(conn, decision: LabelDecision, dry_run: bool) -> None:
    if dry_run:
        return
    cols = get_columns(conn, LOCAL_CLUSTER_TABLE)
    now = utcnow()
    reps = [decision.raw_label]
    emb = decision.embedding

    values = {
        "field_name": decision.field_name,
        "cluster_id": decision.cluster_id,
        "run_id": decision.run_id,
        "cluster_version": decision.cluster_version,
        "display_name": decision.display_name,
        "cluster_name": decision.display_name,
        "cluster_source": "clean_true_anomaly" if decision.is_true_anomaly else "clean_historical_standard",
        "centroid_embedding": psycopg2.extras.Json(emb) if emb is not None else None,
        "medoid_label": decision.raw_label,
        "medoid_similarity_to_centroid": 1.0 if emb is not None else None,
        "representative_labels": psycopg2.extras.Json(reps),
        "cluster_size": 1,
        "total_occurrences": max(1, int(decision.occurrence_count)),
        "is_true_anomaly_cluster": decision.is_true_anomaly,
        "active": True,
        "promotion_status": "ACTIVE_TRUE_ANOMALY" if decision.is_true_anomaly else "PROMOTED_TO_STANDARD",
        "promotion_candidate_reason": decision.source,
        "created_at": now,
        "updated_at": now,
    }

    updated = execute_update_by_keys(
        conn,
        LOCAL_CLUSTER_TABLE,
        cols,
        {
            **values,
            "updated_at": now,
            "total_occurrences": max(1, int(decision.occurrence_count)),
            "cluster_size": 1,
        },
        {"field_name": decision.field_name, "cluster_id": decision.cluster_id},
    )
    if updated <= 0:
        execute_insert(conn, LOCAL_CLUSTER_TABLE, cols, values)



def existing_standard_display_names_local(conn, field_name: str, excluding_cluster_id: Optional[str] = None) -> Set[str]:
    if not table_exists(conn, LOCAL_CLUSTER_NAME_TABLE):
        return set()
    params: List[Any] = [field_name]
    exclude_sql = ""
    if excluding_cluster_id:
        exclude_sql = "AND n.cluster_id <> %s"
        params.append(excluding_cluster_id)
    with conn.cursor() as cur:
        cur.execute(
            f"""
            SELECT DISTINCT n.display_name
            FROM {safe_table(LOCAL_CLUSTER_NAME_TABLE)} n
            LEFT JOIN {safe_table(LOCAL_CLUSTER_TABLE)} c
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


def gpt_name_for_decision(args, decision: LabelDecision, existing_names: Set[str]) -> Tuple[Optional[str], str]:
    if getattr(args, "standard_naming_method", DEFAULT_STANDARD_NAMING_METHOD) != "gpt":
        return None, "GPT naming disabled."
    api_key = os.getenv("OPENAI_API_KEY")
    if not api_key:
        return None, "OPENAI_API_KEY is not set."
    try:
        from openai import OpenAI  # type: ignore
    except Exception as exc:
        return None, f"openai package unavailable: {exc}"

    max_words = int(getattr(args, "gpt_name_max_words", DEFAULT_GPT_NAME_MAX_WORDS))
    max_retries = max(1, int(getattr(args, "gpt_name_max_retries", DEFAULT_GPT_NAME_MAX_RETRIES)))
    client = OpenAI(api_key=api_key)
    rejected: Set[str] = set()
    history: List[str] = []
    proposed = sanitize_display_name_candidate(decision.display_name, max_words=max_words) or display_name_from_label(decision.raw_label)

    for attempt in range(1, max_retries + 1):
        prompt = {
            "field_name": decision.field_name,
            "normalized_label": decision.normalized_label,
            "raw_label_examples": [decision.raw_label],
            "current_or_proposed_name": proposed,
            "occurrence_count": decision.occurrence_count,
            "historical_count": decision.historical_count,
            "action": decision.action,
            "attempt": attempt,
            "names_to_avoid_normalized": sorted(existing_names | rejected)[:350],
            "previous_validation_failures": history[-10:],
            "rules": [
                "Return only one display name.",
                f"Maximum {max_words} words.",
                "Use Title Case.",
                "Preserve acronyms exactly when present, e.g. DM, LOA, IVR, TPS, CRM, VAT, MOP, KVA, MPAN, MHHS, CCL, DNC, CoT, 3CX.",
                "Do not use quotes, bullets, IDs, counts, underscores, or explanations.",
                "Do not invent concepts not supported by the label.",
                "Avoid generic names unless the label is truly broad.",
                "If the proposed name is exact and unique, return it unchanged.",
            ],
        }
        try:
            response = client.chat.completions.create(
                model=getattr(args, "gpt_naming_model", DEFAULT_GPT_NAMING_MODEL),
                messages=[
                    {"role": "system", "content": "You name call-classification taxonomy clusters. Return a concise supported cluster display name only."},
                    {"role": "user", "content": json.dumps(prompt, ensure_ascii=False)},
                ],
                temperature=0,
                max_tokens=32,
            )
            raw_name = response.choices[0].message.content if response.choices else ""
        except Exception as exc:
            history.append(f"attempt_{attempt}_gpt_error:{exc}")
            continue
        cleaned = sanitize_display_name_candidate(raw_name, max_words=max_words)
        notes = validate_display_name(cleaned, existing_names | rejected, max_words=max_words)
        if cleaned and not notes:
            retry = "" if attempt == 1 else f" after {attempt} attempts"
            return cleaned, f"GPT-generated standard cluster name{retry}."
        key = normalize_display_name_key(cleaned or raw_name)
        if key:
            rejected.add(key)
        history.append(f"attempt_{attempt}: proposed={cleaned or raw_name!r} notes={','.join(notes) if notes else 'invalid'}")
    return None, "; ".join(history[-5:]) if history else "no valid GPT response"


def resolve_standard_display_name_for_decision(conn, args, decision: LabelDecision) -> None:
    if decision.action not in {"PROMOTE_EXISTING_ANOMALY_TO_STANDARD", "CREATE_STANDARD_FROM_HISTORICAL_UNRESOLVED"}:
        return
    existing = existing_standard_display_names_local(conn, decision.field_name, excluding_cluster_id=decision.cluster_id)
    proposed = sanitize_display_name_candidate(decision.display_name, max_words=int(getattr(args, "gpt_name_max_words", DEFAULT_GPT_NAME_MAX_WORDS))) or display_name_from_label(decision.raw_label)
    gpt_name, gpt_reason = gpt_name_for_decision(args, decision, existing)
    if gpt_name:
        decision.display_name = gpt_name
        decision.naming_method = "gpt_standard_cluster_name"
        decision.naming_reason = gpt_reason
        return
    # deterministic fallback, but keep duplicate handling visible in audit instead of silently failing
    if normalize_display_name_key(proposed) not in existing:
        decision.display_name = proposed
        decision.naming_method = "deterministic_standard_name_fallback"
        decision.naming_reason = f"GPT unavailable or failed; deterministic name was unique. {gpt_reason}"
        return
    # last-resort supported unique name; avoids blocking the full automated cleanup
    unique = sanitize_display_name_candidate(f"{proposed} {stable_hash(decision.field_name, decision.normalized_label, length=4)}")
    decision.display_name = unique or proposed
    decision.naming_method = "deterministic_standard_name_unique_hash_fallback"
    decision.naming_reason = f"GPT unavailable/failed and proposed name duplicated; appended stable short hash. {gpt_reason}"


def upsert_label_embedding(conn, decision: LabelDecision, dry_run: bool, args=None) -> None:
    if dry_run or decision.embedding is None or not table_exists(conn, LOCAL_EMBEDDINGS_TABLE):
        return
    cols = get_columns(conn, LOCAL_EMBEDDINGS_TABLE)
    emb_col = None
    for c in ["embedding", "label_embedding", "embedding_vector", "vector"]:
        if c in cols:
            emb_col = c
            break
    if not emb_col or "normalized_label" not in cols:
        return
    now = utcnow()
    values = {
        "field_name": decision.field_name,
        "raw_label": decision.raw_label,
        "normalized_label": decision.normalized_label,
        emb_col: psycopg2.extras.Json(decision.embedding),
        "embedding_text": decision.embedding_text,
        "model_name": getattr(args, "embedding_model", DEFAULT_EMBEDDING_MODEL) if args is not None else DEFAULT_EMBEDDING_MODEL,
        "text_mode": getattr(args, "text_mode", "field_label") if args is not None else "field_label",
        "created_at": now,
        "updated_at": now,
    }
    vals = {k: v for k, v in values.items() if k in cols}
    if not vals:
        return
    # Update first, then insert if absent; avoids assuming the exact unique constraint.
    key_values = {"normalized_label": decision.normalized_label}
    if "field_name" in cols:
        key_values["field_name"] = decision.field_name
    updated = execute_update_by_keys(conn, LOCAL_EMBEDDINGS_TABLE, cols, vals, key_values)
    if updated <= 0:
        execute_insert(conn, LOCAL_EMBEDDINGS_TABLE, cols, vals)


def rebuild_cluster_metadata(conn, field_name: str, cluster_id: str, dry_run: bool) -> None:
    if dry_run or not table_exists(conn, LOCAL_LABEL_MAP_TABLE) or not table_exists(conn, LOCAL_EMBEDDINGS_TABLE):
        return
    cluster_cols = get_columns(conn, LOCAL_CLUSTER_TABLE)
    map_cols = get_columns(conn, LOCAL_LABEL_MAP_TABLE)
    emb_cols = get_columns(conn, LOCAL_EMBEDDINGS_TABLE)
    emb_col = None
    for c in ["embedding", "label_embedding", "embedding_vector", "vector"]:
        if c in emb_cols:
            emb_col = c
            break
    if not emb_col or not {"field_name", "normalized_label", "final_cluster_id"}.issubset(map_cols):
        return
    value_expr = "COALESCE(lm.value_count, 1)" if "value_count" in map_cols else "1"
    raw_expr = "lm.raw_label" if "raw_label" in map_cols else "lm.normalized_label"
    join_field = "AND e.field_name = lm.field_name" if "field_name" in emb_cols else ""
    with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
        cur.execute(
            f"""
            SELECT {raw_expr} AS raw_label, lm.normalized_label, {value_expr} AS value_count, e.{safe_id(emb_col)} AS embedding
            FROM {safe_table(LOCAL_LABEL_MAP_TABLE)} lm
            LEFT JOIN {safe_table(LOCAL_EMBEDDINGS_TABLE)} e
              ON e.normalized_label = lm.normalized_label
             {join_field}
            WHERE lm.field_name = %s
              AND lm.final_cluster_id = %s
            """,
            (field_name, cluster_id),
        )
        rows = cur.fetchall()
    vectors: List[Tuple[str, np.ndarray, int]] = []
    for row in rows:
        vec = parse_embedding(row.get("embedding"))
        if vec is None:
            continue
        label = str(row.get("raw_label") or row.get("normalized_label") or "")
        vectors.append((label, vec, int(row.get("value_count") or 1)))
    center, medoid, medoid_sim, reps, cluster_size, total_occ = weighted_centroid(vectors)
    values = {
        "centroid_embedding": psycopg2.extras.Json(vector_to_list(center)) if center is not None else None,
        "medoid_label": medoid,
        "medoid_similarity_to_centroid": medoid_sim,
        "representative_labels": psycopg2.extras.Json(reps),
        "cluster_size": cluster_size,
        "total_occurrences": total_occ,
        "updated_at": utcnow(),
    }
    values = {k: v for k, v in values.items() if k in cluster_cols and v is not None}
    if values:
        execute_update_by_keys(conn, LOCAL_CLUSTER_TABLE, cluster_cols, values, {"field_name": field_name, "cluster_id": cluster_id})

def materialize_local_taxonomy(
    local_dsn: str,
    decisions: Sequence[LabelDecision],
    dry_run: bool,
    args=None,
) -> Dict[str, int]:
    counts = Counter(d.action for d in decisions)
    if dry_run:
        log.info("[DRY-RUN] Would materialize local taxonomy decisions: %s", dict(counts))
        return dict(counts)

    conn = connect(local_dsn)
    conn.autocommit = False
    try:
        touched_clusters: Set[Tuple[str, str]] = set()
        for idx, d in enumerate(decisions, start=1):
            if args is not None:
                resolve_standard_display_name_for_decision(conn, args, d)
            upsert_label_embedding(conn, d, dry_run=False, args=args)

            if d.action == "PROMOTE_EXISTING_ANOMALY_TO_STANDARD":
                promote_existing_anomaly_cluster(conn, d, dry_run=False)
                upsert_cluster_name(conn, d, dry_run=False)
                upsert_label_map(conn, d, dry_run=False)
            elif d.action in {"CREATE_STANDARD_FROM_HISTORICAL_UNRESOLVED", "CREATE_TRUE_ANOMALY_NEW_LABEL"}:
                create_cluster(conn, d, dry_run=False)
                upsert_cluster_name(conn, d, dry_run=False)
                upsert_label_map(conn, d, dry_run=False)
            elif d.action == "MAP_TO_EXISTING_STANDARD":
                upsert_label_map(conn, d, dry_run=False)
            else:
                raise RuntimeError(f"Unknown action {d.action}")

            touched_clusters.add((d.field_name, d.cluster_id))
            if idx % 1000 == 0:
                log.info("materialized %d/%d local taxonomy decisions", idx, len(decisions))

        for idx, (field_name, cluster_id) in enumerate(sorted(touched_clusters), start=1):
            rebuild_cluster_metadata(conn, field_name, cluster_id, dry_run=False)
            if idx % 1000 == 0:
                log.info("rebuilt metadata for %d/%d touched clusters", idx, len(touched_clusters))

        conn.commit()
        return dict(counts)
    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()


def apply_stage_mapping_to_row(row: Dict[str, Any], fields: Sequence[str], decisions: Dict[Tuple[str, str], LabelDecision]) -> Tuple[Dict[str, Any], bool]:
    updates: Dict[str, Any] = {}
    changed = False

    for field in fields:
        original = row.get(field)
        raw_labels = parse_stage_labels(original, field)

        if not raw_labels:
            continue

        mapped: List[str] = []
        any_mapped = False
        for raw in raw_labels:
            norm = normalize_loose(raw)
            if not norm:
                mapped.append(raw)
                continue
            decision = decisions.get((field, norm))
            if decision:
                mapped.append(decision.display_name)
                any_mapped = True
            else:
                mapped.append(raw)

        if not any_mapped:
            continue

        new_value = assemble_stage_value(original, mapped, field)
        if str(new_value or "").strip() != str(original or "").strip():
            updates[field] = new_value
            changed = True

    return updates, changed


def update_worker(
    worker_id: int,
    stage_dsn: str,
    stage_table: str,
    id_column: str,
    fields: Sequence[str],
    decisions: Dict[Tuple[str, str], LabelDecision],
    id_range: Tuple[int, int],
    batch_size: int,
    update_page_size: int,
    dry_run: bool,
    progress_every: int,
) -> UpdateResult:
    start_id, end_id = id_range
    result = UpdateResult(worker_id=worker_id)

    conn = connect(stage_dsn)
    conn.autocommit = False
    try:
        last_id = start_id - 1
        select_cols = [id_column] + list(fields)

        while True:
            with conn.cursor(cursor_factory=psycopg2.extras.RealDictCursor) as cur:
                cur.execute(
                    f"""
                    SELECT {", ".join(safe_id(c) for c in select_cols)}
                    FROM {safe_table(stage_table)}
                    WHERE {safe_id(id_column)} > %s
                      AND {safe_id(id_column)} <= %s
                    ORDER BY {safe_id(id_column)}
                    LIMIT %s
                    """,
                    (last_id, end_id, batch_size),
                )
                rows = cur.fetchall()

            if not rows:
                break

            update_rows: List[Tuple[Any, ...]] = []
            update_cols_seen: Set[str] = set()

            # Per-row variable update columns; execute separately in pages by column-set.
            grouped_updates: Dict[Tuple[str, ...], List[Tuple[Any, ...]]] = defaultdict(list)

            for row in rows:
                rid = row[id_column]
                last_id = int(rid)
                result.rows_scanned += 1

                try:
                    updates, changed = apply_stage_mapping_to_row(dict(row), fields, decisions)
                    if changed:
                        result.rows_changed += 1
                        cols = tuple(sorted(updates.keys()))
                        params = tuple(updates[c] for c in cols) + (utcnow(), rid)
                        grouped_updates[cols].append(params)
                    else:
                        result.rows_unchanged += 1
                except Exception:
                    result.rows_error += 1
                    log.exception("worker=%s row=%s mapping error", worker_id, rid)

            if not dry_run:
                with conn.cursor() as cur:
                    for cols, params_list in grouped_updates.items():
                        set_clause = ", ".join([f"{safe_id(c)} = %s" for c in cols])
                        set_clause += ", better_tags_updated_at = %s"
                        sql = (
                            f"UPDATE {safe_table(stage_table)} "
                            f"SET {set_clause} "
                            f"WHERE {safe_id(id_column)} = %s"
                        )
                        for i in range(0, len(params_list), update_page_size):
                            psycopg2.extras.execute_batch(cur, sql, params_list[i:i + update_page_size], page_size=update_page_size)
                conn.commit()
            else:
                conn.rollback()

            if progress_every and result.rows_scanned % progress_every < batch_size:
                log.info(
                    "update worker=%s scanned=%s changed=%s unchanged=%s errors=%s",
                    worker_id, result.rows_scanned, result.rows_changed, result.rows_unchanged, result.rows_error,
                )

        if dry_run:
            conn.rollback()
        else:
            conn.commit()

    except Exception:
        conn.rollback()
        raise
    finally:
        conn.close()

    return result


def run_discovery(args, ranges: List[Tuple[int, int]]) -> Dict[Tuple[str, str, str], int]:
    log.info("Discovery scan starting across %d workers", len(ranges))
    t0 = time.monotonic()
    results: List[DiscoverResult] = []

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                discover_worker,
                idx,
                args.stage_database_url,
                args.stage_table,
                args.id_column,
                args.fields,
                r,
                args.batch_size,
                args.progress_every,
            )
            for idx, r in enumerate(ranges)
        ]
        for fut in as_completed(futures):
            result = fut.result()
            results.append(result)
            log.info(
                "discover worker=%s done scanned=%s unique=%s",
                result.worker_id, result.rows_scanned, result.unique_label_keys,
            )

    merged = merge_discover_results(results)
    log.info(
        "Discovery complete in %.1fs; unique raw variants=%d",
        time.monotonic() - t0,
        len(merged),
    )
    return merged


def run_stage_update(args, ranges: List[Tuple[int, int]], decisions: Dict[Tuple[str, str], LabelDecision]) -> Dict[str, int]:
    log.info("STAGE update scan starting across %d workers (dry_run=%s)", len(ranges), not args.apply)
    t0 = time.monotonic()
    total = Counter()

    with ThreadPoolExecutor(max_workers=args.workers) as executor:
        futures = [
            executor.submit(
                update_worker,
                idx,
                args.stage_database_url,
                args.stage_table,
                args.id_column,
                args.fields,
                decisions,
                r,
                args.batch_size,
                args.update_page_size,
                not args.apply,
                args.progress_every,
            )
            for idx, r in enumerate(ranges)
        ]
        for fut in as_completed(futures):
            result = fut.result()
            total["rows_scanned"] += result.rows_scanned
            total["rows_changed"] += result.rows_changed
            total["rows_unchanged"] += result.rows_unchanged
            total["rows_error"] += result.rows_error
            log.info(
                "update worker=%s done scanned=%s changed=%s unchanged=%s errors=%s",
                result.worker_id, result.rows_scanned, result.rows_changed, result.rows_unchanged, result.rows_error,
            )

    total["elapsed_sec"] = round(time.monotonic() - t0, 1)
    log.info("STAGE update complete: %s", dict(total))
    return dict(total)


def load_or_build_embeddings(args, decisions: List[LabelDecision]) -> None:
    needs_embedding = [
        d for d in decisions
        if d.action in {"CREATE_STANDARD_FROM_HISTORICAL_UNRESOLVED", "CREATE_TRUE_ANOMALY_NEW_LABEL"}
        and d.embedding is None
    ]
    if not needs_embedding:
        return

    texts = sorted({d.embedding_text or build_embedding_text(d.field_name, d.raw_label, args.text_mode) for d in needs_embedding if d.raw_label})
    log.info("Embedding %d remaining new cluster labels using %s on %s", len(texts), args.embedding_model, args.embedding_device)

    model = load_embedding_model(args.embedding_model, args.embedding_device)
    embed_map = embed_texts(model, texts, args.embedding_batch_size)

    for d in needs_embedding:
        text = d.embedding_text or build_embedding_text(d.field_name, d.raw_label, args.text_mode)
        d.embedding_text = text
        emb = embed_map.get(text)
        if emb is not None:
            d.embedding = emb


def write_summary(args, summary: Dict[str, Any], decisions_detail: Sequence[LabelDecision]) -> Path:
    audit_dir = Path(args.audit_dir)
    audit_dir.mkdir(parents=True, exist_ok=True)
    stamp = now_stamp()

    decision_csv = audit_dir / f"clean_autopilot_decisions_{stamp}.csv"
    write_decision_csv(decision_csv, decisions_detail)

    summary["decision_csv"] = str(decision_csv)
    summary_path = audit_dir / f"clean_autopilot_summary_{stamp}.json"
    summary_path.write_text(json.dumps(summary, indent=2, default=str), encoding="utf-8")
    return summary_path


# ── Backfill monitoring table names ───────────────────────────────────────────
BACKFILL_RUN_TABLE        = "public.taxonomy_backfill_runs"
BACKFILL_FIELD_AUDIT_TABLE = "public.taxonomy_backfill_field_audit"
UNRESOLVED_QUEUE_TABLE    = "public.taxonomy_unresolved_label_queue"

_ACTION_TO_FIELD_STATUS = {
    "MAP_TO_EXISTING_STANDARD":               "CHANGED",
    "PROMOTE_EXISTING_ANOMALY_TO_STANDARD":   "CHANGED",
    "CREATE_STANDARD_FROM_HISTORICAL_UNRESOLVED": "CHANGED",
    "CREATE_TRUE_ANOMALY_NEW_LABEL":          "CHANGED",
}
_ACTION_TO_MAPPING_METHOD = {
    "MAP_TO_EXISTING_STANDARD":               "existing_standard",
    "PROMOTE_EXISTING_ANOMALY_TO_STANDARD":   "promoted_anomaly",
    "CREATE_STANDARD_FROM_HISTORICAL_UNRESOLVED": "new_standard_historical",
    "CREATE_TRUE_ANOMALY_NEW_LABEL":          "new_anomaly",
}
_ACTION_TO_RESOLVER_STATUS = {
    "MAP_TO_EXISTING_STANDARD":               "MAP_TO_EXISTING",
    "PROMOTE_EXISTING_ANOMALY_TO_STANDARD":   "PROMOTE",
    "CREATE_STANDARD_FROM_HISTORICAL_UNRESOLVED": "MAP_TO_EXISTING",
    "CREATE_TRUE_ANOMALY_NEW_LABEL":          "ANOMALY",
}


def ensure_backfill_tables(conn) -> None:
    with conn.cursor() as cur:
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.taxonomy_backfill_runs (
            id                      bigserial PRIMARY KEY,
            backfill_run_id         text NOT NULL UNIQUE,
            mode                    text,
            dry_run                 boolean DEFAULT false,
            source_schema           text,
            source_table            text,
            selected_fields         jsonb,
            worker_count            int,
            batch_size              int,
            update_page_size        int,
            include_already_updated boolean DEFAULT false,
            started_at              timestamptz DEFAULT NOW(),
            finished_at             timestamptz,
            status                  text DEFAULT 'RUNNING',
            rows_scanned            bigint DEFAULT 0,
            rows_changed            bigint DEFAULT 0,
            rows_unchanged          bigint DEFAULT 0,
            rows_error              bigint DEFAULT 0,
            rows_pending_before     bigint,
            rows_pending_after      bigint,
            summary_json            jsonb,
            error_message           text
        )
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.taxonomy_backfill_field_audit (
            id                   bigserial PRIMARY KEY,
            backfill_run_id      text NOT NULL,
            stage_row_id         bigint,
            field_name           text NOT NULL,
            field_status         text,
            old_value            text,
            new_value            text,
            changed              boolean,
            mapping_method       text,
            mapped_display_names jsonb,
            unmapped_labels      jsonb,
            ambiguous_labels     jsonb,
            notes                text,
            created_at           timestamptz DEFAULT NOW()
        )
        """)
        cur.execute("""
        CREATE INDEX IF NOT EXISTS idx_tbfa_run_field
          ON public.taxonomy_backfill_field_audit (backfill_run_id, field_name)
        """)
        cur.execute("""
        CREATE TABLE IF NOT EXISTS public.taxonomy_unresolved_label_queue (
            id                         bigserial PRIMARY KEY,
            field_name                 text NOT NULL,
            raw_label                  text NOT NULL,
            normalized_label           text,
            occurrence_count           bigint DEFAULT 1,
            distinct_call_count        bigint DEFAULT 0,
            resolver_status            text,
            target_cluster_id          text,
            target_display_name        text,
            similarity_score           numeric,
            actor_guard_status         text,
            contradiction_guard_status text,
            first_seen_at              timestamptz DEFAULT NOW(),
            last_seen_at               timestamptz DEFAULT NOW(),
            evidence_json              jsonb,
            created_at                 timestamptz DEFAULT NOW(),
            updated_at                 timestamptz DEFAULT NOW(),
            UNIQUE (field_name, normalized_label)
        )
        """)
    conn.commit()


def write_backfill_run_start(conn, backfill_run_id: str, args, rows_pending_before: int) -> None:
    schema, table = split_schema_table(args.stage_table)
    with conn.cursor() as cur:
        cur.execute("""
        INSERT INTO public.taxonomy_backfill_runs
            (backfill_run_id, mode, dry_run, source_schema, source_table,
             selected_fields, worker_count, batch_size, update_page_size,
             include_already_updated, started_at, status, rows_pending_before)
        VALUES (%s, %s, %s, %s, %s, %s::jsonb, %s, %s, %s, %s, %s, 'RUNNING', %s)
        ON CONFLICT (backfill_run_id) DO NOTHING
        """, (
            backfill_run_id,
            "clean_autopilot",
            not args.apply,
            schema,
            table,
            json.dumps(args.fields),
            args.workers,
            args.batch_size,
            args.update_page_size,
            False,
            utcnow(),
            rows_pending_before,
        ))
    conn.commit()


def write_backfill_run_finish(
    conn,
    backfill_run_id: str,
    update_counts: Dict[str, int],
    rows_pending_before: int,
    dry_run: bool,
    summary: Dict[str, Any],
    error_message: Optional[str] = None,
) -> None:
    status = "FAILED" if error_message else "DONE"
    rows_changed = update_counts.get("rows_changed", 0)
    rows_pending_after = rows_pending_before - rows_changed if not dry_run else rows_pending_before
    with conn.cursor() as cur:
        cur.execute("""
        UPDATE public.taxonomy_backfill_runs
        SET status             = %s,
            finished_at        = %s,
            rows_scanned       = %s,
            rows_changed       = %s,
            rows_unchanged     = %s,
            rows_error         = %s,
            rows_pending_after = %s,
            summary_json       = %s::jsonb,
            error_message      = %s
        WHERE backfill_run_id = %s
        """, (
            status,
            utcnow(),
            update_counts.get("rows_scanned", 0),
            rows_changed,
            update_counts.get("rows_unchanged", 0),
            update_counts.get("rows_error", 0),
            max(0, rows_pending_after),
            json.dumps(summary, default=str),
            error_message,
            backfill_run_id,
        ))
    conn.commit()


def write_backfill_field_audit(conn, backfill_run_id: str, decisions: Sequence[LabelDecision]) -> None:
    rows = []
    now = utcnow()
    for d in decisions:
        status = _ACTION_TO_FIELD_STATUS.get(d.action, "CHANGED")
        method = _ACTION_TO_MAPPING_METHOD.get(d.action, d.action)
        rows.append((
            backfill_run_id,
            None,
            d.field_name,
            status,
            d.raw_label,
            d.display_name,
            True,
            method,
            json.dumps([d.display_name]) if d.display_name else json.dumps([]),
            json.dumps([]),
            json.dumps([]),
            d.source,
            now,
        ))
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, """
        INSERT INTO public.taxonomy_backfill_field_audit
            (backfill_run_id, stage_row_id, field_name, field_status,
             old_value, new_value, changed, mapping_method,
             mapped_display_names, unmapped_labels, ambiguous_labels, notes, created_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s::jsonb, %s::jsonb, %s, %s)
        """, rows, page_size=500)
    conn.commit()


def write_unresolved_queue(conn, decisions: Sequence[LabelDecision], applied: bool) -> None:
    rows = []
    now = utcnow()
    for d in decisions:
        resolver_status = _ACTION_TO_RESOLVER_STATUS.get(d.action)
        ev = d.evidence or {}
        sim = ev.get("best_similarity") if ev.get("semantic_match_accepted") else None
        actor_guard = "blocked" if ev.get("reason") == "actor_direction_conflict" else None
        contra_guard = "blocked" if ev.get("reason") == "contradiction_guard_blocked" else None
        evidence_out = {
            "materialized": applied,
            "action": d.action,
            "cluster_id": d.cluster_id,
            "source": d.source,
        }
        if ev:
            evidence_out["semantic"] = {k: v for k, v in ev.items()
                                         if k in ("best_similarity", "top_candidates", "reason", "matched_by")}
        rows.append((
            d.field_name,
            d.raw_label,
            d.normalized_label,
            max(1, int(d.occurrence_count)),
            0,
            resolver_status,
            d.cluster_id,
            d.display_name,
            sim,
            actor_guard,
            contra_guard,
            now,
            now,
            json.dumps(evidence_out, default=str),
            now,
            now,
        ))
    if not rows:
        return
    with conn.cursor() as cur:
        psycopg2.extras.execute_batch(cur, """
        INSERT INTO public.taxonomy_unresolved_label_queue
            (field_name, raw_label, normalized_label, occurrence_count, distinct_call_count,
             resolver_status, target_cluster_id, target_display_name,
             similarity_score, actor_guard_status, contradiction_guard_status,
             first_seen_at, last_seen_at, evidence_json, created_at, updated_at)
        VALUES (%s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s, %s::jsonb, %s, %s)
        ON CONFLICT (field_name, normalized_label) DO UPDATE SET
            occurrence_count           = GREATEST(EXCLUDED.occurrence_count,
                                                   taxonomy_unresolved_label_queue.occurrence_count),
            resolver_status            = EXCLUDED.resolver_status,
            target_cluster_id          = EXCLUDED.target_cluster_id,
            target_display_name        = EXCLUDED.target_display_name,
            similarity_score           = EXCLUDED.similarity_score,
            actor_guard_status         = EXCLUDED.actor_guard_status,
            contradiction_guard_status = EXCLUDED.contradiction_guard_status,
            last_seen_at               = EXCLUDED.last_seen_at,
            evidence_json              = EXCLUDED.evidence_json,
            updated_at                 = EXCLUDED.updated_at
        """, rows, page_size=500)
    conn.commit()


def parse_args(argv=None):
    p = argparse.ArgumentParser(description="Standalone deterministic STAGE taxonomy cleanup autopilot.")
    p.add_argument("--apply", action="store_true", default=False, help="Write LOCAL taxonomy changes and STAGE updates. Default is dry-run.")
    p.add_argument("--fields", default=",".join(DEFAULT_FIELDS), help="Comma-separated fields to process.")
    p.add_argument("--stage-table", default=DEFAULT_STAGE_TABLE)
    p.add_argument("--id-column", default=DEFAULT_ID_COLUMN)
    p.add_argument("--workers", type=int, default=DEFAULT_WORKERS)
    p.add_argument("--batch-size", type=int, default=DEFAULT_BATCH_SIZE)
    p.add_argument("--update-page-size", type=int, default=DEFAULT_UPDATE_PAGE_SIZE)
    p.add_argument("--progress-every", type=int, default=100000)
    p.add_argument("--audit-dir", default=DEFAULT_AUDIT_DIR)
    p.add_argument("--historical-unresolved-csv", required=True, help="CSV containing historical unresolved labels: field_name,label,count")
    p.add_argument("--local-database-url", default=build_local_dsn())
    p.add_argument("--stage-database-url", default=build_stage_dsn())
    p.add_argument("--embedding-model", default=os.getenv("EMBEDDING_MODEL", DEFAULT_EMBEDDING_MODEL))
    p.add_argument("--embedding-device", default=os.getenv("EMBEDDING_DEVICE", "cuda"), choices=["cuda", "cpu", "openvino"])
    p.add_argument("--embedding-batch-size", type=int, default=DEFAULT_EMBEDDING_BATCH_SIZE)
    p.add_argument("--semantic-threshold", type=float, default=DEFAULT_SEMANTIC_THRESHOLD, help="Minimum cosine similarity for semantic mapping to an existing cluster.")
    p.add_argument("--semantic-top-margin", type=float, default=DEFAULT_SEMANTIC_TOP_MARGIN, help="Required margin between best and second semantic match.")
    p.add_argument("--text-mode", default="field_label", choices=["field_label", "label_only"], help="Embedding text mode. field_label adds business field context and coaching/next-step awareness.")
    p.add_argument("--standard-naming-method", choices=["gpt", "deterministic"], default=DEFAULT_STANDARD_NAMING_METHOD, help="Naming method for created/promoted standard clusters. GPT is used on apply when OPENAI_API_KEY is set; deterministic fallback is used otherwise.")
    p.add_argument("--gpt-naming-model", default=DEFAULT_GPT_NAMING_MODEL)
    p.add_argument("--gpt-name-max-words", type=int, default=DEFAULT_GPT_NAME_MAX_WORDS)
    p.add_argument("--gpt-name-max-retries", type=int, default=DEFAULT_GPT_NAME_MAX_RETRIES)
    p.add_argument("--log-level", default="INFO", choices=["DEBUG", "INFO", "WARNING", "ERROR"])
    args = p.parse_args(argv)

    args.fields = [f.strip() for f in str(args.fields).split(",") if f.strip()]
    if not args.fields:
        p.error("--fields cannot be empty")
    if not args.local_database_url:
        p.error("No LOCAL DB DSN found. Set LOCAL_DATABASE_URL or LOCAL_PG_*")
    if not args.stage_database_url:
        p.error("No STAGE DB DSN found. Set STAGE_DATABASE_URL or DWH_*")
    if args.workers < 1:
        p.error("--workers must be >= 1")
    return args


def main(argv=None) -> int:
    args = parse_args(argv)
    logging.basicConfig(
        level=getattr(logging, args.log_level),
        format="%(asctime)s | %(levelname)s | %(name)s | %(message)s",
    )

    run_start = time.monotonic()
    clean_version = f"clean_auto_{datetime.now(timezone.utc).strftime('%Y%m%d')}"
    backfill_run_id = f"auto_{clean_version}_{stable_hash(args.stage_table, ','.join(args.fields), now_stamp(), length=8)}"
    log.info("Starting clean taxonomy autopilot apply=%s fields=%s run_id=%s", args.apply, args.fields, backfill_run_id)

    # Load local taxonomy references.
    local_conn = connect(args.local_database_url)
    try:
        historical_counts = load_historical_counts(args.historical_unresolved_csv)
        log.info("Loaded historical unresolved labels: %d", len(historical_counts))

        active_run_ids = resolve_active_run_ids(local_conn, args.fields)
        log.info("Active run IDs: %s", active_run_ids)

        cluster_refs = load_cluster_refs(local_conn, args.fields)
        log.info("Loaded active cluster refs: %d", len(cluster_refs))

        exact_index = build_exact_cluster_index(local_conn, args.fields, cluster_refs)
        log.info("Built exact taxonomy index: %d keys", len(exact_index))

        semantic_index = build_semantic_cluster_index(cluster_refs)
        log.info("Built semantic centroid index for %d fields / %d clusters", len(semantic_index), sum(len(v["refs"]) for v in semantic_index.values()))

        try:
            ensure_backfill_tables(local_conn)
        except Exception as exc:
            log.warning("Could not create backfill monitoring tables: %s", exc)
            local_conn.rollback()
    finally:
        local_conn.close()

    min_id, max_id, total_rows = get_stage_bounds(args.stage_database_url, args.stage_table, args.id_column)
    if total_rows <= 0:
        raise RuntimeError("STAGE table has no rows to process.")
    ranges = make_id_ranges(min_id, max_id, args.workers)
    log.info("STAGE bounds id=%s..%s rows=%s ranges=%s", min_id, max_id, total_rows, ranges)

    # Write RUNNING run row before heavy work begins.
    _telem_conn = connect(args.local_database_url)
    try:
        write_backfill_run_start(_telem_conn, backfill_run_id, args, total_rows)
    except Exception as exc:
        log.warning("Could not write backfill run start record: %s", exc)
        _telem_conn.rollback()
    finally:
        _telem_conn.close()

    run_error: Optional[str] = None
    update_counts: Dict[str, int] = {}
    decisions_detail: List[LabelDecision] = []
    decisions_map: Dict[Tuple[str, str], LabelDecision] = {}
    local_counts: Dict[str, int] = {}

    try:
        label_counts = run_discovery(args, ranges)
        # Embed all unique normalized labels once on the requested device.
        # These embeddings are used both for semantic existing-cluster matching and
        # for centroid metadata when creating new standard/anomaly clusters.
        grouped_for_embedding: Dict[Tuple[str, str], str] = {}
        for (field, raw, norm), count in label_counts.items():
            key = (field, norm)
            if key not in grouped_for_embedding:
                grouped_for_embedding[key] = raw

        key_to_embedding_text: Dict[Tuple[str, str], str] = {
            key: build_embedding_text(key[0], raw, args.text_mode)
            for key, raw in grouped_for_embedding.items()
        }
        embedding_texts = sorted(set(key_to_embedding_text.values()))
        log.info("Embedding %d unique discovered labels for semantic matching using %s on %s text_mode=%s", len(embedding_texts), args.embedding_model, args.embedding_device, args.text_mode)
        model = load_embedding_model(args.embedding_model, args.embedding_device)
        raw_embedding_map = embed_texts(model, embedding_texts, args.embedding_batch_size)
        semantic_embeddings: Dict[Tuple[str, str], List[float]] = {}
        for key, text in key_to_embedding_text.items():
            emb = raw_embedding_map.get(text)
            if emb is not None:
                semantic_embeddings[key] = emb

        decisions_map, decisions_detail = build_decisions(
            label_counts=label_counts,
            exact_index=exact_index,
            semantic_index=semantic_index,
            semantic_embeddings=semantic_embeddings,
            historical_counts=historical_counts,
            active_run_ids=active_run_ids,
            clean_version=clean_version,
            semantic_threshold=args.semantic_threshold,
            semantic_top_margin=args.semantic_top_margin,
            text_mode=args.text_mode,
        )

        decision_counts = Counter(d.action for d in decisions_detail)
        log.info("Decision counts: %s", dict(decision_counts))

        load_or_build_embeddings(args, decisions_detail)

        local_counts = materialize_local_taxonomy(args.local_database_url, decisions_detail, dry_run=not args.apply, args=args)

        update_counts = run_stage_update(args, ranges, decisions_map)

    except Exception as exc:
        run_error = str(exc)
        log.exception("Run failed: %s", exc)

    summary = {
        "run_timestamp": now_stamp(),
        "apply": args.apply,
        "fields": args.fields,
        "stage_table": args.stage_table,
        "id_column": args.id_column,
        "workers": args.workers,
        "batch_size": args.batch_size,
        "update_page_size": args.update_page_size,
        "embedding_model": args.embedding_model,
        "embedding_device": args.embedding_device,
        "semantic_threshold": args.semantic_threshold,
        "semantic_top_margin": args.semantic_top_margin,
        "text_mode": args.text_mode,
        "standard_naming_method": args.standard_naming_method,
        "gpt_naming_model": args.gpt_naming_model,
        "historical_unresolved_csv": args.historical_unresolved_csv,
        "stage_bounds": {"min_id": min_id, "max_id": max_id, "total_rows": total_rows},
        "historical_label_count": len(historical_counts),
        "unique_raw_variants": len(label_counts) if not run_error else 0,
        "unique_normalized_decisions": len(decisions_detail),
        "decision_counts": dict(Counter(d.action for d in decisions_detail)),
        "local_materialization_counts": local_counts,
        "stage_update_counts": update_counts,
        "elapsed_sec": round(time.monotonic() - run_start, 1),
    }

    # Write telemetry to backfill monitoring tables.
    _telem_conn = connect(args.local_database_url)
    try:
        if decisions_detail:
            write_backfill_field_audit(_telem_conn, backfill_run_id, decisions_detail)
            write_unresolved_queue(_telem_conn, decisions_detail, applied=args.apply and not run_error)
        write_backfill_run_finish(
            _telem_conn,
            backfill_run_id,
            update_counts,
            total_rows,
            dry_run=not args.apply,
            summary=summary,
            error_message=run_error,
        )
        log.info("Backfill telemetry written for run_id=%s", backfill_run_id)
    except Exception as exc:
        log.warning("Could not write backfill telemetry: %s", exc)
        _telem_conn.rollback()
    finally:
        _telem_conn.close()

    if run_error:
        raise RuntimeError(run_error)

    summary_path = write_summary(args, summary, decisions_detail)
    log.info("Audit summary written to %s", summary_path)
    print(json.dumps(summary, indent=2, default=str))
    return 0


if __name__ == "__main__":
    raise SystemExit(main())
