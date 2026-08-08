import { useCallback, useEffect, useState } from 'react'
import { Download, KeyRound, Minus, NotebookPen, Play, Plus, RefreshCw, RotateCcw, ScrollText, Timer } from 'lucide-react'
import type { BackendInfo, LogEntry, SchedulerConfig, SchedulerInfo, SchedulerJob } from '@shared/types'
import type { BoardMap, Sport } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import { Badge, Card, GhostButton, Page, PrimaryButton, Section, Toggle } from './common'
import { cls } from '../utils/format'
import { ModeSwitch, type UiMode } from '../uiMode'
import { MAX_STAKE, MIN_STAKE, clampStake, defaultStake, saveDefaultStake } from '../prefs'
import { activateLicense, deactivateLicense, fetchLicense, type LicenseStatus } from '../api'

/** Krypt Tools license: activate a key (from the Krypt dashboard) on this
 *  machine. Live auto-betting requires it; everything else works without one. */
function LicenseCard() {
  const [status, setStatus] = useState<LicenseStatus | null>(null)
  const [key, setKey] = useState('')
  const [busy, setBusy] = useState(false)
  const [error, setError] = useState<string | null>(null)

  const refresh = useCallback(async () => setStatus(await fetchLicense()), [])
  useEffect(() => {
    void refresh()
  }, [refresh])

  const activate = async () => {
    if (!key.trim()) return
    setBusy(true)
    setError(null)
    try {
      setStatus(await activateLicense(key.trim()))
      setKey('')
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Activation failed')
    } finally {
      setBusy(false)
    }
  }

  const deactivate = async () => {
    if (!confirm('Deactivate this machine? Live auto-betting will stop until you activate again.')) return
    setBusy(true)
    setError(null)
    try {
      setStatus(await deactivateLicense())
    } catch (e) {
      setError(e instanceof Error ? e.message : 'Deactivation failed')
    } finally {
      setBusy(false)
    }
  }

  const licensed = status?.licensed === true

  return (
    <Card>
      <div className="flex items-center justify-between gap-3">
        <div className="flex items-center gap-2">
          <KeyRound size={15} className="shrink-0 text-scout-red-bright" />
          <span className="text-sm font-medium text-white">Krypt Tools license</span>
        </div>
        <Badge tone={licensed ? 'success' : 'neutral'}>
          {licensed ? 'Active' : 'Not licensed'}
        </Badge>
      </div>

      {licensed ? (
        <div className="mt-3 space-y-2 text-xs text-krypt-muted">
          <div>
            Devices: <span className="text-white">{status?.activations ?? '—'}/{status?.max_seats ?? '—'}</span>
            {status?.expires_at && (
              <>
                {' · '}renews before <span className="text-white">{new Date(status.expires_at).toLocaleDateString()}</span>
              </>
            )}
          </div>
          <GhostButton onClick={deactivate} disabled={busy} className="!px-3 !py-1.5 text-xs">
            {busy ? '…' : 'Deactivate this device'}
          </GhostButton>
        </div>
      ) : (
        <div className="mt-3">
          <div className="flex flex-wrap items-center gap-3">
            <input
              value={key}
              onChange={(e) => setKey(e.target.value)}
              placeholder="KRYPT-XXXX-XXXX-XXXX-XXXX"
              spellCheck={false}
              className="min-w-[260px] flex-1 rounded-xl border border-white/10 bg-black/30 px-3 py-2 font-mono text-xs text-white placeholder:font-sans placeholder:text-krypt-muted/50 focus:border-scout-red/40 focus:outline-none focus:ring-1 focus:ring-scout-red/30"
            />
            <PrimaryButton onClick={activate} disabled={busy || !key.trim()} className="!px-3 !py-2 text-xs">
              {busy ? 'Activating…' : 'Activate'}
            </PrimaryButton>
          </div>
          <p className="mt-2 text-[11px] text-krypt-muted/70">
            Generate a key on the Krypt dashboard → Utility → Krypt Premium. Live auto-betting needs it; the board,
            picks, tracker and dry-run work without one.
          </p>
        </div>
      )}
      {error && <p className="mt-2 text-[11px] text-rose-300">{error}</p>}
    </Card>
  )
}

