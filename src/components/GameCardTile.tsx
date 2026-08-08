import { forwardRef } from 'react'
import { Target } from 'lucide-react'
import type { GameCard } from '../types'
import { SPORT_TAG } from '../types'
import { bestPrice, bookAbbrev, consensusTotal, fairLine, fmtAmerican, fmtPct } from '../oddsmath'
import { daysFromToday, favoredKey, fmtKick, fmtShortDate, matchLabel, sideViews } from '../cardview'
import { derivePick } from '../picks'
import BookLink from './BookLink'
import { cls } from '../utils/format'
import { useUiMode } from '../uiMode'

function SideRow({
  tag,
  name,
  prob,
  favored,
  draw,
}: {
  tag: string
  name: string
  prob: number
  favored: boolean
  draw: boolean
}) {
  return (
    <div className="flex items-center gap-2">
      <span className="w-5 shrink-0 font-mono text-[9px] font-semibold uppercase text-krypt-muted/60">
        {tag}
      </span>
      <span
        className={cls(
          'w-[96px] shrink-0 truncate text-xs font-semibold leading-4',
          favored ? 'text-white' : draw ? 'text-krypt-muted/70' : 'text-krypt-muted',
        )}
        title={name}
      >
        {name}
      </span>
      <div className="h-1 min-w-0 flex-1 overflow-hidden rounded-full bg-white/[0.06]">
        <div
          className={cls(
            'h-full rounded-full',
            favored ? 'bg-emerald-400/80' : draw ? 'bg-white/15' : 'bg-white/25',
          )}
          style={{ width: `${Math.min(100, Math.max(0, prob * 100))}%` }}
        />
      </div>
      <span
        className={cls(
          'w-11 shrink-0 text-right font-mono text-[11px] leading-4',
          favored ? 'font-semibold text-emerald-300' : 'text-krypt-muted',
        )}
      >
        {fmtPct(prob)}%
      </span>
    </div>
  )
}

interface Props {
  card: GameCard
  highlighted?: boolean
  /** Pulsing LIVE chip — game started but not completed (TODAY page pinning). */
  live?: boolean
  /** Rendered in a cross-sport list — surface the sport tag on the card header. */
  crossSport?: boolean
  onOpen: () => void
}

