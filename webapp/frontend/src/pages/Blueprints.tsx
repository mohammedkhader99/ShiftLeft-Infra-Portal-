import { useEffect, useState } from 'react'
import { Tile, Tag, Button, Toggle, TextInput, InlineNotification } from '@carbon/react'
import { CheckmarkFilled, Undo } from '@carbon/icons-react'
import {
  getBlueprints, certifyBlueprint, decertifyBlueprint, type BlueprintMatrix,
} from '../api'

const TARGET_LABEL: Record<string, string> = {
  onprem: 'on-prem', azure: 'azure', oci: 'oci', aws: 'aws', gcp: 'gcp',
}

const STATUS_TAG: Record<string, 'green' | 'blue' | 'red' | 'gray'> = {
  certified: 'green',   // approved — the portal builds this automatically
  draft: 'blue',        // recipe exists, nobody has approved it yet
  missing: 'red',       // certified here but the orchestrator no longer ships it
  none: 'gray',         // no recipe — fulfilled by the infrastructure team
}

/**
 * technology → blueprint mapping, per cloud (F-CAT-10).
 *
 * A flat row per (technology, target) with the blueprint_ref visible: that
 * reference is the pointer into version control, so an engineer can go and read
 * the actual recipe. Merged with what the orchestrator ACTUALLY ships, so the
 * portal never advertises a capability that cannot run.
 */
