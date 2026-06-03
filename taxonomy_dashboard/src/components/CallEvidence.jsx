import { useState, useCallback, useEffect } from 'react'
import { ChevronDown, ChevronRight, Phone } from 'lucide-react'

function formatCallDate(d) {
  if (!d) return null
  const date = new Date(d)
  if (isNaN(date.getTime())) return null
  return date.toLocaleDateString(undefined, { year: 'numeric', month: 'short', day: 'numeric' })
}

function CallRow({ call }) {
  const [expanded, setExpanded] = useState(false)
  const date = formatCallDate(call.call_date || call.created_at)

  return (
    <div style={{ background: 'rgba(3,8,15,0.65)', border: '1px solid rgba(71,85,105,0.45)', borderRadius: 6, overflow: 'hidden' }}>
      <button
        className="w-full text-left"
        style={{ padding: '7px 10px', display: 'flex', alignItems: 'flex-start', gap: 6 }}
        onClick={() => setExpanded(e => !e)}
      >
        <span style={{ color: '#64748b', flexShrink: 0, marginTop: 2 }}>
          {expanded ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            {date ? (
              <span style={{ fontSize: 9, fontFamily: 'monospace', color: '#94a3b8' }}>{date}</span>
            ) : (
              <span style={{ fontSize: 9, fontFamily: 'monospace', color: '#475569' }}>No date</span>
            )}
            {call.agent_name && (
              <span style={{ fontSize: 9, color: '#94a3b8' }}>{call.agent_name}</span>
            )}
          </div>
          {call.summary ? (
            <div style={{
              fontSize: 9.5,
              color: '#94a3b8',
              marginTop: 3,
              lineHeight: 1.4,
              display: '-webkit-box',
              WebkitLineClamp: 2,
              WebkitBoxOrient: 'vertical',
              overflow: 'hidden',
              wordBreak: 'break-word',
            }}>
              {call.summary}
            </div>
          ) : (
            <div style={{ fontSize: 9, color: '#64748b', marginTop: 3, fontStyle: 'italic' }}>No summary available</div>
          )}
        </div>
      </button>

      {expanded && (
        <div style={{ borderTop: '1px solid rgba(71,85,105,0.30)', padding: '8px 10px', display: 'flex', flexDirection: 'column', gap: 8 }}>
          {call.call_id && (
            <div style={{ fontSize: 8, fontFamily: 'monospace', color: '#64748b', wordBreak: 'break-all' }}>
              ID: {call.call_id}
            </div>
          )}
          {call.summary && (
            <div>
              <div style={{ fontSize: 8, textTransform: 'uppercase', letterSpacing: '0.12em', color: '#64748b', marginBottom: 4 }}>
                Summary
              </div>
              <div style={{ fontSize: 9.5, color: '#94a3b8', lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                {call.summary}
              </div>
            </div>
          )}
          {call.transcript_preview && (
            <div>
              <div style={{ fontSize: 8, textTransform: 'uppercase', letterSpacing: '0.12em', color: '#64748b', marginBottom: 4 }}>
                Transcript{call.full_transcript_available ? ' (preview)' : ''}
              </div>
              <div style={{ fontSize: 9, color: '#94a3b8', lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'monospace' }}>
                {call.transcript_preview}{call.full_transcript_available ? '…' : ''}
              </div>
            </div>
          )}
          {!call.summary && !call.transcript_preview && (
            <div style={{ fontSize: 9, color: '#64748b', fontStyle: 'italic' }}>
              No call content available for this record.
            </div>
          )}
        </div>
      )}
    </div>
  )
}

export default function CallEvidence({ fieldName, rawLabel }) {
  const [open,    setOpen]    = useState(false)
  const [loading, setLoading] = useState(false)
  const [data,    setData]    = useState(null)
  const [error,   setError]   = useState(null)

  // Reset when the label changes so stale results from a previous label don't show
  useEffect(() => {
    setOpen(false)
    setLoading(false)
    setData(null)
    setError(null)
  }, [fieldName, rawLabel])

  const toggle = useCallback(() => {
    if (open) { setOpen(false); setError(null); return }
    // Re-use cached successful result; always retry after an error
    if (data && !error) { setOpen(true); return }
    setOpen(true)
    setLoading(true)
    setError(null)
    setData(null)

    const controller = new AbortController()
    const timeoutId  = setTimeout(() => controller.abort(), 24000)

    const params = new URLSearchParams({ field_name: fieldName, raw_label: rawLabel, limit: '25' })
    fetch(`/api/calls/by-label?${params}`, { signal: controller.signal })
      .then(r => {
        if (r.status === 404) throw new Error('Endpoint not found — restart the taxonomy server.')
        if (r.status === 503) return r.json().then(d => { throw new Error(d.error || 'Cannot reach the calls database. Check VPN.') })
        if (r.status === 422) return r.json().then(d => { throw new Error(d.error || 'This field needs a GIN index.') })
        const ct = r.headers.get('content-type') || ''
        if (!ct.includes('application/json')) throw new Error(`Unexpected server response (${r.status}). Ensure the taxonomy server is running.`)
        return r.json()
      })
      .then(d => {
        setLoading(false)
        if (d.error) { setError(d.error); setData(null) }
        else         { setData(d);        setError(null) }
      })
      .catch(e => {
        setLoading(false)
        if (e.name === 'AbortError') setError('Request timed out after 24s. Check VPN connectivity and server status.')
        else setError(e.message)
      })
      .finally(() => clearTimeout(timeoutId))
  }, [fieldName, rawLabel, open, data, error])

  if (!fieldName || !rawLabel) return null

  const callCount = Array.isArray(data?.calls) ? data.calls.length : null

  return (
    <div style={{ marginTop: 4 }}>
      <button
        onClick={toggle}
        style={{
          display: 'inline-flex',
          alignItems: 'center',
          gap: 4,
          fontSize: 8.5,
          padding: '2px 7px',
          borderRadius: 4,
          cursor: 'pointer',
          color:       open ? '#22d3ee' : '#475569',
          background:  open ? 'rgba(34,211,238,0.07)' : 'transparent',
          border: '1px solid ' + (open ? 'rgba(34,211,238,0.22)' : 'rgba(71,85,105,0.28)'),
          transition: 'all 0.12s',
        }}
      >
        <Phone size={9} />
        {loading
          ? 'Loading…'
          : open
            ? `Hide calls${callCount != null ? ` (${callCount})` : ''}`
            : 'View calls'}
      </button>

      {open && (
        <div style={{ marginTop: 6, display: 'flex', flexDirection: 'column', gap: 6 }}>
          {loading && (
            <div style={{ fontSize: 9.5, color: '#94a3b8', textAlign: 'center', padding: '6px 0' }}>
              Fetching calls…
            </div>
          )}
          {error && (
            <div style={{
              fontSize: 9.5,
              color: '#fb7185',
              background: 'rgba(244,63,94,0.08)',
              border: '1px solid rgba(244,63,94,0.22)',
              borderRadius: 6,
              padding: '8px 10px',
              lineHeight: 1.5,
              wordBreak: 'break-word',
            }}>
              {error.includes('VPN') || error.includes('calls database')
                ? 'Cannot reach the calls database. Check VPN is connected, then restart the server.'
                : error}
            </div>
          )}
          {!loading && !error && data && !Array.isArray(data.calls) && (
            <div style={{ fontSize: 9.5, color: '#f59e0b' }}>Unexpected response format from server.</div>
          )}
          {!loading && !error && Array.isArray(data?.calls) && data.calls.length === 0 && (
            <div style={{ fontSize: 9.5, color: '#94a3b8' }}>No calls found for this label in recent data.</div>
          )}
          {Array.isArray(data?.calls) && data.calls.map((call, i) => (
            <CallRow key={call.call_id || i} call={call} />
          ))}
          {data?.has_more && (
            <div style={{ fontSize: 8.5, color: '#64748b' }}>Showing latest 25 calls. More exist.</div>
          )}
          {data?.date_filtered && (
            <div style={{ fontSize: 8, color: '#475569', marginTop: 2 }}>
              Searched most recent 30,000 calls. A GIN index is needed for full-history lookup.
            </div>
          )}
        </div>
      )}
    </div>
  )
}
