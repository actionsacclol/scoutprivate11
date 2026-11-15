/**
 * Bundled demo slate — realistic sample GameCards matching schemas.py exactly.
 *
 * Book prices are derived from the model probabilities plus a per-book
 * disagreement shift and an overround (vig), then edges are computed the same
 * way the backend does (vig-removed market prob, positive-EV sides only), so
 * every EV / Kelly number on screen is internally coherent.
 */
import type { BoardMap, Edge, GameCard, MoneylineOdds, Prediction, Sport } from './types'
import { expectedValue, kellyFraction, removeVig } from './oddsmath'

const NOW = Date.now()
const MIN = 60_000
const HOUR = 3_600_000

function iso(msFromNow: number): string {
  return new Date(NOW + msFromNow).toISOString()
}

/** Kickoff ISO for a spec. Negative offsets (live / finished demo games) are
 *  clamped to local midnight so they always land on TODAY's slate, even when
 *  the app is opened shortly after midnight. */
function kickoffIso(hoursOut: number): string {
  const t = NOW + hoursOut * HOUR
  if (hoursOut >= 0) return new Date(t).toISOString()
  const midnight = new Date(NOW)
  midnight.setHours(0, 5, 0, 0)
  return new Date(Math.max(t, midnight.getTime())).toISOString()
}

export function priceFromProb(q: number): number {
  const c = Math.min(0.985, Math.max(0.015, q))
  return c >= 0.5 ? -Math.round((100 * c) / (1 - c)) : Math.round((100 * (1 - c)) / c)
}

interface BookSpec {
  book: string
  /** Shift applied to the book's home probability vs. the model (creates edges). */
  h?: number
  /** Shift applied to the book's draw probability (soccer). */
  d?: number
  /** Offset applied to the book's total line. */
  t?: number
}

interface DemoSpec {
  n: number
  sport: Sport
  league?: string
  away: string
  home: string
  /** Kickoff offset from now — negative = already started (TODAY page demo). */
  hoursOut: number
  /** Live/final scoreboard as [away, home]. */
  score?: [number, number]
  /** Marks the game finished — sinks to FINAL on TODAY. */
  completed?: boolean
  homeProb: number
  drawProb?: number
  predHome?: number
  predAway?: number
  bookTotal?: number
  overProb?: number
  confidence: number
  model: string
  books: BookSpec[]
}

function buildCard(spec: DemoSpec): GameCard {
  const { sport, homeProb } = spec
  const drawProb = spec.drawProb ?? null
  const awayProb = +(1 - homeProb - (drawProb ?? 0)).toFixed(4)
  const gameId = `demo-${sport}-${spec.n}`
  const threeWay = drawProb !== null
  const overround = threeWay ? 1.06 : 1.045

  const predictedTotal =
    spec.predHome !== undefined && spec.predAway !== undefined
      ? +(spec.predHome + spec.predAway).toFixed(1)
      : null

  const prediction: Prediction = {
    game_id: gameId,
    sport,
    model: spec.model,
    generated_at: iso(-42 * MIN),
    home_win_prob: homeProb,
    away_win_prob: awayProb,
    draw_prob: drawProb,
    predicted_home_score: spec.predHome ?? null,
    predicted_away_score: spec.predAway ?? null,
    predicted_total: predictedTotal,
    over_prob: spec.overProb ?? null,
    confidence: spec.confidence,
  }

  const odds: MoneylineOdds[] = spec.books.map((b, i) => {
    const mktHome = homeProb + (b.h ?? 0)
    const mktDraw = drawProb !== null ? drawProb + (b.d ?? 0) : 0
    const mktAway = 1 - mktHome - mktDraw
    const totalLine = spec.bookTotal !== undefined ? +(spec.bookTotal + (b.t ?? 0)).toFixed(1) : null
    return {
      book: b.book,
      fetched_at: iso(-(6 + i * 3) * MIN),
      home_ml: priceFromProb(mktHome * overround),
      away_ml: priceFromProb(mktAway * overround),
      draw_ml: drawProb !== null ? priceFromProb(mktDraw * overround) : null,
      total_line: totalLine,
      over_price: totalLine !== null ? -112 + i * 2 : null,
      under_price: totalLine !== null ? -108 - i * 2 : null,
    }
  })

  // Edges computed exactly like the backend's _compute_edges.
  const edges: Edge[] = []
  for (const ml of odds) {
    if (ml.home_ml === null || ml.away_ml === null) continue
    const prices: number[] = [ml.home_ml, ml.away_ml]
    const sides: Array<['home' | 'away' | 'draw', number]> = [
      ['home', homeProb],
      ['away', awayProb],
    ]
    if (ml.draw_ml !== null && drawProb !== null) {
      prices.push(ml.draw_ml)
      sides.push(['draw', drawProb])
    }
    const fair = removeVig(prices)
    sides.forEach(([side, modelProb], i) => {
      const price = prices[i]
      const ev = expectedValue(modelProb, price)
      if (ev <= 0) return
      edges.push({
        game_id: gameId,
        sport,
        side,
        book: ml.book,
        price,
        model_prob: modelProb,
        market_prob: +fair[i].toFixed(4),
        ev_per_dollar: +ev.toFixed(4),
        kelly_fraction: +kellyFraction(modelProb, price).toFixed(4),
      })
    })
  }
  edges.sort((a, b) => b.ev_per_dollar - a.ev_per_dollar)

  return {
    game: {
      id: gameId,
      sport,
      league: spec.league ?? '',
      start_time: kickoffIso(spec.hoursOut),
      home: spec.home,
      away: spec.away,
      home_score: spec.score ? spec.score[1] : null,
      away_score: spec.score ? spec.score[0] : null,
      completed: spec.completed ?? false,
    },
    prediction,
    odds,
    edges,
  }
}

