import { useEffect, useState } from 'react'
import { Tile, Tag, InlineNotification } from '@carbon/react'
import { getPolicies, type PolicyCatalogue as Catalogue } from '../api'

const groupTitle = {
  fontSize: '0.72rem',
  fontWeight: 600 as const,
  textTransform: 'uppercase' as const,
  letterSpacing: '0.04em',
  color: 'var(--cds-text-secondary)',
  margin: '0.9rem 0 0.4rem',
}

export default function PolicyCatalogue() {
  const [data, setData] = useState<Catalogue | null>(null)

  useEffect(() => {
    getPolicies().then(({ status, body }) => {
      if (status === 200) setData(body)
    })
  }, [])

  if (!data) return null

  const blocking = data.rules.filter((r) => r.effect === 'block')
  const advisory = data.rules.filter((r) => r.effect !== 'block')

  function rule(r: Catalogue['rules'][number]) {
    const isBlock = r.effect === 'block'
    return (
      <div
        key={r.title}
        style={{
          padding: '0.5rem 0',
          borderBottom: '1px solid var(--cds-border-subtle)',
        }}
      >
        <div style={{ display: 'flex', gap: '0.5rem', alignItems: 'baseline', flexWrap: 'wrap' }}>
          <span style={{ fontSize: '0.85rem', fontWeight: 500 }}>{r.title}</span>
          <Tag type={isBlock ? 'red' : 'blue'} size="sm" style={{ margin: 0 }}>
            {isBlock ? 'blocks' : 'advises'}
          </Tag>
          {r.feature && (
            <Tag type="cool-gray" size="sm" style={{ margin: 0 }}>
              {r.feature}
            </Tag>
          )}
        </div>
        <div style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', marginTop: '0.15rem' }}>
          {r.description}
        </div>
        {r.applies_to && (
          <div style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.1rem' }}>
            Applies to: {r.applies_to}
          </div>
        )}
      </div>
    )
  }

  return (
    <Tile>
      <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>
        Policies — what every request is judged against (F-GOV-03){' '}
        <Tag size="sm" type="gray">read-only</Tag>
      </h4>

      {!data.available ? (
        <InlineNotification
          kind="error"
          lowContrast
          hideCloseButton
          title="Policy engine unreachable"
          subtitle={
            'The rules below cannot be listed right now, which is NOT the same as no rules ' +
            'being enforced. A request is still blocked if the policy engine cannot be reached.'
          }
          style={{ marginTop: '0.5rem', maxWidth: 'none' }}
        />
      ) : (
        <>
          <p style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.2rem' }}>
            {data.overview?.description}
          </p>
          <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.5rem' }}>
            Read live from the running policy engine, so this is what is actually enforcing —{' '}
            <strong>{data.blocking}</strong> blocking, <strong>{data.advisory}</strong> advisory.
            Policy is code: it is reviewed and deployed, never edited here.
          </p>

          <div style={groupTitle}>Blocking — a request cannot be submitted</div>
          {blocking.map(rule)}

          <div style={groupTitle}>Advisory — guidance, never blocks</div>
          {advisory.map(rule)}

          <p style={{ fontSize: '0.7rem', color: 'var(--cds-text-secondary)', marginTop: '0.5rem' }}>
            Source: {data.modules.join(', ')}
          </p>
        </>
      )}
    </Tile>
  )
}
