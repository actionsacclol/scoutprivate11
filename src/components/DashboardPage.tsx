/**
 * DASHBOARD — the suite-standard home overview (every Krypt tool lands here).
 *
 * One screen that answers "what should I look at right now?":
 *   hero strip (date · games · live · picks · record) → top pick spotlight →
 *   today's slate at a glance | system tiles + record sparkline → quick actions.
 * Pure view over data already flowing: the boards prop, derivePicks, useRoi,
 * and the (optional, ?.-guarded) window.scout supervisor surface.
 */
import { useEffect, useMemo, useState } from 'react'
import {
  ArrowRight,
  CalendarDays,
  Database,
  Gauge,
  Layers,
  NotebookPen,
  Radio,
  Server,
  SquareTerminal,
  Target,
  Timer,
  Webhook,
} from 'lucide-react'
import type { BackendInfo, DiscordInfo, SchedulerInfo } from '@shared/types'
import type { GameCard } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import type { RoiDaily, RoiSummary } from '../api'
import { byStartAsc, daysFromToday, fmtKick, fmtShortDate, matchLabel, sideViews } from '../cardview'
import { derivePicks, type Pick, type PickTier } from '../picks'
import { bookAbbrev, fmtAmerican, fmtPct } from '../oddsmath'
import { bookName } from '../config/referrals'
import AsciiBanner from './AsciiBanner'
import BookLink, { BetNowButton } from './BookLink'
import { TrackBetButton } from './PicksPage'
import { Badge, Card, GhostButton, PrimaryButton, StatTile } from './common'
import { cls } from '../utils/format'
import { useUiMode } from '../uiMode'
import { useRoi } from '../share/useRoi'
import { useMyBets } from '../mybets'

// ------------------------------------------------------------------ helpers

const signed = (v: number, digits = 1) => `${v >= 0 ? '+' : ''}${v.toFixed(digits)}`
const pct0 = (p: number) => Math.round(p * 100)

/** "7:05 PM" — friendly local clock time. */
function fmtClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/** "IN 1H 23M" / "IN 43M" until the given moment — null once it has passed. */
function fmtUntil(iso: string, now: Date): string | null {
  const mins = Math.round((new Date(iso).getTime() - now.getTime()) / 60_000)
  if (mins <= 0) return null
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return h > 0 ? `IN ${h}H ${String(m).padStart(2, '0')}M` : `IN ${m}M`
}

// -------------------------------------------------------------- system tiles

type DotTone = 'ok' | 'warn' | 'bad' | 'off'

function Dot({ tone }: { tone: DotTone }) {
  const map: Record<DotTone, string> = {
    ok: 'bg-emerald-400 shadow-[0_0_6px_rgba(52,211,153,0.8)]',
    warn: 'bg-amber-400 shadow-[0_0_6px_rgba(251,191,36,0.7)]',
    bad: 'bg-rose-500 shadow-[0_0_6px_rgba(244,63,94,0.8)]',
    off: 'bg-white/25',
  }
  return <span className={cls('h-1.5 w-1.5 shrink-0 rounded-full', map[tone])} />
}

/** Suite-standard status tile: label + dot, mono value, one-line detail. */
function SystemTile({
  icon: Icon,
  label,
  tone,
  value,
  sub,
}: {
  icon: typeof Server
  label: string
  tone: DotTone
  value: string
  sub?: string
}) {
  return (
    <div className="rounded-xl border border-white/10 bg-white/[0.02] px-3 py-2.5">
      <div className="flex items-center gap-1.5">
        <Icon size={11} className="shrink-0 text-krypt-muted/70" />
        <span className="truncate text-[9px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
          {label}
        </span>
        <span className="ml-auto pl-1">
          <Dot tone={tone} />
        </span>
      </div>
      <div className="mt-1.5 truncate font-mono text-[13px] font-bold leading-none text-white" title={value}>
        {value}
      </div>
      <div className="mt-1 truncate text-[9px] uppercase tracking-wider text-krypt-muted/60" title={sub}>
        {sub ?? ' '}
      </div>
    </div>
  )
}

// ---------------------------------------------------------- record sparkline

/** Compact cumulative-units sparkline — the PerfPage equity curve, shrunk:
 *  emerald above zero, rose below, dashed zero baseline, per-day tooltips.
 *  `clipId` must be unique per instance (SVG clipPaths are document-global). */
