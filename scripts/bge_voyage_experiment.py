#!/usr/bin/env python3
"""
BGE-M3 + Voyage rerank-2.5-lite offline experiment.

Compares BGE-M3 dense retrieval + Voyage cross-encoder reranking against
the current all-MiniLM-L6-v2 cosine pipeline for taxonomy semantic search.

Usage:
    python scripts/bge_voyage_experiment.py
    python scripts/bge_voyage_experiment.py --rebuild
    python scripts/bge_voyage_experiment.py --compare
    python scripts/bge_voyage_experiment.py --query "agent rude"
    python scripts/bge_voyage_experiment.py --top-k 100 --top-n 5

First run downloads BAAI/bge-m3 (~1.5 GB) and builds a FAISS index of all
taxonomy member labels.  Subsequent runs load the cached index from
scripts/.bge_cache/bge_index.pkl (use --rebuild to force refresh).
"""

import argparse
import json
import os
import pickle
import sys
import time
from pathlib import Path
from typing import Optional

# ── env ───────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
load_dotenv(ROOT / 'taxonomy_dashboard' / '.env')

# ── config ────────────────────────────────────────────────────────────────────
BGE_MODEL           = os.getenv('BGE_MODEL', 'BAAI/bge-m3')
VOYAGE_RERANK_MODEL = os.getenv('VOYAGE_RERANK_MODEL', 'rerank-2.5-lite')
TOP_K_RETRIEVE      = int(os.getenv('BGE_TOP_K_RETRIEVE', '200'))
CACHE_PATH          = ROOT / 'scripts' / '.bge_cache' / 'bge_index.pkl'
VOYAGE_API_KEY      = os.getenv('VOYAGE_API_KEY')
BGE_DEVICE          = os.getenv('BGE_DEVICE', 'cuda')
BGE_BATCH_SIZE      = int(os.getenv('BGE_BATCH_SIZE', '4'))

THRESHOLDS = {
    'confident': 0.6,
    'possible':  0.4,
}

TEST_QUERIES = [
    'agent rude',
    'loa not send',
    'customer shouting',
    'robotic audio',
    'dm unavailable',
    'contract review customer contact',
]

# ── DB ────────────────────────────────────────────────────────────────────────
import psycopg2

LABEL_SQL = """
    SELECT
        lm.id                                  AS label_id,
        lm.field_name,
        lm.raw_label,
        lm.normalized_label,
        lm.final_cluster_id                    AS cluster_id,
        lm.cluster_version,
        COALESCE(lm.value_count, 1)            AS value_count,
        tc.id                                  AS cluster_db_id,
        COALESCE(tc.run_id, '')                AS run_id,
        tc.medoid_label,
        tc.cluster_size,
        tc.total_occurrences,
        COALESCE(tc.is_true_anomaly_cluster, FALSE) AS is_true_anomaly,
        tcn.display_name,
        tcn.naming_method
    FROM taxonomy_label_cluster_map lm
    LEFT JOIN taxonomy_clusters tc
        ON  tc.field_name      = lm.field_name
        AND tc.cluster_id      = lm.final_cluster_id
        AND tc.cluster_version = lm.cluster_version
        AND tc.active          = TRUE
    LEFT JOIN taxonomy_cluster_names tcn
        ON  tcn.field_name      = lm.field_name
        AND tcn.cluster_id      = lm.final_cluster_id
        AND tcn.cluster_version = lm.cluster_version
    WHERE lm.final_cluster_id IS NOT NULL
"""


def get_conn():
    return psycopg2.connect(
        host=os.environ['LOCAL_PG_HOST'],
        port=int(os.environ.get('LOCAL_PG_PORT', '5432')),
        database=os.environ['LOCAL_PG_DB'],
        user=os.environ['LOCAL_PG_USER'],
        password=os.environ['LOCAL_PG_PASSWORD'],
    )


def load_labels(conn) -> list[dict]:
    with conn.cursor() as cur:
        cur.execute(LABEL_SQL)
        cols = [d[0] for d in cur.description]
        return [dict(zip(cols, row)) for row in cur.fetchall()]


def build_emb_text(row: dict) -> str:
    raw   = (row.get('raw_label')        or '').strip()
    norm  = (row.get('normalized_label') or '').strip()
    disp  = (row.get('display_name')     or '').strip()
    medo  = (row.get('medoid_label')     or '').strip()
    field = (row.get('field_name')       or '').strip()
    parts = []
    if raw:                                parts.append(raw)
    if norm and norm != raw:               parts.append(norm)
    if disp and disp not in (raw, norm):   parts.append(disp)
    if medo and medo not in (raw, norm):   parts.append(medo)
    if field:                              parts.append(field)
    return ' | '.join(parts)


# ── BGE-M3 FAISS index ────────────────────────────────────────────────────────
def get_bge_device() -> str:
    import torch

    requested = BGE_DEVICE.lower().strip()

    if requested.startswith('cuda') and torch.cuda.is_available():
        print(f'[torch] cuda available: True')
        print(f'[torch] gpu: {torch.cuda.get_device_name(0)}')
        print(f'[torch] vram gb: {round(torch.cuda.get_device_properties(0).total_memory / 1024**3, 2)}')
        return 'cuda:0'

    print('[torch] cuda available: False, using CPU')
    return 'cpu'
