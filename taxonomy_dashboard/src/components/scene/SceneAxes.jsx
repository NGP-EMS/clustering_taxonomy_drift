import { useRef } from 'react'
import { Html } from '@react-three/drei'
import * as THREE from 'three'

function GlowLine({ from, to, color }) {
  const points = [new THREE.Vector3(...from), new THREE.Vector3(...to)]
  const geo    = new THREE.BufferGeometry().setFromPoints(points)
  return (
    <group>
      {/* Core line */}
      <line geometry={geo}>
        <lineBasicMaterial color={color} transparent opacity={0.8} />
      </line>
      {/* Glow halo */}
      <line geometry={geo}>
        <lineBasicMaterial color={color} transparent opacity={0.15} linewidth={3} />
      </line>
    </group>
  )
}

function AxisLabel({ position, label, color }) {
  return (
    <Html position={position} center>
      <div style={{
        color,
        fontSize: '10px',
        fontFamily: '"Cascadia Code", "Fira Code", monospace',
        fontWeight: 700,
        letterSpacing: '0.08em',
        textShadow: `0 0 12px ${color}`,
        userSelect: 'none',
        pointerEvents: 'none',
        whiteSpace: 'nowrap',
        opacity: 0.85,
      }}>
        {label}
      </div>
    </Html>
  )
}

export default function SceneAxes({ length = 30 }) {
  return (
    <group>
      {/* X axis — Semantic Polarity */}
      <GlowLine from={[-length, 0, 0]} to={[length, 0, 0]} color="#00d4ff" />
      <AxisLabel position={[length + 2, 0, 0]} label="← Semantic Polarity →" color="#00d4ff" />

      {/* Y axis — Operational Intent */}
      <GlowLine from={[0, -length * 0.5, 0]} to={[0, length * 0.5, 0]} color="#a855f7" />
      <AxisLabel position={[0, length * 0.5 + 1.5, 0]} label="↑ Operational Intent" color="#a855f7" />

      {/* Z axis — Confidence / Density */}
      <GlowLine from={[0, 0, -length]} to={[0, 0, length]} color="#10b981" />
      <AxisLabel position={[0, 0, length + 2]} label="Confidence / Density →" color="#10b981" />

      {/* Origin glow */}
      <mesh position={[0, 0, 0]}>
        <sphereGeometry args={[0.3, 16, 16]} />
        <meshBasicMaterial color="#ffffff" transparent opacity={0.6} />
      </mesh>
    </group>
  )
}