// Books with |h| <= ~0.012 stay inside the vig — no edge. Larger negative h
// underprices the home side (home edge); larger positive h creates away value.
const FLAT: BookSpec[] = [
  { book: 'Pinnacle', h: 0.004 },
  { book: 'DraftKings', h: -0.008, t: 0 },
  { book: 'FanDuel', h: 0.01, t: 0.5 },
  { book: 'BetMGM', h: -0.005, t: -0.5 },
]

const SPECS: DemoSpec[] = [
  // ---- NBA ------------------------------------------------------------
  {
    n: 1, sport: 'nba', away: 'LAL', home: 'BOS', hoursOut: 2.5,
    homeProb: 0.643, predHome: 116.8, predAway: 109.6, bookTotal: 224.5,
    overProb: 0.57, confidence: 0.74, model: 'nba-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.006 },
      { book: 'DraftKings', h: -0.052, t: -0.5 }, // home value
      { book: 'FanDuel', h: 0.009, t: 0.5 },
      { book: 'BetMGM', h: -0.01 },
    ],
  },
  {
    n: 2, sport: 'nba', away: 'GSW', home: 'DEN', hoursOut: 4,
    homeProb: 0.581, predHome: 118.2, predAway: 114.9, bookTotal: 231.5,
    overProb: 0.53, confidence: 0.68, model: 'nba-elo-v1', books: FLAT,
  },
  {
    n: 3, sport: 'nba', away: 'MIL', home: 'PHX', hoursOut: 4.5,
    homeProb: 0.468, predHome: 111.4, predAway: 113.9, bookTotal: 226.0,
    overProb: 0.49, confidence: 0.61, model: 'nba-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.005 },
      { book: 'DraftKings', h: 0.011 },
      { book: 'FanDuel', h: 0.048, t: 0.5 }, // away value
      { book: 'BetMGM', h: -0.006 },
    ],
  },
  {
    n: 4, sport: 'nba', away: 'NYK', home: 'MIA', hoursOut: 5,
    homeProb: 0.552, predHome: 108.7, predAway: 106.2, bookTotal: 213.5,
    overProb: 0.55, confidence: 0.66, model: 'nba-elo-v1', books: FLAT,
  },
  {
    n: 5, sport: 'nba', away: 'DAL', home: 'OKC', hoursOut: 6.5,
    homeProb: 0.712, predHome: 121.3, predAway: 111.8, bookTotal: 233.0,
    overProb: 0.51, confidence: 0.79, model: 'nba-elo-v1',
    books: [
      { book: 'Pinnacle', h: -0.002 },
      { book: 'DraftKings', h: 0.008 },
      { book: 'FanDuel', h: -0.038 }, // mild home value
      { book: 'BetMGM', h: 0.004, t: 0.5 },
    ],
  },
  {
    n: 6, sport: 'nba', away: 'CLE', home: 'MIN', hoursOut: 7,
    homeProb: 0.517, predHome: 112.1, predAway: 110.9, bookTotal: 222.5,
    overProb: 0.52, confidence: 0.58, model: 'nba-elo-v1', books: FLAT,
  },

  // ---- NHL ------------------------------------------------------------
  {
    n: 1, sport: 'nhl', away: 'TOR', home: 'BOS', hoursOut: 3,
    homeProb: 0.571, predHome: 3.4, predAway: 2.8, bookTotal: 6.0,
    overProb: 0.54, confidence: 0.7, model: 'nhl-poisson-v2',
    books: [
      { book: 'Pinnacle', h: 0.004 },
      { book: 'DraftKings', h: -0.046, t: 0.5 }, // home value
      { book: 'FanDuel', h: 0.008 },
      { book: 'BetMGM', h: -0.009 },
    ],
  },
  {
    n: 2, sport: 'nhl', away: 'NYR', home: 'FLA', hoursOut: 3.5,
    homeProb: 0.608, predHome: 3.6, predAway: 2.6, bookTotal: 6.5,
    overProb: 0.48, confidence: 0.72, model: 'nhl-poisson-v2', books: FLAT,
  },
  {
    n: 3, sport: 'nhl', away: 'EDM', home: 'COL', hoursOut: 5.5,
    homeProb: 0.484, predHome: 3.1, predAway: 3.2, bookTotal: 6.5,
    overProb: 0.51, confidence: 0.63, model: 'nhl-poisson-v2',
    books: [
      { book: 'Pinnacle', h: 0.006 },
      { book: 'DraftKings', h: 0.01 },
      { book: 'FanDuel', h: 0.009 },
      { book: 'BetMGM', h: 0.055, t: -0.5 }, // away value (EDM)
    ],
  },
  {
    n: 4, sport: 'nhl', away: 'VGK', home: 'DAL', hoursOut: 6,
    homeProb: 0.543, predHome: 3.2, predAway: 2.9, bookTotal: 6.0,
    overProb: 0.53, confidence: 0.65, model: 'nhl-poisson-v2', books: FLAT,
  },
  {
    n: 5, sport: 'nhl', away: 'CAR', home: 'NJD', hoursOut: 6,
    homeProb: 0.441, predHome: 2.7, predAway: 3.1, bookTotal: 5.5,
    overProb: 0.56, confidence: 0.6, model: 'nhl-poisson-v2', books: FLAT,
  },
  {
    n: 6, sport: 'nhl', away: 'TBL', home: 'WSH', hoursOut: 7.5,
    homeProb: 0.509, predHome: 3.0, predAway: 3.0, bookTotal: 6.0,
    overProb: 0.5, confidence: 0.55, model: 'nhl-poisson-v2',
    books: [
      { book: 'Pinnacle', h: -0.003 },
      { book: 'DraftKings', h: 0.007 },
      { book: 'FanDuel', h: -0.041 }, // home value
      { book: 'BetMGM', h: 0.005 },
    ],
  },

  // ---- NFL ------------------------------------------------------------
  {
    n: 1, sport: 'nfl', away: 'KC', home: 'BUF', hoursOut: 26,
    homeProb: 0.554, predHome: 27.3, predAway: 24.8, bookTotal: 51.5,
    overProb: 0.52, confidence: 0.71, model: 'nfl-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.005 },
      { book: 'DraftKings', h: -0.049, t: 0.5 }, // home value
      { book: 'FanDuel', h: 0.01 },
      { book: 'BetMGM', h: -0.008, t: -0.5 },
    ],
  },
  {
    n: 2, sport: 'nfl', away: 'DAL', home: 'PHI', hoursOut: 28,
    homeProb: 0.618, predHome: 26.1, predAway: 21.4, bookTotal: 47.5,
    overProb: 0.5, confidence: 0.75, model: 'nfl-elo-v1', books: FLAT,
  },
  {
    n: 3, sport: 'nfl', away: 'GB', home: 'DET', hoursOut: 29,
    homeProb: 0.583, predHome: 25.6, predAway: 22.9, bookTotal: 48.0,
    overProb: 0.51, confidence: 0.69, model: 'nfl-elo-v1', books: FLAT,
  },
  {
    n: 4, sport: 'nfl', away: 'BAL', home: 'CIN', hoursOut: 31,
    homeProb: 0.462, predHome: 22.7, predAway: 24.3, bookTotal: 46.5,
    overProb: 0.53, confidence: 0.64, model: 'nfl-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.008 },
      { book: 'DraftKings', h: 0.012 },
      { book: 'FanDuel', h: 0.044 }, // away value (BAL)
      { book: 'BetMGM', h: -0.004 },
    ],
  },
  {
    n: 5, sport: 'nfl', away: 'SF', home: 'LAR', hoursOut: 32,
    homeProb: 0.491, predHome: 23.8, predAway: 24.1, bookTotal: 49.0,
    overProb: 0.48, confidence: 0.62, model: 'nfl-elo-v1', books: FLAT,
  },
  {
    n: 6, sport: 'nfl', away: 'MIA', home: 'NYJ', hoursOut: 50,
    homeProb: 0.427, predHome: 20.2, predAway: 22.6, bookTotal: 43.5,
    overProb: 0.46, confidence: 0.6, model: 'nfl-elo-v1', books: FLAT,
  },
  { // far-future preseason-style slate: no books have posted lines yet —
    // demos the date-first card header + the "lines not posted" states, and
    // the damped preseason confidence (<0.25) → LOW CONFIDENCE chip.
    n: 7, sport: 'nfl', away: 'CHI', home: 'MIN', hoursOut: 26 * 24,
    homeProb: 0.538, predHome: 23.4, predAway: 21.9,
    confidence: 0.22, model: 'nfl-elo-v1', books: [],
  },

  // ---- NCAAF (college blowout probs are normal) -------------------------
  {
    n: 1, sport: 'ncaaf', league: 'SEC', away: 'Alabama', home: 'Georgia', hoursOut: 27,
    homeProb: 0.583, predHome: 27.4, predAway: 24.2, bookTotal: 51.5,
    overProb: 0.52, confidence: 0.71, model: 'ncaaf-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.005 },
      { book: 'DraftKings', h: -0.048, t: 0.5 }, // home value (UGA)
      { book: 'FanDuel', h: 0.009 },
      { book: 'BetMGM', h: -0.007, t: -0.5 },
    ],
  },
  {
    n: 2, sport: 'ncaaf', league: 'BIG TEN', away: 'Michigan', home: 'Ohio State', hoursOut: 29,
    homeProb: 0.641, predHome: 29.8, predAway: 23.6, bookTotal: 53.5,
    overProb: 0.5, confidence: 0.74, model: 'ncaaf-elo-v1', books: FLAT,
  },
  { // rivalry-week blowout — heavy home favorite, thin edge only
    n: 3, sport: 'ncaaf', league: 'SEC', away: 'Vanderbilt', home: 'Alabama', hoursOut: 30,
    homeProb: 0.912, predHome: 38.7, predAway: 12.8, bookTotal: 52.5,
    overProb: 0.49, confidence: 0.83, model: 'ncaaf-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.003 },
      { book: 'DraftKings', h: -0.006 },
      { book: 'FanDuel', h: -0.052 }, // home value — favorites still get mispriced
      { book: 'BetMGM', h: 0.004, t: 0.5 },
    ],
  },
  {
    n: 4, sport: 'ncaaf', league: 'SEC', away: 'LSU', home: 'Ole Miss', hoursOut: 31,
    homeProb: 0.527, predHome: 31.1, predAway: 29.9, bookTotal: 61.5,
    overProb: 0.55, confidence: 0.64, model: 'ncaaf-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.006 },
      { book: 'DraftKings', h: 0.011 },
      { book: 'FanDuel', h: 0.047, t: 0.5 }, // away value (LSU)
      { book: 'BetMGM', h: -0.005 },
    ],
  },
  { // another big line — Big Ten bottom-feeder on the road
    n: 5, sport: 'ncaaf', league: 'BIG TEN', away: 'Purdue', home: 'Penn State', hoursOut: 33,
    homeProb: 0.879, predHome: 34.6, predAway: 14.1, bookTotal: 48.5,
    overProb: 0.51, confidence: 0.79, model: 'ncaaf-elo-v1', books: FLAT,
  },
  {
    n: 6, sport: 'ncaaf', league: 'BIG TEN', away: 'Iowa', home: 'Wisconsin', hoursOut: 34,
    homeProb: 0.469, predHome: 23.7, predAway: 24.9, bookTotal: 48.5,
    overProb: 0.47, confidence: 0.6, model: 'ncaaf-elo-v1', books: FLAT,
  },

  // ---- NCAAB (blue-blood slate) -----------------------------------------
  {
    n: 1, sport: 'ncaab', league: 'ACC', away: 'Duke', home: 'North Carolina', hoursOut: 3.5,
    homeProb: 0.541, predHome: 77.8, predAway: 75.4, bookTotal: 152.5,
    overProb: 0.54, confidence: 0.67, model: 'ncaab-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.005 },
      { book: 'DraftKings', h: -0.05, t: -0.5 }, // home value (UNC)
      { book: 'FanDuel', h: 0.009, t: 0.5 },
      { book: 'BetMGM', h: -0.008 },
    ],
  },
  {
    n: 2, sport: 'ncaab', league: 'SEC', away: 'Kansas', home: 'Kentucky', hoursOut: 4.5,
    homeProb: 0.577, predHome: 76.2, predAway: 72.9, bookTotal: 148.5,
    overProb: 0.52, confidence: 0.7, model: 'ncaab-elo-v1', books: FLAT,
  },
  {
    n: 3, sport: 'ncaab', league: 'BIG EAST', away: 'UConn', home: 'Villanova', hoursOut: 5,
    homeProb: 0.438, predHome: 70.6, predAway: 73.8, bookTotal: 143.5,
    overProb: 0.51, confidence: 0.62, model: 'ncaab-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.006 },
      { book: 'DraftKings', h: 0.01 },
      { book: 'FanDuel', h: 0.046 }, // away value (UConn)
      { book: 'BetMGM', h: -0.006, t: -0.5 },
    ],
  },
  {
    n: 4, sport: 'ncaab', league: 'BIG TEN', away: 'Michigan State', home: 'Purdue', hoursOut: 5.5,
    homeProb: 0.619, predHome: 74.3, predAway: 69.8, bookTotal: 144.5,
    overProb: 0.49, confidence: 0.72, model: 'ncaab-elo-v1', books: FLAT,
  },
  {
    n: 5, sport: 'ncaab', league: 'WCC', away: 'Gonzaga', home: 'Saint Mary’s', hoursOut: 6.5,
    homeProb: 0.472, predHome: 71.1, predAway: 72.6, bookTotal: 141.5,
    overProb: 0.48, confidence: 0.61, model: 'ncaab-elo-v1',
    books: [
      { book: 'Pinnacle', h: -0.003 },
      { book: 'DraftKings', h: -0.042 }, // home value
      { book: 'FanDuel', h: 0.008 },
      { book: 'BetMGM', h: 0.005, t: 0.5 },
    ],
  },
  {
    n: 6, sport: 'ncaab', league: 'BIG 12', away: 'Houston', home: 'Arizona', hoursOut: 7,
    homeProb: 0.507, predHome: 73.4, predAway: 72.8, bookTotal: 146.5,
    overProb: 0.53, confidence: 0.59, model: 'ncaab-elo-v1', books: FLAT,
  },

  // ---- MLB ------------------------------------------------------------
  {
    n: 1, sport: 'mlb', away: 'NYY', home: 'BOS', hoursOut: 2,
    homeProb: 0.472, predHome: 4.3, predAway: 4.6, bookTotal: 9.0,
    overProb: 0.52, confidence: 0.63, model: 'mlb-poisson-v1',
    books: [
      { book: 'Pinnacle', h: 0.005 },
      { book: 'DraftKings', h: 0.011 },
      { book: 'FanDuel', h: 0.047, t: 0.5 }, // away value (NYY)
      { book: 'BetMGM', h: -0.006 },
    ],
  },
  {
    n: 2, sport: 'mlb', away: 'LAD', home: 'SF', hoursOut: 3.5,
    homeProb: 0.438, predHome: 3.9, predAway: 4.4, bookTotal: 8.0,
    overProb: 0.49, confidence: 0.61, model: 'mlb-poisson-v1', books: FLAT,
  },
  {
    n: 3, sport: 'mlb', away: 'HOU', home: 'TEX', hoursOut: 4,
    homeProb: 0.512, predHome: 4.5, predAway: 4.3, bookTotal: 8.5,
    overProb: 0.54, confidence: 0.59, model: 'mlb-poisson-v1',
    books: [
      { book: 'Pinnacle', h: 0.004 },
      { book: 'DraftKings', h: -0.049, t: -0.5 }, // home value
      { book: 'FanDuel', h: 0.009 },
      { book: 'BetMGM', h: -0.007 },
    ],
  },
  {
    n: 4, sport: 'mlb', away: 'ATL', home: 'PHI', hoursOut: 4.5,
    homeProb: 0.556, predHome: 4.8, predAway: 4.1, bookTotal: 9.0,
    overProb: 0.5, confidence: 0.66, model: 'mlb-poisson-v1', books: FLAT,
  },
  {
    n: 5, sport: 'mlb', away: 'CHC', home: 'MIL', hoursOut: 5.5,
    homeProb: 0.531, predHome: 4.4, predAway: 4.0, bookTotal: 8.5,
    overProb: 0.47, confidence: 0.6, model: 'mlb-poisson-v1',
    books: [
      { book: 'Pinnacle', h: -0.002 },
      { book: 'DraftKings', h: 0.007 },
      { book: 'FanDuel', h: -0.043, t: 0.5 }, // home value
      { book: 'BetMGM', h: 0.005 },
    ],
  },
  {
    n: 6, sport: 'mlb', away: 'SD', home: 'ARI', hoursOut: 6.5,
    homeProb: 0.464, predHome: 4.2, predAway: 4.5, bookTotal: 8.5,
    overProb: 0.51, confidence: 0.57, model: 'mlb-poisson-v1', books: FLAT,
  },
  { // in progress — demos the TODAY page's pinned LIVE section
    n: 7, sport: 'mlb', away: 'TB', home: 'BAL', hoursOut: -1.2, score: [3, 2],
    homeProb: 0.489, predHome: 4.1, predAway: 4.4, bookTotal: 8.5,
    overProb: 0.5, confidence: 0.58, model: 'mlb-poisson-v1', books: FLAT,
  },
  { // finished earlier today — demos the TODAY page's FINAL section
    n: 8, sport: 'mlb', away: 'SEA', home: 'KC', hoursOut: -4.5, score: [6, 4], completed: true,
    homeProb: 0.517, predHome: 4.5, predAway: 4.2, bookTotal: 9.0,
    overProb: 0.53, confidence: 0.6, model: 'mlb-poisson-v1', books: FLAT,
  },

  // ---- WNBA -----------------------------------------------------------
  {
    n: 1, sport: 'wnba', away: 'NYL', home: 'LVA', hoursOut: 3,
    homeProb: 0.547, predHome: 86.4, predAway: 83.9, bookTotal: 168.5,
    overProb: 0.55, confidence: 0.68, model: 'wnba-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.005 },
      { book: 'DraftKings', h: -0.051, t: -0.5 }, // home value
      { book: 'FanDuel', h: 0.008, t: 0.5 },
      { book: 'BetMGM', h: -0.009 },
    ],
  },
  {
    n: 2, sport: 'wnba', away: 'CON', home: 'MIN', hoursOut: 4,
    homeProb: 0.618, predHome: 84.7, predAway: 79.2, bookTotal: 162.5,
    overProb: 0.52, confidence: 0.71, model: 'wnba-elo-v1', books: FLAT,
  },
  {
    n: 3, sport: 'wnba', away: 'IND', home: 'CHI', hoursOut: 4.5,
    homeProb: 0.443, predHome: 81.6, predAway: 84.8, bookTotal: 165.5,
    overProb: 0.53, confidence: 0.62, model: 'wnba-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.006 },
      { book: 'DraftKings', h: 0.01 },
      { book: 'FanDuel', h: 0.046 }, // away value (IND)
      { book: 'BetMGM', h: -0.005, t: -0.5 },
    ],
  },
  {
    n: 4, sport: 'wnba', away: 'SEA', home: 'PHX', hoursOut: 5.5,
    homeProb: 0.509, predHome: 82.3, predAway: 81.7, bookTotal: 163.0,
    overProb: 0.5, confidence: 0.58, model: 'wnba-elo-v1', books: FLAT,
  },
  {
    n: 5, sport: 'wnba', away: 'ATL', home: 'WAS', hoursOut: 6,
    homeProb: 0.471, predHome: 80.9, predAway: 82.6, bookTotal: 161.5,
    overProb: 0.48, confidence: 0.6, model: 'wnba-elo-v1',
    books: [
      { book: 'Pinnacle', h: -0.003 },
      { book: 'DraftKings', h: -0.044 }, // home value
      { book: 'FanDuel', h: 0.009 },
      { book: 'BetMGM', h: 0.006, t: 0.5 },
    ],
  },
  {
    n: 6, sport: 'wnba', away: 'DAL', home: 'GSV', hoursOut: 7,
    homeProb: 0.583, predHome: 85.1, predAway: 80.4, bookTotal: 164.5,
    overProb: 0.51, confidence: 0.65, model: 'wnba-elo-v1', books: FLAT,
  },

  // ---- SOCCER (3-way) --------------------------------------------------
  {
    n: 1, sport: 'soccer', league: 'EPL', away: 'Arsenal', home: 'Man City', hoursOut: 20,
    homeProb: 0.451, drawProb: 0.272, predHome: 1.8, predAway: 1.3, bookTotal: 2.5,
    overProb: 0.58, confidence: 0.66, model: 'soccer-dixoncoles-v1',
    books: [
      { book: 'Pinnacle', h: 0.005, d: -0.002 },
      { book: 'Bet365', h: -0.051, d: 0.008 }, // home value
      { book: 'DraftKings', h: 0.01, d: -0.006 },
      { book: 'FanDuel', h: -0.006, d: 0.004 },
    ],
  },
  {
    n: 2, sport: 'soccer', league: 'EPL', away: 'Liverpool', home: 'Chelsea', hoursOut: 22,
    homeProb: 0.338, drawProb: 0.281, predHome: 1.2, predAway: 1.6, bookTotal: 2.5,
    overProb: 0.55, confidence: 0.61, model: 'soccer-dixoncoles-v1',
    books: [
      { book: 'Pinnacle', h: 0.006, d: -0.003 },
      { book: 'Bet365', h: 0.009, d: 0.002 },
      { book: 'DraftKings', h: 0.052, d: -0.01 }, // away value (Liverpool)
      { book: 'FanDuel', h: -0.005, d: 0.005 },
    ],
  },
  {
    n: 3, sport: 'soccer', league: 'LA LIGA', away: 'Real Madrid', home: 'Barcelona', hoursOut: 23,
    homeProb: 0.412, drawProb: 0.262, predHome: 1.7, predAway: 1.5, bookTotal: 3.0,
    overProb: 0.6, confidence: 0.64, model: 'soccer-dixoncoles-v1',
    books: [
      { book: 'Pinnacle', h: 0.004, d: 0.002 },
      { book: 'Bet365', h: 0.007, d: -0.031 }, // draw value
      { book: 'DraftKings', h: -0.008, d: 0.004 },
      { book: 'FanDuel', h: 0.01, d: -0.002 },
    ],
  },
  {
    n: 4, sport: 'soccer', league: 'EPL', away: 'Tottenham', home: 'Newcastle', hoursOut: 25,
    homeProb: 0.443, drawProb: 0.264, predHome: 1.6, predAway: 1.3, bookTotal: 2.75,
    overProb: 0.54, confidence: 0.59, model: 'soccer-dixoncoles-v1',
    books: [
      { book: 'Pinnacle', h: 0.005, d: -0.002 },
      { book: 'Bet365', h: -0.007, d: 0.003 },
      { book: 'DraftKings', h: 0.009, d: -0.004 },
      { book: 'FanDuel', h: -0.004, d: 0.002 },
    ],
  },
  {
    n: 5, sport: 'soccer', league: 'SERIE A', away: 'Inter', home: 'Juventus', hoursOut: 44,
    homeProb: 0.352, drawProb: 0.301, predHome: 1.1, predAway: 1.4, bookTotal: 2.25,
    overProb: 0.47, confidence: 0.57, model: 'soccer-dixoncoles-v1',
    books: [
      { book: 'Pinnacle', h: 0.006, d: -0.003 },
      { book: 'Bet365', h: 0.008, d: 0.002 },
      { book: 'DraftKings', h: -0.005, d: -0.002 },
      { book: 'FanDuel', h: 0.007, d: 0.004 },
    ],
  },
  {
    n: 6, sport: 'soccer', league: 'BUNDESLIGA', away: 'Bayern', home: 'Dortmund', hoursOut: 46,
    homeProb: 0.331, drawProb: 0.243, predHome: 1.5, predAway: 1.9, bookTotal: 3.5,
    overProb: 0.62, confidence: 0.63, model: 'soccer-dixoncoles-v1',
    books: [
      { book: 'Pinnacle', h: 0.004, d: -0.002 },
      { book: 'Bet365', h: 0.006, d: 0.003 },
      { book: 'DraftKings', h: 0.049, d: -0.008 }, // away value (Bayern)
      { book: 'FanDuel', h: -0.006, d: 0.002 },
    ],
  },

  // ---- TENNIS (home = player1, away = player2; no totals) --------------
  {
    n: 1, sport: 'tennis', league: 'ATP', home: 'Alcaraz', away: 'Sinner', hoursOut: 1.5,
    homeProb: 0.522, confidence: 0.67, model: 'tennis-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.005 },
      { book: 'Bet365', h: -0.047 }, // P1 value (Alcaraz)
      { book: 'DraftKings', h: 0.009 },
      { book: 'FanDuel', h: -0.006 },
    ],
  },
  {
    n: 2, sport: 'tennis', league: 'ATP', home: 'Djokovic', away: 'Zverev', hoursOut: 3,
    homeProb: 0.613, confidence: 0.73, model: 'tennis-elo-v1', books: FLAT,
  },
  {
    n: 3, sport: 'tennis', league: 'WTA', home: 'Swiatek', away: 'Gauff', hoursOut: 4,
    homeProb: 0.658, confidence: 0.76, model: 'tennis-elo-v1',
    books: [
      { book: 'Pinnacle', h: -0.003 },
      { book: 'Bet365', h: 0.008 },
      { book: 'DraftKings', h: -0.042 }, // P1 value (Swiatek)
      { book: 'FanDuel', h: 0.006 },
    ],
  },
  {
    n: 4, sport: 'tennis', league: 'ATP', home: 'Medvedev', away: 'Rune', hoursOut: 5.5,
    homeProb: 0.547, confidence: 0.62, model: 'tennis-elo-v1', books: FLAT,
  },
  {
    n: 5, sport: 'tennis', league: 'WTA', home: 'Sabalenka', away: 'Rybakina', hoursOut: 6,
    homeProb: 0.583, confidence: 0.69, model: 'tennis-elo-v1', books: FLAT,
  },
  {
    n: 6, sport: 'tennis', league: 'ATP', home: 'Fritz', away: 'Shelton', hoursOut: 7.5,
    homeProb: 0.469, confidence: 0.58, model: 'tennis-elo-v1',
    books: [
      { book: 'Pinnacle', h: 0.007 },
      { book: 'Bet365', h: 0.01 },
      { book: 'DraftKings', h: 0.046 }, // P2 value (Shelton)
      { book: 'FanDuel', h: -0.005 },
    ],
  },
]

