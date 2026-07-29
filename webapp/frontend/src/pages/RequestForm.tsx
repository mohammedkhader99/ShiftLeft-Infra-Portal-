import { useEffect, useMemo, useState } from 'react'
import {
  Stack,
  RadioButtonGroup,
  RadioButton,
  Select,
  SelectItem,
  TextInput,
  TextArea,
  DatePicker,
  DatePickerInput,
  Checkbox,
  Button,
  Tile,
  InlineNotification,
  IconButton,
  FormGroup,
} from '@carbon/react'
import { Add, TrashCan } from '@carbon/icons-react'
import {
  getLookups,
  getMe,
  getRequests,
  getCost,
  saveDraft,
  submitRequest,
  type Lookups,
  type Component,
  type Cost,
  type RequestRow,
} from '../api'

const SIZES = ['small', 'medium', 'large']
const CLASSIFICATIONS = ['public', 'internal', 'confidential', 'restricted']
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

type Result = { kind: 'success' | 'error'; title: string; subtitle?: string }

const compKey = (c: Component) => `${c.technology_code}:${c.size}`

// Local YYYY-MM-DD (avoids the UTC shift that toISOString can cause).
const fmtDate = (d: Date) =>
  `${d.getFullYear()}-${String(d.getMonth() + 1).padStart(2, '0')}-${String(
    d.getDate(),
  ).padStart(2, '0')}`
const TODAY = fmtDate(new Date())

export default function RequestForm() {
  const [lookups, setLookups] = useState<Lookups | null>(null)
  const [email, setEmail] = useState<string | null>(null)
  const [requestType, setRequestType] = useState('create')

  const [projectCode, setProjectCode] = useState('')
  const [costCentre, setCostCentre] = useState('')
  const [subsidiary, setSubsidiary] = useState('')
  const [target, setTarget] = useState('')
  const [envName, setEnvName] = useState('')
  const [targetEnv, setTargetEnv] = useState('')
  const [classification, setClassification] = useState('')
  const [components, setComponents] = useState<Component[]>([{ technology_code: '', size: '' }])

  // Governance metadata (increment 6.1).
  const [justification, setJustification] = useState('')
  const [priority, setPriority] = useState('')
  const [criticality, setCriticality] = useState('')
  const [deliveryDate, setDeliveryDate] = useState('')
  const [appOwner, setAppOwner] = useState('')
  const [bizOwner, setBizOwner] = useState('')
  const [techOwner, setTechOwner] = useState('')
  const [envOwner, setEnvOwner] = useState('')

  // Decommission
  const [provisioned, setProvisioned] = useState<RequestRow[]>([])
  const [sourceRef, setSourceRef] = useState('')
  const [selected, setSelected] = useState<Set<string>>(new Set())

  const [cost, setCost] = useState<Cost | null>(null)
  const [errors, setErrors] = useState<Record<string, string>>({})
  const [result, setResult] = useState<Result | null>(null)
  const [busy, setBusy] = useState(false)

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
  const pricedKey = JSON.stringify([pricedTarget, pricedComponents])

  useEffect(() => {
    if (!pricedTarget || pricedComponents.length === 0) {
      setCost(null)
      return
    }
    let cancelled = false
    getCost(pricedTarget, pricedComponents)
      .then((c) => !cancelled && setCost(c))
      .catch(() => !cancelled && setCost(null))
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [pricedKey])

  function setComponent(i: number, patch: Partial<Component>) {
    setComponents((cs) => cs.map((c, idx) => (idx === i ? { ...c, ...patch } : c)))
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
      components: filledComponents,
    }
    if (isCreate) {
      p.project_code = projectCode || null
      p.environment_name = envName || null
    } else {
      p.target_environment = targetEnv || null
    }
    return p
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

        <Stack gap={6}>
          <RadioButtonGroup
            legendText="Request type"
            name="request_type"
            valueSelected={requestType}
            onChange={(v) => setRequestType(String(v))}
          >
            <RadioButton labelText="Create environment" value="create" id="rt-create" />
            <RadioButton labelText="Add component" value="add" id="rt-add" />
            <RadioButton labelText="Resize component" value="resize" id="rt-resize" />
            <RadioButton labelText="Decommission" value="decommission" id="rt-decom" />
          </RadioButtonGroup>

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
                <Stack gap={4}>
                  {components.map((c, i) => (
                    <div key={i} style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-end' }}>
                      <div style={{ flex: 3 }}>
                        <Select id={`tech-${i}`} labelText={i === 0 ? 'Technology' : ''} value={c.technology_code} onChange={(e) => setComponent(i, { technology_code: e.target.value })}>
                          <SelectItem value="" text="— technology —" />
                          {lookups.technologies.map((t) => (
                            <SelectItem key={t.code} value={t.code} text={`${t.name} (${t.lifecycle_state})`} />
                          ))}
                        </Select>
                      </div>
                      <div style={{ flex: 2 }}>
                        <Select id={`size-${i}`} labelText={i === 0 ? 'Size' : ''} value={c.size} onChange={(e) => setComponent(i, { size: e.target.value })}>
                          <SelectItem value="" text="— size —" />
                          {SIZES.map((s) => (
                            <SelectItem key={s} value={s} text={s} />
                          ))}
                        </Select>
                      </div>
                      <IconButton label="Remove" kind="ghost" onClick={() => setComponents((cs) => (cs.length > 1 ? cs.filter((_, idx) => idx !== i) : cs))} disabled={components.length === 1}>
                        <TrashCan />
                      </IconButton>
                    </div>
                  ))}
                </Stack>
                <Button kind="ghost" size="sm" renderIcon={Add} onClick={() => setComponents((cs) => [...cs, { technology_code: '', size: '' }])} style={{ marginTop: '0.5rem' }}>
                  Add component
                </Button>
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
              <div style={{ fontSize: '0.875rem', color: 'var(--cds-text-secondary)', lineHeight: 2 }}>
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
