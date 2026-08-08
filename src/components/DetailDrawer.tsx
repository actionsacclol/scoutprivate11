import { useEffect, useState, type MouseEvent as ReactMouseEvent } from 'react'
import { BookOpen, Check, Cpu, Pin, Plus, Target, X, Zap } from 'lucide-react'
import type {
  GameCard,
  InjuryReport,
  MovementReport,
  MovementSnapshot,
  Sport,
  WeatherInfo,
} from '../types'
import { SPORT_TAG } from '../types'
import { bookAbbrev, fmtAmerican, fmtPct, impliedProb } from '../oddsmath'
import { fetchInjuries, fetchMovement, fetchWeather } from '../api'
import { demoInjuries, demoMovement, demoWeather } from '../demoIntel'
import { fmtDT, fmtKick } from '../cardview'
import { derivePick, lineShop, pickSideName, type PickSide } from '../picks'
import { useMyBets } from '../mybets'
import { bookName } from '../config/referrals'
import { defaultStake, isPreferredBook, orderBooks, usePrefsVersion } from '../prefs'
import BookLink, { BetNowButton } from './BookLink'
import { Badge, Eyebrow, IconButton } from './common'
import { cls } from '../utils/format'
import { useUiMode } from '../uiMode'

// ------------------------------------------------------------- intel loading
// Lazy-fetched on first open, cached per game id (and per data mode) for the
// session. Every feed is optional: a failed fetch renders an explicit
// "NO … DATA" state and never breaks the drawer.

interface IntelState {
  loaded: boolean
  injuries: InjuryReport[] | null
  weather: WeatherInfo | null
  movement: MovementReport | null
}

const EMPTY_INTEL: IntelState = { loaded: false, injuries: null, weather: null, movement: null }
const intelCache = new Map<string, IntelState>()
// The injury endpoint is per sport, not per game — share one fetch per sport.
const injuryFeeds = new Map<Sport, Promise<InjuryReport[] | null>>()

function sharedInjuries(sport: Sport): Promise<InjuryReport[] | null> {
  let p = injuryFeeds.get(sport)
  if (!p) {
    p = fetchInjuries(sport)
    injuryFeeds.set(sport, p)
  }
  return p
}

function useIntel(card: GameCard, demo: boolean): IntelState {
  const key = `${demo ? 'demo' : 'live'}:${card.game.id}`
  const [intel, setIntel] = useState<IntelState>(() => intelCache.get(key) ?? EMPTY_INTEL)

  useEffect(() => {
    const cached = intelCache.get(key)
    if (cached) {
      setIntel(cached)
      return
    }
    if (demo) {
      const state: IntelState = {
        loaded: true,
        injuries: demoInjuries(card),
        weather: demoWeather(card),
        movement: demoMovement(card),
      }
      intelCache.set(key, state)
      setIntel(state)
      return
    }
    let alive = true
    setIntel(EMPTY_INTEL)
    const outdoorSport =
      card.game.sport === 'nfl' || card.game.sport === 'ncaaf' || card.game.sport === 'mlb'
    void Promise.all([
      sharedInjuries(card.game.sport),
      outdoorSport ? fetchWeather(card.game.id) : Promise.resolve(null),
      fetchMovement(card.game.id),
    ]).then(([injuries, weather, movement]) => {
      const state: IntelState = { loaded: true, injuries, weather, movement }
      intelCache.set(key, state)
      if (alive) setIntel(state)
    })
    return () => {
      alive = false
    }
  }, [key, demo, card])

  return intel
}

// ------------------------------------------------------------ table plumbing

const TH = 'px-2 py-1 text-left text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/70'
const TD = 'px-2 py-1 font-mono text-[10px] leading-4'

function DimNote({ children }: { children: string }) {
  return (
    <div className="rounded-lg border border-dashed border-white/10 bg-black/20 px-3 py-2 text-[10px] uppercase tracking-wider text-krypt-muted/60">
      {children}
    </div>
  )
}

/** Honest empty state when NO book has quoted the game at all — far-future
 *  slates legitimately have no lines yet; this reads as expected, not broken. */
function NoLinesNote({ sport }: { sport: Sport }) {
  return (
    <div className="rounded-xl border border-dashed border-white/10 bg-black/20 px-4 py-4 text-center">
      <div className="text-xs font-semibold text-white/85">No live prices for this game yet</div>
      <div className="mx-auto mt-1 max-w-[300px] text-[11px] leading-relaxed text-krypt-muted">
        Books and prediction markets post lines closer to start time.
        {sport === 'tennis' && ' Tennis odds require an Odds API key — see Settings.'}
      </div>
    </div>
  )
}

