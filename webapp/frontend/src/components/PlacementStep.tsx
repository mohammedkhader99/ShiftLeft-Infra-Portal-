/**
 * The placement step: what will run where, what it costs, and why you cannot
 * have the rest (P.11, F-UX-16 + F-UX-10).
 *
 * SILENT FILTERING IS THE DEFECT THIS FILE EXISTS TO AVOID. Every option the
 * server produced is rendered, including the ones that cannot be chosen, each
 * with the sentence saying why. An option that quietly disappears is
 * indistinguishable from a portal that is broken: the requester goes looking for
 * a choice the documentation promised, does not find it, and raises the ticket
 * this whole phase exists to prevent. A greyed card with a reason on it costs a
 * few pixels and answers the question.
 *
 * NOTHING HERE DECIDES ANYTHING. Every figure — the shapes, the totals, the
 * deltas, the refusals — arrives from /api/placement/options, which asked OPA
 * whether each layout is permitted before pricing it. The component does not
 * recompute a single number, does not sort by its own idea of cheapest, and does
 * not hide an option it thinks is a bad idea. ARCHITECTURE.md §14 decision 10:
 * the API is the authority, and a browser that recalculated any of this would be
 * a second authority that disagrees with the first under load.
 *
 * THE ONE EXCEPTION TO "SHOW EVERYTHING" is a cluster the requester is not
 * entitled to, and it is the server that applies it: those never arrive here at
 * all. Listing them as refused would publish their names, regions and sizes to
 * anyone who can open the form, which is a disclosure and not an explanation.
 * Capacity and quota are the opposite — those clusters DO arrive, refused, with
 * the figure that stopped them, because "the cluster is full" is something a
 * requester can act on.
 */

import { useState } from 'react'
import {
  Button,
  InlineNotification,
  RadioButton,
  RadioButtonGroup,
  Tag,
  Tile,
} from '@carbon/react'
import type {
  PlacementCluster,
  PlacementOption,
  ProposedHost,
  RecordedPlacement,
  SizedHost,
} from '../api'
import TopologyDiagram from './TopologyDiagram'
import TopologyEditor from './TopologyEditor'
import TopologyWorkspace from './TopologyWorkspace'

// The option keys, spelled the way the resolver spells them, for naming a layout
// that was RECORDED rather than one that was just computed. A recorded placement
// carries its key and its topology, not the title the option set gave it.
const OPTION_LABEL: Record<string, string> = {
  managed: 'Managed by the cloud',
  consolidated: 'Consolidated onto one machine',
  separated: 'A machine per component',
  'existing-cluster': 'Deployed onto an existing cluster',
  'new-cluster': 'On a new cluster',
}

const HOST_MODE_LABEL: Record<string, string> = {
  vm: 'virtual machine',
  container: 'container',
  managed: 'managed by the cloud',
}

// "1 machine" reads as a fact; "1 host(s)" reads as a placeholder somebody
// forgot to finish.
const plural = (n: number, one: string, many: string) =>
  `${n} ${n === 1 ? one : many}`

const shape = (host: SizedHost): string => {
  const parts: string[] = []
  if (host.vcpu != null) parts.push(`${host.vcpu} vCPU`)
  if (host.memory_gb != null) parts.push(`${host.memory_gb} GB RAM`)
  if (host.storage_gb != null) parts.push(`${host.storage_gb} GB storage`)
  return parts.join(' · ')
}

/**
 * One host, and what lands on it.
 *
 * A host the server declined to size is shown as unsized WITH the components it
 * could not size, never as a host of zero. Printing "0 vCPU" for a machine whose
 * requirement is missing is the same class of lie as pricing it at 0.00, and this
 * portal has already been bitten once by that: a 90.59/month resource was
 * approved at zero, twice.
 */