function buildBoards(): BoardMap {
  const boards: BoardMap = {
    nba: [], nhl: [], nfl: [], ncaaf: [], ncaab: [], mlb: [], wnba: [], soccer: [], tennis: [],
  }
  for (const spec of SPECS) boards[spec.sport]!.push(buildCard(spec))
  return boards
}

export const DEMO_BOARDS: BoardMap = buildBoards()

// ---------------------------------------------------- demo ROI + pick log
// ONE source of truth for the bot's demo record. Graded pick rows are
// GENERATED from ROI_DELTAS — each day's picks sum to exactly that day's
// delta — then padded with zero-sum win/loss pairs up to the fabricated
// graded counts (62 STRONG + 41 LEAN = 103 value picks). DEMO_ROI (the
// dashboard/performance surfaces) and DEMO_PICKLOG (the tracker's BOT PICKS
// tab) are both computed from these same rows, so the demo can never
// disagree with itself. Pending rows come straight off the demo boards via
// derivePicks, the same derivation the PICKS page renders.

import type { PickLogReport, PickLogRow, RoiSummary } from './api'
import { derivePicks } from './picks'

/** Hand-tuned daily unit deltas — realistic streaks and two real drawdowns,
 *  trending to ~+11u over 30 days. */
const ROI_DELTAS = [
  1.2, -0.8, 0.6, 1.9, -1.4, 0.4, 0.0, 2.1, -0.6, 0.9,
  -1.8, 1.1, 0.7, -0.5, 1.6, -2.2, 0.8, 1.4, -0.9, 0.5,
  2.4, -1.1, 0.6, 1.2, -0.7, 1.8, -1.3, 0.9, 1.5, 0.6,
]

