/**
 * Boost evaluator — pure math. Scores an odds boost or a profit-boost token
 * against a SHARP fair probability (GameCard.fair: Kalshi first, then
 * Polymarket, the reference that led DraftKings' close 64% of the time).
 *
 * No model anywhere in here. Every model this app ships has measured zero
 * information beyond the market; a boost is a book handing out a price above
 * the market's fair by construction, so the only number needed is the fair
 * probability and the only question is how far above it the boost lands.
 */
import { fairLine } from './oddsmath'
import { americanFromDecimal, decimalFromAmerican } from './parlay'

export type BoostKind = 'odds' | 'profit'

export interface BoostInput {
  /** Sharp fair probability of the side being bet. */
  fairProb: number
  kind: BoostKind
  /** Odds boost: the boosted American price the book is offering. */
  boostedPrice?: number | null
  /** Profit boost: the unboosted American price and the token's percentage. */
  basePrice?: number | null
  profitBoostPct?: number | null
  /** Optional stake (any unit) and the promo's max stake. */
  stake?: number | null
  maxStake?: number | null
}

export type BoostVerdict = 'PLAY' | 'MARGINAL' | 'PASS'

export interface BoostEval {
  effectiveDecimal: number
  effectiveAmerican: number
  /** Break-even American price at the fair probability. */
  fairAmerican: number
  /** Probability the effective price charges you to believe. */
  impliedProb: number
  /** Expected profit per $1 staked at the fair probability. */
  evPerDollar: number
  /** fairProb − impliedProb, in probability points. */
  edgePts: number
  kelly: number
  quarterKelly: number
  /** Stake after the promo cap, when a stake was given. */
  stake: number | null
  evAtStake: number | null
  verdict: BoostVerdict
}

/**
 * PLAY needs +2% per $1: the reference itself is only good to about a point
 * (a 1c exchange spread is ~1% of stake), so anything thinner is noise.
 * MARGINAL covers −1%…+2%; PASS is everything below.
 */
export const PLAY_EV = 0.02
export const MARGINAL_EV = -0.01

/** True for a usable American price: |price| ≥ 100 and finite. */
export function validAmerican(price: number | null | undefined): price is number {
  return price !== null && price !== undefined && Number.isFinite(price) && Math.abs(price) >= 100
}

/** Total return per $1 at the boosted terms, or null when inputs are incomplete. */
export function effectiveDecimal(input: BoostInput): number | null {
  if (input.kind === 'odds') {
    return validAmerican(input.boostedPrice) ? decimalFromAmerican(input.boostedPrice) : null
  }
  if (!validAmerican(input.basePrice)) return null
  const pct = input.profitBoostPct
  if (pct === null || pct === undefined || !Number.isFinite(pct) || pct < 0) return null
  // A profit boost multiplies WINNINGS, not the return: +150 at 50% pays 2.25 net, 3.25 total.
  return 1 + (decimalFromAmerican(input.basePrice) - 1) * (1 + pct / 100)
}

export function evaluateBoost(input: BoostInput): BoostEval | null {
  const p = input.fairProb
  if (!(p > 0 && p < 1)) return null
  const dec = effectiveDecimal(input)
  if (dec === null || dec <= 1) return null
  const b = dec - 1
  const evPerDollar = p * b - (1 - p)
  const kelly = Math.max(0, p - (1 - p) / b)
  let stake: number | null = null
  if (input.stake !== null && input.stake !== undefined && Number.isFinite(input.stake) && input.stake > 0) {
    stake = input.stake
    if (input.maxStake !== null && input.maxStake !== undefined && Number.isFinite(input.maxStake) && input.maxStake > 0) {
      stake = Math.min(stake, input.maxStake)
    }
  }
  const verdict: BoostVerdict = evPerDollar >= PLAY_EV ? 'PLAY' : evPerDollar >= MARGINAL_EV ? 'MARGINAL' : 'PASS'
  return {
    effectiveDecimal: dec,
    effectiveAmerican: americanFromDecimal(dec),
    fairAmerican: fairLine(p),
    impliedProb: 1 / dec,
    evPerDollar,
    edgePts: (p - 1 / dec) * 100,
    kelly,
    quarterKelly: kelly / 4,
    stake,
    evAtStake: stake !== null ? evPerDollar * stake : null,
    verdict,
  }
}

