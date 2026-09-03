/** Mirrors backend/kryptscout/core/schemas.py exactly (snake_case as served). */

export type Sport =
  | 'nba'
  | 'nhl'
  | 'nfl'
  | 'ncaaf'
  | 'ncaab'
  | 'mlb'
  | 'wnba'
  | 'soccer'
  | 'tennis'

export interface Game {
  id: string
  sport: Sport
  league: string
  start_time: string // ISO datetime
  home: string
  away: string
  home_score: number | null
  away_score: number | null
  completed: boolean
}

export interface MoneylineOdds {
  book: string
  fetched_at: string // ISO datetime
  home_ml: number | null
  away_ml: number | null
  draw_ml: number | null // soccer three-way markets
  total_line: number | null
  over_price: number | null
  under_price: number | null
}

export interface Prediction {
  game_id: string
  sport: Sport
  model: string
  generated_at: string // ISO datetime
  home_win_prob: number
  away_win_prob: number
  draw_prob: number | null
  predicted_home_score: number | null
  predicted_away_score: number | null
  predicted_total: number | null
  over_prob: number | null
  confidence: number | null
}

export type EdgeSide = 'home' | 'away' | 'draw' | 'over' | 'under'

export interface Edge {
  game_id: string
  sport: Sport
  side: EdgeSide
  book: string
  price: number // American odds
  model_prob: number
  market_prob: number // vig-removed implied probability
  ev_per_dollar: number
  kelly_fraction: number
  /** Value edges are priced against the sharp fair: which exchange, its
   *  probability, the gap in points, and whether the other exchange agreed.
   *  Totals edges leave these null. `model_prob` stays the model's number. */
  fair_prob?: number | null
  ref_book?: string | null
  gap_pts?: number | null
  agree?: boolean | null
}

/** One side of a locked-in cross-venue position (core/arb.py). */
export interface ArbLeg {
  side: 'home' | 'away' | 'draw'
  book: string
  price: number
  implied: number
  /** Dollars per $1 of payout, exchange taker fee included. */
  cost: number
  stake_fraction: number
}

/** True arbitrage: every outcome bought for less than $1 of payout. No model
 *  involved; only the fee-inclusive net margin can flag it. */
export interface Arb {
  game_id: string
  sport: Sport
  legs: ArbLeg[]
  implied_sum: number
  cost_sum: number
  margin_pct: number
  net_margin_pct: number
}

/** Exchange lead (core/lean.py): the DraftKings side Kalshi/Polymarket say is
 *  underpriced. A measured reference tracked against the close — never a
 *  pick, never fed to autobet. */
export interface Lean {
  game_id: string
  sport: Sport
  side: 'home' | 'away'
  book: string
  price: number
  home_ml: number
  away_ml: number
  book_prob: number
  ref_book: string
  ref_prob: number
  gap_pts: number
  agree: boolean
}

/** Sharp fair per side (core/fair.py): exchange-devigged, Kalshi first. The
 *  number a boost or promo is scored against. `sharp` is false when only a
 *  sportsbook quotes the game. */
export interface FairPrices {
  game_id: string
  sport: Sport
  source: string
  sharp: boolean
  home: number
  away: number
  draw: number | null
  fetched_at: string
  /** Each full-market book's devigged HOME probability. */
  sources: Record<string, number>
}

/** Soccer same-game joint table (core/sgp.py): result × total from the
 *  sharp fair 1X2 and a real O/U 2.5 quote. Keys: singles by mask name,
 *  joints as "result|total". Keeps an SGP fair honest; never a pick. */
export interface SgpFair {
  game_id: string
  sport: Sport
  source: string
  over_book: string
  total_line: number
  fair_over: number
  lambda_home: number
  lambda_away: number
  rho: number
  delta: number
  exact: boolean
  singles: Record<string, number>
  joints: Record<string, number>
}

/** The probability the terminal DISPLAYS for a game (core/forecast.py):
 *  the market where any full-market fair exists (it beats every model and
 *  ESPN's predictor, and no blend survives out of sample), else the model
 *  labelled as a provisional pre-line estimate with its measured error. */
export interface Forecast {
  game_id: string
  sport: Sport
  kind: 'market' | 'model'
  source: string
  sharp: boolean
  thin: boolean
  home: number
  away: number
  draw: number | null
  error_pts: number | null
  model_home: number | null
  model_away: number | null
  model_draw: number | null
}

export interface GameCard {
  game: Game
  prediction: Prediction | null
  odds: MoneylineOdds[]
  edges: Edge[]
  /** Over/under edges (side 'over'|'under') — display + manual tracking only;
   *  totals have no walk-forward validation yet, so they never feed
   *  picks/autobet. Best book per side, best EV first. */
  totals_edges?: Edge[]
  /** Model-free signals; absent on demo/older payloads. */
  arb?: Arb | null
  lean?: Lean | null
  fair?: FairPrices | null
  sgp?: SgpFair | null
  forecast?: Forecast | null
}

export type BoardMap = Partial<Record<Sport, GameCard[]>>

export const SPORTS: Sport[] = [
  'nba',
  'nhl',
  'nfl',
  'ncaaf',
  'ncaab',
  'mlb',
  'wnba',
  'soccer',
  'tennis',
]

