export function hashStr(str) {
  let h = 0
  const s = String(str || '')
  for (let i = 0; i < s.length; i++) {
    h = (Math.imul(31, h) + s.charCodeAt(i)) | 0
  }
  return h >>> 0
}

export function seededRand(seed) {
  const x = Math.sin(Number(seed || 0) + 1) * 10000
  return x - Math.floor(x)
}

const FIELD_PALETTE = [
  '#00d4ff', '#a855f7', '#10b981', '#f97316',
  '#e879f9', '#3b82f6', '#f59e0b', '#06b6d4',
  '#8b5cf6', '#ec4899', '#14b8a6', '#84cc16',
]
const _fieldColorCache = {}
let _fieldIndex = 0

export function getFieldColor(fieldName) {
  if (!fieldName) return '#94a3b8'
  if (_fieldColorCache[fieldName]) return _fieldColorCache[fieldName]
  const color = FIELD_PALETTE[_fieldIndex % FIELD_PALETTE.length]
  _fieldColorCache[fieldName] = color
  _fieldIndex++
  return color
}

export function clusterColor(cluster, mode = 'field', stats = {}) {
  if (mode === 'anomaly') return cluster.is_true_anomaly_cluster ? '#ff4d6d' : '#10b981'
  if (mode === 'density') {
    const size = Number(cluster.cluster_size) || 1
    const t = Math.min(1, Math.log1p(size) / Math.log1p(stats.maxSize || size || 1))
    if (t > 0.75) return '#f59e0b'
    if (t > 0.45) return '#00d4ff'
    return '#64748b'
  }
  if (mode === 'quality') {
    if (cluster.is_true_anomaly_cluster) return '#ff4d6d'
    if (!cluster.display_name) return '#f59e0b'
    if (cluster.has_centroid === false) return '#a855f7'
    return '#10b981'
  }
  if (mode === 'cluster') {
    return FIELD_PALETTE[hashStr(cluster.cluster_id || cluster.id) % FIELD_PALETTE.length]
  }
  return cluster.is_true_anomaly_cluster ? '#ff4d6d' : getFieldColor(cluster.field_name)
}

function textSignature(cluster) {
  return [
    cluster.display_name,
    cluster.medoid_label,
    cluster.representative_label,
    cluster.representative_labels,
    cluster.cluster_id,
  ].filter(Boolean).join(' ')
}

function parseEmbedding(value) {
  if (!value) return null
  if (Array.isArray(value)) return value.map(Number).filter(Number.isFinite)
  if (typeof value !== 'string') return null
  try {
    const parsed = JSON.parse(value)
    return Array.isArray(parsed) ? parsed.map(Number).filter(Number.isFinite) : null
  } catch {
    return null
  }
}

function asFiniteNumber(value) {
  const n = Number(value)
  return Number.isFinite(n) ? n : null
}

function hasPersistedProjection(cluster) {
  return cluster?.projection_method && cluster.projection_method !== 'fallback'
    && asFiniteNumber(cluster.projection_x) != null
    && asFiniteNumber(cluster.projection_y) != null
    && asFiniteNumber(cluster.projection_z) != null
}

function persistedProjectionPosition(cluster, stats) {
  if (!hasPersistedProjection(cluster) || !stats.projectionScale) return null
  const x = asFiniteNumber(cluster.projection_x)
  const y = asFiniteNumber(cluster.projection_y)
  const z = asFiniteNumber(cluster.projection_z)
  if (stats.map2d) {
    return [
      (x - stats.projectionCenter[0]) * stats.projectionScale,
      (y - stats.projectionCenter[1]) * stats.projectionScale,
      0,
    ]
  }
  return [
    (x - stats.projectionCenter[0]) * stats.projectionScale,
    (y - stats.projectionCenter[1]) * stats.projectionScale,
    (z - stats.projectionCenter[2]) * stats.projectionScale,
  ]
}

function projectedEmbeddingPosition(cluster, stats) {
  const emb = parseEmbedding(cluster.centroid_embedding)
  if (!emb?.length) return null
  let x = 0, y = 0, z = 0
  for (let i = 0; i < emb.length; i++) {
    const v = emb[i]
    x += v * (seededRand(i * 17 + 3) * 2 - 1)
    y += v * (seededRand(i * 29 + 5) * 2 - 1)
    z += v * (seededRand(i * 43 + 7) * 2 - 1)
  }
  const norm = Math.sqrt(emb.reduce((s, v) => s + v * v, 0)) || 1
  const scale = stats.embeddingScale || 44
  return [x / norm * scale, y / norm * scale * 0.72, z / norm * scale]
}

