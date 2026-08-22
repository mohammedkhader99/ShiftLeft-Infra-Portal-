import type { AuditEntry } from './api'

export type WFStep = {
  label: string
  state: 'done' | 'current' | 'pending' | 'failed'
  when?: string
}

const TERMINAL_FAIL: Record<string, string> = {
  'apply-failed': 'Provisioning',
  'teardown-failed': 'Provisioning',
  // Built and running, but the machine reported it is not working — so the
  // stage that failed is Provisioned, not Provisioning. Pointing at
  // Provisioning would send someone hunting a Terraform error there isn't one of.
  'verify-failed': 'Provisioned',
  rejected: 'Approved',
}

// Approved and deliberately NOT built by the portal: nothing in the request
// has a certified blueprint, so the infrastructure team fulfils it. Neither a
// failure nor a completion — the lifecycle simply stops at Approved.
const STOPS_AT: Record<string, string> = {
  'manual-fulfil': 'Approved',
}

// Mirrors the HTMX portal's _workflow_steps: derive the lifecycle stages from
// the request status + audit trail.
export function workflowSteps(status: string, audit: AuditEntry[]): WFStep[] {
  const firstTs: Record<string, string> = {}
  for (const e of audit) {
    if (e.event && !(e.event in firstTs)) firstTs[e.event] = e.created_at
  }

  const raw: [string, boolean, string | undefined][] = [
    ['Submitted', status !== 'draft' && status !== '', undefined],
    ['Approved', 'approval.approved' in firstTs, firstTs['approval.approved']],
    ['Planned', 'plan.previewed' in firstTs, firstTs['plan.previewed']],
    [
      'Provisioning',
      'provisioning.started' in firstTs || 'jira.in_progress' in firstTs,
      firstTs['provisioning.started'] || firstTs['jira.in_progress'],
    ],
    ['Provisioned', 'provisioned' in firstTs || status === 'provisioned', firstTs['provisioned']],
  ]

  const steps: WFStep[] = raw.map(([label, done, when]) => ({
    label,
    when,
    state: done ? 'done' : 'pending',
  }))

  // Cancelled is neither a failure nor a completion: the lifecycle stopped
  // where it had got to, on purpose. Marking a stage 'failed' would send
  // somebody hunting an error that does not exist, and marking one 'current'
  // would suggest it is still moving.
  if (status === 'cancelled') {
    steps.push({ label: 'Cancelled', state: 'done', when: firstTs['request.cancelled'] })
    return steps
  }

  if (status === 'decommissioned') {
    steps.forEach((s) => (s.state = 'done'))
    steps.push({
      label: 'Decommissioned',
      state: 'done',
      when: firstTs['decommissioned'] || firstTs['destroyed'],
    })
    return steps
  }

  const stopsAt = STOPS_AT[status]
  if (stopsAt) {
    const upTo = steps.findIndex((s) => s.label === stopsAt)
    steps.forEach((s, i) => (s.state = i <= upTo ? 'done' : 'pending'))
    return steps
  }

  const failedStage = TERMINAL_FAIL[status]
  if (failedStage) {
    steps.forEach((s) => {
      if (s.label === failedStage) s.state = 'failed'
    })
    return steps
  }

  const current = steps.find((s) => s.state === 'pending')
  if (current) current.state = 'current'
  return steps
}

export function fmtWhen(iso?: string): string {
  if (!iso) return ''
  return iso.slice(0, 16).replace('T', ' ')
}
