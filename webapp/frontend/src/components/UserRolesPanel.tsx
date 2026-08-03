import { useEffect, useState } from 'react'
import { Tile, Tag, TextInput, Select, SelectItem, Button, InlineNotification } from '@carbon/react'
import { TrashCan, Add } from '@carbon/icons-react'
import { getUserRoles, grantUserRole, revokeUserRole, type UserRoles } from '../api'

/**
 * Who holds which portal role.
 *
 * Roles are maintained here because there is no external source: Entra provides
 * login only (no group claims) and Jira's authority model is per-ticket
 * Assignment Groups, which answer "who may act on this ticket" rather than "who
 * may administer the portal". Identity stays federated; authorisation lives here.
 */
export default function UserRolesPanel() {
  const [data, setData] = useState<UserRoles | null>(null)
  const [email, setEmail] = useState('')
  const [role, setRole] = useState('')
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  function reload() {
    getUserRoles().then((d) => {
      if (d && d !== 'forbidden') {
        setData(d)
        setRole((cur) => cur || d.roles[0] || '')
      }
    })
  }
  useEffect(reload, [])

  async function onGrant() {
    if (!email.trim() || !role) return
    setBusy(true)
    const { status, body } = await grantUserRole(email.trim(), role)
    setBusy(false)
    if (status === 200) {
      setEmail('')
      setMsg({ ok: true, text: `${role} granted to ${body.email}.` })
      reload()
    } else {
      setMsg({ ok: false, text: body?.detail || 'Could not grant that role.' })
    }
  }

  async function onRevoke(who: string, which: string) {
    setBusy(true)
    await revokeUserRole(who, which)
    setBusy(false)
    reload()
  }

  if (!data) return null

  const portalManaged = data.role_source === 'portal'

  return (
    <Tile>
      <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>
        Access control — who holds which role (F-IAM-01){' '}
        <Tag size="sm" type={portalManaged ? 'green' : 'red'}>
          {portalManaged ? 'enforced' : `not enforced (${data.role_source})`}
        </Tag>
      </h4>

      {!portalManaged && (
        <InlineNotification
          kind="error"
          lowContrast
          hideCloseButton
          title="Roles are not being enforced"
          subtitle={
            `ROLE_SOURCE is '${data.role_source}', so this list is ignored. In 'mock' every ` +
            `signed-in user gets EVERY role, including platform administrator. Set ` +
            `ROLE_SOURCE=portal in .env to enforce the list below.`
          }
          style={{ margin: '0.5rem 0', maxWidth: 'none' }}
        />
      )}

      <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.5rem' }}>
        Anyone who signs in and is not listed here gets{' '}
        <strong>{data.default_role.join(', ') || 'no role'}</strong> — enough to raise a request,
        which Jira still has to approve. Grant elevated roles per person below.
      </p>

      {data.bootstrap_admins.length > 0 && (
        <p style={{ fontSize: '0.75rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.75rem' }}>
          Break-glass administrators (set in <code>.env</code>, cannot be revoked here):{' '}
          {data.bootstrap_admins.map((b) => (
            <Tag key={b} size="sm" type="purple" style={{ margin: '0 0.2rem 0 0' }}>{b}</Tag>
          ))}
        </p>
      )}

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

      <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
        <thead>
          <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
            <th style={{ padding: '0.35rem 0.4rem', fontWeight: 500 }}>Person</th>
            <th style={{ padding: '0.35rem 0.4rem', fontWeight: 500 }}>Roles</th>
            <th style={{ padding: '0.35rem 0.4rem' }} />
          </tr>
        </thead>
        <tbody>
          {data.users.length === 0 && (
            <tr>
              <td colSpan={3} style={{ padding: '0.6rem 0.4rem', color: 'var(--cds-text-secondary)' }}>
                Nobody has been granted an elevated role yet — everyone who signs in is a requester.
              </td>
            </tr>
          )}
          {data.users.map((u) => (
            <tr key={u.email} style={{ borderBottom: '1px solid var(--cds-border-subtle)' }}>
              <td style={{ padding: '0.35rem 0.4rem' }}>{u.email}</td>
              <td style={{ padding: '0.35rem 0.4rem' }}>
                {u.roles.map((r) => (
                  <Tag key={r} size="sm" type={r === 'platform_admin' ? 'red' : 'cool-gray'} style={{ margin: '0 0.2rem 0 0' }}>
                    {r}
                  </Tag>
                ))}
              </td>
              <td style={{ padding: '0.35rem 0.4rem', textAlign: 'right' }}>
                {u.roles.map((r) => (
                  <Button
                    key={r}
                    size="sm"
                    kind="ghost"
                    renderIcon={TrashCan}
                    hasIconOnly
                    iconDescription={`Remove ${r} from ${u.email}`}
                    disabled={busy}
                    onClick={() => onRevoke(u.email, r)}
                  />
                ))}
              </td>
            </tr>
          ))}
        </tbody>
      </table>

      <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'flex-end', marginTop: '0.75rem' }}>
        <div style={{ flex: 1, maxWidth: '22rem' }}>
          <TextInput
            id="grant-email"
            labelText="Person's email"
            placeholder="name@emaratechg.ae"
            value={email}
            onChange={(e) => setEmail(e.target.value)}
          />
        </div>
        <div style={{ width: '12rem' }}>
          <Select id="grant-role" labelText="Role" value={role} onChange={(e) => setRole(e.target.value)}>
            {data.roles.map((r) => (
              <SelectItem key={r} value={r} text={r} />
            ))}
          </Select>
        </div>
        <Button size="md" renderIcon={Add} disabled={busy || !email.trim()} onClick={onGrant}>
          Grant
        </Button>
      </div>
    </Tile>
  )
}
