import type {
  BoardMap,
  ClvReport,
  GameCard,
  InjuryReport,
  MovementReport,
  PerformanceReport,
  Sport,
  WeatherInfo,
} from './types'
import { SPORTS } from './types'
import { apiBase, invalidateApiBase } from './backendBase'

// Every request resolves the backend base URL first: http://127.0.0.1:<port>
// inside Electron (port picked by the supervisor), '' (relative, vite proxy)
// in plain browser dev. See src/backendBase.ts.
async function fetchJSON<T>(path: string, timeoutMs = 8000): Promise<T> {
  const base = await apiBase()
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), timeoutMs)
  try {
    const res = await fetch(base + path, { signal: ctl.signal })
    if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
    return (await res.json()) as T
  } finally {
    clearTimeout(timer)
  }
}

export async function checkHealth(): Promise<boolean> {
  try {
    const h = await fetchJSON<{ ok: boolean }>('/api/health', 4000)
    return h.ok === true
  } catch {
    // The backend may have restarted on a different port — re-resolve the
    // base on the next poll instead of pinning a dead one forever.
    invalidateApiBase()
    return false
  }
}

/** Fetch one sport's board (throws on failure — callers decide the fallback). */
export async function fetchBoard(sport: Sport): Promise<GameCard[]> {
  return fetchJSON<GameCard[]>(`/api/board/${sport}`)
}

/** Fetch every sport's board in parallel; a failed sport yields an empty board. */
export async function fetchAllBoards(): Promise<BoardMap> {
  const results = await Promise.all(
    SPORTS.map(async (sport: Sport): Promise<[Sport, GameCard[]]> => {
      try {
        return [sport, await fetchBoard(sport)]
      } catch {
        return [sport, []]
      }
    }),
  )
  const map: BoardMap = {}
  for (const [sport, cards] of results) map[sport] = cards
  return map
}

// ------------------------------------------------------------- intel feeds
// All of these swallow failures and return null — every panel that consumes
// them renders an explicit "NO … DATA" state instead of breaking the board.

export async function fetchInjuries(sport: Sport): Promise<InjuryReport[] | null> {
  try {
    return await fetchJSON<InjuryReport[]>(`/api/injuries/${sport}`)
  } catch {
    return null
  }
}

export async function fetchWeather(gameId: string): Promise<WeatherInfo | null> {
  try {
    // 200 with a null body when no weather applies (dome-less data, indoor sport…)
    return await fetchJSON<WeatherInfo | null>(`/api/weather/${encodeURIComponent(gameId)}`)
  } catch {
    return null
  }
}

export async function fetchMovement(gameId: string): Promise<MovementReport | null> {
  try {
    return await fetchJSON<MovementReport>(`/api/movement/${encodeURIComponent(gameId)}`)
  } catch {
    return null
  }
}

export async function fetchPerformance(): Promise<PerformanceReport | null> {
  try {
    return await fetchJSON<PerformanceReport>('/api/performance')
  } catch {
    return null
  }
}

export async function fetchClv(): Promise<ClvReport | null> {
  try {
    return await fetchJSON<ClvReport>('/api/clv')
  } catch {
    return null
  }
}

// ------------------------------------------------------------------ ROI feed
// GET /api/roi — flat-1u pick record graded by the settle job. Types live here
// (not types.ts) because the endpoint ships in parallel; every consumer must
// tolerate null (endpoint absent / backend down) and render a fallback.

/** One tier's graded record. `units` is absent for MODEL picks (never staked). */
export interface RoiTierStat {
  n: number
  units?: number | null
  /** Win rate as a 0..1 fraction (matches accuracy conventions elsewhere). */
  win_pct: number
}

export interface RoiOverall {
  n_picks: number
  n_graded: number
  /** Net units won at flat 1u stakes across graded value picks. */
  units: number
  /** units / n_graded × 100. */
  roi_pct: number
  by_tier: Partial<Record<'STRONG' | 'LEAN' | 'MODEL', RoiTierStat>>
}

export interface RoiSportRow {
  sport: string
  n: number
  units: number
  win_pct: number
}

export interface RoiDaily {
  date: string // YYYY-MM-DD
  units: number
  cum_units: number
}

