'use strict';

const express = require('express');
const cors    = require('cors');
const path    = require('path');
const { spawn } = require('child_process');

require('dotenv').config({ path: path.resolve(__dirname, '../.env') });

const pool = require('./db');
const app  = express();

app.use(cors());
app.use(express.json());
app.set('etag', false);

// ── Schema cache ───────────────────────────────────────────────────────────────
const _colCache = {};
const _tableCache = {};

async function getCols(tableName) {
  if (_colCache[tableName]) return _colCache[tableName];
  const { rows } = await pool.query(
    `SELECT column_name FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = $1`,
    [tableName]
  );
  _colCache[tableName] = new Set(rows.map(r => r.column_name));
  return _colCache[tableName];
}

async function tableExists(tableName) {
  if (_tableCache[tableName] === true) return true;
  const { rows } = await pool.query(`SELECT to_regclass($1) IS NOT NULL AS exists`, [`public.${tableName}`]);
  _tableCache[tableName] = !!rows[0]?.exists;
  return _tableCache[tableName];
}


async function getColInfo(tableName) {
  const { rows } = await pool.query(
    `SELECT column_name, data_type, udt_name
     FROM information_schema.columns
     WHERE table_schema = 'public' AND table_name = $1`,
    [tableName]
  );
  return Object.fromEntries(rows.map(r => [r.column_name, r]));
}

function pickCol(cols, names) {
  for (const name of names) if (cols.has(name)) return name;
  return null;
}

function vectorLiteral(arr) {
  if (!Array.isArray(arr) || !arr.length) return null;
  return `[${arr.map(v => Number(v).toFixed(8)).join(',')}]`;
}

function numericParam(value, fallback, min, max) {
  const n = Number(value);
  if (!Number.isFinite(n)) return fallback;
  return Math.max(min, Math.min(max, n));
}

function normalizeSemanticQueryText(text) {
  return String(text || '')
    .replace(/\bagressive\b/gi, 'aggressive')
    .replace(/\bagression\b/gi, 'aggression')
    .trim();
}


class SemanticEmbeddingWorker {
  constructor() {
    this.child = null;
    this.ready = false;
    this.readyPromise = null;
    this.pending = new Map();
    this.buffer = '';
    this.nextId = 1;
    this.lastStart = 0;
  }

  start() {
    if (this.child && !this.child.killed) return this.readyPromise;

    const now = Date.now();
    if (now - this.lastStart < 1000) {
      return this.readyPromise || Promise.reject(new Error('Semantic embedding worker restart throttled'));
    }
    this.lastStart = now;

    const pythonExec = process.env.SEMANTIC_SEARCH_PYTHON || process.env.PYTHON || 'python';
    const scriptPath = path.resolve(__dirname, 'embed_worker.py');
    this.ready = false;
    this.buffer = '';

    this.child = spawn(pythonExec, ['-u', scriptPath], {
      cwd: path.resolve(__dirname, '..'),
      env: process.env,
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    const startupTimeoutMs = parseInt(process.env.SEMANTIC_EMBED_STARTUP_TIMEOUT_MS || '180000', 10);
    this.readyPromise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(`Semantic embedding worker startup timed out after ${startupTimeoutMs}ms`));
      }, startupTimeoutMs);

      const finishReady = payload => {
        clearTimeout(timer);
        this.ready = true;
        resolve(payload);
      };

      this._finishReady = finishReady;
      this._failReady = err => {
        clearTimeout(timer);
        reject(err);
      };
    });

    this.child.stdout.on('data', d => this.handleStdout(d));
    this.child.stderr.on('data', d => {
      const msg = d.toString().trim();
      if (msg) console.warn('[semantic-embed-worker]', msg);
    });
    this.child.on('error', err => this.failAll(err));
    this.child.on('close', code => {
      const err = new Error(`Semantic embedding worker exited with code ${code}`);
      this.ready = false;
      this.child = null;
      if (this._failReady) this._failReady(err);
      this.failAll(err);
    });

    return this.readyPromise;
  }

  handleStdout(chunk) {
    this.buffer += chunk.toString();
    let idx;
    while ((idx = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;

      let payload;
      try {
        payload = JSON.parse(line);
      } catch (err) {
        console.warn('[semantic-embed-worker] non-json stdout:', line.slice(0, 200));
        continue;
      }

      if (payload.type === 'ready') {
        if (this._finishReady) this._finishReady(payload);
        continue;
      }

      const id = payload.id;
      if (!id || !this.pending.has(id)) continue;
      const item = this.pending.get(id);
      this.pending.delete(id);
      clearTimeout(item.timer);

      if (payload.error) {
        item.reject(new Error(payload.error));
      } else if (!payload.embedding) {
        item.reject(new Error('Semantic embedding worker returned no embedding'));
      } else {
        item.resolve({
          embedding: payload.embedding,
          model: payload.model || null,
          dimension: payload.dimension || payload.embedding.length,
        });
      }
    }
  }

  failAll(err) {
    for (const [, item] of this.pending) {
      clearTimeout(item.timer);
      item.reject(err);
    }
    this.pending.clear();
  }

  async embed(text) {
    const query = String(text || '').trim();
    if (!query) throw new Error('Missing semantic search query');

    await this.start();

    const requestTimeoutMs = parseInt(process.env.SEMANTIC_EMBED_REQUEST_TIMEOUT_MS || '30000', 10);
    const id = String(this.nextId++);

    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`Semantic query embedding request timed out after ${requestTimeoutMs}ms`));
      }, requestTimeoutMs);

      this.pending.set(id, { resolve, reject, timer });

      try {
        this.child.stdin.write(`${JSON.stringify({ id, query })}\n`);
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(err);
      }
    });
  }
}

const semanticEmbeddingWorker = new SemanticEmbeddingWorker();

async function embedSemanticQuery(text) {
  return semanticEmbeddingWorker.embed(text);
}

// Preload the model when the API starts so the first dashboard search does not
// pay the full SentenceTransformers import/model-load cost.
semanticEmbeddingWorker.start().catch(err => {
  console.warn('[semantic-embed-worker] preload failed:', err.message);
});

// ── BGE-M3 + Voyage rerank worker ────────────────────────────────────────────
// Activated via SEMANTIC_SEARCH_PROVIDER=bge_voyage.
// On startup the worker builds (or loads) a FAISS index of all taxonomy labels.
// Each search request returns pre-grouped cluster results ranked by Voyage score.
class BgeVoyageWorker {
  constructor() {
    this.child        = null;
    this.ready        = false;
    this.readyPromise = null;
    this.pending      = new Map();
    this.buffer       = '';
    this.nextId       = 1;
    this.lastStart    = 0;
  }

  start() {
    if (this.child && !this.child.killed) return this.readyPromise;

    const now = Date.now();
    if (now - this.lastStart < 1000) {
      return this.readyPromise || Promise.reject(new Error('BGE/Voyage worker restart throttled'));
    }
    this.lastStart = now;

    const pythonExec = process.env.SEMANTIC_SEARCH_PYTHON || process.env.PYTHON || 'python';
    const scriptPath = path.resolve(__dirname, 'bge_voyage_worker.py');
    this.ready  = false;
    this.buffer = '';

    // First run downloads BAAI/bge-m3 (~1.5 GB) and embeds all taxonomy labels.
    // Allow up to 30 minutes before declaring startup failure.
    const startupTimeoutMs = parseInt(
      process.env.BGE_VOYAGE_STARTUP_TIMEOUT_MS || String(30 * 60 * 1000), 10
    );

    this.child = spawn(pythonExec, ['-u', scriptPath], {
      cwd:   path.resolve(__dirname, '..'),
      env:   process.env,
      stdio: ['pipe', 'pipe', 'pipe'],
    });

    this.readyPromise = new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        reject(new Error(`BGE/Voyage worker startup timed out after ${startupTimeoutMs}ms`));
      }, startupTimeoutMs);
      this._finishReady = payload => { clearTimeout(timer); this.ready = true; resolve(payload); };
      this._failReady   = err    => { clearTimeout(timer); reject(err); };
    });

    this.child.stdout.on('data', d => this.handleStdout(d));
    this.child.stderr.on('data', d => {
      const msg = d.toString().trim();
      if (msg) console.warn('[bge-voyage-worker]', msg);
    });
    this.child.on('error', err  => this.failAll(err));
    this.child.on('close', code => {
      const err = new Error(`BGE/Voyage worker exited with code ${code}`);
      this.ready = false;
      this.child = null;
      if (this._failReady) this._failReady(err);
      this.failAll(err);
    });

    return this.readyPromise;
  }

  handleStdout(chunk) {
    this.buffer += chunk.toString();
    let idx;
    while ((idx = this.buffer.indexOf('\n')) >= 0) {
      const line = this.buffer.slice(0, idx).trim();
      this.buffer = this.buffer.slice(idx + 1);
      if (!line) continue;

      let payload;
      try { payload = JSON.parse(line); }
      catch { console.warn('[bge-voyage-worker] non-json stdout:', line.slice(0, 200)); continue; }

      if (payload.type === 'ready') {
        console.log(
          `[bge-voyage-worker] ready — model=${payload.model}` +
          ` dim=${payload.dimension} indexed_docs=${payload.indexed_docs}`
        );
        if (this._finishReady) this._finishReady(payload);
        continue;
      }

      const id = String(payload.id || '');
      if (!id || !this.pending.has(id)) continue;
      const item = this.pending.get(id);
      this.pending.delete(id);
      clearTimeout(item.timer);
      if (payload.error) item.reject(new Error(payload.error));
      else               item.resolve(payload);
    }
  }

  failAll(err) {
    for (const [, item] of this.pending) { clearTimeout(item.timer); item.reject(err); }
    this.pending.clear();
  }

  async search(params) {
    await this.start();
    const requestTimeoutMs = parseInt(
      process.env.BGE_VOYAGE_REQUEST_TIMEOUT_MS || '90000', 10
    );
    const id = String(this.nextId++);
    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error(`BGE/Voyage search timed out after ${requestTimeoutMs}ms`));
      }, requestTimeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.child.stdin.write(`${JSON.stringify({ id, ...params })}\n`);
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(err);
      }
    });
  }

  async refresh() {
    await this.start();
    // Embedding all labels can take several minutes — allow up to 20 min
    const timeoutMs = 20 * 60 * 1000;
    const id = String(this.nextId++);
    return await new Promise((resolve, reject) => {
      const timer = setTimeout(() => {
        this.pending.delete(id);
        reject(new Error('BGE/Voyage index refresh timed out after 20 minutes'));
      }, timeoutMs);
      this.pending.set(id, { resolve, reject, timer });
      try {
        this.child.stdin.write(`${JSON.stringify({ id, type: 'refresh_index' })}\n`);
      } catch (err) {
        clearTimeout(timer);
        this.pending.delete(id);
        reject(err);
      }
    });
  }
}

const bgeVoyageWorker = new BgeVoyageWorker();

async function getLabelEmbeddingConfig() {
  if (!(await tableExists('taxonomy_label_embeddings'))) return null;
  const cols = await getCols('taxonomy_label_embeddings');
  const info = await getColInfo('taxonomy_label_embeddings');
  const embeddingCol = pickCol(cols, ['embedding', 'label_embedding', 'embedding_vector', 'label_vector', 'vector', 'text_embedding']);
  if (!embeddingCol) return null;
  return {
    cols,
    info,
    embeddingCol,
    fieldCol: pickCol(cols, ['field_name', 'field']),
    rawCol: pickCol(cols, ['raw_label', 'label', 'original_label']),
    normCol: pickCol(cols, ['normalized_label', 'normalized_key', 'clean_label']),
    isPgVector: String(info[embeddingCol]?.udt_name || '').toLowerCase() === 'vector',
  };
}

function buildLabelMapJoin({ lmCols, hitAlias = 'h', lmAlias = 'lm' }) {
  const parts = [];
  if (lmCols.has('field_name')) parts.push(`${lmAlias}.field_name = ${hitAlias}.field_name`);
  const labelParts = [];
  if (lmCols.has('raw_label')) labelParts.push(`${lmAlias}.raw_label = ${hitAlias}.raw_label`);
  if (lmCols.has('normalized_label')) labelParts.push(`${lmAlias}.normalized_label = ${hitAlias}.normalized_label`);
  if (!labelParts.length) return null;
  parts.push(`(${labelParts.join(' OR ')})`);
  return parts.join(' AND ');
}

function semanticResultSelectSql(tcCols, tcnCols, lmCols) {
  const aCol = anomalyColSql(tcCols, 'tc');
  const lmCC = lmCols.has('final_cluster_id') ? 'final_cluster_id' : 'cluster_id';
  const clusterRunExpr = tcCols.has('run_id') ? `COALESCE(tc.run_id, '')` : `''`;
  const clusterVersionExpr = tcCols.has('cluster_version') ? `COALESCE(tc.cluster_version, 'v1')` : `'v1'`;
  const activeFilter = tcCols.has('active') ? 'AND COALESCE(tc.active, true) = true' : '';
  const joinOn = [
    'tc.cluster_id = ch.cluster_id',
    lmCols.has('field_name') ? 'tc.field_name = ch.field_name' : null,
    lmCols.has('run_id') && tcCols.has('run_id') ? "COALESCE(tc.run_id, '') = COALESCE(ch.run_id, '')" : null,
  ].filter(Boolean).join(' AND ');

  return {
    lmCC,
    activeFilter,
    sql: `
      ranked_labels AS (
        SELECT
          m.*,
          ROW_NUMBER() OVER (
            PARTITION BY m.field_name, m.${lmCC}
            ORDER BY m.similarity DESC NULLS LAST, m.value_count DESC NULLS LAST, m.raw_label
          ) AS rn
        FROM mapped_hits m
        WHERE m.${lmCC} IS NOT NULL
      ),
      cluster_hits AS (
        SELECT
          rl.field_name,
          ${lmCols.has('run_id') ? 'rl.run_id' : 'NULL::text AS run_id'},
          rl.${lmCC} AS cluster_id,
          MAX(rl.similarity)::double precision AS best_label_similarity,
          AVG(rl.similarity)::double precision AS avg_label_similarity,
          COUNT(*)::int AS matched_label_count,
          COALESCE(SUM(COALESCE(rl.value_count, 1)), 0)::bigint AS matched_occurrences,
          MAX(rl.raw_label) FILTER (WHERE rl.rn = 1) AS best_label,
          JSONB_AGG(
            JSONB_BUILD_OBJECT(
              'raw_label', rl.raw_label,
              'normalized_label', rl.normalized_label,
              'similarity', ROUND(rl.similarity::numeric, 4),
              'value_count', rl.value_count
            )
            ORDER BY rl.similarity DESC NULLS LAST, rl.value_count DESC NULLS LAST
          ) FILTER (WHERE rl.rn <= 8) AS matched_labels
        FROM ranked_labels rl
        GROUP BY rl.field_name, ${lmCols.has('run_id') ? 'rl.run_id,' : ''} rl.${lmCC}
      )
      SELECT
        tc.id,
        tc.field_name,
        ${clusterRunExpr} AS run_id,
        ${clusterVersionExpr} AS cluster_version,
        tc.cluster_id,
        ${tcCols.has('cluster_size') ? 'tc.cluster_size' : 'NULL::int AS cluster_size'},
        ${tcCols.has('total_occurrences') ? 'tc.total_occurrences' : 'NULL::bigint AS total_occurrences'},
        ${tcCols.has('medoid_label') ? 'tc.medoid_label' : 'NULL::text AS medoid_label'},
        ${tcCols.has('representative_labels') ? 'tc.representative_labels::text AS representative_labels' : 'NULL::text AS representative_labels'},
        ${tcCols.has('medoid_similarity_to_centroid') ? 'tc.medoid_similarity_to_centroid' : 'NULL::numeric AS medoid_similarity_to_centroid'},
        ${aCol ? `${aCol} AS is_true_anomaly_cluster` : 'NULL::boolean AS is_true_anomaly_cluster'},
        ${tcCols.has('centroid_embedding') ? 'CASE WHEN tc.centroid_embedding IS NOT NULL THEN true ELSE false END AS has_centroid' : 'NULL::boolean AS has_centroid'},
        tcn.display_name,
        tcn.naming_method,
        ch.best_label_similarity,
        ch.avg_label_similarity,
        ROUND(((ch.best_label_similarity * 0.78) + (ch.avg_label_similarity * 0.22))::numeric, 4)::double precision AS semantic_score,
        ch.matched_label_count,
        ch.matched_occurrences,
        ch.best_label AS semantic_best_label,
        ch.matched_labels AS semantic_matched_labels
      FROM cluster_hits ch
      JOIN taxonomy_clusters tc ON ${joinOn}
      LEFT JOIN taxonomy_cluster_names tcn ON ${nameJoinSql(tcCols, tcnCols)}
      WHERE ((ch.best_label_similarity * 0.78) + (ch.avg_label_similarity * 0.22)) >= $MIN_SCORE
        ${activeFilter}
      ORDER BY semantic_score DESC, ch.best_label_similarity DESC, ${tcCols.has('total_occurrences') ? 'COALESCE(tc.total_occurrences, 0)' : tcCols.has('cluster_size') ? 'COALESCE(tc.cluster_size, 0)' : 'tc.id'} DESC
      LIMIT $LIMIT
    `,
  };
}

