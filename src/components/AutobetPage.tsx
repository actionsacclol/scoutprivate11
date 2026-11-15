/**
 * AUTO-BET — the auto-trading panel (kryptscout/autobet).
 *
 * Three exchanges: Polymarket (a wallet), Kalshi and Polymarket US (an API
 * key each). The rule needs no forecast: buy a side on one exchange when it
 * sells for less than ANOTHER exchange says it is worth, after the fee.
 * Real money: the backend defaults to disabled + paper, and this UI keeps
 * both switches explicit — nothing trades until the user connects an account,
 * turns the engine ON, and switches PAPER to LIVE. Keys are write-only: they
 * go to the backend's encrypted credential store and never come back.
 */
import { useCallback, useEffect, useState } from 'react'
import {
  ArrowUpRight,
  Bot,
  ChevronDown,
  Download,
  KeyRound,
  Loader2,
  Play,
  RefreshCw,
  ShieldAlert,
  SlidersHorizontal,
  Wallet,
  X,
} from 'lucide-react'
import type {
  AutobetAttempt,
  AutobetSettings,
  AutobetStatus,
  AutobetStrategies,
  AutobetSweepSummary,
  AutobetVenue,
} from '../api'
import {
  applyAutobetProfile,
  clearAutobetVenueKey,
  clearAutobetWallet,
  deleteAutobetProfile,
  fetchAutobet,
  fetchAutobetLog,
  fetchAutobetProfiles,
  runAutobet,
  saveAutobetProfile,
  saveAutobetSettings,
  saveAutobetVenueKey,
  saveAutobetWallet,
  testAutobetVenue,
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

const VENUE_NAME: Record<string, string> = {
  polymarket: 'Polymarket',
  kalshi: 'Kalshi',
  polymarket_us: 'Polymarket US',
}
const VENUE_SHORT: Record<string, string> = {
  polymarket: 'PM',
  kalshi: 'KAL',
  polymarket_us: 'PM·US',
}

/** The three exchanges, for a backend that does not list them itself — the
 *  accounts card must never render empty, it is the only way to connect. */
const VENUE_FALLBACK: AutobetVenue[] = [
  { key: 'polymarket', label: 'Polymarket', connected: false, keyPreview: '', accountNoun: 'wallet', onBoard: true },
  { key: 'kalshi', label: 'Kalshi', connected: false, keyPreview: '', accountNoun: 'API key', onBoard: true },
  { key: 'polymarket_us', label: 'Polymarket US', connected: false, keyPreview: '', accountNoun: 'API key', onBoard: false },
]

/** Whether the RULES card is unfolded. The user's own choice sticks; until
 *  they make one it follows the mode (folded in SIMPLE, open in PRO). */
const RULES_KEY = 'scout-autobet-rules-open'

function loadRulesOpen(): boolean | null {
  try {
    const raw = localStorage.getItem(RULES_KEY)
    return raw === '1' ? true : raw === '0' ? false : null
  } catch {
    return null
  }
}

/** Why a sweep passed on a price, in words. Unlisted reasons show as-is. */
const SKIP_WORDS: Record<string, string> = {
  below_min_ev: 'under your EV floor',
  thin_reference: 'reference book too thin to trust',
  tier_filtered: 'reference not switched on',
  out_of_window: 'outside your time window',
  already_bet: 'game already bet',
  no_depth: 'nothing offered at the limit',
  no_book: 'order book unreadable',
  no_market: 'no matching market',
  daily_cap: 'daily cap reached',
  not_best_edge: 'not among the best edges',
  venue_not_connected: 'exchange not connected',
  ev_implausible: 'gap too large to be real',
  stake_too_small: 'stake under $1',
  below_min_size: 'under the exchange minimum',
  price_out_of_range: 'limit outside your price range',
  no_balance: 'balance unreadable',
  lookup_budget: 'lookup limit reached',
  no_wallet: 'no exchange connected',
  unlicensed: 'live betting needs a license',
  disabled: 'engine off',
}

function StatusChip({ status }: { status: AutobetAttempt['status'] }) {
  const tone: Record<AutobetAttempt['status'], string> = {
    placed: 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300',
    dry: 'border-sky-400/30 bg-sky-500/10 text-sky-300',
    unmatched: 'border-amber-400/30 bg-amber-500/10 text-amber-300',
    error: 'border-rose-400/30 bg-rose-500/10 text-rose-300',
    unknown: 'border-rose-400/50 bg-rose-500/20 text-rose-200',
  }
  const word: Record<AutobetAttempt['status'], string> = {
    placed: 'placed',
    dry: 'paper',
    unmatched: 'no fill',
    error: 'refused',
    unknown: 'check account',
  }
  return (
    <span
      className={cls(
        'inline-flex items-center whitespace-nowrap rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest',
        tone[status] ?? tone.error,
      )}
    >
      {word[status] ?? status}
    </span>
  )
}

const INPUT =
  'w-full rounded-xl border border-white/10 bg-black/30 px-3 py-2 font-mono text-xs text-white placeholder:font-sans placeholder:text-krypt-muted/50 focus:border-scout-red/40 focus:outline-none'

/** Connect / test / disconnect for an exchange that uses an API key pair. */
function KeyVenue({
  venue,
  open,
  onChanged,
}: {
  venue: AutobetVenue
  /** Show the key form (the row's CONNECT button). Ignored once connected. */
  open: boolean
  onChanged: () => void
}) {
  const key = venue.key as 'kalshi' | 'polymarket_us'
  const kalshi = key === 'kalshi'
  const [keyId, setKeyId] = useState('')
  const [secret, setSecret] = useState('')
  const [busy, setBusy] = useState(false)
  const [armed, setArmed] = useState(false)
  const [msg, setMsg] = useState<{ ok: boolean; text: string } | null>(null)

  useEffect(() => {
    if (!armed) return
    const t = window.setTimeout(() => setArmed(false), 3000)
    return () => window.clearTimeout(t)
  }, [armed])

  const report = (check: { ok?: boolean; balance_usd?: number; error?: string } | undefined) => {
    if (!check) return
    setMsg(
      check.ok
        ? {
            ok: true,
            text: `Connected${
              typeof check.balance_usd === 'number' ? ` · cash $${check.balance_usd.toFixed(2)}` : ''
            }`,
          }
        : { ok: false, text: check.error || 'The exchange did not accept this key.' },
    )
  }

  const connect = async () => {
    if (!keyId.trim() || !secret.trim() || busy) return
    setBusy(true)
    setMsg(null)
    const res = await saveAutobetVenueKey(key, { key_id: keyId.trim(), secret: secret.trim() })
    setBusy(false)
    if (res.error) {
      setMsg({ ok: false, text: res.error })
      return
    }
    setKeyId('')
    setSecret('')
    report(res.check)
    onChanged()
  }

  const test = async () => {
    if (busy) return
    setBusy(true)
    setMsg(null)
    report(await testAutobetVenue(key))
    setBusy(false)
  }

  const disconnect = async () => {
    if (!armed) {
      setArmed(true)
      return
    }
    setArmed(false)
    await clearAutobetVenueKey(key)
    setMsg(null)
    onChanged()
  }

  return (
    <div className="space-y-2">
      {venue.connected ? (
        <div className="flex flex-wrap items-center gap-3">
          <span className="font-mono text-xs text-white">{venue.keyPreview || 'key saved'}</span>
          {venue.error && <span className="text-[11px] text-rose-300">{venue.error}</span>}
          <span className="ml-auto flex items-center gap-2">
            <GhostButton onClick={() => void test()} className="!px-3 !py-1.5 text-xs">
              {busy ? <Loader2 size={12} className="animate-spin" /> : <RefreshCw size={12} />}
              Test
            </GhostButton>
            <GhostButton
              onClick={() => void disconnect()}
              className={cls(
                '!px-3 !py-1.5 text-xs',
                armed && '!border-rose-400/50 !bg-rose-500/15 !text-rose-200',
              )}
            >
              {armed ? 'SURE?' : 'Disconnect'}
            </GhostButton>
          </span>
        </div>
      ) : !open ? null : (
        <>
          <div className="flex flex-wrap items-start gap-2">
            <input
              value={keyId}
              onChange={(e) => setKeyId(e.target.value)}
              placeholder={kalshi ? 'API key ID' : 'Key ID (1a2b3c4d-…)'}
              spellCheck={false}
              autoFocus
              className={cls(INPUT, 'max-w-[280px]')}
            />
            {kalshi ? (
              <textarea
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder="Private key — paste the whole file, from -----BEGIN to -----END"
                spellCheck={false}
                rows={2}
                className={cls(INPUT, 'min-w-[260px] flex-1 resize-y [-webkit-text-security:disc]')}
              />
            ) : (
              <input
                type="password"
                value={secret}
                onChange={(e) => setSecret(e.target.value)}
                placeholder="Secret key"
                spellCheck={false}
                className={cls(INPUT, 'min-w-[260px] flex-1')}
              />
            )}
            <PrimaryButton onClick={() => void connect()} className="!px-3 !py-2 text-xs">
              {busy ? 'Connecting…' : 'Save'}
            </PrimaryButton>
          </div>
          <p className="text-[11px] leading-relaxed text-krypt-muted">
            {kalshi
              ? 'kalshi.com → Account & security → API keys → Create key. Paste the key ID and the private key file it gives you (shown once).'
              : 'polymarket.us → Developer → create an API key. Paste the key ID and the secret (shown once).'}{' '}
            Encrypted on this machine and only ever used to sign your own orders.
          </p>
        </>
      )}
      {msg && (
        <div
          className={cls(
            'rounded-lg border px-2.5 py-1.5 text-[11px] font-semibold',
            msg.ok
              ? 'border-emerald-400/25 bg-emerald-500/[0.08] text-emerald-200'
              : 'border-rose-400/25 bg-rose-500/[0.08] text-rose-200',
          )}
        >
          {msg.text}
        </div>
      )}
    </div>
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
  const mode = s.dry_run ? 'paper' : 'live'
  const parts = [
    `${s.scanned} prices checked`,
    s.placed ? `${s.placed} placed` : '',
    s.dry ? `${s.dry} paper` : '',
    s.unmatched ? `${s.unmatched} unfilled` : '',
    s.errors ? `${s.errors} refused` : '',
    s.unknown ? `${s.unknown} unanswered` : '',
  ].filter(Boolean)
  return `${mode} · ${parts.join(' · ')}`
}

function skipWords(s: AutobetSweepSummary): string {
  return Object.entries(s.skipped ?? {})
    .sort((a, b) => b[1] - a[1])
    .map(([k, n]) => `${n} ${SKIP_WORDS[k] ?? k}`)
    .join(' · ')
}

const pct = (v: number, digits = 1) => `${v >= 0 ? '+' : '−'}${Math.abs(v * 100).toFixed(digits)}%`

export default function AutobetPage({ demo, apiUp }: { demo: boolean; apiUp: boolean | null }) {
  const simple = useUiMode() === 'simple'
  const [status, setStatus] = useState<AutobetStatus | null>(null)
  const [log, setLog] = useState<AutobetAttempt[]>([])
  const [loaded, setLoaded] = useState(false)
  const [busy, setBusy] = useState(false)

  // Strategy presets + saved profiles
  const [strategies, setStrategies] = useState<AutobetStrategies | null>(null)
  const [profileName, setProfileName] = useState('')
  const [profileMsg, setProfileMsg] = useState<{ ok: boolean; text: string } | null>(null)

  // Which exchange's connect form is showing (one at a time), and whether the
  // rules are unfolded.
  const [openVenue, setOpenVenue] = useState<string | null>(null)
  const [rulesChoice, setRulesChoice] = useState<boolean | null>(loadRulesOpen)
  const rulesOpen = rulesChoice ?? !simple
  const toggleRules = () => {
    const next = !rulesOpen
    setRulesChoice(next)
    try {
      localStorage.setItem(RULES_KEY, next ? '1' : '0')
    } catch {
      /* storage unavailable — the fold still works for the session */
    }
  }

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
    setOpenVenue(null)
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
  const venues: AutobetVenue[] = status?.venues?.length
    ? status.venues
    : VENUE_FALLBACK.map((v) =>
        v.key === 'polymarket' ? { ...v, connected: !!wallet?.hasWalletKey } : v,
      )
  const connectedCount = venues.filter((v) => v.connected).length
  // The folded RULES card in one line: what a bet costs, what qualifies, how many.
  const rulesSummary = !s
    ? ''
    : [
        s.stake_mode === 'fixed'
          ? `$${s.stake_usd} a bet`
          : s.stake_mode === 'percent'
            ? `${s.stake_pct}% of balance a bet`
            : s.stake_mode === 'kelly'
              ? `Kelly ×${s.kelly_mult}, max ${s.kelly_cap_pct}%`
              : `${s.stake_pct_min}–${s.stake_pct_max}% of balance a bet`,
        `min EV ${Math.round(s.min_ev * 100)}%`,
        `up to ${s.max_bets_per_day} a day`,
      ].join(' · ')
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
          ? 'Buys a team on one exchange when another exchange prices it higher — your accounts, your caps, off until you say go.'
          : 'Polymarket, Kalshi and Polymarket US, priced against each other: capped immediate orders, per-day caps, paper first.'
      }
      actions={
        <>
          {demo && <Badge tone="gradient">DEMO</Badge>}
          {apiUp === false && !demo && <Badge tone="danger">API DOWN</Badge>}
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
                ? 'LIVE — a qualifying price places a real order after every board refresh.'
                : s?.enabled
                  ? 'PAPER — sweeps record what they WOULD bet; no orders placed.'
                  : s?.dry_run
                    ? 'OFF — nothing runs automatically. RUN NOW paper-trades on demand.'
                    : 'OFF — nothing runs automatically. RUN NOW would place REAL orders (LIVE is selected).'}
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

          {(today?.unknown ?? 0) > 0 && (
            <div className="rounded-xl border border-rose-400/40 bg-rose-500/[0.08] px-3 py-2 text-[11px] leading-relaxed text-rose-100">
              <span className="font-bold uppercase tracking-wider">Check your account.</span>{' '}
              {today?.unknown} order{today?.unknown === 1 ? '' : 's'} today went out and the
              exchange never answered, so it may or may not be live. The game is held and no
              second order will be sent — look at your open positions on the exchange.
            </div>
          )}

          {/* Accounts — first on the page: this is where each exchange is connected. */}
          <Card>
            <div className="flex flex-wrap items-baseline gap-x-3 gap-y-1">
              <span className="flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-krypt-muted">
                <Wallet size={12} className="text-scout-red-bright" />
                Your accounts
              </span>
              <span className="text-[11px] text-krypt-muted/70">
                {connectedCount === 0
                  ? 'Connect one to bet for real. Paper mode works without any.'
                  : `${connectedCount} of ${venues.length} connected`}
              </span>
            </div>
            <div className="mt-2 divide-y divide-white/5">
              {venues.map((v) => {
                const on = s?.venues?.includes(v.key) ?? false
                const others = (s?.venues ?? []).filter((x) => x !== v.key)
                const open = openVenue === v.key
                return (
                  <div key={v.key} className="space-y-2 py-3 last:pb-0">
                    <div className="flex flex-wrap items-center gap-x-2.5 gap-y-2">
                      <span
                        className="text-sm font-semibold text-white"
                        title={
                          v.onBoard
                            ? undefined
                            : 'The boards do not quote this exchange, so auto-bet reads its prices itself: one lookup per game, soonest games first.'
                        }
                      >
                        {v.label}
                      </span>
                      <Badge tone={v.connected ? 'success' : 'neutral'}>
                        {v.connected ? 'CONNECTED' : 'NOT CONNECTED'}
                      </Badge>
                      <span className="ml-auto flex items-center gap-3">
                        <label
                          className="flex cursor-pointer items-center gap-1.5 text-[10px] font-semibold uppercase tracking-wider text-krypt-muted"
                          title="Let auto-bet buy on this exchange. At least one stays on."
                        >
                          Bet here
                          <Toggle
                            checked={on}
                            onChange={() => {
                              // The last exchange cannot be switched off: "bet nowhere" is the
                              // engine's own on/off switch, not this one.
                              if (on && others.length === 0) return
                              void patch({ venues: on ? others : [...(s?.venues ?? []), v.key] })
                            }}
                            label={`Bet on ${v.label}`}
                          />
                        </label>
                        <button
                          onClick={() => openBook(v.key)}
                          title={
                            v.connected
                              ? `Opens ${v.label}`
                              : `No account yet? Opens ${v.label} sign-up (referral link)`
                          }
                          className="inline-flex items-center gap-1 text-[10px] font-bold uppercase tracking-widest text-krypt-muted transition hover:text-white"
                        >
                          {v.connected ? 'Open' : 'Sign up'}
                          <ArrowUpRight size={11} />
                        </button>
                        {!v.connected && (
                          <PrimaryButton
                            onClick={() => setOpenVenue(open ? null : v.key)}
                            className="!px-3 !py-1.5 text-xs"
                          >
                            {open ? 'Close' : 'Connect'}
                          </PrimaryButton>
                        )}
                      </span>
                    </div>
                    {v.key === 'polymarket' ? (
                      <>
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
                          open && (
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
                                    placeholder="Wallet private key (0x…)"
                                    spellCheck={false}
                                    autoFocus
                                    className={cls(INPUT, 'pl-8')}
                                  />
                                </div>
                                <input
                                  value={funder}
                                  onChange={(e) => setFunder(e.target.value)}
                                  placeholder="Deposit address (0x…, optional)"
                                  spellCheck={false}
                                  className={cls(INPUT, 'min-w-[220px] max-w-[320px]')}
                                />
                                <PrimaryButton onClick={() => void connect()} className="!px-3 !py-2 text-xs">
                                  {busy ? 'Connecting…' : 'Save'}
                                </PrimaryButton>
                              </div>
                              <p className="text-[11px] leading-relaxed text-krypt-muted">
                                polymarket.com → profile → export private key. Email / Magic accounts:
                                also paste your profile&apos;s deposit address. Encrypted on this
                                machine and only ever used to sign your own orders.
                              </p>
                            </div>
                          )
                        )}
                        {walletMsg && (
                          <div
                            className={cls(
                              'rounded-lg border px-2.5 py-1.5 text-[11px] font-semibold',
                              walletMsg.ok
                                ? 'border-emerald-400/25 bg-emerald-500/[0.08] text-emerald-200'
                                : 'border-rose-400/25 bg-rose-500/[0.08] text-rose-200',
                            )}
                          >
                            {walletMsg.text}
                          </div>
                        )}
                      </>
                    ) : (
                      <KeyVenue venue={v} open={open} onChanged={() => void refresh()} />
                    )}
                    {v.key === 'kalshi' && v.connected && s && (
                      <div className="flex items-start gap-3">
                        <Toggle
                          checked={s.kalshi_auto_move !== false}
                          onChange={() => void patch({ kalshi_auto_move: s.kalshi_auto_move === false })}
                          label="Move funds between Kalshi exchanges"
                        />
                        <div className="min-w-0 flex-1 text-xs text-krypt-muted">
                          <span className="font-semibold text-white">Fund the order automatically.</span>{' '}
                          Kalshi keeps your cash in separate pools and game markets do not use the
                          one a deposit lands in. ON = a live order moves just its own shortfall
                          between your pools first (it never leaves your account). OFF = the order
                          is refused when its pool is short.
                        </div>
                      </div>
                    )}
                  </div>
                )
              })}
            </div>
          </Card>

          {/* Engine — the two switches that gate real money, each with its
              definition, because a mystery toggle here costs dollars. */}
          {s && (
            <Card>
              <div className="mb-3 flex items-center gap-2 text-[10px] font-bold uppercase tracking-[0.2em] text-krypt-muted">
                <Bot size={12} className="text-scout-red-bright" />
                Engine
              </div>
              <div className="space-y-3">
                <div className="flex items-start gap-3">
                  <Toggle checked={s.enabled} onChange={() => void patch({ enabled: !s.enabled })} label="Auto-bet" />
                  <div className="min-w-0 flex-1">
                    <div className="text-sm font-semibold text-white">Auto-bet</div>
                    <div className="text-xs text-krypt-muted">
                      ON checks prices after every board refresh (every few hours). OFF runs
                      nothing by itself — RUN NOW still does one pass.
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
                    <div className="text-sm font-semibold text-white">Paper or live</div>
                    <div className="text-xs text-krypt-muted">
                      PAPER logs what it <em>would</em> bet, filled off the real order book, and
                      touches no account. LIVE sends real orders on the accounts you connected.
                      Run PAPER for a few days first.
                    </div>
                  </div>
                </div>
              </div>
              <p className="mt-3 border-t border-white/5 pt-3 text-[11px] leading-relaxed text-krypt-muted">
                <span className="font-semibold text-amber-200/90">Expect few bets.</span> It buys a
                side only when another exchange prices it higher by at least your Min EV, after
                the fee. Exchanges are arbitraged against each other all day, so most sweeps place
                nothing, and this kind of bet has not shown a profit in our own tracking.
              </p>
            </Card>
          )}

          {/* Rules — two dozen knobs, folded to one line until the user wants them. */}
          {s && (
            <Card>
              <button
                type="button"
                onClick={toggleRules}
                aria-expanded={rulesOpen}
                className="flex w-full items-center gap-2 text-left"
              >
                <SlidersHorizontal size={12} className="shrink-0 text-scout-red-bright" />
                <span className="text-[10px] font-bold uppercase tracking-[0.2em] text-krypt-muted">
                  Rules
                </span>
                <span className="min-w-0 flex-1 truncate font-mono text-[11px] text-white/70">
                  {rulesSummary}
                </span>
                <span className="text-[10px] font-bold uppercase tracking-widest text-krypt-muted">
                  {rulesOpen ? 'Hide' : 'Edit'}
                </span>
                <ChevronDown
                  size={13}
                  className={cls('shrink-0 text-krypt-muted transition-transform', !rulesOpen && '-rotate-90')}
                />
              </button>
              {rulesOpen && (
                <>
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
                            ['fixed', 'FIXED $', 'Bet the same dollar amount every time'],
                            ['percent', '% BAL', 'Bet a flat percent of the cash on the exchange the order goes to (paper mode uses a $1,000 paper bankroll when no account is connected)'],
                            ['confidence', 'CONF %', 'Scale the % of balance by how likely the side is to win (the reference exchange’s probability) — the band below sets low→high'],
                            ['kelly', 'KELLY', 'Fractional Kelly: sized by the gap between the reference probability and the limit price, hard-capped — grows stakes only where the gap is real, and losses where it isn’t'],
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
                          title="Flat dollar spend per bet"
                        />
                      ) : s.stake_mode === 'percent' ? (
                        <NumField
                          label="Stake"
                          value={s.stake_pct}
                          suffix="%"
                          width="w-12"
                          onCommit={(v) => void patch({ stake_pct: v })}
                          title="Percent of that exchange's cash balance per bet, sized at sweep time"
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
                            sized by the price gap
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
                            title="Stake % on a coin-flip (50%) side"
                          />
                          <NumField
                            label="High %"
                            value={s.stake_pct_max}
                            suffix="%"
                            width="w-12"
                            onCommit={(v) => void patch({ stake_pct_max: v })}
                            title="Stake % on a heavy favourite (≥90%)"
                          />
                          <span className="text-[10px] text-krypt-muted/60">
                            scales with the side&apos;s win probability
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
                      Selection · which prices qualify
                    </div>
                    <div className="flex flex-wrap items-center gap-x-5 gap-y-3">
                      <NumField
                        label="Min EV"
                        value={Math.round(s.min_ev * 100)}
                        suffix="%"
                        width="w-12"
                        onCommit={(v) => void patch({ min_ev: v / 100 })}
                        title="How far under the other exchange's fair a price must be, AFTER this exchange's own fee. It also sets the order's limit price (the price where the gap would shrink to this). Exchanges rarely differ by more than a point or two."
                      />
                      <NumField
                        label="Min win%"
                        value={Math.round(s.min_win_prob * 100)}
                        suffix="%"
                        width="w-12"
                        onCommit={(v) => void patch({ min_win_prob: v / 100 })}
                        title="Skip sides the reference exchange rates below this win probability (0 = off)"
                      />
                      <NumField
                        label="Min edge"
                        value={Math.round(s.min_edge * 100)}
                        suffix="pts"
                        width="w-12"
                        onCommit={(v) => void patch({ min_edge: v / 100 })}
                        title="Skip prices less than this many points under the reference exchange's probability (0 = off)"
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
                      <span className="flex flex-wrap items-center gap-1.5" title="Only bet these sports — none lit = every sport listed. Soccer and tennis are never auto-bet.">
                        {(['mlb', 'nba', 'wnba', 'nhl', 'nfl', 'ncaaf', 'ncaab'] as const).map((sp) => {
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
                                  ? 'Trust Kalshi as the reference: buy on Polymarket or Polymarket US when they sell under Kalshi’s fair'
                                  : 'Also trust Polymarket as the reference — this is what lets Kalshi itself be bought (a weaker reference in our tracking)'
                              }
                              className={cls(
                                'rounded-lg border px-2 py-1 text-[10px] font-bold tracking-widest transition',
                                active
                                  ? 'border-emerald-400/40 bg-emerald-500/10 text-emerald-300'
                                  : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20',
                              )}
                            >
                              {tier === 'STRONG' ? 'VS KALSHI' : 'VS POLYMARKET'}
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
                          ON = qualifying prices are ranked by <em>edge</em> (the reference
                          exchange&apos;s probability minus the price) and only the best{' '}
                          {s.max_bets_per_day} of the day get bet. OFF = the cap goes to the
                          biggest gaps against Kalshi first. Ranked per sweep — a later, bigger
                          edge can&apos;t reclaim a spent slot, so pair it with a tight MAX HOURS
                          OUT.
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
                        title="Cap on placed + paper bets per UTC day, across every exchange. One game is only ever bet once."
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
                </>
              )}
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
                  today: {today.placed} placed · {today.dry} paper · ${today.spent_usd.toFixed(2)} spent
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
            {status.last_sweep && (
              <div className="space-y-2 border-b border-white/5 px-3 py-2.5">
                {status.last_sweep.scan && (
                  <div className="text-[11px] leading-relaxed text-krypt-muted">
                    <span className="font-semibold text-white/80">Last sweep:</span>{' '}
                    {status.last_sweep.scan.games} upcoming game
                    {status.last_sweep.scan.games === 1 ? '' : 's'} in your window,{' '}
                    {status.last_sweep.scan.with_reference} with another exchange to measure
                    against.
                    {Object.keys(status.last_sweep.skipped ?? {}).length > 0 && (
                      <> Passed on: {skipWords(status.last_sweep)}.</>
                    )}
                    {status.last_sweep.scan.games === 0 && (
                      <> The boards only carry games that have not started — try again before the next slate.</>
                    )}
                  </div>
                )}
                {(status.last_sweep.notes ?? []).map((note) => (
                  <div key={note} className="text-[11px] text-sky-200">
                    {note}
                  </div>
                ))}
                {(status.last_sweep.closest ?? []).length > 0 && (
                  <div>
                    <div className="mb-1 text-[9px] font-bold uppercase tracking-[0.18em] text-krypt-muted">
                      Nearest prices · after fees · your floor is{' '}
                      {s ? pct(s.min_ev, 0) : '—'}
                    </div>
                    <div className="grid gap-x-6 gap-y-0.5 sm:grid-cols-2">
                      {(status.last_sweep.closest ?? []).map((c) => (
                        <div
                          key={`${c.game_id}-${c.venue}-${c.team}`}
                          className="flex items-baseline gap-2 font-mono text-[11px]"
                          title={`${VENUE_NAME[c.venue] ?? c.venue} sells ${c.team} at ${(c.ask * 100).toFixed(1)}¢; ${VENUE_NAME[c.ref_book] ?? c.ref_book} says ${(c.fair_prob * 100).toFixed(1)}%`}
                        >
                          <span
                            className={cls(
                              'w-14 shrink-0 text-right font-bold',
                              s && c.net_ev >= s.min_ev ? 'text-emerald-300' : 'text-krypt-muted',
                            )}
                          >
                            {pct(c.net_ev)}
                          </span>
                          <span className="min-w-0 flex-1 truncate font-sans text-white/80">{c.team}</span>
                          <span className="shrink-0 text-[10px] text-krypt-muted/70">
                            {VENUE_SHORT[c.venue] ?? c.venue} {(c.ask * 100).toFixed(1)}¢ vs{' '}
                            {VENUE_SHORT[c.ref_book] ?? c.ref_book} {(c.fair_prob * 100).toFixed(1)}%
                          </span>
                        </div>
                      ))}
                    </div>
                  </div>
                )}
              </div>
            )}
            {log.length === 0 ? (
              <div className="px-3 py-4 text-[11px] text-krypt-muted">
                No bets yet. Sweeps run after every board refresh while the engine is on — or hit
                RUN NOW to check the current prices.
              </div>
            ) : (
              <div className="overflow-x-auto">
                <table className="w-full border-collapse text-left">
                  <thead>
                    <tr className="border-b border-white/10 bg-white/[0.02]">
                      <th className={TH}>Status</th>
                      <th className={TH}>Exchange</th>
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
                          <td
                            className={cls(TD, 'whitespace-nowrap font-sans text-[10px] text-white/70')}
                            title={
                              a.tier === 'AGENT'
                                ? `Asked for by an AI agent (${(a.ref_book ?? '').replace(/^agent:/, '') || 'unnamed'}) — see AI AGENTS`
                                : a.ref_book
                                  ? `Measured against ${VENUE_NAME[a.ref_book] ?? a.ref_book}`
                                  : undefined
                            }
                          >
                            {VENUE_NAME[a.venue ?? 'polymarket'] ?? a.venue}
                            {a.tier === 'AGENT' && (
                              <span className="ml-1.5 rounded border border-sky-400/30 px-1 text-[9px] font-semibold uppercase tracking-wider text-sky-200">
                                agent
                              </span>
                            )}
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
                                {a.status === 'error' || a.status === 'unknown'
                                  ? a.error
                                  : a.error && a.status === 'placed'
                                    ? a.error
                                    : a.order_id
                                      ? `${a.order_id.slice(0, 10)}…`
                                      : a.status === 'dry'
                                        ? 'paper'
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
            Real money once LIVE. Orders are immediate at a capped limit price — never resting,
            never above your caps, one bet per game. Placed bets grade into MY BETS
            automatically. You are responsible for each exchange&apos;s terms and your
            jurisdiction. 21+.
          </div>
        </div>
      )}
    </Page>
  )
}
