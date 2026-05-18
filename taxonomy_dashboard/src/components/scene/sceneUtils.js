// Deterministic hash helpers for stable spatial layout
export function hashStr(str) {
  let h = 0
  for (let i = 0; i < str.length; i++) {
    h = (Math.imul(31, h) + str.charCodeAt(i)) | 0
  }
  return h >>> 0
}

export function seededRand(seed) {
  const x = Math.sin(seed + 1) * 10000
  return x - Math.floor(x)
}

// Field → color mapping (stable)
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

// Generate deterministic 3D position for a cluster
export function clusterPosition(cluster, fieldIdx, numFields) {
  const id  = cluster.cluster_id || String(cluster.id || '')
  const h1  = hashStr(id)
  const h2  = hashStr(id + '_x')
  const h3  = hashStr(id + '_y')

  // Field "galaxy arm" angle — each field gets its own arm
  const armAngle  = (fieldIdx / Math.max(numFields, 1)) * Math.PI * 2
  const armRadius = 18 + seededRand(fieldIdx * 17 + 3) * 10

  // Local position within the field arm
  const localAngle = seededRand(h1) * Math.PI * 2
  const sizeWeight = Math.log(Math.max(cluster.cluster_size || 1, 1) + 1)
  const localR     = sizeWeight * 1.2 + seededRand(h2) * 5

  // Height varies by label count + noise
  const labelFactor = Math.log(Math.max(cluster.label_count || 1, 1) + 1)
  const localY      = (seededRand(h3) - 0.5) * 14 + (labelFactor - 2) * 0.8

  // Anomalies pushed to periphery
  const anomalyPush = cluster.is_true_anomaly_cluster ? 10 + seededRand(h1 * 3) * 8 : 0

  return [
    Math.cos(armAngle) * (armRadius + anomalyPush) + Math.cos(localAngle) * localR,
    localY,
    Math.sin(armAngle) * (armRadius + anomalyPush) + Math.sin(localAngle) * localR,
  ]
}

// Map clusters to spatial positions
export function buildSpatialLayout(clusters) {
  const fieldNames = [...new Set(clusters.map(c => c.field_name).filter(Boolean))]
  const fieldIdxMap = Object.fromEntries(fieldNames.map((f, i) => [f, i]))

  return clusters.map(c => ({
    ...c,
    _pos: clusterPosition(c, fieldIdxMap[c.field_name] ?? 0, fieldNames.length),
    _color: c.is_true_anomaly_cluster ? '#ef4444' : getFieldColor(c.field_name),
    _size: Math.max(0.12, Math.min(1.6, Math.sqrt(Math.max(c.cluster_size || 1, 1)) * 0.14)),
  }))
}