def build_or_load_index(rows: list[dict], rebuild: bool = False) -> dict:
    if not rebuild and CACHE_PATH.exists():
        print(f'[cache] loading from {CACHE_PATH}')
        with open(CACHE_PATH, 'rb') as f:
            return pickle.load(f)

    docs = [build_emb_text(r) for r in rows]

    print(f'[bge] loading {BGE_MODEL} (first run downloads ~1.5 GB) ...')
    from sentence_transformers import SentenceTransformer

    device = get_bge_device()
    model = SentenceTransformer(BGE_MODEL, device=device)
    if device.startswith("cuda"):
        model.half()

    model.max_seq_length = 64

    print(f'[bge] embedding {len(docs):,} documents on {device} with batch_size={BGE_BATCH_SIZE} ...')
    t0 = time.time()

    embs = model.encode(
        docs,
        batch_size=BGE_BATCH_SIZE,
        normalize_embeddings=True,
        show_progress_bar=True,
        convert_to_numpy=True,
        device=device,
    )

    print(f'[bge] done in {time.time() - t0:.1f}s')

    import faiss
    dim = embs.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embs.astype('float32'))

    cache = {
        'index': index,
        'rows': rows,
        'docs': docs,
        'model': BGE_MODEL,
        'dim': dim,
        'built_at': time.time(),
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, 'wb') as f:
        pickle.dump(cache, f)
    print(f'[cache] saved to {CACHE_PATH}')
    return cache


# ── Voyage rerank ─────────────────────────────────────────────────────────────
def voyage_rerank(query: str, candidates: list[dict], voyage_client) -> list[dict]:
    docs = [c['_emb_text'] for c in candidates]
    result = voyage_client.rerank(query, docs, model=VOYAGE_RERANK_MODEL, top_k=len(docs))
    ranked = []
    for r in result.results:
        row = dict(candidates[r.index])
        row['_voyage_score'] = float(r.relevance_score)
        ranked.append(row)
    ranked.sort(key=lambda x: x['_voyage_score'], reverse=True)
    return ranked


# ── Group by cluster ──────────────────────────────────────────────────────────
def group_by_cluster(ranked: list[dict]) -> list[dict]:
    clusters: dict = {}
    for row in ranked:
        key = (row['field_name'], row['cluster_id'], row.get('cluster_version', 'v1'))
        if key not in clusters:
            clusters[key] = {
                'field_name':       row['field_name'],
                'cluster_id':       row['cluster_id'],
                'cluster_version':  row.get('cluster_version', 'v1'),
                'display_name':     row.get('display_name'),
                'medoid_label':     row.get('medoid_label'),
                'cluster_size':     row.get('cluster_size'),
                'total_occurrences': row.get('total_occurrences'),
                'is_true_anomaly':  row.get('is_true_anomaly', False),
                'best_label':       row['raw_label'],
                'best_score':       row['_voyage_score'],
                'matched_labels':   [],
            }
        c = clusters[key]
        c['matched_labels'].append({
            'raw_label':    row['raw_label'],
            'rerank_score': round(row['_voyage_score'], 4),
            'bge_score':    round(row['_bge_score'], 4),
            'value_count':  row.get('value_count'),
        })
        if row['_voyage_score'] > c['best_score']:
            c['best_score'] = row['_voyage_score']
            c['best_label'] = row['raw_label']

    result = list(clusters.values())
    result.sort(key=lambda x: x['best_score'], reverse=True)
    return result


# ── Pretty print ──────────────────────────────────────────────────────────────
GRN   = '\033[92m'
YLW   = '\033[93m'
GRAY  = '\033[90m'
RED   = '\033[91m'
RESET = '\033[0m'


def conf_band(score: float) -> tuple[str, str]:
    if score >= THRESHOLDS['confident']:
        return 'confident', GRN
    if score >= THRESHOLDS['possible']:
        return 'possible', YLW
    return 'weak', GRAY


def print_query_results(query: str, clusters: list[dict], top_n: int = 10):
    print(f'\n{"=" * 72}')
    print(f'  QUERY: {query!r}')
    print(f'{"=" * 72}')

    shown = clusters[:top_n]
    if not shown:
        print(f'  {GRAY}No results.{RESET}')
        return

    for i, c in enumerate(shown, 1):
        score = c['best_score']
        band, color = conf_band(score)
        display = c['display_name'] or c['cluster_id']
        size = c.get('cluster_size') or '?'
        n_matched = len(c['matched_labels'])
        print(
            f'  {i:2}. {color}{score:.4f} [{band}]{RESET}  '
            f'{display}  {GRAY}[{c["field_name"]}]{RESET}'
        )
        print(f'       best_label: {c["best_label"]!r}   '
              f'cluster_size={size}   matched_labels={n_matched}')
        for m in c['matched_labels'][:3]:
            print(f'         {GRAY}{m["rerank_score"]:.4f} (bge {m["bge_score"]:.4f})  '
                  f'{m["raw_label"]!r}{RESET}')


