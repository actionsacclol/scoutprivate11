/**
 * Persisted user preferences — first-run onboarding state + preferred books.
 *
 * Stored in localStorage under a versioned key. Everything degrades
 * gracefully when storage is unavailable (prefs live for the session only).
 * `orderBooks` is the one integration point the rest of the terminal uses:
 * line-shop strips and odds tables pass their entries through it so the
 * user's books float to the front (BEST-price logic is untouched — best is
 * best regardless of preference).
 */
import { useSyncExternalStore } from 'react'
import { bookConfig } from './config/referrals'

export const PREFS_KEY = 'scout.prefs.v1'
export const MAX_PREFERRED_BOOKS = 4

export interface ScoutPrefs {
  /** First-run onboarding completed (or skipped). */
  onboarded: boolean
  /** Post-onboarding tab tour completed (or declined). */
  toured?: boolean
  /** referrals.ts book keys, in preference order, max MAX_PREFERRED_BOOKS. */
  preferredBooks: string[]
  /** UI mode picked during onboarding (uiMode.ts remains the live source). */
  mode?: 'simple' | 'pro'
  /** Units every TRACK action stakes (bets and parlays). 0.1–100, default 1. */
  defaultStake?: number
}

const DEFAULT_PREFS: ScoutPrefs = { onboarded: false, preferredBooks: [] }

export const MIN_STAKE = 0.1
export const MAX_STAKE = 100

/** Clamp any candidate stake to the backend's accepted range. */
export function clampStake(v: number): number {
  if (!Number.isFinite(v)) return 1
  return Math.min(Math.max(Math.round(v * 10) / 10, MIN_STAKE), MAX_STAKE)
}

/** The stake TRACK buttons use — the user's default, falling back to 1u. */
export function defaultStake(): number {
  return loadPrefs().defaultStake ?? 1
}

export function saveDefaultStake(v: number): void {
  savePrefs({ ...loadPrefs(), defaultStake: clampStake(v) })
}

/** House default when the user hasn't picked books: the prediction markets
 *  lead (sharpest ~vig-free prices), Polymarket first. */
export const DEFAULT_BOOK_ORDER = ['polymarket', 'kalshi']

/** The user's preferred books, falling back to the house default. */
export function effectivePreferredBooks(): string[] {
  const pref = loadPrefs().preferredBooks
  return pref.length > 0 ? pref : DEFAULT_BOOK_ORDER
}

// ------------------------------------------------------------- store + hook
// Module-level cache keeps loadPrefs() cheap enough to call from render, and
// a tiny external store lets components re-render when prefs change (e.g.
// the PICKS grid re-orders the moment onboarding finishes).

let cache: ScoutPrefs | null = null
let version = 0
const listeners = new Set<() => void>()

function emit(): void {
  version += 1
  for (const l of listeners) l()
}

/** Subscribe a component to prefs changes — call it, ignore the value. */
export function usePrefsVersion(): number {
  return useSyncExternalStore(
    (cb) => {
      listeners.add(cb)
      return () => listeners.delete(cb)
    },
    () => version,
  )
}

function sanitize(raw: unknown): ScoutPrefs {
  if (typeof raw !== 'object' || raw === null) return { ...DEFAULT_PREFS }
  const r = raw as Record<string, unknown>
  const preferredBooks = Array.isArray(r.preferredBooks)
    ? r.preferredBooks
        .filter((b): b is string => typeof b === 'string' && b.length > 0)
        .slice(0, MAX_PREFERRED_BOOKS)
    : []
  return {
    onboarded: r.onboarded === true,
    toured: r.toured === true ? true : undefined,
    preferredBooks,
    mode: r.mode === 'pro' || r.mode === 'simple' ? r.mode : undefined,
    defaultStake:
      typeof r.defaultStake === 'number' && Number.isFinite(r.defaultStake)
        ? clampStake(r.defaultStake)
        : undefined,
  }
}

export function loadPrefs(): ScoutPrefs {
  if (cache) return cache
  try {
    const raw = localStorage.getItem(PREFS_KEY)
    cache = raw ? sanitize(JSON.parse(raw)) : { ...DEFAULT_PREFS }
  } catch {
    cache = { ...DEFAULT_PREFS }
  }
  return cache
}

export function savePrefs(prefs: ScoutPrefs): void {
  cache = sanitize(prefs)
  try {
    localStorage.setItem(PREFS_KEY, JSON.stringify(cache))
  } catch {
    /* storage unavailable — prefs still apply for the session */
  }
  emit()
}

/** Re-arm the first-run overlay; other prefs are kept.
 *  (The tab tour is NOT re-armed — that's resetTour, its own ABOUT button.) */
export function resetOnboarding(): void {
  savePrefs({ ...loadPrefs(), onboarded: false })
}

/** Re-arm the post-onboarding tab tour (TourGuide.tsx); ABOUT page hook. */
export function resetTour(): void {
  savePrefs({ ...loadPrefs(), toured: false })
}

// -------------------------------------------------------- preferred ordering

/** Canonical form for any book identifier ('DraftKings' and 'draftkings'
 *  both resolve to the referrals.ts key when the book is known). */
function canon(book: string): string {
  return bookConfig(book)?.key ?? book.toLowerCase().replace(/[^a-z0-9]/g, '')
}

/** True when the book is one of the user's preferred books (or the
 *  Polymarket/Kalshi house default when none are picked). */
export function isPreferredBook(book: string): boolean {
  const k = canon(book)
  return effectivePreferredBooks().some((b) => canon(b) === k)
}

/**
 * Stable re-order: preferred books first (in the user's preference order,
 * defaulting to Polymarket → Kalshi), everything else after in the entries'
 * original order.
 */
export function orderBooks<T>(entries: T[], keyOf: (t: T) => string): T[] {
  const pref = effectivePreferredBooks()
  if (pref.length === 0 || entries.length === 0) return entries
  const rank = new Map<string, number>()
  pref.forEach((b, i) => {
    const k = canon(b)
    if (!rank.has(k)) rank.set(k, i)
  })
  const preferred: Array<{ t: T; r: number; i: number }> = []
  const rest: T[] = []
  entries.forEach((t, i) => {
    const r = rank.get(canon(keyOf(t)))
    if (r === undefined) rest.push(t)
    else preferred.push({ t, r, i })
  })
  if (preferred.length === 0) return entries
  preferred.sort((a, b) => a.r - b.r || a.i - b.i)
  return [...preferred.map((p) => p.t), ...rest]
}
