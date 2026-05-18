import { useEffect, useState, useRef } from 'react'
import { motion, AnimatePresence } from 'framer-motion'
import {
  Layers, AlertTriangle, GitBranch, Cpu, Activity, RotateCcw,
  Eye, EyeOff, Tag, Maximize2, ChevronDown,
} from 'lucide-react'
import { lazy, Suspense } from 'react'
import useStore from '../store/useStore.js'
import RightInspector from '../components/layout/RightInspector.jsx'
import { getFieldColor } from '../components/scene/sceneUtils.js'
import { fmt } from '../utils/format.js'

const SemanticScene = lazy(() => import('../components/scene/SemanticScene.jsx'))

// ── Projection mode button ────────────────────────────────────────────────────
const PROJECTIONS = ['UMAP', 't-SNE', 'PCA']

// ── Field filter chip ─────────────────────────────────────────────────────────
function FieldChip({ field, active, count, onClick }) {
  const color = getFieldColor(field)
  return (
    <button
      onClick={onClick}
      className="flex items-center gap-1.5 px-3 py-1.5 rounded-full text-[10px] font-semibold transition-all duration-150"
      style={active
        ? { background: color + '22', color, border: `1px solid ${color}55`, boxShadow: `0 0 12px ${color}33` }
        : { background: 'rgba(255,255,255,0.04)', color: '#64748b', border: '1px solid rgba(26,45,74,0.6)' }
      }
    >
      <span className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: active ? color : '#334155' }} />
      {field}
      <span className="opacity-60">{count}</span>
    </button>
  )
}

// ── Bottom metric card ────────────────────────────────────────────────────────
function MetricCard({ label, value, sub, color = '#00d4ff', icon: Icon, glow }) {
  return (
    <div
      className="flex flex-col gap-1 p-3 rounded-xl flex-shrink-0"
      style={{
        background: 'rgba(6,13,26,0.85)',
        border: '1px solid rgba(26,45,74,0.7)',
        backdropFilter: 'blur(12px)',
        boxShadow: glow ? `0 0 20px ${color}22` : '0 4px 20px rgba(0,0,0,0.4)',
        minWidth: 120,
      }}
    >
      <div className="flex items-center gap-1.5 mb-1">
        {Icon && <Icon size={10} style={{ color, opacity: 0.7 }} />}
        <span className="text-[9px] uppercase tracking-widest text-dust/70 font-bold">{label}</span>
      </div>
      <div className="text-[20px] font-bold leading-none" style={{ color }}>{value ?? '—'}</div>
      {sub && <div className="text-[10px] text-dust mt-0.5">{sub}</div>}
    </div>
  )
}

// ── Anomaly type dot ──────────────────────────────────────────────────────────
function AnomalyTypeDot({ type }) {
  const colors = {
    noise:             { bg: '#64748b', label: 'Noise' },
    threshold_failure: { bg: '#f59e0b', label: 'Threshold' },
    semantic_outlier:  { bg: '#f97316', label: 'Outlier' },
    emerging:          { bg: '#ef4444', label: 'Emerging' },
  }
  const c = colors[type] || colors.noise
  return (
    <div className="flex items-center gap-1.5">
      <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: c.bg, boxShadow: `0 0 5px ${c.bg}` }} />
      <span className="text-[10px] text-dust">{c.label}</span>
    </div>
  )
}

