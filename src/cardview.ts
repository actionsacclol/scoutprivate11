/**
 * Shared presentation helpers for game cards — side ordering, kickoff
 * formatting, side naming. Pure view logic on top of types.ts / oddsmath.ts.
 */
import type { Edge, GameCard } from './types'

export interface SideView {
  key: 'home' | 'away' | 'draw'
  /** Full display name (team / player / DRAW). */
  name: string
  /** Two-char terminal tag: A / H / D — P1 / P2 for tennis. */
  tag: string
  prob: number
  mlKey: 'home_ml' | 'away_ml' | 'draw_ml'
}

/** Display order for a card's sides: AWAY @ HOME (tennis: P1 vs P2).
 *  Probabilities come from the DISPLAYED forecast (card.forecast: the
 *  market where one exists, else the model) and fall back to the raw model
 *  prediction on older payloads and demo data. */
export function sideViews(card: GameCard): SideView[] {
  const { game, prediction, forecast } = card
  const home = forecast ? forecast.home : prediction?.home_win_prob
  const away = forecast ? forecast.away : prediction?.away_win_prob
  const draw = forecast ? forecast.draw : prediction?.draw_prob
  if (home === undefined || away === undefined) return []
  if (game.sport === 'tennis') {
    return [
      { key: 'home', name: game.home, tag: 'P1', prob: home, mlKey: 'home_ml' },
      { key: 'away', name: game.away, tag: 'P2', prob: away, mlKey: 'away_ml' },
    ]
  }
  const views: SideView[] = [{ key: 'away', name: game.away, tag: 'A', prob: away, mlKey: 'away_ml' }]
  if (draw !== null && draw !== undefined) {
    views.push({ key: 'draw', name: 'DRAW', tag: 'D', prob: draw, mlKey: 'draw_ml' })
  }
  views.push({ key: 'home', name: game.home, tag: 'H', prob: home, mlKey: 'home_ml' })
  return views
}

/** The side with the highest model probability (what emerald highlights). */
export function favoredKey(card: GameCard): SideView['key'] | null {
  const sides = sideViews(card)
  if (sides.length === 0) return null
  return sides.reduce((a, b) => (b.prob > a.prob ? b : a)).key
}

/** Chronological comparator — soonest kickoff first. */
export function byStartAsc(a: GameCard, b: GameCard): number {
  return new Date(a.game.start_time).getTime() - new Date(b.game.start_time).getTime()
}

/** Midnight (local) of the given moment — the calendar-day boundary. */
export function startOfLocalDay(t: Date = new Date()): Date {
  const d = new Date(t)
  d.setHours(0, 0, 0, 0)
  return d
}

/** Whole local-calendar-day offset from today: -1 = yesterday, 0 = today, 1 = tomorrow. */
export function daysFromToday(iso: string, now: Date = new Date()): number {
  const today = startOfLocalDay(now).getTime()
  const target = startOfLocalDay(new Date(iso)).getTime()
  return Math.round((target - today) / 86_400_000)
}

/** "SEP 4" — prominent short date for far-future slates. */
export function fmtShortDate(iso: string): string {
  return new Date(iso)
    .toLocaleDateString('en-US', { month: 'short', day: 'numeric' })
    .toUpperCase()
}

export function fmtKick(iso: string): { time: string; date: string } {
  const d = new Date(iso)
  return {
    time: d.toLocaleTimeString([], { hour: '2-digit', minute: '2-digit', hour12: false }),
    date: d.toLocaleDateString([], { month: '2-digit', day: '2-digit' }),
  }
}

export function fmtDT(iso: string): string {
  const d = new Date(iso)
  return `${d.toLocaleDateString([], { month: '2-digit', day: '2-digit' })} ${d.toLocaleTimeString([], {
    hour: '2-digit',
    minute: '2-digit',
    hour12: false,
  })}`
}

/** Human name of an edge's side ("BOS", "DRAW", "OVER"…). */
export function edgeSideName(edge: Edge, card: GameCard): string {
  if (edge.side === 'home') return card.game.home
  if (edge.side === 'away') return card.game.away
  return edge.side.toUpperCase()
}

export function matchLabel(card: GameCard): string {
  const { game } = card
  return game.sport === 'tennis' ? `${game.home} vs ${game.away}` : `${game.away} @ ${game.home}`
}
