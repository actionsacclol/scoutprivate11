/**
 * Demo-mode intel fabrication: injuries, weather, line movement, performance
 * and CLV history for the bundled slate. Everything is deterministic (seeded
 * by game id) so panels are stable across re-renders and refreshes, and every
 * payload matches the live API contracts exactly.
 */
import type {
  ClvReport,
  GameCard,
  InjuryReport,
  MovementReport,
  MovementSnapshot,
  PerfDaily,
  PerfGroup,
  PerformanceReport,
  Sport,
  WeatherInfo,
} from './types'
import { impliedProb } from './oddsmath'
import { priceFromProb } from './demo'

// ------------------------------------------------------------ seeded random

function hashStr(s: string): number {
  let h = 2166136261
  for (let i = 0; i < s.length; i++) {
    h ^= s.charCodeAt(i)
    h = Math.imul(h, 16777619)
  }
  return h >>> 0
}

/** mulberry32 — tiny deterministic PRNG. */
function rng(seed: string): () => number {
  let a = hashStr(seed)
  return () => {
    a |= 0
    a = (a + 0x6d2b79f5) | 0
    let t = Math.imul(a ^ (a >>> 15), 1 | a)
    t = (t + Math.imul(t ^ (t >>> 7), 61 | t)) ^ t
    return ((t ^ (t >>> 14)) >>> 0) / 4294967296
  }
}

function pick<T>(r: () => number, arr: readonly T[]): T {
  return arr[Math.floor(r() * arr.length)]
}

// --------------------------------------------------------------- injuries

const FIRST = [
  'J.', 'M.', 'T.', 'D.', 'A.', 'C.', 'K.', 'R.', 'B.', 'L.', 'S.', 'N.',
] as const
const LAST = [
  'Johnson', 'Williams', 'Carter', 'Mitchell', 'Brooks', 'Hayes', 'Turner',
  'Reed', 'Morgan', 'Bennett', 'Porter', 'Sanders', 'Diaz', 'Murphy', 'Cole',
] as const

const POSITIONS: Partial<Record<Sport, readonly string[]>> = {
  nba: ['PG', 'SG', 'SF', 'PF', 'C'],
  wnba: ['G', 'G', 'F', 'F', 'C'],
  nhl: ['C', 'LW', 'RW', 'D', 'G'],
  nfl: ['QB', 'RB', 'WR', 'TE', 'OT', 'CB', 'S', 'LB', 'EDGE'],
  mlb: ['SP', 'RP', 'C', '1B', '2B', 'SS', '3B', 'OF', 'DH'],
}

const STATUSES: Partial<Record<Sport, readonly string[]>> = {
  nba: ['Out', 'Questionable', 'Doubtful', 'Day-To-Day'],
  wnba: ['Out', 'Questionable', 'Day-To-Day'],
  nhl: ['Out', 'Injured Reserve', 'Day-To-Day'],
  nfl: ['Out', 'Questionable', 'Doubtful', 'Injured Reserve'],
  mlb: ['10-Day-IL', '15-Day-IL', '60-Day-IL', 'Day-To-Day'],
}

const AILMENTS = [
  'Ankle', 'Hamstring', 'Knee', 'Back', 'Shoulder', 'Groin', 'Calf', 'Wrist',
  'Illness', 'Foot', 'Quad', 'Oblique',
] as const

/** Fabricated injury feed for one demo game: 1–3 plausible rows per team. */
export function demoInjuries(card: GameCard): InjuryReport[] {
  const { game } = card
  const positions = POSITIONS[game.sport]
  const statuses = STATUSES[game.sport]
  // soccer / tennis / college (ncaaf, ncaab): no league-wide injury feed
  if (!positions || !statuses) return []
  const rows: InjuryReport[] = []
  for (const team of [game.away, game.home]) {
    const r = rng(`inj:${game.id}:${team}`)
    const n = 1 + Math.floor(r() * 3)
    for (let i = 0; i < n; i++) {
      const ailment = pick(r, AILMENTS)
      const status = pick(r, statuses)
      const daysAgo = 1 + Math.floor(r() * 9)
      rows.push({
        team,
        player: `${pick(r, FIRST)} ${pick(r, LAST)}`,
        position: pick(r, positions),
        status,
        detail: ailment,
        comment: `${ailment} — ${status.toLowerCase()}, re-evaluated daily.`,
        date: new Date(Date.now() - daysAgo * 86_400_000).toISOString().slice(0, 10),
      })
    }
  }
  return rows
}

