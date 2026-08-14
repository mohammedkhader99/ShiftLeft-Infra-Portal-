import { useEffect, useState, type ReactNode } from 'react'
import { Tile, Tag, TextInput, Select, SelectItem, Button, Toggle, InlineLoading, InlineNotification } from '@carbon/react'
import { TrashCan, Add, Password, Save } from '@carbon/icons-react'
import {
  getConfig, getBudgets, getLookups, setBudget, deleteBudget, getOrphans,
  getQuotas, setQuota, deleteQuota,
  getApiKeys, createApiKey, revokeApiKey,
  getShutdown, setShutdownPolicy,
  getWebhooks, createWebhook, deleteWebhook, testWebhook,
  getRoleMap, setRoleMap, deleteRoleMap, resolveAccess, getUsers,
  getProjects, saveProject, deleteProject,
  type SystemConfig, type BudgetRow, type Lookups, type OrphanRow, type QuotaRow, type ApiKeyRow,
  type Shutdown, type ShutdownPolicy, type WebhookRow, type RoleMapRow, type UserRow,
  type ProjectRow,
} from '../api'
import SettingsEditor from '../components/SettingsEditor'
import UserRolesPanel from '../components/UserRolesPanel'

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

// Projects (F-CAT-02). Defining a project here is what puts it in the request
// form's dropdown; disabling it takes it out AND refuses new requests naming it.
// Expiry is softer — it warns unless PROJECT_EXPIRY_ENFORCED is on — and neither
// ever touches infrastructure the project already owns.
function ProjectsPanel() {
  const [rows, setRows] = useState<ProjectRow[]>([])
  const [busy, setBusy] = useState(false)
  const [err, setErr] = useState<string | null>(null)
  const [editing, setEditing] = useState<Partial<ProjectRow> | null>(null)

  function reload() {
    getProjects().then((p) => p && p !== 'forbidden' && setRows(p.projects)).catch(() => {})
  }
  useEffect(reload, [])

  async function save(p: Partial<ProjectRow>) {
    setBusy(true); setErr(null)
    try {
      const r = await saveProject({
        code: (p.code || '').trim(), name: (p.name || '').trim(),
        description: p.description || null, owner_email: p.owner_email || null,
        cost_centre_code: p.cost_centre_code || null,
        expires_at: p.expires_at || null, active: p.active !== false,
      })
      if (r.status !== 200) {
        setErr(typeof r.body?.detail === 'string' ? r.body.detail : 'Could not save the project.')
        return
      }
      setEditing(null); reload()
    } finally { setBusy(false) }
  }

  async function remove(code: string) {
    setBusy(true); setErr(null)
    try {
      const r = await deleteProject(code)
      // 409 means it is referenced — the message names disabling as the way out.
      if (r.status !== 200) setErr(typeof r.body?.detail === 'string' ? r.body.detail : 'Could not delete.')
      else reload()
    } finally { setBusy(false) }
  }

  const expiryTag = (p: ProjectRow) => {
    if (p.expiry_status === 'expired') return <Tag type="red" size="sm">expired</Tag>
    if (p.expiry_status === 'expiring') return <Tag type="magenta" size="sm">{p.days_left}d left</Tag>
    if (p.expires_at) return <Tag type="gray" size="sm">{p.expires_at}</Tag>
    return <span style={{ color: 'var(--cds-text-secondary)' }}>—</span>
  }

  const cell = { padding: '0.3rem 0.5rem' } as const

  return (
    <Tile>
      <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>Projects</h4>
      <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', marginBottom: '0.75rem' }}>
        A project must be defined here before it can be chosen on a request. Disabling one removes it
        from the form and refuses new requests naming it; it never touches environments the project
        already owns.
      </p>

      {err && (
        <InlineNotification kind="error" lowContrast title="" subtitle={err}
          onCloseButtonClick={() => setErr(null)} style={{ marginBottom: '0.75rem' }} />
      )}

      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
        <thead>
          <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
            <th style={cell}>Code</th>
            <th style={cell}>Name</th>
            <th style={cell}>Owner</th>
            <th style={cell}>Expires</th>
            <th style={{ ...cell, textAlign: 'right' }}>In use</th>
            <th style={cell}>Enabled</th>
            <th style={cell} />
          </tr>
        </thead>
        <tbody>
          {rows.map((p) => (
            <tr key={p.code} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)', opacity: p.active ? 1 : 0.55 }}>
              <td style={{ ...cell, fontWeight: 500 }}>{p.code}</td>
              <td style={cell}>{p.name}</td>
              <td style={cell}>{p.owner_email || <span style={{ color: 'var(--cds-support-warning)' }}>no owner</span>}</td>
              <td style={cell}>{expiryTag(p)}</td>
              <td style={{ ...cell, textAlign: 'right' }} title="requests / environments">
                {p.request_count} / {p.environment_count}
              </td>
              <td style={cell}>
                <Toggle id={`proj-${p.code}`} size="sm" hideLabel labelText=""
                  toggled={p.active} disabled={busy}
                  onToggle={(on: boolean) => save({ ...p, active: on })} />
              </td>
              <td style={{ ...cell, textAlign: 'right', whiteSpace: 'nowrap' }}>
                <Button kind="ghost" size="sm" disabled={busy} onClick={() => setEditing(p)}>Edit</Button>
                <Button hasIconOnly kind="ghost" size="sm" renderIcon={TrashCan}
                  iconDescription={`Delete ${p.code}`} disabled={busy}
                  onClick={() => remove(p.code)} />
              </td>
            </tr>
          ))}
          {rows.length === 0 && (
            <tr><td colSpan={7} style={{ padding: '0.5rem', color: 'var(--cds-text-secondary)' }}>No projects defined.</td></tr>
          )}
        </tbody>
      </table>

      <div style={{ marginTop: '0.75rem' }}>
        {!editing && (
          <Button size="sm" renderIcon={Add} disabled={busy}
            onClick={() => setEditing({ active: true })}>Add project</Button>
        )}
      </div>

      {editing && (
        <div style={{ marginTop: '0.75rem', padding: '0.75rem', border: '1px solid var(--cds-border-subtle)', borderRadius: '4px' }}>
          <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', alignItems: 'flex-end' }}>
            <TextInput id="p-code" labelText="Code" size="sm" style={{ maxWidth: '9rem' }}
              value={editing.code || ''} disabled={!!editing.created_at}
              onChange={(e) => setEditing({ ...editing, code: e.target.value })} />
            <TextInput id="p-name" labelText="Name" size="sm" style={{ maxWidth: '14rem' }}
              value={editing.name || ''}
              onChange={(e) => setEditing({ ...editing, name: e.target.value })} />
            <TextInput id="p-owner" labelText="Owner email" size="sm" style={{ maxWidth: '15rem' }}
              value={editing.owner_email || ''}
              onChange={(e) => setEditing({ ...editing, owner_email: e.target.value })} />
            <TextInput id="p-cc" labelText="Cost centre" size="sm" style={{ maxWidth: '10rem' }}
              value={editing.cost_centre_code || ''}
              onChange={(e) => setEditing({ ...editing, cost_centre_code: e.target.value })} />
            <TextInput id="p-exp" labelText="Expires (YYYY-MM-DD, blank = never)" size="sm"
              style={{ maxWidth: '16rem' }} placeholder="2027-12-31"
              value={editing.expires_at || ''}
              onChange={(e) => setEditing({ ...editing, expires_at: e.target.value })} />
          </div>
          <TextInput id="p-desc" labelText="Description" size="sm" style={{ marginTop: '0.5rem' }}
            value={editing.description || ''}
            onChange={(e) => setEditing({ ...editing, description: e.target.value })} />
          <div style={{ display: 'flex', gap: '0.5rem', marginTop: '0.75rem' }}>
            <Button size="sm" renderIcon={Save} disabled={busy || !editing.code || !editing.name}
              onClick={() => save(editing)}>{busy ? 'Saving…' : 'Save'}</Button>
            <Button size="sm" kind="ghost" disabled={busy} onClick={() => { setEditing(null); setErr(null) }}>Cancel</Button>
          </div>
        </div>
      )}
    </Tile>
  )
}

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
  const [apiKeys, setApiKeys] = useState<ApiKeyRow[]>([])
  const [newKeyLabel, setNewKeyLabel] = useState('')
  const [issuedKey, setIssuedKey] = useState<string | null>(null)
  const [sd, setSd] = useState<Shutdown | null>(null)
  const [sdForm, setSdForm] = useState<ShutdownPolicy>({ enabled: false, days: 'mon-fri', start: '08:00', end: '20:00', tz: 'UTC' })
  const [sdSaving, setSdSaving] = useState(false)
  const [sdMsg, setSdMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [webhooks, setWebhooks] = useState<WebhookRow[]>([])
  const [webhooksEnabled, setWebhooksEnabled] = useState(false)
  const [whUrl, setWhUrl] = useState('')
  const [whSecret, setWhSecret] = useState('')
  const [whEvents, setWhEvents] = useState('')
  const [whMsg, setWhMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [roleMap, setRoleMapRows] = useState<RoleMapRow[]>([])
  const [roleOptions, setRoleOptions] = useState<string[]>([])
  const [roleSource, setRoleSource] = useState('mock')
  const [newGroup, setNewGroup] = useState('')
  const [newRole, setNewRole] = useState('')
  const [rmMsg, setRmMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [checkEmail, setCheckEmail] = useState('')
  const [checkResult, setCheckResult] = useState<{ groups: string[]; roles: string[] } | null>(null)
  const [users, setUsers] = useState<UserRow[]>([])

  function reloadWebhooks() {
    getWebhooks().then((w) => { if (w && w !== 'forbidden') { setWebhooks(w.webhooks); setWebhooksEnabled(w.enabled) } }).catch(() => {})
  }

  function reloadBudgets() {
    getBudgets().then((b) => b && b !== 'forbidden' && setBudgets(b.budgets)).catch(() => {})
  }
  function reloadShutdown() {
    getShutdown().then((s) => { if (s && s !== 'forbidden') { setSd(s); setSdForm(s.policy) } }).catch(() => {})
  }
  function reloadQuotas() {
    getQuotas().then((q) => q && q !== 'forbidden' && setQuotas(q.quotas)).catch(() => {})
  }
  function reloadApiKeys() {
    getApiKeys().then((k) => k && setApiKeys(k.keys)).catch(() => {})
  }
  function reloadRoleMap() {
    getRoleMap().then((m) => {
      if (m && m !== 'forbidden') {
        setRoleMapRows(m.mappings)
        setRoleOptions(m.roles)
        setRoleSource(m.role_source)
        setNewRole((cur) => cur || m.roles[0] || '')
      }
    }).catch(() => {})
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
    reloadApiKeys()
    reloadShutdown()
    reloadWebhooks()
    reloadRoleMap()
    getUsers().then((u) => u && u !== 'forbidden' && setUsers(u.users)).catch(() => {})
    getOrphans().then((o) => o && o !== 'forbidden' && setOrphans(o.orphans)).catch(() => {})
  }, [])

  async function onAddMapping() {
    setRmMsg(null)
    if (!newGroup.trim() || !newRole) return
    const { status, body } = await setRoleMap(newGroup.trim(), newRole)
    if (status === 200) { setNewGroup(''); reloadRoleMap() }
    else setRmMsg({ ok: false, text: body?.detail?.[0]?.msg || (typeof body?.detail === 'string' ? body.detail : 'Could not save the mapping.') })
  }
  async function onDeleteMapping(group: string) { await deleteRoleMap(group); reloadRoleMap() }
  async function onCheckAccess() {
    const res = await resolveAccess(checkEmail.trim())
    setCheckResult(res ? { groups: res.groups, roles: res.roles } : { groups: [], roles: [] })
  }

  async function onAddWebhook() {
    setWhMsg(null)
    const events = whEvents.split(',').map((e) => e.trim()).filter(Boolean)
    const { status, body } = await createWebhook(whUrl.trim(), whSecret, events)
    if (status === 200) { setWhUrl(''); setWhSecret(''); setWhEvents(''); reloadWebhooks() }
    else setWhMsg({ ok: false, text: body?.detail?.[0]?.msg || (typeof body?.detail === 'string' ? body.detail : 'Could not add webhook.') })
  }
  async function onTestWebhook(id: number) {
    const { body } = await testWebhook(id)
    setWhMsg({ ok: !!body?.ok, text: body?.ok ? 'Test event delivered.' : `Test failed: ${body?.error || 'no response'}` })
    reloadWebhooks()
  }
  async function onDeleteWebhook(id: number) { await deleteWebhook(id); reloadWebhooks() }

  async function onSaveShutdown() {
    setSdSaving(true)
    setSdMsg(null)
    try {
      const { status, body } = await setShutdownPolicy(sdForm)
      if (status === 200) {
        setSd(body); setSdForm(body.policy); setSdMsg({ ok: true, text: 'Schedule saved.' })
      } else {
        const msg = body?.detail?.[0]?.msg || (typeof body?.detail === 'string' ? body.detail : 'Could not save the schedule.')
        setSdMsg({ ok: false, text: msg })
      }
    } finally {
      setSdSaving(false)
    }
  }

  async function onCreateKey() {
    const label = newKeyLabel.trim() || 'api key'
    const res = await createApiKey(label)
    if (res.status === 200 && res.body?.key) setIssuedKey(res.body.key)
    setNewKeyLabel('')
    reloadApiKeys()
  }

  async function onRevokeKey(id: number) {
    await revokeApiKey(id)
    reloadApiKeys()
  }

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
            <Group title="Component catalogue">
              <Row label="Version choice offered">
                {cfg.catalogue?.with_version_choice ?? 0} of {cfg.catalogue?.technologies_total ?? 0} technologies
              </Row>
              <Row label="Catalogued options">{cfg.catalogue?.options_total ?? 0}</Row>
              <Row label="Option sources">{(cfg.catalogue?.sources ?? []).join(', ') || '—'}</Row>
              <Row label="Shape options from">{cfg.catalogue?.shape_options_from ?? '—'}</Row>
              <Row label="Hourly cloud fetch"><Flag on={!!cfg.catalogue?.live_fetch_enabled} /></Row>
              <Row label="Refresh interval">
                {Math.round((cfg.catalogue?.refresh_interval_seconds ?? 0) / 60)} min
              </Row>
              {/* A stale timestamp means the fetch stopped running — which is
                  invisible without showing it, because the cache keeps serving
                  the last good options either way. */}
              <Row label="Last refreshed">
                {cfg.catalogue?.last_refreshed
                  ? new Date(cfg.catalogue.last_refreshed).toLocaleString()
                  : 'never'}
              </Row>
              <Row label="Cached from cloud">
                {cfg.catalogue?.images_cached ?? 0} images · {cfg.catalogue?.shapes_cached ?? 0} shapes
              </Row>
            </Group>
          </div>
        </Tile>
      )}

      <SettingsEditor />

      <UserRolesPanel />

      {roleSource === 'jira' && (
      <Tile>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>
          Access control — group → role map (F-IAM-01){' '}
          <Tag size="sm" type={roleSource === 'jira' ? 'blue' : 'gray'}>
            {roleSource === 'jira' ? 'live (Jira groups)' : 'dev / mock'}
          </Tag>
        </h4>
        <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', marginBottom: '0.75rem' }}>
          Map each Jira group to a portal role — the one authorization decision the portal owns. In live
          mode these drive who can do what; a user in no mapped group is <code>read_only</code>. Verify people
          with the checker below, then set <code>ROLE_SOURCE=jira</code> to enforce.
        </p>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
              <th style={{ padding: '0.3rem 0.5rem' }}>Jira group</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Portal role</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Updated by</th>
              <th style={{ padding: '0.3rem 0.5rem' }} />
            </tr>
          </thead>
          <tbody>
            {roleMap.map((m) => (
              <tr key={m.jira_group} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                <td style={{ padding: '0.3rem 0.5rem', fontWeight: 500 }}>{m.jira_group}</td>
                <td style={{ padding: '0.3rem 0.5rem' }}><Tag size="sm" type="blue">{m.role}</Tag></td>
                <td style={{ padding: '0.3rem 0.5rem', color: 'var(--cds-text-secondary)' }}>{m.updated_by || '—'}</td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>
                  <Button hasIconOnly kind="ghost" size="sm" renderIcon={TrashCan} iconDescription={`Delete ${m.jira_group} mapping`} onClick={() => onDeleteMapping(m.jira_group)} />
                </td>
              </tr>
            ))}
            {roleMap.length === 0 && (
              <tr><td colSpan={4} style={{ padding: '0.5rem', color: 'var(--cds-text-secondary)' }}>No mappings yet — falls back to the JIRA_ROLE_MAP env.</td></tr>
            )}
          </tbody>
        </table>
        {rmMsg && (
          <InlineNotification kind={rmMsg.ok ? 'success' : 'error'} lowContrast title={rmMsg.text}
            onCloseButtonClick={() => setRmMsg(null)} style={{ maxWidth: 'none', marginTop: '0.75rem' }} />
        )}
        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end', marginTop: '0.75rem', flexWrap: 'wrap' }}>
          <TextInput id="rm-group" size="sm" labelText="Jira group" placeholder="e.g. imd-approvers"
            value={newGroup} onChange={(e) => setNewGroup(e.target.value)} style={{ minWidth: '16rem' }} />
          <Select id="rm-role" size="sm" labelText="Portal role" value={newRole} onChange={(e) => setNewRole(e.target.value)} style={{ minWidth: '12rem' }}>
            {roleOptions.map((r) => (<SelectItem key={r} value={r} text={r} />))}
          </Select>
          <Button size="sm" renderIcon={Add} disabled={!newGroup.trim() || !newRole} onClick={onAddMapping}>Add mapping</Button>
        </div>
        <div style={{ marginTop: '1rem', paddingTop: '0.75rem', borderTop: '1px solid var(--cds-border-subtle-01)' }}>
          <div style={{ fontWeight: 500, fontSize: '0.8rem', marginBottom: '0.4rem' }}>Check a user's access</div>
          <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end', flexWrap: 'wrap' }}>
            <TextInput id="rm-check" size="sm" labelText="Email" placeholder="user@emaratechg.ae"
              value={checkEmail} onChange={(e) => setCheckEmail(e.target.value)} style={{ minWidth: '16rem' }} />
            <Button size="sm" kind="tertiary" disabled={!checkEmail.trim()} onClick={onCheckAccess}>Check</Button>
          </div>
          {checkResult && (
            <div style={{ fontSize: '0.82rem', marginTop: '0.6rem' }}>
              <div style={{ marginBottom: '0.25rem' }}>
                <span style={{ color: 'var(--cds-text-secondary)' }}>Groups: </span>
                {checkResult.groups.length ? checkResult.groups.map((g) => <Tag key={g} size="sm" type="cool-gray">{g}</Tag>) : <em style={{ color: 'var(--cds-text-secondary)' }}>none</em>}
              </div>
              <div>
                <span style={{ color: 'var(--cds-text-secondary)' }}>Resolves to: </span>
                {checkResult.roles.length ? checkResult.roles.map((r) => <Tag key={r} size="sm" type="green">{r}</Tag>) : <em style={{ color: 'var(--cds-text-secondary)' }}>read_only</em>}
              </div>
            </div>
          )}
        </div>
      </Tile>
      )}

      <Tile>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>
          Users &amp; roles <span style={{ fontWeight: 400, color: 'var(--cds-text-secondary)' }}>— who has used the portal</span>
        </h4>
        <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', marginBottom: '0.75rem' }}>
          Read-only. People who've acted in the portal (from the audit trail), with the roles and groups they
          currently resolve to. Reflects the directory — as complete as who has signed in.
        </p>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
              <th style={{ padding: '0.3rem 0.5rem' }}>Email</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Roles</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Groups</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Last seen</th>
              <th style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>Actions</th>
            </tr>
          </thead>
          <tbody>
            {users.map((u) => (
              <tr key={u.email} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                <td style={{ padding: '0.3rem 0.5rem', fontWeight: 500 }}>{u.email}</td>
                <td style={{ padding: '0.3rem 0.5rem' }}>
                  {u.roles.length ? u.roles.map((r) => <Tag key={r} size="sm" type="green">{r}</Tag>) : <em style={{ color: 'var(--cds-text-secondary)' }}>read_only</em>}
                </td>
                <td style={{ padding: '0.3rem 0.5rem' }}>
                  {u.groups.length ? u.groups.map((g) => <Tag key={g} size="sm" type="cool-gray">{g}</Tag>) : <span style={{ color: 'var(--cds-text-secondary)' }}>—</span>}
                </td>
                <td style={{ padding: '0.3rem 0.5rem', color: 'var(--cds-text-secondary)' }}>{u.last_seen ? u.last_seen.slice(0, 16).replace('T', ' ') : '—'}</td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right', color: 'var(--cds-text-secondary)' }}>{u.actions}</td>
              </tr>
            ))}
            {users.length === 0 && (
              <tr><td colSpan={5} style={{ padding: '0.5rem', color: 'var(--cds-text-secondary)' }}>No users have acted yet.</td></tr>
            )}
          </tbody>
        </table>
      </Tile>

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

      <ProjectsPanel />

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
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>Auto-shutdown schedule (F-FIN-06)</h4>
        <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', marginBottom: '0.75rem' }}>
          Stops non-prod compute out-of-hours and starts it back in-hours. Applies to all non-prod environments.
        </p>
        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end', flexWrap: 'wrap' }}>
          <Toggle id="sd-enabled" size="sm" labelText="Enabled" labelA="Off" labelB="On"
            toggled={sdForm.enabled} onToggle={(v) => setSdForm({ ...sdForm, enabled: v })} />
          <TextInput id="sd-days" size="sm" labelText="Business days" placeholder="mon-fri"
            value={sdForm.days} onChange={(e) => setSdForm({ ...sdForm, days: e.target.value })} style={{ maxWidth: '10rem' }} />
          <TextInput id="sd-start" size="sm" labelText="Start (HH:MM)" value={sdForm.start}
            onChange={(e) => setSdForm({ ...sdForm, start: e.target.value })} style={{ maxWidth: '8rem' }} />
          <TextInput id="sd-end" size="sm" labelText="End (HH:MM)" value={sdForm.end}
            onChange={(e) => setSdForm({ ...sdForm, end: e.target.value })} style={{ maxWidth: '8rem' }} />
          <TextInput id="sd-tz" size="sm" labelText="Timezone" placeholder="Asia/Dubai" value={sdForm.tz}
            onChange={(e) => setSdForm({ ...sdForm, tz: e.target.value })} style={{ maxWidth: '12rem' }} />
          <Button size="sm" renderIcon={Save} disabled={sdSaving} onClick={onSaveShutdown}>
            {sdSaving ? 'Saving…' : 'Save schedule'}
          </Button>
        </div>
        {sdMsg && (
          <InlineNotification kind={sdMsg.ok ? 'success' : 'error'} lowContrast title={sdMsg.text}
            onCloseButtonClick={() => setSdMsg(null)} style={{ maxWidth: 'none', marginTop: '0.75rem' }} />
        )}
        {sd && (
          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(12rem, 1fr))', gap: '0 2rem', marginTop: '0.75rem' }}>
            <Group title="Now">
              <Row label="Off-hours now"><Flag on={sd.off_hours_now} onLabel="off-hours" offLabel="business hours" /></Row>
              <Row label="Off-hours share">{Math.round(sd.off_hours_fraction * 100)}%</Row>
            </Group>
            <Group title="Non-prod estate">
              <Row label="Environments">{sd.environment_count}</Row>
              <Row label="Potential monthly saving">{money(sd.total_saving)}</Row>
            </Group>
          </div>
        )}
        <p style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
          The Enabled toggle runs the scheduler; a real cloud stop additionally requires <code>OCI_ACTUATE_ENABLED</code>.
        </p>
      </Tile>

      <Tile>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>
          Outbound webhooks (F-INT-10) <Flag on={webhooksEnabled} onLabel="delivering" offLabel="disabled" />
        </h4>
        <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', marginBottom: '0.75rem' }}>
          Lifecycle events are POSTed to these endpoints, HMAC-signed (<code>X-Signature</code>), with retries.
          Payloads carry no secrets. Delivery is off until <code>WEBHOOKS_ENABLED=true</code> (a test send fires regardless).
        </p>
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
              <th style={{ padding: '0.3rem 0.5rem' }}>Endpoint</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Events</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Last delivery</th>
              <th style={{ padding: '0.3rem 0.5rem' }} />
            </tr>
          </thead>
          <tbody>
            {webhooks.map((w) => (
              <tr key={w.id} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                <td style={{ padding: '0.3rem 0.5rem', wordBreak: 'break-all' }}>{w.url}</td>
                <td style={{ padding: '0.3rem 0.5rem', color: 'var(--cds-text-secondary)' }}>{w.events.length ? w.events.join(', ') : 'all'}</td>
                <td style={{ padding: '0.3rem 0.5rem' }}>
                  {w.last_delivery
                    ? <Tag size="sm" type={w.last_delivery.status === 'delivered' ? 'green' : w.last_delivery.status === 'failed' ? 'red' : 'gray'}>
                        {w.last_delivery.event} · {w.last_delivery.status}
                      </Tag>
                    : <span style={{ color: 'var(--cds-text-secondary)' }}>—</span>}
                </td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right', whiteSpace: 'nowrap' }}>
                  <Button size="sm" kind="ghost" onClick={() => onTestWebhook(w.id)}>Test</Button>
                  <Button hasIconOnly kind="ghost" size="sm" renderIcon={TrashCan} iconDescription="Delete webhook" onClick={() => onDeleteWebhook(w.id)} />
                </td>
              </tr>
            ))}
            {webhooks.length === 0 && (
              <tr><td colSpan={4} style={{ padding: '0.5rem', color: 'var(--cds-text-secondary)' }}>No webhooks configured.</td></tr>
            )}
          </tbody>
        </table>
        {whMsg && (
          <InlineNotification kind={whMsg.ok ? 'success' : 'error'} lowContrast title={whMsg.text}
            onCloseButtonClick={() => setWhMsg(null)} style={{ maxWidth: 'none', marginTop: '0.75rem' }} />
        )}
        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end', marginTop: '0.75rem', flexWrap: 'wrap' }}>
          <TextInput id="wh-url" size="sm" labelText="Endpoint URL" placeholder="https://…" value={whUrl}
            onChange={(e) => setWhUrl(e.target.value)} style={{ minWidth: '16rem' }} />
          <TextInput id="wh-secret" size="sm" labelText="Signing secret" type="password" value={whSecret}
            onChange={(e) => setWhSecret(e.target.value)} style={{ maxWidth: '12rem' }} />
          <TextInput id="wh-events" size="sm" labelText="Events (blank = all)" placeholder="provisioned, decommissioned"
            value={whEvents} onChange={(e) => setWhEvents(e.target.value)} style={{ minWidth: '14rem' }} />
          <Button size="sm" renderIcon={Add} disabled={!whUrl.trim() || whSecret.length < 8} onClick={onAddWebhook}>
            Add webhook
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

      <Tile>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.75rem' }}>
          API keys <span style={{ fontWeight: 400, color: 'var(--cds-text-secondary)' }}>— programmatic access as you</span>
        </h4>
        {issuedKey && (
          <InlineNotification
            kind="success"
            lowContrast
            title="New API key — copy it now, it won't be shown again"
            subtitle={issuedKey}
            onCloseButtonClick={() => setIssuedKey(null)}
            style={{ maxWidth: 'none', marginBottom: '0.75rem' }}
          />
        )}
        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
              <th style={{ padding: '0.3rem 0.5rem' }}>Label</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Created</th>
              <th style={{ padding: '0.3rem 0.5rem' }}>Last used</th>
              <th style={{ padding: '0.3rem 0.5rem' }} />
            </tr>
          </thead>
          <tbody>
            {apiKeys.filter((k) => k.active).map((k) => (
              <tr key={k.id} style={{ borderBottom: '1px solid var(--cds-border-subtle-01)' }}>
                <td style={{ padding: '0.3rem 0.5rem', fontWeight: 500 }}>{k.label}</td>
                <td style={{ padding: '0.3rem 0.5rem', color: 'var(--cds-text-secondary)' }}>{k.created_at?.slice(0, 10) || '—'}</td>
                <td style={{ padding: '0.3rem 0.5rem', color: 'var(--cds-text-secondary)' }}>{k.last_used_at ? k.last_used_at.slice(0, 16).replace('T', ' ') : 'never'}</td>
                <td style={{ padding: '0.3rem 0.5rem', textAlign: 'right' }}>
                  <Button hasIconOnly kind="ghost" size="sm" renderIcon={TrashCan} iconDescription={`Revoke ${k.label}`} onClick={() => onRevokeKey(k.id)} />
                </td>
              </tr>
            ))}
            {apiKeys.filter((k) => k.active).length === 0 && (
              <tr><td colSpan={4} style={{ padding: '0.5rem', color: 'var(--cds-text-secondary)' }}>No API keys.</td></tr>
            )}
          </tbody>
        </table>
        <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end', marginTop: '0.75rem', flexWrap: 'wrap' }}>
          <TextInput id="new-key-label" labelText="Label" size="sm" placeholder="e.g. ci-pipeline" value={newKeyLabel} onChange={(e) => setNewKeyLabel(e.target.value)} style={{ maxWidth: '16rem' }} />
          <Button size="sm" renderIcon={Password} onClick={onCreateKey}>Issue key</Button>
        </div>
        <p style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
          Use it with the <code>X-API-Key</code> header; it acts as you and inherits your roles.
        </p>
      </Tile>
    </div>
  )
}
