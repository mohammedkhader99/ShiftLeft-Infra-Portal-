// Thin fetch helpers — every call goes to the BFF, which proxies to the API
// with the signed-in user's token. No credentials or business logic here.

export type Lookups = {
  projects: { code: string; name: string }[]
  cost_centres: { code: string; name: string }[]
  subsidiaries: { code: string; name: string }[]
  // `automated_targets` are the targets the orchestrator provisions itself; on any
  // other target the request is governed here and fulfilled by the infra team.
  technologies: { code: string; name: string; lifecycle_state: string; targets: string[]; automated_targets: string[] }[]
  environments: { name: string; environment_class: string }[]
}

// The detail fields are optional everywhere: a component that carries none
// resolves from its size anchor server-side, exactly as before this form existed.
export type Component = {
  technology_code: string
  size: string
  version?: string
  image?: string
  vcpu?: number
  memory_gb?: number
  storage_gb?: number
}

// What the server is willing to offer for one component's detail fields, and
// the size presets that fill them in. The browser renders this; it never decides
// it — every value comes back through server-side validation on submit.
export type ComponentOptions = {
  technology_code: string | null
  technology_name: string | null
  fields: Record<
    string,
    { label: string; options: { value: string; label: string }[]; default: string }
  >
  presets: Record<string, { vcpu: number; memory_gb: number; storage_gb: number }>
  // The chosen image's OS family, and whether this technology can be installed
  // on it. `installable: false` is a blocking combination — the server refuses
  // it on submit, so the form says so rather than letting it get that far.
  os_family: string | null
  installable: boolean
}

export async function getComponentOptions(
  technology: string,
  target: string,
  image = '',
): Promise<ComponentOptions> {
  const q = new URLSearchParams({ technology, target, image })
  return json<ComponentOptions>(await fetch(`/api/catalogue/component-options?${q}`))
}

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
  // Components the server could not price — it knows what they are, and until
  // now only the TOTAL reached this form, which read 0.00 and looked free.
  // REQ-2026-0176 was approved at that fiction.
  unpriced?: string[]
  // Priced, but on a component nothing has certified yet. The figure is the
  // same arithmetic execution will do; what is uncertain is whether the thing
  // gets built that way at all.
  provisional?: string[]
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

// AI cloud & sizing recommendation (E4). The AI picks a catalogue stack; the
// portal — not the model — prices it across every cloud it can run on, so the
// user can compare and pick the best value. Recommend-only; pre-fills the form.
export type AiRecommendation = {
  mode: string
  components: { technology_code: string; size: string }[]
  rationale: string | null
  eligible_targets: string[]
  comparison: {
    target: string
    currency: string
    monthly: number
    one_time: number
    annual: number
    pricing_source: string
  }[]
  recommended_target: string | null
  notes: string[]
  warnings: string[]
}

export async function recommendWithAI(
  description: string,
): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/ai/recommend', {
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
  data_classification?: string | null
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
  // Group ownership (F-IAM-09): the owning directory group, if set.
  owner_group?: string | null
  // Environment health (F-LCM-08): score + grade + the factors that lowered it.
  health?: { score: number; grade: string; factors: { signal: string; impact: number; detail: string }[] } | null
  // Drift (F-LCM-09): result of the last drift check, if one has run.
  drift?: { detected: boolean; checked_at: string } | null
  // Cloud state sync: result of the last reconciliation, if one has run.
  state?: { status: string; synced_at: string } | null
  // Operational power state (cloud-sync increment 2): 'running' | 'stopped' | 'partial'.
  power?: string | null
  // Per-request auto-shutdown (F-FIN-06 B): the override + the resolved effective schedule.
  shutdown?: { override: Partial<ShutdownPolicy> | null; effective: ShutdownPolicy } | null
  // Backup restore-points (F-LCM-06), newest first.
  backups?: BackupRow[] | null
  // Active JIT access grants (F-IAM-07) — metadata only, never the credential.
  access_grants?: AccessGrantRow[] | null
  // What was actually built (F-INT-04): the details Terraform recorded, so a
  // requester can find their own server without asking anyone.
  resources?: ProvisionedResourceRow[] | null
}

export type ProvisionedResourceRow = {
  kind: string
  name: string
  region?: string | null
  lifecycle_state?: string | null
  power_state?: string | null
  created_at?: string | null
  ocids?: string[]
  names?: string[]
  private_ips?: string[]
  public_ips?: string[]
  hostnames?: string[]
  urls?: string[]
  dns?: string[]
  // Build facts rather than identity (e.g. the ports a service listens on).
  info?: Record<string, unknown>
  // Outputs from a blueprint the portal doesn't know by name. Shown raw rather
  // than dropped, so a new recipe's outputs never disappear silently.
  other?: Record<string, unknown>
}

