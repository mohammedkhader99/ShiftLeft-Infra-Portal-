import { useEffect, useMemo, useState, type CSSProperties, type ReactNode } from 'react'
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
  ProgressIndicator,
  ProgressStep,
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
  Cloud,
  Enterprise,
  type CarbonIconType,
} from '@carbon/icons-react'
import {
  getLookups,
  getMe,
  getRequests,
  getCost,
  explainCost,
  saveDraft,
  submitRequest,
  draftWithAI,
  recommendWithAI,
  getApprovalInfo,
  getComponentOptions,
  type Lookups,
  type Component,
  type ComponentOptions,
  type Cost,
  type RequestRow,
  type ApprovalInfo,
  type AiRecommendation,
} from '../api'

// Detail fields whose value is text, not a number. Everything else is coerced
// with Number(), which would turn an image OCID into NaN.
const TEXT_DETAIL_FIELDS = new Set(['version', 'image'])

const SIZES = ['small', 'medium', 'large', 'xlarge']
const SIZE_INDEX: Record<string, number> = { small: 0, medium: 1, large: 2, xlarge: 3 }
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
  ['aws', 'Amazon Web Services (AWS)'],
  ['gcp', 'Google Cloud (GCP)'],
]
// Short labels for the single-row target cards (full names stay in summaries).
const TARGET_SHORT: Record<string, string> = {
  onprem: 'On-prem', azure: 'Azure', oci: 'OCI', aws: 'AWS', gcp: 'GCP',
}
const RTYPE_LABEL: Record<string, string> = {
  create: 'Create environment',
  clone: 'Clone environment',
  sandbox: 'Sandbox environment',
  temporary: 'Temporary environment',
  add: 'Add component',
  resize: 'Resize component',
}
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
  // Cloud-managed services + add-ons (target-aware catalogue).
  'aws-rds': DataBase, 'azure-sql': DataBase, 'azure-cosmos': DataBase, 'oci-adb': DataBase,
  'gcp-cloudsql': DataBase, 'gcp-firestore': DataBase, 'aws-dynamodb': DataBase,
  'aws-eks': ContainerSoftware, 'azure-aks': ContainerSoftware, 'oci-oke': ContainerSoftware,
  'gcp-gke': ContainerSoftware, 'service-mesh': ContainerSoftware,
  'aws-lambda': Code, 'azure-functions': Code, 'oci-functions': Code, 'gcp-functions': Code,
  'api-gateway': Api,
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

