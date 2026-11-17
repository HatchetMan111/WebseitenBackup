import React, { useCallback, useEffect, useMemo, useRef, useState } from 'react'
import { api, post, kb, mb, planTxt, fmtDateTime, domainOf } from './api.js'

// ---------------------------------------------------------------- kleine Bausteine
let toastSeq = 0
function useToasts() {
  const [toasts, setToasts] = useState([])
  const push = useCallback((msg, kind = 'info') => {
    const id = ++toastSeq
    setToasts(t => [...t, { id, msg, kind }])
    setTimeout(() => setToasts(t => t.filter(x => x.id !== id)), 6000)
  }, [])
  const remove = id => setToasts(t => t.filter(x => x.id !== id))
  return { toasts, push, remove }
}

function Toasts({ toasts, remove }) {
  return (
    <div className="toasts">
      {toasts.map(t => (
        <div key={t.id} className={`toast ${t.kind}`} onClick={() => remove(t.id)}>{t.msg}</div>
      ))}
    </div>
  )
}

function Pill({ kind = 'idle', children }) {
  return <span className={`pill ${kind}`}>{children}</span>
}

function Bar({ done, total }) {
  const pct = total > 0 ? Math.round(100 * done / total) : 0
  return (
    <div className="barwrap">
      <div className="bar"><i style={{ width: pct + '%' }} /></div>
      <span className="hint">{done} von {total} Seiten ({pct} %)</span>
    </div>
  )
}

