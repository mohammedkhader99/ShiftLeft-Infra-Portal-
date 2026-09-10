/**
 * Rearrange the topology, and be told what it costs (D.3).
 *
 * Drag a component onto another machine, add a machine, take one away — and the
 * portal re-prices and re-checks it as you go. The requester is the one who
 * knows their application; the platform is the one that knows what things cost
 * and what policy forbids. This is where those meet.
 *
 * A REFUSED ARRANGEMENT IS KEPT ON SCREEN. This is the decision the whole
 * component is built around, and the obvious implementation gets it wrong.
 *
 * Snapping the block back is what most drag-and-drop does when a drop is
 * invalid, and here it would be actively unhelpful: the requester was
 * expressing an intention — "these two belong together" — and the portal would
 * erase it and say nothing they can act on. So an invalid arrangement stays
 * exactly as they left it, the reason is attached to the block they moved, and
 * they choose what to do about it. Undo is right there for when the answer is
 * "put it back".
 *
 * NOTHING IS DECIDED HERE. Every figure and every refusal comes from
 * /api/placement/evaluate, which runs the same policy check, the same sizing and
 * the same pricing the offered layouts get. The browser holds the arrangement;
 * it holds no opinion about whether the arrangement is allowed.
 *
 * DRAGGING IS NOT THE ONLY WAY IN. Every block also carries a "Move to" menu, so
 * the feature works by keyboard and with a screen reader. A capability only
 * reachable by dragging a mouse is a capability some colleagues do not have.
 */

import { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import {
  Button,
  InlineNotification,
  OverflowMenu,
  OverflowMenuItem,
  Tag,
} from '@carbon/react'
import { evaluateLayout, type EvaluatedLayout, type PlacementOption, type ProposedHost } from '../api'

const MODE_LABEL: Record<string, string> = {
  vm: 'Virtual machine',
  container: 'On a cluster',
  managed: 'Run by the cloud',
}

/** The starting arrangement, taken from whichever layout is being adapted. */
function hostsOf(option: PlacementOption): ProposedHost[] {
  return (option.sizing?.hosts ?? []).map((h) => ({
    id: h.host_id,
    host_mode: h.host_mode,
    components: [...h.components],
  }))
}

/** A host id nothing else is using. */
function nextHostId(hosts: ProposedHost[]): string {
  let n = hosts.length + 1
  const taken = new Set(hosts.map((h) => h.id))
  while (taken.has(`host-${n}`)) n += 1
  return `host-${n}`
}

/**
 * Which refusals are about this component.
 *
 * The server's sentences name what they are about — "mssql cannot run as
 * container", "oracle-db … did not ask for" — so matching on the code puts each
 * message beside the block it concerns. A reason that names nothing specific
 * falls through to the summary, which is the honest place for it.
 */
function reasonsFor(code: string, reasons: string[]): string[] {
  return reasons.filter((r) => r.includes(code))
}

function unattributed(reasons: string[], codes: string[]): string[] {
  return reasons.filter((r) => !codes.some((c) => r.includes(c)))
}

export default function TopologyEditor({
  reference,
  option,
  environment,
  deploymentTarget,
  onUse,
  onClose,
}: {
  reference: string
  option: PlacementOption
  environment?: string | null
  deploymentTarget?: string | null
  /** Choose this arrangement. The server re-judges it before recording. */
  onUse: (hosts: ProposedHost[]) => void
  onClose: () => void
}) {
  const [hosts, setHosts] = useState<ProposedHost[]>(() => hostsOf(option))
  const [history, setHistory] = useState<ProposedHost[][]>([])
  const [judged, setJudged] = useState<EvaluatedLayout | null>(null)
  const [busy, setBusy] = useState(false)
  const [failed, setFailed] = useState<string | null>(null)
  // The block most recently moved, so its refusal can be pointed at rather than
  // left for the requester to match up by reading.
  const [moved, setMoved] = useState<string | null>(null)
  const dragging = useRef<{ code: string; from: string } | null>(null)

  const key = useMemo(() => JSON.stringify(hosts), [hosts])

  useEffect(() => {
    let cancelled = false
    setBusy(true)
    setFailed(null)
    evaluateLayout(reference, hosts)
      .then(({ status, body }) => {
        if (cancelled) return
        if (status !== 200) {
          setFailed(body?.detail || `The platform could not answer (HTTP ${status}).`)
          setJudged(null)
          return
        }
        setJudged(body as EvaluatedLayout)
      })
      .catch(() => !cancelled && setFailed('The platform could not be reached.'))
      .finally(() => !cancelled && setBusy(false))
    return () => {
      cancelled = true
    }
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [key, reference])

  const change = useCallback((next: ProposedHost[], justMoved: string | null) => {
    setHistory((h) => [...h, hosts])
    setHosts(next)
    setMoved(justMoved)
    // eslint-disable-next-line react-hooks/exhaustive-deps
  }, [hosts])

  const move = useCallback((code: string, from: string, to: string) => {
    if (from === to) return
    change(
      hosts.map((h) =>
        h.id === from
          ? { ...h, components: h.components.filter((c) => c !== code) }
          : h.id === to
            ? { ...h, components: [...h.components, code] }
            : h,
      ),
      code,
    )
  }, [hosts, change])

  const addMachine = () =>
    change([...hosts, { id: nextHostId(hosts), host_mode: 'vm', components: [] }], null)

  const removeHost = (id: string) =>
    change(hosts.filter((h) => h.id !== id), null)

  const undo = () => {
    const previous = history[history.length - 1]
    if (!previous) return
    setHistory((h) => h.slice(0, -1))
    setHosts(previous)
    setMoved(null)
  }

  const reasons = judged?.reasons ?? []
  const allCodes = hosts.flatMap((h) => h.components)
  const currency = judged?.estimate?.currency || option.estimate?.currency || 'AED'

  return (
    <section
      aria-label="Arrange the topology"
      style={{
        border: '1px solid var(--cds-border-interactive)',
        background: 'var(--cds-layer)',
        padding: '0.85rem',
        marginTop: '0.6rem',
      }}
    >
      <header
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          alignItems: 'baseline',
          gap: '0.5rem 1rem',
          marginBottom: '0.75rem',
        }}
      >
        <strong style={{ fontSize: '0.9rem' }}>Arrange it yourself</strong>
        <span style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)' }}>
          Drag a component onto another machine, or use its menu. Every change is
          re-checked and re-priced.
        </span>
        <span style={{ marginLeft: 'auto', display: 'flex', gap: '0.4rem' }}>
          <Button kind="ghost" size="sm" onClick={undo} disabled={!history.length}>
            Undo
          </Button>
          <Button kind="ghost" size="sm" onClick={onClose}>
            Done
          </Button>
        </span>
      </header>

      <div
        style={{
          display: 'flex',
          flexWrap: 'wrap',
          gap: '0.5rem 1rem',
          alignItems: 'baseline',
          fontSize: '0.78rem',
          color: 'var(--cds-text-secondary)',
          marginBottom: '0.6rem',
        }}
      >
        {deploymentTarget && <span>{deploymentTarget}</span>}
        {environment && <span>{environment}</span>}
        <span>
          {hosts.filter((h) => h.host_mode !== 'managed').length === 1
            ? '1 machine'
            : `${hosts.filter((h) => h.host_mode !== 'managed').length} machines`}
        </span>
        <span style={{ marginLeft: 'auto', color: 'var(--cds-text-primary)' }}>
          {busy ? (
            'Re-checking…'
          ) : judged?.resolved ? (
            <>
              <strong>{judged.totals.monthly.toFixed(2)}</strong> {currency}/mo
              {judged.monthly_delta != null && judged.monthly_delta !== 0 && (
                <span style={{ color: 'var(--cds-text-secondary)' }}>
                  {' '}({judged.monthly_delta > 0 ? '+' : ''}
                  {judged.monthly_delta.toFixed(2)} vs the cheapest offered)
                </span>
              )}
            </>
          ) : (
            'Not priced'
          )}
        </span>
      </div>

      {failed && (
        <InlineNotification
          kind="error"
          lowContrast
          hideCloseButton
          title="Could not check this arrangement"
          subtitle={failed}
          style={{ maxWidth: 'none', marginBottom: '0.6rem' }}
        />
      )}

      {/* THE ARRANGEMENT STAYS AS IT WAS LEFT. The refusals sit against the
          blocks below; this only says how many there are, so a requester who
          scrolled away knows to look. */}
      {!busy && judged && !judged.eligible && (
        <InlineNotification
          kind="warning"
          lowContrast
          hideCloseButton
          title="This arrangement cannot be built"
          subtitle={
            unattributed(reasons, allCodes).join(' ') ||
            `${reasons.length} problem${reasons.length > 1 ? 's' : ''}, marked below. Undo puts it back.`
          }
          style={{ maxWidth: 'none', marginBottom: '0.6rem' }}
        />
      )}

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
        {hosts.map((host) => {
          const sized = judged?.sizing?.hosts?.find((h) => h.host_id === host.id)
          const managed = host.host_mode === 'managed'
          return (
            <li
              key={host.id}
              onDragOver={(e) => {
                // A managed service is run by the cloud; nothing is dropped onto
                // it, and refusing the drop is clearer than accepting it and
                // explaining afterwards.
                if (!managed) e.preventDefault()
              }}
              onDrop={(e) => {
                e.preventDefault()
                const held = dragging.current
                if (held && !managed) move(held.code, held.from, host.id)
                dragging.current = null
              }}
              style={{
                flex: '1 1 15rem',
                minWidth: '13rem',
                display: 'flex',
                flexDirection: 'column',
                background: 'var(--cds-layer-01, var(--cds-layer))',
                border: managed
                  ? '1px dashed var(--cds-border-strong)'
                  : '1px solid var(--cds-border-subtle)',
                borderTop: `3px solid ${
                  managed ? 'var(--cds-support-success)' : 'var(--cds-border-interactive)'
                }`,
              }}
            >
              <div style={{ padding: '0.6rem 0.75rem 0.4rem' }}>
                <div
                  style={{
                    display: 'flex',
                    alignItems: 'baseline',
                    justifyContent: 'space-between',
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
                    {MODE_LABEL[host.host_mode] || host.host_mode}
                  </span>
                  {!managed && host.components.length === 0 && (
                    <Button kind="ghost" size="sm" onClick={() => removeHost(host.id)}>
                      Remove
                    </Button>
                  )}
                </div>
                <div style={{ fontSize: '0.8rem', fontFamily: 'monospace' }}>{host.id}</div>
                <div style={{ fontSize: '0.73rem', color: 'var(--cds-text-secondary)' }}>
                  {managed
                    ? 'No machine is provisioned.'
                    : sized?.resolved
                      ? `${sized.vcpu} vCPU · ${sized.memory_gb} GB · ${sized.storage_gb} GB disk`
                      : busy
                        ? 'Re-checking…'
                        : 'Size not determined'}
                </div>
              </div>

              <ul
                style={{
                  listStyle: 'none',
                  margin: 0,
                  padding: '0.5rem 0.75rem 0.75rem',
                  borderTop: '1px solid var(--cds-border-subtle)',
                  display: 'flex',
                  flexDirection: 'column',
                  gap: '0.35rem',
                  flex: 1,
                }}
              >
                {host.components.length === 0 ? (
                  <li style={{ fontSize: '0.74rem', color: 'var(--cds-text-secondary)' }}>
                    Drop something here.
                  </li>
                ) : (
                  host.components.map((code) => {
                    const problems = reasonsFor(code, reasons)
                    return (
                      <li key={code}>
                        <div
                          draggable
                          onDragStart={() => {
                            dragging.current = { code, from: host.id }
                          }}
                          style={{
                            display: 'flex',
                            alignItems: 'center',
                            gap: '0.35rem',
                            cursor: 'grab',
                          }}
                        >
                          <Tag
                            type={problems.length ? 'red' : managed ? 'green' : 'blue'}
                            size="md"
                            style={{ margin: 0 }}
                          >
                            {code}
                          </Tag>
                          {/* THE KEYBOARD PATH. A capability only reachable by
                              dragging a mouse is one some colleagues do not
                              have. */}
                          <OverflowMenu
                            size="sm"
                            aria-label={`Move ${code}`}
                            flipped
                          >
                            {hosts
                              .filter((h) => h.id !== host.id && h.host_mode !== 'managed')
                              .map((target) => (
                                <OverflowMenuItem
                                  key={target.id}
                                  itemText={`Move to ${target.id}`}
                                  onClick={() => move(code, host.id, target.id)}
                                />
                              ))}
                            <OverflowMenuItem
                              itemText="Move to a new machine"
                              onClick={() => {
                                const id = nextHostId(hosts)
                                change(
                                  [
                                    ...hosts.map((h) =>
                                      h.id === host.id
                                        ? { ...h, components: h.components.filter((c) => c !== code) }
                                        : h,
                                    ),
                                    { id, host_mode: 'vm', components: [code] },
                                  ],
                                  code,
                                )
                              }}
                            />
                          </OverflowMenu>
                        </div>

                        {/* THE REASON, BESIDE THE BLOCK IT IS ABOUT. Not a banner
                            at the top the requester has to map back onto what
                            they just did. */}
                        {problems.map((reason, i) => (
                          <p
                            key={i}
                            style={{
                              margin: '0.2rem 0 0',
                              fontSize: '0.72rem',
                              color: 'var(--cds-text-error)',
                              fontWeight: code === moved ? 600 : 400,
                            }}
                          >
                            {reason}
                          </p>
                        ))}
                      </li>
                    )
                  })
                )}
              </ul>
            </li>
          )
        })}
      </ul>

      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'center', marginTop: '0.75rem' }}>
        <Button kind="tertiary" size="sm" onClick={addMachine}>
          Add a machine
        </Button>
        <Button
          size="sm"
          disabled={busy || !judged?.eligible}
          onClick={() => onUse(hosts)}
        >
          Use this layout
        </Button>
        <span style={{ fontSize: '0.73rem', color: 'var(--cds-text-secondary)' }}>
          {judged?.eligible
            ? 'Recorded when you submit — the platform decides again at that moment.'
            : 'Fix what is marked, or undo.'}
        </span>
      </div>
    </section>
  )
}
