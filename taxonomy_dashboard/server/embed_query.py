#!/usr/bin/env python3
"""Embed a dashboard semantic-search query and return JSON on stdout."""

import json
import os
import sys


def main() -> int:
    query = " ".join(sys.argv[1:]).strip()
    if not query:
        payload = {"error": "Missing query text"}
        print(json.dumps(payload))
        return 2

    model_name = os.environ.get("SEMANTIC_SEARCH_MODEL", "all-MiniLM-L6-v2")
    cache_folder = os.environ.get("SENTENCE_TRANSFORMERS_HOME") or os.environ.get("HF_HOME")

    try:
        from sentence_transformers import SentenceTransformer
    except Exception as exc:
        payload = {
            "error": "sentence-transformers is not installed in the Python environment used by the dashboard API",
            "detail": str(exc),
        }
        print(json.dumps(payload))
        return 3

    try:
        kwargs = {"cache_folder": cache_folder} if cache_folder else {}
        model = SentenceTransformer(model_name, **kwargs)
        vec = model.encode([query], normalize_embeddings=True, show_progress_bar=False)[0]
        embedding = [float(x) for x in vec.tolist()]
        print(json.dumps({
            "model": model_name,
            "dimension": len(embedding),
            "embedding": embedding,
        }))
        return 0
    except Exception as exc:
        payload = {
            "error": "Failed to embed semantic-search query",
            "model": model_name,
            "detail": str(exc),
        }
        print(json.dumps(payload))
        return 4


if __name__ == "__main__":
    raise SystemExit(main())
