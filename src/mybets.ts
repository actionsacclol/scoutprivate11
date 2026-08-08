/**
 * useMyBets — the USER'S bet tracker data layer (TRACK buttons + TRACKER page).
 *
 * One hook, two backends:
 *   · live mode  — the /api/mybets endpoints (see api.ts); optimistic
 *     updates followed by an authoritative refetch, last-good report kept on
 *     transient failures so the page never blanks mid-session.
 *   · demo mode  — a localStorage-backed store ('scout.demo-mybets')
 *     implementing the same shapes fully client-side. Grading is simulated:
 *     a demo bet on a completed demo game grades INSTANTLY with the same
 *     payout math the backend settle job uses — W pays +stake × payout(price),
 *     L costs -stake, and a tied final pushes (P, 0u) on two-way markets while
 *     losing on soccer three-ways (the DRAW side wins those). Bets whose
 *     game_id isn't on the demo boards keep their stored grade — that's how
 *     the seeded sample log (first demo visit, ~10 bets, +2.3u) works.
 *
 * Parlays (built on the PARLAY page) track alongside straight bets: live mode
 * posts to /api/mybets/parlay, demo mode keeps a second localStorage key
 * ('scout.demo-myparlays') and grades slips leg-by-leg with the backend's
 * rules (lost leg → slip loses, pushed legs drop out of the payout,
 * all-pushed slips push). A parlay counts as ONE bet in the P&L.
 *
 * All consumers share one module-level store, so a TRACK click on PICKS flips
 * the tracked state everywhere (drawer, tracker page, dashboard) instantly.
 */
import { useCallback, useEffect, useSyncExternalStore } from 'react'
import type { GameCard } from './types'
import type {
  MyBetRow,
  MyBetsPnl,
  MyBetsReport,
  MyParlayLeg,
  MyParlayRow,
  TrackBetInput,
  TrackParlayInput,
  TrackParlayLegInput,
} from './api'
import { deleteMyBet, deleteMyParlay, fetchMyBets, postMyBet, postMyParlay } from './api'
import { payout } from './oddsmath'
import { americanFromDecimal, decimalFromAmerican } from './parlay'
import { DEMO_BOARDS } from './demo'

export type {
  MyBetRow,
  MyBetsPnl,
  MyBetsReport,
  MyParlayLeg,
  MyParlayRow,
  TrackBetInput,
  TrackParlayInput,
} from './api'

const DEMO_KEY = 'scout.demo-mybets'
const DEMO_PARLAY_KEY = 'scout.demo-myparlays'
const LIVE_REFRESH_MS = 60_000

const round2 = (v: number) => +v.toFixed(2)

// ------------------------------------------------------------ shared helpers

/** Resolve a side to its display name on a game. */
function sideTeam(side: string, home: string, away: string, line?: number | null): string {
  if (side === 'home') return home
  if (side === 'away') return away
  if (side === 'over' || side === 'under') {
    return `${side.toUpperCase()}${line != null ? ` ${line}` : ''}`
  }
  return 'DRAW'
}

/** Build a full bet row from its game card — used for optimistic live rows
 *  and as the write path of the demo store. */
function rowFromCard(input: TrackBetInput, card: GameCard, id: number): MyBetRow {
  const g = card.game
  return {
    id,
    game_id: g.id,
    sport: g.sport,
    side: input.side,
    team: sideTeam(input.side, g.home, g.away, input.line),
    price: input.price,
    book: input.book ?? null,
    stake_units: input.stake_units ?? 1,
    note: input.note ?? null,
    created_at: new Date().toISOString(),
    result: null,
    units: null,
    home: g.home,
    away: g.away,
    league: g.league,
    kickoff: g.start_time,
    completed: g.completed,
  }
}

/** The date a parlay's units land on: its LAST leg's kickoff date — the
 *  slip can't grade before that game. */
function parlayDate(p: MyParlayRow): string {
  let latest = p.created_at.slice(0, 10)
  for (const leg of p.legs) {
    const d = leg.kickoff.slice(0, 10)
    if (d > latest) latest = d
  }
  return latest
}

/** Aggregate P&L from rows — the same shape the backend serves; a parlay
 *  counts as ONE bet at its own stake, landing on its last leg's date. Demo
 *  mode's authoritative pnl; live mode only uses it for the optimistic beat
 *  between a mutation and its refetch. */
