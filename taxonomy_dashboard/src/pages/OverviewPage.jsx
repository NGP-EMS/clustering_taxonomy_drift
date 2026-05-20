import { useEffect, useMemo, useState } from 'react'
import {
  Activity, AlertTriangle, ArrowRight, CheckCircle, Database, GitMerge,
  Layers, LineChart, ShieldCheck, Sparkles, Target, Zap,
} from 'lucide-react'
import { useAppCtx } from '../context/AppContext.jsx'
import { fmt } from '../utils/format.js'
import { getFieldColor } from '../utils/colors.js'

const EMPTY = []

function safeArray(value) {
  return Array.isArray(value) ? value : EMPTY
}

function n(value, fallback = 0) {
  const x = Number(value)
  return Number.isFinite(x) ? x : fallback
}

function pct(value, digits = 0) {
  if (value == null || !Number.isFinite(Number(value))) return '—'
  return `${(Number(value) * 100).toFixed(digits)}%`
}

function rate(part, total) {
  const p = n(part)
  const t = n(total)
  return t > 0 ? p / t : null
}

function scrollToSection(id) {
  const el = document.getElementById(id)
  if (el) el.scrollIntoView({ behavior: 'smooth', block: 'start' })
}

function normalizeLabel(value) {
  if (value == null) return '—'
  return String(value).replace(/_/g, ' ')
}

function Panel({ id, title, subtitle, icon: Icon = Activity, children, compact = false }) {
  return (
    <section id={id} className="rounded-2xl overflow-hidden scroll-mt-20" style={{ background: 'rgba(5,11,22,0.78)', border: '1px solid rgba(26,45,74,0.78)', boxShadow: '0 18px 40px rgba(0,0,0,0.18)' }}>
      <div className="flex items-center justify-between gap-4 px-5 py-4" style={{ borderBottom: '1px solid rgba(26,45,74,0.62)' }}>
        <div className="flex items-center gap-3 min-w-0">
          <div className="w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0" style={{ background: 'rgba(0,212,255,0.08)', border: '1px solid rgba(0,212,255,0.20)', color: '#00d4ff' }}>
            <Icon size={16} />
          </div>
          <div className="min-w-0">
            <h2 className="text-[14px] font-bold text-star tracking-tight">{title}</h2>
            {subtitle && <p className="text-[10.5px] text-dust mt-0.5 truncate">{subtitle}</p>}
          </div>
        </div>
      </div>
      <div className={compact ? 'p-0' : 'p-5'}>{children}</div>
    </section>
  )
}