export default function Observatory() {
  const {
    selectedClusterId, setSelectedClusterId,
    activeField, setActiveField,
    projectionMode, setProjectionMode,
    showLabels, setShowLabels,
    anomalyFilter, setAnomalyFilter,
    triggerCameraReset,
    health,
  } = useStore()

  const [clusters,     setClusters]     = useState([])
  const [loading,      setLoading]      = useState(false)
  const [anomalySummary, setAnomalySummary] = useState(null)
  const [compression,  setCompression]  = useState(null)
  const [showControls, setShowControls] = useState(true)
  const [showBottomPanel, setShowBottomPanel] = useState(true)

  useEffect(() => {
    setLoading(true)
    Promise.allSettled([
      fetch('/api/clusters?limit=1500&offset=0').then(r => r.json()),
      fetch('/api/anomaly-intelligence').then(r => r.json()),
      fetch('/api/semantic-compression').then(r => r.json()),
    ]).then(([cl, an, comp]) => {
      if (cl.status === 'fulfilled' && Array.isArray(cl.value)) setClusters(cl.value)
      if (an.status === 'fulfilled') setAnomalySummary(an.value?.summary)
      if (comp.status === 'fulfilled') setCompression(comp.value)
    }).finally(() => setLoading(false))
  }, [])

  // Filtered clusters for display
  const displayClusters = clusters.filter(c => {
    if (activeField && c.field_name !== activeField) return false
    if (anomalyFilter === 'anomaly'  && !c.is_true_anomaly_cluster) return false
    if (anomalyFilter === 'standard' &&  c.is_true_anomaly_cluster) return false
    return true
  })

  // Field stats
  const fieldGroups = clusters.reduce((acc, c) => {
    acc[c.field_name] = (acc[c.field_name] || 0) + 1
    return acc
  }, {})
  const fields = Object.entries(fieldGroups).sort((a, b) => b[1] - a[1])

  const anomalyCount = health?.anomaly_clusters || anomalySummary?.total || 0

  return (
    <div className="relative flex w-full h-full overflow-hidden" style={{ background: '#02050a' }}>
      {/* Full-screen 3D canvas */}
      <div className="absolute inset-0">
        <Suspense fallback={
          <div className="flex items-center justify-center h-full flex-col gap-4">
            <div className="relative">
              <div className="w-16 h-16 rounded-full border-2 border-cyan/20 border-t-cyan animate-spin" />
              <div className="absolute inset-2 rounded-full border border-violet-bright/20 border-b-violet-bright animate-spin" style={{ animationDirection: 'reverse', animationDuration: '1.5s' }} />
            </div>
            <p className="text-dust text-xs tracking-widest uppercase">Initializing Semantic Space…</p>
          </div>
        }>
          {!loading && (
            <SemanticScene
              clusters={displayClusters}
              onClusterClick={c => setSelectedClusterId(c?.id ?? null)}
            />
          )}
          {loading && (
            <div className="flex items-center justify-center h-full flex-col gap-4">
              <div className="relative">
                <div className="w-16 h-16 rounded-full border-2 border-cyan/20 border-t-cyan animate-spin" />
                <div className="absolute inset-2 rounded-full border border-violet-bright/20 border-b-violet-bright animate-spin" style={{ animationDirection: 'reverse', animationDuration: '1.5s' }} />
              </div>
              <p className="text-dust text-xs tracking-widest uppercase">Loading semantic space…</p>
              <p className="text-[10px]" style={{ color: '#1e3450' }}>
                {clusters.length > 0 ? `${clusters.length.toLocaleString()} clusters indexed` : ''}
              </p>
            </div>
          )}
        </Suspense>
      </div>

      {/* ── Top control strip ── */}
      <div
        className="absolute top-0 left-0 right-0 z-20 flex items-center justify-between px-5 py-3"
        style={{
          background: 'linear-gradient(180deg, rgba(2,5,10,0.9) 0%, transparent 100%)',
          pointerEvents: 'none',
        }}
      >
        {/* Left: title */}
        <div style={{ pointerEvents: 'auto' }}>
          <div className="text-[11px] font-bold tracking-[0.2em] uppercase mb-0.5" style={{ color: '#00d4ff', textShadow: '0 0 12px rgba(0,212,255,0.5)' }}>
            Semantic Observatory
          </div>
          <div className="text-[10px] text-dust/60">
            {displayClusters.length.toLocaleString()} clusters · {fields.length} fields
          </div>
        </div>

        {/* Right: controls */}
        <div className="flex items-center gap-2" style={{ pointerEvents: 'auto' }}>
          {/* Projection mode */}
          <div className="flex rounded-lg overflow-hidden" style={{ border: '1px solid rgba(26,45,74,0.8)', background: 'rgba(3,8,15,0.85)', backdropFilter: 'blur(12px)' }}>
            {PROJECTIONS.map(p => (
              <button
                key={p}
                onClick={() => setProjectionMode(p.toLowerCase())}
                className="px-3 py-1.5 text-[10px] font-mono transition-all duration-150"
                style={projectionMode === p.toLowerCase()
                  ? { background: 'rgba(0,212,255,0.15)', color: '#00d4ff' }
                  : { color: '#475569' }}
              >
                {p}
              </button>
            ))}
          </div>

          {/* Anomaly filter */}
          <div className="flex rounded-lg overflow-hidden" style={{ border: '1px solid rgba(26,45,74,0.8)', background: 'rgba(3,8,15,0.85)', backdropFilter: 'blur(12px)' }}>
            {[['all','All'],['standard','Std'],['anomaly','Anom']].map(([v,l]) => (
              <button
                key={v}
                onClick={() => setAnomalyFilter(v)}
                className="px-3 py-1.5 text-[10px] transition-all duration-150"
                style={anomalyFilter === v
                  ? v === 'anomaly'
                    ? { background: 'rgba(239,68,68,0.2)', color: '#ef4444' }
                    : { background: 'rgba(0,212,255,0.12)', color: '#00d4ff' }
                  : { color: '#475569' }}
              >
                {l}
              </button>
            ))}
          </div>

          {/* Reset camera */}
          <button
            onClick={triggerCameraReset}
            className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-[10px] text-dust transition-all duration-150 hover:text-nebula"
            style={{ background: 'rgba(3,8,15,0.85)', border: '1px solid rgba(26,45,74,0.8)', backdropFilter: 'blur(12px)' }}
          >
            <RotateCcw size={10} /> Reset
          </button>
        </div>
      </div>

      {/* ── Left: field filters ── */}
      <div
        className="absolute left-3 top-1/2 -translate-y-1/2 z-20 flex flex-col gap-1.5"
        style={{ maxHeight: '60vh', overflowY: 'auto' }}
      >
        <button
          onClick={() => setActiveField(null)}
          className="px-3 py-1.5 rounded-full text-[10px] font-semibold transition-all duration-150"
          style={!activeField
            ? { background: 'rgba(0,212,255,0.15)', color: '#00d4ff', border: '1px solid rgba(0,212,255,0.4)', backdropFilter: 'blur(8px)' }
            : { background: 'rgba(3,8,15,0.7)', color: '#475569', border: '1px solid rgba(26,45,74,0.5)', backdropFilter: 'blur(8px)' }
          }
        >
          All Fields
        </button>
        {fields.map(([field, count]) => (
          <FieldChip
            key={field}
            field={field}
            count={count}
            active={activeField === field}
            onClick={() => setActiveField(activeField === field ? null : field)}
          />
        ))}
      </div>

      {/* ── Bottom metrics panel ── */}
      <AnimatePresence>
        {showBottomPanel && (
          <motion.div
            initial={{ y: '100%', opacity: 0 }}
            animate={{ y: 0, opacity: 1 }}
            exit={{ y: '100%', opacity: 0 }}
            transition={{ type: 'spring', stiffness: 280, damping: 32 }}
            className="absolute bottom-0 left-0 right-0 z-20 px-5 pb-4 pt-2"
            style={{ background: 'linear-gradient(0deg, rgba(2,5,10,0.95) 60%, transparent 100%)' }}
          >
            <div className="flex items-end justify-between gap-4">
              {/* Metric cards */}
              <div className="flex items-end gap-3 overflow-x-auto pb-1">
                <MetricCard
                  label="Total Clusters"
                  value={(health?.total_clusters || clusters.length || 0).toLocaleString()}
                  sub={`${fields.length} fields`}
                  color="#00d4ff"
                  icon={Layers}
                />
                <MetricCard
                  label="Compression"
                  value={compression?.compression_ratio != null ? `${compression.compression_ratio}×` : '—'}
                  sub="raw labels per cluster"
                  color="#a855f7"
                  icon={Cpu}
                />
                <MetricCard
                  label="Anomalies"
                  value={anomalyCount.toLocaleString()}
                  sub={anomalySummary?.by_type ? Object.keys(anomalySummary.by_type).join(' · ') : ''}
                  color="#ef4444"
                  icon={AlertTriangle}
                  glow={anomalyCount > 0}
                />
                {anomalySummary?.by_type && Object.entries(anomalySummary.by_type).map(([type, n]) => (
                  <MetricCard
                    key={type}
                    label={type.replace(/_/g, ' ')}
                    value={n}
                    color={type === 'emerging' ? '#ef4444' : type === 'semantic_outlier' ? '#f97316' : type === 'threshold_failure' ? '#f59e0b' : '#64748b'}
                    icon={Activity}
                  />
                ))}
                {compression?.raw_label_count && (
                  <MetricCard
                    label="Raw Labels"
                    value={(compression.raw_label_count || 0).toLocaleString()}
                    sub="distinct in label map"
                    color="#10b981"
                    icon={GitBranch}
                  />
                )}
              </div>

              {/* Toggle */}
              <button
                onClick={() => setShowBottomPanel(false)}
                className="flex-shrink-0 mb-1 text-dust/40 hover:text-dust transition-colors"
              >
                <ChevronDown size={16} />
              </button>
            </div>
          </motion.div>
        )}
      </AnimatePresence>

      {/* Show panel button when hidden */}
      {!showBottomPanel && (
        <button
          onClick={() => setShowBottomPanel(true)}
          className="absolute bottom-4 right-5 z-20 flex items-center gap-2 px-3 py-2 rounded-lg text-[10px] text-dust hover:text-nebula transition-all duration-150"
          style={{ background: 'rgba(3,8,15,0.85)', border: '1px solid rgba(26,45,74,0.7)', backdropFilter: 'blur(8px)' }}
        >
          <Activity size={10} /> Show Metrics
        </button>
      )}

      {/* ── Right Inspector panel ── */}
      <AnimatePresence>
        {selectedClusterId && (
          <div className="absolute right-0 top-0 bottom-0 z-30">
            <RightInspector key="inspector" clusterId={selectedClusterId} />
          </div>
        )}
      </AnimatePresence>

      {/* Node count badge (top-right corner) */}
      <div
        className="absolute top-16 right-4 z-10 flex items-center gap-2 px-3 py-1.5 rounded-lg"
        style={{ background: 'rgba(3,8,15,0.75)', border: '1px solid rgba(26,45,74,0.6)', backdropFilter: 'blur(8px)' }}
      >
        <span className="text-[10px] text-nebula">{displayClusters.length.toLocaleString()}</span>
        <span className="text-[9px] text-dust">nodes</span>
        <span className="text-dust/30">·</span>
        <span className="text-[10px]" style={{ color: '#a855f7' }}>
          {projectionMode.toUpperCase()}
        </span>
      </div>
    </div>
  )
}
