import { motion } from 'framer-motion'
import {
  Orbit, LayoutDashboard, Database, AlertTriangle, GitBranch,
  Search, Settings, Activity, Layers, ChevronRight,
} from 'lucide-react'
import useStore from '../../store/useStore.js'

const NAV = [
  {
    group: 'Observe',
    items: [
      { id: 'observatory', label: 'Observatory',   sub: 'Semantic space',    Icon: Orbit,           color: '#00d4ff' },
      { id: 'overview',    label: 'Intelligence',  sub: 'Compression & health', Icon: LayoutDashboard, color: '#a855f7' },
    ],
  },
  {
    group: 'Explore',
    items: [
      { id: 'clusters',    label: 'Cluster Explorer', sub: 'Browse & inspect', Icon: Database,    color: '#10b981' },
      { id: 'anomalies',   label: 'Anomalies',        sub: 'Outlier analysis', Icon: AlertTriangle, color: '#ef4444' },
      { id: 'drift',       label: 'Drift Monitor',    sub: 'Pattern changes',  Icon: GitBranch,   color: '#f97316' },
    ],
  },
]

function NavItem({ item, active, onClick }) {
  return (
    <motion.button
      whileHover={{ x: 2 }}
      transition={{ type: 'spring', stiffness: 400, damping: 30 }}
      onClick={() => onClick(item.id)}
      className={[
        'w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-left transition-all duration-150 group relative',
        active
          ? 'bg-obs-elevated text-star'
          : 'text-dust hover:text-nebula hover:bg-obs-surface',
      ].join(' ')}
    >
      {active && (
        <motion.div
          layoutId="active-nav"
          className="absolute inset-0 rounded-lg"
          style={{
            background: `linear-gradient(90deg, ${item.color}18 0%, transparent 100%)`,
            borderLeft: `2px solid ${item.color}`,
          }}
        />
      )}
      <div
        className="relative flex-shrink-0 w-7 h-7 flex items-center justify-center rounded-md transition-all duration-150"
        style={active
          ? { background: item.color + '22', color: item.color, boxShadow: `0 0 10px ${item.color}44` }
          : { background: 'rgba(255,255,255,0.04)', color: 'inherit' }
        }
      >
        <item.Icon size={14} />
      </div>
      <div className="relative flex-1 min-w-0">
        <div className={[
          'text-xs font-semibold tracking-wide truncate',
          active ? '' : 'group-hover:text-star',
        ].join(' ')}
          style={active ? { color: item.color } : {}}
        >
          {item.label}
        </div>
        <div className="text-[10px] text-dust truncate mt-0.5">{item.sub}</div>
      </div>
      {active && <ChevronRight size={11} style={{ color: item.color }} className="relative flex-shrink-0 opacity-60" />}
    </motion.button>
  )
}

export default function Sidebar() {
  const { activePage, navigate, health } = useStore()

  const anomalyCount = health?.anomaly_clusters || 0

  return (
    <aside
      className="flex flex-col flex-shrink-0 overflow-hidden"
      style={{
        width: 220,
        background: 'linear-gradient(180deg, #060d1a 0%, #03080f 100%)',
        borderRight: '1px solid rgba(26,45,74,0.8)',
      }}
    >
      {/* Brand */}
      <div className="flex items-center gap-3 px-4 py-5 border-b border-obs-border/60">
        <div
          className="flex-shrink-0 w-8 h-8 rounded-lg flex items-center justify-center"
          style={{
            background: 'linear-gradient(135deg, #00d4ff22 0%, #7c3aed22 100%)',
            border: '1px solid rgba(0,212,255,0.25)',
            boxShadow: '0 0 16px rgba(0,212,255,0.15)',
          }}
        >
          <Orbit size={15} style={{ color: '#00d4ff' }} />
        </div>
        <div>
          <div className="text-[13px] font-bold text-star tracking-tight">Semantic</div>
          <div className="text-[10px] text-dust uppercase tracking-widest">Observatory</div>
        </div>
      </div>

      {/* Search hint */}
      <div className="px-3 pt-3 pb-1">
        <button
          onClick={() => useStore.getState().setSearchOpen(true)}
          className="w-full flex items-center gap-2 px-3 py-2 rounded-lg text-dust text-xs transition-all duration-150 hover:text-nebula"
          style={{ background: 'rgba(255,255,255,0.03)', border: '1px solid rgba(26,45,74,0.6)' }}
        >
          <Search size={12} className="flex-shrink-0" />
          <span className="flex-1 text-left">Search clusters…</span>
          <kbd className="text-[10px] px-1.5 py-0.5 rounded" style={{ background: 'rgba(255,255,255,0.06)', color: '#475569' }}>⌘K</kbd>
        </button>
      </div>

      {/* Nav groups */}
      <nav className="flex-1 overflow-y-auto px-2 py-2 space-y-4">
        {NAV.map(group => (
          <div key={group.group}>
            <div className="px-3 pb-1.5 text-[9px] font-bold uppercase tracking-widest text-dust/60">
              {group.group}
            </div>
            <div className="space-y-0.5">
              {group.items.map(item => (
                <NavItem
                  key={item.id}
                  item={item}
                  active={activePage === item.id}
                  onClick={navigate}
                />
              ))}
            </div>
          </div>
        ))}
      </nav>

      {/* System health footer */}
      <div
        className="mx-3 mb-3 rounded-lg p-3"
        style={{ background: 'rgba(255,255,255,0.02)', border: '1px solid rgba(26,45,74,0.7)' }}
      >
        <div className="flex items-center justify-between mb-2">
          <span className="text-[9px] uppercase tracking-widest text-dust/60 font-bold">System</span>
          <span
            className="flex items-center gap-1 text-[10px] font-semibold"
            style={{ color: anomalyCount > 0 ? '#f97316' : '#10b981' }}
          >
            <span
              className="w-1.5 h-1.5 rounded-full"
              style={{
                background: anomalyCount > 0 ? '#f97316' : '#10b981',
                boxShadow: anomalyCount > 0 ? '0 0 6px #f97316' : '0 0 6px #10b981',
              }}
            />
            {anomalyCount > 0 ? `${anomalyCount} anomalies` : 'Nominal'}
          </span>
        </div>
        {health && (
          <div className="grid grid-cols-2 gap-1.5">
            <div className="text-center">
              <div className="text-[13px] font-bold text-cyan">{(health.total_clusters || 0).toLocaleString()}</div>
              <div className="text-[9px] text-dust">clusters</div>
            </div>
            <div className="text-center">
              <div className="text-[13px] font-bold text-violet-bright">{health.fields_count || '—'}</div>
              <div className="text-[9px] text-dust">fields</div>
            </div>
          </div>
        )}
        {!health && (
          <div className="text-center text-[10px] text-dust">connecting…</div>
        )}
      </div>
    </aside>
  )
}
