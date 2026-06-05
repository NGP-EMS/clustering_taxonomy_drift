#!/usr/bin/env python
"""
Generate persisted semantic projection coordinates from taxonomy cluster centroids.

Methods
-------
umap_2d            Standard 2-D UMAP over MiniLM centroid embeddings.
umap_3d            Standard 3-D UMAP over MiniLM centroid embeddings.
umap_2d_cloud      2-D UMAP over MiniLM embeddings with per-field bias removed
                   (n_neighbors=100, min_dist=0.40).
umap_2d_legacy_cloud  2-D UMAP over BGE-M3 label embeddings aggregated to cluster
                   level. Uses the existing .bge_cache/bge_index.pkl produced by
                   bge_voyage_worker. Falls back to centroid_embedding if cache
                   is missing. (n_neighbors=50, min_dist=0.10)
umap_2d_compact_cloud  2-D UMAP over centroid embeddings with strong global params
                   (n_neighbors=500, min_dist=0.80) — maximum field blending.
pca                PCA 3-D projection.
tsne               t-SNE 3-D projection.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values


VALID_METHODS = {
    "umap", "umap_2d", "umap_3d",
    "umap_2d_cloud", "umap_2d_legacy_cloud", "umap_2d_compact_cloud",
    "tsne", "pca",
}

_DEFAULT_BGE_CACHE = os.path.join(
    os.path.dirname(__file__),
    "taxonomy_dashboard", "server", ".bge_cache", "bge_index.pkl",
)


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persist UMAP/t-SNE/PCA coordinates for semantic clusters.")
    parser.add_argument("--methods", default="umap_2d,umap_2d_cloud,umap_2d_legacy_cloud,umap_2d_compact_cloud,umap_3d,pca",
                        help="Comma-separated projection methods to compute and persist.")
    parser.add_argument("--embedding-model", default=os.getenv("EMBEDDING_MODEL", ""), help="Embedding model label to persist.")
    parser.add_argument("--source-run-id", default="", help="Optional taxonomy_clusters.run_id filter.")
    parser.add_argument("--projection-run-id", default="", help="Projection run_id to persist. Defaults to each cluster run_id.")
    parser.add_argument("--limit", type=int, default=0, help="Optional row limit for testing.")
    parser.add_argument("--umap-neighbors", type=int, default=30)
    parser.add_argument("--umap-min-dist", type=float, default=0.08)
    parser.add_argument("--umap-cloud-neighbors", type=int, default=100,
                        help="n_neighbors for umap_2d_cloud (default 100)")
    parser.add_argument("--umap-cloud-min-dist", type=float, default=0.40,
                        help="min_dist for umap_2d_cloud (default 0.40)")
    parser.add_argument("--bge-cache", default=_DEFAULT_BGE_CACHE,
                        help="Path to bge_index.pkl for umap_2d_legacy_cloud.")
    parser.add_argument("--tsne-perplexity", type=float, default=30.0)
    parser.add_argument("--random-state", type=int, default=42)
    return parser.parse_args()


def connect():
    load_dotenv()
    return psycopg2.connect(
        host=os.getenv("LOCAL_PG_HOST", "127.0.0.1"),
        port=int(os.getenv("LOCAL_PG_PORT", "5432")),
        dbname=os.getenv("LOCAL_PG_DB"),
        user=os.getenv("LOCAL_PG_USER"),
        password=os.getenv("LOCAL_PG_PASSWORD"),
    )


def table_columns(cur, table: str) -> set[str]:
    cur.execute(
        """
        SELECT column_name
        FROM information_schema.columns
        WHERE table_schema = 'public' AND table_name = %s
        """,
        (table,),
    )
    return {row[0] for row in cur.fetchall()}


def ensure_table(cur) -> None:
    cur.execute(
        """
        CREATE TABLE IF NOT EXISTS semantic_projection_coordinates (
          id BIGSERIAL PRIMARY KEY,
          field_name TEXT NOT NULL,
          cluster_id TEXT NOT NULL,
          projection_method TEXT NOT NULL,
          x DOUBLE PRECISION NOT NULL,
          y DOUBLE PRECISION NOT NULL,
          z DOUBLE PRECISION NOT NULL,
          embedding_model TEXT,
          run_id TEXT NOT NULL DEFAULT '',
          created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
        """
    )
    # Drop old CHECK constraint (it enumerated methods and blocks new ones).
    cur.execute(
        """
        DO $$
        BEGIN
          ALTER TABLE semantic_projection_coordinates
            DROP CONSTRAINT IF EXISTS semantic_projection_coordinates_projection_method_check;
        EXCEPTION WHEN others THEN NULL;
        END $$
        """
    )
    cur.execute("CREATE INDEX IF NOT EXISTS idx_semantic_projection_field_name ON semantic_projection_coordinates(field_name)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_semantic_projection_cluster_id ON semantic_projection_coordinates(cluster_id)")
    cur.execute("CREATE INDEX IF NOT EXISTS idx_semantic_projection_method ON semantic_projection_coordinates(projection_method)")
    cur.execute(
        """
        CREATE UNIQUE INDEX IF NOT EXISTS ux_semantic_projection_coordinates_key
        ON semantic_projection_coordinates(field_name, cluster_id, projection_method, run_id)
        """
    )


def parse_embedding(value) -> list[float] | None:
    if value is None:
        return None
    if isinstance(value, list):
        arr = value
    elif isinstance(value, str):
        try:
            arr = json.loads(value)
        except json.JSONDecodeError:
            return None
    else:
        return None
    if not isinstance(arr, list) or not arr:
        return None
    nums = []
    for v in arr:
        try:
            n = float(v)
        except (TypeError, ValueError):
            continue
        if np.isfinite(n):
            nums.append(n)
    return nums if nums else None


def load_clusters(cur, source_run_id: str, limit: int) -> tuple[list[dict], np.ndarray]:
    cols = table_columns(cur, "taxonomy_clusters")
    if "centroid_embedding" not in cols:
        raise RuntimeError("taxonomy_clusters.centroid_embedding does not exist.")

    fields = ["id", "field_name", "cluster_id", "centroid_embedding::text"]
    fields.append("COALESCE(run_id, '') AS run_id" if "run_id" in cols else "'' AS run_id")
    conditions = ["centroid_embedding IS NOT NULL"]
    params: list[object] = []
    if "is_active" in cols:
        conditions.append("(is_active = true OR is_active IS NULL)")
    if source_run_id and "run_id" in cols:
        params.append(source_run_id)
        conditions.append(f"run_id = %s")
    sql = f"""
      SELECT {", ".join(fields)}
      FROM taxonomy_clusters
      WHERE {" AND ".join(conditions)}
      ORDER BY field_name, cluster_id
    """
    if limit and limit > 0:
        sql += " LIMIT %s"
        params.append(limit)

    cur.execute(sql, params)
    rows = []
    vectors = []
    dims = None
    for row in cur.fetchall():
        emb = parse_embedding(row[3])
        if not emb:
            continue
        if dims is None:
            dims = len(emb)
        if len(emb) != dims:
            continue
        rows.append({"id": row[0], "field_name": row[1], "cluster_id": row[2], "run_id": row[4] or ""})
        vectors.append(emb)
    if not rows:
        raise RuntimeError("No centroid embeddings were available to project.")
    return rows, np.asarray(vectors, dtype=np.float32)


def load_bge_cluster_embeddings(cache_path: str, rows: list[dict]) -> np.ndarray | None:
    """
    Aggregate BGE-M3 label embeddings from the FAISS cache into one vector per cluster.

    The cache (bge_index.pkl) was built by bge_voyage_worker and stores one BGE-M3
    embedding per raw label, with the associated cluster_id and value_count in the
    rows list.  We compute a value_count-weighted mean per cluster and return the
    vectors in the same order as `rows` (matched by field_name + cluster_id).
    """
    if not os.path.exists(cache_path):
        print(f"  [bge] Cache not found at {cache_path}, falling back to centroid embeddings.")
        return None
    try:
        import pickle
        import faiss  # type: ignore
        print(f"  [bge] Loading cache ({os.path.getsize(cache_path) / 1e9:.1f} GB)…")
        with open(cache_path, "rb") as f:
            obj = pickle.load(f)
        index: faiss.IndexFlatIP = obj["index"]
        cache_rows: list[dict] = obj["rows"]
        n_vecs = index.ntotal
        dim = index.d
        print(f"  [bge] Extracting {n_vecs:,} × {dim}-dim vectors…")
        all_vecs = np.zeros((n_vecs, dim), dtype=np.float32)
        index.reconstruct_n(0, n_vecs, all_vecs)

        # Build weighted sum per (field_name, cluster_id)
        from collections import defaultdict
        acc_sum: dict[str, np.ndarray] = defaultdict(lambda: np.zeros(dim, dtype=np.float64))
        acc_weight: dict[str, float] = defaultdict(float)
        for i, cr in enumerate(cache_rows):
            key = f"{cr.get('field_name','')}::{cr.get('cluster_id','')}"
            w = float(cr.get("value_count") or 1)
            acc_sum[key] += all_vecs[i].astype(np.float64) * w
            acc_weight[key] += w

        # Map to rows in the same order as load_clusters output
        n = len(rows)
        out = np.zeros((n, dim), dtype=np.float32)
        missing = 0
        for i, row in enumerate(rows):
            key = f"{row['field_name']}::{row['cluster_id']}"
            w = acc_weight.get(key, 0.0)
            if w < 1e-12:
                missing += 1
                # Zero vector will be handled gracefully by normalisation (→ stays near origin)
                continue
            v = (acc_sum[key] / w).astype(np.float32)
            norm = np.linalg.norm(v)
            out[i] = v / max(norm, 1e-12)
        if missing:
            print(f"  [bge] Warning: {missing}/{n} clusters had no BGE label rows, using zero fallback.")
        print(f"  [bge] Aggregated {n - missing:,}/{n} clusters with BGE-M3 embeddings.")
        return out
    except Exception as exc:
        print(f"  [bge] Failed to load cache: {exc}. Falling back to centroid embeddings.")
        return None


def pad_3d(coords: np.ndarray) -> np.ndarray:
    if coords.shape[1] >= 3:
        return coords[:, :3]
    return np.pad(coords, ((0, 0), (0, 3 - coords.shape[1])), mode="constant")


def remove_field_bias(vectors: np.ndarray, field_names: list[str]) -> np.ndarray:
    """
    Remove the per-field mean direction from each cluster's unit-sphere vector.
    This de-correlates the field-name signal baked into MiniLM centroid embeddings,
    allowing UMAP to see cross-field semantic similarity.
    """
    result = vectors.copy()
    for field in set(field_names):
        idx = [i for i, f in enumerate(field_names) if f == field]
        if len(idx) < 2:
            continue
        field_vecs = result[idx]
        field_mean = field_vecs.mean(axis=0)
        mean_norm = np.linalg.norm(field_mean)
        if mean_norm < 1e-10:
            continue
        field_mean_unit = field_mean / mean_norm
        projections = field_vecs.dot(field_mean_unit)[:, np.newaxis]
        result[idx] = field_vecs - projections * field_mean_unit
    norms = np.linalg.norm(result, axis=1, keepdims=True)
    return result / np.maximum(norms, 1e-12)


def _run_umap(vectors, n_components, n_neighbors, min_dist, random_state):
    try:
        import umap  # type: ignore
    except ImportError as exc:
        raise RuntimeError("umap-learn is not installed.") from exc
    n = vectors.shape[0]
    neighbors = min(max(2, n_neighbors), max(2, n - 1))
    reducer = umap.UMAP(
        n_components=n_components,
        n_neighbors=neighbors,
        min_dist=min_dist,
        metric="cosine",
        random_state=random_state,
    )
    return pad_3d(reducer.fit_transform(vectors))


def compute_projection(method: str, vectors: np.ndarray, args: argparse.Namespace,
                       rows: list[dict] | None = None) -> np.ndarray:
    # L2-normalise to unit sphere for cosine-based methods
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / np.maximum(norms, 1e-12)
    n = vectors.shape[0]

    # ── PCA ──────────────────────────────────────────────────────────────────
    if method == "pca":
        from sklearn.decomposition import PCA
        components = min(3, n, vectors.shape[1])
        return pad_3d(PCA(n_components=components, random_state=args.random_state).fit_transform(vectors))

    # ── t-SNE ─────────────────────────────────────────────────────────────────
    if method == "tsne":
        from sklearn.manifold import TSNE
        perplexity = min(args.tsne_perplexity, max(2.0, (n - 1) / 3))
        return TSNE(n_components=3, perplexity=perplexity, metric="cosine",
                    init="pca", learning_rate="auto",
                    random_state=args.random_state).fit_transform(vectors)

    # ── Standard UMAP (umap / umap_2d / umap_3d) ─────────────────────────────
    if method in ("umap", "umap_2d", "umap_3d"):
        n_components = 2 if method in ("umap", "umap_2d") else 3
        return _run_umap(vectors, n_components,
                         args.umap_neighbors, args.umap_min_dist, args.random_state)

    # ── Cloud UMAP: centroid embeddings, field-bias removed ───────────────────
    if method == "umap_2d_cloud":
        if rows is not None:
            field_names = [r["field_name"] for r in rows]
            print(f"  Removing per-field embedding bias across {len(set(field_names))} fields…")
            vectors = remove_field_bias(vectors, field_names)
        print(f"  n_neighbors={args.umap_cloud_neighbors} min_dist={args.umap_cloud_min_dist}")
        return _run_umap(vectors, 2, args.umap_cloud_neighbors, args.umap_cloud_min_dist, args.random_state)

    # ── Legacy cloud: BGE-M3 aggregated label embeddings ─────────────────────
    if method == "umap_2d_legacy_cloud":
        bge_vecs = load_bge_cluster_embeddings(args.bge_cache, rows or [])
        if bge_vecs is not None:
            # BGE-M3 embeddings are already normalised; use them directly
            vectors = bge_vecs
            print(f"  Using BGE-M3 aggregated embeddings (dim={vectors.shape[1]})")
        else:
            print("  Falling back to MiniLM centroid embeddings for legacy cloud.")
        # Moderate global params — close to original 'umap' defaults but slightly broader
        n_nb = min(max(2, 50), max(2, n - 1))
        print(f"  n_neighbors={n_nb} min_dist=0.10")
        return _run_umap(vectors, 2, n_nb, 0.10, args.random_state)

    # ── Compact cloud: extreme global params, strong field blending ───────────
    if method == "umap_2d_compact_cloud":
        if rows is not None:
            field_names = [r["field_name"] for r in rows]
            print(f"  Removing per-field embedding bias across {len(set(field_names))} fields…")
            vectors = remove_field_bias(vectors, field_names)
        n_nb = min(500, max(2, n - 1))
        print(f"  n_neighbors={n_nb} min_dist=0.80")
        return _run_umap(vectors, 2, n_nb, 0.80, args.random_state)

    raise ValueError(f"Unsupported projection method: {method}")


def persist_projection(cur, rows: list[dict], coords: np.ndarray, method: str,
                       embedding_model: str, projection_run_id: str) -> None:
    payload = []
    for row, xyz in zip(rows, coords):
        run_id = projection_run_id or row["run_id"] or ""
        payload.append((
            row["field_name"],
            row["cluster_id"],
            method,
            float(xyz[0]),
            float(xyz[1]),
            float(xyz[2]),
            embedding_model or None,
            run_id,
        ))
    execute_values(
        cur,
        """
        INSERT INTO semantic_projection_coordinates
          (field_name, cluster_id, projection_method, x, y, z, embedding_model, run_id)
        VALUES %s
        ON CONFLICT (field_name, cluster_id, projection_method, run_id)
        DO UPDATE SET
          x = EXCLUDED.x,
          y = EXCLUDED.y,
          z = EXCLUDED.z,
          embedding_model = EXCLUDED.embedding_model,
          created_at = NOW()
        """,
        payload,
        page_size=1000,
    )


def parse_methods(value: str) -> list[str]:
    methods = [m.strip().lower().replace("t-sne", "tsne") for m in value.split(",") if m.strip()]
    invalid = [m for m in methods if m not in VALID_METHODS]
    if invalid:
        raise ValueError(f"Invalid projection method(s): {', '.join(invalid)}")
    return methods or ["umap_2d", "umap_2d_cloud", "umap_2d_legacy_cloud", "umap_3d", "pca"]


def main() -> None:
    args = parse_args()
    methods = parse_methods(args.methods)
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_table(cur)
            rows, vectors = load_clusters(cur, args.source_run_id, args.limit)
            print(f"Loaded {len(rows):,} centroid embeddings ({vectors.shape[1]}-dim).")
            for method in methods:
                print(f"\nComputing {method.upper()}…")
                coords = compute_projection(method, vectors, args, rows)
                persist_projection(cur, rows, coords, method, args.embedding_model, args.projection_run_id)
                conn.commit()
                print(f"Persisted {len(rows):,} {method.upper()} coordinates.")


if __name__ == "__main__":
    main()
