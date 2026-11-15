import { useEffect, useMemo, useRef, useState } from 'react'
import { FlaskConical, RefreshCw } from 'lucide-react'
import type { BoardMap, GameCard, Sport } from '../types'
import { SPORTS, SPORT_NAME } from '../types'
import SportPills from './SportPills'
import GameCardTile from './GameCardTile'
import DetailDrawer from './DetailDrawer'
import { Badge, Empty, Eyebrow, StripBtn } from './common'
import { byStartAsc, daysFromToday } from '../cardview'
import { cls } from '../utils/format'
import { useUiMode } from '../uiMode'

function UtcClock() {
  const [now, setNow] = useState(() => new Date())
  useEffect(() => {
    const t = setInterval(() => setNow(new Date()), 1000)
    return () => clearInterval(t)
  }, [])
  return (
    <span className="font-mono text-[10px] tabular-nums text-krypt-muted">
      {now.toISOString().slice(11, 19)}
      <span className="text-krypt-muted/50"> UTC</span>
    </span>
  )
}

const LEGEND =
  'FAIR → BEST = model fair line vs top book price · TOT = predicted total vs consensus line · EV = edge vs vig-free market · K = full Kelly · auto-poll 60s'

interface Props {
  boards: BoardMap
  active: Sport
  demo: boolean
  apiUp: boolean | null
  fetchedAt: Date | null
  loading: boolean
  /** Sports whose last board request failed, with the reason. */
  boardErrors: Partial<Record<Sport, string>>
  highlightId: string | null
  onSport: (s: Sport) => void
  onRefresh: () => void
  onToggleDemo: () => void
  onClearHighlight: () => void
}

/** BOARDS — the flagship page: sport strip + responsive game-card grid +
 *  right-side detail drawer. */