export function computePnl(rows: MyBetRow[], parlays: MyParlayRow[] = []): MyBetsPnl {
  const graded = rows.filter((r) => r.result !== null)
  const gradedParlays = parlays.filter((p) => p.result !== null)
  let units = 0
  let staked = 0
  let w = 0
  let l = 0
  let p = 0
  for (const x of [...graded, ...gradedParlays]) {
    units += x.units ?? 0
    staked += x.stake_units
    if (x.result === 'W') w++
    else if (x.result === 'L') l++
    else p++
  }
  // Daily equity grouped by settle date (kickoff date of the graded game;
  // last-leg kickoff for parlays).
  const byDate = new Map<string, number>()
  for (const r of graded) {
    const date = (r.kickoff || r.created_at).slice(0, 10)
    byDate.set(date, (byDate.get(date) ?? 0) + (r.units ?? 0))
  }
  for (const pr of gradedParlays) {
    const date = parlayDate(pr)
    byDate.set(date, (byDate.get(date) ?? 0) + (pr.units ?? 0))
  }
  const dates = [...byDate.keys()].sort()
  let cum = 0
  const daily = dates.map((date) => {
    const day = round2(byDate.get(date) ?? 0)
    cum = round2(cum + day)
    return { date, units: day, cum_units: cum }
  })
  return {
    n: rows.length + parlays.length,
    graded: graded.length + gradedParlays.length,
    units: round2(units),
    roi_pct: staked > 0 ? round2((units / staked) * 100) : 0,
    record: `${w}-${l}-${p}`,
    daily,
  }
}

// ------------------------------------------------------------- demo grading

let demoCardIndex: Map<string, GameCard> | null = null
function demoCard(gameId: string): GameCard | undefined {
  if (!demoCardIndex) {
    demoCardIndex = new Map()
    for (const cards of Object.values(DEMO_BOARDS)) {
      for (const c of cards ?? []) demoCardIndex.set(c.game.id, c)
    }
  }
  return demoCardIndex.get(gameId)
}

/** W/L/P for one side of a completed demo game (null while it's live):
 *  the same outcome rules the backend settle job applies. */
function demoSideResult(g: GameCard['game'], side: string): MyBetRow['result'] {
  if (!g.completed || g.home_score === null || g.away_score === null) return null
  const winner =
    g.home_score > g.away_score ? 'home' : g.away_score > g.home_score ? 'away' : 'draw'
  if (side === winner) return 'W'
  if (winner === 'draw' && g.sport !== 'soccer') return 'P' // two-way tie: push
  return 'L' // includes home/away sides losing a soccer draw
}

/** Re-join a stored demo bet against the live demo boards: refresh game
 *  metadata and grade instantly when the game has completed. Bets on games
 *  not on the boards (the settled seeds) keep their stored grade. */
function regradeDemo(row: MyBetRow): MyBetRow {
  const card = demoCard(row.game_id)
  if (!card) return row
  const g = card.game
  const result = demoSideResult(g, row.side)
  return {
    ...row,
    sport: g.sport,
    team: sideTeam(row.side, g.home, g.away),
    home: g.home,
    away: g.away,
    league: g.league,
    kickoff: g.start_time,
    completed: g.completed,
    result,
    units:
      result === 'W'
        ? round2(row.stake_units * payout(row.price))
        : result === 'L'
          ? round2(-row.stake_units)
          : result === 'P'
            ? 0
            : null,
  }
}

/** Re-join a stored demo parlay against the demo boards: each leg grades
 *  like a straight bet; the slip finalizes once every leg has a result
 *  (lost leg → slip loses; pushed legs drop out of the payout; all pushed
 *  → push). Legs whose games left the boards keep their stored grade. */
