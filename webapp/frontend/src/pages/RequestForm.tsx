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
  getDraft,
  getPlacementOptions,
  resolvePlacement,
  type Lookups,
  type Component,
  type ComponentOptions,
  type Cost,
  type RequestRow,
  type ApprovalInfo,
  type AiRecommendation,
  type PlacementOption,
  type ProposedHost,
  type RecordedPlacement,
  type SavedRequest,
} from '../api'
import PlacementStep from '../components/PlacementStep'

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
// WHAT THE SERVER STORES A TIER AS, BACK INTO WHAT THIS FORM OFFERS (P.11a).
//
// The vocabulary changed on 16 Aug 2026 and this dropdown did not follow it. The
// API accepts the old spellings and stores the canonical name — `normalise_tier`
// in api/validation.py, which is deliberately liberal so existing integrations
// keep working — so a draft saved as 'uat' comes back as 'UAT', which matches no
// option above. Without this, resuming a draft put a value in the tier control
// that it could not display: the requester's tier would look blank and the next
// save would send whatever they picked instead.
//
// A TRANSLATION ON THE WAY IN, NOT A SECOND VOCABULARY. Nothing here decides
// what a tier is; the server still normalises whatever this form sends, and an
// unrecognised value is passed through untouched rather than guessed at.
//
// The folding is lossy at the source and cannot be undone here: the server maps
// BOTH 'sit' and 'preprod' onto tiers that no longer distinguish them, so a
// resumed 'preprod' draft correctly reads as UAT — that is the tier the request
// actually has now.
const TIER_FROM_STORED: Record<string, string> = {
  Development: 'dev',
  Test: 'test',
  'Pre-Test': 'sit',
  UAT: 'uat',
  Production: 'prod',
}

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
  'platform-service': 'Platform service',
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
//
// COMPACT SINCE 2026-09-05. These were 9.5rem wide and carried a full-size
// "automated" pill, so four fitted a row and the pill outweighed the name it
// described. The catalogue has grown past forty entries and is grouped now, so
// the tile has to be scannable in bulk rather than readable in isolation.
//
// A FIXED HEIGHT is what makes a grid look deliberate: names run to one or two
// lines, and without it every row sets its own height and the whole block reads
// as ragged. Two lines is the cap, the full name is always in the tooltip, and
// nothing is clipped without somewhere to read it.
const cardStyle = (selected: boolean): CSSProperties => ({
  display: 'flex',
  flexDirection: 'column',
  gap: '0.25rem',
  padding: '0.45rem 0.5rem',
  minHeight: '4.15rem',
  textAlign: 'left',
  cursor: 'pointer',
  width: '100%',
  borderRadius: 0,
  color: 'var(--cds-text-primary)',
  background: selected ? 'var(--cds-layer-selected)' : 'var(--cds-layer)',
  border: `1px solid ${selected ? 'var(--cds-border-interactive)' : 'var(--cds-border-subtle)'}`,
  boxShadow: selected ? 'inset 0 0 0 1px var(--cds-border-interactive)' : 'none',
})

