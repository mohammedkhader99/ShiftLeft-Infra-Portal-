import { useEffect, useState } from 'react'
import { Tile, InlineLoading, Select, SelectItem, ContentSwitcher, Switch } from '@carbon/react'
import { getShowback, getBudgets, getVariance, getForecast, getOptimisation, type Showback, type BudgetRow, type Variance, type Forecast, type Optimisation } from '../api'

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
  const [budgets, setBudgets] = useState<BudgetRow[]>([])
  const [variance, setVariance] = useState<Variance | null>(null)
  const [forecast, setForecast] = useState<Forecast | null>(null)
  const [optim, setOptim] = useState<Optimisation | null>(null)
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

  useEffect(() => {
    let active = true
    getBudgets()
      .then((b) => active && b && b !== 'forbidden' && setBudgets(b.budgets))
      .catch(() => {})
    getVariance()
      .then((v) => active && v && v !== 'forbidden' && setVariance(v))
      .catch(() => {})
    getForecast(6)
      .then((f) => active && f && f !== 'forbidden' && setForecast(f))
      .catch(() => {})
    getOptimisation()
      .then((o) => active && o && o !== 'forbidden' && setOptim(o))
      .catch(() => {})
    return () => {
      active = false
    }
  }, [])

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
  const forecastMax = Math.max(1, ...(forecast?.months.map((m) => m.projected_monthly) || [1]))

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

      {budgets.length > 0 && (
        <Tile style={{ marginTop: '1rem' }}>
          <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.75rem' }}>
            Cost-centre budgets (F-FIN-02)
          </h4>
          {budgets.map((b) => {
            const used = b.limit > 0 ? Math.min(100, (b.current / b.limit) * 100) : 0
            const color =
              b.status === 'over'
                ? 'var(--cds-support-error)'
                : b.status === 'near'
                  ? 'var(--cds-support-warning)'
                  : 'var(--cds-support-success)'
            return (
              <div key={b.cost_centre} style={{ marginBottom: '0.75rem', fontSize: '0.85rem' }}>
                <div style={{ display: 'flex', justifyContent: 'space-between', marginBottom: '0.2rem' }}>
                  <span style={{ fontWeight: 500 }}>{b.cost_centre}</span>
                  <span style={{ color: 'var(--cds-text-secondary)' }}>
                    {money(b.current, b.currency)} of {money(b.limit, b.currency)} ·{' '}
                    <span style={{ color }}>{money(b.remaining, b.currency)} left</span>
                  </span>
                </div>
                <span style={{ display: 'block', background: 'var(--cds-layer-accent)', height: '0.6rem' }}>
                  <span style={{ display: 'block', width: `${used}%`, height: '100%', background: color }} />
                </span>
              </div>
            )
          })}
        </Tile>
      )}

      {variance && variance.rows.length > 0 && (
        <Tile style={{ marginTop: '1rem' }}>
          <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.75rem' }}>
            Actual vs estimate (F-FIN-01)
          </h4>
          <div style={{ fontSize: '0.85rem', color: 'var(--cds-text-secondary)', marginBottom: '0.75rem' }}>
            Billed {money(variance.total.actual, variance.currency)} vs estimated{' '}
            {money(variance.total.estimate, variance.currency)} ·{' '}
            <span style={{ color: variance.total.variance > 0 ? 'var(--cds-support-error)' : 'var(--cds-support-success)' }}>
              {variance.total.variance_pct > 0 ? '+' : ''}
              {variance.total.variance_pct}% overall
            </span>
          </div>
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
            <thead>
              <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
                <th style={{ padding: '0.3rem 0.5rem' }}>Request</th>
                <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Estimate</th>
                <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Actual</th>
                <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Variance</th>
              </tr>
            </thead>
            <tbody>
              {variance.rows.slice(0, 8).map((r) => (
                <tr key={r.reference} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                  <td style={{ padding: '0.3rem 0.5rem' }} title={r.environment || undefined}>{r.reference}</td>
                  <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>{money(r.estimate, variance.currency)}</td>
                  <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>{money(r.actual, variance.currency)}</td>
                  <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right', fontWeight: 500,
                    color: r.status === 'over' ? 'var(--cds-support-error)' : r.status === 'under' ? 'var(--cds-support-success)' : 'var(--cds-text-secondary)' }}>
                    {r.variance_pct > 0 ? '+' : ''}{r.variance_pct}%
                  </td>
                </tr>
              ))}
            </tbody>
          </table>
        </Tile>
      )}

      {forecast && (
        <Tile style={{ marginTop: '1rem' }}>
          <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>
            Spend forecast — next {forecast.horizon_months} months (F-RPT-05)
          </h4>
          <div style={{ fontSize: '0.85rem', color: 'var(--cds-text-secondary)', marginBottom: '0.75rem' }}>
            Now {money(forecast.current_monthly, forecast.currency)}/mo → projected{' '}
            {money(forecast.projected_monthly, forecast.currency)}/mo ·{' '}
            <span style={{ color: 'var(--cds-support-success)' }}>+{money(forecast.pipeline_monthly, forecast.currency)} pipeline</span>{' '}
            ·{' '}
            <span style={{ color: 'var(--cds-support-warning)' }}>−{money(forecast.expiring_monthly, forecast.currency)} expiring</span>
          </div>
          {forecast.months.map((m) => (
            <div key={m.month} style={{ display: 'grid', gridTemplateColumns: '5.5rem 1fr 7rem', gap: '0.5rem', alignItems: 'center', fontSize: '0.8rem', marginBottom: '0.25rem' }}>
              <span style={{ color: 'var(--cds-text-secondary)' }}>{m.label}{m.month === 0 ? ' · now' : ''}</span>
              <span style={{ display: 'block', background: 'var(--cds-layer-accent)', height: '0.8rem' }}>
                <span style={{ display: 'block', width: `${(m.projected_monthly / forecastMax) * 100}%`, height: '100%', background: 'var(--cds-interactive)' }} />
              </span>
              <span style={{ textAlign: 'right', fontWeight: 500 }}>{money(m.projected_monthly, forecast.currency)}</span>
            </div>
          ))}
          {(forecast.drivers.pipeline.length > 0 || forecast.drivers.expiring.length > 0) && (
            <div style={{ marginTop: '0.85rem', fontSize: '0.78rem', color: 'var(--cds-text-secondary)', display: 'flex', gap: '2.5rem', flexWrap: 'wrap' }}>
              {forecast.drivers.pipeline.length > 0 && (
                <div>
                  <div style={{ fontWeight: 500, color: 'var(--cds-text-primary)', marginBottom: '0.2rem' }}>Landing (pipeline)</div>
                  {forecast.drivers.pipeline.map((d) => (
                    <div key={d.reference}>{d.reference} +{money(d.monthly, forecast.currency)} · month {d.month}</div>
                  ))}
                </div>
              )}
              {forecast.drivers.expiring.length > 0 && (
                <div>
                  <div style={{ fontWeight: 500, color: 'var(--cds-text-primary)', marginBottom: '0.2rem' }}>Expiring (TTL)</div>
                  {forecast.drivers.expiring.map((d) => (
                    <div key={d.reference}>{d.reference} −{money(d.monthly, forecast.currency)} · month {d.month}</div>
                  ))}
                </div>
              )}
            </div>
          )}
          <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.75rem' }}>
            A projection from the estate + pipeline, not a guarantee: today's run-rate, plus in-flight
            requests as they provision, minus non-prod environments as their TTL expires.
          </p>
        </Tile>
      )}

      {optim && (
        <Tile style={{ marginTop: '1rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '0.75rem', flexWrap: 'wrap', gap: '0.5rem' }}>
            <h4 style={{ fontSize: '0.95rem', fontWeight: 500 }}>Optimisation digest (F-FIN-12)</h4>
            {optim.total_saving > 0 && (
              <span style={{ fontSize: '0.85rem' }}>
                Potential saving{' '}
                <strong style={{ color: 'var(--cds-support-success)' }}>{money(optim.total_saving, optim.currency)}/mo</strong>
                <span style={{ color: 'var(--cds-text-secondary)' }}> · {optim.environment_count} env(s) · {optim.owner_count} owner(s)</span>
              </span>
            )}
          </div>
          {optim.owners.length === 0 && (
            <p style={{ fontSize: '0.85rem', color: 'var(--cds-text-secondary)' }}>
              No savings opportunities — your non-prod environments are well-sized 👍
            </p>
          )}
          {optim.owners.map((o) => (
            <div key={o.owner} style={{ marginBottom: '0.9rem' }}>
              <div style={{ display: 'flex', justifyContent: 'space-between', fontSize: '0.85rem', fontWeight: 500, marginBottom: '0.25rem' }}>
                <span>{o.owner}</span>
                <span style={{ color: 'var(--cds-support-success)' }}>−{money(o.saving, optim.currency)}/mo</span>
              </div>
              {o.environments.map((e) => (
                <div key={e.reference} style={{ marginLeft: '0.5rem', marginBottom: '0.35rem', fontSize: '0.8rem' }}>
                  <span style={{ color: 'var(--cds-text-secondary)' }}>
                    {e.reference}{e.environment ? ` · ${e.environment}` : ''}{e.tier ? ` (${e.tier})` : ''}
                  </span>
                  <ul style={{ margin: '0.15rem 0 0 1.1rem', padding: 0 }}>
                    {e.recommendations.map((r, i) => (
                      <li key={i}>
                        {r.detail}{' '}
                        <span style={{ color: 'var(--cds-support-success)' }}>(−{money(r.monthly_saving, optim.currency)}/mo)</span>
                      </li>
                    ))}
                  </ul>
                </div>
              ))}
            </div>
          ))}
          {optim.owners.length > 0 && (
            <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.25rem' }}>
              Recommendations only — nothing changes automatically. Savings are re-priced estimates for non-prod environments.
            </p>
          )}
        </Tile>
      )}
    </div>
  )
}
