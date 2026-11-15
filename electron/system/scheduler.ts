import type {
  LogEntry,
  SchedulerConfig,
  SchedulerInfo,
  SchedulerJob,
  SchedulerRun,
} from '../../shared/types';
import { request as httpRequest } from 'node:http';
import { app } from 'electron';
import { pythonBackend } from './python-backend';
import * as store from './settings-store';

// ──────────────────────────────────────────────────────────────────────
// In-app background job scheduler. Keeps the prediction/odds history
// growing without the user ever configuring Task Scheduler: once the
// supervised backend reaches "running" we wait for the model engines to
// warm, run one settle + one capture, then repeat each on its own
// configurable interval. The refresh job (engine data updates: the results
// history every rating is built from) also runs at startup, BEFORE the
// capture, whenever it is due: never run on this install, last run a full
// interval ago, or last run by a different app version (an update can
// discard an engine's saved state). When and by which version it last
// succeeded is persisted, so the daily cadence survives restarts — it used
// to be a bare 24h timer from launch, which a new install waited a day for
// and anyone who closes the app each night never reached: their boards
// showed the home-advantage constant for every game.
// Runs POST the backend's /api/jobs/* endpoints
// over 127.0.0.1 using the CURRENT backend port at call time (a crashed
// backend that respawns on a new port is picked up automatically).
//
// Guarantees:
//  • Runs never overlap — everything funnels through one serial queue,
//    and a job that is already queued/in-flight is not queued again.
//  • Runs never throw — every outcome (including a 404 from a backend
//    build that doesn't ship the jobs API yet) is recorded into a
//    last-20 ring and logged.
//  • Timers are cleared on app quit (main.ts calls stop()).
//
// Dev/testing: KRYPT_SCOUT_SCHED_FAST=1 shrinks the 90s warmup to 5s and
// reinterprets the configured "hours" as MINUTES, so a full schedule
// cycle is observable in a couple of minutes.
// ──────────────────────────────────────────────────────────────────────

const FAST = process.env.KRYPT_SCOUT_SCHED_FAST === '1';
/** Delay after the backend turns healthy before the first runs. */
const WARMUP_MS = FAST ? 5_000 : 90_000;
/** The same delay on an install that has never refreshed: its boards are
 *  showing untrained numbers, so don't sit on that for a minute and a half.
 *  The refresh endpoint waits for the engines itself. */
const FIRST_RUN_WARMUP_MS = FAST ? 5_000 : 10_000;
/** Soonest a restored refresh timer may fire after the timers arm. */
const MIN_REARM_MS = 60_000;
/** What one configured "hour" means on the wall clock. */
const HOUR_MS = FAST ? 60_000 : 3_600_000;
/** Hard cap on a single job HTTP call. */
const RUN_TIMEOUT_MS = 120_000;
/**
 * Refresh downloads engine data (whole college seasons) and retrains any
 * stale models — far slower than capture/settle, so it gets a 15-minute
 * cap instead of the standard 120s.
 */
const REFRESH_TIMEOUT_MS = 900_000;
/** Ring size for lastRuns. */
const RING_MAX = 20;

const JOBS: SchedulerJob[] = ['capture', 'settle', 'refresh'];

function defaults(): SchedulerConfig {
  return { enabled: true, captureEveryHours: 4, settleEveryHours: 6, refreshEveryHours: 24 };
}

/** Clamp a raw hours value to something sane; fall back otherwise. */
function sanitizeHours(v: unknown, fallback: number): number {
  const n = typeof v === 'number' ? v : Number(v);
  if (!Number.isFinite(n) || n <= 0) return fallback;
  return Math.min(n, 168);
}

function sanitize(partial: Partial<SchedulerConfig> | undefined, base: SchedulerConfig): SchedulerConfig {
  const p = partial ?? {};
  return {
    enabled: typeof p.enabled === 'boolean' ? p.enabled : base.enabled,
    captureEveryHours: sanitizeHours(p.captureEveryHours, base.captureEveryHours),
    settleEveryHours: sanitizeHours(p.settleEveryHours, base.settleEveryHours),
    refreshEveryHours: sanitizeHours(p.refreshEveryHours, base.refreshEveryHours),
  };
}

function truncate(s: string, max: number): string {
  return s.length > max ? s.slice(0, max - 1) + '…' : s;
}

