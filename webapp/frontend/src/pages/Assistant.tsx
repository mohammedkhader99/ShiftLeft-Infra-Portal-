import { useEffect, useRef, useState } from 'react'
import { TextInput, Button, Tag } from '@carbon/react'
import { Send, Checkmark, Close } from '@carbon/icons-react'
import { aiChat, chatops } from '../api'

type Turn = { role: 'you' | 'bot'; text: string; ok?: boolean }

const EXAMPLES = [
  "what's pending?",
  'show me REQ-2026-0056',
  'approve REQ-2026-0056, looks good',
  'reject REQ-2026-0056',
  'help',
]

export default function Assistant() {
  const [turns, setTurns] = useState<Turn[]>([
    {
      role: 'bot',
      text:
        'Approvals assistant. Ask in plain English — e.g. "what\'s waiting on me?", ' +
        '"how\'s REQ-2026-0056 doing?", or "approve REQ-2026-0056". ' +
        'I\'ll always ask you to confirm before recording any approval in Jira.',
    },
  ])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  // A pending approve/reject the AI has proposed but not run — awaiting confirm.
  const [pending, setPending] = useState<{ command: string } | null>(null)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns, pending])

  function addBot(text: string, ok?: boolean) {
    setTurns((t) => [...t, { role: 'bot', text, ok }])
  }

  // Send a plain-English message to the AI layer. Read-only intents come back
  // answered; approve/reject come back as a proposal to confirm.
  async function send(message: string) {
    const msg = message.trim()
    if (!msg || busy) return
    setInput('')
    setPending(null)
    setTurns((t) => [...t, { role: 'you', text: msg }])
    setBusy(true)
    const { status, body } = await aiChat(msg)
    setBusy(false)
    if (status !== 200) {
      addBot(body?.detail || 'Something went wrong.', false)
      return
    }
    addBot(body.response, body.ok === false ? false : undefined)
    if (body.needs_confirmation && body.command) setPending({ command: body.command })
  }

  // Run the proposed approve/reject through the authority-preserving path — this
  // is the only thing that records a decision in Jira.
  async function confirm() {
    if (!pending || busy) return
    const command = pending.command
    setPending(null)
    setBusy(true)
    const { status, body } = await chatops(command)
    setBusy(false)
    const text = status === 200 ? body.response : body?.detail || 'Something went wrong.'
    addBot(text, status === 200 ? body.ok : false)
  }

  function cancel() {
    setPending(null)
    addBot('Okay — cancelled. Nothing was recorded.')
  }

  return (
    <div style={{ maxWidth: '46rem' }}>
      <p style={{ fontSize: '0.85rem', color: 'var(--cds-text-secondary)', margin: '0 0 1rem' }}>
        Ask about and act on requests in plain English (F-INT-08). Runs as you — approve/reject need
        the approver role, always require your confirmation, and are recorded in Jira. The assistant
        interprets your words; it never decides on its own.
      </p>

      <div
        style={{
          border: '1px solid var(--cds-border-subtle)',
          background: 'var(--cds-layer)',
          padding: '1rem',
          minHeight: '18rem',
          maxHeight: '28rem',
          overflowY: 'auto',
          display: 'flex',
          flexDirection: 'column',
          gap: '0.75rem',
        }}
      >
        {turns.map((t, i) => (
          <div key={i} style={{ display: 'flex', flexDirection: 'column', alignItems: t.role === 'you' ? 'flex-end' : 'flex-start' }}>
            <span style={{ fontSize: '0.7rem', color: 'var(--cds-text-secondary)', marginBottom: '0.15rem' }}>
              {t.role === 'you' ? 'You' : 'Assistant'}
            </span>
            <pre
              style={{
                margin: 0,
                whiteSpace: 'pre-wrap',
                fontFamily: t.role === 'you' ? 'inherit' : 'var(--cds-code-01-font-family, monospace)',
                fontSize: '0.85rem',
                lineHeight: 1.5,
                padding: '0.5rem 0.75rem',
                borderRadius: 0,
                maxWidth: '90%',
                background: t.role === 'you' ? 'var(--cds-layer-selected)' : 'var(--cds-layer-accent)',
                color: t.ok === false ? 'var(--cds-text-error)' : 'var(--cds-text-primary)',
              }}
            >
              {t.text}
            </pre>
          </div>
        ))}
        <div ref={endRef} />
      </div>

      {pending && (
        <div
          style={{
            marginTop: '0.75rem',
            display: 'flex',
            gap: '0.5rem',
            alignItems: 'center',
            padding: '0.6rem 0.75rem',
            border: '1px solid var(--cds-support-warning)',
            background: 'var(--cds-layer)',
          }}
        >
          <span style={{ flex: 1, fontSize: '0.85rem' }}>
            Confirm to record this in Jira:{' '}
            <code style={{ fontWeight: 600 }}>{pending.command}</code>
          </span>
          <Button size="sm" kind="ghost" renderIcon={Close} onClick={cancel} disabled={busy}>
            Cancel
          </Button>
          <Button size="sm" renderIcon={Checkmark} onClick={confirm} disabled={busy}>
            {busy ? 'Recording…' : 'Confirm'}
          </Button>
        </div>
      )}

      <form
        onSubmit={(e) => {
          e.preventDefault()
          send(input)
        }}
        style={{ display: 'flex', gap: '0.5rem', marginTop: '0.75rem', alignItems: 'flex-end' }}
      >
        <div style={{ flex: 1 }}>
          <TextInput
            id="assistant-input"
            labelText=""
            placeholder="Ask in plain English…"
            value={input}
            onChange={(e) => setInput(e.target.value)}
          />
        </div>
        <Button type="submit" renderIcon={Send} disabled={busy || !input.trim()}>
          {busy ? 'Sending…' : 'Send'}
        </Button>
      </form>

      <div style={{ marginTop: '0.75rem', display: 'flex', flexWrap: 'wrap', gap: '0.4rem', alignItems: 'center' }}>
        <span style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)' }}>Try:</span>
        {EXAMPLES.map((ex) => (
          <Tag key={ex} type="blue" size="sm" onClick={() => send(ex)} style={{ cursor: 'pointer' }}>
            {ex}
          </Tag>
        ))}
      </div>
    </div>
  )
}