// ---------------------------------------------------------------- injuries

/** Conservative team match: exact, or containment when long enough to be safe. */
function teamMatches(rowTeam: string, team: string): boolean {
  const a = rowTeam.trim().toLowerCase()
  const b = team.trim().toLowerCase()
  if (a === b) return true
  const shorter = a.length <= b.length ? a : b
  const longer = a.length <= b.length ? b : a
  return shorter.length >= 4 && longer.includes(shorter)
}

export function injurySeverity(status: string): 'out' | 'quest' | 'dtd' {
  if (/out|injured reserve|\bir\b|60/i.test(status)) return 'out'
  if (/questionable|doubtful/i.test(status)) return 'quest'
  return 'dtd'
}

export function InjuryBadge({ status }: { status: string }) {
  const sev = injurySeverity(status)
  return <Badge tone={sev === 'out' ? 'danger' : 'warn'}>{status}</Badge>
}

const MAX_INJURY_ROWS = 12

function InjurySection({ card, intel }: { card: GameCard; intel: IntelState }) {
  const { game } = card
  // No league-wide injury feed: soccer, tennis, college (ncaaf / ncaab).
  if (
    game.sport === 'soccer' ||
    game.sport === 'tennis' ||
    game.sport === 'ncaaf' ||
    game.sport === 'ncaab'
  )
    return null
  const rows = (intel.injuries ?? []).filter(
    (r) =>
      r.status !== 'Active' && // offseason feeds list healthy actives; drop them
      (teamMatches(r.team, game.home) || teamMatches(r.team, game.away)),
  )
  rows.sort((a, b) => a.team.localeCompare(b.team) || a.player.localeCompare(b.player))
  const shown = rows.slice(0, MAX_INJURY_ROWS)

  return (
    <section>
      <Eyebrow sub={`${rows.length} listed · both teams`}>Injuries</Eyebrow>
      {!intel.loaded ? (
        <DimNote>Fetching injury feed…</DimNote>
      ) : rows.length === 0 ? (
        <DimNote>No injury data</DimNote>
      ) : (
        <div className="space-y-1">
          {shown.map((r, i) => (
            <div
              key={`${r.team}-${r.player}-${i}`}
              className="flex items-center gap-2 rounded-lg border border-white/5 bg-white/[0.02] px-2 py-1"
            >
              <span className="w-10 shrink-0 truncate text-[10px] font-semibold text-krypt-muted/70" title={r.team}>
                {r.team}
              </span>
              <span className="min-w-0 flex-1 truncate text-[11px] font-semibold text-white/90">
                {r.player}
                <span className="ml-1.5 font-mono text-[9px] text-krypt-muted/60">{r.position}</span>
              </span>
              <span className="shrink-0 truncate text-[9px] uppercase tracking-wider text-krypt-muted/60">
                {r.detail}
              </span>
              <InjuryBadge status={r.status} />
            </div>
          ))}
          {rows.length > shown.length && (
            <div className="px-2 pt-0.5 text-[9px] uppercase tracking-wider text-krypt-muted/50">
              +{rows.length - shown.length} more on the INTEL page
            </div>
          )}
        </div>
      )}
    </section>
  )
}

// ------------------------------------------------------------- line movement

function Sparkline({ values }: { values: number[] }) {
  const w = 84
  const h = 16
  const min = Math.min(...values)
  const max = Math.max(...values)
  const span = max - min || 1
  const pts = values
    .map((v, i) => {
      const x = values.length === 1 ? w / 2 : 1.5 + (i / (values.length - 1)) * (w - 3)
      const y = h - 2 - ((v - min) / span) * (h - 4)
      return `${x.toFixed(1)},${y.toFixed(1)}`
    })
    .join(' ')
  const last = pts.split(' ').pop()?.split(',') ?? []
  return (
    <svg width={w} height={h} viewBox={`0 0 ${w} ${h}`} className="shrink-0">
      <polyline
        points={pts}
        fill="none"
        stroke="#F43F5E"
        strokeWidth="1.25"
        strokeLinejoin="round"
        strokeLinecap="round"
        opacity={0.9}
      />
      {last.length === 2 && <circle cx={last[0]} cy={last[1]} r="1.75" fill="#F43F5E" />}
    </svg>
  )
}

interface BookSeries {
  book: string
  snaps: MovementSnapshot[]
}

