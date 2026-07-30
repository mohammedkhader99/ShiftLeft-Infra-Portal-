import { useEffect, useState } from 'react'
import { Tile, InlineLoading, Select, SelectItem, ContentSwitcher, Switch } from '@carbon/react'
import { getShowback, type Showback } from '../api'

// The dimensions cost can be attributed to (must match the API's group_by set).
const GROUPS: [string, string][] = [
  ['cost_centre', 'Cost centre'],
  ['project', 'Project'],
  ['environment', 'Environment'],
  ['owner', 'Owner'],
]

function money(n: number, currency: string) {
  return `${n.toLocaleString(undefined, { maximumFractionDigits: 0 })} ${currency}`
}

export default function ShowbackPage() {
  const [groupBy, setGroupBy] = useState('cost_centre')
  const [scope, setScope] = useState('active')
  const [data, setData] = useState<Showback | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let active = true
    getShowback(groupBy, scope)
      .then((s) => {
        if (!active) return
        if (s === 'forbidden') setForbidden(true)
        else if (s) {
          setData(s)
          setForbidden(false)
        }
      })
      .catch(() => {})
      .finally(() => active && setLoaded(true))
    return () => {
      active = false
    }
  }, [groupBy, scope])

  if (forbidden)
    return (
      <Tile>
        <p style={{ color: 'var(--cds-text-secondary)' }}>
          Showback is for oversight roles (platform admin, auditor, finops). Your account doesn't
          have access.
        </p>
      </Tile>
    )

  const groupLabel = GROUPS.find(([v]) => v === (data?.group_by || groupBy))?.[1]
  const maxMonthly = Math.max(1, ...(data?.rows.map((r) => r.monthly) || [1]))

  return (
    <div>
      <div style={{ display: 'flex', gap: '1.5rem', alignItems: 'flex-end', marginBottom: '1.25rem', flexWrap: 'wrap' }}>
        <div style={{ minWidth: '14rem' }}>
          <Select id="group_by" labelText="Break down by" value={groupBy} onChange={(e) => setGroupBy(e.target.value)}>
            {GROUPS.map(([v, l]) => (
              <SelectItem key={v} value={v} text={l} />
            ))}
          </Select>
        </div>
        <div>
          <label style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', display: 'block', marginBottom: '0.25rem' }}>
            Scope
          </label>
          <ContentSwitcher
            selectedIndex={scope === 'active' ? 0 : 1}
            onChange={({ index }) => setScope(index === 0 ? 'active' : 'committed')}
          >
            <Switch name="active" text="Running" />
            <Switch name="committed" text="Committed" />
          </ContentSwitcher>
        </div>
      </div>

      {!loaded && !data ? (
        <InlineLoading description="Loading showback…" />
      ) : !data ? (
        <p style={{ color: 'var(--cds-text-error)' }}>Couldn't load showback.</p>
      ) : (
        <Tile>
          <div style={{ marginBottom: '1rem' }}>
            <div style={{ fontSize: '1.75rem', fontWeight: 300 }}>
              {money(data.total.monthly, data.currency)}
              <span style={{ fontSize: '0.9rem', color: 'var(--cds-text-secondary)' }}> / month</span>
            </div>
            <div style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)' }}>
              {money(data.total.annual, data.currency)} / year · {data.total.count} request(s) ·{' '}
              {scope === 'active' ? 'running' : 'committed'} spend
            </div>
          </div>

          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
            <thead>
              <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
                <th style={{ padding: '0.4rem 0.5rem' }}>{groupLabel}</th>
                <th style={{ padding: '0.4rem 0.5rem', width: '35%' }}>Share</th>
                <th style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>Count</th>
                <th style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>Monthly</th>
                <th style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>Annual</th>
              </tr>
            </thead>
            <tbody>
              {data.rows.length ? (
                data.rows.map((r) => (
                  <tr key={r.key} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                    <td style={{ padding: '0.4rem 0.5rem', maxWidth: '12rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.key}>
                      {r.key}
                    </td>
                    <td style={{ padding: '0.4rem 0.5rem' }}>
                      <span style={{ display: 'block', background: 'var(--cds-layer-accent)', height: '0.8rem' }}>
                        <span style={{ display: 'block', width: `${(r.monthly / maxMonthly) * 100}%`, height: '100%', background: 'var(--cds-interactive)' }} />
                      </span>
                    </td>
                    <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right' }}>{r.count}</td>
                    <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right', fontWeight: 500 }}>{money(r.monthly, data.currency)}</td>
                    <td style={{ padding: '0.4rem 0.5rem', textAlign: 'right', color: 'var(--cds-text-secondary)' }}>{money(r.annual, data.currency)}</td>
                  </tr>
                ))
              ) : (
                <tr>
                  <td colSpan={5} style={{ padding: '0.75rem 0.5rem', color: 'var(--cds-text-secondary)' }}>
                    No {scope === 'active' ? 'running' : 'committed'} spend yet.
                  </td>
                </tr>
              )}
            </tbody>
          </table>
        </Tile>
      )}
    </div>
  )
}