export type BackupRow = { id: number; label: string; created_by?: string | null; created_at?: string | null }
export type AccessGrantRow = {
  id: number; grantee: string; scope: string; granted_by?: string | null
  granted_at?: string | null; expires_at?: string | null; status: string
}

// Grant time-bound JIT access (F-IAM-07). Approver/admin only. The response
// carries a one-time vault link (F-INT-05) shown ONCE — never stored.
export async function grantAccess(
  reference: string, grantee: string, scope: string, ttlHours: number,
): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/access`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ grantee, scope, ttl_hours: ttlHours }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function revokeAccess(reference: string, id: number): Promise<{ status: number }> {
  const r = await fetch(`/api/requests/${reference}/access/${id}/revoke`, { method: 'POST' })
  return { status: r.status }
}

// Take a backup restore-point of a provisioned environment (F-LCM-06). Owner
// self-service (owner or platform-admin); no approval — mock records metadata.
export async function createBackup(
  reference: string,
  label?: string,
): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/backup`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ label: label || null }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function listBackups(reference: string): Promise<BackupRow[]> {
  const r = await fetch(`/api/requests/${reference}/backups`)
  return r.ok ? (await r.json()).backups : []
}

export async function checkDrift(reference: string): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/drift-check`, { method: 'POST' })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

// Read-only cloud state sync (reconciliation). Flags out-of-band changes; it
// observes only and never changes cloud state.
export async function reconcileState(reference: string): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/reconcile`, { method: 'POST' })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

// Actuation (cloud-sync increment 2): stop/start a provisioned environment from
// the portal. Platform-admin only + audited; the actual cloud change runs through
// the orchestrator (mock changes nothing real until live credentials are wired).
export async function actuate(
  reference: string,
  action: 'stop' | 'start',
): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/actuate`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ action }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

// Set or clear a request's per-request auto-shutdown override (F-FIN-06 B). The
// override is merged over the global schedule (its fields win); { clear: true }
// removes it (inherit global). Platform-admin only.
export async function setRequestShutdown(
  reference: string,
  body: Partial<ShutdownPolicy> | { clear: true },
): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/shutdown`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(body),
  })
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

// Set or clear an environment's owning directory group (F-IAM-09). Owner or admin.
export async function setOwnerGroup(
  reference: string, group: string | null,
): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/owner-group`, {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ group }),
  })
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

export async function cancelRequest(
  reference: string,
  reason: string,
): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/requests/${reference}/cancel`, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ reason }),
  })
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

// Optimisation digest (E4, F-FIN-12).
export type OptRecommendation = { type: string; detail: string; monthly_saving: number }
export type OptEnvironment = {
  reference: string
  environment?: string | null
  tier?: string | null
  recommendations: OptRecommendation[]
  saving: number
}
export type OptOwner = { owner: string; environments: OptEnvironment[]; saving: number }
export type Optimisation = {
  currency: string
  generated_at: string
  total_saving: number
  environment_count: number
  owner_count: number
  owners: OptOwner[]
}

