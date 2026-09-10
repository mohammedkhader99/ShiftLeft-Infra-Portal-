/**
 * Does the placement step actually RUN? (P.11)
 *
 * `tsc --noEmit` in the image build proves the types line up. It cannot prove the
 * component executes: a map over something undefined, or a property read on a
 * field the server left null, only throws when React renders it — and this
 * component sits inside the request form, so a throw here takes the whole form
 * down rather than just the new step.
 *
 * There is no test runner in this frontend (no vitest, no jest, and adding one is
 * a decision nobody has made). So this is a plain script: it server-renders the
 * component in every state it has, against REAL answers captured from
 * /api/placement/options, and fails loudly. No mocked shapes — a fixture invented
 * by hand would drift from what the API sends, and drift is exactly what this is
 * meant to catch.
 *
 * HOW TO RUN IT (needs Docker; there is no node on the host):
 *
 *   docker build --target build -t placement-render-check ./webapp
 *   docker run --rm -v "${PWD}/webapp/frontend/tests:/tests:ro" -w /frontend \
 *     placement-render-check sh -c \
 *     'cp /tests/placement-step.render.tsx . && \
 *      ./node_modules/.bin/esbuild ./placement-step.render.tsx --bundle \
 *        --platform=node --format=cjs --outfile=/tmp/r.cjs --jsx=automatic \
 *        --loader:.json=json && node /tmp/r.cjs'
 *
 * On Windows run that from PowerShell, not Git Bash: Git Bash rewrites the
 * container paths (`-w /frontend` becomes `C:/Program Files/Git/frontend`).
 *
 * A pass prints ALL RENDER CHECKS PASSED and exits 0.
 *
 * REFRESHING THE FIXTURES when the API's response shape changes: capture a real
 * answer rather than editing these by hand —
 *   POST /api/placement/options {"reference": "<a draft with components>"}
 * and save its `options` array over the file.
 */
import { renderToString } from 'react-dom/server'
import { createElement as h } from 'react'
import PlacementStep from './src/components/PlacementStep'
import TopologyDiagram from './src/components/TopologyDiagram'

import prodOptions from './tests/fixtures/options-prod.json'
import clusterOptions from './tests/fixtures/options-cluster.json'

const cases: [string, any[]][] = [
  ['prod: two databases, consolidation refused', prodOptions as any[]],
  ['dev: a Kubernetes selection', clusterOptions as any[]],
]

let failures = 0

function render(props: Record<string, unknown>): string {
  return renderToString(
    h(PlacementStep as any, {
      options: null,
      chosen: null,
      clusterId: null,
      onFetch: () => {},
      onChoose: () => {},
      onClusterChange: () => {},
      busy: false,
      error: null,
      ready: true,
      notReadyReason: 'Add a component first.',
      ...props,
    }),
  )
}

// Every state the step has. `null` options and `[]` options are different states
// and are checked separately, because they say different things to a requester:
// one is "not asked yet", the other is the server's answer.
for (const [label, options] of cases) {
  const states: [string, Record<string, unknown>][] = [
    ['not asked yet', { options: null }],
    ['options shown', { options }],
    ['one chosen', { options, chosen: options.find((o) => o.eligible)?.key ?? null }],
    ['no layouts at all', { options: [] }],
    ['not ready', { ready: false }],
    ['the request failed', { error: 'The platform could not answer.' }],
    ['working on it', { busy: true }],
    // A RESUMED DRAFT (P.11a): no options computed, but a layout already
    // recorded against the request. This state exists to stop the step coming
    // back empty while `chosen` quietly holds a decision underneath it — so a
    // render that produced nothing here would be the exact defect, passing.
    ['resumed, layout recorded', {
      options: null,
      chosen: 'consolidated',
      recorded: {
        version: 2,
        option_key: 'consolidated',
        cluster_id: null,
        topology: { environment: 'UAT', deployment_target: 'oci',
                    hosts: [{ id: 'host-1', host_mode: 'vm', components: ['postgres16', 'nodejs20'] }] },
        sizing: { machine_count: 1, resolved: true },
        estimate: { currency: 'AED', resolved: true,
                    totals: { one_time: 0, monthly: 412.5, annual: 4950 } },
        created_at: '2026-09-09T06:00:00',
      },
    }],
    // The same, unpriced. `resolved: false` means the server declined to price
    // it, and a layout showing 0.00 AED because nobody could price it is the
    // figure-that-disagrees-with-itself this form has already been bitten by.
    ['resumed, layout recorded but unpriced', {
      options: null,
      chosen: 'separated',
      recorded: {
        version: 1,
        option_key: 'separated',
        cluster_id: null,
        topology: { environment: 'Development', deployment_target: 'oci', hosts: [] },
        sizing: { machine_count: 0, resolved: false },
        estimate: { currency: 'AED', resolved: false },
        created_at: null,
      },
    }],
  ]
  for (const [what, props] of states) {
    try {
      const html = render(props)
      // Rendering nothing at all is a silent failure, not a pass.
      if (!html || html.length < 40) {
        console.log(`FAIL  ${label} / ${what}: rendered almost nothing (${html.length} chars)`)
        failures++
      } else {
        console.log(`ok    ${label} / ${what}  (${html.length} chars)`)
      }
    } catch (e: any) {
      console.log(`FAIL  ${label} / ${what}: ${e?.message || e}`)
      failures++
    }
  }
}

