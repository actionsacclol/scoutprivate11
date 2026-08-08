/**
 * PARLAY — the parlay builder that tells you the truth.
 *
 * LEFT: pick 2–8 moneyline legs from the boards (sport filter, TODAY|ALL
 * scope, search, expandable game rows). RIGHT: the slip, priced two ways —
 * the books' combined price and the model's fair price — with an honest
 * verdict about the gap. Same-game legs are blocked (correlated), completed
 * games never qualify, and the slip persists across sessions. A fully-priced
 * slip can be TRACKED to the user's bet log (TRACKER page) at a flat 1u,
 * where it grades leg-by-leg once its games settle.
 */
import { useEffect, useMemo, useRef, useState } from 'react'
import {
  Check,
  ChevronDown,
  ChevronRight,
  Copy,
  Layers,
  NotebookPen,
  Plus,
  Search,
  Trash2,
  X,
} from 'lucide-react'
import type { GameCard, Sport } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import { fetchBacktestAll, type BacktestReport } from '../api'
import { useMyBets } from '../mybets'
import { defaultStake, usePrefsVersion } from '../prefs'
import { bookAbbrev, fmtAmerican, fmtPct } from '../oddsmath'
import { byStartAsc, daysFromToday, fmtKick, matchLabel, sideViews } from '../cardview'
import {
  MAX_LEGS,
  MIN_LEGS,
  buildLeg,
  canAddLeg,
  parlayMath,
  rehydrateSlip,
  type Leg,
  type LegPrice,
  type LegSide,
  type ParlayMath,
  type SlipRef,
} from '../parlay'
import { Badge, Empty, Eyebrow, StripBtn } from './common'
import { cls } from '../utils/format'
import { useUiMode } from '../uiMode'

type SportFilter = Sport | 'all'
type Scope = 'today' | 'all'

const SLIP_KEY = 'scout.parlay-slip.v1'
const SCOPE_KEY = 'scout.parlay-scope'

const SCOPE_HELP: Record<Scope, string> = {
  today: "Only games on today's local slate",
  all: 'Every upcoming tracked game',
}

function loadScope(): Scope {
  try {
    return localStorage.getItem(SCOPE_KEY) === 'all' ? 'all' : 'today'
  } catch {
    return 'today'
  }
}

function loadRefs(): SlipRef[] {
  try {
    const raw = localStorage.getItem(SLIP_KEY)
    if (!raw) return []
    const parsed = JSON.parse(raw) as unknown
    if (!Array.isArray(parsed)) return []
    return parsed.filter(
      (r): r is SlipRef =>
        !!r &&
        typeof (r as SlipRef).gameId === 'string' &&
        ['home', 'away', 'draw'].includes((r as SlipRef).side),
    )
  } catch {
    return []
  }
}

function saveRefs(refs: SlipRef[]): void {
  try {
    localStorage.setItem(SLIP_KEY, JSON.stringify(refs))
  } catch {
    /* storage unavailable — slip still works for the session */
  }
}

/** Payout multiplier at a decimal price ("5.50x") — stake-agnostic, since
 *  the slip has no bet amount attached. */
function multiplier(decimal: number): string {
  return `${decimal >= 100 ? Math.round(decimal).toLocaleString('en-US') : decimal.toFixed(2)}x`
}

/** Plain-text slip summary for the clipboard. */
function slipText(legs: Leg[], math: ParlayMath): string {
  const head = math.priced
    ? `${fmtAmerican(math.offeredAmerican as number)} (fair ${fmtAmerican(math.fairAmerican)})`
    : `MODEL-ONLY (fair ${fmtAmerican(math.fairAmerican)})`
  const lines = [
    `KRYPT SCOUT PARLAY · ${legs.length} LEGS · ${head}`,
    ...legs.map((l, i) => {
      const price = l.bestPrice
        ? `${fmtAmerican(l.bestPrice.american)} @ ${bookAbbrev(l.bestPrice.book)}`
        : `no line (fair ${fmtAmerican(l.fairAmerican)})`
      return `${i + 1}. ${l.team} ML ${price} · model ${fmtPct(l.modelProb)}%`
    }),
    math.priced && math.evPerDollar !== null
      ? `Model odds ${fmtPct(math.combinedModelProb)}% · EV ${math.evPerDollar >= 0 ? '+' : ''}${fmtPct(math.evPerDollar)}% per $1`
      : `Model odds ${fmtPct(math.combinedModelProb)}% · no combined book price yet`,
    'krypt.cc/tools/scout',
  ]
  return lines.join('\n')
}

