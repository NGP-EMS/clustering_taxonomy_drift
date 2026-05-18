import { useEffect, useState, useCallback } from 'react'
import { motion } from 'framer-motion'
import { X, Copy, CheckCheck, Tag, Layers, Activity, Sparkles, AlertTriangle, CheckCircle } from 'lucide-react'
import useStore from '../../store/useStore.js'
import { getFieldColor } from '../scene/sceneUtils.js'

function useCopy(text, ms = 1500) {
  const [copied, setCopied] = useState(false)
  const copy = useCallback(() => {
    if (!text) return
    navigator.clipboard.writeText(text).then(() => {
      setCopied(true)
      setTimeout(() => setCopied(false), ms)
    })
  }, [text, ms])
  return [copied, copy]
}

function StatRow({ label, value, mono, accent }) {
  if (!value && value !== 0) return null
  const colors = { cyan: '#00d4ff', violet: '#a855f7', emerald: '#10b981', red: '#ef4444' }
  return (
    <div className="flex items-start justify-between gap-3 py-1.5 border-b border-obs-border/40 last:border-0">
      <span className="text-[10px] uppercase tracking-wider text-dust/70 mt-0.5 flex-shrink-0">{label}</span>
      <span
        className={['text-[11px] text-right max-w-[60%]', mono && 'font-mono'].join(' ')}
        style={{ color: accent ? (colors[accent] || '#e2e8f0') : '#94a3b8', wordBreak: 'break-all' }}
      >
        {value}
      </span>
    </div>
  )
}

function QualityItem({ type, text }) {
  const styles = {
    ok:       { color: '#10b981', bg: 'rgba(16,185,129,0.07)',  border: 'rgba(16,185,129,0.2)'  },
    warn:     { color: '#f59e0b', bg: 'rgba(245,158,11,0.07)',  border: 'rgba(245,158,11,0.2)'  },
    critical: { color: '#ef4444', bg: 'rgba(239,68,68,0.07)',   border: 'rgba(239,68,68,0.2)'   },
  }
  const s = styles[type] || styles.ok
  const Icon = type === 'ok' ? CheckCircle : AlertTriangle
  return (
    <div className="flex items-center gap-2 rounded-md px-2.5 py-1.5 text-[11px]"
      style={{ background: s.bg, border: `1px solid ${s.border}`, color: s.color }}>
      <Icon size={10} className="flex-shrink-0" />
      <span>{text}</span>
    </div>
  )
}

function LabelBarRow({ label, count, max, color }) {
  const pct = max ? Math.max(4, (count / max) * 100) : 4
  return (
    <div className="flex items-center gap-2">
      <div className="w-10 h-1.5 rounded-full overflow-hidden bg-obs-elevated flex-shrink-0">
        <div style={{ width: `${pct}%`, background: color + '99', height: '100%', borderRadius: 999 }} />
      </div>
      <span className="flex-1 text-[11px] text-nebula truncate" title={label}>{label.length > 35 ? label.slice(0, 35) + '…' : label}</span>
      <span className="text-[10px] text-dust flex-shrink-0">{count.toLocaleString()}</span>
    </div>
  )
}

function SimilarBtn({ cluster, onClick }) {
  const fc = getFieldColor(cluster.field_name)
  return (
    <button
      onClick={() => onClick(cluster.id)}
      className="flex flex-col gap-0.5 p-2.5 rounded-lg text-left transition-all duration-150"
      style={{ background: 'rgba(255,255,255,0.025)', border: '1px solid rgba(26,45,74,0.7)' }}
      onMouseEnter={e => e.currentTarget.style.borderColor = fc + '44'}
      onMouseLeave={e => e.currentTarget.style.borderColor = 'rgba(26,45,74,0.7)'}
    >
      <span className="text-[10px] font-bold" style={{ color: fc }}>{cluster.field_name}</span>
      <span className="text-[11px] text-star truncate max-w-[130px]">{cluster.display_name || <em className="text-dust">unnamed</em>}</span>
      <span className="text-[9px] text-dust">{(cluster.cluster_size || 0).toLocaleString()} items</span>
    </button>
  )
}