function regradeDemoParlay(row: MyParlayRow): MyParlayRow {
  const legs = row.legs.map((leg): MyParlayLeg => {
    const card = demoCard(leg.game_id)
    if (!card) return leg
    const g = card.game
    return {
      ...leg,
      sport: g.sport,
      team: sideTeam(leg.side, g.home, g.away),
      home: g.home,
      away: g.away,
      league: g.league,
      kickoff: g.start_time,
      completed: g.completed,
      home_score: g.home_score,
      away_score: g.away_score,
      result: demoSideResult(g, leg.side),
    }
  })
  const next: MyParlayRow = { ...row, legs, result: null, units: null }
  if (legs.length > 0 && legs.every((l) => l.result !== null)) {
    if (legs.some((l) => l.result === 'L')) {
      next.result = 'L'
      next.units = round2(-row.stake_units)
    } else if (legs.every((l) => l.result === 'P')) {
      next.result = 'P'
      next.units = 0
    } else {
      let returned = 1
      for (const l of legs) if (l.result === 'W') returned *= decimalFromAmerican(l.price)
      next.result = 'W'
      next.units = round2(row.stake_units * (returned - 1))
    }
  }
  return next
}

// ---------------------------------------------------------------- demo store

/** Seeded sample log — ~10 plausible bets, mixed results, nets exactly +2.3u
 *  (5-3-1 graded + one pending on today's demo slate). Written on the first
 *  demo visit only; the user's own demo bets persist alongside afterwards. */
function buildSeeds(): MyBetRow[] {
  const day = 86_400_000
  const now = Date.now()
  const seed = (
    n: number,
    daysAgo: number,
    sport: string,
    away: string,
    home: string,
    side: string,
    price: number,
    book: string | null,
    result: 'W' | 'L' | 'P',
    note: string | null = null,
  ): MyBetRow => {
    const kick = new Date(now - daysAgo * day)
    kick.setHours(19, 10, 0, 0)
    const units = result === 'W' ? round2(payout(price)) : result === 'L' ? -1 : 0
    return {
      id: n,
      game_id: `demo-settled-${n}`,
      sport,
      side,
      team: sideTeam(side, home, away),
      price,
      book,
      stake_units: 1,
      note,
      created_at: new Date(kick.getTime() - 4 * 3_600_000).toISOString(),
      result,
      units,
      home,
      away,
      league: '',
      kickoff: kick.toISOString(),
      completed: true,
    }
  }
  const rows: MyBetRow[] = [
    seed(1, 7, 'nhl', 'TOR', 'BOS', 'away', 150, 'DraftKings', 'W'), // +1.50
    seed(2, 6, 'nba', 'MIA', 'NYK', 'home', -120, 'FanDuel', 'W'), // +0.83
    seed(3, 6, 'mlb', 'ATL', 'PHI', 'home', -110, 'BetMGM', 'L'), // -1.00
    seed(4, 5, 'ncaab', 'Duke', 'North Carolina', 'away', 110, 'DraftKings', 'W', 'road dog spot'), // +1.10
    seed(5, 4, 'soccer', 'Arsenal', 'Man City', 'away', 135, 'Bet365', 'L'), // -1.00
    seed(6, 3, 'tennis', 'Zverev', 'Djokovic', 'home', -150, 'Pinnacle', 'W'), // +0.67
    seed(7, 3, 'nfl', 'BAL', 'CIN', 'home', -105, 'DraftKings', 'L'), // -1.00
    seed(8, 2, 'nfl', 'GB', 'DET', 'home', 100, 'FanDuel', 'P'), // 0 — OT tie
    seed(9, 1, 'wnba', 'NYL', 'LVA', 'home', 120, 'BetMGM', 'W'), // +1.20 → +2.30 total
  ]
  // One pending bet on today's demo slate — demos the PENDING chip and the
  // TRACKED state on its pick card.
  const card = demoCard('demo-nba-1')
  if (card) {
    let best: { book: string; price: number } | null = null
    for (const o of card.odds) {
      if (o.home_ml !== null && (best === null || payout(o.home_ml) > payout(best.price))) {
        best = { book: o.book, price: o.home_ml }
      }
    }
    if (best) {
      rows.push(
        rowFromCard(
          { game_id: card.game.id, side: 'home', price: best.price, book: best.book },
          card,
          10,
        ),
      )
    }
  }
  return rows
}

function readDemoStore(): MyBetRow[] {
  try {
    const raw = localStorage.getItem(DEMO_KEY)
    if (raw === null) {
      const seeds = buildSeeds()
      localStorage.setItem(DEMO_KEY, JSON.stringify(seeds))
      return seeds
    }
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as MyBetRow[]) : []
  } catch {
    return [] // storage unavailable — tracker still renders, just empty
  }
}

function writeDemoStore(rows: MyBetRow[]): void {
  try {
    localStorage.setItem(DEMO_KEY, JSON.stringify(rows))
  } catch {
    /* storage unavailable — the in-memory report still updates this session */
  }
}

