import { useMemo, useState, type MouseEvent } from 'react'
import { Check, FlaskConical, Pin, Plus, RefreshCw, Share2, Target, Wallet, X } from 'lucide-react'
import type { BoardMap, GameCard, Sport } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import { fmtAmerican, fmtPct } from '../oddsmath'
import { daysFromToday, fmtKick } from '../cardview'
import { derivePicks, lineShop, type Pick, type PickTier } from '../picks'
import { bookName } from '../config/referrals'
import { defaultStake, isPreferredBook, orderBooks, usePrefsVersion } from '../prefs'
import DetailDrawer from './DetailDrawer'
import BookLink, { BetNowButton } from './BookLink'
import { Badge, Empty, Eyebrow, StripBtn } from './common'
import { cls } from '../utils/format'
import { useShareCard } from '../share/useShareCard'
import { useRoi } from '../share/useRoi'
import { useMyBets } from '../mybets'

/** Compact TRACK BET button — "the plus button on each play". One tap tracks
 *  the pick at the user's default stake (SETTINGS, 1u out of the box; model
 *  picks with no book price track at the model fair line, noted); tracked
 *  state flips to a disabled TRACKED check. */
export function TrackBetButton({ pick, demo }: { pick: Pick; demo: boolean }) {
  const { track, isTracked, busy } = useMyBets(demo)
  const [failed, setFailed] = useState(false)
  usePrefsVersion() // re-render when the default stake changes
  const stake = defaultStake()
  const tracked = isTracked(pick.gameId, pick.side)
  const onClick = (e: MouseEvent) => {
    e.stopPropagation()
    if (tracked || busy) return
    void track(
      {
        game_id: pick.gameId,
        side: pick.side,
        price: pick.price ?? pick.fairPrice,
        book: pick.book,
        stake_units: stake,
        note: pick.price === null ? 'tracked at fair (no book price)' : null,
      },
      pick.card,
    ).then((ok) => {
      if (!ok) {
        setFailed(true)
        window.setTimeout(() => setFailed(false), 3000)
      }
    })
  }
  return (
    <button
      type="button"
      data-tour="pick-track"
      onClick={onClick}
      disabled={tracked || busy}
      title={
        failed
          ? "Couldn't reach the backend — is it running? (SETTINGS → BACKEND, or restart the app)"
          : tracked
            ? 'Tracked — this bet is on your TRACKER'
            : pick.price === null
              ? `Track this pick at the model fair line (${stake} unit${stake === 1 ? '' : 's'} — change under SETTINGS) — it grades on your TRACKER when the game settles`
              : `Track this bet (${stake} unit${stake === 1 ? '' : 's'} — change under SETTINGS) — it grades on your TRACKER when the game settles`
      }
      className={cls(
        'inline-flex shrink-0 items-center gap-1 rounded-lg border px-2.5 py-1 text-[10px] font-bold uppercase tracking-widest transition',
        failed
          ? 'border-rose-400/50 bg-rose-500/15 text-rose-300'
          : tracked
            ? 'cursor-default border-emerald-400/40 bg-emerald-500/15 text-emerald-300'
            : 'border-emerald-400/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/25 active:scale-[0.98] disabled:opacity-60',
      )}
    >
      {failed ? <X size={11} /> : tracked ? <Check size={11} /> : <Plus size={11} />}
      {failed ? 'FAILED — RETRY' : tracked ? 'TRACKED' : stake === 1 ? 'TRACK BET' : `TRACK · ${stake}u`}
    </button>
  )
}

type SportFilter = Sport | 'all'
const TIERS: PickTier[] = ['STRONG', 'LEAN', 'MODEL']

// TODAY (default) shows only games on today's local slate (incl. live
// spillover from before midnight); ALL is the full tracked board.
type PickScope = 'today' | 'all'
const SCOPE_KEY = 'scout.picks-scope'

function loadScope(): PickScope {
  try {
    return localStorage.getItem(SCOPE_KEY) === 'all' ? 'all' : 'today'
  } catch {
    return 'today'
  }
}

const SCOPE_HELP: Record<PickScope, string> = {
  today: "Only picks for games on today's local slate",
  all: 'Every tracked pick, including future slates',
}

