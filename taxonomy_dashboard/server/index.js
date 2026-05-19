'use strict';

const express = require('express');
const cors    = require('cors');
const path    = require('path');

require('dotenv').config({ path: path.resolve(__dirname, '../.env') });

const pool = require('./db');
const app  = express();

app.use(cors());
app.use(express.json());

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
  const arr = Array.isArray(value) ? value : typeof value === 'string' ? JSON.parse(value) : null;
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
  const limit  = Math.min(Math.max(parseInt(filters.limit)  || 50, 1), 5000);
  const offset = Math.max(parseInt(filters.offset) || 0, 0);
  const projectionMethod = ['umap', 'tsne', 'pca'].includes(String(filters.projection || '').toLowerCase())
    ? String(filters.projection).toLowerCase()
    : 'umap';
  if (hasProjectionCoordinates) vals.push(projectionMethod);
  const projectionParam = hasProjectionCoordinates ? `$${vals.length}` : null;
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

  const projectionSelect = hasProjectionCoordinates
    ? `spc.projection_method, spc.x AS projection_x, spc.y AS projection_y, spc.z AS projection_z`
    : `'fallback'::text AS projection_method, NULL::double precision AS projection_x, NULL::double precision AS projection_y, NULL::double precision AS projection_z`;

  const projectionJoin = hasProjectionCoordinates
    ? `LEFT JOIN semantic_projection_coordinates spc
        ON spc.field_name = tc.field_name
       AND spc.cluster_id = tc.cluster_id
       AND spc.projection_method = ${projectionParam}
       AND COALESCE(spc.run_id, '') = ${runIdExpr}`
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
    ORDER BY tc.field_name, tc.cluster_id
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
        SELECT DISTINCT field_name, run_id, cluster_version, cluster_id
        FROM taxonomy_cluster_names WHERE display_name IS NOT NULL
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
      projection: req.query.projection || 'umap',
    };
    const { sql, vals } = await buildClusterQuery({ filters });
    const { rows } = await pool.query(sql, vals);
    res.json(rows);
  } catch (err) { console.error('/api/clusters:', err.message); res.status(500).json({ error: err.message }); }
});

// ── GET /api/anomalies ─────────────────────────────────────────────────────────
app.get('/api/anomalies', async (req, res) => {
  try {
    const filters = {
      field_name: req.query.field_name || '', search: req.query.search || '',
      limit: req.query.limit || 50, offset: req.query.offset || 0,
      projection: req.query.projection || 'umap',
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
    res.json(rows[0]);
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

// ── Start ──────────────────────────────────────────────────────────────────────
const PORT = parseInt(process.env.SERVER_PORT || '5050', 10);
app.listen(PORT, () => console.log(`Taxonomy API → http://localhost:${PORT}`));