function HostLine({ host }: { host: SizedHost }) {
  const managed = host.host_mode === 'managed'
  return (
    <div
      style={{
        display: 'flex',
        gap: '0.5rem',
        alignItems: 'baseline',
        padding: '0.3rem 0',
        borderTop: '1px solid var(--cds-border-subtle)',
        fontSize: '0.8rem',
      }}
    >
      <Tag type={managed ? 'green' : host.resolved ? 'blue' : 'red'} size="sm" style={{ margin: 0, flex: '0 0 auto' }}>
        {HOST_MODE_LABEL[host.host_mode] || host.host_mode}
      </Tag>
      <div style={{ flex: 1 }}>
        <div>{host.components.join(', ') || 'nothing placed'}</div>
        {managed ? (
          <div style={{ color: 'var(--cds-text-secondary)', fontSize: '0.72rem' }}>
            {host.note || 'Run by the cloud. No machine is provisioned.'}
          </div>
        ) : host.resolved ? (
          <div style={{ color: 'var(--cds-text-secondary)', fontSize: '0.72rem' }}>
            {shape(host)}
            {host.headroom_percent > 0 && (
              <> — includes {host.headroom_percent}% headroom</>
            )}
          </div>
        ) : (
          /* NOT "0 vCPU". The server says which components it has no requirement
             for; showing a shape anyway would put a number on a machine nobody
             sized and let it reach an approver looking calculated. */
          <div style={{ color: 'var(--cds-text-error)', fontSize: '0.72rem' }}>
            {host.note || `Cannot be sized — no requirement recorded for ${host.missing.join(', ')}.`}
          </div>
        )}
      </div>
    </div>
  )
}

/**
 * The clusters this option could land on.
 *
 * Ineligible ones stay on the page with their reason: capacity and quota are
 * facts about the cluster, and a requester who can read "98 of 100 vCPU used"
 * knows whether to wait, ask for more, or provision their own. Only the
 * entitlement boundary is invisible, and it is invisible before it reaches here.
 */
function ClusterPicker({
  clusters,
  chosen,
  onChoose,
  disabled,
}: {
  clusters: PlacementCluster[]
  chosen: string | null
  onChoose: (id: string) => void
  disabled: boolean
}) {
  const usable = clusters.filter((c) => c.eligible)
  const refused = clusters.filter((c) => !c.eligible)

  return (
    <div style={{ marginTop: '0.5rem' }}>
      {usable.length > 0 && (
        <RadioButtonGroup
          legendText="Which cluster?"
          name="placement-cluster"
          orientation="vertical"
          valueSelected={chosen ?? ''}
          onChange={(value) => onChoose(String(value))}
        >
          {usable.map((c) => (
            <RadioButton
              key={c.id}
              value={c.id}
              disabled={disabled}
              labelText={`${c.name} — ${c.region} · ${c.allocatable_vcpu} vCPU / ${c.allocatable_memory_gb} GB free`}
            />
          ))}
        </RadioButtonGroup>
      )}
      {refused.map((c) => (
        <div
          key={c.id}
          style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', marginTop: '0.35rem' }}
        >
          <strong>{c.name}</strong> — {c.reasons.join(' ')}
        </div>
      ))}
    </div>
  )
}

