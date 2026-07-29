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
): Promise<Cost> {
  const r = await fetch('/api/cost', {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: JSON.stringify({ deployment_target, components }),
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
  environment_name?: string | null
  target_environment?: string | null
  components: { technology_code: string | null; size: string | null }[]
  estimate?: { currency: string; monthly: number } | null
  approval?: { jira_key: string; ticket_url?: string | null } | null
}

export async function getMe(): Promise<{ email: string; roles: string[] } | null> {
  const r = await fetch('/api/me')
  if (r.status === 401) {
    window.location.href = '/login'
    return null
  }
  return r.ok ? r.json() : null
}

export async function getRequests(requester?: string): Promise<RequestRow[]> {
  const q = requester ? `?requester=${encodeURIComponent(requester)}` : ''
  const r = await fetch(`/api/requests${q}`)
  return r.ok ? r.json() : []
}

export type AuditEntry = { event: string; created_at: string }

export async function getAudit(reference: string): Promise<AuditEntry[]> {
  const r = await fetch(`/api/requests/${reference}/audit`)
  if (!r.ok) return []
  const d = await r.json()
  return (d.entries as AuditEntry[]) || []
}