function RecordSpark({ daily, clipId }: { daily: RoiDaily[]; clipId: string }) {
  const h = 72
  const pad = 6
  const stepW = 14
  const w = Math.max(daily.length * stepW, 280)
  const vals = daily.map((d) => d.cum_units)
  const hi = Math.max(...vals, 0.5)
  const lo = Math.min(...vals, -0.5)
  const Y = (v: number) => pad + ((hi - v) / (hi - lo)) * (h - 2 * pad)
  const X = (i: number) => (i / Math.max(daily.length - 1, 1)) * w
  const zeroY = Y(0)
  const line = daily
    .map((d, i) => `${i ? 'L' : 'M'}${X(i).toFixed(1)},${Y(d.cum_units).toFixed(1)}`)
    .join(' ')
  const area = `${line} L${w},${zeroY.toFixed(1)} L0,${zeroY.toFixed(1)} Z`
  const last = daily[daily.length - 1]
  return (
    <div className="overflow-x-auto">
      <svg width="100%" height={h} viewBox={`0 0 ${w} ${h}`} preserveAspectRatio="none" className="min-w-[240px]">
        <defs>
          <clipPath id={`${clipId}-above`}>
            <rect x={0} y={0} width={w} height={zeroY} />
          </clipPath>
          <clipPath id={`${clipId}-below`}>
            <rect x={0} y={zeroY} width={w} height={h - zeroY} />
          </clipPath>
        </defs>
        <line x1={0} y1={zeroY} x2={w} y2={zeroY} stroke="rgba(255,255,255,0.18)" strokeWidth="1" strokeDasharray="3 3" />
        <path d={area} fill="rgba(52,211,153,0.22)" clipPath={`url(#${clipId}-above)`} />
        <path d={area} fill="rgba(225,29,72,0.22)" clipPath={`url(#${clipId}-below)`} />
        <path d={line} fill="none" stroke="rgba(52,211,153,0.9)" strokeWidth="1.6" clipPath={`url(#${clipId}-above)`} />
        <path d={line} fill="none" stroke="rgba(244,63,94,0.9)" strokeWidth="1.6" clipPath={`url(#${clipId}-below)`} />
        {last && (
          <circle
            cx={X(daily.length - 1)}
            cy={Y(last.cum_units)}
            r={2.5}
            fill={last.cum_units >= 0 ? 'rgb(52,211,153)' : 'rgb(244,63,94)'}
          />
        )}
        {daily.map((d, i) => (
          <rect key={d.date} x={X(i) - stepW / 2} y={0} width={stepW} height={h} fill="transparent">
            <title>{`${d.date} · ${signed(d.units)}u · cum ${signed(d.cum_units)}u`}</title>
          </rect>
        ))}
      </svg>
    </div>
  )
}

// ------------------------------------------------------------- top pick hero

function TierBadge({ tier, simple }: { tier: PickTier; simple: boolean }) {
  return (
    <span
      className={cls(
        'rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest',
        tier === 'STRONG' && 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300',
        tier === 'LEAN' && 'border-emerald-400/15 bg-emerald-500/[0.05] text-emerald-300/70',
        tier === 'MODEL' && 'border-white/10 bg-white/5 text-krypt-muted',
      )}
    >
      {simple && tier === 'MODEL' ? 'FAVORITE' : tier}
    </span>
  )
}

/** The spotlight card: today's highest-EV pick, or — honestly labeled — the
 *  model's favorite when no book is offering value. */
