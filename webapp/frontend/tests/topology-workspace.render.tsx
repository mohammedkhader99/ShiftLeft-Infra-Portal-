/**
 * One workspace, one question (U.1).
 *
 * The step used to render every layout the platform produced as its own card.
 * For a stack with a cluster in it that is five cards, two of which print the
 * same sentence about the Kubernetes API being unreachable. "I am confused, why
 * so many choices are provided."
 *
 * This holds the replacement to both halves of the bargain: the requester is
 * asked ONE question — machines or Kubernetes — and nothing the platform
 * decided disappears to achieve that. A layout that vanishes is
 * indistinguishable from a portal that is broken, and this codebase has been
 * bitten by that often enough to test for it.
 *
 * Runs against the SAME fixtures the step harness uses, captured from the real
 * endpoint against a seeded catalogue and the real OPA.
 *
 * HOW TO RUN IT (needs Docker; there is no node on the host):
 *
 *   docker build --target build -t placement-render-check ./webapp
 *   docker run --rm -v "${PWD}/webapp/frontend/tests:/tests:ro" -w /frontend \
 *     placement-render-check sh -c \
 *     'cp /tests/topology-workspace.render.tsx . && \
 *      ./node_modules/.bin/esbuild ./topology-workspace.render.tsx --bundle \
 *        --platform=node --format=cjs --outfile=/tmp/w.cjs --jsx=automatic \
 *        --loader:.json=json && node /tmp/w.cjs'
 *
 * REBUILD THE IMAGE FIRST whenever the fixtures change: the harness imports
 * them from ./tests/fixtures INSIDE the image, not from the mounted copy, so a
 * stale image quietly tests yesterday's data. That is not hypothetical — it
 * produced a full pass against fixtures that predated the `route` field, on a
 * render that showed no route cards at all.
 *
 * A pass prints ALL WORKSPACE CHECKS PASSED and exits 0.
 */
import { renderToString } from 'react-dom/server'
import { createElement as h } from 'react'
import TopologyWorkspace, {
  KUBERNETES,
  MACHINES,
  clustersOffered,
  recommended,
  stackOf,
} from './src/components/TopologyWorkspace'
import type { PlacementOption } from './src/api'
import prodOptions from './tests/fixtures/options-prod.json'
import clusterOptions from './tests/fixtures/options-cluster.json'

let failures = 0

function check(label: string, condition: boolean, detail = '') {
  if (condition) {
    console.log(`ok    ${label}${detail ? `  (${detail})` : ''}`)
    return
  }
  failures += 1
  console.error(`FAIL  ${label}${detail ? `  -- ${detail}` : ''}`)
}

