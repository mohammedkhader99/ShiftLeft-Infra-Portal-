import { Fragment, useEffect, useState } from 'react'
import {
  TableContainer,
  Table,
  TableHead,
  TableRow,
  TableHeader,
  TableBody,
  TableCell,
  TableExpandHeader,
  TableExpandRow,
  TableExpandedRow,
  Tag,
  ProgressIndicator,
  ProgressStep,
  InlineLoading,
} from '@carbon/react'
import { getMe, getRequests, getAudit, type RequestRow } from '../api'
import { workflowSteps, fmtWhen, type WFStep } from '../workflow'

type BadgeType =
  | 'green' | 'teal' | 'blue' | 'cyan' | 'red' | 'gray' | 'cool-gray'

function badgeType(status: string): BadgeType {
  if (status === 'provisioned') return 'green'
  if (status === 'in-progress') return 'teal'
  if (status === 'planned') return 'blue'
  if (status === 'submitted') return 'cyan'
  if (status.endsWith('failed') || status === 'rejected') return 'red'
  if (status === 'decommissioned') return 'gray'
  return 'cool-gray'
}

function componentsText(r: RequestRow): string {
  const parts = r.components
    .filter((c) => c.technology_code || c.size)
    .map((c) => `${c.technology_code ?? '?'} (${c.size ?? '?'})`)
  return parts.length ? parts.join(', ') : '—'
}

export default function MyRequests() {
  const [email, setEmail] = useState<string | null>(null)
  const [rows, setRows] = useState<RequestRow[]>([])
  const [loaded, setLoaded] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [steps, setSteps] = useState<Record<string, WFStep[]>>({})

  useEffect(() => {
    getMe().then((m) => setEmail(m?.email ?? null))
  }, [])

  // Poll the list so status badges update live (no F5).
  useEffect(() => {
    if (!email) return
    let active = true
    const load = () =>
      getRequests(email)
        .then((rs) => active && setRows(rs))
        .catch(() => {})
        .finally(() => active && setLoaded(true))
    load()
    const id = setInterval(load, 8000)
    return () => {
      active = false
      clearInterval(id)
    }
  }, [email])

  // Keep the workflow of every expanded row fresh as the list polls.
  useEffect(() => {
    expanded.forEach((ref) => {
      const status = rows.find((r) => r.reference === ref)?.status ?? ''
      getAudit(ref)
        .then((a) => setSteps((s) => ({ ...s, [ref]: workflowSteps(status, a) })))
        .catch(() => {})
    })
  }, [expanded, rows])

  function toggle(ref: string) {
    setExpanded((s) => {
      const next = new Set(s)
      next.has(ref) ? next.delete(ref) : next.add(ref)
      return next
    })
  }

  if (!loaded) return <InlineLoading description="Loading your requests…" />

  if (rows.length === 0) {
    return (
      <p style={{ color: 'var(--cds-text-secondary)' }}>
        You haven't raised any requests yet.
      </p>
    )
  }

  return (
    <TableContainer title="My requests" description="Live — updates automatically.">
      <Table>
        <TableHead>
          <TableRow>
            <TableExpandHeader aria-label="Expand row" />
            <TableHeader>Reference</TableHeader>
            <TableHeader>Status</TableHeader>
            <TableHeader>Submitted by</TableHeader>
            <TableHeader>Environment</TableHeader>
            <TableHeader>Components</TableHeader>
            <TableHeader>Monthly</TableHeader>
            <TableHeader>Jira ticket</TableHeader>
          </TableRow>
        </TableHead>
        <TableBody>
          {rows.map((r) => (
            <Fragment key={r.reference}>
              <TableExpandRow
                aria-label={`Toggle workflow for ${r.reference}`}
                isExpanded={expanded.has(r.reference)}
                onExpand={() => toggle(r.reference)}
              >
                <TableCell>
                  <strong>{r.reference}</strong>
                </TableCell>
                <TableCell>
                  <Tag type={badgeType(r.status)} size="sm">
                    {r.status}
                  </Tag>
                </TableCell>
                <TableCell>{r.requester_name || r.requester}</TableCell>
                <TableCell>{r.environment_name || r.target_environment || '—'}</TableCell>
                <TableCell>{componentsText(r)}</TableCell>
                <TableCell>
                  {r.estimate ? `${r.estimate.monthly.toFixed(2)} ${r.estimate.currency}` : '—'}
                </TableCell>
                <TableCell>
                  {r.approval?.jira_key ? (
                    r.approval.ticket_url ? (
                      <a href={r.approval.ticket_url} target="_blank" rel="noopener noreferrer">
                        {r.approval.jira_key}
                      </a>
                    ) : (
                      r.approval.jira_key
                    )
                  ) : (
                    '—'
                  )}
                </TableCell>
              </TableExpandRow>
              <TableExpandedRow colSpan={8}>
                {steps[r.reference] ? (
                  <div style={{ padding: '1rem 0.5rem' }}>
                    <ProgressIndicator spaceEqually>
                      {steps[r.reference].map((s) => (
                        <ProgressStep
                          key={s.label}
                          label={s.label}
                          secondaryLabel={fmtWhen(s.when)}
                          complete={s.state === 'done'}
                          current={s.state === 'current'}
                          invalid={s.state === 'failed'}
                        />
                      ))}
                    </ProgressIndicator>
                  </div>
                ) : (
                  <InlineLoading description="Loading workflow…" />
                )}
              </TableExpandedRow>
            </Fragment>
          ))}
        </TableBody>
      </Table>
    </TableContainer>
  )
}