export interface RoiSummary {
  overall: RoiOverall
  by_sport: RoiSportRow[]
  daily: RoiDaily[]
}

/** Fetch the flat-stakes pick record; null when the endpoint is missing/down. */
export async function fetchRoi(): Promise<RoiSummary | null> {
  try {
    return await fetchJSON<RoiSummary>('/api/roi')
  } catch {
    return null
  }
}

// ------------------------------------------------------------- backtest feed
// GET /api/backtest[/{sport}] — model-skill backtest over each engine's
// persisted walk-forward log. "log" sports (nba/wnba) carry a calibration
// curve + confidence sweep; "summary" sports carry aggregate metrics only;
// "none" sports persist nothing. Same null-tolerance as the other feeds.

/** One reliability bin: mean predicted prob vs realized win rate. */
export interface BacktestCalBin {
  lo: number
  hi: number
  n: number
  /** Mean model prob in the bin (null when empty). */
  predicted: number | null
  /** Realized win rate in the bin (null when empty). */
  actual: number | null
}

/** One "only count picks the model is ≥ threshold sure of" row. */
export interface BacktestSweepRow {
  /** Confidence floor, 0.50..0.85 (0.50 = every game). */
  threshold: number
  n: number
  /** Accuracy among picks clearing the floor (null when none do). */
  accuracy: number | null
  /** Fraction of all games clearing the floor. */
  share: number
}

export interface BacktestReport {
  sport: string
  model: string | null
  /** log = per-game curve+sweep, summary = aggregate only, none = nothing. */
  kind: 'log' | 'summary' | 'none'
  n: number | null
  accuracy: number | null
  log_loss: number | null
  /** Only computed for "log" sports. */
  brier: number | null
  start: string | null
  end: string | null
  calibration: BacktestCalBin[] | null
  sweep: BacktestSweepRow[] | null
  /** Raw per-year / per-tour breakdown for "summary" sports. */
  summary: Record<string, unknown> | null
  note: string | null
}

/** Fetch every engine's backtest report; null when the endpoint is missing/down. */
export async function fetchBacktestAll(): Promise<BacktestReport[] | null> {
  try {
    const r = await fetchJSON<{ reports: BacktestReport[] }>('/api/backtest')
    return r.reports
  } catch {
    return null
  }
}

// ---------------------------------------------------------- $-ROI backtest
// GET /api/backtest/{sport}/roi — strategy replay over backfilled Polymarket
// price history (jobs/backfill.py must have harvested the sport). Same knobs
// as the live autobet engine; three fill assumptions per run.

/** One fill-assumption simulation result. */
export interface RoiBacktestSim {
  fill: 'pessimistic' | 'realistic' | 'optimistic'
  n_bets: number
  wins: number
  win_rate: number | null
  staked_usd: number
  pnl_usd: number
  roi_pct: number | null
  end_bankroll_usd: number
  max_drawdown_usd: number
  equity: { date: string; pnl: number; cum: number }[]
  skipped: Record<string, number>
}

export interface RoiBacktestReport {
  sport: string
  offset_minutes: number
  /** Join quality: joined vs markets, thin/stale/mismatch drops. */
  join: {
    log_rows: number
    markets: number
    joined: number
    thin: number
    stale: number
    outcome_mismatch: number
    /** Games with a REAL harvested order book (jobs/bookfill.py). */
    with_book?: number
  }
  /** `book` (real archived ask, no padding) present only when harvested
   *  books cover part of the sample. */
  sims: Record<'pessimistic' | 'realistic' | 'optimistic', RoiBacktestSim> & {
    book?: RoiBacktestSim
  }
  note: string
}

/** Run the $-ROI backtest for one sport with (a subset of) autobet knobs. */
export async function fetchRoiBacktest(
  sport: string,
  params: Partial<{
    offset_minutes: number
    min_volume: number
    min_ev: number
    min_win_prob: number
    min_edge: number
    min_prob: number
    max_prob: number
    stake_mode: 'fixed' | 'percent' | 'confidence' | 'kelly'
    stake_usd: number
    stake_pct: number
    stake_pct_min: number
    stake_pct_max: number
    bankroll_usd: number
    daily_stop_loss_usd: number
    daily_stop_profit_usd: number
    stop_loss_usd: number
    stop_profit_usd: number
  }> = {},
): Promise<RoiBacktestReport | null> {
  try {
    const qs = new URLSearchParams(
      Object.entries(params).map(([k, v]) => [k, String(v)]),
    ).toString()
    return await fetchJSON<RoiBacktestReport>(
      `/api/backtest/${sport}/roi${qs ? `?${qs}` : ''}`,
      30_000,
    )
  } catch {
    return null
  }
}