/** Decompose one day's unit delta into pick results whose units sum to it:
 *  whole wins at +100, whole losses, and one fractional piece priced so the
 *  American price actually pays the fraction (e.g. +0.6u → a -167 winner). */
function decomposeDay(delta: number): Array<{ result: 'W' | 'L' | 'P'; price: number; units: number }> {
  const out: Array<{ result: 'W' | 'L' | 'P'; price: number; units: number }> = []
  let tenths = Math.round(delta * 10)
  if (tenths === 0) return [{ result: 'P', price: -110, units: 0 }]
  while (tenths >= 10) {
    out.push({ result: 'W', price: 100, units: 1 })
    tenths -= 10
  }
  while (tenths <= -10) {
    out.push({ result: 'L', price: -115, units: -1 })
    tenths += 10
  }
  if (tenths >= 5) {
    out.push({ result: 'W', price: -Math.round(1000 / tenths), units: tenths / 10 })
  } else if (tenths > 0) {
    out.push({ result: 'W', price: 100 + tenths * 10, units: 1 + tenths / 10 })
    out.push({ result: 'L', price: -110, units: -1 })
  } else if (tenths < 0) {
    out.push({ result: 'W', price: -Math.round(1000 / (10 + tenths)), units: (10 + tenths) / 10 })
    out.push({ result: 'L', price: -105, units: -1 })
  }
  return out
}