function Modal({ title, onClose, children, wide }) {
  useEffect(() => {
    const h = e => { if (e.key === 'Escape') onClose() }
    window.addEventListener('keydown', h)
    return () => window.removeEventListener('keydown', h)
  }, [onClose])
  return (
    <div className="overlay" onMouseDown={e => { if (e.target === e.currentTarget) onClose() }}>
      <div className={`modal ${wide ? 'wide' : ''}`}>
        <div className="modal-head">
          <b>{title}</b>
          <button className="ghost btn-sm" onClick={onClose}>✕</button>
        </div>
        <div className="modal-body">{children}</div>
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- Haupt-App
export default function App() {
  const { toasts, push, remove } = useToasts()
  const [jobs, setJobs] = useState(null)          // null = laedt, [] = leer
  const [runs, setRuns] = useState([])
  const [health, setHealth] = useState(null)
  const [quickUrl, setQuickUrl] = useState('')
  const [quickBusy, setQuickBusy] = useState(false)
  const [editJob, setEditJob] = useState(null)    // null | 'new' | Job-Objekt
  const [detail, setDetail] = useState(null)      // {type: 'pages'|'discovery'|'log'|'preview', ...}
  const [search, setSearch] = useState({ job: '', q: '', busy: false, result: null, err: '' })
  const runningRef = useRef(false)
  const timerRef = useRef(null)

  const load = useCallback(async (quiet = true) => {
    try {
      const [j, r] = await Promise.all([api('/api/jobs'), api('/api/runs?limit=25')])
      setJobs(j)
      setRuns(r)
      setHealth(true)
      runningRef.current = r.some(x => x.status === 'running')
      return { jobs: j, runs: r }
    } catch (e) {
      setHealth(false)
      if (!quiet) push('Server nicht erreichbar: ' + e.message, 'err')
      return { jobs: [], runs: [] }
    }
  }, [push])

  // Polling: schnell waehrend ein Backup laeuft, sonst gemuetlich
  useEffect(() => {
    let alive = true
    const tick = async () => {
      if (!alive) return
      await load()
      timerRef.current = setTimeout(tick, runningRef.current ? 4000 : 20000)
    }
    tick()
    return () => { alive = false; clearTimeout(timerRef.current) }
  }, [load])

  // ---------------------------------------------------------------- Aktionen
  const act = async (fn, okMsg, errPrefix) => {
    try {
      const res = await fn()
      if (okMsg) push(okMsg, 'ok')
      load()
      return res
    } catch (e) {
      push(`${errPrefix}: ${e.message}`, 'err')
      return null
    }
  }

  const discoverJob = async (job) => {
    push('Discovery läuft: robots.txt → sitemap.xml → Links …', 'info')
    const d = await act(
      () => post(`/api/jobs/${job.id}/discover`),
      null, 'Discovery-Fehler')
    if (d) setDetail({ type: 'discovery', job, data: d })
  }

  const runJob = async (job) => {
    await act(() => post(`/api/jobs/${job.id}/run`), 'Backup gestartet – Fortschritt in den Job-Karten')
  }

  const delJob = async (job) => {
    if (!confirm(`Job „${job.name}“ inkl. aller Läufe wirklich löschen?\nDateien bleiben unter data/jobs/${job.id}/ erhalten.`)) return
    await act(() => api(`/api/jobs/${job.id}`, { method: 'DELETE' }), 'Job gelöscht')
  }

  const quickStart = async (e) => {
    e.preventDefault()
    let url = quickUrl.trim()
    if (!url) return
    if (!/^https?:\/\//i.test(url)) url = 'https://' + url
    setQuickBusy(true)
    try {
      let name = domainOf(url) || 'Webseite'
      const job = await post('/api/jobs', { name, start_url: url, schedule: 'manual' })
      await post(`/api/jobs/${job.id}/run`)
      setQuickUrl('')
      push(`🎉 Backup von ${name} gestartet! Wenn es fertig ist, kannst du die komplette Website als eine HTML-Datei herunterladen.`, 'ok')
      await load()
    } catch (err) {
      push('Start fehlgeschlagen: ' + err.message, 'err')
    } finally {
      setQuickBusy(false)
    }
  }

  const openPages = async (job) => {
    try {
      const d = await api(`/api/jobs/${job.id}/pages`)
      if (d.discovered) {
        setDetail({ type: 'discovery', job, data: { count: d.discovered.length, pages: d.discovered } })
      } else if (d.pages && d.pages.length) {
        setDetail({ type: 'pages', job, data: d })
      } else {
        push(d.note || 'Noch keine Discovery / kein Backup – erst „Seiten entdecken“ klicken', 'info')
      }
    } catch (e) {
      push('Seiten laden fehlgeschlagen: ' + e.message, 'err')
    }
  }

  const doSearch = async (e) => {
    e && e.preventDefault()
    const q = search.q.trim()
    if (q.length < 2) { setSearch(s => ({ ...s, err: 'Mindestens 2 Zeichen' })); return }
    setSearch(s => ({ ...s, busy: true, err: '' }))
    try {
      const r = await api(`/api/search?q=${encodeURIComponent(q)}${search.job ? '&job_id=' + search.job : ''}`)
      setSearch(s => ({ ...s, busy: false, result: r }))
    } catch (err) {
      setSearch(s => ({ ...s, busy: false, err: err.message, result: null }))
    }
  }

  // ---------------------------------------------------------------- Statistik
  const stats = useMemo(() => {
    if (!jobs) return null
    const totalPages = jobs.reduce((n, j) => n + (j.last_run?.pages_ok || 0), 0)
    const totalBytes = runs.reduce((n, r) => n + (r.status === 'ok' ? r.total_bytes || 0 : 0), 0)
    const scheduled = jobs.filter(j => j.schedule !== 'manual' && j.enabled)
    const next = scheduled
      .map(j => j.next_run)
      .filter(Boolean)
      .sort()[0]
    const running = runs.some(r => r.status === 'running')
    return { totalPages, totalBytes, next, running }
  }, [jobs, runs])

  // ---------------------------------------------------------------- Render
  return (
    <>
      <header>
        <div className="logo">SB</div>
        <div className="head-txt">
          <h1>SiteBackup</h1>
          <div className="sub">Websites für immer sichern — als HTML-Dateien oder eine einzige portabel HTML-Datei</div>
        </div>
        <div className={`health ${health ? 'ok' : 'err'}`}>{health ? '● Server online' : '● Server nicht erreichbar'}</div>
      </header>

      <main>
        {/* Statistik */}
        {stats && (
          <div className="stats">
            <div className="stat"><b>{jobs.length}</b><span>Backup-Jobs</span></div>
            <div className="stat"><b>{stats.totalPages}</b><span>Seiten gesichert</span></div>
            <div className="stat"><b>{mb(stats.totalBytes)}</b><span>Archiv-Größe</span></div>
            <div className="stat">
              <b className={stats.running ? 'spin' : ''}>{stats.running ? '⏳' : '🗂'}</b>
              <span>{stats.running ? 'Backup läuft …' : stats.next ? 'nächster: ' + fmtDateTime(stats.next) : 'keine Termine'}</span>
            </div>
          </div>
        )}

        {/* Schnell-Backup: der 1-Klick-Flow */}
        <section className="card highlight">
          <h2>🌐 Website jetzt einmal sichern</h2>
          <p className="hint">URL eingeben, fertig: Es wird automatisch eine Discovery gemacht (robots.txt + Sitemap + Links),
            jede Unterseite als HTML gesichert — und danach kannst du <b>die komplette Website als eine einzige HTML-Datei</b> herunterladen
            (Bilder/CSS/Fonts eingebettet, offline lesbar und navigierbar — auf jeder Plattform, für immer).</p>
          <form onSubmit={quickStart} className="row">
            <input
              className="grow"
              placeholder="https://deine-webseite.de"
              value={quickUrl}
              onChange={e => setQuickUrl(e.target.value)}
              inputMode="url"
              autoFocus
            />
            <button disabled={quickBusy || !quickUrl.trim()}>{quickBusy ? 'Startet …' : '⚡ Jetzt sichern'}</button>
          </form>
          <div className="hint" style={{ marginTop: 6 }}>Der Job heißt wie die Domain und wird sofort gestartet — Fortschritt unten bei den Jobs.</div>
        </section>

        {/* Jobs */}
        <section className="card">
          <div className="card-head">
            <h2>📦 Backup-Jobs</h2>
            <button onClick={() => setEditJob('new')}>+ Neuer Job</button>
          </div>
          {jobs === null && <div className="hint">Lade …</div>}
          {jobs && jobs.length === 0 && (
            <div className="empty">Noch keine Jobs. Oben eine URL eingeben — oder einen eigenen Job mit Zeitplan anlegen.</div>
          )}
          {jobs && jobs.length > 0 && (
            <div className="jobgrid">
              {jobs.map(j => <JobCard key={j.id} job={j} run={runs.find(r => r.job_id === j.id && r.status === 'running') || null}
                onDiscover={() => discoverJob(j)} onRun={() => runJob(j)} onPages={() => openPages(j)}
                onEdit={() => setEditJob(j)} onDelete={() => delJob(j)} push={push} />)}
            </div>
          )}
        </section>

        {/* Suche */}
        <section className="card">
          <h2>🔎 Archiv durchsuchen</h2>
          <p className="hint">Volltextsuche über Titel + Text aller gesicherten Seiten (nur erfolgreiche Backups).</p>
          <form onSubmit={doSearch} className="row">
            <select value={search.job} onChange={e => setSearch(s => ({ ...s, job: e.target.value }))}>
              <option value="">Alle Jobs</option>
              {jobs && jobs.map(j => <option key={j.id} value={j.id}>{j.name} (#{j.id})</option>)}
            </select>
            <input className="grow" placeholder="Suchbegriff … (min. 2 Zeichen)" value={search.q}
              onChange={e => setSearch(s => ({ ...s, q: e.target.value }))} />
            <button disabled={search.busy}>{search.busy ? 'Suche …' : 'Suchen'}</button>
          </form>
          {search.err && <div className="hint errtxt">{search.err}</div>}
          {search.result && (
            search.result.count === 0
              ? <div className="hint" style={{ marginTop: 10 }}>Keine Treffer.</div>
              : <div className="twrap" style={{ marginTop: 10 }}>
                <table>
                  <thead><tr><th>Seite</th><th>Ausschnitt</th></tr></thead>
                  <tbody>
                    {search.result.results.map((t, i) => (
                      <tr key={i}>
                        <td>
                          {t.file
                            ? <a href="#" onClick={e => { e.preventDefault(); setDetail({ type: 'preview', job: { id: t.job_id }, file: t.file, title: t.title }) }}>{t.title || t.url}</a>
                            : <b>{t.title || t.url}</b>}
                          <br /><span className="hint">Job {t.job_id} · <a href={t.url} target="_blank" rel="noopener">{t.url}</a></span>
                        </td>
                        <td className="snippet">{t.snippet || ''}</td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
          )}
        </section>

        {/* Läufe */}
        <section className="card">
          <h2>🕘 Letzte Läufe</h2>
          {runs.length === 0 && <div className="hint">Noch keine Läufe.</div>}
          {runs.length > 0 && (
            <div className="twrap">
              <table>
                <thead><tr><th>Run</th><th>Status</th><th>Ergebnis</th><th>Log</th></tr></thead>
                <tbody>
                  {runs.map(r => (
                    <tr key={r.id}>
                      <td>#{r.id} · Job {r.job_id}<br /><span className="hint">{fmtDateTime(r.started_at)}</span></td>
                      <td>
                        <Pill kind={r.status === 'ok' ? 'ok' : r.status === 'running' ? 'run' : 'err'}>{r.status}</Pill>
                        {r.error && <div className="hint errtxt">{r.error.slice(0, 120)}</div>}
                      </td>
                      <td>
                        {r.status === 'running' && r.log
                          ? <Bar done={lastProgress(r.log).done} total={lastProgress(r.log).total} />
                          : <>OK {r.pages_ok} · Fehler {r.pages_failed}<br /><span className="hint">{kb(r.total_bytes)}</span></>}
                      </td>
                      <td><a href="#" onClick={e => { e.preventDefault(); setDetail({ type: 'log', runId: r.id }) }}>anzeigen</a></td>
                    </tr>
                  ))}
                </tbody>
              </table>
            </div>
          )}
        </section>

        <footer>SiteBackup v1.4 · lokale Archivierung eigener Webseiten · MIT</footer>
      </main>

      {/* Modals */}
      {editJob && <JobFormModal job={editJob === 'new' ? null : editJob} onClose={() => setEditJob(null)}
        onSaved={msg => { setEditJob(null); push(msg, 'ok'); load() }} push={push} />}
      {detail?.type === 'discovery' && (
        <DiscoveryModal job={detail.job} data={detail.data} onClose={() => setDetail(null)}
          onRun={j => { setDetail(null); runJob(j) }} />
      )}
      {detail?.type === 'pages' && <PagesModal job={detail.job} data={detail.data} onClose={() => setDetail(null)}
        onPreview={(file, title) => setDetail({ ...detail, type: 'preview', file, title })} />}
      {detail?.type === 'log' && <LogModal runId={detail.runId} onClose={() => setDetail(null)} />}
      {detail?.type === 'preview' && (
        <PreviewModal job={detail.job} file={detail.file} title={detail.title}
          onClose={() => detail.data ? setDetail({ ...detail, type: 'pages' }) : setDetail(null)} />
      )}
      <Toasts toasts={toasts} remove={remove} />
    </>
  )
}

function lastProgress(log) {
  const m = /\[(\d+)\/(\d+)\]/.exec((log || '').trim().split('\n').pop() || '')
  return m ? { done: +m[1], total: +m[2] } : { done: 0, total: 0 }
}

// ---------------------------------------------------------------- Job-Karte
function JobCard({ job, run, onDiscover, onRun, onPages, onEdit, onDelete, push }) {
  const busy = !!run
  const progress = busy && run.log ? lastProgress(run.log) : null
  const url = domainOf(job.start_url)
  return (
    <div className={`jobcard ${busy ? 'busy' : ''}`} id={`job-${job.id}`}>
      <div className="job-head">
        <b>{job.name}</b>
        {!job.enabled && <Pill>deaktiviert</Pill>}
        <div className="job-actions-top">
          <button className="ghost btn-sm" onClick={onEdit} title="Bearbeiten">✏️</button>
          <button className="ghost btn-sm danger-txt" onClick={onDelete} title="Löschen">🗑</button>
        </div>
      </div>
      <a className="job-url" href={job.start_url} target="_blank" rel="noopener">{job.start_url}</a>
      <div className="pills">
        {job.discovered != null
          ? <Pill kind="ok">Discovery: {job.discovered} Seiten</Pill>
          : <Pill>Discovery: –</Pill>}
        {job.last_run
          ? <Pill kind={job.last_run.status === 'ok' ? 'ok' : job.last_run.status === 'running' ? 'run' : 'err'}>
            Backup {job.last_run.status}: {job.last_run.pages_ok} Seiten · {kb(job.last_run.total_bytes)}
          </Pill>
          : <Pill>Backup: –</Pill>}
        <Pill kind="plan">{planTxt(job)}{job.next_run ? ` · nächster: ${fmtDateTime(job.next_run)}` : ''}</Pill>
      </div>
      {busy && progress && progress.total > 0 && (
        <Bar done={progress.done} total={progress.total} />
      )}
      <div className="job-actions">
        <button className="ghost" onClick={onDiscover} disabled={busy}>1. Seiten entdecken</button>
        <button onClick={onRun} disabled={busy}>{busy ? 'läuft …' : '2. Backup starten'}</button>
      </div>
      <div className="job-actions">
        <button className="accent" onClick={() => dl(`/api/jobs/${job.id}/download-html`)} title="Gesamte Website als eine einzige HTML-Datei (offline lesbar)">🌐 1 HTML-Datei</button>
        <button className="ghost" onClick={() => dl(`/api/jobs/${job.id}/download`)} title="Komplettes Archiv als ZIP (HTML + Assets)">🗜 ZIP</button>
        <button className="ghost" onClick={onPages} title="Seiten-Index + Einzelseiten-Vorschau">📄 Seiten</button>
      </div>
    </div>
  )
}

function dl(url) {
  const a = document.createElement('a')
  a.href = url
  a.rel = 'noopener'
  document.body.appendChild(a)
  a.click()
  a.remove()
}

// ---------------------------------------------------------------- Job-Formular (Modal)
function JobFormModal({ job, onClose, onSaved, push }) {
  const [f, setF] = useState({
    name: job?.name || '', start_url: job?.start_url || '',
    max_pages: job?.max_pages ?? 100, max_depth: job?.max_depth ?? 3,
    same_domain: job?.same_domain !== false, schedule: job?.schedule || 'weekly',
    schedule_time: job?.schedule_time || '03:00', schedule_weekday: job?.schedule_weekday || 'mon',
    schedule_day: job?.schedule_day || 1, cron_expr: job?.cron_expr || '', enabled: job?.enabled !== false,
  })
  const [busy, setBusy] = useState(false)
  const set = (k, v) => setF(s => ({ ...s, [k]: v }))

  const save = async (e) => {
    e.preventDefault()
    setBusy(true)
    try {
      if (job) {
        await api(`/api/jobs/${job.id}`, { method: 'PUT', headers: { 'Content-Type': 'application/json' }, body: JSON.stringify(f) })
        onSaved('Job aktualisiert ✓')
      } else {
        await post('/api/jobs', f)
        onSaved('Job gespeichert ✓ – jetzt „Seiten entdecken“ oder direkt „Backup starten“')
      }
    } catch (err) {
      push('Speichern fehlgeschlagen: ' + err.message, 'err')
      setBusy(false)
    }
  }

  return (
    <Modal title={job ? `Job „${job.name}“ bearbeiten` : 'Neuer Backup-Job'} onClose={onClose}>
      <form onSubmit={save}>
        <div className="grid2">
          <label>Name
            <input value={f.name} onChange={e => set('name', e.target.value)} placeholder="z. B. Firmenwebseite" required />
          </label>
          <label>Start-URL
            <input value={f.start_url} onChange={e => set('start_url', e.target.value)} placeholder="https://example.com" required />
          </label>
          <label>Max. Seiten
            <input type="number" min="1" max="5000" value={f.max_pages} onChange={e => set('max_pages', +e.target.value)} />
          </label>
          <label>Max. Tiefe
            <input type="number" min="0" max="10" value={f.max_depth} onChange={e => set('max_depth', +e.target.value)} />
          </label>
          <label>Nur eigene Domain?
            <select value={f.same_domain ? '1' : '0'} onChange={e => set('same_domain', e.target.value === '1')}>
              <option value="1">Ja (empfohlen)</option>
              <option value="0">Nein (folgt externen Links)</option>
            </select>
          </label>
          <label>Zeitplan
            <select value={f.schedule} onChange={e => set('schedule', e.target.value)}>
              <option value="manual">Manuell</option>
              <option value="hourly">Stündlich</option>
              <option value="daily">Täglich</option>
              <option value="weekly">Wöchentlich</option>
              <option value="monthly">Monatlich</option>
              <option value="cron">Cron-Ausdruck</option>
            </select>
          </label>
          {f.schedule !== 'manual' && f.schedule !== 'cron' && (
            <label>Uhrzeit (HH:MM)
              <input value={f.schedule_time} onChange={e => set('schedule_time', e.target.value)} placeholder="03:00" />
            </label>
          )}
          {f.schedule === 'weekly' && (
            <label>Wochentag
              <select value={f.schedule_weekday} onChange={e => set('schedule_weekday', e.target.value)}>
                <option value="mon">Montag</option><option value="tue">Dienstag</option>
                <option value="wed">Mittwoch</option><option value="thu">Donnerstag</option>
                <option value="fri">Freitag</option><option value="sat">Samstag</option>
                <option value="sun">Sonntag</option>
              </select>
            </label>
          )}
          {f.schedule === 'monthly' && (
            <label>Monatstag (1–28)
              <input type="number" min="1" max="28" value={f.schedule_day} onChange={e => set('schedule_day', +e.target.value)} />
            </label>
          )}
          {f.schedule === 'cron' && (
            <label>Cron (m h dom mon dow)
              <input value={f.cron_expr} onChange={e => set('cron_expr', e.target.value)} placeholder="0 3 * * 1" />
            </label>
          )}
          <label>Aktiv?
            <select value={f.enabled ? '1' : '0'} onChange={e => set('enabled', e.target.value === '1')}>
              <option value="1">Ja</option><option value="0">Nein (nur manuell startbar)</option>
            </select>
          </label>
        </div>
        <div className="row" style={{ marginTop: 14 }}>
          <button type="submit" disabled={busy}>{busy ? 'Speichere …' : 'Speichern'}</button>
          <button type="button" className="ghost" onClick={onClose}>Abbrechen</button>
        </div>
      </form>
    </Modal>
  )
}

// ---------------------------------------------------------------- Discovery-Modal
function DiscoveryModal({ job, data, onClose, onRun }) {
  const [q, setQ] = useState('')
  const pages = (data.pages || []).filter(p =>
    !q || (p.title || '').toLowerCase().includes(q.toLowerCase()) || p.url.toLowerCase().includes(q.toLowerCase()))
  const src = { sitemap: 0, link: 0, start: 0 }
  ;(data.pages || []).forEach(p => { if (src[p.source] != null) src[p.source]++ })
  return (
    <Modal title={`Discovery: ${data.count} Unterseiten gefunden`} onClose={onClose} wide>
      <div className="row">
        <button onClick={() => onRun(job)}>2. Alle {data.count} Seiten jetzt sichern</button>
        <span className="hint">{src.sitemap} aus Sitemap · {src.link} via Links · {src.start} Startseite</span>
        <input className="grow" placeholder="Filtern (Titel oder URL) …" value={q} onChange={e => setQ(e.target.value)} />
      </div>
      <div className="twrap">
        <table>
          <thead><tr><th>Nr</th><th>Seitenname</th><th>URL</th><th>Quelle</th></tr></thead>
          <tbody>
            {pages.map(p => (
              <tr key={p.nr}>
                <td>{p.nr}</td>
                <td><b>{p.title || '(ohne Titel)'}</b></td>
                <td className="urltd"><a href={p.url} target="_blank" rel="noopener">{p.url}</a></td>
                <td><Pill kind={'src-' + p.source}>{p.source}</Pill></td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------- Seiten-Index-Modal
function PagesModal({ job, data, onClose, onPreview }) {
  const [q, setQ] = useState('')
  const pages = (data?.pages || []).filter(p =>
    !q || (p.title || '').toLowerCase().includes(q.toLowerCase()) || p.url.toLowerCase().includes(q.toLowerCase()))
  const okCount = pages.filter(p => p.file).length
  return (
    <Modal title={`Seiten von „${job.name}“ (${okCount}/${(data.pages || []).length} gesichert)`} onClose={onClose} wide>
      <div className="row" style={{ marginBottom: 10 }}>
        <button className="accent" onClick={() => dl(`/api/jobs/${job.id}/download-html`)}>🌐 Website als 1 HTML-Datei</button>
        <button className="ghost" onClick={() => dl(`/api/jobs/${job.id}/download`)}>🗜 ZIP (HTML + Assets)</button>
        <a className="ghost btn-like" href={`/api/jobs/${job.id}/index`} target="_blank" rel="noopener">📇 Index</a>
        <input className="grow" placeholder="Filtern (Titel oder URL) …" value={q} onChange={e => setQ(e.target.value)} />
      </div>
      <div className="twrap">
        <table>
          <thead><tr><th>Nr</th><th>Seite</th><th>URL</th><th>Aktionen</th></tr></thead>
          <tbody>
            {pages.map(p => (
              <tr key={p.nr ?? p.url}>
                <td>{p.nr ?? '–'}</td>
                <td><b>{p.title || p.page_name || '(ohne Titel)'}</b>
                  {p.note && <div className="hint errtxt">{String(p.note).slice(0, 100)}</div>}</td>
                <td className="urltd"><a href={p.url} target="_blank" rel="noopener">{p.url}</a></td>
                <td>
                  {p.file
                    ? <div className="row">
                      <button className="ghost btn-sm" onClick={() => onPreview(p.file, p.title)}>👁 Vorschau</button>
                      <button className="ghost btn-sm" onClick={() => dl(`/api/jobs/${job.id}/single-page?url=${encodeURIComponent(p.url)}`)}
                        title="Diese Seite als eine selbstenthaltene HTML-Datei">⬇ HTML</button>
                      <span className="hint">{p.assets || 0} Assets</span>
                    </div>
                    : <span className="hint">–</span>}
                </td>
              </tr>
            ))}
          </tbody>
        </table>
      </div>
    </Modal>
  )
}

// ---------------------------------------------------------------- Log-Modal
function LogModal({ runId, onClose }) {
  const [run, setRun] = useState(null)
  const [err, setErr] = useState('')
  useEffect(() => {
    api(`/api/runs/${runId}`).then(setRun).catch(e => setErr(e.message))
  }, [runId])
  return (
    <Modal title={`Log von Run #${runId}`} onClose={onClose} wide>
      {err && <div className="errtxt">{err}</div>}
      {!run && !err && <div className="hint">Lade …</div>}
      {run && <pre className="log">{run.log || run.error || '(leer)'}</pre>}
    </Modal>
  )
}

// ---------------------------------------------------------------- Vorschau-Modal (iframe)
function PreviewModal({ job, file, title, onClose }) {
  const src = `/api/jobs/${job.id}/preview/${file}`
  return (
    <Modal title={`Vorschau: ${title || file}`} onClose={onClose} wide>
      <div className="row" style={{ marginBottom: 8 }}>
        <a className="ghost btn-like" href={src} target="_blank" rel="noopener">In neuem Tab öffnen ↗</a>
        <span className="hint">So sieht die gesicherte Seite offline aus (inkl. Assets).</span>
      </div>
      <iframe className="preview" src={src} title="Seiten-Vorschau" sandbox="allow-same-origin allow-popups" />
    </Modal>
  )
}
