import { randomUUID } from 'node:crypto';
import type {
  DiscordConfig,
  DiscordInfo,
  DiscordSend,
  DiscordWebhook,
  LogEntry,
} from '../../shared/types';
import { pythonBackend } from './python-backend';
import * as store from './settings-store';

// ──────────────────────────────────────────────────────────────────────
// Discord picks sender. Posts the day's recorded picks (GET /api/picks on
// the supervised backend) to user-configured Discord webhooks from the
// main process (the renderer CSP blocks discord.com; main has no such
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
/** Max pick lines in one embed description. */
const MAX_LINES = 12;
const EMBED_COLOR = 0xe11d48;
/** Where readers can get the terminal — the embed title links here and the
 *  description closes with it. Footers can't carry links on Discord. */
const TOOL_URL = 'https://krypt.cc/tools/scout';

/** The Scout mark (red circle, white ◈ — resources/scout-webhook-avatar.png)
 *  embedded as base64: webhook avatars are set by PATCHing the webhook with a
 *  data-URI image, and nothing hosts this file publicly. One-time per webhook,
 *  best-effort (see ensureWebhookAvatar). */
const AVATAR_DATA_URI =
  'data:image/png;base64,' +
  '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';

/** PATCH a webhook's default name + avatar to the Scout identity. One-time
 *  per webhook (callers gate on avatarSet), best-effort: a failure just means
 *  Discord shows the webhook's original avatar. Never throws. */
async function ensureWebhookAvatar(url: string): Promise<boolean> {
  const ctl = new AbortController();
  const timer = setTimeout(() => ctl.abort(), WEBHOOK_TIMEOUT_MS);
  try {
    const res = await fetch(url, {
      method: 'PATCH',
      headers: { 'content-type': 'application/json' },
      body: JSON.stringify({ name: 'Krypt Scout', avatar: AVATAR_DATA_URI }),
      signal: ctl.signal,
    });
    return res.ok;
  } catch {
    return false;
  } finally {
    clearTimeout(timer);
  }
}
const MONTHS = ['Jan', 'Feb', 'Mar', 'Apr', 'May', 'Jun', 'Jul', 'Aug', 'Sep', 'Oct', 'Nov', 'Dec'];

const WEBHOOK_URL_PREFIXES = [
  'https://discord.com/api/webhooks/',
  'https://discordapp.com/api/webhooks/',
];

type SendReason = 'manual' | 'auto';

/** One pick as served by GET /api/picks (fields defensively optional). */
interface ApiPick {
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
}

