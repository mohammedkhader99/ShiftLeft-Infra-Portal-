import { Fragment, useEffect, useState } from 'react'
import { Tile, InlineLoading, Select, SelectItem, ContentSwitcher, Switch, Tag, Button, TextInput } from '@carbon/react'
import { getShowback, getBudgets, getVariance, getForecast, getOptimisation, getSustainability, getShutdown, getReportSubscriptions, createReportSubscription, deleteReportSubscription, runReportNow, getReportRuns, type Showback, type BudgetRow, type Variance, type Forecast, type Optimisation, type Sustainability, type Shutdown, type ReportSubRow, type ReportRunRow } from '../api'

// Report subscriptions (F-RPT-11): the report kinds + cadences the API accepts.
const REPORT_KINDS: [string, string][] = [
  ['forecast', 'Spend forecast'],
  ['anomalies', 'Cost anomalies'],
  ['estate', 'Estate summary'],
]
const CADENCES: [string, string][] = [
  ['daily', 'Daily'],
  ['weekly', 'Weekly'],
  ['monthly', 'Monthly'],
]

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
  const [sustain, setSustain] = useState<Sustainability | null>(null)
  const [sd, setSd] = useState<Shutdown | null>(null)
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
    getSustainability()
      .then((s) => active && s && s !== 'forbidden' && setSustain(s))
      .catch(() => {})
    getShutdown()
      .then((s) => active && s && s !== 'forbidden' && setSd(s))
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

      {sustain && (
        <Tile style={{ marginTop: '1rem' }}>
          <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.5rem' }}>Sustainability estimate (F-FIN-13)</h4>
          {sustain.environment_count === 0 ? (
            <p style={{ fontSize: '0.85rem', color: 'var(--cds-text-secondary)' }}>No provisioned environments to estimate yet.</p>
          ) : (
            <>
              <div style={{ fontSize: '0.85rem', marginBottom: '0.75rem' }}>
                Estate:{' '}
                <strong>{sustain.total_energy_kwh_month.toLocaleString(undefined, { maximumFractionDigits: 0 })} kWh/mo</strong>
                {' · '}
                <strong>{sustain.total_carbon_kg_month.toLocaleString(undefined, { maximumFractionDigits: 0 })} kg CO₂e/mo</strong>
                <span style={{ color: 'var(--cds-text-secondary)' }}>
                  {' '}— ≈ {sustain.equivalents.car_km.toLocaleString()} km driven, or {sustain.equivalents.trees_year} trees/year to offset
                </span>
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                <thead>
                  <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
                    <th style={{ padding: '0.3rem 0.5rem' }}>Environment</th>
                    <th style={{ padding: '0.3rem 0.5rem' }}>Target</th>
                    <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>kWh/mo</th>
                    <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>kg CO₂e/mo</th>
                  </tr>
                </thead>
                <tbody>
                  {sustain.environments.map((e) => (
                    <tr key={e.reference} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                      <td style={{ padding: '0.3rem 0.5rem' }} title={e.environment || undefined}>
                        {e.reference}{e.environment ? ` · ${e.environment}` : ''}
                      </td>
                      <td style={{ padding: '0.3rem 0.5rem', color: 'var(--cds-text-secondary)' }}>{e.deployment_target || '—'}</td>
                      <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>{e.energy_kwh_month.toLocaleString(undefined, { maximumFractionDigits: 0 })}</td>
                      <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right', fontWeight: 500 }}>{e.carbon_kg_month.toLocaleString(undefined, { maximumFractionDigits: 0 })}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>{sustain.note}</p>
        </Tile>
      )}

      {sd && (
        <Tile style={{ marginTop: '1rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '0.5rem', flexWrap: 'wrap', gap: '0.5rem' }}>
            <h4 style={{ fontSize: '0.95rem', fontWeight: 500 }}>Scheduled auto-shutdown (F-FIN-06)</h4>
            <span style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)' }}>
              {sd.schedule.days} {sd.schedule.start}–{sd.schedule.end} {sd.schedule.tz}
              {' · '}
              {sd.enabled ? (sd.off_hours_now ? 'off-hours now' : 'business hours now') : 'auto-pause off'}
            </span>
          </div>
          {sd.environment_count === 0 ? (
            <p style={{ fontSize: '0.85rem', color: 'var(--cds-text-secondary)' }}>No non-prod environments to shut down.</p>
          ) : (
            <>
              <div style={{ fontSize: '0.85rem', marginBottom: '0.75rem' }}>
                Potential saving{' '}
                <strong style={{ color: 'var(--cds-support-success)' }}>{money(sd.total_saving, sd.currency)}/mo</strong>
                <span style={{ color: 'var(--cds-text-secondary)' }}>
                  {' '}from powering non-prod down out-of-hours (≈ {Math.round(sd.off_hours_fraction * 100)}% of the week)
                </span>
              </div>
              <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
                <thead>
                  <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
                    <th style={{ padding: '0.3rem 0.5rem' }}>Environment</th>
                    <th style={{ padding: '0.3rem 0.5rem' }}>Tier</th>
                    <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Compute/mo</th>
                    <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Saving/mo</th>
                  </tr>
                </thead>
                <tbody>
                  {sd.environments.map((e) => (
                    <tr key={e.reference} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                      <td style={{ padding: '0.3rem 0.5rem' }} title={e.environment || undefined}>
                        {e.reference}{e.environment ? ` · ${e.environment}` : ''}
                        {e.paused && <Tag type="cool-gray" size="sm" style={{ marginLeft: '0.4rem' }}>paused</Tag>}
                      </td>
                      <td style={{ padding: '0.3rem 0.5rem', color: 'var(--cds-text-secondary)' }}>{e.tier}</td>
                      <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>{money(e.compute_monthly, sd.currency)}</td>
                      <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right', fontWeight: 500, color: 'var(--cds-support-success)' }}>{money(e.monthly_saving, sd.currency)}</td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </>
          )}
          <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>{sd.note}</p>
        </Tile>
      )}

      <ReportSubscriptions />
    </div>
  )
}

function fmtWhen(iso?: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '—' : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function sevTag(sev: string) {
  const type = sev === 'high' ? 'red' : sev === 'medium' ? 'magenta' : 'cool-gray'
  return <Tag type={type as any} size="sm">{sev}</Tag>
}

// The exact stored JSON, one click away under every formatted report.
function RawData({ data }: { data: any }) {
  return (
    <details style={{ marginTop: '0.6rem' }}>
      <summary style={{ cursor: 'pointer', fontSize: '0.72rem', color: 'var(--cds-text-secondary)' }}>Raw data</summary>
      <pre style={{ margin: '0.35rem 0 0', padding: '0.5rem', overflowX: 'auto', background: 'var(--cds-layer-accent-01)', fontSize: '0.72rem' }}>
        {JSON.stringify(data, null, 2)}
      </pre>
    </details>
  )
}

// Render a stored report run as a readable report (per kind), not raw JSON.
function ReportView({ report, summary }: { report: string; summary: any }) {
  if (!summary || typeof summary !== 'object') return <RawData data={summary} />
  const currency: string = summary.currency || 'AED'

  if (report === 'estate') {
    const rows = Object.entries((summary.by_status || {}) as Record<string, number>).sort((a, b) => b[1] - a[1])
    return (
      <div style={{ fontSize: '0.82rem' }}>
        <div style={{ marginBottom: '0.5rem' }}>
          <strong style={{ fontSize: '1.05rem', fontWeight: 500 }}>{money(summary.committed_monthly || 0, currency)}</strong>
          <span style={{ color: 'var(--cds-text-secondary)' }}> / month committed · {summary.environments_provisioned ?? 0} provisioned environment(s)</span>
        </div>
        {rows.length > 0 && (
          <table style={{ borderCollapse: 'collapse', fontSize: '0.8rem' }}>
            <tbody>
              {rows.map(([status, count]) => (
                <tr key={status}>
                  <td style={{ padding: '0.15rem 1.5rem 0.15rem 0', color: 'var(--cds-text-secondary)', textTransform: 'capitalize' }}>{status}</td>
                  <td style={{ padding: '0.15rem 0', textAlign: 'right', fontWeight: 500 }}>{count}</td>
                </tr>
              ))}
            </tbody>
          </table>
        )}
        <RawData data={summary} />
      </div>
    )
  }

  if (report === 'forecast') {
    const months: any[] = Array.isArray(summary.months) ? summary.months : []
    const fmax = Math.max(1, ...months.map((m) => m.projected_monthly || 0))
    return (
      <div style={{ fontSize: '0.82rem' }}>
        <div style={{ marginBottom: '0.6rem', color: 'var(--cds-text-secondary)' }}>
          Now <strong style={{ color: 'var(--cds-text-primary)' }}>{money(summary.current_monthly || 0, currency)}</strong>/mo → projected{' '}
          <strong style={{ color: 'var(--cds-text-primary)' }}>{money(summary.projected_monthly || 0, currency)}</strong>/mo
          {typeof summary.pipeline_monthly === 'number' && (
            <> · <span style={{ color: 'var(--cds-support-success)' }}>+{money(summary.pipeline_monthly, currency)} pipeline</span></>
          )}
          {typeof summary.expiring_monthly === 'number' && (
            <> · <span style={{ color: 'var(--cds-support-warning)' }}>−{money(summary.expiring_monthly, currency)} expiring</span></>
          )}
        </div>
        {months.map((m) => (
          <div key={m.month} style={{ display: 'grid', gridTemplateColumns: '5.5rem 1fr 7rem', gap: '0.5rem', alignItems: 'center', fontSize: '0.78rem', marginBottom: '0.2rem' }}>
            <span style={{ color: 'var(--cds-text-secondary)' }}>{m.label}{m.month === 0 ? ' · now' : ''}</span>
            <span style={{ display: 'block', background: 'var(--cds-layer-accent)', height: '0.7rem' }}>
              <span style={{ display: 'block', width: `${((m.projected_monthly || 0) / fmax) * 100}%`, height: '100%', background: 'var(--cds-interactive)' }} />
            </span>
            <span style={{ textAlign: 'right', fontWeight: 500 }}>{money(m.projected_monthly || 0, currency)}</span>
          </div>
        ))}
        <RawData data={summary} />
      </div>
    )
  }

  if (report === 'anomalies') {
    const items: any[] = Array.isArray(summary.anomalies) ? summary.anomalies : []
    const by = summary.by_severity || {}
    return (
      <div style={{ fontSize: '0.82rem' }}>
        <div style={{ marginBottom: '0.5rem' }}>
          <strong>{summary.count ?? items.length}</strong> signal(s)
          {(by.high || by.medium || by.low) ? (
            <span style={{ marginLeft: '0.5rem', display: 'inline-flex', gap: '0.35rem', alignItems: 'center' }}>
              {by.high ? <>{sevTag('high')}×{by.high}</> : null}
              {by.medium ? <>{sevTag('medium')}×{by.medium}</> : null}
              {by.low ? <>{sevTag('low')}×{by.low}</> : null}
            </span>
          ) : null}
        </div>
        {items.length === 0 ? (
          <p style={{ color: 'var(--cds-text-secondary)' }}>No anomalies flagged 👍</p>
        ) : (
          <ul style={{ margin: 0, padding: 0, listStyle: 'none' }}>
            {items.map((a, i) => (
              <li key={i} style={{ padding: '0.35rem 0', borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginBottom: '0.1rem', flexWrap: 'wrap' }}>
                  {sevTag(a.severity)}
                  <strong>{a.subject || a.reference}</strong>
                  <span style={{ color: 'var(--cds-text-secondary)', fontSize: '0.72rem' }}>{a.kind}/{a.type}</span>
                </div>
                <div style={{ color: 'var(--cds-text-secondary)' }}>{a.signal}</div>
              </li>
            ))}
          </ul>
        )}
        <RawData data={summary} />
      </div>
    )
  }

  return <RawData data={summary} />
}

// Scheduled report subscriptions (F-RPT-11). Oversight-only; the Showback page
// already gates on that, so if the API says 'forbidden' we simply render nothing.
function ReportSubscriptions() {
  const [subs, setSubs] = useState<ReportSubRow[] | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [report, setReport] = useState('forecast')
  const [cadence, setCadence] = useState('weekly')
  const [url, setUrl] = useState('')
  const [secret, setSecret] = useState('')
  const [msg, setMsg] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)
  const [runsFor, setRunsFor] = useState<number | null>(null)
  const [runs, setRuns] = useState<ReportRunRow[]>([])

  const load = () =>
    getReportSubscriptions()
      .then((r) => {
        if (r === 'forbidden') setForbidden(true)
        else if (r) {
          setSubs(r.subscriptions)
          setForbidden(false)
        }
      })
      .catch(() => {})

  useEffect(() => {
    load()
  }, [])

  if (forbidden) return null

  const create = async () => {
    setBusy(true)
    const r = await createReportSubscription(report, cadence, url.trim() || undefined, secret.trim() || undefined)
    setBusy(false)
    if (r.status >= 400) {
      setMsg(typeof r.body?.detail === 'string' ? r.body.detail : 'Could not create the subscription.')
      return
    }
    setUrl('')
    setSecret('')
    setMsg(null)
    load()
  }

  const remove = async (id: number) => {
    await deleteReportSubscription(id)
    if (runsFor === id) setRunsFor(null)
    load()
  }

  const viewRuns = async (id: number) => {
    if (runsFor === id) {
      setRunsFor(null)
      return
    }
    setRunsFor(id)
    const r = await getReportRuns(id)
    setRuns(r?.runs || [])
  }

  const runNow = async (id: number) => {
    setBusy(true)
    const r = await runReportNow(id)
    setBusy(false)
    if (r.status < 400) {
      setMsg(`Report generated for subscription #${id}.`)
      setRunsFor(id)
      const rr = await getReportRuns(id)
      setRuns(rr?.runs || [])
      load()
    }
  }

  const deliveredTag = (d?: string | null) =>
    d === 'delivered' ? (
      <Tag type="green" size="sm">delivered</Tag>
    ) : d === 'failed' ? (
      <Tag type="red" size="sm">delivery failed</Tag>
    ) : (
      <Tag type="cool-gray" size="sm">saved in portal</Tag>
    )

  return (
    <Tile style={{ marginTop: '1rem' }}>
      <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.5rem' }}>
        Report subscriptions (F-RPT-11)
      </h4>
      <p style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', marginBottom: '0.85rem' }}>
        Schedule a report to finance, security or owners. Each run is saved here (always viewable);
        a webhook URL is an optional external copy, HMAC-signed. Email delivery is a future option.
      </p>

      <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: '0.5rem' }}>
        <div style={{ minWidth: '11rem' }}>
          <Select id="rs-report" labelText="Report" value={report} onChange={(e) => setReport(e.target.value)}>
            {REPORT_KINDS.map(([v, l]) => (
              <SelectItem key={v} value={v} text={l} />
            ))}
          </Select>
        </div>
        <div style={{ minWidth: '8rem' }}>
          <Select id="rs-cadence" labelText="Cadence" value={cadence} onChange={(e) => setCadence(e.target.value)}>
            {CADENCES.map(([v, l]) => (
              <SelectItem key={v} value={v} text={l} />
            ))}
          </Select>
        </div>
        <div style={{ flex: '1 1 14rem', minWidth: '12rem' }}>
          <TextInput id="rs-url" labelText="Webhook URL (optional)" placeholder="https://…" value={url}
            onChange={(e) => setUrl(e.target.value)} />
        </div>
        <div style={{ flex: '1 1 10rem', minWidth: '9rem' }}>
          <TextInput id="rs-secret" type="password" labelText="Signing secret (optional)" placeholder="for X-Signature"
            value={secret} onChange={(e) => setSecret(e.target.value)} />
        </div>
        <Button size="md" onClick={create} disabled={busy}>Subscribe</Button>
      </div>

      {msg && (
        <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', marginBottom: '0.5rem' }}>{msg}</p>
      )}

      {subs === null ? (
        <InlineLoading description="Loading subscriptions…" />
      ) : subs.length === 0 ? (
        <p style={{ fontSize: '0.85rem', color: 'var(--cds-text-secondary)' }}>No report subscriptions yet.</p>
      ) : (
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
              <th style={{ padding: '0.3rem 0.5rem' }}>Report</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Cadence</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Next due</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Last run</th>
              <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {subs.map((s) => (
              <Fragment key={s.id}>
                <tr style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                  <td style={{ padding: '0.3rem 0.5rem' }}>
                    {REPORT_KINDS.find(([v]) => v === s.report)?.[1] || s.report}
                    {s.target_url && <Tag type="blue" size="sm" style={{ marginLeft: '0.4rem' }}>webhook</Tag>}
                  </td>
                  <td style={{ padding: '0.3rem 0.5rem' }}>{s.cadence}</td>
                  <td style={{ padding: '0.3rem 0.5rem', color: 'var(--cds-text-secondary)' }}>{fmtWhen(s.next_due)}</td>
                  <td style={{ padding: '0.3rem 0.5rem' }}>
                    {s.last_run ? (
                      <>
                        {fmtWhen(s.last_run.generated_at)} {deliveredTag(s.last_run.delivered)}
                      </>
                    ) : (
                      <span style={{ color: 'var(--cds-text-secondary)' }}>never</span>
                    )}
                  </td>
                  <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right', whiteSpace: 'nowrap' }}>
                    <Button kind="ghost" size="sm" onClick={() => runNow(s.id)} disabled={busy}>Run now</Button>
                    <Button kind="ghost" size="sm" onClick={() => viewRuns(s.id)}>{runsFor === s.id ? 'Hide' : 'Runs'}</Button>
                    <Button kind="danger--ghost" size="sm" onClick={() => remove(s.id)}>Delete</Button>
                  </td>
                </tr>
                {runsFor === s.id && (
                  <tr>
                    <td colSpan={5} style={{ padding: '0.25rem 0.5rem 0.75rem', background: 'var(--cds-layer-accent-01)' }}>
                      {runs.length === 0 ? (
                        <span style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)' }}>No runs yet — use “Run now”.</span>
                      ) : (
                        runs.map((run) => (
                          <details key={run.id} style={{ fontSize: '0.78rem', marginBottom: '0.35rem' }}>
                            <summary style={{ cursor: 'pointer' }}>
                              {fmtWhen(run.generated_at)} · {REPORT_KINDS.find(([v]) => v === run.report)?.[1] || run.report} {deliveredTag(run.delivered)}
                            </summary>
                            <div style={{ margin: '0.5rem 0 0', padding: '0.65rem', background: 'var(--cds-layer)' }}>
                              <ReportView report={run.report} summary={run.summary} />
                            </div>
                          </details>
                        ))
                      )}
                    </td>
                  </tr>
                )}
              </Fragment>
            ))}
          </tbody>
        </table>
      )}
    </Tile>
  )
}