// ── Shared helpers ─────────────────────────────────────────────────────────────
function nameJoinSql(tcCols, tcnCols, tcAlias = 'tc', tcnAlias = 'tcn') {
  let j = `${tcAlias}.field_name = ${tcnAlias}.field_name AND ${tcAlias}.cluster_id = ${tcnAlias}.cluster_id`;
  if (tcCols.has('run_id')          && tcnCols.has('run_id'))          j += ` AND ${tcAlias}.run_id = ${tcnAlias}.run_id`;
  if (tcCols.has('cluster_version') && tcnCols.has('cluster_version')) j += ` AND ${tcAlias}.cluster_version = ${tcnAlias}.cluster_version`;
  return j;
}

function anomalyColSql(tcCols, alias = 'tc') {
  if (tcCols.has('is_true_anomaly_cluster')) return `${alias}.is_true_anomaly_cluster`;
  if (tcCols.has('is_anomaly'))              return `${alias}.is_anomaly`;
  return null;
}

function parseEmbedding(value) {
  if (!value) return null;
  let arr = null;
  if (Array.isArray(value)) {
    arr = value;
  } else if (typeof value === 'string') {
    const text = value.trim();
    try {
      arr = JSON.parse(text);
    } catch {
      const body = (text.startsWith('[') && text.endsWith(']')) || (text.startsWith('{') && text.endsWith('}'))
        ? text.slice(1, -1)
        : text;
      arr = body.split(',').map(v => Number(v.trim())).filter(Number.isFinite);
    }
  }
  if (!Array.isArray(arr) || !arr.length) return null;
  return arr.map(Number).filter(Number.isFinite);
}

function cosineSimilarity(a, b) {
  if (!a || !b || a.length !== b.length) return null;
  let dot = 0, na = 0, nb = 0;
  for (let i = 0; i < a.length; i++) {
    dot += a[i] * b[i];
    na += a[i] * a[i];
    nb += b[i] * b[i];
  }
  if (!na || !nb) return null;
  return dot / (Math.sqrt(na) * Math.sqrt(nb));
}

function similarityInterpretation(score, sameField, isAnomaly) {
  if (score == null) return 'not computed';
  if (isAnomaly && score >= 0.82) return 'anomaly recovery candidate';
  if (sameField && score >= 0.9) return 'merge candidate';
  if (score >= 0.82) return 'semantic neighbor';
  if (score >= 0.72) return 'weak semantic neighbor';
  return 'distant neighborhood';
}

// ── Cluster list query builder ─────────────────────────────────────────────────
async function buildClusterQuery({ filters = {}, anomalyOnly = false }) {
  const [tcCols, tcnCols, lmCols] = await Promise.all([
    getCols('taxonomy_clusters'),
    getCols('taxonomy_cluster_names'),
    getCols('taxonomy_label_cluster_map'),
  ]);
  const hasProjectionCoordinates = await tableExists('semantic_projection_coordinates');

  const vals = [];
  const cond = [];
  const aCol = anomalyColSql(tcCols);

  if (anomalyOnly && aCol) {
    cond.push(`${aCol} = true`);
  } else if (!anomalyOnly && filters.anomaly === 'anomaly' && aCol) {
    cond.push(`${aCol} = true`);
  } else if (!anomalyOnly && filters.anomaly === 'standard' && aCol) {
    cond.push(`(${aCol} = false OR ${aCol} IS NULL)`);
  }

  if (filters.field_name) { vals.push(filters.field_name); cond.push(`tc.field_name = $${vals.length}`); }

  if (filters.search) {
    vals.push(`%${filters.search}%`);
    const idx = vals.length;
    const parts = [`tc.cluster_id ILIKE $${idx}`];
    if (tcCols.has('medoid_label'))         parts.push(`tc.medoid_label ILIKE $${idx}`);
    if (tcCols.has('representative_label')) parts.push(`tc.representative_label ILIKE $${idx}`);
    if (tcCols.has('representative_labels')) parts.push(`tc.representative_labels::text ILIKE $${idx}`);
    if (tcnCols.has('display_name'))        parts.push(`tcn.display_name ILIKE $${idx}`);
    const lmClusterColSearch = lmCols.has('final_cluster_id') ? 'final_cluster_id' : 'cluster_id';
    const lmSearchParts = [];
    if (lmCols.has('raw_label')) lmSearchParts.push(`lm_search.raw_label ILIKE $${idx}`);
    if (lmCols.has('normalized_label')) lmSearchParts.push(`lm_search.normalized_label ILIKE $${idx}`);
    if (lmSearchParts.length) {
      let exists = `EXISTS (SELECT 1 FROM taxonomy_label_cluster_map lm_search WHERE lm_search.${lmClusterColSearch} = tc.cluster_id`;
      if (lmCols.has('field_name')) exists += ' AND lm_search.field_name = tc.field_name';
      if (lmCols.has('run_id') && tcCols.has('run_id')) exists += ' AND lm_search.run_id = tc.run_id';
      exists += ` AND (${lmSearchParts.join(' OR ')}))`;
      parts.push(exists);
    }
    cond.push(`(${parts.join(' OR ')})`);
  }

  if (filters.min_size && tcCols.has('cluster_size')) {
    vals.push(parseInt(filters.min_size, 10) || 1);
    cond.push(`tc.cluster_size >= $${vals.length}`);
  }

  if (!anomalyOnly) {
    if (filters.named === 'named')   cond.push('tcn.display_name IS NOT NULL');
    if (filters.named === 'unnamed') cond.push('tcn.display_name IS NULL');
  }

  const whereClause = cond.length ? `WHERE ${cond.join(' AND ')}` : '';
  // Observatory can request larger per-field samples. Keep a safety ceiling,
  // but do not silently force 8k requests back down to 5k.
  const requestedLimit = parseInt(filters.limit) || 50;
  const maxClusterLimit = Math.max(parseInt(process.env.MAX_CLUSTER_API_LIMIT || '50000', 10) || 50000, 5000);
  const limit  = Math.min(Math.max(requestedLimit, 1), maxClusterLimit);
  const offset = Math.max(parseInt(filters.offset) || 0, 0);
  const _requestedProj = String(filters.projection || '').toLowerCase();
  const projectionMethod = ['umap_2d', 'umap_2d_cloud', 'umap_2d_legacy_cloud', 'umap_2d_compact_cloud', 'umap_3d', 'tsne', 'pca'].includes(_requestedProj)
    ? _requestedProj
    : null;
  const useProjection = hasProjectionCoordinates && projectionMethod != null;
  if (useProjection) vals.push(projectionMethod);
  const projectionParam = useProjection ? `$${vals.length}` : null;
  vals.push(limit);  const limitParam  = `$${vals.length}`;
  vals.push(offset); const offsetParam = `$${vals.length}`;

  const sel = {
    size:    tcCols.has('cluster_size')         ? 'tc.cluster_size'         : 'NULL::int AS cluster_size',
    occ:     tcCols.has('total_occurrences')    ? 'tc.total_occurrences'    : 'NULL::bigint AS total_occurrences',
    medoid:  tcCols.has('medoid_label')         ? 'tc.medoid_label'         : 'NULL::text AS medoid_label',
    rep:     tcCols.has('representative_label') ? 'tc.representative_label' : 'NULL::text AS representative_label',
    reps:    tcCols.has('representative_labels') ? 'tc.representative_labels::text AS representative_labels' : 'NULL::text AS representative_labels',
    embedding: tcCols.has('centroid_embedding') ? 'tc.centroid_embedding::text AS centroid_embedding' : 'NULL::text AS centroid_embedding',
    medoidSim: tcCols.has('medoid_similarity_to_centroid') ? 'tc.medoid_similarity_to_centroid' : 'NULL::numeric AS medoid_similarity_to_centroid',
    anomaly: aCol ? `${aCol} AS is_true_anomaly_cluster`                    : 'NULL::boolean AS is_true_anomaly_cluster',
    reason:  tcnCols.has('naming_reason')       ? 'tcn.naming_reason'       : 'NULL::text AS naming_reason',
    centroid: tcCols.has('centroid_embedding') ? 'CASE WHEN tc.centroid_embedding IS NOT NULL THEN true ELSE false END AS has_centroid'
      : tcCols.has('centroid') ? 'CASE WHEN tc.centroid IS NOT NULL THEN true ELSE false END AS has_centroid'
      : 'NULL::boolean AS has_centroid',
  };

  const lmClusterCol = lmCols.has('final_cluster_id') ? 'final_cluster_id' : 'cluster_id';
  const lmGroupCols  = [lmClusterCol];
  if (lmCols.has('field_name')) lmGroupCols.push('field_name');
  if (lmCols.has('run_id'))     lmGroupCols.push('run_id');

  let lmJoinOn = `lm_sub.${lmClusterCol} = tc.cluster_id`;
  if (lmCols.has('field_name'))                     lmJoinOn += ' AND lm_sub.field_name = tc.field_name';
  if (lmCols.has('run_id') && tcCols.has('run_id')) lmJoinOn += ' AND lm_sub.run_id = tc.run_id';
  const runIdExpr = tcCols.has('run_id') ? `COALESCE(tc.run_id, '')` : `''`;
  const clusterVersionExpr = tcCols.has('cluster_version') ? `COALESCE(tc.cluster_version,'v1')` : `'v1'`;

  const projectionSelect = useProjection
    ? `spc.projection_method, spc.x AS projection_x, spc.y AS projection_y, spc.z AS projection_z`
    : `'fallback'::text AS projection_method, NULL::double precision AS projection_x, NULL::double precision AS projection_y, NULL::double precision AS projection_z`;

  const projectionJoin = useProjection
    ? `LEFT JOIN (
        SELECT DISTINCT ON (field_name, cluster_id, projection_method)
          field_name, cluster_id, projection_method, x, y, z
        FROM semantic_projection_coordinates
        WHERE projection_method = ${projectionParam}
        ORDER BY field_name, cluster_id, projection_method, created_at DESC
      ) spc ON spc.field_name = tc.field_name
           AND spc.cluster_id = tc.cluster_id`
    : '';

  const sql = `
    SELECT
      tc.id,
      tc.field_name,
      ${runIdExpr} AS run_id,
      ${clusterVersionExpr} AS cluster_version,
      tc.cluster_id,
      ${sel.size},
      ${sel.occ},
      ${sel.medoid},
      ${sel.medoidSim},
      ${sel.rep},
      ${sel.reps},
      ${sel.embedding},
      ${sel.anomaly},
      ${sel.centroid},
      tcn.display_name,
      tcn.naming_method,
      ${sel.reason},
      COALESCE(lm_sub.label_count, 0) AS label_count,
      ${projectionSelect}
    FROM taxonomy_clusters tc
    LEFT JOIN taxonomy_cluster_names tcn ON ${nameJoinSql(tcCols, tcnCols)}
    LEFT JOIN (
      SELECT ${lmGroupCols.join(', ')}, COUNT(DISTINCT raw_label)::int AS label_count
      FROM taxonomy_label_cluster_map
      GROUP BY ${lmGroupCols.join(', ')}
    ) lm_sub ON ${lmJoinOn}
    ${projectionJoin}
    ${whereClause}
    ORDER BY
      CASE WHEN COALESCE(${aCol || 'false'}, false) THEN 1 ELSE 0 END,
      COALESCE(tc.cluster_size, 0) DESC,
      tc.field_name,
      tc.cluster_id
    LIMIT ${limitParam} OFFSET ${offsetParam}
  `;

  return { sql, vals };
}

