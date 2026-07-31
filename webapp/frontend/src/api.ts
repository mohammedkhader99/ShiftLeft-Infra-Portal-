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

// AI cost explanation (F-RPT-07). Explains the authoritative cost breakdown and
// suggests advisory tips — it never changes the request or provisions anything.
export type CostExplanation = {
  mode: string
  currency: string
  monthly: number
  known_target: boolean
  drivers: { category: string; label: string; amount: number; pct: number }[]
  summary: string
  tips: string[]
}

export async function explainCost(
  deployment_target: string,
  components: Component[],
  advanced_options?: Record<string, unknown>,
): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/cost/explain', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ deployment_target, components, advanced_options }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
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

// AI request drafting (F-RPT-06). The AI recommends a DRAFT only — the browser
// pre-fills the form with it and the user reviews, edits, and submits as normal.
export type AiDraft = {
  mode: string
  draft: {
    request_type: string
    project_code: string | null
    cost_centre_code: string | null
    deployment_target: string | null
    environment_name: string | null
    environment_tier: string | null
    data_classification: string | null
    priority: string | null
    business_criticality: string | null
    business_justification: string | null
    components: { technology_code: string; size: string | null }[]
  }
  notes: string[]
  warnings: string[]
}

export async function draftWithAI(
  description: string,
): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/ai/draft', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ description }),
  })
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
  // Drift (F-LCM-09): result of the last drift check, if one has run.
  drift?: { detected: boolean; checked_at: string } | null
}

export async function checkDrift(reference: string): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/drift-check`, { method: 'POST' })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

// AI failure triage (F-RPT-08). Diagnoses a failed/blocked request from its real
// signals — advisory only; it never retries, applies, or changes the request.
export type Triage = {
  mode: string
  reference: string
  status: string
  status_detail?: string | null
  failing: boolean
  summary: string
  likely_causes: string[]
  next_steps: string[]
  signals: { event: string; detail: unknown; created_at?: string | null }[]
}

export async function triageFailure(reference: string): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/triage`, { method: 'POST' })
  return { status: r.status, body: await r.json().catch(() => ({})) }
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

// Spend forecast (E4, F-RPT-05).
export type ForecastMonth = {
  month: number
  label: string
  projected_monthly: number
  projected_annual: number
  added: number
  removed: number
}
export type ForecastDriver = { reference: string; environment?: string | null; monthly: number; month: number }
export type Forecast = {
  currency: string
  horizon_months: number
  current_monthly: number
  pipeline_monthly: number
  expiring_monthly: number
  projected_monthly: number
  months: ForecastMonth[]
  drivers: { pipeline: ForecastDriver[]; expiring: ForecastDriver[] }
}

export async function getForecast(months = 6): Promise<Forecast | 'forbidden' | null> {
  const r = await fetch(`/api/forecast?months=${months}`)
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Anomaly detection (E4, F-FIN-09 cost + F-RPT-10 request).
export type Anomaly = {
  kind: string
  type: string
  severity: 'high' | 'medium' | 'low'
  subject: string
  reference?: string | null
  signal: string
  detail?: Record<string, unknown>
}
export type Anomalies = {
  generated_at: string
  count: number
  by_severity: { high: number; medium: number; low: number }
  anomalies: Anomaly[]
}

export async function getAnomalies(): Promise<Anomalies | 'forbidden' | null> {
  const r = await fetch('/api/anomalies')
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

// API keys (E1, F-INT-01).
export type ApiKeyRow = { id: number; label: string; identity: string; active: boolean; created_at?: string | null; last_used_at?: string | null }
export async function getApiKeys(): Promise<{ keys: ApiKeyRow[] } | null> {
  const r = await fetch('/api/api-keys')
  return r.ok ? r.json() : null
}
export async function createApiKey(label: string): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/api-keys', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}
export async function revokeApiKey(id: number): Promise<{ status: number }> {
  const r = await fetch(`/api/api-keys/${id}`, { method: 'DELETE' })
  return { status: r.status }
}

// Lifecycle event stream (F-INT-02). A curated, read-only feed of request/
// environment events, tailed by a monotonic cursor.
export type LifecycleEvent = {
  id: number
  type: string
  reference?: string | null
  actor?: string | null
  detail?: Record<string, unknown> | null
  at?: string | null
}

export async function getEvents(
  since = 0,
  params: { tail?: number; limit?: number; reference?: string; types?: string } = {},
): Promise<{ events: LifecycleEvent[]; cursor: number } | 'forbidden' | null> {
  const qs = new URLSearchParams({ since: String(since) })
  if (params.tail) qs.set('tail', String(params.tail))
  if (params.limit) qs.set('limit', String(params.limit))
  if (params.reference) qs.set('reference', params.reference)
  if (params.types) qs.set('types', params.types)
  const r = await fetch(`/api/events?${qs}`)
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// ChatOps approvals bot (F-INT-08). Runs a command as the signed-in user.
export async function chatops(command: string): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/chatops', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ command }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export type AuditEntry = { event: string; created_at: string }

export async function getAudit(reference: string): Promise<AuditEntry[]> {
  const r = await fetch(`/api/requests/${reference}/audit`)
  if (!r.ok) return []
  const d = await r.json()
  return (d.entries as AuditEntry[]) || []
}
