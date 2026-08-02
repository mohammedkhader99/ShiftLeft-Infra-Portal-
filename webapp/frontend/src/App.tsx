import { useEffect, useState } from 'react'
import {
  Header,
  HeaderName,
  HeaderGlobalBar,
  HeaderGlobalAction,
  SideNav,
  SideNavItems,
  SideNavLink,
  SideNavMenu,
  SideNavMenuItem,
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
  Money,
  Report,
  Settings,
  Activity as ActivityIcon,
  Chat,
} from '@carbon/icons-react'
import RequestForm from './pages/RequestForm'
import MyRequests from './pages/MyRequests'
import Overview from './pages/Overview'
import Showback from './pages/Showback'
import Reports from './pages/Reports'
import Admin from './pages/Admin'
import Activity from './pages/Activity'
import Assistant from './pages/Assistant'
import HeaderSearch from './components/HeaderSearch'

type Me = { email: string; roles: string[] }

// Request types live under the expandable "New request" nav menu (each routes
// to #/request/new/<type>); the form on the right follows the selection.
const REQUEST_TYPES: [string, string][] = [
  ['create', 'Create environment'],
  ['clone', 'Clone environment'],
  ['sandbox', 'Sandbox environment'],
  ['temporary', 'Temporary environment'],
  ['add', 'Add component'],
  ['resize', 'Resize component'],
  ['reduce', 'Reduce capacity'],
  ['refresh', 'Refresh environment'],
  ['restore', 'Restore from backup'],
  ['decommission', 'Decommission'],
]
const RT_LABEL = Object.fromEntries(REQUEST_TYPES) as Record<string, string>

function parseRequestType(route: string): string {
  const m = route.match(/^#\/request\/new\/(create|clone|sandbox|temporary|add|resize|reduce|refresh|restore|decommission)/)
  return m ? m[1] : 'create'
}

function useHashRoute() {
  const [route, setRoute] = useState(window.location.hash || '#/request/new')
  useEffect(() => {
    const onHash = () => setRoute(window.location.hash || '#/request/new')
    window.addEventListener('hashchange', onHash)
    return () => window.removeEventListener('hashchange', onHash)
  }, [])
  return route
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

  const onRequest =
    !route.startsWith('#/requests') &&
    !route.startsWith('#/overview') &&
    !route.startsWith('#/showback') &&
    !route.startsWith('#/reports') &&
    !route.startsWith('#/activity') &&
    !route.startsWith('#/assistant') &&
    !route.startsWith('#/admin')
  const isAdmin = !!me && me.roles.includes('platform_admin')
  const oversight = !!me && me.roles.some((r) => ['platform_admin', 'auditor', 'finops'].includes(r))
  let title: string
  let page: JSX.Element
  if (route.startsWith('#/requests')) {
    title = 'My requests'
    page = <MyRequests route={route} />
  } else if (route.startsWith('#/overview')) {
    title = 'Estate overview'
    page = <Overview />
  } else if (route.startsWith('#/showback')) {
    title = 'Showback'
    page = <Showback />
  } else if (route.startsWith('#/reports')) {
    title = 'Reports'
    page = <Reports />
  } else if (route.startsWith('#/activity')) {
    title = 'Activity'
    page = <Activity />
  } else if (route.startsWith('#/assistant')) {
    title = 'Assistant'
    page = <Assistant />
  } else if (route.startsWith('#/admin')) {
    title = 'Admin console'
    page = <Admin />
  } else {
    const t = parseRequestType(route)
    title = `New request · ${RT_LABEL[t]}`
    page = <RequestForm initialType={t} />
  }

  return (
    <Theme theme={dark ? 'g100' : 'white'}>
      <div style={{ minHeight: '100vh', background: 'var(--cds-background)' }}>
        <Header aria-label="Infrastructure Provisioning Portal">
          <HeaderName href="#/request/new" prefix="IMD">
            Infrastructure Provisioning Portal
          </HeaderName>
          {me && <HeaderSearch />}
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
            <SideNavMenu renderIcon={Add} title="New request" defaultExpanded={onRequest} isActive={onRequest}>
              {REQUEST_TYPES.map(([type, label]) => (
                <SideNavMenuItem
                  key={type}
                  href={`#/request/new/${type}`}
                  isActive={onRequest && parseRequestType(route) === type}
                >
                  {label}
                </SideNavMenuItem>
              ))}
            </SideNavMenu>
            <SideNavLink renderIcon={ListChecked} href="#/requests" isActive={route.startsWith('#/requests')}>
              My requests
            </SideNavLink>
            <SideNavLink renderIcon={ChartColumn} href="#/overview" isActive={route.startsWith('#/overview')}>
              Estate overview
            </SideNavLink>
            <SideNavLink renderIcon={Money} href="#/showback" isActive={route.startsWith('#/showback')}>
              Showback
            </SideNavLink>
            {oversight && (
              <SideNavLink renderIcon={Report} href="#/reports" isActive={route.startsWith('#/reports')}>
                Reports
              </SideNavLink>
            )}
            {oversight && (
              <SideNavLink renderIcon={ActivityIcon} href="#/activity" isActive={route.startsWith('#/activity')}>
                Activity
              </SideNavLink>
            )}
            <SideNavLink renderIcon={Chat} href="#/assistant" isActive={route.startsWith('#/assistant')}>
              Assistant
            </SideNavLink>
            {isAdmin && (
              <SideNavLink renderIcon={Settings} href="#/admin" isActive={route.startsWith('#/admin')}>
                Admin
              </SideNavLink>
            )}
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
