import { useEffect, useState, useCallback } from 'react'
import {
  Activity, AlertTriangle, ArrowRight, CheckCircle, Clock,
  Database, FileText, Filter, RefreshCw, Tag, Zap,
} from 'lucide-react'
import { fmt } from '../utils/format.js'
import { getFieldColor } from '../utils/colors.js'
import { fetchJson } from '../utils/api.js'

const EMPTY = []

function safeArray(v) { return Array.isArray(v) ? v : EMPTY }

function n(v, fb = 0) { const x = Number(v); return Number.isFinite(x) ? x : fb }

function pct(num, denom, digits = 1) {
  const d = n(denom)
  if (!d) return '—'
  return `${((n(num) / d) * 100).toFixed(digits)}%`
}

function fmtDate(v) {
  if (!v) return '—'
  try { return new Date(v).toLocaleString() } catch { return String(v) }
}

function fmtSecs(secs) {
  if (!Number.isFinite(secs) || secs < 0) return '—'
  if (secs < 60)   return `${secs}s`
  if (secs < 3600) return `${Math.floor(secs / 60)}m ${secs % 60}s`
  return `${Math.floor(secs / 3600)}h ${Math.floor((secs % 3600) / 60)}m`
}

function fmtDuration(a, b) {
  if (!a || !b) return '—'
  return fmtSecs(Math.round((new Date(b) - new Date(a)) / 1000))
}

// ── Shared primitives (same style as OverviewPage) ────────────────────────────

function Panel({ id, title, subtitle, icon: Icon = Activity, children, compact = false, action }) {
  return (
    <section
      id={id}
      className="rounded-2xl overflow-hidden scroll-mt-20"
      style={{
        background: 'rgba(5,11,22,0.78)',
        border: '1px solid rgba(26,45,74,0.78)',
        boxShadow: '0 18px 40px rgba(0,0,0,0.18)',
      }}
    >
      <div
        className="flex items-center justify-between gap-4 px-5 py-4"
        style={{ borderBottom: '1px solid rgba(26,45,74,0.62)' }}
      >
        <div className="flex items-center gap-3 min-w-0">
          <div
            className="w-9 h-9 rounded-xl flex items-center justify-center flex-shrink-0"
            style={{
              background: 'rgba(0,212,255,0.08)',
              border: '1px solid rgba(0,212,255,0.20)',
              color: '#00d4ff',
            }}
          >
            <Icon size={16} />
          </div>
          <div className="min-w-0">
            <h2 className="text-[14px] font-bold text-star tracking-tight">{title}</h2>
            {subtitle && <p className="text-[10.5px] text-dust mt-0.5 truncate">{subtitle}</p>}
          </div>
        </div>
        {action && <div className="flex-shrink-0">{action}</div>}
      </div>
      <div className={compact ? 'p-0' : 'p-5'}>{children}</div>
    </section>
  )
}

function MetricCard({ label, value, note, color = '#00d4ff', icon: Icon = Zap }) {
  return (
    <div
      className="rounded-2xl p-4 min-w-0"
      style={{
        background: `linear-gradient(135deg, ${color}10, rgba(255,255,255,0.018))`,
        border: `1px solid ${color}28`,
        boxShadow: `0 0 26px ${color}08`,
      }}
    >
      <div className="flex items-start justify-between gap-3">
        <div className="min-w-0">
          <div className="text-[9px] uppercase tracking-[0.22em] font-bold" style={{ color }}>
            {label}
          </div>
          <div
            className="mt-2 text-[24px] leading-none font-bold truncate"
            style={{ color, textShadow: `0 0 18px ${color}40` }}
          >
            {value}
          </div>
        </div>
        <Icon size={17} style={{ color, opacity: 0.75 }} />
      </div>
      {note && (
        <div className="mt-3 text-[11px] leading-snug" style={{ color: '#64748b' }}>
          {note}
        </div>
      )}
    </div>
  )
}

function StatusBadge({ status, dryRun }) {
  const dry = Boolean(dryRun)
  const colors = {
    RUNNING:      '#f59e0b',
    DONE:         '#10b981',
    DRY_RUN_DONE: '#00d4ff',
    FAILED:       '#ef4444',
  }
  const color = colors[status] || '#94a3b8'
  const label = dry && status === 'DONE' ? 'DRY_RUN_DONE' : status
  return (
    <span
      className="inline-flex items-center gap-1 px-2 py-0.5 rounded-full text-[10px] font-bold uppercase tracking-wider"
      style={{ background: color + '22', color, border: `1px solid ${color}44` }}
    >
      {label || '—'}
    </span>
  )
}