/** [sport, away, home, [winner score, loser score]] — rotated through. */
const LOG_POOL: Array<[string, string, string, [number, number]]> = [
  ['nba', 'Boston Celtics', 'Milwaukee Bucks', [114, 105]],
  ['nhl', 'Colorado Avalanche', 'Dallas Stars', [4, 2]],
  ['mlb', 'LA Dodgers', 'San Diego Padres', [6, 3]],
  ['wnba', 'Las Vegas Aces', 'New York Liberty', [88, 79]],
  ['soccer', 'Liverpool', 'Newcastle', [2, 1]],
  ['nba', 'Phoenix Suns', 'Dallas Mavericks', [121, 113]],
  ['tennis', 'C. Alcaraz', 'J. Sinner', [1, 0]],
  ['nhl', 'Edmonton Oilers', 'Vegas Golden Knights', [3, 1]],
  ['mlb', 'NY Yankees', 'Houston Astros', [5, 2]],
  ['ncaab', 'Duke', 'North Carolina', [74, 68]],
  ['soccer', 'Real Madrid', 'Sevilla', [3, 1]],
  ['nba', 'OKC Thunder', 'Denver Nuggets', [118, 110]],
  ['tennis', 'I. Swiatek', 'A. Sabalenka', [1, 0]],
  ['mlb', 'Atlanta Braves', 'Philadelphia Phillies', [7, 4]],
  ['nhl', 'Toronto Maple Leafs', 'Florida Panthers', [5, 3]],
  ['wnba', 'Seattle Storm', 'Indiana Fever', [84, 80]],
]
const PUSH_GAME: [string, string, string, [number, number]] = [
  'nfl', 'Green Bay Packers', 'Detroit Lions', [20, 20],
]
const LOG_BOOKS = ['DraftKings', 'FanDuel', 'BetMGM', 'Caesars', 'Pinnacle', 'Bet365']