// -------------------------------------------------------------- sharp money
// GET /api/sharpmoney/{sport} — whale flow + top holders + leaderboard-wallet
// positioning per slate game, from the public Polymarket Data API. Display
// context only (top wallets often profit on execution, not picks).

export interface SharpMoneyRow {
  game_id: string
  home: string
  away: string
  kickoff: string
  slug: string | null
  flow_home_usd: number
  flow_away_usd: number
  whale_trades: number
  lean: 'home' | 'away' | null
  sharp_home: string[]
  sharp_away: string[]
  top_holder_home: { name: string; shares: number } | null
  top_holder_away: { name: string; shares: number } | null
}

export interface SharpMoneyReport {
  sport: string
  games: SharpMoneyRow[]
  leaderboard_loaded?: boolean
  error?: string
}

/** One optimizer row: a gate combo scored on tune, then shown on validation. */
export interface RoiOptimizeRow {
  min_ev: number
  min_win_prob: number
  min_edge: number
  tune: { n_bets: number; roi_pct: number | null; pnl_usd: number }
  validation: { n_bets: number; roi_pct: number | null; pnl_usd: number }
}

export interface RoiOptimizeReport {
  sport: string
  split_date: string | null
  results: RoiOptimizeRow[]
  note: string
}

/** Grid-search gate thresholds with a 70/30 chronological tune/validation split. */
export async function fetchRoiOptimize(
  sport: string,
  params: Partial<{ offset_minutes: number; min_volume: number; stake_mode: string }> = {},
): Promise<RoiOptimizeReport | null> {
  try {
    const qs = new URLSearchParams(
      Object.entries(params).map(([k, v]) => [k, String(v)]),
    ).toString()
    return await fetchJSON<RoiOptimizeReport>(
      `/api/backtest/${sport}/roi/optimize${qs ? `?${qs}` : ''}`,
      60_000,
    )
  } catch {
    return null
  }
}

/** Kick off the Polymarket history harvest server-side (idempotent). */
export async function startBackfill(): Promise<{ started: boolean; running: boolean } | null> {
  try {
    return await postJSON<{ started: boolean; running: boolean }>('/api/jobs/backfill')
  } catch {
    return null
  }
}

export async function fetchSharpMoney(sport: string): Promise<SharpMoneyReport | null> {
  try {
    return await fetchJSON<SharpMoneyReport>(`/api/sharpmoney/${sport}`, 45_000)
  } catch {
    return null
  }
}

// ----------------------------------------------------------------- pick log
// GET /api/picklog — every VALUE pick the bot has recommended (STRONG/LEAN
// with a book price), pending and graded, plus the bot's own P&L at the flat
// 1u stake the settle job grades picks with. The BOT PICKS view of the
// tracker page. Same null-tolerance rules as the other feeds.

/** One row of GET /api/picklog — a value pick joined with its game. */
export interface PickLogRow {
  game_id: string
  sport: string
  model: string
  side: string
  /** Resolved display name for the side (team / player / "DRAW"). */
  team: string
  book: string | null
  /** American price the pick was recorded at. */
  price: number
  /** Expected profit per $1 staked. */
  ev: number | null
  kelly: number | null
  tier: 'STRONG' | 'LEAN'
  created_at: string
  locked_at: string | null
  result: MyBetResult | null
  /** Net units at a flat 1u stake once graded. */
  units: number | null
  graded_at: string | null
  home: string
  away: string
  league: string
  kickoff: string
  completed: boolean
  home_score: number | null
  away_score: number | null
}

/** {picks, pnl} — pnl reuses the MyBetsPnl shape so tiles/curves are shared. */
export interface PickLogReport {
  picks: PickLogRow[]
  pnl: MyBetsPnl
}

