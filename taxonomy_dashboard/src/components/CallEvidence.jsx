import { useState, useCallback } from 'react'
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
    <div style={{ background: 'rgba(3,8,15,0.55)', border: '1px solid rgba(71,85,105,0.26)', borderRadius: 6, overflow: 'hidden' }}>
      <button
        className="w-full text-left"
        style={{ padding: '6px 10px', display: 'flex', alignItems: 'flex-start', gap: 6 }}
        onClick={() => setExpanded(e => !e)}
      >
        <span style={{ color: '#475569', flexShrink: 0, marginTop: 2 }}>
          {expanded ? <ChevronDown size={10} /> : <ChevronRight size={10} />}
        </span>
        <div style={{ flex: 1, minWidth: 0 }}>
          <div style={{ display: 'flex', alignItems: 'center', gap: 8, flexWrap: 'wrap' }}>
            {date && (
              <span style={{ fontSize: 9, fontFamily: 'monospace', color: '#64748b' }}>{date}</span>
            )}
            {call.agent_name && (
              <span style={{ fontSize: 9, color: '#94a3b8' }}>{call.agent_name}</span>
            )}
          </div>
          {call.summary ? (
            <div style={{
              fontSize: 9.5,
              color: '#94a3b8',
              marginTop: 2,
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
            <div style={{ fontSize: 9, color: '#334155', marginTop: 2 }}>No summary available</div>
          )}
        </div>
      </button>

      {expanded && (
        <div style={{ borderTop: '1px solid rgba(71,85,105,0.18)', padding: '8px 10px', display: 'flex', flexDirection: 'column', gap: 8 }}>
          <div style={{ fontSize: 8, fontFamily: 'monospace', color: '#334155', wordBreak: 'break-all' }}>
            {call.call_id}
          </div>
          {call.summary && (
            <div>
              <div style={{ fontSize: 8, textTransform: 'uppercase', letterSpacing: '0.12em', color: '#475569', marginBottom: 4 }}>
                Summary
              </div>
              <div style={{ fontSize: 9.5, color: '#94a3b8', lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word' }}>
                {call.summary}
              </div>
            </div>
          )}
          {call.transcript_preview && (
            <div>
              <div style={{ fontSize: 8, textTransform: 'uppercase', letterSpacing: '0.12em', color: '#475569', marginBottom: 4 }}>
                Transcript{call.full_transcript_available ? ' (preview)' : ''}
              </div>
              <div style={{ fontSize: 9, color: '#64748b', lineHeight: 1.5, whiteSpace: 'pre-wrap', wordBreak: 'break-word', fontFamily: 'monospace' }}>
                {call.transcript_preview}{call.full_transcript_available ? '…' : ''}
              </div>
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

  const toggle = useCallback(() => {
    if (open) { setOpen(false); return }
    // Re-use cached result if available
    if (data || error) { setOpen(true); return }
    setOpen(true)
    setLoading(true)
    const params = new URLSearchParams({ field_name: fieldName, raw_label: rawLabel, limit: '25' })
    fetch(`/api/calls/by-label?${params}`)
      .then(r => {
        if (!r.ok && r.status === 404) throw new Error('Endpoint not found — restart the taxonomy server to load the new /api/calls/by-label route.')
        const ct = r.headers.get('content-type') || ''
        if (!ct.includes('application/json')) throw new Error(`Server returned unexpected response (${r.status}). Ensure the taxonomy server is running with the latest code.`)
        return r.json()
      })
      .then(d => {
        setLoading(false)
        if (d.error) { setError(d.error); setData(null) }
        else         { setData(d);        setError(null) }
      })
      .catch(e => { setLoading(false); setError(e.message) })
  }, [fieldName, rawLabel, open, data, error])

  if (!fieldName || !rawLabel) return null

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
        {loading ? 'Loading…' : open ? 'Hide calls' : 'View calls'}
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
              {error.includes('timed out') || error.includes('timeout')
                ? 'Call lookup timed out. This field needs an indexed lookup — add taxonomy_call_label_index backfill to support it.'
                : error}
            </div>
          )}
          {!loading && !error && data && !Array.isArray(data.calls) && (
            <div style={{ fontSize: 9.5, color: '#f59e0b' }}>Unexpected response format.</div>
          )}
          {!loading && !error && Array.isArray(data?.calls) && data.calls.length === 0 && (
            <div style={{ fontSize: 9.5, color: '#94a3b8' }}>No calls found for this label.</div>
          )}
          {Array.isArray(data?.calls) && data.calls.map((call, i) => (
            <CallRow key={call.call_id || i} call={call} />
          ))}
          {data?.has_more && (
            <div style={{ fontSize: 8.5, color: '#64748b' }}>
              Showing latest 25 calls. More available.
            </div>
          )}
        </div>
      )}
    </div>
  )
}