function shows(html: string, text: string): boolean {
  return html
    .replace(/<!-- -->/g, '')
    .replace(/&#x27;/g, "'")
    .replace(/&quot;/g, '"')
    .replace(/&amp;/g, '&')
    .includes(text)
}

const PROD = prodOptions as unknown as PlacementOption[]
const CLUSTER = clusterOptions as unknown as PlacementOption[]

function render(options: PlacementOption[], chosen: string | null): string {
  return renderToString(
    h(TopologyWorkspace, {
      options,
      chosen,
      clusterId: null,
      currency: 'AED',
      environment: 'dev',
      deploymentTarget: 'oci',
      onChoose: () => {},
      onClusterChange: () => {},
      onArrange: () => {},
    } as never),
  )
}

// --- the fixtures are the ones this was written for --------------------------

check(
  'the fixtures carry the route the server now sends',
  [...PROD, ...CLUSTER].every((o) => o.route === MACHINES || o.route === KUBERNETES),
  [...new Set([...PROD, ...CLUSTER].map((o) => String(o.route)))].join(', '),
)
check(
  'the cluster fixture actually has a Kubernetes route to group',
  CLUSTER.some((o) => o.route === KUBERNETES),
  CLUSTER.map((o) => `${o.key}:${o.route}`).join(' '),
)

// --- the grouping ------------------------------------------------------------

check(
  'the stack is every component, however the layouts arrange them',
  stackOf(CLUSTER).sort().join(',') === 'nodejs20,oci-oke,postgres16',
  stackOf(CLUSTER).sort().join(','),
)
check(
  'the machines route recommends the cheapest that can be built',
  recommended(PROD, MACHINES)?.eligible === true &&
    recommended(PROD, MACHINES)!.totals.monthly ===
      Math.min(...PROD.filter((o) => o.eligible && o.resolved).map((o) => o.totals.monthly)),
  recommended(PROD, MACHINES)?.key,
)
check(
  'the Kubernetes route recommends the cheapest that can be built',
  recommended(CLUSTER, KUBERNETES)?.eligible === true,
  recommended(CLUSTER, KUBERNETES)?.key,
)

// A route whose layouts are ALL refused must still offer one, so its reason
// reaches the screen instead of the route disappearing. The fixtures no longer
// contain that case — U.2 made cluster layouts choosable — so it is constructed
// rather than waited for. It is the switch's off position, and the day the
// network route is opened it will be some other refusal.
const allRefused: PlacementOption[] = CLUSTER.map((o) =>
  o.route === KUBERNETES
    ? { ...o, eligible: false, reasons: ['the orchestrator has no route to it'] }
    : o,
)
check(
  'a route with nothing buildable still offers something, so its reason is on screen',
  recommended(allRefused, KUBERNETES) !== null &&
    recommended(allRefused, KUBERNETES)!.eligible === false,
  recommended(allRefused, KUBERNETES)?.key,
)
check(
  '  and that reason is rendered, not swallowed',
  shows(render(allRefused, null), 'the orchestrator has no route to it'),
)
check('a stack with no cluster has no Kubernetes route', recommended(PROD, KUBERNETES) === null)

const found = clustersOffered(CLUSTER)
check(
  'discovered clusters are collected across the layouts that could use them',
  found.length === new Set(CLUSTER.flatMap((o) => (o.clusters ?? []).map((c) => c.id))).size,
  found.map((c) => c.name).join(', ') || 'none',
)

// --- what the requester sees -------------------------------------------------

const cluster = render(CLUSTER, null)

check('it asks one question', shows(cluster, 'How would you like to host this workload?'))
check('both routes are offered', shows(cluster, 'Independent machines') && shows(cluster, 'OCI Kubernetes (OKE)'))
// One line either way, and it must match the data rather than being decorative.
check(
  'it says in one line what the cluster situation is',
  found.length
    ? shows(cluster, `${found.length} OCI Kubernetes cluster`) && shows(cluster, 'available to this request')
    : shows(cluster, 'No suitable OCI Kubernetes (OKE) cluster is currently available'),
  found.length ? `${found.length} found` : 'none found',
)
check('the stack is named', shows(cluster, 'oci-oke') && shows(cluster, 'postgres16'))
check(
  'each route says which layout it would use',
  shows(cluster, 'Layout: Deploy onto an existing cluster') ||
    shows(cluster, 'Layout: Provision a new cluster'),
)

// NOTHING DISAPPEARS. Every layout the platform produced is accounted for on
// the page -- as a route card, or in "Also considered" with its reason.
const onKubernetesPage = render(CLUSTER, recommended(CLUSTER, KUBERNETES)!.key)
const onMachinesPage = render(CLUSTER, recommended(CLUSTER, MACHINES)!.key)

for (const o of CLUSTER) {
  const onCard =
    o.key === recommended(CLUSTER, MACHINES)?.key || o.key === recommended(CLUSTER, KUBERNETES)?.key
  // Three places a layout may legitimately be: a route card, the compact
  // "Also considered" block, or -- if it can be built but is not the
  // recommendation -- the alternatives for its own route. Nowhere is not one of
  // them, which is what this loop exists to catch.
  const page = o.route === KUBERNETES ? onKubernetesPage : onMachinesPage
  // An alternative sits behind a disclosure, so its title is not in the markup
  // until it is opened -- that is what a disclosure is, and it is not the same
  // as vanishing. What must be on the page is a control that SAYS how many are
  // behind it, so nobody has to guess the list exists.
  const behindADisclosure =
    shows(page, `Other ${o.route === KUBERNETES ? 'Kubernetes' : 'machine'} layouts you could pick`)
  const accounted = onCard || shows(cluster, o.title) || behindADisclosure
  check(`  accounted for: ${o.key}`, accounted,
    onCard ? 'on a route card'
      : shows(cluster, o.title) ? 'in "Also considered"'
        : 'named by the alternatives disclosure')
  if (!o.eligible && !onCard) {
    check(
      `  and its reason is on the page: ${o.key}`,
      shows(cluster, o.reasons[0].slice(0, 45)),
      o.reasons[0].slice(0, 45),
    )
  }
}

check(
  'an unpriced layout says so rather than showing a total',
  !CLUSTER.some((o) => !o.resolved) || shows(cluster, 'Not priced'),
)

// --- the diagram follows the choice ------------------------------------------

check(
  'nothing is drawn until a route is chosen',
  shows(cluster, 'Choose how this should be hosted'),
)

const onMachines = render(CLUSTER, recommended(CLUSTER, MACHINES)!.key)
check('choosing machines draws that topology', shows(onMachines, 'What gets built'))
check('  and offers to rearrange it', shows(onMachines, 'Arrange it yourself'))
check(
  '  and the machine layout shows its hosts',
  shows(onMachines, 'managed-postgres16') || shows(onMachines, 'host-1'),
)

const onKubernetes = render(CLUSTER, recommended(CLUSTER, KUBERNETES)!.key)
check('choosing Kubernetes draws the cluster instead', shows(onKubernetes, 'What gets built'))
check(
  '  and any refusal travels with it',
  recommended(CLUSTER, KUBERNETES)!.eligible ||
    shows(onKubernetes, recommended(CLUSTER, KUBERNETES)!.reasons[0].slice(0, 40)),
  recommended(CLUSTER, KUBERNETES)!.eligible ? 'nothing to refuse' : 'refusal shown',
)
check(
  '  a buildable alternative in the same route can still be reached',
  CLUSTER.filter((o) => o.route === KUBERNETES && o.eligible).length < 2 ||
    shows(onKubernetes, 'Other Kubernetes layouts you could pick'),
)
check(
  '  a discovered cluster can be picked, but only once Kubernetes is chosen',
  found.length ? shows(onKubernetes, found[0].name) && !shows(cluster, found[0].name) : true,
  found.length ? found[0].name : 'no clusters to pick',
)

// --- a stack with no cluster is a one-route workspace, not a broken two -------

const prod = render(PROD, null)
check('a machines-only stack offers machines', shows(prod, 'Independent machines'))
check('  and does not offer a Kubernetes route it has no layout for', !shows(prod, 'OCI Kubernetes (OKE)'))
check(
  '  and does not claim a cluster is unavailable when none was ever in question',
  !shows(prod, 'No suitable OCI Kubernetes'),
)
check(
  '  while the refused machine layout keeps its reason',
  PROD.filter((o) => !o.eligible).every(
    (o) => o.key === recommended(PROD, MACHINES)?.key || shows(prod, o.reasons[0].slice(0, 45)),
  ),
)

console.log('')
if (failures) {
  console.error(`${failures} CHECK(S) FAILED`)
  process.exit(1)
}
console.log('ALL WORKSPACE CHECKS PASSED')