# ── MiniLM comparison ─────────────────────────────────────────────────────────
def compare_minilm(query: str, rows: list[dict], top_n: int = 10):
    print(f'\n  {GRAY}-- MiniLM (all-MiniLM-L6-v2 + cosine) baseline --{RESET}')
    from sentence_transformers import SentenceTransformer
    import numpy as np

    model = SentenceTransformer('all-MiniLM-L6-v2')
    docs = [build_emb_text(r) for r in rows]
    qvec = model.encode([query], normalize_embeddings=True)[0]
    dvecs = model.encode(docs, batch_size=64, normalize_embeddings=True, show_progress_bar=False)
    sims = dvecs @ qvec
    idxs = sims.argsort()[::-1][:200]

    clusters: dict = {}
    for idx in idxs:
        row = rows[idx]
        key = (row['field_name'], row['cluster_id'], row.get('cluster_version', 'v1'))
        if key not in clusters:
            clusters[key] = {
                'display': row.get('display_name') or row['cluster_id'],
                'field': row['field_name'],
                'best': float(sims[idx]),
                'best_lbl': row['raw_label'],
                'n': 0,
            }
        c = clusters[key]
        c['n'] += 1
        if float(sims[idx]) > c['best']:
            c['best'] = float(sims[idx])
            c['best_lbl'] = row['raw_label']

    ranked = sorted(clusters.values(), key=lambda x: x['best'], reverse=True)[:top_n]
    for i, c in enumerate(ranked, 1):
        band, color = conf_band(c['best'])
        print(f'   {i:2}. {color}{c["best"]:.4f} [{band}]{RESET}  {c["display"]}  '
              f'{GRAY}[{c["field"]}]{RESET}')
        print(f'        best_label: {c["best_lbl"]!r}')


# ── main ──────────────────────────────────────────────────────────────────────
def main():
    parser = argparse.ArgumentParser(description='BGE-M3 + Voyage rerank experiment')
    parser.add_argument('--rebuild',  action='store_true', help='Rebuild FAISS index from DB')
    parser.add_argument('--compare',  action='store_true', help='Also run MiniLM baseline')
    parser.add_argument('--query', '-q', default=None, help='Single query (overrides test set)')
    parser.add_argument('--top-k', type=int, default=TOP_K_RETRIEVE,
                        help=f'BGE retrieval top-k (default {TOP_K_RETRIEVE})')
    parser.add_argument('--top-n', type=int, default=10,
                        help='Clusters to print per query (default 10)')
    parser.add_argument('--min-score', type=float, default=None,
                        help='Voyage score threshold for display (default: show all)')
    args = parser.parse_args()

    queries = [args.query] if args.query else TEST_QUERIES

    if not VOYAGE_API_KEY:
        print(f'{RED}[error] VOYAGE_API_KEY is not set.  '
              f'Add it to the root .env file.{RESET}')
        return 1

    try:
        import voyageai
        voyage_client = voyageai.Client(api_key=VOYAGE_API_KEY)
    except ImportError:
        print(f'{RED}[error] voyageai package not installed.  '
              f'Run: pip install voyageai{RESET}')
        return 1

    print('[db] loading taxonomy labels ...')
    conn = get_conn()
    rows = load_labels(conn)
    conn.close()
    print(f'[db] loaded {len(rows):,} label rows')

    cache = build_or_load_index(rows, rebuild=args.rebuild)
    print(f'\n[bge] model={cache["model"]}  dim={cache["dim"]}  '
          f'docs={len(cache["docs"]):,}')
    print(f'[voyage] rerank_model={VOYAGE_RERANK_MODEL}')
    print(f'[config] top_k={args.top_k}  thresholds={THRESHOLDS}')

    from sentence_transformers import SentenceTransformer
    import numpy as np
    device = get_bge_device()
    bge_model = SentenceTransformer(cache['model'], device=device)

    for query in queries:
        # First-stage: BGE-M3 dense retrieval
        qvec = bge_model.encode(
            [query],
            normalize_embeddings=True,
            convert_to_numpy=True,
            device=device,
        ).astype('float32')
        k = min(args.top_k, cache['index'].ntotal)
        bge_scores, bge_idxs = cache['index'].search(qvec, k)

        candidates = []
        for score, idx in zip(bge_scores[0], bge_idxs[0]):
            row = dict(cache['rows'][idx])
            row['_bge_score'] = float(score)
            row['_emb_text'] = cache['docs'][idx]
            candidates.append(row)

        # Second-stage: Voyage cross-encoder rerank
        ranked = voyage_rerank(query, candidates, voyage_client)

        # Group by cluster, best rerank score wins
        clusters = group_by_cluster(ranked)

        if args.min_score is not None:
            clusters = [c for c in clusters if c['best_score'] >= args.min_score]

        print_query_results(query, clusters, top_n=args.top_n)

        if args.compare:
            compare_minilm(query, rows, top_n=args.top_n)

    print(f'\n{"=" * 72}')
    print('Done.')
    return 0


if __name__ == '__main__':
    raise SystemExit(main())
