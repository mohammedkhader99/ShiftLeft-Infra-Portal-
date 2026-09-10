/**
 * What will actually be built, drawn (D.2).
 *
 * The option cards answer "which layout"; this answers "what does that mean".
 * A requester reading "Consolidated — one machine hosts all 3 components" has to
 * assemble the picture themselves; a requester looking at one machine with three
 * blocks on it does not.
 *
 * DRAWN IN DOM ELEMENTS RATHER THAN SVG, deliberately. This is the surface D.3
 * makes draggable, and drop targets, keyboard focus and live re-evaluation are
 * all things the DOM gives for free and SVG does not. An SVG version would look
 * the same today and be thrown away next increment.
 *
 * NOTHING HERE IS COMPUTED. Every shape, every figure and every refusal comes
 * from /api/placement/options, which asked OPA whether the layout is permitted
 * and priced it. The diagram is a rendering of that answer — a second place that
 * calculated a shape would be a second authority that disagrees with the first.
 *
 * THE HONESTY RULES CARRY OVER FROM THE OPTION CARDS, because a picture makes it
 * easier to imply something untrue, not harder:
 *
 *   - a host the server could not size is drawn AS unsized, never as a box with
 *     zeros in it. A shape of zero is the same lie as a price of zero, and this
 *     portal has already been bitten by that one;
 *   - a managed service is drawn as something the cloud runs, visibly not a
 *     machine, because whether somebody on the team patches it at 2am is the
 *     difference the diagram most needs to show;
 *   - a refused layout is still drawn. Seeing the arrangement you cannot have,
 *     beside the reason, is how somebody works out what to change.
 */

import { Tag } from '@carbon/react'
import type { PlacementOption, SizedHost } from '../api'

const MODE_LABEL: Record<string, string> = {
  vm: 'Virtual machine',
  container: 'On a cluster',
  managed: 'Run by the cloud',
}

/** A host's shape, or nothing when the server declined to size it. */
function shapeOf(host: SizedHost): string | null {
  const parts: string[] = []
  if (host.vcpu != null) parts.push(`${host.vcpu} vCPU`)
  if (host.memory_gb != null) parts.push(`${host.memory_gb} GB`)
  if (host.storage_gb != null) parts.push(`${host.storage_gb} GB disk`)
  return parts.length ? parts.join(' · ') : null
}

function HostBox({ host }: { host: SizedHost }) {
  const managed = host.host_mode === 'managed'
  const unsized = !managed && !host.resolved
  const shape = shapeOf(host)

  return (
    <li
      style={{
        listStyle: 'none',
        flex: '1 1 15rem',
        minWidth: '13rem',
        display: 'flex',
        flexDirection: 'column',
        background: 'var(--cds-layer)',
        // A managed service is not a machine, and the border says so before any
        // label is read: nothing is provisioned, so nothing is drawn solid.
        border: managed
          ? '1px dashed var(--cds-border-strong)'
          : `1px solid ${unsized ? 'var(--cds-support-error)' : 'var(--cds-border-subtle)'}`,
        borderTop: `3px solid ${
          managed
            ? 'var(--cds-support-success)'
            : unsized
              ? 'var(--cds-support-error)'
              : 'var(--cds-border-interactive)'
        }`,
      }}
    >
      <div style={{ padding: '0.6rem 0.75rem 0.5rem' }}>
        <div
          style={{
            fontSize: '0.68rem',
            textTransform: 'uppercase',
            letterSpacing: '0.02em',
            color: 'var(--cds-text-secondary)',
          }}
        >
          {MODE_LABEL[host.host_mode] || host.host_mode}
        </div>
        <div style={{ fontFamily: 'var(--cds-code-01-font-family, monospace)', fontSize: '0.8rem' }}>
          {host.host_id}
        </div>

        {managed ? (
          <div style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.2rem' }}>
            No machine is provisioned.
          </div>
        ) : unsized ? (
          /* NOT A BOX WITH ZEROS IN IT. The server says which components it has
             no requirement for; drawing a shape anyway would put a number on a
             machine nobody sized. */
          <div style={{ fontSize: '0.72rem', color: 'var(--cds-text-error)', marginTop: '0.2rem' }}>
            Size not determined
            {host.missing?.length ? ` — no requirement for ${host.missing.join(', ')}` : ''}
          </div>
        ) : (
          <div style={{ fontSize: '0.75rem', marginTop: '0.2rem' }}>
            {shape}
            {host.headroom_percent > 0 && (
              <span style={{ color: 'var(--cds-text-secondary)' }}>
                {' '}· incl. {host.headroom_percent}% headroom
              </span>
            )}
          </div>
        )}
      </div>

      {/* What sits on it. The blocks are the thing D.3 will let you move. */}
      <ul
        style={{
          listStyle: 'none',
          margin: 0,
          padding: '0.5rem 0.75rem 0.75rem',
          display: 'flex',
          flexWrap: 'wrap',
          gap: '0.35rem',
          borderTop: '1px solid var(--cds-border-subtle)',
          background: 'var(--cds-layer-accent-01, transparent)',
          flex: 1,
          alignContent: 'flex-start',
        }}
      >
        {host.components.length === 0 ? (
          <li style={{ fontSize: '0.75rem', color: 'var(--cds-text-error)' }}>
            Nothing runs here — it would be built and billed for nothing.
          </li>
        ) : (
          host.components.map((code) => (
            <li key={code}>
              <Tag type={managed ? 'green' : 'blue'} size="md" style={{ margin: 0 }}>
                {code}
              </Tag>
            </li>
          ))
        )}
      </ul>
    </li>
  )
}