/** Fetch the bot's full pick log; null when the endpoint is missing/down. */
export async function fetchPickLog(): Promise<PickLogReport | null> {
  try {
    return await fetchJSON<PickLogReport>('/api/picklog')
  } catch {
    return null
  }
}

// ---------------------------------------------------------------- my-bets feed
// GET/POST /api/mybets + DELETE /api/mybets/{id} — the USER'S tracked-bet
// record (TRACK buttons + the TRACKER page), distinct from the model's
// auto-graded pick record (/api/roi). Types live here (the endpoints ship in
// parallel); every consumer must tolerate null (endpoint absent / backend
// down) — demo mode uses the localStorage store in src/mybets.ts instead.

export type MyBetResult = 'W' | 'L' | 'P'

/** One row of GET /api/mybets — a bet the user tracked, graded by the settle
 *  job once the game completes (result/units stay null until then). */
export interface MyBetRow {
  id: number
  game_id: string
  sport: string
  /** Outcome taken: 'home' | 'away' | 'draw'. */
  side: string
  /** Resolved display name for the side (team / player / "DRAW"). */
  team: string
  /** American price the bet was tracked at. */
  price: number
  book: string | null
  stake_units: number
  note: string | null
  created_at: string
  result: MyBetResult | null
  /** Net units won/lost once graded (W: +stake × payout, L: -stake, P: 0). */
  units: number | null
  home: string
  away: string
  league: string
  kickoff: string
  completed: boolean
}

/** Aggregate P&L over the user's graded bets. `daily` reuses the RoiDaily
 *  shape ({date, units, cum_units}) so equity-curve components are shared. */
export interface MyBetsPnl {
  n: number
  graded: number
  units: number
  roi_pct: number
  /** "12-8-1" — wins-losses-pushes. */
  record: string
  daily: RoiDaily[]
}

/** One leg of a tracked parlay, joined with its game like MyBetRow. */
export interface MyParlayLeg {
  game_id: string
  sport: string
  side: string
  team: string
  /** American price of this leg at track time. */
  price: number
  book: string | null
  /** W/L/P once this leg's game settles (independent of the slip). */
  result: MyBetResult | null
  home: string
  away: string
  league: string
  kickoff: string
  completed: boolean
  home_score: number | null
  away_score: number | null
}

/** One row of GET /api/mybets "parlays" — a parlay the user tracked. Grades
 *  once EVERY leg's game settles: a lost leg loses the slip, pushed legs
 *  drop out of the payout, all-pushed slips push. */
export interface MyParlayRow {
  id: number
  stake_units: number
  /** Combined American price at track time (product of leg decimals). */
  price: number
  note: string | null
  created_at: string
  result: MyBetResult | null
  units: number | null
  graded_at: string | null
  legs: MyParlayLeg[]
}

export interface MyBetsReport {
  bets: MyBetRow[]
  /** Absent on older backends — treat as []. */
  parlays?: MyParlayRow[]
  pnl: MyBetsPnl
}

/** POST /api/mybets body. */
export interface TrackBetInput {
  game_id: string
  side: string
  price: number
  book?: string | null
  stake_units?: number
  note?: string | null
  /** Totals bets only (side 'over'|'under'): the O/U line to grade against. */
  line?: number | null
}

// ------------------------------------------------------------- CLV coaching
// GET /api/mybets/clv — every logged bet vs the LAST pregame odds capture.
// Sustained positive CLV is the strongest known predictor of long-run profit.

export interface UserClvEntry {
  id: number
  sport: string
  team: string
  side: string
  price: number
  book: string | null
  close_book: string
  kickoff: string
  result: 'W' | 'L' | 'P' | null
  /** Moneyline bets: price advantage vs close, % (positive = beat the close). */
  clv_pct: number | null
  /** Totals bets: line movement in the bet's favor, points. */
  line_move_pts: number | null
  close_price: number | null
}

export interface UserClvReport {
  overall: {
    n_bets: number
    n_evaluated: number
    beat_close: number
    beat_rate: number | null
    avg_clv_pct: number | null
    avg_line_move_pts: number | null
  }
  by_sport: { sport: string; n: number; beat_rate: number; avg_clv_pct: number | null }[]
  recent: UserClvEntry[]
  verdict: string
}