export async function getOptimisation(): Promise<Optimisation | 'forbidden' | null> {
  const r = await fetch('/api/optimisation')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Sustainability estimate (E4, F-FIN-13).
export type SustainEnv = {
  reference: string
  environment?: string | null
  deployment_target?: string | null
  vcpu: number
  memory_gb: number
  storage_gb: number
  energy_kwh_month: number
  carbon_kg_month: number
}
export type Sustainability = {
  generated_at: string
  total_energy_kwh_month: number
  total_carbon_kg_month: number
  equivalents: { car_km: number; trees_year: number }
  environment_count: number
  environments: SustainEnv[]
  note: string
}

export async function getSustainability(): Promise<Sustainability | 'forbidden' | null> {
  const r = await fetch('/api/sustainability')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Scheduled auto-shutdown (E3, F-FIN-06).
export type ShutdownEnv = {
  reference: string
  environment?: string | null
  tier?: string | null
  compute_monthly: number
  monthly_saving: number
  paused: boolean
}
export type ShutdownPolicy = { enabled: boolean; days: string; start: string; end: string; tz: string }
export type Shutdown = {
  enabled: boolean
  policy: ShutdownPolicy
  schedule: { days: string; start: string; end: string; tz: string }
  off_hours_now: boolean
  off_hours_fraction: number
  currency: string
  total_saving: number
  environment_count: number
  environments: ShutdownEnv[]
  note: string
}

export async function getShutdown(): Promise<Shutdown | 'forbidden' | null> {
  const r = await fetch('/api/shutdown')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Outbound webhooks (F-INT-10, platform-admin). The secret is never returned.
export type WebhookRow = {
  id: number; url: string; events: string[]; active: boolean; created_by?: string | null
  last_delivery?: { event: string; status: string; attempts: number; error?: string | null } | null
}

export async function getWebhooks(): Promise<{ enabled: boolean; webhooks: WebhookRow[] } | 'forbidden' | null> {
  const r = await fetch('/api/webhooks')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

export async function createWebhook(url: string, secret: string, events: string[]): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/webhooks', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ url, secret, events }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function deleteWebhook(id: number): Promise<{ status: number }> {
  const r = await fetch(`/api/webhooks/${id}`, { method: 'DELETE' })
  return { status: r.status }
}

export async function testWebhook(id: number): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/webhooks/${id}/test`, { method: 'POST' })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

// Report subscriptions (F-RPT-11, oversight). Scheduled reports to finance/
// security/owners; each run is stored (viewable) + optionally POSTed signed.
export type ReportSubRow = {
  id: number; report: string; cadence: string; target_url?: string | null
  active: boolean; created_by?: string | null; next_due?: string | null; last_sent_at?: string | null
  last_run?: { id: number; delivered?: string | null; generated_at?: string | null } | null
}
export type ReportRunRow = {
  id: number; report: string; delivered?: string | null; generated_at?: string | null; summary: any
}

export async function getReportSubscriptions(): Promise<{ subscriptions: ReportSubRow[] } | 'forbidden' | null> {
  const r = await fetch('/api/report-subscriptions')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

export async function createReportSubscription(
  report: string, cadence: string, target_url?: string, secret?: string,
): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/report-subscriptions', {
    method: 'POST', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ report, cadence, target_url: target_url || null, secret: secret || null }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function deleteReportSubscription(id: number): Promise<{ status: number }> {
  const r = await fetch(`/api/report-subscriptions/${id}`, { method: 'DELETE' })
  return { status: r.status }
}

export async function runReportNow(id: number): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/report-subscriptions/${id}/run`, { method: 'POST' })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function getReportRuns(id: number): Promise<{ runs: ReportRunRow[] } | null> {
  const r = await fetch(`/api/report-subscriptions/${id}/runs`)
  return r.ok ? r.json() : null
}

// What happens after Submit — non-sensitive approval facts for any requester.
export type ApprovalInfo = {
  system_of_record: string
  quorum: number
  sla_hours: number
  four_eyes: boolean
  sod_enforced: boolean
  change_window: { enabled: boolean; open_now: boolean; days: string; start: string; end: string; tz: string }
}

export async function getApprovalInfo(): Promise<ApprovalInfo | null> {
  const r = await fetch('/api/approval-info')
  return r.ok ? r.json() : null
}

// Header quick-search over the caller's own requests.
export type SearchResult = { reference: string; environment: string; status: string; tier: string; request_type: string }

export async function searchRequests(q: string): Promise<SearchResult[]> {
  const r = await fetch(`/api/search?q=${encodeURIComponent(q)}`)
  if (!r.ok) return []
  return (await r.json()).results || []
}

// Access control: group -> role map (F-IAM-01, platform-admin).
export type RoleMapRow = { jira_group: string; role: string; updated_by?: string | null; updated_at?: string | null }

export async function getRoleMap(): Promise<{ mappings: RoleMapRow[]; roles: string[]; role_source: string } | 'forbidden' | null> {
  const r = await fetch('/api/access/role-map')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

export async function setRoleMap(jira_group: string, role: string): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/access/role-map', {
    method: 'PUT', headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ jira_group, role }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function deleteRoleMap(jira_group: string): Promise<{ status: number }> {
  const r = await fetch(`/api/access/role-map/${encodeURIComponent(jira_group)}`, { method: 'DELETE' })
  return { status: r.status }
}

export async function resolveAccess(email: string): Promise<{ email: string; source: string; groups: string[]; roles: string[] } | null> {
  const r = await fetch(`/api/access/resolve?email=${encodeURIComponent(email)}`)
  return r.ok ? r.json() : null
}

export type UserRow = { email: string; roles: string[]; groups: string[]; actions: number; last_seen?: string | null }

export async function getUsers(): Promise<{ users: UserRow[]; source: string } | 'forbidden' | null> {
  const r = await fetch('/api/access/users')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Generate a report on demand (F-RPT-11). Read-only; nothing is stored.
export async function getReport(kind: string): Promise<{ report: string; data: any } | 'forbidden' | null> {
  const r = await fetch(`/api/reports/${kind}`)
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}

// Set the global auto-shutdown schedule (F-FIN-06, platform-admin).
export async function setShutdownPolicy(
  policy: ShutdownPolicy,
): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/shutdown', {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(policy),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

// Admin console (E1, F-OPS-09).
export type SystemConfig = {
  modes: { auth: string; jira: string; auto_provision: boolean; provision_mode: string; use_mock: boolean }
  governance: { sod_enforced: boolean; four_eyes_enforced: boolean; approval_quorum: number; approval_sla_hours: number; audit_hmac: boolean }
  change_window: { enabled: boolean; open_now: boolean; days: string; start: string; end: string; tz: string }
  finops: { ttl_days_nonprod: number; ttl_warn_days: number; ttl_enforce: boolean; budget_enforce: boolean; budget_warn_pct: number; variance_alert_pct: number; departed_owners_count: number }
  // What the component detail form can currently offer. Catalogue data, not a
  // setting — but an admin still needs to see it without reading the seed file.
  catalogue: {
    technologies_total: number
    with_version_choice: number
    options_total: number
    sources: string[]
    shape_options_from: string
    live_fetch_enabled: boolean
    refresh_interval_seconds: number
    last_refreshed: string | null
    images_cached: number
    shapes_cached: number
  }
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

// Projects (F-CAT-02). The console lists DISABLED projects too — it has to be
// able to re-enable what it disabled. The request form uses /api/lookups, which
// returns only active ones.
export type ProjectRow = {
  code: string
  name: string
  description?: string | null
  owner_email?: string | null
  cost_centre_code?: string | null
  requested_by?: string | null
  requested_by_name?: string | null
  expires_at?: string | null
  active: boolean
  created_at?: string | null
  // none | ok | expiring | expired
  expiry_status: string
  days_left?: number | null
  // What disabling or deleting would affect.
  request_count: number
  environment_count: number
}
export async function getProjects(): Promise<{ projects: ProjectRow[] } | 'forbidden' | null> {
  const r = await fetch('/api/projects')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}
export async function saveProject(p: {
  code: string; name: string; description?: string | null; owner_email?: string | null
  cost_centre_code?: string | null; expires_at?: string | null; active?: boolean
}): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/projects', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify(p),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}
export async function deleteProject(code: string): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/projects/${code}`, { method: 'DELETE' })
  return { status: r.status, body: await r.json().catch(() => ({})) }
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
  trace_id?: string | null
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

// AI natural-language layer for the approvals bot (F-INT-08). Interprets a plain-
// English message into a safe command. Read-only intents run; approve/reject come
// back as a proposal (needs_confirmation + command) that the AI never executes —
// the caller confirms, which runs the authority-preserving chatops() path.
export async function aiChat(message: string): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/ai/chat', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ message }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

// Runtime settings (F-OPS-09). Non-secret governance/FinOps/AI knobs editable
// from the Admin console; secrets/security switches stay read-only in .env.
export type AdminSetting = {
  key: string
  label: string
  help: string
  type: string
  group: string
  choices?: string[] | null
  default: string
  value: string
  source: string
}
export type AdminSettings = {
  editable: AdminSetting[]
  read_only: { key: string; label: string; value: string; source: string }[]
  // Execution gates read from the ORCHESTRATOR's environment (whether real,
  // billable infrastructure can be created). Reported as unavailable rather than
  // guessed when the orchestrator can't be reached.
  execution: { key: string; label: string; value: string; source: string }[]
  execution_available: boolean
  note: string
}
// Blueprint registry (F-CAT-10): which technology can be built on which cloud.
// 'certified' = the portal builds it automatically; 'available' = the recipe
// exists but nobody has approved it; 'missing' = certified here but the
// orchestrator no longer ships it.
export type BlueprintRow = {
  technology_code: string
  deployment_target: string
  blueprint_ref: string
  version: string
  // 'suspended' is withdrawn BY EVIDENCE (C1) — the portal pulled it after
  // consecutive failures — which is a different thing from never certified.
  // 'stale' = certified once, and its proof aged out (C2). Distinct from
  // 'suspended', where the evidence turned against it: nothing failed here, the
  // evidence simply expired.
  state: 'certified' | 'available' | 'missing' | 'draft' | 'suspended' | 'stale' | 'none'
  certified_by: string | null
  certified_at: string | null
  // Why it was withdrawn, naming the requests that failed.
  notes: string | null
  description: string
  ready: boolean
  missing_config: string[]
  available_version: string
}
export type BlueprintMatrix = {
  orchestrator_available: boolean
  blueprints: BlueprintRow[]
  certified: number
  missing: number
  suspended: number
  stale: number
  targets: string[]
}
export async function getBlueprints(): Promise<BlueprintMatrix | 'forbidden' | null> {
  const r = await fetch('/api/blueprints')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}
export async function certifyBlueprint(
  technology_code: string, deployment_target: string, blueprint_ref: string, version: string,
): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/blueprints', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ technology_code, deployment_target, blueprint_ref, version }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}
export async function decertifyBlueprint(technology_code: string, deployment_target: string): Promise<number> {
  const r = await fetch(`/api/blueprints/${encodeURIComponent(technology_code)}/${encodeURIComponent(deployment_target)}`,
    { method: 'DELETE' })
  return r.status
}

// Portal-managed roles (F-IAM-01). There is no external source here — Entra is
// login only and Jira has no group model the portal can read — so authorisation
// is maintained in the portal while identity stays federated.
export type UserRoles = {
  role_source: string
  default_role: string[]
  bootstrap_admins: string[]
  roles: string[]
  users: { email: string; roles: string[] }[]
}
export async function getUserRoles(): Promise<UserRoles | 'forbidden' | null> {
  const r = await fetch('/api/access/user-roles')
  if (r.status === 403) return 'forbidden'
  return r.ok ? r.json() : null
}
export async function grantUserRole(email: string, role: string): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/access/user-roles', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ email, role }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}
export async function revokeUserRole(email: string, role: string): Promise<number> {
  const r = await fetch(`/api/access/user-roles/${encodeURIComponent(email)}/${encodeURIComponent(role)}`,
    { method: 'DELETE' })
  return r.status
}

// The governance rules OPA currently enforces (F-GOV-03). Descriptions come from
// each rule's own METADATA annotation in the loaded policy, so they cannot drift.
export type PolicyRule = {
  title: string
  description: string
  feature: string
  effect: string
  applies_to: string
  module: string
}
export type PolicyCatalogue = {
  available: boolean
  error?: string
  overview: PolicyRule | null
  rules: PolicyRule[]
  blocking: number
  advisory: number
  modules: string[]
}
export async function getPolicies(): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/admin/policies')
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export async function getAdminSettings(): Promise<{ status: number; body: any }> {
  const r = await fetch('/api/admin/settings')
  return { status: r.status, body: await r.json().catch(() => ({})) }
}
export async function setAdminSetting(key: string, value: string): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/admin/settings/${key}`, {
    method: 'PUT',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ value }),
  })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}
