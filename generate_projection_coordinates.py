#!/usr/bin/env python
"""
Generate persisted semantic projection coordinates from taxonomy cluster centroids.

The projection is computed globally across fields so related taxonomy fields can
overlap naturally in the same embedding universe. The script does not radialize,
field-separate, or sphere-normalize coordinates.
"""

from __future__ import annotations

import argparse
import json
import os

import numpy as np
import psycopg2
from dotenv import load_dotenv
from psycopg2.extras import execute_values


VALID_METHODS = {"umap", "umap_2d", "umap_3d", "tsne", "pca"}


def parse_args() -> argparse.Namespace:
    parser = argparse.ArgumentParser(description="Persist UMAP/t-SNE/PCA coordinates for semantic clusters.")
    parser.add_argument("--methods", default="umap_2d,umap_3d,pca", help="Comma-separated methods: umap_2d,umap_3d,tsne,pca")
    parser.add_argument("--embedding-model", default=os.getenv("EMBEDDING_MODEL", ""), help="Embedding model label to persist.")
    parser.add_argument("--source-run-id", default="", help="Optional taxonomy_clusters.run_id filter.")
    parser.add_argument("--projection-run-id", default="", help="Projection run_id to persist. Defaults to each cluster run_id.")
    parser.add_argument("--limit", type=int, default=0, help="Optional row limit for testing.")
    parser.add_argument("--umap-neighbors", type=int, default=30)
    parser.add_argument("--umap-min-dist", type=float, default=0.08)
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
          projection_method TEXT NOT NULL CHECK (projection_method IN ('umap','umap_2d','umap_3d','tsne','pca')),
          x DOUBLE PRECISION NOT NULL,
          y DOUBLE PRECISION NOT NULL,
          z DOUBLE PRECISION NOT NULL,
          embedding_model TEXT,
          run_id TEXT NOT NULL DEFAULT '',
          created_at TIMESTAMP WITHOUT TIME ZONE DEFAULT NOW()
        )
        """
    )
    # Expand the CHECK constraint on existing tables so umap_2d / umap_3d are accepted.
    cur.execute(
        """
        DO $$
        BEGIN
          ALTER TABLE semantic_projection_coordinates
            DROP CONSTRAINT IF EXISTS semantic_projection_coordinates_projection_method_check;
          ALTER TABLE semantic_projection_coordinates
            ADD CONSTRAINT semantic_projection_coordinates_projection_method_check
            CHECK (projection_method IN ('umap','umap_2d','umap_3d','tsne','pca'));
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


def pad_3d(coords: np.ndarray) -> np.ndarray:
    if coords.shape[1] >= 3:
        return coords[:, :3]
    return np.pad(coords, ((0, 0), (0, 3 - coords.shape[1])), mode="constant")


def compute_projection(method: str, vectors: np.ndarray, args: argparse.Namespace) -> np.ndarray:
    norms = np.linalg.norm(vectors, axis=1, keepdims=True)
    vectors = vectors / np.maximum(norms, 1e-12)
    n = vectors.shape[0]
    if method == "pca":
        from sklearn.decomposition import PCA

        components = min(3, vectors.shape[0], vectors.shape[1])
        return pad_3d(PCA(n_components=components, random_state=args.random_state).fit_transform(vectors))
    if method in ("umap", "umap_2d", "umap_3d"):
        try:
            import umap
        except ImportError as exc:
            raise RuntimeError("umap-learn is not installed. Run with --methods pca or install requirements.txt.") from exc
        n_components = 2 if method == "umap_2d" else 3
        neighbors = min(max(2, args.umap_neighbors), max(2, n - 1))
        reducer = umap.UMAP(
            n_components=n_components,
            n_neighbors=neighbors,
            min_dist=args.umap_min_dist,
            metric="cosine",
            random_state=args.random_state,
        )
        coords = reducer.fit_transform(vectors)
        return pad_3d(coords)  # umap_2d gets z=0; umap_3d already has 3 cols
    if method == "tsne":
        from sklearn.manifold import TSNE

        perplexity = min(args.tsne_perplexity, max(2.0, (n - 1) / 3))
        reducer = TSNE(
            n_components=3,
            perplexity=perplexity,
            metric="cosine",
            init="pca",
            learning_rate="auto",
            random_state=args.random_state,
        )
        return reducer.fit_transform(vectors)
    raise ValueError(f"Unsupported projection method: {method}")


def persist_projection(cur, rows: list[dict], coords: np.ndarray, method: str, embedding_model: str, projection_run_id: str) -> None:
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
    return methods or ["umap_2d", "umap_3d", "pca"]


def main() -> None:
    args = parse_args()
    methods = parse_methods(args.methods)
    with connect() as conn:
        with conn.cursor() as cur:
            ensure_table(cur)
            rows, vectors = load_clusters(cur, args.source_run_id, args.limit)
            print(f"Loaded {len(rows):,} centroid embeddings with {vectors.shape[1]} dimensions.")
            for method in methods:
                print(f"Computing {method.upper()} projection...")
                coords = compute_projection(method, vectors, args)
                persist_projection(cur, rows, coords, method, args.embedding_model, args.projection_run_id)
                conn.commit()
                print(f"Persisted {len(rows):,} {method.upper()} coordinates.")


if __name__ == "__main__":
    main()
