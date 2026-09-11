/**
 * The rules for rearranging a topology, as plain functions (D.3).
 *
 * WHY THESE LEFT THE COMPONENT. They were closures inside TopologyEditor, which
 * made them unreachable from anything but a browser — and this frontend has no
 * test runner, no jsdom and no way to fire a drag. So "does dragging the
 * database onto its own machine work?" was a question nobody could answer
 * except by opening the page and trying it, which is exactly how a bug reached
 * a requester twice.
 *
 * Out here they are ordinary functions over ordinary data, and
 * webapp/frontend/tests/topology-editor.render.tsx exercises them directly.
 *
 * NOTHING HERE DECIDES ANYTHING. These say what the requester ARRANGED. Whether
 * an arrangement may be built is answered by /api/placement/evaluate, which runs
 * the same policy, sizing and pricing the offered layouts get. This module holds
 * no opinion about that and must never grow one.
 */

import type { PlacementOption, ProposedHost } from '../api'

export const MANAGED = 'managed'

/**
 * What each kind of host is called, on every screen that draws one.
 *
 * ONE COPY. The editor and the read-only diagram each had their own, which is
 * two places to change and one to forget — and the first thing that needed
 * changing was `container`, which both spelled "On a cluster". A requester who
 * had asked for exactly "minio and Oracle in a container on OKE" was looking at
 * that phrase and asked where it was.
 */
export const MODE_LABEL: Record<string, string> = {
  vm: 'Virtual machine',
  container: 'Container on the cluster',
  managed: 'Run by the cloud',
}

/** The starting arrangement, taken from whichever layout is being adapted. */
export function hostsOf(option: PlacementOption): ProposedHost[] {
  return (option.sizing?.hosts ?? []).map((h) => ({
    id: h.host_id,
    host_mode: h.host_mode,
    components: [...h.components],
  }))
}

/** A host id nothing else is using. */
export function nextHostId(hosts: ProposedHost[]): string {
  let n = hosts.length + 1
  const taken = new Set(hosts.map((h) => h.id))
  while (taken.has(`host-${n}`)) n += 1
  return `host-${n}`
}

/**
 * What is actually PROPOSED to the server.
 *
 * A host carrying nothing is not a machine anybody asked for, and the server
 * rightly refuses one: "it would be built and billed for nothing."
 *
 * Before this existed, dragging the last component off a block left that block
 * empty and put the WHOLE arrangement into a refused state — so moving the
 * database onto its own machine, an entirely ordinary thing to want, read as
 * the drag having done nothing. "Add a machine" had the same fault in reverse:
 * it created an empty host, so the layout was refused the instant it was
 * clicked, before anything could be dropped on.
 *
 * An empty block is somewhere to drop things. It is not a machine, so it is not
 * part of the proposal — and the server's rule is untouched: if an empty host
 * ever does reach it, it is still refused.
 */
export function proposedOf(hosts: ProposedHost[]): ProposedHost[] {
  return hosts.filter((h) => h.components.length > 0)
}

/** Which components each host started with, so a managed block knows its own. */
export function belongsToOf(original: ProposedHost[]): Map<string, Set<string>> {
  const map = new Map<string, Set<string>>()
  for (const h of original) map.set(h.id, new Set(h.components))
  return map
}

/**
 * Whether `host` will take `code`.
 *
 * A managed block is the cloud running ONE named component. Nothing else may be
 * dropped onto it — but the component it was created for may come back, because
 * otherwise dragging it out is a one-way door and the only way to reverse one
 * move is to undo every change made since it.
 */
export function accepts(
  belongsTo: Map<string, Set<string>>,
  host: ProposedHost,
  code: string | undefined,
): boolean {
  if (!code) return false
  if (host.host_mode !== MANAGED) return true
  return belongsTo.get(host.id)?.has(code) ?? false
}

/** `code` leaves `from` and joins `to`. Hosts left empty are kept on screen. */
export function move(
  hosts: ProposedHost[],
  code: string,
  from: string,
  to: string,
): ProposedHost[] {
  if (from === to) return hosts
  return hosts.map((h) =>
    h.id === from
      ? { ...h, components: h.components.filter((c) => c !== code) }
      : h.id === to
        ? { ...h, components: [...h.components, code] }
        : h,
  )
}

/** `code` leaves `from` for a machine that does not exist yet. */
export function moveToNewMachine(
  hosts: ProposedHost[],
  code: string,
  from: string,
): ProposedHost[] {
  return [
    ...hosts.map((h) =>
      h.id === from ? { ...h, components: h.components.filter((c) => c !== code) } : h,
    ),
    { id: nextHostId(hosts), host_mode: 'vm', components: [code] },
  ]
}

export function addMachine(hosts: ProposedHost[]): ProposedHost[] {
  return [...hosts, { id: nextHostId(hosts), host_mode: 'vm', components: [] }]
}

export function removeHost(hosts: ProposedHost[], id: string): ProposedHost[] {
  return hosts.filter((h) => h.id !== id)
}

/** Machines that would actually be built — what the count beside the price means. */
export function machineCount(hosts: ProposedHost[]): number {
  return proposedOf(hosts).filter((h) => h.host_mode !== MANAGED).length
}
