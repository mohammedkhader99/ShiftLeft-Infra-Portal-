import { useEffect, useState, type ReactNode } from 'react'
import { Tile, Tag, TextInput, Select, SelectItem, Button, InlineLoading } from '@carbon/react'
import { TrashCan, Add } from '@carbon/icons-react'
import {
  getConfig, getBudgets, getLookups, setBudget, deleteBudget, getOrphans,
  getQuotas, setQuota, deleteQuota,
  type SystemConfig, type BudgetRow, type Lookups, type OrphanRow, type QuotaRow,
} from '../api'

function Flag({ on, onLabel, offLabel }: { on: boolean; onLabel?: string; offLabel?: string }) {
  return (
    <Tag type={on ? 'green' : 'gray'} size="sm">
      {on ? onLabel || 'on' : offLabel || 'off'}
    </Tag>
  )
}

function Row({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '1rem', padding: '0.3rem 0', fontSize: '0.85rem', borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
      <span style={{ color: 'var(--cds-text-secondary)' }}>{label}</span>
      <span style={{ textAlign: 'right' }}>{children}</span>
    </div>
  )
}

function Group({ title, children }: { title: string; children: ReactNode }) {
  return (
    <div>
      <div style={{ fontWeight: 500, fontSize: '0.8rem', margin: '0.75rem 0 0.25rem' }}>{title}</div>
      {children}
    </div>
  )
}

const money = (n: number) => `${n.toLocaleString(undefined, { maximumFractionDigits: 0 })} AED`

