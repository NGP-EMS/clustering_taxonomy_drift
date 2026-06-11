import { motion } from 'framer-motion'
import { Orbit, LayoutDashboard, ChevronRight, ArrowDownToLine } from 'lucide-react'
import useStore from '../../store/useStore.js'

const NAV = [
  { id: 'observatory', label: 'Observatory',  sub: 'Explore clusters', Icon: Orbit,           color: '#00d4ff' },
  { id: 'overview',    label: 'Analysis',     sub: 'Insights',          Icon: LayoutDashboard, color: '#a855f7' },
  { id: 'backfill',    label: 'Backfill',     sub: 'Monitor runs',      Icon: ArrowDownToLine, color: '#10b981' },
]

function NavItem({ item, active, onClick }) {
  return (
    <motion.button
      whileHover={{ x: 2 }}
      transition={{ type: 'spring', stiffness: 400, damping: 30 }}
      onClick={() => onClick(item.id)}
      className={[
        'w-full flex items-center gap-3 px-3 py-2.5 rounded-lg text-left transition-all duration-150 group relative',
        active ? 'bg-obs-elevated text-star' : 'text-dust hover:text-nebula hover:bg-obs-surface',
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
        <div className="text-xs font-semibold tracking-wide truncate" style={active ? { color: item.color } : {}}>{item.label}</div>
        <div className="text-[10px] text-dust truncate mt-0.5">{item.sub}</div>
      </div>
      {active && <ChevronRight size={11} style={{ color: item.color }} className="relative flex-shrink-0 opacity-60" />}
    </motion.button>
  )
}

export default function Sidebar() {
  const { activePage, navigate } = useStore()

  return (
    <aside
      className="flex flex-col flex-shrink-0 overflow-hidden"
      style={{
        width: 'clamp(168px, 11vw, 196px)',
        background: 'linear-gradient(180deg, #060d1a 0%, #03080f 100%)',
        borderRight: '1px solid rgba(26,45,74,0.8)',
      }}
    >
      <nav className="flex-1 overflow-y-auto px-2 py-3 space-y-1.5">
        {NAV.map(item => (
          <NavItem
            key={item.id}
            item={item}
            active={activePage === item.id}
            onClick={navigate}
          />
        ))}
      </nav>
    </aside>
  )
}
