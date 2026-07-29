import { useEffect, useState } from 'react'
import {
  Header,
  HeaderName,
  HeaderGlobalBar,
  HeaderGlobalAction,
  SideNav,
  SideNavItems,
  SideNavLink,
  Theme,
  Tag,
} from '@carbon/react'
import {
  Notification,
  Help,
  UserAvatar,
  Light,
  Asleep,
  Add,
  ListChecked,
  ChartColumn,
} from '@carbon/icons-react'

type Me = { email: string; roles: string[] }

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [loaded, setLoaded] = useState(false)
  const [dark, setDark] = useState(true)

  useEffect(() => {
    fetch('/api/me')
      .then((r) => (r.ok ? r.json() : null))
      .then(setMe)
      .catch(() => setMe(null))
      .finally(() => setLoaded(true))
  }, [])

  return (
    <Theme theme={dark ? 'g100' : 'white'}>
      <div style={{ minHeight: '100vh', background: 'var(--cds-background)' }}>
        <Header aria-label="Infrastructure Provisioning Portal">
          <HeaderName href="#" prefix="IMD">
            Infrastructure Provisioning Portal
          </HeaderName>
          <HeaderGlobalBar>
            <HeaderGlobalAction
              aria-label="Toggle theme"
              onClick={() => setDark((d) => !d)}
            >
              {dark ? <Light size={20} /> : <Asleep size={20} />}
            </HeaderGlobalAction>
            <HeaderGlobalAction aria-label="Notifications" onClick={() => {}}>
              <Notification size={20} />
            </HeaderGlobalAction>
            <HeaderGlobalAction aria-label="Help" onClick={() => {}}>
              <Help size={20} />
            </HeaderGlobalAction>
            <HeaderGlobalAction aria-label="User" onClick={() => {}}>
              <UserAvatar size={20} />
            </HeaderGlobalAction>
          </HeaderGlobalBar>
        </Header>

        <SideNav aria-label="Side navigation" expanded isPersistent>
          <SideNavItems>
            <SideNavLink renderIcon={Add} href="#/request/new" isActive>
              New request
            </SideNavLink>
            <SideNavLink renderIcon={ListChecked} href="#/requests">
              My requests
            </SideNavLink>
            <SideNavLink renderIcon={ChartColumn} href="#/overview">
              Estate overview
            </SideNavLink>
          </SideNavItems>
        </SideNav>

        <main style={{ marginTop: '3rem', marginLeft: '16rem', padding: '2.5rem 3rem' }}>
          <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.875rem' }}>
            Home
          </p>
          <h1 style={{ fontWeight: 300, fontSize: '2rem', margin: '0.25rem 0 1.5rem' }}>
            Welcome{me?.email ? `, ${me.email}` : ''}
          </h1>

          <div
            style={{
              background: 'var(--cds-layer)',
              border: '1px solid var(--cds-border-subtle)',
              padding: '1.5rem',
              maxWidth: '40rem',
            }}
          >
            <p style={{ color: 'var(--cds-text-secondary)', margin: '0 0 0.75rem' }}>
              You are signed in. Your roles (resolved live from the API):
            </p>
            {!loaded ? (
              <span style={{ color: 'var(--cds-text-secondary)' }}>Loading…</span>
            ) : me && me.roles.length ? (
              <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.25rem' }}>
                {me.roles.map((r) => (
                  <Tag key={r} type="blue">
                    {r}
                  </Tag>
                ))}
              </div>
            ) : (
              <span style={{ color: 'var(--cds-text-error)' }}>
                Could not read your roles from the API (sign-in required).
              </span>
            )}
            <p
              style={{
                marginTop: '1.5rem',
                color: 'var(--cds-text-secondary)',
                fontSize: '0.875rem',
              }}
            >
              This is the new React + IBM Carbon shell (UX.1). The request form, My
              Requests and the overview move here next, screen by screen.
            </p>
          </div>
        </main>
      </div>
    </Theme>
  )
}