// Parlays live in their own key so existing demo bet logs upgrade in place.
function readDemoParlays(): MyParlayRow[] {
  try {
    const raw = localStorage.getItem(DEMO_PARLAY_KEY)
    if (raw === null) return []
    const parsed: unknown = JSON.parse(raw)
    return Array.isArray(parsed) ? (parsed as MyParlayRow[]) : []
  } catch {
    return []
  }
}

function writeDemoParlays(rows: MyParlayRow[]): void {
  try {
    localStorage.setItem(DEMO_PARLAY_KEY, JSON.stringify(rows))
  } catch {
    /* storage unavailable — the in-memory report still updates this session */
  }
}

/** Build one leg of a tracked parlay from its game card. */
function legFromCard(input: TrackParlayLegInput, card: GameCard): MyParlayLeg {
  const g = card.game
  return {
    game_id: g.id,
    sport: g.sport,
    side: input.side,
    team: sideTeam(input.side, g.home, g.away),  // parlay legs are moneyline-only
    price: input.price,
    book: input.book ?? null,
    result: null,
    home: g.home,
    away: g.away,
    league: g.league,
    kickoff: g.start_time,
    completed: g.completed,
    home_score: g.home_score,
    away_score: g.away_score,
  }
}

/** Build a full parlay row from the slip + cards — the demo write path and
 *  live mode's optimistic row. Null when any leg's card is missing. */
function parlayFromCards(
  input: TrackParlayInput,
  cardsById: Map<string, GameCard>,
  id: number,
): MyParlayRow | null {
  const legs: MyParlayLeg[] = []
  let decimal = 1
  for (const legInput of input.legs) {
    const card = cardsById.get(legInput.game_id)
    if (!card) return null
    legs.push(legFromCard(legInput, card))
    decimal *= decimalFromAmerican(legInput.price)
  }
  return {
    id,
    stake_units: input.stake_units ?? 1,
    price: americanFromDecimal(decimal),
    note: input.note ?? null,
    created_at: new Date().toISOString(),
    result: null,
    units: null,
    graded_at: null,
    legs,
  }
}

/** Order-independent identity of a leg set (double-submit detection —
 *  mirrors the backend's _parlay_key). */
function parlayKey(legs: Array<{ game_id: string; side: string; price: number; book?: string | null }>): string {
  return legs
    .map((l) => `${l.game_id}:${l.side}:${l.price}:${l.book ?? ''}`)
    .sort()
    .join('|')
}

function computeDemoReport(): MyBetsReport {
  const bets = readDemoStore().map(regradeDemo)
  const parlays = readDemoParlays().map(regradeDemoParlay)
  return { bets, parlays, pnl: computePnl(bets, parlays) }
}

// ------------------------------------------------------- shared module store

type BetMode = 'demo' | 'live'

interface ModeState {
  report: MyBetsReport | null
  loading: boolean
  /** A mutation is in flight (track/remove). */
  busy: boolean
  loadedOnce: boolean
}

const initial: ModeState = { report: null, loading: false, busy: false, loadedOnce: false }
const states: Record<BetMode, ModeState> = { demo: { ...initial }, live: { ...initial } }
const listeners = new Set<() => void>()

function setState(mode: BetMode, patch: Partial<ModeState>): void {
  states[mode] = { ...states[mode], ...patch }
  for (const l of listeners) l()
}

function subscribe(fn: () => void): () => void {
  listeners.add(fn)
  return () => listeners.delete(fn)
}

function refreshDemo(): void {
  setState('demo', { report: computeDemoReport(), loading: false, loadedOnce: true })
}

let liveInFlight = false
async function refreshLive(): Promise<void> {
  if (liveInFlight) return
  liveInFlight = true
  if (!states.live.loadedOnce) setState('live', { loading: true })
  try {
    const r = await fetchMyBets()
    // Keep the last good report on transient failures mid-session.
    setState('live', { loading: false, loadedOnce: true, report: r ?? states.live.report })
  } finally {
    liveInFlight = false
  }
}

/** Add/replace a row in a (possibly null) report, recomputing pnl locally. */
function withRow(report: MyBetsReport | null, row: MyBetRow): MyBetsReport {
  const bets = [...(report?.bets ?? []).filter((b) => b.id !== row.id), row]
  const parlays = report?.parlays ?? []
  return { bets, parlays, pnl: computePnl(bets, parlays) }
}

