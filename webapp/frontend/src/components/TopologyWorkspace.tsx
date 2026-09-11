/**
 * One workspace, one question: machines, or Kubernetes? (U.1)
 *
 * WHAT THIS REPLACED, AND WHY. The step used to render every layout the
 * platform produced as its own card — five of them for a stack with a cluster
 * in it, each with a title, a summary, a price, a refusal, its own advisory
 * notices and its own diagram. Two of the five said the same sentence about the
 * Kubernetes API being unreachable. The requester's reaction was the correct
 * one: "I am confused, why so many choices are provided."
 *
 * They were never five choices. They were two — run this on machines, or run it
 * on Kubernetes — and three variations on the first that the platform is better
 * placed to pick than the person asking for a database.
 *
 * So: the stack, one line about cluster availability, two routes, and the
 * topology drawn for whichever is chosen. The machine layouts collapse to the
 * platform's recommendation, with the others behind a disclosure for anyone who
 * wants them, and "Arrange it yourself" for anyone who wants something else
 * entirely.
 *
 * NOTHING IS DECIDED HERE. Every layout, price, refusal and warning on this
 * screen was computed by /api/placement/options. This component chooses which of
 * them to show and in what order — display, not authority. It recomputes no
 * price and overrides no refusal, and the key it hands back on selection is one
 * the server produced.
 *
 * THE ROUTE IS THE SERVER'S FACT TOO. Layouts are grouped by the `route` field
 * the API sends, not by matching option keys here: a list of cluster keys kept
 * in the browser is one that goes stale the day a layout is added, and the
 * requester is the one who would find out.
 */

import { useMemo, useState } from 'react'
import { Button, Tag, Tile } from '@carbon/react'
import type { PlacementCluster, PlacementOption } from '../api'
import TopologyDiagram from './TopologyDiagram'

export const MACHINES = 'machines'
export const KUBERNETES = 'kubernetes'

/** Every component the request places, however the layouts arrange them. */
export function stackOf(options: PlacementOption[]): string[] {
  const seen = new Set<string>()
  for (const o of options) {
    for (const h of o.sizing?.hosts ?? []) for (const c of h.components) seen.add(c)
    for (const h of o.hosts ?? []) for (const c of h.components ?? []) seen.add(c)
  }
  return [...seen]
}

/**
 * The layout the platform would pick for a route.
 *
 * The cheapest one that can actually be built, because that is the
 * recommendation a platform owes somebody who did not ask to compare layouts.
 * Falling back to the first when none can be priced keeps the route selectable
 * and keeps its refusal on screen — a route that vanishes because nothing in it
 * is priceable is indistinguishable from a portal that is broken.
 */
export function recommended(options: PlacementOption[], route: string): PlacementOption | null {
  const inRoute = options.filter((o) => o.route === route)
  if (!inRoute.length) return null
  const buildable = inRoute.filter((o) => o.eligible && o.resolved)
  if (!buildable.length) return inRoute.find((o) => o.eligible) ?? inRoute[0]
  return buildable.reduce((best, o) => (o.totals.monthly < best.totals.monthly ? o : best))
}

/** Clusters the platform found, across whichever layouts could land on one. */
export function clustersOffered(options: PlacementOption[]): PlacementCluster[] {
  const byId = new Map<string, PlacementCluster>()
  for (const o of options) for (const c of o.clusters ?? []) byId.set(c.id, c)
  return [...byId.values()]
}

function Money({ option, currency }: { option: PlacementOption; currency: string }) {
  if (!option.resolved) {
    return <span style={{ color: 'var(--cds-text-secondary)' }}>Not priced</span>
  }
  return (
    <span>
      <strong>{option.totals.monthly.toFixed(2)}</strong>{' '}
      <span style={{ fontSize: '0.8rem' }}>{currency}/mo</span>
    </span>
  )
}