function MetricCard({ label, value, note, color = '#00d4ff', icon: Icon = Sparkles, onClick }) {
  const Wrapper = onClick ? 'button' : 'div'
  return (
    <Wrapper
      onClick={onClick}
      className="rounded-2xl p-4 text-left min-w-0 transition-all duration-150 hover:-translate-y-0.5"
      style={{ background: `linear-gradient(135deg, ${color}10, rgba(255,255,255,0.018))`, border: `1px solid ${color}28`, boxShadow: `0 0 26px ${color}08` }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[9px] uppercase tracking-[0.22em] font-bold" style={{ color }}>{label}</div>
          <div className="mt-2 text-[24px] leading-none font-bold truncate" style={{ color, textShadow: `0 0 18px ${color}40` }}>{value}</div>
        </div>
        <Icon size={17} style={{ color, opacity: 0.75 }} />
      </div>
      {note && <div className="mt-3 text-[11px] leading-snug" style={{ color: '#64748b' }}>{note}</div>}
    </Wrapper>
  )
}

function Progress({ value, color = '#00d4ff', label, right }) {
  const width = Math.max(0, Math.min(100, n(value) * 100))
  return (
    <div className="py-1.5">
      {(label || right) && (
        <div className="flex justify-between gap-3 mb-1.5 text-[10.5px]">
          <span style={{ color: '#94a3b8' }}>{label}</span>
          <span className="font-mono" style={{ color }}>{right ?? pct(value)}</span>
        </div>
      )}
      <div className="h-1.5 rounded-full overflow-hidden" style={{ background: 'rgba(26,45,74,0.72)' }}>
        <div className="h-full rounded-full" style={{ width: `${width}%`, background: `linear-gradient(90deg, ${color}, ${color}88)`, boxShadow: `0 0 9px ${color}55` }} />
      </div>
    </div>
  )
}

function DataRow({ label, value, color = '#94a3b8', chip = false }) {
  if (value == null || value === '') return null
  return (
    <div className="flex items-center justify-between gap-3 py-2" style={{ borderBottom: '1px solid rgba(26,45,74,0.36)' }}>
      <span className="text-[10.5px]" style={{ color: '#64748b' }}>{label}</span>
      {chip ? (
        <span className="text-[10px] px-2 py-0.5 rounded-md font-semibold" style={{ color, background: `${color}14`, border: `1px solid ${color}28` }}>{value}</span>
      ) : (
        <span className="text-[10.5px] text-right truncate max-w-[65%]" style={{ color }}>{value}</span>
      )}
    </div>
  )
}

function StatusPill({ status }) {
  const color = status === 'Healthy' ? '#10b981' : status === 'Watch' ? '#f97316' : '#ef4444'
  return (
    <span className="text-[9.5px] px-2 py-0.5 rounded-md font-semibold" style={{ color, background: `${color}16`, border: `1px solid ${color}30` }}>
      {status}
    </span>
  )
}

function FieldBarRow({ field, left, right, percent, color }) {
  const c = color || getFieldColor(field)
  return (
    <div className="py-2.5" style={{ borderBottom: '1px solid rgba(26,45,74,0.32)' }}>
      <div className="flex items-center gap-3">
        <span className="w-[150px] text-[11px] truncate" style={{ color: c }}>{field}</span>
        <div className="flex-1 h-1.5 rounded-full overflow-hidden" style={{ background: 'rgba(26,45,74,0.72)' }}>
          <div className="h-full rounded-full" style={{ width: `${Math.max(4, Math.min(100, percent))}%`, background: `linear-gradient(90deg, ${c}, ${c}77)`, boxShadow: `0 0 8px ${c}44` }} />
        </div>
        <span className="w-[86px] text-right text-[10.5px]" style={{ color: '#94a3b8' }}>{left}</span>
        <span className="w-[88px] text-right text-[10.5px] font-mono" style={{ color: '#cbd5e1' }}>{right}</span>
      </div>
    </div>
  )
}

function ClusterRow({ item, onClick, rightLabel = 'Open' }) {
  const color = getFieldColor(item.field_name)
  return (
    <button onClick={() => onClick?.(item.id)} className="w-full flex items-center gap-3 px-3 py-2 rounded-xl text-left transition-colors hover:bg-white/[0.035]" style={{ border: '1px solid rgba(26,45,74,0.35)' }}>
      <span className="w-2 h-2 rounded-full flex-shrink-0" style={{ background: color, boxShadow: `0 0 8px ${color}` }} />
      <div className="min-w-0 flex-1">
        <div className="text-[11px] text-star truncate">{item.display_name || item.medoid_label || item.cluster_id || 'Unnamed cluster'}</div>
        <div className="text-[9.5px] truncate" style={{ color: '#64748b' }}>{item.field_name} · {item.cluster_id}</div>
      </div>
      <span className="text-[10px] px-2 py-0.5 rounded-md flex-shrink-0" style={{ color, background: `${color}13`, border: `1px solid ${color}25` }}>{rightLabel}</span>
    </button>
  )
}

function ActionCard({ title, detail, severity = 'info', metric, onClick }) {
  const color = severity === 'critical' ? '#ef4444' : severity === 'warning' ? '#f97316' : severity === 'good' ? '#10b981' : '#00d4ff'
  const Icon = severity === 'good' ? CheckCircle : severity === 'critical' || severity === 'warning' ? AlertTriangle : Sparkles
  const Wrapper = onClick ? 'button' : 'div'
  return (
    <Wrapper onClick={onClick} className="rounded-xl px-3 py-3 text-left flex items-start gap-3 transition-all hover:bg-white/[0.035]" style={{ background: `${color}08`, border: `1px solid ${color}24` }}>
      <Icon size={15} className="mt-0.5 flex-shrink-0" style={{ color }} />
      <div className="min-w-0 flex-1">
        <div className="flex items-center justify-between gap-3">
          <div className="text-[11px] font-semibold text-star truncate">{title}</div>
          {metric && <span className="text-[10px] font-mono flex-shrink-0" style={{ color }}>{metric}</span>}
        </div>
        <div className="text-[10px] leading-snug mt-1" style={{ color: '#64748b' }}>{detail}</div>
      </div>
    </Wrapper>
  )
}

function SectionNav() {
  const items = [
    ['summary', 'Overview'],
    ['production', 'Production'],
    ['actions', 'Actions'],
    ['matrix', 'Field Matrix'],
    ['compression', 'Compression'],
    ['quality', 'Quality'],
    ['anomalies', 'Anomalies'],
    ['merge', 'Merge'],
    ['coverage', 'Coverage'],
    ['metadata', 'Metadata'],
  ]
  return (
    <div className="sticky top-0 z-20 -mx-1 mb-5 py-2" style={{ background: 'linear-gradient(180deg, rgba(2,5,10,0.98), rgba(2,5,10,0.88))', backdropFilter: 'blur(12px)' }}>
      <div className="flex flex-wrap gap-2">
        {items.map(([id, label]) => (
          <button key={id} onClick={() => scrollToSection(id)} className="px-3 py-1.5 rounded-lg text-[10px] font-semibold transition-all hover:text-star" style={{ background: 'rgba(255,255,255,0.035)', border: '1px solid rgba(26,45,74,0.70)', color: '#94a3b8' }}>
            {label}
          </button>
        ))}
      </div>
    </div>
  )
}

function FieldHealthMatrix({ rows }) {
  return (
    <div className="overflow-x-auto">
      <table className="w-full min-w-[1040px] text-left text-[10.5px]">
        <thead style={{ background: 'rgba(255,255,255,0.025)', color: '#64748b' }}>
          <tr>
            <th className="px-3 py-2 font-semibold">Field</th>
            <th className="px-3 py-2 font-semibold text-right">Raw Labels</th>
            <th className="px-3 py-2 font-semibold text-right">Clusters</th>
            <th className="px-3 py-2 font-semibold text-right">Compression</th>
            <th className="px-3 py-2 font-semibold text-right">Named</th>
            <th className="px-3 py-2 font-semibold text-right">Anomaly</th>
            <th className="px-3 py-2 font-semibold text-right">Recovery</th>
            <th className="px-3 py-2 font-semibold text-right">Medoid Risk</th>
            <th className="px-3 py-2 font-semibold text-right">Run</th>
            <th className="px-3 py-2 font-semibold text-right">Status</th>
          </tr>
        </thead>
        <tbody>
          {rows.map(r => (
            <tr key={r.field_name} style={{ borderTop: '1px solid rgba(26,45,74,0.35)' }}>
              <td className="px-3 py-2 truncate font-semibold" style={{ color: getFieldColor(r.field_name) }}>{r.field_name}</td>
              <td className="px-3 py-2 text-right text-dust font-mono">{fmt(r.raw_labels)}</td>
              <td className="px-3 py-2 text-right text-dust font-mono">{fmt(r.clusters)}</td>
              <td className="px-3 py-2 text-right text-dust font-mono">{r.compression_ratio ? `${r.compression_ratio}×` : '—'}</td>
              <td className="px-3 py-2 text-right font-mono" style={{ color: r.named_rate >= 1 ? '#10b981' : '#f97316' }}>{r.named_rate != null ? pct(r.named_rate) : '—'}</td>
              <td className="px-3 py-2 text-right font-mono" style={{ color: r.anomaly_rate >= 0.2 ? '#ef4444' : '#94a3b8' }}>{r.anomaly_rate != null ? pct(r.anomaly_rate, 1) : '—'}</td>
              <td className="px-3 py-2 text-right text-dust font-mono">{r.recovery_rate != null ? pct(r.recovery_rate, 1) : '—'}</td>
              <td className="px-3 py-2 text-right text-dust font-mono">{r.medoid_weak_rate != null ? pct(r.medoid_weak_rate, 1) : '—'}</td>
              <td className="px-3 py-2 text-right text-dust font-mono truncate max-w-[130px]">{r.run_id || '—'}</td>
              <td className="px-3 py-2 text-right"><StatusPill status={r.status} /></td>
            </tr>
          ))}
        </tbody>
      </table>
      {!rows.length && <div className="p-4 text-[11px] text-dust">No field health matrix data returned.</div>}
    </div>
  )
}


function ProductionMappingPanel({ production, runs }) {
  const summary = production?.summary || {}
  const fields = safeArray(production?.field_health)
  const emerging = safeArray(production?.emerging)
  const configIssues = safeArray(production?.config_issues)
  const available = production?.available !== false
  const latestRun = production?.latest_run_id || summary.mapper_run_id || '—'
  const totalRows = n(summary.total_rows)
  const existingRows = n(summary.existing_cluster_rows)
  const emergingRows = n(summary.new_cluster_candidate_rows) + n(summary.true_anomaly_rows)
  const configRows = n(summary.no_cluster_reference_rows)
  const existingRate = summary.existing_cluster_rate != null ? Number(summary.existing_cluster_rate) : rate(existingRows, totalRows)

  if (!available) {
    return (
      <div className="rounded-xl p-4" style={{ background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(26,45,74,0.55)' }}>
        <div className="text-[11px] text-dust">Production mapper output table is not available yet. Run the hourly mapper to populate taxonomy_call_cluster_outputs.</div>
      </div>
    )
  }

  return (
    <div className="space-y-4">
      <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(170px, 1fr))' }}>
        <MetricCard label="Latest Run" value={String(latestRun).replace('mapper_', '').slice(0, 18) || '—'} color="#00d4ff" icon={Activity} note={summary.mapper_window_start && summary.mapper_window_end ? `${new Date(summary.mapper_window_start).toLocaleString()} → ${new Date(summary.mapper_window_end).toLocaleString()}` : 'No mapper window returned.'} />
        <MetricCard label="Mapped Labels" value={fmt(totalRows)} color="#a855f7" icon={Layers} note={`${fmt(n(summary.distinct_calls))} distinct calls processed in the latest mapper run.`} />
        <MetricCard label="Existing Cluster" value={existingRate != null ? pct(existingRate, 1) : '—'} color="#10b981" icon={CheckCircle} note={`${fmt(existingRows)} labels safely mapped into approved taxonomy clusters.`} />
        <MetricCard label="Emerging" value={fmt(emergingRows)} color={emergingRows ? '#f97316' : '#10b981'} icon={AlertTriangle} note={`${fmt(n(summary.new_cluster_candidate_rows))} new-cluster candidates, ${fmt(n(summary.true_anomaly_rows))} low-similarity anomalies.`} />
        <MetricCard label="Config Issues" value={fmt(configRows)} color={configRows ? '#ef4444' : '#10b981'} icon={Database} note="Rows with no active cluster reference for the field." />
      </div>

      <div className="grid grid-cols-1 xl:grid-cols-[1.2fr_1fr] gap-4">
        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid rgba(26,45,74,0.55)' }}>
          <div className="px-4 py-3 flex items-center justify-between gap-3" style={{ background: 'rgba(255,255,255,0.022)', borderBottom: '1px solid rgba(26,45,74,0.45)' }}>
            <div>
              <div className="text-[9px] uppercase tracking-[0.2em] text-dust font-bold">Field Mapping Health</div>
              <div className="text-[10px] text-dust/70 mt-1">Latest run distribution by taxonomy field.</div>
            </div>
            <span className="text-[10px] text-dust font-mono">{fmt(fields.length)} fields</span>
          </div>
          <div className="overflow-x-auto max-h-[320px]" style={{ scrollbarWidth: 'thin', scrollbarColor: '#1a2d4a transparent' }}>
            <table className="w-full min-w-[820px] text-left text-[10.5px]">
              <thead style={{ background: 'rgba(255,255,255,0.018)', color: '#64748b' }}>
                <tr>
                  <th className="px-3 py-2 font-semibold">Field</th>
                  <th className="px-3 py-2 font-semibold text-right">Rows</th>
                  <th className="px-3 py-2 font-semibold text-right">Calls</th>
                  <th className="px-3 py-2 font-semibold text-right">Exact</th>
                  <th className="px-3 py-2 font-semibold text-right">Centroid</th>
                  <th className="px-3 py-2 font-semibold text-right">Existing</th>
                  <th className="px-3 py-2 font-semibold text-right">Emerging</th>
                  <th className="px-3 py-2 font-semibold text-right">Avg Sim</th>
                </tr>
              </thead>
              <tbody>
                {fields.map(f => {
                  const emergingCount = n(f.new_cluster_candidate_rows) + n(f.true_anomaly_rows)
                  const existing = f.existing_cluster_rate != null ? Number(f.existing_cluster_rate) : rate(f.existing_cluster_rows, f.total_rows)
                  return (
                    <tr key={f.field_name} style={{ borderTop: '1px solid rgba(26,45,74,0.35)' }}>
                      <td className="px-3 py-2 truncate font-semibold" style={{ color: getFieldColor(f.field_name) }}>{f.field_name}</td>
                      <td className="px-3 py-2 text-right text-dust font-mono">{fmt(f.total_rows)}</td>
                      <td className="px-3 py-2 text-right text-dust font-mono">{fmt(f.distinct_calls)}</td>
                      <td className="px-3 py-2 text-right text-dust font-mono">{fmt(f.exact_label_map_rows)}</td>
                      <td className="px-3 py-2 text-right text-dust font-mono">{fmt(f.centroid_similarity_rows)}</td>
                      <td className="px-3 py-2 text-right font-mono" style={{ color: existing >= 0.98 ? '#10b981' : '#f97316' }}>{existing != null ? pct(existing, 1) : '—'}</td>
                      <td className="px-3 py-2 text-right font-mono" style={{ color: emergingCount ? '#f97316' : '#64748b' }}>{fmt(emergingCount)}</td>
                      <td className="px-3 py-2 text-right text-dust font-mono">{f.avg_similarity != null ? Number(f.avg_similarity).toFixed(3) : '—'}</td>
                    </tr>
                  )
                })}
              </tbody>
            </table>
            {!fields.length && <div className="p-4 text-[11px] text-dust">No production field health rows returned.</div>}
          </div>
        </div>

        <div className="rounded-xl overflow-hidden" style={{ border: '1px solid rgba(26,45,74,0.55)' }}>
          <div className="px-4 py-3" style={{ background: 'rgba(255,255,255,0.022)', borderBottom: '1px solid rgba(26,45,74,0.45)' }}>
            <div className="text-[9px] uppercase tracking-[0.2em] text-dust font-bold">Emerging Watchlist</div>
            <div className="text-[10px] text-dust/70 mt-1">Production labels not safe enough for canonical mapping.</div>
          </div>
          <div className="max-h-[320px] overflow-y-auto p-3 flex flex-col gap-2" style={{ scrollbarWidth: 'thin', scrollbarColor: '#1a2d4a transparent' }}>
            {emerging.map((row, i) => {
              let candidates = row.top_candidates
              if (typeof candidates === 'string') {
                try { candidates = JSON.parse(candidates) } catch { candidates = [] }
              }
              const top = safeArray(candidates)[0]
              const color = row.mapping_status === 'TRUE_ANOMALY' ? '#ef4444' : '#f97316'
              return (
                <div key={`${row.source_record_id}-${row.field_name}-${row.normalized_label}-${i}`} className="rounded-xl px-3 py-2" style={{ background: `${color}08`, border: `1px solid ${color}24` }}>
                  <div className="flex items-center justify-between gap-3">
                    <span className="text-[11px] text-star truncate">{row.raw_label}</span>
                    <span className="text-[9.5px] px-2 py-0.5 rounded-md font-semibold" style={{ color, background: `${color}14`, border: `1px solid ${color}28` }}>{normalizeLabel(row.mapping_status)}</span>
                  </div>
                  <div className="mt-1 text-[9.5px] truncate" style={{ color: getFieldColor(row.field_name) }}>{row.field_name}</div>
                  <div className="mt-1 text-[10px] leading-snug" style={{ color: '#64748b' }}>
                    Nearest: {top?.display_name || top?.cluster_name || row.mapped_display_name || '—'} · score {row.similarity_score != null ? Number(row.similarity_score).toFixed(3) : '—'}
                  </div>
                </div>
              )
            })}
            {!emerging.length && <div className="flex items-center gap-2 text-[11px] text-dust"><CheckCircle size={13} /> No emerging labels in the latest mapper run.</div>}
            {configIssues.length > 0 && <div className="mt-2 text-[10px] text-rose-300">{fmt(configIssues.length)} config issue rows need active cluster references.</div>}
          </div>
        </div>
      </div>

      <div className="rounded-xl p-4" style={{ background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(26,45,74,0.55)' }}>
        <div className="text-[9px] uppercase tracking-[0.2em] text-dust mb-3 font-bold">Recent Mapper Runs</div>
        <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-4 gap-2">
          {safeArray(runs?.runs).slice(0, 8).map(r => (
            <div key={r.mapper_run_id} className="rounded-lg px-3 py-2" style={{ background: 'rgba(3,8,15,0.72)', border: '1px solid rgba(26,45,74,0.42)' }}>
              <div className="text-[10px] text-star font-mono truncate">{String(r.mapper_run_id || '').replace('mapper_', '')}</div>
              <div className="text-[9.5px] text-dust mt-1">{fmt(r.total_rows)} labels · {fmt(r.distinct_calls)} calls</div>
              <div className="text-[9.5px] mt-1" style={{ color: n(r.new_cluster_candidate_rows) || n(r.true_anomaly_rows) ? '#f97316' : '#10b981' }}>{fmt(n(r.new_cluster_candidate_rows) + n(r.true_anomaly_rows))} emerging</div>
            </div>
          ))}
          {!safeArray(runs?.runs).length && <div className="text-[11px] text-dust">No mapper run metadata returned.</div>}
        </div>
      </div>
    </div>
  )
}

export default function OverviewPage() {
  const { health, setSelectedClusterId } = useAppCtx()
  const [data, setData] = useState({})
  const [loading, setLoading] = useState(true)

  useEffect(() => {
    let cancelled = false
    setLoading(true)
    Promise.allSettled([
      fetch('/api/semantic-compression').then(r => r.json()),
      fetch('/api/anomaly-intelligence').then(r => r.json()),
      fetch('/api/medoid-intelligence').then(r => r.json()),
      fetch('/api/field-health').then(r => r.json()),
      fetch('/api/duplicate-name-intelligence').then(r => r.json()),
      fetch('/api/recovery-intelligence').then(r => r.json()),
      fetch('/api/drift-summary').then(r => r.json()),
      fetch('/api/run-metadata').then(r => r.ok ? r.json() : r.json().catch(() => ({ runs: [], table_exists: null, _status: r.status }))),
      fetch('/api/review-priorities').then(r => r.json()),
      fetch('/api/production-mapper/summary').then(r => r.json()),
      fetch('/api/production-mapper/runs').then(r => r.json()),
    ]).then(([compression, anomalies, medoid, fieldHealth, duplicates, recovery, drift, runMetadata, priorities, productionMapper, productionRuns]) => {
      if (cancelled) return
      setData({
        compression: compression.status === 'fulfilled' ? compression.value : null,
        anomalies: anomalies.status === 'fulfilled' ? anomalies.value : null,
        medoid: medoid.status === 'fulfilled' ? medoid.value : null,
        fieldHealth: fieldHealth.status === 'fulfilled' ? fieldHealth.value : [],
        duplicates: duplicates.status === 'fulfilled' ? duplicates.value : null,
        recovery: recovery.status === 'fulfilled' ? recovery.value : null,
        drift: drift.status === 'fulfilled' ? drift.value : null,
        runMetadata: runMetadata.status === 'fulfilled' ? runMetadata.value : { runs: [], table_exists: null, _unreachable: true },
        priorities: priorities.status === 'fulfilled' ? priorities.value : [],
        productionMapper: productionMapper.status === 'fulfilled' ? productionMapper.value : { available: false },
        productionRuns: productionRuns.status === 'fulfilled' ? productionRuns.value : { available: false, runs: [] },
      })
    }).finally(() => { if (!cancelled) setLoading(false) })
    return () => { cancelled = true }
  }, [])

  const fields = safeArray(data.compression?.by_field)
  const fieldHealth = safeArray(data.fieldHealth)
  const anomalyByField = safeArray(data.anomalies?.summary?.by_field)
  const priorities = safeArray(data.priorities)
  const runRows = safeArray(data.runMetadata?.runs)
  const productionSummary = data.productionMapper?.summary || null
  const productionEmergingCount = n(productionSummary?.new_cluster_candidate_rows) + n(productionSummary?.true_anomaly_rows)
  const productionConfigIssueCount = n(productionSummary?.no_cluster_reference_rows)
  const medoidByField = safeArray(data.medoid?.by_field)
  const recoveryByField = safeArray(data.recovery?.by_field)

  const rawLabels = n(data.compression?.raw_label_count || health?.total_label_rows)
  const clusters = n(data.compression?.total_clusters || health?.total_clusters)
  const named = n(health?.named_clusters)
  const anomalyClusters = n(data.anomalies?.summary?.total || health?.anomaly_clusters)
  const anomalyLabels = n(data.anomalies?.summary?.anomaly_labels)
  const anomalyOccurrences = n(data.anomalies?.summary?.anomaly_occurrences)
  const compressionRatio = data.compression?.compression_ratio || (rawLabels && clusters ? +(rawLabels / clusters).toFixed(1) : null)
  const reduction = rawLabels && clusters ? 1 - (clusters / rawLabels) : null
  const namedRate = clusters ? named / clusters : null
  const medoidCoverage = data.medoid?.coverage_rate ?? null
  const centroidMissing = health?.centroid_missing_count
  const centroidCoverage = clusters && centroidMissing != null ? 1 - (centroidMissing / clusters) : null
  const anomalyRate = clusters ? anomalyClusters / clusters : null
  const sameFieldDupes = n(data.duplicates?.same_field_duplicate_groups)
  const crossFieldDupes = n(data.duplicates?.cross_field_duplicate_groups)
  const totalCoveredLabels = n(health?.total_label_rows || data.compression?.total_items)

  const maxFieldLabels = Math.max(...fields.map(f => n(f.label_count)), 1)
  const maxAnomaly = Math.max(...anomalyByField.map(f => n(f.anomaly_clusters || f.true_anomaly_count)), 1)

  const fieldMatrix = useMemo(() => {
    const map = new Map()
    const ensure = (field) => {
      if (!field) return null
      if (!map.has(field)) map.set(field, { field_name: field })
      return map.get(field)
    }

    for (const f of fields) {
      const row = ensure(f.field_name)
      if (!row) continue
      row.raw_labels = n(f.label_count)
      row.clusters = n(f.cluster_count)
      row.compression_ratio = f.compression_ratio
      row.avg_size = f.avg_size
    }
    for (const f of fieldHealth) {
      const row = ensure(f.field_name)
      if (!row) continue
      row.clusters = n(f.total_clusters, row.clusters)
      row.named_clusters = n(f.named_clusters)
      row.unnamed_clusters = n(f.unnamed_clusters)
      row.named_rate = f.naming_rate != null ? Number(f.naming_rate) : rate(f.named_clusters, f.total_clusters)
      row.field_anomaly_clusters = n(f.anomaly_clusters)
      row.field_anomaly_rate = f.anomaly_rate != null ? Number(f.anomaly_rate) : rate(f.anomaly_clusters, f.total_clusters)
      row.max_cluster_size = f.max_cluster_size
    }
    for (const f of anomalyByField) {
      const row = ensure(f.field_name)
      if (!row) continue
      row.anomaly_clusters = n(f.anomaly_clusters || f.true_anomaly_count)
      row.anomaly_rate = f.anomaly_rate != null ? Number(f.anomaly_rate) : row.field_anomaly_rate
    }
    for (const f of medoidByField) {
      const row = ensure(f.field_name)
      if (!row) continue
      row.medoid_weak_rate = f.weak_rate != null ? Number(f.weak_rate) : rate(f.weak, f.total)
    }
    for (const f of recoveryByField) {
      const row = ensure(f.field_name)
      if (!row) continue
      row.recovery_rate = f.rescue_rate != null ? Number(f.rescue_rate) : rate(f.recovered_labels, f.total_labels)
      row.recovered_labels = n(f.recovered_labels)
    }
    for (const r of runRows) {
      const row = ensure(r.field_name)
      if (!row) continue
      row.run_id = r.run_id
      row.raw_labels = row.raw_labels || n(r.total_labels)
      row.clusters = row.clusters || n(r.final_cluster_count)
      row.run_anomaly_count = n(r.true_anomaly_count)
      if (r.strict_recovery) {
        row.recovery_rate = row.recovery_rate ?? (r.strict_recovery.label_recovery_rate != null ? Number(r.strict_recovery.label_recovery_rate) : null)
      }
    }

    return [...map.values()].map(row => {
      const ar = row.anomaly_rate ?? row.field_anomaly_rate ?? rate(row.anomaly_clusters ?? row.field_anomaly_clusters ?? row.run_anomaly_count, row.clusters)
      const nr = row.named_rate
      const mr = row.medoid_weak_rate
      const reviewScore = (ar || 0) * 1.4 + (nr != null ? Math.max(0, 1 - nr) : 0) + (mr || 0) * 0.7
      const status = reviewScore > 0.45 ? 'Review' : reviewScore > 0.18 ? 'Watch' : 'Healthy'
      return {
        ...row,
        anomaly_rate: ar,
        named_rate: nr,
        medoid_weak_rate: mr,
        status,
      }
    }).sort((a, b) => {
      const order = { Review: 0, Watch: 1, Healthy: 2 }
      return (order[a.status] - order[b.status]) || n(b.raw_labels) - n(a.raw_labels)
    })
  }, [fields, fieldHealth, anomalyByField, medoidByField, recoveryByField, runRows])

  const strongestCompression = useMemo(() => {
    return [...fields].filter(f => f.compression_ratio).sort((a, b) => n(b.compression_ratio) - n(a.compression_ratio))[0]
  }, [fields])

  const highestAnomaly = useMemo(() => {
    return [...anomalyByField].sort((a, b) => n(b.anomaly_clusters || b.true_anomaly_count) - n(a.anomaly_clusters || a.true_anomaly_count))[0]
  }, [anomalyByField])

  const actionItems = useMemo(() => {
    const items = []
    if (productionEmergingCount > 0) {
      items.push({ title: 'Production emerging labels', severity: 'warning', metric: fmt(productionEmergingCount), detail: 'Latest mapper run found labels that should stay in the emerging watchlist instead of being forced into approved clusters.', target: 'production' })
    }
    if (productionConfigIssueCount > 0) {
      items.push({ title: 'Production config issues', severity: 'critical', metric: fmt(productionConfigIssueCount), detail: 'Some production rows have no active cluster reference for their field.', target: 'production' })
    }
    if (centroidMissing > 0) {
      items.push({ title: 'Centroid rebuild needed', severity: 'critical', metric: fmt(centroidMissing), detail: `${fmt(centroidMissing)} clusters are missing centroid coverage. Rebuild centroids before trusting semantic distance or medoid quality.` })
    }
    if (safeArray(data.medoid?.weak).length) {
      items.push({ title: 'Weak medoid examples found', severity: 'warning', metric: fmt(safeArray(data.medoid?.weak).length), detail: 'Some representative labels look generic or too short. Review medoids before using them as cluster anchors.', target: 'quality' })
    }
    if (sameFieldDupes > 0) {
      items.push({ title: 'Same-field duplicate names', severity: 'warning', metric: fmt(sameFieldDupes), detail: 'Duplicate display names exist inside the same field. These are naming or merge-review candidates.', target: 'merge' })
    }
    if (highestAnomaly) {
      const count = n(highestAnomaly.anomaly_clusters || highestAnomaly.true_anomaly_count)
      items.push({ title: `${highestAnomaly.field_name} anomaly pressure`, severity: count > 1000 ? 'critical' : 'warning', metric: fmt(count), detail: 'This field has the largest visible anomaly load. Review whether these are true unique cases or recoverable threshold failures.', target: 'anomalies' })
    }
    if (!data.drift?.has_computed_drift) {
      items.push({ title: 'Drift comparison not active', severity: 'info', detail: 'The page is ready for drift, but true run-to-run centroid movement and label migration are not computed yet.', target: 'metadata' })
    }
    if (!items.length) {
      items.push({ title: 'No urgent taxonomy blockers', severity: 'good', detail: 'Current health signals do not expose urgent missing coverage, duplicate-name, or weak-medoid issues.' })
    }
    return items.slice(0, 6)
  }, [centroidMissing, data.medoid, data.drift, sameFieldDupes, highestAnomaly, productionEmergingCount, productionConfigIssueCount])

  function openCluster(id) {
    if (id) setSelectedClusterId(id)
  }

  return (
    <div className="min-h-full px-6 py-5" style={{ background: '#02050a' }}>
      <div className="max-w-[1540px] mx-auto">
        <div className="flex items-start justify-between gap-4 mb-3">
          <div>
            <h1 className="text-[22px] font-bold text-star tracking-tight">Taxonomy Intelligence Center</h1>
            <p className="text-[11px] text-dust mt-1 max-w-[820px]">
              One operational page for deciding what to trust, what to review, and what to fix across compression, semantic quality, anomaly pressure, merge risk, coverage, and run metadata.
            </p>
          </div>
          {loading && <div className="text-[10px] uppercase tracking-[0.2em] text-dust">Loading signals…</div>}
        </div>

        <SectionNav />

        <div className="space-y-5">
          <Panel id="summary" title="Taxonomy Health Summary" subtitle="compact readout of the full clustering system" icon={Sparkles}>
            <div className="grid gap-3" style={{ gridTemplateColumns: 'repeat(auto-fit, minmax(190px, 1fr))' }}>
              <MetricCard label="Compression" value={compressionRatio ? `${compressionRatio}×` : '—'} color="#a855f7" icon={GitMerge} note={reduction != null ? `${fmt(rawLabels)} raw labels → ${fmt(clusters)} clusters. ${(reduction * 100).toFixed(1)}% reduction.` : 'Compression source data unavailable.'} onClick={() => scrollToSection('compression')} />
              <MetricCard label="Semantic Quality" value={namedRate != null ? pct(namedRate) : '—'} color="#10b981" icon={ShieldCheck} note={`${fmt(named)} named clusters. Centroid coverage ${centroidCoverage != null ? pct(centroidCoverage) : 'not exposed'}.`} onClick={() => scrollToSection('quality')} />
              <MetricCard label="Anomaly Pressure" value={fmt(anomalyClusters)} color="#ef4444" icon={AlertTriangle} note={`${anomalyRate != null ? pct(anomalyRate, 1) : '—'} of clusters. ${fmt(anomalyLabels)} anomaly labels, ${fmt(anomalyOccurrences)} occurrences.`} onClick={() => scrollToSection('anomalies')} />
              <MetricCard label="Merge Risk" value={fmt(sameFieldDupes)} color="#06b6d4" icon={Target} note={`${fmt(sameFieldDupes)} same-field duplicate-name groups. ${fmt(crossFieldDupes)} cross-field overlaps are tracked separately.`} onClick={() => scrollToSection('merge')} />
              <MetricCard label="Coverage" value={namedRate != null ? pct(namedRate) : '—'} color="#00d4ff" icon={Database} note={`${fmt(totalCoveredLabels)} label-map rows covered by active taxonomy surfaces.`} onClick={() => scrollToSection('coverage')} />
            </div>
          </Panel>

          <Panel id="production" title="Production Mapping" subtitle="hourly mapper output from Iris-facing canonical, emerging, and config feeds" icon={Activity}>
            <ProductionMappingPanel production={data.productionMapper} runs={data.productionRuns} />
          </Panel>

          <Panel id="actions" title="Action Queue" subtitle="prioritized review work generated from the available signals" icon={Zap}>
            <div className="grid grid-cols-1 md:grid-cols-2 xl:grid-cols-3 gap-3 mb-4">
              {actionItems.map((item, i) => (
                <ActionCard key={`${item.title}-${i}`} {...item} onClick={item.target ? () => scrollToSection(item.target) : undefined} />
              ))}
            </div>

            <div className="grid grid-cols-1 xl:grid-cols-2 gap-4">
              <div>
                <div className="text-[9px] uppercase tracking-[0.2em] text-dust mb-3 font-bold">Cluster-level review examples</div>
                <div className="flex flex-col gap-2">
                  {priorities.slice(0, 6).map(item => <ClusterRow key={item.id} item={item} onClick={openCluster} rightLabel={normalizeLabel((item.reasons || ['review'])[0])} />)}
                  {!priorities.length && <div className="flex items-center gap-2 text-[11px] text-dust"><CheckCircle size={13} /> No cluster-level priority rows returned.</div>}
                </div>
              </div>
              <div className="rounded-xl p-4" style={{ background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(26,45,74,0.55)' }}>
                <div className="text-[9px] uppercase tracking-[0.2em] text-dust mb-3 font-bold">How to read this page</div>
                <p className="text-[11px] leading-relaxed" style={{ color: '#64748b' }}>
                  This page is not another visualization tab. It is the control room for taxonomy quality: first check the Action Queue, then use the Field Health Matrix to decide which field needs cleanup, then inspect compression, anomalies, merge risk, and metadata only where needed.
                </p>
              </div>
            </div>
          </Panel>

          <Panel id="matrix" title="Field Health Matrix" subtitle="one comparison table for every taxonomy field" icon={LineChart} compact>
            <FieldHealthMatrix rows={fieldMatrix} />
          </Panel>

          <Panel id="compression" title="Compression Intelligence" subtitle="raw taxonomy language consolidated into semantic clusters" icon={GitMerge}>
            <div className="grid grid-cols-1 lg:grid-cols-[0.9fr_1.4fr] gap-5">
              <div className="rounded-xl p-4" style={{ background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(26,45,74,0.55)' }}>
                <DataRow label="Raw labels" value={fmt(rawLabels)} />
                <DataRow label="Final clusters" value={fmt(clusters)} />
                <DataRow label="Compression ratio" value={compressionRatio ? `${compressionRatio}×` : '—'} color="#a855f7" chip />
                <DataRow label="Reduction" value={reduction != null ? `${(reduction * 100).toFixed(1)}%` : '—'} color="#10b981" chip />
                <DataRow label="Strongest field" value={strongestCompression ? `${strongestCompression.field_name} (${strongestCompression.compression_ratio}×)` : '—'} />
              </div>
              <div>
                {fields.map(f => (
                  <FieldBarRow
                    key={f.field_name}
                    field={f.field_name}
                    left={`${fmt(f.label_count || 0)} labels`}
                    right={f.compression_ratio ? `${f.compression_ratio}×` : '—'}
                    percent={(n(f.label_count) / maxFieldLabels) * 100}
                  />
                ))}
                {!fields.length && <div className="text-dust text-xs">No field-level compression data returned.</div>}
              </div>
            </div>
          </Panel>

          <Panel id="quality" title="Semantic Quality" subtitle="naming, centroid, medoid, and cleanup signals" icon={ShieldCheck}>
            <div className="grid grid-cols-1 lg:grid-cols-3 gap-5">
              <div className="rounded-xl p-4" style={{ background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(26,45,74,0.55)' }}>
                <Progress label="Named clusters" value={namedRate || 0} color="#10b981" right={namedRate != null ? pct(namedRate) : '—'} />
                <Progress label="Centroid coverage" value={centroidCoverage || 0} color="#00d4ff" right={centroidCoverage != null ? pct(centroidCoverage) : '—'} />
                <Progress label="Medoid coverage" value={medoidCoverage || 0} color="#f97316" right={medoidCoverage != null ? pct(medoidCoverage) : '—'} />
                <DataRow label="Centroids missing" value={centroidMissing ?? 'not exposed'} />
                <DataRow label="Weak medoid examples" value={safeArray(data.medoid?.weak).length} />
              </div>
              <div className="lg:col-span-2 grid grid-cols-1 xl:grid-cols-2 gap-3">
                <div>
                  <div className="text-[9px] uppercase tracking-[0.2em] text-dust mb-3 font-bold">Weak medoid examples</div>
                  <div className="flex flex-col gap-2">
                    {safeArray(data.medoid?.weak).slice(0, 8).map(item => <ClusterRow key={item.id} item={item} onClick={openCluster} rightLabel="weak medoid" />)}
                    {!safeArray(data.medoid?.weak).length && <div className="flex items-center gap-2 text-[11px] text-dust"><CheckCircle size={13} /> No weak medoid examples returned.</div>}
                  </div>
                </div>
                <div>
                  <div className="text-[9px] uppercase tracking-[0.2em] text-dust mb-3 font-bold">Medoid risk by field</div>
                  <div className="flex flex-col gap-1">
                    {medoidByField.slice(0, 8).map(f => <FieldBarRow key={f.field_name} field={f.field_name} left={`${fmt(f.weak)} weak`} right={f.weak_rate != null ? pct(f.weak_rate, 1) : '—'} percent={n(f.weak_rate) * 100} color="#f97316" />)}
                    {!medoidByField.length && <div className="text-[11px] text-dust">No medoid-by-field data returned.</div>}
                  </div>
                </div>
              </div>
            </div>
          </Panel>

          <Panel id="anomalies" title="Anomaly Intelligence" subtitle="true anomaly pressure and strict graph recovery signals" icon={AlertTriangle}>
            <div className="grid grid-cols-1 lg:grid-cols-[0.9fr_1.4fr] gap-5">
              <div className="rounded-xl p-4" style={{ background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(26,45,74,0.55)' }}>
                <DataRow label="Anomaly clusters" value={fmt(anomalyClusters)} color="#ef4444" chip />
                <DataRow label="Anomaly labels" value={fmt(anomalyLabels)} />
                <DataRow label="Anomaly occurrences" value={fmt(anomalyOccurrences)} />
                <DataRow label="Highest anomaly field" value={highestAnomaly ? highestAnomaly.field_name : '—'} />
                <DataRow label="Recovery available" value={data.recovery?.has_recovery ? 'Yes' : 'Metadata/API only'} color={data.recovery?.has_recovery ? '#10b981' : '#64748b'} chip />
                {data.recovery?.has_recovery && <Progress label="Rescue rate" value={data.recovery.rescue_rate || 0} color="#a855f7" right={pct(data.recovery.rescue_rate || 0, 1)} />}
                <p className="text-[10.5px] leading-relaxed mt-4" style={{ color: '#64748b' }}>
                  Anomalies are labels not absorbed into stable semantic clusters. They can be true unique cases, unresolved drift, or recoverable threshold failures.
                </p>
              </div>
              <div>
                {anomalyByField.map(f => {
                  const count = n(f.anomaly_clusters || f.true_anomaly_count)
                  return <FieldBarRow key={f.field_name} field={f.field_name} left={`${fmt(count)} anomalies`} right={f.anomaly_rate != null ? pct(f.anomaly_rate, 1) : ''} percent={(count / maxAnomaly) * 100} color="#ef4444" />
                })}
                {!anomalyByField.length && <div className="text-dust text-xs">No anomaly-by-field breakdown returned.</div>}
              </div>
            </div>
          </Panel>

          <Panel id="merge" title="Merge Intelligence" subtitle="duplicate-name and merge-risk signals without cross-field false positives" icon={Target}>
            <div className="grid grid-cols-1 lg:grid-cols-2 gap-5">
              <div>
                <div className="text-[9px] uppercase tracking-[0.2em] text-dust mb-3 font-bold">Same-field duplicate risk</div>
                <div className="flex flex-col gap-2">
                  {safeArray(data.duplicates?.same_field_examples).slice(0, 10).map((d, i) => (
                    <div key={`${d.field_name}-${d.display_name}-${i}`} className="rounded-xl px-3 py-2" style={{ background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(26,45,74,0.45)' }}>
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-[11px] text-star truncate">{d.display_name}</span>
                        <span className="text-[10px] px-2 py-0.5 rounded-md" style={{ color: '#06b6d4', background: 'rgba(6,182,212,0.12)', border: '1px solid rgba(6,182,212,0.22)' }}>{d.cluster_count} clusters</span>
                      </div>
                      <div className="text-[9.5px] mt-1" style={{ color: getFieldColor(d.field_name) }}>{d.field_name}</div>
                    </div>
                  ))}
                  {!safeArray(data.duplicates?.same_field_examples).length && <div className="text-[11px] text-dust">No same-field duplicate groups returned.</div>}
                </div>
              </div>
              <div>
                <div className="text-[9px] uppercase tracking-[0.2em] text-dust mb-3 font-bold">Cross-field naming overlap</div>
                <div className="flex flex-col gap-2">
                  {safeArray(data.duplicates?.cross_field_examples).slice(0, 10).map((d, i) => (
                    <div key={`${d.display_name}-${i}`} className="rounded-xl px-3 py-2" style={{ background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(26,45,74,0.45)' }}>
                      <div className="flex items-center justify-between gap-3">
                        <span className="text-[11px] text-star truncate">{d.display_name}</span>
                        <span className="text-[10px] px-2 py-0.5 rounded-md" style={{ color: '#64748b', background: 'rgba(100,116,139,0.12)', border: '1px solid rgba(100,116,139,0.22)' }}>not auto-merge</span>
                      </div>
                      <div className="text-[9.5px] mt-1 text-dust truncate">{safeArray(d.fields).join(', ')}</div>
                    </div>
                  ))}
                  {!safeArray(data.duplicates?.cross_field_examples).length && <div className="text-[11px] text-dust">No cross-field overlaps returned.</div>}
                </div>
              </div>
            </div>
          </Panel>

          <Panel id="coverage" title="Coverage Intelligence" subtitle="active taxonomy coverage and field health" icon={Database}>
            <div className="grid grid-cols-1 lg:grid-cols-[0.8fr_1.5fr] gap-5">
              <div className="rounded-xl p-4" style={{ background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(26,45,74,0.55)' }}>
                <DataRow label="Total clusters" value={fmt(clusters)} />
                <DataRow label="Named clusters" value={fmt(named)} />
                <DataRow label="Unnamed clusters" value={fmt(health?.unnamed_clusters || 0)} />
                <DataRow label="Label-map rows" value={fmt(totalCoveredLabels)} />
                <DataRow label="Fields" value={health?.fields_count || fieldHealth.length || '—'} />
                <DataRow label="Run records" value={health?.last_run_count ?? runRows.length ?? '—'} />
              </div>
              <div>
                {fieldHealth.map(f => (
                  <FieldBarRow
                    key={f.field_name}
                    field={f.field_name}
                    left={`${fmt(f.total_clusters)} clusters`}
                    right={f.naming_rate != null ? pct(f.naming_rate) : '—'}
                    percent={(n(f.named_clusters) / Math.max(1, n(f.total_clusters))) * 100}
                  />
                ))}
                {!fieldHealth.length && <div className="text-dust text-xs">No field health rows returned.</div>}
              </div>
            </div>
          </Panel>

          <Panel id="metadata" title="Run Metadata" subtitle="model, clustering config, graph recovery, and drift readiness" icon={Layers}>
            <div className="grid grid-cols-1 xl:grid-cols-[1fr_1fr] gap-5">
              <div>
                <div className="text-[9px] uppercase tracking-[0.2em] text-dust mb-3 font-bold">Current runs by field</div>
                <div className="overflow-hidden rounded-xl" style={{ border: '1px solid rgba(26,45,74,0.55)' }}>
                  <table className="w-full text-left text-[10.5px]">
                    <thead style={{ background: 'rgba(255,255,255,0.025)', color: '#64748b' }}>
                      <tr>
                        <th className="px-3 py-2 font-semibold">Field</th>
                        <th className="px-3 py-2 font-semibold">Run</th>
                        <th className="px-3 py-2 font-semibold">Model</th>
                        <th className="px-3 py-2 font-semibold">Device</th>
                        <th className="px-3 py-2 font-semibold">k / θ</th>
                      </tr>
                    </thead>
                    <tbody>
                      {runRows.slice(0, 12).map((r, i) => (
                        <tr key={`${r.field_name}-${r.run_id}-${i}`} style={{ borderTop: '1px solid rgba(26,45,74,0.35)' }}>
                          <td className="px-3 py-2 truncate" style={{ color: getFieldColor(r.field_name) }}>{r.field_name}</td>
                          <td className="px-3 py-2 text-dust font-mono">{r.run_id}</td>
                          <td className="px-3 py-2 text-dust truncate max-w-[220px]">{r.model_name || '—'}</td>
                          <td className="px-3 py-2 text-dust">{r.embedding_device || '—'}</td>
                          <td className="px-3 py-2 text-dust font-mono">{r.graph_k_values || r.strict_recovery?.k_neighbors || '—'} / {r.graph_threshold_values || r.strict_recovery?.similarity_threshold || '—'}</td>
                        </tr>
                      ))}
                    </tbody>
                  </table>
                  {!runRows.length && (
                    <div className="p-4 text-[11px] text-dust">
                      {data.runMetadata?.table_exists === false
                        ? 'Table taxonomy_run_metadata does not exist — run the pipeline to populate it.'
                        : data.runMetadata?.table_exists === true
                          ? 'taxonomy_run_metadata table exists but has no rows yet.'
                          : data.runMetadata?._unreachable
                            ? 'Run metadata unavailable — API server not responding on port 5050.'
                            : data.runMetadata?._status
                              ? `API error ${data.runMetadata._status} — check the server logs.`
                              : 'Run metadata unavailable.'}
                    </div>
                  )}
                </div>
              </div>
              <div>
                <div className="text-[9px] uppercase tracking-[0.2em] text-dust mb-3 font-bold">Drift readiness</div>
                <div className="rounded-xl p-4" style={{ background: 'rgba(255,255,255,0.022)', border: '1px solid rgba(26,45,74,0.55)' }}>
                  <DataRow label="Drift status" value={data.drift?.has_computed_drift ? 'Computed' : 'Not computed yet'} color={data.drift?.has_computed_drift ? '#10b981' : '#f97316'} chip />
                  <DataRow label="Latest run metadata rows" value={fmt(runRows.length)} />
                  <DataRow label="Fields ready for comparison" value={fmt(data.runMetadata?.fields_with_runs || 0)} />
                  <DataRow label="Newest cluster examples" value={fmt(safeArray(data.drift?.newest_clusters).length)} />
                  <p className="text-[11px] leading-relaxed mt-4" style={{ color: '#64748b' }}>
                    Drift stays inside this Intelligence page. Until true run-to-run centroid movement, label migration, and cluster birth/death are computed, this area shows readiness and metadata instead of pretending drift exists.
                  </p>
                </div>
              </div>
            </div>
          </Panel>
        </div>
      </div>
    </div>
  )
}