const statusTone: Record<string, string> = {
  running: 'bg-emerald-400 shadow-[0_0_8px_rgba(52,211,153,0.8)]',
  starting: 'bg-amber-400 animate-pulse-slow',
  restarting: 'bg-amber-400 animate-pulse-slow',
  crashed: 'bg-rose-500 shadow-[0_0_8px_rgba(244,63,94,0.8)]',
  stopped: 'bg-white/20',
}

function fmtClock(d: Date | undefined): string {
  return d ? d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', second: '2-digit', hour12: false }) : '—'
}

const levelColor = (level: string): string =>
  /err|crit/i.test(level) ? 'text-rose-400' : /warn/i.test(level) ? 'text-amber-300' : 'text-krypt-muted'

/** "in 3h 42m" style countdown to an ISO timestamp. */
function fmtIn(iso: string | null | undefined, now: number): string {
  if (!iso) return '—'
  const ms = new Date(iso).getTime() - now
  if (!Number.isFinite(ms)) return '—'
  if (ms <= 30_000) return 'due now'
  const mins = Math.round(ms / 60_000)
  if (mins < 1) return 'in <1m'
  const h = Math.floor(mins / 60)
  return h > 0 ? `in ${h}h ${mins % 60}m` : `in ${mins}m`
}

/** "3h 12m ago" style age of an ISO timestamp. */
function fmtAgo(iso: string, now: number): string {
  const ms = now - new Date(iso).getTime()
  if (!Number.isFinite(ms) || ms < 0) return '—'
  const mins = Math.floor(ms / 60_000)
  if (mins < 1) return 'just now'
  if (mins < 60) return `${mins}m ago`
  const h = Math.floor(mins / 60)
  if (h < 24) return `${h}h ${mins % 60}m ago`
  return `${Math.floor(h / 24)}d ago`
}

const CAPTURE_INTERVALS = [2, 4, 8, 12]
const SETTLE_INTERVALS = [3, 6, 12, 24]
const REFRESH_INTERVALS = [12, 24, 48, 72]

const intervalSelectCls =
  'rounded-lg border border-white/10 bg-black/30 px-2 py-1.5 font-mono text-[11px] text-white ' +
  'focus:border-scout-red/40 focus:outline-none focus:ring-1 focus:ring-scout-red/30 ' +
  'disabled:cursor-not-allowed disabled:opacity-40 [&>option]:bg-krypt-panel'

interface Props {
  boards: BoardMap
  demo: boolean
  apiUp: boolean | null
  backend: BackendInfo | null
  sportFetchedAt: Partial<Record<Sport, Date>>
  onToggleDemo: () => void
  onRefreshSport: (s: Sport) => Promise<void>
  mode: UiMode
  onMode: (m: UiMode) => void
}

