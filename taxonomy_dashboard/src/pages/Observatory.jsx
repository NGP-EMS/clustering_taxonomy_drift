import { useEffect, useState, lazy, Suspense } from 'react'
import { RotateCcw, Map, Orbit } from 'lucide-react'
import useStore from '../store/useStore.js'
import RightInspector from '../components/layout/RightInspector.jsx'
import { getFieldColor } from '../components/scene/sceneUtils.js'

const SemanticScene = lazy(() => import('../components/scene/SemanticScene.jsx'))

// ── Mini sparkline ─────────────────────────────────────────────────────────────
function Sparkline({ seed = 2.1, color = '#00d4ff', width = 78, height = 30 }) {
  const pts = Array.from({ length: 10 }, (_, i) => {
    const base = 0.2 + 0.55 * (i / 9)
    const wave = Math.sin(i * seed * 2.7 + seed * 0.9) * 0.14
    return Math.max(0.05, Math.min(0.95, base + wave))
  })
  const w = width, h = height
  const d = pts.map((v, i) => `${i === 0 ? 'M' : 'L'}${(i * w / 9).toFixed(1)},${(h - v * h * 0.86 - 2).toFixed(1)}`).join(' ')
  const lx = w.toFixed(1), ly = (h - pts[9] * h * 0.86 - 2).toFixed(1)
  return (
    <svg width={w} height={h} style={{ display: 'block', overflow: 'visible' }}>
      <path d={d} stroke={color} strokeWidth="1.5" fill="none" strokeLinejoin="round" opacity={0.8} />
      <circle cx={lx} cy={ly} r="2.5" fill={color} style={{ filter: `drop-shadow(0 0 4px ${color})` }} />
    </svg>
  )
}

// ── Anomaly donut ──────────────────────────────────────────────────────────────
function AnomalyDonut({ pct = 0.08, color = '#ef4444' }) {
  const r = 15, cx = 19, cy = 19, circ = 2 * Math.PI * r
  const used = Math.max(0, Math.min(1, pct)) * circ
  return (
    <svg width={38} height={38} style={{ flexShrink: 0 }}>
      <circle cx={cx} cy={cy} r={r} fill="none" stroke="rgba(255,255,255,0.06)" strokeWidth={3.5} />
      <circle cx={cx} cy={cy} r={r} fill="none" stroke={color} strokeWidth={3.5}
        strokeDasharray={`${used.toFixed(1)} ${circ.toFixed(1)}`} strokeLinecap="round"
        transform={`rotate(-90 ${cx} ${cy})`} style={{ filter: `drop-shadow(0 0 4px ${color}99)` }} />
      <text x={cx} y={cy + 3.5} textAnchor="middle" fontSize="7.5" fill={color} fontWeight="700">
        {Math.round(pct * 100)}%
      </text>
    </svg>
  )
}

// ── Bottom metric card ─────────────────────────────────────────────────────────
function BottomCard({ label, color, children }) {
  return (
    <div className="flex flex-col gap-1.5 px-3 pt-2.5 pb-2 rounded-xl" style={{
      background: 'rgba(6,13,26,0.92)', border: `1px solid ${color}28`,
      backdropFilter: 'blur(16px)',
      boxShadow: `0 4px 24px rgba(0,0,0,0.55), 0 0 28px ${color}08`,
      minWidth: 0, minHeight: 88, overflow: 'hidden',
    }}>
      <div className="text-[8.5px] uppercase tracking-[0.22em] font-bold" style={{ color: color + 'aa' }}>{label}</div>
      {children}
    </div>
  )
}

function InsightLine({ children }) {
  return <div className="text-[8.5px] leading-snug" style={{ color: '#64748b' }}>{children}</div>
}

// ── Left panel helpers ─────────────────────────────────────────────────────────
function CtrlSection({ label, children }) {
  return (
    <div className="flex-shrink-0">
      <div className="text-[8px] uppercase tracking-[0.22em] font-bold pb-1.5" style={{ color: '#1e3450' }}>{label}</div>
      {children}
    </div>
  )
}

