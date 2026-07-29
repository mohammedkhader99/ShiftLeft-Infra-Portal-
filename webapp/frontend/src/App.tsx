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
  Tile,
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
import RequestForm from './pages/RequestForm'
import MyRequests from './pages/MyRequests'

type Me = { email: string; roles: string[] }

const NAV = [
  { hash: '#/request/new', label: 'New request', icon: Add },
  { hash: '#/requests', label: 'My requests', icon: ListChecked },
  { hash: '#/overview', label: 'Estate overview', icon: ChartColumn },
]

function useHashRoute() {
  const [route, setRoute] = useState(window.location.hash || '#/request/new')
  useEffect(() => {
    const onHash = () => setRoute(window.location.hash || '#/request/new')
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  return route
}

function Placeholder({ name }: { name: string }) {
  return (
    <Tile>
      <p style={{ color: 'var(--cds-text-secondary)' }}>
        {name} moves to Carbon in the next increment. For now it's live in the classic
        portal at <a href="http://localhost:5173">:5173</a>.
      </p>
    </Tile>
  )
}

export default function App() {
  const [me, setMe] = useState<Me | null>(null)
  const [dark, setDark] = useState(true)
  const route = useHashRoute()

  useEffect(() => {
    fetch('/api/me')
      .then((r) => {
        if (r.status === 401) {
          window.location.href = '/login'
          return null
        }
        return r.ok ? r.json() : null
      })
      .then(setMe)
      .catch(() => setMe(null))
  }, [])

  let title: string
  let page: JSX.Element
  if (route.startsWith('#/requests')) {
    title = 'My requests'
    page = <MyRequests />
  } else if (route.startsWith('#/overview')) {
    title = 'Estate overview'
    page = <Placeholder name="Estate overview" />
  } else {
    title = 'New infrastructure request'
    page = <RequestForm />
  }

  return (
    <Theme theme={dark ? 'g100' : 'white'}>
      <div style={{ minHeight: '100vh', background: 'var(--cds-background)' }}>
        <Header aria-label="Infrastructure Provisioning Portal">
          <HeaderName href="#/request/new" prefix="IMD">
            Infrastructure Provisioning Portal
          </HeaderName>
          <HeaderGlobalBar>
            <HeaderGlobalAction aria-label="Toggle theme" onClick={() => setDark((d) => !d)}>
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
            {NAV.map((n) => (
              <SideNavLink
                key={n.hash}
                renderIcon={n.icon}
                href={n.hash}
                isActive={route.startsWith(n.hash) || (n.hash === '#/request/new' && route === '#/')}
              >
                {n.label}
              </SideNavLink>
            ))}
          </SideNavItems>
        </SideNav>

        <main style={{ marginTop: '3rem', marginLeft: '16rem', padding: '2rem 2.5rem' }}>
          <div style={{ display: 'flex', justifyContent: 'space-between', alignItems: 'flex-start', gap: '1rem' }}>
            <div>
              <p style={{ color: 'var(--cds-text-secondary)', fontSize: '0.8rem', margin: 0 }}>
                {me?.email || 'Home'}
              </p>
              <h1 style={{ fontWeight: 300, fontSize: '1.75rem', margin: '0.25rem 0 0' }}>{title}</h1>
            </div>
            <div style={{ display: 'flex', flexWrap: 'wrap', gap: '0.25rem', justifyContent: 'flex-end' }}>
              {me?.roles?.map((r) => (
                <Tag key={r} type="blue" size="sm">
                  {r}
                </Tag>
              ))}
            </div>
          </div>
          <div style={{ marginTop: '1.5rem' }}>{page}</div>
        </main>
      </div>
    </Theme>
  )
}
