import { useRef, useMemo, useEffect, useState } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import { Html } from '@react-three/drei'
import * as THREE from 'three'

function sameId(a, b) { return String(a) === String(b) }

// ── Topology links on hover / select ─────────────────────────────────────────
function NeighborLinks({ focus, neighbors }) {
  const geo = useMemo(() => {
    if (!focus || !neighbors.length) return null
    const pts = []
    neighbors.forEach(n => {
      pts.push(focus._pos[0], focus._pos[1], 0.05)
      pts.push(n._pos[0],     n._pos[1],     0.05)
    })
    const g = new THREE.BufferGeometry()
    g.setAttribute('position', new THREE.BufferAttribute(new Float32Array(pts), 3))
    return g
  }, [focus, neighbors])

  if (!geo) return null
  return (
    <lineSegments geometry={geo} raycast={() => {}}>
      <lineBasicMaterial color="#4a5568" transparent opacity={0.45} depthWrite={false} />
    </lineSegments>
  )
}

// ── Thin ring for selected cluster ────────────────────────────────────────────
function SelectionRing({ cluster }) {
  const r = cluster._size * 1.8 + 0.3
  return (
    <mesh position={[cluster._pos[0], cluster._pos[1], 0.02]} raycast={() => {}}>
      <ringGeometry args={[r, r + 0.22, 36]} />
      <meshBasicMaterial color={cluster._color} transparent opacity={0.60} depthWrite={false} />
    </mesh>
  )
}

// ── Cluster label (zoom-gated) ────────────────────────────────────────────────
function ClusterLabel({ cluster }) {
  const name = cluster.display_name || cluster.medoid_label || cluster.representative_label
  if (!name) return null
  return (
    <Html
      position={[cluster._pos[0], cluster._pos[1] + cluster._size + 1.2, 0.1]}
      center
      style={{ pointerEvents: 'none' }}
    >
      <div style={{
        color: '#94a3b8',
        fontSize: '8px',
        fontFamily: '"Inter", "SF Pro Text", system-ui, sans-serif',
        fontWeight: 500,
        opacity: 0.82,
        userSelect: 'none',
        whiteSpace: 'nowrap',
        padding: '1px 5px',
        borderRadius: 3,
        background: 'rgba(9,13,20,0.78)',
        border: '1px solid rgba(255,255,255,0.05)',
        maxWidth: 160,
        overflow: 'hidden',
        textOverflow: 'ellipsis',
      }}>
        {name.length > 30 ? name.slice(0, 30) + '…' : name}
      </div>
    </Html>
  )
}