// ------------------------------------------------------------- picker (left)

function SideRow({
  card,
  side,
  legs,
  simple,
  onToggle,
}: {
  card: GameCard
  side: ReturnType<typeof sideViews>[number]
  legs: Leg[]
  simple: boolean
  onToggle: (card: GameCard, side: LegSide) => void
}) {
  const inSlip = legs.some((l) => l.gameId === card.game.id && l.side === side.key)
  const check = canAddLeg(legs, card, side.key)
  const blocked = !inSlip && !check.ok
  const leg = buildLeg(card, side.key)
  const blockTitle =
    check.reason === 'same-game'
      ? 'Same-game legs are correlated — one leg per game. Remove the other leg first.'
      : check.reason === 'max-legs'
        ? `Slip is full — max ${MAX_LEGS} legs.`
        : check.reason === 'completed'
          ? 'Game is final.'
          : 'No model probability for this side.'

  return (
    <div className="flex items-center gap-2 py-1 pl-7 pr-2">
      <span className="w-5 shrink-0 font-mono text-[9px] font-semibold uppercase text-krypt-muted/60">
        {side.tag}
      </span>
      <span
        className="min-w-0 flex-1 truncate text-xs font-semibold text-white/85"
        title={side.name}
      >
        {side.name}
      </span>
      <span
        className="shrink-0 font-mono text-[11px] text-emerald-300"
        title={simple ? 'Our win chance' : 'Model win probability'}
      >
        {fmtPct(side.prob)}%
      </span>
      <span className="w-24 shrink-0 text-right font-mono text-[11px]">
        {leg?.bestPrice ? (
          <>
            <span className="text-white/85">{fmtAmerican(leg.bestPrice.american)}</span>
            <span className="text-krypt-muted/50"> {bookAbbrev(leg.bestPrice.book)}</span>
          </>
        ) : (
          <span
            className="text-krypt-muted/50"
            title={
              simple
                ? 'No prices yet — lines post closer to game time'
                : `No live line — model fair ${leg ? fmtAmerican(leg.fairAmerican) : '—'}`
            }
          >
            {simple || !leg ? 'no line' : `FAIR ${fmtAmerican(leg.fairAmerican)}`}
          </span>
        )}
      </span>
      <button
        onClick={() => onToggle(card, side.key)}
        disabled={blocked || !leg}
        title={
          inSlip
            ? 'In the slip — click to remove'
            : blocked
              ? blockTitle
              : 'Add this leg to the slip'
        }
        aria-pressed={inSlip}
        className={cls(
          'flex h-6 w-6 shrink-0 items-center justify-center rounded-md border transition',
          inSlip
            ? 'border-emerald-400/40 bg-emerald-500/15 text-emerald-300 hover:bg-emerald-500/25'
            : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-scout-red/40 hover:text-white',
          (blocked || !leg) && 'cursor-not-allowed opacity-35 hover:border-white/10 hover:text-krypt-muted',
        )}
      >
        {inSlip ? <Check size={12} /> : <Plus size={12} />}
      </button>
    </div>
  )
}

function GameRow({
  card,
  legs,
  simple,
  crossSport,
  expanded,
  onExpand,
  onToggle,
}: {
  card: GameCard
  legs: Leg[]
  simple: boolean
  crossSport: boolean
  expanded: boolean
  onExpand: () => void
  onToggle: (card: GameCard, side: LegSide) => void
}) {
  const kick = fmtKick(card.game.start_time)
  const sides = sideViews(card)
  const slipLeg = legs.find((l) => l.gameId === card.game.id)
  return (
    <div
      className={cls(
        'overflow-hidden rounded-xl border bg-krypt-panel/60 transition',
        slipLeg ? 'border-scout-red/30' : 'border-white/10 hover:border-white/20',
      )}
    >
      <button
        onClick={onExpand}
        aria-expanded={expanded}
        className="flex w-full items-center gap-2 px-2 py-1.5 text-left outline-none transition hover:bg-white/[0.03] focus-visible:ring-1 focus-visible:ring-scout-red/60"
      >
        <span className="shrink-0 text-krypt-muted/60">
          {expanded ? <ChevronDown size={13} /> : <ChevronRight size={13} />}
        </span>
        <span className="shrink-0 font-mono text-[10px] leading-none text-krypt-muted/70">
          {kick.date} {kick.time}
        </span>
        <span
          className="min-w-0 flex-1 truncate text-xs font-semibold text-white/90"
          title={matchLabel(card)}
        >
          {matchLabel(card)}
        </span>
        {slipLeg && (
          <span
            className="shrink-0 rounded border border-scout-red/40 bg-scout-red/10 px-1.5 py-0.5 font-mono text-[9px] font-bold leading-none tracking-wider text-scout-red-bright"
            title={`${slipLeg.team} is in your slip`}
          >
            IN SLIP
          </span>
        )}
        {crossSport && (
          <span className="shrink-0 rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-krypt-muted">
            {SPORT_TAG[card.game.sport]}
          </span>
        )}
      </button>
      {expanded && (
        <div className="animate-fade-in border-t border-white/5 py-1">
          {sides.map((s) => (
            <SideRow
              key={s.key}
              card={card}
              side={s}
              legs={legs}
              simple={simple}
              onToggle={onToggle}
            />
          ))}
        </div>
      )}
    </div>
  )
}