function RouteCard({
  title,
  blurb,
  option,
  currency,
  selected,
  onSelect,
}: {
  title: string
  blurb: string
  option: PlacementOption
  currency: string
  selected: boolean
  onSelect: () => void
}) {
  return (
    <Tile
      style={{
        flex: '1 1 18rem',
        padding: '0.85rem',
        borderLeft: `4px solid ${
          selected ? 'var(--cds-interactive)' : 'var(--cds-border-subtle)'
        }`,
        background: selected ? 'var(--cds-layer-selected, var(--cds-layer-01))' : undefined,
      }}
    >
      <div style={{ display: 'flex', justifyContent: 'space-between', gap: '0.5rem', alignItems: 'baseline' }}>
        <strong style={{ fontSize: '0.9rem' }}>{title}</strong>
        <Money option={option} currency={currency} />
      </div>
      <p style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', margin: '0.35rem 0 0.6rem' }}>
        {blurb}
      </p>
      {/* WHICH LAYOUT THIS ROUTE WOULD ACTUALLY USE. A card headed "OCI
          Kubernetes (OKE)" that silently resolves to "Deploy onto an existing
          cluster" is a choice the requester cannot check — and the two are
          different requests. */}
      <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.5rem' }}>
        Layout: {option.title}
      </p>

      {/* THE REFUSAL TRAVELS WITH THE ROUTE. A route that can be picked but not
          built has to say so here, where the picking happens, rather than
          leaving the requester to press it and find out. */}
      {!option.eligible &&
        [...new Set(option.reasons)].map((r) => (
          <p key={r} style={{ fontSize: '0.75rem', color: 'var(--cds-text-error)', margin: '0 0 0.4rem' }}>
            {r}
          </p>
        ))}

      <Button
        size="sm"
        kind={selected ? 'primary' : 'tertiary'}
        disabled={!option.eligible}
        onClick={onSelect}
      >
        {selected ? 'Selected' : 'Select'}
      </Button>
    </Tile>
  )
}

