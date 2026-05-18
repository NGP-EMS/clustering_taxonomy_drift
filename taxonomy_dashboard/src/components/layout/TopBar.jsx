import { Search, RefreshCw, SlidersHorizontal } from 'lucide-react'
import useStore from '../../store/useStore.js'

const PAGE_LABELS = {
  overview:  { title: 'Semantic Intelligence', sub: 'Compression, recovery & quality signals' },
  clusters:  { title: 'Cluster Explorer',      sub: 'Browse, search and inspect all clusters' },
  anomalies: { title: 'Anomaly Intelligence',  sub: 'Outlier analysis and recovery potential' },
  drift:     { title: 'Drift Monitor',         sub: 'Pattern changes and emerging signals' },
}

export default function TopBar() {
  const { activePage, setSearchOpen, refreshAll } = useStore()
  const meta = PAGE_LABELS[activePage] || { title: activePage, sub: '' }

  return (
    <header
      className="flex-shrink-0 flex items-center justify-between px-5 py-3 gap-4"
      style={{
        background: 'rgba(6,13,26,0.95)',
        borderBottom: '1px solid rgba(26,45,74,0.7)',
        backdropFilter: 'blur(12px)',
      }}
    >
      <div>
        <h1 className="text-sm font-bold text-star tracking-tight">{meta.title}</h1>
        <p className="text-[11px] text-dust mt-0.5">{meta.sub}</p>
      </div>

      <div className="flex items-center gap-2">
        <button
          onClick={() => setSearchOpen(true)}
          className="flex items-center gap-2 px-3 py-1.5 rounded-lg text-dust text-xs transition-all duration-150 hover:text-nebula"
          style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(26,45,74,0.7)' }}
        >
          <Search size={12} />
          <span>Search</span>
          <kbd className="text-[10px] px-1 rounded" style={{ background: 'rgba(255,255,255,0.06)', color: '#475569' }}>⌘K</kbd>
        </button>
        <button
          onClick={() => refreshAll()}
          className="flex items-center gap-1.5 px-3 py-1.5 rounded-lg text-dust text-xs transition-all duration-150 hover:text-nebula"
          style={{ background: 'rgba(255,255,255,0.04)', border: '1px solid rgba(26,45,74,0.7)' }}
        >
          <RefreshCw size={12} />
          Refresh
        </button>
      </div>
    </header>
  )
}
