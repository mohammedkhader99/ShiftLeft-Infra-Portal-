import { useEffect, useState } from 'react'
import { Tile, InlineLoading, Select, SelectItem, Button } from '@carbon/react'
import { getReport } from '../api'
import { REPORT_KINDS, ReportView, ReportSubscriptions, fmtWhen } from '../components/reporting'

// Reports hub (F-RPT-11): view any report on demand, and manage the scheduled
// subscriptions. Oversight-only — the API gates every call; if it refuses, we
// show a friendly note.
export default function ReportsPage() {
  const [kind, setKind] = useState('forecast')
  const [data, setData] = useState<any | null>(null)
  const [generatedAt, setGeneratedAt] = useState<string | null>(null)
  const [loading, setLoading] = useState(false)
  const [forbidden, setForbidden] = useState(false)
  const [error, setError] = useState(false)

  const view = (k: string) => {
    setLoading(true)
    setError(false)
    getReport(k)
      .then((r) => {
        if (r === 'forbidden') setForbidden(true)
        else if (r) {
          setData(r.data)
          setGeneratedAt(r.generated_at)
          setForbidden(false)
        } else setError(true)
      })
      .catch(() => setError(true))
      .finally(() => setLoading(false))
  }

  // Load the default report once on mount.
  useEffect(() => {
    view('forecast')
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [])

  if (forbidden)
    return (
      <Tile>
        <p style={{ color: 'var(--cds-text-secondary)' }}>
          Reports are for oversight roles (platform admin, auditor, finops). Your account doesn't
          have access.
        </p>
      </Tile>
    )

  const kindLabel = REPORT_KINDS.find(([v]) => v === kind)?.[1] || kind

  return (
    <div>
      <Tile>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.5rem' }}>View a report now</h4>
        <p style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', marginBottom: '0.85rem' }}>
          Generate a report on demand from live data — read-only, nothing is stored. To have it sent
          on a schedule, add a subscription below.
        </p>
        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end', flexWrap: 'wrap', marginBottom: '1rem' }}>
          <div style={{ minWidth: '12rem' }}>
            <Select id="report-kind" labelText="Report" value={kind} onChange={(e) => setKind(e.target.value)}>
              {REPORT_KINDS.map(([v, l]) => (
                <SelectItem key={v} value={v} text={l} />
              ))}
            </Select>
          </div>
          <Button size="md" onClick={() => view(kind)} disabled={loading}>View report</Button>
          {generatedAt && !loading && (
            <span style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)' }}>
              generated {fmtWhen(generatedAt)}
            </span>
          )}
        </div>

        {loading ? (
          <InlineLoading description="Generating report…" />
        ) : error ? (
          <p style={{ color: 'var(--cds-text-error)', fontSize: '0.85rem' }}>Couldn't generate the report.</p>
        ) : data ? (
          <div style={{ padding: '0.75rem', background: 'var(--cds-layer)' }}>
            <div style={{ fontSize: '0.85rem', fontWeight: 500, marginBottom: '0.5rem' }}>{kindLabel}</div>
            <ReportView report={kind} summary={data} />
          </div>
        ) : null}
      </Tile>

      <ReportSubscriptions />
    </div>
  )
}