function usableSeries(movement: MovementReport | null): BookSeries[] {
  if (!movement) return []
  return Object.entries(movement.books)
    .map(([book, snaps]) => ({ book, snaps: snaps.filter((s) => s.home_ml !== null) }))
    .filter((s) => s.snaps.length >= 2)
    .sort((a, b) => a.book.localeCompare(b.book))
}

function MovementSection({ card, intel }: { card: GameCard; intel: IntelState }) {
  const series = usableSeries(intel.movement)
  const homeLabel = card.game.sport === 'tennis' ? card.game.home : 'home ML'
  return (
    <section>
      <Eyebrow sub={`${homeLabel} · open → close`}>Line movement</Eyebrow>
      {!intel.loaded ? (
        <DimNote>Fetching snapshots…</DimNote>
      ) : series.length === 0 ? (
        <DimNote>No movement data — needs multiple snapshots</DimNote>
      ) : (
        <div className="space-y-1">
          {series.map(({ book, snaps }) => {
            const open = snaps[0].home_ml as number
            const close = snaps[snaps.length - 1].home_ml as number
            const probs = snaps.map((s) => impliedProb(s.home_ml as number))
            const steamedIn = probs[probs.length - 1] >= probs[0]
            return (
              <div
                key={book}
                className="flex items-center gap-2 rounded-lg border border-white/5 bg-white/[0.02] px-2 py-1"
              >
                <span className="w-9 shrink-0 text-[10px] font-semibold text-krypt-muted/70">
                  {bookAbbrev(book)}
                </span>
                <span className="min-w-0 flex-1 truncate font-mono text-[10px] text-white/80">
                  {fmtAmerican(open)} <span className="text-krypt-muted/40">→</span>{' '}
                  <span className={steamedIn ? 'text-emerald-300' : 'text-rose-400'}>
                    {fmtAmerican(close)}
                  </span>
                </span>
                <Sparkline values={probs} />
                <span className="w-[76px] shrink-0 text-right font-mono text-[9px] text-krypt-muted/60">
                  {fmtPct(probs[0])} → {fmtPct(probs[probs.length - 1])}%
                </span>
              </div>
            )
          })}
        </div>
      )}
    </section>
  )
}

// ------------------------------------------------------------------ weather

function WeatherChip({ card, intel }: { card: GameCard; intel: IntelState }) {
  const outdoorSport =
    card.game.sport === 'nfl' || card.game.sport === 'ncaaf' || card.game.sport === 'mlb'
  if (!outdoorSport) return null
  const wx = intel.weather
  return (
    <section>
      <Eyebrow>Weather</Eyebrow>
      {!intel.loaded ? (
        <DimNote>Fetching forecast…</DimNote>
      ) : wx === null ? (
        <DimNote>No weather data</DimNote>
      ) : (
        <div className="inline-flex items-center gap-2 rounded-lg border border-white/10 bg-white/[0.03] px-2.5 py-1.5">
          {wx.is_dome && <Badge tone="neutral">{wx.retractable ? 'ROOF' : 'DOME'}</Badge>}
          <span className="font-mono text-[10px] uppercase tracking-wide text-white/85">
            {wx.summary}
          </span>
        </div>
      )}
    </section>
  )
}

// ------------------------------------------------------------ books table

/** One moneyline price cell: emerald when it carries a +EV edge, and ringed
 *  when it's the best available price for the recommended pick side. */
function PriceCell({
  side,
  book,
  price,
  hasEdge,
  isBestCell,
}: {
  side: 'home' | 'away' | 'draw'
  book: string
  price: number | null
  hasEdge: (book: string, side: string) => boolean
  isBestCell: (side: 'home' | 'away' | 'draw', book: string) => boolean
}) {
  const best = price !== null && isBestCell(side, book)
  return (
    <td
      className={cls(
        TD,
        'text-right',
        hasEdge(book, side) || best ? 'font-semibold text-emerald-300' : 'text-white/80',
      )}
    >
      {price === null ? (
        '—'
      ) : best ? (
        <span className="rounded bg-emerald-500/10 px-1 py-0.5 ring-1 ring-emerald-400/50" title="Best price for the pick side">
          {fmtAmerican(price)}
        </span>
      ) : (
        fmtAmerican(price)
      )}
    </td>
  )
}

// -------------------------------------------------------------------- totals

/** TOTAL (O/U) strip — model total vs the best book line, with one-tap
 *  tracking for either side. Totals never feed picks/autobet (unvalidated);
 *  this is display + manual tracking, graded on the TRACKER like any bet. */
