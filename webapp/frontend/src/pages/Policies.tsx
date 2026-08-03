import PolicyCatalogue from '../components/PolicyCatalogue'

/**
 * The OPA page: the governance rules every request is judged against.
 *
 * Its own top-level page rather than a panel inside Admin — the rules are what
 * govern the portal, so they are worth finding directly rather than scrolling
 * past budgets and API keys. Still platform-admin gated, and still read-only:
 * policy is code, reviewed and deployed.
 */
export default function Policies() {
  return (
    <div style={{ display: 'grid', gap: '1rem', maxWidth: '60rem' }}>
      <PolicyCatalogue />
    </div>
  )
}