function FieldChip({ field, active, count, capped, onClick }) {
  const color = getFieldColor(field)
  return (
    <button onClick={onClick} className="w-full flex items-center gap-1.5 px-2 py-1.5 rounded-lg text-left transition-all duration-150"
      style={active
        ? { background: color + '18', border: `1px solid ${color}44`, boxShadow: `0 0 10px ${color}18` }
        : { background: 'transparent', border: '1px solid transparent' }
      }>
      <span className="w-1.5 h-1.5 rounded-full flex-shrink-0"
        style={{ background: active ? color : '#1e3450', boxShadow: active ? `0 0 5px ${color}` : 'none' }} />
      <span className="flex-1 text-[10px] truncate" style={{ color: active ? color : '#475569' }}>{field}</span>
      <span className="text-[9px]" style={{ color: active ? color + '88' : '#334155' }}>
        {count}{capped && <span style={{ color: '#475569' }} title="Sample only — more clusters exist">+</span>}
      </span>
    </button>
  )
}

// ── Default inspector (no selection) ──────────────────────────────────────────
function DefaultInspector({ health, clusters, fields }) {
  const top5 = [...clusters].sort((a, b) => (b.cluster_size || 0) - (a.cluster_size || 0)).slice(0, 5)
  return (
    <div className="flex flex-col h-full overflow-y-auto" style={{ scrollbarWidth: 'thin', scrollbarColor: '#1a2d4a transparent' }}>
      <div className="px-4 py-4 flex-shrink-0" style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
        <div className="text-[9px] uppercase tracking-[0.2em] font-bold mb-1" style={{ color: '#00d4ff88' }}>Cluster Inspector</div>
        <div className="text-[13px] font-semibold text-dust mb-0.5">Select a node</div>
        <div className="text-[10px]" style={{ color: '#475569' }}>Click any cluster in the semantic map to inspect its data.</div>
        <div className="mt-3 h-px" style={{ background: 'linear-gradient(90deg, rgba(0,212,255,0.3), transparent)' }} />
      </div>

      {health && (
        <div className="px-4 py-3 grid grid-cols-2 gap-2.5" style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
          {[
            { v: (health.total_clusters || 0).toLocaleString(),  l: 'Clusters',  c: '#00d4ff' },
            { v: health.fields_count || '—',                      l: 'Fields',    c: '#a855f7' },
            { v: (health.named_clusters || 0).toLocaleString(),  l: 'Named',     c: '#10b981' },
            { v: (health.anomaly_clusters || 0).toLocaleString(), l: 'Anomalies', c: '#ef4444' },
          ].map(({ v, l, c }) => (
            <div key={l} className="flex flex-col items-center py-2.5 rounded-lg"
              style={{ background: 'rgba(255,255,255,0.02)', border: `1px solid ${c}18` }}>
              <span className="text-[16px] font-bold" style={{ color: c, textShadow: `0 0 12px ${c}44` }}>{v}</span>
              <span className="text-[8.5px] uppercase tracking-wider text-dust mt-0.5">{l}</span>
            </div>
          ))}
        </div>
      )}

      {top5.length > 0 && (
        <div className="px-4 py-3 flex-shrink-0" style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
          <div className="text-[9px] uppercase tracking-[0.18em] text-dust/60 mb-2.5 font-bold">Largest Clusters</div>
          <div className="flex flex-col gap-1.5">
            {top5.map(c => {
              const fc = getFieldColor(c.field_name)
              return (
                <div key={c.id} className="flex items-center gap-2 px-2.5 py-2 rounded-lg"
                  style={{ background: 'rgba(255,255,255,0.02)', border: `1px solid ${fc}1a` }}>
                  <span className="w-1.5 h-1.5 rounded-full flex-shrink-0" style={{ background: fc, boxShadow: `0 0 5px ${fc}` }} />
                  <span className="flex-1 text-[11px] truncate" style={{ color: '#94a3b8' }}>
                    {c.display_name || <em style={{ color: '#475569' }}>unnamed</em>}
                  </span>
                  <span className="text-[9.5px] flex-shrink-0" style={{ color: fc + 'cc' }}>
                    {(c.cluster_size || 0).toLocaleString()}
                  </span>
                </div>
              )
            })}
          </div>
        </div>
      )}

      {fields.length > 0 && (
        <div className="px-4 py-3">
          <div className="text-[9px] uppercase tracking-[0.18em] text-dust/60 mb-2.5 font-bold">Field Distribution</div>
          <div className="flex flex-col gap-2">
            {fields.slice(0, 7).map(([field, count]) => {
              const fc = getFieldColor(field)
              const total = fields.reduce((a, [, n]) => a + n, 0)
              const pct = total ? (count / total) * 100 : 0
              return (
                <div key={field} className="flex items-center gap-2">
                  <span className="text-[9.5px] truncate" style={{ color: fc, width: 80, flexShrink: 0 }}>{field}</span>
                  <div className="flex-1 h-1 rounded-full" style={{ background: 'rgba(26,45,74,0.7)' }}>
                    <div style={{
                      width: `${pct}%`, height: '100%', borderRadius: 999,
                      background: `linear-gradient(90deg, ${fc}cc, ${fc}55)`,
                      boxShadow: `0 0 6px ${fc}44`, transition: 'width 0.8s ease',
                    }} />
                  </div>
                  <span className="text-[9px] text-dust flex-shrink-0" style={{ width: 28, textAlign: 'right' }}>{count}</span>
                </div>
              )
            })}
          </div>
        </div>
      )}
    </div>
  )
}