/**
 * Render the refresh job's response shape —
 * {"results": {sport: {ok, seconds, summary}}} — as e.g.
 * "9/9 engines · 41s" (failures listed by sport). Returns null when the
 * body is not that shape.
 */
function summarizeRefreshResults(data: Record<string, unknown>): string | null {
  const results = data.results;
  if (!results || typeof results !== 'object' || Array.isArray(results)) return null;
  const entries = Object.entries(results as Record<string, unknown>).filter(
    (pair): pair is [string, Record<string, unknown>] => {
      const v = pair[1];
      return !!v && typeof v === 'object' && !Array.isArray(v);
    },
  );
  if (!entries.length || !entries.every(([, v]) => typeof v.ok === 'boolean')) return null;

  const okCount = entries.filter(([, v]) => v.ok === true).length;
  const parts = [`${okCount}/${entries.length} engines`];

  let seconds = 0;
  let haveSeconds = false;
  for (const [, v] of entries) {
    if (typeof v.seconds === 'number' && Number.isFinite(v.seconds)) {
      seconds += v.seconds;
      haveSeconds = true;
    }
  }
  if (haveSeconds) parts.push(`${Math.round(seconds)}s`);

  const failed = entries.filter(([, v]) => v.ok !== true);
  if (failed.length) {
    const names = failed.map(([sport, v]) =>
      typeof v.summary === 'string' && v.summary ? `${sport} (${truncate(v.summary, 40)})` : sport,
    );
    parts.push(`failed: ${names.join(', ')}`);
  }
  return truncate(parts.join(' · '), 200);
}

/**
 * Turn a jobs-endpoint JSON response into a short human snippet, e.g.
 * {"nfl": 12, "nba": 30} → "nfl: 12 · nba: 30". The refresh job's nested
 * {"results": {sport: {ok, seconds, summary}}} shape renders as
 * "9/9 engines · 41s". Falls back to the raw body (truncated) when the
 * shape is unexpected.
 */
function summarize(body: string): string {
  try {
    const data: unknown = JSON.parse(body);
    if (data && typeof data === 'object' && !Array.isArray(data)) {
      const refresh = summarizeRefreshResults(data as Record<string, unknown>);
      if (refresh !== null) return refresh;
      const parts: string[] = [];
      for (const [k, v] of Object.entries(data as Record<string, unknown>)) {
        if (typeof v === 'number' || typeof v === 'string' || typeof v === 'boolean') {
          parts.push(`${k}: ${v}`);
        } else if (v && typeof v === 'object' && !Array.isArray(v)) {
          // One nested level (e.g. {"counts": {"nfl": 12}}).
          const inner = Object.entries(v as Record<string, unknown>)
            .filter(([, iv]) => typeof iv === 'number' || typeof iv === 'string')
            .map(([ik, iv]) => `${ik} ${iv}`);
          if (inner.length) parts.push(`${k}: ${inner.join(', ')}`);
        }
      }
      if (parts.length) return truncate(parts.join(' · '), 200);
    }
  } catch {
    /* not JSON — fall through to raw snippet */
  }
  const raw = body.trim();
  return raw ? truncate(raw, 200) : 'ok';
}

/** True when a refresh response says every engine it touched succeeded.
 *  An unexpected shape counts as success: the call itself returned 200. */
function refreshFullyOk(body: string): boolean {
  try {
    const data: unknown = JSON.parse(body);
    const results = (data as { results?: unknown } | null)?.results;
    if (!results || typeof results !== 'object' || Array.isArray(results)) return true;
    const entries = Object.values(results as Record<string, unknown>);
    // No engines at all means the registry was empty — that trained nothing.
    if (!entries.length) return false;
    return entries.every((v) => !!v && typeof v === 'object' && (v as { ok?: unknown }).ok === true);
  } catch {
    return true;
  }
}

type LogCallback = (entry: LogEntry) => void;

class JobScheduler {
  private config: SchedulerConfig = defaults();
  private lastRuns: SchedulerRun[] = [];
  private inFlight: SchedulerJob | null = null;
  /** Jobs queued or executing right now — the no-overlap / no-double-queue guard. */
  private pending = new Set<SchedulerJob>();
  /** Serial execution queue: every run chains onto this promise. */
  private chain: Promise<void> = Promise.resolve();
  private timers: Partial<Record<SchedulerJob, NodeJS.Timeout>> = {};
  private nextAt: Record<SchedulerJob, number | null> = { capture: null, settle: null, refresh: null };
  private warmupTimer: NodeJS.Timeout | null = null;
  /** True once we've reacted to the backend's first "running". */
  private backendSeenRunning = false;
  /** True once the initial settle+capture pair has been queued. */
  private initialDone = false;
  private stopped = false;
  private logHandlers: LogCallback[] = [];