export const SPORT_TAG: Record<Sport, string> = {
  nba: 'NBA',
  nhl: 'NHL',
  nfl: 'NFL',
  ncaaf: 'CFB',
  ncaab: 'CBB',
  mlb: 'MLB',
  wnba: 'WNBA',
  soccer: 'SOC',
  tennis: 'TEN',
}

export const SPORT_NAME: Record<Sport, string> = {
  nba: 'NBA',
  nhl: 'NHL',
  nfl: 'NFL',
  ncaaf: 'COLLEGE FOOTBALL',
  ncaab: 'COLLEGE BASKETBALL',
  mlb: 'MLB',
  wnba: 'WNBA',
  soccer: 'SOCCER',
  tennis: 'TENNIS',
}

// ---------------------------------------------------------------- intel feeds

/** GET /api/injuries/{sport} row (empty array for soccer/tennis). */
export interface InjuryReport {
  team: string
  player: string
  position: string
  status: string
  detail: string
  comment: string
  date: string
}

/** GET /api/weather/{game_id} body (endpoint returns 200 with null when N/A). */
export interface WeatherInfo {
  temp_c: number | null
  temp_f: number | null
  wind_kph: number | null
  wind_dir_deg: number | null
  precip_prob_pct: number | null
  is_dome: boolean
  retractable?: boolean
  summary: string
}

/** One odds snapshot inside GET /api/movement/{game_id} (MoneylineOdds-shaped,
 *  minus `book`, which is the map key). */
export interface MovementSnapshot {
  book?: string
  fetched_at: string
  home_ml: number | null
  away_ml: number | null
  draw_ml: number | null
  total_line?: number | null
  over_price?: number | null
  under_price?: number | null
}

export interface MovementReport {
  game_id: string
  books: Record<string, MovementSnapshot[]>
}

// -------------------------------------------------- performance / CLV feeds
// Mirrors backend/kryptscout/store/tracker.py performance() / clv() exactly.

export interface PerfGroup {
  sport: string
  model: string
  n: number
  accuracy: number
  log_loss: number
  brier: number
}

export interface PerfDaily {
  date: string
  n: number
  accuracy: number
  log_loss: number
  /** Not emitted by tracker.performance() today; optional for forward compat. */
  sport?: string
  model?: string
}

export interface PerformanceReport {
  overall: PerfGroup[]
  daily: PerfDaily[]
}

export interface ClvAgg {
  n: number
  avg_clv_prob_pts: number
  avg_clv_bps: number
}

export interface ClvSportAgg extends ClvAgg {
  sport: string
}

export interface ClvGame {
  game_id: string
  sport: string
  model: string
  side: string
  books: number
  clv_prob_pts: number
  clv_bps: number
}

/** CLV of one pick tier's own-side bets vs the close, with an error bar. */
export interface ClvTierAgg {
  n: number
  avg_clv_prob_pts: number | null
  avg_clv_bps: number | null
  /** Standard error of the mean bps — the honest error bar. */
  se_bps: number | null
  win_rate: number | null
  /** True when |t| >= 2 at n >= 30 — i.e. the edge is statistically real. */
  significant: boolean
}

/** Same-book CLV of the exchange-lead reference (tracker.lean_clv). */
export interface LeanAgg {
  n: number
  clv_pct: number | null
  clv_se: number | null
  beat_close_rate: number | null
  n_moved: number
  /** Share of moved lines that moved TOWARD the lean side by the close. */
  hit_rate: number | null
  roi_pct: number | null
  significant: boolean
}

export interface LeanReport {
  overall: LeanAgg
  by_sport: Array<LeanAgg & { sport: string }>
  by_reference: Array<LeanAgg & { ref_book: string }>
  note?: string
}

/** GET /api/benchmark — Scout vs ESPN's predictor vs the market. */
export interface BenchmarkScore {
  n: number
  log_loss: number
  brier: number
  accuracy: number
}

/** A comparator scored on the games it shares with ESPN, with ESPN
 *  re-scored on exactly those games. */
export interface BenchmarkPair {
  n: number
  log_loss: number
  espn_log_loss: number
  brier: number
  accuracy: number
  espn_accuracy: number
  /** Share of common games where the comparator's log loss beat ESPN's. */
  beats_espn: number
}

export interface BenchmarkRow {
  sport: string
  n: number
  espn: BenchmarkScore | null
  scout: BenchmarkPair | null
  exchange: BenchmarkPair | null
  close: BenchmarkPair | null
}

export interface BenchmarkReport {
  overall: BenchmarkRow
  by_sport: BenchmarkRow[]
  note?: string
}

export interface ClvReport {
  overall: ClvAgg
  by_sport: ClvSportAgg[]
  games: ClvGame[]
  /** Per-tier CLV (STRONG/LEAN/MODEL) — the edge truth-teller. Optional so
   *  demo/older payloads without it still type-check. */
  by_tier?: { STRONG: ClvTierAgg; LEAN: ClvTierAgg; MODEL: ClvTierAgg }
  /** Exchange-lead reference vs DraftKings' own close. */
  lean?: LeanReport
  note?: string
}