// --------------------------------------------------------------- slip (right)

function SlipLegRow({
  leg,
  simple,
  onRemove,
}: {
  leg: Leg
  simple: boolean
  onRemove: () => void
}) {
  return (
    <div className="flex items-center gap-2 rounded-lg border border-white/5 bg-white/[0.02] px-2 py-1.5">
      <div className="min-w-0 flex-1">
        <div className="truncate text-xs font-bold text-white" title={leg.team}>
          {leg.team}
          <span className="ml-1 text-[9px] font-semibold uppercase tracking-wider text-krypt-muted/60">
            ML
          </span>
        </div>
        <div className="truncate text-[10px] text-krypt-muted/70" title={leg.opponent}>
          {leg.side === 'draw' ? leg.opponent : `vs ${leg.opponent}`}
        </div>
      </div>
      <span
        className="shrink-0 font-mono text-[11px] text-emerald-300"
        title={simple ? 'Our win chance' : 'Model win probability'}
      >
        {fmtPct(leg.modelProb)}%
      </span>
      <span className="w-[76px] shrink-0 text-right font-mono text-xs">
        {leg.bestPrice ? (
          <>
            <span className="font-semibold text-white">{fmtAmerican(leg.bestPrice.american)}</span>
            <span className="text-[9px] text-krypt-muted/50"> {bookAbbrev(leg.bestPrice.book)}</span>
          </>
        ) : (
          <span
            className="text-[10px] font-semibold text-krypt-muted/60"
            title={
              simple
                ? 'No book price yet — using our fair value'
                : `No live line — model fair ${fmtAmerican(leg.fairAmerican)}`
            }
          >
            {simple ? 'NO PRICE' : `FAIR ${fmtAmerican(leg.fairAmerican)}`}
          </span>
        )}
      </span>
      <button
        onClick={onRemove}
        title="Remove leg"
        className="flex h-5 w-5 shrink-0 items-center justify-center rounded-md border border-white/10 bg-white/[0.03] text-krypt-muted transition hover:border-rose-400/40 hover:text-rose-300"
      >
        <X size={11} />
      </button>
    </div>
  )
}

function VerdictStat({
  label,
  value,
  tone = 'neutral',
  help,
}: {
  label: string
  value: string
  tone?: 'neutral' | 'success' | 'ice'
  help?: string
}) {
  return (
    <div
      title={help}
      className="rounded-xl border border-white/10 bg-black/20 px-2.5 py-2 text-center"
    >
      <div className="text-[8px] font-semibold uppercase tracking-[0.18em] text-krypt-muted">
        {label}
      </div>
      <div
        className={cls(
          'mt-1 font-mono text-lg font-bold leading-none tracking-tight',
          tone === 'success' && 'text-emerald-300',
          tone === 'ice' && 'text-sky-300',
          tone === 'neutral' && 'text-white',
        )}
      >
        {value}
      </div>
    </div>
  )
}

/** Track-record adjustment: for legs in sports with a walk-forward log,
 *  replace the model's claimed probability with the EMPIRICAL hit rate of
 *  that confidence bucket (the Backtest panel's calibration bins). The
 *  adjusted slip probability is "what slips like this actually hit". */
export interface ProvenSlip {
  prob: number
  verifiedLegs: number
  totalLegs: number
  sampleGames: number
}