// THE PROPERTY THIS STEP EXISTS FOR. An option that cannot be chosen must be on
// the page WITH the sentence saying why. Worth more than the render itself:
// silently dropping a refused option is the defect P.11 was written to prevent,
// and it would still render perfectly well.
for (const [label, options] of cases) {
  const html = render({ options })
  const refused = options.filter((o) => !o.eligible)
  for (const option of refused) {
    if (!html.includes(option.title)) {
      console.log(`FAIL  ${label}: refused option not rendered at all — ${option.title}`)
      failures++
    }
    for (const reason of option.reasons) {
      // A prefix, because the markup escapes some punctuation.
      const head = reason.slice(0, 40)
      if (!html.includes(head)) {
        console.log(`FAIL  ${label}: refusal reason missing from the markup — ${head}…`)
        failures++
      }
    }
  }
  console.log(`ok    ${label}: ${refused.length} refused option(s), every reason on the page`)

  // An unsizeable host must never render as a host of zero. A price of 0.00 has
  // reached an approver in this portal before; a shape of zero is the same lie in
  // a different column.
  if (/0\s*vCPU/.test(html)) {
    console.log(`FAIL  ${label}: an unsized host rendered as "0 vCPU"`)
    failures++
  }

  // And an unpriceable option must say so rather than showing a total.
  if (options.some((o) => !o.resolved) && !html.includes('Not priced')) {
    console.log(`FAIL  ${label}: an unpriced option did not say "Not priced"`)
    failures++
  }
}

// --- the topology diagram (D.2) ---------------------------------------------
//
// The diagram only appears once someone presses "View as diagram", so rendering
// the step never reaches it — it has to be rendered directly. Worth the extra
// few lines: a PICTURE makes it easier to imply something untrue, not harder,
// and the properties below are the ones a drawing could quietly break.
for (const [label, options] of cases) {
  for (const option of options as any[]) {
    let html = ''
    try {
      html = renderToString(
        h(TopologyDiagram as any, {
          option,
          environment: 'Development',
          deploymentTarget: 'oci',
        }),
      )
    } catch (e: any) {
      console.log(`FAIL  diagram ${label} / ${option.key}: ${e?.message || e}`)
      failures++
      continue
    }

    if (html.length < 40) {
      console.log(`FAIL  diagram ${label} / ${option.key}: rendered almost nothing`)
      failures++
      continue
    }

    // A host with no determined size must never be drawn as a box of zeros.
    if (/0\s*vCPU/.test(html)) {
      console.log(`FAIL  diagram ${label} / ${option.key}: an unsized host drawn as zero`)
      failures++
    }
    const unsized = (option.sizing?.hosts ?? []).filter(
      (host: any) => host.host_mode !== 'managed' && !host.resolved,
    )
    if (unsized.length && !html.includes('Size not determined')) {
      console.log(`FAIL  diagram ${label} / ${option.key}: an unsized host was not marked`)
      failures++
    }

    // A managed service is drawn as something the cloud runs. Whether somebody
    // patches it at 2am is the difference the diagram most needs to show.
    const managed = (option.sizing?.hosts ?? []).filter(
      (host: any) => host.host_mode === 'managed',
    )
    if (managed.length && !html.includes('No machine is provisioned')) {
      console.log(`FAIL  diagram ${label} / ${option.key}: a managed host drawn as a machine`)
      failures++
    }

    // Every component on a host is drawn. A block silently missing from the
    // picture is an environment silently missing a component.
    for (const host of option.sizing?.hosts ?? []) {
      for (const code of host.components ?? []) {
        if (!html.includes(code)) {
          console.log(`FAIL  diagram ${label} / ${option.key}: ${code} is not on the diagram`)
          failures++
        }
      }
    }

    // A refused layout is still drawn, with the reason under it — seeing the
    // arrangement you cannot have is how somebody works out what to change.
    if (!option.eligible) {
      for (const reason of option.reasons ?? []) {
        if (!html.includes(reason.slice(0, 40))) {
          console.log(`FAIL  diagram ${label} / ${option.key}: refusal missing from the drawing`)
          failures++
        }
      }
    }

    // And it never invents a figure the option does not carry.
    if (!option.resolved && !html.includes('Not priced')) {
      console.log(`FAIL  diagram ${label} / ${option.key}: unpriced layout showed a total`)
      failures++
    }
  }
  console.log(`ok    diagram ${label}: ${(options as any[]).length} layout(s) drawn`)
}

console.log(failures === 0 ? '\nALL RENDER CHECKS PASSED' : `\n${failures} FAILURE(S)`)
process.exit(failures === 0 ? 0 : 1)