/** Fabricated graded-count targets: 62 STRONG + 41 LEAN = 103 value picks. */
const STRONG_TARGET = 62
const GRADED_TARGET = 103

function buildGradedLogRows(): PickLogRow[] {
  const day = 86_400_000
  const n = ROI_DELTAS.length
  // Delta decompositions first, then zero-sum win/loss pairs rotated across
  // days until the graded count hits the target — the daily sums (and so the
  // equity curve) are untouched by the padding.
  const pieces: Array<{ daysAgo: number; result: 'W' | 'L' | 'P'; price: number; units: number }> = []
  for (let d = 0; d < n; d++) {
    for (const part of decomposeDay(ROI_DELTAS[d])) pieces.push({ daysAgo: n - 1 - d, ...part })
  }
  let washDay = 0
  while (pieces.length + 1 < GRADED_TARGET) {
    pieces.push({ daysAgo: n - 1 - washDay, result: 'W', price: 100, units: 1 })
    pieces.push({ daysAgo: n - 1 - washDay, result: 'L', price: -120, units: -1 })
    washDay = (washDay + 7) % n
  }
  if (pieces.length < GRADED_TARGET) {
    pieces.push({ daysAgo: n - 1, result: 'P', price: -110, units: 0 })
  }
  pieces.sort((a, b) => b.daysAgo - a.daysAgo) // oldest kickoff first

  let strongLeft = STRONG_TARGET
  return pieces.map((piece, idx) => {
    const push = piece.result === 'P'
    const [sport, away, home, [hi, lo]] = push ? PUSH_GAME : LOG_POOL[idx % LOG_POOL.length]
    const side = push || idx % 2 === 0 ? 'home' : 'away'
    const homeWins = (piece.result === 'W') === (side === 'home')
    // Alternate tiers until the STRONG quota is spent; force STRONG once the
    // remaining rows are exactly the remaining quota. EV agrees with tier.
    const remaining = pieces.length - idx
    const strong = strongLeft > 0 && (idx % 2 === 0 || strongLeft >= remaining)
    if (strong) strongLeft--
    const ev = strong ? 0.04 + ((idx * 7) % 50) / 1000 : 0.01 + ((idx * 3) % 29) / 1000
    const kick = new Date(NOW - piece.daysAgo * day)
    // UTC hours: the row's kickoff DATE must equal the daily curve's date
    // (both slice the UTC ISO string), or the log drifts a day off the curve.
    kick.setUTCHours(19, 10, 0, 0)
    return {
      game_id: `demo-log-${idx}`,
      sport,
      model: `${sport}-model`,
      side,
      team: side === 'home' ? home : away,
      book: LOG_BOOKS[idx % LOG_BOOKS.length],
      price: piece.price,
      ev: +ev.toFixed(3),
      kelly: +(ev * 1.4).toFixed(3),
      tier: strong ? ('STRONG' as const) : ('LEAN' as const),
      created_at: new Date(kick.getTime() - 5 * 3_600_000).toISOString(),
      locked_at: kick.toISOString(),
      result: piece.result,
      units: piece.units,
      graded_at: new Date(kick.getTime() + 4 * 3_600_000).toISOString(),
      home,
      away,
      league: '',
      kickoff: kick.toISOString(),
      completed: true,
      home_score: push ? hi : homeWins ? hi : lo,
      away_score: push ? hi : homeWins ? lo : hi,
    }
  })
}

