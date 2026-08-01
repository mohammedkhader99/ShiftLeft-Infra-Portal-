// Client-side report export (F-RPT-11): turn an on-demand report into a CSV
// download or a printable page (browser "Save as PDF"). Zero dependencies, so
// it works in the offline build and needs no server round-trip.

type Table = { title: string; subtitle?: string; columns: string[]; rows: (string | number)[][] }

// Normalise a report of `kind` into a single titled table for export.
export function reportTable(kind: string, data: any): Table {
  const currency: string = data?.currency || 'AED'
  const money = (n: any) => `${Number(n || 0).toLocaleString(undefined, { maximumFractionDigits: 0 })} ${currency}`

  if (kind === 'estate') {
    const byStatus = (data?.by_status || {}) as Record<string, number>
    return {
      title: 'Estate summary',
      subtitle: `${money(data?.committed_monthly)}/month committed · ${data?.environments_provisioned ?? 0} provisioned environment(s)`,
      columns: ['Status', 'Count'],
      rows: Object.entries(byStatus).sort((a, b) => b[1] - a[1]).map(([s, c]) => [s, c]),
    }
  }
  if (kind === 'forecast') {
    const months: any[] = Array.isArray(data?.months) ? data.months : []
    return {
      title: 'Spend forecast',
      subtitle: `Now ${money(data?.current_monthly)}/mo → projected ${money(data?.projected_monthly)}/mo`,
      columns: ['Month', 'Period', 'Projected monthly'],
      rows: months.map((m) => [m.month, m.label, money(m.projected_monthly)]),
    }
  }
  if (kind === 'anomalies') {
    const items: any[] = Array.isArray(data?.anomalies) ? data.anomalies : []
    return {
      title: 'Cost anomalies',
      subtitle: `${data?.count ?? items.length} signal(s)`,
      columns: ['Severity', 'Subject', 'Type', 'Signal'],
      rows: items.map((a) => [a.severity, a.subject || a.reference || '', `${a.kind}/${a.type}`, a.signal || '']),
    }
  }
  // Fallback: flatten the object to key/value pairs.
  return {
    title: kind,
    columns: ['Key', 'Value'],
    rows: Object.entries(data || {}).map(([k, v]) => [k, typeof v === 'object' ? JSON.stringify(v) : String(v)]),
  }
}

export function dateStamp(): string {
  return new Date().toISOString().slice(0, 10)
}

function csvField(v: string | number): string {
  const s = String(v ?? '')
  return /[",\n\r]/.test(s) ? `"${s.replace(/"/g, '""')}"` : s
}

export function reportToCsv(kind: string, data: any, generatedAt?: string | null): string {
  const t = reportTable(kind, data)
  const lines: string[] = [csvField(t.title)]
  if (t.subtitle) lines.push(csvField(t.subtitle))
  if (generatedAt) lines.push(csvField(`Generated ${new Date(generatedAt).toLocaleString()}`))
  lines.push('')
  lines.push(t.columns.map(csvField).join(','))
  for (const row of t.rows) lines.push(row.map(csvField).join(','))
  return lines.join('\r\n')
}

export function downloadFile(filename: string, content: string, mime: string) {
  const blob = new Blob([content], { type: mime })
  const url = URL.createObjectURL(blob)
  const a = document.createElement('a')
  a.href = url
  a.download = filename
  document.body.appendChild(a)
  a.click()
  document.body.removeChild(a)
  setTimeout(() => URL.revokeObjectURL(url), 1000)
}

const HTML_ESCAPES: Record<string, string> = { '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }

function escapeHtml(s: string): string {
  return s.replace(/[&<>"]/g, (c) => HTML_ESCAPES[c])
}

// Open the report in a new window as a clean printable page and invoke the
// browser print dialog ("Save as PDF"). Returns false if the popup was blocked.
export function printReport(kind: string, data: any, generatedAt?: string | null): boolean {
  const t = reportTable(kind, data)
  const head = t.columns.map((c) => `<th>${escapeHtml(c)}</th>`).join('')
  const body = t.rows
    .map((r) => `<tr>${r.map((c) => `<td>${escapeHtml(String(c ?? ''))}</td>`).join('')}</tr>`)
    .join('')
  const gen = generatedAt ? `Generated ${new Date(generatedAt).toLocaleString()}` : ''
  const html = `<!doctype html><html><head><meta charset="utf-8"><title>${escapeHtml(t.title)}</title>
  <style>
    body { font-family: 'IBM Plex Sans', Arial, sans-serif; color: #161616; margin: 2.5rem; }
    h1 { font-weight: 400; font-size: 1.5rem; margin: 0 0 0.25rem; }
    .sub { color: #525252; margin: 0 0 0.15rem; }
    .meta { color: #6f6f6f; font-size: 0.8rem; margin: 0 0 1.5rem; }
    table { border-collapse: collapse; width: 100%; font-size: 0.9rem; }
    th, td { text-align: left; padding: 0.4rem 0.6rem; border-bottom: 1px solid #e0e0e0; vertical-align: top; }
    th { border-bottom: 2px solid #8d8d8d; }
    .footer { margin-top: 2rem; color: #8d8d8d; font-size: 0.72rem; }
    @media print { body { margin: 1.5cm; } }
  </style></head><body>
    <h1>${escapeHtml(t.title)}</h1>
    ${t.subtitle ? `<p class="sub">${escapeHtml(t.subtitle)}</p>` : ''}
    <p class="meta">Infrastructure Provisioning Portal${gen ? ' · ' + escapeHtml(gen) : ''}</p>
    <table><thead><tr>${head}</tr></thead><tbody>${body}</tbody></table>
    <p class="footer">Generated from portal data. Figures are estimates in ${escapeHtml(data?.currency || 'AED')} unless noted.</p>
  </body></html>`

  const w = window.open('', '_blank')
  if (!w) return false
  w.document.open()
  w.document.write(html)
  w.document.close()
  // Let the new document lay out before printing.
  setTimeout(() => {
    try {
      w.focus()
      w.print()
    } catch {
      /* ignore — user can print manually */
    }
  }, 300)
  return true
}