const TIER_HELP: Record<PickTier, string> = {
  STRONG:
    'Our best bets — the payout on offer is clearly bigger than the risk we measure (4%+ expected profit per $1). If you only bet a little, bet here.',
  LEAN: 'A small edge — the price is a bit better than it should be, but not by much. Worth less of your money than a STRONG pick.',
  MODEL:
    'We like this team to win, but no sportsbook is paying extra for it — so there is nothing to gain long-run. Fine to skip, or track it free to test us.',
}

/** Tier label shown to the user — MODEL always reads as FAVORITE here. */
function tierLabel(tier: PickTier): string {
  return tier === 'MODEL' ? 'FAVORITE' : tier
}

/** Matchup context from the pick side's point of view. */
function contextLabel(p: Pick): string {
  const g = p.card.game
  if (p.side === 'draw') return `${g.away} @ ${g.home}`
  if (g.sport === 'tennis') return `vs ${p.side === 'home' ? g.away : g.home}`
  return p.side === 'home' ? `vs ${g.away}` : `@ ${g.home}`
}

/** Model-vs-market probability mini bars (market row dims out for MODEL picks). */
function ProbBars({ model, market }: { model: number; market: number | null }) {
  return (
    <div className="flex items-center gap-2">
      <div className="w-14 shrink-0 space-y-[3px]">
        <div className="h-[3px] overflow-hidden rounded-full bg-white/[0.06]">
          <div className="h-full rounded-full bg-emerald-400/80" style={{ width: `${model * 100}%` }} />
        </div>
        <div className="h-[3px] overflow-hidden rounded-full bg-white/[0.06]">
          {market !== null && (
            <div className="h-full rounded-full bg-white/30" style={{ width: `${market * 100}%` }} />
          )}
        </div>
      </div>
      <span
        className="whitespace-nowrap font-mono text-[10px] leading-none"
        title="Model win probability / market-implied probability (vig removed)"
      >
        <span className="text-emerald-300">{fmtPct(model)}</span>
        <span className="text-krypt-muted/40"> / </span>
        <span className="text-krypt-muted">{market !== null ? fmtPct(market) : '—'}</span>
      </span>
    </div>
  )
}

function TierBadge({ tier }: { tier: PickTier }) {
  return (
    <span
      title={TIER_HELP[tier]}
      className={cls(
        'rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest',
        tier === 'STRONG' && 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300',
        tier === 'LEAN' && 'border-emerald-400/15 bg-emerald-500/[0.05] text-emerald-300/70',
        tier === 'MODEL' && 'border-white/10 bg-white/5 text-krypt-muted',
      )}
    >
      {tierLabel(tier)}
    </span>
  )
}

/** Whole-number percent for plain-English copy. */
const pct0 = (p: number) => Math.round(p * 100)

/** Risk bucket from the model win probability — favorites are steadier, dogs
 *  pay more but lose more often. Purely presentational. */
function riskLevel(p: number): 'LOW RISK' | 'MEDIUM RISK' | 'HIGH RISK' {
  if (p >= 0.6) return 'LOW RISK'
  if (p >= 0.45) return 'MEDIUM RISK'
  return 'HIGH RISK'
}

function riskHelp(p: number): string {
  const base = `Risk = how often a bet like this loses. We give it a ${pct0(p)}% chance to win, so expect it to lose about ${100 - pct0(p)} times out of 100.`
  if (p >= 0.6) return `${base} Low risk wins often but pays less per win.`
  if (p >= 0.45) return `${base} Close to a coin flip — wins and losses come in streaks.`
  return `${base} High risk loses most of the time, but pays big when it hits.`
}

function RiskBadge({ prob }: { prob: number }) {
  const level = riskLevel(prob)
  return (
    <span
      data-tour="pick-risk"
      title={riskHelp(prob)}
      className={cls(
        'rounded border px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest',
        level === 'LOW RISK' && 'border-sky-400/30 bg-sky-500/10 text-sky-300',
        level === 'MEDIUM RISK' && 'border-amber-400/30 bg-amber-500/10 text-amber-300',
        level === 'HIGH RISK' && 'border-rose-400/30 bg-rose-500/10 text-rose-300',
      )}
    >
      {level}
    </span>
  )
}