const LOG_GRADED: PickLogRow[] = buildGradedLogRows()

/** Today's pending value picks — straight off the demo boards. */
function buildPendingLogRows(): PickLogRow[] {
  const allCards = Object.values(DEMO_BOARDS).flatMap((cards) => cards ?? [])
  const rows: PickLogRow[] = []
  for (const p of derivePicks(allCards)) {
    if (p.tier === 'MODEL' || p.price === null) continue
    const g = p.card.game
    rows.push({
      game_id: g.id,
      sport: g.sport,
      model: `${g.sport}-model`,
      side: p.side,
      team: p.team,
      book: p.book,
      price: p.price,
      ev: p.ev,
      kelly: p.kelly,
      tier: p.tier,
      created_at: new Date(NOW - 2 * 3_600_000).toISOString(),
      locked_at: null,
      result: null,
      units: null,
      graded_at: null,
      home: g.home,
      away: g.away,
      league: g.league,
      kickoff: g.start_time,
      completed: false,
      home_score: null,
      away_score: null,
    })
  }
  return rows
}

const LOG_PENDING: PickLogRow[] = buildPendingLogRows()

const round1 = (v: number) => +v.toFixed(1)

/** W / (W + L), pushes excluded — mirrors the backend's _win_pct. */
function winPct(rows: PickLogRow[]): number {
  const w = rows.filter((r) => r.result === 'W').length
  const l = rows.filter((r) => r.result === 'L').length
  return w + l > 0 ? +(w / (w + l)).toFixed(3) : 0
}

