import { useRef, useMemo, useEffect, useState } from 'react'
import { useFrame, useThree } from '@react-three/fiber'
import { Html } from '@react-three/drei'
import * as THREE from 'three'

// Pulsing ring for anomaly clusters
function AnomalyRing({ position, color, size }) {
  const ref = useRef()
  useFrame(({ clock }) => {
    if (!ref.current) return
    const t = clock.getElapsedTime()
    const scale = 1 + Math.sin(t * 2.5) * 0.3
    ref.current.scale.setScalar(scale)
    ref.current.material.opacity = 0.4 - Math.sin(t * 2.5) * 0.2
  })
  return (
    <mesh ref={ref} position={position}>
      <ringGeometry args={[size * 2.5, size * 3.5, 32]} />
      <meshBasicMaterial color={color} transparent opacity={0.3} side={THREE.DoubleSide} depthWrite={false} />
    </mesh>
  )
}

// Hover tooltip rendered in HTML space
function ClusterTooltip({ cluster, position }) {
  return (
    <Html position={position} style={{ pointerEvents: 'none' }}>
      <div style={{
        background: 'rgba(3,8,15,0.95)',
        border: '1px solid rgba(0,212,255,0.3)',
        borderRadius: 8,
        padding: '10px 14px',
        minWidth: 200,
        maxWidth: 260,
        backdropFilter: 'blur(12px)',
        boxShadow: '0 8px 32px rgba(0,0,0,0.6), 0 0 20px rgba(0,212,255,0.08)',
        transform: 'translate(-50%, calc(-100% - 14px))',
        fontFamily: 'Inter, system-ui, sans-serif',
      }}>
        <div style={{ display: 'flex', alignItems: 'center', gap: 6, marginBottom: 6 }}>
          <span style={{
            fontSize: 9, fontWeight: 700, padding: '1px 7px', borderRadius: 8,
            background: cluster._color + '22', color: cluster._color, border: `1px solid ${cluster._color}44`,
          }}>
            {cluster.field_name}
          </span>
          {cluster.is_true_anomaly_cluster && (
            <span style={{ fontSize: 9, padding: '1px 6px', borderRadius: 8, background: 'rgba(239,68,68,0.15)', color: '#ef4444', fontWeight: 700 }}>
              ANOMALY
            </span>
          )}
        </div>
        <div style={{ fontSize: 13, fontWeight: 600, color: '#e2e8f0', marginBottom: 5, lineHeight: 1.3 }}>
          {cluster.display_name || cluster.cluster_id || 'Unnamed'}
        </div>
        {cluster.medoid_label && (
          <div style={{ fontSize: 11, color: '#94a3b8', fontStyle: 'italic', marginBottom: 6, lineHeight: 1.3 }}>
            "{cluster.medoid_label.length > 52 ? cluster.medoid_label.slice(0, 52) + '…' : cluster.medoid_label}"
          </div>
        )}
        <div style={{ display: 'flex', gap: 12, fontSize: 11, color: '#64748b' }}>
          <span><span style={{ color: '#94a3b8' }}>{(cluster.cluster_size || 0).toLocaleString()}</span> items</span>
          <span><span style={{ color: '#94a3b8' }}>{cluster.label_count || 0}</span> labels</span>
        </div>
        {cluster.cluster_id && (
          <div style={{ marginTop: 5, fontSize: 9, color: '#334155', fontFamily: 'monospace' }}>
            {cluster.cluster_id}
          </div>
        )}
      </div>
    </Html>
  )
}