export default function TopologyWorkspace({
  options,
  chosen,
  clusterId,
  currency,
  environment,
  deploymentTarget,
  onChoose,
  onClusterChange,
  onArrange,
}: {
  options: PlacementOption[]
  chosen: string | null
  clusterId: string | null
  currency: string
  environment?: string | null
  deploymentTarget?: string | null
  onChoose: (key: string) => void
  onClusterChange: (id: string) => void
  onArrange?: (option: PlacementOption) => void
}) {
  const stack = useMemo(() => stackOf(options), [options])
  const machines = useMemo(() => recommended(options, MACHINES), [options])
  const kubernetes = useMemo(() => recommended(options, KUBERNETES), [options])
  const clusters = useMemo(() => clustersOffered(options), [options])

  // Which route the current selection belongs to, so the diagram follows the
  // choice rather than needing its own state to fall out of step with it.
  const route = useMemo(() => {
    const picked = options.find((o) => o.key === chosen)
    return picked?.route ?? null
  }, [options, chosen])

  const showing = route === KUBERNETES ? kubernetes : route === MACHINES ? machines : null
  const [otherLayouts, setOtherLayouts] = useState(false)

  // Layouts you could pick INSTEAD, within the route you are on.
  //
  // Only the ones that can actually be built: an unavailable layout is not an
  // alternative, it is a fact, and it belongs below with the other facts rather
  // than in a list of things to choose from.
  //
  // WITHIN THE ROUTE, not within machines. This read `o.route === MACHINES` and
  // the workspace harness caught what that costs the moment a Kubernetes layout
  // became choosable: with both an existing cluster and a new one available, one
  // of them was the route card and the other appeared NOWHERE — not as a choice,
  // not in "Also considered", not at all. A requester who has a cluster and
  // wants a fresh one instead could not say so.
  const onRoute = route ?? MACHINES
  const chosenForRoute = onRoute === KUBERNETES ? kubernetes : machines
  const alternatives = options.filter(
    (o) => o.route === onRoute && o.eligible && o.key !== chosenForRoute?.key,
  )

  // EVERYTHING THAT CANNOT BE BUILT, AND WHY — one line each, on the page,
  // without being asked for.
  //
  // The old screen gave each of these a full card, which is what made five
  // layouts unreadable: two of them printed the same sentence about the
  // Kubernetes API being unreachable, at full size, one above the other. But
  // dropping them was worse and the render harness caught it immediately: a
  // layout that vanishes is indistinguishable from a portal that is broken, and
  // "why can't I consolidate these?" is a question the requester is entitled to
  // an answer to whether or not they asked for it.
  //
  // So: still every reason, deduplicated, in one compact block instead of
  // three cards. What is already explained on a route card is left out — the
  // same sentence twice is the thing being fixed.
  // A layout needs explaining when it cannot be built OR cannot be priced. The
  // second half matters as much as the first: "we do not know what this costs"
  // is not the same as a number, and a portal that quietly omits the layout
  // rather than saying so is how a figure nobody calculated reaches an
  // approver. The route cards already say it for the two recommendations.
  const explainedOnCard = new Set([machines?.key, kubernetes?.key].filter(Boolean))
  const alsoConsidered = options.filter(
    (o) => (!o.eligible || !o.resolved) && !explainedOnCard.has(o.key),
  )

  return (
    <div style={{ marginTop: '0.75rem' }}>
      {/* --- what is being laid out ---------------------------------------- */}
      <div style={{ display: 'flex', gap: '0.4rem', alignItems: 'baseline', flexWrap: 'wrap' }}>
        <span
          style={{
            fontSize: '0.68rem',
            textTransform: 'uppercase',
            letterSpacing: '0.02em',
            color: 'var(--cds-text-secondary)',
          }}
        >
          Your stack
        </span>
        {stack.map((code) => (
          <Tag key={code} type="cool-gray" size="sm" style={{ margin: 0 }}>
            {code}
          </Tag>
        ))}
      </div>

      {/* --- one line on cluster availability ------------------------------- */}
      {kubernetes && (
        <p
          style={{
            fontSize: '0.78rem',
            color: 'var(--cds-text-secondary)',
            margin: '0.6rem 0 0',
            borderLeft: '3px solid var(--cds-support-warning)',
            paddingLeft: '0.6rem',
          }}
        >
          {clusters.length
            ? `${clusters.length} OCI Kubernetes cluster${clusters.length === 1 ? '' : 's'} available to this request.`
            : 'No suitable OCI Kubernetes (OKE) cluster is currently available. A new one will be created if you choose Kubernetes.'}
        </p>
      )}

      {/* --- the one question ----------------------------------------------- */}
      <p
        style={{
          fontSize: '0.68rem',
          textTransform: 'uppercase',
          letterSpacing: '0.02em',
          color: 'var(--cds-text-secondary)',
          margin: '0.9rem 0 0.4rem',
        }}
      >
        How would you like to host this workload?
      </p>

      <div style={{ display: 'flex', gap: '0.75rem', flexWrap: 'wrap', alignItems: 'stretch' }}>
        {machines && (
          <RouteCard
            title="Independent machines"
            blurb="Components run on dedicated OCI machines and managed services. The platform picks the shapes, images and networking from its approved standards."
            option={machines}
            currency={currency}
            selected={route === MACHINES}
            onSelect={() => onChoose(machines.key)}
          />
        )}
        {kubernetes && (
          <RouteCard
            title="OCI Kubernetes (OKE)"
            blurb={
              clusters.length
                ? 'Components run as containers on an existing OKE cluster.'
                : 'A new OKE cluster is created to approved standards, and applicable components run on it as containers.'
            }
            option={kubernetes}
            currency={currency}
            selected={route === KUBERNETES}
            onSelect={() => onChoose(kubernetes.key)}
          />
        )}
      </div>

      {/* --- what cannot be built, and why ---------------------------------- */}
      {alsoConsidered.length > 0 && (
        <div style={{ marginTop: '0.6rem' }}>
          <span
            style={{
              fontSize: '0.68rem',
              textTransform: 'uppercase',
              letterSpacing: '0.02em',
              color: 'var(--cds-text-secondary)',
            }}
          >
            Also considered
          </span>
          <ul style={{ listStyle: 'none', margin: '0.25rem 0 0', padding: 0 }}>
            {alsoConsidered.map((o) => (
              <li key={o.key} style={{ fontSize: '0.75rem', padding: '0.15rem 0' }}>
                <strong style={{ color: 'var(--cds-text-secondary)' }}>{o.title}</strong>{' '}
                {!o.eligible && (
                  <span style={{ color: 'var(--cds-text-error)' }}>
                    {[...new Set(o.reasons)].join(' ')}{' '}
                  </span>
                )}
                {!o.resolved && (
                  <span style={{ color: 'var(--cds-text-secondary)' }}>
                    Not priced — the platform could not work out what this would cost.
                  </span>
                )}
              </li>
            ))}
          </ul>
        </div>
      )}

      {/* --- which existing cluster, only once Kubernetes is the choice ------ */}
      {route === KUBERNETES && clusters.length > 0 && (
        <div style={{ marginTop: '0.6rem', fontSize: '0.78rem' }}>
          <label htmlFor="workspace-cluster" style={{ marginRight: '0.5rem' }}>
            Cluster
          </label>
          <select
            id="workspace-cluster"
            value={clusterId || ''}
            onChange={(e) => onClusterChange(e.target.value)}
          >
            <option value="">Choose a cluster…</option>
            {clusters.map((c) => (
              <option key={c.id} value={c.id} disabled={!c.eligible}>
                {c.name}
                {c.eligible ? '' : ` — ${c.reasons?.[0] || 'not available'}`}
              </option>
            ))}
          </select>
        </div>
      )}

      {/* --- the topology, redrawn for whatever is chosen -------------------- */}
      {showing ? (
        <div style={{ marginTop: '0.9rem' }}>
          <div
            style={{
              display: 'flex',
              justifyContent: 'space-between',
              alignItems: 'baseline',
              gap: '0.5rem',
            }}
          >
            <span
              style={{
                fontSize: '0.68rem',
                textTransform: 'uppercase',
                letterSpacing: '0.02em',
                color: 'var(--cds-text-secondary)',
              }}
            >
              What gets built
            </span>
            {onArrange && (
              <Button kind="ghost" size="sm" onClick={() => onArrange(showing)}>
                Arrange it yourself
              </Button>
            )}
          </div>
          <TopologyDiagram
            option={showing}
            environment={environment}
            deploymentTarget={deploymentTarget}
          />
        </div>
      ) : (
        <p style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', marginTop: '0.9rem' }}>
          Choose how this should be hosted and the topology is drawn here.
        </p>
      )}

      {/* --- the machine layouts the platform did not pick -------------------
          Behind a disclosure rather than gone: somebody who knows they want one
          machine rather than three should be able to say so, and nobody should
          have to compare five cards to accept the recommendation. */}
      {alternatives.length > 0 && (
        <div style={{ marginTop: '0.6rem' }}>
          <Button kind="ghost" size="sm" onClick={() => setOtherLayouts((v) => !v)}>
            {otherLayouts
              ? 'Hide the other layouts'
              : `Other ${onRoute === KUBERNETES ? 'Kubernetes' : 'machine'} layouts ` +
                `you could pick (${alternatives.length})`}
          </Button>
          {otherLayouts && (
            <ul style={{ listStyle: 'none', margin: '0.4rem 0 0', padding: 0 }}>
              {alternatives.map((o) => (
                <li
                  key={o.key}
                  style={{
                    display: 'flex',
                    gap: '0.5rem',
                    alignItems: 'baseline',
                    padding: '0.35rem 0',
                    borderTop: '1px solid var(--cds-border-subtle)',
                    fontSize: '0.8rem',
                  }}
                >
                  <strong style={{ minWidth: '9rem' }}>{o.title}</strong>
                  <span style={{ color: 'var(--cds-text-secondary)', flex: 1 }}>
                    {o.summary}
                  </span>
                  <Money option={o} currency={currency} />
                  <Button kind="ghost" size="sm" onClick={() => onChoose(o.key)}>
                    {chosen === o.key ? 'Selected' : 'Use this'}
                  </Button>
                </li>
              ))}
            </ul>
          )}
        </div>
      )}
    </div>
  )
}
