/**
 * AUTO-BET — the Polymarket auto-trading panel (kryptscout/autobet).
 *
 * Polymarket-only by design (it's the one book with a tradable API and the
 * sharpest vig-free prices on the board), so the page carries the Polymarket
 * referral link up top. Real money: the backend defaults to disabled +
 * dry-run, and this UI keeps both switches explicit — nothing trades until
 * the user connects a wallet, turns the engine ON, and turns dry-run OFF.
 * The wallet key is write-only: it goes to the backend's encrypted
 * credential store and never comes back.
 */
import { useCallback, useEffect, useState } from 'react'
import {
  ArrowUpRight,
  Bot,
  Download,
  KeyRound,
  Loader2,
  Play,
  RefreshCw,
  ShieldAlert,
  Wallet,
  X,
} from 'lucide-react'
import type {
  AutobetAttempt,
  AutobetSettings,
  AutobetStatus,
  AutobetStrategies,
  AutobetSweepSummary,
} from '../api'
import type { AutobetDrift } from '../api'
import {
  applyAutobetProfile,
  clearAutobetWallet,
  deleteAutobetProfile,
  fetchAutobet,
  fetchAutobetDrift,
  fetchAutobetLog,
  fetchAutobetProfiles,
  runAutobet,
  saveAutobetProfile,
  saveAutobetSettings,
  saveAutobetWallet,
  testAutobetWallet,
} from '../api'
import { openBook } from '../config/referrals'
import { fmtKick } from '../cardview'
import { Badge, Card, GhostButton, Page, PrimaryButton, Toggle } from './common'
import { cls } from '../utils/format'
import { useUiMode } from '../uiMode'

const TH = 'px-3 py-2 text-left text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70'
const TD = 'px-3 py-1.5 font-mono text-[11px]'
const REFRESH_MS = 60_000

function StatusChip({ status }: { status: AutobetAttempt['status'] }) {
  const tone: Record<AutobetAttempt['status'], string> = {
    placed: 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300',
    dry: 'border-sky-400/30 bg-sky-500/10 text-sky-300',
    unmatched: 'border-amber-400/30 bg-amber-500/10 text-amber-300',
    error: 'border-rose-400/30 bg-rose-500/10 text-rose-300',
  }
  return (
    <span
      className={cls(
        'inline-flex items-center rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest',
        tone[status],
      )}
    >
      {status}
    </span>
  )
}

/** Small numeric field: local text state, commits a parsed value on blur/Enter. */
function NumField({
  label,
  value,
  suffix,
  onCommit,
  width = 'w-20',
  title,
}: {
  label: string
  value: number
  suffix?: string
  onCommit: (v: number) => void
  width?: string
  title?: string
}) {
  const [text, setText] = useState(String(value))
  useEffect(() => setText(String(value)), [value])
  const commit = () => {
    const parsed = parseFloat(text)
    if (Number.isFinite(parsed) && parsed !== value) onCommit(parsed)
    else setText(String(value))
  }
  return (
    <label className="flex items-center gap-2" title={title}>
      <span className="text-[10px] font-semibold uppercase tracking-wider text-krypt-muted">
        {label}
      </span>
      <span className="flex items-center overflow-hidden rounded-lg border border-white/10 bg-black/30">
        <input
          value={text}
          onChange={(e) => setText(e.target.value)}
          onBlur={commit}
          onKeyDown={(e) => e.key === 'Enter' && (e.target as HTMLInputElement).blur()}
          className={cls('bg-transparent px-2 py-1 text-right font-mono text-xs text-white outline-none', width)}
        />
        {suffix && <span className="pr-2 font-mono text-[10px] text-krypt-muted/60">{suffix}</span>}
      </span>
    </label>
  )
}

function sweepLine(s: AutobetSweepSummary): string {
  const skipped = Object.entries(s.skipped ?? {})
    .map(([k, n]) => `${k} ×${n}`)
    .join(', ')
  const mode = s.dry_run ? 'dry-run' : 'live'
  const parts = [
    `${s.scanned} scanned`,
    s.placed ? `${s.placed} placed` : '',
    s.dry ? `${s.dry} dry` : '',
    s.unmatched ? `${s.unmatched} unfilled` : '',
    s.errors ? `${s.errors} errors` : '',
    skipped ? `skipped: ${skipped}` : '',
  ].filter(Boolean)
  return `${mode} · ${parts.join(' · ')}`
}

