/**
 * Does the pipeline draw what the server said? (U.3)
 *
 * The risk in a progress view is not that it crashes — it is that it is
 * BELIEVED. A view that quietly fills in a plausible time, or shows a green
 * tick for a step nothing recorded, tells somebody their infrastructure was
 * verified when nothing verified it. This portal has already shipped a request
 * marked `provisioned` that had built an empty bucket.
 *
 * So these render real server output and check that what is on the page is what
 * came back: no invented times, the failure reason where the failure is, and
 * every state readable as words rather than only as a coloured glyph.
 *
 * HOW TO RUN IT (needs Docker; there is no node on the host):
 *
 *   docker build --target build -t placement-render-check ./webapp
 *   docker run --rm -v "${PWD}/webapp/frontend/tests:/tests:ro" -w /frontend \
 *     placement-render-check sh -c \
 *     'cp /tests/provisioning-pipeline.render.tsx . && \
 *      ./node_modules/.bin/esbuild ./provisioning-pipeline.render.tsx --bundle \
 *        --platform=node --format=cjs --outfile=/tmp/p.cjs --jsx=automatic \
 *        --loader:.json=json && node /tmp/p.cjs'
 *
 * REBUILD THE IMAGE FIRST when the fixture changes — it is imported from inside
 * the image, not from the mounted copy.
 *
 * Refresh the fixture with webapp/frontend/tests/refresh_progress_fixture.py
 * rather than editing it: it is a capture, not a mock.
 *
 * A pass prints ALL PIPELINE CHECKS PASSED and exits 0.
 */
import { renderToString } from 'react-dom/server'
import { createElement as h } from 'react'
import ProvisioningPipeline, { PipelineStages } from './src/components/ProvisioningPipeline'
import type { ProgressStage } from './src/api'
import stalled from './tests/fixtures/progress-stalled.json'

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

const STAGES = stalled as unknown as ProgressStage[]
const html = renderToString(h(PipelineStages, { stages: STAGES }))

// --- the fixture is the one this was written for ------------------------------

check('the fixture is a real capture of all nine stages', STAGES.length === 9,
  STAGES.map((s) => s.state).join(','))
check('  it contains a step that is done with no recorded time',
  STAGES.some((s) => s.state === 'done' && s.at === null),
  STAGES.filter((s) => s.state === 'done' && s.at === null).map((s) => s.title).join(', '))
check('  and a step that failed', STAGES.some((s) => s.state === 'failed'))

// --- what reaches the page ----------------------------------------------------

check('every stage the server sent is drawn',
  STAGES.every((s) => shows(html, s.title)),
  STAGES.filter((s) => !shows(html, s.title)).map((s) => s.title).join(', ') || 'all')

check('each stage says what it means',
  STAGES.every((s) => shows(html, s.blurb.slice(0, 30))))

// THE ONE THAT MATTERS. A step the trail never timed must print no time.
const untimed = STAGES.filter((s) => s.at === null)
check('a step with no recorded time prints no time', untimed.length > 0)
for (const s of untimed) {
  // The row is there; what must not be there is a date beside it. Checked by
  // counting rendered dates against steps that actually carry one.
  const dates = (html.match(/\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}/g) || []).length
  const timed = STAGES.filter((x) => x.at !== null).length
  check(`  no invented time for: ${s.title}`, dates <= timed, `${dates} dates, ${timed} timed steps`)
  break
}

check('the reason it stopped is beside the step that stopped',
  STAGES.filter((s) => s.detail).every((s) => shows(html, s.detail!.slice(0, 40))),
  STAGES.find((s) => s.detail)?.title || 'no detail in this capture')

// --- states are readable, not only coloured -----------------------------------

check('a done step says so in words', shows(html, '— done'))
check('a failed step says so in words', shows(html, '— stopped here'))
check('a step that has not happened says so in words', shows(html, '— not yet'))
check('the list announces itself', shows(html, 'aria-label="Provisioning progress"'))

// --- the panel it lives in ----------------------------------------------------

const panel = renderToString(
  h(ProvisioningPipeline, { reference: 'REQ-2026-0410', status: 'apply-failed' } as never),
)
check('the panel offers to show progress', shows(panel, 'Where has this got to?'))
check('  and draws nothing until asked', !shows(panel, 'Provisioning progress'))

// --- a request that has done nothing yet --------------------------------------

const fresh: ProgressStage[] = STAGES.map((s) => ({
  ...s, state: 'pending', at: null, detail: null,
}))
// Normalised the same way `shows` does: React's server renderer puts <!-- -->
// between adjacent text and an expression, so the raw markup reads
// "— <!-- -->not yet" and a plain regex misses every one of them.
const freshHtml = renderToString(h(PipelineStages, { stages: fresh }))
  .replace(/<!-- -->/g, '')
check('a request at the start draws every step as not yet',
  (freshHtml.match(/— not yet/g) || []).length === 9,
  String((freshHtml.match(/— not yet/g) || []).length))
check('  and no times at all',
  !/\d{1,4}[/\-.]\d{1,2}[/\-.]\d{1,4}/.test(freshHtml))

console.log('')
if (failures) {
  console.error(`${failures} CHECK(S) FAILED`)
  process.exit(1)
}
console.log('ALL PIPELINE CHECKS PASSED')