// ── Tooltip (hover / selected) ────────────────────────────────────────────────
function ClusterTooltip({ cluster }) {
  return (
    <Html
      position={[cluster._pos[0], cluster._pos[1] + cluster._size + 2.4, 0.2]}
      center
      style={{ pointerEvents: 'none', zIndex: 100 }}
    >
      <div style={{
        background: 'rgba(8,12,22,0.96)',
        border: '1px solid rgba(255,255,255,0.08)',
        borderLeft: `3px solid ${cluster._fieldColor || cluster._color}`,
        borderRadius: 7,
        padding: '9px 13px',
        minWidth: 162,
        maxWidth: 248,
        fontFamily: '"Inter", system-ui, sans-serif',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
          <span style={{
            fontSize: 8.5, fontWeight: 700,
            color: cluster._fieldColor || cluster._color,
            letterSpacing: '0.06em', textTransform: 'uppercase',
          }}>
            {cluster.field_name}
          </span>
          {cluster.is_true_anomaly_cluster && (
            <span style={{
              fontSize: 7.5, fontWeight: 700, color: '#f87171',
              padding: '1px 5px', borderRadius: 4,
              background: 'rgba(248,113,113,0.12)',
              border: '1px solid rgba(248,113,113,0.25)',
            }}>ANOMALY</span>
          )}
        </div>
        <div style={{ fontSize: 11.5, fontWeight: 600, color: '#e2e8f0', marginBottom: 5, lineHeight: 1.35 }}>
          {cluster.display_name || cluster.medoid_label || cluster.cluster_id || 'Unnamed'}
        </div>
        <div style={{ display: 'flex', gap: 14, fontSize: 10, color: '#64748b' }}>
          <span>
            <span style={{ color: '#94a3b8' }}>{(cluster.cluster_size || 0).toLocaleString()}</span> items
          </span>
          {(cluster.total_occurrences || 0) > 0 && (
            <span>
              <span style={{ color: '#94a3b8' }}>{cluster.total_occurrences.toLocaleString()}</span> occ
            </span>
          )}
        </div>
      </div>
    </Html>
  )
}

// ── Main component ────────────────────────────────────────────────────────────
export default function ClusterCloud({
  clusters, selectedId, hoveredId, onHover, onClick,
  showLabels, renderMode, showProximity,
}) {
  const meshRef    = useRef()
  const { camera } = useThree()
  const N          = clusters.length

  const [lodLevel, setLodLevel] = useState('neighborhood')
  const lodRef = useRef(lodLevel)

  // Stable geometry — 12-segment circle of radius 1
  const diskGeo = useMemo(() => new THREE.CircleGeometry(1, 12), [])

  // Zoom → LOD (orthographic camera uses .zoom)
  useFrame(() => {
    const z    = typeof camera.zoom === 'number' ? camera.zoom : 5
    const next = z < 3.5 ? 'macro' : z < 7.5 ? 'neighborhood' : z < 14 ? 'cluster' : 'deep'
    if (next !== lodRef.current) { lodRef.current = next; setLodLevel(next) }
  })

  // Update all instance matrices + colors whenever data or selection changes
  useEffect(() => {
    if (!meshRef.current || N === 0) return

    // Allocate scratch objects per-effect-call (not module-level, avoids stale state)
    const mat   = new THREE.Matrix4()
    const pos   = new THREE.Vector3()
    const rot   = new THREE.Quaternion()   // identity (0,0,0,1)
    const scl   = new THREE.Vector3()
    const color = new THREE.Color()

    for (let i = 0; i < N; i++) {
      const c    = clusters[i]
      const isSel = sameId(selectedId, c.id)
      const isHov = sameId(hoveredId,  c.id)
      const s    = (c._size || 0.5) * (isSel ? 1.7 : isHov ? 1.4 : 1.0)

      // Force z=0 — all circles lie in the 2D map plane
      pos.set(c._pos[0], c._pos[1], 0)
      scl.setScalar(s)
      mat.compose(pos, rot, scl)
      meshRef.current.setMatrixAt(i, mat)

      color.set(isSel ? '#f8fafc' : isHov ? '#cbd5e1' : (c._color || '#6366f1'))
      meshRef.current.setColorAt(i, color)
    }

    meshRef.current.instanceMatrix.needsUpdate = true
    if (meshRef.current.instanceColor) meshRef.current.instanceColor.needsUpdate = true
  }, [clusters, selectedId, hoveredId, N])

  // Focus cluster + nearest 6 neighbors for topology links
  const { focusCluster, nearestNeighbors } = useMemo(() => {
    const focusId = selectedId ?? hoveredId
    if (focusId == null || !clusters.length) return { focusCluster: null, nearestNeighbors: [] }
    const focus = clusters.find(c => sameId(c.id, focusId))
    if (!focus) return { focusCluster: null, nearestNeighbors: [] }
    const [fx, fy] = focus._pos
    const nearest = clusters
      .filter(c => !sameId(c.id, focusId))
      .map(c => { const dx = c._pos[0]-fx, dy = c._pos[1]-fy; return { c, d2: dx*dx+dy*dy } })
      .sort((a, b) => a.d2 - b.d2)
      .slice(0, 6)
      .map(r => r.c)
    return { focusCluster: focus, nearestNeighbors: nearest }
  }, [selectedId, hoveredId, clusters])

  // Labels: always show selected/hovered; zoom-gate the rest
  const labelClusters = useMemo(() => {
    const always = clusters.filter(c => sameId(c.id, selectedId) || sameId(c.id, hoveredId))
    if (!showLabels && renderMode !== 'labels') return always

    const thresh = lodLevel === 'macro' ? 0.82 : lodLevel === 'neighborhood' ? 0.64
      : lodLevel === 'cluster' ? 0.38 : 0.12
    const cap = lodLevel === 'macro' ? 8 : lodLevel === 'neighborhood' ? 22
      : lodLevel === 'cluster' ? 60 : 140

    const extra = clusters
      .filter(c => c._sizeRatio > thresh && !sameId(c.id, selectedId) && !sameId(c.id, hoveredId))
      .slice(0, cap)

    return [...always, ...extra]
  }, [clusters, selectedId, hoveredId, showLabels, renderMode, lodLevel])

  const hoveredCluster  = hoveredId  != null ? clusters.find(c => sameId(c.id, hoveredId))  : null
  const selectedCluster = selectedId != null ? clusters.find(c => sameId(c.id, selectedId)) : null

  if (N === 0) return null

  return (
    <group>
      {/* ── Cluster circles — old proven args pattern: [geo, null, N] + child material */}
      <instancedMesh
        ref={meshRef}
        args={[diskGeo, null, N]}
        frustumCulled={false}
        onClick={e => { e.stopPropagation(); if (e.instanceId != null) onClick(clusters[e.instanceId]) }}
        onPointerMove={e => { e.stopPropagation(); if (e.instanceId != null) onHover(clusters[e.instanceId]) }}
        onPointerLeave={() => onHover(null)}
      >
        <meshBasicMaterial vertexColors transparent opacity={0.92} depthWrite={false} />
      </instancedMesh>

      {/* ── Thin selection ring ───────────────────────────────────────────── */}
      {selectedCluster && <SelectionRing cluster={selectedCluster} />}

      {/* ── Topology links ────────────────────────────────────────────────── */}
      {focusCluster && (showProximity || hoveredId != null || selectedId != null) && (
        <NeighborLinks focus={focusCluster} neighbors={nearestNeighbors} />
      )}

      {/* ── Tooltips ──────────────────────────────────────────────────────── */}
      {hoveredCluster && !sameId(hoveredCluster.id, selectedId) && <ClusterTooltip cluster={hoveredCluster} />}
      {selectedCluster && !hoveredCluster && <ClusterTooltip cluster={selectedCluster} />}

      {/* ── Zoom-gated labels ─────────────────────────────────────────────── */}
      {labelClusters.map(c => <ClusterLabel key={`lbl-${c.id}`} cluster={c} />)}
    </group>
  )
}
