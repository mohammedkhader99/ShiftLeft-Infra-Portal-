import { useEffect, useMemo, useState, type CSSProperties } from 'react'
import {
  Stack,
  Select,
  SelectItem,
  TextInput,
  TextArea,
  DatePicker,
  DatePickerInput,
  Checkbox,
  Button,
  Tile,
  Tag,
  InlineNotification,
  FormGroup,
  Accordion,
  AccordionItem,
} from '@carbon/react'
import {
  TrashCan,
  DataBase,
  Api,
  Search,
  Code,
  Application,
  ContainerSoftware,
  Security,
  Terminal,
  type CarbonIconType,
} from '@carbon/icons-react'
import {
  getLookups,
  getMe,
  getRequests,
  getCost,
  saveDraft,
  submitRequest,
  draftWithAI,
  type Lookups,
  type Component,
  type Cost,
  type RequestRow,
} from '../api'

const SIZES = ['small', 'medium', 'large', 'xlarge']
const CLASSIFICATIONS = ['public', 'internal', 'confidential', 'restricted']
const ENV_TIERS: [string, string][] = [
  ['dev', 'Development'],
  ['test', 'Test'],
  ['sit', 'SIT'],
  ['uat', 'UAT'],
  ['preprod', 'Pre-Production'],
  ['prod', 'Production'],
  ['dr', 'Disaster Recovery'],
]
const TARGETS: [string, string][] = [
  ['onprem', 'On-premises'],
  ['azure', 'Microsoft Azure'],
  ['oci', 'Oracle Cloud (OCI)'],
]
const PRIORITIES = ['low', 'medium', 'high', 'critical']
const CRITICALITIES: [string, string][] = [
  ['tier1', 'Tier 1 — mission critical'],
  ['tier2', 'Tier 2 — business critical'],
  ['tier3', 'Tier 3 — important'],
  ['tier4', 'Tier 4 — low impact'],
]

// Size specs shown on the size cards. These mirror the server-side sizing
// anchors (db/seed.py SIZES); the server remains authoritative for pricing.
type Spec = { vcpu: number; mem: number; storage: number }
const SIZE_SPECS: Record<string, Spec> = {
  small: { vcpu: 2, mem: 4, storage: 50 },
  medium: { vcpu: 4, mem: 16, storage: 200 },
  large: { vcpu: 8, mem: 64, storage: 500 },
  xlarge: { vcpu: 16, mem: 128, storage: 1000 },
}

// Generic category icons — NOT brand logos (trademarked + external assets).
const TECH_ICON_BY_CODE: Record<string, CarbonIconType> = {
  postgres16: DataBase, 'oracle-db': DataBase, mssql: DataBase, mongodb: DataBase, redis7: DataBase,
  kafka: Api, rabbitmq: Api,
  elasticsearch: Search, opensearch: Search,
  java21: Code, dotnet8: Code, nodejs20: Code, python312: Code,
  nginx: Application, apache: Application,
  k8s: ContainerSoftware, openshift: ContainerSoftware,
  vault: Security, keycloak: Security,
  rhel9: Terminal, win2019: Terminal,
}
const techIcon = (code: string): CarbonIconType => TECH_ICON_BY_CODE[code] ?? Application