/** One compartmentalized game card in the boards grid. Click → detail drawer. */
const GameCardTile = forwardRef<HTMLDivElement, Props>(function GameCardTile(
  { card, highlighted, live, crossSport, onOpen },
  ref,
) {
  const simple = useUiMode() === 'simple'
  const { game, prediction, odds, edges } = card
  const sides = sideViews(card)
  const fav = favoredKey(card)
  const favSide = fav ? sides.find((s) => s.key === fav) ?? null : null
  const kick = fmtKick(game.start_time)
  const pick = derivePick(card)
  const valuePick = pick?.kind === 'value'
  const bookTotal = consensusTotal(odds)
  const showTotal = game.sport !== 'tennis' && (prediction?.predicted_total != null || bookTotal != null)
  const league = game.league || SPORT_TAG[game.sport]
  // 7+ days out: lead with the date so far-future slates are obviously not today.
  const farOut = daysFromToday(game.start_time) >= 7
  // Cross-sport lists get an explicit sport chip unless the league badge already says it.
  const showSportChip = crossSport && league.toUpperCase() !== SPORT_TAG[game.sport]
  const finalScore =
    game.completed && game.home_score !== null && game.away_score !== null
      ? `${game.away_score}–${game.home_score}`
      : null

  return (
    <div
      ref={ref}
      role="button"
      tabIndex={0}
      onClick={onOpen}
      onKeyDown={(e) => {
        if (e.key === 'Enter' || e.key === ' ') {
          e.preventDefault()
          onOpen()
        }
      }}
      className={cls(
        'group relative flex cursor-pointer flex-col overflow-hidden rounded-2xl border bg-krypt-panel/80 shadow-krypt-card outline-none transition',
        valuePick
          ? 'border-white/10 hover:border-scout-red/40 hover:shadow-scout-glow'
          : 'border-white/10 hover:border-white/20',
        'focus-visible:ring-1 focus-visible:ring-scout-red/60',
        highlighted && 'animate-flash-ring border-scout-red/50',
      )}
    >
      {/* Subtle scout-red spine on cards carrying a live edge. */}
      {valuePick && (
        <span className="absolute bottom-3 left-0 top-3 w-[2px] rounded-r-full bg-scout-red/70" />
      )}

      <div className={cls('flex-1', simple ? 'p-4' : 'p-3')}>
        {/* Top row: LIVE / kickoff (date-first when far out) + sport/league badges. */}
        <div className="flex items-center gap-2">
          {live && (
            <span className="flex items-center gap-1 rounded border border-scout-red/40 bg-scout-red/10 px-1.5 py-0.5 font-mono text-[9px] font-bold leading-none tracking-widest text-scout-red-bright">
              <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-scout-red-bright" />
              LIVE
            </span>
          )}
          {farOut ? (
            <>
              <span
                className="font-mono text-[11px] font-semibold leading-none text-white/85"
                title="Scheduled well ahead — lines post closer to game time"
              >
                {fmtShortDate(game.start_time)}
              </span>
              <span className="font-mono text-[10px] leading-none text-krypt-muted/60">{kick.time}</span>
            </>
          ) : (
            <>
              <span className="font-mono text-[11px] font-semibold leading-none text-white/85">
                {kick.time}
              </span>
              <span className="font-mono text-[10px] leading-none text-krypt-muted/60">{kick.date}</span>
            </>
          )}
          {game.completed && (
            <span className="rounded border border-white/10 bg-white/5 px-1.5 py-0.5 font-mono text-[9px] font-semibold leading-none tracking-wider text-krypt-muted">
              FINAL{finalScore ? ` ${finalScore}` : ''}
            </span>
          )}
          {/* Honest flag when the engine itself isn't sure (e.g. damped
              preseason / early-season ratings) — subtle, informational amber. */}
          {prediction?.confidence != null && prediction.confidence < 0.25 && (
            <span
              className="rounded border border-amber-400/25 bg-amber-500/[0.08] px-1.5 py-0.5 font-mono text-[8px] font-semibold leading-none tracking-widest text-amber-300/90"
              title="Model confidence under 25% — early-season or preseason ratings carry less signal"
            >
              LOW CONFIDENCE
            </span>
          )}
          <span className="ml-auto flex items-center gap-1.5">
            {showSportChip && (
              <span className="rounded border border-scout-red/30 bg-scout-red/10 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-scout-red-bright/90">
                {SPORT_TAG[game.sport]}
              </span>
            )}
            <span className="rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-[9px] font-bold uppercase tracking-widest text-krypt-muted">
              {league}
            </span>
          </span>
          {!simple && <span className="font-mono text-[9px] text-krypt-muted/50">{odds.length} BK</span>}
        </div>

        {simple ? (
          // SIMPLE: matchup + one win-chance bar for the favorite.
          <>
            <div
              className="mt-3 truncate text-base font-bold leading-tight text-white"
              title={matchLabel(card)}
            >
              {matchLabel(card)}
            </div>
            {favSide ? (
              <div className="mt-3">
                <div className="flex items-baseline justify-between gap-2">
                  <span className="min-w-0 truncate text-sm font-semibold text-white/90" title={favSide.name}>
                    {favSide.name}
                  </span>
                  <span className="shrink-0 font-mono text-sm font-semibold text-emerald-300">
                    {Math.round(favSide.prob * 100)}% to win
                  </span>
                </div>
                <div className="mt-1.5 h-1.5 overflow-hidden rounded-full bg-white/[0.06]">
                  <div
                    className="h-full rounded-full bg-emerald-400/80"
                    style={{ width: `${Math.min(100, Math.max(0, favSide.prob * 100))}%` }}
                  />
                </div>
              </div>
            ) : (
              <div className="mt-3 py-2 text-center text-[10px] uppercase tracking-wider text-krypt-muted/60">
                No prediction for this game yet
              </div>
            )}
            {odds.length === 0 && (
              <div className="mt-3 border-t border-white/5 pt-2 text-center text-[11px] text-krypt-muted/70">
                No prices yet — lines post closer to game time
              </div>
            )}
          </>
        ) : (
          <>
            {/* Probability bars per side. */}
            <div className="mt-2.5 space-y-1.5">
              {sides.length > 0 ? (
                sides.map((s) => (
                  <SideRow
                    key={s.key}
                    tag={s.tag}
                    name={s.name}
                    prob={s.prob}
                    favored={s.key === fav}
                    draw={s.key === 'draw'}
                  />
                ))
              ) : (
                <div className="py-2 text-center text-[10px] uppercase tracking-wider text-krypt-muted/60">
                  No model output — {game.sport === 'tennis' ? `${game.home} vs ${game.away}` : `${game.away} @ ${game.home}`}
                </div>
              )}
            </div>

            {/* Footer: model fair vs best book per side + totals — or an honest
                no-lines note when no book has quoted this game yet. */}
            {odds.length === 0 ? (
              <div
                className="mt-2.5 border-t border-white/5 pt-2 text-center font-mono text-[9px] uppercase tracking-[0.2em] text-krypt-muted/50"
                title="Books and prediction markets post lines closer to start time"
              >
                LINES NOT POSTED YET
              </div>
            ) : (
            <div className="mt-2.5 grid grid-cols-2 gap-x-3 gap-y-0.5 border-t border-white/5 pt-2 font-mono text-[10px] leading-4">
              {sides.map((s) => {
                const bp = bestPrice(odds, s.mlKey)
                const hasEdge = edges.some((e) => e.side === s.key)
                return (
                  <div key={s.key} className="flex min-w-0 items-baseline justify-between gap-1.5">
                    <span className="shrink-0 text-krypt-muted/50">{s.tag}</span>
                    <span className="truncate text-right">
                      <span className="text-white/70" title="Model fair line (break-even price)">
                        {fmtAmerican(fairLine(s.prob))}
                      </span>
                      <span className="text-krypt-muted/40"> → </span>
                      {bp ? (
                        <>
                          <span className={hasEdge ? 'font-semibold text-emerald-300' : 'text-white/85'}>
                            {fmtAmerican(bp.price)}
                          </span>
                          <span className="text-krypt-muted/50"> {bookAbbrev(bp.book)}</span>
                        </>
                      ) : (
                        <span className="text-krypt-muted/40">—</span>
                      )}
                    </span>
                  </div>
                )
              })}
              {showTotal && (
                <div className="flex min-w-0 items-baseline justify-between gap-1.5">
                  <span className="shrink-0 text-krypt-muted/50">TOT</span>
                  <span className="truncate text-right">
                    <span className="text-white/70">
                      {prediction?.predicted_total != null ? prediction.predicted_total.toFixed(1) : '—'}
                    </span>
                    <span className="text-krypt-muted/40"> vs </span>
                    <span className="text-white/85">{bookTotal ?? '—'}</span>
                  </span>
                </div>
              )}
            </div>
            )}
          </>
        )}
      </div>

      {/* PICK strip — the recommended bet at a glance (replaces the EV strip):
          emerald when backed by a +EV book price, neutral for model-only. */}
      {pick && (
        <div
          className={cls(
            'flex items-center gap-2 border-t px-3 py-1.5',
            valuePick
              ? 'border-emerald-400/15 bg-emerald-500/[0.08]'
              : 'border-white/5 bg-white/[0.03]',
          )}
        >
          <Target
            size={11}
            className={cls('shrink-0', valuePick ? 'text-emerald-300' : 'text-krypt-muted')}
          />
          <span
            className={cls(
              'truncate font-mono text-[11px] font-semibold leading-none',
              valuePick ? 'text-emerald-300' : 'text-white/75',
            )}
            title={pick.team}
          >
            PICK {pick.team}
          </span>
          {valuePick ? (
            <>
              {!simple && (
                <span
                  className="shrink-0 font-mono text-[10px] leading-none text-emerald-300/60"
                  title="EV = expected value: average profit per $1 bet if the model probability is right"
                >
                  +{fmtPct(pick.ev as number)}% EV
                </span>
              )}
              <BookLink
                book={pick.book as string}
                className="ml-auto shrink-0 rounded border border-emerald-400/20 bg-emerald-500/10 px-1.5 py-0.5 font-mono text-[9px] font-semibold leading-none text-emerald-200 transition hover:bg-emerald-500/20"
              >
                {bookAbbrev(pick.book as string)} {fmtAmerican(pick.price as number)}
              </BookLink>
              {!simple && edges.length > 1 && (
                <span className="shrink-0 font-mono text-[9px] leading-none text-emerald-300/50">
                  +{edges.length - 1}
                </span>
              )}
            </>
          ) : simple ? (
            <span className="ml-auto shrink-0 rounded border border-white/10 bg-white/5 px-1.5 py-0.5 font-mono text-[9px] font-semibold leading-none text-krypt-muted">
              FAVORITE
            </span>
          ) : (
            <span
              className="ml-auto shrink-0 rounded border border-white/10 bg-white/5 px-1.5 py-0.5 font-mono text-[9px] font-semibold leading-none text-krypt-muted"
              title="Fair = the model's break-even odds; no book beats it right now"
            >
              FAIR {fmtAmerican(pick.fairPrice)} · MODEL
            </span>
          )}
        </div>
      )}
    </div>
  )
})

export default GameCardTile
