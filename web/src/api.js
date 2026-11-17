// API-Wrapper: einheitliches Fehler-Handling + kleine Format-Helfer.
export async function api(path, opts = {}) {
  const r = await fetch(path, opts)
  const text = await r.text()
  let body
  try { body = JSON.parse(text) } catch { body = { detail: text } }
  if (!r.ok) {
    const msg = typeof body.detail === 'string' ? body.detail : `HTTP ${r.status}`
    throw new Error(msg)
  }
  return body
}

export function post(path, data) {
  return api(path, {
    method: 'POST',
    headers: { 'Content-Type': 'application/json' },
    body: data ? JSON.stringify(data) : undefined,
  })
}

export const kb = b => ((b || 0) / 1024).toFixed(1) + ' KB'
export const mb = b => b >= 1024 * 1024 ? (b / (1024 * 1024)).toFixed(1) + ' MB' : kb(b)

export function esc(s) {
  return String(s ?? '').replace(/[&<>"]/g, c => ({ '&': '&amp;', '<': '&lt;', '>': '&gt;', '"': '&quot;' }[c]))
}

export function planTxt(j) {
  if (j.schedule === 'manual') return 'manuell'
  if (j.schedule === 'hourly') return `stündlich :${j.schedule_time?.slice(-2) ?? ''}`
  if (j.schedule === 'daily') return `täglich ${j.schedule_time}`
  if (j.schedule === 'weekly') {
    const days = { mon: 'Mo', tue: 'Di', wed: 'Mi', thu: 'Do', fri: 'Fr', sat: 'Sa', sun: 'So' }
    return `wöchentlich ${days[j.schedule_weekday] ?? j.schedule_weekday} ${j.schedule_time}`
  }
  if (j.schedule === 'monthly') return `monatlich am ${j.schedule_day}. ${j.schedule_time}`
  return 'cron ' + (j.cron_expr || '')
}

export function fmtDateTime(iso) {
  if (!iso) return '–'
  const d = new Date(iso)
  return d.toLocaleString('de-DE', { dateStyle: 'short', timeStyle: 'short' })
}

export function domainOf(url) {
  try { return new URL(url).hostname.replace(/^www\./, '') } catch { return '' }
}