/** Live-vs-backtest drift for the autobet record (advisory). */
export interface AutobetDrift {
  live: { n_graded: number; wins: number; win_rate: number | null }
  expected: {
    win_rate: number | null
    by_sport: { sport: string; win_rate: number; roi_pct: number | null; n: number }[]
  }
  verdict: string | null
}

export async function fetchAutobetDrift(): Promise<AutobetDrift | null> {
  try {
    return await fetchJSON<AutobetDrift>('/api/autobet/drift', 30_000)
  } catch {
    return null
  }
}

/** Slip guardian: injury warnings on games carrying pending bets. */
export interface GuardianWarning {
  bet_id: number
  game_id: string
  bet_team: string
  home: string
  away: string
  kickoff: string
  player: string
  injured_team: string
  status: string
}

export async function fetchGuardian(): Promise<{
  pending_bets: number
  warnings: GuardianWarning[]
} | null> {
  try {
    return await fetchJSON('/api/mybets/guardian', 30_000)
  } catch {
    return null
  }
}

export async function fetchUserClv(): Promise<UserClvReport | null> {
  try {
    return await fetchJSON<UserClvReport>('/api/mybets/clv')
  } catch {
    return null
  }
}

/** Fetch the user's bet log + P&L; null when the endpoint is missing/down. */
export async function fetchMyBets(): Promise<MyBetsReport | null> {
  try {
    return await fetchJSON<MyBetsReport>('/api/mybets')
  } catch {
    return null
  }
}

/** Track a bet. Resolves the created row (or the existing one on duplicate —
 *  the backend responds {"duplicate": true, ...existing}); null on failure. */
export async function postMyBet(
  input: TrackBetInput,
): Promise<(MyBetRow & { duplicate?: boolean }) | null> {
  try {
    const base = await apiBase()
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), 8000)
    try {
      const res = await fetch(`${base}/api/mybets`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(input),
        signal: ctl.signal,
      })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      return (await res.json()) as MyBetRow & { duplicate?: boolean }
    } finally {
      clearTimeout(timer)
    }
  } catch {
    return null
  }
}

/** POST /api/mybets/parlay body — one leg of the slip to track. */
export interface TrackParlayLegInput {
  game_id: string
  side: string
  price: number
  book?: string | null
}

/** POST /api/mybets/parlay body. */
export interface TrackParlayInput {
  legs: TrackParlayLegInput[]
  stake_units?: number
  note?: string | null
}

/** Track a parlay. Resolves the created row (or the existing one on
 *  duplicate leg set); null on failure. */
export async function postMyParlay(
  input: TrackParlayInput,
): Promise<(MyParlayRow & { duplicate?: boolean }) | null> {
  try {
    const base = await apiBase()
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), 8000)
    try {
      const res = await fetch(`${base}/api/mybets/parlay`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify(input),
        signal: ctl.signal,
      })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      return (await res.json()) as MyParlayRow & { duplicate?: boolean }
    } finally {
      clearTimeout(timer)
    }
  } catch {
    return null
  }
}

/** Delete an ungraded tracked parlay. False on failure (incl. 409 = already
 *  graded — graded slips are permanent record). */