function defaults(): DiscordConfig {
  return { enabled: false, sendAt: '11:00', tiers: ['STRONG', 'LEAN'], webhooks: [] };
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
      avatarSet: w.avatarSet === true,
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

/** Model win probability recovered from the pick itself: EV = p·d − 1 for
 *  decimal price d, so p = (1+EV)/d. A MODEL pick's fair price is its EV=0
 *  price, so p is just that price's implied probability. Shown instead of
 *  the tier label — a +EV pick can still be a 6%-to-win longshot, and
 *  readers should see that before they tail it. */
function winChance(p: ApiPick): string {
  const price = Number(p.price);
  if (!Number.isFinite(price) || Math.abs(price) < 100) return '?%';
  const decimal = price > 0 ? 1 + price / 100 : 1 + 100 / Math.abs(price);
  const ev = Number(p.ev);
  const prob = (Number.isFinite(ev) ? 1 + ev : 1) / decimal;
  if (!(prob > 0 && prob < 1)) return '?%';
  return `${Math.round(prob * 100)}%`;
}

/** One description line per pick. No emoji, per suite convention. */
function pickLine(p: ApiPick): string {
  const tag = sportTag(p.sport);
  const team = (typeof p.team === 'string' && p.team.trim() ? p.team.trim() : 'UNKNOWN').toUpperCase();
  const tier = typeof p.tier === 'string' ? p.tier.trim().toUpperCase() : '';
  const price = formatPrice(p.price);
  if (tier === 'MODEL') {
    return `\`${tag}\` **${team}** ML (fair ${price})  ·  model pick  ·  ${winChance(p)} to win`;
  }
  const book = typeof p.book === 'string' && p.book.trim() ? p.book.trim() : '—';
  return `\`${tag}\` **${team}** ML ${price} @ ${book}  ·  EV ${formatEv(p.ev)}%  ·  ${winChance(p)} to win`;
}

/** Build the single Discord webhook JSON payload for a set of picks.
 *  Footer: "Krypt Scout · N picks · krypt.cc"; the title links to
 *  krypt.cc/tools/scout and the description closes with the same link so
 *  readers can grab the terminal themselves. */
export function buildPayload(picks: ApiPick[], now = new Date()): Record<string, unknown> {
  const lines = picks.slice(0, MAX_LINES).map(pickLine);
  if (picks.length > MAX_LINES) {
    lines.push(`*…and ${picks.length - MAX_LINES} more in the terminal*`);
  }
  const body = lines.length ? lines.join('\n') : 'No qualifying picks today.';
  const getIt = `[Get the free terminal — krypt.cc/tools/scout](${TOOL_URL}) · flat 1u · not betting advice`;
  return {
    username: 'Krypt Scout',
    embeds: [
      {
        title: `TODAY'S PICKS — ${MONTHS[now.getMonth()]} ${now.getDate()}`,
        url: TOOL_URL,
        color: EMBED_COLOR,
        description: `${body}\n\n${getIt}`,
        footer: {
          text: `Krypt Scout · ${picks.length} pick${picks.length === 1 ? '' : 's'} · krypt.cc`,
        },
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
   * and resolves with fresh info once that send has finished.
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
    // Best-effort: give the webhook the Scout identity (name + red-diamond
    // avatar) before its first visible post. The URL may not be saved yet,
    // so no avatarSet bookkeeping here — the PATCH is idempotent.
    await ensureWebhookAvatar(url);
    return postToWebhook(url, buildPayload(fetched.picks));
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
   * local backend. Shared by real sends AND the TEST button — a test posts
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
        const res = await fetch(url, { signal: ctl.signal });
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

    const fetched = await this.fetchTodaysPicks();
    if ('error' in fetched) {
      record(false, undefined, fetched.error);
      return;
    }
    const picks = fetched.picks;

    // The AUTO post skips silently on an empty board; a manual send still
    // posts the "No qualifying picks today." embed.
    if (picks.length === 0 && reason === 'auto') {
      record(true, 'no picks — skipped');
      return;
    }

    const payload = buildPayload(picks);

    // POST the one payload to each webhook, sequentially. New webhooks get
    // the Scout identity (name + red-diamond avatar) PATCHed on first —
    // one-time, remembered via avatarSet, and never a reason to fail a send.
    const failures: string[] = [];
    for (const w of webhooks) {
      if (!w.avatarSet && (await ensureWebhookAvatar(w.url))) {
        w.avatarSet = true;
        const cfgHook = this.config.webhooks.find((c) => c.id === w.id);
        if (cfgHook) {
          cfgHook.avatarSet = true;
          store.save({
            ...store.get(),
            discord: {
              ...this.config,
              tiers: [...this.config.tiers],
              webhooks: this.config.webhooks.map((c) => ({ ...c })),
            },
          });
        }
      }
      const result = await postToWebhook(w.url, payload);
      if (!result.ok) failures.push(`${w.name || w.id}: ${result.error ?? 'failed'}`);
    }

    const summary = `${webhooks.length - failures.length}/${webhooks.length} webhooks · ${picks.length} picks`;
    if (failures.length === 0) {
      record(true, `${webhooks.length} webhook${webhooks.length === 1 ? '' : 's'} · ${picks.length} picks`);
    } else {
      record(false, undefined, `${summary} — failed: ${truncate(failures.join('; '), 200)}`);
    }
  }
}

export const discordPicks = new DiscordPicks();