// Advanced options (6.5) — mirrors api/validation.ADVANCED_OPTIONS. The first
// four drive cost; the rest are captured for the approver.
type AdvOpt = { key: string; label: string; type: 'toggle' | 'select' | 'text'; options?: [string, string][] }
// Selects render a leading "—" (value '') meaning unset; cleanAdvanced drops it.
const ADVANCED_OPTIONS: AdvOpt[] = [
  { key: 'high_availability', label: 'High availability (×2 compute)', type: 'toggle' },
  { key: 'backup_retention', label: 'Backup retention', type: 'select', options: [['7', '7 days'], ['30', '30 days'], ['90', '90 days']] },
  { key: 'monitoring_level', label: 'Monitoring level', type: 'select', options: [['basic', 'Basic'], ['enhanced', 'Enhanced']] },
  { key: 'support_tier', label: 'Support tier', type: 'select', options: [['standard', 'Standard'], ['business', 'Business'], ['premium', 'Premium']] },
  { key: 'region', label: 'Region', type: 'select', options: [['uae-north', 'UAE North'], ['uae-central', 'UAE Central'], ['eu-west', 'EU West'], ['us-east', 'US East'], ['ap-south', 'AP South']] },
  { key: 'availability_zone', label: 'Availability zone', type: 'select', options: [['single', 'Single'], ['az-1', 'AZ-1'], ['az-2', 'AZ-2'], ['az-3', 'AZ-3'], ['multi-az', 'Multi-AZ']] },
  { key: 'database_version', label: 'Database version', type: 'text' },
  { key: 'encryption', label: 'Encryption', type: 'select', options: [['at-rest', 'At rest'], ['in-transit', 'In transit'], ['at-rest-and-in-transit', 'At rest & in transit']] },
  { key: 'disaster_recovery', label: 'Disaster recovery', type: 'select', options: [['backup-restore', 'Backup & restore'], ['warm-standby', 'Warm standby'], ['active-active', 'Active-active']] },
  { key: 'logging_level', label: 'Logging level', type: 'select', options: [['standard', 'Standard'], ['verbose', 'Verbose']] },
  { key: 'storage_tier', label: 'Storage tier', type: 'select', options: [['standard', 'Standard'], ['performance', 'Performance'], ['archive', 'Archive']] },
  { key: 'autoscaling', label: 'Autoscaling', type: 'toggle' },
  { key: 'network_type', label: 'Network type', type: 'select', options: [['public', 'Public'], ['private', 'Private'], ['isolated', 'Isolated']] },
  { key: 'firewall_profile', label: 'Firewall profile', type: 'select', options: [['default', 'Default'], ['restricted', 'Restricted'], ['custom', 'Custom']] },
  { key: 'private_endpoint', label: 'Private endpoint', type: 'toggle' },
  { key: 'public_endpoint', label: 'Public endpoint', type: 'toggle' },
  { key: 'dns', label: 'DNS', type: 'select', options: [['internal', 'Internal'], ['external', 'External']] },
  { key: 'certificates', label: 'Certificates', type: 'select', options: [['self-signed', 'Self-signed'], ['ca-signed', 'CA-signed']] },
  { key: 'secrets_management', label: 'Secrets management', type: 'select', options: [['vault', 'Vault'], ['cloud-kms', 'Cloud KMS']] },
  { key: 'compliance_profile', label: 'Compliance profile', type: 'select', options: [['iso-27001', 'ISO 27001'], ['pci-dss', 'PCI-DSS'], ['hipaa', 'HIPAA'], ['uae-ia', 'UAE IA']] },
]
// Keep only meaningful selections (drop unset / false) before sending.
const cleanAdvanced = (a: Record<string, string | boolean>) =>
  Object.fromEntries(
    Object.entries(a).filter(([, v]) => v !== '' && v !== false && v != null),
  )

type Result = { kind: 'success' | 'error'; title: string; subtitle?: string }

const compKey = (c: Component) => `${c.technology_code}:${c.size}`

