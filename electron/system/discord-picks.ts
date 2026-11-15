import { randomUUID } from 'node:crypto';
import type {
  DiscordConfig,
  DiscordInfo,
  DiscordPreview,
  DiscordPreviewPick,
  DiscordPreviewQuote,
  DiscordSend,
  DiscordWebhook,
  LogEntry,
} from '../../shared/types';
import { EXCHANGE_SIGNUP } from '../../shared/exchange-links';
import { pythonBackend } from './python-backend';
import { scheduler } from './scheduler';
import * as store from './settings-store';

// ──────────────────────────────────────────────────────────────────────
// Discord picks sender. Posts the day's card — a short list chosen from the
// recorded picks (GET /api/picks, then POST /api/picks/card on the supervised
// backend), each with its price on the exchanges, every exchange name a
// sign-up link —
// to user-configured Discord webhooks from the main process (the renderer CSP blocks discord.com; main has no such
// limit). One embed payload is built per send and POSTed to every
// configured webhook sequentially.
//
// Guarantees (mirrors the scheduler):
//  • Sends never overlap — everything funnels through one serial queue,
//    and a send that is already queued/in-flight is not queued again.
//  • Sends never throw — every outcome (backend down, picks endpoint not
//    deployed, webhook 4xx/timeout) is recorded into a last-20 ring.
//  • The daily timer is re-armed after every fire / config change and
//    cleared on disable and on app quit (main.ts calls stop()).
//
// Dev/testing: KRYPT_SCOUT_DISCORD_FAST=1 additionally accepts "HH:MM:SS"
// for sendAt so the auto-timer can be observed firing within seconds.
// ──────────────────────────────────────────────────────────────────────

const FAST = process.env.KRYPT_SCOUT_DISCORD_FAST === '1';
/** Hard cap on the picks fetch from the local backend. */
const PICKS_TIMEOUT_MS = 15_000;
/** Hard cap on a single webhook POST. */
const WEBHOOK_TIMEOUT_MS = 10_000;
/** Longest we are willing to honor a 429 retry_after for. */
const RETRY_AFTER_CAP_MS = 15_000;
/** Ring size for lastSends. */
const RING_MAX = 20;
const EMBED_COLOR = 0xe11d48;
/** Where readers can get the terminal — the embed title links here. */
const TOOL_URL = 'https://krypt.cc/tools/scout';

const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const WEBHOOK_URL_PREFIXES = [
  'https://discord.com/api/webhooks/',
  'https://discordapp.com/api/webhooks/',
];

type SendReason = 'manual' | 'auto';

/** Hard cap on building the card. Longer than the picks fetch: it can rebuild
 *  a board and makes one Polymarket US lookup per pick it keeps. */
const CARD_TIMEOUT_MS = 45_000;
/** Picks per post: the default, and the most the setting allows. */
const DEFAULT_MAX_PICKS = 6;
const MAX_PICKS_CAP = 12;
/** Discord's limit on an embed description (4096), with room to spare. */
const DESCRIPTION_MAX = 4000;
/** The record lookup and the posted-card write are local and tiny. */
const RECORD_TIMEOUT_MS = 10_000;

/** The exchanges listed under each pick, in the order they are shown, each
 *  with the sign-up link its name carries in the post. These are the
 *  publisher's referral links (shared/exchange-links.ts, the same ones the
 *  app's own buttons use); the post never links a market page. */
const EXCHANGES: [key: string, label: string, signup: string][] = [
  ['polymarket', 'Polymarket', EXCHANGE_SIGNUP.polymarket],
  ['kalshi', 'Kalshi', EXCHANGE_SIGNUP.kalshi],
  ['polymarket_us', 'Polymarket US', EXCHANGE_SIGNUP.polymarket_us],
];
const SIGNUPS = EXCHANGES.map(([, label, url]) => ({ label, url }));
/** Leads the sign-up line that closes every post. */
const SIGNUP_LEAD = 'Free bonus when you sign up:';
/** The embed footer: the legal line and nothing else — no branding. It also
 *  discloses that the sign-up links are referral links. */
const LEGAL_FOOTER =
  'Not financial or betting advice · 18+ (21+ where required), only where legal · ' +
  'Sign-up links are referral links · Gambling problem? Call 1-800-GAMBLER';

/** One pick: the GET /api/picks row, plus what POST /api/picks/card adds —
 *  `prob` (win probability, 0-1), `exchanges` ({venue: {price, cost}})
 *  and `best` (the cheapest venue after fees). All defensively optional. */
interface ApiPick {
  game_id?: unknown;
  team?: unknown;
  side?: unknown;
  price?: unknown;
  book?: unknown;
  ev?: unknown;
  tier?: unknown;
  home?: unknown;
  away?: unknown;
  league?: unknown;
  sport?: unknown;
  kickoff?: unknown;
  prob?: unknown;
  fair_prob?: unknown;
  model_prob?: unknown;
  exchanges?: unknown;
  best?: unknown;
}