export default function ClusterCloud({
  clusters,
  selectedId,
  hoveredId,
  onHover,
  onClick,
}) {
  const meshRef    = useRef()
  const glowRef    = useRef()
  const { camera } = useThree()

  const N = clusters.length

  const { posArray, colorArray, scaleArray, idMap } = useMemo(() => {
    const posArray   = new Float32Array(N * 3)
    const colorArray = new Float32Array(N * 3)
    const scaleArray = new Float32Array(N)
    const idMap      = {}
    const c          = new THREE.Color()

    for (let i = 0; i < N; i++) {
      const cl = clusters[i]
      const [x, y, z] = cl._pos
      posArray[i*3]   = x
      posArray[i*3+1] = y
      posArray[i*3+2] = z
      c.set(cl._color)
      colorArray[i*3]   = c.r
      colorArray[i*3+1] = c.g
      colorArray[i*3+2] = c.b
      scaleArray[i]     = cl._size
      idMap[cl.id ?? cl.cluster_id] = i
    }
    return { posArray, colorArray, scaleArray, idMap }
  }, [clusters])

  // Build instanced mesh
  useEffect(() => {
    if (!meshRef.current || !glowRef.current) return
    const mat   = new THREE.Matrix4()
    const color = new THREE.Color()

    for (let i = 0; i < N; i++) {
      const cl   = clusters[i]
      const sel  = selectedId === cl.id
      const hov  = hoveredId  === cl.id
      const s    = cl._size * (sel ? 2.2 : hov ? 1.7 : 1)

      mat.compose(
        new THREE.Vector3(...cl._pos),
        new THREE.Quaternion(),
        new THREE.Vector3(s, s, s),
      )
      meshRef.current.setMatrixAt(i, mat)

      // Glow halo — larger & transparent
      const gs = s * 2.8
      mat.compose(
        new THREE.Vector3(...cl._pos),
        new THREE.Quaternion(),
        new THREE.Vector3(gs, gs, gs),
      )
      glowRef.current.setMatrixAt(i, mat)

      color.set(sel ? '#ffffff' : hov ? '#ffffff' : cl._color)
      meshRef.current.setColorAt(i, color)
      color.set(cl._color)
      glowRef.current.setColorAt(i, color)
    }
    meshRef.current.instanceMatrix.needsUpdate = true
    meshRef.current.instanceColor.needsUpdate  = true
    glowRef.current.instanceMatrix.needsUpdate  = true
    glowRef.current.instanceColor.needsUpdate   = true
  }, [clusters, selectedId, hoveredId, N])

  // Gentle ambient rotation of the whole cloud
  useFrame(({ clock }) => {
    if (!meshRef.current) return
    meshRef.current.rotation.y = clock.getElapsedTime() * 0.018
    if (glowRef.current) glowRef.current.rotation.y = meshRef.current.rotation.y
  })

  const hoveredCluster = hoveredId != null ? clusters.find(c => c.id === hoveredId) : null

  const geo = useMemo(() => new THREE.SphereGeometry(1, 8, 8), [])

  return (
    <group>
      {/* Glow halos — additive blending */}
      <instancedMesh ref={glowRef} args={[geo, null, N]}>
        <meshBasicMaterial
          transparent
          opacity={0.06}
          depthWrite={false}
          blending={THREE.AdditiveBlending}
          vertexColors
        />
      </instancedMesh>

      {/* Core cluster spheres */}
      <instancedMesh
        ref={meshRef}
        args={[geo, null, N]}
        onClick={e => {
          e.stopPropagation()
          const i = e.instanceId
          if (i != null) onClick(clusters[i])
        }}
        onPointerMove={e => {
          e.stopPropagation()
          const i = e.instanceId
          if (i != null) onHover(clusters[i])
        }}
        onPointerLeave={() => onHover(null)}
      >
        <meshStandardMaterial
          vertexColors
          roughness={0.3}
          metalness={0.1}
          emissiveIntensity={0.4}
        />
      </instancedMesh>

      {/* Anomaly pulse rings */}
      {clusters
        .filter(c => c.is_true_anomaly_cluster)
        .slice(0, 40)
        .map((c, i) => (
          <AnomalyRing key={c.id ?? i} position={c._pos} color={c._color} size={c._size} />
        ))
      }

      {/* Hover tooltip */}
      {hoveredCluster && (
        <ClusterTooltip cluster={hoveredCluster} position={hoveredCluster._pos} />
      )}
    </group>
  )
}