export async function resetAdminSetting(key: string): Promise<{ status: number; body: any }> {
  const r = await fetch(`/api/admin/settings/${key}`, { method: 'DELETE' })
  return { status: r.status, body: await r.json().catch(() => ({})) }
}

export type AuditEntry = { event: string; created_at: string }

// What the machines of a request said about themselves at first boot.
//
// The reports live behind a pre-authenticated OCI URL — a bearer token in URL
// form — so this is a PROXY, never a link. The API fetches the text server-side;
// the browser is never given the URL, and must not be.
export type BootReportComponent = {
  kind: string
  available: boolean
  files: Record<string, string>
  note: string
}
export type BootReport = {
  reference: string
  reachable: boolean
  historical?: boolean
  reports: BootReportComponent[]
  note?: string
}
// Where the catalogue and the cloud disagree about what exists (C3).
export type CatalogueGap = {
  family: string
  label: string
  sold: string[]
  offered: string[]
  retired: string[]
  newer: string[]
  severity: 'retired' | 'behind'
  note: string
}
export type CatalogueGaps = {
  reachable: boolean
  gaps: CatalogueGap[]
  retired?: number
  behind?: number
  families_unreachable?: string[]
  note?: string
}
export async function getCatalogueGaps(): Promise<CatalogueGaps | null> {
  const r = await fetch('/api/catalogue/gaps')
  if (!r.ok) return null
  return (await r.json()) as CatalogueGaps
}

export async function getBootReport(reference: string): Promise<BootReport | null> {
  const r = await fetch(`/api/requests/${reference}/boot-report`)
  if (!r.ok) return null
  return (await r.json()) as BootReport
}

export async function getAudit(reference: string): Promise<AuditEntry[]> {
  const r = await fetch(`/api/requests/${reference}/audit`)
  if (!r.ok) return []
  const d = await r.json()
  return (d.entries as AuditEntry[]) || []
}
