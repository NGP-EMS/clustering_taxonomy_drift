import { RefreshCw, Search } from 'lucide-react'

export default function Filters({ filters, fields, onChange, onRefresh, anomalyMode, compact }) {
  return (
    <div className={['filters-bar', compact && 'filters-bar--compact'].filter(Boolean).join(' ')}>
      <div className="filters-row">

        <div className="filter-search-wrap">
          <Search size={12} className="filter-search-icon" />
          <input
            className="filter-input filter-input--search"
            type="text"
            placeholder="Search ID, name, label…"
            value={filters.search}
            onChange={e => onChange({ search: e.target.value })}
          />
        </div>

        <select
          className="filter-select"
          value={filters.field_name}
          onChange={e => onChange({ field_name: e.target.value })}
        >
          <option value="">All Fields</option>
          {fields.map(f => <option key={f} value={f}>{f}</option>)}
        </select>

        {!anomalyMode && (
          <>
            <select
              className="filter-select"
              value={filters.anomaly}
              onChange={e => onChange({ anomaly: e.target.value })}
            >
              <option value="">All Types</option>
              <option value="standard">Standard</option>
              <option value="anomaly">Anomaly</option>
            </select>

            <select
              className="filter-select"
              value={filters.named}
              onChange={e => onChange({ named: e.target.value })}
            >
              <option value="">Named &amp; Unnamed</option>
              <option value="named">Named only</option>
              <option value="unnamed">Unnamed only</option>
            </select>
          </>
        )}

        <input
          className="filter-input filter-select--xs"
          type="number"
          min="1"
          placeholder="Min size"
          value={filters.min_size || 1}
          onChange={e => onChange({ min_size: Math.max(1, parseInt(e.target.value, 10) || 1) })}
          title="Minimum cluster size"
        />

        <select
          className="filter-select filter-select--xs"
          value={filters.limit}
          onChange={e => onChange({ limit: parseInt(e.target.value, 10) })}
        >
          {[50, 100, 250, 500].map(n => <option key={n} value={n}>{n} rows</option>)}
        </select>

        <button className="btn-icon-text" onClick={onRefresh}>
          <RefreshCw size={12} />
          Refresh
        </button>
      </div>
    </div>
  )
}
