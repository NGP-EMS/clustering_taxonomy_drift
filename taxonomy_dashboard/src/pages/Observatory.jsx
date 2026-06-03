import { useEffect, useMemo, useRef, useState, lazy, Suspense } from 'react'
import { RotateCcw, Map as MapIcon, Orbit, Search, SlidersHorizontal, Minus, Plus, Maximize2, Table2, Columns, LayoutDashboard, X } from 'lucide-react'
import useStore from '../store/useStore.js'
import RightInspector from '../components/layout/RightInspector.jsx'
import ClusterTable from '../components/ClusterTable.jsx'
import { getFieldColor } from '../components/scene/sceneUtils.js'
import { fetchJson } from '../utils/api.js'
import { makeClusterKey } from '../utils/clusterKey.js'

const SemanticScene = lazy(() => import('../components/scene/SemanticScene.jsx'))

const OBSERVATORY_FIELD_LIMIT = 8000
const OBSERVATORY_GLOBAL_LIMIT = 8000
const SEMANTIC_MIN_SCORE = 0.10  // Voyage rerank-2.5-lite scores top out lower than cosine similarity

function safeQueryStr(q) {
  if (!q) return ''
  if (typeof q === 'string') return q
  if (typeof q === 'object') return q.normalized_query || q.query || q.text || q.label || JSON.stringify(q)
  return String(q)
}

function confBand(score) {
  const s = Number(score) || 0
  if (s >= 0.75) return { label: 'confident', color: '#10b981', bg: 'rgba(16,185,129,0.12)', border: 'rgba(16,185,129,0.32)' }
  if (s >= 0.60) return { label: 'possible',  color: '#f59e0b', bg: 'rgba(245,158,11,0.12)',  border: 'rgba(245,158,11,0.32)'  }
  return              { label: 'weak',       color: '#64748b', bg: 'rgba(100,116,139,0.10)', border: 'rgba(100,116,139,0.22)' }
}

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