/** "How much to bet" — quarter-Kelly spelled out in plain English, with a
 *  concrete dollars-per-$1,000 example. Model picks get an honest "nothing". */
function StakeRow({ pick }: { pick: Pick }) {
  const quarter = pick.kelly !== null ? pick.kelly / 4 : null
  return (
    <div
      data-tour="pick-stake"
      title={
        quarter !== null
          ? 'Quarter-Kelly sizing: the bigger the edge and the payout, the more you bet — quartered for safety. Your bankroll is the money you set aside just for betting and can afford to lose completely.'
          : 'No sportsbook is paying more than this bet is worth, so no amount of money makes it profitable. Track it free to test the model instead.'
      }
      className="ml-1.5 mt-2 flex flex-wrap items-center gap-x-1.5 gap-y-0.5 rounded-lg border border-white/5 bg-white/[0.02] px-2 py-1.5"
    >
      <Wallet size={11} className="shrink-0 text-krypt-muted/60" />
      <span className="text-[9px] font-bold uppercase tracking-[0.2em] text-krypt-muted/70">
        How much
      </span>
      {quarter !== null ? (
        <span className="text-[11px] leading-tight text-white/80">
          <span className="font-mono font-semibold text-emerald-300">
            {quarter < 0.001 ? '<0.1' : fmtPct(quarter)}%
          </span>{' '}
          of your bankroll — about{' '}
          <span className="font-mono font-semibold text-white">
            ${Math.max(1, Math.round(quarter * 1000))}
          </span>{' '}
          per $1,000 you bet with. Never more.
        </span>
      ) : (
        <span className="text-[11px] leading-tight text-krypt-muted">
          Nothing — no extra payout on offer. Track it free instead.
        </span>
      )}
    </div>
  )
}

/** The one plain-English sentence every card leads with. */
function PlainSentence({ pick }: { pick: Pick }) {
  if (pick.kind === 'value') {
    return (
      <p className="mt-2 border-t border-white/5 pl-1.5 pt-2 text-sm leading-relaxed text-white/80">
        We give <span className="font-semibold text-white">{pick.team}</span> a{' '}
        <span className="font-mono font-semibold text-emerald-300">{pct0(pick.modelProb)}%</span>{' '}
        chance to win — at{' '}
        <span className="font-mono font-semibold text-white">{fmtAmerican(pick.price as number)}</span>{' '}
        the market only implies{' '}
        <span className="font-mono font-semibold text-krypt-muted">
          {pick.marketProb !== null ? pct0(pick.marketProb) : '—'}%
        </span>
        .
      </p>
    )
  }
  if (pick.card.prediction?.model === 'market-consensus') {
    return (
      <p className="mt-2 border-t border-white/5 pl-1.5 pt-2 text-sm leading-relaxed text-white/80">
        The market has <span className="font-semibold text-white">{pick.team}</span> at{' '}
        <span className="font-mono font-semibold text-emerald-300">{pct0(pick.modelProb)}%</span> —
        our model doesn't rate these players yet, so this is the market's number, not ours.
      </p>
    )
  }
  return (
    <p className="mt-2 border-t border-white/5 pl-1.5 pt-2 text-sm leading-relaxed text-white/80">
      Our model favors <span className="font-semibold text-white">{pick.team}</span> at{' '}
      <span className="font-mono font-semibold text-emerald-300">{pct0(pick.modelProb)}%</span> — no
      sportsbook edge right now.
    </p>
  )
}