function TH({ children, style }) {
  return (
    <th
      className="px-3 py-2 text-left text-[10px] font-bold uppercase tracking-wider"
      style={{ color: '#64748b', borderBottom: '1px solid rgba(26,45,74,0.72)', ...style }}
    >
      {children}
    </th>
  )
}

function TD({ children, style, mono = false }) {
  return (
    <td
      className={`px-3 py-2 text-[12px] ${mono ? 'font-mono' : ''}`}
      style={{ color: '#94a3b8', borderBottom: '1px solid rgba(26,45,74,0.35)', ...style }}
    >
      {children}
    </td>
  )
}

function FieldPill({ fieldName }) {
  const color = getFieldColor(fieldName)
  return (
    <span
      className="inline-block px-1.5 py-0.5 rounded text-[10px] font-semibold"
      style={{ background: color + '22', color }}
    >
      {fieldName}
    </span>
  )
}

function ResolverBadge({ status, materialized }) {
  const colors = {
    MAP_TO_EXISTING: '#10b981',
    ANOMALY:         '#f59e0b',
    PROMOTE:         '#a855f7',
    MATERIALIZED:    '#00d4ff',
  }
  if (!status) return <span className="text-[11px]" style={{ color: '#64748b' }}>—</span>
  const color = colors[status] || '#94a3b8'
  return (
    <span className="inline-flex items-center gap-1">
      <span
        className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-bold uppercase"
        style={{ background: color + '22', color }}
      >
        {status.replace(/_/g, ' ')}
      </span>
      {materialized && (
        <span
          className="inline-flex items-center px-1.5 py-0.5 rounded text-[10px] font-semibold"
          style={{ background: '#00d4ff18', color: '#00d4ff', border: '1px solid #00d4ff33' }}
          title="Materialized by autopilot"
        >
          <CheckCircle size={9} className="mr-0.5" />
          done
        </span>
      )}
    </span>
  )
}

function GuardBadge({ value, label }) {
  if (!value) return <span className="text-[11px]" style={{ color: '#64748b' }}>OK</span>
  return (
    <span
      className="inline-flex items-center gap-1 px-1.5 py-0.5 rounded text-[10px]"
      style={{ background: '#f59e0b22', color: '#f59e0b', border: '1px solid #f59e0b44' }}
      title={value}
    >
      <AlertTriangle size={9} />
      {label}
    </span>
  )
}

function Pagination({ offset, limit, total, onPage }) {
  const page    = Math.floor(offset / limit)
  const pages   = Math.ceil(total / limit)
  if (pages <= 1) return null
  return (
    <div className="flex items-center justify-between gap-3 px-3 py-2 text-[11px]" style={{ color: '#64748b' }}>
      <span>{total.toLocaleString()} total</span>
      <div className="flex items-center gap-1">
        <button
          disabled={page === 0}
          onClick={() => onPage(Math.max(0, offset - limit))}
          className="px-2 py-1 rounded disabled:opacity-30"
          style={{ background: 'rgba(26,45,74,0.5)' }}
        >
          ‹
        </button>
        <span className="px-2">
          {page + 1} / {pages}
        </span>
        <button
          disabled={page >= pages - 1}
          onClick={() => onPage(offset + limit)}
          className="px-2 py-1 rounded disabled:opacity-30"
          style={{ background: 'rgba(26,45,74,0.5)' }}
        >
          ›
        </button>
      </div>
    </div>
  )
}

// ── Drilldown: row/field audit ────────────────────────────────────────────────

