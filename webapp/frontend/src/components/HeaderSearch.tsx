import { useEffect, useRef, useState } from 'react'
import { Search } from '@carbon/react'
import { searchRequests, type SearchResult } from '../api'

// A quick-search box for the top header (Portal UI polish). Debounced search over
// the caller's own requests; a result jumps to it in My Requests.
export default function HeaderSearch() {
  const [q, setQ] = useState('')
  const [results, setResults] = useState<SearchResult[]>([])
  const [open, setOpen] = useState(false)
  const boxRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    const term = q.trim()
    if (term.length < 2) {
      setResults([])
      return
    }
    let active = true
    const t = setTimeout(() => {
      searchRequests(term)
        .then((r) => {
          if (active) {
            setResults(r)
            setOpen(true)
          }
        })
        .catch(() => {})
    }, 250)
    return () => {
      active = false
      clearTimeout(t)
    }
  }, [q])

  // Close the dropdown on a click outside the box.
  useEffect(() => {
    const onDoc = (e: MouseEvent) => {
      if (boxRef.current && !boxRef.current.contains(e.target as Node)) setOpen(false)
    }
    document.addEventListener('mousedown', onDoc)
    return () => document.removeEventListener('mousedown', onDoc)
  }, [])

  const go = (ref: string) => {
    window.location.hash = `#/requests?reference=${encodeURIComponent(ref)}`
    setQ('')
    setResults([])
    setOpen(false)
  }

  return (
    <div ref={boxRef} style={{ position: 'relative', width: '18rem', maxWidth: '40vw', marginLeft: '1rem' }}>
      <Search
        size="sm"
        labelText="Search your requests"
        placeholder="Search your requests…"
        value={q}
        onChange={(e) => setQ((e.target as HTMLInputElement).value)}
        onFocus={() => results.length > 0 && setOpen(true)}
        onKeyDown={(e: React.KeyboardEvent) => {
          if (e.key === 'Escape') setOpen(false)
        }}
        closeButtonLabelText="Clear search"
      />
      {open && q.trim().length >= 2 && (
        <div
          style={{
            position: 'absolute',
            top: '100%',
            left: 0,
            right: 0,
            zIndex: 8000,
            background: 'var(--cds-layer)',
            border: '1px solid var(--cds-border-subtle)',
            boxShadow: '0 2px 6px rgba(0,0,0,0.25)',
            maxHeight: '20rem',
            overflowY: 'auto',
          }}
        >
          {results.length === 0 ? (
            <div style={{ padding: '0.6rem 0.75rem', fontSize: '0.82rem', color: 'var(--cds-text-secondary)' }}>
              No matching requests.
            </div>
          ) : (
            results.map((r) => (
              <button
                key={r.reference}
                type="button"
                onClick={() => go(r.reference)}
                style={{
                  display: 'block',
                  width: '100%',
                  textAlign: 'left',
                  padding: '0.5rem 0.75rem',
                  background: 'transparent',
                  border: 'none',
                  borderBottom: '1px solid var(--cds-border-subtle-01)',
                  cursor: 'pointer',
                  color: 'var(--cds-text-primary)',
                }}
              >
                <div style={{ fontSize: '0.85rem', fontWeight: 500 }}>
                  {r.reference}
                  {r.environment && (
                    <span style={{ color: 'var(--cds-text-secondary)', fontWeight: 400 }}> · {r.environment}</span>
                  )}
                </div>
                <div style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)' }}>
                  {r.request_type} · {r.status}
                  {r.tier ? ` · ${r.tier}` : ''}
                </div>
              </button>
            ))
          )}
        </div>
      )}
    </div>
  )
}