  // ─── Public API ────────────────────────────────────────────────────

  /**
   * Load persisted config and watch the backend supervisor. Call BEFORE
   * pythonBackend.start() so the first "running" transition is not missed.
   */
  init(): void {
    this.config = sanitize(store.get().scheduler, defaults());
    pythonBackend.onStatusChange((info) => {
      if (info.status === 'running') this.onBackendRunning();
    });
    // In case the backend was already up before init (defensive).
    if (pythonBackend.isRunning()) this.onBackendRunning();
    this.log(
      'INFO',
      `scheduler ready (enabled=${this.config.enabled}, capture every ${this.config.captureEveryHours}${FAST ? 'm' : 'h'}, ` +
        `settle every ${this.config.settleEveryHours}${FAST ? 'm' : 'h'}, ` +
        `refresh every ${this.config.refreshEveryHours}${FAST ? 'm' : 'h'}${FAST ? ', FAST mode' : ''})`,
    );
  }

  info(): SchedulerInfo {
    return {
      config: { ...this.config },
      lastRuns: [...this.lastRuns],
      nextRuns: {
        capture: this.nextAt.capture ? new Date(this.nextAt.capture).toISOString() : null,
        settle: this.nextAt.settle ? new Date(this.nextAt.settle).toISOString() : null,
        refresh: this.nextAt.refresh ? new Date(this.nextAt.refresh).toISOString() : null,
      },
      inFlight: this.inFlight,
      refreshedAt: store.get().schedulerState?.refreshedAt ?? null,
    };
  }

  /** When the engines' data was last fully refreshed (ms), or null = never. */
  private refreshedAtMs(): number | null {
    const at = Date.parse(store.get().schedulerState?.refreshedAt ?? '');
    return Number.isFinite(at) ? at : null;
  }

  /** Never refreshed, a full interval ago, or by another app version. */
  private refreshDue(): boolean {
    const at = this.refreshedAtMs();
    if (at === null) return true;
    if (store.get().schedulerState?.refreshedVersion !== app.getVersion()) return true;
    return Date.now() - at >= this.intervalMs('refresh');
  }

  setConfig(partial: Partial<SchedulerConfig>): SchedulerInfo {
    const prev = this.config;
    this.config = sanitize(partial, prev);
    store.save({ ...store.get(), scheduler: { ...this.config } });

    if (!this.config.enabled) {
      // Master off: tear down all pending fires (an in-flight run finishes).
      this.clearWarmup();
      for (const job of JOBS) this.clearTimer(job);
      if (prev.enabled) this.log('INFO', 'scheduler disabled — timers cleared');
    } else if (this.backendSeenRunning) {
      if (this.initialDone) {
        // Re-arm both intervals from "now" with the new cadence.
        for (const job of JOBS) this.armTimer(job);
        this.log('INFO', 'scheduler config updated — intervals re-armed');
      } else if (!this.warmupTimer) {
        // Was disabled (or never armed) when the backend came up; start the
        // warm-up → initial-runs sequence now.
        this.scheduleWarmup();
      }
      // else: warmup already pending — it reads config at fire time.
    } else if (!prev.enabled) {
      this.log('INFO', 'scheduler enabled — waiting for backend to reach running');
    }
    return this.info();
  }

  /**
   * Manual trigger. Queues the job (unless it is already queued/running)
   * and resolves with fresh info once that run has finished. Works even
   * while the schedule master switch is off.
   */
  async runNow(job: SchedulerJob): Promise<SchedulerInfo> {
    if (JOBS.includes(job)) {
      await this.enqueue(job, 'manual');
    }
    return this.info();
  }

  /** Clear every timer. Called from main.ts on before-quit. */
  stop(): void {
    this.stopped = true;
    this.clearWarmup();
    for (const job of JOBS) this.clearTimer(job);
  }