function TotalsSection({ card, demo }: { card: GameCard; demo: boolean }) {
  const totals = card.totals_edges ?? []
  const { track, isTracked, busy } = useMyBets(demo)
  usePrefsVersion()
  const stake = defaultStake()
  const pred = card.prediction
  if (totals.length === 0 || pred?.predicted_total == null) return null
  const line = card.odds.find((o) => o.book === totals[0].book)?.total_line
  if (line == null) return null
  return (
    <section>
      <div className="mb-1.5 flex items-baseline gap-2">
        <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
          Total · O/U {line}
        </span>
        <span className="text-[9px] uppercase tracking-wider text-krypt-muted/60">
          model total {pred.predicted_total.toFixed(1)}
        </span>
      </div>
      <div className="flex flex-wrap items-center gap-2">
        {totals.map((e) => {
          const tracked = isTracked(card.game.id, e.side)
          const positive = e.ev_per_dollar > 0
          return (
            <button
              key={e.side}
              type="button"
              disabled={tracked || busy}
              onClick={(ev) => {
                ev.stopPropagation()
                if (tracked || busy) return
                void track(
                  {
                    game_id: card.game.id,
                    side: e.side,
                    price: e.price,
                    book: e.book,
                    stake_units: stake,
                    line,
                  },
                  card,
                )
              }}
              title={
                tracked
                  ? 'Tracked — this bet is on your TRACKER'
                  : `Track ${e.side.toUpperCase()} ${line} at ${fmtAmerican(e.price)} (${e.book}) — model ${Math.round(e.model_prob * 100)}% to hit`
              }
              className={cls(
                'inline-flex items-center gap-1.5 rounded-lg border px-2.5 py-1.5 text-[10px] font-bold uppercase tracking-widest transition',
                tracked
                  ? 'cursor-default border-emerald-400/30 bg-emerald-500/10 text-emerald-300'
                  : positive
                    ? 'border-emerald-400/30 bg-emerald-500/[0.06] text-emerald-200 hover:bg-emerald-500/15'
                    : 'border-white/10 bg-white/[0.03] text-krypt-muted hover:border-white/20 hover:text-white',
              )}
            >
              {tracked ? 'TRACKED' : `${e.side} ${line}`}
              <span className="font-mono text-[9px] font-semibold normal-case tracking-normal text-current/80">
                {fmtAmerican(e.price)} · {Math.round(e.model_prob * 100)}% · EV{' '}
                {e.ev_per_dollar >= 0 ? '+' : ''}
                {(e.ev_per_dollar * 100).toFixed(1)}%
              </span>
            </button>
          )
        })}
      </div>
    </section>
  )
}

// ----------------------------------------------------------------- track bet

/** Compact TRACK button — one tap tracks a side (at the user's default
 *  stake, SETTINGS) onto the TRACKER; tracked state flips to a disabled
 *  check for that game + side. */
function DrawerTrackBtn({
  card,
  side,
  price,
  book,
  note,
  demo,
  label,
  className,
}: {
  card: GameCard
  side: PickSide
  price: number
  book: string | null
  note?: string | null
  demo: boolean
  /** Chip label override — defaults to TRACK BET / TRACKED. */
  label?: string
  className?: string
}) {
  const { track, isTracked, busy } = useMyBets(demo)
  usePrefsVersion() // re-render when the default stake changes
  const stake = defaultStake()
  const tracked = isTracked(card.game.id, side)
  const onClick = (e: ReactMouseEvent) => {
    e.stopPropagation()
    if (tracked || busy) return
    void track(
      { game_id: card.game.id, side, price, book, stake_units: stake, note: note ?? null },
      card,
    )
  }
  return (
    <button
      type="button"
      onClick={onClick}
      disabled={tracked || busy}
      title={
        tracked
          ? 'Tracked — this bet is on your TRACKER'
          : `Track ${pickSideName(side, card)} at ${fmtAmerican(price)} (${stake} unit${stake === 1 ? '' : 's'} — change under SETTINGS) — grades on your TRACKER when the game settles`
      }
      className={cls(
        'inline-flex shrink-0 items-center gap-1 rounded-lg border px-2 py-1 text-[9px] font-bold uppercase tracking-widest transition',
        tracked
          ? 'cursor-default border-emerald-400/40 bg-emerald-500/15 text-emerald-300'
          : 'border-emerald-400/40 bg-emerald-500/10 text-emerald-300 hover:bg-emerald-500/25 active:scale-[0.98] disabled:opacity-60',
        className,
      )}
    >
      {tracked ? <Check size={10} /> : <Plus size={10} />}
      {tracked ? 'TRACKED' : (label ?? 'TRACK BET')}
    </button>
  )
}