export async function deleteMyParlay(id: number): Promise<boolean> {
  try {
    const base = await apiBase()
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), 8000)
    try {
      const res = await fetch(`${base}/api/mybets/parlay/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        signal: ctl.signal,
      })
      return res.ok
    } finally {
      clearTimeout(timer)
    }
  } catch {
    return false
  }
}

/** Delete an ungraded tracked bet. False on failure (incl. 409 = already
 *  graded — graded rows are permanent record). */
export async function deleteMyBet(id: number): Promise<boolean> {
  try {
    const base = await apiBase()
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), 8000)
    try {
      const res = await fetch(`${base}/api/mybets/${encodeURIComponent(id)}`, {
        method: 'DELETE',
        signal: ctl.signal,
      })
      return res.ok
    } finally {
      clearTimeout(timer)
    }
  } catch {
    return false
  }
}

// -------------------------------------------------------------- autobet feed
// /api/autobet — Polymarket auto-betting (real money; backend defaults to
// disabled + dry-run). Wallet key material never appears in any response.

export interface AutobetSettings {
  enabled: boolean
  dry_run: boolean
  /** 'fixed' = flat stake_usd; 'percent' = flat stake_pct% of balance;
   *  'confidence' = stake_pct_min→stake_pct_max scaled by model confidence;
   *  'kelly' = fractional Kelly sized by EDGE, hard-capped. */
  stake_mode: 'fixed' | 'percent' | 'confidence' | 'kelly'
  stake_usd: number
  stake_pct: number
  /** Confidence-mode band: % staked at low vs high model confidence. */
  stake_pct_min: number
  stake_pct_max: number
  /** Kelly mode: fraction of full Kelly (0.25 = quarter) + stake cap %. */
  kelly_mult: number
  kelly_cap_pct: number
  unit_usd: number
  min_ev: number
  /** Skip picks the model rates below this win probability (0..1; 0 = off). */
  min_win_prob: number
  /** Skip picks whose model_prob − market_prob is below this (0..1; 0 = off). */
  min_edge: number
  tiers: string[]
  /** Sport allowlist; empty = every sport. */
  sports: string[]
  /** Rank the eligible slate by edge and spend the daily cap on the top of
   *  it; everything the cap can't reach is skipped as 'not_best_edge'. */
  best_edge_only: boolean
  max_bets_per_day: number
  /** Never buy shares below this price (0..1; 0 = off) — longshot filter. */
  min_prob: number
  max_prob: number
  lead_minutes: number
  /** Never bet games starting further out than this many days. */
  max_days_ahead: number
  /** Tighter window in hours (0 = use max_days_ahead). */
  max_hours_ahead: number
  /** Realized-P&L halts; 0 = off. */
  daily_stop_loss_usd: number
  daily_stop_profit_usd: number
  stop_loss_usd: number
  stop_profit_usd: number
}

/** One named strategy (built-in preset or user profile) — the rules only. */
export interface AutobetStrategy {
  name: string
  settings: Partial<AutobetSettings>
}

export interface AutobetStrategies {
  presets: AutobetStrategy[]
  profiles: AutobetStrategy[]
}

/** Realized USD P&L over placed auto bets (win = shares − cost). */
export interface AutobetPnl {
  today_usd: number
  total_usd: number
  open_usd: number
}

export interface AutobetWallet {
  hasWalletKey: boolean
  hasApiCreds?: boolean
  address?: string
  addressPreview?: string
  funder?: string
  walletMode?: string
  keyStoredUnencrypted?: boolean
  metaError?: string
}

export interface AutobetToday {
  placed: number
  dry: number
  unmatched: number
  errors: number
  spent_usd: number
}

export interface AutobetSweepSummary {
  at?: string
  scanned: number
  placed: number
  dry: number
  unmatched: number
  errors: number
  dry_run?: boolean
  skipped: Record<string, number>
  pnl?: AutobetPnl
  /** Resolved per-pick stake this sweep (percent mode sizes off balance). */
  stake_usd?: number
  error?: string
}

export interface AutobetStatus {
  settings: AutobetSettings
  wallet: AutobetWallet
  today: AutobetToday
  pnl: AutobetPnl
  /** Which stop currently halts the engine, or null. */
  stopped:
    | 'daily_stop_loss'
    | 'daily_stop_profit'
    | 'stop_loss'
    | 'stop_profit'
    | null
  last_sweep: AutobetSweepSummary | null
}

/** One auto-bet attempt row (GET /api/autobet/log). */
export interface AutobetAttempt {
  id: number
  game_id: string
  sport: string
  side: string
  team: string
  tier: string
  ev: number | null
  limit_prob: number
  shares: number
  usd: number
  order_id: string | null
  status: 'dry' | 'placed' | 'unmatched' | 'error'
  error: string | null
  created_at: string
  best_ask_cents: number | null
  book_depth: number | null
  fill_shares: number | null
  fill_prob: number | null
  fee_rate: number | null
  home: string | null
  away: string | null
  kickoff: string | null
  // Graded on settled games only (dry + placed) — paper bets settle too.
  result: 'W' | 'L' | 'P' | null
  pnl_usd: number | null
}

export async function fetchAutobet(): Promise<AutobetStatus | null> {
  try {
    return await fetchJSON<AutobetStatus>('/api/autobet')
  } catch {
    return null
  }
}

export async function fetchAutobetLog(limit = 25): Promise<AutobetAttempt[] | null> {
  try {
    const r = await fetchJSON<{ attempts: AutobetAttempt[] }>(`/api/autobet/log?limit=${limit}`)
    return r.attempts
  } catch {
    return null
  }
}

async function postJSON<T>(path: string, body?: unknown, method = 'POST', timeoutMs = 30_000): Promise<T> {
  const base = await apiBase()
  const ctl = new AbortController()
  const timer = setTimeout(() => ctl.abort(), timeoutMs)
  try {
    const res = await fetch(base + path, {
      method,
      headers: body !== undefined ? { 'Content-Type': 'application/json' } : undefined,
      body: body !== undefined ? JSON.stringify(body) : undefined,
      signal: ctl.signal,
    })
    if (!res.ok) {
      let detail = `${res.status} ${res.statusText}`
      try {
        const parsed = (await res.json()) as { detail?: unknown }
        if (typeof parsed.detail === 'string') detail = parsed.detail
      } catch {
        /* non-JSON error body */
      }
      throw new Error(detail)
    }
    return (await res.json()) as T
  } finally {
    clearTimeout(timer)
  }
}

export async function saveAutobetSettings(
  patch: Partial<AutobetSettings>,
): Promise<AutobetSettings | null> {
  try {
    return await postJSON<AutobetSettings>('/api/autobet/settings', patch)
  } catch {
    return null
  }
}

/** Save the trading wallet. Resolves {error} (not null) on failure so the
 *  form can show WHY (bad key = 422 with a message). The readiness check
 *  runs server-side after the save and can take ~10s. */
export async function saveAutobetWallet(input: {
  private_key: string
  funder?: string | null
}): Promise<{ wallet?: AutobetWallet; check?: unknown; error?: string }> {
  try {
    return await postJSON('/api/autobet/wallet', input, 'POST', 60_000)
  } catch (e) {
    return { error: e instanceof Error ? e.message : 'request failed' }
  }
}

export async function testAutobetWallet(): Promise<{
  ready?: Record<string, unknown>
  balance?: Record<string, unknown>
  error?: string
}> {
  try {
    return await postJSON('/api/autobet/wallet/test', undefined, 'POST', 60_000)
  } catch (e) {
    return { error: e instanceof Error ? e.message : 'request failed' }
  }
}

export async function clearAutobetWallet(): Promise<boolean> {
  try {
    await postJSON('/api/autobet/wallet', undefined, 'DELETE')
    return true
  } catch {
    return false
  }
}

export async function runAutobet(): Promise<AutobetSweepSummary | null> {
  try {
    return await postJSON<AutobetSweepSummary>('/api/autobet/run', undefined, 'POST', 120_000)
  } catch {
    return null
  }
}

// ── Licensing (Krypt Tools premium) ──────────────────────────────────────
// Live auto-betting requires an active license; everything else works without
// one. Activation binds this machine to a key generated on the Krypt dashboard.
export interface LicenseStatus {
  licensed: boolean
  product: string
  reason: string | null
  hwid: string
  expires_at: string | null
  max_seats: number | null
  activations: number | null
  server: string
}

export async function fetchLicense(): Promise<LicenseStatus | null> {
  try {
    return await fetchJSON<LicenseStatus>('/api/license')
  } catch {
    return null
  }
}

/** Activate a key on this machine. THROWS with the server's message on failure
 *  (bad key, device limit, etc.) so the UI can surface it. */
export async function activateLicense(key: string): Promise<LicenseStatus> {
  return await postJSON<LicenseStatus>('/api/license/activate', { key }, 'POST', 20_000)
}

export async function deactivateLicense(): Promise<LicenseStatus> {
  return await postJSON<LicenseStatus>('/api/license/deactivate', undefined, 'POST', 20_000)
}

/** Renew the token / enforce revocation. Returns fresh status, or null if the
 *  server is unreachable (offline grace — the stored token stays valid). */
export async function heartbeatLicense(): Promise<LicenseStatus | null> {
  try {
    const r = await postJSON<{ ok: boolean } & LicenseStatus>('/api/license/heartbeat', undefined, 'POST', 20_000)
    return r
  } catch {
    return null
  }
}

/** Built-in presets + saved profiles; null when the endpoint is missing/down. */
export async function fetchAutobetProfiles(): Promise<AutobetStrategies | null> {
  try {
    return await fetchJSON<AutobetStrategies>('/api/autobet/profiles')
  } catch {
    return null
  }
}

/** Save the current live strategy under `name`. Returns the refreshed list,
 *  or an {error} the form can show (422 = name shadows a preset / is blank). */
export async function saveAutobetProfile(
  name: string,
): Promise<{ strategies?: AutobetStrategies; error?: string }> {
  try {
    return await postJSON<{ strategies: AutobetStrategies }>('/api/autobet/profiles', { name })
  } catch (e) {
    return { error: e instanceof Error ? e.message : 'request failed' }
  }
}

/** Apply a preset/profile's rules to the live settings (never arms). Returns
 *  the new full settings so the form refreshes like any settings patch. */
export async function applyAutobetProfile(name: string): Promise<AutobetSettings | null> {
  try {
    const r = await postJSON<{ settings: AutobetSettings }>('/api/autobet/profiles', {
      name,
      apply: true,
    })
    return r.settings
  } catch {
    return null
  }
}

export async function deleteAutobetProfile(name: string): Promise<AutobetStrategies | null> {
  try {
    const r = await postJSON<{ strategies: AutobetStrategies }>(
      `/api/autobet/profiles/${encodeURIComponent(name)}`,
      undefined,
      'DELETE',
    )
    return r.strategies
  } catch {
    return null
  }
}

// --------------------------------------------------------------- models feed
// GET /api/models + POST /api/models/{sport}/refresh — the MODELS panel feed.
// Types live here (the endpoints ship in parallel); every consumer must
// tolerate null (endpoint absent / backend down) and render a fallback.

/** On-disk artifact snapshot (state json or model pickle). */
export interface ModelFileStat {
  exists: boolean
  path: string
  size_kb: number
  updated_at: string // ISO datetime
}

/** Graded live record for one engine (mirrors tracker.performance() fields). */
export interface ModelLiveRecord {
  n: number
  accuracy: number
  log_loss: number
  brier: number
}

export interface ModelInfo {
  sport: string
  model: string
  state: ModelFileStat | null
  pickle: ModelFileStat | null
  live: ModelLiveRecord | null
  /** Engine-specific backtest blob — shape varies per engine, may be null. */
  backtest: Record<string, unknown> | null
}

export interface ModelsReport {
  models: ModelInfo[]
}

/** POST /api/models/{sport}/refresh response — full run log included. */
export interface ModelRefreshResult {
  sport: string
  ok: boolean
  seconds: number
  log: string[]
  state?: ModelFileStat | null
}

/** Fetch per-engine model status; null when the endpoint is missing/down. */
export async function fetchModels(): Promise<ModelsReport | null> {
  try {
    return await fetchJSON<ModelsReport>('/api/models')
  } catch {
    return null
  }
}

/** Kick a model data-refresh (or full retrain). Retraining can take tens of
 *  seconds, so the timeout is generous. Resolves null on failure. */
export async function refreshModel(
  sport: Sport,
  forceRetrain: boolean,
): Promise<ModelRefreshResult | null> {
  try {
    const base = await apiBase()
    const ctl = new AbortController()
    const timer = setTimeout(() => ctl.abort(), 180_000)
    try {
      const res = await fetch(`${base}/api/models/${sport}/refresh`, {
        method: 'POST',
        headers: { 'Content-Type': 'application/json' },
        body: JSON.stringify({ force_retrain: forceRetrain }),
        signal: ctl.signal,
      })
      if (!res.ok) throw new Error(`${res.status} ${res.statusText}`)
      return (await res.json()) as ModelRefreshResult
    } finally {
      clearTimeout(timer)
    }
  } catch {
    return null
  }
}