function defaults(): DiscordConfig {
  return {
    enabled: false,
    sendAt: '11:00',
    tiers: ['STRONG', 'LEAN', 'MODEL'],
    maxPicks: DEFAULT_MAX_PICKS,
    webhooks: [],
  };
}

function sanitizeMaxPicks(v: unknown, base: number): number {
  const n = Math.round(Number(v));
  return Number.isFinite(n) && n >= 1 ? Math.min(n, MAX_PICKS_CAP) : base;
}

function isValidWebhookUrl(url: unknown): url is string {
  return typeof url === 'string' && WEBHOOK_URL_PREFIXES.some((p) => url.startsWith(p));
}

/** Parse "HH:MM" (plus "HH:MM:SS" in FAST mode). Null when invalid. */
function parseSendAt(v: unknown): { h: number; m: number; s: number } | null {
  if (typeof v !== 'string') return null;
  const m = FAST
    ? /^([01]?\d|2[0-3]):([0-5]\d)(?::([0-5]\d))?$/.exec(v.trim())
    : /^([01]?\d|2[0-3]):([0-5]\d)$/.exec(v.trim());
  if (!m) return null;
  return { h: Number(m[1]), m: Number(m[2]), s: m[3] ? Number(m[3]) : 0 };
}

function sanitizeTiers(v: unknown, base: string[]): string[] {
  if (!Array.isArray(v)) return base;
  const out: string[] = [];
  for (const t of v) {
    if (typeof t !== 'string') continue;
    const up = t.trim().toUpperCase();
    if (up && !out.includes(up)) out.push(up);
  }
  return out.length ? out : base;
}

/**
 * Validate + normalize an incoming webhooks array. THROWS on a URL that is
 * not a Discord webhook — setConfig surfaces that rejection over IPC so bad
 * URLs are never persisted.
 */
function sanitizeWebhooks(v: unknown, base: DiscordWebhook[]): DiscordWebhook[] {
  if (v === undefined) return base;
  if (!Array.isArray(v)) throw new Error('webhooks must be an array');
  const out: DiscordWebhook[] = [];
  for (const raw of v) {
    const w = (raw ?? {}) as Partial<DiscordWebhook>;
    if (!isValidWebhookUrl(w.url)) {
      throw new Error(
        'invalid webhook URL — must start with https://discord.com/api/webhooks/ (or discordapp.com)',
      );
    }
    out.push({
      id: typeof w.id === 'string' && w.id ? w.id : randomUUID(),
      name: typeof w.name === 'string' ? w.name : '',
      url: w.url,
    });
  }
  return out;
}