function OptionCard({
  option,
  currency,
  selected,
  clusterId,
  onSelect,
  onClusterChange,
  busy,
  environment,
  deploymentTarget,
  onArrange,
}: {
  option: PlacementOption
  currency: string
  selected: boolean
  clusterId: string | null
  onSelect: () => void
  onClusterChange: (id: string) => void
  busy: boolean
  environment?: string | null
  deploymentTarget?: string | null
  onArrange?: (option: PlacementOption) => void
}) {
  // ON REQUEST, not by default. The list of options is for comparing; the
  // diagram is for understanding ONE of them, and drawing all of them at once
  // would turn a scannable list into a wall the requester has to read through to
  // find the figures they were comparing.
  const [showTopology, setShowTopology] = useState(false)
  const choosable = option.eligible && !busy
  // An option needing a cluster is not a complete choice until one is named. The
  // server refuses it anyway (400) — this only saves the requester the round
  // trip and says which click is missing.
  const needsCluster = option.clusters.length > 0 && option.eligible
  const priced = option.resolved

  return (
    <Tile
      style={{
        marginBottom: '0.75rem',
        borderLeft: `3px solid ${
          selected
            ? 'var(--cds-border-interactive)'
            : option.eligible
              ? 'var(--cds-border-subtle)'
              : 'var(--cds-support-error)'
        }`,
        background: selected ? 'var(--cds-layer-selected)' : 'var(--cds-layer)',
        opacity: option.eligible ? 1 : 0.85,
      }}
    >
      <div style={{ display: 'flex', gap: '1rem', alignItems: 'flex-start' }}>
        <div style={{ flex: 1 }}>
          <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', flexWrap: 'wrap' }}>
            <strong style={{ fontSize: '0.95rem' }}>{option.title}</strong>
            {option.cheapest && priced && (
              <Tag type="green" size="sm" style={{ margin: 0 }}>cheapest</Tag>
            )}
            {!option.eligible && (
              <Tag type="red" size="sm" style={{ margin: 0 }}>not available</Tag>
            )}
          </div>
          <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0.2rem 0 0' }}>
            {option.summary}
          </p>
        </div>

        {/* THE NUMBER, or the honest absence of one. An option the server could
            not price shows "Not priced" — never 0.00, and never a delta, because
            a zero would make the unpriceable option look like the bargain of the
            set. This is the third place in this form to learn that lesson. */}
        <div style={{ textAlign: 'right', flex: '0 0 auto' }}>
          {priced ? (
            <>
              <div style={{ fontSize: '1.25rem', fontWeight: 300 }}>
                {option.totals.monthly.toFixed(2)}
                <span style={{ fontSize: '0.75rem' }}> {currency}/mo</span>
              </div>
              {option.monthly_delta != null && option.monthly_delta > 0 && (
                <div style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)' }}>
                  +{option.monthly_delta.toFixed(2)} vs cheapest
                </div>
              )}
              <div style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)' }}>
                {plural(option.sizing.machine_count, 'machine', 'machines')}
              </div>
            </>
          ) : (
            <div style={{ fontSize: '0.95rem', fontWeight: 400 }}>Not priced</div>
          )}
        </div>
      </div>

      {/* WHY YOU CANNOT HAVE THIS. The reason is the reason the step exists;
          rendering the card without it would be the silent filtering this phase
          keeps refusing, only slower. */}
      {!option.eligible && option.reasons.length > 0 && (
        <div style={{ marginTop: '0.5rem' }}>
          {option.reasons.map((r, i) => (
            <p
              key={i}
              style={{ fontSize: '0.78rem', color: 'var(--cds-text-primary)', margin: '0.2rem 0' }}
            >
              {r}
            </p>
          ))}
        </div>
      )}

      {/* Advisory, never blocking. Running a database on Kubernetes is a
          legitimate choice; the portal's job is to see it made knowingly. */}
      {option.warnings.map((w, i) => (
        <InlineNotification
          key={i}
          kind="info"
          lowContrast
          hideCloseButton
          title="Worth knowing"
          subtitle={w}
          style={{ maxWidth: 'none', marginTop: '0.5rem', marginBottom: 0 }}
        />
      ))}

      {/* WHAT RUNS WHERE. The whole point of the screen, and the thing a
          component list could never show. */}
      {option.sizing.hosts.length > 0 && (
        <div style={{ marginTop: '0.6rem' }}>
          <div
            style={{
              display: 'flex',
              alignItems: 'baseline',
              justifyContent: 'space-between',
              gap: '0.75rem',
            }}
          >
            <div
              style={{
                fontSize: '0.68rem',
                textTransform: 'uppercase',
                letterSpacing: '0.02em',
                color: 'var(--cds-text-secondary)',
              }}
            >
              What runs where
            </div>
            <span style={{ display: 'flex', gap: '0.25rem' }}>
              <Button
                kind="ghost"
                size="sm"
                aria-expanded={showTopology}
                onClick={() => setShowTopology((v) => !v)}
              >
                {showTopology ? 'Hide diagram' : 'View as diagram'}
              </Button>
              {/* Only where there is something to rearrange. One machine with
                  one thing on it has no arrangement to change, and offering the
                  control anyway would be a button that does nothing. */}
              {onArrange && option.sizing.hosts.length + option.sizing.hosts
                .reduce((n, h) => n + h.components.length, 0) > 2 && (
                <Button kind="ghost" size="sm" onClick={() => onArrange(option)}>
                  Arrange it yourself
                </Button>
              )}
            </span>
          </div>

          {showTopology ? (
            <TopologyDiagram
              option={option}
              environment={environment}
              deploymentTarget={deploymentTarget}
            />
          ) : (
            option.sizing.hosts.map((h) => (
              <HostLine key={h.host_id} host={h} />
            ))
          )}
        </div>
      )}

      {option.clusters.length > 0 && (
        <ClusterPicker
          clusters={option.clusters}
          chosen={clusterId}
          onChoose={onClusterChange}
          disabled={!choosable}
        />
      )}

      {option.eligible && (
        <div style={{ marginTop: '0.6rem', display: 'flex', gap: '0.5rem', alignItems: 'center' }}>
          <Button
            size="sm"
            kind={selected ? 'primary' : 'tertiary'}
            disabled={!choosable || (needsCluster && !clusterId)}
            onClick={onSelect}
          >
            {selected ? 'Chosen' : 'Choose this'}
          </Button>
          {needsCluster && !clusterId && (
            <span style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)' }}>
              Pick a cluster first.
            </span>
          )}
        </div>
      )}
    </Tile>
  )
}