// Local YYYY-MM-DD (avoids the UTC shift that toISOString can cause).
const fmtDate = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate(),
  ).padStart(2, '0')}`
const TODAY = fmtDate(new Date())

// A sharp-edged selection card styled with Carbon design tokens (theme-aware).
const cardStyle = (selected: boolean): CSSProperties => ({
  display: 'flex',
  flexDirection: 'column',
  gap: '0.3rem',
  padding: '0.7rem 0.75rem',
  textAlign: 'left',
  cursor: 'pointer',
  width: '100%',
  borderRadius: 0,
  color: 'var(--cds-text-primary)',
  background: selected ? 'var(--cds-layer-selected)' : 'var(--cds-layer)',
  border: `1px solid ${selected ? 'var(--cds-border-interactive)' : 'var(--cds-border-subtle)'}`,
  boxShadow: selected ? 'inset 0 0 0 1px var(--cds-border-interactive)' : 'none',
})

export default function RequestForm({ initialType = 'create' }: { initialType?: string }) {
  const [lookups, setLookups] = useState<Lookups | null>(null)
  const [email, setEmail] = useState<string | null>(null)
  // The request type is chosen in the left nav (New request menu) and passed in.
  const [requestType, setRequestType] = useState(initialType)
  useEffect(() => setRequestType(initialType), [initialType])

  const [projectCode, setProjectCode] = useState('')
  const [costCentre, setCostCentre] = useState('')
  const [subsidiary, setSubsidiary] = useState('')
  const [target, setTarget] = useState('')
  const [envName, setEnvName] = useState('')
  const [envTier, setEnvTier] = useState('')
  const [targetEnv, setTargetEnv] = useState('')
  const [classification, setClassification] = useState('')
  const [components, setComponents] = useState<Component[]>([])

  // Governance metadata (increment 6.1).
  const [justification, setJustification] = useState('')
  const [priority, setPriority] = useState('')
  const [criticality, setCriticality] = useState('')
  const [deliveryDate, setDeliveryDate] = useState('')
  const [appOwner, setAppOwner] = useState('')
  const [bizOwner, setBizOwner] = useState('')
  const [techOwner, setTechOwner] = useState('')
  const [envOwner, setEnvOwner] = useState('')

  // Advanced options (6.5): a single bag of key -> value|bool.
  const [advanced, setAdvanced] = useState<Record<string, string | boolean>>({})
  const setAdvOpt = (key: string, value: string | boolean) =>
    setAdvanced((a) => ({ ...a, [key]: value }))

  // Decommission
  const [provisioned, setProvisioned] = useState<RequestRow[]>([])
  const [sourceRef, setSourceRef] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const [cost, setCost] = useState<Cost | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [result, setResult] = useState<Result | null>(null)
  // Advisory policy warnings (F-GOV-03) returned by a successful submit.
  const [warnings, setWarnings] = useState<string[]>([])
  const [busy, setBusy] = useState(false)

  // AI request drafting (F-RPT-06): a plain-English description pre-fills the
  // form. The AI recommends a draft only; the user reviews, edits, and submits.
  const [aiText, setAiText] = useState('')
  const [aiBusy, setAiBusy] = useState(false)
  const [aiMsg, setAiMsg] = useState<
    { kind: 'success' | 'warning' | 'error'; title: string; subtitle?: string } | null
  >(null)

  useEffect(() => {
    getLookups().then(setLookups).catch(() => setLookups(null))
    getMe().then((m) => setEmail(m?.email ?? null))
  }, [])

  const isCreate = requestType === 'create'
  const isDecommission = requestType === 'decommission'

  // Load the user's provisioned requests once decommission is chosen.
  useEffect(() => {
    if (isDecommission && email) {
      getRequests({ requester: email, status: 'provisioned' })
        .then(setProvisioned)
        .catch(() => setProvisioned([]))
    }
  }, [isDecommission, email])

  const sourceObj = provisioned.find((p) => p.reference === sourceRef)
  const sourceComponents: Component[] = (sourceObj?.components ?? [])
    .filter((c) => c.technology_code)
    .map((c) => ({ technology_code: c.technology_code as string, size: c.size ?? '' }))

  const filledComponents = useMemo(
    () => components.filter((c) => c.technology_code || c.size),
    [components],
  )
  const selectedComponents = sourceComponents.filter((c) => selected.has(compKey(c)))

  // What we price: the chosen stack (create/add/resize) or the selected
  // technologies being torn down (decommission, at the source's target).
  const pricedTarget = isDecommission ? sourceObj?.deployment_target ?? '' : target
  const pricedComponents = isDecommission
    ? selectedComponents
    : components.filter((c) => c.technology_code && c.size)
  const pricedAdvanced = isDecommission ? undefined : cleanAdvanced(advanced)
  const pricedKey = JSON.stringify([pricedTarget, pricedComponents, pricedAdvanced])

  useEffect(() => {
    if (!pricedTarget || pricedComponents.length === 0) {
      setCost(null)
      return
    }
    let cancelled = false
    getCost(pricedTarget, pricedComponents, pricedAdvanced)
      .then((c) => !cancelled && setCost(c))
      .catch(() => !cancelled && setCost(null))
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pricedKey])

  const techName = (code: string) =>
    lookups?.technologies.find((t) => t.code === code)?.name ?? code

  // Click a technology card to add/remove it as a component (default size medium).
  function toggleTech(code: string) {
    setComponents((cs) =>
      cs.some((c) => c.technology_code === code)
        ? cs.filter((c) => c.technology_code !== code)
        : [...cs, { technology_code: code, size: 'medium' }],
    )
  }
  function setSize(code: string, size: string) {
    setComponents((cs) => cs.map((c) => (c.technology_code === code ? { ...c, size } : c)))
  }
  function toggleSelected(c: Component, checked: boolean) {
    setSelected((s) => {
      const next = new Set(s)
      checked ? next.add(compKey(c)) : next.delete(compKey(c))
      return next
    })
  }

  function buildPayload(): Record<string, unknown> {
    if (isDecommission) {
      return {
        request_type: 'decommission',
        source_reference: sourceRef || null,
        components: selectedComponents,
      }
    }
    const p: Record<string, unknown> = {
      request_type: requestType,
      cost_centre_code: costCentre || null,
      subsidiary: subsidiary || null,
      deployment_target: target || null,
      data_classification: classification || null,
      business_justification: justification || null,
      priority: priority || null,
      business_criticality: criticality || null,
      required_delivery_date: deliveryDate || null,
      application_owner: appOwner || null,
      business_owner: bizOwner || null,
      technical_owner: techOwner || null,
      environment_owner: envOwner || null,
      advanced_options: cleanAdvanced(advanced),
      components: filledComponents,
    }
    if (isCreate) {
      p.project_code = projectCode || null
      p.environment_name = envName || null
      p.environment_tier = envTier || null
    } else {
      p.target_environment = targetEnv || null
    }
    return p
  }

  // Ask the assistant to draft the request, then pre-fill the form fields from
  // its suggestion. Nothing is submitted — the user reviews and edits below.
  async function onDraftWithAI() {
    setAiBusy(true)
    setAiMsg(null)
    const { status, body } = await draftWithAI(aiText.trim())
    setAiBusy(false)
    if (status !== 200) {
      setAiMsg({ kind: 'error', title: 'Could not draft', subtitle: body?.detail || 'Please try again.' })
      return
    }
    const d = body.draft
    if (d.project_code) setProjectCode(d.project_code)
    if (d.cost_centre_code) setCostCentre(d.cost_centre_code)
    if (d.deployment_target) setTarget(d.deployment_target)
    if (d.environment_name) setEnvName(d.environment_name)
    if (d.environment_tier) setEnvTier(d.environment_tier)
    if (d.data_classification) setClassification(d.data_classification)
    if (d.priority) setPriority(d.priority)
    if (d.business_criticality) setCriticality(d.business_criticality)
    if (d.business_justification) setJustification(d.business_justification)
    if (d.components?.length)
      setComponents(
        d.components.map((c: { technology_code: string; size: string | null }) => ({
          technology_code: c.technology_code,
          size: c.size || 'medium',
        })),
      )
    const bits = [...(body.notes || []), ...(body.warnings || [])]
    setAiMsg({
      kind: (body.warnings || []).length ? 'warning' : 'success',
      title: `Draft ready${body.mode ? ` · ${body.mode}` : ''} — review the fields below`,
      subtitle: bits.join('  ·  ') || 'Fields filled in from your description.',
    })
  }

  async function onSaveDraft() {
    setBusy(true)
    setResult(null)
    const { status, body } = await saveDraft(buildPayload())
    setBusy(false)
    if (status === 200)
      setResult({ kind: 'success', title: `Draft saved as ${body.reference}`, subtitle: 'You can resume it later.' })
    else setResult({ kind: 'error', title: 'Could not save the draft', subtitle: body?.detail || '' })
  }

  async function onSubmit() {
    setBusy(true)
    setErrors({})
    setResult(null)
    setWarnings([])
    const draft = await saveDraft(buildPayload())
    if (draft.status !== 200) {
      setBusy(false)
      setResult({ kind: 'error', title: 'Could not save the request', subtitle: draft.body?.detail || '' })
      return
    }
    const ref = draft.body.reference
    const submit = await submitRequest(ref)
    setBusy(false)
    if (submit.status === 200) {
      const appr = submit.body.approval
      setResult({
        kind: 'success',
        title: `Request ${ref} submitted`,
        subtitle: appr?.jira_key ? `Jira ticket ${appr.jira_key} — awaiting approval.` : 'Awaiting approval.',
      })
      setWarnings(submit.body.policy_warnings || [])
    } else if (submit.status === 422) {
      setErrors(submit.body.errors || {})
      const pv = submit.body.policy_violations
      if (pv?.length) setResult({ kind: 'error', title: 'Blocked by policy', subtitle: pv.join('; ') })
    } else {
      setResult({ kind: 'error', title: 'Submit failed', subtitle: submit.body?.error || submit.body?.detail || '' })
    }
  }

  if (!lookups) return <p style={{ color: 'var(--cds-text-secondary)' }}>Loading form…</p>

  return (
    <div style={{ display: 'flex', gap: '2rem', flexWrap: 'wrap' }}>
      <div style={{ flex: '1 1 30rem', maxWidth: '40rem' }}>
        {result && (
          <InlineNotification
            kind={result.kind}
            title={result.title}
            subtitle={result.subtitle}
            lowContrast
            onCloseButtonClick={() => setResult(null)}
            style={{ marginBottom: '1rem', maxWidth: 'none' }}
          />
        )}
        {warnings.length > 0 && (
          <InlineNotification
            kind="warning"
            lowContrast
            title="Submitted with advisory notes (F-GOV-03)"
            subtitle={warnings.join('  ·  ')}
            onCloseButtonClick={() => setWarnings([])}
            style={{ marginBottom: '1rem', maxWidth: 'none' }}
          />
        )}

        <Stack gap={6}>

          {isDecommission ? (
            <>
              <Select
                id="source_reference"
                labelText="Which provisioned request?"
                value={sourceRef}
                onChange={(e) => {
                  setSourceRef(e.target.value)
                  setSelected(new Set())
                }}
                invalid={!!errors.source_reference}
                invalidText={errors.source_reference}
              >
                <SelectItem value="" text="— select a provisioned request —" />
                {provisioned.map((p) => (
                  <SelectItem
                    key={p.reference}
                    value={p.reference}
                    text={`${p.reference} — ${p.environment_name || p.target_environment || 'environment'}`}
                  />
                ))}
              </Select>
              {provisioned.length === 0 && (
                <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.85rem' }}>
                  You have no provisioned requests to decommission.
                </p>
              )}
              {sourceComponents.length > 0 && (
                <FormGroup legendText="Technologies to decommission">
                  {errors.components && (
                    <p style={{ color: 'var(--cds-text-error)', fontSize: '0.75rem', marginBottom: '0.5rem' }}>
                      {errors.components}
                    </p>
                  )}
                  {sourceComponents.map((c, i) => (
                    <Checkbox
                      key={compKey(c)}
                      id={`decom-${i}`}
                      labelText={`${c.technology_code} (${c.size || 'n/a'})`}
                      checked={selected.has(compKey(c))}
                      onChange={(_e: unknown, data: { checked: boolean }) => toggleSelected(c, data.checked)}
                    />
                  ))}
                </FormGroup>
              )}
            </>
          ) : (
            <>
              <Tile style={{ borderLeft: '3px solid var(--cds-border-interactive)' }}>
                <p style={{ fontWeight: 600, margin: '0 0 0.25rem' }}>Draft with AI</p>
                <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.6rem' }}>
                  Describe what you need in plain English — the assistant fills the form in for you to
                  review. It only suggests a draft; it never submits, prices, or provisions.
                </p>
                <TextArea
                  id="ai-description"
                  labelText="Describe your request"
                  placeholder="e.g. a medium Postgres database for the eGate UAT environment, on-prem, internal data"
                  rows={2}
                  value={aiText}
                  onChange={(e) => setAiText(e.target.value)}
                />
                <div style={{ marginTop: '0.5rem' }}>
                  <Button size="sm" onClick={onDraftWithAI} disabled={aiBusy || aiText.trim().length < 8}>
                    {aiBusy ? 'Drafting…' : 'Draft with AI'}
                  </Button>
                </div>
                {aiMsg && (
                  <InlineNotification
                    kind={aiMsg.kind}
                    lowContrast
                    title={aiMsg.title}
                    subtitle={aiMsg.subtitle}
                    onCloseButtonClick={() => setAiMsg(null)}
                    style={{ marginTop: '0.75rem', maxWidth: 'none' }}
                  />
                )}
              </Tile>

              {isCreate && (
                <Select id="project_code" labelText="Project" value={projectCode} onChange={(e) => setProjectCode(e.target.value)} invalid={!!errors.project_code} invalidText={errors.project_code}>
                  <SelectItem value="" text="— select —" />
                  {lookups.projects.map((p) => (
                    <SelectItem key={p.code} value={p.code} text={p.name} />
                  ))}
                </Select>
              )}

              <Select id="cost_centre_code" labelText="Cost centre" value={costCentre} onChange={(e) => setCostCentre(e.target.value)} invalid={!!errors.cost_centre_code} invalidText={errors.cost_centre_code}>
                <SelectItem value="" text="— select —" />
                {lookups.cost_centres.map((c) => (
                  <SelectItem key={c.code} value={c.code} text={`${c.code} — ${c.name}`} />
                ))}
              </Select>

              <Select id="subsidiary" labelText="Subsidiary" value={subsidiary} onChange={(e) => setSubsidiary(e.target.value)} invalid={!!errors.subsidiary} invalidText={errors.subsidiary}>
                <SelectItem value="" text="— select —" />
                {lookups.subsidiaries.map((s) => (
                  <SelectItem key={s.code} value={s.code} text={s.name} />
                ))}
              </Select>

              <Select id="deployment_target" labelText="Deployment target" value={target} onChange={(e) => setTarget(e.target.value)} invalid={!!errors.deployment_target} invalidText={errors.deployment_target}>
                <SelectItem value="" text="— select —" />
                {TARGETS.map(([v, label]) => (
                  <SelectItem key={v} value={v} text={label} />
                ))}
              </Select>

              {isCreate ? (
                <TextInput id="environment_name" labelText="New environment name" placeholder="e.g. egate-uat" value={envName} onChange={(e) => setEnvName(e.target.value)} invalid={!!errors.environment_name} invalidText={errors.environment_name} />
              ) : (
                <Select id="target_environment" labelText="Existing environment" value={targetEnv} onChange={(e) => setTargetEnv(e.target.value)} invalid={!!errors.target_environment} invalidText={errors.target_environment}>
                  <SelectItem value="" text="— select —" />
                  {lookups.environments.map((e) => (
                    <SelectItem key={e.name} value={e.name} text={`${e.name} (${e.environment_class})`} />
                  ))}
                </Select>
              )}

              {isCreate && (
                <Select id="environment_tier" labelText="Environment tier" value={envTier} onChange={(e) => setEnvTier(e.target.value)} invalid={!!errors.environment_tier} invalidText={errors.environment_tier}>
                  <SelectItem value="" text="— select —" />
                  {ENV_TIERS.map(([v, label]) => (
                    <SelectItem key={v} value={v} text={label} />
                  ))}
                </Select>
              )}

              {isCreate && (
                <Select id="data_classification" labelText="Data classification" value={classification} onChange={(e) => setClassification(e.target.value)} invalid={!!errors.data_classification} invalidText={errors.data_classification}>
                  <SelectItem value="" text="— select —" />
                  {CLASSIFICATIONS.map((c) => (
                    <SelectItem key={c} value={c} text={c} />
                  ))}
                </Select>
              )}

              <FormGroup legendText="Components">
                {errors.components && (
                  <p style={{ color: 'var(--cds-text-error)', fontSize: '0.75rem', marginBottom: '0.5rem' }}>{errors.components}</p>
                )}
                <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.6rem' }}>
                  Pick one or more technologies, then choose a size for each.
                </p>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(9.5rem, 1fr))', gap: '0.5rem' }}>
                  {lookups.technologies.map((t) => {
                    const Icon = techIcon(t.code)
                    const sel = components.some((c) => c.technology_code === t.code)
                    return (
                      <button key={t.code} type="button" aria-pressed={sel} onClick={() => toggleTech(t.code)} style={cardStyle(sel)}>
                        <Icon size={20} style={{ color: 'var(--cds-icon-primary)' }} />
                        <span style={{ fontWeight: 500, fontSize: '0.82rem', lineHeight: 1.2 }}>{t.name}</span>
                        {t.lifecycle_state !== 'certified' && (
                          <Tag type={t.lifecycle_state === 'deprecated' ? 'red' : 'purple'} size="sm" style={{ margin: 0 }}>
                            {t.lifecycle_state}
                          </Tag>
                        )}
                      </button>
                    )
                  })}
                </div>

                {components.length > 0 && (
                  <div style={{ marginTop: '1.25rem', display: 'flex', flexDirection: 'column', gap: '1.1rem' }}>
                    {components.map((c) => {
                      const Icon = techIcon(c.technology_code)
                      return (
                        <div key={c.technology_code}>
                          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'center', marginBottom: '0.4rem' }}>
                            <span style={{ display: 'flex', alignItems: 'center', gap: '0.4rem', fontWeight: 500, fontSize: '0.9rem' }}>
                              <Icon size={16} /> {techName(c.technology_code)}
                            </span>
                            <Button kind="ghost" size="sm" renderIcon={TrashCan} onClick={() => toggleTech(c.technology_code)}>
                              Remove
                            </Button>
                          </div>
                          <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(8rem, 1fr))', gap: '0.5rem' }}>
                            {SIZES.map((s) => {
                              const spec = SIZE_SPECS[s]
                              const sel = c.size === s
                              return (
                                <button key={s} type="button" aria-pressed={sel} onClick={() => setSize(c.technology_code, s)} style={cardStyle(sel)}>
                                  <span style={{ fontWeight: 500, textTransform: 'capitalize', fontSize: '0.85rem' }}>{s}</span>
                                  <span style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', lineHeight: 1.5 }}>
                                    {spec.vcpu} vCPU · {spec.mem} GB RAM<br />{spec.storage} GB storage
                                  </span>
                                </button>
                              )
                            })}
                          </div>
                        </div>
                      )
                    })}
                  </div>
                )}
              </FormGroup>

              <FormGroup legendText="Request details">
                <Stack gap={5}>
                  <TextArea
                    id="business_justification"
                    labelText="Business justification"
                    placeholder="Why is this needed? (at least 20 characters)"
                    rows={3}
                    value={justification}
                    onChange={(e) => setJustification(e.target.value)}
                    invalid={!!errors.business_justification}
                    invalidText={errors.business_justification}
                  />
                  <div style={{ display: 'flex', gap: '0.75rem' }}>
                    <div style={{ flex: 1 }}>
                      <Select id="priority" labelText="Priority" value={priority} onChange={(e) => setPriority(e.target.value)} invalid={!!errors.priority} invalidText={errors.priority}>
                        <SelectItem value="" text="— select —" />
                        {PRIORITIES.map((p) => (
                          <SelectItem key={p} value={p} text={p} />
                        ))}
                      </Select>
                    </div>
                    <div style={{ flex: 1 }}>
                      <Select id="business_criticality" labelText="Business criticality" value={criticality} onChange={(e) => setCriticality(e.target.value)} invalid={!!errors.business_criticality} invalidText={errors.business_criticality}>
                        <SelectItem value="" text="— select —" />
                        {CRITICALITIES.map(([v, label]) => (
                          <SelectItem key={v} value={v} text={label} />
                        ))}
                      </Select>
                    </div>
                  </div>
                  <DatePicker
                    datePickerType="single"
                    dateFormat="Y-m-d"
                    minDate={TODAY}
                    value={deliveryDate}
                    onChange={(dates: Date[]) => setDeliveryDate(dates[0] ? fmtDate(dates[0]) : '')}
                  >
                    <DatePickerInput
                      id="required_delivery_date"
                      labelText="Required delivery date"
                      placeholder="yyyy-mm-dd"
                      invalid={!!errors.required_delivery_date}
                      invalidText={errors.required_delivery_date}
                    />
                  </DatePicker>
                  <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', margin: 0 }}>
                    Owners (optional)
                  </p>
                  <div style={{ display: 'flex', gap: '0.75rem' }}>
                    <TextInput id="application_owner" labelText="Application owner" placeholder="name or email" value={appOwner} onChange={(e) => setAppOwner(e.target.value)} />
                    <TextInput id="business_owner" labelText="Business owner" placeholder="name or email" value={bizOwner} onChange={(e) => setBizOwner(e.target.value)} />
                  </div>
                  <div style={{ display: 'flex', gap: '0.75rem' }}>
                    <TextInput id="technical_owner" labelText="Technical owner" placeholder="name or email" value={techOwner} onChange={(e) => setTechOwner(e.target.value)} />
                    <TextInput id="environment_owner" labelText="Environment owner" placeholder="name or email" value={envOwner} onChange={(e) => setEnvOwner(e.target.value)} />
                  </div>
                </Stack>
              </FormGroup>

              <Accordion>
                <AccordionItem title="Advanced options (optional)">
                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(13rem, 1fr))', gap: '0.75rem 1rem', alignItems: 'end' }}>
                    {ADVANCED_OPTIONS.map((opt) => {
                      const err = errors[`advanced.${opt.key}`]
                      if (opt.type === 'toggle') {
                        return (
                          <Checkbox
                            key={opt.key}
                            id={`adv-${opt.key}`}
                            labelText={opt.label}
                            checked={advanced[opt.key] === true}
                            onChange={(_e: unknown, data: { checked: boolean }) => setAdvOpt(opt.key, data.checked)}
                          />
                        )
                      }
                      if (opt.type === 'text') {
                        return (
                          <TextInput
                            key={opt.key}
                            id={`adv-${opt.key}`}
                            labelText={opt.label}
                            placeholder="e.g. latest"
                            value={(advanced[opt.key] as string) || ''}
                            onChange={(e) => setAdvOpt(opt.key, e.target.value)}
                            invalid={!!err}
                            invalidText={err}
                          />
                        )
                      }
                      return (
                        <Select
                          key={opt.key}
                          id={`adv-${opt.key}`}
                          labelText={opt.label}
                          value={(advanced[opt.key] as string) || ''}
                          onChange={(e) => setAdvOpt(opt.key, e.target.value)}
                          invalid={!!err}
                          invalidText={err}
                        >
                          <SelectItem value="" text="—" />
                          {opt.options!.map(([v, label]) => (
                            <SelectItem key={v} value={v} text={label} />
                          ))}
                        </Select>
                      )
                    })}
                  </div>
                </AccordionItem>
              </Accordion>
            </>
          )}

          <div style={{ display: 'flex', gap: '0.75rem' }}>
            <Button kind="secondary" onClick={onSaveDraft} disabled={busy}>
              Save draft
            </Button>
            <Button onClick={onSubmit} disabled={busy}>
              {isDecommission ? 'Submit decommission' : 'Submit request'}
            </Button>
          </div>
        </Stack>
      </div>

      <div style={{ flex: '0 0 16rem' }}>
        <Tile style={{ borderTop: '3px solid var(--cds-border-interactive)' }}>
          <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em' }}>
            {isDecommission ? 'Monthly cost to free' : 'Live cost'}
          </p>
          {cost ? (
            <>
              <p style={{ fontSize: '2rem', fontWeight: 300, margin: '0.25rem 0' }}>
                {cost.totals.monthly.toFixed(2)} <span style={{ fontSize: '0.9rem' }}>{cost.currency}/mo</span>
              </p>
              {cost.by_category && (
                <div style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', lineHeight: 1.9, borderTop: '1px solid var(--cds-border-subtle)', paddingTop: '0.5rem', marginTop: '0.25rem' }}>
                  <div style={{ textTransform: 'uppercase', fontSize: '0.68rem', letterSpacing: '0.02em', marginBottom: '0.15rem' }}>Monthly by category</div>
                  <div>Compute <span style={{ float: 'right' }}>{cost.by_category.compute.toFixed(2)}</span></div>
                  <div>Storage <span style={{ float: 'right' }}>{cost.by_category.storage.toFixed(2)}</span></div>
                  <div>Licence <span style={{ float: 'right' }}>{cost.by_category.licence.toFixed(2)}</span></div>
                  {!!cost.by_category.backup && <div>Backup <span style={{ float: 'right' }}>{cost.by_category.backup.toFixed(2)}</span></div>}
                  {!!cost.by_category.monitoring && <div>Monitoring <span style={{ float: 'right' }}>{cost.by_category.monitoring.toFixed(2)}</span></div>}
                  {!!cost.by_category.support && <div>Support <span style={{ float: 'right' }}>{cost.by_category.support.toFixed(2)}</span></div>}
                </div>
              )}
              <div style={{ fontSize: '0.875rem', color: 'var(--cds-text-secondary)', lineHeight: 2, borderTop: '1px solid var(--cds-border-subtle)', paddingTop: '0.5rem', marginTop: '0.5rem' }}>
                <div>One-time <span style={{ float: 'right' }}>{cost.totals.one_time.toFixed(2)}</span></div>
                <div>Annual <span style={{ float: 'right' }}>{cost.totals.annual.toFixed(2)}</span></div>
              </div>
            </>
          ) : (
            <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.875rem', marginTop: '0.5rem' }}>
              {isDecommission
                ? 'Pick a provisioned request and tick technologies to see the monthly cost freed.'
                : 'Choose a deployment target and add a component to see the cost.'}
            </p>
          )}
        </Tile>
      </div>
    </div>
  )
}