/** Merge + validate a config patch. Throws on invalid webhook URLs. */
function sanitize(partial: Partial<DiscordConfig> | undefined, base: DiscordConfig): DiscordConfig {
  const p = partial ?? {};
  const sendAt =
    p.sendAt !== undefined && parseSendAt(p.sendAt) !== null
      ? (p.sendAt as string).trim()
      : base.sendAt;
  return {
    enabled: typeof p.enabled === 'boolean' ? p.enabled : base.enabled,
    sendAt,
    tiers: p.tiers !== undefined ? sanitizeTiers(p.tiers, base.tiers) : [...base.tiers],
    maxPicks: sanitizeMaxPicks(p.maxPicks, sanitizeMaxPicks(base.maxPicks, DEFAULT_MAX_PICKS)),
    webhooks: sanitizeWebhooks(p.webhooks, base.webhooks.map((w) => ({ ...w }))),
  };
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

function pad2(n: number): string {
  return String(n).padStart(2, '0');
}

/** Local calendar date as yyyy-mm-dd (NB: the backend keys picks by UTC day — see fetchTodaysPicks). */
function localDateStr(d = new Date()): string {
  return `${d.getFullYear()}-${pad2(d.getMonth() + 1)}-${pad2(d.getDate())}`;
}

function formatPrice(v: unknown): string {
  const n = Number(v);
  if (!Number.isFinite(n)) return '?';
  const r = Math.round(n);
  return r > 0 ? `+${r}` : `${r}`;
}

function formatEv(v: unknown): string {
  // The backend serves EV as a 0-1 fraction of the stake (0.042 = +4.2%).
  const n = Number(v) * 100;
  if (!Number.isFinite(n)) return '+?';
  return n >= 0 ? `+${n.toFixed(1)}` : n.toFixed(1);
}

/** Sport tag prefixes — mirrors SPORT_TAG in src/types.ts. */
const SPORT_TAGS: Record<string, string> = {
  nba: 'NBA', nhl: 'NHL', nfl: 'NFL', ncaaf: 'CFB', ncaab: 'CBB',
  mlb: 'MLB', wnba: 'WNBA', soccer: 'SOC', tennis: 'TEN',
};

function sportTag(v: unknown): string {
  const s = typeof v === 'string' ? v.trim().toLowerCase() : '';
  return SPORT_TAGS[s] ?? (s ? s.toUpperCase().slice(0, 4) : '?');
}

function text(v: unknown): string {
  return typeof v === 'string' ? v.trim() : '';
}

/** "68% to win", or "" when the pick carries no probability. The card serves
 *  `prob` (the exchange fair, else the model's). Without it — an older
 *  backend — a value pick's is recovered from its own numbers: EV = p·d − 1
 *  for decimal price d, so p = (1+EV)/d. Nothing is printed rather than a
 *  placeholder: "?% to win" tells a reader nothing. */
function winChance(p: ApiPick): string {
  const prob = pickProb(p);
  return prob === null ? '' : `${Math.round(prob * 100)}% to win`;
}

/** The pick's win probability (0-1), or null when nothing gives one: the
 *  card's `prob`, else the exchange fair, else the model's, else recovered
 *  from a value pick's price and EV. */
function pickProb(p: ApiPick): number | null {
  for (const raw of [p.prob, p.fair_prob, p.model_prob]) {
    const prob = typeof raw === 'number' ? raw : NaN;
    if (prob > 0 && prob < 1) return prob;
  }
  const price = Number(p.price);
  const ev = Number(p.ev);
  if (!Number.isFinite(price) || Math.abs(price) < 100 || !Number.isFinite(ev)) return null;
  const decimal = price > 0 ? 1 + price / 100 : 1 + 100 / Math.abs(price);
  const prob = (1 + ev) / decimal;
  return prob > 0 && prob < 1 ? prob : null;
}

/** Only picks between 50% and 85% are ever posted — the backend's card rule
 *  (odds/compare.py CARD_PROB_MIN / CARD_PROB_MAX), repeated here so the
 *  fallback path cannot send a 95% lock or a pick with no number at all. */
const CARD_PROB_MIN = 0.5;
const CARD_PROB_MAX = 0.85;
function inBand(p: ApiPick): boolean {
  const prob = pickProb(p);
  return prob !== null && prob >= CARD_PROB_MIN && prob <= CARD_PROB_MAX;
}

/** A share price (0-1) in cents: "58¢", or "57.5¢" when it is not whole. */
function cents(prob: number): string {
  const c = Math.round(prob * 1000) / 10;
  return `${Number.isInteger(c) ? c : c.toFixed(1)}¢`;
}

/** What each exchange charges for the pick's side, in listing order. `best`
 *  marks the cheapest AFTER its fee — the three charge different fees, so the
 *  lowest sticker is not always the lowest cost. Screen prices are shown (what
 *  the reader will see when they open the exchange). Each carries that
 *  exchange's sign-up link, never a market page. */
function exchangeQuotes(p: ApiPick): DiscordPreviewQuote[] {
  const quotes =
    p.exchanges && typeof p.exchanges === 'object' ? (p.exchanges as Record<string, unknown>) : {};
  const listed: { key: string; label: string; price: number; cost: number; url: string }[] = [];
  for (const [key, label, url] of EXCHANGES) {
    const q = (quotes[key] ?? {}) as Record<string, unknown>;
    const price = Number(q.price);
    if (!(price > 0 && price < 1)) continue;
    const cost = Number(q.cost);
    listed.push({ key, label, price, cost: Number.isFinite(cost) && cost >= price ? cost : price, url });
  }
  if (listed.length === 0) return [];
  const best = listed.reduce((a, b) => (b.cost < a.cost ? b : a)).key;
  return listed.map((q) => ({ label: q.label, price: cents(q.price), url: q.url, best: q.key === best }));
}

/** One pick as the post shows it — the single source for both the Discord
 *  markdown (buildPayload) and the in-app preview, so they cannot drift. */
export function describePick(p: ApiPick): DiscordPreviewPick {
  const tier = text(p.tier).toUpperCase();
  const side = text(p.side);
  const opponent = side === 'home' ? text(p.away) : side === 'away' ? text(p.home) : '';
  const parts: string[] = [];
  if (tier === 'MODEL') {
    parts.push('ML');
    if (opponent) parts.push(`vs ${opponent}`);
  } else {
    parts.push(`ML ${formatPrice(p.price)} @ ${text(p.book) || '—'}`);
    parts.push(`EV ${formatEv(p.ev)}%`);
  }
  const chance = winChance(p);
  if (chance) parts.push(chance);
  const kick = typeof p.kickoff === 'string' ? Date.parse(p.kickoff) : NaN;
  return {
    key: `${text(p.game_id)}|${side}`,
    tag: sportTag(p.sport),
    team: text(p.team) || 'Unknown',
    tier,
    detail: parts.join('  ·  '),
    kickoff: Number.isFinite(kick) ? new Date(kick).toISOString() : null,
    exchanges: exchangeQuotes(p),
  };
}

/** The markdown lines for one pick. No emoji, per suite convention. The
 *  kickoff is a Discord timestamp, so every reader sees their own local time.
 *  Every exchange's price is its sign-up link; the cheapest is bold when
 *  there is more than one to compare. */
function pickLines(d: DiscordPreviewPick, links: boolean): { lines: string[]; bold: boolean } {
  const when = d.kickoff ? `  ·  <t:${Math.floor(Date.parse(d.kickoff) / 1000)}:t>` : '';
  const lines = [`\`${d.tag}\` **${d.team.toUpperCase()}** ${d.detail}${when}`];
  let bold = false;
  if (d.exchanges.length > 0) {
    const compared = d.exchanges.length > 1;
    const parts = d.exchanges.map((q) => {
      let label = `${q.label} ${q.price}`;
      if (links) label = `[${label}](${q.url})`;
      if (q.best && compared) {
        label = `**${label}**`;
        bold = true;
      }
      return label;
    });
    lines.push(`> ${parts.join('  ·  ')}`);
  }
  return { lines, bold };
}

/** The sign-up line every post carries, picks or not. */
/** GET /api/picks/card/record — how the posted cards have done. */
export interface CardRecord {
  overall?: { wins?: number; losses?: number; pushes?: number; units?: number; since?: string | null };
  last?: { date?: string; wins?: number; losses?: number; pushes?: number } | null;
}

/** "Nov 14" from "2026-11-14"; "" when it is not a date. */
function shortDate(iso: unknown): string {
  const m = typeof iso === 'string' ? /^(\d{4})-(\d{2})-(\d{2})$/.exec(iso) : null;
  const month = m ? MONTHS[Number(m[2]) - 1] : undefined;
  return m && month ? `${month} ${Number(m[3])}` : '';
}

function winLoss(t: { wins?: number; losses?: number; pushes?: number }): string {
  const base = `${t.wins ?? 0}–${t.losses ?? 0}`;
  return (t.pushes ?? 0) > 0 ? `${base}–${t.pushes}` : base;
}

/**
 * The record line a post carries, as plain text (the post bolds its label):
 * "Record 12–6 (67%) · +1.4u since Nov 14 · last card (Nov 14) 4–2". Every
 * posted pick counts, one unit each at the cheapest exchange price after
 * fees — the losing days print too. "" until a posted pick has been graded.
 */
export function recordLine(record: CardRecord | null | undefined): string {
  const o = record?.overall;
  const wins = o?.wins ?? 0;
  const losses = o?.losses ?? 0;
  if (!o || wins + losses === 0) return '';
  const parts = [`Record ${winLoss(o)} (${Math.round((wins / (wins + losses)) * 100)}%)`];
  const units = Number(o.units);
  if (Number.isFinite(units)) {
    const since = shortDate(o.since);
    parts.push(`${units >= 0 ? '+' : '−'}${Math.abs(units).toFixed(1)}u${since ? ` since ${since}` : ''}`);
  }
  const last = record?.last;
  if (last && (last.wins ?? 0) + (last.losses ?? 0) + (last.pushes ?? 0) > 0) {
    const day = shortDate(last.date);
    parts.push(`last card${day ? ` (${day})` : ''} ${winLoss(last)}`);
  }
  return parts.join(' · ');
}

function signupLine(): string {
  return `${SIGNUP_LEAD} ${EXCHANGES.map(([, label, url]) => `[${label}](${url})`).join('  ·  ')}`;
}

function describeBody(picks: DiscordPreviewPick[], candidates: number, links: boolean): string {
  const lines: string[] = [];
  let bold = false;
  for (const d of picks) {
    const one = pickLines(d, links);
    lines.push(...one.lines);
    bold = bold || one.bold;
  }
  if (lines.length === 0) return 'No qualifying picks today.';
  const notes: string[] = [];
  if (picks.some((d) => d.exchanges.length > 0)) {
    notes.push('Prices to buy that side on each exchange.' + (bold ? ' Bold = cheapest after fees.' : ''));
  }
  if (candidates > picks.length) {
    notes.push(`${picks.length} of ${candidates} games today — the full board is in Krypt Scout.`);
  }
  return notes.length ? `${lines.join('\n')}\n\n*${notes.join(' ')}*` : lines.join('\n');
}

/** Build the single Discord webhook JSON payload for a card of picks. The
 *  title links to krypt.cc/tools/scout; the description closes with the
 *  exchanges' sign-up links; the footer is the legal line and nothing else.
 *  No `username` or avatar is sent: the post appears under whatever name and
 *  picture the server's owner gave the webhook. `candidates` is how many
 *  games the card was chosen from. */
export function buildPayload(
  picks: ApiPick[],
  now = new Date(),
  candidates = picks.length,
  record = '',
): Record<string, unknown> {
  const shown = picks.slice(0, MAX_PICKS_CAP).map(describePick);
  const links = signupLine();
  // The record sits between the picks and the links, its label in bold:
  // "**Record:** 12–6 (67%) · …".
  const tail = record ? `${record.replace(/^Record /, '**Record:** ')}\n\n${links}` : links;
  let body = describeBody(shown, candidates, true);
  // A post that would not fit keeps the sign-up line and unlinks the prices.
  if (body.length + tail.length + 2 > DESCRIPTION_MAX) body = describeBody(shown, candidates, false);
  return {
    embeds: [
      {
        title: `TODAY'S PICKS — ${MONTHS[now.getMonth()]} ${now.getDate()}`,
        url: TOOL_URL,
        color: EMBED_COLOR,
        description: `${body}\n\n${tail}`,
        footer: { text: LEGAL_FOOTER },
        timestamp: now.toISOString(),
      },
    ],
  };
}

function sleep(ms: number): Promise<void> {
  return new Promise((r) => setTimeout(r, ms));
}

/** Best-effort 429 retry delay: JSON body retry_after (s) → header → 1.5s. */
async function retryAfterMs(res: Response): Promise<number> {
  let seconds: number | null = null;
  try {
    const body: unknown = await res.clone().json();
    if (body && typeof body === 'object') {
      const ra = (body as Record<string, unknown>).retry_after;
      if (typeof ra === 'number' && Number.isFinite(ra)) seconds = ra;
    }
  } catch {
    /* not JSON */
  }
  if (seconds === null) {
    const h = Number(res.headers.get('retry-after') ?? res.headers.get('x-ratelimit-reset-after'));
    if (Number.isFinite(h)) seconds = h;
  }
  const ms = Math.round((seconds ?? 1.5) * 1000);
  return Math.min(Math.max(ms, 0), RETRY_AFTER_CAP_MS);
}

/** POST one payload to one webhook; honors a single 429 retry. Never throws. */
async function postToWebhook(
  url: string,
  payload: Record<string, unknown>,
): Promise<{ ok: boolean; error?: string }> {
  for (let attempt = 0; attempt < 2; attempt++) {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), WEBHOOK_TIMEOUT_MS);
    try {
      const res = await fetch(url, {
        method: 'POST',
        headers: { 'content-type': 'application/json' },
        body: JSON.stringify(payload),
        signal: ctl.signal,
      });
      if (res.status === 429 && attempt === 0) {
        const wait = await retryAfterMs(res);
        clearTimeout(timer);
        await sleep(wait);
        continue;
      }
      if (res.ok) return { ok: true };
      const body = await res.text().catch(() => '');
      return { ok: false, error: `HTTP ${res.status}: ${truncate(body.trim() || res.statusText, 120)}` };
    } catch (e: unknown) {
      const msg = ctl.signal.aborted
        ? `timed out after ${WEBHOOK_TIMEOUT_MS / 1000}s`
        : e instanceof Error
          ? e.message
          : String(e);
      return { ok: false, error: msg };
    } finally {
      clearTimeout(timer);
    }
  }
  return { ok: false, error: 'rate limited (429) after retry' };
}