export default function Admin() {
  const [cfg, setCfg] = useState<SystemConfig | null>(null)
  const [budgets, setBudgets] = useState<BudgetRow[]>([])
  const [lookups, setLookups] = useState<Lookups | null>(null)
  const [orphans, setOrphans] = useState<OrphanRow[]>([])
  const [forbidden, setForbidden] = useState(false)
  const [loaded, setLoaded] = useState(false)
  const [newCC, setNewCC] = useState('')
  const [newLimit, setNewLimit] = useState('')
  const [quotas, setQuotas] = useState<QuotaRow[]>([])
  const [newProject, setNewProject] = useState('')
  const [newMax, setNewMax] = useState('')

  function reloadBudgets() {
    getBudgets().then((b) => b && b !== 'forbidden' && setBudgets(b.budgets)).catch(() => {})
  }
  function reloadQuotas() {
    getQuotas().then((q) => q && q !== 'forbidden' && setQuotas(q.quotas)).catch(() => {})
  }

  useEffect(() => {
    getConfig()
      .then((c) => {
        if (c === 'forbidden') setForbidden(true)
        else if (c) setCfg(c)
      })
      .catch(() => {})
      .finally(() => setLoaded(true))
    getLookups().then(setLookups).catch(() => {})
    reloadBudgets()
    reloadQuotas()
    getOrphans().then((o) => o && o !== 'forbidden' && setOrphans(o.orphans)).catch(() => {})
  }, [])

  async function onAddBudget() {
    const limit = parseFloat(newLimit)
    if (!newCC || !(limit > 0)) return
    await setBudget(newCC, limit)
    setNewCC('')
    setNewLimit('')
    reloadBudgets()
  }

  async function onDeleteBudget(cc: string) {
    await deleteBudget(cc)
    reloadBudgets()
  }

  async function onAddQuota() {
    const max = parseInt(newMax, 10)
    if (!newProject || !(max > 0)) return
    await setQuota(newProject, max)
    setNewProject('')
    setNewMax('')
    reloadQuotas()
  }

  async function onDeleteQuota(project: string) {
    await deleteQuota(project)
    reloadQuotas()
  }

  if (!loaded) return <InlineLoading description="Loading admin console…" />
  if (forbidden)
    return (
      <Tile>
        <p style={{ color: 'var(--cds-text-secondary)' }}>
          The admin console is for platform administrators. Your account doesn't have access.
        </p>
      </Tile>
    )

  return (
    <div style={{ display: 'grid', gap: '1rem' }}>
      {cfg && (
        <Tile>
          <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>System posture</h4>
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(16rem, 1fr))', gap: '0 2rem' }}>
            <Group title="Modes">
              <Row label="Auth"><Tag size="sm" type={cfg.modes.auth === 'live' ? 'blue' : 'gray'}>{cfg.modes.auth}</Tag></Row>
              <Row label="Jira"><Tag size="sm" type={cfg.modes.jira === 'live' ? 'blue' : 'gray'}>{cfg.modes.jira}</Tag></Row>
              <Row label="Auto-provision"><Flag on={cfg.modes.auto_provision} /></Row>
              <Row label="Provision mode"><Tag size="sm" type={cfg.modes.provision_mode === 'apply' ? 'teal' : 'gray'}>{cfg.modes.provision_mode}</Tag></Row>
            </Group>
            <Group title="Governance">
              <Row label="Segregation of duties"><Flag on={cfg.governance.sod_enforced} onLabel="enforced" /></Row>
              <Row label="Four-eyes"><Flag on={cfg.governance.four_eyes_enforced} onLabel="enforced" /></Row>
              <Row label="Approval quorum">{cfg.governance.approval_quorum}</Row>
              <Row label="Approval SLA">{cfg.governance.approval_sla_hours}h</Row>
              <Row label="Audit HMAC signing"><Flag on={cfg.governance.audit_hmac} /></Row>
            </Group>
            <Group title="Change window">
              <Row label="Enabled"><Flag on={cfg.change_window.enabled} /></Row>
              {cfg.change_window.enabled && (
                <Row label="Window">{cfg.change_window.days} {cfg.change_window.start}–{cfg.change_window.end} {cfg.change_window.tz}</Row>
              )}
              {cfg.change_window.enabled && (
                <Row label="Open now"><Flag on={cfg.change_window.open_now} onLabel="open" offLabel="closed" /></Row>
              )}
            </Group>
            <Group title="FinOps">
              <Row label="Non-prod TTL">{cfg.finops.ttl_days_nonprod}d (warn {cfg.finops.ttl_warn_days}d)</Row>
              <Row label="TTL auto-reclaim"><Flag on={cfg.finops.ttl_enforce} /></Row>
              <Row label="Budget enforce"><Flag on={cfg.finops.budget_enforce} /></Row>
              <Row label="Budget warn at">{cfg.finops.budget_warn_pct}%</Row>
              <Row label="Variance alert">±{cfg.finops.variance_alert_pct}%</Row>
              <Row label="Departed owners">{cfg.finops.departed_owners_count}</Row>
            </Group>
          </div>
        </Tile>
      )}

      <Tile>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.75rem' }}>Cost-centre budgets</h4>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
              <th style={{ padding: '0.3rem 0.5rem' }}>Cost centre</th>
              <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Spend</th>
              <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Limit</th>
              <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Remaining</th>
              <th style={{ padding: '0.3rem 0.5rem' }} />
            </tr>
          </thead>
          <tbody>
            {budgets.map((b) => (
              <tr key={b.cost_centre} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                <td style={{ padding: '0.3rem 0.5rem', fontWeight: 500 }}>{b.cost_centre}</td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>{money(b.current)}</td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>{money(b.limit)}</td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right', color: b.status === 'over' ? 'var(--cds-support-error)' : b.status === 'near' ? 'var(--cds-support-warning)' : 'var(--cds-support-success)' }}>
                  {money(b.remaining)}
                </td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>
                  <Button hasIconOnly kind="ghost" size="sm" renderIcon={TrashCan} iconDescription={`Delete ${b.cost_centre} budget`} onClick={() => onDeleteBudget(b.cost_centre)} />
                </td>
              </tr>
            ))}
            {budgets.length === 0 && (
              <tr><td colSpan={5} style={{ padding: '0.5rem', color: 'var(--cds-text-secondary)' }}>No budgets set.</td></tr>
            )}
          </tbody>
        </table>
        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end', marginTop: '0.75rem', flexWrap: 'wrap' }}>
          <Select id="new-cc" labelText="Cost centre" size="sm" value={newCC} onChange={(e) => setNewCC(e.target.value)} style={{ minWidth: '14rem' }}>
            <SelectItem value="" text="— select —" />
            {lookups?.cost_centres.map((c) => (
              <SelectItem key={c.code} value={c.code} text={`${c.code} — ${c.name}`} />
            ))}
          </Select>
          <TextInput id="new-limit" labelText="Monthly limit (AED)" size="sm" type="number" value={newLimit} onChange={(e) => setNewLimit(e.target.value)} style={{ maxWidth: '12rem' }} />
          <Button size="sm" renderIcon={Add} disabled={!newCC || !(parseFloat(newLimit) > 0)} onClick={onAddBudget}>
            Set budget
          </Button>
        </div>
      </Tile>

      <Tile>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.75rem' }}>Project quotas</h4>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
              <th style={{ padding: '0.3rem 0.5rem' }}>Project</th>
              <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Environments</th>
              <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Limit</th>
              <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Remaining</th>
              <th style={{ padding: '0.3rem 0.5rem' }} />
            </tr>
          </thead>
          <tbody>
            {quotas.map((q) => (
              <tr key={q.project} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                <td style={{ padding: '0.3rem 0.5rem', fontWeight: 500 }}>{q.project}</td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>{q.current}</td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>{q.limit}</td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right', color: q.status === 'over' ? 'var(--cds-support-error)' : q.status === 'near' ? 'var(--cds-support-warning)' : 'var(--cds-support-success)' }}>
                  {q.remaining}
                </td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>
                  <Button hasIconOnly kind="ghost" size="sm" renderIcon={TrashCan} iconDescription={`Delete ${q.project} quota`} onClick={() => onDeleteQuota(q.project)} />
                </td>
              </tr>
            ))}
            {quotas.length === 0 && (
              <tr><td colSpan={5} style={{ padding: '0.5rem', color: 'var(--cds-text-secondary)' }}>No quotas set.</td></tr>
            )}
          </tbody>
        </table>
        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end', marginTop: '0.75rem', flexWrap: 'wrap' }}>
          <Select id="new-project" labelText="Project" size="sm" value={newProject} onChange={(e) => setNewProject(e.target.value)} style={{ minWidth: '14rem' }}>
            <SelectItem value="" text="— select —" />
            {lookups?.projects.map((p) => (
              <SelectItem key={p.code} value={p.code} text={`${p.code} — ${p.name}`} />
            ))}
          </Select>
          <TextInput id="new-max" labelText="Max environments" size="sm" type="number" value={newMax} onChange={(e) => setNewMax(e.target.value)} style={{ maxWidth: '12rem' }} />
          <Button size="sm" renderIcon={Add} disabled={!newProject || !(parseInt(newMax, 10) > 0)} onClick={onAddQuota}>
            Set quota
          </Button>
        </div>
      </Tile>

      <Tile>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.75rem' }}>
          Orphaned environments ({orphans.length})
        </h4>
        {orphans.length ? (
          <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
            <thead>
              <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
                <th style={{ padding: '0.3rem 0.5rem' }}>Request</th>
                <th style={{ padding: '0.3rem 0.5rem' }}>Environment</th>
                <th style={{ padding: '0.3rem 0.5rem' }}>Owner</th>
                <th style={{ padding: '0.3rem 0.5rem' }}>Reason</th>
              </tr>
            </thead>
            <tbody>
              {orphans.map((o) => (
                <tr key={o.reference} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                  <td style={{ padding: '0.3rem 0.5rem' }}>
                    <a href={`#/requests?reference=${o.reference}`}>{o.reference}</a>
                  </td>
                  <td style={{ padding: '0.3rem 0.5rem' }}>{o.environment || '—'}</td>
                  <td style={{ padding: '0.3rem 0.5rem' }}>{o.owner || '—'}</td>
                  <td style={{ padding: '0.3rem 0.5rem', color: 'var(--cds-text-secondary)' }}>{o.reason}</td>
                </tr>
              ))}
            </tbody>
          </table>
        ) : (
          <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.85rem' }}>No orphaned environments.</p>
        )}
      </Tile>
    </div>
  )
}
