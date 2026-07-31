// Thin fetch helpers — every call goes to the BFF, which proxies to the API
// with the signed-in user's token. No credentials or business logic here.

export type Lookups = {
  projects: { code: string; name: string }[]
  cost_centres: { code: string; name: string }[]
  subsidiaries: { code: string; name: string }[]
  technologies: { code: string; name: string; lifecycle_state: string }[]
  environments: { name: string; environment_class: string }[]
}

export type Component = { technology_code: string; size: string }

export type Cost = {
  currency: string
  known_target?: boolean
  by_category?: {
    compute: number
    storage: number
    licence: number
    backup: number
    monitoring: number
    support: number
  }
  totals: { one_time: number; monthly: number; annual: number }
}

async function json<T>(r: Response): Promise<T> {
  return (await r.json()) as T
}

export async function getLookups(): Promise<Lookups> {
  return json<Lookups>(await fetch('/api/lookups'))
}

export async function getCost(
  deployment_target: string,
  components: Component[],
  advanced_options?: Record<string, unknown>,
): Promise<Cost> {
  const r = await fetch('/api/cost', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ deployment_target, components, advanced_options }),
  })
  return json<Cost>(r)
}

export async function saveDraft(
  payload: Record<string, unknown>,
): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/requests/draft', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(payload),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function submitRequest(
  reference: string,
): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/submit`, { method: 'POST' })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export type RequestRow = {
  reference: string
  status: string
  status_detail?: string | null
  requester: string
  requester_name?: string | null
  deployment_target?: string | null
  environment_name?: string | null
  target_environment?: string | null
  environment_tier?: string | null
  // Governance metadata (increment 6.1).
  business_justification?: string | null
  priority?: string | null
  business_criticality?: string | null
  required_delivery_date?: string | null
  application_owner?: string | null
  business_owner?: string | null
  technical_owner?: string | null
  environment_owner?: string | null
  advanced_options?: Record<string, unknown> | null
  submitted_at?: string | null
  approval_sla?: { sla_hours: number; elapsed_hours: number; due_at: string; status: string } | null
  components: { technology_code: string | null; size: string | null }[]
  estimate?: { currency: string; monthly: number } | null
  approval?: { jira_key: string; ticket_url?: string | null } | null
  // Policy waiver (F-GOV-02): a documented exception, if one was granted.
  waiver?: { reason: string; granted_by: string; granted_at?: string | null; expires_at?: string | null } | null
  // Environment TTL (F-FIN-07): expiry + ok/expiring/expired, for provisioned non-prod envs.
  ttl?: { expiry: string; days_left: number; status: string } | null
  // Cost variance (F-FIN-01): estimate vs actual, when an actual has been recorded.
  variance?: { estimate: number; actual: number; variance_pct: number; status: string } | null
  // Ownership (F-LCM-10): the resolved owner + whether the environment is orphaned.
  owner?: string | null
  orphaned?: boolean
  // Environment health (F-LCM-08): score + grade + the factors that lowered it.
  health?: { score: number; grade: string; factors: { signal: string; impact: number; detail: string }[] } | null
}

export async function transferOwner(
  reference: string,
  newOwner: string,
): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/transfer-owner`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ new_owner: newOwner }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function renewRequest(
  reference: string,
  days?: number,
): Promise<{ status: number; body: any }> {
  const qs = days ? `?days=${days}` : ''
  const r = await fetch(`/api/requests/${reference}/renew${qs}`, { method: 'POST' })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function getMe(): Promise<{ email: string; roles: string[] } | null> {
  const r = await fetch('/api/me')
  if (r.status === 401) {
    window.location.href = '/login'
    return null
  }
  return r.ok ? r.json() : null
}

export async function getRequests(
  params: Record<string, string | undefined> = {},
): Promise<RequestRow[]> {
  const clean: Record<string, string> = {}
  for (const [k, v] of Object.entries(params)) if (v) clean[k] = v
  const qs = new URLSearchParams(clean).toString()
  const r = await fetch(`/api/requests${qs ? `?${qs}` : ''}`)
  return r.ok ? r.json() : []
}

export type Breakdown = { key: string; count: number }
export type Stats = {
  kpis: { total: number; active: number; in_flight: number; failed: number; decommissioned: number; breaching_sla: number }
  active_monthly_cost: { amount: number; currency: string }
  by_status: Breakdown[]
  by_type: Breakdown[]
  by_technology: Breakdown[]
  by_target: Breakdown[]
  by_requester: Breakdown[]
  by_subsidiary: Breakdown[]
  trend: { week: string; count: number }[]
}

export async function getStats(): Promise<Stats | 'forbidden' | null> {
  const r = await fetch('/api/stats')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Showback & chargeback (E3.2, F-FIN-03).
export type ShowbackRow = { key: string; count: number; monthly: number; annual: number }
export type Showback = {
  group_by: string
  scope: string
  currency: string
  total: { count: number; monthly: number; annual: number }
  rows: ShowbackRow[]
}

export async function getShowback(
  groupBy: string,
  scope: string,
): Promise<Showback | 'forbidden' | null> {
  const r = await fetch(`/api/showback?group_by=${groupBy}&scope=${scope}`)
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Budget guardrails (E3.3, F-FIN-02).
export type BudgetRow = {
  cost_centre: string
  limit: number
  currency: string
  current: number
  remaining: number
  status: string
}

export async function getBudgets(): Promise<{ currency: string; budgets: BudgetRow[] } | 'forbidden' | null> {
  const r = await fetch('/api/budgets')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Actual-vs-estimate variance (E3.5, F-FIN-01).
export type VarianceRow = {
  reference: string
  cost_centre?: string | null
  environment?: string | null
  estimate: number
  actual: number
  variance: number
  variance_pct: number
  status: string
}
export type Variance = {
  currency: string
  total: { estimate: number; actual: number; variance: number; variance_pct: number }
  rows: VarianceRow[]
}

export async function getVariance(): Promise<Variance | 'forbidden' | null> {
  const r = await fetch('/api/variance')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Admin console (E1, F-OPS-09).
export type SystemConfig = {
  modes: { auth: string; jira: string; auto_provision: boolean; provision_mode: string; use_mock: boolean }
  governance: { sod_enforced: boolean; four_eyes_enforced: boolean; approval_quorum: number; approval_sla_hours: number; audit_hmac: boolean }
  change_window: { enabled: boolean; open_now: boolean; days: string; start: string; end: string; tz: string }
  finops: { ttl_days_nonprod: number; ttl_warn_days: number; ttl_enforce: boolean; budget_enforce: boolean; budget_warn_pct: number; variance_alert_pct: number; departed_owners_count: number }
}

export async function getConfig(): Promise<SystemConfig | 'forbidden' | null> {
  const r = await fetch('/api/config')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

export async function setBudget(costCentre: string, monthlyLimit: number): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/budgets', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ cost_centre_code: costCentre, monthly_limit: monthlyLimit }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function deleteBudget(costCentre: string): Promise<{ status: number }> {
  const r = await fetch(`/api/budgets/${costCentre}`, { method: 'DELETE' })
  return { status: r.status }
}

export type OrphanRow = { reference: string; environment?: string | null; owner?: string | null; reason: string }
export async function getOrphans(): Promise<{ count: number; orphans: OrphanRow[] } | 'forbidden' | null> {
  const r = await fetch('/api/orphans')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Quota management (E3.9, F-FIN-08).
export type QuotaRow = { project: string; limit: number; current: number; remaining: number; status: string }
export async function getQuotas(): Promise<{ quotas: QuotaRow[] } | 'forbidden' | null> {
  const r = await fetch('/api/quotas')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}
export async function setQuota(projectCode: string, maxEnvironments: number): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/quotas', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ project_code: projectCode, max_environments: maxEnvironments }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}
export async function deleteQuota(projectCode: string): Promise<{ status: number }> {
  const r = await fetch(`/api/quotas/${projectCode}`, { method: 'DELETE' })
  return { status: r.status }
}

export type AuditEntry = { event: string; created_at: string }

export async function getAudit(reference: string): Promise<AuditEntry[]> {
  const r = await fetch(`/api/requests/${reference}/audit`)
  if (!r.ok) return []
  const d = await r.json()
  return (d.entries as AuditEntry[]) || []
}