type LogCallback = (entry: LogEntry) => void;

class DiscordPicks {
  private config: DiscordConfig = defaults();
  private lastSends: DiscordSend[] = [];
  /** Serial execution queue: every send chains onto this promise. */
  private chain: Promise<void> = Promise.resolve();
  /** No-overlap / no-double-queue guard. */
  private sendPending = false;
  private timer: NodeJS.Timeout | null = null;
  private nextAt: number | null = null;
  private stopped = false;
  private logHandlers: LogCallback[] = [];

  // ─── Public API ────────────────────────────────────────────────────

  /** Load persisted config and arm the daily timer if applicable. */
  init(): void {
    try {
      this.config = sanitize(store.get().discord, defaults());
    } catch {
      // A hand-edited settings.json with a bad webhook URL must not brick
      // startup — fall back to defaults (bad entry is dropped on next save).
      this.config = defaults();
    }
    this.armTimer();
    this.log(
      'INFO',
      `discord picks ready (enabled=${this.config.enabled}, sendAt=${this.config.sendAt}, ` +
        `${this.config.webhooks.length} webhook(s)${FAST ? ', FAST mode' : ''})`,
    );
  }

  info(): DiscordInfo {
    return {
      config: {
        ...this.config,
        tiers: [...this.config.tiers],
        webhooks: this.config.webhooks.map((w) => ({ ...w })),
      },
      lastSends: [...this.lastSends],
      nextSend: this.nextAt ? new Date(this.nextAt).toISOString() : null,
    };
  }

