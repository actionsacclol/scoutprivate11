/**
 * Recommended-bet derivation — pure functions over GameCards, no fetches.
 *
 * A "pick" answers two user questions at once:
 *   1. WHAT to take — the side the model likes (best +EV edge when live odds
 *      exist, otherwise the argmax-probability side as a model-only pick).
 *   2. WHERE to take it — the best price across every quoted book (line shop),
 *      because users hold accounts at all kinds of different books.
 *
 * Pricing semantics: `fairPrice` is the American price at which EV is exactly
 * zero for the model's probability (the model fair line). Any price BETTER
 * than fair — numerically higher in American terms, e.g. +145 vs a +138 fair,
 * or -140 vs a -152 fair — is +EV, so `minAcceptable` (=== fairPrice) is the
 * worst price still worth taking.
 */
import type { Edge, GameCard, Sport } from './types'
import { fairLine, payout } from './oddsmath'

export type PickTier = 'STRONG' | 'LEAN' | 'MODEL'
export type PickKind = 'value' | 'model'
export type PickSide = 'home' | 'away' | 'draw'

/** EV threshold (per $1) separating STRONG from LEAN value picks. */
export const STRONG_EV = 0.04
/** EV above this is a model artifact (fallback probs vs a longshot price),
 * never real market inefficiency — mirrored in backend store/picks.py. */
export const MAX_PLAUSIBLE_EV = 0.4
/** Claimed EV above this demotes STRONG → LEAN: the live graded record shows
 * high claimed EV anti-selects (every bucket above 10% EV lost money) — a
 * huge edge against a liquid market means the MODEL is wrong. Mirrored in
 * backend store/picks.py. */
export const SUSPECT_EV = 0.15
/** Sports whose model has measured NEGATIVE alpha vs the market (tennis:
 * −18% walk-forward ROI, −13u live, CLV ≈ 0). Never given a value tier —
 * MODEL picks only. Mirrored in backend store/picks.py. */
export const NO_VALUE_SPORTS: ReadonlySet<Sport> = new Set(['tennis'] as Sport[])

export interface Pick {
  card: GameCard
  sport: Sport
  gameId: string
  /** Which outcome to take. */
  side: PickSide
  /** Resolved display name: team / player, or "DRAW" for soccer three-ways. */
  team: string
  /** 'value' = backed by a +EV edge at a live book; 'model' = probabilities only. */
  kind: PickKind
  /** Book carrying the edge (value picks only). */
  book: string | null
  /** American price at that book (value picks only). */
  price: number | null
  /** Expected profit per $1 staked (value picks only). */
  ev: number | null
  /** Full-Kelly stake fraction (value picks only). */
  kelly: number | null
  modelProb: number
  /** Vig-removed market probability (value picks only). */
  marketProb: number | null
  /** Model fair line in American odds — the EV=0 price for this side. */
  fairPrice: number
  /** Worst acceptable price: anything BETTER than this is +EV. === fairPrice. */
  minAcceptable: number
  tier: PickTier
  /** Model confidence (used to rank MODEL picks). */
  confidence: number | null
}

const ML_KEY: Record<PickSide, 'home_ml' | 'away_ml' | 'draw_ml'> = {
  home: 'home_ml',
  away: 'away_ml',
  draw: 'draw_ml',
}

/** Resolve a pick side to its display name on a given card. */
export function pickSideName(side: PickSide, card: GameCard): string {
  if (side === 'home') return card.game.home
  if (side === 'away') return card.game.away
  return 'DRAW'
}

function isMoneylineSide(side: Edge['side']): side is PickSide {
  return side === 'home' || side === 'away' || side === 'draw'
}

/**
 * Derive the single recommended bet for one game, or null when there is
 * nothing to recommend (game completed, or no model output at all).
 *
 * Value pick: the highest-EV moneyline edge (over/under edges are excluded —
 * PICKS answers "what team wins", and totals don't resolve to a team).
 * Model pick: the argmax-probability side, priced at the model fair line.
 */
