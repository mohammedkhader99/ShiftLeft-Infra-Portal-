import { useEffect, useRef, useState } from 'react'
import { TextInput, Button, Tag } from '@carbon/react'
import { Send } from '@carbon/icons-react'
import { chatops } from '../api'

type Turn = { role: 'you' | 'bot'; text: string; ok?: boolean }

const EXAMPLES = ['pending', 'status REQ-2026-0056', 'approve REQ-2026-0056 looks good', 'reject REQ-2026-0056', 'help']

export default function Assistant() {
  const [turns, setTurns] = useState<Turn[]>([
    { role: 'bot', text: 'Approvals bot. Try "pending", "status <REQ-ID>", "approve <REQ-ID>", or "help".' },
  ])
  const [input, setInput] = useState('')
  const [busy, setBusy] = useState(false)
  const endRef = useRef<HTMLDivElement>(null)

  useEffect(() => {
    endRef.current?.scrollIntoView({ behavior: 'smooth' })
  }, [turns])

  async function send(command: string) {
    const cmd = command.trim()
    if (!cmd || busy) return
    setInput('')
    setTurns((t) => [...t, { role: 'you', text: cmd }])
    setBusy(true)
    const { status, body } = await chatops(cmd)
    setBusy(false)
    const text = status === 200 ? body.response : body?.detail || 'Something went wrong.'
    setTurns((t) => [...t, { role: 'bot', text, ok: status === 200 ? body.ok : false }])
  }

  return (
    <div style={{ maxWidth: '46rem' }}>
      <p style={{ fontSize: '0.85rem', color: 'var(--cds-text-secondary)', margin: '0 0 1rem' }}>
        Query and act on requests from chat (F-INT-08). Runs as you — approve/reject need the approver
        role and are recorded in Jira. The same commands work from Slack.
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
              {t.role === 'you' ? 'You' : 'Bot'}
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

      <form
        onSubmit={(e) => {
          e.preventDefault()
          send(input)
        }}
        style={{ display: 'flex', gap: '0.5rem', marginTop: '0.75rem', alignItems: 'flex-end' }}
      >
        <div style={{ flex: 1 }}>
          <TextInput
            id="chatops-input"
            labelText=""
            placeholder="Type a command…"
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
