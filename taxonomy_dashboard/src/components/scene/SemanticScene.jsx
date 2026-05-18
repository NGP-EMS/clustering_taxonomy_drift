import { Suspense, useEffect, useRef, useState, useMemo } from 'react'
import { Canvas, useFrame } from '@react-three/fiber'
import { OrbitControls, Stars, Grid } from '@react-three/drei'
import * as THREE from 'three'
import useStore from '../../store/useStore.js'
import ClusterCloud from './ClusterCloud.jsx'
import SceneAxes from './SceneAxes.jsx'
import AmbientParticles from './AmbientParticles.jsx'
import { buildSpatialLayout } from './sceneUtils.js'

// Animated fog/depth gradient sphere
function NebulaFog() {
  const ref = useRef()
  useFrame(({ clock }) => {
    if (!ref.current) return
    ref.current.material.opacity = 0.04 + Math.sin(clock.getElapsedTime() * 0.3) * 0.01
  })
  return (
    <mesh ref={ref}>
      <sphereGeometry args={[90, 32, 32]} />
      <meshBasicMaterial
        color="#050e1f"
        transparent
        opacity={0.05}
        side={THREE.BackSide}
        depthWrite={false}
      />
    </mesh>
  )
}

function SceneContent({ clusters, selectedId, hoveredId, onHover, onClick }) {
  const orbitRef   = useStore(s => s.cameraReset)
  const controlRef = useRef()

  // Camera reset when triggered
  useEffect(() => {
    if (controlRef.current) {
      controlRef.current.target.set(0, 0, 0)
      controlRef.current.object.position.set(0, 25, 55)
      controlRef.current.update()
    }
  }, [orbitRef])

  return (
    <>
      {/* Lighting */}
      <ambientLight intensity={0.15} color="#0a1628" />
      <pointLight position={[0, 0, 0]}  intensity={1.2} color="#1a3a6a" distance={120} />
      <pointLight position={[40, 20, 0]} intensity={0.6} color="#00d4ff" distance={80} />
      <pointLight position={[-40, -10, 0]} intensity={0.4} color="#7c3aed" distance={80} />
      <pointLight position={[0, -20, 40]} intensity={0.3} color="#10b981" distance={70} />

      {/* Background stars */}
      <Stars radius={150} depth={60} count={3000} factor={4} saturation={0.3} fade speed={0.5} />

      {/* Subtle depth grid */}
      <Grid
        position={[0, -18, 0]}
        args={[120, 120]}
        cellSize={8}
        cellThickness={0.3}
        cellColor="#0a1628"
        sectionSize={32}
        sectionThickness={0.6}
        sectionColor="#0f1f38"
        infiniteGrid
        fadeDistance={80}
        fadeStrength={2}
      />

      <NebulaFog />
      <SceneAxes length={32} />
      <AmbientParticles count={700} />

      {clusters.length > 0 && (
        <ClusterCloud
          clusters={clusters}
          selectedId={selectedId}
          hoveredId={hoveredId}
          onHover={onHover}
          onClick={onClick}
        />
      )}

      <OrbitControls
        ref={controlRef}
        enableDamping
        dampingFactor={0.06}
        rotateSpeed={0.5}
        zoomSpeed={0.8}
        minDistance={5}
        maxDistance={150}
        autoRotate
        autoRotateSpeed={0.15}
      />
    </>
  )
}

export default function SemanticScene({ clusters, onClusterClick }) {
  const {
    selectedClusterId,
    hoveredClusterId,
    setHoveredClusterId,
    setSelectedClusterId,
  } = useStore()

  const positioned = useMemo(
    () => clusters?.length ? buildSpatialLayout(clusters) : [],
    [clusters]
  )

  function handleHover(cluster) {
    setHoveredClusterId(cluster ? (cluster.id ?? null) : null)
  }

  function handleClick(cluster) {
    if (!cluster) return
    const id = cluster.id ?? null
    setSelectedClusterId(prev => prev === id ? null : id)
    onClusterClick?.(cluster)
  }

  return (
    <Canvas
      camera={{ position: [0, 25, 55], fov: 58, near: 0.1, far: 500 }}
      gl={{
        antialias: true,
        alpha: false,
        powerPreference: 'high-performance',
        toneMapping: THREE.ACESFilmicToneMapping,
        toneMappingExposure: 1.1,
      }}
      style={{ background: 'radial-gradient(ellipse at center, #030c1a 0%, #02050a 100%)' }}
    >
      <Suspense fallback={null}>
        <SceneContent
          clusters={positioned}
          selectedId={selectedClusterId}
          hoveredId={hoveredClusterId}
          onHover={handleHover}
          onClick={handleClick}
        />
      </Suspense>
    </Canvas>
  )
}
