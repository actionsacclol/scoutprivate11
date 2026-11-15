/**
 * Sportsbook directory + referral-link config — THIS is the file you edit.
 *
 * HOW TO MONETIZE:
 *   1. Find the book below (keys match what The Odds API emits).
 *   2. Replace `referralUrl: undefined` with your affiliate/referral link:
 *        referralUrl: 'https://sportsbook.example.com/?ref=YOUR_CODE',
 *   3. (Optional) Set `bonusHeadline` / `bonusDetail` so it shows on the
 *      BONUSES page, and `featured: true` to pin it big at the top.
 *
 * Every clickable book in the app (line-shop chips, book badges, odds tables,
 * BET NOW buttons, BONUSES cards) routes through bookLink() below, so a link
 * pasted here is used everywhere at once.
 *
 * Keep bonusHeadline/bonusDetail copy accurate to the book's real, current
 * offer — offer accuracy is the distributor's responsibility.
 */
import { EXCHANGE_SIGNUP } from '@shared/exchange-links'
import { openExternal } from '../utils/format'

export interface BookConfig {
  /** The Odds API bookmaker key (e.g. 'williamhill_us'). */
  key: string
  /** Full display name. */
  name: string
  /** Short mark, 2–4 chars, for chips/avatars. */
  short: string
  /** Public homepage — the fallback when no referralUrl is set. */
  homepage: string
  /** YOUR referral/affiliate link. When set, every click uses it. */
  referralUrl?: string
  /** Big offer line on the BONUSES page ("Bet $5, Get $150 in Bonus Bets"). */
  bonusHeadline?: string
  /** Small print under the headline. */
  bonusDetail?: string
  /** Pin this book to the large featured row on the BONUSES page. */
  featured?: boolean
}