function TopPickHero({
  pick,
  future,
  demo,
  onNav,
}: {
  pick: Pick
  /** True when the pick comes from an upcoming slate (nothing today). */
  future: boolean
  demo: boolean
  onNav: (page: string) => void
}) {
  const simple = useUiMode() === 'simple'
  const value = pick.kind === 'value'
  const g = pick.card.game
  const kick = fmtKick(g.start_time)
  const league = g.league || SPORT_TAG[pick.sport]
  const opponent =
    pick.side === 'draw'
      ? `${g.away} @ ${g.home}`
      : g.sport === 'tennis'
        ? `vs ${pick.side === 'home' ? g.away : g.home}`
        : pick.side === 'home'
          ? `vs ${g.away}`
          : `@ ${g.home}`

  return (
    <Card className={cls('relative overflow-hidden', value && 'shadow-scout-glow')}>
      {/* Scout-red spine — "this is the call". */}
      <span
        className={cls(
          'absolute bottom-4 left-0 top-4 w-[3px] rounded-r-full',
          value ? 'bg-scout-red/80' : 'bg-scout-red/30',
        )}
      />
      <div className="pl-2">
        {/* Eyebrow: what this card is + tier + league + kickoff. */}
        <div className="flex flex-wrap items-center gap-2">
          <span className="flex items-center gap-1.5 text-[9px] font-bold uppercase tracking-[0.25em] text-scout-red-bright">
            <Target size={11} />
            {value ? 'TOP PICK' : 'MODEL FAVORITE'}
          </span>
          <TierBadge tier={pick.tier} simple={simple} />
          {future && <Badge tone="warn">{fmtShortDate(g.start_time)} SLATE</Badge>}
          <span className="ml-auto rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-krypt-muted">
            {league}
          </span>
          <span className="font-mono text-[10px] text-krypt-muted/60">
            {kick.date} {kick.time}
          </span>
        </div>

        {/* Headline: team + price + book + BET NOW. */}
        <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-2">
          <div className="min-w-0">
            <div className="truncate text-2xl font-bold leading-tight text-white" title={pick.team}>
              {pick.team}
            </div>
            <div className="mt-0.5 truncate text-[11px] font-semibold text-krypt-muted" title={opponent}>
              {opponent}
              {!simple && <span className="text-krypt-muted/50"> · MONEYLINE</span>}
            </div>
          </div>
          <span className="ml-auto flex shrink-0 items-center gap-2">
            {value ? (
              <>
                <span className="font-mono text-3xl font-bold leading-none text-emerald-300">
                  {fmtAmerican(pick.price as number)}
                </span>
                <BookLink book={pick.book as string}>
                  <Badge tone="success">
                    {simple ? bookName(pick.book as string) : bookAbbrev(pick.book as string)}
                  </Badge>
                </BookLink>
                <BetNowButton book={pick.book as string} />
                <TrackBetButton pick={pick} demo={demo} />
              </>
            ) : simple ? (
              <TrackBetButton pick={pick} demo={demo} />
            ) : (
              <>
                <span className="font-mono text-3xl font-bold leading-none text-white/85">
                  {fmtAmerican(pick.fairPrice)}
                </span>
                <Badge tone="neutral">FAIR · NO LIVE EDGE</Badge>
                <TrackBetButton pick={pick} demo={demo} />
              </>
            )}
          </span>
        </div>

        {/* Detail: plain sentence in simple, numbers in pro. */}
        {simple ? (
          <p className="mt-3 border-t border-white/5 pt-2.5 text-sm leading-relaxed text-white/80">
            {value ? (
              <>
                We give <span className="font-semibold text-white">{pick.team}</span> a{' '}
                <span className="font-mono font-semibold text-emerald-300">{pct0(pick.modelProb)}%</span>{' '}
                chance to win — at{' '}
                <span className="font-mono font-semibold text-white">{fmtAmerican(pick.price as number)}</span>{' '}
                the market only implies{' '}
                <span className="font-mono font-semibold text-krypt-muted">
                  {pick.marketProb !== null ? pct0(pick.marketProb) : '—'}%
                </span>
                .
              </>
            ) : (
              <>
                Our model favors <span className="font-semibold text-white">{pick.team}</span> at{' '}
                <span className="font-mono font-semibold text-emerald-300">{pct0(pick.modelProb)}%</span> — no
                sportsbook edge right now.
              </>
            )}
          </p>
        ) : (
          <div className="mt-3 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-white/5 pt-2.5 font-mono text-[10px] leading-none">
            <span>
              <span className="text-emerald-300">MODEL {fmtPct(pick.modelProb)}%</span>
              <span className="text-krypt-muted/40"> vs </span>
              <span className="text-krypt-muted">
                MARKET {pick.marketProb !== null ? `${fmtPct(pick.marketProb)}%` : '—'}
              </span>
            </span>
            {value ? (
              <span className="ml-auto">
                <span
                  className="font-semibold text-emerald-300"
                  title="EV = expected value: average profit per $1 bet if the model probability is right"
                >
                  +{fmtPct(pick.ev as number)}% EV
                </span>
                <span className="text-krypt-muted/40"> · </span>
                <span
                  className="text-emerald-300/70"
                  title="Kelly = bankroll-sizing formula; shown at quarter strength for safety"
                >
                  1/4 KELLY {fmtPct((pick.kelly as number) / 4)}%
                </span>
              </span>
            ) : (
              <span className="ml-auto text-krypt-muted">
                {pick.confidence !== null ? `CONF ${fmtPct(pick.confidence)}%` : 'NO LIVE EDGE'}
              </span>
            )}
          </div>
        )}

        {/* Footer: route to the full picks board. */}
        <div className="mt-3 flex items-center justify-between border-t border-white/5 pt-2.5">
          <span className="text-[9px] uppercase tracking-wider text-krypt-muted/60">
            {value
              ? simple
                ? 'Best price across every book we track'
                : `Best price across ${pick.card.odds.length} quoted ${pick.card.odds.length === 1 ? 'book' : 'books'}`
              : simple
                ? 'No betting edge — just who we think wins'
                : 'Argmax side at the model fair line'}
          </span>
          <button
            onClick={() => onNav('picks')}
            className="flex items-center gap-1 text-[10px] font-bold uppercase tracking-widest text-scout-red-bright transition hover:text-white"
          >
            SEE ALL PICKS
            <ArrowRight size={11} />
          </button>
        </div>
      </div>
    </Card>
  )
}

