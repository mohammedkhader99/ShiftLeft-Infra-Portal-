import { useEffect, useRef, useState } from 'react'
import { Tag, InlineLoading, InlineNotification } from '@carbon/react'
import { getEvents, type LifecycleEvent } from '../api'

type BadgeType = 'green' | 'red' | 'purple' | 'teal' | 'blue' | 'gray'

// Colour the event by what it means, so the feed is scannable.
function tagType(type: string): BadgeType {
  if (/(failed|blocked|refused|unreachable|orphaned|expired|breached)/.test(type)) return 'red'
  if (/(provisioned|renewed|transferred|quorum\.met|waived|waiver\.granted|decommissioned|destroyed|approved)/.test(type)) return 'green'
  if (/(warning|expiring|variance|drift|scan|held|rejected)/.test(type)) return 'purple'
  if (/(budget|quota|actual)/.test(type)) return 'teal'
  return 'blue'
}

function summarise(detail?: Record<string, unknown> | null): string {
  if (!detail || typeof detail !== 'object') return ''
  const d = detail as Record<string, unknown>
  for (const key of ['error', 'reason', 'summary', 'detail', 'message']) {
    if (typeof d[key] === 'string') return d[key] as string
  }
  const parts = Object.entries(d)
    .filter(([, v]) => v !== null && v !== '' && typeof v !== 'object')
    .slice(0, 3)
    .map(([k, v]) => `${k}: ${v}`)
  return parts.join('  ·  ')
}

function when(at?: string | null): string {
  if (!at) return ''
  const d = new Date(at)
  return isNaN(d.getTime()) ? at : d.toLocaleString()
}

const MAX_KEPT = 250

export default function Activity() {
  const [events, setEvents] = useState<LifecycleEvent[]>([])
  const [state, setState] = useState<'loading' | 'live' | 'forbidden' | 'error'>('loading')
  const cursor = useRef(0)

  useEffect(() => {
    let active = true

    async function tick(initial: boolean) {
      const res = await getEvents(initial ? 0 : cursor.current, initial ? { tail: 80 } : { limit: 200 })
      if (!active) return
      if (res === 'forbidden') {
        setState('forbidden')
        return
      }
      if (!res) {
        if (initial) setState('error')
        return
      }
      cursor.current = res.cursor
      if (res.events.length) {
        setEvents((prev) => {
          const merged = initial ? res.events : [...prev, ...res.events]
          return merged.slice(-MAX_KEPT)
        })
      }
      setState('live')
    }

    tick(true)
    const id = setInterval(() => tick(false), 4000)
    return () => {
      active = false
      clearInterval(id)
    }
  }, [])

  if (state === 'forbidden')
    return (
      <InlineNotification
        kind="info"
        lowContrast
        hideCloseButton
        title="Not available for your role"
        subtitle="The lifecycle activity feed is for oversight roles (platform admin, auditor, finops)."
        style={{ maxWidth: 'none' }}
      />
    )
  if (state === 'loading') return <InlineLoading description="Connecting to the event stream…" />

  const newestFirst = [...events].reverse()

  return (
    <div>
      <div style={{ display: 'flex', alignItems: 'center', gap: '0.5rem', marginBottom: '0.75rem' }}>
        <Tag type="green" size="sm">live</Tag>
        <span style={{ fontSize: '0.85rem', color: 'var(--cds-text-secondary)' }}>
          Request &amp; environment lifecycle events, newest first — updates automatically (F-INT-02).
        </span>
      </div>
      {newestFirst.length === 0 ? (
        <p style={{ color: 'var(--cds-text-secondary)' }}>No events yet. Activity will appear here as requests progress.</p>
      ) : (
        <div style={{ borderTop: '1px solid var(--cds-border-subtle)' }}>
          {newestFirst.map((e) => (
            <div
              key={e.id}
              style={{
                display: 'grid',
                gridTemplateColumns: '11rem 12rem 1fr',
                gap: '1rem',
                alignItems: 'baseline',
                padding: '0.5rem 0.25rem',
                borderBottom: '1px solid var(--cds-border-subtle)',
                fontSize: '0.85rem',
              }}
            >
              <span style={{ color: 'var(--cds-text-secondary)', whiteSpace: 'nowrap' }}>{when(e.at)}</span>
              <span>
                <Tag type={tagType(e.type)} size="sm">{e.type}</Tag>
              </span>
              <span style={{ minWidth: 0 }}>
                {e.reference && <strong style={{ marginRight: '0.5rem' }}>{e.reference}</strong>}
                <span style={{ color: 'var(--cds-text-secondary)' }}>{summarise(e.detail)}</span>
                {e.actor && (
                  <span style={{ color: 'var(--cds-text-secondary)', marginLeft: '0.5rem' }}>· {e.actor}</span>
                )}
                {e.trace_id && (
                  <code title={`trace ${e.trace_id}`} style={{ color: 'var(--cds-text-secondary)', marginLeft: '0.5rem', fontSize: '0.72rem' }}>
                    trace:{e.trace_id.slice(0, 8)}
                  </code>
                )}
              </span>
            </div>
          ))}
        </div>
      )}
    </div>
  )
}