/** PRO: one TRACK chip per side at its best available price ("the plus button
 *  on each play", drawer edition). Sides without a live price are skipped. */
function TrackSidesSection({ card, demo }: { card: GameCard; demo: boolean }) {
  const threeWay =
    card.odds.some((o) => o.draw_ml !== null) || card.prediction?.draw_prob != null
  const sides: PickSide[] = threeWay ? ['home', 'draw', 'away'] : ['home', 'away']
  const rows = sides
    .map((side) => ({ side, best: lineShop(card, side)[0] ?? null }))
    .filter((r): r is { side: PickSide; best: { book: string; price: number; best: boolean } } => r.best !== null)
  if (rows.length === 0 || card.game.completed) return null
  return (
    <section>
      <Eyebrow sub="best price per side · at your default stake">Track a bet</Eyebrow>
      <div className="flex flex-wrap items-center gap-1.5">
        {rows.map(({ side, best }) => (
          <DrawerTrackBtn
            key={side}
            card={card}
            side={side}
            price={best.price}
            book={best.book}
            demo={demo}
            label={`${pickSideName(side, card)} ${fmtAmerican(best.price)} · ${bookAbbrev(best.book)}`}
          />
        ))}
      </div>
    </section>
  )
}

// -------------------------------------------------------------------- drawer

interface Props {
  card: GameCard | null
  demo: boolean
  onClose: () => void
}

/** Right-side slide-open drawer: books, edges, model meta, injuries, weather,
 *  line movement. Glass panel with HUD corner brackets. */
export default function DetailDrawer({ card, demo, onClose }: Props) {
  useEffect(() => {
    if (!card) return
    const onKey = (e: KeyboardEvent) => {
      if (e.key === 'Escape') onClose()
    }
    window.addEventListener('keydown', onKey)
    return () => window.removeEventListener('keydown', onKey)
  }, [card, onClose])

  if (!card) return null
  return <DrawerBody card={card} demo={demo} onClose={onClose} />
}