// --------------------------------------------------------- today-at-a-glance

function GlanceRow({
  card,
  live,
  onOpen,
}: {
  card: GameCard
  live: boolean
  onOpen: () => void
}) {
  const g = card.game
  const sides = sideViews(card)
  const fav = sides.length > 0 ? sides.reduce((a, b) => (b.prob > a.prob ? b : a)) : null
  const hasScore = g.home_score !== null && g.away_score !== null
  return (
    <button
      onClick={onOpen}
      className="flex w-full items-center gap-3 border-b border-white/5 px-4 py-2 text-left transition last:border-0 hover:bg-white/[0.03]"
      title={`${matchLabel(card)} — open TODAY`}
    >
      <span className="w-[68px] shrink-0">
        {live ? (
          <span className="inline-flex items-center gap-1.5 rounded border border-scout-red/40 bg-scout-red/10 px-1.5 py-0.5 font-mono text-[9px] font-bold tracking-widest text-scout-red-bright">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-scout-red-bright" />
            LIVE
          </span>
        ) : (
          <span className="font-mono text-[10px] font-semibold text-white/75">
            {fmtClock(g.start_time)}
          </span>
        )}
      </span>
      <span className="w-11 shrink-0 rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-center text-[9px] font-bold uppercase tracking-widest text-krypt-muted">
        {SPORT_TAG[g.sport]}
      </span>
      <span className="min-w-0 flex-1 truncate text-xs font-semibold text-white/85">
        {matchLabel(card)}
      </span>
      {live && hasScore ? (
        <span className="shrink-0 font-mono text-xs font-bold text-white">
          {g.away_score}–{g.home_score}
        </span>
      ) : fav ? (
        <span className="flex min-w-0 shrink-0 items-center gap-1.5 font-mono text-[10px]">
          <span className="max-w-[88px] truncate text-krypt-muted/70" title={fav.name}>
            {fav.name}
          </span>
          <span className="font-semibold text-emerald-300">{pct0(fav.prob)}%</span>
        </span>
      ) : (
        <span className="shrink-0 font-mono text-[10px] text-krypt-muted/40">—</span>
      )}
    </button>
  )
}

// -------------------------------------------------------------------- page

