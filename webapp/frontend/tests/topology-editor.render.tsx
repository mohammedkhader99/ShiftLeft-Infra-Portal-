/**
 * Does "Arrange it yourself" actually work? (D.3 / H.5)
 *
 * Reported twice from a real screen — "when I moved the DB to a new VM it is
 * not actually doing", then "Arrange it yourself, do not work now" — and both
 * times the honest answer was that nobody could check. `tsc --noEmit` proves the
 * types line up. The placement-step harness proves the STEP renders. Neither
 * could say whether dragging a component does what dragging it should, because
 * the rules lived in closures inside the component and this frontend has no test
 * runner, no jsdom and no way to fire a drag event.
 *
 * So the rules moved out into src/components/topologyArrangement.ts, and this
 * exercises them directly — data in, data out — alongside a server render of the
 * editor in the states it can actually be in.
 *
 * HOW TO RUN IT (needs Docker; there is no node on the host):
 *
 *   docker build --target build -t placement-render-check ./webapp
 *   docker run --rm -v "${PWD}/webapp/frontend/tests:/tests:ro" -w /frontend \
 *     placement-render-check sh -c \
 *     'cp /tests/topology-editor.render.tsx . && \
 *      ./node_modules/.bin/esbuild ./topology-editor.render.tsx --bundle \
 *        --platform=node --format=cjs --outfile=/tmp/e.cjs --jsx=automatic \
 *        --loader:.json=json && node /tmp/e.cjs'
 *
 * On Windows run that from PowerShell, not Git Bash: Git Bash rewrites the
 * container paths (`-w /frontend` becomes `C:/Program Files/Git/frontend`).
 *
 * A pass prints ALL EDITOR CHECKS PASSED and exits 0.
 */
import { renderToString } from 'react-dom/server'
import { createElement as h } from 'react'
import TopologyEditor from './src/components/TopologyEditor'
import {
  accepts,
  addMachine,
  belongsToOf,
  hostsOf,
  machineCount,
  move,
  moveToNewMachine,
  proposedOf,
} from './src/components/topologyArrangement'
import type { PlacementOption } from './src/api'

let failures = 0

function check(label: string, condition: boolean, detail = '') {
  if (condition) {
    console.log(`ok    ${label}${detail ? `  (${detail})` : ''}`)
    return
  }
  failures += 1
  console.error(`FAIL  ${label}${detail ? `  -- ${detail}` : ''}`)
}