export function derivePick(card: GameCard): Pick | null {
  const { game, prediction, edges } = card
  if (game.completed) return null
  if (!prediction) return null

  // ---- value pick: best moneyline edge across all books -------------------
  const mlEdges = NO_VALUE_SPORTS.has(game.sport)
    ? []
    : edges.filter(
        (e) => isMoneylineSide(e.side) && e.ev_per_dollar <= MAX_PLAUSIBLE_EV,
      )
  if (mlEdges.length > 0) {
    const best = mlEdges.reduce((a, b) => (b.ev_per_dollar > a.ev_per_dollar ? b : a))
    const side = best.side as PickSide
    const fair = fairLine(best.model_prob)
    return {
      card,
      sport: game.sport,
      gameId: game.id,
      side,
      team: pickSideName(side, card),
      kind: 'value',
      book: best.book,
      price: best.price,
      ev: best.ev_per_dollar,
      kelly: best.kelly_fraction,
      modelProb: best.model_prob,
      marketProb: best.market_prob,
      fairPrice: fair,
      minAcceptable: fair,
      tier:
        best.ev_per_dollar >= STRONG_EV && best.ev_per_dollar <= SUSPECT_EV
          ? 'STRONG'
          : 'LEAN',
      confidence: prediction.confidence,
    }
  }

  // ---- model pick: argmax probability, no book attached --------------------
  const probs: Array<[PickSide, number]> = [
    ['home', prediction.home_win_prob],
    ['away', prediction.away_win_prob],
  ]
  if (prediction.draw_prob !== null) probs.push(['draw', prediction.draw_prob])
  const [side, prob] = probs.reduce((a, b) => (b[1] > a[1] ? b : a))
  const fair = fairLine(prob)
  return {
    card,
    sport: game.sport,
    gameId: game.id,
    side,
    team: pickSideName(side, card),
    kind: 'model',
    book: null,
    price: null,
    ev: null,
    kelly: null,
    modelProb: prob,
    marketProb: null,
    fairPrice: fair,
    minAcceptable: fair,
    tier: 'MODEL',
    confidence: prediction.confidence,
  }
}

const TIER_RANK: Record<PickTier, number> = { STRONG: 0, LEAN: 1, MODEL: 2 }

/**
 * Derive picks for a slate, sorted for display: STRONG by EV desc, then LEAN
 * by EV desc, then MODEL by confidence desc (model probability as tiebreak).
 */
export function derivePicks(cards: GameCard[]): Pick[] {
  const picks: Pick[] = []
  for (const card of cards) {
    const p = derivePick(card)
    if (p) picks.push(p)
  }
  picks.sort((a, b) => {
    const t = TIER_RANK[a.tier] - TIER_RANK[b.tier]
    if (t !== 0) return t
    if (a.tier === 'MODEL') {
      return (
        (b.confidence ?? 0) - (a.confidence ?? 0) || b.modelProb - a.modelProb
      )
    }
    return (b.ev ?? 0) - (a.ev ?? 0)
  })
  return picks
}

export interface ShopEntry {
  book: string
  price: number
  /** True for the bettor-best price (highest decimal payout); ties all flagged. */
  best: boolean
}

/**
 * Line shop for one side across every quoted book, sorted best-first for a
 * bettor. "Best" = highest decimal-odds equivalent (biggest payout); American
 * prices order the same way numerically (+150 > +120 > -105 > -140) but we
 * compare decimal payouts explicitly to keep the intent obvious.
 */
export function lineShop(card: GameCard, side: PickSide): ShopEntry[] {
  const key = ML_KEY[side]
  const entries = card.odds
    .filter((o) => o[key] !== null && o[key] !== undefined)
    .map((o) => ({ book: o.book, price: o[key] as number }))
    .sort((a, b) => payout(b.price) - payout(a.price))
  if (entries.length === 0) return []
  const top = payout(entries[0].price)
  return entries.map((e) => ({ ...e, best: payout(e.price) === top }))
}