/** SETTINGS — per-sport data freshness, backend supervisor, appearance. */
export default function SettingsPage({
  boards,
  demo,
  apiUp,
  backend,
  sportFetchedAt,
  onToggleDemo,
  onRefreshSport,
  mode,
  onMode,
}: Props) {
  // Default stake for every TRACK action (bets + parlays).
  const [stake, setStakeState] = useState(() => defaultStake())
  const setStake = (v: number) => {
    const clamped = clampStake(v)
    saveDefaultStake(clamped)
    setStakeState(clamped)
  }

  // System behavior (Electron only): launch-at-login + close-to-tray.
  const [sysCfg, setSysCfg] = useState<{ autoStartOnLogin: boolean; closeToTray: boolean } | null>(null)
  useEffect(() => {
    if (!window.scout?.app.systemConfig) return
    void window.scout.app.systemConfig().then(setSysCfg).catch(() => undefined)
  }, [])
  const patchSys = (partial: Partial<{ autoStartOnLogin: boolean; closeToTray: boolean }>) => {
    if (!window.scout?.app.setSystemConfig) return
    void window.scout.app.setSystemConfig(partial).then(setSysCfg).catch(() => undefined)
  }

  // ---- per-sport refresh ----
  const [busy, setBusy] = useState<Partial<Record<Sport, boolean>>>({})
  const refreshSport = useCallback(
    async (s: Sport) => {
      setBusy((b) => ({ ...b, [s]: true }))
      try {
        await onRefreshSport(s)
      } finally {
        setBusy((b) => ({ ...b, [s]: false }))
      }
    },
    [onRefreshSport],
  )

  // ---- backend logs ----
  const [logs, setLogs] = useState<LogEntry[] | null>(null)
  const [logsBusy, setLogsBusy] = useState(false)
  const loadLogs = useCallback(async () => {
    if (!window.scout) return
    setLogsBusy(true)
    try {
      setLogs(await window.scout.backend.logs(200))
    } finally {
      setLogsBusy(false)
    }
  }, [])
  useEffect(() => {
    void loadLogs()
  }, [loadLogs])

  // ---- export logs (to a user-chosen file, for support) ----
  const [exportMsg, setExportMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [exporting, setExporting] = useState(false)
  const exportLogs = useCallback(async () => {
    if (!window.scout?.backend.exportLogs || exporting) return
    setExporting(true)
    setExportMsg(null)
    try {
      const res = await window.scout.backend.exportLogs()
      if (res.ok) setExportMsg({ ok: true, text: `Saved to ${res.path}` })
      else if (res.canceled) setExportMsg(null)
      else setExportMsg({ ok: false, text: res.error || 'Export failed' })
    } catch (err) {
      setExportMsg({ ok: false, text: (err as Error)?.message || 'Export failed' })
    } finally {
      setExporting(false)
    }
  }, [exporting])

  const [restarting, setRestarting] = useState(false)
  const restart = async () => {
    if (!window.scout) return
    setRestarting(true)
    try {
      await window.scout.backend.restart()
    } finally {
      setTimeout(() => setRestarting(false), 1500)
    }
  }

  const status = backend?.status ?? (window.scout ? 'stopped' : 'running')

  // ---- automation (in-app job scheduler) ----
  const schedApi = window.scout?.scheduler
  const [sched, setSched] = useState<SchedulerInfo | null>(null)
  const [schedBusy, setSchedBusy] = useState<SchedulerJob | null>(null)
  const [now, setNow] = useState(() => Date.now())

  useEffect(() => {
    if (!schedApi) return
    let alive = true
    const pull = async () => {
      try {
        const i = await schedApi.info()
        if (alive) setSched(i)
      } catch {
        /* main process unavailable — leave the last snapshot */
      }
    }
    void pull()
    const poll = setInterval(() => void pull(), 30_000)
    const tick = setInterval(() => setNow(Date.now()), 15_000)
    return () => {
      alive = false
      clearInterval(poll)
      clearInterval(tick)
    }
  }, [schedApi])

  const updateSched = useCallback(
    async (partial: Partial<SchedulerConfig>) => {
      if (!schedApi) return
      try {
        setSched(await schedApi.setConfig(partial))
      } catch {
        /* keep previous snapshot */
      }
    },
    [schedApi],
  )

  const runSchedNow = useCallback(
    async (job: SchedulerJob) => {
      if (!schedApi || schedBusy) return
      setSchedBusy(job)
      try {
        setSched(await schedApi.runNow(job))
      } catch {
        /* run outcome still lands in lastRuns on the next poll */
      } finally {
        setSchedBusy(null)
        setNow(Date.now())
      }
    },
    [schedApi, schedBusy],
  )

  const schedCfg = sched?.config
  const jobRunning = schedBusy ?? sched?.inFlight ?? null

  return (
    <Page title="Settings" subtitle="Odds provider, data freshness, backend supervisor and terminal preferences.">
      {/* LICENSE */}
      <Section
        title="License"
        description="Your Krypt Tools license on this machine. Deactivating frees the seat for another device."
      >
        <LicenseCard />
      </Section>

      {/* DATA */}
      <Section title="Data" description="Per-sport board freshness — force a refetch of any single board.">
        <Card padded={false} className="overflow-hidden">
          <table className="w-full border-collapse text-left">
            <thead>
              <tr className="border-b border-white/10 bg-white/[0.02]">
                <th className="px-4 py-2 text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70">Board</th>
                <th className="px-4 py-2 text-right text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70">Games</th>
                <th className="px-4 py-2 text-right text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70">Odds books</th>
                <th className="px-4 py-2 text-right text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70">Last refreshed</th>
                <th className="px-4 py-2 text-right text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70" />
              </tr>
            </thead>
            <tbody>
              {SPORTS.map((s) => {
                const cards = boards[s] ?? []
                const books = new Set(cards.flatMap((c) => c.odds.map((o) => o.book))).size
                return (
                  <tr key={s} className="border-b border-white/5 last:border-0">
                    <td className="px-4 py-2 text-[10px] font-bold uppercase tracking-widest text-white/85">
                      {SPORT_TAG[s]}
                    </td>
                    <td className="px-4 py-2 text-right font-mono text-[11px] text-white/80">{cards.length}</td>
                    <td className="px-4 py-2 text-right font-mono text-[11px] text-krypt-muted">{books}</td>
                    <td className="px-4 py-2 text-right font-mono text-[10px] text-krypt-muted">
                      {fmtClock(sportFetchedAt[s])}
                    </td>
                    <td className="px-4 py-2 text-right">
                      <button
                        onClick={() => void refreshSport(s)}
                        disabled={!!busy[s] || demo}
                        title={demo ? 'Disabled in demo mode' : `Refetch /api/board/${s}`}
                        className="inline-flex items-center gap-1.5 rounded-lg border border-white/10 bg-white/[0.03] px-2 py-1 text-[9px] font-bold tracking-widest text-krypt-muted transition hover:border-white/20 hover:text-white disabled:cursor-not-allowed disabled:opacity-40"
                      >
                        <RefreshCw size={10} className={busy[s] ? 'animate-spin' : undefined} />
                        REFRESH
                      </button>
                    </td>
                  </tr>
                )
              })}
            </tbody>
          </table>
        </Card>
      </Section>

      {/* BACKEND */}
      <Section title="Backend" description="The bundled Python engine, supervised by the Electron shell.">
        <Card>
          <div className="flex flex-wrap items-center gap-x-5 gap-y-2">
            <span className="flex items-center gap-2 text-xs font-semibold uppercase tracking-wider text-white/85">
              <span className={cls('h-2 w-2 rounded-full', statusTone[status] ?? 'bg-white/20')} />
              {status}
            </span>
            <span className="font-mono text-[11px] text-krypt-muted">
              PORT <span className="text-white/80">{backend?.port ?? '—'}</span>
            </span>
            <span className="font-mono text-[11px] text-krypt-muted">
              PID <span className="text-white/80">{backend?.pid ?? '—'}</span>
            </span>
            <span className="font-mono text-[11px] text-krypt-muted">
              API <span className={apiUp ? 'text-emerald-300' : 'text-rose-400'}>{apiUp === null ? '—' : apiUp ? 'HEALTHY' : 'DOWN'}</span>
            </span>
            <div className="ml-auto flex items-center gap-2">
              <GhostButton
                onClick={() => void restart()}
                disabled={!window.scout || restarting}
                className="!px-3 !py-1.5 !text-xs"
              >
                <RotateCcw size={12} className={restarting ? 'animate-spin' : undefined} />
                Restart backend
              </GhostButton>
            </div>
          </div>
          {backend?.lastError && (
            <p className="mt-2 font-mono text-[10px] text-rose-400">{backend.lastError}</p>
          )}
          {!window.scout && (
            <p className="mt-2 text-[10px] uppercase tracking-wider text-krypt-muted/60">
              Browser dev mode — supervisor controls available inside the Electron app only.
            </p>
          )}

          <div className="mt-4">
            <div className="mb-1.5 flex items-center gap-2">
              <ScrollText size={12} className="text-scout-red-bright" />
              <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
                Backend log
              </span>
              <span className="text-[10px] text-krypt-muted/60">last 200 lines</span>
              <div className="ml-auto flex items-center gap-2">
                <button
                  onClick={() => void exportLogs()}
                  disabled={!window.scout || exporting}
                  title="Save the full backend log history (plus environment info) to a file you can share for support"
                  className="inline-flex items-center gap-1 rounded-lg border border-white/10 bg-white/[0.03] px-2 py-1 text-[9px] font-bold tracking-widest text-krypt-muted transition hover:border-white/20 hover:text-white disabled:opacity-40"
                >
                  <Download size={9} className={exporting ? 'animate-pulse' : undefined} />
                  EXPORT
                </button>
                <button
                  onClick={() => void loadLogs()}
                  disabled={!window.scout || logsBusy}
                  className="inline-flex items-center gap-1 rounded-lg border border-white/10 bg-white/[0.03] px-2 py-1 text-[9px] font-bold tracking-widest text-krypt-muted transition hover:border-white/20 hover:text-white disabled:opacity-40"
                >
                  <RefreshCw size={9} className={logsBusy ? 'animate-spin' : undefined} />
                  RELOAD
                </button>
              </div>
            </div>
            {exportMsg && (
              <div
                className={cls(
                  'mb-1.5 break-all rounded-lg border px-2.5 py-1.5 font-mono text-[10px]',
                  exportMsg.ok
                    ? 'border-emerald-400/25 bg-emerald-500/[0.08] text-emerald-200'
                    : 'border-rose-400/25 bg-rose-500/[0.08] text-rose-200',
                )}
              >
                {exportMsg.text}
              </div>
            )}
            <div className="max-h-64 overflow-y-auto rounded-xl border border-white/10 bg-black/40 p-3">
              {!window.scout ? (
                <div className="text-[10px] uppercase tracking-wider text-krypt-muted/50">
                  No supervisor in browser dev mode
                </div>
              ) : logs === null ? (
                <div className="text-[10px] uppercase tracking-wider text-krypt-muted/50">Loading…</div>
              ) : logs.length === 0 ? (
                <div className="text-[10px] uppercase tracking-wider text-krypt-muted/50">
                  Log buffer empty
                </div>
              ) : (
                logs.map((l, i) => (
                  <div key={i} className="whitespace-pre-wrap font-mono text-[10px] leading-4">
                    <span className="text-krypt-muted/40">{l.ts.slice(11, 19)} </span>
                    <span className={levelColor(l.level)}>{l.level.toUpperCase().padEnd(5)} </span>
                    <span className="text-krypt-muted/60">{l.source} </span>
                    <span className="text-white/75">{l.msg}</span>
                  </div>
                ))
              )}
            </div>
          </div>
        </Card>
      </Section>

      {/* AUTOMATION */}
      <Section
        title="Automation"
        description="Background jobs keep prediction & odds history growing — no Windows Task Scheduler needed."
      >
        <Card>
          {!schedApi ? (
            <p className="text-[10px] uppercase tracking-wider text-krypt-muted/60">
              Automation runs inside the desktop app — browser dev mode has no scheduler.
            </p>
          ) : (
            <>
              <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
                <div className="flex items-center gap-3">
                  <Toggle
                    checked={schedCfg?.enabled ?? true}
                    onChange={() => void updateSched({ enabled: !(schedCfg?.enabled ?? true) })}
                    label="Scheduled jobs"
                  />
                  <div>
                    <div className="text-sm font-semibold text-white">Scheduled jobs</div>
                    <div className="text-xs text-krypt-muted">
                      Capture odds/prediction snapshots, settle finished picks and refresh engine
                      data + stale models automatically.
                    </div>
                  </div>
                </div>
                <label className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-krypt-muted">
                  Capture every
                  <select
                    value={schedCfg?.captureEveryHours ?? 4}
                    onChange={(e) => void updateSched({ captureEveryHours: Number(e.target.value) })}
                    disabled={!schedCfg}
                    className={intervalSelectCls}
                  >
                    {CAPTURE_INTERVALS.map((h) => (
                      <option key={h} value={h}>{h}h</option>
                    ))}
                  </select>
                </label>
                <label className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-krypt-muted">
                  Settle every
                  <select
                    value={schedCfg?.settleEveryHours ?? 6}
                    onChange={(e) => void updateSched({ settleEveryHours: Number(e.target.value) })}
                    disabled={!schedCfg}
                    className={intervalSelectCls}
                  >
                    {SETTLE_INTERVALS.map((h) => (
                      <option key={h} value={h}>{h}h</option>
                    ))}
                  </select>
                </label>
                <label className="flex items-center gap-2 text-[10px] font-semibold uppercase tracking-widest text-krypt-muted">
                  Refresh every
                  <select
                    value={schedCfg?.refreshEveryHours ?? 24}
                    onChange={(e) => void updateSched({ refreshEveryHours: Number(e.target.value) })}
                    disabled={!schedCfg}
                    className={intervalSelectCls}
                  >
                    {REFRESH_INTERVALS.map((h) => (
                      <option key={h} value={h}>{h}h</option>
                    ))}
                  </select>
                </label>
                <div className="ml-auto flex items-center gap-2">
                  <GhostButton
                    onClick={() => void runSchedNow('capture')}
                    disabled={jobRunning !== null}
                    className="!px-3 !py-1.5 !text-xs"
                  >
                    {jobRunning === 'capture'
                      ? <RefreshCw size={12} className="animate-spin" />
                      : <Play size={12} />}
                    Run capture now
                  </GhostButton>
                  <GhostButton
                    onClick={() => void runSchedNow('settle')}
                    disabled={jobRunning !== null}
                    className="!px-3 !py-1.5 !text-xs"
                  >
                    {jobRunning === 'settle'
                      ? <RefreshCw size={12} className="animate-spin" />
                      : <Play size={12} />}
                    Run settle now
                  </GhostButton>
                  <GhostButton
                    onClick={() => void runSchedNow('refresh')}
                    disabled={jobRunning !== null}
                    className="!px-3 !py-1.5 !text-xs"
                  >
                    {jobRunning === 'refresh'
                      ? <RefreshCw size={12} className="animate-spin" />
                      : <Play size={12} />}
                    Run refresh now
                  </GhostButton>
                </div>
              </div>

              <div className="mt-3 flex flex-wrap items-center gap-x-5 gap-y-1 border-t border-white/5 pt-3">
                <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-widest text-krypt-muted">
                  <Timer size={11} className="text-scout-red-bright" />
                  Next capture
                  <span className="font-mono text-[11px] normal-case tracking-normal text-white/80">
                    {schedCfg?.enabled ? fmtIn(sched?.nextRuns.capture, now) : 'paused'}
                  </span>
                </span>
                <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-widest text-krypt-muted">
                  <Timer size={11} className="text-scout-red-bright" />
                  Next settle
                  <span className="font-mono text-[11px] normal-case tracking-normal text-white/80">
                    {schedCfg?.enabled ? fmtIn(sched?.nextRuns.settle, now) : 'paused'}
                  </span>
                </span>
                <span className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-widest text-krypt-muted">
                  <Timer size={11} className="text-scout-red-bright" />
                  Next refresh
                  <span className="font-mono text-[11px] normal-case tracking-normal text-white/80">
                    {schedCfg?.enabled ? fmtIn(sched?.nextRuns.refresh, now) : 'paused'}
                  </span>
                </span>
                {jobRunning && <Badge tone="gradient">RUNNING {jobRunning.toUpperCase()}</Badge>}
              </div>

              {apiUp === false && (
                <p className="mt-2 text-[10px] uppercase tracking-wider text-amber-300/90">
                  Backend API is unreachable — scheduled runs will record failures until it recovers.
                </p>
              )}

              <div className="mt-4">
                <div className="mb-1.5 text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
                  Recent runs
                </div>
                {!sched || sched.lastRuns.length === 0 ? (
                  <div className="rounded-xl border border-white/10 bg-black/40 p-3 text-[10px] uppercase tracking-wider text-krypt-muted/50">
                    No runs yet — the first settle + capture fires ~90s after the backend comes up.
                  </div>
                ) : (
                  <div className="max-h-48 overflow-y-auto rounded-xl border border-white/10 bg-black/40">
                    {sched.lastRuns.map((r, i) => (
                      <div
                        key={`${r.startedAt}-${r.job}-${i}`}
                        className="flex items-center gap-3 border-b border-white/5 px-3 py-1.5 last:border-0"
                      >
                        <span className="w-16 shrink-0 font-mono text-[10px] font-bold uppercase tracking-widest text-white/85">
                          {r.job}
                        </span>
                        <span className="w-24 shrink-0 font-mono text-[10px] text-krypt-muted">
                          {fmtAgo(r.startedAt, now)}
                        </span>
                        <Badge tone={r.ok ? 'success' : 'danger'}>{r.ok ? 'OK' : 'ERR'}</Badge>
                        <span
                          className={cls(
                            'min-w-0 flex-1 truncate font-mono text-[10px]',
                            r.ok ? 'text-krypt-muted' : 'text-rose-400/90',
                          )}
                          title={r.summary ?? r.error}
                        >
                          {r.summary ?? r.error ?? '—'}
                        </span>
                      </div>
                    ))}
                  </div>
                )}
              </div>
            </>
          )}
        </Card>
      </Section>

      {/* APPEARANCE */}
      {/* TRACKING */}
      <Section
        title="Tracking"
        description="How TRACK buttons log bets and parlays to your tracker."
      >
        <Card>
          <div className="flex flex-wrap items-center gap-3">
            <NotebookPen size={15} className="shrink-0 text-scout-red-bright" />
            <div className="flex items-center overflow-hidden rounded-xl border border-white/10 bg-black/30">
              <button
                onClick={() => setStake(stake - (stake > 1 ? 0.5 : 0.1))}
                disabled={stake <= MIN_STAKE}
                title="Smaller default stake"
                className="px-2.5 py-2 text-krypt-muted transition hover:bg-white/5 hover:text-white disabled:cursor-not-allowed disabled:opacity-30"
              >
                <Minus size={12} />
              </button>
              <span className="min-w-[64px] px-1 text-center font-mono text-sm font-bold text-white">
                {stake}u
              </span>
              <button
                onClick={() => setStake(stake + (stake >= 1 ? 0.5 : 0.1))}
                disabled={stake >= MAX_STAKE}
                title="Bigger default stake"
                className="px-2.5 py-2 text-krypt-muted transition hover:bg-white/5 hover:text-white disabled:cursor-not-allowed disabled:opacity-30"
              >
                <Plus size={12} />
              </button>
            </div>
            <div className="min-w-[240px] flex-1">
              <div className="text-sm font-semibold text-white">Default stake</div>
              <div className="text-xs text-krypt-muted">
                Every TRACK button — straight bets and parlays — logs at this many units
                ({MIN_STAKE}–{MAX_STAKE}). Bets you already tracked keep the stake they were
                tracked at; to change one before it grades, delete and re-track it.
              </div>
            </div>
            {stake !== 1 && (
              <GhostButton onClick={() => setStake(1)} className="!px-3 !py-2 text-xs">
                Reset to 1u
              </GhostButton>
            )}
          </div>
        </Card>
      </Section>

      {/* SYSTEM — tray + startup (Electron only) */}
      {sysCfg && (
        <Section
          title="System"
          description="Scout runs best as an always-on companion — the scheduler, auto-posts and auto-bet all live in this app."
        >
          <Card>
            <div className="flex items-center gap-3">
              <Toggle
                checked={sysCfg.autoStartOnLogin}
                onChange={() => patchSys({ autoStartOnLogin: !sysCfg.autoStartOnLogin })}
                label="Launch at startup"
              />
              <div>
                <div className="text-sm font-semibold text-white">Launch at startup</div>
                <div className="text-xs text-krypt-muted">
                  Boots with Windows, silently minimized to the system tray — captures, posts and
                  auto-bet sweeps run without you opening anything.
                </div>
              </div>
            </div>
            <div className="mt-4 flex items-center gap-3 border-t border-white/5 pt-4">
              <Toggle
                checked={sysCfg.closeToTray}
                onChange={() => patchSys({ closeToTray: !sysCfg.closeToTray })}
                label="Close to tray"
              />
              <div>
                <div className="text-sm font-semibold text-white">Close to tray</div>
                <div className="text-xs text-krypt-muted">
                  The X button hides Scout to the tray instead of quitting, so background jobs keep
                  running. Quit for real from the tray icon&apos;s menu.
                </div>
              </div>
            </div>
          </Card>
        </Section>
      )}

      <Section title="Appearance" description="Terminal presentation preferences.">
        <Card>
          <div className="flex items-center gap-3">
            <ModeSwitch mode={mode} onMode={onMode} size="md" />
            <div>
              <div className="text-sm font-semibold text-white">Interface mode</div>
              <div className="text-xs text-krypt-muted">
                SIMPLE keeps it readable: picks in plain English, no stats jargon. PRO shows
                every page and every number (value scanner, performance, intel, Kelly sizing).
              </div>
            </div>
          </div>
          <div className="mt-4 flex items-center gap-3 border-t border-white/5 pt-4">
            <Toggle checked={demo} onChange={onToggleDemo} label="Demo mode" />
            <div>
              <div className="text-sm font-semibold text-white">Demo mode</div>
              <div className="text-xs text-krypt-muted">
                Render the bundled sample slate instead of the live feed. Auto-enables while the API is
                unreachable.
              </div>
            </div>
            {demo && <Badge tone="gradient">DEMO</Badge>}
          </div>
        </Card>
      </Section>
    </Page>
  )
}
