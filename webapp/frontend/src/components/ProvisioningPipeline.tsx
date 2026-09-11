/**
 * Where a request has got to (U.3).
 *
 * Asked for as a picture: Agent, Blueprint, Terraform, Validate, Plan,
 * Security/Policy, Provision, Verify, Ready. Every one of those already happens
 * — what was missing was anywhere to see it, so a request sitting at
 * `in-progress` for eleven minutes told its owner nothing about whether the
 * agent was building a recipe, whether Terraform had planned, or whether it was
 * stuck.
 *
 * EVERY WORD HERE CAME FROM THE SERVER. The stages, their order, their state,
 * their times and the sentence explaining a stall are all computed by
 * /api/requests/{ref}/progress from the request's status and its append-only
 * audit trail. This file decides nothing: it cannot know whether a plan ran, and
 * a pipeline assembled in the browser from a status string would be a second
 * opinion about history when the audit log is the first one.
 *
 * A MISSING TIME IS SHOWN AS MISSING. Where the trail does not say when a step
 * happened, the server sends no time and this prints none. A progress view is
 * believed, and one that fills in a plausible timestamp is worse than one that
 * admits the gap.
 */

import { useEffect, useState } from 'react'
import { Button } from '@carbon/react'
import { getRequestProgress, type ProgressStage } from '../api'

const DOT: Record<string, { mark: string; colour: string; label: string }> = {
  done: { mark: '✓', colour: 'var(--cds-support-success)', label: 'done' },
  current: { mark: '●', colour: 'var(--cds-support-info)', label: 'in progress' },
  failed: { mark: '✕', colour: 'var(--cds-support-error)', label: 'stopped here' },
  skipped: { mark: '–', colour: 'var(--cds-text-secondary)', label: 'not needed' },
  pending: { mark: '○', colour: 'var(--cds-border-strong)', label: 'not yet' },
}

function when(at: string | null): string {
  if (!at) return ''
  const d = new Date(at)
  return Number.isNaN(d.getTime()) ? '' : d.toLocaleString()
}

/**
 * The stages, drawn. Exported so it can be rendered against real server output
 * without a fetch — the lesson from TopologyEditor, where the behaviour lived
 * inside a component and "does it work?" could only be answered by opening the
 * page.
 */
export function PipelineStages({ stages }: { stages: ProgressStage[] }) {
  return (
    <ol
      aria-label="Provisioning progress"
      style={{ listStyle: 'none', margin: '0.5rem 0 0', padding: 0 }}
    >
      {stages.map((s) => {
        const dot = DOT[s.state] || DOT.pending
        return (
          <li
            key={s.key}
            style={{
              display: 'flex',
              gap: '0.6rem',
              alignItems: 'baseline',
              padding: '0.3rem 0',
              borderTop: '1px solid var(--cds-border-subtle)',
            }}
          >
            <span aria-hidden style={{ color: dot.colour, width: '1rem' }}>
              {dot.mark}
            </span>
            <span style={{ minWidth: '11rem' }}>
              <strong
                style={{
                  fontSize: '0.8rem',
                  color: s.state === 'pending' ? 'var(--cds-text-secondary)' : undefined,
                }}
              >
                {s.title}
              </strong>
              {/* The state in words as well as a mark, because a coloured glyph
                  is not a fact a screen reader can read out. */}
              <span style={{ fontSize: '0.7rem', color: 'var(--cds-text-secondary)' }}>
                {' '}— {dot.label}
              </span>
            </span>
            <span style={{ flex: 1, fontSize: '0.75rem', color: 'var(--cds-text-secondary)' }}>
              {s.blurb}
              {s.detail && (
                <span style={{ display: 'block', color: 'var(--cds-text-error)' }}>
                  {s.detail}
                </span>
              )}
            </span>
            {/* Blank where nothing recorded a time. Not an em dash, which reads
                as a value, and certainly not a guess. */}
            <span
              style={{
                fontSize: '0.72rem',
                color: 'var(--cds-text-secondary)',
                whiteSpace: 'nowrap',
              }}
            >
              {when(s.at)}
            </span>
          </li>
        )
      })}
    </ol>
  )
}

export default function ProvisioningPipeline({
  reference,
  // Re-read when the request's status changes, so a pipeline left open on
  // screen follows the request rather than showing where it was when opened.
  status,
}: {
  reference: string
  status?: string | null
}) {
  const [stages, setStages] = useState<ProgressStage[] | null>(null)
  const [failed, setFailed] = useState<string | null>(null)
  const [open, setOpen] = useState(false)

  useEffect(() => {
    if (!open) return
    let cancelled = false
    getRequestProgress(reference)
      .then(({ status: code, body }) => {
        if (cancelled) return
        if (code !== 200) {
          setFailed(body?.detail || `The platform could not answer (HTTP ${code}).`)
          return
        }
        setFailed(null)
        setStages(body.stages)
      })
      .catch(() => !cancelled && setFailed('The platform could not be reached.'))
    return () => {
      cancelled = true
    }
  }, [reference, status, open])

  return (
    <div style={{ marginTop: '0.5rem' }}>
      <Button kind="ghost" size="sm" aria-expanded={open} onClick={() => setOpen((v) => !v)}>
        {open ? 'Hide progress' : 'Where has this got to?'}
      </Button>

      {open && failed && (
        <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-error)', margin: '0.4rem 0 0' }}>
          {failed}
        </p>
      )}

      {open && !failed && !stages && (
        <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', margin: '0.4rem 0 0' }}>
          Reading the trail…
        </p>
      )}

      {open && stages && <PipelineStages stages={stages} />}

    </div>
  )
}