export default function PlacementStep({
  options,
  chosen,
  clusterId,
  recorded = null,
  onFetch,
  onChoose,
  onClusterChange,
  busy,
  error,
  ready,
  notReadyReason,
  environment = null,
  deploymentTarget = null,
  reference = null,
  onArranged,
}: {
  options: PlacementOption[] | null
  chosen: string | null
  clusterId: string | null
  // The layout already recorded against this request (P.8), shown when a saved
  // draft is resumed and the options have not been worked out again. Null for a
  // new request, which is every other way into this step.
  recorded?: RecordedPlacement | null
  onFetch: () => void
  onChoose: (key: string) => void
  onClusterChange: (id: string) => void
  busy: boolean
  error: string | null
  ready: boolean
  notReadyReason: string
  // Shown on the diagram so a picture of "what gets built" says WHERE, which is
  // the first thing anyone asks of a topology.
  environment?: string | null
  deploymentTarget?: string | null
  // The saved draft the layout is judged against. Rearranging needs one, because
  // the server evaluates against the stored request rather than a body the
  // browser composed.
  reference?: string | null
  // A layout the requester arranged, chosen. Held here and sent at submit, the
  // same way an offered option's key is.
  onArranged?: (hosts: ProposedHost[] | null) => void
}) {
  const [open, setOpen] = useState(true)
  // Which layout is being adapted, if any. One at a time: two editors would be
  // two arrangements, and only one of them can be chosen.
  const [arranging, setArranging] = useState<PlacementOption | null>(null)
  const currency = options?.[0]?.estimate?.currency || 'AED'

  return (
    <div>
      <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'baseline', flexWrap: 'wrap' }}>
        <strong style={{ fontSize: '0.95rem' }}>Placement</strong>
        <span style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)' }}>
          How the stack is laid out — one machine or several, or run by the cloud.
        </span>
      </div>

      {!ready ? (
        <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
          {notReadyReason}
        </p>
      ) : (
        <>
          {/* WHY THERE IS A BUTTON HERE AT ALL, rather than options appearing on
              their own. The API resolves placement against the SAVED request —
              its components, its environment tier, its target — because a body
              the browser composed is an assertion and this decision determines
              what gets built. So asking for options saves the draft first, and
              a form that wrote a draft to the database as a side effect of
              typing would leave one behind for every abandoned visit. The
              requester presses the button, and the button says what it does. */}
          <div style={{ display: 'flex', gap: '0.75rem', alignItems: 'center', marginTop: '0.6rem' }}>
            <Button size="sm" kind={options ? 'ghost' : 'tertiary'} onClick={onFetch} disabled={busy}>
              {busy ? 'Working it out…' : options ? 'Work it out again' : 'Work out placement'}
            </Button>
            <span style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)' }}>
              Saves a draft first, then asks the platform what this could be built as.
            </span>
          </div>

          {error && (
            <InlineNotification
              kind="error"
              lowContrast
              hideCloseButton
              title="Could not work out the placement"
              subtitle={error}
              style={{ maxWidth: 'none', marginTop: '0.6rem' }}
            />
          )}

          {/* A RESUMED DRAFT SHOWS THE LAYOUT IT WAS SAVED WITH. Without this
              the step would come back empty while `chosen` quietly held the
              recorded option underneath it — a decision in force, driving what
              gets built, with nothing on screen saying so. An invisible choice
              is worse than no choice.

              The figure is the one RECORDED with the decision, not a fresh
              calculation. Rates move (P.8), and the number the requester chose
              against is the number to show them again; "Work it out again"
              re-prices against today. */}
          {!options && recorded && (
            <Tile style={{ marginTop: '0.6rem', padding: '0.75rem' }}>
              <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'baseline', flexWrap: 'wrap' }}>
                <strong style={{ fontSize: '0.85rem' }}>
                  {OPTION_LABEL[recorded.option_key] || recorded.option_key}
                </strong>
                <Tag type="blue" size="sm">saved with this draft</Tag>
                {recorded.version > 1 && (
                  <span style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)' }}>
                    version {recorded.version}
                  </span>
                )}
              </div>
              <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0.35rem 0 0' }}>
                {plural(recorded.topology?.hosts?.length ?? 0, 'host', 'hosts')}
                {recorded.estimate?.totals?.monthly != null && recorded.estimate?.resolved !== false
                  ? ` · ${recorded.estimate.totals.monthly.toFixed(2)} ${recorded.estimate.currency || 'AED'}/month when it was chosen`
                  : ' · not priced'}
                . Work it out again to change it or to re-price it against today's rates.
              </p>
            </Tile>
          )}

          {options && options.length === 0 && (
            <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', marginTop: '0.6rem' }}>
              The platform produced no layouts for this stack. Nothing in the
              selection needs a host — a capability is fulfilled by the
              infrastructure team, not provisioned onto a machine.
            </p>
          )}

          {arranging && reference && (
            <TopologyEditor
              reference={reference}
              option={arranging}
              environment={environment}
              deploymentTarget={deploymentTarget}
              // THE PLATFORM'S OWN SENTENCE, not one written here. A refused
              // layout whose hosts are containers is a cluster layout, and the
              // server already said in the option list why it cannot be built.
              // Found by HOST MODE rather than by option key, because the mode
              // is the fact — a key is a string this file would have to keep in
              // step with the server's.
              clusterUnavailable={
                options?.find(
                  (o) =>
                    !o.eligible &&
                    o.reasons?.length &&
                    o.sizing?.hosts?.some((h) => h.host_mode === 'container'),
                )?.reasons?.[0] ?? null
              }
              cheapestOffered={
                options
                  ?.filter((o) => o.resolved)
                  .reduce<number | null>(
                    (least, o) =>
                      least == null || o.totals.monthly < least ? o.totals.monthly : least,
                    null,
                  ) ?? null
              }
              onClose={() => setArranging(null)}
              onUse={(hosts) => {
                onArranged?.(hosts)
                setArranging(null)
              }}
            />
          )}

          {options && options.length > 0 && (
            <>
              {/* ONE WORKSPACE, ONE QUESTION (U.1). This was a list of every
                  layout the platform produced, each its own card with its own
                  title, price, refusal, advisory notices and diagram — five of
                  them for a stack with a cluster in it, two of which said the
                  same sentence about the Kubernetes API being unreachable.
                  "I am confused, why so many choices are provided." They were
                  never five choices; they were two, and three variations on one
                  of them that the platform is better placed to pick. */}
              <TopologyWorkspace
                options={options}
                chosen={chosen}
                clusterId={clusterId}
                currency={currency}
                environment={environment}
                deploymentTarget={deploymentTarget}
                onChoose={onChoose}
                onClusterChange={onClusterChange}
                onArrange={reference ? setArranging : undefined}
              />

              {/* Submitting without choosing stays allowed, and says what then
                  happens. Blocking it would break every request type that has
                  never had a placement, and inventing a default here would be
                  the client deciding. */}
              <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
                {chosen
                  ? 'This layout is recorded when you submit, and the platform decides again at that moment — an option available now can still be refused then, which is the check working.'
                  : 'You can submit without choosing. The request then carries no layout and the infrastructure team decides how it is built.'}
              </p>
            </>
          )}
        </>
      )}
    </div>
  )
}