export const REFERRALS: BookConfig[] = [
  {
    key: 'draftkings',
    name: 'DraftKings',
    short: 'DK',
    homepage: 'https://sportsbook.draftkings.com',
    referralUrl: 'https://www.draftkings.com/r/gambadepo/US-DK',
    bonusHeadline: 'New Player Welcome Offer',
    bonusDetail: 'Sign up through Krypt Scout — current DraftKings welcome offer applies.',
    featured: true,
  },
  {
    key: 'fanduel',
    name: 'FanDuel',
    short: 'FD',
    homepage: 'https://sportsbook.fanduel.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'betmgm',
    name: 'BetMGM',
    short: 'MGM',
    homepage: 'https://sports.betmgm.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'williamhill_us',
    name: 'Caesars Sportsbook',
    short: 'CZR',
    homepage: 'https://sportsbook.caesars.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    // ESPN Bet was terminated Dec 2025 and rebranded to theScore Bet; the
    // 'espnbet' key is kept because odds feeds may still emit it.
    key: 'espnbet',
    name: 'theScore Bet',
    short: 'SCOR',
    homepage: 'https://thescore.bet',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  // DFS pick-em apps — not in the odds feeds, but they belong on the BONUSES
  // page: real partner programs, and live in far more states than sportsbooks.
  {
    key: 'underdog',
    name: 'Underdog Fantasy',
    short: 'UD',
    homepage: 'https://underdogfantasy.com',
    referralUrl: undefined, // PASTE YOUR PARTNER LINK HERE (https://...)
  },
  {
    key: 'prizepicks',
    name: 'PrizePicks',
    short: 'PP',
    homepage: 'https://www.prizepicks.com',
    referralUrl: undefined, // PASTE YOUR PARTNER/REFERRAL LINK HERE (https://...)
  },
  {
    key: 'hardrockbet',
    name: 'Hard Rock Bet',
    short: 'HRB',
    homepage: 'https://hardrock.bet',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'betrivers',
    name: 'BetRivers',
    short: 'BR',
    homepage: 'https://www.betrivers.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'fanatics',
    name: 'Fanatics Sportsbook',
    short: 'FAN',
    homepage: 'https://sportsbook.fanatics.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'bovada',
    name: 'Bovada',
    short: 'BOV',
    homepage: 'https://www.bovada.lv',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'mybookieag',
    name: 'MyBookie',
    short: 'MB',
    homepage: 'https://www.mybookie.ag',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'betonlineag',
    name: 'BetOnline',
    short: 'BOL',
    homepage: 'https://www.betonline.ag',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'lowvig',
    name: 'LowVig',
    short: 'LV',
    homepage: 'https://www.lowvig.ag',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'pinnacle',
    name: 'Pinnacle',
    short: 'PIN',
    homepage: 'https://www.pinnacle.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'betus',
    name: 'BetUS',
    short: 'BUS',
    homepage: 'https://www.betus.com.pa',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'ballybet',
    name: 'Bally Bet',
    short: 'BLY',
    homepage: 'https://play.ballybet.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'betparx',
    name: 'betPARX',
    short: 'PARX',
    homepage: 'https://betparx.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'fliff',
    name: 'Fliff',
    short: 'FLF',
    homepage: 'https://www.getfliff.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'novig',
    name: 'Novig',
    short: 'NV',
    homepage: 'https://novig.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'prophetx',
    name: 'ProphetX',
    short: 'PX',
    homepage: 'https://www.prophetx.co',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'unibet_us',
    name: 'Unibet',
    short: 'UNI',
    homepage: 'https://www.unibet.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'twinspires',
    name: 'TwinSpires',
    short: 'TS',
    homepage: 'https://www.twinspires.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'wynnbet',
    name: 'WynnBET',
    short: 'WYNN',
    homepage: 'https://www.wynnbet.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'bet365',
    name: 'bet365',
    short: 'B365',
    homepage: 'https://www.bet365.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'superbook',
    name: 'SuperBook',
    short: 'SB',
    homepage: 'https://superbook.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  {
    key: 'circasports',
    name: 'Circa Sports',
    short: 'CIRC',
    homepage: 'https://www.circasports.com',
    referralUrl: undefined, // PASTE YOUR REFERRAL LINK HERE (https://...)
  },
  // Prediction markets — real-money, ~vig-free probabilities; both run
  // referral programs (Kalshi referral codes, Polymarket invite links).
  {
    key: 'polymarket',
    name: 'Polymarket',
    short: 'POLY',
    homepage: 'https://polymarket.com',
    referralUrl: EXCHANGE_SIGNUP.polymarket, // shared/exchange-links.ts
    bonusHeadline: 'Trade Sports on Polymarket',
    bonusDetail: 'The sharpest vig-free prices in Krypt Scout — trade them directly.',
    featured: true,
  },
  {
    // The US-regulated Polymarket exchange — a separate account from
    // polymarket.com. Not quoted on the boards; auto-bet can buy on it.
    key: 'polymarket_us',
    name: 'Polymarket US',
    short: 'PMUS',
    homepage: 'https://polymarket.us',
    referralUrl: EXCHANGE_SIGNUP.polymarket_us, // shared/exchange-links.ts
    bonusHeadline: 'Trade Sports on Polymarket US',
    bonusDetail: 'The US-regulated Polymarket exchange — sign up through Krypt Scout.',
    featured: true,
  },
  {
    key: 'kalshi',
    name: 'Kalshi',
    short: 'KLSH',
    homepage: 'https://kalshi.com',
    referralUrl: EXCHANGE_SIGNUP.kalshi, // shared/exchange-links.ts
    bonusHeadline: 'Trade Sports on Kalshi',
    bonusDetail: 'CFTC-regulated prediction market — sign up through Krypt Scout.',
  },
  // Crypto books (not in the US odds feeds — BONUSES page entries).
  {
    key: 'stake',
    name: 'Stake',
    short: 'STK',
    homepage: 'https://stake.com',
    referralUrl: 'https://stake.com/?c=HuMNm4fZ&offer=edge',
    bonusHeadline: 'Get $25 Free',
    bonusDetail: 'Exclusive Krypt deal — $25 free when you sign up through Krypt Scout.',
    featured: true,
  },
  {
    key: 'bcgame',
    name: 'BC.Game',
    short: 'BC',
    homepage: 'https://bc.game',
    referralUrl: 'https://bc.game/i-2q68hti3q-n',
  },
]

// ------------------------------------------------------------------ lookups
// Both raw Odds API keys ('williamhill_us') and display names the backend may
// pass through ('DraftKings', 'MyBookie.ag') resolve to the same entry.

const normKey = (book: string): string => book.toLowerCase().replace(/[^a-z0-9]/g, '')

const BY_KEY = new Map<string, BookConfig>(REFERRALS.map((b) => [normKey(b.key), b]))
// Also index by display name so demo-slate book labels resolve.
for (const b of REFERRALS) {
  const n = normKey(b.name)
  if (!BY_KEY.has(n)) BY_KEY.set(n, b)
}

/** Config entry for any book identifier, or undefined for unknown books. */
export function bookConfig(book: string): BookConfig | undefined {
  return BY_KEY.get(normKey(book))
}

/** "somebook_us" → "Somebook Us" — readable fallback for unknown keys. */
function prettifyKey(book: string): string {
  return book
    .replace(/[_-]+/g, ' ')
    .trim()
    .replace(/\b[a-z]/g, (c) => c.toUpperCase())
}

/** Display name with graceful fallback for books not in the directory. */
export function bookName(book: string): string {
  return bookConfig(book)?.name ?? prettifyKey(book)
}

/** Short mark with graceful fallback (first 4 alphanumerics, uppercased). */
export function bookShort(book: string): string {
  return (
    bookConfig(book)?.short ?? book.replace(/[^A-Za-z0-9]/g, '').slice(0, 4).toUpperCase()
  )
}

/** The URL a click on this book should open: referral link when set, else the
 *  homepage; unknown books fall back to a web search for the name. */
export function bookLink(book: string): string {
  const cfg = bookConfig(book)
  if (cfg) return cfg.referralUrl ?? cfg.homepage
  return `https://www.google.com/search?q=${encodeURIComponent(`${prettifyKey(book)} sportsbook`)}`
}

/** Open the book in the system browser (Electron) or a new tab (browser dev). */
export function openBook(book: string): void {
  openExternal(bookLink(book))
}
