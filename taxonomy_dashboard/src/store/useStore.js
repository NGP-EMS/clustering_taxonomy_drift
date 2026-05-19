import { create } from 'zustand'

const useStore = create((set, get) => ({
  // Navigation
  activePage: 'observatory',
  navigate: (page) => set({ activePage: page, selectedClusterId: null }),

  // Cluster selection
  selectedClusterId: null,
  setSelectedClusterId: (idOrUpdater) => set((s) => ({
    selectedClusterId: typeof idOrUpdater === 'function'
      ? idOrUpdater(s.selectedClusterId)
      : idOrUpdater,
  })),
  hoveredClusterId: null,
  setHoveredClusterId: (id) => set({ hoveredClusterId: id }),

  // Search
  searchOpen: false,
  setSearchOpen: (open) => set({ searchOpen: open }),

  // Global data
  fields: [],
  setFields: (fields) => set({ fields }),
  health: null,
  setHealth: (health) => set({ health }),

  // 3D scene state
  projectionMode: 'umap',
  setProjectionMode: (mode) => set({ projectionMode: mode }),
  showEdges: false,
  setShowEdges: (v) => set({ showEdges: v }),
  showLabels: false,
  setShowLabels: (v) => set({ showLabels: v }),
  anomalyFilter: 'all',
  setAnomalyFilter: (f) => set({ anomalyFilter: f }),
  activeFields: [],
  setActiveFields: (fields) => set({ activeFields: Array.isArray(fields) ? fields : [] }),
  activeField: null,
  setActiveField: (f) => set({ activeField: f, activeFields: f ? [f] : [] }),
  colorMode: 'field',
  setColorMode: (mode) => set({ colorMode: mode }),
  showKnn: false,
  setShowKnn: (v) => set({ showKnn: v }),
  showAxes: true,
  setShowAxes: (v) => set({ showAxes: v }),
  cameraReset: 0,
  triggerCameraReset: () => set((s) => ({ cameraReset: s.cameraReset + 1 })),

  // Scene clusters (loaded from API)
  sceneClusters: [],
  setSceneClusters: (clusters) => set({ sceneClusters: clusters }),

  // Methods
  refreshAll: async () => {
    try {
      const [hRes, fRes] = await Promise.all([
        fetch('/api/health'),
        fetch('/api/fields'),
      ])
      if (hRes.ok) set({ health: await hRes.json() })
      if (fRes.ok) set({ fields: await fRes.json() })
    } catch {}
  },
}))

export default useStore