// ---------------------------------------------------------------- weather

const DOME_HOMES = new Set([
  // NFL
  'DET', 'MIN', 'NO', 'ATL', 'DAL', 'LV', 'IND', 'LAR',
  // MLB (fixed or retractable roofs)
  'TB', 'MIL', 'ARI', 'TEX', 'TOR', 'HOU', 'SEA', 'MIA',
])
const RETRACTABLE_HOMES = new Set(['MIL', 'ARI', 'TEX', 'TOR', 'HOU', 'SEA', 'MIA'])
const WIND_DIRS = ['N', 'NNE', 'NE', 'E', 'SE', 'S', 'SSW', 'SW', 'W', 'NW'] as const

/** Fabricated weather for demo games — outdoor sports only (NFL / NCAAF / MLB),
 *  like the live feed. College demo homes are all open-air stadiums. */
export function demoWeather(card: GameCard): WeatherInfo | null {
  const { game } = card
  if (game.sport !== 'nfl' && game.sport !== 'ncaaf' && game.sport !== 'mlb') return null
  const r = rng(`wx:${game.id}`)
  if (DOME_HOMES.has(game.home)) {
    const retractable = RETRACTABLE_HOMES.has(game.home)
    return {
      temp_c: 21,
      temp_f: 70,
      wind_kph: 0,
      wind_dir_deg: 0,
      precip_prob_pct: 0,
      is_dome: true,
      retractable,
      summary: retractable ? 'ROOF CLOSED — CLIMATE CONTROLLED' : 'DOME — CLIMATE CONTROLLED',
    }
  }
  const tempF = Math.round(game.sport === 'mlb' ? 66 + r() * 26 : 30 + r() * 45)
  const tempC = Math.round(((tempF - 32) * 5) / 9)
  const windKph = Math.round(4 + r() * 26)
  const dirIdx = Math.floor(r() * WIND_DIRS.length)
  const precip = Math.round(r() * 60)
  return {
    temp_c: tempC,
    temp_f: tempF,
    wind_kph: windKph,
    wind_dir_deg: dirIdx * 36,
    precip_prob_pct: precip,
    is_dome: false,
    summary: `${tempF}°F · WIND ${windKph} KPH ${WIND_DIRS[dirIdx]} · PRECIP ${precip}%`,
  }
}

// ------------------------------------------------------------ line movement

/**
 * Fabricated snapshot history per book: 5 snapshots over the last ~6 hours
 * drifting from a plausible opener to the book's current price, so the
 * open→close text and the sparkline agree with the odds table.
 */
export function demoMovement(card: GameCard): MovementReport {
  const books: Record<string, MovementSnapshot[]> = {}
  const now = Date.now()
  for (const ml of card.odds) {
    if (ml.home_ml === null || ml.away_ml === null) continue
    const r = rng(`mv:${card.game.id}:${ml.book}`)
    const closeProb = impliedProb(ml.home_ml)
    const drift = (r() - 0.5) * 0.07 // open differs from close by up to ±3.5 pts
    const openProb = Math.min(0.95, Math.max(0.05, closeProb - drift))
    const overround = impliedProb(ml.home_ml) + impliedProb(ml.away_ml) +
      (ml.draw_ml !== null ? impliedProb(ml.draw_ml) : 0)
    const steps = 5
    const snaps: MovementSnapshot[] = []
    for (let i = 0; i < steps; i++) {
      const f = i / (steps - 1)
      const jitter = i === 0 || i === steps - 1 ? 0 : (r() - 0.5) * 0.012
      const p = Math.min(0.95, Math.max(0.05, openProb + (closeProb - openProb) * f + jitter))
      const homeMl = i === steps - 1 ? ml.home_ml : priceFromProb(p)
      const rest = Math.max(0.02, overround - p)
      const drawShare = ml.draw_ml !== null ? impliedProb(ml.draw_ml) / rest : 0
      snaps.push({
        fetched_at: new Date(now - (steps - 1 - i) * 90 * 60_000).toISOString(),
        home_ml: homeMl,
        away_ml: i === steps - 1 ? ml.away_ml : priceFromProb(rest * (1 - drawShare)),
        draw_ml: ml.draw_ml !== null
          ? (i === steps - 1 ? ml.draw_ml : priceFromProb(rest * drawShare))
          : null,
        total_line: ml.total_line,
        over_price: ml.over_price,
        under_price: ml.under_price,
      })
    }
    books[ml.book] = snaps
  }
  return { game_id: card.game.id, books }
}

