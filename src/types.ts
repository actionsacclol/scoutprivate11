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

export interface ClvReport {
  overall: ClvAgg
  by_sport: ClvSportAgg[]
  games: ClvGame[]
  /** Per-tier CLV (STRONG/LEAN/MODEL) — the edge truth-teller. Optional so
   *  demo/older payloads without it still type-check. */
  by_tier?: { STRONG: ClvTierAgg; LEAN: ClvTierAgg; MODEL: ClvTierAgg }
  note?: string
}