// A label/value line in the live Environment-summary panel.
function SummaryRow({ label, children }: { label: string; children: ReactNode }) {
  return (
    <div style={{ display: 'flex', justifyContent: 'space-between', gap: '0.75rem' }}>
      <span style={{ color: 'var(--cds-text-secondary)' }}>{label}</span>
      <span style={{ textAlign: 'right', fontWeight: 500 }}>{children}</span>
    </div>
  )
}

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
  // What the SERVER offers for each chosen technology's detail fields, keyed by
  // technology code. Fetched per component because the answer depends on the
  // technology and the deployment target. The browser renders these; validation
  // on submit re-checks every value against the same source, so a tampered form
  // cannot widen what is allowed.
  const [techOptions, setTechOptions] = useState<Record<string, ComponentOptions>>({})

  // Governance metadata (increment 6.1).
  const [justification, setJustification] = useState('')
  const [priority, setPriority] = useState('')
  const [criticality, setCriticality] = useState('')
  const [deliveryDate, setDeliveryDate] = useState('')
  const [expiresOn, setExpiresOn] = useState('')  // temporary env expiry (F-CAT)
  const [appOwner, setAppOwner] = useState('')
  const [bizOwner, setBizOwner] = useState('')
  const [techOwner, setTechOwner] = useState('')
  const [envOwner, setEnvOwner] = useState('')
  const [ownerGroup, setOwnerGroup] = useState('')  // F-IAM-09: durable group owner

  // Advanced options (6.5): a single bag of key -> value|bool.
  const [advanced, setAdvanced] = useState<Record<string, string | boolean>>({})
  const setAdvOpt = (key: string, value: string | boolean) =>
    setAdvanced((a) => ({ ...a, [key]: value }))

  // Decommission + refresh both operate on the user's provisioned environments.
  const [provisioned, setProvisioned] = useState<RequestRow[]>([])
  const [sourceRef, setSourceRef] = useState('')   // decommission source / refresh target
  const [selected, setSelected] = useState<Set<string>>(new Set())
  const [refreshFrom, setRefreshFrom] = useState('')  // refresh copy-from (higher env)
  const [restoreBackupId, setRestoreBackupId] = useState('')  // restore: chosen backup
  const [reduceSizes, setReduceSizes] = useState<Record<string, string>>({})  // reduce: tech -> smaller size

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

  // AI cloud & sizing recommendation (E4): recommend a stack + compare its cost
  // across every cloud it can run on. Recommend-only; "Use this" pre-fills below.
  const [recBusy, setRecBusy] = useState(false)
  const [rec, setRec] = useState<AiRecommendation | null>(null)
  const [recErr, setRecErr] = useState<string | null>(null)

  // AI cost explanation (F-RPT-07): explain the live cost breakdown on demand.
  const [explain, setExplain] = useState<{ summary: string; tips: string[]; mode?: string } | null>(null)
  const [explainBusy, setExplainBusy] = useState(false)

  const [approvalInfo, setApprovalInfo] = useState<ApprovalInfo | null>(null)

  useEffect(() => {
    getLookups().then(setLookups).catch(() => setLookups(null))
    getMe().then((m) => setEmail(m?.email ?? null))
    getApprovalInfo().then(setApprovalInfo).catch(() => setApprovalInfo(null))
  }, [])

  const isCreate = requestType === 'create'
  const isClone = requestType === 'clone'
  const isSandbox = requestType === 'sandbox'
  const isTemporary = requestType === 'temporary'
  const isDr = requestType === 'dr'
  // All of these provision a NEW environment (share the create form fields).
  const isCreateLike = isCreate || isClone || isSandbox || isTemporary || isDr
  const isDecommission = requestType === 'decommission'
  const isRefresh = requestType === 'refresh'
  const isRestore = requestType === 'restore'
  const isReduce = requestType === 'reduce'
  const NONPROD_TIERS = ['dev', 'test', 'sit', 'uat', 'preprod']

  // Load the user's provisioned requests once an env-targeting type is chosen
  // (decommission/refresh/restore/reduce operate on one; clone copies one).
  useEffect(() => {
    if ((isDecommission || isRefresh || isRestore || isClone || isReduce || isDr) && email) {
      getRequests({ requester: email, status: 'provisioned' })
        .then(setProvisioned)
        .catch(() => setProvisioned([]))
    }
  }, [isDecommission, isRefresh, isRestore, isClone, isReduce, isDr, email])

  // Clone: selecting a source copies its stack, target and classification into the
  // form (the user gives the clone a new name/tier). The server re-validates it all.
  function onCloneSourceChange(ref: string) {
    setSourceRef(ref)
    const src = provisioned.find((p) => p.reference === ref)
    if (!src) return
    setTarget(src.deployment_target || '')
    setClassification(src.data_classification || '')
    setComponents(
      (src.components ?? [])
        .filter((c) => c.technology_code)
        .map((c) => ({ technology_code: c.technology_code as string, size: c.size || 'medium' })),
    )
    if (src.advanced_options && typeof src.advanced_options === 'object') {
      setAdvanced(src.advanced_options as Record<string, string | boolean>)
    }
  }

  // Restore: the backups of the currently-selected target (already on the row).
  const restoreBackups = provisioned.find((p) => p.reference === sourceRef)?.backups ?? []

  const sourceObj = provisioned.find((p) => p.reference === sourceRef)
  const sourceComponents: Component[] = (sourceObj?.components ?? [])
    .filter((c) => c.technology_code)
    .map((c) => ({ technology_code: c.technology_code as string, size: c.size ?? '' }))

  const filledComponents = useMemo(
    () => components.filter((c) => c.technology_code || c.size),
    [components],
  )
  const selectedComponents = sourceComponents.filter((c) => selected.has(compKey(c)))
  // Reduce: the chosen components at their new (smaller) sizes.
  const reduceComponents: Component[] = sourceComponents
    .filter((c) => reduceSizes[c.technology_code])
    .map((c) => ({ technology_code: c.technology_code, size: reduceSizes[c.technology_code] }))

  // What we price: the chosen stack (create/add/resize/clone/…), the technologies
  // being torn down (decommission), or the reduced components at their new sizes.
  const pricedTarget = isRefresh || isRestore ? '' : (isDecommission || isReduce) ? sourceObj?.deployment_target ?? '' : target
  const pricedComponents = isDecommission
    ? selectedComponents
    : isReduce
      ? reduceComponents
      : components.filter((c) => c.technology_code && c.size)
  const pricedAdvanced = isDecommission || isReduce ? undefined : cleanAdvanced(advanced)
  const pricedKey = JSON.stringify([pricedTarget, pricedComponents, pricedAdvanced])

  useEffect(() => {
    setExplain(null)  // a changed config invalidates any prior explanation
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

  // Target-aware catalogue (F-CAT): when the deployment target changes, drop any
  // picked components not offered on the new target (so the stack stays valid).
  useEffect(() => {
    if (!target || !lookups) return
    const allowed = new Set(
      lookups.technologies.filter((t) => t.targets.includes(target)).map((t) => t.code),
    )
    setComponents((cs) => cs.filter((c) => !c.technology_code || allowed.has(c.technology_code)))
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [target])

  // Fetch the detail options for every chosen technology, and fill in the
  // defaults for a component that has none yet. Re-runs when the target changes,
  // because what can be offered depends on where it runs.
  const chosenCodes = components.map((c) => c.technology_code).filter(Boolean).join(',')
  // Re-queried when the chosen IMAGE changes too: the image decides the OS
  // family, which decides what software can be installed and which versions can
  // be pinned. Keyed on tech:image pairs so changing a size does not refetch.
  const chosenImages = components.map((c) => `${c.technology_code}:${c.image ?? ''}`).join(',')
  useEffect(() => {
    const codes = chosenCodes ? chosenCodes.split(',') : []
    if (!codes.length) return
    let cancelled = false
    Promise.all(
      codes.map((code) =>
        getComponentOptions(
          code, target,
          components.find((c) => c.technology_code === code)?.image ?? '',
        ).then(
          (o) => [code, o] as const,
          // A failed fetch must not wedge the form: the component keeps its size
          // and the server fills the shape from the anchor, which is exactly the
          // pre-form behaviour.
          () => null,
        ),
      ),
    ).then((results) => {
      if (cancelled) return
      const fetched = Object.fromEntries(
        results.filter((r): r is readonly [string, ComponentOptions] => r !== null),
      )
      setTechOptions(fetched)
      // Seed any component that has no detail values yet from its current size,
      // so the boxes are never blank and what is shown is what is priced.
      setComponents((cs) =>
        cs.map((c) => {
          const opts = fetched[c.technology_code]
          if (!opts || c.vcpu) return c
          const preset = opts.presets?.[c.size]
          // Seed every text field's default too. A dropdown renders its first
          // option when the bound value is empty, so without this the form would
          // SHOW an OS image that the state does not hold — and submit without
          // one, silently falling back to the platform default.
          const defaults: Record<string, string> = {}
          for (const field of ['version', 'image']) {
            const value = opts.fields?.[field]?.default
            if (value) defaults[field] = value
          }
          return { ...c, ...(preset ?? {}), ...defaults }
        }),
      )
    })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [chosenCodes, chosenImages, target])

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

  // Choosing a size FILLS IN the detail boxes rather than replacing them: the
  // preset is a shortcut, and the four values are what actually get priced and
  // built. Presets come from the server's sizing anchors — they used to be a
  // table hard-coded here, which could show one shape while the server priced
  // another.
  function setSize(code: string, size: string) {
    const preset = techOptions[code]?.presets?.[size]
    setComponents((cs) =>
      cs.map((c) =>
        c.technology_code === code ? { ...c, size, ...(preset ?? {}) } : c,
      ),
    )
  }

  // One detail field on one component. Numeric fields are stored as numbers so
  // they serialise correctly for the API.
  function setDetail(code: string, field: string, value: string) {
    setComponents((cs) =>
      cs.map((c) =>
        c.technology_code === code
          ? { ...c, [field]: TEXT_DETAIL_FIELDS.has(field) ? value : Number(value) }
          : c,
      ),
    )
  }

  // Which preset (if any) a component's current numbers match. "Custom" is not
  // an error — it is shown so the requester knows they have left the standard
  // shapes, the same thing the approver is told.
  function presetName(c: Component): string {
    // Before the options load — and for a component carrying no explicit shape
    // at all — the size IS the shape, so report it rather than flashing
    // "Custom" at a requester who has chosen nothing unusual.
    if (c.vcpu == null) return c.size
    const presets = techOptions[c.technology_code]?.presets ?? {}
    const match = Object.entries(presets).find(
      ([, p]) =>
        p.vcpu === c.vcpu && p.memory_gb === c.memory_gb && p.storage_gb === c.storage_gb,
    )
    return match ? match[0] : 'custom'
  }
  function toggleSelected(c: Component, checked: boolean) {
    setSelected((s) => {
      const next = new Set(s)
      checked ? next.add(compKey(c)) : next.delete(compKey(c))
      return next
    })
  }

  function buildPayload(): Record<string, unknown> {
    if (isRefresh) {
      return {
        request_type: 'refresh',
        source_reference: sourceRef || null,             // target being refreshed
        refresh_from_reference: refreshFrom || null,     // higher source to copy from
      }
    }
    if (isRestore) {
      return {
        request_type: 'restore',
        source_reference: sourceRef || null,             // target being restored
        restore_backup_id: restoreBackupId ? Number(restoreBackupId) : null,
      }
    }
    if (isDecommission) {
      return {
        request_type: 'decommission',
        source_reference: sourceRef || null,
        components: selectedComponents,
      }
    }
    if (isReduce) {
      return {
        request_type: 'reduce',
        source_reference: sourceRef || null,
        components: sourceComponents
          .filter((c) => reduceSizes[c.technology_code])
          .map((c) => ({ technology_code: c.technology_code, size: reduceSizes[c.technology_code] })),
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
      owner_group: ownerGroup || null,
      business_owner: bizOwner || null,
      technical_owner: techOwner || null,
      environment_owner: envOwner || null,
      advanced_options: cleanAdvanced(advanced),
      components: filledComponents,
    }
    if (isCreateLike) {
      p.project_code = projectCode || null
      p.environment_name = envName || null
      p.environment_tier = envTier || null
    } else {
      p.target_environment = targetEnv || null
    }
    if (isClone || isDr) p.source_reference = sourceRef || null
    if (isTemporary) p.expires_on = expiresOn || null
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

  // Ask the assistant to recommend a stack, then compare its cost across every
  // cloud it can run on. Nothing is applied until the user clicks "Use this".
  async function onRecommend() {
    setRecBusy(true)
    setRec(null)
    setRecErr(null)
    const { status, body } = await recommendWithAI(aiText.trim())
    setRecBusy(false)
    if (status !== 200) {
      setRecErr(body?.detail || 'Could not produce a recommendation. Please try again.')
      return
    }
    setRec(body as AiRecommendation)
  }

  // Pre-fill the form from a recommendation, for the chosen cloud target. Sets
  // the target first so the target-aware catalogue keeps the recommended stack.
  function useRecommendation(target: string) {
    if (!rec) return
    setTarget(target)
    if (rec.components.length)
      setComponents(rec.components.map((c) => ({ technology_code: c.technology_code, size: c.size })))
    setAiMsg({
      kind: 'success',
      title: `Applied recommendation on ${TARGET_SHORT[target] || target}`,
      subtitle: 'Review the stack and the rest of the form below, then submit as normal.',
    })
  }

  // Ask the assistant to explain the live cost breakdown (recommend-only).
  async function onExplainCost() {
    setExplainBusy(true)
    setExplain(null)
    const { status, body } = await explainCost(pricedTarget, pricedComponents, pricedAdvanced)
    setExplainBusy(false)
    if (status === 200) setExplain({ summary: body.summary, tips: body.tips || [], mode: body.mode })
    else setExplain({ summary: body?.detail || 'Could not explain the cost.', tips: [] })
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

  // Section progress (Portal UI polish) — completion is derived from live state;
  // the current step is the first incomplete one. Single-page form is preserved;
  // clicking a step just scrolls to that section.
  const basicsDone = isClone || isDr
    ? !!(sourceRef && projectCode && costCentre && envName && envTier)
    : isCreate || isSandbox || isTemporary
      ? !!(projectCode && costCentre && target && envName && envTier && classification && (!isTemporary || expiresOn))
      : !!(costCentre && target && targetEnv)
  const stackDone = filledComponents.some((c) => c.technology_code && c.size)
  const detailsDone = justification.trim().length >= 20 && !!priority && !!criticality && !!deliveryDate
  const stepDone = [basicsDone, stackDone, detailsDone]
  const firstIncomplete = stepDone.indexOf(false)
  const currentIndex = firstIncomplete === -1 ? stepDone.length - 1 : firstIncomplete
  const SECTION_IDS = ['section-basics', 'section-stack', 'section-details']
  const scrollToSection = (i: number) =>
    document.getElementById(SECTION_IDS[i])?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  if (!lookups) return <p style={{ color: 'var(--cds-text-secondary)' }}>Loading form…</p>

  // Only technologies offered on the selected deployment target (F-CAT); the full
  // catalogue until a target is chosen.
  const availableTechs = target
    ? lookups.technologies.filter((t) => t.targets.includes(target))
    : lookups.technologies

  // Chosen components the platform does NOT provision automatically — the infra
  // team fulfils these after approval. Say so before the requester submits.
  // Only meaningful once a deployment target is chosen.
  const manualComponents = !target
    ? []
    : components
        .map((c) => lookups.technologies.find((t) => t.code === c.technology_code))
        .filter((t): t is NonNullable<typeof t> => !!t && !(t.automated_targets || []).includes(target))

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

        {!isDecommission && !isRefresh && !isRestore && !isReduce && (
          <ProgressIndicator
            currentIndex={currentIndex}
            spaceEqually
            onChange={scrollToSection}
            style={{ marginBottom: '1.5rem' }}
          >
            <ProgressStep label="Basics" complete={basicsDone} />
            <ProgressStep label="Stack" complete={stackDone} />
            <ProgressStep label="Details" complete={detailsDone} />
          </ProgressIndicator>
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
          ) : isRefresh ? (
            <>
              <Select
                id="refresh_target"
                labelText="Environment to refresh (non-prod)"
                value={sourceRef}
                onChange={(e) => setSourceRef(e.target.value)}
                invalid={!!errors.source_reference}
                invalidText={errors.source_reference}
              >
                <SelectItem value="" text="— select a non-prod environment —" />
                {provisioned
                  .filter((p) => NONPROD_TIERS.includes((p.environment_tier || '').toLowerCase()))
                  .map((p) => (
                    <SelectItem key={p.reference} value={p.reference}
                      text={`${p.reference} — ${p.environment_name || 'env'} (${p.environment_tier || '?'})`} />
                  ))}
              </Select>
              <Select
                id="refresh_from"
                labelText="Refresh from (a higher environment)"
                value={refreshFrom}
                onChange={(e) => setRefreshFrom(e.target.value)}
                invalid={!!errors.refresh_from_reference}
                invalidText={errors.refresh_from_reference}
              >
                <SelectItem value="" text="— select the source environment —" />
                {provisioned
                  .filter((p) => p.reference !== sourceRef)
                  .map((p) => (
                    <SelectItem key={p.reference} value={p.reference}
                      text={`${p.reference} — ${p.environment_name || 'env'} (${p.environment_tier || '?'})`} />
                  ))}
              </Select>
              {provisioned.length === 0 && (
                <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.85rem' }}>
                  You have no provisioned environments to refresh.
                </p>
              )}
              <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.8rem' }}>
                Copies data from the source down into the target. Sensitive source data
                (restricted/confidential) is masked automatically. Requires approval; nothing is
                copied in mock mode.
              </p>
            </>
          ) : isRestore ? (
            <>
              <Select
                id="restore_target"
                labelText="Environment to restore"
                value={sourceRef}
                onChange={(e) => { setSourceRef(e.target.value); setRestoreBackupId('') }}
                invalid={!!errors.source_reference}
                invalidText={errors.source_reference}
              >
                <SelectItem value="" text="— select a provisioned environment —" />
                {provisioned.map((p) => (
                  <SelectItem key={p.reference} value={p.reference}
                    text={`${p.reference} — ${p.environment_name || 'env'} (${p.environment_tier || '?'})`} />
                ))}
              </Select>
              <Select
                id="restore_backup"
                labelText="Backup to restore from"
                value={restoreBackupId}
                onChange={(e) => setRestoreBackupId(e.target.value)}
                invalid={!!errors.restore_backup_id}
                invalidText={errors.restore_backup_id}
              >
                <SelectItem value=""
                  text={restoreBackups.length ? '— select a backup —' : '— no backups for this environment —'} />
                {restoreBackups.map((b) => (
                  <SelectItem key={b.id} value={String(b.id)}
                    text={`${b.label}${b.created_at ? ` · ${b.created_at.slice(0, 16).replace('T', ' ')}` : ''}`} />
                ))}
              </Select>
              {sourceRef && restoreBackups.length === 0 && (
                <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.85rem' }}>
                  This environment has no backups yet — take one from My Requests first.
                </p>
              )}
              <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.8rem' }}>
                Rolls the environment back to the selected backup. Requires approval; nothing
                changes in mock mode.
              </p>
            </>
          ) : isReduce ? (
            <>
              <Select
                id="reduce_source"
                labelText="Environment to reduce"
                value={sourceRef}
                onChange={(e) => { setSourceRef(e.target.value); setReduceSizes({}) }}
                invalid={!!errors.source_reference}
                invalidText={errors.source_reference}
              >
                <SelectItem value="" text="— select a provisioned environment —" />
                {provisioned.map((p) => (
                  <SelectItem key={p.reference} value={p.reference}
                    text={`${p.reference} — ${p.environment_name || 'env'} (${p.environment_tier || '?'})`} />
                ))}
              </Select>
              {provisioned.length === 0 && (
                <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.85rem' }}>
                  You have no provisioned environments to reduce.
                </p>
              )}
              {sourceComponents.length > 0 && (
                <FormGroup legendText="Reduce component sizes">
                  {errors.components && (
                    <p style={{ color: 'var(--cds-text-error)', fontSize: '0.75rem', marginBottom: '0.5rem' }}>
                      {errors.components}
                    </p>
                  )}
                  <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.6rem' }}>
                    Choose a smaller size for any component — sizing up isn't allowed here (use Resize for that).
                  </p>
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                    {sourceComponents.map((c) => {
                      const smaller = SIZES.filter((s) => SIZE_INDEX[s] < SIZE_INDEX[c.size])
                      return (
                        <div key={c.technology_code} style={{ display: 'flex', gap: '0.75rem', alignItems: 'flex-end' }}>
                          <div style={{ flex: 1, fontSize: '0.85rem' }}>
                            <div style={{ fontWeight: 500 }}>{techName(c.technology_code)}</div>
                            <div style={{ color: 'var(--cds-text-secondary)', fontSize: '0.78rem' }}>current: {c.size || '—'}</div>
                          </div>
                          <div style={{ minWidth: '9rem' }}>
                            <Select id={`reduce-${c.technology_code}`} labelText="" size="sm"
                              value={reduceSizes[c.technology_code] || ''}
                              onChange={(e) => setReduceSizes((m) => ({ ...m, [c.technology_code]: e.target.value }))}
                              disabled={smaller.length === 0}>
                              <SelectItem value="" text={smaller.length ? '— no change —' : 'already smallest'} />
                              {smaller.map((s) => (
                                <SelectItem key={s} value={s} text={s} />
                              ))}
                            </Select>
                          </div>
                        </div>
                      )
                    })}
                  </div>
                </FormGroup>
              )}
              <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.8rem' }}>
                Scales the chosen components down. Approval-governed; nothing is resized in mock mode.
              </p>
            </>
          ) : (
            <>
              <div id="section-basics" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
              {isClone && (
                <>
                  <Select id="clone_source" labelText="Environment to clone" value={sourceRef}
                    onChange={(e) => onCloneSourceChange(e.target.value)}
                    invalid={!!errors.source_reference} invalidText={errors.source_reference}>
                    <SelectItem value="" text="— select a provisioned environment —" />
                    {provisioned.map((p) => (
                      <SelectItem key={p.reference} value={p.reference}
                        text={`${p.reference} — ${p.environment_name || 'env'} (${p.environment_tier || '?'})`} />
                    ))}
                  </Select>
                  {provisioned.length === 0 ? (
                    <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.85rem' }}>
                      You have no provisioned environments to clone.
                    </p>
                  ) : sourceRef ? (
                    <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.8rem' }}>
                      The stack, deployment target and classification below are copied from the source —
                      give the clone a new name (and adjust anything if needed). Approval-governed;
                      nothing is provisioned in mock mode.
                    </p>
                  ) : null}
                </>
              )}
              {isDr && (
                <>
                  <Select id="dr_source" labelText="Environment to protect (primary)" value={sourceRef}
                    onChange={(e) => { onCloneSourceChange(e.target.value); setEnvTier(e.target.value ? 'dr' : '') }}
                    invalid={!!errors.source_reference} invalidText={errors.source_reference}>
                    <SelectItem value="" text="— select a provisioned environment —" />
                    {provisioned.map((p) => (
                      <SelectItem key={p.reference} value={p.reference}
                        text={`${p.reference} — ${p.environment_name || 'env'} (${p.environment_tier || '?'})`} />
                    ))}
                  </Select>
                  {provisioned.length === 0 ? (
                    <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.85rem' }}>
                      You have no provisioned environments to protect.
                    </p>
                  ) : sourceRef ? (
                    <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.8rem' }}>
                      The DR replica copies the primary's stack and classification — give it a new name and
                      choose its DR deployment target (a different region for resilience). It's a prod-class
                      (DR-tier) environment. Approval-governed; nothing is provisioned in mock mode.
                    </p>
                  ) : null}
                </>
              )}
              {isCreateLike && (
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

              <div>
                <label style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', display: 'block', marginBottom: '0.35rem' }}>
                  Deployment target
                </label>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(5, minmax(0, 1fr))', gap: '0.5rem' }}>
                  {TARGETS.map(([v]) => {
                    const Icon = v === 'onprem' ? Enterprise : Cloud
                    const sel = target === v
                    return (
                      <button key={v} type="button" aria-pressed={sel} onClick={() => setTarget(v)}
                        style={{ ...cardStyle(sel), alignItems: 'center', textAlign: 'center', padding: '0.6rem 0.4rem' }}>
                        <Icon size={20} style={{ color: 'var(--cds-icon-primary)' }} />
                        <span style={{ fontWeight: 500, fontSize: '0.82rem', lineHeight: 1.2 }}>{TARGET_SHORT[v] || v}</span>
                      </button>
                    )
                  })}
                </div>
                {errors.deployment_target && (
                  <p style={{ color: 'var(--cds-text-error)', fontSize: '0.75rem', marginTop: '0.35rem' }}>
                    {errors.deployment_target}
                  </p>
                )}
              </div>

              {isCreateLike ? (
                <TextInput id="environment_name" labelText="New environment name" placeholder="e.g. egate-uat" value={envName} onChange={(e) => setEnvName(e.target.value)} invalid={!!errors.environment_name} invalidText={errors.environment_name} />
              ) : (
                <Select id="target_environment" labelText="Existing environment" value={targetEnv} onChange={(e) => setTargetEnv(e.target.value)} invalid={!!errors.target_environment} invalidText={errors.target_environment}>
                  <SelectItem value="" text="— select —" />
                  {lookups.environments.map((e) => (
                    <SelectItem key={e.name} value={e.name} text={`${e.name} (${e.environment_class})`} />
                  ))}
                </Select>
              )}

              {isCreateLike && (
                <Select id="environment_tier" labelText="Environment tier" value={envTier} onChange={(e) => setEnvTier(e.target.value)} disabled={isDr} invalid={!!errors.environment_tier} invalidText={errors.environment_tier}>
                  <SelectItem value="" text="— select —" />
                  {(isDr
                    ? ENV_TIERS.filter(([v]) => v === 'dr')
                    : isSandbox || isTemporary
                      ? ENV_TIERS.filter(([v]) => NONPROD_TIERS.includes(v))
                      : ENV_TIERS
                  ).map(([v, label]) => (
                    <SelectItem key={v} value={v} text={label} />
                  ))}
                </Select>
              )}

              {isCreateLike && (
                <Select id="data_classification" labelText="Data classification" value={classification} onChange={(e) => setClassification(e.target.value)} invalid={!!errors.data_classification} invalidText={errors.data_classification}>
                  <SelectItem value="" text="— select —" />
                  {CLASSIFICATIONS.map((c) => (
                    <SelectItem key={c} value={c} text={c} />
                  ))}
                </Select>
              )}

              {isTemporary && (
                <DatePicker datePickerType="single" dateFormat="Y-m-d" minDate={TODAY} value={expiresOn}
                  onChange={(dates: Date[]) => setExpiresOn(dates[0] ? fmtDate(dates[0]) : '')}>
                  <DatePickerInput id="expires_on" labelText="Expires on"
                    placeholder="yyyy-mm-dd" invalid={!!errors.expires_on} invalidText={errors.expires_on} />
                </DatePicker>
              )}
              {isSandbox && (
                <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)' }}>
                  A sandbox is a short-lived non-prod environment — it's given a short lifetime and
                  auto-expires (warned before, then reclaimed when TTL enforcement is on).
                </p>
              )}
              </div>

              <div id="section-stack">
              <FormGroup legendText="Components">
                {errors.components && (
                  <p style={{ color: 'var(--cds-text-error)', fontSize: '0.75rem', marginBottom: '0.5rem' }}>{errors.components}</p>
                )}
                <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.6rem' }}>
                  {target
                    ? `Technologies available on ${TARGETS.find(([v]) => v === target)?.[1] || target}. Pick one or more, then choose a size for each.`
                    : 'Pick a deployment target above to see its technologies, then choose sizes.'}
                </p>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(9.5rem, 1fr))', gap: '0.5rem' }}>
                  {availableTechs.map((t) => {
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
                        {/* How it actually gets delivered on the selected target.
                            Only meaningful once a target is chosen. */}
                        {target && (
                          <Tag
                            type={(t.automated_targets || []).includes(target) ? 'green' : 'gray'}
                            size="sm"
                            style={{ margin: 0 }}
                            title={
                              (t.automated_targets || []).includes(target)
                                ? 'The portal provisions this automatically.'
                                : 'The infrastructure team fulfils this after approval.'
                            }
                          >
                            {(t.automated_targets || []).includes(target) ? 'automated' : 'manual'}
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
                          {(() => {
                            const opts = techOptions[c.technology_code]
                            const presets = opts?.presets ?? {}
                            const active = presetName(c)
                            return (
                              <>
                                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(8rem, 1fr))', gap: '0.5rem' }}>
                                  {SIZES.map((s) => {
                                    // Server presets when we have them; the static
                                    // table only until the fetch lands, so the card
                                    // is never blank on first paint.
                                    const spec = presets[s] ?? {
                                      vcpu: SIZE_SPECS[s].vcpu,
                                      memory_gb: SIZE_SPECS[s].mem,
                                      storage_gb: SIZE_SPECS[s].storage,
                                    }
                                    const sel = active === s
                                    return (
                                      <button key={s} type="button" aria-pressed={sel} onClick={() => setSize(c.technology_code, s)} style={cardStyle(sel)}>
                                        <span style={{ fontWeight: 500, textTransform: 'capitalize', fontSize: '0.85rem' }}>{s}</span>
                                        <span style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', lineHeight: 1.5 }}>
                                          {spec.vcpu} vCPU · {spec.memory_gb} GB RAM<br />{spec.storage_gb} GB storage
                                        </span>
                                      </button>
                                    )
                                  })}
                                </div>

                                {/* The detail fields. Every option here came from
                                    the server and is re-checked on submit. */}
                                {opts && (
                                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(9rem, 1fr))', gap: '0.75rem', marginTop: '0.75rem' }}>
                                    {['version', 'image', 'vcpu', 'memory_gb', 'storage_gb'].map((field) => {
                                      const spec = opts.fields[field]
                                      if (!spec) return null  // nothing honest to offer
                                      const value = String(
                                        (c as unknown as Record<string, unknown>)[field] ?? '',
                                      )
                                      const errKey = `component_${components.findIndex((x) => x.technology_code === c.technology_code)}_${field}`
                                      return (
                                        <Select
                                          key={field}
                                          id={`detail-${c.technology_code}-${field}`}
                                          labelText={spec.label}
                                          size="sm"
                                          value={value}
                                          invalid={!!errors[errKey]}
                                          invalidText={errors[errKey]}
                                          onChange={(e) => setDetail(c.technology_code, field, e.target.value)}
                                        >
                                          {spec.options.map((o) => (
                                            <SelectItem key={o.value} value={o.value} text={o.label} />
                                          ))}
                                        </Select>
                                      )
                                    })}
                                  </div>
                                )}

                                {opts && !opts.installable && (
                                  <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-error)', marginTop: '0.5rem' }}>
                                    {techName(c.technology_code)} cannot be installed on
                                    a {opts.os_family} image. Choose a different OS image,
                                    or remove this component.
                                  </p>
                                )}

                                {opts && opts.os_family && opts.installable && !opts.fields.version && (
                                  <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.4rem' }}>
                                    This OS installs whichever version its release carries,
                                    so there is no version to choose.
                                  </p>
                                )}

                                {active === 'custom' && (
                                  <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.4rem' }}>
                                    Custom shape — not one of the standard sizes. Allowed, priced
                                    as configured, and flagged for the approver.
                                  </p>
                                )}
                              </>
                            )
                          })()}
                        </div>
                      )
                    })}
                  </div>
                )}
              </FormGroup>
              </div>

              <div id="section-details" style={{ display: 'flex', flexDirection: 'column', gap: '1.5rem' }}>
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
                  <TextInput id="owner_group" labelText="Owning group (F-IAM-09)" placeholder="directory/Jira group — survives staff movement" value={ownerGroup} onChange={(e) => setOwnerGroup(e.target.value)} />
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
              </div>
            </>
          )}

          <div
            style={{
              position: 'sticky',
              bottom: 0,
              zIndex: 10,
              display: 'flex',
              gap: '0.75rem',
              alignItems: 'center',
              padding: '0.75rem 0',
              background: 'var(--cds-background)',
              borderTop: '1px solid var(--cds-border-subtle)',
            }}
          >
            <Button kind="secondary" onClick={onSaveDraft} disabled={busy}>
              Save draft
            </Button>
            <Button onClick={onSubmit} disabled={busy}>
              {isDecommission ? 'Submit decommission' : isRefresh ? 'Submit refresh' : isRestore ? 'Submit restore' : isReduce ? 'Submit reduction' : isClone ? 'Submit clone' : isSandbox ? 'Submit sandbox' : isTemporary ? 'Submit temporary' : isDr ? 'Submit DR request' : 'Submit request'}
            </Button>
            {cost && !isDecommission && (
              <span style={{ marginLeft: 'auto', fontSize: '0.9rem', color: 'var(--cds-text-secondary)' }}>
                <strong style={{ color: 'var(--cds-text-primary)' }}>{cost.totals.monthly.toFixed(2)} {cost.currency}</strong>/mo
              </span>
            )}
          </div>
        </Stack>
      </div>

      <div style={{ flex: '0 0 20rem' }}>
        {!isDecommission && !isRefresh && !isRestore && !isReduce && (
          <Tile style={{ marginBottom: '1rem', borderTop: '3px solid var(--cds-border-interactive)' }}>
            <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em', marginBottom: '0.5rem' }}>
              Environment summary
            </p>
            <div style={{ fontSize: '0.82rem', lineHeight: 1.9 }}>
              <SummaryRow label="Type">{RTYPE_LABEL[requestType] || requestType}</SummaryRow>
              {isClone && <SummaryRow label="Cloned from">{sourceRef || '—'}</SummaryRow>}
              {isDr && <SummaryRow label="DR of">{sourceRef || '—'}</SummaryRow>}
              {isTemporary && <SummaryRow label="Expires on">{expiresOn || '—'}</SummaryRow>}
              {isSandbox && <SummaryRow label="Lifetime">short-lived (auto-expires)</SummaryRow>}
              <SummaryRow label="Target">{TARGETS.find(([v]) => v === target)?.[1] || '—'}</SummaryRow>
              {isCreateLike ? (
                <>
                  <SummaryRow label="Environment">{envName || '—'}</SummaryRow>
                  <SummaryRow label="Tier">
                    {envTier ? (
                      <>
                        {ENV_TIERS.find(([v]) => v === envTier)?.[1] || envTier}
                        <Tag type={NONPROD_TIERS.includes(envTier) ? 'teal' : 'blue'} size="sm" style={{ marginLeft: '0.4rem' }}>
                          {NONPROD_TIERS.includes(envTier) ? 'non-prod' : 'prod-class'}
                        </Tag>
                      </>
                    ) : '—'}
                  </SummaryRow>
                  <SummaryRow label="Classification">{classification || '—'}</SummaryRow>
                </>
              ) : (
                <SummaryRow label="Environment">{targetEnv || '—'}</SummaryRow>
              )}
              <SummaryRow label="Technologies">
                {filledComponents.length
                  ? `${filledComponents.length} — ${filledComponents.map((c) => `${techName(c.technology_code)}${c.size ? ` (${c.size})` : ''}`).join(', ')}`
                  : '—'}
              </SummaryRow>
              {cost && <SummaryRow label="Est. monthly"><strong>{cost.totals.monthly.toFixed(2)} {cost.currency}</strong></SummaryRow>}
              {filledComponents.length > 0 && target && (
                <SummaryRow label="Delivery">
                  {manualComponents.length === 0 ? (
                    <Tag type="green" size="sm" style={{ margin: 0 }}>fully automated</Tag>
                  ) : (
                    <Tag type="gray" size="sm" style={{ margin: 0 }}>
                      {manualComponents.length} of {filledComponents.length} fulfilled by the infra team
                    </Tag>
                  )}
                </SummaryRow>
              )}
            </div>
            {manualComponents.length > 0 && (
              <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
                After approval, the infrastructure team provisions{' '}
                {manualComponents.map((t) => t.name).join(', ')} — {target ? TARGET_SHORT[target] || target : 'this target'}{' '}
                has no automated build for {manualComponents.length > 1 ? 'these' : 'this'} yet. The portal still validates,
                prices, routes the approval and records the audit trail.
              </p>
            )}
            {isCreateLike && envTier && NONPROD_TIERS.includes(envTier) && (
              <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
                Non-prod environments have a limited lifetime (auto-expire per the TTL policy) and can be auto-shut-down out of hours.
              </p>
            )}
          </Tile>
        )}
        <Tile style={{ borderTop: '3px solid var(--cds-border-interactive)' }}>
          <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em' }}>
            {isDecommission ? 'Monthly cost to free' : isRefresh ? 'Environment refresh' : isRestore ? 'Environment restore' : isReduce ? 'Reduced components — new monthly cost' : 'Live cost'}
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
                : isRefresh
                ? 'Refresh reuses the target environment — no new monthly cost. It copies data from a higher environment down, masking sensitive data.'
                : isRestore
                ? 'Restore reuses the target environment — no new monthly cost. It rolls the environment back to the selected backup (approval-governed, verified).'
                : isReduce
                ? 'Pick a provisioned environment and choose a smaller size for a component to see the reduced monthly cost.'
                : 'Choose a deployment target and add a component to see the cost.'}
            </p>
          )}

          {cost && !isDecommission && (
            <div style={{ marginTop: '1rem', borderTop: '1px solid var(--cds-border-subtle)', paddingTop: '0.6rem' }}>
              <Button kind="ghost" size="sm" onClick={onExplainCost} disabled={explainBusy}>
                {explainBusy ? 'Explaining…' : 'Explain this cost'}
              </Button>
              {explain && (
                <div style={{ marginTop: '0.5rem', fontSize: '0.82rem', lineHeight: 1.5 }}>
                  <p style={{ margin: '0 0 0.4rem' }}>{explain.summary}</p>
                  {explain.tips.length > 0 && (
                    <ul style={{ margin: 0, paddingLeft: '1.1rem', color: 'var(--cds-text-secondary)' }}>
                      {explain.tips.map((t, i) => (
                        <li key={i} style={{ marginBottom: '0.25rem' }}>{t}</li>
                      ))}
                    </ul>
                  )}
                </div>
              )}
            </div>
          )}
        </Tile>

        {approvalInfo && (
          <Tile style={{ marginTop: '1rem', borderTop: '3px solid var(--cds-border-interactive)' }}>
            <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', textTransform: 'uppercase', letterSpacing: '0.02em', marginBottom: '0.25rem' }}>
              Approval summary
            </p>
            <p style={{ fontSize: '0.82rem', margin: '0 0 0.5rem' }}>
              Submitting raises a ticket in <strong>{approvalInfo.system_of_record}</strong> for approval —
              nothing is provisioned until it's approved.
            </p>
            <div style={{ fontSize: '0.82rem', lineHeight: 1.9 }}>
              <SummaryRow label="Approvals needed">
                {approvalInfo.quorum} approver{approvalInfo.quorum > 1 ? 's' : ''}
              </SummaryRow>
              <SummaryRow label="Response SLA">{approvalInfo.sla_hours}h</SummaryRow>
              {(approvalInfo.four_eyes || approvalInfo.sod_enforced) && (
                <SummaryRow label="Self-approval"><Tag type="gray" size="sm">blocked — not you</Tag></SummaryRow>
              )}
              {approvalInfo.change_window.enabled && (
                <SummaryRow label="Change window">
                  {approvalInfo.change_window.days} {approvalInfo.change_window.start}–{approvalInfo.change_window.end} {approvalInfo.change_window.tz}
                  <Tag type={approvalInfo.change_window.open_now ? 'green' : 'gray'} size="sm" style={{ marginLeft: '0.4rem' }}>
                    {approvalInfo.change_window.open_now ? 'open now' : 'closed now'}
                  </Tag>
                </SummaryRow>
              )}
            </div>
            {approvalInfo.change_window.enabled && (
              <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
                Once approved, it provisions inside the change window.
              </p>
            )}
          </Tile>
        )}

        {!isDecommission && (
          <Tile style={{ marginTop: '1rem', borderTop: '3px solid var(--cds-border-interactive)' }}>
            <p style={{ fontWeight: 600, margin: '0 0 0.25rem' }}>Draft with AI</p>
            <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.6rem' }}>
              Describe what you need in plain English — the assistant fills the form in for you to
              review. It only suggests a draft; it never submits, prices, or provisions.
            </p>
            <TextArea
              id="ai-description"
              labelText="Describe your request"
              placeholder="e.g. a medium Postgres database for the eGate UAT environment, on-prem, internal data"
              rows={3}
              value={aiText}
              onChange={(e) => setAiText(e.target.value)}
            />
            <div style={{ marginTop: '0.5rem', display: 'flex', gap: '0.5rem', flexWrap: 'wrap' }}>
              <Button size="sm" onClick={onDraftWithAI} disabled={aiBusy || aiText.trim().length < 8}>
                {aiBusy ? 'Drafting…' : 'Draft with AI'}
              </Button>
              {isCreateLike && (
                <Button
                  size="sm"
                  kind="tertiary"
                  onClick={onRecommend}
                  disabled={recBusy || aiText.trim().length < 8}
                >
                  {recBusy ? 'Comparing clouds…' : 'Recommend cloud & size'}
                </Button>
              )}
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
            {recErr && (
              <InlineNotification
                kind="error"
                lowContrast
                title="Could not recommend"
                subtitle={recErr}
                onCloseButtonClick={() => setRecErr(null)}
                style={{ marginTop: '0.75rem', maxWidth: 'none' }}
              />
            )}
            {rec && (
              <div style={{ marginTop: '0.85rem' }}>
                <p style={{ fontWeight: 600, fontSize: '0.85rem', margin: '0 0 0.15rem' }}>
                  Recommended stack{rec.mode ? ` · ${rec.mode}` : ''}
                </p>
                {rec.components.length ? (
                  <div style={{ display: 'flex', gap: '0.35rem', flexWrap: 'wrap', margin: '0.2rem 0 0.5rem' }}>
                    {rec.components.map((c) => (
                      <Tag key={c.technology_code} type="cool-gray" size="sm">
                        {c.technology_code} · {c.size}
                      </Tag>
                    ))}
                  </div>
                ) : (
                  <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0.2rem 0 0.5rem' }}>
                    No catalogue technology matched — describe the workload's stack.
                  </p>
                )}
                {rec.rationale && (
                  <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.6rem' }}>
                    {rec.rationale}
                  </p>
                )}
                {rec.comparison.length > 0 && (
                  <>
                    <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.35rem' }}>
                      Same stack, priced by the portal on every cloud it can run on. Cheapest first.
                    </p>
                    <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
                      <thead>
                        <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)' }}>
                          <th style={{ padding: '0.3rem 0.4rem', fontWeight: 500 }}>Cloud</th>
                          <th style={{ padding: '0.3rem 0.4rem', fontWeight: 500, textAlign: 'right' }}>Monthly</th>
                          <th style={{ padding: '0.3rem 0.4rem', fontWeight: 500, textAlign: 'right' }}>One-time</th>
                          <th style={{ padding: '0.3rem 0.4rem' }} />
                        </tr>
                      </thead>
                      <tbody>
                        {rec.comparison.map((row) => {
                          const isRec = row.target === rec.recommended_target
                          return (
                            <tr
                              key={row.target}
                              style={{
                                borderTop: '1px solid var(--cds-border-subtle)',
                                background: isRec ? 'var(--cds-layer-accent)' : undefined,
                              }}
                            >
                              <td style={{ padding: '0.35rem 0.4rem' }}>
                                {TARGET_SHORT[row.target] || row.target}
                                {isRec && (
                                  <Tag type="green" size="sm" style={{ marginLeft: '0.4rem' }}>
                                    best value
                                  </Tag>
                                )}
                              </td>
                              <td style={{ padding: '0.35rem 0.4rem', textAlign: 'right', fontWeight: isRec ? 600 : 400 }}>
                                {row.monthly.toFixed(2)} {row.currency}
                              </td>
                              <td style={{ padding: '0.35rem 0.4rem', textAlign: 'right', color: 'var(--cds-text-secondary)' }}>
                                {row.one_time.toFixed(2)}
                              </td>
                              <td style={{ padding: '0.35rem 0.4rem', textAlign: 'right' }}>
                                <Button size="sm" kind="ghost" onClick={() => useRecommendation(row.target)}>
                                  Use this
                                </Button>
                              </td>
                            </tr>
                          )
                        })}
                      </tbody>
                    </table>
                  </>
                )}
                {(rec.notes.length > 0 || rec.warnings.length > 0) && (
                  <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
                    {[...rec.notes, ...rec.warnings].join('  ·  ')}
                  </p>
                )}
                <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.4rem' }}>
                  A suggestion only — nothing is submitted or provisioned. The portal re-prices and
                  re-validates everything when you submit.
                </p>
              </div>
            )}
          </Tile>
        )}
      </div>
    </div>
  )
}
