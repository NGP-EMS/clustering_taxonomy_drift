import { useMemo, useRef } from 'react'
import { useFrame } from '@react-three/fiber'
import { Html } from '@react-three/drei'
import * as THREE from 'three'

function GuideLine({ points, color }) {
  const ref = useRef()
  const geo = useMemo(() => new THREE.BufferGeometry().setFromPoints(points.map(p => new THREE.Vector3(...p))), [points])

  useFrame(({ clock }) => {
    if (ref.current) ref.current.material.opacity = 0.13 + Math.sin(clock.getElapsedTime() * 0.45) * 0.025
  })

  return (
    <line ref={ref} geometry={geo}>
      <lineBasicMaterial color={color} transparent opacity={0.22} depthWrite={false} />
    </line>
  )
}

function GuideLabel({ position, label, color }) {
  return (
    <Html position={position} center style={{ pointerEvents: 'none' }}>
      <div style={{
        color,
        fontSize: '8px',
        fontFamily: '"Cascadia Code","Fira Code",monospace',
        fontWeight: 600,
        letterSpacing: '0.08em',
        textShadow: `0 0 10px ${color}66`,
        userSelect: 'none',
        whiteSpace: 'nowrap',
        opacity: 0.46,
        padding: '2px 6px',
        borderRadius: 4,
        background: 'rgba(2,5,10,0.28)',
        border: `1px solid ${color}14`,
      }}>
        {label}
      </div>
    </Html>
  )
}

export default function SceneAxes({ length = 42 }) {
  return (
    <group>
      <GuideLine from="semantic" points={[[-length, 0, 0], [length, 0, 0]]} color="#00d4ff" />
      <GuideLine points={[[0, -length * 0.55, 0], [0, length * 0.55, 0]]} color="#a855f7" />
      <GuideLine points={[[0, 0, -length], [0, 0, length]]} color="#10b981" />
      <GuideLine points={[[-length * 0.55, 0, -length * 0.55], [length * 0.55, 0, length * 0.55]]} color="#f97316" />
      <GuideLine points={[[-length * 0.55, 0, length * 0.55], [length * 0.55, 0, -length * 0.55]]} color="#06b6d4" />

      <GuideLabel position={[length + 2, 0.3, 0]} label="Semantic Polarity" color="#00d4ff" />
      <GuideLabel position={[0.4, length * 0.55 + 2, 0]} label="Operational Intent" color="#a855f7" />
      <GuideLabel position={[0, 0.3, length + 2]} label="Confidence / Density" color="#10b981" />
    </group>
  )
}
