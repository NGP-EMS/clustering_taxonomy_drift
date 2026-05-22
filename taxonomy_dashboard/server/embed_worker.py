#!/usr/bin/env python3
"""Persistent embedding worker for dashboard semantic search.

Protocol:
- stdout emits one JSON object per line.
- first line: {"type": "ready", ...}
- stdin accepts: {"id": "1", "query": "aggressive"}
- stdout responds: {"id": "1", "embedding": [...], ...}
"""

import json
import os
import sys
import traceback


def emit(payload):
    print(json.dumps(payload), flush=True)


def log(message):
    print(str(message), file=sys.stderr, flush=True)


def main() -> int:
    model_name = os.environ.get("SEMANTIC_SEARCH_MODEL", "all-MiniLM-L6-v2")
    cache_folder = os.environ.get("SENTENCE_TRANSFORMERS_HOME") or os.environ.get("HF_HOME")

    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:
        emit({
            "type": "error",
            "error": "sentence-transformers is not installed in the Python environment used by the dashboard API",
            "detail": str(exc),
        })
        return 3

    try:
        kwargs = {"cache_folder": cache_folder} if cache_folder else {}
        log(f"Loading semantic search model: {model_name}")
        model = SentenceTransformer(model_name, **kwargs)

        # Warm up the model once so later requests are fast and deterministic.
        warmup = model.encode(["semantic search warmup"], normalize_embeddings=True, show_progress_bar=False)[0]
        emit({
            "type": "ready",
            "model": model_name,
            "dimension": int(len(warmup)),
        })
    except Exception as exc:
        emit({
            "type": "error",
            "error": "Failed to initialize semantic-search embedding worker",
            "model": model_name,
            "detail": str(exc),
            "traceback": traceback.format_exc(limit=2),
        })
        return 4

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        try:
            request = json.loads(line)
            request_id = str(request.get("id") or "")
            query = str(request.get("query") or "").strip()
            if not request_id:
                emit({"error": "Missing request id"})
                continue
            if not query:
                emit({"id": request_id, "error": "Missing query text"})
                continue

            vec = model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
            embedding = [float(x) for x in vec.tolist()]
            emit({
                "id": request_id,
                "model": model_name,
                "dimension": len(embedding),
                "embedding": embedding,
            })
        except Exception as exc:
            emit({
                "id": str(locals().get("request_id", "")),
                "error": "Failed to embed semantic-search query",
                "detail": str(exc),
                "traceback": traceback.format_exc(limit=2),
            })

    return 0


if __name__ == "__main__":
    raise SystemExit(main())
