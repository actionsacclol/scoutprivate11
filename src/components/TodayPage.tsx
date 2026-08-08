import { useEffect, useMemo, useState } from 'react'
import { CalendarDays, ChevronDown, ChevronRight, FlaskConical, RefreshCw } from 'lucide-react'
import type { BoardMap, GameCard } from '../types'
import { SPORTS, SPORT_TAG } from '../types'
import { byStartAsc, daysFromToday, matchLabel } from '../cardview'
import GameCardTile from './GameCardTile'
import DetailDrawer from './DetailDrawer'
import { Badge, Empty, Eyebrow, StripBtn } from './common'
import { cls } from '../utils/format'
import { useUiMode } from '../uiMode'

/** "7:05 PM" — friendly kickoff time for the NEXT chip. */
function fmtClock(iso: string): string {
  return new Date(iso).toLocaleTimeString([], { hour: 'numeric', minute: '2-digit' })
}

/** "IN 1H 23M" / "IN 43M" until the given kickoff. */
function fmtUntil(iso: string, now: Date): string | null {
  const mins = Math.round((new Date(iso).getTime() - now.getTime()) / 60_000)
  if (mins <= 0) return null
  const h = Math.floor(mins / 60)
  const m = mins % 60
  return h > 0 ? `IN ${h}H ${String(m).padStart(2, '0')}M` : `IN ${m}M`
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

/** TODAY — one chronological cross-sport view of everything on today's local
 *  slate: in-progress games pinned live on top, then soonest-first, with
 *  finished games sunk into a collapsed FINAL section. */
export default function TodayPage({
  boards,
  demo,
  apiUp,
  fetchedAt,
  loading,
  onRefresh,
  onToggleDemo,
}: Props) {
  const simple = useUiMode() === 'simple'
  const [openId, setOpenId] = useState<string | null>(null)
  const [showFinals, setShowFinals] = useState(false)
  // Re-bucket live/upcoming and refresh the countdown as the clock moves.
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 30_000)
    return () => clearInterval(t)
  }, [])

  const { todays, live, upcoming, finals, sportsOn } = useMemo(() => {
    const todays: GameCard[] = []
    for (const s of SPORTS) {
      for (const card of boards[s] ?? []) {
        if (daysFromToday(card.game.start_time, now) === 0) todays.push(card)
      }
    }
    todays.sort(byStartAsc)
    const nowMs = now.getTime()
    const live = todays.filter(
      (c) => !c.game.completed && new Date(c.game.start_time).getTime() <= nowMs,
    )
    const upcoming = todays.filter(
      (c) => !c.game.completed && new Date(c.game.start_time).getTime() > nowMs,
    )
    const finals = todays.filter((c) => c.game.completed)
    const sportsOn = new Set(todays.map((c) => c.game.sport)).size
    return { todays, live, upcoming, finals, sportsOn }
  }, [boards, now])

  const next = upcoming[0] ?? null
  const openCard: GameCard | null = openId
    ? (todays.find((c) => c.game.id === openId) ?? null)
    : null

  const dateLabel = `${now
    .toLocaleDateString('en-US', { weekday: 'long' })
    .toUpperCase()} · ${now
    .toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    .toUpperCase()}`

  const grid = simple
    ? 'grid gap-4 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]'
    : 'grid gap-3 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]'

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Header strip: date · count · next kickoff + the usual data affordances. */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-white/5 bg-krypt-panel/40 px-4 py-2">
        <span className="flex items-center gap-2">
          <CalendarDays size={13} className="text-scout-red-bright" />
          <span className="font-mono text-[11px] font-bold tracking-widest text-white">
            {dateLabel}
          </span>
        </span>
        <span className="rounded border border-white/10 bg-white/5 px-1.5 py-0.5 font-mono text-[9px] font-semibold tracking-wider text-krypt-muted">
          {todays.length} {todays.length === 1 ? 'GAME' : 'GAMES'} TODAY
        </span>
        {live.length > 0 && (
          <span className="flex items-center gap-1.5 rounded border border-scout-red/40 bg-scout-red/10 px-1.5 py-0.5 font-mono text-[9px] font-bold tracking-widest text-scout-red-bright">
            <span className="h-1.5 w-1.5 animate-pulse rounded-full bg-scout-red-bright" />
            {live.length} LIVE
          </span>
        )}
        {next && (
          <span
            className="truncate font-mono text-[10px] tracking-wide text-krypt-muted"
            title={`Next kickoff — ${matchLabel(next)}`}
          >
            <span className="text-krypt-muted/60">NEXT: </span>
            <span className="font-semibold text-white/85">{fmtClock(next.game.start_time)}</span>
            <span className="text-krypt-muted/60"> — {matchLabel(next)}</span>
            {fmtUntil(next.game.start_time, now) && (
              <span className="text-scout-red-bright/80"> · {fmtUntil(next.game.start_time, now)}</span>
            )}
          </span>
        )}
        <div className="ml-auto flex items-center gap-2">
          {demo && <Badge tone="gradient">DEMO</Badge>}
          {apiUp === false && !demo && <Badge tone="danger">API DOWN</Badge>}
          <span className="hidden font-mono text-[10px] text-krypt-muted/60 sm:inline">
            {loading ? 'FETCHING…' : fetchedAt ? `AS OF ${fetchedAt.toISOString().slice(11, 19)}Z` : 'NO DATA'}
          </span>
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

      <main className="min-h-0 flex-1 overflow-y-auto px-4 pb-3 pt-3">
        {apiUp === false && !demo && (
          <div className="mb-3 rounded-xl border border-amber-400/25 bg-amber-500/[0.07] px-3 py-2 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
            {simple
              ? 'Data feed offline — turn on demo mode to look around.'
              : 'API offline — live feed unavailable. Enable demo mode to review the terminal.'}
          </div>
        )}

        {todays.length === 0 ? (
          <Empty
            title="Nothing on today's slate"
            message="No games across the nine boards start today — off-season feeds often carry only far-future or finished games. Check BOARDS for upcoming slates, or flip on demo mode."
          />
        ) : (
          <>
            {/* In-progress games pinned at the very top. */}
            {live.length > 0 && (
              <section className="mb-5">
                <div className="mb-2 flex items-center gap-2">
                  <span className="relative flex h-2 w-2 shrink-0">
                    <span className="absolute inline-flex h-full w-full animate-ping rounded-full bg-scout-red opacity-60" />
                    <span className="relative inline-flex h-2 w-2 rounded-full bg-scout-red-bright" />
                  </span>
                  <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-white/80">
                    Live now
                  </span>
                  <span className="text-[10px] uppercase tracking-wider text-krypt-muted/70">
                    {live.length} in progress
                  </span>
                </div>
                <div className={cls(grid, 'animate-fade-in')}>
                  {live.map((card) => (
                    <GameCardTile
                      key={card.game.id}
                      card={card}
                      live
                      crossSport
                      onOpen={() => setOpenId(card.game.id)}
                    />
                  ))}
                </div>
              </section>
            )}

            {/* Today's remaining slate, soonest first. */}
            {upcoming.length > 0 && (
              <section>
                <Eyebrow
                  sub={`${upcoming.length} upcoming · ${sportsOn} ${sportsOn === 1 ? 'sport' : 'sports'} · soonest first`}
                >
                  Today across all sports
                </Eyebrow>
                <div className={cls(grid, 'animate-fade-in')}>
                  {upcoming.map((card) => (
                    <GameCardTile
                      key={card.game.id}
                      card={card}
                      crossSport
                      onOpen={() => setOpenId(card.game.id)}
                    />
                  ))}
                </div>
              </section>
            )}

            {live.length === 0 && upcoming.length === 0 && (
              <Empty
                title="Today's slate is done"
                message="Every game on today's boards has finished — the finals are below."
              />
            )}

            {/* Finished games sink to a collapsed FINAL shelf. */}
            {finals.length > 0 && (
              <section className={cls((live.length > 0 || upcoming.length > 0) && 'mt-6')}>
                <button
                  onClick={() => setShowFinals((v) => !v)}
                  className="flex w-full items-center gap-2 text-left"
                  aria-expanded={showFinals}
                >
                  {showFinals ? (
                    <ChevronDown size={12} className="shrink-0 text-krypt-muted" />
                  ) : (
                    <ChevronRight size={12} className="shrink-0 text-krypt-muted" />
                  )}
                  <span className="text-[10px] font-semibold uppercase tracking-[0.2em] text-krypt-muted transition hover:text-white">
                    Final · {finals.length}
                  </span>
                  <span className="text-[9px] uppercase tracking-wider text-krypt-muted/50">
                    finished earlier today
                  </span>
                  <span className="h-px min-w-4 flex-1 bg-white/10" />
                </button>
                {showFinals && (
                  <div className="mt-2 space-y-1 animate-fade-in">
                    {finals.map((card) => {
                      const { game } = card
                      const hasScore = game.home_score !== null && game.away_score !== null
                      return (
                        <div
                          key={game.id}
                          role="button"
                          tabIndex={0}
                          onClick={() => setOpenId(game.id)}
                          onKeyDown={(e) => {
                            if (e.key === 'Enter' || e.key === ' ') {
                              e.preventDefault()
                              setOpenId(game.id)
                            }
                          }}
                          className="flex cursor-pointer items-center gap-3 rounded-xl border border-white/5 bg-white/[0.02] px-3 py-2 opacity-70 transition hover:border-white/15 hover:opacity-100"
                        >
                          <span className="w-11 shrink-0 rounded border border-white/10 bg-white/5 px-1.5 py-0.5 text-center text-[9px] font-bold uppercase tracking-widest text-krypt-muted">
                            {SPORT_TAG[game.sport]}
                          </span>
                          <span className="min-w-0 flex-1 truncate text-xs font-semibold text-white/80">
                            {matchLabel(card)}
                          </span>
                          <span className="shrink-0 font-mono text-xs font-semibold text-white/85">
                            {hasScore ? `${game.away_score}–${game.home_score}` : '—'}
                          </span>
                          <Badge tone="neutral">FINAL</Badge>
                        </div>
                      )
                    })}
                  </div>
                )}
              </section>
            )}
          </>
        )}
      </main>

      {/* Legend footer — matches BOARDS. */}
      <footer className="shrink-0 border-t border-white/5 bg-krypt-panel/40 px-4 py-1.5">
        <span className="text-[9px] uppercase tracking-wider text-krypt-muted/50">
          {simple
            ? 'Everything starting today, all sports in one place. Click any game for details.'
            : 'ALL SPORTS · LOCAL DATE = TODAY · LIVE pinned first, then soonest kickoff · finals collapse below · auto-poll 60s'}
        </span>
      </footer>

      <DetailDrawer card={openCard} demo={demo} onClose={() => setOpenId(null)} />
    </div>
  )
}