function PickCard({ pick, demo, onOpen }: { pick: Pick; demo: boolean; onOpen: () => void }) {
  usePrefsVersion() // re-render when preferred books change (onboarding finish)
  const { card } = pick
  const value = pick.kind === 'value'
  const kick = fmtKick(card.game.start_time)
  const league = card.game.league || SPORT_TAG[pick.sport]
  // Preferred books first (user's order), everyone else in best-price order.
  const shop = orderBooks(lineShop(card, pick.side), (s) => s.book)

  return (
    <div
      role="button"
      tabIndex={0}
      data-tour="pick-card"
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
      className={cls(
        'group relative flex cursor-pointer flex-col overflow-hidden rounded-2xl border border-white/10 bg-krypt-panel/80 p-4 shadow-krypt-card outline-none transition',
        'hover:border-scout-red/40 focus-visible:ring-1 focus-visible:ring-scout-red/60',
        pick.tier === 'STRONG' && 'shadow-scout-glow',
      )}
    >
      {/* Scout-red spine — the "this is the call" marker. */}
      <span
        className={cls(
          'absolute bottom-3 left-0 top-3 w-[2px] rounded-r-full',
          value ? 'bg-scout-red/70' : 'bg-scout-red/30',
        )}
      />

      {/* Eyebrow row: TAKE + tier + risk + league + kickoff. */}
      <div data-tour="pick-tier" className="flex items-center gap-2 pl-1.5">
        <span className="flex items-center gap-1 text-[9px] font-bold uppercase tracking-[0.25em] text-scout-red-bright">
          <Target size={10} />
          TAKE
        </span>
        <TierBadge tier={pick.tier} />
        <RiskBadge prob={pick.modelProb} />
        <span className="ml-auto rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-krypt-muted">
          {league}
        </span>
        <span className="font-mono text-[10px] text-krypt-muted/60">
          {kick.date} {kick.time}
        </span>
      </div>

      {/* Headline: team + market + price + book + BET NOW + TRACK. */}
      <div data-tour="pick-headline" className="mt-2 flex flex-wrap items-center gap-x-2 gap-y-1.5 pl-1.5">
        <span className="min-w-0 truncate text-xl font-bold leading-tight text-white" title={pick.team}>
          {pick.team}
        </span>
        <span
          className="shrink-0 text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/60"
          title="ML = moneyline — a bet on who wins the game, nothing else"
        >
          ML
        </span>
        <span className="ml-auto flex shrink-0 items-center gap-1.5">
          {value ? (
            <>
              <span
                className="font-mono text-2xl font-bold leading-none text-emerald-300"
                title="American odds. Minus (-150): bet $150 to win $100 — the safer side. Plus (+150): bet $100 to win $150 — the underdog."
              >
                {fmtAmerican(pick.price as number)}
              </span>
              <BookLink book={pick.book as string}>
                <Badge tone="success">{bookName(pick.book as string)}</Badge>
              </BookLink>
              <BetNowButton book={pick.book as string} />
              <TrackBetButton pick={pick} demo={demo} />
            </>
          ) : (
            <>
              <span
                className="font-mono text-2xl font-bold leading-none text-white/85"
                title="Our fair price for this team — no book is beating it right now, so there is nothing to bet"
              >
                {fmtAmerican(pick.fairPrice)}
              </span>
              <Badge tone="neutral">FAIR · OUR LINE</Badge>
              <TrackBetButton pick={pick} demo={demo} />
            </>
          )}
        </span>
      </div>

      {/* The plain-English one-liner AND the numbers — no more either/or. */}
      <div data-tour="pick-sentence">
        <PlainSentence pick={pick} />
      </div>

      <div
        data-tour="pick-numbers"
        className="mt-2 flex flex-wrap items-center gap-x-3 gap-y-1 border-t border-white/5 pl-1.5 pt-2"
      >
        <span className="truncate text-[11px] font-semibold text-krypt-muted" title={contextLabel(pick)}>
          {contextLabel(pick)}
        </span>
        <ProbBars model={pick.modelProb} market={pick.marketProb} />
        {value ? (
          <span
            className="ml-auto whitespace-nowrap font-mono text-[10px] font-semibold leading-none text-emerald-300"
            title="EV = expected value: your average profit per $1 bet if our win chance is right. +5% EV ≈ 5 cents per dollar over many bets like this. Positive EV is what makes a bet good."
          >
            +{fmtPct(pick.ev as number)}% EV
          </span>
        ) : (
          <span
            className="ml-auto whitespace-nowrap font-mono text-[10px] leading-none text-krypt-muted"
            title="How sure the model is about its read on this game — not the win chance itself"
          >
            {pick.confidence !== null ? `CONF ${fmtPct(pick.confidence)}%` : 'NO LIVE EDGE'}
          </span>
        )}
      </div>

      <StakeRow pick={pick} />

      {/* Line shop: every book's price for the pick side, best flagged, all clickable. */}
      <div data-tour="pick-shop" className="mt-3 pl-1.5">
        <div className="mb-1 text-[9px] font-semibold uppercase tracking-[0.2em] text-krypt-muted/60">
          Same bet, every book · {pick.team} ML
        </div>
        {shop.length === 0 ? (
          <div className="rounded-lg border border-dashed border-white/10 bg-black/20 px-2 py-1 text-[9px] uppercase tracking-wider text-krypt-muted/60">
            No prices yet — books post lines closer to game time
          </div>
        ) : (
          <div className="flex flex-wrap items-center gap-1.5">
            {shop.map((s) => (
              <BookLink
                key={s.book}
                book={s.book}
                className={cls(
                  'flex items-center gap-1 rounded-lg border px-1.5 py-0.5 font-mono text-[11px] leading-4 transition',
                  s.best
                    ? 'border-emerald-400/30 bg-emerald-500/10 font-semibold text-emerald-300 hover:bg-emerald-500/20'
                    : 'border-white/5 bg-white/[0.02] text-krypt-muted/70 hover:border-white/20 hover:text-white',
                )}
              >
                {isPreferredBook(s.book) && (
                  <Pin size={10} className="shrink-0 text-krypt-muted/50" />
                )}
                {bookName(s.book)} {fmtAmerican(s.price)}
                {s.best && <Badge tone="success">BEST</Badge>}
              </BookLink>
            ))}
          </div>
        )}
        <div
          className="mt-1.5 font-mono text-[9px] uppercase tracking-wider text-krypt-muted/60"
          title="Fair = the price where this bet exactly breaks even by our numbers. A better price than fair makes money long-run; a worse one loses it."
        >
          Worth taking at {fmtAmerican(pick.minAcceptable)} or better
          <span className="text-krypt-muted/40"> — a worse price makes this a bad bet</span>
        </div>
      </div>
    </div>
  )
}