  /** Merge a config patch. Throws (rejects over IPC) on invalid webhook URLs. */
  setConfig(partial: Partial<DiscordConfig>): DiscordInfo {
    const prev = this.config;
    this.config = sanitize(partial, prev); // throws before any state changes
    store.save({ ...store.get(), discord: { ...this.config, tiers: [...this.config.tiers], webhooks: this.config.webhooks.map((w) => ({ ...w })) } });
    this.armTimer();
    if (prev.enabled && !this.config.enabled) {
      this.log('INFO', 'discord auto-post disabled — timer cleared');
    } else if (this.config.enabled && this.nextAt) {
      this.log('INFO', `discord auto-post armed for ${new Date(this.nextAt).toLocaleString()}`);
    }
    return this.info();
  }

  /**
   * Manual trigger. Queues a send (unless one is already queued/running)
   * and resolves with fresh info once that send has finished. A send runs
   * the picks job first (see execute), so this is run-and-send in one.
   */
  async sendNow(): Promise<DiscordInfo> {
    await this.enqueue('manual');
    return this.info();
  }

  /** Post today's REAL picks embed to one webhook URL — the exact payload
   *  the daily auto-post sends, so a test shows what subscribers will see
   *  (an empty slate posts the "No qualifying picks" embed). Never throws. */
  async test(url: string): Promise<{ ok: boolean; error?: string }> {
    if (!isValidWebhookUrl(url)) {
      return {
        ok: false,
        error: 'invalid webhook URL — must start with https://discord.com/api/webhooks/ (or discordapp.com)',
      };
    }
    const fetched = await this.fetchTodaysPicks();
    if ('error' in fetched) return { ok: false, error: fetched.error };
    const { card, candidates } = await this.fetchCard(fetched.picks);
    // A test shows the record line but is not a send: nothing is recorded.
    return postToWebhook(url, buildPayload(card, new Date(), candidates, await this.fetchRecord()));
  }