export default function RequestForm({
  initialType = 'create',
  resumeRef = null,
}: {
  initialType?: string
  // The reference of a saved draft to reopen, from #/request/resume/<ref>.
  // Null for a new request, which is every other route into this form.
  resumeRef?: string | null
}) {
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
  // The draft this form is editing, once it has been saved once. Sent back on
  // every later save so the server UPDATES it instead of creating another:
  // POST /api/requests/draft with no reference means "new draft", so without
  // this every click of Save or Submit minted a fresh one. Invisible while
  // submits succeed — one draft, immediately submitted — and very visible after
  // nine rejected attempts left nine abandoned drafts behind.
  const [draftRef, setDraftRef] = useState<string | null>(null)
  const [busy, setBusy] = useState(false)

  // RESUMING A SAVED DRAFT (P.11a, F-UX-01). `resuming` is true only while the
  // fetch is in flight; `resumeError` holds the sentence explaining why a draft
  // could not be reopened, which is a thing that must be SAID rather than left
  // as an empty form the requester mistakes for their own work having vanished.
  const [resuming, setResuming] = useState(!!resumeRef)
  const [resumeError, setResumeError] = useState<string | null>(null)
  const [resumed, setResumed] = useState<string | null>(null)
  // The layout this draft was saved with, held separately from the computed
  // options: it is what the server RECORDED, not something worked out again
  // here. Applied to the chosen-option state by an effect below — see the
  // comment there for why it cannot simply be set during the load.
  const [resumedPlacement, setResumedPlacement] = useState<RecordedPlacement | null>(null)
  const [pendingPlacement, setPendingPlacement] = useState<RecordedPlacement | null>(null)

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

  // PLACEMENT (P.11). `null` means not asked yet, which is not the same as "no
  // layouts" -- an empty array is the server's answer and gets its own sentence.
  // The chosen key is held here and resolved server-side at submit: the browser
  // remembers a preference, it never records a decision.
  const [placement, setPlacement] = useState<PlacementOption[] | null>(null)
  const [placementChosen, setPlacementChosen] = useState<string | null>(null)
  const [placementCluster, setPlacementCluster] = useState<string | null>(null)
  const [placementBusy, setPlacementBusy] = useState(false)
  const [placementError, setPlacementError] = useState<string | null>(null)
  // A layout the requester arranged in the diagram, if they did. Held as hosts
  // rather than a key, because the server has no key for something it did not
  // enumerate — it re-judges the arrangement itself at submit.
  const [arrangedHosts, setArrangedHosts] = useState<ProposedHost[] | null>(null)

  useEffect(() => {
    getLookups().then(setLookups).catch(() => setLookups(null))
    getMe().then((m) => setEmail(m?.email ?? null))
    getApprovalInfo().then(setApprovalInfo).catch(() => setApprovalInfo(null))
  }, [])

  // REOPEN A SAVED DRAFT (P.11a, F-UX-01).
  //
  // WHAT WAS WRONG. This form told people "Draft saved as REQ-2026-0001 — you
  // can resume it later" and then had no way to resume anything: the reference
  // lived in `draftRef` and nowhere else, so leaving the page lost the contents
  // of a draft that was sitting in the database the whole time. The promise was
  // true of the row and false of the portal.
  //
  // THE IDENTITY IS FETCHED ALONGSIDE THE DRAFT rather than read from `email`,
  // which the effect above is still filling in. Reading a half-loaded identity
  // would decide whose draft this is against `null` and get it wrong on a slow
  // connection — and getting it wrong in the lenient direction is opening
  // somebody else's request in an editable form.
  //
  // THIS CHECK IS COURTESY, NOT SECURITY. The API refuses a draft save against a
  // request that is not the caller's; this exists so the refusal arrives before
  // the typing rather than after it.
  useEffect(() => {
    if (!resumeRef) return
    let cancelled = false
    setResuming(true)
    setResumeError(null)
    Promise.all([getDraft(resumeRef), getMe()])
      .then(([{ status, body }, me]) => {
        if (cancelled) return
        setResuming(false)
        if (status === 404) {
          setResumeError(`There is no request called ${resumeRef}.`)
          return
        }
        if (status !== 200) {
          setResumeError(body?.detail || `The draft could not be opened (HTTP ${status}).`)
          return
        }
        const saved = body as SavedRequest
        if (me?.email && saved.requester && me.email.toLowerCase() !== saved.requester.toLowerCase()) {
          setResumeError(
            `${saved.reference} belongs to ${saved.requester}. You can only resume your own drafts.`,
          )
          return
        }
        // A SUBMITTED REQUEST IS NOT A DRAFT, and opening one in an editable
        // form would invite an edit the API will refuse — after the typing.
        // Said plainly here instead.
        if (saved.status !== 'draft') {
          setResumeError(
            `${saved.reference} was already submitted and is now '${saved.status}', so it can ` +
              'no longer be edited. Raise a new request instead.',
          )
          return
        }
        applySaved(saved)
      })
      .catch(() => {
        if (cancelled) return
        setResuming(false)
        setResumeError('The draft could not be opened. Check your connection and try again.')
      })
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [resumeRef])

  // Put a saved request back into the form it was typed into.
  //
  // `draftRef` IS SET FIRST AND DELIBERATELY. Every later save sends it, so the
  // server UPDATES this draft instead of minting another; without it, resuming a
  // draft and saving would leave two rows where the requester believes there is
  // one, which is the same duplication `draftRef` was added to stop.
  function applySaved(d: SavedRequest) {
    setDraftRef(d.reference)
    setResumed(d.reference)
    if (d.request_type) setRequestType(d.request_type)
    setProjectCode(d.project_code || '')
    setCostCentre(d.cost_centre_code || '')
    setSubsidiary(d.subsidiary || '')
    setTarget(d.deployment_target || '')
    setEnvName(d.environment_name || '')
    setEnvTier(d.environment_tier ? TIER_FROM_STORED[d.environment_tier] ?? d.environment_tier : '')
    setTargetEnv(d.target_environment || '')
    setClassification(d.data_classification || '')
    setJustification(d.business_justification || '')
    setPriority(d.priority || '')
    setCriticality(d.business_criticality || '')
    setDeliveryDate(d.required_delivery_date || '')
    setExpiresOn(d.expires_on || '')
    setAppOwner(d.application_owner || '')
    setBizOwner(d.business_owner || '')
    setTechOwner(d.technical_owner || '')
    setEnvOwner(d.environment_owner || '')
    setOwnerGroup(d.owner_group || '')
    setSourceRef(d.source_reference || '')
    setRefreshFrom(d.refresh_from_reference || '')
    setRestoreBackupId(d.restore_backup_id != null ? String(d.restore_backup_id) : '')

    // Only the values this form can actually render. `advanced_options` is a
    // free-form JSON bag on the server, so a number or a nested object could
    // come back into a control that expects a string or a checkbox.
    const adv: Record<string, string | boolean> = {}
    for (const [k, v] of Object.entries(d.advanced_options || {}))
      if (typeof v === 'string' || typeof v === 'boolean') adv[k] = v
    setAdvanced(adv)

    const saved = (d.components || []).filter((c) => c.technology_code)
    setComponents(
      saved.map((c) => ({
        technology_code: c.technology_code as string,
        size: c.size,
        version: c.version ?? undefined,
        image: c.image ?? undefined,
        vcpu: c.vcpu ?? undefined,
        memory_gb: c.memory_gb ?? undefined,
        storage_gb: c.storage_gb ?? undefined,
      })),
    )
    // Decommission and reduce do not hold their choice in `components` — they
    // derive it from the environment being operated on — so the same saved rows
    // have to be read back into the state each of those screens actually reads.
    // Restoring only `components` would reopen a decommission draft with nothing
    // ticked, which reads as "you selected nothing" rather than "we lost it".
    setSelected(
      new Set(
        saved.map((c) => compKey({ technology_code: c.technology_code as string, size: c.size ?? '' })),
      ),
    )
    setReduceSizes(
      Object.fromEntries(saved.filter((c) => c.size).map((c) => [c.technology_code as string, c.size as string])),
    )

    // Handed to the effect below rather than applied here — see the comment on
    // it. Setting the chosen layout at this point would be undone in the same
    // render by the rule that drops a layout when the stack changes, and loading
    // a draft changes the stack, the target and the tier all at once.
    setPendingPlacement(d.placement ?? null)
  }

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
  // A capability, not a component: nothing to size, nothing to price, and no
  // build path to fail at. What it carries is the justification, the approval
  // and the audit trail — the reason to raise it here rather than by email.
  const isPlatformService = requestType === 'platform-service'
  const NONPROD_TIERS = ['dev', 'test', 'sit', 'uat', 'preprod']

  // Load the user's provisioned requests once an env-targeting type is chosen
  // (decommission/refresh/restore/reduce operate on one; clone copies one).
  useEffect(() => {
    if ((isDecommission || isRefresh || isRestore || isClone || isReduce || isDr) && email) {
      // Anything with a resource still active — not just requests whose
      // status says 'provisioned'. A machine that failed its boot
      // verification is still a machine, and still billing.
      getRequests({ requester: email, decommissionable: true })
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
  // A platform service buys the infrastructure team's time, not a cloud
  // resource. An empty target stops the cost fetch entirely — the same way
  // refresh and restore do — so nothing shows a figure where there is none.
  // Showing 0.00 here would be the fiction this portal spent a day removing.
  const pricedTarget = isPlatformService || isRefresh || isRestore ? '' : (isDecommission || isReduce) ? sourceObj?.deployment_target ?? '' : target
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

  // A LAYOUT FOR A STACK THAT NO LONGER EXISTS IS WORSE THAN NO LAYOUT. Adding a
  // component, changing a size, or switching target or tier changes what the
  // machines must be and what OPA says about them, so the computed options are
  // dropped rather than left on screen looking current. Keeping a stale
  // "consolidated -- 412.00 AED/mo" beside a stack it was never costed for is how
  // a figure nobody calculated reaches an approver.
  //
  // Keyed on the same inputs the API uses: the components, the target and the
  // environment tier. Not on the justification or the owners, which cannot move
  // a machine.
  const placementKey = JSON.stringify([target, envTier, pricedComponents])
  useEffect(() => {
    setPlacement(null)
    setPlacementChosen(null)
    setPlacementCluster(null)
    setPlacementError(null)
    // The arrangement described the OLD components. Keeping it would send the
    // server a layout placing things the request no longer asks for, which it
    // would rightly refuse — after the requester had already pressed submit.
    setArrangedHosts(null)
    // The layout a resumed draft came back with goes too, and for exactly the
    // same reason: it was recorded against the stack that was saved, so once
    // that stack changes it describes an arrangement of components this request
    // no longer has. The row in the database is untouched -- /api/placement/
    // resolve supersedes it if a new layout is chosen -- but it stops being
    // shown as though it still described this form.
    setResumedPlacement(null)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [placementKey])

  // PUTTING BACK THE LAYOUT A RESUMED DRAFT WAS SAVED WITH (P.11a).
  //
  // WHY THIS IS A SEPARATE EFFECT, AND WHY IT IS DECLARED HERE RATHER THAN
  // ANYWHERE ELSE. The effect immediately above drops the chosen layout whenever
  // the stack, the target or the tier changes -- and loading a draft changes all
  // three in one go. Setting the choice while loading would therefore be undone
  // microseconds later by a rule written about a requester editing the form, and
  // the draft would resume having silently forgotten its own placement: exactly
  // the defect this increment exists to fix, reintroduced one layer down.
  //
  // React runs effects in the order they are declared, so in the render where
  // both fire, the drop runs first and this puts back what the SERVER recorded.
  // Moving this above the drop would break it silently -- no type error, no
  // warning, just a layout that vanishes on load.
  //
  // It is consumed once. Nothing here re-decides anything: the choice is still
  // re-resolved by the API at submit, which is where a layout that has since
  // become impermissible is refused.
  useEffect(() => {
    if (!pendingPlacement) return
    setResumedPlacement(pendingPlacement)
    setPlacementChosen(pendingPlacement.option_key)
    setPlacementCluster(pendingPlacement.cluster_id ?? null)
    setPendingPlacement(null)
  }, [pendingPlacement])

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
    // A capability has no shape to choose: no size, no image, no version. Asking
    // the catalogue for its options would 404 on something that is not a
    // provisionable component.
    if (!codes.length || isPlatformService) return
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
      setComponents((cs) =>
        cs.map((c) => {
          const opts = fetched[c.technology_code]
          if (!opts) return c
          const next: Component = { ...c }

          // CLEAR any value whose field the server no longer offers. Picking an
          // Ubuntu image withdraws the version dropdown, and a version left
          // behind from before that choice is a value the server refuses — under
          // a field that is no longer on screen, so the requester sees a submit
          // that does nothing at all. Withdrawing the control has to withdraw
          // the value with it.
          for (const field of TEXT_DETAIL_FIELDS) {
            const key = field as 'version' | 'image'
            const spec = opts.fields?.[field]
            const held = next[key]
            if (!held) continue
            // Cleared when the field is withdrawn entirely, AND when the value
            // held is no longer among the offered options — an OS image list
            // narrows to what the technology runs on, so a value chosen before
            // that narrowing would otherwise survive invisibly and be refused at
            // submit with nothing on screen to point at.
            if (!spec || !spec.options.some((o) => o.value === held)) {
              delete next[key]
            }
          }

          // Seed defaults only where nothing is set yet, so the boxes are never
          // blank and what is shown is what gets priced. A dropdown renders its
          // first option when its value is empty, so without this the form would
          // SHOW an image the state does not hold.
          // No size means no preset to seed from — a platform service has
          // nothing to size. `presets[null]` looked up the key "null",
          // found nothing and was hidden by the `?? {}`.
          if (!next.vcpu && c.size) Object.assign(next, opts.presets?.[c.size] ?? {})
          for (const field of TEXT_DETAIL_FIELDS) {
            const key = field as 'version' | 'image'
            if (!next[key] && opts.fields?.[field]?.default) {
              next[key] = opts.fields[field].default
            }
          }
          return next
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
    // '' rather than null: React renders both as nothing, and the callers
    // treat this as a string.
    if (c.vcpu == null) return c.size ?? ''
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
    if (isCreateLike || isPlatformService) {
      // A platform service names the environment it is FOR — which estate needs
      // backing up, which tier needs monitoring — not an existing environment it
      // operates on. Sending target_environment here would ask the team to act
      // on something rather than to build something.
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

  // Every save goes through here so the reference is remembered in exactly one
  // place. Re-saving an existing draft updates it; only the first call creates.
  async function persistDraft() {
    const payload = buildPayload()
    if (draftRef) payload.reference = draftRef
    const saved = await saveDraft(payload)
    if (saved.status === 200 && saved.body?.reference) setDraftRef(saved.body.reference)
    return saved
  }

  // WHY THIS SAVES A DRAFT FIRST. /api/placement/options resolves the layout
  // against the stored request -- its components, its tier, its target -- and not
  // against a body the browser composed, because this decision determines what
  // gets built and a browser-composed body is an assertion. So there must be a
  // request to resolve against. It is behind a button rather than an effect for
  // the same reason the draftRef comment above exists: a form that persisted a
  // draft as a side effect of typing would leave one behind for every visit
  // somebody abandoned halfway.
  async function onWorkOutPlacement() {
    setPlacementBusy(true)
    setPlacementError(null)
    const draft = await persistDraft()
    if (draft.status !== 200) {
      setPlacementBusy(false)
      setPlacementError(
        draft.body?.detail ||
          'The draft could not be saved, so there is nothing to work the placement out against.',
      )
      return
    }
    const { status, body } = await getPlacementOptions(draft.body.reference)
    setPlacementBusy(false)
    if (status !== 200) {
      setPlacementError(body?.detail || `The platform could not answer (HTTP ${status}).`)
      return
    }
    setPlacement((body.options || []) as PlacementOption[])
    setPlacementChosen(null)
    setPlacementCluster(null)
  }

  async function onSaveDraft() {
    setBusy(true)
    setResult(null)
    const { status, body } = await persistDraft()
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
    const draft = await persistDraft()
    if (draft.status !== 200) {
      setBusy(false)
      setResult({ kind: 'error', title: 'Could not save the request', subtitle: draft.body?.detail || '' })
      return
    }
    const ref = draft.body.reference

    // THE DECISION IS MADE HERE, BY THE SERVER, AND AGAIN. A layout chosen
    // minutes ago may no longer be permitted -- the policy can have changed, a
    // cluster can have filled up -- and /api/placement/resolve re-decides before
    // it records anything. A refusal at this point is the check working, so it
    // stops the submit and says what happened rather than submitting a request
    // whose layout was silently dropped.
    //
    // Skipped entirely when nothing was chosen: placement is offered, not
    // required, and every request raised before this step existed has none.
    if (placementChosen) {
      const placed = await resolvePlacement(ref, placementChosen, placementCluster,
                                            arrangedHosts)
      if (placed.status !== 200) {
        setBusy(false)
        setResult({
          kind: 'error',
          title: 'That layout is no longer available',
          subtitle:
            (placed.body?.detail || 'The platform refused it.') +
            ' Work out the placement again and choose from what is offered now.',
        })
        setPlacement(null)
        setPlacementChosen(null)
        setPlacementCluster(null)
        return
      }
    }

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
      // This draft is now a submitted request. Forget it, or the NEXT request
      // raised from this form would overwrite the one just submitted instead of
      // creating its own.
      setDraftRef(null)
      // And forget the layout with it, for the same reason. The options were
      // computed for the request that has just gone to an approver; carrying the
      // choice forward would silently apply a decision made about one request to
      // a different one.
      setPlacement(null)
      setPlacementChosen(null)
      setPlacementCluster(null)
    } else if (submit.status === 422) {
      const fieldErrors: Record<string, string> = submit.body.errors || {}
      setErrors(fieldErrors)
      const pv = submit.body.policy_violations
      if (pv?.length) {
        setResult({ kind: 'error', title: 'Blocked by policy', subtitle: pv.join('; ') })
      } else {
        // ALWAYS say something. Previously a validation error was stored and
        // nothing was shown, so an error on a field that is no longer rendered —
        // a version withdrawn when an Ubuntu image was chosen, say — made Submit
        // look like a dead button. Silence is the one response a submit must
        // never give.
        const messages = Object.values(fieldErrors)
        setResult({
          kind: 'error',
          title: messages.length
            ? `The request could not be submitted (${messages.length} problem${messages.length > 1 ? 's' : ''})`
            : 'The request could not be submitted',
          subtitle: messages.join(' · ') || 'The server rejected it without saying why.',
        })
      }
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
  // ONE MONTHLY FIGURE ON THE SCREEN. Once a layout is chosen, the cost that
  // matters is the placement cost: the per-component estimate priced the same
  // components in a different arrangement, so both being on screen puts two
  // different monthly totals in front of the same person. This form has already
  // been bitten twice by a figure that disagreed with itself -- the footer
  // reading 0.00 beside a panel reading "Not priced", and a 90.59/month resource
  // approved at zero -- and this is the same defect with better arithmetic.
  //
  // Falls back to the per-component estimate when nothing is chosen, and to
  // nothing at all when neither can be priced. Never to a zero.
  const chosenPlacement = placement?.find((o) => o.key === placementChosen) ?? null
  const placementPriced = !!chosenPlacement?.resolved
  const shownMonthly = placementPriced
    ? chosenPlacement!.totals.monthly
    : cost && !cost.unpriced?.length
      ? cost.totals.monthly
      : null
  const shownCurrency = placementPriced
    ? chosenPlacement!.estimate.currency
    : cost?.currency || 'AED'

  // Placement is OFFERED, not required: nothing before this step existed needed
  // a layout, and demanding one would block every request type that has never
  // had one. So the step is complete once a layout is chosen, and the marker
  // resting here while Details is already ticked is a nudge rather than a block.
  const placementDone = !!placementChosen
  const stepDone = [basicsDone, stackDone, placementDone, detailsDone]
  const firstIncomplete = stepDone.indexOf(false)
  const currentIndex = firstIncomplete === -1 ? stepDone.length - 1 : firstIncomplete
  const SECTION_IDS = ['section-basics', 'section-stack', 'section-placement',
                       'section-details']
  const scrollToSection = (i: number) =>
    document.getElementById(SECTION_IDS[i])?.scrollIntoView({ behavior: 'smooth', block: 'start' })

  if (!lookups) return <p style={{ color: 'var(--cds-text-secondary)' }}>Loading form…</p>

  // Only technologies offered on the selected deployment target (F-CAT); the full
  // catalogue until a target is chosen.
  const availableTechs = target
    ? lookups.technologies.filter((t) => t.targets.includes(target))
    : lookups.technologies

  // HOW EACH THING ARRIVES, which decides the heading it sits under.
  //
  // Forty-eight names in one grid asks a requester to tell OCI's managed
  // PostgreSQL from PostgreSQL installed on a VM by reading two labels that say
  // "PostgreSQL". Those are different products — one Oracle patches and one you
  // do — and the catalogue has recorded the difference since S2. The form was
  // simply never told.
  //
  // The order is deliberate: least work for the requester first. A managed
  // service is run by the cloud; software on a machine is a machine you own with
  // something installed on it; a machine is bare; a capability is not built at
  // all — your platform team fulfils it.
  const GROUPS: { model: string; title: string; blurb: string }[] = [
    { model: 'managed', title: 'Managed cloud services',
      blurb: 'The cloud provider runs and patches these. No machine of yours.' },
    { model: 'software', title: 'Software on a machine',
      blurb: 'A machine of your own with the software installed and running on it.' },
    { model: 'machine', title: 'Machines',
      blurb: 'A bare virtual machine with an operating system and nothing else.' },
    { model: 'capability', title: 'Capabilities',
      blurb: 'Fulfilled by the infrastructure team — nothing is provisioned.' },
    // "" means the catalogue does not RECORD how this arrives. Shown, not
    // hidden and not guessed at: the grouped catalogue view has always reported
    // unknowns separately, and the form now does the same.
    { model: '', title: 'Not yet classified',
      blurb: 'The catalogue does not record how these are delivered.' },
  ]
  const groupedTechs = GROUPS
    .map((g) => ({ ...g, items: availableTechs.filter((t) => (t.delivery_model || '') === g.model) }))
    .filter((g) => g.items.length > 0)

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
        {/* RESUMING SAYS SO, IN ALL THREE OF ITS STATES (P.11a). An empty form
            is what a requester sees whether their draft is still loading, could
            not be opened, or never existed — three very different facts that
            look identical unless the page names which one happened. */}
        {resuming && (
          <InlineNotification
            kind="info"
            lowContrast
            hideCloseButton
            title={`Opening ${resumeRef}…`}
            subtitle="Fetching what you saved."
            style={{ marginBottom: '1rem', maxWidth: 'none' }}
          />
        )}
        {resumeError && (
          <InlineNotification
            kind="error"
            lowContrast
            hideCloseButton
            title="That draft could not be opened"
            subtitle={resumeError}
            style={{ marginBottom: '1rem', maxWidth: 'none' }}
          />
        )}
        {resumed && !resumeError && (
          <InlineNotification
            kind="success"
            lowContrast
            title={`Resumed ${resumed}`}
            subtitle="Saving again updates this draft — it does not create another."
            onCloseButtonClick={() => setResumed(null)}
            style={{ marginBottom: '1rem', maxWidth: 'none' }}
          />
        )}
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
            <ProgressStep label="Placement" complete={placementDone} />
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
                  You have nothing with active resources to decommission.
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
                      // Nothing is smaller than a component that has no size.
                      // Previously SIZE_INDEX[null] was undefined and every
                      // comparison against it was false, which gave the same
                      // empty list by accident rather than on purpose.
                      const smaller = c.size
                        ? SIZES.filter((s) => SIZE_INDEX[s] < SIZE_INDEX[c.size as string])
                        : []
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
              {(isCreateLike || isPlatformService) && (
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

              {(isCreateLike || isPlatformService) && (
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

              {(isCreateLike || isPlatformService) && (
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
              {/* A CAPABILITY, NOT A COMPONENT. Backup, centralised logging and
                  monitoring have no package, no archive and no cloud resource —
                  nothing can provision one. They used to sit in the component
                  list beside NGINX, where a requester could select one, have it
                  priced, have it approved, and receive a work item; the agent
                  even spent a real machine discovering `dnf install backup`
                  finds nothing (REQ-2026-0183). Asked for here instead, with no
                  sizing and no price, because there is nothing to size or price. */}
              {isPlatformService && (
                <FormGroup legendText="What do you need?">
                  {errors.components && (
                    <p style={{ color: 'var(--cds-text-error)', fontSize: '0.75rem', marginBottom: '0.5rem' }}>{errors.components}</p>
                  )}
                  <div style={{ display: 'flex', flexDirection: 'column', gap: '0.75rem' }}>
                    {(lookups.platform_services || []).map((svc) => {
                      const chosen = components[0]?.technology_code === svc.code
                      return (
                        <button
                          key={svc.code}
                          type="button"
                          onClick={() => setComponents([{ technology_code: svc.code, size: null }])}
                          style={{
                            textAlign: 'left', cursor: 'pointer', padding: '0.85rem 1rem',
                            borderRadius: 4, background: 'var(--cds-layer)',
                            border: chosen
                              ? '2px solid var(--cds-border-interactive)'
                              : '1px solid var(--cds-border-subtle)',
                          }}
                        >
                          <div style={{ fontWeight: 500, marginBottom: '0.25rem' }}>{svc.name}</div>
                          {/* The note says what this is and, where one exists,
                              what to request instead — a service mesh needs a
                              Kubernetes cluster first, and generic "Kubernetes"
                              means OCI Container Engine here. */}
                          <div style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', lineHeight: 1.5 }}>
                            {svc.note}
                          </div>
                        </button>
                      )
                    })}
                  </div>
                  <p style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', marginTop: '0.9rem', lineHeight: 1.5 }}>
                    One at a time — each is scoped and delivered separately. There is
                    nothing to size and no price: this asks the infrastructure team to
                    design and build a capability, and what it carries is your
                    justification, the approval and the audit trail.
                  </p>
                </FormGroup>
              )}
              {!isPlatformService && (
              <FormGroup legendText="Components">
                {errors.components && (
                  <p style={{ color: 'var(--cds-text-error)', fontSize: '0.75rem', marginBottom: '0.5rem' }}>{errors.components}</p>
                )}
                <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.35rem' }}>
                  {target
                    ? `Technologies available on ${TARGETS.find(([v]) => v === target)?.[1] || target}. Pick one or more, then choose a size for each.`
                    : 'Pick a deployment target above to see its technologies, then choose sizes.'}
                </p>
                {/* THE DOT, GIVEN WORDS ONCE. Each tile used to carry a full
                    "automated" pill, which cost more width than the name it sat
                    under and repeated the same two words forty times. Said here
                    instead, so the tiles can be half the size. */}
                {target && (
                  <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.6rem', display: 'flex', alignItems: 'center', gap: '0.35rem', flexWrap: 'wrap' }}>
                    <span aria-hidden="true" style={{ width: '0.4rem', height: '0.4rem', borderRadius: '50%', background: 'var(--cds-support-success, #24a148)' }} />
                    <span>the portal builds it</span>
                    <span style={{ color: 'var(--cds-text-placeholder)' }}>·</span>
                    <span aria-hidden="true" style={{ width: '0.4rem', height: '0.4rem', borderRadius: '50%', background: 'var(--cds-border-strong, #8d8d8d)' }} />
                    <span>the infrastructure team fulfils it after approval</span>
                  </p>
                )}
                {groupedTechs.map((group) => (
                <div key={group.model || 'unclassified'} style={{ marginBottom: '1.1rem' }}>
                  <div style={{ display: 'flex', alignItems: 'baseline', gap: '0.5rem', marginBottom: '0.15rem' }}>
                    <span style={{ fontSize: '0.72rem', fontWeight: 600, letterSpacing: '0.04em', textTransform: 'uppercase', color: 'var(--cds-text-secondary)' }}>
                      {group.title}
                    </span>
                    <span style={{ fontSize: '0.72rem', color: 'var(--cds-text-placeholder)' }}>
                      {group.items.length}
                    </span>
                  </div>
                  <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.45rem' }}>
                    {group.blurb}
                  </p>
                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(6.25rem, 1fr))', gap: '0.4rem' }}>
                  {group.items.map((t) => {
                    const Icon = techIcon(t.code)
                    const sel = components.some((c) => c.technology_code === t.code)
                    const automated = (t.automated_targets || []).includes(target)
                    // THE WHOLE TILE CARRIES THE EXPLANATION, because the pill
                    // that used to is gone and a dot alone explains nothing to
                    // someone meeting it for the first time.
                    const explain = t.name + (!target ? '' : automated
                      ? ' — the portal provisions this automatically.'
                      : ' — the infrastructure team fulfils this after approval.')
                    return (
                      <button key={t.code} type="button" aria-pressed={sel} title={explain}
                              onClick={() => toggleTech(t.code)} style={cardStyle(sel)}>
                        <span style={{ display: 'flex', alignItems: 'center', justifyContent: 'space-between', gap: '0.25rem' }}>
                          <Icon size={16} style={{ color: 'var(--cds-icon-primary)', flex: 'none' }} />
                          {/* HOW IT ARRIVES, in the space a dot takes. A full
                              "automated" pill outweighed the name it described
                              and cost more width than the name itself. The
                              legend above the grid gives it words once. */}
                          {target && (
                            <span
                              aria-hidden="true"
                              style={{
                                width: '0.4rem', height: '0.4rem', borderRadius: '50%', flex: 'none',
                                background: automated
                                  ? 'var(--cds-support-success, #24a148)'
                                  : 'var(--cds-border-strong, #8d8d8d)',
                              }}
                            />
                          )}
                        </span>
                        <span
                          style={{
                            fontWeight: 500, fontSize: '0.72rem', lineHeight: 1.25,
                            display: '-webkit-box', WebkitLineClamp: 2,
                            WebkitBoxOrient: 'vertical', overflow: 'hidden',
                          } as CSSProperties}
                        >
                          {t.name}
                        </span>
                        {t.lifecycle_state !== 'certified' && (
                          <span style={{
                            fontSize: '0.58rem', letterSpacing: '0.04em', textTransform: 'uppercase',
                            color: t.lifecycle_state === 'deprecated'
                              ? 'var(--cds-text-error)' : 'var(--cds-text-secondary)',
                          }}>
                            {t.lifecycle_state}
                          </span>
                        )}
                      </button>
                    )
                  })}
                </div>
                </div>
                ))}

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
                                <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fill, minmax(6.5rem, 1fr))', gap: '0.4rem' }}>
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
                                      <button
                                        key={s} type="button" aria-pressed={sel}
                                        title={`${s}: ${spec.vcpu} vCPU, ${spec.memory_gb} GB RAM, ${spec.storage_gb} GB storage`}
                                        onClick={() => setSize(c.technology_code, s)} style={cardStyle(sel)}
                                      >
                                        <span style={{ fontWeight: 500, textTransform: 'capitalize', fontSize: '0.72rem' }}>{s}</span>
                                        {/* ONE FACT PER LINE, each unwrappable.
                                            These ran as "N vCPU · N GB RAM" with a
                                            <br /> before storage, so Xlarge's wider
                                            numbers wrapped "RAM" onto its own line
                                            and that tile stood taller than the three
                                            beside it. Three fixed lines are the same
                                            height whatever the numbers, which is what
                                            makes a row of them read as a set.
                                            Tabular figures keep the digits in column. */}
                                        <span style={{
                                          fontSize: '0.66rem', color: 'var(--cds-text-secondary)',
                                          lineHeight: 1.45, display: 'flex', flexDirection: 'column',
                                          fontVariantNumeric: 'tabular-nums',
                                        }}>
                                          <span style={{ whiteSpace: 'nowrap' }}>{spec.vcpu} vCPU</span>
                                          <span style={{ whiteSpace: 'nowrap' }}>{spec.memory_gb} GB RAM</span>
                                          <span style={{ whiteSpace: 'nowrap' }}>{spec.storage_gb} GB storage</span>
                                        </span>
                                      </button>
                                    )
                                  })}
                                </div>

                                {/* The detail fields. Every option here came from
                                    the server and is re-checked on submit. */}
                                {/* ONE ROW. These were minmax(9rem) with a 0.75rem
                                    gap -- about 38rem for the four a machine
                                    carries -- so the fourth wrapped onto a line of
                                    its own and the card grew a half-empty row.
                                    Narrower columns fit all five (version appears
                                    for some technologies) inside the form column.

                                    THE OS NAME IS THE ONE THAT SUFFERS: it was
                                    already truncated at 9rem and is shorter here.
                                    So the chosen value is repeated in the tooltip,
                                    where it can be read in full -- narrowing a
                                    control must not be the same as hiding what it
                                    says. */}
                                {opts && (
                                  <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(6.75rem, 1fr))', gap: '0.5rem', marginTop: '0.6rem' }}>
                                    {['version', 'image', 'vcpu', 'memory_gb', 'storage_gb'].map((field) => {
                                      const spec = opts.fields[field]
                                      if (!spec) return null  // nothing honest to offer
                                      const value = String(
                                        (c as unknown as Record<string, unknown>)[field] ?? '',
                                      )
                                      const chosen = spec.options.find((o) => o.value === value)
                                      const errKey = `component_${components.findIndex((x) => x.technology_code === c.technology_code)}_${field}`
                                      return (
                                        <Select
                                          key={field}
                                          id={`detail-${c.technology_code}-${field}`}
                                          labelText={spec.label}
                                          size="sm"
                                          value={value}
                                          title={`${spec.label}: ${chosen?.label ?? (value || 'not set')}`}
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

                                {/* WHAT YOU ARE ACTUALLY GETTING.
                                    "PostgreSQL" and "Kafka" sit next to each
                                    other on one catalogue and arrive as
                                    completely different things: one is a
                                    database OCI runs, the other is machines
                                    somebody has to patch. Nothing here said so,
                                    and a requester found out from the bill. */}
                                {opts?.delivery && opts.delivery.options.length > 0 && (
                                  <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
                                    <strong>{opts.delivery.options[0].label}</strong>
                                    {' — '}{opts.delivery.options[0].meaning}.
                                    {opts.delivery.options.length > 1 && (
                                      <>
                                        {' '}This can also be delivered as{' '}
                                        {opts.delivery.options.slice(1).map((o) => o.label.toLowerCase()).join(', ')}.
                                        The platform will use the first; choosing between
                                        them is not yet available.
                                      </>
                                    )}
                                  </p>
                                )}

                                {opts?.delivery && opts.delivery.options.length === 0 && (
                                  <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
                                    {opts.delivery.known
                                      ? 'No blueprint says how this is delivered, so the platform cannot tell you what you would get.'
                                      : 'How this is delivered is not known right now — the build service could not be reached.'}
                                  </p>
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
              )}
              </div>

              <div id="section-placement">
                <PlacementStep
                  options={placement}
                  chosen={placementChosen}
                  clusterId={placementCluster}
                  // What this draft was SAVED with, so a resumed request shows
                  // its layout instead of an empty step with a choice held
                  // invisibly behind it. Dropped the moment the stack changes.
                  recorded={resumedPlacement}
                  busy={placementBusy}
                  error={placementError}
                  onFetch={onWorkOutPlacement}
                  onChoose={setPlacementChosen}
                  onClusterChange={setPlacementCluster}
                  reference={draftRef}
                  onArranged={(hosts) => {
                    setArrangedHosts(hosts)
                    setPlacementChosen(hosts ? 'custom' : null)
                  }}
                  // The same gate the cost panel uses: a component with no size
                  // chosen has no requirement row to size a host from, so asking
                  // now would return every layout unsizeable and read as a
                  // failure rather than as an unfinished form.
                  environment={envTier}
                  deploymentTarget={target}
                  ready={!!target && pricedComponents.length > 0 && !isPlatformService}
                  notReadyReason={
                    isPlatformService
                      ? 'A platform service is fulfilled by the infrastructure team, so there is nothing to place on a machine.'
                      : !target
                        ? 'Choose a deployment target first. Where it runs decides what it can run on.'
                        : filledComponents.length > 0
                          ? 'Choose a size for each component. The size decides the shape of the machine it needs.'
                          : 'Add a component first. Placement is about where the components go.'
                  }
                />
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
            {/* AND THE SAME TRUTH HERE. The panel on the right already says
                "Not priced" when the server could not cost a component — this
                bar printed the raw total beside it and read "0.00 AED/mo",
                which is the contradiction a person actually sees: the footer
                sits next to the submit button and follows the page down.
                Reported on 2026-09-05 for MySQL, whose sizing anchors were
                missing from the running database, and it is the same defect
                the comment above the panel describes, in the other half of
                the screen. A price nobody can compute is not a price of zero. */}
            {cost && !isDecommission && (
              <span style={{ marginLeft: 'auto', fontSize: '0.9rem', color: 'var(--cds-text-secondary)' }}>
                {shownMonthly == null ? (
                  <strong style={{ color: 'var(--cds-text-primary)' }}>Not priced</strong>
                ) : (
                  <>
                    <strong style={{ color: 'var(--cds-text-primary)' }}>{shownMonthly.toFixed(2)} {shownCurrency}</strong>/mo
                    {/* Which of the two calculations this is. Without it the
                        number changes when a layout is chosen and nothing on the
                        screen says why. */}
                    {placementPriced && <> after placement</>}
                  </>
                )}
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
              {/* Third place the same number is shown, and the third that has
                  to agree with the server about whether it IS a number. */}
              {cost && (
                <SummaryRow label={placementPriced ? 'Monthly, as placed' : 'Est. monthly'}>
                  <strong>
                    {shownMonthly == null
                      ? 'Not priced'
                      : `${shownMonthly.toFixed(2)} ${shownCurrency}`}
                  </strong>
                </SummaryRow>
              )}
              {placementPriced && (
                <SummaryRow label="Layout">
                  {chosenPlacement!.title}
                  <Tag type="blue" size="sm" style={{ marginLeft: '0.4rem' }}>
                    {chosenPlacement!.sizing.machine_count === 1
                      ? '1 machine'
                      : `${chosenPlacement!.sizing.machine_count} machines`}
                  </Tag>
                </SummaryRow>
              )}
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
              {/* A PRICE NOBODY CAN COMPUTE IS NOT A PRICE OF ZERO. The server
                  says which components it could not price; showing the total
                  alone rendered 0.00 and read as free, which is how
                  REQ-2026-0176 reached an approver at a figure that was never
                  real. Say so here, while the form is still being filled in,
                  rather than refusing only at submit. */}
              {/* Priced, but not yet certified. The figure is REAL — the same
                  calculation execution will make — so it is shown as a figure
                  and labelled, rather than hidden behind "Not priced". Showing
                  0.00 got a 90.59/month resource approved at zero twice. */}
              {!cost.unpriced?.length && cost.provisional?.length ? (
                <>
                  <p style={{ fontSize: '2rem', fontWeight: 300, margin: '0.25rem 0' }}>
                    {cost.totals.monthly.toFixed(2)} <span style={{ fontSize: '0.9rem' }}>{cost.currency}/mo</span>
                  </p>
                  <InlineNotification
                    kind="info"
                    lowContrast
                    hideCloseButton
                    title="Provisional — not yet certified"
                    subtitle={`${cost.provisional.join(', ')} has no certified blueprint yet. This is what it will cost if the portal builds it as software on a machine, which is what it would do — if that cannot be proven, the infrastructure team fulfils it instead and the final cost may differ.`}
                    style={{ maxWidth: 'none', marginBottom: '0.5rem' }}
                  />
                </>
              ) : cost.unpriced?.length ? (
                <>
                  <p style={{ fontSize: '1.5rem', fontWeight: 300, margin: '0.25rem 0' }}>
                    Not priced
                  </p>
                  <InlineNotification
                    kind="warning"
                    lowContrast
                    hideCloseButton
                    title="This cannot be costed yet"
                    subtitle={`${cost.unpriced.join(', ')} — no certified blueprint says what this builds, and what it builds is what decides how it is charged. It cannot be submitted until it is certified, or removed from the request.`}
                    style={{ maxWidth: 'none', marginBottom: '0.5rem' }}
                  />
                </>
              ) : (
                <p style={{ fontSize: '2rem', fontWeight: 300, margin: '0.25rem 0' }}>
                  {cost.totals.monthly.toFixed(2)} <span style={{ fontSize: '0.9rem' }}>{cost.currency}/mo</span>
                </p>
              )}
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