export function clusterPosition(cluster, fieldIdx, numFields, stats = {}) {
  const persisted = persistedProjectionPosition(cluster, stats)
  if (persisted) return persisted

  const projected = projectedEmbeddingPosition(cluster, stats)
  if (projected) {
    if (stats.map2d) return [projected[0], projected[1], 0]
    const h = hashStr(cluster.field_name || '')
    const gentleField = numFields > 1 ? 3 + seededRand(fieldIdx * 11) * 5 : 0
    const angle = seededRand(h) * Math.PI * 2
    const anomalyPush = cluster.is_true_anomaly_cluster ? 10 + seededRand(hashStr(cluster.cluster_id || cluster.id)) * 16 : 0
    return [
      projected[0] + Math.cos(angle) * gentleField + Math.cos(angle) * anomalyPush,
      projected[1] + (seededRand(h + 4) - 0.5) * 4,
      projected[2] + Math.sin(angle) * gentleField + Math.sin(angle) * anomalyPush,
    ]
  }

  const id = cluster.cluster_id || String(cluster.id || '')
  const sig = textSignature(cluster) || id
  const h1 = hashStr(sig)
  const h2 = hashStr(`${sig}_x`)
  const h3 = hashStr(`${sig}_y`)
  const h4 = hashStr(`${cluster.field_name || ''}_field`)

  const size = Number(cluster.cluster_size) || 1
  const occ = Number(cluster.total_occurrences) || size
  const sizeT = Math.min(1, Math.log1p(size) / Math.log1p(stats.maxSize || size || 1))
  const occT = Math.min(1, Math.log1p(occ) / Math.log1p(stats.maxOcc || occ || 1))

  const theta = seededRand(h1) * Math.PI * 2
  const phi = (seededRand(h2) - 0.5) * Math.PI
  const radius = 18 + seededRand(h3) * 70 + (1 - sizeT) * 12
  const base = [
    Math.cos(theta) * Math.cos(phi) * radius,
    Math.sin(phi) * 44 + (occT - 0.5) * 18,
    Math.sin(theta) * Math.cos(phi) * radius,
  ]

  const fieldAngle = seededRand(h4) * Math.PI * 2
  const fieldStrength = numFields > 1 ? 12 + seededRand(fieldIdx * 17 + 5) * 12 : 0
  const fieldOffset = [
    Math.cos(fieldAngle) * fieldStrength,
    (seededRand(h4 + 13) - 0.5) * 12,
    Math.sin(fieldAngle) * fieldStrength,
  ]

  const anomalyAngle = seededRand(h1 * 3) * Math.PI * 2
  const anomalyPush = cluster.is_true_anomaly_cluster ? 22 + seededRand(h2 * 5) * 34 : 0
  return [
    base[0] + fieldOffset[0] + Math.cos(anomalyAngle) * anomalyPush,
    base[1] + fieldOffset[1] + (cluster.is_true_anomaly_cluster ? (seededRand(h3) - 0.5) * 22 : 0),
    stats.map2d ? 0 : base[2] + fieldOffset[2] + Math.sin(anomalyAngle) * anomalyPush,
  ]
}

export function buildSpatialLayout(clusters, options = {}) {
  const fieldNames = [...new Set(clusters.map(c => c.field_name).filter(Boolean))]
  const fieldIdxMap = Object.fromEntries(fieldNames.map((f, i) => [f, i]))
  const sizes = clusters.map(c => Number(c.cluster_size) || 1)
  const occs = clusters.map(c => Number(c.total_occurrences) || Number(c.cluster_size) || 1)
  const stats = {
    maxSize: Math.max(1, ...sizes),
    maxOcc: Math.max(1, ...occs),
    embeddingScale: options.embeddingScale || 52,
    projectionCenter: [0, 0, 0],
    projectionScale: null,
    map2d: options.viewMode !== 'galaxy',
  }

  const projectedClusters = clusters.filter(hasPersistedProjection)
  if (projectedClusters.length) {
    const xs = projectedClusters.map(c => asFiniteNumber(c.projection_x))
    const ys = projectedClusters.map(c => asFiniteNumber(c.projection_y))
    const zs = projectedClusters.map(c => asFiniteNumber(c.projection_z))
    const minX = Math.min(...xs), maxX = Math.max(...xs)
    const minY = Math.min(...ys), maxY = Math.max(...ys)
    const minZ = Math.min(...zs), maxZ = Math.max(...zs)
    const span = Math.max(maxX - minX, maxY - minY, maxZ - minZ, 1e-6)
    stats.projectionCenter = [(minX + maxX) / 2, (minY + maxY) / 2, (minZ + maxZ) / 2]
    stats.projectionScale = (options.projectionTargetSpan || (stats.map2d ? 150 : 145)) / span
  }

  return clusters.map(c => {
    const size = Number(c.cluster_size) || 1
    const occ = Number(c.total_occurrences) || size
    const sizeRatio = Math.min(1, Math.log1p(size) / Math.log1p(stats.maxSize || 1))
    const occRatio = Math.min(1, Math.log1p(occ) / Math.log1p(stats.maxOcc || 1))
    return {
      ...c,
      _pos: clusterPosition(c, fieldIdxMap[c.field_name] ?? 0, fieldNames.length, stats),
      _color: clusterColor(c, options.colorMode, stats),
      _fieldColor: getFieldColor(c.field_name),
      _sizeRatio: sizeRatio,
      _occRatio: occRatio,
      // Circle radius: map range ≈ 0.50–2.0 units (≈5.6–22px diameter at zoom 5.6)
      _size: stats.map2d
        ? 0.24 + Math.pow(sizeRatio, 0.55) * 0.82
        : 0.20 + Math.pow(sizeRatio, 0.72) * 1.10,
      _usesPersistedProjection: hasPersistedProjection(c),
    }
  })
}