export default function RightInspector({ clusterId }) {
  const setSelectedClusterId = useStore(s => s.setSelectedClusterId)
  const [cluster, setCluster] = useState(null)
  const [labels,  setLabels]  = useState([])
  const [similar, setSimilar] = useState([])
  const [loading, setLoading] = useState(false)
  const [copiedId, copyId]    = useCopy(cluster?.cluster_id)

  useEffect(() => {
    if (!clusterId) return
    setCluster(null); setLabels([]); setSimilar([])
    setLoading(true)
    Promise.allSettled([
      fetch(`/api/cluster/${clusterId}`).then(r => r.json()),
      fetch(`/api/cluster/${clusterId}/labels?limit=30`).then(r => r.json()),
      fetch(`/api/cluster/${clusterId}/similar?limit=6`).then(r => r.json()),
    ]).then(([c, l, s]) => {
      if (c.status === 'fulfilled') setCluster(c.value)
      if (l.status === 'fulfilled' && Array.isArray(l.value)) setLabels(l.value)
      if (s.status === 'fulfilled' && Array.isArray(s.value)) setSimilar(s.value)
    }).finally(() => setLoading(false))
  }, [clusterId])

  const fc       = cluster ? getFieldColor(cluster.field_name) : '#00d4ff'
  const maxCount = labels.length ? Math.max(...labels.map(l => Number(l.value_count) || 1)) : 1

  const quality = cluster ? (() => {
    const issues = [], ok = []
    if (!cluster.display_name)          issues.push({ type: 'warn',     text: 'No display name' })
    if (!cluster.has_centroid)           issues.push({ type: 'warn',     text: 'Centroid missing' })
    if (cluster.is_true_anomaly_cluster) issues.push({ type: 'critical', text: 'Flagged as anomaly' })
    if (cluster.cluster_size <= 2)       issues.push({ type: 'warn',     text: 'Micro-cluster (≤ 2 items)' })
    if (cluster.display_name)    ok.push({ type: 'ok', text: 'Named' })
    if (cluster.has_centroid)    ok.push({ type: 'ok', text: 'Centroid present' })
    if (!cluster.is_true_anomaly_cluster) ok.push({ type: 'ok', text: 'Standard cluster' })
    return [...issues, ...ok]
  })() : []

  return (
    <motion.aside
      initial={{ x: '100%', opacity: 0 }}
      animate={{ x: 0, opacity: 1 }}
      exit={{ x: '100%', opacity: 0 }}
      transition={{ type: 'spring', stiffness: 320, damping: 32 }}
      className="flex flex-col overflow-hidden flex-shrink-0"
      style={{
        width: 320,
        background: 'linear-gradient(180deg, #060d1a 0%, #03080f 100%)',
        borderLeft: '1px solid rgba(26,45,74,0.8)',
      }}
    >
      {/* Header */}
      <div
        className="flex items-center justify-between px-4 py-3 flex-shrink-0"
        style={{ borderBottom: `1px solid ${fc}22`, background: 'rgba(255,255,255,0.015)' }}
      >
        <div className="flex items-center gap-2 flex-1 min-w-0">
          <span
            className="text-[10px] font-bold px-2 py-0.5 rounded-full flex-shrink-0"
            style={{ background: fc + '18', color: fc, border: `1px solid ${fc}33` }}
          >
            {cluster?.field_name || '…'}
          </span>
          {cluster?.is_true_anomaly_cluster && (
            <span className="text-[9px] font-bold px-2 py-0.5 rounded-full flex-shrink-0"
              style={{ background: 'rgba(239,68,68,0.15)', color: '#ef4444', border: '1px solid rgba(239,68,68,0.3)' }}>
              ANOMALY
            </span>
          )}
        </div>
        <button
          onClick={() => setSelectedClusterId(null)}
          className="w-6 h-6 flex items-center justify-center rounded-md text-dust hover:text-star hover:bg-obs-elevated transition-all duration-150 flex-shrink-0"
        >
          <X size={13} />
        </button>
      </div>

      {loading && !cluster && (
        <div className="flex items-center justify-center flex-1 gap-3 text-dust text-xs">
          <div className="w-5 h-5 rounded-full border-2 border-cyan/20 border-t-cyan animate-spin" />
          Loading cluster…
        </div>
      )}

      {cluster && (
        <div className="flex-1 overflow-y-auto" style={{ scrollbarWidth: 'thin', scrollbarColor: '#1a2d4a transparent' }}>
          {/* Hero */}
          <div className="px-4 pt-4 pb-3" style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
            <div className="text-[15px] font-bold text-star leading-snug mb-1.5">
              {cluster.display_name || <span className="text-dust italic">Unnamed Cluster</span>}
            </div>
            <div
              className="flex items-center gap-2 group cursor-pointer"
              onClick={copyId}
            >
              <span className="text-[10px] font-mono text-dust truncate">{cluster.cluster_id}</span>
              <span className="text-dust/40 group-hover:text-cyan transition-colors duration-150">
                {copiedId ? <CheckCheck size={10} style={{ color: '#10b981' }} /> : <Copy size={10} />}
              </span>
            </div>
            {cluster.naming_reason && (
              <div className="mt-2 text-[11px] text-nebula italic leading-snug"
                style={{ borderLeft: `2px solid ${fc}`, paddingLeft: 8 }}>
                "{cluster.naming_reason}"
              </div>
            )}
          </div>

          {/* Key metrics */}
          <div className="grid grid-cols-3 divide-x divide-obs-border/50"
            style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
            {[
              { v: cluster.cluster_size?.toLocaleString(), l: 'Items', c: fc },
              { v: labels.length,                         l: 'Labels', c: '#a855f7' },
              { v: cluster.total_occurrences?.toLocaleString(), l: 'Occ.', c: '#10b981' },
            ].map(({ v, l, c }) => (
              <div key={l} className="flex flex-col items-center py-3">
                <span className="text-[18px] font-bold" style={{ color: c }}>{v ?? '—'}</span>
                <span className="text-[9px] uppercase tracking-wider text-dust mt-0.5">{l}</span>
              </div>
            ))}
          </div>

          {/* Medoid */}
          {cluster.medoid_label && (
            <div className="px-4 py-3" style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
              <div className="text-[9px] uppercase tracking-widest text-dust/60 mb-2 flex items-center gap-2">
                <Tag size={9} /> Representative Label
              </div>
              <div className="font-mono text-[11px] text-star rounded-md px-3 py-2"
                style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(26,45,74,0.7)', wordBreak: 'break-all' }}>
                {cluster.medoid_label}
              </div>
            </div>
          )}

          {/* Quality signals */}
          {quality.length > 0 && (
            <div className="px-4 py-3" style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
              <div className="text-[9px] uppercase tracking-widest text-dust/60 mb-2 flex items-center gap-2">
                <Activity size={9} /> Quality Signals
              </div>
              <div className="flex flex-col gap-1.5">
                {quality.map((q, i) => <QualityItem key={i} type={q.type} text={q.text} />)}
              </div>
            </div>
          )}

          {/* Identity */}
          <div className="px-4 py-3" style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
            <div className="text-[9px] uppercase tracking-widest text-dust/60 mb-2">Identity</div>
            <StatRow label="Field"     value={cluster.field_name} accent="cyan" />
            <StatRow label="Version"   value={cluster.cluster_version} />
            <StatRow label="Source"    value={cluster.cluster_source} />
            <StatRow label="Method"    value={cluster.naming_method} />
            <StatRow label="Threshold" value={cluster.similarity_threshold != null ? Number(cluster.similarity_threshold).toFixed(3) : null} mono />
            <StatRow label="Centroid"  value={cluster.has_centroid ? '✓ present' : '✗ missing'} accent={cluster.has_centroid ? 'emerald' : 'red'} />
            <StatRow label="Run ID"    value={cluster.run_id ? cluster.run_id.slice(0, 20) : null} mono />
          </div>

          {/* Label distribution */}
          <div className="px-4 py-3" style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
            <div className="text-[9px] uppercase tracking-widest text-dust/60 mb-2 flex items-center gap-2">
              <Layers size={9} /> Labels{labels.length ? ` · ${labels.length}` : ''}
            </div>
            {labels.length === 0 && <p className="text-[11px] text-dust">No label data.</p>}
            <div className="flex flex-col gap-2">
              {labels.map((l, i) => (
                <LabelBarRow
                  key={i}
                  label={l.raw_label}
                  count={Number(l.value_count) || 1}
                  max={maxCount}
                  color={fc}
                />
              ))}
            </div>
          </div>

          {/* Similar clusters */}
          <div className="px-4 py-3">
            <div className="text-[9px] uppercase tracking-widest text-dust/60 mb-2 flex items-center gap-2">
              <Sparkles size={9} /> Similar in Field
            </div>
            {similar.length === 0 && <p className="text-[11px] text-dust">No similar clusters found.</p>}
            <div className="grid grid-cols-2 gap-2">
              {similar.map(s => (
                <SimilarBtn key={s.id} cluster={s} onClick={id => setSelectedClusterId(id)} />
              ))}
            </div>
          </div>
        </div>
      )}
    </motion.aside>
  )
}