/** Server-rendered markup escapes quotes; compare on the text that matters. */
function shows(html: string, text: string): boolean {
  return html.replace(/&#x27;/g, "'").replace(/&quot;/g, '"').includes(text)
}

/**
 * THE LAYOUT FROM THE REPORTED SCREEN, in the shape the API sends: the cloud
 * runs the cluster and the database, and a machine carries the rest.
 */
const MANAGED_LAYOUT: PlacementOption = {
  key: 'managed',
  route: 'machines',
  title: 'Managed where available',
  summary: 'The cloud runs oci-oke, postgres16.',
  hosts: [],
  host_count: 1,
  eligible: true,
  reasons: [],
  clusters: [],
  warnings: [],
  sizing: {
    hosts: [
      {
        host_id: 'managed-oci-oke', host_mode: 'managed', components: ['oci-oke'],
        resolved: true, missing: [], headroom_percent: 20,
        vcpu: null, memory_gb: null, storage_gb: null, iops: null,
      },
      {
        host_id: 'managed-postgres16', host_mode: 'managed', components: ['postgres16'],
        resolved: true, missing: [], headroom_percent: 20,
        vcpu: null, memory_gb: null, storage_gb: null, iops: null,
      },
      {
        host_id: 'host-1', host_mode: 'vm', components: ['vault', 'oracle-free'],
        resolved: true, missing: [], headroom_percent: 20,
        vcpu: 5, memory_gb: 10, storage_gb: 120, iops: 1000,
      },
    ],
    totals: { vcpu: 5, memory_gb: 10, storage_gb: 120, iops: 1000 },
    machine_count: 1,
    headroom_percent: 20,
    resolved: true,
  },
  estimate: {
    currency: 'AED', machine_count: 1, licences: [], resolved: true,
    totals: { one_time: 0, monthly: 771.9, annual: 9262.8 },
  },
  resolved: true,
  totals: { one_time: 0, monthly: 771.9, annual: 9262.8 },
  monthly_delta: null,
  cheapest: true,
}

// --- the reported move, step by step -----------------------------------------

const start = hostsOf(MANAGED_LAYOUT)
const belongsTo = belongsToOf(start)

check(
  'the starting arrangement is read off the layout',
  start.length === 3 && start[2].id === 'host-1',
  start.map((x) => `${x.id}:[${x.components}]`).join(' '),
)

// "I moved the DB to a new VM."
const afterDrag = moveToNewMachine(start, 'postgres16', 'managed-postgres16')
const emptied = afterDrag.find((x) => x.id === 'managed-postgres16')!
const landed = afterDrag.find((x) => x.components.includes('postgres16'))!

check('the database leaves the block it was on', emptied.components.length === 0)
check(
  'the database lands on a machine of its own',
  landed.id === 'host-4' && landed.host_mode === 'vm',
  landed.id,
)

// THE BUG. The emptied block used to go to the server, which refused the whole
// arrangement because of it -- "Host managed-postgres16 carries nothing" -- so
// the drag read as having done nothing at all.
const sent = proposedOf(afterDrag)
check(
  'the emptied block is NOT sent to the server',
  !sent.some((x) => x.id === 'managed-postgres16'),
  sent.map((x) => x.id).join(', '),
)
check(
  'everything that carries something IS sent',
  sent.length === 3 && sent.every((x) => x.components.length > 0),
)
check(
  'nothing the request asked for is dropped on the way',
  new Set(sent.flatMap((x) => x.components)).size === 4,
  [...new Set(sent.flatMap((x) => x.components))].join(', '),
)

// --- adding a machine no longer refuses the layout the instant it is clicked --

const withEmpty = addMachine(start)
check('an added machine is on screen', withEmpty.length === 4)
check(
  'an added machine is not proposed until something is on it',
  proposedOf(withEmpty).length === 3,
)
check(
  'the machine count means machines that would be built',
  machineCount(withEmpty) === 1,
  String(machineCount(withEmpty)),
)

// --- a managed block takes its own back, and nothing else ---------------------

check(
  'a managed block takes back the component it was created for',
  accepts(belongsTo, start[1], 'postgres16'),
)
check('a managed block refuses anything else', !accepts(belongsTo, start[1], 'vault'))
check('a machine takes anything', accepts(belongsTo, start[2], 'postgres16'))
check('nothing is accepted when nothing is being dragged', !accepts(belongsTo, start[2], undefined))

const backAgain = move(afterDrag, 'postgres16', 'host-4', 'managed-postgres16')
const asPairs = (hs: { id: string; components: string[] }[]) =>
  JSON.stringify(hs.map((x) => [x.id, [...x.components].sort()]).sort())
check(
  'putting it back restores the original arrangement',
  asPairs(proposedOf(backAgain)) === asPairs(start),
  asPairs(proposedOf(backAgain)),
)

// --- and the panel renders, in the states it can be in ------------------------

const REFUSAL =
  'The portal can provision a cluster, but it cannot deploy workloads into one: ' +
  'the Kubernetes API endpoint is private and the orchestrator has no route to it.'

const states: [string, Record<string, unknown>][] = [
  ['plain', {}],
  ['with the cluster refusal explained', { clusterUnavailable: REFUSAL }],
  ['with a comparison figure', { cheapestOffered: 700, clusterUnavailable: REFUSAL }],
]

for (const [label, extra] of states) {
  let html = ''
  try {
    html = renderToString(
      h(TopologyEditor, {
        reference: 'REQ-2026-0305',
        option: MANAGED_LAYOUT,
        environment: 'dev',
        deploymentTarget: 'oci',
        onUse: () => {},
        onClose: () => {},
        ...extra,
      } as never),
    )
  } catch (e) {
    failures += 1
    console.error(`FAIL  editor renders: ${label} -- ${(e as Error).message}`)
    continue
  }
  check(`editor renders: ${label}`, html.length > 200, `${html.length} chars`)
  check(`  it offers a way to add a machine: ${label}`, shows(html, 'Add a machine'))
  check(
    `  every block is on the page: ${label}`,
    shows(html, 'managed-postgres16') && shows(html, 'host-1'),
  )
  if (extra.clusterUnavailable) {
    check(
      `  it says why a cluster is not on offer: ${label}`,
      shows(html, 'Machines are the only thing that can be added here') &&
        shows(html, 'no route to it'),
    )
  }
}

// An unpriced layout is the state the second report arrived in: minio had no
// sizing requirement, so the whole option came back "Not priced". The editor
// must still open -- a requester whose layout cannot be priced is exactly the
// one who needs to rearrange it.
const UNPRICED: PlacementOption = {
  ...MANAGED_LAYOUT,
  resolved: false,
  sizing: {
    ...MANAGED_LAYOUT.sizing,
    resolved: false,
    hosts: [
      {
        host_id: 'host-1', host_mode: 'vm', components: ['minio', 'oracle-db'],
        resolved: false, missing: ['minio'], headroom_percent: 20,
        vcpu: null, memory_gb: null, storage_gb: null, iops: null,
      },
    ],
  },
  estimate: { ...MANAGED_LAYOUT.estimate, resolved: false },
}

try {
  const html = renderToString(
    h(TopologyEditor, {
      reference: 'REQ-2026-0305',
      option: UNPRICED,
      onUse: () => {},
      onClose: () => {},
    } as never),
  )
  check('editor opens for a layout that could not be priced', html.length > 200, `${html.length} chars`)
  check('  the unsized machine is drawn, not hidden', shows(html, 'host-1'))
} catch (e) {
  failures += 1
  console.error(`FAIL  editor opens for an unpriced layout -- ${(e as Error).message}`)
}

console.log('')
if (failures) {
  console.error(`${failures} CHECK(S) FAILED`)
  process.exit(1)
}
console.log('ALL EDITOR CHECKS PASSED')
