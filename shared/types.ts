// Shared contract between the Electron main process and the renderer.
// The renderer only ever sees this surface (exposed as window.scout by the
// preload); the Python backend is reached over plain HTTP on 127.0.0.1.

export type BackendStatus =
  | 'stopped'
  | 'starting'
  | 'running'
  | 'restarting'
  | 'crashed';

export interface BackendInfo {
  status: BackendStatus;
  pid: number | null;
  /** The 127.0.0.1 port uvicorn is serving on (probed from 8000 upward). */
  port: number | null;
  startedAt: string | null;
  lastError: string | null;
  pythonOk: boolean;
}

export interface LogEntry {
  ts: string;
  level: string;
  source: string;
  msg: string;
}

/** Outcome of backend.exportLogs() — a user Save-dialog write. */
export interface LogExportResult {
  ok: boolean;
  /** Absolute path written to (ok only). */
  path?: string;
  /** The user dismissed the save dialog. */
  canceled?: boolean;
  /** Write/dialog failure (recorded, never thrown). */
  error?: string;
}

export interface WindowBounds {
  x: number;
  y: number;
  width: number;
  height: number;
}

// ── In-app job scheduler ─────────────────────────────────────────────
// Background jobs (odds/prediction history capture, pick settlement) run
// inside the Electron main process against the supervised backend — the
// user never touches Task Scheduler.

export type SchedulerJob = 'capture' | 'settle' | 'refresh';

/** Persisted scheduler configuration (lives in AppState.scheduler). */
export interface SchedulerConfig {
  enabled: boolean;
  captureEveryHours: number;
  settleEveryHours: number;
  /** Engine data refresh + stale-model retrain cadence (default 24h). */
  refreshEveryHours: number;
}

/** One completed (or failed) job execution, kept in a last-20 ring. */
export interface SchedulerRun {
  job: SchedulerJob;
  startedAt: string;
  finishedAt: string;
  ok: boolean;
  /** Human-readable result snippet (present when ok). */
  summary?: string;
  /** Failure description (present when !ok). Never thrown, always recorded. */
  error?: string;
}

export interface SchedulerInfo {
  config: SchedulerConfig;
  /** Newest first, capped at 20. */
  lastRuns: SchedulerRun[];
  /** ISO timestamp of the next scheduled fire per job (null = not armed). */
  nextRuns: Record<SchedulerJob, string | null>;
  /** Job currently executing, if any (runs are serialized, never overlap). */
  inFlight: SchedulerJob | null;
}

// ── Discord picks webhooks ───────────────────────────────────────────
// Daily picks are posted to user-configured Discord webhooks from the main
// process (renderer CSP blocks discord.com; main has no such limit). The
// payload is built from the backend's recorded picks (GET /api/picks), so
// posts match the app's tracked record exactly.

export interface DiscordWebhook {
  id: string;
  /** User label, e.g. "The Boys server #picks". */
  name: string;
  url: string;
  /** The Scout avatar has been PATCHed onto this webhook (one-time, best-effort). */
  avatarSet?: boolean;
}

export interface DiscordConfig {
  enabled: boolean;
  /** Local HH:MM 24h time for the daily auto-post, e.g. "11:00". */
  sendAt: string;
  /** Include only these tiers; default ["STRONG", "LEAN"]. */
  tiers: string[];
  webhooks: DiscordWebhook[];
}

export interface DiscordSend {
  startedAt: string;
  ok: boolean;
  /** e.g. "2 webhooks · 6 picks" (present when ok). */
  summary?: string;
  error?: string;
}

export interface DiscordInfo {
  config: DiscordConfig;
  /** Newest first, capped at 20. */
  lastSends: DiscordSend[];
  /** ISO timestamp of the next auto-post (null = disabled). */
  nextSend: string | null;
}

// ── Auto-updates ─────────────────────────────────────────────────────
// Scaffolding around electron-updater (NSIS). While build.publish is null
// (no GitHub repo / private repo — no embedded tokens, ever) the updater
// reports 'unconfigured' and does nothing. Every failure mode degrades to
// a recorded state — the main process never throws or shows a dialog.