function SceneLoader({ label = 'Initializing…' }) {
  return (
    <div className="flex items-center justify-center h-full flex-col gap-4">
      <div className="relative">
        <div className="w-14 h-14 rounded-full border-2 animate-spin" style={{ borderColor: 'rgba(0,212,255,0.15)', borderTopColor: '#00d4ff' }} />
        <div className="absolute inset-2 rounded-full border animate-spin" style={{ borderColor: 'rgba(168,85,247,0.1)', borderBottomColor: '#a855f7', animationDirection: 'reverse', animationDuration: '1.4s' }} />
      </div>
      <p className="text-xs tracking-widest uppercase" style={{ color: '#1e3450' }}>{label}</p>
    </div>
  )
}

// ── Observatory ────────────────────────────────────────────────────────────────
export default function Observatory() {
  const {
    selectedClusterId, setSelectedClusterId,
    activeField, setActiveField,
    activeFields, setActiveFields,
    colorMode, setColorMode,
    anomalyFilter, setAnomalyFilter,
    triggerCameraReset,
    health,
  } = useStore()

  const [clusters,       setClusters]       = useState([])
  const [fieldStats,     setFieldStats]     = useState({})
  const [loading,        setLoading]        = useState(false)
  const [anomalySummary, setAnomalySummary] = useState(null)
  const [compression,    setCompression]    = useState(null)
  const [drift,          setDrift]          = useState(null)
  const [medoid,         setMedoid]         = useState(null)
  const [viewMode,       setViewMode]       = useState('map')
  const [showLabels,     setShowLabels]     = useState(false)
  const [sizeFilter,     setSizeFilter]     = useState(1)

  const selectedFields = activeFields?.length ? activeFields : (activeField ? [activeField] : [])

  useEffect(() => {
    setLoading(true)
    Promise.allSettled([
      fetch('/api/fields').then(r => r.json()),
      fetch('/api/anomaly-intelligence').then(r => r.json()),
      fetch('/api/semantic-compression').then(r => r.json()),
      fetch('/api/drift-summary').then(r => r.json()),
      fetch('/api/medoid-intelligence').then(r => r.json()),
    ]).then(([fieldsRes, an, comp, dr, med]) => {
      if (an.status === 'fulfilled')   setAnomalySummary(an.value?.summary)
      if (comp.status === 'fulfilled') setCompression(comp.value)
      if (dr.status === 'fulfilled')   setDrift(dr.value)
      if (med.status === 'fulfilled')  setMedoid(med.value)

      const fieldList = (fieldsRes.status === 'fulfilled' && Array.isArray(fieldsRes.value))
        ? fieldsRes.value : []

      if (fieldList.length === 0) {
        return fetch('/api/clusters?limit=2000&projection=umap').then(r => r.json()).then(data => {
          if (Array.isArray(data)) setClusters(data)
        })
      }

      return Promise.allSettled(
        fieldList.map(f =>
          fetch(`/api/clusters?field_name=${encodeURIComponent(f)}&limit=2000&projection=umap`).then(r => r.json())
        )
      ).then(results => {
        const seen = new Set(), merged = [], stats = {}
        results.forEach((r, idx) => {
          if (r.status !== 'fulfilled' || !Array.isArray(r.value)) return
          const field = fieldList[idx], data = r.value
          stats[field] = { rendered: data.length, capped: data.length >= 2000 }
          data.forEach(c => {
            const key = c.id ?? c.cluster_id
            if (!seen.has(key)) { seen.add(key); merged.push(c) }
          })
        })
        setClusters(merged)
        setFieldStats(stats)
      })
    }).finally(() => setLoading(false))
  }, [])

  const displayClusters = clusters.filter(c => {
    if (selectedFields.length && !selectedFields.includes(c.field_name)) return false
    if (anomalyFilter === 'anomaly'  && !c.is_true_anomaly_cluster) return false
    if (anomalyFilter === 'standard' &&  c.is_true_anomaly_cluster) return false
    if (sizeFilter > 1 && (c.cluster_size || 0) < sizeFilter)      return false
    return true
  })

  const fieldGroups = clusters.reduce((acc, c) => {
    acc[c.field_name] = (acc[c.field_name] || 0) + 1; return acc
  }, {})
  const fields = Object.entries(fieldGroups).sort((a, b) => b[1] - a[1])

  const anomalyCount  = health?.anomaly_clusters || anomalySummary?.total || 0
  const totalClusters = health?.total_clusters || clusters.length || 0
  const namedCount    = health?.named_clusters || 0
  const coveragePct   = totalClusters ? namedCount / totalClusters : 0
  const rawLabels     = compression?.raw_label_count || health?.total_label_rows || 0
  const compressionRatio = compression?.compression_ratio || (totalClusters ? rawLabels / totalClusters : null)
  const reductionPct  = rawLabels ? Math.max(0, 1 - (totalClusters / rawLabels)) : null
  const anomalyPct    = totalClusters ? anomalyCount / totalClusters : 0

  function toggleField(field) {
    setSelectedClusterId(null)
    if (!field) { setActiveFields([]); setActiveField(null); return }
    const next = selectedFields.includes(field)
      ? selectedFields.filter(f => f !== field)
      : [...selectedFields, field]
    setActiveFields(next)
    if (next.length === 0) setActiveField(null)
    if (next.length === 1) setActiveField(next[0])
  }

  return (
    <div className="flex flex-col w-full h-full overflow-hidden" style={{ background: '#02050a' }}>

      {/* ══ THREE-COLUMN MAIN AREA ══════════════════════════════════════════════ */}
      <div className="flex flex-1 overflow-hidden min-h-0">

        {/* ── LEFT CONTROL PANEL ─────────────────────────────────────────────── */}
        <div className="flex flex-col flex-shrink-0 overflow-y-auto overflow-x-hidden gap-3.5 px-3 py-3"
          style={{
            width: 176,
            background: 'linear-gradient(180deg, #070e1c 0%, #030810 100%)',
            borderRight: '1px solid rgba(26,45,74,0.65)',
            scrollbarWidth: 'thin', scrollbarColor: '#1a2d4a transparent',
          }}>

          {/* Live indicator */}
          <div className="flex-shrink-0 pb-1" style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
            <div className="text-[8.5px] uppercase tracking-[0.25em] font-bold" style={{ color: '#00d4ff', textShadow: '0 0 10px rgba(0,212,255,0.35)' }}>
              Semantic Map
            </div>
            <div className="flex items-center gap-1.5 mt-1.5">
              <span className="w-1.5 h-1.5 rounded-full flex-shrink-0 animate-pulse" style={{ background: '#10b981', boxShadow: '0 0 5px #10b981' }} />
              <span className="text-[9px]" style={{ color: '#1e3450' }}>
                {displayClusters.length.toLocaleString()}
                {clusters.length !== displayClusters.length && ` / ${clusters.length.toLocaleString()}`} nodes
              </span>
            </div>
          </div>

          {/* View */}
          <CtrlSection label="View">
            <div className="flex rounded-lg overflow-hidden" style={{ border: '1px solid rgba(26,45,74,0.65)' }}>
              {[['map', Map, 'Map'], ['3d', Orbit, '3D']].map(([mode, Icon, lbl]) => (
                <button key={mode} onClick={() => setViewMode(mode)}
                  className="flex-1 flex items-center justify-center gap-1 py-1.5 text-[9px] font-semibold transition-all duration-150"
                  style={viewMode === mode
                    ? { background: 'rgba(0,212,255,0.16)', color: '#00d4ff' }
                    : { background: 'rgba(3,8,15,0.85)', color: '#334155' }
                  }>
                  <Icon size={10} /> {lbl}
                </button>
              ))}
            </div>
          </CtrlSection>

          {/* Color By */}
          <CtrlSection label="Color By">
            <div className="flex items-center justify-between px-2.5 py-1.5 rounded-lg"
              style={{ background: 'rgba(255,255,255,0.025)', border: '1px solid rgba(26,45,74,0.65)' }}>
              <select value={colorMode} onChange={e => setColorMode(e.target.value)}
                className="w-full bg-transparent outline-none text-[10px]" style={{ color: '#94a3b8' }}>
                <option value="field">Field</option>
                <option value="cluster">Cluster</option>
                <option value="anomaly">Anomaly</option>
                <option value="density">Density</option>
                <option value="quality">Quality</option>
              </select>
            </div>
          </CtrlSection>

          {/* Filters */}
          <CtrlSection label="Filters">
            <div className="flex flex-col gap-1.5">
              {/* Labels toggle */}
              <button onClick={() => setShowLabels(v => !v)}
                className="w-full flex items-center justify-between px-2.5 py-1.5 rounded-lg transition-all duration-150"
                style={{
                  background: showLabels ? 'rgba(0,212,255,0.08)' : 'rgba(255,255,255,0.025)',
                  border: `1px solid ${showLabels ? 'rgba(0,212,255,0.35)' : 'rgba(26,45,74,0.5)'}`,
                }}>
                <span className="text-[10px]" style={{ color: showLabels ? '#94a3b8' : '#475569' }}>Labels</span>
                <div className="relative rounded-full transition-all duration-200"
                  style={{ width: 28, height: 14, background: showLabels ? 'rgba(0,212,255,0.35)' : 'rgba(26,45,74,0.8)', border: `1px solid ${showLabels ? 'rgba(0,212,255,0.6)' : 'rgba(26,45,74,0.6)'}` }}>
                  <div className="absolute top-0.5 rounded-full transition-all duration-200"
                    style={{ width: 10, height: 10, background: showLabels ? '#00d4ff' : '#334155', left: showLabels ? 15 : 2, boxShadow: showLabels ? '0 0 6px #00d4ff' : 'none' }} />
                </div>
              </button>

              <div className="flex rounded-lg overflow-hidden" style={{ border: '1px solid rgba(26,45,74,0.65)' }}>
                {[['all', 'All'], ['standard', 'Std'], ['anomaly', 'Anom']].map(([v, l]) => (
                  <button key={v} onClick={() => setAnomalyFilter(v)}
                    className="flex-1 py-1 text-[9px] transition-all duration-150"
                    style={anomalyFilter === v
                      ? v === 'anomaly'
                        ? { background: 'rgba(239,68,68,0.18)', color: '#ef4444' }
                        : { background: 'rgba(0,212,255,0.12)', color: '#00d4ff' }
                      : { background: 'rgba(3,8,15,0.85)', color: '#334155' }
                    }>{l}</button>
                ))}
              </div>

              <div>
                <div className="flex justify-between items-center mb-1">
                  <span className="text-[9px]" style={{ color: '#475569' }}>Min Size</span>
                  <span className="text-[9px] font-mono" style={{ color: '#00d4ff' }}>{sizeFilter}</span>
                </div>
                <input type="range" min={1} max={50} step={1} value={sizeFilter}
                  onChange={e => setSizeFilter(Number(e.target.value))}
                  className="w-full h-1 rounded-full cursor-pointer appearance-none"
                  style={{ accentColor: '#00d4ff', background: 'rgba(26,45,74,0.7)' }} />
              </div>
            </div>
          </CtrlSection>

          {/* Field selector */}
          <CtrlSection label="Field">
            <div className="flex flex-col gap-0.5">
              <button onClick={() => toggleField(null)}
                className="w-full flex items-center gap-1.5 px-2 py-1.5 rounded-lg text-left transition-all duration-150"
                style={!selectedFields.length
                  ? { background: 'rgba(0,212,255,0.12)', border: '1px solid rgba(0,212,255,0.32)' }
                  : { background: 'transparent', border: '1px solid transparent' }
                }>
                <span className="w-1.5 h-1.5 rounded-full flex-shrink-0"
                  style={{ background: !selectedFields.length ? '#00d4ff' : '#1e3450', boxShadow: !selectedFields.length ? '0 0 5px #00d4ff' : 'none' }} />
                <span className="flex-1 text-[10px]" style={{ color: !selectedFields.length ? '#00d4ff' : '#475569' }}>All Fields</span>
                <span className="text-[9px]" style={{ color: !selectedFields.length ? '#00d4ff88' : '#334155' }}>{clusters.length}</span>
              </button>
              {fields.map(([field, count]) => (
                <FieldChip key={field} field={field} count={count}
                  capped={fieldStats[field]?.capped ?? false}
                  active={selectedFields.includes(field)}
                  onClick={() => toggleField(field)} />
              ))}
            </div>
          </CtrlSection>

          {/* Reset view */}
          <button onClick={triggerCameraReset}
            className="flex items-center justify-center gap-1.5 rounded-lg py-2 text-[9.5px] transition-all duration-150 mt-auto flex-shrink-0"
            style={{ background: 'rgba(255,255,255,0.025)', border: '1px solid rgba(26,45,74,0.6)', color: '#475569' }}
            onMouseEnter={e => { e.currentTarget.style.color = '#00d4ff'; e.currentTarget.style.borderColor = 'rgba(0,212,255,0.3)' }}
            onMouseLeave={e => { e.currentTarget.style.color = '#475569'; e.currentTarget.style.borderColor = 'rgba(26,45,74,0.6)' }}>
            <RotateCcw size={11} /> Reset View
          </button>
        </div>

        {/* ── CENTER: SEMANTIC MAP ────────────────────────────────────────────── */}
        <div className="relative flex-1 min-w-0 overflow-hidden">
          <Suspense fallback={<SceneLoader label="Initializing Semantic Map…" />}>
            {!loading
              ? <SemanticScene clusters={displayClusters} colorMode={colorMode} viewMode={viewMode} showLabels={showLabels} />
              : <SceneLoader label="Loading semantic map…" />
            }
          </Suspense>

          {/* Top info strip */}
          <div className="absolute top-0 left-0 right-0 z-10 flex items-center justify-between px-4 py-2"
            style={{ background: 'linear-gradient(180deg, rgba(2,5,10,0.75) 0%, transparent 100%)', pointerEvents: 'none' }}>
            <span className="text-[8.5px]" style={{ color: '#1e3450' }}>
              {displayClusters.length.toLocaleString()} clusters · {fields.length} fields
            </span>
            <button onClick={triggerCameraReset}
              className="flex items-center justify-center rounded p-1.5 transition-colors"
              style={{ background: 'rgba(3,8,15,0.85)', border: '1px solid rgba(26,45,74,0.6)', color: '#334155', pointerEvents: 'auto' }}
              onMouseEnter={e => e.currentTarget.style.color = '#00d4ff'}
              onMouseLeave={e => e.currentTarget.style.color = '#334155'}>
              <RotateCcw size={10} />
            </button>
          </div>
        </div>

        {/* ── RIGHT INSPECTOR ───────────────────────────────────────────────── */}
        <div className="flex-shrink-0 overflow-hidden"
          style={{
            width: 320,
            background: 'linear-gradient(180deg, #060d1a 0%, #03080f 100%)',
            borderLeft: '1px solid rgba(26,45,74,0.75)',
          }}>
          {selectedClusterId
            ? <div className="h-full overflow-hidden"><RightInspector clusterId={selectedClusterId} /></div>
            : <DefaultInspector
                health={health}
                clusters={displayClusters}
                fields={fields.filter(([f]) => !selectedFields.length || selectedFields.includes(f))}
              />
          }
        </div>
      </div>

      {/* ══ BOTTOM INTELLIGENCE ROW ═════════════════════════════════════════════ */}
      <div className="flex-shrink-0 grid gap-2 px-3 py-2 overflow-y-auto"
        style={{
          background: 'linear-gradient(0deg, rgba(2,5,10,0.99) 0%, rgba(4,10,20,0.95) 100%)',
          borderTop: '1px solid rgba(26,45,74,0.6)',
          gridTemplateColumns: 'repeat(auto-fit, minmax(160px, 1fr))',
          maxHeight: 220,
          scrollbarWidth: 'thin', scrollbarColor: '#1a2d4a transparent',
        }}>

        <BottomCard label="Compression Intelligence" color="#a855f7">
          <div className="flex items-center justify-between gap-2">
            <div>
              <div className="text-[19px] font-bold leading-none" style={{ color: '#a855f7', textShadow: '0 0 12px rgba(168,85,247,0.4)' }}>
                {compression?.compression_ratio != null ? `${compression.compression_ratio}×` : '—'}
              </div>
              <div className="text-[8.5px] mt-1" style={{ color: '#475569' }}>{rawLabels.toLocaleString()} raw labels</div>
              <InsightLine>{reductionPct != null ? `${(reductionPct * 100).toFixed(0)}% reduction into repeated semantic patterns.` : 'Compression source data unavailable.'}</InsightLine>
            </div>
            <Sparkline seed={compressionRatio || 2.4} color="#a855f7" />
          </div>
        </BottomCard>

        <BottomCard label="Semantic Quality" color="#10b981">
          <div className="flex items-center justify-between gap-2">
            <div>
              <div className="text-[19px] font-bold leading-none" style={{ color: '#10b981', textShadow: '0 0 12px rgba(16,185,129,0.4)' }}>
                {totalClusters ? `${(coveragePct * 100).toFixed(0)}%` : '—'}
              </div>
              <div className="text-[8.5px] mt-1" style={{ color: '#475569' }}>{namedCount.toLocaleString()} named</div>
              <InsightLine>{medoid?.weak?.length ? `${medoid.weak.length} weak medoid examples need review.` : 'Medoid similarity not computed.'}</InsightLine>
            </div>
            <Sparkline seed={coveragePct * 8 + 1.2} color="#10b981" />
          </div>
        </BottomCard>

        <BottomCard label="Anomaly Intelligence" color="#ef4444">
          <div className="flex items-center justify-between gap-2">
            <div>
              <div className="text-[19px] font-bold leading-none" style={{ color: '#ef4444', textShadow: '0 0 12px rgba(239,68,68,0.4)' }}>
                {anomalyCount.toLocaleString()}
              </div>
              <div className="text-[8.5px] mt-1" style={{ color: '#475569' }}>
                {anomalySummary?.by_type ? `${Object.keys(anomalySummary.by_type).length} types` : 'clusters'}
              </div>
              <InsightLine>{`${(anomalyPct * 100).toFixed(1)}% anomaly pressure across the taxonomy.`}</InsightLine>
            </div>
            <AnomalyDonut pct={totalClusters ? Math.min(anomalyCount / totalClusters, 1) : 0} color="#ef4444" />
          </div>
        </BottomCard>

        <BottomCard label="Drift Intelligence" color="#f97316">
          <div className="flex items-center justify-between gap-2">
            <div>
              <div className="text-[19px] font-bold leading-none" style={{ color: '#f97316', textShadow: '0 0 12px rgba(249,115,22,0.4)' }}>
                {drift?.total_drift_events ?? drift?.run_count ?? '—'}
              </div>
              <div className="text-[8.5px] mt-1" style={{ color: '#475569' }}>{drift ? 'recent signals' : 'not computed'}</div>
              <InsightLine>{drift?.field_stats?.[0] ? `${drift.field_stats[0].field_name} has the largest current cluster surface.` : 'Drift concentration not computed.'}</InsightLine>
            </div>
            <Sparkline seed={3.2} color="#f97316" />
          </div>
        </BottomCard>

        <BottomCard label="Merge Intelligence" color="#06b6d4">
          <div className="flex items-center justify-between gap-2">
            <div>
              <div className="text-[19px] font-bold leading-none" style={{ color: '#06b6d4', textShadow: '0 0 12px rgba(6,182,212,0.4)' }}>
                {medoid?.merge_candidates ?? medoid?.total_clusters_with_medoids ?? '—'}
              </div>
              <div className="text-[8.5px] mt-1" style={{ color: '#475569' }}>field-scoped duplicate names</div>
              <InsightLine>Cross-field duplicate names are not automatic merge candidates.</InsightLine>
            </div>
            <Sparkline seed={1.7} color="#06b6d4" />
          </div>
        </BottomCard>

        <BottomCard label="Coverage Intelligence" color="#00d4ff">
          <div className="flex flex-col gap-1.5">
            <div className="text-[19px] font-bold leading-none" style={{ color: '#00d4ff', textShadow: '0 0 12px rgba(0,212,255,0.4)' }}>
              {totalClusters ? `${(coveragePct * 100).toFixed(0)}%` : '—'}
            </div>
            <div className="w-full h-1.5 rounded-full overflow-hidden" style={{ background: 'rgba(26,45,74,0.7)' }}>
              <div style={{
                width: `${coveragePct * 100}%`, height: '100%',
                background: 'linear-gradient(90deg, #00d4ff, #a855f7)',
                borderRadius: 999, boxShadow: '0 0 8px rgba(0,212,255,0.45)',
                transition: 'width 1.2s ease',
              }} />
            </div>
            <div className="text-[8.5px]" style={{ color: '#475569' }}>
              {(health?.total_label_rows || 0).toLocaleString()} labels
            </div>
          </div>
        </BottomCard>
      </div>
    </div>
  )
}