  /** Exactly what a send would post right now, as data: the page's preview
   *  is drawn from this, so it is the real card and not a facsimile. Posts
   *  nothing. Never throws. */
  async preview(): Promise<DiscordPreview> {
    const fetched = await this.fetchTodaysPicks();
    if ('error' in fetched) {
      return { picks: [], candidates: 0, signups: SIGNUPS, signupLead: SIGNUP_LEAD, footer: LEGAL_FOOTER, record: '', error: fetched.error };
    }
    const { card, candidates } = await this.fetchCard(fetched.picks);
    return {
      picks: card.slice(0, MAX_PICKS_CAP).map(describePick),
      candidates,
      signups: SIGNUPS,
      signupLead: SIGNUP_LEAD,
      footer: LEGAL_FOOTER,
      record: await this.fetchRecord(),
    };
  }

  /** Clear the daily timer. Called from main.ts on before-quit. */
  stop(): void {
    this.stopped = true;
    this.clearTimer();
  }

  onLog(cb: LogCallback): () => void {
    this.logHandlers.push(cb);
    return () => {
      this.logHandlers = this.logHandlers.filter((c) => c !== cb);
    };
  }

  // ─── Internals ─────────────────────────────────────────────────────

  /**
   * Today's UPCOMING picks (local calendar date, configured tiers) from the
   * local backend — every one recorded, before the card is cut (fetchCard).
   * Shared by real sends, the preview AND the TEST button — a test posts
   * the exact payload subscribers get, never a placeholder message.
   *
   * The backend buckets picks by the UTC date of kickoff, but "today's
   * picks" means the LOCAL slate — and a local evening slate spans two UTC
   * dates (a 7pm PT tip-off is 02:00 UTC tomorrow). So this queries the
   * adjacent UTC dates too, then keeps only picks whose kickoff lands on
   * today's local date AND is still in the future: started/final games are
   * never posted (stale odds, unbettable picks — a dead Polymarket book is
   * also where the absurd-EV lines come from). A pick with no parseable
   * kickoff is dropped for the same reason: can't prove it's pregame.
   */
  private async fetchTodaysPicks(): Promise<{ picks: ApiPick[] } | { error: string }> {
    // Current port at call time — a restarted backend on a new port just works.
    const backend = pythonBackend.info();
    if (backend.status !== 'running' || backend.port === null) {
      return { error: `backend not running (status=${backend.status}) — send skipped` };
    }
    const tiers = this.config.tiers.map((t) => t.toUpperCase());
    const dates = [-1, 0, 1].map((offset) => {
      const d = new Date();
      d.setDate(d.getDate() + offset);
      return localDateStr(d);
    });
    const all: ApiPick[] = [];
    for (const date of dates) {
      const url =
        `http://127.0.0.1:${backend.port}/api/picks` +
        `?date=${date}&tiers=${encodeURIComponent(tiers.join(','))}`;
      const ctl = new AbortController();
      const timer = setTimeout(() => ctl.abort(), PICKS_TIMEOUT_MS);
      try {
        const token = pythonBackend.token();
        const res = await fetch(url, {
          headers: token ? { 'X-Scout-Token': token } : undefined,
          signal: ctl.signal,
        });
        if (res.status === 404) {
          return { error: 'picks endpoint not available (404) — this backend build does not ship /api/picks yet' };
        }
        if (!res.ok) {
          const body = await res.text().catch(() => '');
          return { error: `picks fetch failed — HTTP ${res.status}: ${truncate(body.trim() || res.statusText, 120)}` };
        }
        const data: unknown = await res.json();
        const arr = data && typeof data === 'object' ? (data as Record<string, unknown>).picks : null;
        if (!Array.isArray(arr)) {
          return { error: 'picks fetch failed — unexpected response shape (no picks array)' };
        }
        all.push(...(arr as ApiPick[]));
      } catch (e: unknown) {
        const msg = ctl.signal.aborted
          ? `picks fetch timed out after ${PICKS_TIMEOUT_MS / 1000}s`
          : e instanceof Error
            ? e.message
            : String(e);
        return { error: msg };
      } finally {
        clearTimeout(timer);
      }
    }
    const nowMs = Date.now();
    const today = localDateStr();
    // A game has exactly one UTC date, so the three queries can't overlap.
    const picks = all.filter((p) => {
      if (typeof p.tier !== 'string' || !tiers.includes(p.tier.toUpperCase())) return false;
      const kick = typeof p.kickoff === 'string' ? Date.parse(p.kickoff) : NaN;
      if (!Number.isFinite(kick) || kick <= nowMs) return false;
      return localDateStr(new Date(kick)) === today;
    });
    return { picks };
  }

