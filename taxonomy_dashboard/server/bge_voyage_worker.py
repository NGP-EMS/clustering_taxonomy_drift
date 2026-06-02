#!/usr/bin/env python3
"""
BGE-M3 + Voyage rerank worker for taxonomy semantic search.

Activated when SEMANTIC_SEARCH_PROVIDER=bge_voyage is set in the environment.

Protocol (same line-delimited JSON format as embed_worker.py):
  stdout  {"type": "ready", "model": "...", "dimension": 1024, "indexed_docs": N}
  stdin   {"id": "1", "query": "agent rude", "field_name": "", "limit": 60,
            "top_k": 200, "min_score": 0.3}
  stdout  {"id": "1", "engine": "bge_voyage", "results": [...], ...}

  Control messages:
  stdin   {"id": "2", "type": "refresh_index"}
  stdout  {"id": "2", "type": "refresh_done", "indexed_docs": N}

On startup:
  1. Loads BAAI/bge-m3 via sentence-transformers.
  2. Queries the taxonomy DB to build label documents.
  3. Embeds all documents and stores a FAISS IndexFlatIP.
  4. Caches the index to server/.bge_cache/bge_index.pkl (reused until stale).

Result format is compatible with the existing /api/semantic-search response
mapper in server/index.js — best_label_similarity and semantic_score are both
set to the best Voyage rerank score for the cluster.
"""

import json
import os
import pickle
import sys
import time
import traceback
from pathlib import Path

# ── env ───────────────────────────────────────────────────────────────────────
ROOT = Path(__file__).resolve().parent.parent.parent
DASH_ROOT = Path(__file__).resolve().parent.parent

from dotenv import load_dotenv
load_dotenv(ROOT / '.env')
load_dotenv(DASH_ROOT / '.env')

# ── config ────────────────────────────────────────────────────────────────────
BGE_MODEL           = os.getenv('BGE_MODEL', 'BAAI/bge-m3')
VOYAGE_RERANK_MODEL = os.getenv('VOYAGE_RERANK_MODEL', 'rerank-2.5-lite')
VOYAGE_API_KEY      = os.getenv('VOYAGE_API_KEY')
TOP_K_RETRIEVE      = int(os.getenv('BGE_TOP_K_RETRIEVE', '200'))
INDEX_MAX_AGE_SECS  = int(os.getenv('BGE_INDEX_MAX_AGE_SECS', str(6 * 3600)))
DEFAULT_MIN_SCORE   = float(os.getenv('BGE_VOYAGE_MIN_SCORE', '0.3'))
CACHE_PATH          = Path(__file__).parent / '.bge_cache' / 'bge_index.pkl'

# ── helpers ───────────────────────────────────────────────────────────────────
def emit(payload: dict):
    print(json.dumps(payload, default=str), flush=True)


def log(msg: str):
    print(str(msg), file=sys.stderr, flush=True)