export function provenSlipProb(
  legs: Array<{ sport: string; modelProb: number }>,
  reports: BacktestReport[] | null,
): ProvenSlip | null {
  if (!reports || legs.length === 0) return null
  const bySport = new Map(reports.filter((r) => r.kind === 'log' && r.calibration).map((r) => [r.sport, r]))
  let prob = 1
  let verified = 0
  let sample = 0
  for (const leg of legs) {
    const rep = bySport.get(leg.sport)
    const bin = rep?.calibration?.find(
      (b, i) => leg.modelProb >= b.lo && (leg.modelProb < b.hi || i === rep.calibration!.length - 1),
    )
    if (bin && bin.n >= 30 && bin.actual != null) {
      prob *= bin.actual
      verified += 1
      sample += bin.n
    } else {
      prob *= leg.modelProb
    }
  }
  if (verified === 0) return null
  return { prob, verifiedLegs: verified, totalLegs: legs.length, sampleGames: sample }
}

function Verdict({ math, simple, proven }: { math: ParlayMath; simple: boolean; proven: ProvenSlip | null }) {
  const priced = math.priced && math.offeredAmerican !== null
  const ev = math.evPerDollar
  const positive = priced && ev !== null && ev > 0
  const dragPct = math.vigDrag !== null ? fmtPct(math.vigDrag) : null

  return (
    <div className="mt-3 border-t border-white/10 pt-3">
      <div className="grid grid-cols-3 gap-1.5">
        <VerdictStat
          label={simple ? 'PAYS' : 'COMBINED PRICE'}
          value={
            priced
              ? simple
                ? multiplier(math.offeredDecimal as number)
                : fmtAmerican(math.offeredAmerican as number)
              : '—'
          }
          tone="ice"
          help={
            simple
              ? 'Your total return multiplier if every leg wins, at the best available prices'
              : 'Product of each leg’s best decimal price, in American odds'
          }
        />
        <VerdictStat
          label={simple ? 'WIN CHANCE' : 'MODEL ODDS'}
          value={`${fmtPct(math.combinedModelProb)}%`}
          tone="success"
          help={
            simple
              ? 'The chance every leg wins, by our model'
              : 'Product of leg model probabilities (legs assumed independent — same-game legs are blocked)'
          }
        />
        <VerdictStat
          label={simple ? 'FAIR' : 'FAIR PRICE'}
          value={simple ? multiplier(math.fairDecimal) : fmtAmerican(math.fairAmerican)}
          help={
            simple
              ? 'The multiplier this slip should pay if the price matched our win chance exactly'
              : 'Break-even price for the model probability: EV = 0'
          }
        />
      </div>

      {/* Track record: what slips like this have ACTUALLY hit, from the
          walk-forward calibration record — the receipts behind WIN CHANCE. */}
      {proven && (
        <div
          className="mt-1.5 flex items-baseline justify-between rounded-lg border border-sky-400/20 bg-sky-500/[0.06] px-2.5 py-1.5"
          title={`Each verified leg's model confidence is replaced with the REAL hit rate of that confidence bucket across the sport's walk-forward record (${proven.sampleGames.toLocaleString()} graded games behind the verified legs). Unverified legs keep the model number.`}
        >
          <span className="text-[9px] font-bold uppercase tracking-widest text-sky-300">
            Proven hit rate · {proven.verifiedLegs}/{proven.totalLegs} legs verified
          </span>
          <span className="font-mono text-xs font-bold text-sky-200">
            {fmtPct(proven.prob)}%
          </span>
        </div>
      )}

      {/* The honesty line. */}
      {priced ? (
        positive ? (
          <div className="mt-2.5 rounded-lg border border-emerald-400/25 bg-emerald-500/[0.08] px-2.5 py-2 text-xs leading-relaxed text-emerald-200">
            {simple ? (
              <>
                The books pay{' '}
                <span className="font-mono font-bold">{multiplier(math.offeredDecimal as number)}</span>{' '}
                your stake — more than the{' '}
                <span className="font-mono font-bold">{multiplier(math.fairDecimal)}</span> our win
                chances say is fair. That's rare for a parlay.
              </>
            ) : (
              <>
                <span className="font-mono font-bold">+EV +{fmtPct(ev as number)}%</span> — rare for
                a parlay, nice.
                <span className="mt-1 block font-mono text-[10px] text-emerald-300/80">
                  1/4 KELLY: {fmtPct(math.quarterKelly as number, 2)}% BANKROLL
                </span>
              </>
            )}
          </div>
        ) : (
          <div className="mt-2.5 rounded-lg border border-rose-400/25 bg-rose-500/[0.08] px-2.5 py-2 text-xs leading-relaxed text-rose-200">
            {simple ? (
              <>
                The books pay{' '}
                <span className="font-mono font-bold">{multiplier(math.offeredDecimal as number)}</span>{' '}
                your stake. Based on our win chances, a fair slip would pay{' '}
                <span className="font-mono font-bold">{multiplier(math.fairDecimal)}</span>. You're
                overpaying by <span className="font-mono font-bold">{dragPct}%</span>.
              </>
            ) : (
              <>
                This parlay pays{' '}
                <span className="font-mono font-bold">{fmtAmerican(math.offeredAmerican as number)}</span>{' '}
                but is worth{' '}
                <span className="font-mono font-bold">{fmtAmerican(math.fairAmerican)}</span> —
                you're giving the book <span className="font-mono font-bold">{dragPct}%</span>.
              </>
            )}
          </div>
        )
      ) : (
        <div className="mt-2.5 rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-2 text-[11px] leading-relaxed text-krypt-muted">
          {simple
            ? `${math.unpricedLegs} leg${math.unpricedLegs === 1 ? ' has' : 's have'} no book price yet — we can show your win chance and fair payout, but not what the books would pay.`
            : `Model-only slip: ${math.unpricedLegs} leg${math.unpricedLegs === 1 ? '' : 's'} unpriced — no combined book price. Fair value shown; shop the legs before betting.`}
        </div>
      )}

      {!simple && priced && math.impliedProbOffered !== null && (
        <div
          className="mt-1.5 font-mono text-[9px] uppercase tracking-wider text-krypt-muted/60"
          title="The win probability the combined price implies — what the book is charging you to believe"
        >
          Price implies {fmtPct(math.impliedProbOffered)}% · model says{' '}
          {fmtPct(math.combinedModelProb)}%
        </div>
      )}
    </div>
  )
}

