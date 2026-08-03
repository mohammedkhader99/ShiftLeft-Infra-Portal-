import { useEffect, useState } from 'react'
import { Tile, Tag, Button, InlineNotification } from '@carbon/react'
import { CheckmarkFilled, Undo } from '@carbon/icons-react'
import {
  getBlueprints, certifyBlueprint, decertifyBlueprint, type BlueprintMatrix,
} from '../api'

const TARGET_LABEL: Record<string, string> = {
  onprem: 'On-prem', azure: 'Azure', oci: 'OCI', aws: 'AWS', gcp: 'GCP',
}

/**
 * Which technology can be built automatically, on which cloud.
 *
 * A blueprint is a reviewed recipe in git (Terraform module, Helm chart,
 * playbook). This page shows the pointer + certification record, merged with
 * what the orchestrator ACTUALLY ships — so it can never advertise a capability
 * that cannot run, and flags the reverse if a certified recipe goes missing.
 */
export default function Blueprints() {
  const [data, setData] = useState<BlueprintMatrix | null>(null)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)

  function reload() {
    getBlueprints().then((d) => { if (d && d !== 'forbidden') setData(d) })
  }
  useEffect(reload, [])

  async function onCertify(code: string, target: string, ref: string) {
    setBusy(true)
    const { status, body } = await certifyBlueprint(code, target, ref, '')
    setBusy(false)
    setMsg(status === 200
      ? { ok: true, text: `${code} certified on ${TARGET_LABEL[target] || target}.` }
      : { ok: false, text: body?.detail || 'Could not certify.' })
    reload()
  }

  async function onDecertify(code: string, target: string) {
    setBusy(true)
    await decertifyBlueprint(code, target)
    setBusy(false)
    reload()
  }

  if (!data) return null

  // Group by technology so each row reads as "this technology, across the clouds".
  const byTech = new Map<string, Record<string, BlueprintMatrix['blueprints'][number]>>()
  for (const b of data.blueprints) {
    const row = byTech.get(b.technology_code) || {}
    row[b.deployment_target] = b
    byTech.set(b.technology_code, row)
  }

  function cell(entry?: BlueprintMatrix['blueprints'][number]) {
    if (!entry) return <span style={{ color: 'var(--cds-text-secondary)' }}>—</span>
    if (entry.state === 'certified') {
      return (
        <div style={{ display: 'flex', gap: '0.3rem', alignItems: 'center', justifyContent: 'center' }}>
          <Tag type="green" size="sm" style={{ margin: 0 }} title={entry.blueprint_ref}>
            certified{entry.version ? ` ${entry.version}` : ''}
          </Tag>
          <Button size="sm" kind="ghost" renderIcon={Undo} hasIconOnly
                  iconDescription="Withdraw certification" disabled={busy}
                  onClick={() => onDecertify(entry.technology_code, entry.deployment_target)} />
        </div>
      )
    }
    if (entry.state === 'missing') {
      return (
        <Tag type="red" size="sm" style={{ margin: 0 }}
             title="Certified here, but the orchestrator no longer ships it">
          missing from orchestrator
        </Tag>
      )
    }
    return (
      <div style={{ display: 'flex', gap: '0.3rem', alignItems: 'center', justifyContent: 'center' }}>
        <Tag type="gray" size="sm" style={{ margin: 0 }} title={entry.description}>available</Tag>
        <Button size="sm" kind="ghost" renderIcon={CheckmarkFilled} hasIconOnly
                iconDescription={`Certify ${entry.technology_code} on ${entry.deployment_target}`}
                disabled={busy}
                onClick={() => onCertify(entry.technology_code, entry.deployment_target, entry.blueprint_ref)} />
      </div>
    )
  }

  return (
    <div style={{ display: 'grid', gap: '1rem', maxWidth: '62rem' }}>
      <Tile>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>
          Blueprints — what the portal can build automatically (F-CAT-10)
        </h4>
        <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.5rem' }}>
          A blueprint is a reviewed recipe held in version control. A technology is provisioned
          automatically on a cloud only when a <strong>certified</strong> blueprint exists for that
          pair — everything else is fulfilled by the infrastructure team. Certifying is a deliberate
          act and is recorded.
        </p>

        {!data.orchestrator_available && (
          <InlineNotification
            kind="error" lowContrast hideCloseButton
            title="Orchestrator unreachable"
            subtitle="Which recipes actually exist cannot be confirmed right now, so nothing can be certified."
            style={{ margin: '0.5rem 0', maxWidth: 'none' }}
          />
        )}
        {data.missing > 0 && (
          <InlineNotification
            kind="warning" lowContrast hideCloseButton
            title={`${data.missing} certified blueprint(s) missing from the orchestrator`}
            subtitle="These are approved for use but the executing layer no longer ships them. Requests relying on them will fail."
            style={{ margin: '0.5rem 0', maxWidth: 'none' }}
          />
        )}
        {msg && (
          <InlineNotification
            kind={msg.ok ? 'success' : 'error'} lowContrast
            title={msg.ok ? 'Saved' : 'Not saved'} subtitle={msg.text}
            onCloseButtonClick={() => setMsg(null)}
            style={{ margin: '0.5rem 0', maxWidth: 'none' }}
          />
        )}

        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.85rem' }}>
          <thead>
            <tr style={{ textAlign: 'left', color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
              <th style={{ padding: '0.4rem', fontWeight: 500 }}>Technology</th>
              {data.targets.map((t) => (
                <th key={t} style={{ padding: '0.4rem', fontWeight: 500, textAlign: 'center' }}>
                  {TARGET_LABEL[t] || t}
                </th>
              ))}
            </tr>
          </thead>
          <tbody>
            {[...byTech.entries()].sort().map(([code, row]) => (
              <tr key={code} style={{ borderBottom: '1px solid var(--cds-border-subtle)' }}>
                <td style={{ padding: '0.4rem', fontWeight: 500 }}>{code}</td>
                {data.targets.map((t) => (
                  <td key={t} style={{ padding: '0.4rem', textAlign: 'center' }}>{cell(row[t])}</td>
                ))}
              </tr>
            ))}
          </tbody>
        </table>

        <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.6rem' }}>
          <strong>{data.certified}</strong> certified. Only technologies with a recipe appear here;
          the rest of the catalogue has no blueprint yet and is fulfilled manually. To add one: write
          the module, redeploy the orchestrator, then certify it above.
        </p>
      </Tile>
    </div>
  )
}