function withoutRow(report: MyBetsReport | null, id: number): MyBetsReport {
  const bets = (report?.bets ?? []).filter((b) => b.id !== id)
  const parlays = report?.parlays ?? []
  return { bets, parlays, pnl: computePnl(bets, parlays) }
}

/** Add/replace a parlay in a (possibly null) report, recomputing pnl. */
function withParlay(report: MyBetsReport | null, row: MyParlayRow): MyBetsReport {
  const bets = report?.bets ?? []
  const parlays = [...(report?.parlays ?? []).filter((p) => p.id !== row.id), row]
  return { bets, parlays, pnl: computePnl(bets, parlays) }
}

function withoutParlay(report: MyBetsReport | null, id: number): MyBetsReport {
  const bets = report?.bets ?? []
  const parlays = (report?.parlays ?? []).filter((p) => p.id !== id)
  return { bets, parlays, pnl: computePnl(bets, parlays) }
}

// -------------------------------------------------------------- mutations

async function trackBet(mode: BetMode, input: TrackBetInput, card: GameCard): Promise<boolean> {
  const payload: TrackBetInput = { stake_units: 1, ...input }
  if (mode === 'demo') {
    const rows = readDemoStore()
    // Same duplicate semantics as the backend: one bet per game + side.
    if (!rows.some((r) => r.game_id === payload.game_id && r.side === payload.side)) {
      const id = rows.reduce((m, r) => Math.max(m, r.id), 0) + 1
      writeDemoStore([...rows, rowFromCard(payload, card, id)])
    }
    refreshDemo()
    return true
  }
  // Live: optimistic pending row (negative temp id), then POST + refetch.
  const temp = rowFromCard(payload, card, -Date.now())
  setState('live', { busy: true, report: withRow(states.live.report, temp) })
  const created = await postMyBet(payload)
  if (created === null) {
    // POST failed (backend down / preflight rejection) — roll the row back
    // and report failure so buttons can SAY so instead of silently reverting.
    setState('live', { busy: false, report: withoutRow(states.live.report, temp.id) })
    return false
  }
  await refreshLive()
  setState('live', { busy: false })
  return true
}

async function removeBet(mode: BetMode, id: number): Promise<void> {
  if (mode === 'demo') {
    const target = states.demo.report?.bets.find((b) => b.id === id)
    if (target && target.result === null) {
      // Mirrors the backend's 409: graded rows are permanent record.
      writeDemoStore(readDemoStore().filter((r) => r.id !== id))
    }
    refreshDemo()
    return
  }
  const prev = states.live.report
  setState('live', { busy: true, report: withoutRow(prev, id) })
  const ok = id < 0 ? false : await deleteMyBet(id) // temp rows can't be deleted server-side
  if (!ok) await refreshLive() // 409 / failure — refetch restores the row
  setState('live', { busy: false })
}

async function trackParlay(
  mode: BetMode,
  input: TrackParlayInput,
  cardsById: Map<string, GameCard>,
): Promise<boolean> {
  const payload: TrackParlayInput = { stake_units: 1, ...input }
  if (mode === 'demo') {
    const rows = readDemoParlays()
    // Same duplicate semantics as the backend: one slip per exact leg set.
    if (!rows.some((r) => r.result === null && parlayKey(r.legs) === parlayKey(payload.legs))) {
      const id = rows.reduce((m, r) => Math.max(m, r.id), 0) + 1
      const row = parlayFromCards(payload, cardsById, id)
      if (!row) return false
      writeDemoParlays([...rows, row])
    }
    refreshDemo()
    return true
  }
  // Live: optimistic pending slip (negative temp id), then POST + refetch.
  const temp = parlayFromCards(payload, cardsById, -Date.now())
  if (temp) setState('live', { busy: true, report: withParlay(states.live.report, temp) })
  else setState('live', { busy: true })
  const created = await postMyParlay(payload)
  if (created === null) {
    setState('live', {
      busy: false,
      report: temp ? withoutParlay(states.live.report, temp.id) : states.live.report,
    })
    return false
  }
  await refreshLive()
  setState('live', { busy: false })
  return true
}