function DrawerBody({ card, demo, onClose }: { card: GameCard; demo: boolean; onClose: () => void }) {
  const simple = useUiMode() === 'simple'
  usePrefsVersion() // re-render when preferred books change
  const { game, prediction, odds, edges } = card
  const intel = useIntel(card, demo)
  const tennis = game.sport === 'tennis'
  const threeWay = odds.some((o) => o.draw_ml !== null) || prediction?.draw_prob != null
  const hasEdge = (book: string, side: string) => edges.some((e) => e.book === book && e.side === side)
  const kick = fmtKick(game.start_time)
  // Recommended bet: drives the summary strip + books-table highlighting.
  // Preferred books order first; `best` flags are computed before re-ordering
  // so BEST-price highlighting is unaffected by preference.
  const pick = derivePick(card)
  const pickShop = pick ? orderBooks(lineShop(card, pick.side), (s) => s.book) : []
  const orderedOdds = orderBooks(odds, (o) => o.book)
  const isPickCol = (side: 'home' | 'away' | 'draw') => pick?.side === side
  const isBestCell = (side: 'home' | 'away' | 'draw', book: string) =>
    isPickCol(side) && pickShop.some((s) => s.best && s.book === book)
  const latestFetch = odds.reduce<string | null>(
    (acc, o) => (acc === null || o.fetched_at > acc ? o.fetched_at : acc),
    null,
  )

  return (
    <>
      <div
        className="fixed inset-0 top-9 z-40 animate-fade-in bg-black/50 backdrop-blur-[2px]"
        onClick={onClose}
      />
      <aside className="glass hud fixed bottom-0 right-0 top-9 z-50 flex w-[420px] max-w-full animate-drawer-in flex-col border-l border-white/10 shadow-krypt-card">
        {/* Header */}
        <div className="flex shrink-0 items-start gap-3 border-b border-white/10 px-4 py-3">
          <div className="min-w-0 flex-1">
            <div className="flex items-center gap-2">
              <span className="rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-krypt-muted">
                {game.league || SPORT_TAG[game.sport]}
              </span>
              <span className="font-mono text-[10px] text-krypt-muted">
                {kick.date} · {kick.time}
              </span>
              {demo && <Badge tone="gradient">DEMO</Badge>}
            </div>
            <h2 className="mt-1.5 truncate text-base font-bold leading-tight text-white">
              {tennis ? (
                <>
                  {game.home} <span className="font-normal text-krypt-muted/60">vs</span> {game.away}
                </>
              ) : (
                <>
                  {game.away} <span className="font-normal text-krypt-muted/60">@</span> {game.home}
                </>
              )}
            </h2>
          </div>
          <IconButton onClick={onClose} title="Close (Esc)">
            <X size={15} />
          </IconButton>
        </div>

        {/* Body */}
        <div className="min-h-0 flex-1 space-y-5 overflow-y-auto px-4 py-4">
          {/* One-line recommended-bet summary. */}
          {pick && (
            <div
              className={cls(
                'flex items-center gap-2 rounded-xl border px-3 py-2',
                pick.kind === 'value'
                  ? 'border-emerald-400/20 bg-emerald-500/[0.07]'
                  : 'border-white/10 bg-white/[0.03]',
              )}
            >
              <Target
                size={12}
                className={cls('shrink-0', pick.kind === 'value' ? 'text-emerald-300' : 'text-krypt-muted')}
              />
              {simple ? (
                <span className="min-w-0 flex-1 truncate text-xs font-semibold leading-none">
                  {pick.kind === 'value' ? (
                    <span className="text-emerald-300">
                      Take {pick.team} at{' '}
                      <span className="font-mono">{fmtAmerican(pick.price as number)}</span> on{' '}
                      {bookName(pick.book as string)}
                    </span>
                  ) : (
                    <span className="text-white/80">
                      Our model favors {pick.team} —{' '}
                      <span className="font-mono">{Math.round(pick.modelProb * 100)}%</span> to win
                    </span>
                  )}
                </span>
              ) : (
                <span className="min-w-0 truncate font-mono text-[11px] font-semibold leading-none">
                  {pick.kind === 'value' ? (
                    <span className="text-emerald-300">
                      TAKE {pick.team} {fmtAmerican(pick.price as number)} @ {bookAbbrev(pick.book as string)}
                      <span
                        className="text-emerald-300/60"
                        title="EV = expected value: average profit per $1 bet if the model probability is right"
                      >
                        {' '}· EV +{fmtPct(pick.ev as number)}%
                      </span>
                      <span
                        className="text-krypt-muted/70"
                        title="MIN = worst price still worth taking (model fair line)"
                      >
                        {' '}· MIN {fmtAmerican(pick.minAcceptable)}
                      </span>
                    </span>
                  ) : (
                    <span className="text-white/80">
                      TAKE {pick.team}
                      <span className="text-krypt-muted"> · MODEL {fmtPct(pick.modelProb)}%</span>
                      <span
                        className="text-krypt-muted/70"
                        title="Fair = the model's break-even odds; no book beats it right now"
                      >
                        {' '}· FAIR {fmtAmerican(pick.fairPrice)} · NO +EV PRICE
                      </span>
                    </span>
                  )}
                </span>
              )}
              {pick.kind === 'value' ? (
                <span className="ml-auto flex shrink-0 items-center gap-1.5">
                  <BetNowButton book={pick.book as string} />
                  <DrawerTrackBtn
                    card={card}
                    side={pick.side}
                    price={pick.price as number}
                    book={pick.book}
                    demo={demo}
                  />
                </span>
              ) : (
                <DrawerTrackBtn
                  card={card}
                  side={pick.side}
                  price={pick.fairPrice}
                  book={null}
                  note="tracked at fair (no book price)"
                  demo={demo}
                  className="ml-auto"
                />
              )}
            </div>
          )}

          <TotalsSection card={card} demo={demo} />

          {simple ? (
            // SIMPLE: just the best prices for the pick side — no quant tables.
            <section>
              {pick ? (
                <>
                  <Eyebrow sub={`${pickShop.length} books quoted`}>
                    Best prices — {pick.team} to win
                  </Eyebrow>
                  {pickShop.length === 0 ? (
                    odds.length === 0 ? (
                      <NoLinesNote sport={game.sport} />
                    ) : (
                      <DimNote>No live prices for this side yet</DimNote>
                    )
                  ) : (
                    <div className="overflow-hidden rounded-xl border border-white/10">
                      <table className="w-full border-collapse bg-white/[0.02]">
                        <tbody>
                          {pickShop.map((s) => (
                            <tr key={s.book} className="border-b border-white/5 last:border-0">
                              <td className={cls(TD, 'font-sans text-[11px] font-semibold text-white/85')}>
                                <BookLink
                                  book={s.book}
                                  className="inline-flex items-center gap-1.5 hover:text-white"
                                >
                                  {isPreferredBook(s.book) && (
                                    <Pin size={10} className="shrink-0 text-krypt-muted/50" />
                                  )}
                                  {bookName(s.book)}
                                </BookLink>
                              </td>
                              <td
                                className={cls(
                                  TD,
                                  'text-right text-[11px]',
                                  s.best ? 'font-semibold text-emerald-300' : 'text-white/80',
                                )}
                              >
                                {fmtAmerican(s.price)}
                                {s.best && (
                                  <span className="ml-1.5 align-middle">
                                    <Badge tone="success">BEST</Badge>
                                  </span>
                                )}
                              </td>
                            </tr>
                          ))}
                        </tbody>
                      </table>
                    </div>
                  )}
                </>
              ) : (
                <DimNote>No pick for this game yet</DimNote>
              )}
            </section>
          ) : (
            <>
          {/* Edges first — the reason you opened the drawer. */}
          <section>
            <div className="mb-2 flex items-center gap-1.5">
              <Zap size={11} className="text-emerald-300" />
              <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
                Edges
              </span>
              <span className="text-[10px] uppercase tracking-wider text-krypt-muted/70">
                {edges.length} positive-EV
              </span>
            </div>
            {edges.length === 0 ? (
              <DimNote>No positive-EV edges at current prices</DimNote>
            ) : (
              <div className="overflow-hidden rounded-xl border border-emerald-400/15">
                <table className="w-full border-collapse bg-emerald-500/[0.04]">
                  <thead>
                    <tr className="border-b border-emerald-400/10">
                      <th className={TH}>Side</th>
                      <th className={TH}>Book</th>
                      <th className={cls(TH, 'text-right')}>Price</th>
                      <th className={cls(TH, 'text-right')} title="Model win probability">Model</th>
                      <th className={cls(TH, 'text-right')} title="Market-implied probability with the vig removed">Mkt</th>
                      <th className={cls(TH, 'text-right')} title="EV = expected value: average profit per $1 bet if the model probability is right">EV</th>
                      <th className={cls(TH, 'text-right')} title="Kelly = bankroll-sizing formula (full-Kelly stake fraction)">Kelly</th>
                    </tr>
                  </thead>
                  <tbody>
                    {edges.map((e) => (
                      <tr key={`${e.book}-${e.side}`} className="border-b border-white/5 last:border-0">
                        <td className={cls(TD, 'font-sans font-semibold uppercase text-emerald-300')}>
                          {e.side}
                        </td>
                        <td className={cls(TD, 'text-white/80')}>
                          <BookLink book={e.book}>{bookAbbrev(e.book)}</BookLink>
                        </td>
                        <td className={cls(TD, 'text-right text-white/85')}>{fmtAmerican(e.price)}</td>
                        <td className={cls(TD, 'text-right text-white/85')}>{fmtPct(e.model_prob)}%</td>
                        <td className={cls(TD, 'text-right text-krypt-muted')}>{fmtPct(e.market_prob)}%</td>
                        <td className={cls(TD, 'text-right font-semibold text-emerald-300')}>
                          +{fmtPct(e.ev_per_dollar)}%
                        </td>
                        <td className={cls(TD, 'text-right text-emerald-300/80')}>
                          {fmtPct(e.kelly_fraction)}%
                        </td>
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          {/* Per-book odds */}
          <section>
            <div className="mb-2 flex items-center gap-1.5">
              <BookOpen size={11} className="text-scout-red-bright" />
              <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
                Books
              </span>
              <span className="text-[10px] uppercase tracking-wider text-krypt-muted/70">
                {odds.length} quoted{latestFetch ? ` · as of ${fmtDT(latestFetch)}` : ''}
              </span>
            </div>
            {odds.length === 0 ? (
              <NoLinesNote sport={game.sport} />
            ) : (
              <div className="overflow-hidden rounded-xl border border-white/10">
                <table className="w-full border-collapse bg-white/[0.02]">
                  <thead>
                    <tr className="border-b border-white/10">
                      <th className={TH}>Book</th>
                      <th className={cls(TH, 'text-right', isPickCol('home') && 'text-emerald-300')}>
                        {isPickCol('home') && <span className="mr-1 text-emerald-300/60">PICK</span>}
                        {tennis ? 'P1' : 'Home'}
                      </th>
                      {threeWay && (
                        <th className={cls(TH, 'text-right', isPickCol('draw') && 'text-emerald-300')}>
                          {isPickCol('draw') && <span className="mr-1 text-emerald-300/60">PICK</span>}
                          Draw
                        </th>
                      )}
                      <th className={cls(TH, 'text-right', isPickCol('away') && 'text-emerald-300')}>
                        {isPickCol('away') && <span className="mr-1 text-emerald-300/60">PICK</span>}
                        {tennis ? 'P2' : 'Away'}
                      </th>
                      {!tennis && (
                        <>
                          <th className={cls(TH, 'text-right')}>Tot</th>
                          <th className={cls(TH, 'text-right')}>O</th>
                          <th className={cls(TH, 'text-right')}>U</th>
                        </>
                      )}
                    </tr>
                  </thead>
                  <tbody>
                    {orderedOdds.map((o) => (
                      <tr key={o.book} className="border-b border-white/5 last:border-0">
                        <td className={cls(TD, 'font-sans text-[10px] font-semibold text-white/80')}>
                          <BookLink book={o.book} className="inline-flex items-center gap-1">
                            {isPreferredBook(o.book) && (
                              <Pin size={10} className="shrink-0 text-krypt-muted/50" />
                            )}
                            {bookAbbrev(o.book)}
                          </BookLink>
                        </td>
                        <PriceCell side="home" book={o.book} price={o.home_ml} hasEdge={hasEdge} isBestCell={isBestCell} />
                        {threeWay && (
                          <PriceCell side="draw" book={o.book} price={o.draw_ml} hasEdge={hasEdge} isBestCell={isBestCell} />
                        )}
                        <PriceCell side="away" book={o.book} price={o.away_ml} hasEdge={hasEdge} isBestCell={isBestCell} />
                        {!tennis && (
                          <>
                            <td className={cls(TD, 'text-right text-krypt-muted')}>{o.total_line ?? '—'}</td>
                            <td className={cls(TD, 'text-right text-krypt-muted')}>
                              {o.over_price !== null ? fmtAmerican(o.over_price) : '—'}
                            </td>
                            <td className={cls(TD, 'text-right text-krypt-muted')}>
                              {o.under_price !== null ? fmtAmerican(o.under_price) : '—'}
                            </td>
                          </>
                        )}
                      </tr>
                    ))}
                  </tbody>
                </table>
              </div>
            )}
          </section>

          <TrackSidesSection card={card} demo={demo} />
            </>
          )}

          <WeatherChip card={card} intel={intel} />

          <InjurySection card={card} intel={intel} />

          {!simple && <MovementSection card={card} intel={intel} />}

          {!simple && (
          <section className="pb-1">
            <div className="mb-2 flex items-center gap-1.5">
              <Cpu size={11} className="text-scout-red-bright" />
              <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
                Model
              </span>
            </div>
            {prediction ? (
              <div className="grid grid-cols-2 gap-x-4 gap-y-1.5 rounded-xl border border-white/10 bg-white/[0.02] p-3">
                <Meta label="Engine" value={prediction.model} accent />
                <Meta label="Generated" value={fmtDT(prediction.generated_at)} mono />
                {prediction.confidence !== null && (
                  <Meta label="Confidence" value={`${fmtPct(prediction.confidence)}%`} mono />
                )}
                {prediction.over_prob !== null && (
                  <Meta label="P(over)" value={`${fmtPct(prediction.over_prob)}%`} mono />
                )}
                {prediction.predicted_home_score !== null && prediction.predicted_away_score !== null && (
                  <Meta
                    label="Proj score"
                    value={`${game.away} ${prediction.predicted_away_score.toFixed(1)} — ${game.home} ${prediction.predicted_home_score.toFixed(1)}`}
                    mono
                    wide
                  />
                )}
                <Meta label="Game ID" value={game.id} mono dim wide />
              </div>
            ) : (
              <DimNote>No model output</DimNote>
            )}
          </section>
          )}
        </div>
      </aside>
    </>
  )
}

function Meta({
  label,
  value,
  mono,
  accent,
  dim,
  wide,
}: {
  label: string
  value: string
  mono?: boolean
  accent?: boolean
  dim?: boolean
  wide?: boolean
}) {
  return (
    <div className={cls('min-w-0', wide && 'col-span-2')}>
      <div className="text-[9px] font-semibold uppercase tracking-widest text-krypt-muted/60">
        {label}
      </div>
      <div
        className={cls(
          'truncate text-[11px] leading-4',
          mono && 'font-mono text-[10px]',
          accent ? 'font-semibold text-scout-red-bright' : dim ? 'text-krypt-muted' : 'text-white/85',
        )}
        title={value}
      >
        {value}
      </div>
    </div>
  )
}