export default function AutobetPage({ demo, apiUp }: { demo: boolean; apiUp: boolean | null }) {
  const simple = useUiMode() === 'simple'
  const [status, setStatus] = useState<AutobetStatus | null>(null)
  const [log, setLog] = useState<AutobetAttempt[]>([])
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)

  // Live-vs-backtest drift (advisory; fetched once — the expected side
  // replays backtests server-side, so no polling)
  const [drift, setDrift] = useState<AutobetDrift | null>(null)
  useEffect(() => {
    let alive = true
    void fetchAutobetDrift().then((d) => alive && setDrift(d))
    return () => {
      alive = false
    }
  }, [])

  // Strategy presets + saved profiles
  const [strategies, setStrategies] = useState<AutobetStrategies | null>(null)
  const [profileName, setProfileName] = useState('')
  const [profileMsg, setProfileMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // Wallet form
  const [pk, setPk] = useState('')
  const [funder, setFunder] = useState('')
  const [walletMsg, setWalletMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [disarmTimer, setDisarmTimer] = useState<number | null>(null)
  const [disconnectArmed, setDisconnectArmed] = useState(false)

  // Export backend logs — surfaced here too, since "backend unreachable" is the
  // exact state where a user needs to grab logs and this page is where they land.
  const [exportMsg, setExportMsg] = useState<{ ok: boolean; text: string } | null>(null)
  const [exporting, setExporting] = useState(false)
  const exportLogs = async () => {
    if (!window.scout?.backend.exportLogs || exporting) return
    setExporting(true)
    setExportMsg(null)
    try {
      const res = await window.scout.backend.exportLogs()
      if (res.ok) setExportMsg({ ok: true, text: `Saved to ${res.path}` })
      else if (!res.canceled) setExportMsg({ ok: false, text: res.error || 'Export failed' })
    } catch (err) {
      setExportMsg({ ok: false, text: (err as Error)?.message || 'Export failed' })
    } finally {
      setExporting(false)
    }
  }

  const refresh = useCallback(async () => {
    const [s, l] = await Promise.all([fetchAutobet(), fetchAutobetLog(50)])
    if (s) setStatus(s)
    if (l) setLog(l)
    setLoaded(true)
  }, [])

  useEffect(() => {
    void refresh()
    void fetchAutobetProfiles().then((st) => st && setStrategies(st))
    const t = window.setInterval(() => void refresh(), REFRESH_MS)
    return () => window.clearInterval(t)
  }, [refresh])
  useEffect(
    () => () => {
      if (disarmTimer !== null) window.clearTimeout(disarmTimer)
    },
    [disarmTimer],
  )

  const patch = async (p: Partial<AutobetSettings>) => {
    const saved = await saveAutobetSettings(p)
    if (saved && status) setStatus({ ...status, settings: saved })
  }

  // Load a preset/profile's rules into the live settings (never arms — the
  // backend strips enabled/dry_run). Then the form reflects the loaded rules.
  const applyStrategy = async (name: string) => {
    const saved = await applyAutobetProfile(name)
    if (saved && status) setStatus({ ...status, settings: saved })
    setProfileMsg({ ok: !!saved, text: saved ? `Loaded “${name}”` : `Couldn't load “${name}”` })
  }

  const saveStrategy = async () => {
    const name = profileName.trim()
    if (!name) return
    const res = await saveAutobetProfile(name)
    if (res.error) {
      setProfileMsg({ ok: false, text: res.error })
      return
    }
    if (res.strategies) setStrategies(res.strategies)
    setProfileName('')
    setProfileMsg({ ok: true, text: `Saved “${name}”` })
  }

  const deleteStrategy = async (name: string) => {
    const st = await deleteAutobetProfile(name)
    if (st) setStrategies(st)
  }

  const connect = async () => {
    if (!pk.trim() || busy) return
    setBusy(true)
    setWalletMsg(null)
    const res = await saveAutobetWallet({ private_key: pk.trim(), funder: funder.trim() || null })
    setBusy(false)
    if (res.error) {
      setWalletMsg({ ok: false, text: res.error })
      return
    }
    setPk('')
    setWalletMsg({ ok: true, text: 'Wallet saved (encrypted). Readiness check ran — see TEST for details.' })
    void refresh()
  }

  const test = async () => {
    if (busy) return
    setBusy(true)
    setWalletMsg(null)
    const res = await testAutobetWallet()
    setBusy(false)
    if (res.error) {
      setWalletMsg({ ok: false, text: res.error })
      return
    }
    // get_balance serves CENTS: {balance: cash_cents, portfolio_value: cents|null}.
    const bal = res.balance as { balance?: number; portfolio_value?: number | null } | undefined
    const cash = typeof bal?.balance === 'number' ? bal.balance / 100 : undefined
    const ready = (res.ready as { ok?: boolean; ready?: boolean } | undefined) ?? {}
    const isReady = ready.ok === true || ready.ready === true
    setWalletMsg({
      ok: isReady,
      text: `${isReady ? 'Trading ready' : 'Not ready — see backend logs'}${
        cash !== undefined ? ` · cash $${cash.toFixed(2)}` : ''
      }`,
    })
  }

  const disconnect = async () => {
    if (!disconnectArmed) {
      setDisconnectArmed(true)
      if (disarmTimer !== null) window.clearTimeout(disarmTimer)
      setDisarmTimer(window.setTimeout(() => setDisconnectArmed(false), 3000))
      return
    }
    setDisconnectArmed(false)
    await clearAutobetWallet()
    setWalletMsg(null)
    void refresh()
  }

  const runNow = async () => {
    if (busy) return
    setBusy(true)
    const summary = await runAutobet()
    setBusy(false)
    if (summary && status) setStatus({ ...status, last_sweep: summary })
    void refresh()
  }

  const s = status?.settings
  const wallet = status?.wallet
  const today = status?.today
  const pnl = status?.pnl
  const stopped = status?.stopped ?? null
  const live = !!s && s.enabled && !s.dry_run
  const stopLabel: Record<string, string> = {
    daily_stop_loss: 'DAILY STOP-LOSS HIT — halted until tomorrow (UTC)',
    daily_stop_profit: 'DAILY TAKE-PROFIT HIT — halted until tomorrow (UTC)',
    stop_loss: 'STOP-LOSS HIT — halted until you raise or clear the stop',
    stop_profit: 'STOP-PROFIT HIT — halted until you raise or clear the stop',
  }

  return (
    <Page
      title="Auto-Bet"
      subtitle={
        simple
          ? 'Let the bot place its own picks on Polymarket — your wallet, your caps, off until you say go.'
          : 'Tail the bot’s value picks with real Polymarket CLOB orders — EV-floor FAK pricing, per-day caps, dry-run first.'
      }
      actions={
        <>
          {demo && <Badge tone="gradient">DEMO</Badge>}
          {apiUp === false && !demo && <Badge tone="danger">API DOWN</Badge>}
          <button
            onClick={() => openBook('polymarket')}
            title="Polymarket — the only book with a tradable API (referral link)"
            className="inline-flex items-center gap-1 rounded-lg bg-krypt-gradient px-2.5 py-1 text-[10px] font-bold uppercase tracking-widest text-white shadow-krypt-glow transition hover:brightness-110 active:scale-[0.98]"
          >
            Polymarket
            <ArrowUpRight size={11} />
          </button>
        </>
      }
    >
      {!loaded ? (
        <div className="flex items-center gap-2 py-16 text-xs uppercase tracking-wider text-krypt-muted">
          <Loader2 size={14} className="animate-spin text-scout-red-bright" />
          Loading auto-bet status…
        </div>
      ) : !status ? (
        <Card>
          <div className="text-xs leading-relaxed text-krypt-muted">
            {apiUp === true ? (
              <>
                <span className="font-semibold text-amber-300">
                  Auto-bet API missing on the running backend.
                </span>{' '}
                The backend is up and healthy, but <span className="font-mono text-[11px] text-white/70">
                  /api/autobet
                </span>{' '}
                returns 404 — this build&apos;s Python backend predates auto-bet. Update to the
                latest build (a reinstall re-freezes the backend) to enable it. Demo mode never
                trades.
              </>
            ) : (
              <>
                Backend unreachable — auto-bet needs the live Python backend (SETTINGS → BACKEND,
                or restart the app). Demo mode never trades.
              </>
            )}
          </div>
          {window.scout && (
            <div className="mt-3 flex flex-wrap items-center gap-2">
              <span title="Save the backend log history + environment info to a file — attach it when reporting this">
                <GhostButton
                  onClick={() => void exportLogs()}
                  disabled={exporting}
                  className="!px-3 !py-1.5 text-xs"
                >
                  <Download size={12} className={exporting ? 'animate-pulse' : undefined} />
                  Export backend logs
                </GhostButton>
              </span>
              {exportMsg && (
                <span
                  className={cls(
                    'break-all font-mono text-[10px]',
                    exportMsg.ok ? 'text-emerald-300' : 'text-rose-400',
                  )}
                >
                  {exportMsg.text}
                </span>
              )}
            </div>
          )}
        </Card>
      ) : (
        <div className="space-y-3">
          {/* Mode banner — always explicit about what would happen right now. */}
          <div
            className={cls(
              'flex flex-wrap items-center gap-2 rounded-xl border px-3 py-2 text-[11px] font-semibold uppercase tracking-wider',
              live
                ? 'border-rose-400/40 bg-rose-500/[0.08] text-rose-200'
                : 'border-white/10 bg-white/[0.03] text-krypt-muted',
            )}
          >
            <ShieldAlert size={13} className={live ? 'text-rose-300' : 'text-krypt-muted/60'} />
            {stopped
              ? stopLabel[stopped]
              : live
                ? 'LIVE — qualifying picks place real orders after every capture.'
                : s?.enabled
                  ? 'PAPER — sweeps record what they WOULD bet; no orders placed.'
                  : 'OFF — nothing runs automatically. RUN NOW paper-trades on demand.'}
            <span className="ml-auto flex items-center gap-2">
              {wallet?.keyStoredUnencrypted && (
                <Badge tone="danger">KEY UNENCRYPTED ON DISK</Badge>
              )}
              {stopped && <Badge tone="danger">STOPPED</Badge>}
              <Badge tone={live ? 'danger' : s?.enabled ? 'warn' : 'neutral'}>
                {live ? 'LIVE' : s?.enabled ? 'PAPER' : 'OFF'}
              </Badge>
            </span>
          </div>

          {/* Drift monitor — is the live record tracking the backtest? */}
          {drift && drift.verdict && drift.live.n_graded > 0 && (
            <div className="flex flex-wrap items-baseline gap-x-4 gap-y-1 rounded-xl border border-white/10 bg-white/[0.03] px-3 py-2 text-xs">
              <span className="text-[9px] font-bold uppercase tracking-[0.18em] text-krypt-muted">
                Drift monitor
              </span>
              <span className="font-mono text-[11px] text-white">
                live {drift.live.win_rate != null ? `${Math.round(drift.live.win_rate * 100)}%` : '—'} win
                ({drift.live.n_graded} graded)
              </span>
              {drift.expected.win_rate != null && (
                <span className="font-mono text-[11px] text-krypt-muted">
                  backtest expects {Math.round(drift.expected.win_rate * 100)}%
                </span>
              )}
              <span className="text-[11px] text-krypt-muted">{drift.verdict}</span>
            </div>
          )}

          {/* Wallet */}
          <Card>
            <div className="mb-2 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-krypt-muted">
              <Wallet size={12} className="text-scout-red-bright" />
              Trading wallet
            </div>
            {wallet?.hasWalletKey ? (
              <div className="flex flex-wrap items-center gap-3">
                <span className="font-mono text-xs text-white">{wallet.addressPreview || wallet.address}</span>
                {wallet.funder && (
                  <span
                    className="font-mono text-[10px] text-krypt-muted"
                    title={`Deposit wallet (funder): ${wallet.funder}`}
                  >
                    deposit {wallet.funder.slice(0, 6)}…{wallet.funder.slice(-4)}
                  </span>
                )}
                <Badge tone="neutral">{(wallet.walletMode || 'eoa').toUpperCase()}</Badge>
                <span className="ml-auto flex items-center gap-2">
                  <GhostButton onClick={() => void test()} className="!px-3 !py-1.5 text-xs">
                    {busy ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
                    Test
                  </GhostButton>
                  <GhostButton
                    onClick={() => void disconnect()}
                    className={cls(
                      '!px-3 !py-1.5 text-xs',
                      disconnectArmed && '!border-rose-400/50 !bg-rose-500/15 !text-rose-200',
                    )}
                  >
                    {disconnectArmed ? 'SURE?' : 'Disconnect'}
                  </GhostButton>
                </span>
              </div>
            ) : (
              <div className="space-y-2">
                <div className="flex flex-wrap items-center gap-2">
                  <div className="relative min-w-[240px] flex-1">
                    <KeyRound
                      size={12}
                      className="pointer-events-none absolute left-2.5 top-1/2 -translate-y-1/2 text-krypt-muted/50"
                    />
                    <input
                      type="password"
                      value={pk}
                      onChange={(e) => setPk(e.target.value)}
                      placeholder="Polygon wallet private key (0x…) — exported from Polymarket"
                      spellCheck={false}
                      className="w-full rounded-xl border border-white/10 bg-black/30 py-2 pl-8 pr-3 font-mono text-xs text-white placeholder:font-sans placeholder:text-krypt-muted/50 focus:border-scout-red/40 focus:outline-none"
                    />
                  </div>
                  <input
                    value={funder}
                    onChange={(e) => setFunder(e.target.value)}
                    placeholder="Deposit wallet address (0x…, optional)"
                    spellCheck={false}
                    className="min-w-[220px] rounded-xl border border-white/10 bg-black/30 px-3 py-2 font-mono text-xs text-white placeholder:font-sans placeholder:text-krypt-muted/50 focus:border-scout-red/40 focus:outline-none"
                  />
                  <PrimaryButton onClick={() => void connect()} className="!px-3 !py-2 text-xs">
                    {busy ? 'Connecting…' : 'Connect'}
                  </PrimaryButton>
                </div>
                <p className="text-[11px] leading-relaxed text-krypt-muted">
                  No Polymarket account yet? Create one via the button up top. Email/Magic
                  accounts: export the key from Polymarket (profile → export private key) and
                  paste your profile&apos;s deposit address as the deposit wallet. The key is
                  encrypted at rest (DPAPI / OS keychain) and never leaves this machine except to
                  sign orders.
                </p>
              </div>
            )}
            {walletMsg && (
              <div
                className={cls(
                  'mt-2 rounded-lg border px-2.5 py-1.5 text-[11px] font-semibold',
                  walletMsg.ok
                    ? 'border-emerald-400/25 bg-emerald-500/[0.08] text-emerald-200'
                    : 'border-rose-400/25 bg-rose-500/[0.08] text-rose-200',
                )}
              >
                {walletMsg.text}
              </div>
            )}
          </Card>

          {/* Engine controls */}
          {s && (
            <Card>
              <div className="mb-3 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-krypt-muted">
                <Bot size={12} className="text-scout-red-bright" />
                Engine
              </div>

              {/* The two switches that gate real money — each with its
                  definition, because a mystery toggle here costs dollars. */}
              <div className="space-y-3">
                <div className="flex items-start gap-3">
                  <Toggle checked={s.enabled} onChange={() => void patch({ enabled: !s.enabled })} label="Auto-bet" />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-semibold text-white">Auto-bet</div>
                    <div className="text-xs text-krypt-muted">
                      ON = a sweep runs after every board capture (every few hours) and acts on
                      qualifying picks. OFF = nothing runs by itself — RUN NOW still works for a
                      one-off pass.
                    </div>
                  </div>
                </div>
                <div className="flex items-start gap-3 border-t border-white/5 pt-3">
                  <div
                    className="flex shrink-0 items-center overflow-hidden rounded-lg border border-white/10 bg-white/[0.03]"
                    role="group"
                    aria-label="Paper or live orders"
                  >
                    {(['paper', 'live'] as const).map((mode) => {
                      const active = mode === 'paper' ? s.dry_run : !s.dry_run
                      return (
                        <button
                          key={mode}
                          onClick={() => void patch({ dry_run: mode === 'paper' })}
                          aria-pressed={active}
                          className={cls(
                            'px-3 py-1.5 text-[11px] font-bold tracking-widest transition',
                            active
                              ? mode === 'live'
                                ? 'bg-rose-500/20 text-rose-200 shadow-[inset_0_0_0_1px_rgba(244,63,94,0.5)]'
                                : 'bg-sky-500/15 text-sky-200 shadow-[inset_0_0_0_1px_rgba(56,189,248,0.4)]'
                              : 'text-krypt-muted hover:bg-white/5 hover:text-white',
                          )}
                        >
                          {mode.toUpperCase()}
                        </button>
                      )
                    })}
                  </div>
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-semibold text-white">Paper vs live</div>
                    <div className="text-xs text-krypt-muted">
                      PAPER records what the engine <em>would</em> bet (the log shows every
                      would-be order) without touching your wallet. LIVE signs real fill-and-kill
                      orders. Run PAPER for a few days first and check the log against results.
                    </div>
                  </div>
                </div>
              </div>

              {/* Strategy — load a preset or a saved profile, or save current. */}
              {strategies && (
                <div className="mt-3 border-t border-white/5 pt-3">
                  <div className="mb-2 flex items-center justify-between gap-2">
                    <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                      Strategy · load a preset or your own
                    </span>
                    {profileMsg && (
                      <span
                        className={cls(
                          'font-mono text-[10px]',
                          profileMsg.ok ? 'text-emerald-300' : 'text-rose-400',
                        )}
                      >
                        {profileMsg.text}
                      </span>
                    )}
                  </div>
                  <div className="flex flex-wrap items-center gap-1.5">
                    {strategies.presets.map((p) => (
                      <button
                        key={p.name}
                        onClick={() => void applyStrategy(p.name)}
                        title={`Load the ${p.name} preset's rules (won't arm the engine)`}
                        className="rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-1 text-[10px] font-bold tracking-widest text-krypt-muted transition hover:border-scout-red/40 hover:text-white"
                      >
                        {p.name.toUpperCase()}
                      </button>
                    ))}
                    {strategies.profiles.map((p) => (
                      <span
                        key={p.name}
                        className="inline-flex items-center overflow-hidden rounded-lg border border-sky-400/20 bg-sky-500/[0.06]"
                      >
                        <button
                          onClick={() => void applyStrategy(p.name)}
                          title={`Load your saved “${p.name}” profile`}
                          className="px-2.5 py-1 text-[10px] font-bold tracking-widest text-sky-200/90 transition hover:bg-white/5 hover:text-white"
                        >
                          {p.name.toUpperCase()}
                        </button>
                        <button
                          onClick={() => void deleteStrategy(p.name)}
                          title="Delete this profile"
                          className="border-l border-sky-400/20 px-1.5 py-1 text-krypt-muted/60 transition hover:bg-rose-500/10 hover:text-rose-300"
                        >
                          <X size={10} />
                        </button>
                      </span>
                    ))}
                    <span className="ml-1 inline-flex items-center overflow-hidden rounded-lg border border-white/10 bg-black/30">
                      <input
                        value={profileName}
                        onChange={(e) => setProfileName(e.target.value)}
                        onKeyDown={(e) => e.key === 'Enter' && void saveStrategy()}
                        placeholder="save current as…"
                        className="w-32 bg-transparent px-2 py-1 font-mono text-[11px] text-white outline-none placeholder:text-krypt-muted/40"
                      />
                      <button
                        onClick={() => void saveStrategy()}
                        disabled={!profileName.trim()}
                        className="border-l border-white/10 px-2 py-1 text-[10px] font-bold uppercase tracking-widest text-scout-red-bright transition hover:bg-white/5 disabled:opacity-40"
                      >
                        Save
                      </button>
                    </span>
                  </div>
                </div>
              )}

              {/* Sizing — flat $, flat % of balance, or % scaled by confidence. */}
              <div className="mt-3 border-t border-white/5 pt-3">
                <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                  Sizing
                </div>
                <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
                  <div
                    className="flex shrink-0 items-center overflow-hidden rounded-lg border border-white/10 bg-white/[0.03]"
                    role="group"
                    aria-label="Stake mode"
                  >
                    {(
                      [
                        ['fixed', 'FIXED $', 'Bet the same dollar amount on every pick'],
                        ['percent', '% BAL', 'Bet a flat percent of your live cash balance (needs the wallet, paper mode included)'],
                        ['confidence', 'CONF %', 'Scale the % of balance by model confidence — the band below sets low→high'],
                        ['kelly', 'KELLY', 'Fractional Kelly: sized by EDGE (model prob vs limit price), hard-capped — accelerates gains only where edge is real, and losses where it isn’t'],
                      ] as const
                    ).map(([mode, label, help]) => {
                      const active = s.stake_mode === mode
                      return (
                        <button
                          key={mode}
                          onClick={() => void patch({ stake_mode: mode })}
                          title={help}
                          aria-pressed={active}
                          className={cls(
                            'px-2.5 py-1.5 text-[10px] font-bold tracking-widest transition',
                            active
                              ? 'bg-white/10 text-white shadow-[inset_0_0_0_1px_rgba(225,29,72,0.45)]'
                              : 'text-krypt-muted hover:bg-white/5 hover:text-white',
                          )}
                        >
                          {label}
                        </button>
                      )
                    })}
                  </div>
                  {s.stake_mode === 'fixed' ? (
                    <NumField
                      label="Stake"
                      value={s.stake_usd}
                      suffix="$"
                      onCommit={(v) => void patch({ stake_usd: v })}
                      title="Flat dollar spend per pick"
                    />
                  ) : s.stake_mode === 'percent' ? (
                    <NumField
                      label="Stake"
                      value={s.stake_pct}
                      suffix="%"
                      width="w-12"
                      onCommit={(v) => void patch({ stake_pct: v })}
                      title="Percent of your cash balance per pick, sized at sweep time"
                    />
                  ) : s.stake_mode === 'kelly' ? (
                    <>
                      <NumField
                        label="Kelly ×"
                        value={s.kelly_mult}
                        width="w-12"
                        onCommit={(v) => void patch({ kelly_mult: v })}
                        title="Fraction of full Kelly (0.25 = quarter-Kelly, the sane default)"
                      />
                      <NumField
                        label="Cap"
                        value={s.kelly_cap_pct}
                        suffix="%"
                        width="w-12"
                        onCommit={(v) => void patch({ kelly_cap_pct: v })}
                        title="Hard cap on any single stake, % of balance"
                      />
                      <span className="text-[10px] text-krypt-muted/60">
                        sized by edge · needs wallet
                      </span>
                    </>
                  ) : (
                    <>
                      <NumField
                        label="Low %"
                        value={s.stake_pct_min}
                        suffix="%"
                        width="w-12"
                        onCommit={(v) => void patch({ stake_pct_min: v })}
                        title="Stake % at a coin-flip (50%) model confidence"
                      />
                      <NumField
                        label="High %"
                        value={s.stake_pct_max}
                        suffix="%"
                        width="w-12"
                        onCommit={(v) => void patch({ stake_pct_max: v })}
                        title="Stake % at a near-lock (≥90%) model confidence"
                      />
                      <span className="text-[10px] text-krypt-muted/60">
                        scales with model confidence · needs wallet
                      </span>
                    </>
                  )}
                  <NumField
                    label="1 unit"
                    value={s.unit_usd}
                    suffix="$"
                    onCommit={(v) => void patch({ unit_usd: v })}
                    title="Dollars per tracker unit — placed bets log to MY BETS at stake/unit units"
                  />
                </div>
              </div>

              {/* Selection — which picks qualify. */}
              <div className="mt-3 border-t border-white/5 pt-3">
                <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                  Selection · which picks qualify
                </div>
                <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
                  <NumField
                    label="Min EV"
                    value={Math.round(s.min_ev * 100)}
                    suffix="%"
                    width="w-12"
                    onCommit={(v) => void patch({ min_ev: v / 100 })}
                    title="EV floor NET of Polymarket's 5% sports taker fee — also sets the order's limit price (the price where net EV would fall to this)"
                  />
                  <NumField
                    label="Min win%"
                    value={Math.round(s.min_win_prob * 100)}
                    suffix="%"
                    width="w-12"
                    onCommit={(v) => void patch({ min_win_prob: v / 100 })}
                    title="Skip picks the model rates below this win probability (0 = off)"
                  />
                  <NumField
                    label="Min edge"
                    value={Math.round(s.min_edge * 100)}
                    suffix="pts"
                    width="w-12"
                    onCommit={(v) => void patch({ min_edge: v / 100 })}
                    title="Skip picks whose model probability minus the market's implied probability is below this many points (0 = off)"
                  />
                  <NumField
                    label="Min price"
                    value={Math.round((s.min_prob ?? 0) * 100)}
                    suffix="¢"
                    width="w-12"
                    onCommit={(v) => void patch({ min_prob: v / 100 })}
                    title="Never buy shares below this price — filters longshots (0 = off)"
                  />
                  <NumField
                    label="Max price"
                    value={Math.round(s.max_prob * 100)}
                    suffix="¢"
                    width="w-12"
                    onCommit={(v) => void patch({ max_prob: v / 100 })}
                    title="Never buy shares above this price"
                  />
                  <span className="flex flex-wrap items-center gap-1.5" title="Only bet these sports — none lit = every sport. Check the BACKTEST panel's $-ROI verdicts before including a sport.">
                    {(['mlb', 'nba', 'wnba', 'nhl', 'nfl', 'ncaaf', 'ncaab', 'soccer', 'tennis'] as const).map((sp) => {
                      const active = s.sports?.includes(sp) ?? false
                      return (
                        <button
                          key={sp}
                          onClick={() =>
                            void patch({
                              sports: active
                                ? (s.sports ?? []).filter((x) => x !== sp)
                                : [...(s.sports ?? []), sp],
                            })
                          }
                          className={cls(
                            'rounded-lg border px-1.5 py-1 text-[9px] font-bold tracking-widest transition',
                            active
                              ? 'border-sky-400/40 bg-sky-500/10 text-sky-300'
                              : 'border-white/10 bg-white/[0.03] text-krypt-muted/70 hover:border-white/20',
                          )}
                        >
                          {sp.toUpperCase()}
                        </button>
                      )
                    })}
                  </span>
                  <span className="flex flex-wrap items-center gap-1.5">
                    {(['STRONG', 'LEAN'] as const).map((tier) => {
                      const active = s.tiers.includes(tier)
                      return (
                        <button
                          key={tier}
                          onClick={() =>
                            void patch({
                              tiers: active ? s.tiers.filter((t) => t !== tier) : [...s.tiers, tier],
                            })
                          }
                          title={
                            tier === 'STRONG'
                              ? 'Tail STRONG picks (EV ≥ 4%)'
                              : 'Also tail LEAN picks (smaller edges — more volume, thinner margin)'
                          }
                          className={cls(
                            'rounded-lg border px-2 py-1 text-[10px] font-bold tracking-widest transition',
                            active
                              ? 'border-emerald-400/40 bg-emerald-500/10 text-emerald-300'
                              : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20',
                          )}
                        >
                          {tier}
                        </button>
                      )
                    })}
                  </span>
                </div>

                {/* Best edge — turn the daily cap into a "top N of the
                    board" rule instead of first-come-first-served. */}
                <div className="mt-3 flex items-start gap-3 border-t border-white/5 pt-3">
                  <Toggle
                    checked={!!s.best_edge_only}
                    onChange={() => void patch({ best_edge_only: !s.best_edge_only })}
                    label="Best edge only"
                  />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-semibold text-white">
                      Best edge only
                      {s.best_edge_only && (
                        <span className="ml-2 font-mono text-[10px] font-normal text-sky-300">
                          top {s.max_bets_per_day}/day
                        </span>
                      )}
                    </div>
                    <div className="text-xs text-krypt-muted">
                      ON = every qualifying pick is ranked by <em>edge</em> (model
                      probability minus the market&apos;s implied probability) and only the
                      best {s.max_bets_per_day} of the day get bet — the rest are skipped as
                      &ldquo;not_best_edge&rdquo;. OFF = the cap goes to whichever qualifying
                      picks the board listed first. Ranking happens per sweep, so a bigger
                      edge surfacing later in the day can&apos;t reclaim a slot already spent
                      — pair this with a tight MAX HOURS OUT so each sweep ranks one
                      comparable slate.
                    </div>
                  </div>
                </div>
              </div>

              {/* Timing & caps. */}
              <div className="mt-3 border-t border-white/5 pt-3">
                <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                  Timing &amp; caps
                </div>
                <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
                  <NumField
                    label="Max/day"
                    value={s.max_bets_per_day}
                    width="w-12"
                    onCommit={(v) => void patch({ max_bets_per_day: Math.round(v) })}
                    title="Cap on placed + paper bets per UTC day"
                  />
                  <NumField
                    label="Lead"
                    value={s.lead_minutes}
                    suffix="min"
                    width="w-12"
                    onCommit={(v) => void patch({ lead_minutes: Math.round(v) })}
                    title="Skip games starting within this many minutes"
                  />
                  <NumField
                    label="Max days out"
                    value={s.max_days_ahead}
                    suffix="d"
                    width="w-12"
                    onCommit={(v) => void patch({ max_days_ahead: Math.round(v) })}
                    title="Never bet games starting further out than this many days — far-ahead odds go stale before kickoff"
                  />
                  <NumField
                    label="Max hours out"
                    value={s.max_hours_ahead}
                    suffix="h"
                    width="w-12"
                    onCommit={(v) => void patch({ max_hours_ahead: Math.round(v) })}
                    title="Tighter window in hours — 0 = use max days out"
                  />
                </div>
              </div>

              {/* Stops — realized-P&L halts. */}
              <div className="mt-3 border-t border-white/5 pt-3">
                <div className="mb-2 text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                  Stops · realized P&amp;L halts · 0 = off
                </div>
                <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
                  <NumField
                    label="Daily stop-loss"
                    value={s.daily_stop_loss_usd}
                    suffix="$"
                    width="w-16"
                    onCommit={(v) => void patch({ daily_stop_loss_usd: v })}
                    title="Halt for the rest of the day once today's realized loss reaches this (0 = off)"
                  />
                  <NumField
                    label="Daily take-profit"
                    value={s.daily_stop_profit_usd}
                    suffix="$"
                    width="w-16"
                    onCommit={(v) => void patch({ daily_stop_profit_usd: v })}
                    title="Halt for the rest of the day once today's realized profit reaches this (0 = off)"
                  />
                  <NumField
                    label="Stop-loss"
                    value={s.stop_loss_usd}
                    suffix="$"
                    width="w-16"
                    onCommit={(v) => void patch({ stop_loss_usd: v })}
                    title="Kill switch: halt entirely once lifetime realized loss reaches this (0 = off)"
                  />
                  <NumField
                    label="Stop-profit"
                    value={s.stop_profit_usd}
                    suffix="$"
                    width="w-16"
                    onCommit={(v) => void patch({ stop_profit_usd: v })}
                    title="Take-profit: halt entirely once lifetime realized profit reaches this (0 = off)"
                  />
                  <span className="text-[10px] text-krypt-muted/60">
                    Computed from settled auto-bets only — open bets don&apos;t count until they
                    grade.
                  </span>
                </div>
              </div>
            </Card>
          )}

          {/* Activity */}
          <Card padded={false} className="overflow-hidden">
            <div className="flex flex-wrap items-center gap-3 border-b border-white/5 px-3 py-2">
              <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-krypt-muted">
                Activity
              </span>
              {today && (
                <span className="font-mono text-[10px] text-krypt-muted">
                  today: {today.placed} placed · {today.dry} dry · ${today.spent_usd.toFixed(2)} spent
                </span>
              )}
              {pnl && (
                <span
                  className="font-mono text-[10px]"
                  title="Realized P&L of placed auto-bets (settled games); open = still riding"
                >
                  <span className={pnl.today_usd >= 0 ? 'text-emerald-300' : 'text-rose-400'}>
                    today {pnl.today_usd >= 0 ? '+' : ''}${pnl.today_usd.toFixed(2)}
                  </span>
                  <span className={cls('ml-2', pnl.total_usd >= 0 ? 'text-emerald-300' : 'text-rose-400')}>
                    total {pnl.total_usd >= 0 ? '+' : ''}${pnl.total_usd.toFixed(2)}
                  </span>
                  <span className="ml-2 text-krypt-muted/70">open ${pnl.open_usd.toFixed(2)}</span>
                </span>
              )}
              {status.last_sweep && (
                <span
                  className="min-w-0 flex-1 truncate font-mono text-[10px] text-krypt-muted/70"
                  title={sweepLine(status.last_sweep)}
                >
                  last sweep: {sweepLine(status.last_sweep)}
                </span>
              )}
              <GhostButton onClick={() => void runNow()} className="ml-auto !px-3 !py-1.5 text-xs">
                {busy ? <Loader2 size={12} className="animate-spin" /> : <Play size={12} />}
                Run now
              </GhostButton>
            </div>
            {log.length === 0 ? (
              <div className="px-3 py-4 text-[11px] text-krypt-muted">
                No attempts yet. Sweeps run after every board capture while enabled — or hit RUN
                NOW to dry-run the current picks.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-left">
                  <thead>
                    <tr className="border-b border-white/10 bg-white/[0.02]">
                      <th className={TH}>Status</th>
                      <th className={TH}>Kickoff</th>
                      <th className={TH}>Matchup</th>
                      <th className={TH}>Side</th>
                      <th className={cls(TH, 'text-right')}>Limit</th>
                      <th className={cls(TH, 'text-right')}>Shares</th>
                      <th className={cls(TH, 'text-right')}>$</th>
                      <th className={TH}>Result</th>
                    </tr>
                  </thead>
                  <tbody>
                    {log.map((a) => {
                      const kick = a.kickoff ? fmtKick(a.kickoff) : null
                      return (
                        <tr key={a.id} className="border-b border-white/5 last:border-0 hover:bg-white/[0.02]">
                          <td className={cls(TD, 'font-sans')}>
                            <StatusChip status={a.status} />
                          </td>
                          <td className={cls(TD, 'whitespace-nowrap text-[10px] text-krypt-muted/70')}>
                            {kick ? `${kick.date} ${kick.time}` : '—'}
                          </td>
                          <td className={cls(TD, 'font-sans text-[11px] text-white/70')}>
                            {a.away && a.home ? `${a.away} @ ${a.home}` : a.game_id}
                          </td>
                          <td className={cls(TD, 'font-sans text-[11px] font-bold text-white')}>
                            {a.team}
                          </td>
                          <td className={cls(TD, 'text-right text-white/85')}>
                            {Math.round(a.limit_prob * 100)}¢
                          </td>
                          <td className={cls(TD, 'text-right text-krypt-muted')}>{a.shares}</td>
                          <td className={cls(TD, 'text-right text-krypt-muted')}>{a.usd.toFixed(2)}</td>
                          <td className={cls(TD, 'max-w-[200px] font-sans')}>
                            {a.result ? (
                              <span
                                className={cls(
                                  'text-[10px] font-bold',
                                  a.result === 'W'
                                    ? 'text-emerald-300'
                                    : a.result === 'L'
                                      ? 'text-red-300'
                                      : 'text-krypt-muted',
                                )}
                              >
                                {a.result === 'P' ? 'PUSH' : a.result}
                                {a.result !== 'P' && typeof a.pnl_usd === 'number'
                                  ? ` ${a.pnl_usd >= 0 ? '+' : '−'}$${Math.abs(a.pnl_usd).toFixed(2)}`
                                  : ''}
                              </span>
                            ) : (
                              <span
                                className="block truncate text-[10px] text-krypt-muted/70"
                                title={a.error || a.order_id || ''}
                              >
                                {a.status === 'error'
                                  ? a.error
                                  : a.order_id
                                    ? `${a.order_id.slice(0, 10)}…`
                                    : a.status === 'dry'
                                      ? 'dry run'
                                      : '—'}
                              </span>
                            )}
                          </td>
                        </tr>
                      )
                    })}
                  </tbody>
                </table>
              </div>
            )}
          </Card>

          <div className="text-[9px] uppercase tracking-wider text-krypt-muted/50">
            Real money on Polymarket only. Orders are fill-and-kill at an EV-floor limit price —
            never resting, never above your caps. Placed bets grade into MY BETS automatically.
            You are responsible for Polymarket ToS and your jurisdiction. 21+.
          </div>
        </div>
      )}
    </Page>
  )
}
