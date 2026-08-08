/**
 * Parlay pricing — pure functions over GameCards, no fetches, no DOM.
 *
 * The product promise: "the parlay builder that tells you the truth."
 * A parlay's honest value is the product of the model win probabilities of
 * its legs; the books' combined price is the product of each leg's decimal
 * price. We show both and the gap between them.
 *
 * INDEPENDENCE ASSUMPTION — combinedModelProb multiplies leg probabilities,
 * which is only correct when legs are statistically independent. Legs from
 * the SAME game are strongly correlated (a moneyline can't be independent of
 * another outcome of the same game), so same-game legs are rejected here and
 * blocked in the UI. Cross-game legs (different games, even same day/league)
 * are close enough to independent for moneyline purposes.
 */
import type { GameCard, Sport } from './types'
import { bestPrice, fairLine, payout } from './oddsmath'

export const MIN_LEGS = 2
/** No practical leg cap — stack as deep as you dare; the math (a product)
 *  is numerically fine and the verdict gets more brutal with every leg. */
export const MAX_LEGS = Number.POSITIVE_INFINITY

export type LegSide = 'home' | 'away' | 'draw'

export interface LegPrice {
  book: string
  american: number
}

export interface Leg {
  gameId: string
  sport: Sport
  side: LegSide
  /** Display name of the outcome taken (team / player, or "DRAW"). */
  team: string
  /** The other side of the matchup, for context ("vs OPP"). */
  opponent: string
  /** Game start time, ISO. */
  kickoff: string
  /** Model win probability for this side. */
  modelProb: number
  /** Best (highest-payout) live American price across quoted books, if any. */
  bestPrice: LegPrice | null
  /** Model fair line for this side in American odds (EV = 0 price). */
  fairAmerican: number
}

const ML_KEY: Record<LegSide, 'home_ml' | 'away_ml' | 'draw_ml'> = {
  home: 'home_ml',
  away: 'away_ml',
  draw: 'draw_ml',
}

/** Decimal odds (total return per $1 staked) for an American price. */
export function decimalFromAmerican(american: number): number {
  return 1 + payout(american)
}

/** American odds for a decimal price. No probability clamp — parlay prices
 *  legitimately go far beyond single-bet ranges (8 legs → five figures). */
export function americanFromDecimal(decimal: number): number {
  const b = decimal - 1
  if (b <= 0) return -100_000 // degenerate (prob ~1); never reachable via buildLeg
  return b >= 1 ? Math.round(b * 100) : -Math.round(100 / b)
}

/**
 * Build a parlay leg from a game card for one side. Returns null when the
 * side can't be priced by the model: no prediction, game completed, or a
 * draw side on a two-way market.
 */
export function buildLeg(card: GameCard, side: LegSide): Leg | null {
  const { game, prediction, odds } = card
  if (game.completed || !prediction) return null

  const prob =
    side === 'home'
      ? prediction.home_win_prob
      : side === 'away'
        ? prediction.away_win_prob
        : prediction.draw_prob
  if (prob === null || prob === undefined || prob <= 0 || prob >= 1) return null

  const team = side === 'home' ? game.home : side === 'away' ? game.away : 'DRAW'
  const opponent =
    side === 'home' ? game.away : side === 'away' ? game.home : `${game.away} / ${game.home}`

  const bp = bestPrice(odds, ML_KEY[side])
  return {
    gameId: game.id,
    sport: game.sport,
    side,
    team,
    opponent,
    kickoff: game.start_time,
    modelProb: prob,
    bestPrice: bp ? { book: bp.book, american: bp.price } : null,
    fairAmerican: fairLine(prob),
  }
}

export type AddLegBlock = 'same-game' | 'completed' | 'max-legs' | 'no-model'

export interface AddLegCheck {
  ok: boolean
  reason: AddLegBlock | null
}

/**
 * Whether a side of `card` may join the slip. Same-game legs are blocked
 * because they're correlated and would break the independence assumption
 * in parlayMath (books void or reprice them for the same reason).
 */
export function canAddLeg(legs: Leg[], card: GameCard, side: LegSide): AddLegCheck {
  if (card.game.completed) return { ok: false, reason: 'completed' }
  if (!card.prediction) return { ok: false, reason: 'no-model' }
  if (legs.length >= MAX_LEGS) return { ok: false, reason: 'max-legs' }
  if (legs.some((l) => l.gameId === card.game.id)) return { ok: false, reason: 'same-game' }
  if (buildLeg(card, side) === null) return { ok: false, reason: 'no-model' }
  return { ok: true, reason: null }
}

