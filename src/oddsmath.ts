import type { MoneylineOdds } from './types'

/** Model fair line in American odds from a win probability. */
export function fairLine(p: number): number {
  const q = Math.min(0.995, Math.max(0.005, p))
  return q >= 0.5 ? -Math.round((100 * q) / (1 - q)) : Math.round((100 * (1 - q)) / q)
}

/** Format American odds with explicit sign: +150 / -120. */
export function fmtAmerican(n: number): string {
  return n > 0 ? `+${n}` : `${n}`
}

/** Probability as a percentage string, e.g. 62.4 */
export function fmtPct(p: number, digits = 1): string {
  return (p * 100).toFixed(digits)
}

/** Net payout per $1 staked at an American price. */
export function payout(price: number): number {
  return price > 0 ? price / 100 : 100 / -price
}

/** Implied probability (with vig) of an American price. */
export function impliedProb(price: number): number {
  return price > 0 ? 100 / (price + 100) : -price / (-price + 100)
}

/** Vig-removed (normalized) implied probabilities for a set of prices. */
export function removeVig(prices: number[]): number[] {
  const raw = prices.map(impliedProb)
  const total = raw.reduce((a, b) => a + b, 0)
  return raw.map((q) => q / total)
}

/** Vig-removed probabilities by the POWER method (each raw implied
 *  probability raised to the k that makes them sum to 1). Proportional devig
 *  hands longshots more probability than they deserve; this is the
 *  conservative reference. Mirrors core/oddsmath.remove_vig_power. */
export function removeVigPower(prices: number[]): number[] {
  const raw = prices.map(impliedProb)
  if (raw.length === 0 || raw.some((r) => !(r > 0 && r < 1))) return removeVig(prices)
  let lo = 0.05
  let hi = 20
  for (let i = 0; i < 60; i++) {
    const k = (lo + hi) / 2
    const sum = raw.reduce((a, r) => a + Math.pow(r, k), 0)
    if (sum > 1) lo = k
    else hi = k
  }
  const k = (lo + hi) / 2
  const out = raw.map((r) => Math.pow(r, k))
  const total = out.reduce((a, b) => a + b, 0)
  return out.map((v) => v / total)
}

/** Expected profit per $1 staked given model probability and offered price. */
export function expectedValue(modelProb: number, price: number): number {
  return modelProb * payout(price) - (1 - modelProb)
}

/** Full-Kelly stake fraction. */
export function kellyFraction(modelProb: number, price: number): number {
  const b = payout(price)
  return Math.max(0, modelProb - (1 - modelProb) / b)
}

export interface BestPrice {
  book: string
  price: number
}

/** Best (highest) available American price for one side across all books. */
export function bestPrice(
  odds: MoneylineOdds[],
  side: 'home_ml' | 'away_ml' | 'draw_ml',
): BestPrice | null {
  let best: BestPrice | null = null
  for (const ml of odds) {
    const price = ml[side]
    if (price === null || price === undefined) continue
    if (best === null || price > best.price) best = { book: ml.book, price }
  }
  return best
}

/** Consensus (median) total line across books, if any book posts one. */
export function consensusTotal(odds: MoneylineOdds[]): number | null {
  const lines = odds
    .map((o) => o.total_line)
    .filter((t): t is number => t !== null && t !== undefined)
    .sort((a, b) => a - b)
  if (lines.length === 0) return null
  const mid = Math.floor(lines.length / 2)
  return lines.length % 2 === 1 ? lines[mid] : (lines[mid - 1] + lines[mid]) / 2
}

const BOOK_ABBREV: Record<string, string> = {
  pinnacle: 'PIN',
  draftkings: 'DK',
  fanduel: 'FD',
  betmgm: 'MGM',
  caesars: 'CZR',
  bet365: 'B365',
  pointsbet: 'PB',
  betrivers: 'BR',
  bovada: 'BOV',
  unibet: 'UNI',
  williamhill: 'WH',
}

/** Terminal-density abbreviation for a sportsbook name. */
export function bookAbbrev(book: string): string {
  const key = book.toLowerCase().replace(/[^a-z0-9]/g, '')
  return BOOK_ABBREV[key] ?? book.replace(/[^A-Za-z0-9]/g, '').slice(0, 4).toUpperCase()
}
