import { createContext, useContext, useEffect } from 'react'
import useStore from '../store/useStore.js'

const AppContext = createContext(null)

export function useAppCtx() {
  const ctx = useContext(AppContext)
  if (!ctx) throw new Error('useAppCtx must be used inside AppProvider')
  return ctx
}

export function AppProvider({ children }) {
  const store = useStore()

  useEffect(() => {
    store.refreshAll()
  }, [])

  const value = {
    activePage:          store.activePage,
    navigate:            store.navigate,
    selectedClusterId:   store.selectedClusterId,
    setSelectedClusterId: store.setSelectedClusterId,
    searchOpen:          store.searchOpen,
    setSearchOpen:       store.setSearchOpen,
    fields:              store.fields,
    health:              store.health,
    refreshAll:          store.refreshAll,
  }

  return <AppContext.Provider value={value}>{children}</AppContext.Provider>
}