interface Props {
  boards: BoardMap
  demo: boolean
  apiUp: boolean | null
  fetchedAt: Date | null
  loading: boolean
  onRefresh: () => void
  onToggleDemo: () => void
}

/** PICKS — the recommended-bets panel: one clear call per game (what team to
 *  take, at which book, at what minimum price), line-shopped across books. */
export default function PicksPage({
  boards,
  demo,
  apiUp,
  fetchedAt,
  loading,
  onRefresh,
  onToggleDemo,
}: Props) {
  const [scope, setScopeState] = useState<PickScope>(loadScope)
  const [sportFilter, setSportFilter] = useState<SportFilter>('all')
  const [tiers, setTiers] = useState<Set<PickTier>>(() => new Set(TIERS))
  const [openId, setOpenId] = useState<string | null>(null)
  const share = useShareCard()
  const roi = useRoi(demo)

  const setScope = (s: PickScope) => {
    setScopeState(s)
    try {
      localStorage.setItem(SCOPE_KEY, s)
    } catch {
      /* storage unavailable — scope still works for the session */
    }
  }

  const allPicks = useMemo(() => {
    const cards: GameCard[] = []
    for (const s of SPORTS) cards.push(...(boards[s] ?? []))
    return derivePicks(cards)
  }, [boards])

  // Picks never include completed games, so d <= 0 means "on today's local
  // slate" (today + live spillover from before midnight).
  const todayPicks = useMemo(
    () => allPicks.filter((p) => daysFromToday(p.card.game.start_time) <= 0),
    [allPicks],
  )
  const scoped = scope === 'today' ? todayPicks : allPicks

  const sportCounts = useMemo(() => {
    const c: Partial<Record<Sport, number>> = {}
    for (const s of SPORTS) c[s] = 0
    for (const p of scoped) c[p.sport] = (c[p.sport] ?? 0) + 1
    return c
  }, [scoped])

  const tierCounts = useMemo(() => {
    const c: Record<PickTier, number> = { STRONG: 0, LEAN: 0, MODEL: 0 }
    for (const p of scoped) c[p.tier] += 1
    return c
  }, [scoped])

  const shown = useMemo(
    () =>
      scoped.filter(
        (p) => (sportFilter === 'all' || p.sport === sportFilter) && tiers.has(p.tier),
      ),
    [scoped, sportFilter, tiers],
  )

  const toggleTier = (t: PickTier) =>
    setTiers((prev) => {
      const next = new Set(prev)
      if (next.has(t)) next.delete(t)
      else next.add(t)
      return next
    })

  const openCard: GameCard | null = openId
    ? (allPicks.find((p) => p.gameId === openId)?.card ?? null)
    : null

  const boardsEmpty = SPORTS.every((s) => (boards[s]?.length ?? 0) === 0)

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Filter strip: ALL + sports with pick counts, tier chips, data affordances. */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-white/5 bg-krypt-panel/40 px-4 py-2">
        {/* Scope toggle: TODAY (default) | ALL, with pick counts. */}
        <div
          className="flex items-center overflow-hidden rounded-lg border border-white/10 bg-white/[0.03]"
          role="group"
          aria-label="Pick scope"
        >
          {(['today', 'all'] as PickScope[]).map((s) => {
            const active = scope === s
            const count = s === 'today' ? todayPicks.length : allPicks.length
            return (
              <button
                key={s}
                onClick={() => setScope(s)}
                title={SCOPE_HELP[s]}
                aria-pressed={active}
                className={cls(
                  'flex items-center gap-1.5 px-2.5 py-1 text-[11px] font-bold tracking-widest transition',
                  active
                    ? 'bg-white/10 text-white shadow-[inset_0_0_0_1px_rgba(225,29,72,0.45)]'
                    : 'text-krypt-muted hover:bg-white/5 hover:text-white',
                )}
              >
                {s === 'today' ? 'TODAY' : 'ALL'}
                <span
                  className={cls(
                    'min-w-[16px] rounded px-1 text-center font-mono text-[9px] font-semibold leading-4',
                    active ? 'bg-scout-red/25 text-scout-red-bright' : 'bg-white/5 text-krypt-muted',
                  )}
                >
                  {count}
                </span>
              </button>
            )
          })}
        </div>

        <div className="flex flex-wrap items-center gap-1.5 border-l border-white/10 pl-3">
          {(['all', ...SPORTS] as SportFilter[]).map((s) => {
            const isActive = s === sportFilter
            const count = s === 'all' ? scoped.length : (sportCounts[s] ?? 0)
            return (
              <button
                key={s}
                onClick={() => setSportFilter(s)}
                className={cls(
                  'flex items-center gap-1.5 rounded-lg border px-2.5 py-1 text-[11px] font-bold tracking-widest transition',
                  isActive
                    ? 'border-scout-red/50 bg-white/10 text-white shadow-scout-glow ring-1 ring-scout-red/40'
                    : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20 hover:bg-white/5 hover:text-white',
                )}
              >
                {s === 'all' ? 'ALL' : SPORT_TAG[s]}
                <span
                  className={cls(
                    'min-w-[16px] rounded px-1 text-center font-mono text-[9px] font-semibold leading-4',
                    isActive
                      ? 'bg-scout-red/25 text-scout-red-bright'
                      : count > 0
                        ? 'bg-white/5 text-krypt-muted'
                        : 'bg-white/[0.03] text-krypt-muted/40',
                  )}
                >
                  {count}
                </span>
              </button>
            )
          })}
        </div>

        {/* Tier chips */}
        <div className="flex items-center gap-1.5 border-l border-white/10 pl-3">
          {TIERS.map((t) => {
            const on = tiers.has(t)
            return (
              <button
                key={t}
                onClick={() => toggleTier(t)}
                title={TIER_HELP[t]}
                className={cls(
                  'flex items-center gap-1 rounded-lg border px-2 py-1 text-[10px] font-semibold tracking-wider transition',
                  on
                    ? t === 'MODEL'
                      ? 'border-white/20 bg-white/10 text-white'
                      : 'border-emerald-400/30 bg-emerald-500/10 text-emerald-300'
                    : 'border-white/10 bg-white/[0.02] text-krypt-muted/50 hover:border-white/20 hover:text-krypt-muted',
                )}
              >
                {tierLabel(t)}
                <span className="font-mono text-[9px] opacity-70">{tierCounts[t]}</span>
              </button>
            )
          })}
        </div>

        <div className="ml-auto flex items-center gap-2">
          {demo && <Badge tone="gradient">DEMO</Badge>}
          {apiUp === false && !demo && <Badge tone="danger">API DOWN</Badge>}
          <span className="hidden font-mono text-[10px] text-krypt-muted/60 sm:inline">
            {loading ? 'FETCHING…' : fetchedAt ? `AS OF ${fetchedAt.toISOString().slice(11, 19)}Z` : 'NO DATA'}
          </span>
          {share.status !== 'idle' && (
            <span
              className={cls(
                'animate-fade-in font-mono text-[9px] font-semibold uppercase tracking-wider',
                share.status === 'error'
                  ? 'text-rose-400'
                  : share.status === 'working'
                    ? 'text-krypt-muted'
                    : 'text-emerald-300',
              )}
            >
              {share.statusLabel}
            </span>
          )}
          <StripBtn
            onClick={() =>
              void share.share({ picks: shown, roi, date: new Date(), demo })
            }
            disabled={share.busy || shown.length === 0}
            title="Render these picks as a branded PNG card (max 8) and copy it to the clipboard"
          >
            <Share2 size={11} />
            SHARE PICKS
          </StripBtn>
          <StripBtn onClick={onRefresh} disabled={loading} title="Refresh all boards">
            <RefreshCw size={11} className={loading ? 'animate-spin' : undefined} />
            REFRESH
          </StripBtn>
          <StripBtn onClick={onToggleDemo} active={demo} title="Toggle bundled demo slate">
            <FlaskConical size={11} />
            DEMO
          </StripBtn>
        </div>
      </div>

      {/* Pick grid */}
      <main className="min-h-0 flex-1 overflow-y-auto px-4 pb-3 pt-3">
        <Eyebrow
          sub={`${shown.length} of ${scoped.length} ${scope === 'today' ? "today's" : 'tracked'} picks · one call per game · best price across every book`}
        >
          Recommended bets
        </Eyebrow>

        {apiUp === false && !demo && (
          <div className="mb-3 rounded-xl border border-amber-400/25 bg-amber-500/[0.07] px-3 py-2 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
            Data feed offline — turn on DEMO mode to look around.
          </div>
        )}

        {shown.length === 0 ? (
          scope === 'today' && todayPicks.length === 0 && allPicks.length > 0 ? (
            <Empty
              title="Nothing on today's slate"
              message={`No games with picks start today. ${allPicks.length} pick${allPicks.length === 1 ? '' : 's'} on upcoming slates — switch the scope to ALL to browse ahead.`}
              action={
                <StripBtn onClick={() => setScope('all')} title={SCOPE_HELP.all}>
                  SHOW ALL PICKS
                </StripBtn>
              }
            />
          ) : boardsEmpty || allPicks.length === 0 ? (
            <Empty
              title="No picks right now"
              message="Picks appear when games with model output are on the boards. Value picks (with a book price and an EV number) also need live odds — set ODDS_API_KEY under SETTINGS; without it you'll only see FAVORITE picks. Turn on demo mode to explore example picks."
            />
          ) : (
            <Empty
              title="No picks match the current filters"
              message="Widen the sport filter or re-enable a tier chip above — the slate has picks outside this view."
            />
          )
        ) : (
          <div className="grid animate-fade-in gap-4 [grid-template-columns:repeat(auto-fill,minmax(400px,1fr))]">
            {shown.map((p) => (
              <PickCard key={p.gameId} pick={p} demo={demo} onOpen={() => setOpenId(p.gameId)} />
            ))}
          </div>
        )}
      </main>

      {/* Disclaimer footer */}
      <footer className="shrink-0 border-t border-white/5 bg-krypt-panel/40 px-4 py-1.5">
        <span className="text-[9px] uppercase tracking-wider text-krypt-muted/50">
          Computer predictions, not guarantees. Bet sizes are quarter-Kelly. Only bet what you can afford to lose. 21+.
        </span>
      </footer>

      <DetailDrawer card={openCard} demo={demo} onClose={() => setOpenId(null)} />
    </div>
  )
}