# ── DB ────────────────────────────────────────────────────────────────────────
LABEL_SQL = """
    SELECT
        lm.id                                       AS label_id,
        lm.field_name,
        lm.raw_label,
        lm.normalized_label,
        lm.final_cluster_id                         AS cluster_id,
        lm.cluster_version,
        COALESCE(lm.value_count, 1)                 AS value_count,
        tc.id                                       AS cluster_db_id,
        COALESCE(tc.run_id, '')                     AS run_id,
        tc.medoid_label,
        tc.cluster_size,
        tc.total_occurrences,
        COALESCE(tc.is_true_anomaly_cluster, FALSE) AS is_true_anomaly_cluster,
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
    import psycopg2
    return psycopg2.connect(
        host=os.environ['LOCAL_PG_HOST'],
        port=int(os.environ.get('LOCAL_PG_PORT', '5432')),
        database=os.environ['LOCAL_PG_DB'],
        user=os.environ['LOCAL_PG_USER'],
        password=os.environ['LOCAL_PG_PASSWORD'],
    )


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


# ── index ─────────────────────────────────────────────────────────────────────
def load_or_build_index(bge_model, rebuild: bool = False) -> dict:
    if not rebuild and CACHE_PATH.exists():
        age = time.time() - CACHE_PATH.stat().st_mtime
        if age < INDEX_MAX_AGE_SECS:
            log(f'[bge_voyage] loading cached index (age={age/3600:.1f}h)')
            with open(CACHE_PATH, 'rb') as f:
                return pickle.load(f)
        log(f'[bge_voyage] cache stale ({age/3600:.1f}h), rebuilding')

    log('[bge_voyage] loading taxonomy labels from DB ...')
    conn = get_conn()
    with conn.cursor() as cur:
        cur.execute(LABEL_SQL)
        cols = [d[0] for d in cur.description]
        rows = [dict(zip(cols, row)) for row in cur.fetchall()]
    conn.close()
    log(f'[bge_voyage] loaded {len(rows):,} label rows')

    docs = [build_emb_text(r) for r in rows]

    log(f'[bge_voyage] embedding {len(docs):,} documents ...')
    t0 = time.time()
    import numpy as np
    embs = bge_model.encode(
        docs, batch_size=64, normalize_embeddings=True, show_progress_bar=False
    )
    log(f'[bge_voyage] embedded in {time.time() - t0:.1f}s')

    import faiss
    dim = embs.shape[1]
    index = faiss.IndexFlatIP(dim)
    index.add(embs.astype('float32'))

    cache = {
        'index': index,
        'rows': rows,
        'docs': docs,
        'dim': dim,
        'built_at': time.time(),
    }
    CACHE_PATH.parent.mkdir(parents=True, exist_ok=True)
    with open(CACHE_PATH, 'wb') as f:
        pickle.dump(cache, f)
    log(f'[bge_voyage] index saved ({len(rows):,} docs, dim={dim})')
    return cache


# ── search ────────────────────────────────────────────────────────────────────
def search(
    query: str,
    bge_model,
    cache: dict,
    voyage_client,
    field_name: str = '',
    limit: int = 60,
    top_k: int = TOP_K_RETRIEVE,
    min_score: float = DEFAULT_MIN_SCORE,
) -> list[dict]:
    import numpy as np

    qvec = bge_model.encode([query], normalize_embeddings=True).astype('float32')
    k = min(top_k, cache['index'].ntotal)
    bge_scores, bge_idxs = cache['index'].search(qvec, k)

    rows = cache['rows']
    docs = cache['docs']

    candidates = []
    for score, idx in zip(bge_scores[0], bge_idxs[0]):
        row = rows[idx]
        if field_name and row.get('field_name') != field_name:
            continue
        candidates.append({'row': row, 'doc': docs[idx], 'bge': float(score)})

    if not candidates:
        return []

    rerank_result = voyage_client.rerank(
        query,
        [c['doc'] for c in candidates],
        model=VOYAGE_RERANK_MODEL,
        top_k=len(candidates),
    )
    for r in rerank_result.results:
        candidates[r.index]['voyage'] = float(r.relevance_score)

    # Group by cluster; cluster score = best member rerank score
    clusters: dict = {}
    for c in candidates:
        if 'voyage' not in c:
            continue
        row = c['row']
        key = (row['field_name'], row['cluster_id'], row.get('cluster_version', 'v1'))
        if key not in clusters:
            clusters[key] = {
                'id':                    row.get('cluster_db_id'),
                'field_name':            row['field_name'],
                'run_id':                row.get('run_id', ''),
                'cluster_version':       row.get('cluster_version', 'v1'),
                'cluster_id':            row['cluster_id'],
                'cluster_size':          row.get('cluster_size'),
                'total_occurrences':     row.get('total_occurrences'),
                'medoid_label':          row.get('medoid_label'),
                'representative_labels': None,
                'medoid_similarity_to_centroid': None,
                'is_true_anomaly_cluster': row.get('is_true_anomaly_cluster', False),
                'has_centroid':          None,
                'display_name':          row.get('display_name'),
                'naming_method':         row.get('naming_method'),
                '_best_score':           -1.0,
                '_best_label':           '',
                '_labels':               [],
            }
        cl = clusters[key]
        vs = c['voyage']
        cl['_labels'].append({
            'raw_label':        row.get('raw_label'),
            'normalized_label': row.get('normalized_label'),
            'similarity':       round(vs, 4),
            'value_count':      row.get('value_count'),
        })
        if vs > cl['_best_score']:
            cl['_best_score'] = vs
            cl['_best_label'] = row.get('raw_label', '')

    results = []
    for cl in clusters.values():
        best = cl['_best_score']
        if best < min_score:
            continue
        labels = sorted(cl['_labels'], key=lambda x: x['similarity'], reverse=True)
        scores = [lbl['similarity'] for lbl in labels]
        avg = round(sum(scores) / len(scores), 4) if scores else 0.0
        occ = sum((lbl.get('value_count') or 0) for lbl in labels)
        results.append({
            'id':                           cl['id'],
            'field_name':                   cl['field_name'],
            'run_id':                       cl['run_id'],
            'cluster_version':              cl['cluster_version'],
            'cluster_id':                   cl['cluster_id'],
            'cluster_size':                 cl['cluster_size'],
            'total_occurrences':            cl['total_occurrences'],
            'medoid_label':                 cl['medoid_label'],
            'representative_labels':        cl['representative_labels'],
            'medoid_similarity_to_centroid': cl['medoid_similarity_to_centroid'],
            'is_true_anomaly_cluster':      cl['is_true_anomaly_cluster'],
            'has_centroid':                 cl['has_centroid'],
            'display_name':                 cl['display_name'],
            'naming_method':                cl['naming_method'],
            'best_label_similarity':        round(best, 4),
            'avg_label_similarity':         avg,
            'semantic_score':               round(best, 4),
            'matched_label_count':          len(labels),
            'matched_occurrences':          occ,
            'semantic_best_label':          cl['_best_label'],
            'semantic_matched_labels':      labels[:8],
        })

    results.sort(key=lambda x: x['semantic_score'], reverse=True)
    return results[:limit]


# ── main ──────────────────────────────────────────────────────────────────────
def main() -> int:
    if not VOYAGE_API_KEY:
        emit({'type': 'error',
              'error': 'VOYAGE_API_KEY is not set — bge_voyage worker cannot start'})
        return 1

    try:
        from sentence_transformers import SentenceTransformer
    except ImportError as exc:
        emit({'type': 'error', 'error': f'sentence-transformers not installed: {exc}'})
        return 3

    try:
        import faiss
    except ImportError as exc:
        emit({'type': 'error', 'error': f'faiss-cpu not installed: {exc}'})
        return 3

    try:
        import voyageai
    except ImportError as exc:
        emit({'type': 'error', 'error': f'voyageai package not installed: {exc}'})
        return 3

    try:
        log(f'[bge_voyage] loading {BGE_MODEL} ...')
        bge_model = SentenceTransformer(BGE_MODEL)
        dim = bge_model.get_sentence_embedding_dimension()
        log(f'[bge_voyage] model loaded, dim={dim}')
    except Exception as exc:
        emit({'type': 'error',
              'error': f'Failed to load BGE-M3: {exc}',
              'detail': traceback.format_exc(limit=3)})
        return 4

    try:
        voyage_client = voyageai.Client(api_key=VOYAGE_API_KEY)
    except Exception as exc:
        emit({'type': 'error', 'error': f'Failed to init Voyage client: {exc}'})
        return 4

    try:
        cache = load_or_build_index(bge_model)
    except Exception as exc:
        emit({'type': 'error',
              'error': f'Failed to build label index: {exc}',
              'detail': traceback.format_exc(limit=4)})
        return 4

    emit({
        'type':          'ready',
        'model':         f'{BGE_MODEL}+{VOYAGE_RERANK_MODEL}',
        'bge_model':     BGE_MODEL,
        'rerank_model':  VOYAGE_RERANK_MODEL,
        'dimension':     dim,
        'indexed_docs':  len(cache['docs']),
    })

    for line in sys.stdin:
        line = line.strip()
        if not line:
            continue

        request_id = ''
        try:
            req = json.loads(line)
            request_id = str(req.get('id') or '')

            if req.get('type') == 'refresh_index':
                log('[bge_voyage] refreshing index ...')
                cache = load_or_build_index(bge_model, rebuild=True)
                emit({'id': request_id, 'type': 'refresh_done',
                      'indexed_docs': len(cache['docs'])})
                continue

            query = str(req.get('query') or '').strip()
            if not request_id:
                emit({'error': 'missing id'})
                continue
            if not query:
                emit({'id': request_id, 'error': 'missing query'})
                continue

            field_name = str(req.get('field_name') or '')
            limit      = int(req.get('limit')    or 60)
            top_k      = int(req.get('top_k')    or TOP_K_RETRIEVE)
            min_score  = float(req.get('min_score') if req.get('min_score') is not None
                               else DEFAULT_MIN_SCORE)

            results = search(
                query, bge_model, cache, voyage_client,
                field_name=field_name, limit=limit,
                top_k=top_k, min_score=min_score,
            )

            emit({
                'id':                        request_id,
                'engine':                    'bge_voyage',
                'model':                     f'{BGE_MODEL}+{VOYAGE_RERANK_MODEL}',
                'dimension':                 dim,
                'searched_label_candidates': min(top_k, len(cache['docs'])),
                'results':                   results,
            })

        except Exception as exc:
            emit({
                'id':     request_id,
                'error':  f'bge_voyage search failed: {exc}',
                'detail': traceback.format_exc(limit=4),
            })

    return 0


if __name__ == '__main__':
    raise SystemExit(main())
