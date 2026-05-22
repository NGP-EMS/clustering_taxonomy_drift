export function getApiBaseUrl() {
  const explicit = String(import.meta.env.VITE_API_BASE_URL || '').trim()
  if (explicit) return explicit.replace(/\/+$/, '')

  if (typeof window === 'undefined') return ''
  const { protocol, hostname, port } = window.location
  const localHost = hostname === 'localhost' || hostname === '127.0.0.1' || hostname === '0.0.0.0'

  // Vite dev/preview serves the UI on 5173/4173. The Express API runs on 5050.
  // Using an explicit local API URL prevents Vite preview from returning index.html for /api/*.
  if (localHost && port && port !== '5050') return `${protocol}//${hostname}:5050`

  return ''
}

export function apiUrl(path) {
  const value = String(path || '')
  if (/^https?:\/\//i.test(value)) return value
  const normalized = value.startsWith('/') ? value : `/${value}`
  return `${getApiBaseUrl()}${normalized}`
}

export async function readJsonResponse(res, label = 'API request') {
  const contentType = res.headers.get('content-type') || ''
  const text = await res.text()

  if (!contentType.toLowerCase().includes('application/json')) {
    const sample = text.slice(0, 120).replace(/\s+/g, ' ').trim()
    const apiBase = getApiBaseUrl() || window.location.origin
    throw new Error(`${label} returned non-JSON from ${apiBase}. Make sure npm run server is running. Response started with: ${sample || 'empty response'}`)
  }

  let payload
  try {
    payload = text ? JSON.parse(text) : null
  } catch (err) {
    throw new Error(`${label} returned invalid JSON: ${err.message}`)
  }

  if (!res.ok) {
    throw new Error(payload?.error || payload?.message || `${label} failed with HTTP ${res.status}`)
  }

  return payload
}

export async function fetchJson(path, options = {}, label = 'API request') {
  const res = await fetch(apiUrl(path), options)
  return readJsonResponse(res, label)
}

export function installApiFetchPatch() {
  if (typeof window === 'undefined') return
  if (window.__taxonomyApiFetchPatched) return

  const nativeFetch = window.fetch.bind(window)
  window.fetch = (input, init) => {
    if (typeof input === 'string' && input.startsWith('/api/')) {
      return nativeFetch(apiUrl(input), init)
    }

    if (input instanceof Request) {
      const url = new URL(input.url)
      const sameOrigin = url.origin === window.location.origin
      if (sameOrigin && url.pathname.startsWith('/api/')) {
        return nativeFetch(apiUrl(`${url.pathname}${url.search}`), init || input)
      }
    }

    return nativeFetch(input, init)
  }

  window.__taxonomyApiFetchPatched = true
}