// ---------------------------------------------------------------- parlay promos
//
// Measured 2026-09-02: plain and auto-generated parlays lose 13–46% because
// the legs have no edge and the book compounds its hold. The ONLY parlay
// arithmetic that comes out positive is promotional: a book refunds a loss in
// site credit, multiplies winnings, or insures one losing leg. Each formula
// below is exact against market-fair leg probabilities; nothing here uses
// the model.
//
//   P  = Π p_i  (combined fair probability; correlation-adjusted when known)
//   D  = Π d_i  (the book's combined decimal price)
//   v  = value of $1 of site credit (0.70: bonus bets return no stake, and
//        practitioners measure 60–80% of face after hedging)
//   C  = the promo's cap on the stake it applies to

export type ParlayPromo = 'none' | 'profit' | 'nosweat' | 'insurance' | 'bonusbet'

export interface ParlayPromoInput {
  /** Per-leg fair probabilities (market where available). */
  legProbs: number[]
  /** Product of each leg's best decimal price. */
  offeredDecimal: number
  promo: ParlayPromo
  /** Profit boost: the token's percentage and the stake it applies to. */
  boostPct?: number | null
  boostCap?: number | null
  /** No-sweat / insurance: the refund cap in stake units. */
  refundCap?: number | null
  /** Value of $1 of site credit; default 0.70. */
  creditValue?: number | null
  stake?: number | null
}

export interface ParlayPromoEval {
  combinedProb: number
  /** Expected profit per $1 staked, promo included. */
  evPerDollar: number
  /** The plain parlay's EV per $1, for the delta. */
  plainEvPerDollar: number
  evAtStake: number | null
  /** Profit boost only: the boost % at which the slip breaks even. */
  breakEvenBoostPct: number | null
  /** No-sweat: P(lose) · insurance: P(exactly one leg loses). */
  refundProb: number | null
  verdict: BoostVerdict
}

export const CREDIT_VALUE = 0.7

/** P(exactly one leg loses) under independence. */
export function exactlyOneLosesProb(legProbs: number[]): number {
  let total = 0
  for (let i = 0; i < legProbs.length; i++) {
    let term = 1 - legProbs[i]
    for (let j = 0; j < legProbs.length; j++) if (j !== i) term *= legProbs[j]
    total += term
  }
  return total
}

export function evaluateParlayPromo(input: ParlayPromoInput): ParlayPromoEval | null {
  const probs = input.legProbs.filter((p) => p > 0 && p < 1)
  if (probs.length === 0 || probs.length !== input.legProbs.length) return null
  const D = input.offeredDecimal
  if (!(D > 1)) return null
  const P = probs.reduce((a, b) => a * b, 1)
  const v = input.creditValue ?? CREDIT_VALUE
  const S = input.stake && input.stake > 0 ? input.stake : 1
  const capShare = (cap: number | null | undefined) =>
    cap !== null && cap !== undefined && cap > 0 ? Math.min(1, cap / S) : 1
  const plain = P * D - 1
  let ev = plain
  let breakEven: number | null = null
  let refundProb: number | null = null
  switch (input.promo) {
    case 'none':
      break
    case 'profit': {
      const b = (input.boostPct ?? 0) / 100
      if (!(b >= 0)) return null
      const boosted = P * (1 + (D - 1) * (1 + b)) - 1
      const share = capShare(input.boostCap)
      ev = share * boosted + (1 - share) * plain
      breakEven = ((1 / P - 1) / (D - 1) - 1) * 100
      break
    }
    case 'nosweat': {
      refundProb = 1 - P
      ev = plain + v * refundProb * capShare(input.refundCap)
      break
    }
    case 'insurance': {
      refundProb = exactlyOneLosesProb(probs)
      ev = plain + v * refundProb * capShare(input.refundCap)
      break
    }
    case 'bonusbet': {
      // The stake is site credit: no cash at risk, and the stake is not
      // returned on a win. Value per $1 of face = P · (D − 1).
      ev = P * (D - 1)
      break
    }
  }
  const verdict: BoostVerdict = ev >= PLAY_EV ? 'PLAY' : ev >= MARGINAL_EV ? 'MARGINAL' : 'PASS'
  return {
    combinedProb: P,
    evPerDollar: ev,
    plainEvPerDollar: plain,
    evAtStake: input.stake && input.stake > 0 ? ev * input.stake : null,
    breakEvenBoostPct: breakEven,
    refundProb,
    verdict,
  }
}

/** Parse a typed American price ("+150", "-120", "150"); null when not usable. */
export function parseAmerican(text: string): number | null {
  const t = text.trim()
  if (!t) return null
  const n = Number(t)
  if (!Number.isFinite(n)) return null
  const rounded = Math.round(n)
  return validAmerican(rounded) ? rounded : null
}