export default function TopologyDiagram({
  option,
  environment,
  deploymentTarget,
}: {
  option: PlacementOption
  environment?: string | null
  deploymentTarget?: string | null
}) {
  const hosts = option.sizing?.hosts ?? []
  const machines = hosts.filter((h) => h.host_mode !== 'managed')
  const currency = option.estimate?.currency || 'AED'

  return (
    <section
      aria-label={`Topology for ${option.title}`}
      style={{
        border: '1px solid var(--cds-border-subtle)',
        background: 'var(--cds-layer-01, var(--cds-layer))',
        padding: '0.85rem',
        marginTop: '0.6rem',
      }}
    >
      <header
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: '0.4rem 1rem',
          alignItems: 'baseline',
          marginBottom: '0.7rem',
          fontSize: '0.75rem',
          color: 'var(--cds-text-secondary)',
        }}
      >
        <strong style={{ color: 'var(--cds-text-primary)', fontSize: '0.85rem' }}>
          {option.title}
        </strong>
        {deploymentTarget && <span>{deploymentTarget}</span>}
        {environment && <span>{environment}</span>}
        <span>
          {machines.length === 1 ? '1 machine' : `${machines.length} machines`}
        </span>
        {/* The figure is the option's own, not one this component worked out. */}
        {option.resolved ? (
          <span style={{ marginLeft: 'auto', color: 'var(--cds-text-primary)' }}>
            <strong>{option.totals.monthly.toFixed(2)}</strong> {currency}/mo
          </span>
        ) : (
          <span style={{ marginLeft: 'auto' }}>Not priced</span>
        )}
      </header>

      {hosts.length === 0 ? (
        <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: 0 }}>
          This layout places nothing.
        </p>
      ) : (
        <ul
          style={{
            listStyle: 'none',
            margin: 0,
            padding: 0,
            display: 'flex',
            flexWrap: 'wrap',
            gap: '0.75rem',
            alignItems: 'stretch',
          }}
        >
          {hosts.map((host) => (
            <HostBox key={host.host_id} host={host} />
          ))}
        </ul>
      )}

      {/* A REFUSED LAYOUT IS STILL DRAWN, with the reason under it. Seeing the
          arrangement you cannot have, next to why, is how somebody works out
          what to change — which is the whole point of showing a picture. */}
      {!option.eligible && option.reasons.length > 0 && (
        <div
          style={{
            marginTop: '0.7rem',
            paddingTop: '0.6rem',
            borderTop: '1px solid var(--cds-border-subtle)',
          }}
        >
          {option.reasons.map((reason, i) => (
            <p
              key={i}
              style={{ fontSize: '0.78rem', margin: '0.2rem 0', color: 'var(--cds-text-primary)' }}
            >
              {reason}
            </p>
          ))}
        </div>
      )}
    </section>
  )
}
