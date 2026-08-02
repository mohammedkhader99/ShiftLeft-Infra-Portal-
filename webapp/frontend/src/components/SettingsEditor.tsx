import { useEffect, useState } from 'react'
import { Tile, Tag, Toggle, Select, SelectItem, TextInput, Button, InlineNotification } from '@carbon/react'
import { Reset } from '@carbon/icons-react'
import {
  getAdminSettings,
  setAdminSetting,
  resetAdminSetting,
  type AdminSetting,
  type AdminSettings,
} from '../api'

// Where the current value comes from — DB override, .env, or built-in default.
function SourceBadge({ source }: { source: string }) {
  const type = source === 'database' ? 'blue' : source === 'env' ? 'teal' : 'gray'
  const label = source === 'database' ? 'database' : source === 'env' ? '.env' : 'default'
  return <Tag size="sm" type={type} title={`current value comes from ${label}`}>{label}</Tag>
}

const groupTitle = {
  fontSize: '0.72rem',
  fontWeight: 600 as const,
  textTransform: 'uppercase' as const,
  letterSpacing: '0.04em',
  color: 'var(--cds-text-secondary)',
  margin: '0.75rem 0 0.35rem',
}

export default function SettingsEditor() {
  const [data, setData] = useState<AdminSettings | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [drafts, setDrafts] = useState<Record<string, string>>({})
  const [busy, setBusy] = useState<string | null>(null)

  function load() {
    getAdminSettings().then(({ status, body }) => {
      if (status === 200) setData(body)
    })
  }
  useEffect(load, [])

  function applyResult(key: string, status: number, body: any) {
    if (status === 200) {
      setData((d) =>
        d && {
          ...d,
          editable: d.editable.map((s) =>
            s.key === key ? { ...s, value: body.value, source: body.source } : s,
          ),
        },
      )
      setDrafts((d) => {
        const n = { ...d }
        delete n[key]
        return n
      })
      setMsg({ ok: true, text: `Saved ${key} = ${body.value}.` })
    } else {
      setMsg({ ok: false, text: body?.detail || `Couldn't update ${key}.` })
      load() // resync to server truth on failure
    }
  }

  async function save(key: string, value: string) {
    setBusy(key)
    const { status, body } = await setAdminSetting(key, value)
    setBusy(null)
    applyResult(key, status, body)
  }
  async function reset(key: string) {
    setBusy(key)
    const { status, body } = await resetAdminSetting(key)
    setBusy(null)
    applyResult(key, status, body)
  }

  if (!data) return null
  const groups = [...new Set(data.editable.map((s) => s.group))]

  function control(s: AdminSetting) {
    if (s.type === 'bool') {
      return (
        <Toggle
          id={`set-${s.key}`}
          size="sm"
          hideLabel
          labelText={s.label}
          labelA="off"
          labelB="on"
          toggled={s.value === 'true'}
          disabled={busy === s.key}
          onToggle={(v: boolean) => save(s.key, v ? 'true' : 'false')}
        />
      )
    }
    if (s.type === 'enum') {
      return (
        <Select
          id={`set-${s.key}`}
          labelText=""
          size="sm"
          value={s.value}
          disabled={busy === s.key}
          onChange={(e) => save(s.key, e.target.value)}
        >
          {(s.choices || []).map((c) => (
            <SelectItem key={c} value={c} text={c} />
          ))}
        </Select>
      )
    }
    // int / float / str — edit then Save (avoids a write per keystroke).
    const draft = drafts[s.key]
    const dirty = draft !== undefined && draft !== s.value
    return (
      <div style={{ display: 'flex', gap: '0.35rem', alignItems: 'flex-end' }}>
        <TextInput
          id={`set-${s.key}`}
          labelText=""
          size="sm"
          style={{ width: '8rem' }}
          value={draft ?? s.value}
          disabled={busy === s.key}
          onChange={(e) => setDrafts((d) => ({ ...d, [s.key]: e.target.value }))}
          onKeyDown={(e) => {
            if (e.key === 'Enter' && dirty) save(s.key, draft as string)
          }}
        />
        <Button size="sm" kind="tertiary" disabled={!dirty || busy === s.key} onClick={() => save(s.key, draft as string)}>
          Save
        </Button>
      </div>
    )
  }

  return (
    <Tile>
      <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>
        Settings — governance, FinOps &amp; AI <Tag size="sm" type="blue">editable</Tag>
      </h4>
      <p style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.5rem' }}>
        {data.note} Changes take effect immediately and are audited.
      </p>
      {msg && (
        <InlineNotification
          kind={msg.ok ? 'success' : 'error'}
          lowContrast
          title={msg.ok ? 'Saved' : 'Not saved'}
          subtitle={msg.text}
          onCloseButtonClick={() => setMsg(null)}
          style={{ marginBottom: '0.5rem', maxWidth: 'none' }}
        />
      )}

      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(20rem, 1fr))', gap: '0 2.5rem' }}>
        {groups.map((g) => (
          <div key={g}>
            <div style={groupTitle}>{g}</div>
            {data.editable
              .filter((s) => s.group === g)
              .map((s) => (
                <div
                  key={s.key}
                  style={{
                    display: 'flex',
                    alignItems: 'flex-start',
                    justifyContent: 'space-between',
                    gap: '0.75rem',
                    padding: '0.4rem 0',
                    borderBottom: '1px solid var(--cds-border-subtle)',
                  }}
                >
                  <div style={{ minWidth: 0 }}>
                    <div style={{ fontSize: '0.83rem' }}>{s.label}</div>
                    <div style={{ fontSize: '0.7rem', color: 'var(--cds-text-secondary)' }}>{s.help}</div>
                    <div style={{ marginTop: '0.2rem', display: 'flex', gap: '0.35rem', alignItems: 'center' }}>
                      <SourceBadge source={s.source} />
                      {s.source === 'database' && (
                        <Button
                          size="sm"
                          kind="ghost"
                          renderIcon={Reset}
                          hasIconOnly
                          iconDescription="Reset to .env / default"
                          disabled={busy === s.key}
                          onClick={() => reset(s.key)}
                        />
                      )}
                    </div>
                  </div>
                  <div style={{ flexShrink: 0 }}>{control(s)}</div>
                </div>
              ))}
          </div>
        ))}
      </div>

      <div style={groupTitle}>Managed in .env (read-only)</div>
      <div style={{ display: 'grid', gridTemplateColumns: 'repeat(auto-fit, minmax(16rem, 1fr))', gap: '0.15rem 2rem' }}>
        {data.read_only.map((r) => (
          <div key={r.key} style={{ display: 'flex', justifyContent: 'space-between', gap: '0.5rem', fontSize: '0.8rem', padding: '0.15rem 0' }}>
            <span style={{ color: 'var(--cds-text-secondary)' }}>{r.label}</span>
            <span>
              <code style={{ fontSize: '0.75rem' }}>{r.value}</code> <SourceBadge source={r.source} />
            </span>
          </div>
        ))}
      </div>
      <p style={{ fontSize: '0.7rem', color: 'var(--cds-text-secondary)', marginTop: '0.4rem' }}>
        Secrets (API keys, credentials) and security/provisioning switches are managed in <code>.env</code> and never shown or edited here.
      </p>
    </Tile>
  )
}
