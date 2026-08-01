// Shared reporting UI (F-RPT-11): the formatted report renderer and the
// subscriptions manager, used by both the Reports page and (formerly) Showback.
import { Fragment, useEffect, useState } from 'react'
import { Tile, InlineLoading, Select, SelectItem, Tag, Button, TextInput } from '@carbon/react'
import { Download, Printer } from '@carbon/icons-react'
import {
  getReportSubscriptions,
  createReportSubscription,
  deleteReportSubscription,
  runReportNow,
  getReportRuns,
  type ReportSubRow,
  type ReportRunRow,
} from '../api'
import { reportToCsv, printReport, downloadFile, dateStamp } from './reportExport'

// The report kinds + cadences the API accepts.
export const REPORT_KINDS: [string, string][] = [
  ['forecast', 'Spend forecast'],
  ['anomalies', 'Cost anomalies'],
  ['estate', 'Estate summary'],
]
export const CADENCES: [string, string][] = [
  ['daily', 'Daily'],
  ['weekly', 'Weekly'],
  ['monthly', 'Monthly'],
]

export function money(n: number, currency: string) {
  return `${n.toLocaleString(undefined, { maximumFractionDigits: 0 })} ${currency}`
}

export function fmtWhen(iso?: string | null) {
  if (!iso) return '—'
  const d = new Date(iso)
  return isNaN(d.getTime()) ? '—' : d.toLocaleString(undefined, { dateStyle: 'medium', timeStyle: 'short' })
}

function sevTag(sev: string) {
  const type = sev === 'high' ? 'red' : sev === 'medium' ? 'magenta' : 'cool-gray'
  return <Tag type={type as any} size="sm">{sev}</Tag>
}

// The exact stored JSON, one click away under every formatted report.
export function RawData({ data }: { data: any }) {
  return (
    <details style={{ marginTop: '0.6rem' }}>
      <summary style={{ cursor: 'pointer', fontSize: '0.72rem', color: 'var(--cds-text-secondary)' }}>Raw data</summary>
      <pre style={{ margin: '0.35rem 0 0', padding: '0.5rem', overflowX: 'auto', background: 'var(--cds-layer-accent-01)', fontSize: '0.72rem' }}>
        {JSON.stringify(data, null, 2)}
      </pre>
    </details>
  )
}

// Render a report (a stored run or an on-demand one) as a readable report per
// kind, not raw JSON.
export function ReportView({ report, summary }: { report: string; summary: any }) {
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

// Scheduled report subscriptions (F-RPT-11). Oversight-only; the host page
// already gates on that, so if the API says 'forbidden' we render nothing.
export function ReportSubscriptions() {
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
                              <div style={{ display: 'flex', justifyContent: 'flex-end', gap: '0.5rem', marginBottom: '0.4rem' }}>
                                <Button kind="ghost" size="sm" renderIcon={Download}
                                  onClick={() => downloadFile(`report-${run.report}-run${run.id}.csv`, reportToCsv(run.report, run.summary, run.generated_at), 'text/csv;charset=utf-8')}>CSV</Button>
                                <Button kind="ghost" size="sm" renderIcon={Printer}
                                  onClick={() => { if (!printReport(run.report, run.summary, run.generated_at)) alert('Please allow pop-ups for this site to export the report as PDF.') }}>PDF</Button>
                              </div>
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