export type UpdaterStatus =
  | 'unconfigured' // no publish config baked into this build (today's state)
  | 'idle'         // configured; no update known (incl. "up to date" after a check)
  | 'checking'
  | 'available'    // update found — waiting for the user to opt in
  | 'downloading'
  | 'downloaded'   // ready; install proceeds on confirm (or on next quit)
  | 'error';       // check/download failed (offline, 404, private repo, …)

export interface UpdaterState {
  status: UpdaterStatus;
  /** Version of the available/downloading/downloaded update. */
  version?: string;
  /** Download progress 0–100 (status 'downloading' only). */
  progress?: number;
  /** Why updates are unavailable / what failed. Recorded, never thrown. */
  error?: string;
}

// ── System (tray + login) behavior ───────────────────────────────────
// Both default ON: the installer ships an always-on companion — boots with
// Windows silently in the tray, and the X button hides instead of quitting
// (the scheduler/auto-posts/auto-bet keep running). Users can turn either
// off under SETTINGS → SYSTEM.

export interface SystemConfig {
  /** Launch at OS login, silently minimized to the tray. */
  autoStartOnLogin: boolean;
  /** The window X button hides to the tray instead of quitting. */
  closeToTray: boolean;
}

/** Everything persisted in settings.json under app.getPath('userData'). */
export interface AppState {
  windowBounds?: WindowBounds;
  scheduler?: SchedulerConfig;
  discord?: DiscordConfig;
  system?: SystemConfig;
  /** Set automatically after repeated GPU-process crashes: boot with
   *  hardware acceleration off (the macOS/older-GPU black-screen fix). */
  disableGpu?: boolean;
}

// ── The preload surface (window.scout) ───────────────────────────────

export interface ScoutApi {
  app: {
    version(): Promise<string>;
    openExternal(url: string): Promise<void>;
    systemConfig(): Promise<SystemConfig>;
    setSystemConfig(partial: Partial<SystemConfig>): Promise<SystemConfig>;
  };
  backend: {
    info(): Promise<BackendInfo>;
    /** Resolves once the supervisor has picked a port (null on timeout). */
    port(): Promise<number | null>;
    /** Port + the per-launch API token to send as X-Scout-Token. Null on
     *  timeout; `token` is null only on backends started without one. */
    connection(): Promise<{ port: number; token: string | null } | null>;
    restart(): Promise<void>;
    onStatus(cb: (info: BackendInfo) => void): () => void;
    logs(limit?: number): Promise<LogEntry[]>;
    /** Write an environment header + full log history to a user-chosen file. */
    exportLogs(): Promise<LogExportResult>;
  };
  scheduler: {
    info(): Promise<SchedulerInfo>;
    setConfig(partial: Partial<SchedulerConfig>): Promise<SchedulerInfo>;
    /** Manual trigger; resolves once the run finishes (up to the 120s cap). */
    runNow(job: SchedulerJob): Promise<SchedulerInfo>;
  };
  /** Optional while rolling out — always access via window.scout?.discord?. */
  discord?: {
    info(): Promise<DiscordInfo>;
    setConfig(partial: Partial<DiscordConfig>): Promise<DiscordInfo>;
    /** Post today's picks to every configured webhook right now. */
    sendNow(): Promise<DiscordInfo>;
    /** Post a short test message to one webhook URL. */
    test(url: string): Promise<{ ok: boolean; error?: string }>;
  };
  /** Optional while rolling out — always access via window.scout?.updater?. */
  updater?: {
    /** Current updater state (cheap, no network). */
    state(): Promise<UpdaterState>;
    /** Check the feed now; resolves with the settled state (never rejects). */
    check(): Promise<UpdaterState>;
    /** Download the available update, then quit into the installer. */
    downloadAndInstall(): Promise<void>;
    onState(cb: (state: UpdaterState) => void): () => void;
  };
  window: {
    minimize(): void;
    maximize(): void;
    close(): void;
    isMaximized(): Promise<boolean>;
    onMaximizeChange(cb: (maximized: boolean) => void): () => void;
  };
}