export interface ParlayMath {
  legCount: number
  /** Product of leg model probabilities (independence assumed; see header). */
  combinedModelProb: number
  /** Fair total return per $1 = product of 1/modelProb = 1/combinedModelProb. */
  fairDecimal: number
  fairAmerican: number
  /** True when every leg has a live book price; false → model-only slip. */
  priced: boolean
  /** Legs with no live price (drives the model-only messaging). */
  unpricedLegs: number
  /** Product of each leg's best decimal price. Null when any leg is unpriced. */
  offeredDecimal: number | null
  offeredAmerican: number | null
  /** 1 / offeredDecimal — what the combined price implies you believe. */
  impliedProbOffered: number | null
  /** Expected profit per $1: combinedModelProb * offeredDecimal - 1. */
  evPerDollar: number | null
  /**
   * Payout shortfall vs fair: 1 - offeredDecimal / fairDecimal.
   * Positive → "you're paying X% under fair" (the usual case: the books'
   * compounded vig). Negative → the offer beats fair (+EV). Algebraically
   * this is exactly -evPerDollar, exposed separately for honest copy.
   */
  vigDrag: number | null
  /** Full-Kelly stake fraction for the parlay (0 when not +EV / unpriced). */
  kelly: number | null
  /** Kelly / 4 — the number we actually show. */
  quarterKelly: number | null
}

/**
 * Price a slip. Returns null for an invalid slip: empty, over MAX_LEGS, or
 * duplicate gameIds (correlated same-game legs — blocked upstream too).
 * A single leg is computable (it's just a straight bet); the UI enforces
 * MIN_LEGS for calling it a parlay.
 */
export function parlayMath(legs: Leg[]): ParlayMath | null {
  if (legs.length === 0 || legs.length > MAX_LEGS) return null
  const ids = new Set(legs.map((l) => l.gameId))
  if (ids.size !== legs.length) return null

  let combinedModelProb = 1
  for (const l of legs) combinedModelProb *= l.modelProb

  const fairDecimal = 1 / combinedModelProb
  const fairAmerican = americanFromDecimal(fairDecimal)

  const unpricedLegs = legs.filter((l) => l.bestPrice === null).length
  const priced = unpricedLegs === 0

  let offeredDecimal: number | null = null
  let offeredAmerican: number | null = null
  let impliedProbOffered: number | null = null
  let evPerDollar: number | null = null
  let vigDrag: number | null = null
  let kelly: number | null = null
  let quarterKelly: number | null = null

  if (priced) {
    offeredDecimal = 1
    for (const l of legs) offeredDecimal *= decimalFromAmerican((l.bestPrice as LegPrice).american)
    offeredAmerican = americanFromDecimal(offeredDecimal)
    impliedProbOffered = 1 / offeredDecimal
    evPerDollar = combinedModelProb * offeredDecimal - 1
    vigDrag = 1 - offeredDecimal / fairDecimal
    const b = offeredDecimal - 1 // net payout per $1
    kelly = b > 0 ? Math.max(0, combinedModelProb - (1 - combinedModelProb) / b) : 0
    quarterKelly = kelly / 4
  }

  return {
    legCount: legs.length,
    combinedModelProb,
    fairDecimal,
    fairAmerican,
    priced,
    unpricedLegs,
    offeredDecimal,
    offeredAmerican,
    impliedProbOffered,
    evPerDollar,
    vigDrag,
    kelly,
    quarterKelly,
  }
}

// ---------------------------------------------------------------- persistence

/** What we persist: just enough to rehydrate legs against fresh boards. */
export interface SlipRef {
  gameId: string
  side: LegSide
}

export interface RehydratedSlip {
  legs: Leg[]
  /** Refs that survived (mirror of legs, for saving back). */
  refs: SlipRef[]
  /** Count of saved legs whose games vanished, finished, or lost the model. */
  dropped: number
}

/**
 * Rebuild a saved slip against the current boards. Legs whose games are gone
 * or completed drop out (counted so the UI can say so). Duplicate gameIds and
 * anything past MAX_LEGS are discarded defensively.
 */
export function rehydrateSlip(refs: SlipRef[], cardById: Map<string, GameCard>): RehydratedSlip {
  const legs: Leg[] = []
  const kept: SlipRef[] = []
  let dropped = 0
  const seen = new Set<string>()
  for (const ref of refs) {
    if (seen.has(ref.gameId) || legs.length >= MAX_LEGS) {
      dropped++
      continue
    }
    const card = cardById.get(ref.gameId)
    const leg = card ? buildLeg(card, ref.side) : null
    if (!leg) {
      dropped++
      continue
    }
    seen.add(ref.gameId)
    legs.push(leg)
    kept.push(ref)
  }
  return { legs, refs: kept, dropped }
}