export default function BoardsPage({
  boards,
  active,
  demo,
  apiUp,
  fetchedAt,
  loading,
  boardErrors,
  highlightId,
  onSport,
  onRefresh,
  onToggleDemo,
  onClearHighlight,
}: Props) {
  const simple = useUiMode() === 'simple'
  const [openId, setOpenId] = useState<string | null>(null)
  const highlightRef = useRef<HTMLDivElement | null>(null)

  const counts = useMemo(() => {
    const c: Partial<Record<Sport, number>> = {}
    for (const s of SPORTS) c[s] = boards[s]?.length ?? 0
    return c
  }, [boards])

  // Soonest-first, with past-completed games (off-season feeds return the
  // nearest slate, which can include finished games) sunk below a divider.
  const { cards, upcoming, stale } = useMemo(() => {
    const all = [...(boards[active] ?? [])].sort(byStartAsc)
    const stale = all.filter((c) => c.game.completed && daysFromToday(c.game.start_time) < 0)
    const upcoming = all.filter((c) => !stale.includes(c))
    return { cards: all, upcoming, stale }
  }, [boards, active])

  // Jump-from-VALUE highlighting: scroll the card into view, flash the ring,
  // then release the highlight so re-navigation retriggers cleanly.
  useEffect(() => {
    if (!highlightId) return
    const t1 = setTimeout(() => {
      highlightRef.current?.scrollIntoView({ behavior: 'smooth', block: 'center' })
    }, 60)
    const t2 = setTimeout(onClearHighlight, 3000)
    return () => {
      clearTimeout(t1)
      clearTimeout(t2)
    }
  }, [highlightId, onClearHighlight])

  const loadError = boardErrors[active] ?? null

  const openCard: GameCard | null = openId
    ? (cards.find((c) => c.game.id === openId) ?? null)
    : null

  return (
    <div className="flex h-full min-h-0 flex-col">
      {/* Sport strip under the TitleBar */}
      <div className="flex shrink-0 flex-wrap items-center gap-x-3 gap-y-1.5 border-b border-white/5 bg-krypt-panel/40 px-4 py-2">
        <SportPills active={active} counts={counts} onSelect={onSport} />
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
          <UtcClock />
        </div>
      </div>

      {/* Board grid */}
      <main className="min-h-0 flex-1 overflow-y-auto px-4 pb-3 pt-3">
        <Eyebrow
          sub={`${cards.length} games · ${demo ? 'bundled demo slate' : 'live /api/board'}`}
        >
          {SPORT_NAME[active]} board
        </Eyebrow>

        {apiUp === false && !demo && (
          <div className="mb-3 rounded-xl border border-amber-400/25 bg-amber-500/[0.07] px-3 py-2 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
            API offline — live feed unavailable. Enable demo mode to review the terminal.
          </div>
        )}

        {/* A board that failed to load is NOT an empty slate, and must not be
            dressed as one: "no games scheduled — likely off-season" over a
            full NBA night is how a fetch failure read for months. */}
        {loadError && cards.length > 0 && (
          <div className="mb-3 flex flex-wrap items-center gap-x-3 gap-y-1 rounded-xl border border-amber-400/25 bg-amber-500/[0.07] px-3 py-2 text-[10px] font-semibold uppercase tracking-wider text-amber-300">
            <span>
              Couldn’t refresh {SPORT_NAME[active]} — {loadError}. Showing the last slate that loaded.
            </span>
            <button
              onClick={onRefresh}
              disabled={loading}
              className="rounded-md border border-amber-400/30 px-2 py-0.5 text-[9px] tracking-widest transition hover:bg-amber-400/10 disabled:opacity-40"
            >
              RETRY
            </button>
          </div>
        )}

        {cards.length === 0 && loadError ? (
          <Empty
            title={`Couldn’t load the ${SPORT_NAME[active]} board`}
            message={`The request failed: ${loadError}. This is a loading problem, not an empty slate — the board retries on its own every minute, or press REFRESH.`}
          />
        ) : cards.length === 0 ? (
          <Empty
            title={`No ${SPORT_NAME[active]} games scheduled`}
            message="The board populates when the slate resumes — likely off-season. Flip on demo mode to explore the terminal with a bundled slate."
          />
        ) : (
          <>
            {upcoming.length > 0 && (
              <div
                className={cls(
                  'grid animate-fade-in',
                  simple
                    ? 'gap-4 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]'
                    : 'gap-3 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]',
                )}
              >
                {upcoming.map((card) => (
                  <GameCardTile
                    key={card.game.id}
                    ref={card.game.id === highlightId ? highlightRef : undefined}
                    card={card}
                    highlighted={card.game.id === highlightId}
                    onOpen={() => setOpenId(card.game.id)}
                  />
                ))}
              </div>
            )}

            {/* Past-completed games the upstream feed still returns (off-season
                "nearest slate") — sunk, dimmed, and explicitly labelled. */}
            {stale.length > 0 && (
              <>
                <div className={cls('mb-2 flex items-center gap-2', upcoming.length > 0 && 'mt-6')}>
                  <span className="text-[9px] font-semibold uppercase tracking-[0.2em] text-krypt-muted/60">
                    Completed · stale feed
                  </span>
                  <span className="truncate text-[9px] uppercase tracking-wider text-krypt-muted/40">
                    {stale.length} finished — off-season feed returns the nearest slate
                  </span>
                  <span className="h-px min-w-4 flex-1 bg-white/10" />
                </div>
                <div
                  className={cls(
                    'grid animate-fade-in',
                    simple
                      ? 'gap-4 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]'
                      : 'gap-3 [grid-template-columns:repeat(auto-fill,minmax(300px,1fr))]',
                  )}
                >
                  {stale.map((card) => (
                    <div key={card.game.id} className="opacity-50 saturate-[.65] transition hover:opacity-80">
                      <GameCardTile
                        ref={card.game.id === highlightId ? highlightRef : undefined}
                        card={card}
                        highlighted={card.game.id === highlightId}
                        onOpen={() => setOpenId(card.game.id)}
                      />
                    </div>
                  ))}
                </div>
              </>
            )}
          </>
        )}
      </main>

      {/* Legend footer */}
      <footer className="shrink-0 border-t border-white/5 bg-krypt-panel/40 px-4 py-1.5">
        <span className="text-[9px] uppercase tracking-wider text-krypt-muted/50">
          {simple
            ? 'Green bar = who our model thinks wins. Click any game for details. Predictions, not guarantees.'
            : LEGEND}
        </span>
      </footer>

      <DetailDrawer card={openCard} demo={demo} onClose={() => setOpenId(null)} />
    </div>
  )
}