function RowAuditPanel({ runId, onClose }) {
  const [data, setData]         = useState(null)
  const [loading, setLoading]   = useState(false)
  const [offset, setOffset]     = useState(0)
  const [statusFilter, setStatusFilter] = useState('')
  const [expandedRow, setExpanded]      = useState(null)
  const LIMIT = 20

  const load = useCallback(async (off = 0) => {
    setLoading(true)
    try {
      const params = new URLSearchParams({ run_id: runId, limit: LIMIT, offset: off })
      if (statusFilter) params.set('row_status', statusFilter)
      const d = await fetchJson(`/api/backfill/row-audit?${params}`)
      setData(d)
      setOffset(off)
    } catch (err) {
      console.error(err)
    } finally {
      setLoading(false)
    }
  }, [runId, statusFilter])

  useEffect(() => { load(0) }, [load])

  const STATUS_OPTS = ['', 'CHANGED', 'UNCHANGED', 'ERROR', 'SKIPPED_ALREADY_UPDATED']

  return (
    <div
      className="fixed inset-0 z-50 flex items-center justify-center p-4"
      style={{ background: 'rgba(0,0,0,0.72)' }}
    >
      <div
        className="rounded-2xl overflow-hidden flex flex-col"
        style={{
          width: 'min(900px, 96vw)',
          maxHeight: '85vh',
          background: '#060d1a',
          border: '1px solid rgba(26,45,74,0.9)',
        }}
      >
        {/* Header */}
        <div
          className="flex items-center justify-between px-5 py-3 flex-shrink-0"
          style={{ borderBottom: '1px solid rgba(26,45,74,0.7)' }}
        >
          <div className="flex items-center gap-3">
            <FileText size={15} style={{ color: '#00d4ff' }} />
            <span className="text-[13px] font-bold text-star">Row Audit</span>
            <span className="text-[10px] font-mono" style={{ color: '#64748b' }}>{runId}</span>
          </div>
          <div className="flex items-center gap-2">
            <select
              value={statusFilter}
              onChange={e => setStatusFilter(e.target.value)}
              className="text-[11px] rounded px-2 py-1"
              style={{ background: 'rgba(26,45,74,0.8)', color: '#94a3b8', border: '1px solid rgba(26,45,74,0.9)' }}
            >
              {STATUS_OPTS.map(s => (
                <option key={s} value={s}>{s || 'All statuses'}</option>
              ))}
            </select>
            <button
              onClick={onClose}
              className="px-3 py-1 rounded text-[11px]"
              style={{ background: 'rgba(26,45,74,0.6)', color: '#94a3b8' }}
            >
              Close
            </button>
          </div>
        </div>

        <div className="flex-1 overflow-y-auto">
          {loading && (
            <div className="flex items-center justify-center py-12">
              <div className="w-6 h-6 rounded-full border-2 border-cyan/20 border-t-cyan animate-spin" />
            </div>
          )}
          {!loading && data && (
            <>
              <table className="w-full border-collapse">
                <thead>
                  <tr style={{ background: 'rgba(26,45,74,0.35)' }}>
                    <TH>Row ID</TH>
                    <TH>Call ID</TH>
                    <TH>Status</TH>
                    <TH>Changed</TH>
                    <TH>Unmapped</TH>
                    <TH></TH>
                  </tr>
                </thead>
                <tbody>
                  {safeArray(data.rows).map(row => (
                    <>
                      <tr
                        key={row.id}
                        onClick={() => setExpanded(expandedRow === row.id ? null : row.id)}
                        className="cursor-pointer hover:bg-white/[0.02] transition-colors"
                      >
                        <TD mono>{String(row.stage_row_id || '').slice(0, 18)}</TD>
                        <TD mono>{String(row.call_id || '—').slice(0, 18)}</TD>
                        <TD>
                          <StatusBadge status={row.row_status} dryRun={false} />
                        </TD>
                        <TD>
                          {safeArray(row.changed_fields).length > 0
                            ? safeArray(row.changed_fields).join(', ')
                            : <span style={{ color: '#475569' }}>—</span>}
                        </TD>
                        <TD>
                          {safeArray(row.unmapped_fields).length > 0
                            ? safeArray(row.unmapped_fields).join(', ')
                            : <span style={{ color: '#475569' }}>—</span>}
                        </TD>
                        <TD>
                          <ArrowRight
                            size={12}
                            style={{
                              color: '#64748b',
                              transform: expandedRow === row.id ? 'rotate(90deg)' : 'none',
                              transition: 'transform 150ms',
                            }}
                          />
                        </TD>
                      </tr>
                      {expandedRow === row.id && safeArray(row.field_audit).length > 0 && (
                        <tr key={`${row.id}-expand`}>
                          <td colSpan={6} className="px-4 pb-3 pt-1">
                            <div
                              className="rounded-xl overflow-hidden"
                              style={{ background: 'rgba(26,45,74,0.3)', border: '1px solid rgba(26,45,74,0.5)' }}
                            >
                              <table className="w-full border-collapse">
                                <thead>
                                  <tr>
                                    <TH>Field</TH>
                                    <TH>Status</TH>
                                    <TH>Old Value</TH>
                                    <TH>New Value</TH>
                                    <TH>Unmapped</TH>
                                  </tr>
                                </thead>
                                <tbody>
                                  {safeArray(row.field_audit).map((fa, i) => (
                                    <tr key={i}>
                                      <TD><FieldPill fieldName={fa.field_name} /></TD>
                                      <TD>
                                        <ResolverBadge status={fa.field_status} />
                                      </TD>
                                      <TD style={{ maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                        {JSON.stringify(fa.old_value) ?? '—'}
                                      </TD>
                                      <TD style={{ maxWidth: 180, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                                        {JSON.stringify(fa.new_value) ?? '—'}
                                      </TD>
                                      <TD>
                                        {safeArray(fa.unmapped_labels).join(', ') || <span style={{ color: '#475569' }}>—</span>}
                                      </TD>
                                    </tr>
                                  ))}
                                </tbody>
                              </table>
                            </div>
                          </td>
                        </tr>
                      )}
                    </>
                  ))}
                </tbody>
              </table>
              <Pagination
                offset={offset}
                limit={LIMIT}
                total={data.total}
                onPage={off => load(off)}
              />
            </>
          )}
        </div>
      </div>
    </div>
  )
}

// ── Main page ─────────────────────────────────────────────────────────────────

export default function BackfillMonitor() {
  const [summary, setSummary]       = useState(null)
  const [runs, setRuns]             = useState(null)
  const [fields, setFields]         = useState(null)
  const [unresolved, setUnresolved] = useState(null)
  const [loading, setLoading]       = useState(false)
  const [liveElapsed, setLiveElapsed] = useState(0)

  const [runsOffset,       setRunsOffset]       = useState(0)
  const [unresolvedOffset, setUnresolvedOffset] = useState(0)
  const [fieldFilter,      setFieldFilter]      = useState('')
  const [statusFilter,     setStatusFilter]     = useState('')

  const [drillRunId, setDrillRunId] = useState(null)

  const RUNS_LIMIT       = 15
  const UNRESOLVED_LIMIT = 30
  const POLL_INTERVAL_MS = 4000

  const fetchAll = useCallback(async (rOff = 0, uOff = 0, fieldF = fieldFilter, statusF = statusFilter) => {
    setLoading(true)
    try {
      const uParams = new URLSearchParams({ limit: UNRESOLVED_LIMIT, offset: uOff })
      if (fieldF)  uParams.set('field_name', fieldF)
      if (statusF) uParams.set('resolver_status', statusF)

      const [sum, r, f, u] = await Promise.all([
        fetchJson('/api/backfill/summary'),
        fetchJson(`/api/backfill/runs?limit=${RUNS_LIMIT}&offset=${rOff}`),
        fetchJson('/api/backfill/fields'),
        fetchJson(`/api/backfill/unresolved?${uParams}`),
      ])
      setSummary(sum)
      setRuns(r)
      setFields(f)
      setUnresolved(u)
      setRunsOffset(rOff)
      setUnresolvedOffset(uOff)
    } catch (err) {
      console.error('BackfillMonitor fetch error:', err)
    } finally {
      setLoading(false)
    }
  }, [fieldFilter, statusFilter])

  useEffect(() => { fetchAll() }, [])

  // Auto-poll summary only while a run is RUNNING
  const isRunning = summary?.latest_run?.status === 'RUNNING'
  useEffect(() => {
    if (!isRunning) return
    const id = setInterval(async () => {
      try {
        const sum = await fetchJson('/api/backfill/summary')
        setSummary(sum)
      } catch { /* silent */ }
    }, POLL_INTERVAL_MS)
    return () => clearInterval(id)
  }, [isRunning])

  // Live elapsed-time counter for the progress bar when status = RUNNING
  useEffect(() => {
    if (!isRunning) { setLiveElapsed(0); return }
    const start = summary?.latest_run?.started_at
      ? new Date(summary.latest_run.started_at)
      : new Date()
    const tick = () => setLiveElapsed(Math.round((Date.now() - start) / 1000))
    tick()
    const id = setInterval(tick, 1000)
    return () => clearInterval(id)
  }, [isRunning, summary?.latest_run?.started_at])

  const latestRun = summary?.latest_run || null
  const resolver  = summary?.resolver_counts || {}
  const fieldRows = safeArray(fields?.rows)
  const runRows   = safeArray(runs?.rows)
  const unresolvedRows = safeArray(unresolved?.rows)

  // Unique field names for filter
  const fieldNames = [...new Set(unresolvedRows.map(r => r.field_name).filter(Boolean))]

  const RESOLVER_STATUSES = ['', 'null', 'MAP_TO_EXISTING', 'ANOMALY', 'PROMOTE', 'MATERIALIZED']

  return (
    <div className="flex flex-col gap-5 p-5 max-w-[1200px] mx-auto">

      {/* Page header */}
      <div className="flex items-center justify-between gap-4">
        <div>
          <h1 className="text-[18px] font-bold text-star tracking-tight">Backfill Monitor</h1>
          <p className="text-[11px] mt-0.5" style={{ color: '#64748b' }}>
            Column-level audit of STAGE taxonomy backfill runs
          </p>
        </div>
        <button
          onClick={() => fetchAll()}
          disabled={loading}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-[12px] transition-all disabled:opacity-40"
          style={{ background: 'rgba(0,212,255,0.08)', border: '1px solid rgba(0,212,255,0.22)', color: '#00d4ff' }}
        >
          <RefreshCw size={13} className={loading ? 'animate-spin' : ''} />
          Refresh
        </button>
      </div>

      {/* Latest run summary strip */}
      {latestRun && (() => {
        // Live data from summary_json when RUNNING
        const liveData = isRunning && summary?.latest_run
          ? (typeof summary.latest_run.summary_json === 'object'
              ? summary.latest_run.summary_json
              : null)
          : null

        const scanned    = n(liveData?.rows_scanned   ?? latestRun.rows_scanned)
        const changed    = n(liveData?.rows_changed    ?? latestRun.rows_changed)
        const unchanged  = n(liveData?.rows_unchanged  ?? latestRun.rows_unchanged)
        const errors     = n(liveData?.rows_error      ?? latestRun.rows_error)
        const pending    = n(latestRun.rows_pending_before)
        const pct        = pending > 0 ? Math.min(100, (scanned / pending) * 100) : 0
        const rate       = n(liveData?.rate_rows_per_sec)
        const etaSecs    = liveData?.eta_seconds != null ? n(liveData.eta_seconds) : null

        return (
          <Panel
            id="latest-run"
            title="Latest Run"
            subtitle={latestRun.backfill_run_id}
            icon={Activity}
            action={isRunning && (
              <span
                className="flex items-center gap-1.5 px-2 py-0.5 rounded-full text-[10px] font-bold animate-pulse"
                style={{ background: '#10b98122', color: '#10b981', border: '1px solid #10b98144' }}
              >
                <span className="w-1.5 h-1.5 rounded-full bg-current" />
                LIVE
              </span>
            )}
          >
            {/* Progress bar — shown while RUNNING */}
            {isRunning && (
              <div className="mb-5">
                <div className="flex justify-between items-end mb-1.5 text-[11px]">
                  <span style={{ color: '#64748b' }}>
                    {scanned.toLocaleString()} / {pending > 0 ? pending.toLocaleString() : '?'} rows
                    {rate > 0 && (
                      <span style={{ color: '#475569' }}> · {rate.toLocaleString()} rows/s</span>
                    )}
                  </span>
                  <span className="font-mono" style={{ color: '#00d4ff' }}>
                    {pending > 0 ? `${pct.toFixed(1)}%` : '—'}
                    {etaSecs != null && (
                      <span style={{ color: '#64748b' }}> · ETA {fmtSecs(etaSecs)}</span>
                    )}
                  </span>
                </div>
                <div className="h-2 rounded-full overflow-hidden" style={{ background: 'rgba(26,45,74,0.72)' }}>
                  <div
                    className="h-full rounded-full transition-all duration-1000"
                    style={{
                      width: `${pct}%`,
                      background: 'linear-gradient(90deg, #00d4ff, #00d4ff88)',
                      boxShadow: '0 0 9px #00d4ff55',
                    }}
                  />
                </div>
                {/* Live row counters */}
                <div className="mt-2.5 grid grid-cols-4 gap-2 text-[11px]">
                  {[
                    { label: 'Scanned',   value: scanned,   color: '#94a3b8' },
                    { label: 'Changed',   value: changed,   color: '#10b981' },
                    { label: 'Unchanged', value: unchanged, color: '#64748b' },
                    { label: 'Errors',    value: errors,    color: errors > 0 ? '#ef4444' : '#64748b' },
                  ].map(({ label, value, color }) => (
                    <div key={label} className="rounded-lg px-2 py-1.5 text-center" style={{ background: 'rgba(26,45,74,0.45)' }}>
                      <div className="text-[9px] uppercase tracking-wider" style={{ color: '#475569' }}>{label}</div>
                      <div className="text-[16px] font-bold font-mono mt-0.5" style={{ color }}>{value.toLocaleString()}</div>
                    </div>
                  ))}
                </div>
                {/* Elapsed */}
                <div className="mt-2 text-right text-[10px]" style={{ color: '#475569' }}>
                  Elapsed: {fmtSecs(liveElapsed)}
                </div>
              </div>
            )}

            <div className="grid grid-cols-2 gap-x-8 gap-y-2 text-[12px]">
              <div className="flex items-center justify-between gap-2">
                <span style={{ color: '#64748b' }}>Status</span>
                <StatusBadge status={latestRun.status} dryRun={latestRun.dry_run} />
              </div>
              <div className="flex items-center justify-between gap-2">
                <span style={{ color: '#64748b' }}>Mode</span>
                <span style={{ color: '#94a3b8' }}>{latestRun.dry_run ? 'Dry Run' : 'Apply'}</span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span style={{ color: '#64748b' }}>Started</span>
                <span style={{ color: '#94a3b8' }}>{fmtDate(latestRun.started_at)}</span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span style={{ color: '#64748b' }}>Duration</span>
                <span style={{ color: '#94a3b8' }}>
                  {isRunning ? fmtSecs(liveElapsed) : fmtDuration(latestRun.started_at, latestRun.finished_at)}
                </span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span style={{ color: '#64748b' }}>Workers</span>
                <span style={{ color: '#94a3b8' }}>{latestRun.worker_count}</span>
              </div>
              <div className="flex items-center justify-between gap-2">
                <span style={{ color: '#64748b' }}>Batch Size</span>
                <span style={{ color: '#94a3b8' }}>{latestRun.batch_size?.toLocaleString()}</span>
              </div>
              <div className="flex items-center justify-between gap-2 col-span-2">
                <span style={{ color: '#64748b' }}>Selected Fields</span>
                <div className="flex flex-wrap gap-1 justify-end">
                  {safeArray(latestRun.selected_fields).map(f => (
                    <FieldPill key={f} fieldName={f} />
                  ))}
                </div>
              </div>
            </div>
          </Panel>
        )
      })()}

      {/* Progress cards */}
      <div className="grid grid-cols-2 md:grid-cols-4 gap-3">
        <MetricCard
          label="Pending Rows"
          value={latestRun?.rows_pending_after != null
            ? n(latestRun.rows_pending_after).toLocaleString()
            : '—'}
          note="better_tags_updated_at IS NULL after last run"
          color="#f59e0b"
          icon={Clock}
        />
        <MetricCard
          label="Changed Rows"
          value={latestRun ? n(latestRun.rows_changed).toLocaleString() : '—'}
          note={latestRun
            ? pct(latestRun.rows_changed, latestRun.rows_scanned) + ' of scanned'
            : 'last run'}
          color="#10b981"
          icon={CheckCircle}
        />
        <MetricCard
          label="Unresolved Labels"
          value={n(resolver.total_unresolved).toLocaleString()}
          note={`${n(resolver.pending)} pending · ${n(resolver.materialized)} materialized`}
          color="#ef4444"
          icon={AlertTriangle}
        />
        <MetricCard
          label="Resolver Status"
          value={n(resolver.map_to_existing + resolver.anomaly + resolver.promote).toLocaleString()}
          note={`Map: ${n(resolver.map_to_existing)} · Anomaly: ${n(resolver.anomaly)} · Promote: ${n(resolver.promote)}`}
          color="#a855f7"
          icon={Tag}
        />
      </div>

      {/* Field table */}
      {fieldRows.length > 0 && (
        <Panel
          id="field-stats"
          title="Field Statistics"
          subtitle={`Run: ${fields?.run_id || '—'}`}
          icon={Database}
          compact
        >
          <div className="overflow-x-auto">
            <table className="w-full border-collapse">
              <thead>
                <tr style={{ background: 'rgba(26,45,74,0.35)' }}>
                  <TH>Field</TH>
                  <TH>Scanned</TH>
                  <TH>Changed</TH>
                  <TH>Unchanged</TH>
                  <TH>Unmapped</TH>
                  <TH>Ambiguous</TH>
                  <TH>Errors</TH>
                  <TH>Changed %</TH>
                </tr>
              </thead>
              <tbody>
                {fieldRows.map(row => (
                  <tr key={row.field_name} className="hover:bg-white/[0.015] transition-colors">
                    <TD><FieldPill fieldName={row.field_name} /></TD>
                    <TD mono>{n(row.scanned).toLocaleString()}</TD>
                    <TD mono style={{ color: n(row.changed) > 0 ? '#10b981' : '#94a3b8' }}>
                      {n(row.changed).toLocaleString()}
                    </TD>
                    <TD mono>{n(row.unchanged).toLocaleString()}</TD>
                    <TD mono style={{ color: n(row.unmapped) > 0 ? '#f59e0b' : '#94a3b8' }}>
                      {n(row.unmapped).toLocaleString()}
                    </TD>
                    <TD mono style={{ color: n(row.ambiguous) > 0 ? '#f59e0b' : '#94a3b8' }}>
                      {n(row.ambiguous).toLocaleString()}
                    </TD>
                    <TD mono style={{ color: n(row.errors) > 0 ? '#ef4444' : '#94a3b8' }}>
                      {n(row.errors).toLocaleString()}
                    </TD>
                    <TD mono style={{ color: n(row.changed_pct) > 0 ? '#00d4ff' : '#64748b' }}>
                      {row.changed_pct != null ? `${n(row.changed_pct).toFixed(1)}%` : '—'}
                    </TD>
                  </tr>
                ))}
              </tbody>
            </table>
          </div>
        </Panel>
      )}

      {/* Unresolved labels */}
      <Panel
        id="unresolved"
        title="Unresolved Label Queue"
        subtitle={`${n(unresolved?.total).toLocaleString()} labels`}
        icon={AlertTriangle}
        compact
        action={
          <div className="flex items-center gap-2">
            <select
              value={fieldFilter}
              onChange={e => {
                setFieldFilter(e.target.value)
                fetchAll(0, 0, e.target.value, statusFilter)
              }}
              className="text-[11px] rounded px-2 py-1"
              style={{ background: 'rgba(26,45,74,0.8)', color: '#94a3b8', border: '1px solid rgba(26,45,74,0.9)' }}
            >
              <option value="">All fields</option>
              {fieldRows.map(f => (
                <option key={f.field_name} value={f.field_name}>{f.field_name}</option>
              ))}
            </select>
            <select
              value={statusFilter}
              onChange={e => {
                setStatusFilter(e.target.value)
                fetchAll(0, 0, fieldFilter, e.target.value)
              }}
              className="text-[11px] rounded px-2 py-1"
              style={{ background: 'rgba(26,45,74,0.8)', color: '#94a3b8', border: '1px solid rgba(26,45,74,0.9)' }}
            >
              {RESOLVER_STATUSES.map(s => (
                <option key={s} value={s}>{s === 'null' ? 'Not reviewed' : s || 'All statuses'}</option>
              ))}
            </select>
          </div>
        }
      >
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr style={{ background: 'rgba(26,45,74,0.35)' }}>
                <TH>Field</TH>
                <TH>Raw Label</TH>
                <TH>Normalized</TH>
                <TH>Occurrences</TH>
                <TH>Calls</TH>
                <TH>Resolver</TH>
                <TH>Target</TH>
                <TH>Sim</TH>
                <TH>Actor</TH>
                <TH>Contradiction</TH>
                <TH>First Seen</TH>
                <TH>Last Seen</TH>
              </tr>
            </thead>
            <tbody>
              {unresolvedRows.length === 0 && (
                <tr>
                  <td colSpan={12} className="px-4 py-8 text-center text-[12px]" style={{ color: '#64748b' }}>
                    No unresolved labels found.
                  </td>
                </tr>
              )}
              {unresolvedRows.map(row => (
                <tr key={row.id} className="hover:bg-white/[0.015] transition-colors">
                  <TD><FieldPill fieldName={row.field_name} /></TD>
                  <TD style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                    <span title={row.raw_label}>{row.raw_label}</span>
                  </TD>
                  <TD style={{ maxWidth: 200, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: '#64748b' }}>
                    <span title={row.normalized_label}>{row.normalized_label}</span>
                  </TD>
                  <TD mono style={{ color: '#00d4ff' }}>{n(row.occurrence_count).toLocaleString()}</TD>
                  <TD mono>{n(row.distinct_call_count).toLocaleString()}</TD>
                  <TD><ResolverBadge status={row.resolver_status} materialized={row.evidence_json?.materialized === true} /></TD>
                  <TD style={{ maxWidth: 160, overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: '#94a3b8' }}>
                    {row.target_display_name || <span style={{ color: '#475569' }}>—</span>}
                  </TD>
                  <TD mono style={{ color: row.similarity_score ? '#00d4ff' : '#475569' }}>
                    {row.similarity_score != null ? Number(row.similarity_score).toFixed(3) : '—'}
                  </TD>
                  <TD><GuardBadge value={row.actor_guard_status} label="Actor" /></TD>
                  <TD><GuardBadge value={row.contradiction_guard_status} label="Contra" /></TD>
                  <TD style={{ color: '#64748b' }}>{row.first_seen_at ? new Date(row.first_seen_at).toLocaleDateString() : '—'}</TD>
                  <TD style={{ color: '#64748b' }}>{row.last_seen_at  ? new Date(row.last_seen_at).toLocaleDateString()  : '—'}</TD>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination
            offset={unresolvedOffset}
            limit={UNRESOLVED_LIMIT}
            total={n(unresolved?.total)}
            onPage={off => {
              setUnresolvedOffset(off)
              fetchAll(runsOffset, off)
            }}
          />
        </div>
      </Panel>

      {/* Run history */}
      <Panel
        id="run-history"
        title="Run History"
        subtitle={`${n(runs?.total).toLocaleString()} runs`}
        icon={Clock}
        compact
      >
        <div className="overflow-x-auto">
          <table className="w-full border-collapse">
            <thead>
              <tr style={{ background: 'rgba(26,45,74,0.35)' }}>
                <TH>Run ID</TH>
                <TH>Status</TH>
                <TH>Started</TH>
                <TH>Duration</TH>
                <TH>Scanned</TH>
                <TH>Changed</TH>
                <TH>Errors</TH>
                <TH>Pending After</TH>
                <TH></TH>
              </tr>
            </thead>
            <tbody>
              {runRows.length === 0 && (
                <tr>
                  <td colSpan={9} className="px-4 py-8 text-center text-[12px]" style={{ color: '#64748b' }}>
                    No runs yet. Run the backfill script to see results here.
                  </td>
                </tr>
              )}
              {runRows.map(row => (
                <tr key={row.backfill_run_id} className="hover:bg-white/[0.015] transition-colors">
                  <TD mono style={{ fontSize: 10, color: '#64748b' }}>
                    {row.backfill_run_id}
                  </TD>
                  <TD><StatusBadge status={row.status} dryRun={row.dry_run} /></TD>
                  <TD style={{ color: '#64748b' }}>{fmtDate(row.started_at)}</TD>
                  <TD mono>{fmtDuration(row.started_at, row.finished_at)}</TD>
                  <TD mono>{n(row.rows_scanned).toLocaleString()}</TD>
                  <TD mono style={{ color: n(row.rows_changed) > 0 ? '#10b981' : '#94a3b8' }}>
                    {n(row.rows_changed).toLocaleString()}
                  </TD>
                  <TD mono style={{ color: n(row.rows_error) > 0 ? '#ef4444' : '#94a3b8' }}>
                    {n(row.rows_error).toLocaleString()}
                  </TD>
                  <TD mono style={{ color: '#f59e0b' }}>
                    {row.rows_pending_after != null ? n(row.rows_pending_after).toLocaleString() : '—'}
                  </TD>
                  <TD>
                    <button
                      onClick={() => setDrillRunId(row.backfill_run_id)}
                      className="flex items-center gap-1 px-2 py-0.5 rounded text-[10px] transition-all"
                      style={{ background: 'rgba(0,212,255,0.08)', color: '#00d4ff', border: '1px solid rgba(0,212,255,0.22)' }}
                    >
                      Audit
                      <ArrowRight size={10} />
                    </button>
                  </TD>
                </tr>
              ))}
            </tbody>
          </table>
          <Pagination
            offset={runsOffset}
            limit={RUNS_LIMIT}
            total={n(runs?.total)}
            onPage={off => {
              setRunsOffset(off)
              fetchAll(off, unresolvedOffset)
            }}
          />
        </div>
      </Panel>

      {/* Row audit drilldown */}
      {drillRunId && (
        <RowAuditPanel runId={drillRunId} onClose={() => setDrillRunId(null)} />
      )}
    </div>
  )
}
