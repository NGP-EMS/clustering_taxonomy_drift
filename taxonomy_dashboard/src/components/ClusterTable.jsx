import { useState, useRef, useLayoutEffect, useMemo, useCallback } from 'react'
import { ChevronUp, ChevronDown, ChevronsUpDown } from 'lucide-react'
import { useAppCtx } from '../context/AppContext.jsx'
import { fmt } from '../utils/format.js'
import { getFieldColor } from '../utils/colors.js'

const ROW_H   = 40
const OVERSCAN = 8

const COLUMNS = [
  { key: 'field_name',              label: 'Field',       sortable: true,  width: 126 },
  { key: 'display_name',            label: 'Name',        sortable: true,  width: 230 },
  { key: 'semantic_search_score',   label: 'Score',       sortable: true,  width: 76,  align: 'right' },
  { key: 'semantic_best_label',     label: 'Semantic Match', sortable: false, width: 190 },
  { key: 'semantic_distinct_calls', label: 'Calls',       sortable: true,  width: 72,  align: 'right' },
  { key: 'cluster_size',            label: 'Size',        sortable: true,  width: 78,  align: 'right' },
  { key: 'total_occurrences',       label: 'Occ.',        sortable: true,  width: 86,  align: 'right' },
  { key: 'label_count',             label: 'Labels',      sortable: true,  width: 76,  align: 'right' },
  { key: 'medoid_label',            label: 'Medoid',      sortable: false, width: 190 },
  { key: 'is_true_anomaly_cluster', label: 'Type',        sortable: true,  width: 98  },
  { key: 'naming_method',           label: 'Method',      sortable: false, width: 148 },
  { key: 'cluster_id',              label: 'ID',          sortable: true,  width: 136, mono: true },
]

const TABLE_W = COLUMNS.reduce((sum, c) => sum + c.width, 0)
const STICKY_LEFTS = COLUMNS.reduce((acc, col, idx) => {
  acc[idx] = idx === 0 ? 0 : acc[idx - 1] + COLUMNS[idx - 1].width
  return acc
}, {})

function SortIcon({ col, sortKey, sortDir }) {
  if (sortKey !== col) return <ChevronsUpDown size={10} className="sort-icon-dim" />
  return sortDir === 'asc' ? <ChevronUp size={10} /> : <ChevronDown size={10} />
}

function TypeBadge({ isAnomaly }) {
  if (isAnomaly === null || isAnomaly === undefined)
    return <span className="badge-unknown">—</span>
  return isAnomaly
    ? <span className="badge-anomaly">Anomaly</span>
    : <span className="badge-standard">Standard</span>
}

function MethodChip({ method }) {
  if (!method) return <span className="cell-method">—</span>
  return <span className="method-chip" title={method}>{method}</span>
}

function columnStyle(col, idx) {
  const style = {
    width: `${col.width}px`,
    minWidth: `${col.width}px`,
    maxWidth: `${col.width}px`,
  }
  if (col.sticky) style.left = `${STICKY_LEFTS[idx]}px`
  return style
}

function columnClass(col, idx, extra = '') {
  return [
    extra,
    col.sticky && 'vtable-sticky-col',
    col.sticky && idx === 1 && 'vtable-sticky-col-last',
  ].filter(Boolean).join(' ')
}

function getClusterName(row) {
  return row.display_name || row.medoid_label || row.representative_label || row.cluster_id || 'unnamed'
}

function semanticScore(row) {
  const score = Number(row.semantic_search_score)
  return Number.isFinite(score) ? `${Math.round(score * 100)}%` : '—'
}

