import { useState, useEffect, useRef, useCallback } from 'react'
import { motion } from 'framer-motion'
import { Search, X, ArrowRight, AlertTriangle } from 'lucide-react'
import { useAppCtx } from '../context/AppContext.jsx'
import { useDebounce } from '../hooks/useDebounce.js'
import { truncate } from '../utils/format.js'
import { makeClusterKey } from '../utils/clusterKey.js'

export default function GlobalSearch() {
  const { setSearchOpen, setSelectedClusterId, navigate, fields } = useAppCtx()
  const [query,    setQuery]    = useState('')
  const [results,  setResults]  = useState([])
  const [loading,  setLoading]  = useState(false)
  const [cursor,   setCursor]   = useState(-1)
  const [fieldFilter, setFieldFilter] = useState('')
  const inputRef   = useRef(null)
  const listRef    = useRef(null)
  const debounced  = useDebounce(query, 200)

  useEffect(() => { inputRef.current?.focus() }, [])

  // Fetch search results
  useEffect(() => {
    if (!debounced.trim()) { setResults([]); return }
    const ctl = new AbortController()
    setLoading(true)
    const params = new URLSearchParams({ search: debounced, limit: 12 })
    if (fieldFilter) params.set('field_name', fieldFilter)
    fetch(`/api/clusters?${params}`, { signal: ctl.signal })
      .then(r => r.json())
      .then(d => { setResults(Array.isArray(d) ? d : []); setCursor(-1) })
      .catch(() => {})
      .finally(() => setLoading(false))
    return () => ctl.abort()
  }, [debounced, fieldFilter])

  const close = useCallback(() => {
    setSearchOpen(false)
  }, [setSearchOpen])

  function selectResult(row) {
    navigate('observatory')
    setSelectedClusterId(makeClusterKey(row) || row.id)
    close()
  }

  function onKeyDown(e) {
    if (e.key === 'Escape') { close(); return }
    if (e.key === 'ArrowDown') { e.preventDefault(); setCursor(c => Math.min(c + 1, results.length - 1)) }
    if (e.key === 'ArrowUp')   { e.preventDefault(); setCursor(c => Math.max(c - 1, -1)) }
    if (e.key === 'Enter' && cursor >= 0 && results[cursor]) { selectResult(results[cursor]) }
  }

  // Scroll selected item into view
  useEffect(() => {
    if (cursor >= 0 && listRef.current) {
      const el = listRef.current.children[cursor]
      el?.scrollIntoView({ block: 'nearest' })
    }
  }, [cursor])

  return (
    <>
      {/* Backdrop */}
      <motion.div
        className="search-backdrop"
        initial={{ opacity: 0 }} animate={{ opacity: 1 }} exit={{ opacity: 0 }}
        onClick={close}
      />

      {/* Modal */}
      <motion.div
        className="search-modal"
        initial={{ opacity: 0, scale: 0.96, y: -16 }}
        animate={{ opacity: 1, scale: 1, y: 0 }}
        exit={{ opacity: 0, scale: 0.96, y: -16 }}
        transition={{ duration: 0.15 }}
        onKeyDown={onKeyDown}
      >
        {/* Input row */}
        <div className="search-input-row">
          <Search size={16} className="search-icon" />
          <input
            ref={inputRef}
            className="search-input"
            type="text"
            placeholder="Search clusters, names, labels…"
            value={query}
            onChange={e => setQuery(e.target.value)}
          />
          {query && (
            <button className="search-clear" onClick={() => setQuery('')}>
              <X size={14} />
            </button>
          )}
        </div>

        {/* Field filter pills */}
        <div className="search-field-pills">
          <button
            className={['search-pill', !fieldFilter && 'active'].filter(Boolean).join(' ')}
            onClick={() => setFieldFilter('')}
          >All</button>
          {fields.slice(0, 8).map(f => (
            <button
              key={f}
              className={['search-pill', fieldFilter === f && 'active'].filter(Boolean).join(' ')}
              onClick={() => setFieldFilter(fieldFilter === f ? '' : f)}
            >{f}</button>
          ))}
        </div>

        {/* Results */}
        <div className="search-results" ref={listRef}>
          {loading && <div className="search-hint">Searching…</div>}
          {!loading && query && results.length === 0 && (
            <div className="search-hint">No clusters found for "{truncate(query, 30)}"</div>
          )}
          {!query && (
            <div className="search-hint">Type to search cluster IDs, display names, or labels</div>
          )}
          {results.map((row, i) => (
            <button
              key={row.id || i}
              className={['search-result', cursor === i && 'hovered'].filter(Boolean).join(' ')}
              onClick={() => selectResult(row)}
              onMouseEnter={() => setCursor(i)}
            >
              <span className="sr-field">{row.field_name}</span>
              <span className="sr-name">
                {row.display_name || <span className="sr-unnamed">unnamed</span>}
              </span>
              {row.is_true_anomaly_cluster && <AlertTriangle size={12} className="sr-anomaly-icon" />}
              <span className="sr-id">{row.cluster_id}</span>
              {row.cluster_size && <span className="sr-size">{row.cluster_size}</span>}
              <ArrowRight size={12} className="sr-arrow" />
            </button>
          ))}
        </div>

        <div className="search-footer">
          <kbd>↑↓</kbd> navigate &nbsp;·&nbsp; <kbd>↵</kbd> open &nbsp;·&nbsp; <kbd>Esc</kbd> close
        </div>
      </motion.div>
    </>
  )
}
