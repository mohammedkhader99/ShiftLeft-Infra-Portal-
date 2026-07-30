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
  InlineNotification,
} from '@carbon/react'
import { WarningAltFilled } from '@carbon/icons-react'
import { getMe, getRequests, getAudit, type RequestRow } from '../api'
import { workflowSteps, fmtWhen, type WFStep } from '../workflow'

const FILTER_KEYS = ['status', 'request_type', 'technology', 'deployment_target', 'created_week', 'requested_by', 'subsidiary']
const OVERSIGHT = ['platform_admin', 'auditor', 'finops']

type BadgeType = 'green' | 'teal' | 'blue' | 'cyan' | 'red' | 'gray' | 'cool-gray'

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

function MetaField({ label, value }: { label: string; value?: string | null }) {
  if (!value) return null
  return (
    <div>
      <span style={{ color: 'var(--cds-text-secondary)' }}>{label}: </span>
      {value}
    </div>
  )
}

function parseQuery(route: string): Record<string, string> {
  const i = route.indexOf('?')
  return i < 0 ? {} : Object.fromEntries(new URLSearchParams(route.slice(i + 1)))
}

export default function MyRequests({ route }: { route: string }) {
  const [me, setMe] = useState<{ email: string; roles: string[] } | null>(null)
  const [rows, setRows] = useState<RequestRow[]>([])
  const [loaded, setLoaded] = useState(false)
  const [expanded, setExpanded] = useState<Set<string>>(new Set())
  const [steps, setSteps] = useState<Record<string, WFStep[]>>({})

  const query = parseQuery(route)
  const filters: Record<string, string> = {}
  for (const k of FILTER_KEYS) if (query[k]) filters[k] = query[k]
  const oversight = !!me && me.roles.some((r) => OVERSIGHT.includes(r))
  const estate = query.scope === 'all' && oversight
  const filterActive = estate || Object.keys(filters).length > 0

  // Column widths sum to ~96% so the expand chevron column (which takes the
  // leftover) stays slim (~4%), and the table fills the width without scrolling.
  const cw = estate
    ? { ref: '12%', status: '18%', sub: '12%', env: '11%', comp: '19%', mon: '11%', jira: '13%' }
    : { ref: '14%', status: '22%', sub: '0%', env: '13%', comp: '23%', mon: '11%', jira: '13%' }

  useEffect(() => {
    getMe().then(setMe)
  }, [])

  useEffect(() => {
    if (!me) return
    const params = estate ? { ...filters } : { requester: me.email, ...filters }
    let active = true
    const load = () =>
      getRequests(params)
        .then((rs) => active && setRows(rs))
        .catch(() => {})
        .finally(() => active && setLoaded(true))
    load()
    const id = setInterval(load, 8000)
    return () => {
      active = false
      clearInterval(id)
    }
    // route captures scope + all filters
  }, [me, route])

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

  if (!loaded) return <InlineLoading description="Loading requests…" />

  const banner = filterActive && (
    <div
      style={{
        display: 'flex',
        justifyContent: 'space-between',
        alignItems: 'center',
        flexWrap: 'wrap',
        gap: '1rem',
        background: 'var(--cds-layer-accent)',
        padding: '0.5rem 0.9rem',
        marginBottom: '0.75rem',
        fontSize: '0.85rem',
      }}
    >
      <span>
        {Object.keys(filters).length > 0 && (
          <>
            Filtered by{' '}
            {Object.entries(filters).map(([k, v], i) => (
              <span key={k}>
                {i > 0 && ', '}
                <strong>{k} = {v}</strong>
              </span>
            ))}{' '}
            ·{' '}
          </>
        )}
        {rows.length} result{rows.length === 1 ? '' : 's'}
      </span>
      <span>
        {estate && (
          <a href="#/overview" style={{ marginRight: '1rem' }}>
            ← Back to overview
          </a>
        )}
        <a href="#/requests">Clear filter</a>
      </span>
    </div>
  )

  return (
    <div>
      {banner}
      {rows.length === 0 ? (
        <p style={{ color: 'var(--cds-text-secondary)' }}>No matching requests.</p>
      ) : (
        <div style={{ overflowX: 'auto', maxWidth: '100%' }}>
        <TableContainer title={estate ? 'Estate requests' : 'My requests'} description="Live — updates automatically.">
          <Table size="sm" style={{ tableLayout: 'fixed', width: '100%' }}>
            <TableHead>
              <TableRow>
                <TableExpandHeader aria-label="Expand row" />
                <TableHeader style={{ width: cw.ref }}>Reference</TableHeader>
                <TableHeader style={{ width: cw.status }}>Status</TableHeader>
                {estate && <TableHeader style={{ width: cw.sub }}>Submitted by</TableHeader>}
                <TableHeader style={{ width: cw.env }}>Environment</TableHeader>
                <TableHeader style={{ width: cw.comp }}>Components</TableHeader>
                <TableHeader style={{ width: cw.mon }}>Monthly</TableHeader>
                <TableHeader style={{ width: cw.jira }}>Jira ticket</TableHeader>
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
                      <strong style={{ whiteSpace: 'nowrap' }}>{r.reference}</strong>
                    </TableCell>
                    <TableCell>
                      <Tag type={badgeType(r.status)} size="sm">
                        {r.status}
                      </Tag>
                      {r.approval_sla && (
                        <div>
                          <Tag
                            type={r.approval_sla.status === 'breached' ? 'red' : r.approval_sla.status === 'due-soon' ? 'purple' : 'green'}
                            size="sm"
                            title={`Awaiting approval · due ${r.approval_sla.due_at.slice(0, 16).replace('T', ' ')}`}
                          >
                            {Math.round(r.approval_sla.elapsed_hours)}h / {Math.round(r.approval_sla.sla_hours)}h · {r.approval_sla.status}
                          </Tag>
                        </div>
                      )}
                      {r.status_detail && (
                        <div
                          style={{ display: 'flex', alignItems: 'center', gap: '0.25rem', marginTop: '0.25rem', maxWidth: '100%', color: 'var(--cds-text-error)', fontSize: '0.75rem' }}
                          title={r.status_detail}
                        >
                          <WarningAltFilled size={14} style={{ flexShrink: 0 }} />
                          <span style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>{r.status_detail}</span>
                        </div>
                      )}
                    </TableCell>
                    {estate && (
                      <TableCell>
                        <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.requester_name || r.requester}>
                          {r.requester_name || r.requester}
                        </div>
                      </TableCell>
                    )}
                    <TableCell>
                      <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={r.environment_name || r.target_environment || undefined}>
                        {r.environment_name || r.target_environment || '—'}
                      </div>
                    </TableCell>
                    <TableCell>
                      <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }} title={componentsText(r)}>
                        {componentsText(r)}
                      </div>
                    </TableCell>
                    <TableCell>
                      {r.estimate ? `${r.estimate.monthly.toFixed(2)} ${r.estimate.currency}` : '—'}
                    </TableCell>
                    <TableCell>
                      <div style={{ overflow: 'hidden', textOverflow: 'ellipsis', whiteSpace: 'nowrap' }}>
                        {r.approval?.jira_key ? (
                          r.approval.ticket_url ? (
                            <a href={r.approval.ticket_url} target="_blank" rel="noopener noreferrer" title={r.approval.jira_key}>
                              {r.approval.jira_key}
                            </a>
                          ) : (
                            r.approval.jira_key
                          )
                        ) : (
                          '—'
                        )}
                      </div>
                    </TableCell>
                  </TableExpandRow>
                  <TableExpandedRow colSpan={estate ? 8 : 7}>
                    <div style={{ padding: '1rem 0.5rem' }}>
                      {r.status_detail && (
                        <InlineNotification
                          kind={r.status.endsWith('failed') || /blocked|not found|failed/i.test(r.status_detail) ? 'error' : 'warning'}
                          lowContrast
                          hideCloseButton
                          title={r.status.endsWith('failed') ? 'This request failed' : 'This request is held up'}
                          subtitle={r.status_detail}
                          style={{ maxWidth: 'none', marginBottom: '1rem' }}
                        />
                      )}
                      {(r.priority || r.business_justification) && (
                        <div
                          style={{
                            marginBottom: '1.25rem',
                            fontSize: '0.85rem',
                            display: 'grid',
                            gridTemplateColumns: 'repeat(auto-fit, minmax(12rem, 1fr))',
                            gap: '0.4rem 1.5rem',
                          }}
                        >
                          <MetaField label="Tier" value={r.environment_tier} />
                          <MetaField label="Priority" value={r.priority} />
                          <MetaField label="Criticality" value={r.business_criticality} />
                          <MetaField label="Required by" value={r.required_delivery_date} />
                          <MetaField label="App owner" value={r.application_owner} />
                          <MetaField label="Tech owner" value={r.technical_owner} />
                          <MetaField label="Business owner" value={r.business_owner} />
                          <MetaField label="Env owner" value={r.environment_owner} />
                          {r.business_justification && (
                            <div style={{ gridColumn: '1 / -1' }}>
                              <span style={{ color: 'var(--cds-text-secondary)' }}>Justification: </span>
                              {r.business_justification}
                            </div>
                          )}
                          {r.advanced_options && Object.keys(r.advanced_options).length > 0 && (
                            <div style={{ gridColumn: '1 / -1' }}>
                              <span style={{ color: 'var(--cds-text-secondary)' }}>Advanced: </span>
                              {Object.entries(r.advanced_options)
                                .map(([k, v]) => `${k.replace(/_/g, ' ')}: ${v === true ? 'yes' : String(v)}`)
                                .join('  ·  ')}
                            </div>
                          )}
                        </div>
                      )}
                      {steps[r.reference] ? (
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
                      ) : (
                        <InlineLoading description="Loading workflow…" />
                      )}
                      <div style={{ marginTop: '1rem', fontSize: '0.85rem' }}>
                        <a href={`/api/requests/${r.reference}/costsheet.xlsx`}>
                          ↓ Download cost sheet (Excel)
                        </a>
                      </div>
                    </div>
                  </TableExpandedRow>
                </Fragment>
              ))}
            </TableBody>
          </Table>
        </TableContainer>
        </div>
      )}
    </div>
  )
}