export default function DashboardPage({
  boards,
  demo,
  apiUp,
  onNav,
}: {
  boards: Record<string, GameCard[]>
  demo: boolean
  apiUp: boolean | null
  onNav: (page: string) => void
}) {
  const simple = useUiMode() === 'simple'
  const roi = useRoi(demo)

  // Clock tick so live/upcoming buckets and countdowns stay current.
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000)
    return () => clearInterval(t)
  }, [])

  // Supervisor surface (Electron only) — every access ?.-guarded so plain
  // browser dev renders the same dashboard with fallback tiles.
  const isElectron = typeof window !== 'undefined' && !!window.scout
  const [backend, setBackend] = useState<BackendInfo | null>(null)
  const [sched, setSched] = useState<SchedulerInfo | null>(null)
  const [discord, setDiscord] = useState<DiscordInfo | null>(null)
  useEffect(() => {
    const s = window.scout
    if (!s) return
    void s.backend?.info().then(setBackend).catch(() => {})
    void s.scheduler?.info().then(setSched).catch(() => {})
    void s.discord?.info().then(setDiscord).catch(() => {})
    return s.backend?.onStatus(setBackend)
  }, [])

  // ---- slate + picks, derived from the boards prop ------------------------
  const all = useMemo(() => Object.values(boards).flat(), [boards])

  const { todays, live, upcoming, sportsOn, glance } = useMemo(() => {
    const todays = all
      .filter((c) => daysFromToday(c.game.start_time, now) === 0)
      .sort(byStartAsc)
    const nowMs = now.getTime()
    const live = todays.filter(
      (c) => !c.game.completed && new Date(c.game.start_time).getTime() <= nowMs,
    )
    const upcoming = todays.filter(
      (c) => !c.game.completed && new Date(c.game.start_time).getTime() > nowMs,
    )
    const sportsOn = new Set(todays.map((c) => c.game.sport)).size
    return { todays, live, upcoming, sportsOn, glance: [...live, ...upcoming].slice(0, 6) }
  }, [all, now])

  const allPicks = useMemo(() => derivePicks(all), [all])
  const todayPicks = useMemo(
    () => allPicks.filter((p) => daysFromToday(p.card.game.start_time, now) <= 0),
    [allPicks, now],
  )
  const valuePicks = todayPicks.filter((p) => p.kind === 'value').length
  // derivePicks sorts STRONG→LEAN by EV, then MODEL by confidence — so [0] is
  // today's best value pick, or the most confident model pick when no edge.
  const topPick = todayPicks[0] ?? allPicks[0] ?? null
  const topPickFuture = topPick !== null && todayPicks.length === 0

  // First upcoming slate beyond today — surfaced when today is empty.
  const nextSlate = useMemo(() => {
    const future = all
      .filter((c) => !c.game.completed && daysFromToday(c.game.start_time, now) > 0)
      .sort(byStartAsc)
    return future.length > 0 ? { date: fmtShortDate(future[0].game.start_time), n: future.length } : null
  }, [all, now])

  // ---- record (flat-1u graded picks) ---------------------------------------
  const graded = roi && roi.overall.n_graded > 0 ? (roi as RoiSummary) : null
  const units = graded?.overall.units ?? null

  // ---- the user's own tracker — takes over the hero tile once it has bets --
  const myBets = useMyBets(demo)
  const myPnl = myBets.bets.length > 0 || myBets.parlays.length > 0 ? myBets.pnl : null

  // ---- system tile states ---------------------------------------------------
  const backendTile = (() => {
    if (!isElectron) {
      return {
        value: apiUp === null ? 'CHECKING…' : apiUp ? 'ONLINE' : 'OFFLINE',
        tone: (apiUp === null ? 'warn' : apiUp ? 'ok' : 'bad') as DotTone,
        sub: 'browser mode · dev proxy',
      }
    }
    const st = backend?.status
    const tone: DotTone =
      st === 'running' ? 'ok' : st === 'starting' || st === 'restarting' ? 'warn' : st ? 'bad' : 'off'
    return {
      value: st ? st.toUpperCase() : '—',
      tone,
      sub: backend?.port ? `127.0.0.1:${backend.port}` : (backend?.lastError ?? 'supervisor starting'),
    }
  })()

  const autoTile = (() => {
    if (!isElectron) return { value: 'DESKTOP ONLY', tone: 'off' as DotTone, sub: 'capture + settle run in the app' }
    if (!sched) return { value: '—', tone: 'off' as DotTone, sub: 'scheduler loading' }
    if (sched.inFlight) {
      return { value: `${sched.inFlight.toUpperCase()}…`, tone: 'ok' as DotTone, sub: 'job running now' }
    }
    if (!sched.config.enabled) {
      return { value: 'OFF', tone: 'warn' as DotTone, sub: 'enable in settings' }
    }
    const next = (['capture', 'settle'] as const)
      .map((job) => ({ job, at: sched.nextRuns[job] }))
      .filter((e): e is { job: 'capture' | 'settle'; at: string } => e.at !== null)
      .sort((a, b) => new Date(a.at).getTime() - new Date(b.at).getTime())[0]
    if (!next) return { value: 'ARMED', tone: 'ok' as DotTone, sub: 'no run scheduled yet' }
    return {
      value: next.job.toUpperCase(),
      tone: 'ok' as DotTone,
      sub: `next ${fmtUntil(next.at, now)?.toLowerCase() ?? 'due now'} · ${fmtClock(next.at)}`,
    }
  })()

  const castTile = (() => {
    if (!isElectron) return { value: 'DESKTOP ONLY', tone: 'off' as DotTone, sub: 'discord posts in the app' }
    const targets = discord?.config.webhooks.length ?? 0
    if (targets === 0) return { value: 'NOT SET', tone: 'off' as DotTone, sub: 'add a discord webhook' }
    const nextSend = discord?.nextSend ?? undefined
    return {
      value: `${targets} ${targets === 1 ? 'TARGET' : 'TARGETS'}`,
      tone: (nextSend ? 'ok' : 'warn') as DotTone,
      sub: nextSend
        ? `auto-post ${fmtUntil(nextSend, now)?.toLowerCase() ?? 'due now'}`
        : 'manual send only',
    }
  })()

  const dataTile = (() => {
    const boardsOn = SPORTS.filter((s) => (boards[s]?.length ?? 0) > 0).length
    const books = new Set(all.flatMap((c) => c.odds.map((o) => o.book))).size
    return {
      value: `${boardsOn}/${SPORTS.length} BOARDS`,
      tone: (boardsOn > 0 ? 'ok' : demo ? 'warn' : apiUp === false ? 'bad' : 'warn') as DotTone,
      sub: demo
        ? 'bundled demo slate'
        : books > 0
          ? `${books} ${books === 1 ? 'book' : 'books'} quoted`
          : 'no live odds yet',
    }
  })()

  const weekday = now.toLocaleDateString('en-US', { weekday: 'long' }).toUpperCase()
  const monthDay = now.toLocaleDateString('en-US', { month: 'short', day: 'numeric' }).toUpperCase()
  const recordLabel = simple ? 'RECORD' : 'UNITS'

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header strip — consistent with TODAY / PICKS page chrome. */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-white/5 bg-krypt-panel/40 px-4 py-2">
        <span className="flex items-center gap-2">
          <Gauge size={13} className="text-scout-red-bright" />
          <span className="font-mono text-[11px] font-bold tracking-widest text-white">
            {simple ? 'HOME' : 'MISSION OVERVIEW'}
          </span>
        </span>
        <span className="hidden text-[10px] uppercase tracking-wider text-krypt-muted/60 sm:inline">
          {simple ? "Today's games, best pick, and status" : 'slate · picks · record · systems'}
        </span>
        <div className="ml-auto flex items-center gap-2">
          {demo && <Badge tone="gradient">DEMO</Badge>}
          {apiUp === false && !demo && <Badge tone="danger">API DOWN</Badge>}
        </div>
      </div>

      <main className="min-h-0 flex-1 overflow-y-auto px-4 pb-4 pt-4">
        {apiUp === false && !demo && (
          <div className="mb-4 rounded-xl border border-amber-400/25 bg-amber-500/[0.07] px-3 py-2 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
            {simple
              ? 'Data feed offline — turn on demo mode (any board page) to look around.'
              : 'API offline — live feed unavailable. Enable demo mode to review the terminal.'}
          </div>
        )}

        {/* 0 · The marquee — same glowing-red KRYPT SCOUT as the terminal. */}
        <AsciiBanner className="mb-4 rounded-2xl border border-white/5 bg-black/30 py-3" />

        {/* 1 · Hero strip — date + the five numbers that matter. */}
        <div className="grid grid-cols-2 gap-3 md:grid-cols-3 xl:grid-cols-6">
          <div className="rounded-2xl border border-scout-red/25 bg-scout-red/[0.06] px-4 py-3 shadow-krypt-card">
            <div className="flex items-center gap-1.5 text-[10px] font-semibold uppercase tracking-[0.18em] text-scout-red-bright">
              <CalendarDays size={11} />
              {weekday}
            </div>
            <div className="mt-1 font-mono text-2xl font-bold leading-none tracking-tight text-white">
              {monthDay}
            </div>
            <div className="mt-1.5 text-[10px] uppercase tracking-wider text-krypt-muted/70">
              {demo ? 'demo slate' : 'live slate'}
            </div>
          </div>
          <StatTile
            label="Games today"
            value={todays.length}
            sub={
              todays.length > 0
                ? `${sportsOn} ${sportsOn === 1 ? 'sport' : 'sports'} on the board`
                : nextSlate
                  ? `next slate ${nextSlate.date}`
                  : 'nothing scheduled'
            }
          />
          <StatTile
            label="Live now"
            value={live.length}
            tone={live.length > 0 ? 'accent' : 'neutral'}
            sub={live.length > 0 ? 'in progress' : 'none in progress'}
          />
          <StatTile
            label="Picks today"
            value={todayPicks.length}
            tone={valuePicks > 0 ? 'success' : 'neutral'}
            sub={
              simple
                ? valuePicks > 0
                  ? `${valuePicks} with a betting edge`
                  : 'model favorites only'
                : `${valuePicks} value · ${todayPicks.length - valuePicks} model`
            }
          />
          {/* Both records, side by side: the bot's full pick record AND the
              user's own — the tracker's two tabs, condensed to two tiles. */}
          <StatTile
            label={simple ? 'Bot record' : `Bot ${recordLabel.toLowerCase()}`}
            value={units !== null ? `${signed(units)}u` : '—'}
            tone={units === null ? 'neutral' : units >= 0 ? 'success' : 'danger'}
            sub={
              graded
                ? simple
                  ? `after ${graded.overall.n_graded} settled picks`
                  : `${signed(graded.overall.roi_pct)}% ROI · ${graded.overall.n_graded} graded · flat 1u`
                : 'no settled picks yet'
            }
            help="Net units won at flat 1-unit stakes across every graded value pick (TRACKER → BOT PICKS)"
          />
          <StatTile
            label="My record"
            value={myPnl ? `${signed(myPnl.units)}u` : '—'}
            tone={!myPnl ? 'neutral' : myPnl.units >= 0 ? 'success' : 'danger'}
            sub={myPnl ? myPnl.record : 'track a bet to start'}
            help="Net units across the bets YOU tracked — including auto-bets (TRACKER → MY BETS)"
          />
        </div>

        {/* 2 · Main zone — spotlight + slate (2) | systems + record (1). */}
        <div className="mt-4 grid gap-4 xl:grid-cols-3">
          <div className="flex min-w-0 flex-col gap-4 xl:col-span-2">
            {topPick ? (
              <TopPickHero pick={topPick} future={topPickFuture} demo={demo} onNav={onNav} />
            ) : (
              <Card className="border-dashed">
                <div className="flex flex-col items-center py-6 text-center">
                  <Target size={20} className="mb-2 text-scout-red-bright/70" />
                  <div className="text-sm font-semibold text-white">No pick to spotlight</div>
                  <p className="mt-1 max-w-md text-xs text-krypt-muted">
                    {simple
                      ? 'Picks appear here as soon as games with predictions are on the board. Turn on demo mode to see an example.'
                      : 'Picks derive from boards with model output — value picks additionally need live odds (ODDS_API_KEY in SETTINGS). Flip on demo mode to explore.'}
                  </p>
                  <GhostButton className="mt-3 px-3 py-1.5 text-xs" onClick={() => onNav('picks')}>
                    GO TO PICKS
                    <ArrowRight size={12} />
                  </GhostButton>
                </div>
              </Card>
            )}

            {/* Today at a glance — next ~6 games, LIVE pinned. */}
            <Card padded={false} className="overflow-hidden">
              <div className="flex items-center gap-2 border-b border-white/10 bg-white/[0.02] px-4 py-2.5">
                <span className="relative -top-px inline-block h-2.5 w-0.5 shrink-0 self-center rounded-full bg-scout-red shadow-scout-glow" />
                <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
                  Today at a glance
                </span>
                <span className="truncate text-[10px] uppercase tracking-wider text-krypt-muted/70">
                  {todays.length > 0
                    ? `${live.length} live · ${upcoming.length} upcoming`
                    : 'nothing today'}
                </span>
                <button
                  onClick={() => onNav('today')}
                  className="ml-auto flex shrink-0 items-center gap-1 text-[10px] font-bold uppercase tracking-widest text-krypt-muted transition hover:text-white"
                >
                  FULL SLATE
                  <ArrowRight size={11} />
                </button>
              </div>
              {glance.length > 0 ? (
                <div>
                  {glance.map((card) => (
                    <GlanceRow
                      key={card.game.id}
                      card={card}
                      live={
                        !card.game.completed &&
                        new Date(card.game.start_time).getTime() <= now.getTime()
                      }
                      onOpen={() => onNav('today')}
                    />
                  ))}
                </div>
              ) : (
                <div className="px-4 py-6 text-center">
                  <div className="text-xs font-semibold text-white/80">
                    {todays.length > 0 ? "Today's slate is done" : "Nothing on today's slate"}
                  </div>
                  <p className="mt-1 text-[10px] uppercase tracking-wider text-krypt-muted/60">
                    {todays.length > 0
                      ? 'every game has gone final — see TODAY for scores'
                      : nextSlate
                        ? `next slate ${nextSlate.date} · ${nextSlate.n} ${nextSlate.n === 1 ? 'game' : 'games'} tracked`
                        : simple
                          ? 'check the boards for upcoming games'
                          : 'off-season feeds often carry only far-future slates'}
                  </p>
                </div>
              )}
            </Card>
          </div>

          {/* Right rail: system tiles + record sparkline. */}
          <div className="flex min-w-0 flex-col gap-4">
            <Card padded={false} className="overflow-hidden">
              <div className="flex items-center gap-2 border-b border-white/10 bg-white/[0.02] px-4 py-2.5">
                <span className="relative -top-px inline-block h-2.5 w-0.5 shrink-0 self-center rounded-full bg-scout-red shadow-scout-glow" />
                <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
                  System
                </span>
              </div>
              <div className="grid grid-cols-2 gap-2 p-3">
                <SystemTile icon={Server} label="Backend" tone={backendTile.tone} value={backendTile.value} sub={backendTile.sub} />
                <SystemTile icon={Timer} label="Automation" tone={autoTile.tone} value={autoTile.value} sub={autoTile.sub} />
                <SystemTile icon={Radio} label="Broadcast" tone={castTile.tone} value={castTile.value} sub={castTile.sub} />
                <SystemTile icon={Database} label="Data" tone={dataTile.tone} value={dataTile.value} sub={dataTile.sub} />
              </div>
            </Card>

            {/* Two equity curves, same scale rules: the bot's whole record
                and the user's own — the dashboard mirror of the tracker tabs. */}
            <Card>
              <div className="mb-2 flex items-baseline justify-between gap-2">
                <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                  {simple ? 'Bot record · last 30 days' : 'Bot P&L · cum units'}
                </span>
                {graded && graded.daily.length > 0 && (
                  <span
                    className={cls(
                      'font-mono text-xs font-bold',
                      graded.daily[graded.daily.length - 1].cum_units >= 0
                        ? 'text-emerald-300'
                        : 'text-rose-400',
                    )}
                  >
                    {signed(graded.daily[graded.daily.length - 1].cum_units)}u
                  </span>
                )}
              </div>
              {graded && graded.daily.length > 0 ? (
                <RecordSpark daily={graded.daily} clipId="dash-bot-eq" />
              ) : (
                <div className="py-5 text-center text-[10px] uppercase tracking-wider text-krypt-muted/60">
                  {simple
                    ? 'The record builds as games finish'
                    : 'No graded picks yet — record builds as games settle'}
                </div>
              )}
              {simple ? (
                <div className="mt-2 text-[9px] uppercase tracking-wider text-krypt-muted/60">
                  Every pick counted the same, win or lose
                </div>
              ) : (
                <button
                  onClick={() => onNav('perf')}
                  className="mt-2 flex items-center gap-1 text-[10px] font-bold uppercase tracking-widest text-krypt-muted transition hover:text-white"
                >
                  FULL PERFORMANCE
                  <ArrowRight size={11} />
                </button>
              )}
            </Card>

            <Card>
              <div className="mb-2 flex items-baseline justify-between gap-2">
                <span className="text-[10px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
                  {simple ? 'My record · your bets' : 'My P&L · cum units'}
                </span>
                {myPnl && myPnl.daily.length > 0 && (
                  <span
                    className={cls(
                      'font-mono text-xs font-bold',
                      myPnl.daily[myPnl.daily.length - 1].cum_units >= 0
                        ? 'text-emerald-300'
                        : 'text-rose-400',
                    )}
                  >
                    {signed(myPnl.daily[myPnl.daily.length - 1].cum_units)}u
                  </span>
                )}
              </div>
              {myPnl && myPnl.daily.length > 0 ? (
                <RecordSpark daily={myPnl.daily} clipId="dash-my-eq" />
              ) : (
                <div className="py-5 text-center text-[10px] uppercase tracking-wider text-krypt-muted/60">
                  {simple
                    ? 'Track a bet — your own curve starts here'
                    : 'No graded bets yet — TRACK picks (or run auto-bet) and your curve builds here'}
                </div>
              )}
              <button
                onClick={() => onNav('tracker')}
                className="mt-2 flex items-center gap-1 text-[10px] font-bold uppercase tracking-widest text-krypt-muted transition hover:text-white"
              >
                MY TRACKER
                <ArrowRight size={11} />
              </button>
            </Card>
          </div>
        </div>

        {/* 3 · Quick actions. */}
        <div className="mt-4 flex flex-wrap items-center gap-2">
          <PrimaryButton onClick={() => onNav('picks')}>
            <Target size={14} />
            VIEW PICKS
          </PrimaryButton>
          <GhostButton onClick={() => onNav('terminal')}>
            <SquareTerminal size={14} />
            OPEN TERMINAL
          </GhostButton>
          <GhostButton onClick={() => onNav('parlay')}>
            <Layers size={14} />
            BUILD PARLAY
          </GhostButton>
          <GhostButton onClick={() => onNav('tracker')}>
            <NotebookPen size={14} />
            MY TRACKER
          </GhostButton>
          <GhostButton onClick={() => onNav('discord')}>
            <Webhook size={14} />
            SEND TO DISCORD
          </GhostButton>
        </div>
      </main>

      {/* Footer strip — matches the other pages. */}
      <footer className="shrink-0 border-t border-white/5 bg-krypt-panel/40 px-4 py-1.5">
        <span className="text-[9px] uppercase tracking-wider text-krypt-muted/50">
          {simple
            ? "Your home base — today's games, the best pick, and system status at a glance. Only bet what you can afford to lose. 21+."
            : 'MISSION OVERVIEW · boards poll 60s · picks derive live from boards · record = flat 1u graded value picks · not betting advice · 21+'}
        </span>
      </footer>
    </div>
  )
}