  onLog(cb: LogCallback): () => void {
    this.logHandlers.push(cb);
    return () => {
      this.logHandlers = this.logHandlers.filter((c) => c !== cb);
    };
  }

  // ─── Internals ─────────────────────────────────────────────────────

  private log(level: string, msg: string): void {
    const entry: LogEntry = {
      ts: new Date().toISOString(),
      level,
      source: 'scheduler',
      msg,
    };
    for (const h of this.logHandlers) h(entry);
  }

  private clearWarmup(): void {
    if (this.warmupTimer) {
      clearTimeout(this.warmupTimer);
      this.warmupTimer = null;
    }
    if (!this.initialDone) {
      // nextAt was pointing at the warmup fire — nothing is armed now.
      if (!this.timers.capture) this.nextAt.capture = null;
      if (!this.timers.settle) this.nextAt.settle = null;
    }
  }

  private clearTimer(job: SchedulerJob): void {
    const t = this.timers[job];
    if (t) {
      clearTimeout(t);
      this.timers[job] = undefined;
    }
    this.nextAt[job] = null;
  }

  /** First time the backend reports healthy: arm the warm-up sequence. */
  private onBackendRunning(): void {
    if (this.stopped || this.backendSeenRunning) return;
    this.backendSeenRunning = true;
    if (!this.config.enabled) {
      this.log('INFO', 'backend running; scheduler is disabled — not arming jobs');
      return;
    }
    this.scheduleWarmup();
  }

  /**
   * Wait for the engines to warm, then settle once, refresh if it is due,
   * capture once, then intervals. Refresh goes BEFORE the capture on
   * purpose: a capture taken on missing or day-old ratings records picks
   * the engines would not make once they have caught up. The cost is that a
   * brand-new install's first capture waits out the one-time history
   * download (minutes); an ordinary daily catch-up is a few seconds.
   */
  private scheduleWarmup(): void {
    if (this.stopped || this.warmupTimer) return;
    const warmup = this.refreshedAtMs() === null ? FIRST_RUN_WARMUP_MS : WARMUP_MS;
    const fireAt = Date.now() + warmup;
    this.nextAt.settle = fireAt;
    this.nextAt.capture = fireAt;
    this.log('INFO', `backend running — first jobs in ${Math.round(warmup / 1000)}s`);
    this.warmupTimer = setTimeout(() => {
      this.warmupTimer = null;
      if (this.stopped || !this.config.enabled) return;
      this.initialDone = true;
      // Order matters: settle yesterday's picks first, bring the engines up
      // to date, then capture fresh history. The serial queue preserves it.
      void this.enqueue('settle', 'startup');
      if (this.refreshDue()) void this.enqueue('refresh', 'startup');
      void this.enqueue('capture', 'startup');
      for (const job of JOBS) this.armTimer(job);
    }, warmup);
  }

  private intervalMs(job: SchedulerJob): number {
    const hours =
      job === 'capture'
        ? this.config.captureEveryHours
        : job === 'settle'
          ? this.config.settleEveryHours
          : this.config.refreshEveryHours;
    return Math.max(1_000, Math.round(hours * HOUR_MS));
  }

  /** (Re)arm one job's repeating timer; each fire re-arms itself. */
  private armTimer(job: SchedulerJob): void {
    this.clearTimer(job);
    if (this.stopped || !this.config.enabled) return;
    let ms = this.intervalMs(job);
    // Refresh keeps its cadence across restarts: fire a full interval after
    // the last success, not after this launch. (One queued right now will
    // stamp a new success when it lands, so it takes the full interval.)
    const refreshedAt = job === 'refresh' && !this.pending.has('refresh') ? this.refreshedAtMs() : null;
    if (refreshedAt !== null) {
      ms = Math.min(ms, Math.max(MIN_REARM_MS, refreshedAt + ms - Date.now()));
    }
    this.nextAt[job] = Date.now() + ms;
    this.timers[job] = setTimeout(() => {
      this.timers[job] = undefined;
      void this.enqueue(job, 'schedule');
      this.armTimer(job);
    }, ms);
  }