export default function Blueprints() {
  const [data, setData] = useState<BlueprintMatrix | null>(null)
  const [showGaps, setShowGaps] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [busy, setBusy] = useState(false)
  // Version to pin at certification, per row. "Certified" should mean THIS
  // version was reviewed — not whatever the orchestrator happens to ship later.
  const [versions, setVersions] = useState<Record<string, string>>({})

  function reload() {
    getBlueprints().then((d) => { if (d && d !== 'forbidden') setData(d) })
  }
  useEffect(reload, [])

  async function onCertify(code: string, target: string, ref: string) {
    const key = `${code}:${target}`
    setBusy(true)
    const { status, body } = await certifyBlueprint(code, target, ref, (versions[key] || '').trim())
    setBusy(false)
    setMsg(status === 200
      ? { ok: true, text: `${code} certified on ${target}.` }
      : { ok: false, text: body?.detail || 'Could not certify.' })
    reload()
  }

  async function onWithdraw(code: string, target: string) {
    setBusy(true)
    await decertifyBlueprint(code, target)
    setBusy(false)
    reload()
  }

  if (!data) return null

  const withBlueprint = data.blueprints.filter((b) => b.state !== 'none')
  const rows = showGaps ? data.blueprints : withBlueprint
  const gaps = data.blueprints.length - withBlueprint.length

  const th = { padding: '0.4rem 0.5rem', fontWeight: 500 as const, textAlign: 'left' as const }
  const td = { padding: '0.4rem 0.5rem', verticalAlign: 'top' as const }

  return (
    <div style={{ display: 'grid', gap: '1rem', maxWidth: '68rem' }}>
      <Tile>
        <h4 style={{ fontSize: '0.95rem', fontWeight: 500, marginBottom: '0.25rem' }}>
          Blueprints — technology to build recipe, per cloud (F-CAT-10)
        </h4>
        <p style={{ fontSize: '0.8rem', color: 'var(--cds-text-secondary)', margin: '0 0 0.5rem' }}>
          A blueprint is a reviewed recipe held in version control; <code>blueprint_ref</code> is
          where to find it. A technology is provisioned automatically on a cloud only when its
          blueprint there is <strong>certified</strong> — everything else is fulfilled by the
          infrastructure team.
        </p>

        {!data.orchestrator_available && (
          <InlineNotification
            kind="error" lowContrast hideCloseButton
            title="Orchestrator unreachable"
            subtitle="Which recipes actually exist cannot be confirmed, so nothing can be certified right now."
            style={{ margin: '0.5rem 0', maxWidth: 'none' }}
          />
        )}
        {data.missing > 0 && (
          <InlineNotification
            kind="warning" lowContrast hideCloseButton
            title={`${data.missing} certified blueprint(s) missing from the orchestrator`}
            subtitle="Approved for use, but the executing layer no longer ships them. Requests relying on these will fail."
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

        <div style={{ display: 'flex', alignItems: 'center', gap: '0.75rem', margin: '0.5rem 0 0.25rem' }}>
          <Toggle
            id="show-gaps" size="sm" labelA="" labelB=""
            labelText=""
            toggled={showGaps}
            onToggle={(v: boolean) => setShowGaps(v)}
          />
          <span style={{ fontSize: '0.78rem', color: 'var(--cds-text-secondary)' }}>
            Show the {gaps} combinations with no blueprint yet
          </span>
        </div>

        <table style={{ width: '100%', borderCollapse: 'collapse', fontSize: '0.82rem' }}>
          <thead>
            <tr style={{ color: 'var(--cds-text-secondary)', borderBottom: '1px solid var(--cds-border-subtle)' }}>
              <th style={th}>technology</th>
              <th style={th}>target</th>
              <th style={th}>blueprint_ref</th>
              <th style={th}>version</th>
              <th style={th}>status</th>
              <th style={th}>certified_by</th>
              <th style={th} />
            </tr>
          </thead>
          <tbody>
            {rows.map((b) => (
              <tr key={`${b.technology_code}:${b.deployment_target}`}
                  style={{ borderBottom: '1px solid var(--cds-border-subtle)' }}>
                <td style={{ ...td, fontWeight: 500 }}>{b.technology_code}</td>
                <td style={td}>{TARGET_LABEL[b.deployment_target] || b.deployment_target}</td>
                <td style={td}>
                  {b.blueprint_ref
                    ? <code style={{ fontSize: '0.76rem' }}>{b.blueprint_ref}</code>
                    : <span style={{ color: 'var(--cds-text-secondary)' }}>—</span>}
                </td>
                <td style={td}>
                  {b.state === 'draft' ? (
                    <TextInput
                      id={`v-${b.technology_code}-${b.deployment_target}`}
                      labelText="" size="sm" placeholder="e.g. 1.2.0"
                      style={{ width: '7rem' }}
                      value={versions[`${b.technology_code}:${b.deployment_target}`] || ''}
                      onChange={(e) => setVersions((v) => ({
                        ...v, [`${b.technology_code}:${b.deployment_target}`]: e.target.value }))}
                    />
                  ) : (
                    b.version || <span style={{ color: 'var(--cds-text-secondary)' }}>—</span>
                  )}
                </td>
                <td style={td}>
                  <Tag type={STATUS_TAG[b.state] || 'gray'} size="sm" style={{ margin: 0 }}>{b.state}</Tag>
                </td>
                <td style={td}>
                  {b.certified_by || <span style={{ color: 'var(--cds-text-secondary)' }}>—</span>}
                </td>
                <td style={{ ...td, textAlign: 'right', whiteSpace: 'nowrap' }}>
                  {b.state === 'draft' && (
                    <Button size="sm" kind="ghost" renderIcon={CheckmarkFilled} hasIconOnly
                            iconDescription={`Certify ${b.technology_code} on ${b.deployment_target}`}
                            disabled={busy}
                            onClick={() => onCertify(b.technology_code, b.deployment_target, b.blueprint_ref)} />
                  )}
                  {(b.state === 'certified' || b.state === 'missing') && (
                    <Button size="sm" kind="ghost" renderIcon={Undo} hasIconOnly
                            iconDescription={`Withdraw certification for ${b.technology_code}`}
                            disabled={busy}
                            onClick={() => onWithdraw(b.technology_code, b.deployment_target)} />
                  )}
                </td>
              </tr>
            ))}
          </tbody>
        </table>

        <p style={{ fontSize: '0.72rem', color: 'var(--cds-text-secondary)', marginTop: '0.6rem' }}>
          <strong>{data.certified}</strong> certified · <strong>{withBlueprint.length - data.certified}</strong> awaiting
          certification · <strong>{gaps}</strong> with no blueprint. To add one: write the module in
          version control, redeploy the orchestrator so it appears here as <code>draft</code>, then
          certify it.
        </p>
      </Tile>
    </div>
  )
}