// --------------------------------------------------------------------- page

interface Props {
  boards: Record<string, GameCard[]>
  demo: boolean
}

export default function ParlayPage({ boards, demo }: Props) {
  const simple = useUiMode() === 'simple'
  const [scope, setScopeState] = useState<Scope>(loadScope)
  const [sportFilter, setSportFilter] = useState<SportFilter>('all')
  const [query, setQuery] = useState('')
  const [expanded, setExpanded] = useState<Set<string>>(() => new Set())
  const [refs, setRefs] = useState<SlipRef[]>(loadRefs)
  const [droppedNote, setDroppedNote] = useState<string | null>(null)
  const [copied, setCopied] = useState(false)
  // Walk-forward calibration reports back the PROVEN HIT RATE line (live only).
  const [calReports, setCalReports] = useState<BacktestReport[] | null>(null)
  useEffect(() => {
    if (demo) {
      setCalReports(null)
      return
    }
    let alive = true
    void fetchBacktestAll().then((r) => alive && setCalReports(r))
    return () => {
      alive = false
    }
  }, [demo])
  const [trackFailed, setTrackFailed] = useState(false)
  const copyTimer = useRef<number | null>(null)
  const { trackParlay, isParlayTracked, busy: trackerBusy } = useMyBets(demo)
  usePrefsVersion() // re-render when the default stake changes
  const stake = defaultStake()

  const setScope = (s: Scope) => {
    setScopeState(s)
    try {
      localStorage.setItem(SCOPE_KEY, s)
    } catch {
      /* storage unavailable */
    }
  }

  // ---- boards → candidate cards --------------------------------------------
  const allCards = useMemo(() => {
    const cards: GameCard[] = []
    for (const s of SPORTS) cards.push(...(boards[s] ?? []))
    return cards
  }, [boards])

  const cardById = useMemo(() => {
    const m = new Map<string, GameCard>()
    for (const c of allCards) m.set(c.game.id, c)
    return m
  }, [allCards])

  // Parlay-eligible: not final, has model output.
  const eligible = useMemo(
    () =>
      allCards.filter((c) => !c.game.completed && c.prediction !== null).sort(byStartAsc),
    [allCards],
  )

  const todayCards = useMemo(
    () => eligible.filter((c) => daysFromToday(c.game.start_time) <= 0),
    [eligible],
  )
  const scoped = scope === 'today' ? todayCards : eligible

  const sportCounts = useMemo(() => {
    const c: Partial<Record<Sport, number>> = {}
    for (const s of SPORTS) c[s] = 0
    for (const card of scoped) c[card.game.sport] = (c[card.game.sport] ?? 0) + 1
    return c
  }, [scoped])

  const shown = useMemo(() => {
    const q = query.trim().toLowerCase()
    return scoped.filter((c) => {
      if (sportFilter !== 'all' && c.game.sport !== sportFilter) return false
      if (!q) return true
      return (
        c.game.home.toLowerCase().includes(q) ||
        c.game.away.toLowerCase().includes(q) ||
        c.game.league.toLowerCase().includes(q)
      )
    })
  }, [scoped, sportFilter, query])

  // ---- slip: rehydrate saved refs against live boards ----------------------
  const slip = useMemo(() => rehydrateSlip(refs, cardById), [refs, cardById])
  const legs = slip.legs
  const math = legs.length > 0 ? parlayMath(legs) : null

  // Prune refs whose games vanished/finished — but only once boards have data,
  // so an empty first render doesn't wipe a saved slip.
  useEffect(() => {
    if (allCards.length === 0) return
    if (slip.dropped > 0) {
      setRefs(slip.refs)
      setDroppedNote(
        `${slip.dropped} leg${slip.dropped === 1 ? '' : 's'} dropped — game${slip.dropped === 1 ? '' : 's'} finished or left the board.`,
      )
    }
  }, [allCards.length, slip.dropped, slip.refs])

  useEffect(() => {
    saveRefs(refs)
  }, [refs])

  useEffect(
    () => () => {
      if (copyTimer.current !== null) window.clearTimeout(copyTimer.current)
    },
    [],
  )

  const toggleLeg = (card: GameCard, side: LegSide) => {
    setDroppedNote(null)
    const exists = refs.some((r) => r.gameId === card.game.id && r.side === side)
    if (exists) {
      setRefs(refs.filter((r) => !(r.gameId === card.game.id && r.side === side)))
      return
    }
    if (!canAddLeg(legs, card, side).ok) return
    setRefs([...refs, { gameId: card.game.id, side }])
  }

  const removeLeg = (gameId: string) => {
    setDroppedNote(null)
    setRefs(refs.filter((r) => r.gameId !== gameId))
  }

  const clearSlip = () => {
    setDroppedNote(null)
    setRefs([])
  }

  const copySlip = () => {
    if (!math || legs.length < MIN_LEGS) return
    const text = slipText(legs, math)
    void navigator.clipboard
      .writeText(text)
      .then(() => {
        setCopied(true)
        if (copyTimer.current !== null) window.clearTimeout(copyTimer.current)
        copyTimer.current = window.setTimeout(() => setCopied(false), 1800)
      })
      .catch(() => {
        /* clipboard unavailable — button just doesn't confirm */
      })
  }

  // ---- track the slip as a parlay bet ---------------------------------------
  const slipTracked =
    legs.length >= MIN_LEGS &&
    isParlayTracked(legs.map((l) => ({ gameId: l.gameId, side: l.side })))
  const slipPriced = math !== null && math.priced
  const canTrack =
    legs.length >= MIN_LEGS && slipPriced && !slipTracked && !trackerBusy

  const trackSlip = async () => {
    if (!canTrack) return
    setTrackFailed(false)
    const ok = await trackParlay(
      {
        legs: legs.map((l) => ({
          game_id: l.gameId,
          side: l.side,
          price: (l.bestPrice as LegPrice).american,
          book: l.bestPrice?.book ?? null,
        })),
        stake_units: stake,
      },
      cardById,
    )
    if (!ok) setTrackFailed(true)
  }

  const toggleExpand = (id: string) =>
    setExpanded((prev) => {
      const next = new Set(prev)
      if (next.has(id)) next.delete(id)
      else next.add(id)
      return next
    })

  const boardsEmpty = allCards.length === 0

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Filter strip: scope, sports, search — mirrors the PICKS strip. */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-white/5 bg-krypt-panel/40 px-4 py-2">
        <div
          className="flex items-center overflow-hidden rounded-lg border border-white/10 bg-white/[0.03]"
          role="group"
          aria-label="Game scope"
        >
          {(['today', 'all'] as Scope[]).map((s) => {
            const active = scope === s
            const count = s === 'today' ? todayCards.length : eligible.length
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

        <div className="flex items-center gap-1.5 border-l border-white/10 pl-3">
          <div className="relative">
            <Search
              size={11}
              className="pointer-events-none absolute left-2 top-1/2 -translate-y-1/2 text-krypt-muted/50"
            />
            <input
              value={query}
              onChange={(e) => setQuery(e.target.value)}
              placeholder={simple ? 'Search teams' : 'Search teams / leagues'}
              className="w-40 rounded-lg border border-white/10 bg-white/[0.03] py-1 pl-6 pr-2 text-[11px] text-white placeholder:text-krypt-muted/40 outline-none transition focus:border-scout-red/40"
            />
          </div>
        </div>

        <div className="ml-auto flex items-center gap-2">
          {demo && <Badge tone="gradient">DEMO</Badge>}
          <span className="font-mono text-[10px] font-semibold tracking-wider text-krypt-muted">
            SLIP · {legs.length} {legs.length === 1 ? 'LEG' : 'LEGS'}
          </span>
        </div>
      </div>

      {/* Two columns: picker + slip. */}
      <div className="flex min-h-0 flex-1">
        {/* LEFT — leg picker. */}
        <main className="min-h-0 flex-1 overflow-y-auto px-4 pb-3 pt-3">
          <Eyebrow
            sub={
              simple
                ? `pick ${MIN_LEGS}+ teams to win · one per game`
                : `${shown.length} of ${scoped.length} games · moneyline legs only · one per game (correlation)`
            }
          >
            Build your parlay
          </Eyebrow>

          {boardsEmpty ? (
            <Empty
              title="No games on the board"
              message={
                simple
                  ? 'Nothing to parlay right now. Turn on demo mode in Settings to try the builder with example games.'
                  : 'The parlay builder needs games with model output. Enable demo mode to explore with a bundled slate.'
              }
            />
          ) : shown.length === 0 ? (
            scope === 'today' && todayCards.length === 0 && eligible.length > 0 ? (
              <Empty
                title="Nothing on today's slate"
                message={`No parlay-eligible games start today. ${eligible.length} game${eligible.length === 1 ? '' : 's'} on upcoming slates — switch the scope to ALL.`}
                action={
                  <StripBtn onClick={() => setScope('all')} title={SCOPE_HELP.all}>
                    SHOW ALL GAMES
                  </StripBtn>
                }
              />
            ) : (
              <Empty
                title="No games match the current filters"
                message="Widen the sport filter or clear the search — the slate has more games than this view."
              />
            )
          ) : (
            <div className="animate-fade-in space-y-1.5">
              {shown.map((c) => (
                <GameRow
                  key={c.game.id}
                  card={c}
                  legs={legs}
                  simple={simple}
                  crossSport={sportFilter === 'all'}
                  expanded={expanded.has(c.game.id)}
                  onExpand={() => toggleExpand(c.game.id)}
                  onToggle={toggleLeg}
                />
              ))}
            </div>
          )}
        </main>

        {/* RIGHT — THE SLIP. */}
        <aside className="w-[380px] shrink-0 overflow-y-auto border-l border-white/5 bg-black/10 px-3 pb-3 pt-3">
          <div className="sticky top-0">
            <div className="relative overflow-hidden rounded-2xl border border-white/10 bg-krypt-panel/80 p-3 shadow-krypt-card">
              {/* Scout-red spine. */}
              <span className="absolute bottom-3 left-0 top-3 w-[2px] rounded-r-full bg-scout-red/70" />

              <div className="flex items-center gap-2 pl-1.5">
                <span className="flex items-center gap-1.5 text-[10px] font-bold uppercase tracking-[0.25em] text-scout-red-bright">
                  <Layers size={11} />
                  THE SLIP
                </span>
                <span className="font-mono text-[10px] text-krypt-muted">
                  {legs.length} {legs.length === 1 ? 'LEG' : 'LEGS'}
                </span>
                <span className="ml-auto flex items-center gap-1.5">
                  <StripBtn
                    onClick={copySlip}
                    disabled={!math || legs.length < MIN_LEGS}
                    active={copied}
                    title="Copy a text summary of this slip to the clipboard"
                  >
                    {copied ? <Check size={11} /> : <Copy size={11} />}
                    {copied ? 'COPIED' : 'COPY SLIP'}
                  </StripBtn>
                  <StripBtn
                    onClick={clearSlip}
                    disabled={legs.length === 0}
                    title="Remove every leg"
                  >
                    <Trash2 size={11} />
                    CLEAR
                  </StripBtn>
                </span>
              </div>

              {droppedNote && (
                <div className="mt-2 flex items-center gap-2 rounded-lg border border-amber-400/25 bg-amber-500/[0.07] px-2 py-1.5 pl-2 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
                  <span className="min-w-0 flex-1">{droppedNote}</span>
                  <button
                    onClick={() => setDroppedNote(null)}
                    title="Dismiss"
                    className="shrink-0 text-amber-300/70 transition hover:text-amber-200"
                  >
                    <X size={11} />
                  </button>
                </div>
              )}

              {legs.length === 0 ? (
                <div className="mt-3 rounded-xl border border-dashed border-white/10 bg-black/20 px-3 py-6 text-center">
                  <div className="text-sm font-semibold text-white">Slip is empty</div>
                  <div className="mt-1.5 text-[11px] leading-relaxed text-krypt-muted">
                    {simple
                      ? `Add ${MIN_LEGS}–${MAX_LEGS} teams from the left. We'll show what the books pay — and what it's really worth.`
                      : `Add ${MIN_LEGS}–${MAX_LEGS} moneyline legs from the picker. We price the slip at the best book prices and against the model.`}
                  </div>
                </div>
              ) : (
                <>
                  <div className="mt-3 space-y-1.5 pl-1.5">
                    {legs.map((l) => (
                      <SlipLegRow
                        key={`${l.gameId}:${l.side}`}
                        leg={l}
                        simple={simple}
                        onRemove={() => removeLeg(l.gameId)}
                      />
                    ))}
                  </div>

                  {legs.length < MIN_LEGS ? (
                    <div className="ml-1.5 mt-3 rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-2 text-[11px] text-krypt-muted">
                      {simple
                        ? 'Add at least one more team — a parlay needs two or more.'
                        : `A parlay needs ${MIN_LEGS}–${MAX_LEGS} legs — add at least one more.`}
                    </div>
                  ) : (
                    math && (
                      <div className="pl-1.5">
                        <Verdict
                          math={math}
                          simple={simple}
                          proven={provenSlipProb(legs, calReports)}
                        />

                        {/* Track the slip — lands on TRACKER with your bets. */}
                        <button
                          onClick={() => void trackSlip()}
                          disabled={!canTrack && !slipTracked}
                          title={
                            slipTracked
                              ? 'This slip is tracked — see the TRACKER page'
                              : !slipPriced
                                ? 'Tracking needs a live book price on every leg — unpriced legs have nothing to grade against'
                                : simple
                                  ? 'Save this parlay to your tracker — it grades itself when the games finish'
                                  : `Log this slip at ${stake}u (change under SETTINGS) — graded automatically once every leg settles`
                          }
                          className={cls(
                            'mt-3 flex w-full items-center justify-center gap-2 rounded-xl border px-3 py-2 text-[11px] font-bold uppercase tracking-widest transition disabled:cursor-not-allowed',
                            slipTracked
                              ? 'border-emerald-400/40 bg-emerald-500/15 text-emerald-300'
                              : canTrack
                                ? 'border-scout-red/50 bg-scout-red/15 text-white hover:bg-scout-red/25'
                                : 'border-white/10 bg-white/[0.03] text-krypt-muted/50',
                          )}
                        >
                          {slipTracked ? (
                            <>
                              <Check size={12} />
                              TRACKED — SEE TRACKER
                            </>
                          ) : (
                            <>
                              <NotebookPen size={12} />
                              {slipPriced
                                ? `TRACK PARLAY · ${stake}u`
                                : simple
                                  ? 'NEEDS PRICES TO TRACK'
                                  : 'UNPRICED LEGS — CAN’T TRACK'}
                            </>
                          )}
                        </button>
                        {trackFailed && (
                          <div className="mt-1.5 rounded-lg border border-rose-400/25 bg-rose-500/[0.08] px-2.5 py-1.5 text-[10px] font-semibold uppercase tracking-wider text-rose-300">
                            {simple
                              ? 'Couldn’t save — the data feed is offline. Try again in a moment.'
                              : 'Track failed — backend unreachable. The slip is unchanged; retry when the API is back.'}
                          </div>
                        )}
                      </div>
                    )
                  )}
                </>
              )}
            </div>
          </div>
        </aside>
      </div>

      {/* Educational footer — why the books love parlays. */}
      <footer className="shrink-0 border-t border-white/5 bg-krypt-panel/40 px-4 py-1.5">
        <span className="text-[9px] uppercase tracking-wider text-krypt-muted/50">
          {simple
            ? 'Sportsbooks push parlays because every leg you add multiplies their built-in fee. This page shows what yours is really worth before you pay it. Not betting advice. 21+.'
            : 'Books love parlays: the vig compounds with every leg — three -110 legs and you pay the house tax three times over. We price the slip against the model so you see the drag first. Not betting advice. 21+.'}
        </span>
      </footer>
    </div>
  )
}