  /** One short JSON call to the local backend; null on any failure. */
  private async backendJson(path: string, body?: unknown): Promise<unknown> {
    const backend = pythonBackend.info();
    if (backend.status !== 'running' || backend.port === null) return null;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), RECORD_TIMEOUT_MS);
    try {
      const token = pythonBackend.token();
      const res = await fetch(`http://127.0.0.1:${backend.port}${path}`, {
        method: body === undefined ? 'GET' : 'POST',
        headers: {
          ...(body === undefined ? {} : { 'content-type': 'application/json' }),
          ...(token ? { 'X-Scout-Token': token } : {}),
        },
        body: body === undefined ? undefined : JSON.stringify(body),
        signal: ctl.signal,
      });
      return res.ok ? await res.json() : null;
    } catch {
      return null;
    } finally {
      clearTimeout(timer);
    }
  }

  /** The record line for today's post; "" when there is none yet or the
   *  backend cannot say (the post goes out without it). */
  private async fetchRecord(): Promise<string> {
    const data = await this.backendJson(`/api/picks/card/record?today=${localDateStr()}`);
    return recordLine(data as CardRecord | null);
  }

  /** Write down the card that was just posted, so it can be graded. */
  private async saveCard(picks: ApiPick[]): Promise<void> {
    if (picks.length === 0) return;
    const saved = await this.backendJson('/api/picks/card/posted', { date: localDateStr(), picks });
    if (saved === null) this.log('WARN', 'posted card was not recorded — it will be missing from the record');
  }

  /**
   * The day's card: the short list actually posted, chosen and priced by the
   * backend (POST /api/picks/card) from every pick recorded for today — one
   * per game, value picks first, then the favourites worth backing, each with
   * its price on the three exchanges.
   *
   * If the backend cannot build it (an older build without the endpoint, a
   * timeout) the post still goes out: one pick per game between 50% and
   * 85%, the first `maxPicks`, no exchange prices. Never throws.
   */
  private async fetchCard(picks: ApiPick[]): Promise<{ card: ApiPick[]; candidates: number }> {
    const limit = this.config.maxPicks;
    const seen = new Set<string>();
    const unique = picks.filter((p) => {
      const id = text(p.game_id);
      if (!id || seen.has(id) || !inBand(p)) return false;
      seen.add(id);
      return true;
    });
    const fallback = { card: unique.slice(0, limit), candidates: unique.length };
    const backend = pythonBackend.info();
    if (picks.length === 0 || backend.status !== 'running' || backend.port === null) return fallback;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), CARD_TIMEOUT_MS);
    try {
      const token = pythonBackend.token();
      const res = await fetch(`http://127.0.0.1:${backend.port}/api/picks/card`, {
        method: 'POST',
        headers: {
          'content-type': 'application/json',
          ...(token ? { 'X-Scout-Token': token } : {}),
        },
        body: JSON.stringify({ picks, limit }),
        signal: ctl.signal,
      });
      if (!res.ok) {
        this.log('WARN', `daily card unavailable (HTTP ${res.status}) — posting the first ${limit} picks`);
        return fallback;
      }
      const data = (await res.json()) as Record<string, unknown> | null;
      const card = data && Array.isArray(data.card) ? (data.card as ApiPick[]) : null;
      if (!card) return fallback;
      const candidates = Number(data?.candidates);
      // The backend's card is not re-filtered: it may keep a pick one
      // exchange has just over 85% because another sells it at 85¢ or under.
      return { card, candidates: Number.isFinite(candidates) ? candidates : card.length };
    } catch (e: unknown) {
      const why = ctl.signal.aborted
        ? `timed out after ${CARD_TIMEOUT_MS / 1000}s`
        : e instanceof Error
          ? e.message
          : String(e);
      this.log('WARN', `daily card unavailable (${why}) — posting the first ${limit} picks`);
      return fallback;
    } finally {
      clearTimeout(timer);
    }
  }

  private log(level: string, msg: string): void {
    const entry: LogEntry = {
      ts: new Date().toISOString(),
      level,
      source: 'discord',
      msg,
    };
    for (const h of this.logHandlers) h(entry);
  }

  private clearTimer(): void {
    if (this.timer) {
      clearTimeout(this.timer);
      this.timer = null;
    }
    this.nextAt = null;
  }

  /** Millisecond timestamp of the next local occurrence of sendAt. */
  private nextOccurrence(): number | null {
    const t = parseSendAt(this.config.sendAt);
    if (!t) return null;
    const now = new Date();
    const next = new Date(now.getFullYear(), now.getMonth(), now.getDate(), t.h, t.m, t.s, 0);
    if (next.getTime() <= now.getTime()) next.setDate(next.getDate() + 1);
    return next.getTime();
  }

  /**
   * (Re)arm the daily auto-post for the next local sendAt occurrence.
   * Recomputed after every fire and every config change; only armed while
   * enabled with at least one webhook.
   */
  private armTimer(): void {
    this.clearTimer();
    if (this.stopped || !this.config.enabled || this.config.webhooks.length === 0) return;
    const at = this.nextOccurrence();
    if (at === null) return;
    this.nextAt = at;
    // Delay is < 24h, safely inside the 32-bit setTimeout range.
    this.timer = setTimeout(() => {
      this.timer = null;
      this.nextAt = null;
      void this.enqueue('auto');
      this.armTimer(); // fires at sendAt, so next occurrence is ~24h out
    }, Math.max(0, at - Date.now()));
  }

  /**
   * Put a send on the serial queue. If one is already queued or executing,
   * this is a no-op that resolves when the queue drains.
   */
  private enqueue(reason: SendReason): Promise<void> {
    if (this.sendPending) {
      this.log('WARN', `send requested (${reason}) but one is already queued/in-flight — skipped`);
      return this.chain.then(() => undefined);
    }
    this.sendPending = true;
    const run = this.chain.then(async () => {
      try {
        await this.execute(reason);
      } finally {
        this.sendPending = false;
      }
    });
    // The chain itself must never reject or the queue would wedge.
    this.chain = run.catch(() => undefined);
    return this.chain;
  }

  /** Perform one send. Never throws — every outcome lands in the ring. */
  private async execute(reason: SendReason): Promise<void> {
    const startedAt = new Date().toISOString();
    const record = (ok: boolean, summary?: string, error?: string): void => {
      const send: DiscordSend = { startedAt, ok };
      if (summary !== undefined) send.summary = summary;
      if (error !== undefined) send.error = error;
      this.lastSends.unshift(send);
      if (this.lastSends.length > RING_MAX) this.lastSends.length = RING_MAX;
      this.log(ok ? 'INFO' : 'ERROR', `send (${reason}) ${ok ? 'ok' : 'failed'}: ${summary ?? error ?? ''}`);
    };

    const webhooks = this.config.webhooks.map((w) => ({ ...w }));
    if (webhooks.length === 0) {
      record(false, undefined, 'no webhooks configured — send skipped');
      return;
    }

    // One click (or the daily timer) is "get today's picks, then send them":
    // the picks job runs first, so the card is chosen from prices pulled this
    // minute and not from a capture up to four hours old. If it fails the
    // post still goes out on the picks already recorded.
    const jobs = await scheduler.runNow('capture');
    const capture = jobs.lastRuns.find((r) => r.job === 'capture');
    if (!capture || !capture.ok) {
      this.log('WARN', `picks refresh before the send failed (${capture?.error ?? 'no run'}) — sending the picks on record`);
    }

    const fetched = await this.fetchTodaysPicks();
    if ('error' in fetched) {
      record(false, undefined, fetched.error);
      return;
    }
    const { card: picks, candidates } = await this.fetchCard(fetched.picks);

    // The AUTO post skips silently on an empty board; a manual send still
    // posts the "No qualifying picks today." embed.
    if (picks.length === 0 && reason === 'auto') {
      record(true, 'no picks — skipped');
      return;
    }

    const payload = buildPayload(picks, new Date(), candidates, await this.fetchRecord());

    // POST the one payload to each webhook, sequentially. The webhook's own
    // name and picture are left alone.
    const failures: string[] = [];
    for (const w of webhooks) {
      const result = await postToWebhook(w.url, payload);
      if (!result.ok) failures.push(`${w.name || w.id}: ${result.error ?? 'failed'}`);
    }

    // The card counts toward the record once anyone has actually seen it.
    if (failures.length < webhooks.length) await this.saveCard(picks);

    const summary = `${webhooks.length - failures.length}/${webhooks.length} webhooks · ${picks.length} picks`;
    if (failures.length === 0) {
      record(true, `${webhooks.length} webhook${webhooks.length === 1 ? '' : 's'} · ${picks.length} picks`);
    } else {
      record(false, undefined, `${summary} — failed: ${truncate(failures.join('; '), 200)}`);
    }
  }
}

export const discordPicks = new DiscordPicks();
