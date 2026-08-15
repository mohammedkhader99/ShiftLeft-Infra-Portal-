import { useEffect, useState } from 'react'
import { Tile, InlineLoading, Tag } from '@carbon/react'
import { getStats, getAnomalies, type Stats, type Breakdown, type Anomalies } from '../api'

function sevColor(sev: string): 'red' | 'purple' | 'gray' {
  return sev === 'high' ? 'red' : sev === 'medium' ? 'purple' : 'gray'
}

function drill(params: Record<string, string>): string {
  const qs = new URLSearchParams({ scope: 'all', ...params }).toString()
  return `#/requests?${qs}`
}

function Kpi({ value, label, href, color }: { value: string; label: string; href: string; color?: string }) {
  return (
    <a href={href} style={{ textDecoration: 'none' }}>
      <Tile style={{ borderTop: '3px solid var(--cds-border-interactive)', cursor: 'pointer' }}>
        <div style={{ fontSize: '2rem', fontWeight: 300, color: color || 'var(--cds-text-primary)' }}>{value}</div>
        <div style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)' }}>{label}</div>
      </Tile>
    </a>
  )
}

function Bars({ title, items, param }: { title: string; items: Breakdown[]; param: string }) {
  const max = Math.max(1, ...items.map((i) => i.count))
  return (
    <Tile>
      <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.75rem' }}>{title}</h4>
      {items.length ? (
        items.map((it) => (
          <a
            key={it.key}
            href={drill({ [param]: it.key })}
            title={`Show the ${it.count} request(s) — ${it.key}`}
            style={{
              display: 'flex',
              alignItems: 'center',
              gap: '0.6rem',
              padding: '0.2rem 0.25rem',
              fontSize: '0.85rem',
              textDecoration: 'none',
              color: 'var(--cds-text-primary)',
            }}
          >
            <span style={{ flex: '0 0 8rem', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap', color: 'var(--cds-text-secondary)' }}>
              {it.key}
            </span>
            <span style={{ flex: 1, background: 'var(--cds-layer-accent)', height: '0.85rem' }}>
              <span style={{ display: 'block', width: `${(it.count / max) * 100}%`, height: '100%', background: 'var(--cds-interactive)' }} />
            </span>
            <span style={{ flex: '0 0 2rem', textAlign: 'right', fontWeight: 500 }}>{it.count}</span>
          </a>
        ))
      ) : (
        <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.85rem' }}>No data yet.</p>
      )}
    </Tile>
  )
}

export default function Overview() {
  const [stats, setStats] = useState<Stats | null>(null)
  const [anomalies, setAnomalies] = useState<Anomalies | null>(null)
  const [forbidden, setForbidden] = useState(false)
  const [loaded, setLoaded] = useState(false)

  useEffect(() => {
    let active = true
    const load = () => {
      getStats()
        .then((s) => {
          if (!active) return
          if (s === 'forbidden') setForbidden(true)
          else if (s) setStats(s)
        })
        .catch(() => {})
        .finally(() => active && setLoaded(true))
      getAnomalies()
        .then((a) => active && a && a !== 'forbidden' && setAnomalies(a))
        .catch(() => {})
    }
    load()
    const id = setInterval(load, 10000)
    return () => {
      active = false
      clearInterval(id)
    }
  }, [])

  if (!loaded) return <InlineLoading description="Loading the estate overview…" />
  if (forbidden)
    return (
      <Tile>
        <p style={{ color: 'var(--cds-text-secondary)' }}>
          This dashboard is for oversight roles (platform admin, auditor, finops). Your account
          doesn't have access.
        </p>
      </Tile>
    )
  if (!stats) return <p style={{ color: 'var(--cds-text-error)' }}>Couldn't load the overview.</p>

  const k = stats.kpis
  const maxT = Math.max(1, ...stats.trend.map((t) => t.count))

  return (
    <div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(9rem, 1fr))', gap: '0.75rem', marginBottom: '1rem' }}>
        <Kpi value={String(k.total)} label="Total requests" href={drill({})} />
        <Kpi value={String(k.active)} label="Active" color="var(--cds-support-success)" href={drill({ status: 'provisioned' })} />
        <Kpi value={String(k.in_flight)} label="In flight" color="var(--cds-support-warning)" href={drill({ status: 'submitted,planned,in-progress' })} />
        <Kpi value={String(k.breaching_sla)} label="Approvals breaching SLA" color="var(--cds-support-error)" href={drill({ status: 'submitted' })} />
        <Kpi value={String(k.failed)} label="Failed" color="var(--cds-support-error)" href={drill({ status: 'apply-failed,teardown-failed,rejected,verify-failed' })} />
        <Kpi value={String(k.decommissioned)} label="Decommissioned" href={drill({ status: 'decommissioned' })} />
        <Kpi value={`${stats.active_monthly_cost.amount.toFixed(0)} ${stats.active_monthly_cost.currency}`} label="Active monthly cost" href={drill({ status: 'provisioned' })} />
      </div>

      {anomalies && anomalies.count > 0 && (
        <Tile style={{ marginBottom: '1rem', borderLeft: '3px solid var(--cds-support-error)' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'baseline', marginBottom: '0.6rem', flexWrap: 'wrap', gap: '0.5rem' }}>
            <h4 style={{ fontSize: '0.95rem', fontWeight: 500 }}>Anomalies to review (F-FIN-09 / F-RPT-10)</h4>
            <span style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)' }}>
              {anomalies.by_severity.high} high · {anomalies.by_severity.medium} medium · {anomalies.by_severity.low} low
            </span>
          </div>
          <div style={{ display: 'flex', flexDirection: 'column', gap: '0.35rem' }}>
            {anomalies.anomalies.slice(0, 12).map((a, i) => (
              <div key={i} style={{ display: 'flex', gap: '0.6rem', alignItems: 'baseline', fontSize: '0.85rem' }}>
                <Tag type={sevColor(a.severity)} size="sm" style={{ flexShrink: 0 }}>{a.severity}</Tag>
                <span style={{ flex: '0 0 9rem', color: 'var(--cds-text-secondary)', overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={a.reference || a.subject}>
                  {a.reference || a.subject}
                </span>
                <span style={{ flex: 1 }}>{a.signal}</span>
              </div>
            ))}
          </div>
          {anomalies.count > 12 && (
            <p style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
              …and {anomalies.count - 12} more.
            </p>
          )}
        </Tile>
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(20rem, 1fr))', gap: '1rem' }}>
        <Bars title="By requester" items={stats.by_requester} param="requested_by" />
        <Bars title="By subsidiary" items={stats.by_subsidiary} param="subsidiary" />
        <Bars title="By status" items={stats.by_status} param="status" />
        <Bars title="By request type" items={stats.by_type} param="request_type" />
        <Bars title="By technology" items={stats.by_technology} param="technology" />
        <Bars title="By deployment target" items={stats.by_target} param="deployment_target" />
      </div>

      <Tile style={{ marginTop: '1rem' }}>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.75rem' }}>Requests per week</h4>
        {stats.trend.length ? (
          <div style={{ display: 'flex', alignItems: 'flex-end', gap: '0.5rem', height: '9rem' }}>
            {stats.trend.map((t) => (
              <a
                key={t.week}
                href={drill({ created_week: t.week })}
                title={`${t.week}: ${t.count} request(s)`}
                style={{ flex: 1, display: 'flex', flexDirection: 'column', alignItems: 'center', justifyContent: 'flex-end', height: '100%', textDecoration: 'none', color: 'var(--cds-text-primary)' }}
              >
                <span style={{ width: '70%', minHeight: '2px', height: `${(t.count / maxT) * 100}%`, background: 'var(--cds-interactive)' }} />
                <span style={{ fontSize: '0.72rem', fontWeight: 500, marginTop: '0.2rem' }}>{t.count}</span>
                <span style={{ fontSize: '0.68rem', color: 'var(--cds-text-secondary)' }}>{t.week.slice(5)}</span>
              </a>
            ))}
          </div>
        ) : (
          <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.85rem' }}>No data yet.</p>
        )}
      </Tile>
    </div>
  )
}