async function removeParlay(mode: BetMode, id: number): Promise<void> {
  if (mode === 'demo') {
    const target = states.demo.report?.parlays?.find((p) => p.id === id)
    if (target && target.result === null) {
      // Mirrors the backend's 409: graded slips are permanent record.
      writeDemoParlays(readDemoParlays().filter((r) => r.id !== id))
    }
    refreshDemo()
    return
  }
  const prev = states.live.report
  setState('live', { busy: true, report: withoutParlay(prev, id) })
  const ok = id < 0 ? false : await deleteMyParlay(id)
  if (!ok) await refreshLive() // 409 / failure — refetch restores the slip
  setState('live', { busy: false })
}

// ------------------------------------------------------------------- hook

export interface UseMyBets {
  bets: MyBetRow[]
  parlays: MyParlayRow[]
  pnl: MyBetsPnl | null
  loading: boolean
  busy: boolean
  /** Track a bet (flat 1u unless stake_units given). The card supplies game
   *  metadata for the optimistic row / demo store. Resolves false when the
   *  POST failed (backend unreachable) so buttons can show the failure. */
  track: (input: TrackBetInput, card: GameCard) => Promise<boolean>
  /** Delete an ungraded bet. Graded rows are permanent (backend 409s). */
  remove: (id: number) => Promise<void>
  /** Track a parlay slip (flat 1u unless stake_units given). Cards supply
   *  game metadata per leg. Resolves false when the POST failed. */
  trackParlay: (input: TrackParlayInput, cardsById: Map<string, GameCard>) => Promise<boolean>
  /** Delete an ungraded parlay. Graded slips are permanent (backend 409s). */
  removeParlay: (id: number) => Promise<void>
  /** Is this game+side already tracked? Drives the TRACKED button state. */
  isTracked: (gameId: string, side: string) => boolean
  /** Is an ungraded parlay with this exact game+side leg set tracked?
   *  (Prices move — identity here is the legs, not the price.) */
  isParlayTracked: (legs: Array<{ gameId: string; side: string }>) => boolean
  refresh: () => void
}

let liveConsumers = 0
let liveTimer: number | null = null

export function useMyBets(demo: boolean): UseMyBets {
  const mode: BetMode = demo ? 'demo' : 'live'
  const state = useSyncExternalStore(subscribe, () => states[mode])

  useEffect(() => {
    if (mode === 'demo') {
      refreshDemo() // synchronous — localStorage + instant regrade
      return
    }
    void refreshLive()
    liveConsumers++
    if (liveTimer === null) {
      liveTimer = window.setInterval(() => void refreshLive(), LIVE_REFRESH_MS)
    }
    return () => {
      liveConsumers--
      if (liveConsumers <= 0 && liveTimer !== null) {
        window.clearInterval(liveTimer)
        liveTimer = null
      }
    }
  }, [mode])

  const bets = state.report?.bets ?? []
  const parlays = state.report?.parlays ?? []
  const isTracked = useCallback(
    (gameId: string, side: string) =>
      (states[mode].report?.bets ?? []).some((b) => b.game_id === gameId && b.side === side),
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mode, state.report],
  )
  const isParlayTracked = useCallback(
    (legs: Array<{ gameId: string; side: string }>) => {
      const key = legs
        .map((l) => `${l.gameId}:${l.side}`)
        .sort()
        .join('|')
      return (states[mode].report?.parlays ?? []).some(
        (p) =>
          p.result === null &&
          p.legs
            .map((l) => `${l.game_id}:${l.side}`)
            .sort()
            .join('|') === key,
      )
    },
    // eslint-disable-next-line react-hooks/exhaustive-deps
    [mode, state.report],
  )

  return {
    bets,
    parlays,
    pnl: state.report?.pnl ?? null,
    loading: state.loading && !state.loadedOnce,
    busy: state.busy,
    track: useCallback((input, card) => trackBet(mode, input, card), [mode]),
    remove: useCallback((id) => removeBet(mode, id), [mode]),
    trackParlay: useCallback((input, cardsById) => trackParlay(mode, input, cardsById), [mode]),
    removeParlay: useCallback((id) => removeParlay(mode, id), [mode]),
    isTracked,
    isParlayTracked,
    refresh: useCallback(() => {
      if (mode === 'demo') refreshDemo()
      else void refreshLive()
    }, [mode]),
  }
}
