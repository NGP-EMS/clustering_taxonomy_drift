// Composite key for globally unique cluster identity.
// cluster_id (string like "strict_125") is only unique within a field — the same string
// can appear in multiple fields. c.id is the integer database primary key (globally unique)
// and is required for the /api/cluster/:id endpoint which uses parseInt.
export function makeClusterKey(c) {
  const field = c?.field_name || ''
  const version = c?.cluster_version || c?.run_id || c?.cluster_run_id || ''
  // Prefer the integer primary key (c.id) so the parsed key remains valid for API calls.
  // Fall back to cluster_id only when id is absent (e.g. raw semantic-search API results).
  const id = c?.id ?? c?.cluster_id ?? ''
  return `${field}::${version}::${id}`
}

// Parse a composite key back to its parts. Handles both composite and legacy raw-id keys.
export function parseClusterKey(key) {
  if (!key || typeof key !== 'string') return { field_name: '', cluster_version: '', cluster_id: key || '' }
  if (!key.includes('::')) return { field_name: '', cluster_version: '', cluster_id: key }
  const parts = key.split('::')
  return {
    field_name: parts[0] || '',
    cluster_version: parts[1] || '',
    cluster_id: parts.slice(2).join('::') || '',
  }
}