// ----------------------------------------------------- performance history

interface PerfSeed {
  sport: string
  model: string
  n: number
  accuracy: number
  log_loss: number
  brier: number
}

const PERF_SEEDS: PerfSeed[] = [
  { sport: 'mlb', model: 'mlb-poisson-v1', n: 412, accuracy: 0.578, log_loss: 0.672, brier: 0.481 },
  { sport: 'nba', model: 'nba-xgb-v1', n: 118, accuracy: 0.644, log_loss: 0.628, brier: 0.442 },
  { sport: 'ncaab', model: 'ncaab-elo-v1', n: 74, accuracy: 0.689, log_loss: 0.586, brier: 0.409 },
  { sport: 'ncaaf', model: 'ncaaf-elo-v1', n: 52, accuracy: 0.712, log_loss: 0.549, brier: 0.383 },
  { sport: 'nfl', model: 'nfl-elo-v1', n: 64, accuracy: 0.625, log_loss: 0.651, brier: 0.457 },
  { sport: 'nhl', model: 'nhl-poisson-v2', n: 96, accuracy: 0.594, log_loss: 0.667, brier: 0.474 },
  { sport: 'soccer', model: 'soccer-dixoncoles-v1', n: 143, accuracy: 0.517, log_loss: 1.012, brier: 0.598 },
  { sport: 'tennis', model: 'tennis-elo-v1', n: 207, accuracy: 0.662, log_loss: 0.611, brier: 0.431 },
  { sport: 'wnba', model: 'wnba-xgb-v1', n: 88, accuracy: 0.636, log_loss: 0.635, brier: 0.448 },
]

function buildDemoPerformance(): PerformanceReport {
  const overall: PerfGroup[] = PERF_SEEDS.map((s) => ({ ...s }))
  const daily: PerfDaily[] = []
  const r = rng('perf-daily')
  for (let d = 29; d >= 0; d--) {
    const date = new Date(Date.now() - d * 86_400_000).toISOString().slice(0, 10)
    const n = 14 + Math.floor(r() * 22)
    daily.push({
      date,
      n,
      accuracy: +(0.5 + r() * 0.24).toFixed(4),
      log_loss: +(0.58 + r() * 0.22).toFixed(4),
    })
  }
  return { overall, daily }
}

export const DEMO_PERFORMANCE: PerformanceReport = buildDemoPerformance()

export const DEMO_CLV: ClvReport = {
  overall: { n: 551, avg_clv_prob_pts: 0.41, avg_clv_bps: 89.0 },
  by_sport: [
    { sport: 'mlb', n: 204, avg_clv_prob_pts: 0.38, avg_clv_bps: 84.0 },
    { sport: 'nba', n: 71, avg_clv_prob_pts: 0.66, avg_clv_bps: 128.0 },
    { sport: 'ncaab', n: 31, avg_clv_prob_pts: 0.44, avg_clv_bps: 93.0 },
    { sport: 'ncaaf', n: 24, avg_clv_prob_pts: 0.29, avg_clv_bps: 61.0 },
    { sport: 'nfl', n: 38, avg_clv_prob_pts: 0.21, avg_clv_bps: 47.0 },
    { sport: 'nhl', n: 52, avg_clv_prob_pts: -0.12, avg_clv_bps: -26.0 },
    { sport: 'soccer', n: 47, avg_clv_prob_pts: 0.34, avg_clv_bps: 88.0 },
    { sport: 'tennis', n: 39, avg_clv_prob_pts: 0.74, avg_clv_bps: 149.0 },
    { sport: 'wnba', n: 45, avg_clv_prob_pts: 0.51, avg_clv_bps: 104.0 },
  ],
  games: [],
}