function SemanticSearchPanel({ query, setQuery, searchState, selectedFields, onSearch, onClear, onRefreshIndex, semanticClusters = [], projectionStatus, onRegenerateProjection }) {
  const active = searchState?.active
  const loading = searchState?.loading
  const results = searchState?.results || []
  const refreshing = searchState?.refreshing
  // Prefer enriched clusters (have _clusterKey and full composite identity) over raw API results
  const top = (active && semanticClusters.length ? semanticClusters : results).slice(0, 4)
  const scoped = selectedFields?.length === 1 ? selectedFields[0] : null

  return (
    <CtrlSection label="Semantic Search">
      <div className="flex flex-col gap-1.5">
        <div className="flex items-center gap-1.5 rounded-lg px-2 py-1.5"
          style={{ background: 'rgba(3,8,15,0.78)', border: `1px solid ${active ? 'rgba(168,85,247,0.40)' : 'rgba(26,45,74,0.65)'}` }}>
          <Search size={11} style={{ color: active ? '#a855f7' : '#64748b', flexShrink: 0 }} />
          <input
            value={query}
            onChange={e => setQuery(e.target.value)}
            onKeyDown={e => { if (e.key === 'Enter') onSearch() }}
            placeholder="aggressive, rude, confused..."
            className="w-full bg-transparent outline-none text-[10px] text-star placeholder:text-slate-600"
          />
          {(query || active) && (
            <button onClick={onClear} className="w-4 h-4 flex items-center justify-center rounded text-slate-500 hover:text-cyan">
              <X size={11} />
            </button>
          )}
        </div>

        <button onClick={onSearch} disabled={loading || refreshing || !query.trim()}
          className="w-full flex items-center justify-center gap-1.5 rounded-lg py-1.5 text-[9.5px] font-semibold transition-all duration-150 disabled:opacity-45 disabled:cursor-not-allowed"
          style={{ background: 'rgba(168,85,247,0.12)', border: '1px solid rgba(168,85,247,0.32)', color: '#c084fc' }}>
          {loading ? 'Searching embeddings…' : 'Find semantic clusters'}
        </button>

        <button onClick={onRefreshIndex} disabled={refreshing || loading}
          title="Rebuild the BGE-M3 FAISS index from the current taxonomy. Run this when search results look irrelevant or after a taxonomy update."
          className="w-full flex items-center justify-center gap-1.5 rounded-lg py-1 text-[8.5px] font-medium transition-all duration-150 disabled:opacity-45 disabled:cursor-not-allowed"
          style={{ background: 'rgba(6,182,212,0.07)', border: '1px solid rgba(6,182,212,0.20)', color: refreshing ? '#22d3ee' : '#475569' }}>
          {refreshing ? '⟳ Rebuilding index (may take minutes)…' : '↺ Refresh search index'}
        </button>

        {searchState?.refreshError && (
          <div className="text-[8px] leading-snug rounded px-2 py-1"
            style={{ color: '#fb7185', background: 'rgba(244,63,94,0.07)', border: '1px solid rgba(244,63,94,0.18)' }}>
            Refresh failed: {searchState.refreshError}
          </div>
        )}
        {searchState?.refreshDone && (
          <div className="text-[8px] leading-snug rounded px-2 py-1"
            style={{ color: '#10b981', background: 'rgba(16,185,129,0.07)', border: '1px solid rgba(16,185,129,0.18)' }}>
            ✓ Index rebuilt ({searchState.refreshDone.toLocaleString()} labels). Re-run your search.
          </div>
        )}

        <button onClick={onRegenerateProjection} disabled={projectionStatus?.running}
          title="Recompute UMAP/PCA 3D positions for all clusters. Runs automatically on server start. Re-run after a taxonomy update."
          className="w-full flex items-center justify-center gap-1.5 rounded-lg py-1 text-[8.5px] font-medium transition-all duration-150 disabled:opacity-45 disabled:cursor-not-allowed"
          style={{ background: 'rgba(251,191,36,0.06)', border: '1px solid rgba(251,191,36,0.18)', color: projectionStatus?.running ? '#fbbf24' : '#475569' }}>
          {projectionStatus?.running ? '⟳ Regenerating 3D positions…' : '↺ Regenerate 3D positions'}
        </button>
        {projectionStatus?.error && (
          <div className="text-[8px] leading-snug rounded px-2 py-1"
            style={{ color: '#fb7185', background: 'rgba(244,63,94,0.07)', border: '1px solid rgba(244,63,94,0.18)' }}>
            3D regen failed: {projectionStatus.error}
          </div>
        )}
        {projectionStatus?.done && !projectionStatus?.running && (
          <div className="text-[8px] leading-snug rounded px-2 py-1 flex items-center justify-between gap-2"
            style={{ color: '#fbbf24', background: 'rgba(251,191,36,0.06)', border: '1px solid rgba(251,191,36,0.18)' }}>
            <span>✓ 3D positions updated.</span>
            <button onClick={() => window.location.reload()} className="underline underline-offset-2 hover:opacity-80">
              Reload map
            </button>
          </div>
        )}

        {scoped && !active && (
          <div className="text-[8.5px] leading-snug" style={{ color: '#334155' }}>
            Search will run inside {scoped}. Clear field chips to search all fields.
          </div>
        )}

        {searchState?.error && (
          <div className="text-[8.5px] leading-snug rounded-lg px-2 py-1.5"
            style={{ color: '#fb7185', background: 'rgba(244,63,94,0.08)', border: '1px solid rgba(244,63,94,0.22)' }}>
            {searchState.error}
          </div>
        )}

        {active && !searchState?.error && (
          <div className="rounded-lg px-2 py-2 flex flex-col gap-1.5"
            style={{ background: 'rgba(168,85,247,0.08)', border: '1px solid rgba(168,85,247,0.24)' }}>
            <div className="text-[9px] font-semibold" style={{ color: '#c084fc' }}>
              {results.length.toLocaleString()} cluster{results.length !== 1 ? 's' : ''} matched
            </div>
            {top.length > 0 && (
              <div className="flex flex-col gap-1">
                {top.map(r => {
                  const fc = getFieldColor(r.field_name)
                  const score = r.semantic_best_score ?? r.semantic_search_score ?? 0
                  const band = confBand(score)
                  return (
                    <button key={r._clusterKey || `${r.field_name}:${r.cluster_id}`}
                      onClick={() => { const key = r._clusterKey || makeClusterKey(r); if (key) window.dispatchEvent(new CustomEvent('semantic-search-select', { detail: { id: key } })) }}
                      className="text-left rounded-md px-2 py-1.5"
                      style={{ background: 'rgba(3,8,15,0.55)', border: `1px solid ${fc}22` }}>
                      <div className="flex items-center justify-between gap-2">
                        <span className="text-[9.5px] truncate" style={{ color: '#cbd5e1' }}>{r.display_name || r.medoid_label || r.cluster_id}</span>
                        <div className="flex items-center gap-1 flex-shrink-0">
                          <span className="text-[8px] px-1 py-0.5 rounded font-medium" style={{ color: band.color, background: band.bg, border: `1px solid ${band.border}` }}>{band.label}</span>
                          <span className="text-[8.5px] font-mono" style={{ color: band.color }}>{Math.round(score * 100)}%</span>
                        </div>
                      </div>
                      {r.semantic_best_label && (
                        <div className="text-[8px] truncate mt-0.5" style={{ color: '#64748b' }}>{r.semantic_best_label}</div>
                      )}
                    </button>
                  )
                })}
              </div>
            )}
          </div>
        )}
      </div>
    </CtrlSection>
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
    navigate,
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
  const [showProduction, setShowProduction] = useState(false)
  const [productionOverlay, setProductionOverlay] = useState({ available: false, rows: [], latest_run_id: null })
  const [semanticQuery, setSemanticQuery] = useState('')
  const [semanticSearch, setSemanticSearch] = useState({ active: false, query: '', results: [], loading: false, error: null, engine: null, refreshing: false, refreshError: null, refreshDone: null })
  const [projection, setProjection] = useState({ running: false, lastRunAt: null, error: null, done: false })
  const projPollRef = useRef(null)

  const sendSceneCommand = (action) => {
    window.dispatchEvent(new CustomEvent('semantic-scene-command', { detail: { action } }))
  }

  const resetScene = () => {
    triggerCameraReset()
    sendSceneCommand('reset')
  }

  const selectedFields = activeFields?.length ? activeFields : (activeField ? [activeField] : [])

  const clearSemanticSearch = () => {
    setSemanticQuery('')
    setSemanticSearch({ active: false, query: '', results: [], loading: false, error: null, engine: null, refreshing: false, refreshError: null, refreshDone: null })
  }

  const refreshSemanticIndex = async () => {
    setSemanticSearch(prev => ({ ...prev, refreshing: true, refreshError: null, refreshDone: null }))
    try {
      const r = await fetch('/api/semantic-index/refresh', { method: 'POST' })
      const d = await r.json()
      if (!r.ok || d.error) throw new Error(d.error || `HTTP ${r.status}`)
      setSemanticSearch(prev => ({ ...prev, refreshing: false, refreshDone: d.indexed_docs ?? true }))
    } catch (err) {
      setSemanticSearch(prev => ({ ...prev, refreshing: false, refreshError: err.message }))
    }
  }

  const pollProjectionStatus = async () => {
    try {
      const d = await fetchJson('/api/projection/status', {}, 'Projection status')
      setProjection(prev => ({ ...prev, running: d.running, lastRunAt: d.lastRunAt, error: d.error || null }))
      if (d.running) {
        projPollRef.current = setTimeout(pollProjectionStatus, 3000)
      } else if (projPollRef.current) {
        projPollRef.current = null
        setProjection(prev => ({ ...prev, done: true }))
      }
    } catch (_) {}
  }

  const triggerProjectionRegenerate = async () => {
    setProjection({ running: true, lastRunAt: null, error: null, done: false })
    try {
      await fetch('/api/projection/regenerate', { method: 'POST' })
      projPollRef.current = setTimeout(pollProjectionStatus, 3000)
    } catch (err) {
      setProjection(prev => ({ ...prev, running: false, error: err.message }))
    }
  }

  // Check projection status on mount — catches the server auto-run on startup
  useEffect(() => {
    pollProjectionStatus()
    return () => { if (projPollRef.current) clearTimeout(projPollRef.current) }
  }, []) // eslint-disable-line react-hooks/exhaustive-deps

  const runSemanticSearch = async (overrideQuery) => {
    const q = String(overrideQuery ?? semanticQuery).trim()
    if (!q) {
      clearSemanticSearch()
      return
    }

    setSelectedClusterId(null)
    setSemanticSearch(prev => ({ ...prev, active: true, query: q, loading: true, error: null, results: [] }))

    try {
      const params = new URLSearchParams({ q, limit: '120', min_score: String(SEMANTIC_MIN_SCORE), label_limit: '1200' })
      if (selectedFields.length === 1) params.set('field_name', selectedFields[0])
      params.set('include_calls', 'true')
      params.set('sample_call_limit', '8')
      const payload = await fetchJson(`/api/semantic-search?${params}`, {}, 'Semantic search')
      setSemanticSearch({
        active: true,
        query: payload.normalized_query || payload.query || q,
        results: Array.isArray(payload.results) ? payload.results : [],
        loading: false,
        error: null,
        engine: payload.engine || null,
        model: payload.model || null,
      })
    } catch (err) {
      setSemanticSearch({ active: true, query: q, results: [], loading: false, error: err.message || 'Semantic search failed', engine: null })
    }
  }

  useEffect(() => {
    function onSelect(event) {
      if (event?.detail?.id) setSelectedClusterId(event.detail.id)
    }
    window.addEventListener('semantic-search-select', onSelect)
    return () => window.removeEventListener('semantic-search-select', onSelect)
  }, [setSelectedClusterId])

  useEffect(() => {
    setLoading(true)
    Promise.allSettled([
      fetch('/api/fields').then(r => r.json()),
      fetch('/api/anomaly-intelligence').then(r => r.json()),
      fetch('/api/semantic-compression').then(r => r.json()),
      fetch('/api/drift-summary').then(r => r.json()),
      fetch('/api/medoid-intelligence').then(r => r.json()),
      fetch('/api/production-mapper/semantic-overlay').then(r => r.json()),
    ]).then(([fieldsRes, an, comp, dr, med, prod]) => {
      if (an.status === 'fulfilled')   setAnomalySummary(an.value?.summary)
      if (comp.status === 'fulfilled') setCompression(comp.value)
      if (dr.status === 'fulfilled')   setDrift(dr.value)
      if (med.status === 'fulfilled')  setMedoid(med.value)
      if (prod.status === 'fulfilled') setProductionOverlay(prod.value)

      const fieldList = (fieldsRes.status === 'fulfilled' && Array.isArray(fieldsRes.value))
        ? fieldsRes.value : []

      if (fieldList.length === 0) {
        return fetch(`/api/clusters?limit=${OBSERVATORY_GLOBAL_LIMIT}&projection=umap`).then(r => r.json()).then(data => {
          if (Array.isArray(data)) setClusters(data.map(c => ({ ...c, _clusterKey: makeClusterKey(c) })))
        })
      }

      return Promise.allSettled(
        fieldList.map(f =>
          fetch(`/api/clusters?field_name=${encodeURIComponent(f)}&limit=${OBSERVATORY_FIELD_LIMIT}&projection=umap`).then(r => r.json())
        )
      ).then(results => {
        const seen = new Set(), merged = [], stats = {}
        results.forEach((r, idx) => {
          if (r.status !== 'fulfilled' || !Array.isArray(r.value)) return
          const field = fieldList[idx], data = r.value
          stats[field] = { rendered: data.length, capped: data.length >= OBSERVATORY_FIELD_LIMIT }
          data.forEach(c => {
            // Use composite key (field::version::cluster_id) to avoid cross-field collisions
            const key = makeClusterKey(c)
            if (!seen.has(key)) { seen.add(key); merged.push({ ...c, _clusterKey: key }) }
          })
        })
        setClusters(merged)
        setFieldStats(stats)
      })
    }).finally(() => setLoading(false))
  }, [])


  const productionOverlayMap = useMemo(() => {
    const map = new Map()
    for (const row of productionOverlay?.rows || []) {
      if (!row?.field_name || !row?.mapped_cluster_id) continue
      map.set(`${row.field_name}:${row.mapped_cluster_id}`, row)
    }
    return map
  }, [productionOverlay])

  const clustersWithProduction = useMemo(() => {
    if (!showProduction || !productionOverlayMap.size) return clusters
    return clusters.map(c => {
      const hit = productionOverlayMap.get(`${c.field_name}:${c.cluster_id}`)
      if (!hit) return c
      return {
        ...c,
        production_hit_count: hit.production_hit_count,
        production_distinct_calls: hit.production_distinct_calls,
        production_latest_classified_at: hit.latest_classified_at,
        production_raw_labels: hit.raw_labels || [],
      }
    })
  }, [clusters, showProduction, productionOverlayMap])

  const semanticResultMap = useMemo(() => {
    const map = new Map()
    for (const r of semanticSearch?.results || []) {
      if (!r?.field_name || !r?.cluster_id) continue
      // Use field::cluster_id (no version) since semantic API results may omit cluster_version
      map.set(`${r.field_name}::${r.cluster_id}`, r)
    }
    return map
  }, [semanticSearch])

  const clustersWithSemantic = useMemo(() => {
    if (!semanticSearch?.active || !semanticResultMap.size) return clustersWithProduction
    return clustersWithProduction
      .map(c => {
        const hit = semanticResultMap.get(`${c.field_name}::${c.cluster_id}`)
        if (!hit) return null
        return {
          ...c,
          semantic_search_score: hit.semantic_score,
          semantic_best_score: hit.best_label_similarity,
          semantic_best_label: hit.semantic_best_label,
          semantic_matched_labels: hit.semantic_matched_labels || [],
          semantic_match_count: hit.matched_label_count,
          semantic_matched_occurrences: hit.matched_occurrences,
          semantic_distinct_calls: hit.semantic_distinct_calls,
          sample_call_ids: hit.sample_call_ids || [],
          call_id_source: hit.call_id_source || null,
        }
      })
      .filter(Boolean)
  }, [clustersWithProduction, semanticSearch, semanticResultMap])

  // Find the exact selected cluster by composite key so the inspector and semantic data are always correct
  const selectedCluster = useMemo(() => {
    if (!selectedClusterId) return null
    return clustersWithSemantic.find(c => c._clusterKey === selectedClusterId) || null
  }, [selectedClusterId, clustersWithSemantic])

  // Top semantic result clusters (enriched, sorted by score) for the left panel
  const semanticTopClusters = useMemo(() => {
    if (!semanticSearch?.active) return []
    return [...clustersWithSemantic]
      .sort((a, b) => (b.semantic_search_score || 0) - (a.semantic_search_score || 0))
      .slice(0, 4)
  }, [clustersWithSemantic, semanticSearch?.active])

  const displayClusters = clustersWithSemantic.filter(c => {
    if (selectedFields.length && !selectedFields.includes(c.field_name)) return false
    if (anomalyFilter === 'anomaly'  && !c.is_true_anomaly_cluster) return false
    if (anomalyFilter === 'standard' &&  c.is_true_anomaly_cluster) return false
    if (sizeFilter > 1 && (c.cluster_size || 0) < sizeFilter)      return false
    return true
  })

  const fieldGroups = clustersWithSemantic.reduce((acc, c) => {
    acc[c.field_name] = (acc[c.field_name] || 0) + 1; return acc
  }, {})
  const fields = Object.entries(fieldGroups).sort((a, b) => b[1] - a[1])

  const anomalyCount  = health?.anomaly_clusters || anomalySummary?.total || 0
  const totalClusters = health?.total_clusters || clustersWithProduction.length || 0
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

  const isTableView = viewMode === 'table'
  const isSplitView = viewMode === 'split'
  const sceneProjection = viewMode === '3d' ? '3d' : 'map'

  return (
    <div className="flex flex-col w-full h-full overflow-hidden" style={{ background: '#02050a' }}>

      {/* ══ THREE-COLUMN MAIN AREA ══════════════════════════════════════════════ */}
      <div className="flex flex-1 overflow-hidden min-h-0">

        {/* ── LEFT CONTROL PANEL ─────────────────────────────────────────────── */}
        <div className="flex flex-col flex-shrink-0 overflow-y-auto overflow-x-hidden gap-3.5 px-3 py-3"
          style={{
            width: 'clamp(190px, 12vw, 220px)',
            background: 'linear-gradient(180deg, #070e1c 0%, #030810 100%)',
            borderRight: '1px solid rgba(26,45,74,0.65)',
            scrollbarWidth: 'thin', scrollbarColor: '#1a2d4a transparent',
          }}>

          {/* Active sample indicator */}
          <div className="flex-shrink-0 pb-2" style={{ borderBottom: '1px solid rgba(26,45,74,0.5)' }}>
            <div className="flex items-center gap-1.5">
              <span className="w-1.5 h-1.5 rounded-full flex-shrink-0 animate-pulse" style={{ background: '#10b981', boxShadow: '0 0 5px #10b981' }} />
              <span className="text-[9px]" style={{ color: '#64748b' }}>
                {displayClusters.length.toLocaleString()}
                {clusters.length !== displayClusters.length && ` / ${clusters.length.toLocaleString()}`} nodes
              </span>
            </div>
          </div>

          {/* View */}
          <CtrlSection label="View">
            <div className="grid grid-cols-2 gap-1 rounded-lg p-1" style={{ border: '1px solid rgba(26,45,74,0.65)', background: 'rgba(3,8,15,0.65)' }}>
              {[['map', MapIcon, 'Map'], ['3d', Orbit, '3D'], ['table', Table2, 'Table'], ['split', Columns, 'Split']].map(([mode, Icon, lbl]) => (
                <button key={mode} onClick={() => setViewMode(mode)}
                  className="flex items-center justify-center gap-1.5 rounded-md py-1.5 px-2 text-[9.5px] font-semibold transition-all duration-150 whitespace-nowrap"
                  style={viewMode === mode
                    ? { background: 'rgba(0,212,255,0.16)', color: '#00d4ff', boxShadow: '0 0 10px rgba(0,212,255,0.12)' }
                    : { background: 'rgba(3,8,15,0.85)', color: '#475569' }
                  }>
                  <Icon size={11} /> {lbl}
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

          <SemanticSearchPanel
            query={semanticQuery}
            setQuery={setSemanticQuery}
            searchState={semanticSearch}
            selectedFields={selectedFields}
            onSearch={runSemanticSearch}
            onClear={clearSemanticSearch}
            onRefreshIndex={refreshSemanticIndex}
            semanticClusters={semanticTopClusters}
            projectionStatus={projection}
            onRegenerateProjection={triggerProjectionRegenerate}
          />

          {/* Production overlay */}
          <CtrlSection label="Production">
            <div className="flex flex-col gap-1.5">
              <button onClick={() => setShowProduction(v => !v)}
                className="w-full flex items-center justify-between px-2.5 py-1.5 rounded-lg transition-all duration-150"
                style={{
                  background: showProduction ? 'rgba(16,185,129,0.09)' : 'rgba(255,255,255,0.025)',
                  border: `1px solid ${showProduction ? 'rgba(16,185,129,0.38)' : 'rgba(26,45,74,0.5)'}`,
                }}>
                <span className="text-[10px]" style={{ color: showProduction ? '#10b981' : '#475569' }}>Latest mapped labels</span>
                <span className="text-[9px] font-mono" style={{ color: showProduction ? '#10b981' : '#334155' }}>{(productionOverlay?.rows?.length || 0).toLocaleString()}</span>
              </button>
              <div className="text-[8.5px] leading-snug" style={{ color: '#334155' }}>
                Marks approved clusters touched by the latest production mapper run.
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
                <span className="text-[9px]" style={{ color: !selectedFields.length ? '#00d4ff88' : '#334155' }}>{clustersWithSemantic.length}</span>
              </button>
              {fields.map(([field, count]) => (
                <FieldChip key={field} field={field} count={count}
                  capped={fieldStats[field]?.capped ?? false}
                  active={selectedFields.includes(field)}
                  onClick={() => toggleField(field)} />
              ))}
            </div>
          </CtrlSection>

          <div className="mt-auto flex-shrink-0 flex flex-col gap-2 pt-2" style={{ borderTop: '1px solid rgba(26,45,74,0.45)' }}>
            <button onClick={resetScene}
              className="flex items-center justify-center gap-1.5 rounded-lg py-2 text-[9.5px] transition-all duration-150"
              style={{ background: 'rgba(255,255,255,0.025)', border: '1px solid rgba(26,45,74,0.6)', color: '#64748b' }}
              onMouseEnter={e => { e.currentTarget.style.color = '#00d4ff'; e.currentTarget.style.borderColor = 'rgba(0,212,255,0.3)' }}
              onMouseLeave={e => { e.currentTarget.style.color = '#64748b'; e.currentTarget.style.borderColor = 'rgba(26,45,74,0.6)' }}>
              <RotateCcw size={11} /> Reset View
            </button>

            <button onClick={() => navigate('overview')}
              className="flex items-center justify-center gap-1.5 rounded-lg py-2 text-[9.5px] font-semibold transition-all duration-150"
              style={{ background: 'rgba(168,85,247,0.10)', border: '1px solid rgba(168,85,247,0.26)', color: '#a855f7' }}>
              <LayoutDashboard size={12} /> Analysis
            </button>
          </div>
        </div>

        {/* ── CENTER: OBSERVATORY WORKSPACE ───────────────────────────────────── */}
        <div className="relative flex-1 min-w-0 overflow-hidden">
          {!isTableView && (
            <div className={isSplitView ? 'h-[60%] min-h-[300px] relative overflow-hidden border-b border-obs-border/60' : 'absolute inset-0 overflow-hidden'}>
              <Suspense fallback={<SceneLoader label="Initializing Semantic Map…" />}>
                {!loading
                  ? <SemanticScene clusters={displayClusters} colorMode={colorMode} viewMode={sceneProjection} showLabels={showLabels} />
                  : <SceneLoader label="Loading semantic map…" />
                }
              </Suspense>
            </div>
          )}

          {(isTableView || isSplitView) && (
            <div className={isSplitView ? 'absolute left-0 right-0 bottom-0 h-[40%] min-h-[240px] overflow-hidden p-4' : 'absolute inset-0 overflow-hidden p-5 pt-16'}>
              <div className="observatory-table-shell h-full overflow-hidden rounded-xl" style={{ background: 'rgba(3,8,15,0.78)', border: '1px solid rgba(26,45,74,0.72)' }}>
                <ClusterTable clusters={displayClusters} loading={loading} error={null} />
              </div>
            </div>
          )}

          {/* Top semantic-map toolbar */}
          <div className="absolute top-2.5 left-3 right-3 z-10 flex items-center justify-between gap-2" style={{ pointerEvents: 'none' }}>
            {!isTableView && <div className="flex items-center gap-3 rounded-lg px-2.5 py-1.5"
              style={{ background: 'rgba(3,8,15,0.82)', border: '1px solid rgba(71,85,105,0.42)', backdropFilter: 'blur(14px)', boxShadow: '0 10px 30px rgba(0,0,0,0.28)' }}>
              {[
                ['Cluster', '#e5e7eb', 'circle'],
                ['Centroid', '#3b82f6', 'ring'],
                ['Medoid', '#f97316', 'diamond'],
                ['Anomaly', '#ec4899', 'dot'],
                ...(showProduction ? [['Production hit', '#10b981', 'ring']] : []),
                ...(semanticSearch?.active ? [['Semantic match', '#a855f7', 'ring']] : []),
              ].map(([label, color, kind]) => (
                <div key={label} className="flex items-center gap-1 text-[10px] text-star">
                  {kind === 'diamond' ? <span className="w-2 h-2 rotate-45" style={{ border: `2px solid ${color}` }} />
                    : kind === 'ring' ? <span className="w-2.5 h-2.5 rounded-full" style={{ border: `2px solid ${color}`, boxShadow: `0 0 8px ${color}77` }} />
                    : kind === 'dot' ? <span className="w-3 h-3 rounded-full" style={{ background: color, boxShadow: `0 0 10px ${color}88` }} />
                    : <span className="w-3 h-3 rounded-full" style={{ border: `2px solid ${color}` }} />}
                  <span>{label}</span>
                </div>
              ))}
            </div>}

            <div className="flex items-center gap-2" style={{ pointerEvents: 'auto' }}>
              <div className="hidden md:flex items-center gap-1.5 rounded-lg px-2.5 py-1.5 w-[240px]"
                style={{ background: 'rgba(3,8,15,0.82)', border: '1px solid rgba(71,85,105,0.42)', backdropFilter: 'blur(14px)' }}>
                <Search size={12} style={{ color: '#94a3b8' }} />
                <input
                  placeholder="Semantic search labels..."
                  className="w-full bg-transparent outline-none text-[10px] text-star placeholder:text-slate-500"
                  defaultValue={semanticQuery}
                  onChange={e => setSemanticQuery(e.target.value)}
                  onKeyDown={e => {
                    if (e.key !== 'Enter') return
                    runSemanticSearch(e.currentTarget.value)
                  }}
                />
              </div>

              <button onClick={resetScene} className="w-8 h-8 rounded-lg flex items-center justify-center transition-colors"
                style={{ background: 'rgba(3,8,15,0.82)', border: '1px solid rgba(71,85,105,0.42)', color: '#94a3b8', backdropFilter: 'blur(14px)' }}>
                <RotateCcw size={12} />
              </button>
            </div>
          </div>

          {/* Bottom in-map controls */}
          {!isTableView && <div className="absolute bottom-4 left-1/2 -translate-x-1/2 z-10 flex items-center rounded-xl overflow-hidden"
            style={{ background: 'rgba(3,8,15,0.84)', border: '1px solid rgba(71,85,105,0.42)', backdropFilter: 'blur(14px)', boxShadow: '0 10px 30px rgba(0,0,0,0.35)' }}>
            <button onClick={() => sendSceneCommand('zoomOut')} className="w-10 h-9 flex items-center justify-center text-slate-300 hover:text-cyan transition-colors"><Minus size={14} /></button>
            <div className="w-16 h-9 flex items-center justify-center text-[12px] text-star" style={{ borderLeft: '1px solid rgba(71,85,105,0.35)', borderRight: '1px solid rgba(71,85,105,0.35)' }}>100%</div>
            <button onClick={() => sendSceneCommand('zoomIn')} className="w-10 h-9 flex items-center justify-center text-slate-300 hover:text-cyan transition-colors"><Plus size={16} /></button>
            <button onClick={() => sendSceneCommand('fullscreen')} className="w-10 h-9 flex items-center justify-center text-slate-300 hover:text-cyan transition-colors" style={{ borderLeft: '1px solid rgba(71,85,105,0.35)' }}><Maximize2 size={15} /></button>
          </div>}
        </div>

        {/* ── RIGHT INSPECTOR ───────────────────────────────────────────────── */}
        <div className="flex-shrink-0 overflow-hidden"
          style={{
            width: 'clamp(300px, 21vw, 340px)',
            background: 'linear-gradient(180deg, #060d1a 0%, #03080f 100%)',
            borderLeft: '1px solid rgba(26,45,74,0.75)',
          }}>
          {selectedClusterId
            ? <div className="h-full overflow-hidden"><RightInspector
                clusterId={selectedClusterId}
                semanticMatchedLabels={selectedCluster?.semantic_matched_labels || []}
                semanticQuery={semanticSearch.active ? semanticSearch.query : null}
              /></div>
            : <DefaultInspector
                health={health}
                clusters={displayClusters}
                fields={fields.filter(([f]) => !selectedFields.length || selectedFields.includes(f))}
              />
          }
        </div>
      </div>

    </div>
  )
}
