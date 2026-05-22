# Semantic Cluster Search

This build adds semantic search over taxonomy label embeddings.

## Backend

New endpoint:

```text
GET /api/semantic-search?q=aggressive&limit=120&min_score=0.30&label_limit=1200&include_calls=true
```

Optional field-scoped search:

```text
GET /api/semantic-search?q=aggressive&field_name=coaching_tags
```

The endpoint embeds the query with `server/embed_query.py`, searches `taxonomy_label_embeddings`, joins hits back to `taxonomy_label_cluster_map`, and returns ranked clusters with matched labels.

## Call IDs

Semantic search is cluster-first, not call-first. It does not need a `call_id` to search.

When `include_calls=true`, the endpoint also looks at the latest `taxonomy_call_cluster_outputs` mapper run and returns sample call/source IDs for each matched cluster:

```json
{
  "semantic_distinct_calls": 42,
  "sample_call_ids": ["003xx...", "003yy..."],
  "call_id_source": "taxonomy_call_cluster_outputs.source_record_id"
}
```

If `taxonomy_call_cluster_outputs` is missing or empty, semantic search still works, but call IDs will be empty.

## Required Python dependency

The API Python environment must have:

```bash
pip install sentence-transformers
```

By default it uses:

```text
SEMANTIC_SEARCH_MODEL=all-MiniLM-L6-v2
```

Use the same embedding model that created `taxonomy_label_embeddings`.

## Frontend API URL

The frontend now supports an explicit API base URL:

```text
VITE_API_BASE_URL=http://localhost:5050
```

This prevents the `Unexpected token '<'` JSON error caused by Vite preview returning `index.html` for `/api/*`.

Run both processes locally:

```bash
npm run server
npm run dev
```

## Frontend

The Observatory left panel now has **Semantic Search**. Search terms such as `aggressive`, `rude`, `confused`, or `angry customer` filter the map/table to matching clusters and show:

- semantic score
- best matched label
- top matched clusters
- sample call/source IDs when the mapper output table is available
- purple ring around semantic matches in map/3D view

## Timeout fix

Semantic search now uses a persistent Python embedding worker (`server/embed_worker.py`). The model is loaded once when `npm run server` starts instead of loading SentenceTransformers on every `/api/semantic-search` request.

Optional environment settings:

```env
SEMANTIC_SEARCH_PYTHON=python
SEMANTIC_SEARCH_MODEL=all-MiniLM-L6-v2
SEMANTIC_EMBED_STARTUP_TIMEOUT_MS=180000
SEMANTIC_EMBED_REQUEST_TIMEOUT_MS=30000
```

If the first startup is slow, start the API server and wait until the terminal shows the model loading message before searching from the dashboard.
