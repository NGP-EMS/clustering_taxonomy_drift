import { lazy, Suspense } from 'react'
import { AnimatePresence } from 'framer-motion'
import { AppProvider, useAppCtx } from './context/AppContext.jsx'
import Sidebar from './components/layout/Sidebar.jsx'
import TopBar from './components/layout/TopBar.jsx'
import RightInspector from './components/layout/RightInspector.jsx'
import GlobalSearch from './components/GlobalSearch.jsx'

const Observatory      = lazy(() => import('./pages/Observatory.jsx'))
const OverviewPage     = lazy(() => import('./pages/OverviewPage.jsx'))
const BackfillMonitor  = lazy(() => import('./pages/BackfillMonitor.jsx'))

function PageSuspense({ children }) {
  return (
    <Suspense fallback={
      <div className="flex items-center justify-center h-full">
        <div className="flex flex-col items-center gap-3">
          <div className="w-8 h-8 rounded-full border-2 border-cyan/20 border-t-cyan animate-spin" />
          <span className="text-dust text-xs tracking-widest uppercase">Loading…</span>
        </div>
      </div>
    }>
      {children}
    </Suspense>
  )
}

function AppShell() {
  const { activePage, selectedClusterId, searchOpen } = useAppCtx()
  const isObservatory = activePage === 'observatory'

  return (
    <div className="flex h-screen w-screen overflow-hidden bg-obs-void">
      {!isObservatory && <Sidebar />}

      <div className="flex flex-col flex-1 overflow-hidden min-w-0">
        {!isObservatory && <TopBar />}

        <div className="flex flex-1 overflow-hidden min-w-0">
          <main className={[
            'flex-1 overflow-y-auto overflow-x-hidden min-w-0',
            isObservatory ? 'overflow-hidden' : '',
          ].join(' ')}>
            <PageSuspense>
              {activePage === 'observatory' && <Observatory />}
              {activePage === 'overview' && <OverviewPage />}
              {activePage === 'backfill' && <BackfillMonitor />}
            </PageSuspense>
          </main>

          <AnimatePresence>
            {selectedClusterId && !isObservatory && (
              <RightInspector key="inspector" clusterId={selectedClusterId} />
            )}
          </AnimatePresence>
        </div>
      </div>

      <AnimatePresence>
        {searchOpen && <GlobalSearch key="search" />}
      </AnimatePresence>
    </div>
  )
}

export default function App() {
  return (
    <AppProvider>
      <AppShell />
    </AppProvider>
  )
}