function VirtualBody({ rows, onRowClick, selectedId }) {
  const containerRef  = useRef(null)
  const [scrollTop,   setScrollTop]   = useState(0)
  const [viewHeight,  setViewHeight]  = useState(600)

  useLayoutEffect(() => {
    if (!containerRef.current) return
    const ro = new ResizeObserver(([e]) => setViewHeight(e.contentRect.height))
    ro.observe(containerRef.current)
    setViewHeight(containerRef.current.clientHeight)
    return () => ro.disconnect()
  }, [])

  const total   = rows.length
  const start   = Math.max(0, Math.floor(scrollTop / ROW_H) - OVERSCAN)
  const end     = Math.min(total, Math.ceil((scrollTop + viewHeight) / ROW_H) + OVERSCAN)
  const padTop  = start * ROW_H
  const padBot  = Math.max(0, (total - end) * ROW_H)
  const visible = rows.slice(start, end)

  return (
    <div
      ref={containerRef}
      className="vtable-body"
      onScroll={e => setScrollTop(e.currentTarget.scrollTop)}
    >
      <table className="vtable-inner" style={{ width: TABLE_W, minWidth: TABLE_W }}>
        <colgroup>
          {COLUMNS.map(c => <col key={c.key} style={{ width: `${c.width}px` }} />)}
        </colgroup>
        <tbody>
          {padTop > 0 && <tr style={{ height: padTop }}><td colSpan={COLUMNS.length} /></tr>}
          {visible.map((row, idx) => {
            const fc = getFieldColor(row.field_name)
            const clusterName = getClusterName(row)
            return (
              <tr
                key={row.id ?? `${row.field_name}-${row.cluster_id}-${start + idx}`}
                className={[
                  'vtable-row',
                  row.is_true_anomaly_cluster && 'row-anomaly',
                  String(selectedId) === String(row.id) && 'row-selected',
                ].filter(Boolean).join(' ')}
                onClick={() => onRowClick(row)}
              >
                <td className={columnClass(COLUMNS[0], 0)} style={columnStyle(COLUMNS[0], 0)}>
                  <span
                    className="tag-field"
                    title={row.field_name || 'Unknown field'}
                    style={{ background: fc + '18', color: fc, border: `1px solid ${fc}30` }}
                  >
                    {row.field_name || 'unknown'}
                  </span>
                </td>
                <td
                  className={columnClass(COLUMNS[1], 1, 'cell-name')}
                  style={columnStyle(COLUMNS[1], 1)}
                  title={clusterName}
                >
                  {row.display_name ? clusterName : <span className="unnamed">{clusterName}</span>}
                </td>
                <td className="cell-num" title={row.semantic_search_score ? String(row.semantic_search_score) : ''}>{semanticScore(row)}</td>
                <td className="cell-medoid" title={row.semantic_best_label || ''}>
                  {row.semantic_best_label || '—'}
                </td>
                <td className="cell-num" title={Array.isArray(row.sample_call_ids) && row.sample_call_ids.length ? `Sample call IDs: ${row.sample_call_ids.join(', ')}` : ''}>{fmt(row.semantic_distinct_calls)}</td>
                <td className="cell-num">{fmt(row.cluster_size)}</td>
                <td className="cell-num">{fmt(row.total_occurrences)}</td>
                <td className="cell-num">{fmt(row.label_count)}</td>
                <td className="cell-medoid" title={row.medoid_label || ''}>
                  {row.medoid_label || '—'}
                </td>
                <td><TypeBadge isAnomaly={row.is_true_anomaly_cluster} /></td>
                <td><MethodChip method={row.naming_method} /></td>
                <td className="cell-mono" title={row.cluster_id}>{row.cluster_id}</td>
              </tr>
            )
          })}
          {padBot > 0 && <tr style={{ height: padBot }}><td colSpan={COLUMNS.length} /></tr>}
        </tbody>
      </table>
    </div>
  )
}

export default function ClusterTable({ clusters, loading, error }) {
  const { selectedClusterId, setSelectedClusterId } = useAppCtx()
  const [sortKey, setSortKey] = useState('cluster_size')
  const [sortDir, setSortDir] = useState('desc')

  const sorted = useMemo(() => {
    if (!clusters?.length) return []
    return [...clusters].sort((a, b) => {
      const va = a[sortKey] ?? ''
      const vb = b[sortKey] ?? ''
      if (va === vb) return 0
      const cmp = typeof va === 'number' ? va - vb : String(va).localeCompare(String(vb))
      return sortDir === 'asc' ? cmp : -cmp
    })
  }, [clusters, sortKey, sortDir])

  const handleSort = useCallback((key) => {
    setSortKey(prev => {
      if (prev === key) { setSortDir(d => d === 'asc' ? 'desc' : 'asc'); return key }
      setSortDir('desc')
      return key
    })
  }, [])

  const handleRowClick = useCallback((row) => {
    setSelectedClusterId(prev => String(prev) === String(row.id) ? null : row.id)
  }, [setSelectedClusterId])

  if (error)   return <div className="state-error">{error}</div>
  if (loading) return <div className="state-loading"><span className="loading-dots" />Loading clusters…</div>
  if (!sorted.length) return <div className="state-empty">No clusters match the current filters.</div>

  return (
    <div className="vtable-wrap">
      <div className="vtable-head-row">
        <span className="vtable-count">{clusters.length.toLocaleString()} clusters</span>
      </div>
      <div className="vtable-container">
        <div className="vtable-scroll-x">
          <div className="vtable-content" style={{ width: TABLE_W, minWidth: TABLE_W }}>
            <table className="vtable-header-table" style={{ width: TABLE_W, minWidth: TABLE_W }}>
              <colgroup>
                {COLUMNS.map(c => <col key={c.key} style={{ width: `${c.width}px` }} />)}
              </colgroup>
              <thead>
                <tr>
                  {COLUMNS.map((col, idx) => (
                    <th
                      key={col.key}
                      style={columnStyle(col, idx)}
                      className={[
                        columnClass(col, idx),
                        col.align === 'right' && 'th-right',
                        col.sortable          && 'th-sortable',
                        sortKey === col.key   && 'th-sorted',
                      ].filter(Boolean).join(' ')}
                      onClick={() => col.sortable && handleSort(col.key)}
                    >
                      <span className="th-label">{col.label}</span>
                      {col.sortable && (
                        <SortIcon col={col.key} sortKey={sortKey} sortDir={sortDir} />
                      )}
                    </th>
                  ))}
                </tr>
              </thead>
            </table>

            <VirtualBody
              rows={sorted}
              onRowClick={handleRowClick}
              selectedId={selectedClusterId}
            />
          </div>
        </div>
      </div>
    </div>
  )
}