// ── GET /api/health ────────────────────────────────────────────────────────────
app.get('/api/health', async (req, res) => {
  try {
    const [tcCols, tcnCols] = await Promise.all([getCols('taxonomy_clusters'), getCols('taxonomy_cluster_names')]);
    const aCol       = tcCols.has('is_true_anomaly_cluster') ? 'is_true_anomaly_cluster' : tcCols.has('is_anomaly') ? 'is_anomaly' : null;
    const hasCentroid = tcCols.has('centroid_embedding') || tcCols.has('centroid');
    const centroidCol = tcCols.has('centroid_embedding') ? 'centroid_embedding' : 'centroid';

    let nJoin = 'tc.field_name = n.field_name AND tc.cluster_id = n.cluster_id';
    if (tcCols.has('run_id') && tcnCols.has('run_id'))                   nJoin += ' AND tc.run_id = n.run_id';
    if (tcCols.has('cluster_version') && tcnCols.has('cluster_version')) nJoin += ' AND tc.cluster_version = n.cluster_version';

    const anomalyFrag = aCol
      ? `, COUNT(*) FILTER (WHERE tc.${aCol} = true)::int AS anomaly_clusters,
           COUNT(*) FILTER (WHERE tc.${aCol} IS DISTINCT FROM true)::int AS standard_clusters`
      : `, NULL::int AS anomaly_clusters, NULL::int AS standard_clusters`;

    const { rows: [s] } = await pool.query(`
      SELECT COUNT(*)::int AS total_clusters,
             COUNT(n.cluster_id)::int AS named_clusters,
             (COUNT(*) - COUNT(n.cluster_id))::int AS unnamed_clusters
             ${anomalyFrag}
      FROM taxonomy_clusters tc
      LEFT JOIN (
        SELECT DISTINCT ON (field_name, cluster_id) field_name, run_id, cluster_version, cluster_id
        FROM taxonomy_cluster_names WHERE display_name IS NOT NULL
        ORDER BY field_name, cluster_id
      ) n ON ${nJoin}
    `);

    const [{ rows: [lr] }, { rows: [fr] }] = await Promise.all([
      pool.query('SELECT COUNT(*)::int AS total FROM taxonomy_label_cluster_map'),
      pool.query('SELECT COUNT(DISTINCT field_name)::int AS cnt FROM taxonomy_clusters'),
    ]);

    let centroidMissingCount = null;
    if (hasCentroid) {
      const { rows: [cr] } = await pool.query(`SELECT COUNT(*)::int AS cnt FROM taxonomy_clusters WHERE ${centroidCol} IS NULL`);
      centroidMissingCount = cr.cnt;
    }

    // Duplicate display name count
    let duplicateNames = 0;

    try {
      const { rows: [dr] } = await pool.query(`
        SELECT COUNT(*)::int AS cnt
        FROM (
          SELECT
            field_name,
            run_id,
            cluster_version,
            display_name
          FROM taxonomy_cluster_names
          WHERE display_name IS NOT NULL
            AND TRIM(display_name) <> ''
          GROUP BY
            field_name,
            run_id,
            cluster_version,
            display_name
          HAVING COUNT(*) > 1
        ) sub
      `);

      duplicateNames = dr.cnt;
    } catch {}

    let lastRunCount = null;
    for (const t of ['taxonomy_cluster_runs', 'taxonomy_run_metadata']) {
      if (lastRunCount !== null) break;
      try { const { rows: [r] } = await pool.query(`SELECT COUNT(*)::int AS cnt FROM ${t}`); lastRunCount = r.cnt; } catch {}
    }

    res.json({
      total_clusters: s.total_clusters, named_clusters: s.named_clusters,
      unnamed_clusters: s.unnamed_clusters, anomaly_clusters: s.anomaly_clusters,
      standard_clusters: s.standard_clusters, total_label_rows: lr.total,
      fields_count: fr.cnt, centroid_missing_count: centroidMissingCount,
      duplicate_names: duplicateNames, last_run_count: lastRunCount,
    });
  } catch (err) { console.error('/api/health:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/debug/projection-stats ───────────────────────────────────────────
app.get('/api/debug/projection-stats', async (req, res) => {
  try {
    const exists = await tableExists('semantic_projection_coordinates');
    if (!exists) return res.json({ error: 'table does not exist' });
    const { rows } = await pool.query(`
      SELECT projection_method,
             COUNT(*) AS row_count,
             COUNT(DISTINCT field_name || '::' || cluster_id) AS unique_clusters,
             ROUND(MIN(x)::numeric, 4) AS x_min, ROUND(MAX(x)::numeric, 4) AS x_max,
             ROUND(MIN(y)::numeric, 4) AS y_min, ROUND(MAX(y)::numeric, 4) AS y_max,
             MAX(created_at) AS latest_run
      FROM semantic_projection_coordinates
      GROUP BY projection_method
      ORDER BY projection_method
    `);
    res.json(rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// ── GET /api/fields ────────────────────────────────────────────────────────────
app.get('/api/fields', async (req, res) => {
  try {
    const { rows } = await pool.query('SELECT DISTINCT field_name FROM taxonomy_clusters ORDER BY field_name');
    res.json(rows.map(r => r.field_name));
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// ── GET /api/clusters ──────────────────────────────────────────────────────────
app.get('/api/clusters', async (req, res) => {
  try {
    const filters = {
      field_name: req.query.field_name || '', search: req.query.search || '',
      anomaly: req.query.anomaly || '', named: req.query.named || '',
      min_size: req.query.min_size || '', limit: req.query.limit || 50, offset: req.query.offset || 0,
      projection: req.query.projection || 'umap_2d',
    };
    const { sql, vals } = await buildClusterQuery({ filters });
    const { rows } = await pool.query(sql, vals);
    const projMethod = filters.projection || 'umap_2d';
    const nullProj = rows.filter(r => r.projection_x == null || r.projection_y == null).length;
    const distinctFields = [...new Set(rows.map(r => r.field_name))];
    const uniqueKeys = new Set(rows.map(r => `${r.field_name}::${r.cluster_id}`));
    const dupCount = rows.length - uniqueKeys.size;
    const withCoords = rows.filter(r => r.projection_x != null && r.projection_y != null);
    const xVals = withCoords.map(r => Number(r.projection_x));
    const yVals = withCoords.map(r => Number(r.projection_y));
    const xRange = xVals.length ? `[${Math.min(...xVals).toFixed(3)}, ${Math.max(...xVals).toFixed(3)}]` : 'n/a';
    const yRange = yVals.length ? `[${Math.min(...yVals).toFixed(3)}, ${Math.max(...yVals).toFixed(3)}]` : 'n/a';
    console.log(`[/api/clusters] field=${filters.field_name || 'ALL'} proj=${projMethod} total=${rows.length} unique=${uniqueKeys.size} dups=${dupCount} nullProj=${nullProj} xRange=${xRange} yRange=${yRange} fields=[${distinctFields.join(',')}]`);
    res.set('Cache-Control', 'no-store');
    res.json(rows);
  } catch (err) { console.error('/api/clusters:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/anomalies ─────────────────────────────────────────────────────────
app.get('/api/anomalies', async (req, res) => {
  try {
    const filters = {
      field_name: req.query.field_name || '', search: req.query.search || '',
      limit: req.query.limit || 50, offset: req.query.offset || 0,
      projection: req.query.projection || 'umap_2d',
    };
    const { sql, vals } = await buildClusterQuery({ filters, anomalyOnly: true });
    const { rows } = await pool.query(sql, vals);
    res.json(rows);
  } catch (err) { console.error('/api/anomalies:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/cluster/:id ───────────────────────────────────────────────────────
app.get('/api/cluster/:id', async (req, res) => {
  try {
    const id = parseInt(req.params.id, 10);
    if (isNaN(id)) return res.status(400).json({ error: 'Invalid id' });

    const [tcCols, tcnCols] = await Promise.all([getCols('taxonomy_clusters'), getCols('taxonomy_cluster_names')]);
    const aCol = anomalyColSql(tcCols);

    const { rows } = await pool.query(`
      SELECT
        tc.id, tc.field_name,
        COALESCE(tc.run_id,'')           AS run_id,
        COALESCE(tc.cluster_version,'v1') AS cluster_version,
        tc.cluster_id,
        ${tcCols.has('cluster_size')         ? 'tc.cluster_size'         : 'NULL::int AS cluster_size'},
        ${tcCols.has('total_occurrences')    ? 'tc.total_occurrences'    : 'NULL::bigint AS total_occurrences'},
        ${tcCols.has('medoid_label')         ? 'tc.medoid_label'         : 'NULL::text AS medoid_label'},
        ${tcCols.has('medoid_similarity_to_centroid') ? 'tc.medoid_similarity_to_centroid' : 'NULL::numeric AS medoid_similarity_to_centroid'},
        ${tcCols.has('representative_label')  ? 'tc.representative_label'  : 'NULL::text AS representative_label'},
        ${tcCols.has('representative_labels') ? 'tc.representative_labels::text' : 'NULL::text AS representative_labels'},
        ${tcCols.has('cluster_source')       ? 'tc.cluster_source'       : 'NULL::text AS cluster_source'},
        ${tcCols.has('similarity_threshold') ? 'tc.similarity_threshold' : 'NULL::numeric AS similarity_threshold'},
        ${tcCols.has('active')               ? 'tc.active'               : 'true AS active'},
        ${aCol ? `${aCol} AS is_true_anomaly_cluster` : 'NULL::boolean AS is_true_anomaly_cluster'},
        ${tcCols.has('centroid_embedding') ? 'CASE WHEN tc.centroid_embedding IS NOT NULL THEN true ELSE false END' : tcCols.has('centroid') ? 'CASE WHEN tc.centroid IS NOT NULL THEN true ELSE false END' : 'false'} AS has_centroid,
        tc.created_at,
        tcn.display_name, tcn.naming_method,
        ${tcnCols.has('naming_reason') ? 'tcn.naming_reason' : 'NULL::text AS naming_reason'}
      FROM taxonomy_clusters tc
      LEFT JOIN taxonomy_cluster_names tcn ON ${nameJoinSql(tcCols, tcnCols)}
      WHERE tc.id = $1
    `, [id]);

    if (!rows.length) return res.status(404).json({ error: 'Cluster not found' });
    const clusterRow = rows[0];
    try {
      const rid = clusterRow.run_id || clusterRow.cluster_version || '';
      const payload = await runMetadataPayload(rid, { fieldName: clusterRow.field_name || '' });
      if (payload.status === 200 && payload.body && !payload.body.error) {
        clusterRow.taxonomy_run_metadata = payload.body;
      }
    } catch (metaErr) {
      console.warn('/api/cluster/:id metadata fallback:', metaErr.message);
    }
    res.json(clusterRow);
  } catch (err) { console.error('/api/cluster/:id:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/cluster/:id/labels ────────────────────────────────────────────────
app.get('/api/cluster/:id/labels', async (req, res) => {
  try {
    const id  = parseInt(req.params.id, 10);
    const lim = Math.min(parseInt(req.query.limit) || 50, 500);
    if (isNaN(id)) return res.status(400).json({ error: 'Invalid id' });

    const { rows: [cluster] } = await pool.query(
      'SELECT field_name, cluster_id, run_id FROM taxonomy_clusters WHERE id = $1', [id]
    );
    if (!cluster) return res.status(404).json({ error: 'Cluster not found' });

    const lmCols = await getCols('taxonomy_label_cluster_map');
    const lmCC   = lmCols.has('final_cluster_id') ? 'final_cluster_id' : 'cluster_id';
    const vals   = [cluster.cluster_id];
    const cond   = [`lm.${lmCC} = $1`];

    if (lmCols.has('field_name'))                         { vals.push(cluster.field_name); cond.push(`lm.field_name = $${vals.length}`); }
    if (lmCols.has('run_id') && cluster.run_id)           { vals.push(cluster.run_id);     cond.push(`lm.run_id = $${vals.length}`); }
    vals.push(lim);

    const { rows } = await pool.query(`
      SELECT
        lm.raw_label,
        ${lmCols.has('normalized_label')      ? 'lm.normalized_label'      : 'NULL::text AS normalized_label'},
        ${lmCols.has('value_count')           ? 'lm.value_count'           : '1::bigint AS value_count'},
        ${lmCols.has('final_is_true_anomaly') ? 'lm.final_is_true_anomaly' : 'NULL::boolean AS final_is_true_anomaly'}
      FROM taxonomy_label_cluster_map lm
      WHERE ${cond.join(' AND ')}
      ORDER BY ${lmCols.has('value_count') ? 'lm.value_count DESC NULLS LAST' : 'lm.raw_label'}
      LIMIT $${vals.length}
    `, vals);
    res.json(rows);
  } catch (err) { console.error('/api/cluster/:id/labels:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/cluster/:id/similar ───────────────────────────────────────────────
app.get('/api/cluster/:id/similar', async (req, res) => {
  try {
    const id  = parseInt(req.params.id, 10);
    const lim = Math.min(parseInt(req.query.limit) || 8, 20);
    if (isNaN(id)) return res.status(400).json({ error: 'Invalid id' });

    const { rows: [cluster] } = await pool.query(
      'SELECT id, field_name, cluster_id, is_true_anomaly_cluster, centroid_embedding FROM taxonomy_clusters WHERE id = $1', [id]
    );
    if (!cluster) return res.status(404).json({ error: 'Cluster not found' });

    const [tcCols, tcnCols] = await Promise.all([getCols('taxonomy_clusters'), getCols('taxonomy_cluster_names')]);
    const aCol = anomalyColSql(tcCols);
    const targetEmbedding = parseEmbedding(cluster.centroid_embedding);

    if (!tcCols.has('centroid_embedding') || !targetEmbedding) {
      return res.json({
        status: 'not_computed',
        reason: 'Centroid embedding is missing or not queryable for this cluster.',
        neighbors: [],
      });
    }

    const { rows } = await pool.query(`
      SELECT
        tc.id, tc.field_name, tc.cluster_id,
        ${tcCols.has('cluster_size')  ? 'tc.cluster_size'  : 'NULL::int AS cluster_size'},
        ${tcCols.has('total_occurrences') ? 'tc.total_occurrences' : 'NULL::bigint AS total_occurrences'},
        ${tcCols.has('medoid_label')  ? 'tc.medoid_label'  : 'NULL::text AS medoid_label'},
        ${tcCols.has('medoid_similarity_to_centroid') ? 'tc.medoid_similarity_to_centroid' : 'NULL::numeric AS medoid_similarity_to_centroid'},
        ${aCol ? `${aCol} AS is_true_anomaly_cluster` : 'NULL::boolean AS is_true_anomaly_cluster'},
        tc.centroid_embedding,
        COALESCE(tcn.display_name, ${tcCols.has('display_name') ? 'tc.display_name' : 'NULL::text'}) AS display_name,
        tcn.naming_method
      FROM taxonomy_clusters tc
      LEFT JOIN taxonomy_cluster_names tcn ON ${nameJoinSql(tcCols, tcnCols)}
      WHERE tc.id != $1
        AND tc.centroid_embedding IS NOT NULL
      ORDER BY (tc.field_name = $2) DESC, ${tcCols.has('cluster_size') ? 'tc.cluster_size DESC NULLS LAST' : 'tc.id'}
      LIMIT 8000
    `, [id, cluster.field_name]);

    const neighbors = rows
      .map(r => {
        const score = cosineSimilarity(targetEmbedding, parseEmbedding(r.centroid_embedding));
        const sameField = r.field_name === cluster.field_name;
        return {
          id: r.id,
          field_name: r.field_name,
          cluster_id: r.cluster_id,
          display_name: r.display_name,
          cluster_size: r.cluster_size,
          total_occurrences: r.total_occurrences,
          medoid_label: r.medoid_label,
          medoid_similarity_to_centroid: r.medoid_similarity_to_centroid,
          is_true_anomaly_cluster: r.is_true_anomaly_cluster,
          cosine_similarity: score == null ? null : +score.toFixed(4),
          same_field: sameField,
          interpretation: similarityInterpretation(score, sameField, cluster.is_true_anomaly_cluster),
        };
      })
      .filter(r => r.cosine_similarity != null)
      .sort((a, b) => b.cosine_similarity - a.cosine_similarity)
      .slice(0, lim);

    const avg = neighbors.length
      ? neighbors.reduce((s, n) => s + n.cosine_similarity, 0) / neighbors.length
      : null;

    res.json({
      status: 'computed',
      metric: 'centroid_cosine_similarity',
      explanation: 'Analytical nearest-neighbor hints derived from centroid embeddings. These are not official taxonomy relationships.',
      avg_neighbor_similarity: avg == null ? null : +avg.toFixed(4),
      neighbors,
    });
  } catch (err) { console.error('/api/cluster/:id/similar:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/field-distribution ────────────────────────────────────────────────
app.get('/api/field-distribution', async (req, res) => {
  try {
    const tcCols = await getCols('taxonomy_clusters');
    const aCol   = tcCols.has('is_true_anomaly_cluster') ? 'is_true_anomaly_cluster' : tcCols.has('is_anomaly') ? 'is_anomaly' : null;

    const { rows } = await pool.query(`
      SELECT
        field_name,
        COUNT(*)::int AS total,
        ${aCol ? `COUNT(*) FILTER (WHERE ${aCol} = true)::int AS anomalies` : '0::int AS anomalies'},
        ${tcCols.has('cluster_size') ? 'SUM(cluster_size)::bigint AS total_labels' : '0 AS total_labels'}
      FROM taxonomy_clusters
      GROUP BY field_name
      ORDER BY total DESC
    `);
    res.json(rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// ── GET /api/cluster-size-distribution ─────────────────────────────────────────
app.get('/api/cluster-size-distribution', async (req, res) => {
  try {
    const tcCols = await getCols('taxonomy_clusters');
    if (!tcCols.has('cluster_size')) return res.json([]);
    const { rows } = await pool.query(`
      SELECT
        CASE
          WHEN cluster_size = 1    THEN '1'
          WHEN cluster_size <= 3   THEN '2–3'
          WHEN cluster_size <= 5   THEN '4–5'
          WHEN cluster_size <= 10  THEN '6–10'
          WHEN cluster_size <= 25  THEN '11–25'
          WHEN cluster_size <= 50  THEN '26–50'
          WHEN cluster_size <= 100 THEN '51–100'
          ELSE '100+'
        END AS bucket,
        COUNT(*)::int AS count,
        MIN(cluster_size) AS bucket_min
      FROM taxonomy_clusters
      WHERE cluster_size IS NOT NULL
      GROUP BY bucket, bucket_min
      ORDER BY bucket_min
    `);
    res.json(rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// ── GET /api/anomaly-intelligence ──────────────────────────────────────────────
app.get('/api/anomaly-intelligence', async (req, res) => {
  try {
    const [tcCols, tcnCols, lmCols] = await Promise.all([
      getCols('taxonomy_clusters'), getCols('taxonomy_cluster_names'), getCols('taxonomy_label_cluster_map'),
    ]);
    const aCol = anomalyColSql(tcCols);
    if (!aCol) return res.json({ clusters: [], summary: { total: 0, by_type: {} } });

    const lmCC       = lmCols.has('final_cluster_id') ? 'final_cluster_id' : 'cluster_id';
    const lmGrp      = [lmCC];
    if (lmCols.has('field_name')) lmGrp.push('field_name');
    if (lmCols.has('run_id'))     lmGrp.push('run_id');
    let lmOn = `lm_sub.${lmCC} = tc.cluster_id`;
    if (lmCols.has('field_name'))                     lmOn += ' AND lm_sub.field_name = tc.field_name';
    if (lmCols.has('run_id') && tcCols.has('run_id')) lmOn += ' AND lm_sub.run_id = tc.run_id';
    const sizeExpr = tcCols.has('cluster_size') ? 'tc.cluster_size' : '1';

    const { rows: overviewRows } = await pool.query(`
      SELECT
        tc.field_name,
        COUNT(*)::int AS anomaly_clusters,
        COALESCE(SUM(${tcCols.has('cluster_size') ? 'tc.cluster_size' : '1'}), 0)::bigint AS anomaly_labels,
        COALESCE(SUM(${tcCols.has('total_occurrences') ? 'tc.total_occurrences' : tcCols.has('cluster_size') ? 'tc.cluster_size' : '1'}), 0)::bigint AS anomaly_occurrences
      FROM taxonomy_clusters tc
      WHERE ${aCol} = true
      GROUP BY tc.field_name
      ORDER BY anomaly_clusters DESC
    `);

    const { rows: totalRows } = await pool.query(`
      SELECT
        COUNT(*)::int AS total_clusters,
        COUNT(*) FILTER (WHERE ${aCol} = true)::int AS anomaly_clusters,
        COALESCE(SUM(${tcCols.has('cluster_size') ? 'tc.cluster_size' : '1'}) FILTER (WHERE ${aCol} = true), 0)::bigint AS anomaly_labels,
        COALESCE(SUM(${tcCols.has('total_occurrences') ? 'tc.total_occurrences' : tcCols.has('cluster_size') ? 'tc.cluster_size' : '1'}) FILTER (WHERE ${aCol} = true), 0)::bigint AS anomaly_occurrences
      FROM taxonomy_clusters tc
    `);

    const { rows } = await pool.query(`
      SELECT
        tc.id, tc.field_name,
        COALESCE(tc.run_id,'') AS run_id,
        tc.cluster_id,
        ${tcCols.has('cluster_size')      ? 'tc.cluster_size'      : 'NULL::int AS cluster_size'},
        ${tcCols.has('total_occurrences') ? 'tc.total_occurrences' : 'NULL::bigint AS total_occurrences'},
        ${tcCols.has('medoid_label')      ? 'tc.medoid_label'      : 'NULL::text AS medoid_label'},
        ${tcCols.has('representative_labels') ? 'tc.representative_labels::text' : 'NULL::text AS representative_labels'},
        ${tcCols.has('cluster_source')    ? 'tc.cluster_source'    : 'NULL::text AS cluster_source'},
        ${aCol} AS is_true_anomaly_cluster,
        tcn.display_name, tcn.naming_method,
        ${tcnCols.has('naming_reason') ? 'tcn.naming_reason' : 'NULL::text AS naming_reason'},
        COALESCE(lm_sub.label_count, 0) AS label_count,
        CASE
          WHEN ${sizeExpr} <= 2  THEN 'noise'
          WHEN ${sizeExpr} <= 5  THEN 'threshold_failure'
          WHEN ${sizeExpr} > 20  THEN 'emerging'
          ELSE 'semantic_outlier'
        END AS anomaly_type
      FROM taxonomy_clusters tc
      LEFT JOIN taxonomy_cluster_names tcn ON ${nameJoinSql(tcCols, tcnCols)}
      LEFT JOIN (
        SELECT ${lmGrp.join(', ')}, COUNT(DISTINCT raw_label)::int AS label_count
        FROM taxonomy_label_cluster_map GROUP BY ${lmGrp.join(', ')}
      ) lm_sub ON ${lmOn}
      WHERE ${aCol} = true
      ORDER BY ${tcCols.has('cluster_size') ? 'tc.cluster_size DESC' : 'tc.cluster_id'}
      LIMIT 500
    `);

    const byType = rows.reduce((acc, r) => { acc[r.anomaly_type] = (acc[r.anomaly_type] || 0) + 1; return acc; }, {});
    const totals = totalRows[0] || {};
    res.json({
      clusters: rows.map(r => ({
        ...r,
        recoverability_status: 'not_computed',
        nearest_cluster_candidates: [],
        suggested_action: 'review manually',
      })),
      summary: {
        total: totals.anomaly_clusters || rows.length,
        total_clusters: totals.total_clusters || null,
        anomaly_labels: Number(totals.anomaly_labels) || 0,
        anomaly_occurrences: Number(totals.anomaly_occurrences) || 0,
        anomaly_rate: totals.total_clusters ? Number(totals.anomaly_clusters || 0) / Number(totals.total_clusters) : null,
        by_type: byType,
        by_field: overviewRows,
      },
    });
  } catch (err) { console.error('/api/anomaly-intelligence:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/drift-summary ─────────────────────────────────────────────────────
app.get('/api/drift-summary', async (req, res) => {
  try {
    const [tcCols, tcnCols] = await Promise.all([getCols('taxonomy_clusters'), getCols('taxonomy_cluster_names')]);

    let runTimeline = [];
    try {
      const { rows } = await pool.query(`
        SELECT DATE(created_at)::text AS run_date, field_name, COUNT(*)::int AS run_count
        FROM taxonomy_cluster_runs
        GROUP BY DATE(created_at), field_name
        ORDER BY run_date DESC LIMIT 90
      `);
      runTimeline = rows;
    } catch {}

    let newestClusters = [];
    try {
      const { rows } = await pool.query(`
        SELECT tc.id, tc.field_name, tc.cluster_id, tc.created_at,
          ${tcCols.has('cluster_size')  ? 'tc.cluster_size'  : 'NULL::int AS cluster_size'},
          ${tcCols.has('medoid_label')  ? 'tc.medoid_label'  : 'NULL::text AS medoid_label'},
          ${anomalyColSql(tcCols) ? `${anomalyColSql(tcCols)} AS is_true_anomaly_cluster` : 'NULL::boolean AS is_true_anomaly_cluster'},
          tcn.display_name
        FROM taxonomy_clusters tc
        LEFT JOIN taxonomy_cluster_names tcn ON ${nameJoinSql(tcCols, tcnCols)}
        ORDER BY tc.created_at DESC LIMIT 20
      `);
      newestClusters = rows;
    } catch {}

    let fieldStats = [];
    try {
      const { rows } = await pool.query(`
        SELECT field_name, COUNT(*)::int AS total_clusters,
          ${tcCols.has('cluster_size') ? 'SUM(cluster_size)::bigint AS total_labels' : 'NULL AS total_labels'},
          MAX(created_at) AS last_updated
        FROM taxonomy_clusters GROUP BY field_name ORDER BY total_clusters DESC
      `);
      fieldStats = rows;
    } catch {}

    res.json({ run_timeline: runTimeline, newest_clusters: newestClusters, field_stats: fieldStats });
  } catch (err) { console.error('/api/drift-summary:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/semantic-graph ────────────────────────────────────────────────────
app.get('/api/semantic-graph', async (req, res) => {
  try {
    const fieldFilter = req.query.field_name || '';
    const limit       = Math.min(parseInt(req.query.limit) || 600, 2000);
    const [tcCols, tcnCols, lmCols] = await Promise.all([
      getCols('taxonomy_clusters'), getCols('taxonomy_cluster_names'), getCols('taxonomy_label_cluster_map'),
    ]);

    const vals = [];
    const cond = [];
    if (fieldFilter) { vals.push(fieldFilter); cond.push(`tc.field_name = $${vals.length}`); }
    vals.push(limit);

    const aCol = anomalyColSql(tcCols);

    const { rows: nodeRows } = await pool.query(`
      SELECT tc.id, tc.field_name, tc.cluster_id,
        ${tcCols.has('cluster_size') ? 'COALESCE(tc.cluster_size,1)' : '1'} AS cluster_size,
        ${aCol ? `${aCol}` : 'NULL::boolean'} AS is_anomaly,
        tcn.display_name
      FROM taxonomy_clusters tc
      LEFT JOIN taxonomy_cluster_names tcn ON ${nameJoinSql(tcCols, tcnCols)}
      ${cond.length ? `WHERE ${cond.join(' AND ')}` : ''}
      ORDER BY tc.field_name, ${tcCols.has('cluster_size') ? 'tc.cluster_size DESC' : 'tc.cluster_id'}
      LIMIT $${vals.length}
    `, vals);

    // Build edges from label recovery paths
    let linkRows = [];
    if (lmCols.has('base_cluster_id') && lmCols.has('final_cluster_id')) {
      try {
        const eVals = [];
        const eCond = [];
        if (fieldFilter) { eVals.push(fieldFilter); eCond.push(`lm.field_name = $${eVals.length}`); }
        eVals.push(800);
        const { rows } = await pool.query(`
          SELECT DISTINCT lm.field_name, lm.base_cluster_id AS src, lm.final_cluster_id AS tgt
          FROM taxonomy_label_cluster_map lm
          WHERE lm.base_cluster_id IS NOT NULL AND lm.base_cluster_id != lm.final_cluster_id
          ${eCond.length ? `AND ${eCond.join(' AND ')}` : ''}
          LIMIT $${eVals.length}
        `, eVals);
        linkRows = rows;
      } catch {}
    }

    const COLORS = ['#569cd6','#4ec9b0','#c586c0','#dcdcaa','#ce9178','#9cdcfe','#6a9955','#d7ba7d','#4fc1ff','#b5cea8'];
    const fields      = [...new Set(nodeRows.map(n => n.field_name))];
    const fieldColors = Object.fromEntries(fields.map((f, i) => [f, COLORS[i % COLORS.length]]));
    const keyToId     = {};
    for (const n of nodeRows) keyToId[`${n.field_name}:${n.cluster_id}`] = n.id;

    const nodes = nodeRows.map(n => ({
      id: n.id, label: n.display_name || n.cluster_id,
      field_name: n.field_name, cluster_id: n.cluster_id,
      val: Math.max(1, Math.sqrt(n.cluster_size || 1) * 0.9),
      color: n.is_anomaly ? '#f44747' : (fieldColors[n.field_name] || '#569cd6'),
      is_anomaly: n.is_anomaly || false, cluster_size: n.cluster_size || 1,
    }));

    const seen  = new Set();
    const links = [];
    for (const r of linkRows) {
      const s = keyToId[`${r.field_name}:${r.src}`];
      const t = keyToId[`${r.field_name}:${r.tgt}`];
      if (!s || !t || s === t) continue;
      const k = `${Math.min(s,t)}-${Math.max(s,t)}`;
      if (!seen.has(k)) { seen.add(k); links.push({ source: s, target: t }); }
    }

    res.json({ nodes, links, field_colors: fieldColors, fields });
  } catch (err) { console.error('/api/semantic-graph:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/field-health ─────────────────────────────────────────────────────
app.get('/api/field-health', async (req, res) => {
  try {
    const [tcCols, tcnCols] = await Promise.all([getCols('taxonomy_clusters'), getCols('taxonomy_cluster_names')]);
    const aCol = anomalyColSql(tcCols);
    const nJoin = nameJoinSql(tcCols, tcnCols);
    const { rows } = await pool.query(`
      SELECT tc.field_name,
        COUNT(*)::int AS total_clusters,
        COUNT(tcn.cluster_id)::int AS named_clusters,
        (COUNT(*) - COUNT(tcn.cluster_id))::int AS unnamed_clusters,
        ${aCol ? `COUNT(*) FILTER (WHERE ${aCol} = true)::int AS anomaly_clusters` : '0::int AS anomaly_clusters'},
        ${tcCols.has('cluster_size') ? 'AVG(tc.cluster_size)::numeric AS avg_cluster_size, MAX(tc.cluster_size)::int AS max_cluster_size' : 'NULL AS avg_cluster_size, NULL::int AS max_cluster_size'}
      FROM taxonomy_clusters tc
      LEFT JOIN (SELECT DISTINCT field_name, cluster_id, run_id, cluster_version FROM taxonomy_cluster_names WHERE display_name IS NOT NULL) tcn ON ${nJoin}
      GROUP BY tc.field_name ORDER BY total_clusters DESC
    `);
    res.json(rows.map(r => ({
      ...r,
      naming_rate: r.total_clusters > 0 ? +(r.named_clusters / r.total_clusters).toFixed(3) : 0,
      anomaly_rate: r.total_clusters > 0 ? +(r.anomaly_clusters / r.total_clusters).toFixed(3) : 0,
      avg_cluster_size: r.avg_cluster_size ? +Number(r.avg_cluster_size).toFixed(1) : null,
    })));
  } catch (err) { console.error(err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/duplicate-names ──────────────────────────────────────────────────
app.get('/api/duplicate-names', async (req, res) => {
  try {
    const { rows } = await pool.query(`
      SELECT display_name, COUNT(*)::int AS cluster_count,
        array_agg(DISTINCT field_name ORDER BY field_name) AS fields
      FROM taxonomy_cluster_names
      WHERE display_name IS NOT NULL AND display_name != ''
      GROUP BY display_name HAVING COUNT(*) > 1
      ORDER BY COUNT(*) DESC LIMIT 50
    `);
    res.json(rows);
  } catch (err) { res.status(500).json({ error: err.message }); }
});

// ── GET /api/insights ─────────────────────────────────────────────────────────
app.get('/api/insights', async (req, res) => {
  try {
    const [tcCols, tcnCols, lmCols] = await Promise.all([
      getCols('taxonomy_clusters'), getCols('taxonomy_cluster_names'), getCols('taxonomy_label_cluster_map'),
    ]);
    const aCol  = anomalyColSql(tcCols);
    const nJoin = nameJoinSql(tcCols, tcnCols);
    const insights = [];

    // High anomaly rate fields
    if (aCol) {
      try {
        const { rows } = await pool.query(`
          SELECT field_name, COUNT(*)::int AS total,
            COUNT(*) FILTER (WHERE ${aCol} = true)::int AS anom
          FROM taxonomy_clusters GROUP BY field_name HAVING COUNT(*) >= 3
          ORDER BY (COUNT(*) FILTER (WHERE ${aCol} = true)::float / NULLIF(COUNT(*),0)) DESC LIMIT 3
        `);
        for (const r of rows) {
          if (!r.anom) continue;
          const rate = Math.round((r.anom / r.total) * 100);
          insights.push({ id: `high_anomaly_${r.field_name}`, category: 'anomaly',
            severity: rate > 30 ? 'critical' : rate > 15 ? 'warning' : 'info',
            title: 'High anomaly rate', value: `${rate}%`, metric: rate / 100,
            affected_field: r.field_name, affected_count: r.anom,
            reason: `${r.anom} of ${r.total} clusters are anomalies — may need re-clustering or threshold adjustment.`,
            action: { type: 'filter_field', field: r.field_name, anomaly: 'anomaly' } });
        }
      } catch {}
    }

    // Low naming quality
    try {
      const { rows } = await pool.query(`
        SELECT tc.field_name, COUNT(*)::int AS total, COUNT(tcn.cluster_id)::int AS named
        FROM taxonomy_clusters tc
        LEFT JOIN (SELECT DISTINCT field_name, cluster_id, run_id, cluster_version FROM taxonomy_cluster_names WHERE display_name IS NOT NULL) tcn ON ${nJoin}
        GROUP BY tc.field_name HAVING COUNT(*) >= 5
        ORDER BY (COUNT(tcn.cluster_id)::float / NULLIF(COUNT(*),0)) ASC LIMIT 3
      `);
      for (const r of rows) {
        const rate = Math.round(((r.total - r.named) / r.total) * 100);
        if (rate < 10) continue;
        insights.push({ id: `low_naming_${r.field_name}`, category: 'naming',
          severity: rate > 50 ? 'critical' : 'warning', title: 'Low naming coverage',
          value: `${rate}% unnamed`, metric: rate / 100,
          affected_field: r.field_name, affected_count: r.total - r.named,
          reason: `${r.total - r.named} of ${r.total} clusters lack display names, reducing taxonomy usability.`,
          action: { type: 'filter_field', field: r.field_name, named: 'unnamed' } });
      }
    } catch {}

    // Duplicate names (within same field, standard/non-anomaly clusters only)
    try {
      const { rows } = await pool.query(`
        SELECT COUNT(DISTINCT tcn.display_name)::int AS dup_names, SUM(cnt - 1)::int AS excess
        FROM (
          SELECT tcn.display_name, tc.field_name, COUNT(*) AS cnt
          FROM taxonomy_cluster_names tcn
          JOIN taxonomy_clusters tc ON ${nJoin}
          WHERE tcn.display_name IS NOT NULL AND tcn.display_name != ''
          ${aCol ? `AND (${aCol} = false OR ${aCol} IS NULL)` : ''}
          GROUP BY tcn.display_name, tc.field_name HAVING COUNT(*) > 1
        ) s
      `);
      if (rows[0]?.dup_names > 0) {
        const { dup_names, excess } = rows[0];
        insights.push({ id: 'duplicate_names', category: 'naming',
          severity: dup_names > 20 ? 'critical' : dup_names > 5 ? 'warning' : 'info',
          title: 'Duplicate display names', value: `${dup_names} names`, metric: dup_names,
          affected_field: null, affected_count: excess,
          reason: `${dup_names} display names appear on multiple standard clusters within the same field — may cause ambiguity during lookup.`,
          action: { type: 'page', page: 'clusters' } });
      }
    } catch {}

    // Generic names
    try {
      const hasSz = tcCols.has('cluster_size');
      const { rows } = await pool.query(`
        SELECT tc.id, tc.field_name, tc.cluster_id,
          ${hasSz ? 'tc.cluster_size' : 'NULL::int AS cluster_size'}, tcn.display_name
        FROM taxonomy_clusters tc
        JOIN taxonomy_cluster_names tcn ON ${nJoin}
        WHERE tcn.display_name IS NOT NULL AND (LENGTH(tcn.display_name) <= 4
          OR LOWER(tcn.display_name) IN ('other','misc','unknown','n/a','na','general','default','various','miscellaneous'))
        ORDER BY ${hasSz ? 'tc.cluster_size DESC' : 'tc.cluster_id'} LIMIT 10
      `);
      if (rows.length) {
        insights.push({ id: 'generic_names', category: 'naming',
          severity: rows.length > 5 ? 'warning' : 'info',
          title: 'Generic display names', value: `${rows.length} clusters`, metric: rows.length,
          affected_field: null, affected_count: rows.length,
          reason: `Clusters named "Other", "Misc", "N/A" etc. add noise. Review and provide meaningful names.`,
          action: { type: 'page', page: 'clusters' },
          examples: rows.slice(0, 3).map(r => ({ id: r.id, name: r.display_name, field: r.field_name, size: r.cluster_size })) });
      }
    } catch {}

    // Missing centroids
    const centCol = tcCols.has('centroid_embedding') ? 'centroid_embedding' : tcCols.has('centroid') ? 'centroid' : null;
    if (centCol) {
      try {
        const { rows } = await pool.query(`
          SELECT field_name, COUNT(*) FILTER (WHERE ${centCol} IS NULL)::int AS missing, COUNT(*)::int AS total
          FROM taxonomy_clusters GROUP BY field_name HAVING COUNT(*) FILTER (WHERE ${centCol} IS NULL) > 0
          ORDER BY missing DESC LIMIT 3
        `);
        for (const r of rows) {
          const pct = Math.round((r.missing / r.total) * 100);
          insights.push({ id: `centroids_${r.field_name}`, category: 'quality',
            severity: pct > 30 ? 'critical' : 'warning',
            title: 'Missing centroids', value: `${pct}% missing`, metric: pct / 100,
            affected_field: r.field_name, affected_count: r.missing,
            reason: `${r.missing} clusters in ${r.field_name} have no centroid — similarity search and graph layout degrade.`,
            action: { type: 'filter_field', field: r.field_name } });
        }
      } catch {}
    }

    // Over-compressed clusters
    if (tcCols.has('cluster_size')) {
      try {
        const lmCC = lmCols.has('final_cluster_id') ? 'final_cluster_id' : 'cluster_id';
        const lmGrp = [lmCC, ...(lmCols.has('field_name') ? ['field_name'] : [])];
        let lmOn = `lm_s.${lmCC} = tc.cluster_id`;
        if (lmCols.has('field_name')) lmOn += ' AND lm_s.field_name = tc.field_name';
        const { rows } = await pool.query(`
          SELECT tc.id, tc.field_name, tc.cluster_id, tc.cluster_size, tcn.display_name,
            COALESCE(lm_s.label_count, 0) AS label_count
          FROM taxonomy_clusters tc
          LEFT JOIN taxonomy_cluster_names tcn ON ${nJoin}
          LEFT JOIN (SELECT ${lmGrp.join(', ')}, COUNT(DISTINCT raw_label)::int AS label_count
            FROM taxonomy_label_cluster_map GROUP BY ${lmGrp.join(', ')}) lm_s ON ${lmOn}
          WHERE tc.cluster_size >= 20 AND COALESCE(lm_s.label_count, 0) <= 2
          ORDER BY tc.cluster_size DESC LIMIT 5
        `);
        if (rows.length) {
          insights.push({ id: 'over_compressed', category: 'quality', severity: 'warning',
            title: 'Potentially over-compressed', value: `${rows.length} clusters`, metric: rows.length,
            affected_field: rows[0].field_name, affected_count: rows.length,
            reason: `Large clusters with ≤2 distinct labels may be over-compressed — many raw values forced into one group.`,
            action: { type: 'filter_field', field: rows[0].field_name },
            examples: rows.slice(0, 3).map(r => ({ id: r.id, name: r.display_name || r.cluster_id, size: r.cluster_size })) });
        }
      } catch {}
    }

    res.json(insights);
  } catch (err) { console.error(err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/review-priorities ────────────────────────────────────────────────
app.get('/api/review-priorities', async (req, res) => {
  try {
    const [tcCols, tcnCols, lmCols] = await Promise.all([
      getCols('taxonomy_clusters'), getCols('taxonomy_cluster_names'), getCols('taxonomy_label_cluster_map'),
    ]);
    const aCol  = anomalyColSql(tcCols);
    const nJoin = nameJoinSql(tcCols, tcnCols);
    const hasSz = tcCols.has('cluster_size');
    const hasOc = tcCols.has('total_occurrences');
    const lmCC  = lmCols.has('final_cluster_id') ? 'final_cluster_id' : 'cluster_id';
    const lmGrp = [lmCC, ...(lmCols.has('field_name') ? ['field_name'] : [])];
    let lmOn    = `lm_s.${lmCC} = tc.cluster_id`;
    if (lmCols.has('field_name')) lmOn += ' AND lm_s.field_name = tc.field_name';

    const { rows } = await pool.query(`
      SELECT tc.id, tc.field_name, tc.cluster_id,
        ${hasSz ? 'tc.cluster_size' : 'NULL::int AS cluster_size'},
        ${hasOc ? 'tc.total_occurrences' : 'NULL::bigint AS total_occurrences'},
        ${tcCols.has('medoid_label') ? 'tc.medoid_label' : 'NULL::text AS medoid_label'},
        ${aCol ? `${aCol} AS is_anomaly` : 'false AS is_anomaly'},
        tcn.display_name, tcn.naming_method,
        COALESCE(lm_s.label_count, 0) AS label_count
      FROM taxonomy_clusters tc
      LEFT JOIN taxonomy_cluster_names tcn ON ${nJoin}
      LEFT JOIN (SELECT ${lmGrp.join(', ')}, COUNT(DISTINCT raw_label)::int AS label_count
        FROM taxonomy_label_cluster_map GROUP BY ${lmGrp.join(', ')}) lm_s ON ${lmOn}
      ORDER BY ${hasSz ? 'tc.cluster_size DESC' : 'tc.cluster_id'} LIMIT 2000
    `);

    const GENERIC = new Set(['other','misc','unknown','n/a','na','general','default','various','miscellaneous']);
    const scored = rows.map(r => {
      const reasons = []; let score = 0;
      if (r.is_anomaly)                                             { score += 0.35; reasons.push('anomaly') }
      if (!r.display_name)                                          { score += 0.30; reasons.push('unnamed') }
      if (r.display_name && GENERIC.has(r.display_name.toLowerCase())) { score += 0.20; reasons.push('generic_name') }
      if (r.cluster_size >= 20 && r.label_count <= 2)               { score += 0.25; reasons.push('over_compressed') }
      if (r.cluster_size >= 10 && r.total_occurrences <= 5)         { score += 0.15; reasons.push('low_occurrence') }
      if (r.display_name && r.display_name.length <= 3)             { score += 0.10; reasons.push('short_name') }
      return { ...r, priority_score: Math.min(1, score), reasons };
    }).filter(r => r.priority_score > 0).sort((a, b) => b.priority_score - a.priority_score).slice(0, 30);
    res.json(scored);
  } catch (err) { console.error(err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/semantic-compression ─────────────────────────────────────────────
app.get('/api/semantic-compression', async (req, res) => {
  try {
    const [tcCols, lmCols] = await Promise.all([
      getCols('taxonomy_clusters'), getCols('taxonomy_label_cluster_map'),
    ]);
    const hasSz = tcCols.has('cluster_size');

    const { rows: [totals] } = await pool.query(`
      SELECT COUNT(*)::int AS total_clusters,
        ${hasSz ? 'COALESCE(SUM(cluster_size),0)::bigint AS total_items' : '0::bigint AS total_items'}
      FROM taxonomy_clusters
    `);

    const { rows: fields } = await pool.query(`
      SELECT field_name,
        COUNT(*)::int AS cluster_count,
        ${hasSz
          ? 'COALESCE(SUM(cluster_size),0)::bigint AS label_count, MIN(cluster_size)::int AS min_size, MAX(cluster_size)::int AS max_size, ROUND(AVG(cluster_size),1)::numeric AS avg_size'
          : '0::bigint AS label_count, NULL::int AS min_size, NULL::int AS max_size, NULL::numeric AS avg_size'}
      FROM taxonomy_clusters GROUP BY field_name
      ORDER BY ${hasSz ? 'SUM(cluster_size) DESC' : 'COUNT(*) DESC'}
    `);

    let rawLabelCount = null;
    try {
      const { rows: [lm] } = await pool.query(
        `SELECT COUNT(DISTINCT raw_label)::int AS cnt FROM taxonomy_label_cluster_map`
      );
      rawLabelCount = lm?.cnt ?? null;
    } catch {}

    const totalItems = Number(totals.total_items) || null;
    const compressionRatio = rawLabelCount && totals.total_clusters
      ? +(rawLabelCount / totals.total_clusters).toFixed(1) : null;

    res.json({
      total_clusters:    totals.total_clusters,
      total_items:       totalItems,
      raw_label_count:   rawLabelCount,
      compression_ratio: compressionRatio,
      by_field: fields.map(f => ({
        field_name:        f.field_name,
        cluster_count:     f.cluster_count,
        label_count:       Number(f.label_count) || null,
        min_size:          f.min_size,
        max_size:          f.max_size,
        avg_size:          f.avg_size ? +Number(f.avg_size).toFixed(1) : null,
        compression_ratio: f.label_count && f.cluster_count
          ? +(Number(f.label_count) / f.cluster_count).toFixed(1) : null,
      })),
    });
  } catch (err) { console.error(err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/recovery-intelligence ────────────────────────────────────────────
app.get('/api/recovery-intelligence', async (req, res) => {
  try {
    const lmCols = await getCols('taxonomy_label_cluster_map');
    const hasBase  = lmCols.has('base_cluster_id');
    const hasFinal = lmCols.has('final_cluster_id');
    const hasField = lmCols.has('field_name');
    const finalCol = hasFinal ? 'final_cluster_id' : 'cluster_id';

    if (!hasBase) {
      return res.json({ has_recovery: false, total_labels: 0, recovered_labels: 0, rescue_rate: 0, by_field: [] });
    }

    const { rows: [totals] } = await pool.query(`
      SELECT COUNT(*)::int AS total_labels,
        COUNT(*) FILTER (WHERE base_cluster_id IS NOT NULL AND base_cluster_id != ${finalCol})::int AS recovered_labels
      FROM taxonomy_label_cluster_map
    `);

    let byField = [];
    if (hasField) {
      const { rows } = await pool.query(`
        SELECT field_name,
          COUNT(*)::int AS total_labels,
          COUNT(*) FILTER (WHERE base_cluster_id IS NOT NULL AND base_cluster_id != ${finalCol})::int AS recovered_labels
        FROM taxonomy_label_cluster_map
        GROUP BY field_name ORDER BY recovered_labels DESC
      `);
      byField = rows.map(r => ({
        ...r,
        rescue_rate: r.total_labels > 0 ? +(r.recovered_labels / r.total_labels).toFixed(3) : 0,
      }));
    }

    res.json({
      has_recovery:     true,
      total_labels:     totals.total_labels,
      recovered_labels: totals.recovered_labels,
      rescue_rate:      totals.total_labels > 0
        ? +(totals.recovered_labels / totals.total_labels).toFixed(3) : 0,
      by_field: byField,
    });
  } catch (err) { console.error(err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/medoid-intelligence ──────────────────────────────────────────────
app.get('/api/medoid-intelligence', async (req, res) => {
  try {
    const [tcCols, tcnCols] = await Promise.all([
      getCols('taxonomy_clusters'), getCols('taxonomy_cluster_names'),
    ]);
    const hasMedoid = tcCols.has('medoid_label');
    const hasSz     = tcCols.has('cluster_size');
    const nJoin     = nameJoinSql(tcCols, tcnCols);

    if (!hasMedoid) {
      return res.json({ has_medoids: false, coverage_rate: 0, strong: [], weak: [], by_field: [] });
    }

    const { rows: [counts] } = await pool.query(`
      SELECT COUNT(*)::int AS total,
        COUNT(*) FILTER (WHERE medoid_label IS NOT NULL AND medoid_label != '')::int AS with_medoid
      FROM taxonomy_clusters
    `);

    const { rows } = await pool.query(`
      SELECT tc.id, tc.field_name, tc.cluster_id, tc.medoid_label,
        ${hasSz ? 'tc.cluster_size' : 'NULL::int AS cluster_size'},
        tcn.display_name
      FROM taxonomy_clusters tc
      LEFT JOIN taxonomy_cluster_names tcn ON ${nJoin}
      WHERE tc.medoid_label IS NOT NULL AND tc.medoid_label != ''
      ORDER BY ${hasSz ? 'tc.cluster_size DESC' : 'tc.cluster_id'}
      LIMIT 500
    `);

    const GENERIC = new Set(['other','misc','unknown','n/a','na','general','default',
      'various','miscellaneous','true','false','yes','no','null','undefined','none']);
    const isWeak = l => {
      if (!l) return true;
      const s = l.toLowerCase().trim();
      return GENERIC.has(s) || s.length <= 2 || /^\d+(\.\d+)?$/.test(s);
    };

    const strong = rows.filter(r => !isWeak(r.medoid_label)).slice(0, 8);
    const weak   = rows.filter(r =>  isWeak(r.medoid_label)).slice(0, 8);

    const fieldMap = {};
    for (const r of rows) {
      if (!fieldMap[r.field_name]) fieldMap[r.field_name] = { field_name: r.field_name, total: 0, weak: 0 };
      fieldMap[r.field_name].total++;
      if (isWeak(r.medoid_label)) fieldMap[r.field_name].weak++;
    }

    res.json({
      has_medoids:    true,
      total_clusters: counts.total,
      with_medoid:    counts.with_medoid,
      coverage_rate:  counts.total > 0 ? +(counts.with_medoid / counts.total).toFixed(3) : 0,
      strong: strong.map(r => ({ id: r.id, field_name: r.field_name, medoid_label: r.medoid_label, cluster_size: r.cluster_size, display_name: r.display_name })),
      weak:   weak.map(r =>   ({ id: r.id, field_name: r.field_name, medoid_label: r.medoid_label, cluster_size: r.cluster_size, display_name: r.display_name })),
      by_field: Object.values(fieldMap).sort((a, b) => b.weak - a.weak).map(f => ({
        ...f, weak_rate: f.total > 0 ? +(f.weak / f.total).toFixed(3) : 0,
      })),
    });
  } catch (err) { console.error(err.message); res.status(500).json({ error: err.message }); }
});


// ── GET /api/duplicate-name-intelligence ─────────────────────────────────────
app.get('/api/duplicate-name-intelligence', async (req, res) => {
  try {
    const exists = await tableExists('taxonomy_cluster_names');
    if (!exists) {
      return res.json({ same_field_duplicate_groups: 0, cross_field_duplicate_groups: 0, same_field_examples: [], cross_field_examples: [] });
    }

    const { rows: sameField } = await pool.query(`
      SELECT field_name, run_id, cluster_version, display_name, COUNT(*)::int AS cluster_count
      FROM taxonomy_cluster_names
      WHERE display_name IS NOT NULL AND TRIM(display_name) <> ''
      GROUP BY field_name, run_id, cluster_version, display_name
      HAVING COUNT(*) > 1
      ORDER BY COUNT(*) DESC, field_name, display_name
      LIMIT 50
    `);

    const { rows: crossField } = await pool.query(`
      SELECT display_name,
             COUNT(DISTINCT field_name)::int AS field_count,
             COUNT(*)::int AS cluster_count,
             array_agg(DISTINCT field_name ORDER BY field_name) AS fields
      FROM taxonomy_cluster_names
      WHERE display_name IS NOT NULL AND TRIM(display_name) <> ''
      GROUP BY display_name
      HAVING COUNT(DISTINCT field_name) > 1
      ORDER BY COUNT(DISTINCT field_name) DESC, COUNT(*) DESC, display_name
      LIMIT 50
    `);

    res.json({
      same_field_duplicate_groups: sameField.length,
      cross_field_duplicate_groups: crossField.length,
      same_field_examples: sameField,
      cross_field_examples: crossField,
    });
  } catch (err) { console.error('/api/duplicate-name-intelligence:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/run-metadata ─────────────────────────────────────────────────────
app.get('/api/run-metadata', async (req, res) => {
  try {
    const exists = await tableExists('taxonomy_run_metadata');
    if (!exists) return res.json({ runs: [], fields_with_runs: 0, latest_created_at: null, table_exists: false });

    const cols = await getCols('taxonomy_run_metadata');
    const select = [
      cols.has('run_id') ? 'run_id' : "NULL::text AS run_id",
      cols.has('field_name') ? 'field_name' : "NULL::text AS field_name",
      cols.has('model_name') ? 'model_name' : "NULL::text AS model_name",
      cols.has('embedding_device') ? 'embedding_device' : "NULL::text AS embedding_device",
      cols.has('text_mode') ? 'text_mode' : "NULL::text AS text_mode",
      cols.has('min_cluster_size') ? 'min_cluster_size' : 'NULL::int AS min_cluster_size',
      cols.has('min_samples') ? 'min_samples' : 'NULL::int AS min_samples',
      cols.has('hdbscan_metric') ? 'hdbscan_metric' : "NULL::text AS hdbscan_metric",
      cols.has('graph_k_values') ? 'graph_k_values' : "NULL::text AS graph_k_values",
      cols.has('graph_threshold_values') ? 'graph_threshold_values' : "NULL::text AS graph_threshold_values",
      cols.has('graph_resolution') ? 'graph_resolution' : 'NULL::numeric AS graph_resolution',
      cols.has('graph_min_community_size') ? 'graph_min_community_size' : 'NULL::int AS graph_min_community_size',
      cols.has('mutual_knn') ? 'mutual_knn' : 'NULL::boolean AS mutual_knn',
      cols.has('same_field_only') ? 'same_field_only' : 'NULL::boolean AS same_field_only',
      cols.has('total_labels') ? 'total_labels' : 'NULL::bigint AS total_labels',
      cols.has('total_occurrences') ? 'total_occurrences' : 'NULL::bigint AS total_occurrences',
      cols.has('base_grouped_labels') ? 'base_grouped_labels' : 'NULL::bigint AS base_grouped_labels',
      cols.has('base_anomaly_labels') ? 'base_anomaly_labels' : 'NULL::bigint AS base_anomaly_labels',
      cols.has('final_cluster_count') ? 'final_cluster_count' : 'NULL::bigint AS final_cluster_count',
      cols.has('true_anomaly_count') ? 'true_anomaly_count' : 'NULL::bigint AS true_anomaly_count',
      cols.has('created_at') ? 'created_at' : 'NULL::timestamp AS created_at',
      cols.has('updated_at') ? 'updated_at' : 'NULL::timestamp AS updated_at',
      cols.has('run_report_json') ? 'run_report_json::text AS run_report_json' : 'NULL::text AS run_report_json',
    ];

    const { rows } = await pool.query(`
      SELECT ${select.join(', ')}
      FROM taxonomy_run_metadata
      ORDER BY ${cols.has('created_at') ? 'created_at DESC NULLS LAST,' : ''} field_name NULLS LAST, run_id NULLS LAST
      LIMIT 100
    `);

    const runs = rows.map(r => {
      let report = null;
      if (r.run_report_json) {
        try { report = typeof r.run_report_json === 'string' ? JSON.parse(r.run_report_json) : r.run_report_json; } catch {}
      }
      const best = report?.strict_graph_recovery?.best_config || {};
      return {
        ...r,
        run_report_json: undefined,
        strict_recovery: report?.strict_graph_recovery ? {
          recovered_labels: report.strict_graph_recovery.recovered_labels,
          true_anomaly_labels: report.strict_graph_recovery.true_anomaly_labels,
          recovered_occurrences: report.strict_graph_recovery.recovered_occurrences,
          true_anomaly_occurrences: report.strict_graph_recovery.true_anomaly_occurrences,
          label_recovery_rate: best.label_recovery_rate,
          occurrence_recovery_rate: best.occurrence_recovery_rate,
          similarity_threshold: best.similarity_threshold,
          k_neighbors: best.k_neighbors,
          graph_communities_found: best.graph_communities_found,
        } : null,
      };
    });

    const fields = new Set(runs.map(r => r.field_name).filter(Boolean));
    res.json({
      runs,
      fields_with_runs: fields.size,
      latest_created_at: runs[0]?.created_at || null,
      table_exists: true,
    });
  } catch (err) { console.error('/api/run-metadata:', err.message); res.status(500).json({ error: err.message }); }
});



function firstNonEmpty(...values) {
  for (const value of values) {
    if (value !== undefined && value !== null && value !== '') return value;
  }
  return null;
}

function parseMaybeJson(value) {
  if (!value) return null;
  if (typeof value === 'object') return value;
  if (typeof value !== 'string') return null;
  try { return JSON.parse(value); } catch { return null; }
}

function recoveryFromReport(report) {
  const strict = report?.strict_graph_recovery || report?.strict_recovery || null;
  const best = strict?.best_config || strict?.best || {};
  if (!strict && !best) return null;
  return {
    recovered_labels: firstNonEmpty(strict?.recovered_labels, strict?.grouped_labels, best?.grouped_labels),
    true_anomaly_labels: firstNonEmpty(strict?.true_anomaly_labels, strict?.isolated_labels, best?.isolated_labels),
    recovered_occurrences: firstNonEmpty(strict?.recovered_occurrences, best?.grouped_occurrences),
    true_anomaly_occurrences: firstNonEmpty(strict?.true_anomaly_occurrences, best?.isolated_occurrences),
    label_recovery_rate: firstNonEmpty(best?.label_recovery_rate, strict?.label_recovery_rate),
    occurrence_recovery_rate: firstNonEmpty(best?.occurrence_recovery_rate, strict?.occurrence_recovery_rate),
    similarity_threshold: firstNonEmpty(best?.similarity_threshold, strict?.similarity_threshold),
    k_neighbors: firstNonEmpty(best?.k_neighbors, strict?.k_neighbors),
    graph_communities_found: firstNonEmpty(best?.graph_communities_found, strict?.graph_communities_found),
  };
}

async function computeRunStatsFromClusters({ runId, fieldName }) {
  if (!(await tableExists('taxonomy_clusters'))) return {};
  const tcCols = await getCols('taxonomy_clusters');
  const aCol = anomalyColSql(tcCols, 'tc');

  const cond = [];
  const vals = [];
  if (fieldName) {
    vals.push(fieldName);
    cond.push(`tc.field_name = $${vals.length}`);
  }
  if (runId && (tcCols.has('run_id') || tcCols.has('cluster_version'))) {
    const runConds = [];
    vals.push(runId);
    const idx = vals.length;
    if (tcCols.has('run_id')) runConds.push(`tc.run_id = $${idx}`);
    if (tcCols.has('cluster_version')) runConds.push(`tc.cluster_version = $${idx}`);
    if (runConds.length) cond.push(`(${runConds.join(' OR ')})`);
  }
  if (!cond.length) return {};

  const where = `WHERE ${cond.join(' AND ')}`;
  const { rows } = await pool.query(`
    SELECT
      COUNT(*)::int AS final_cluster_count,
      ${tcCols.has('cluster_size') ? 'COALESCE(SUM(tc.cluster_size),0)::bigint AS total_labels' : 'NULL::bigint AS total_labels'},
      ${tcCols.has('total_occurrences') ? 'COALESCE(SUM(tc.total_occurrences),0)::bigint AS total_occurrences' : 'NULL::bigint AS total_occurrences'},
      ${aCol ? `COUNT(*) FILTER (WHERE ${aCol} = true)::int AS true_anomaly_count` : 'NULL::int AS true_anomaly_count'},
      ${tcCols.has('cluster_size') && aCol ? `COALESCE(SUM(tc.cluster_size) FILTER (WHERE ${aCol} = false),0)::bigint AS base_grouped_labels` : 'NULL::bigint AS base_grouped_labels'},
      ${tcCols.has('cluster_size') && aCol ? `COALESCE(SUM(tc.cluster_size) FILTER (WHERE ${aCol} = true),0)::bigint AS base_anomaly_labels` : 'NULL::bigint AS base_anomaly_labels'}
    FROM taxonomy_clusters tc
    ${where}
  `, vals);

  const row = rows[0] || {};
  if (!Number(row.final_cluster_count || 0) && fieldName && runId) {
    return computeRunStatsFromClusters({ runId: null, fieldName });
  }
  return row;
}

async function runMetadataPayload(runId, options = {}) {
  const cleanRunId = String(runId || '').trim();
  const fieldName = String(options.fieldName || '').trim();

  const exists = await tableExists('taxonomy_run_metadata');
  if (!exists) {
    const computed = await computeRunStatsFromClusters({ runId: cleanRunId, fieldName });
    if (Object.keys(computed).length) {
      return { status: 200, body: { run_id: cleanRunId || null, field_name: fieldName || null, table_exists: false, metadata_source: 'computed_from_taxonomy_clusters', ...computed } };
    }
    return { status: 404, body: { error: 'taxonomy_run_metadata table does not exist', table_exists: false } };
  }

  if (!cleanRunId && !fieldName) return { status: 400, body: { error: 'Missing run id or field_name' } };

  const cols = await getCols('taxonomy_run_metadata');
  const pick = (name, fallback) => cols.has(name) ? name : fallback;
  const select = [
    pick('run_id', "NULL::text AS run_id"),
    pick('field_name', "NULL::text AS field_name"),
    pick('model_name', "NULL::text AS model_name"),
    pick('embedding_device', "NULL::text AS embedding_device"),
    pick('text_mode', "NULL::text AS text_mode"),
    pick('min_cluster_size', 'NULL::int AS min_cluster_size'),
    pick('min_samples', 'NULL::int AS min_samples'),
    pick('hdbscan_metric', "NULL::text AS hdbscan_metric"),
    pick('graph_k_values', "NULL::text AS graph_k_values"),
    pick('graph_threshold_values', "NULL::text AS graph_threshold_values"),
    pick('graph_resolution', 'NULL::numeric AS graph_resolution'),
    pick('graph_min_community_size', 'NULL::int AS graph_min_community_size'),
    pick('mutual_knn', 'NULL::boolean AS mutual_knn'),
    pick('same_field_only', 'NULL::boolean AS same_field_only'),
    pick('total_labels', 'NULL::bigint AS total_labels'),
    pick('total_occurrences', 'NULL::bigint AS total_occurrences'),
    pick('base_grouped_labels', 'NULL::bigint AS base_grouped_labels'),
    pick('base_anomaly_labels', 'NULL::bigint AS base_anomaly_labels'),
    pick('final_cluster_count', 'NULL::bigint AS final_cluster_count'),
    pick('true_anomaly_count', 'NULL::bigint AS true_anomaly_count'),
    pick('created_at', 'NULL::timestamp AS created_at'),
    pick('updated_at', 'NULL::timestamp AS updated_at'),
    cols.has('run_report_json') ? 'run_report_json::text AS run_report_json' : 'NULL::text AS run_report_json',
  ];

  async function queryMeta(whereSql, params, sourceLabel) {
    const { rows } = await pool.query(`
      SELECT ${select.join(', ')}
      FROM taxonomy_run_metadata
      ${whereSql}
      ORDER BY ${cols.has('created_at') ? 'created_at DESC NULLS LAST,' : ''} field_name NULLS LAST, run_id NULLS LAST
      LIMIT 1
    `, params);
    if (!rows.length) return null;
    return { row: rows[0], sourceLabel };
  }

  let found = null;
  if (cleanRunId && fieldName && cols.has('run_id') && cols.has('field_name')) {
    found = await queryMeta('WHERE run_id = $1 AND field_name = $2', [cleanRunId, fieldName], 'exact_run_and_field');
  }
  if (!found && cleanRunId && cols.has('run_id')) {
    found = await queryMeta('WHERE run_id = $1', [cleanRunId], 'exact_run');
  }
  if (!found && fieldName && cols.has('field_name')) {
    found = await queryMeta('WHERE field_name = $1', [fieldName], 'latest_field_metadata');
  }
  if (!found) {
    const computed = await computeRunStatsFromClusters({ runId: cleanRunId, fieldName });
    if (Object.keys(computed).length) {
      return { status: 200, body: { run_id: cleanRunId || null, field_name: fieldName || null, table_exists: true, metadata_source: 'computed_from_taxonomy_clusters', ...computed } };
    }
    return { status: 404, body: { error: `No metadata found for run_id ${cleanRunId || '(none)'}${fieldName ? ` / field ${fieldName}` : ''}`, table_exists: true } };
  }

  const row = found.row;
  const report = parseMaybeJson(row.run_report_json);
  const strict = recoveryFromReport(report);
  const computed = await computeRunStatsFromClusters({ runId: cleanRunId || row.run_id, fieldName: fieldName || row.field_name });

  const body = {
    ...row,
    ...Object.fromEntries(Object.entries(computed).filter(([_, v]) => v !== null && v !== undefined)),
    table_exists: true,
    metadata_source: found.sourceLabel,
    strict_recovery: strict,
  };

  // If metadata columns are sparse, fill from parsed report where possible.
  if (report) {
    body.model_name = firstNonEmpty(body.model_name, report.model_name, report.model);
    body.embedding_device = firstNonEmpty(body.embedding_device, report.embedding_device, report.device);
    body.text_mode = firstNonEmpty(body.text_mode, report.text_mode);
    body.hdbscan_metric = firstNonEmpty(body.hdbscan_metric, report.base_hdbscan?.metric);
    body.min_cluster_size = firstNonEmpty(body.min_cluster_size, report.base_hdbscan?.min_cluster_size);
    body.min_samples = firstNonEmpty(body.min_samples, report.base_hdbscan?.min_samples);
    body.total_labels = firstNonEmpty(body.total_labels, report.total_labels, report.input_labels);
    body.total_occurrences = firstNonEmpty(body.total_occurrences, report.total_occurrences);
    body.final_cluster_count = firstNonEmpty(body.final_cluster_count, report.final_cluster_count);
    body.base_grouped_labels = firstNonEmpty(body.base_grouped_labels, report.base_hdbscan?.base_grouped_labels);
    body.base_anomaly_labels = firstNonEmpty(body.base_anomaly_labels, report.base_hdbscan?.base_anomaly_labels);
  }

  return { status: 200, body };
}

async function sendRunMetadataById(req, res) {
  try {
    const payload = await runMetadataPayload(req.params.runId, { fieldName: req.query.field_name || req.query.field || '' });
    res.status(payload.status).json(payload.body);
  } catch (err) {
    console.error('/api/run-metadata/:runId:', err.message);
    res.status(500).json({ error: err.message });
  }
}

app.get('/api/run-metadata/:runId', sendRunMetadataById);
app.get('/api/taxonomy-run-metadata/:runId', sendRunMetadataById);
app.get('/api/run/:runId/metadata', sendRunMetadataById);

// Field fallback for clusters whose run_id/cluster_version is missing or stale.
app.get('/api/run-metadata-by-field/:fieldName', async (req, res) => {
  try {
    const payload = await runMetadataPayload('', { fieldName: req.params.fieldName });
    res.status(payload.status).json(payload.body);
  } catch (err) {
    console.error('/api/run-metadata-by-field/:fieldName:', err.message);
    res.status(500).json({ error: err.message });
  }
});



// ── Production mapper / Iris feeds ────────────────────────────────────────────
async function productionMapperAvailable() {
  return await tableExists('taxonomy_call_cluster_outputs');
}

function productionLimit(value, fallback = 200, max = 1000) {
  const parsed = parseInt(value, 10);
  if (!Number.isFinite(parsed)) return fallback;
  return Math.max(1, Math.min(parsed, max));
}

async function latestMapperRunId() {
  const exists = await productionMapperAvailable();
  if (!exists) return null;
  const { rows } = await pool.query(`
    SELECT mapper_run_id
    FROM taxonomy_call_cluster_outputs
    WHERE mapper_run_id IS NOT NULL
    GROUP BY mapper_run_id
    ORDER BY MAX(created_at) DESC NULLS LAST
    LIMIT 1
  `);
  return rows[0]?.mapper_run_id || null;
}

function productionRunPredicate(alias = 'o') {
  return `($1::text IS NULL OR ${alias}.mapper_run_id = $1)`;
}

// ── GET /api/production-mapper/summary ───────────────────────────────────────
app.get('/api/production-mapper/summary', async (req, res) => {
  try {
    if (!(await productionMapperAvailable())) {
      return res.json({ available: false, latest_run_id: null, summary: null, field_health: [], emerging: [], config_issues: [] });
    }

    const requestedRun = req.query.run_id ? String(req.query.run_id) : null;
    const runId = requestedRun || await latestMapperRunId();
    if (!runId) {
      return res.json({ available: true, latest_run_id: null, summary: null, field_health: [], emerging: [], config_issues: [] });
    }

    const { rows: [summary] } = await pool.query(`
      SELECT
        mapper_run_id,
        COUNT(*)::int AS total_rows,
        COUNT(DISTINCT source_record_id)::int AS distinct_calls,
        COUNT(*) FILTER (WHERE mapping_status = 'EXISTING_CLUSTER')::int AS existing_cluster_rows,
        COUNT(*) FILTER (WHERE mapping_status = 'NEW_CLUSTER_CANDIDATE')::int AS new_cluster_candidate_rows,
        COUNT(*) FILTER (WHERE mapping_status = 'TRUE_ANOMALY')::int AS true_anomaly_rows,
        COUNT(*) FILTER (WHERE mapping_status = 'NO_CLUSTER_REFERENCE')::int AS no_cluster_reference_rows,
        COUNT(*) FILTER (WHERE mapping_method = 'exact_label_map')::int AS exact_label_map_rows,
        COUNT(*) FILTER (WHERE mapping_method = 'centroid_similarity')::int AS centroid_similarity_rows,
        COUNT(*) FILTER (WHERE mapping_method = 'near_existing_below_threshold')::int AS near_existing_rows,
        MIN(mapper_window_start) AS mapper_window_start,
        MAX(mapper_window_end) AS mapper_window_end,
        MAX(classified_at) AS latest_classified_at,
        MAX(created_at) AS last_written_at,
        CASE WHEN COUNT(*) > 0 THEN COUNT(*) FILTER (WHERE mapping_status = 'EXISTING_CLUSTER')::numeric / COUNT(*) ELSE NULL END AS existing_cluster_rate,
        CASE WHEN COUNT(*) > 0 THEN COUNT(*) FILTER (WHERE mapping_status IN ('NEW_CLUSTER_CANDIDATE','TRUE_ANOMALY'))::numeric / COUNT(*) ELSE NULL END AS emerging_rate
      FROM taxonomy_call_cluster_outputs
      WHERE mapper_run_id = $1
      GROUP BY mapper_run_id
    `, [runId]);

    const { rows: fieldHealth } = await pool.query(`
      SELECT
        field_name,
        COUNT(*)::int AS total_rows,
        COUNT(DISTINCT source_record_id)::int AS distinct_calls,
        COUNT(*) FILTER (WHERE mapping_status = 'EXISTING_CLUSTER')::int AS existing_cluster_rows,
        COUNT(*) FILTER (WHERE mapping_status = 'NEW_CLUSTER_CANDIDATE')::int AS new_cluster_candidate_rows,
        COUNT(*) FILTER (WHERE mapping_status = 'TRUE_ANOMALY')::int AS true_anomaly_rows,
        COUNT(*) FILTER (WHERE mapping_status = 'NO_CLUSTER_REFERENCE')::int AS no_cluster_reference_rows,
        COUNT(*) FILTER (WHERE mapping_method = 'exact_label_map')::int AS exact_label_map_rows,
        COUNT(*) FILTER (WHERE mapping_method = 'centroid_similarity')::int AS centroid_similarity_rows,
        ROUND(AVG(similarity_score)::numeric, 4) AS avg_similarity,
        CASE WHEN COUNT(*) > 0 THEN COUNT(*) FILTER (WHERE mapping_status = 'EXISTING_CLUSTER')::numeric / COUNT(*) ELSE NULL END AS existing_cluster_rate
      FROM taxonomy_call_cluster_outputs
      WHERE mapper_run_id = $1
      GROUP BY field_name
      ORDER BY new_cluster_candidate_rows DESC, true_anomaly_rows DESC, no_cluster_reference_rows DESC, total_rows DESC, field_name
    `, [runId]);

    const { rows: emerging } = await pool.query(`
      SELECT
        mapper_run_id,
        classified_at,
        source_record_id,
        field_name,
        raw_label,
        normalized_label,
        mapped_cluster_id,
        mapped_display_name,
        similarity_score,
        mapping_status,
        mapping_method,
        top_candidates
      FROM taxonomy_call_cluster_outputs
      WHERE mapper_run_id = $1
        AND mapping_status IN ('NEW_CLUSTER_CANDIDATE','TRUE_ANOMALY')
      ORDER BY classified_at DESC NULLS LAST, similarity_score DESC NULLS LAST
      LIMIT 50
    `, [runId]);

    const { rows: configIssues } = await pool.query(`
      SELECT
        mapper_run_id,
        classified_at,
        source_record_id,
        field_name,
        raw_label,
        normalized_label,
        mapping_status,
        mapping_method
      FROM taxonomy_call_cluster_outputs
      WHERE mapper_run_id = $1
        AND mapping_status = 'NO_CLUSTER_REFERENCE'
      ORDER BY classified_at DESC NULLS LAST
      LIMIT 50
    `, [runId]);

    res.json({
      available: true,
      latest_run_id: runId,
      summary: summary || null,
      field_health: fieldHealth,
      emerging,
      config_issues: configIssues,
    });
  } catch (err) { console.error('/api/production-mapper/summary:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/production-mapper/runs ──────────────────────────────────────────
app.get('/api/production-mapper/runs', async (req, res) => {
  try {
    if (!(await productionMapperAvailable())) return res.json({ available: false, runs: [] });
    const limit = productionLimit(req.query.limit, 20, 100);
    const { rows } = await pool.query(`
      SELECT
        mapper_run_id,
        COUNT(*)::int AS total_rows,
        COUNT(DISTINCT source_record_id)::int AS distinct_calls,
        COUNT(*) FILTER (WHERE mapping_status = 'EXISTING_CLUSTER')::int AS existing_cluster_rows,
        COUNT(*) FILTER (WHERE mapping_status = 'NEW_CLUSTER_CANDIDATE')::int AS new_cluster_candidate_rows,
        COUNT(*) FILTER (WHERE mapping_status = 'TRUE_ANOMALY')::int AS true_anomaly_rows,
        COUNT(*) FILTER (WHERE mapping_status = 'NO_CLUSTER_REFERENCE')::int AS no_cluster_reference_rows,
        MIN(mapper_window_start) AS mapper_window_start,
        MAX(mapper_window_end) AS mapper_window_end,
        MAX(created_at) AS last_written_at
      FROM taxonomy_call_cluster_outputs
      WHERE mapper_run_id IS NOT NULL
      GROUP BY mapper_run_id
      ORDER BY MAX(created_at) DESC NULLS LAST
      LIMIT $1
    `, [limit]);
    res.json({ available: true, runs: rows });
  } catch (err) { console.error('/api/production-mapper/runs:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/production-mapper/canonical ─────────────────────────────────────
app.get('/api/production-mapper/canonical', async (req, res) => {
  try {
    const source = await tableExists('iris_taxonomy_canonical_feed') ? 'iris_taxonomy_canonical_feed' : 'taxonomy_call_cluster_outputs';
    if (!(await tableExists(source))) return res.json({ available: false, rows: [] });
    const runId = req.query.run_id ? String(req.query.run_id) : null;
    const limit = productionLimit(req.query.limit, 300, 2000);
    const statusFilter = source === 'taxonomy_call_cluster_outputs' ? "o.mapping_status = 'EXISTING_CLUSTER' AND" : '';
    const { rows } = await pool.query(`
      SELECT
        o.mapper_run_id,
        o.classified_at,
        o.source_record_id,
        o.field_name,
        o.raw_label,
        o.normalized_label,
        o.mapped_cluster_id,
        o.mapped_display_name,
        o.similarity_score,
        'EXISTING_CLUSTER'::text AS mapping_status,
        o.mapping_method
      FROM ${source} o
      WHERE ${statusFilter} ${productionRunPredicate('o')}
      ORDER BY o.classified_at DESC NULLS LAST, o.source_record_id, o.field_name
      LIMIT $2
    `, [runId, limit]);
    res.json({ available: true, source, rows });
  } catch (err) { console.error('/api/production-mapper/canonical:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/production-mapper/emerging ──────────────────────────────────────
app.get('/api/production-mapper/emerging', async (req, res) => {
  try {
    const source = await tableExists('iris_taxonomy_emerging_feed') ? 'iris_taxonomy_emerging_feed' : 'taxonomy_call_cluster_outputs';
    if (!(await tableExists(source))) return res.json({ available: false, rows: [] });
    const runId = req.query.run_id ? String(req.query.run_id) : null;
    const limit = productionLimit(req.query.limit, 200, 1000);
    const mappedClusterExpr = source === 'taxonomy_call_cluster_outputs' ? 'o.mapped_cluster_id' : 'NULL::text AS mapped_cluster_id';
    const mappedDisplayExpr = source === 'taxonomy_call_cluster_outputs' ? 'o.mapped_display_name' : 'NULL::text AS mapped_display_name';
    const methodExpr = source === 'taxonomy_call_cluster_outputs' ? 'o.mapping_method' : 'NULL::text AS mapping_method';
    const { rows } = await pool.query(`
      SELECT
        o.mapper_run_id,
        o.classified_at,
        o.source_record_id,
        o.field_name,
        o.raw_label,
        o.normalized_label,
        ${mappedClusterExpr},
        ${mappedDisplayExpr},
        o.similarity_score,
        o.mapping_status,
        ${methodExpr},
        o.top_candidates
      FROM ${source} o
      WHERE o.mapping_status IN ('NEW_CLUSTER_CANDIDATE','TRUE_ANOMALY')
        AND ${productionRunPredicate('o')}
      ORDER BY o.classified_at DESC NULLS LAST, o.similarity_score DESC NULLS LAST
      LIMIT $2
    `, [runId, limit]);
    res.json({ available: true, source, rows });
  } catch (err) { console.error('/api/production-mapper/emerging:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/production-mapper/config-issues ─────────────────────────────────
app.get('/api/production-mapper/config-issues', async (req, res) => {
  try {
    const source = await tableExists('iris_taxonomy_config_issue_feed') ? 'iris_taxonomy_config_issue_feed' : 'taxonomy_call_cluster_outputs';
    if (!(await tableExists(source))) return res.json({ available: false, rows: [] });
    const runId = req.query.run_id ? String(req.query.run_id) : null;
    const limit = productionLimit(req.query.limit, 200, 1000);
    const methodExpr = source === 'taxonomy_call_cluster_outputs' ? 'o.mapping_method' : 'NULL::text AS mapping_method';
    const { rows } = await pool.query(`
      SELECT
        o.mapper_run_id,
        o.classified_at,
        o.source_record_id,
        o.field_name,
        o.raw_label,
        o.normalized_label,
        o.mapping_status,
        ${methodExpr}
      FROM ${source} o
      WHERE o.mapping_status = 'NO_CLUSTER_REFERENCE'
        AND ${productionRunPredicate('o')}
      ORDER BY o.classified_at DESC NULLS LAST
      LIMIT $2
    `, [runId, limit]);
    res.json({ available: true, source, rows });
  } catch (err) { console.error('/api/production-mapper/config-issues:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/production-mapper/semantic-overlay ──────────────────────────────
app.get('/api/production-mapper/semantic-overlay', async (req, res) => {
  try {
    if (!(await productionMapperAvailable())) return res.json({ available: false, latest_run_id: null, rows: [] });
    const requestedRun = req.query.run_id ? String(req.query.run_id) : null;
    const runId = requestedRun || await latestMapperRunId();
    if (!runId) return res.json({ available: true, latest_run_id: null, rows: [] });

    const { rows } = await pool.query(`
      SELECT
        field_name,
        mapped_cluster_id,
        MAX(mapped_display_name) AS mapped_display_name,
        COUNT(*)::int AS production_hit_count,
        COUNT(DISTINCT source_record_id)::int AS production_distinct_calls,
        MAX(classified_at) AS latest_classified_at,
        ARRAY_AGG(DISTINCT raw_label) FILTER (WHERE raw_label IS NOT NULL) AS raw_labels
      FROM taxonomy_call_cluster_outputs
      WHERE mapper_run_id = $1
        AND mapping_status = 'EXISTING_CLUSTER'
        AND mapped_cluster_id IS NOT NULL
      GROUP BY field_name, mapped_cluster_id
      ORDER BY production_hit_count DESC, field_name, mapped_cluster_id
      LIMIT 1000
    `, [runId]);

    res.json({
      available: true,
      latest_run_id: runId,
      rows: rows.map(r => ({ ...r, raw_labels: Array.isArray(r.raw_labels) ? r.raw_labels.slice(0, 6) : [] })),
    });
  } catch (err) { console.error('/api/production-mapper/semantic-overlay:', err.message); res.status(500).json({ error: err.message }); }
});




function truthyQueryParam(value, defaultValue = true) {
  if (value === undefined || value === null || value === '') return defaultValue;
  return ['1', 'true', 'yes', 'y', 'on'].includes(String(value).trim().toLowerCase());
}

async function attachSemanticCallSamples(rows, options = {}) {
  const baseRows = Array.isArray(rows) ? rows : [];
  const emptyMeta = {
    available: false,
    latest_mapper_run_id: null,
    call_id_source: 'taxonomy_call_cluster_outputs.source_record_id',
  };

  if (!baseRows.length) return { rows: baseRows, meta: emptyMeta };
  if (!(await tableExists('taxonomy_call_cluster_outputs'))) return { rows: baseRows, meta: emptyMeta };

  const mapperRunId = options.mapperRunId || await latestMapperRunId();
  if (!mapperRunId) return { rows: baseRows, meta: { ...emptyMeta, available: true } };

  const sampleLimit = Math.floor(numericParam(options.sampleLimit, 8, 1, 25));
  const pairs = baseRows
    .filter(r => r?.field_name && r?.cluster_id)
    .map(r => ({ field_name: String(r.field_name), cluster_id: String(r.cluster_id) }));

  if (!pairs.length) {
    return { rows: baseRows, meta: { ...emptyMeta, available: true, latest_mapper_run_id: mapperRunId } };
  }

  const fieldNames = pairs.map(p => p.field_name);
  const clusterIds = pairs.map(p => p.cluster_id);

  const { rows: callRows } = await pool.query(`
    WITH requested AS (
      SELECT * FROM unnest($2::text[], $3::text[]) AS r(field_name, cluster_id)
    ),
    matched AS (
      SELECT
        o.field_name,
        o.mapped_cluster_id AS cluster_id,
        o.source_record_id::text AS source_record_id,
        o.classified_at
      FROM taxonomy_call_cluster_outputs o
      JOIN requested r
        ON r.field_name = o.field_name
       AND r.cluster_id = o.mapped_cluster_id
      WHERE o.mapper_run_id = $1
        AND o.mapping_status = 'EXISTING_CLUSTER'
        AND o.source_record_id IS NOT NULL
    ),
    grouped AS (
      SELECT
        field_name,
        cluster_id,
        COUNT(*)::int AS production_rows,
        COUNT(DISTINCT source_record_id)::int AS distinct_call_count,
        MAX(classified_at) AS latest_classified_at
      FROM matched
      GROUP BY field_name, cluster_id
    ),
    ranked AS (
      SELECT
        field_name,
        cluster_id,
        source_record_id,
        ROW_NUMBER() OVER (
          PARTITION BY field_name, cluster_id
          ORDER BY classified_at DESC NULLS LAST, source_record_id
        ) AS rn
      FROM (
        SELECT DISTINCT field_name, cluster_id, source_record_id, classified_at
        FROM matched
      ) d
    )
    SELECT
      g.field_name,
      g.cluster_id,
      g.production_rows,
      g.distinct_call_count,
      g.latest_classified_at,
      COALESCE(
        ARRAY_AGG(r.source_record_id ORDER BY r.rn) FILTER (WHERE r.rn <= $4),
        ARRAY[]::text[]
      ) AS sample_call_ids
    FROM grouped g
    LEFT JOIN ranked r
      ON r.field_name = g.field_name
     AND r.cluster_id = g.cluster_id
    GROUP BY g.field_name, g.cluster_id, g.production_rows, g.distinct_call_count, g.latest_classified_at
  `, [mapperRunId, fieldNames, clusterIds, sampleLimit]);

  const callMap = new Map(callRows.map(r => [`${r.field_name}:${r.cluster_id}`, r]));
  const enriched = baseRows.map(r => {
    const hit = callMap.get(`${r.field_name}:${r.cluster_id}`);
    return {
      ...r,
      semantic_distinct_calls: hit?.distinct_call_count == null ? null : Number(hit.distinct_call_count),
      semantic_production_rows: hit?.production_rows == null ? null : Number(hit.production_rows),
      sample_call_ids: Array.isArray(hit?.sample_call_ids) ? hit.sample_call_ids : [],
      latest_mapper_run_id: mapperRunId,
      call_id_source: 'taxonomy_call_cluster_outputs.source_record_id',
    };
  });

  return {
    rows: enriched,
    meta: {
      available: true,
      latest_mapper_run_id: mapperRunId,
      call_id_source: 'taxonomy_call_cluster_outputs.source_record_id',
    },
  };
}

// ── GET /api/semantic-search ─────────────────────────────────────────────────
app.get('/api/semantic-search', async (req, res) => {
  try {
    const query = String(req.query.q || req.query.query || '').trim();
    if (!query) return res.status(400).json({ error: 'Missing q parameter' });

    const fieldFilter = String(req.query.field_name || '').trim();
    const limit = Math.floor(numericParam(req.query.limit, 60, 1, 200));
    const labelLimit = Math.floor(numericParam(req.query.label_limit, 900, 50, 3000));
    const minScore = numericParam(req.query.min_score, 0.34, -1, 1);
    const includeCalls = truthyQueryParam(req.query.include_calls, true);
    const sampleCallLimit = Math.floor(numericParam(req.query.sample_call_limit, 8, 1, 25));
    const mapperRunId = req.query.mapper_run_id ? String(req.query.mapper_run_id) : null;
    const fallbackCandidateLimit = Math.floor(numericParam(
      req.query.candidate_limit,
      parseInt(process.env.SEMANTIC_SEARCH_CANDIDATE_LIMIT || '120000', 10),
      1000,
      500000
    ));

    const normalizedQuery = normalizeSemanticQueryText(query);

    // ── BGE-M3 + Voyage rerank path (SEMANTIC_SEARCH_PROVIDER=bge_voyage) ───────
    const _provider = (process.env.SEMANTIC_SEARCH_PROVIDER || '').toLowerCase().trim();
    if (_provider === 'bge_voyage') {
      const bgeTopK = parseInt(process.env.BGE_TOP_K_RETRIEVE || '200', 10);
      const bgeResult = await bgeVoyageWorker.search({
        query:      normalizedQuery,
        field_name: fieldFilter || '',
        limit,
        top_k:      bgeTopK,
        min_score:  minScore,
      });

      let bgeRows = Array.isArray(bgeResult.results) ? bgeResult.results : [];
      let bgeCallMeta = {
        available:             false,
        latest_mapper_run_id:  null,
        call_id_source:        'taxonomy_call_cluster_outputs.source_record_id',
      };
      if (includeCalls) {
        const enriched = await attachSemanticCallSamples(bgeRows,
          { mapperRunId, sampleLimit: sampleCallLimit });
        bgeRows     = enriched.rows;
        bgeCallMeta = enriched.meta;
      }

      return res.json({
        query,
        normalized_query:          normalizedQuery,
        field_name:                fieldFilter || null,
        engine:                    bgeResult.engine || 'bge_voyage',
        model:                     bgeResult.model  || null,
        dimension:                 bgeResult.dimension || null,
        searched_label_candidates: bgeResult.searched_label_candidates || bgeTopK,
        result_count:              bgeRows.length,
        include_calls:             includeCalls,
        call_id_available:         !!bgeCallMeta.available,
        latest_mapper_run_id:      bgeCallMeta.latest_mapper_run_id,
        call_id_source:            bgeCallMeta.call_id_source,
        results: bgeRows.map(r => ({
          ...r,
          semantic_score:           r.semantic_score           == null ? null : Number(r.semantic_score),
          best_label_similarity:    r.best_label_similarity    == null ? null : Number(Number(r.best_label_similarity).toFixed(4)),
          avg_label_similarity:     r.avg_label_similarity     == null ? null : Number(Number(r.avg_label_similarity).toFixed(4)),
          semantic_distinct_calls:  r.semantic_distinct_calls  == null ? null : Number(r.semantic_distinct_calls),
          semantic_production_rows: r.semantic_production_rows == null ? null : Number(r.semantic_production_rows),
          sample_call_ids:          Array.isArray(r.sample_call_ids) ? r.sample_call_ids : [],
          latest_mapper_run_id:     r.latest_mapper_run_id || bgeCallMeta.latest_mapper_run_id || null,
          call_id_source:           r.call_id_source || bgeCallMeta.call_id_source || null,
          semantic_matched_labels:  Array.isArray(r.semantic_matched_labels) ? r.semantic_matched_labels : [],
        })),
      });
    }

    // ── existing MiniLM / pgvector path ─────────────────────────────────────────
    const [tcCols, tcnCols, lmCols] = await Promise.all([
      getCols('taxonomy_clusters'),
      getCols('taxonomy_cluster_names'),
      getCols('taxonomy_label_cluster_map'),
    ]);

    const embed = await embedSemanticQuery(normalizedQuery);
    const labelConfig = await getLabelEmbeddingConfig();
    if (!labelConfig) {
      return res.status(501).json({
        error: 'taxonomy_label_embeddings table or embedding column was not found',
        query,
        hint: 'Expected one embedding column such as embedding, label_embedding, embedding_vector, label_vector, vector, or text_embedding.',
      });
    }

    const { embeddingCol, fieldCol, rawCol, normCol, isPgVector } = labelConfig;
    const lmCC = lmCols.has('final_cluster_id') ? 'final_cluster_id' : 'cluster_id';
    const mapJoin = buildLabelMapJoin({ lmCols });
    if (!mapJoin) {
      return res.status(501).json({ error: 'taxonomy_label_cluster_map does not expose raw_label or normalized_label for joining semantic hits.' });
    }

    const resultTemplate = semanticResultSelectSql(tcCols, tcnCols, lmCols).sql
      .replace(/\$MIN_SCORE/g, '$MIN_SCORE_PLACEHOLDER')
      .replace(/\$LIMIT/g, '$LIMIT_PLACEHOLDER');

    let rows = [];
    let engine = isPgVector ? 'pgvector_label_embeddings' : 'javascript_cosine_label_embeddings';
    let searchedLabels = 0;

    if (isPgVector) {
      const vals = [vectorLiteral(embed.embedding)];
      const cond = [`le.${embeddingCol} IS NOT NULL`];
      if (fieldFilter && fieldCol) {
        vals.push(fieldFilter);
        cond.push(`le.${fieldCol} = $${vals.length}`);
      }
      vals.push(labelLimit);
      const labelLimitParam = `$${vals.length}`;

      vals.push(minScore);
      const minScoreParam = `$${vals.length}`;
      vals.push(limit);
      const limitParam = `$${vals.length}`;

      const sql = `
        WITH label_hits AS (
          SELECT
            ${fieldCol ? `le.${fieldCol}` : `NULL::text`} AS field_name,
            ${rawCol ? `le.${rawCol}` : `NULL::text`} AS raw_label,
            ${normCol ? `le.${normCol}` : `NULL::text`} AS normalized_label,
            (1 - (le.${embeddingCol} <=> $1::vector))::double precision AS similarity
          FROM taxonomy_label_embeddings le
          WHERE ${cond.join(' AND ')}
          ORDER BY le.${embeddingCol} <=> $1::vector
          LIMIT ${labelLimitParam}
        ),
        mapped_hits AS (
          SELECT
            COALESCE(lm.field_name, h.field_name) AS field_name,
            lm.${lmCC},
            ${lmCols.has('run_id') ? 'lm.run_id' : 'NULL::text AS run_id'},
            COALESCE(lm.raw_label, h.raw_label) AS raw_label,
            ${lmCols.has('normalized_label') ? 'COALESCE(lm.normalized_label, h.normalized_label)' : 'h.normalized_label'} AS normalized_label,
            ${lmCols.has('value_count') ? 'lm.value_count' : '1::bigint AS value_count'},
            h.similarity
          FROM label_hits h
          JOIN taxonomy_label_cluster_map lm ON ${mapJoin}
        ),
        ${resultTemplate.replace('$MIN_SCORE_PLACEHOLDER', minScoreParam).replace('$LIMIT_PLACEHOLDER', limitParam)}
      `;
      const db = await pool.query(sql, vals);
      rows = db.rows;
      searchedLabels = labelLimit;
    } else {
      const vals = [];
      const cond = [`le.${embeddingCol} IS NOT NULL`];
      if (fieldFilter && fieldCol) {
        vals.push(fieldFilter);
        cond.push(`le.${fieldCol} = $${vals.length}`);
      }
      vals.push(fallbackCandidateLimit);

      const { rows: candidates } = await pool.query(`
        SELECT
          ${fieldCol ? `le.${fieldCol}` : `NULL::text`} AS field_name,
          ${rawCol ? `le.${rawCol}` : `NULL::text`} AS raw_label,
          ${normCol ? `le.${normCol}` : `NULL::text`} AS normalized_label,
          le.${embeddingCol}::text AS embedding
        FROM taxonomy_label_embeddings le
        WHERE ${cond.join(' AND ')}
        LIMIT $${vals.length}
      `, vals);

      searchedLabels = candidates.length;
      const topHits = candidates
        .map(r => ({
          field_name: r.field_name,
          raw_label: r.raw_label,
          normalized_label: r.normalized_label,
          similarity: cosineSimilarity(embed.embedding, parseEmbedding(r.embedding)),
        }))
        .filter(r => r.similarity != null)
        .sort((a, b) => b.similarity - a.similarity)
        .slice(0, labelLimit);

      if (topHits.length) {
        const payload = JSON.stringify(topHits);
        const vals2 = [payload, minScore, limit];
        const sql = `
          WITH label_hits AS (
            SELECT * FROM jsonb_to_recordset($1::jsonb) AS h(
              field_name text,
              raw_label text,
              normalized_label text,
              similarity double precision
            )
          ),
          mapped_hits AS (
            SELECT
              COALESCE(lm.field_name, h.field_name) AS field_name,
              lm.${lmCC},
              ${lmCols.has('run_id') ? 'lm.run_id' : 'NULL::text AS run_id'},
              COALESCE(lm.raw_label, h.raw_label) AS raw_label,
              ${lmCols.has('normalized_label') ? 'COALESCE(lm.normalized_label, h.normalized_label)' : 'h.normalized_label'} AS normalized_label,
              ${lmCols.has('value_count') ? 'lm.value_count' : '1::bigint AS value_count'},
              h.similarity
            FROM label_hits h
            JOIN taxonomy_label_cluster_map lm ON ${mapJoin}
          ),
          ${resultTemplate.replace('$MIN_SCORE_PLACEHOLDER', '$2').replace('$LIMIT_PLACEHOLDER', '$3')}
        `;
        const db = await pool.query(sql, vals2);
        rows = db.rows;
      }
    }

    let callSampleMeta = {
      available: false,
      latest_mapper_run_id: null,
      call_id_source: 'taxonomy_call_cluster_outputs.source_record_id',
    };
    if (includeCalls) {
      const enriched = await attachSemanticCallSamples(rows, { mapperRunId, sampleLimit: sampleCallLimit });
      rows = enriched.rows;
      callSampleMeta = enriched.meta;
    }

    res.json({
      query,
      normalized_query: normalizedQuery,
      field_name: fieldFilter || null,
      engine,
      model: embed.model,
      dimension: embed.dimension,
      searched_label_candidates: searchedLabels,
      result_count: rows.length,
      include_calls: includeCalls,
      call_id_available: !!callSampleMeta.available,
      latest_mapper_run_id: callSampleMeta.latest_mapper_run_id,
      call_id_source: callSampleMeta.call_id_source,
      results: rows.map(r => ({
        ...r,
        semantic_score: r.semantic_score == null ? null : Number(r.semantic_score),
        best_label_similarity: r.best_label_similarity == null ? null : Number(Number(r.best_label_similarity).toFixed(4)),
        avg_label_similarity: r.avg_label_similarity == null ? null : Number(Number(r.avg_label_similarity).toFixed(4)),
        semantic_distinct_calls: r.semantic_distinct_calls == null ? null : Number(r.semantic_distinct_calls),
        semantic_production_rows: r.semantic_production_rows == null ? null : Number(r.semantic_production_rows),
        sample_call_ids: Array.isArray(r.sample_call_ids) ? r.sample_call_ids : [],
        latest_mapper_run_id: r.latest_mapper_run_id || callSampleMeta.latest_mapper_run_id || null,
        call_id_source: r.call_id_source || callSampleMeta.call_id_source || null,
        semantic_matched_labels: Array.isArray(r.semantic_matched_labels) ? r.semantic_matched_labels : [],
      })),
    });
  } catch (err) {
    console.error('/api/semantic-search:', err.message);
    res.status(500).json({
      error: err.message,
      hint: 'Semantic search requires Python sentence-transformers and a taxonomy_label_embeddings table generated with the same embedding model as the query encoder.',
    });
  }
});

// ── Semantic index refresh ─────────────────────────────────────────────────────
app.post('/api/semantic-index/refresh', async (req, res) => {
  const _provider = (process.env.SEMANTIC_SEARCH_PROVIDER || '').toLowerCase().trim();
  if (_provider !== 'bge_voyage') {
    return res.status(400).json({ error: 'Index refresh is only supported for SEMANTIC_SEARCH_PROVIDER=bge_voyage' });
  }
  try {
    console.log('[semantic-index/refresh] rebuild requested');
    const result = await bgeVoyageWorker.refresh();
    console.log(`[semantic-index/refresh] done — indexed_docs=${result.indexed_docs}`);
    return res.json({ ok: true, indexed_docs: result.indexed_docs });
  } catch (err) {
    console.error('[semantic-index/refresh] failed:', err.message);
    return res.status(500).json({ error: err.message });
  }
});

// ── 3D Projection ──────────────────────────────────────────────────────────────
const _proj = { running: false, lastRunAt: null, error: null, autoRun: false };

function runProjectionScript() {
  if (_proj.running) return;
  _proj.running = true;
  _proj.error   = null;

  const pythonExec = process.env.SEMANTIC_SEARCH_PYTHON || process.env.PYTHON || 'python';
  const scriptPath = path.resolve(__dirname, '../../generate_projection_coordinates.py');
  const methods    = process.env.PROJECTION_METHODS || 'umap_2d,umap_2d_cloud,umap_2d_legacy_cloud,umap_2d_compact_cloud,umap_3d,pca';

  console.log(`[projection] starting — methods=${methods}`);
  const proc = spawn(pythonExec, [scriptPath, '--methods', methods], {
    cwd: path.resolve(__dirname, '../..'),
    env: process.env,
  });

  proc.stdout.on('data', d => process.stdout.write('[projection] ' + d));
  proc.stderr.on('data', d => process.stderr.write('[projection] ' + d));

  proc.on('close', code => {
    _proj.running   = false;
    _proj.lastRunAt = new Date().toISOString();
    if (code === 0) {
      _proj.error = null;
      console.log('[projection] done ✓');
    } else {
      _proj.error = `Script exited with code ${code}`;
      console.error(`[projection] ✗ ${_proj.error}`);
    }
  });

  proc.on('error', err => {
    _proj.running = false;
    _proj.error   = err.message;
    console.error('[projection] spawn error:', err.message);
  });
}

// Auto-run only when explicitly opted in via AUTO_GENERATE_PROJECTION=true.
// Without this guard the server rewrites projection coordinates on every restart.
if (process.env.AUTO_GENERATE_PROJECTION === 'true') {
  setTimeout(() => {
    if (!_proj.autoRun) {
      _proj.autoRun = true;
      runProjectionScript();
    }
  }, 10000);
}

app.post('/api/projection/regenerate', (req, res) => {
  if (_proj.running) return res.json({ running: true });
  runProjectionScript();
  res.json({ started: true });
});

app.get('/api/projection/status', (_req, res) => {
  res.json({
    running:   _proj.running,
    lastRunAt: _proj.lastRunAt,
    error:     _proj.error,
    autoRun:   _proj.autoRun,
  });
});

// ── Call Evidence ─────────────────────────────────────────────────────────────
const callsPool = require('./callsDb');

// Server-side result cache so unindexed-field seq-scans only run once per session.
// Key: "fieldName\x00rawLabel"  Value: { ts, payload }
const _callsCache = new Map();
const CALLS_CACHE_TTL_MS = 5 * 60 * 1000; // 5 minutes

function getCachedCalls(key) {
  const e = _callsCache.get(key);
  if (!e) return null;
  if (Date.now() - e.ts > CALLS_CACHE_TTL_MS) { _callsCache.delete(key); return null; }
  return e.payload;
}
function setCachedCalls(key, payload) {
  _callsCache.set(key, { ts: Date.now(), payload });
  if (_callsCache.size > 400) _callsCache.delete(_callsCache.keys().next().value);
}


const TAXONOMY_FIELD_MAP = {
  call_type:            { type: 'scalar' }, // btree indexed
  call_type_base:       { type: 'scalar' }, // btree indexed
  outcome:              { type: 'scalar' }, // btree indexed
  outcome_base:         { type: 'scalar' }, // btree indexed
  main_reason:          { type: 'scalar' },
  additional_tags:      { type: 'array' }, // GIN indexed (sequence=1)
  call_type_sub:        { type: 'array' }, // GIN indexed (sequence=1)
  outcome_sub:          { type: 'array' }, // GIN indexed (sequence=1)
  tags:                 { type: 'array' }, // GIN indexed (sequence=1)
  main_reason_sub:      { type: 'array' }, // GIN indexed (sequence=1)
  next_step:            { type: 'array' }, // GIN indexed (sequence=1)
  descriptive_keywords: { type: 'array' }, // GIN indexed (sequence=1)
  coaching_tags:        { type: 'array' }, // GIN indexed (sequence=1)
};


function parseCallLabel(label) {
  if (label.startsWith('{') && label.endsWith('}')) {
    const inner = label.slice(1, -1);
    const values = inner.split(',')
      .map(s => s.trim().replace(/^"(.*)"$/, '$1'))
      .filter(Boolean);
    return { values, multi: values.length > 1 };
  }
  return { values: [label], multi: false };
}

app.get('/api/calls/by-label', async (req, res) => {
  const fieldName = (req.query.field_name || '').trim();
  const rawLabel  = (req.query.raw_label  || req.query.label || '').trim();
  const limit     = Math.min(Math.max(parseInt(req.query.limit, 10) || 25, 1), 100);
  const t0 = Date.now();

  console.log(`[calls/by-label] → field=${fieldName} label=${JSON.stringify(rawLabel)} limit=${limit}`);

  if (!fieldName || !rawLabel) {
    console.log('[calls/by-label] ✗ missing params');
    return res.status(400).json({ error: 'field_name and raw_label are required' });
  }

  const colDef = TAXONOMY_FIELD_MAP[fieldName];
  if (!colDef) {
    console.log(`[calls/by-label] ✗ unknown field: ${fieldName}`);
    return res.status(400).json({
      error: `Unknown taxonomy field: "${fieldName}". Supported: ${Object.keys(TAXONOMY_FIELD_MAP).join(', ')}`,
    });
  }

  // Parse label — strip PostgreSQL array-literal braces and split multi-value labels
  const { values: labelValues, multi: isMultiLabel } = parseCallLabel(rawLabel);
  const searchLabel = labelValues.length === 1 ? labelValues[0] : rawLabel;
  if (labelValues.join(',') !== rawLabel) {
    console.log(`[calls/by-label] label parsed: ${JSON.stringify(rawLabel)} → [${labelValues.map(v => JSON.stringify(v)).join(', ')}]`);
  }

  const isArrayField = colDef.type === 'array';
  const cacheKey = `${fieldName}\x00${rawLabel}`;

  // Serve from cache (avoids repeated slow queries)
  const cached = getCachedCalls(cacheKey);
  if (cached) {
    console.log(`[calls/by-label] cache hit for ${fieldName}:${rawLabel}`);
    return res.json({ ...cached, from_cache: true });
  }

  console.log(`[calls/by-label] field type=${colDef.type} — connecting to calls DB...`);

  let whereExpr, queryValues;
  if (isArrayField) {
    if (isMultiLabel) {
      whereExpr   = `nc."${fieldName}" @> $1::text[]`;
      queryValues = [labelValues, limit];
    } else {
      whereExpr   = `nc."${fieldName}" @> ARRAY[$1::text]`;
      queryValues = [labelValues[0], limit];
    }
  } else {
    whereExpr   = `nc."${fieldName}" = $1`;
    queryValues = [labelValues[0], limit];
  }

  console.log(`[calls/by-label] WHERE ${isMultiLabel
    ? `"${fieldName}" @> [${labelValues.map(v => JSON.stringify(v)).join(',')}]`
    : `"${fieldName}" ${isArrayField ? '@>' : '='} ${JSON.stringify(queryValues[0])}`}`);

  let client;
  try {
    client = await callsPool.connect();
    console.log(`[calls/by-label] connected (${Date.now() - t0}ms) — running query...`);

    await client.query('BEGIN');
    await client.query("SET LOCAL statement_timeout = '8000'");

    const { rows } = await client.query(
      `SELECT
         nc.call_id,
         nc.call_date_time,
         nc.created_at,
         nc.agent_name,
         nc.call_summary,
         SUBSTRING(nt.transcript, 1, 400) AS transcript_preview,
         (nt.transcript IS NOT NULL AND LENGTH(nt.transcript) > 400) AS full_transcript_available
       FROM ngp_call_classification nc
       LEFT JOIN ngp_transcripts nt ON nc.call_id = nt.call_id
       WHERE ${whereExpr}
         AND nc.sequence = 1
       ORDER BY nc.call_date_time DESC
       LIMIT $2`,
      queryValues
    );
    console.log(`[calls/by-label] : ${rows.length} rows in ${Date.now() - t0}ms`);

    await client.query('COMMIT');


    const payload = {
      field_name:     fieldName,
      searched_label: searchLabel,
      limit,
      field_indexed:  colDef.indexed !== false,
      calls: rows.map(r => ({
        call_id:                   r.call_id,
        call_date:                 r.call_date_time,
        created_at:                r.created_at,
        agent_name:                r.agent_name || null,
        matched_label:             searchLabel,
        summary:                   r.call_summary || null,
        transcript_preview:        r.transcript_preview || null,
        full_transcript_available: !!r.full_transcript_available,
      })),
      has_more: rows.length === limit,
    };

    setCachedCalls(cacheKey, payload);
    return res.json(payload);
  } catch (err) {
    const elapsed = Date.now() - t0;
    if (client) { try { await client.query('ROLLBACK'); } catch (_) {} }

    const msg = err.message || '';

    // PostgreSQL error code 57014 = query_canceled (statement timeout)
    if (err.code === '57014') {
      console.error(`[calls/by-label] ✗ statement timeout after ${elapsed}ms`);
      return res.status(422).json({
        error: `Query timed out after ${elapsed}ms. This field may need a GIN index on the calls database for fast lookups.`,
      });
    }

    // Connection-level failure (pool timeout, TCP refused, ECONNREFUSED, etc.)
    const isConnErr = !client || err.code === 'ECONNREFUSED' || err.code === 'ENOTFOUND' ||
      msg.includes('timeout exceeded when trying to connect') ||
      msg.includes('connect') || msg.includes('ECONNREFUSED') || msg.includes('ETIMEDOUT');
    if (isConnErr) {
      console.error(`[calls/by-label] ✗ connection failed after ${elapsed}ms:`, msg);
      console.error(`[calls/by-label]   APP_DB_HOST=${process.env.APP_DB_HOST || '(unset)'}  APP_DB_PORT=${process.env.APP_DB_PORT || '(unset)'}  APP_DB_NAME=${process.env.APP_DB_NAME || '(unset)'}`);
      return res.status(503).json({
        error: 'Cannot reach the calls database. Make sure you are connected to the VPN and that APP_DB_HOST / APP_DB_PORT are set correctly in the root .env file.',
        detail: msg,
      });
    }
    console.error(`[calls/by-label] ✗ error after ${elapsed}ms:`, msg);
    return res.status(500).json({ error: msg });
  } finally {
    if (client) client.release();
  }
});

// ── Start ──────────────────────────────────────────────────────────────────────
const PORT = parseInt(process.env.SERVER_PORT || '5050', 10);
app.listen(PORT, () => console.log(`Taxonomy API → http://localhost:${PORT}`));