  /**
   * Put a job on the serial queue. If the same job is already queued or
   * executing, this is a no-op (resolves when the queue drains to it).
   */
  private enqueue(job: SchedulerJob, reason: string): Promise<void> {
    if (this.pending.has(job)) {
      this.log('WARN', `${job} run requested (${reason}) but one is already queued/in-flight — skipped`);
      return this.chain.then(() => undefined);
    }
    this.pending.add(job);
    const run = this.chain.then(async () => {
      this.inFlight = job;
      try {
        await this.execute(job, reason);
      } finally {
        this.inFlight = null;
        this.pending.delete(job);
      }
    });
    // The chain itself must never reject or the queue would wedge.
    this.chain = run.catch(() => undefined);
    return this.chain;
  }

  /** Perform one HTTP run. Never throws — every outcome lands in the ring. */
  private async execute(job: SchedulerJob, reason: string): Promise<void> {
    const startedAt = new Date().toISOString();
    const record = (ok: boolean, summary?: string, error?: string): void => {
      const run: SchedulerRun = { job, startedAt, finishedAt: new Date().toISOString(), ok };
      if (summary !== undefined) run.summary = summary;
      if (error !== undefined) run.error = error;
      this.lastRuns.unshift(run);
      if (this.lastRuns.length > RING_MAX) this.lastRuns.length = RING_MAX;
      this.log(ok ? 'INFO' : 'ERROR', `${job} run (${reason}) ${ok ? 'ok' : 'failed'}: ${summary ?? error ?? ''}`);
    };

    // Current port at call time — a restarted backend on a new port just works.
    const backend = pythonBackend.info();
    if (backend.status !== 'running' || backend.port === null) {
      record(false, undefined, `backend not running (status=${backend.status}) — job skipped`);
      return;
    }

    const url = `http://127.0.0.1:${backend.port}/api/jobs/${job}`;
    // Refresh can legitimately run for minutes (college season downloads
    // + stale-model retrains) — everything else keeps the tight 120s cap.
    const timeoutMs = job === 'refresh' ? REFRESH_TIMEOUT_MS : RUN_TIMEOUT_MS;
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const token = pythonBackend.token();
      const res = await postLocal(url, token ? { 'X-Scout-Token': token } : {}, ctl.signal);
      const body = res.body;
      if (res.status === 404) {
        record(false, undefined, 'jobs endpoint not available (404) — this backend build does not ship /api/jobs yet');
      } else if (!res.ok) {
        record(false, undefined, `HTTP ${res.status}: ${truncate(body.trim() || res.statusText, 160)}`);
      } else {
        record(true, summarize(body));
        // Only a refresh in which every engine succeeded counts as "up to
        // date"; a partial one is retried at the next launch.
        if (job === 'refresh' && refreshFullyOk(body)) {
          store.save({
            ...store.get(),
            schedulerState: { refreshedAt: new Date().toISOString(), refreshedVersion: app.getVersion() },
          });
        }
      }
    } catch (e: unknown) {
      const msg = ctl.signal.aborted
        ? `timed out after ${timeoutMs / 1000}s`
        : e instanceof Error
          ? e.message
          : String(e);
      record(false, undefined, msg);
    } finally {
      clearTimeout(timer);
    }
  }
}

/**
 * POST to the local backend and wait as long as the caller's signal allows.
 *
 * Not fetch(): Node's fetch gives up with "fetch failed" when no response
 * headers have arrived 300 s after the request (undici's headersTimeout), and
 * a job answers only when it is finished. A new install's first refresh runs
 * past five minutes, so it was reported as failed ("Setup did not finish")
 * at exactly 5:00 while the backend carried on, whatever REFRESH_TIMEOUT_MS
 * said. node:http has no such limit; the AbortSignal is the only timeout.
 */
function postLocal(
  url: string,
  headers: Record<string, string>,
  signal: AbortSignal,
): Promise<{ ok: boolean; status: number; statusText: string; body: string }> {
  return new Promise((resolve, reject) => {
    const req = httpRequest(
      url,
      { method: 'POST', headers: { ...headers, 'Content-Length': '0' }, signal },
      (res) => {
        const chunks: Buffer[] = [];
        res.on('data', (chunk: Buffer) => chunks.push(chunk));
        res.on('error', reject);
        res.on('end', () => {
          const status = res.statusCode ?? 0;
          resolve({
            ok: status >= 200 && status < 300,
            status,
            statusText: res.statusMessage ?? '',
            body: Buffer.concat(chunks).toString('utf8'),
          });
        });
      },
    );
    req.on('error', reject);
    req.end();
  });
}

export const scheduler = new JobScheduler();