function buildDemoRoi(): RoiSummary {
  const daily: RoiSummary['daily'] = []
  let cum = 0
  const n = ROI_DELTAS.length
  for (let i = 0; i < n; i++) {
    cum = +(cum + ROI_DELTAS[i]).toFixed(2)
    daily.push({
      date: new Date(NOW - (n - 1 - i) * 86_400_000).toISOString().slice(0, 10),
      units: ROI_DELTAS[i],
      cum_units: cum,
    })
  }
  const units = cum // ~+10.9 — identical to the pick-log sum by construction

  const tierStat = (tier: 'STRONG' | 'LEAN') => {
    const items = LOG_GRADED.filter((r) => r.tier === tier)
    return {
      n: items.length,
      units: +items.reduce((s, r) => s + (r.units ?? 0), 0).toFixed(2),
      win_pct: winPct(items),
    }
  }

  const bySport = new Map<string, PickLogRow[]>()
  for (const r of LOG_GRADED) {
    const list = bySport.get(r.sport) ?? []
    list.push(r)
    bySport.set(r.sport, list)
  }

  return {
    overall: {
      n_picks: LOG_GRADED.length + LOG_PENDING.length + 55,
      n_graded: LOG_GRADED.length,
      units,
      roi_pct: round1((units / LOG_GRADED.length) * 100),
      by_tier: {
        STRONG: tierStat('STRONG'),
        LEAN: tierStat('LEAN'),
        // MODEL picks are graded but never staked — and never in the pick
        // log (no price), so this one stays fabricated.
        MODEL: { n: 55, win_pct: 0.618 },
      },
    },
    by_sport: [...bySport.entries()]
      .map(([sport, items]) => ({
        sport,
        n: items.length,
        units: +items.reduce((s, r) => s + (r.units ?? 0), 0).toFixed(2),
        win_pct: winPct(items),
      }))
      .sort((a, b) => b.units - a.units),
    daily,
  }
}

export const DEMO_ROI: RoiSummary = buildDemoRoi()

function buildDemoPickLog(): PickLogReport {
  const rows = [...LOG_GRADED, ...LOG_PENDING].sort((a, b) =>
    a.kickoff < b.kickoff ? 1 : a.kickoff > b.kickoff ? -1 : 0,
  )
  const w = LOG_GRADED.filter((r) => r.result === 'W').length
  const l = LOG_GRADED.filter((r) => r.result === 'L').length
  const p = LOG_GRADED.filter((r) => r.result === 'P').length
  return {
    picks: rows,
    pnl: {
      n: rows.length,
      graded: LOG_GRADED.length,
      units: DEMO_ROI.overall.units,
      roi_pct: DEMO_ROI.overall.roi_pct,
      record: `${w}-${l}-${p}`,
      // Same deltas → the pick-log curve IS the ROI curve.
      daily: DEMO_ROI.daily,
    },
  }
}

export const DEMO_PICKLOG: PickLogReport = buildDemoPickLog()
