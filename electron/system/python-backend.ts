import { app, BrowserWindow, Notification } from 'electron';
import { spawn, spawnSync, ChildProcessWithoutNullStreams } from 'node:child_process';
import { existsSync, mkdirSync } from 'node:fs';
import net from 'node:net';
import { join } from 'node:path';
import type { BackendInfo, BackendStatus, LogEntry } from '../../shared/types';

// ──────────────────────────────────────────────────────────────────────
// Backend lifecycle manager (Zeus pattern, adapted to an HTTP backend).
// Spawns uvicorn serving the Krypt Scout FastAPI app on a probed local
// port, health-checks it over GET /api/health, restarts on crash, and
// broadcasts state to all renderer windows. The renderer talks to the
// backend directly over HTTP — this class only supervises the process.
// ──────────────────────────────────────────────────────────────────────

type LogCallback = (entry: LogEntry) => void;

// A backend that survives this long is considered to have started cleanly;
// a later crash gets a fresh restart budget instead of counting toward the
// rapid-crash cap.
const STABLE_UPTIME_MS = 30_000;
// Hard ceiling on consecutive *fast* crashes before we stop respawning. This
// is what prevents a backend that dies on startup from spinning up dozens of
// short-lived processes in a tight loop.
const MAX_RAPID_RESTARTS = 5;
// How long we give a fresh child to answer /api/health before declaring the
// startup failed. Generous: the model stack imports pandas/sklearn/xgboost.
const STARTUP_HEALTH_TIMEOUT_MS = 60_000;
const STARTUP_POLL_INTERVAL_MS = 500;
// Hang watchdog: poll /api/health on this cadence once running; after this
// many consecutive failures, force-kill + restart. Catches a WEDGED-but-alive
// backend (event loop blocked, deadlock) that the exit handler can never see.
const PING_INTERVAL_MS = 60_000;
const PING_TIMEOUT_MS = 15_000;
const PING_FAILS_TO_RESTART = 3;
// Slow self-heal tier. After the rapid-crash cap gives up (gaveUp), we don't
// stop forever — a transient startup-locking window (AV scan, Windows Update,
// a briefly-locked SQLite) can produce 5 quick crashes and then clear on its
// own. We re-attempt on this slow cadence, resetting the fast budget each
// time. Kept intentionally UNBOUNDED; the notification is de-duped so a
// genuinely-broken bundle can't spam the user.
const SLOW_RETRY_INTERVAL_MS = 5 * 60_000;
// Port probing: first free 127.0.0.1 port at/above this. An occupied 8000
// (e.g. a manually-started uvicorn) simply means we pick 8001+ — never kill
// whatever is already there.
const PORT_PROBE_START = 8000;
const PORT_PROBE_TRIES = 100;

function probePortFree(port: number): Promise<boolean> {
  return new Promise((resolve) => {
    const srv = net.createServer();
    srv.once('error', () => resolve(false));
    srv.listen({ port, host: '127.0.0.1', exclusive: true }, () => {
      srv.close(() => resolve(true));
    });
  });
}

async function findFreePort(): Promise<number | null> {
  for (let p = PORT_PROBE_START; p < PORT_PROBE_START + PORT_PROBE_TRIES; p++) {
    if (await probePortFree(p)) return p;
  }
  return null;
}

class PythonBackend {
  private child: ChildProcessWithoutNullStreams | null = null;
  private status: BackendStatus = 'stopped';
  private port: number | null = null;
  private startedAt: string | null = null;
  private lastError: string | null = null;
  private pythonOk = false;
  private restartTimer: NodeJS.Timeout | null = null;
  private slowRetryTimer: NodeJS.Timeout | null = null;
  private slowRetryCount = 0;
  private crashNotified = false;
  private pingTimer: NodeJS.Timeout | null = null;
  private pingFails = 0;
  private pingInFlight = false;
  private restartAttempts = 0;
  private childStartedAtMs = 0;
  private gaveUp = false;
  private logHandlers: LogCallback[] = [];
  private statusHandlers: ((info: BackendInfo) => void)[] = [];
  private requestedStop = false;
  private starting = false;

  // ─── Public API ────────────────────────────────────────────────────

  async start(): Promise<void> {
    if (this.status === 'running' || this.status === 'starting') return;
    this.requestedStop = false;
    // A manual (re)start clears any prior give-up state and the rapid-crash
    // budget so the user always gets a fresh set of attempts.
    this.gaveUp = false;
    this.restartAttempts = 0;
    this.slowRetryCount = 0;
    this.crashNotified = false;
    // Cancel any pending auto-restart (fast or slow) so it can't fire AFTER we
    // spawn here and bring up a second backend.
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    if (this.slowRetryTimer) {
      clearTimeout(this.slowRetryTimer);
      this.slowRetryTimer = null;
    }
    await this.startChild();
  }

  async stop(): Promise<void> {
    this.requestedStop = true;
    this.stopPingWatchdog();
    if (this.restartTimer) {
      clearTimeout(this.restartTimer);
      this.restartTimer = null;
    }
    // A user Stop must stay stopped — cancel the slow self-heal tier too.
    if (this.slowRetryTimer) {
      clearTimeout(this.slowRetryTimer);
      this.slowRetryTimer = null;
    }
    const c = this.child;
    if (!c) {
      this.setStatus('stopped');
      return;
    }
    try {
      c.kill('SIGTERM');
    } catch {
      /* ignore */
    }
    setTimeout(() => {
      if (this.child === c && !c.killed) {
        try {
          c.kill('SIGKILL');
        } catch {
          /* ignore */
        }
      }
    }, 1500);
  }

  async restart(): Promise<void> {
    this.setStatus('restarting');
    await this.stop();
    setTimeout(() => void this.start(), 1500);
  }

  info(): BackendInfo {
    return {
      status: this.status,
      pid: this.child?.pid ?? null,
      port: this.port,
      startedAt: this.startedAt,
      lastError: this.lastError,
      pythonOk: this.pythonOk,
    };
  }

  isRunning(): boolean {
    return this.status === 'running';
  }

  /** Resolve the backend port once it has been picked. Null on timeout. */
  async waitForPort(timeoutMs = 15_000): Promise<number | null> {
    const deadline = Date.now() + timeoutMs;
    while (this.port === null && Date.now() < deadline) {
      if (this.requestedStop || this.gaveUp) break;
      await new Promise((r) => setTimeout(r, 200));
    }
    return this.port;
  }

  /**
   * Recovery state for out-of-renderer surfaces. Distinguishes "gave up after
   * repeated crashes" from a plain user Stop.
   */
  recoveryState(): { gaveUp: boolean; retrying: boolean } {
    return { gaveUp: this.gaveUp, retrying: this.slowRetryTimer !== null };
  }

  onLog(cb: LogCallback): () => void {
    this.logHandlers.push(cb);
    return () => {
      this.logHandlers = this.logHandlers.filter((c) => c !== cb);
    };
  }

  onStatusChange(cb: (info: BackendInfo) => void): () => void {
    this.statusHandlers.push(cb);
    return () => {
      this.statusHandlers = this.statusHandlers.filter((c) => c !== cb);
    };
  }

  // ─── Internals ─────────────────────────────────────────────────────

  private log(level: string, msg: string): void {
    const entry: LogEntry = {
      ts: new Date().toISOString(),
      level,
      source: 'backend',
      msg,
    };
    for (const h of this.logHandlers) h(entry);
  }

  private resolveBackendBin(port: number): { cmd: string; args: string[]; cwd: string } | null {
    // Production: ship a PyInstaller onedir bundle under
    // resources/python/krypt-scout-backend/ (see scripts/py-bundle.mjs and
    // the extraResources block in package.json). Dev: use the backend venv
    // if present, otherwise fall back through `py -3` / `python` / `python3`.
    if (app.isPackaged) {
      const base = join(process.resourcesPath, 'python', 'krypt-scout-backend');
      const exe =
        process.platform === 'win32'
          ? join(base, 'krypt-scout-backend.exe')
          : join(base, 'krypt-scout-backend');
      if (existsSync(exe)) {
        return { cmd: exe, args: ['--port', String(port)], cwd: base };
      }
      this.lastError = `bundled backend binary missing at ${exe}`;
      return null;
    }

    const backendDir = join(process.cwd(), 'backend');
    if (!existsSync(join(backendDir, 'kryptscout'))) {
      this.lastError = `backend package not found at ${backendDir}`;
      return null;
    }
    const uvicornArgs = [
      '-m', 'uvicorn', 'kryptscout.api.main:app',
      '--host', '127.0.0.1',
      '--port', String(port),
    ];

    // 1. Project-local venv beats anything on PATH.
    const venvPy = process.platform === 'win32'
      ? join(backendDir, '.venv', 'Scripts', 'python.exe')
      : join(backendDir, '.venv', 'bin', 'python');
    if (existsSync(venvPy)) {
      return { cmd: venvPy, args: uvicornArgs, cwd: backendDir };
    }

    // 2. System Python — try in order. Probe with --version (cheap).
    const candidates: { cmd: string; pre: string[] }[] = process.platform === 'win32'
      ? [
          { cmd: 'py', pre: ['-3'] },
          { cmd: 'python', pre: [] },
          { cmd: 'python3', pre: [] },
        ]
      : [
          { cmd: 'python3', pre: [] },
          { cmd: 'python', pre: [] },
        ];
    for (const c of candidates) {
      try {
        const probe = spawnSync(c.cmd, [...c.pre, '--version'], {
          stdio: 'ignore', shell: false, windowsHide: true,
        });
        if (!probe.error && probe.status === 0) {
          return { cmd: c.cmd, args: [...c.pre, ...uvicornArgs], cwd: backendDir };
        }
      } catch { /* keep trying */ }
    }
    this.lastError = 'No Python 3 interpreter found. Run `npm run py:setup` or install Python 3.10+ from https://python.org.';
    return null;
  }

  private async startChild(): Promise<void> {
    if (this.starting || this.child) return;
    this.starting = true;
    try {
      this.setStatus('starting');
      const userData = app.getPath('userData');
      // Make sure logs/data dirs exist before the backend starts.
      for (const sub of ['logs', 'data']) {
        const d = join(userData, sub);
        if (!existsSync(d)) mkdirSync(d, { recursive: true });
      }

      // Probe a fresh port every launch: 8000 may be held by an unrelated
      // process (or a dev uvicorn) — pick the next free one instead.
      const port = await findFreePort();
      if (port === null) {
        this.lastError = `no free port found in ${PORT_PROBE_START}..${PORT_PROBE_START + PORT_PROBE_TRIES}`;
        this.setStatus('crashed');
        this.scheduleRestart(true);
        return;
      }
      this.port = port;

      const resolved = this.resolveBackendBin(port);
      if (!resolved) {
        this.pythonOk = false;
        this.setStatus('crashed');
        // No Python at all — don't loop-retry forever.
        this.requestedStop = true;
        return;
      }
      const { cmd, args, cwd } = resolved;
      let child: ChildProcessWithoutNullStreams;
      try {
        child = spawn(cmd, args, {
          cwd,
          env: {
            ...process.env,
            KRYPT_SCOUT_USERDATA: userData,
            KRYPT_APP_VERSION: app.getVersion(),
            PYTHONUNBUFFERED: '1',
            PYTHONIOENCODING: 'utf-8',
          },
          stdio: ['pipe', 'pipe', 'pipe'],
          windowsHide: true,
        });
      } catch (e: any) {
        this.lastError = `spawn failed: ${e?.message || e}`;
        this.pythonOk = false;
        this.setStatus('crashed');
        this.scheduleRestart(true);
        return;
      }

      this.child = child;
      this.pythonOk = true;
      this.startedAt = new Date().toISOString();
      this.childStartedAtMs = Date.now();
      this.lastError = null;

      child.stdout.setEncoding('utf-8');
      child.stderr.setEncoding('utf-8');
      // Uvicorn logs everything (including access logs) to stderr; both
      // streams go into the ring buffer, stderr flagged as such.
      child.stdout.on('data', (chunk: string) => this.onChildOutput(chunk, 'INFO'));
      child.stderr.on('data', (chunk: string) => this.onChildOutput(chunk, 'STDERR'));
      child.on('error', (err) => {
        if (this.child !== child) return;
        this.lastError = `child error: ${err.message}`;
        this.pythonOk = false;
      });
      child.on('exit', (code, signal) => {
        // Identity guard: if a new child was already assigned (restart race),
        // the OLD child's late exit must NOT null the new child or schedule
        // another spawn — that would leave two live backends.
        if (this.child !== child) return;
        this.child = null;
        this.stopPingWatchdog();
        if (this.requestedStop) {
          this.setStatus('stopped');
          return;
        }
        const uptime = Date.now() - this.childStartedAtMs;
        this.lastError = `backend exited (code=${code} signal=${signal})`;
        this.setStatus('crashed');
        // Only crashes that happen shortly after launch count toward the
        // rapid-crash cap. A backend that ran fine for a while and then died
        // gets a clean restart (handled inside scheduleRestart).
        this.scheduleRestart(uptime < STABLE_UPTIME_MS);
      });

      this.awaitStartupHealth(child, port);
    } finally {
      this.starting = false;
    }
  }

  /** GET /api/health — true iff a 2xx comes back within timeoutMs. */
  private async checkHealth(port: number, timeoutMs: number): Promise<boolean> {
    const ctl = new AbortController();
    const timer = setTimeout(() => ctl.abort(), timeoutMs);
    try {
      const res = await fetch(`http://127.0.0.1:${port}/api/health`, {
        signal: ctl.signal,
      });
      return res.ok;
    } catch {
      return false;
    } finally {
      clearTimeout(timer);
    }
  }

  /** Poll health until 200 → running, or the startup window expires → kill. */
  private awaitStartupHealth(child: ChildProcessWithoutNullStreams, port: number): void {
    const deadline = Date.now() + STARTUP_HEALTH_TIMEOUT_MS;
    const tick = async (): Promise<void> => {
      // Only ever act on the child we were armed for.
      if (this.child !== child || this.requestedStop) return;
      if (await this.checkHealth(port, 3000)) {
        if (this.child !== child || this.requestedStop) return;
        this.setStatus('running');
        this.log('INFO', `backend healthy on http://127.0.0.1:${port}`);
        this.startPingWatchdog(child, port);
        return;
      }
      if (Date.now() > deadline) {
        if (this.child !== child) return;
        this.lastError = `backend never answered /api/health within ${STARTUP_HEALTH_TIMEOUT_MS / 1000}s — restarting`;
        this.log('ERROR', this.lastError);
        try {
          child.kill('SIGKILL');
        } catch {
          /* exit handler owns the restart path */
        }
        return;
      }
      setTimeout(() => void tick(), STARTUP_POLL_INTERVAL_MS);
    };
    setTimeout(() => void tick(), STARTUP_POLL_INTERVAL_MS);
  }

  private stopPingWatchdog(): void {
    if (this.pingTimer) {
      clearInterval(this.pingTimer);
      this.pingTimer = null;
    }
    this.pingFails = 0;
    this.pingInFlight = false;
  }

  private startPingWatchdog(child: ChildProcessWithoutNullStreams, port: number): void {
    this.stopPingWatchdog();
    this.pingTimer = setInterval(() => {
      // Only watch the child we were started for; a restart re-arms its own.
      if (this.child !== child || this.status !== 'running' || this.requestedStop) return;
      if (this.pingInFlight) return; // previous ping still deciding
      this.pingInFlight = true;
      this.checkHealth(port, PING_TIMEOUT_MS)
        .then((ok) => {
          if (this.child !== child || this.requestedStop) return;
          if (ok) {
            this.pingFails = 0;
            return;
          }
          this.pingFails++;
          if (this.pingFails >= PING_FAILS_TO_RESTART) {
            // Wedged-but-alive: the process exists but is not answering.
            // Force-kill; the exit handler owns the restart path
            // (identity-guarded, so this can never double-spawn).
            this.lastError = `backend unresponsive (${this.pingFails} health checks failed) — force-restarting`;
            this.log('ERROR', this.lastError);
            this.stopPingWatchdog();
            try {
              child.kill('SIGKILL');
            } catch {
              /* ignore */
            }
          }
        })
        .finally(() => {
          this.pingInFlight = false;
        });
    }, PING_INTERVAL_MS);
  }

  private scheduleRestart(quickCrash: boolean): void {
    if (this.requestedStop || this.gaveUp) return;
    if (this.restartTimer) return;

    if (quickCrash) {
      this.restartAttempts++;
    } else {
      // Crashed only after a long, stable run — start the budget over. This is
      // a genuine recovery boundary, so also re-arm the crash notification.
      this.restartAttempts = 0;
      this.crashNotified = false;
      this.slowRetryCount = 0;
    }

    if (this.restartAttempts >= MAX_RAPID_RESTARTS) {
      // Stop the FAST loop. NOTE: we set gaveUp but deliberately do NOT set
      // requestedStop. gaveUp means "gave up after crashes" (recoverable);
      // requestedStop means "the user asked to stop" (must stay stopped).
      // gaveUp still blocks the fast tier; the slow self-heal tier below owns
      // recovery.
      this.gaveUp = true;
      this.lastError =
        `backend crashed ${this.restartAttempts}× on startup; pausing fast ` +
        `restarts, will retry every ${Math.round(SLOW_RETRY_INTERVAL_MS / 60_000)}m. ` +
        `${this.lastError ?? ''}`.trim();
      this.setStatus('crashed');
      this.log('ERROR', this.lastError);
      // De-duped: only the first give-up of a crash cycle notifies.
      if (!this.crashNotified) {
        this.crashNotified = true;
        this.notifyCrashed();
      }
      this.scheduleSlowRetry();
      return;
    }

    const delay = Math.min(2000 * this.restartAttempts, 20_000);
    this.restartTimer = setTimeout(() => {
      this.restartTimer = null;
      // A stop()/start() may have raced in during the backoff window — don't
      // resurrect a backend that was asked to stop or already respawned.
      if (this.requestedStop || this.gaveUp || this.child) return;
      void this.startChild();
    }, delay);
  }

  /**
   * Slow self-heal tier. Scheduled after the fast tier gives up. On fire it
   * wipes the rapid-crash budget and clears gaveUp so a cleared transient
   * condition gets a fresh set of fast attempts. If it crashes right back out,
   * scheduleRestart hits the cap again and re-arms us — so the steady state is
   * a short fast burst every SLOW_RETRY_INTERVAL_MS, never a tight loop.
   */
  private scheduleSlowRetry(): void {
    if (this.requestedStop) return; // a real user Stop stays stopped
    if (this.slowRetryTimer) return; // already armed
    this.slowRetryTimer = setTimeout(() => {
      this.slowRetryTimer = null;
      // stop()/start() may have raced in during the wait.
      if (this.requestedStop || this.child) return;
      this.slowRetryCount++;
      this.restartAttempts = 0;
      this.gaveUp = false;
      this.log('INFO', `backend self-heal retry #${this.slowRetryCount} — attempting restart`);
      // Reflect the retry immediately (retrying flag flips off).
      this.emitStatus();
      void this.startChild();
    }, SLOW_RETRY_INTERVAL_MS);
    this.emitStatus();
  }

  private notifyCrashed(): void {
    try {
      if (!Notification.isSupported()) return;
      const n = new Notification({
        title: 'Krypt Scout — data engine stopped',
        body:
          'The scouting backend crashed repeatedly and has stopped. ' +
          'It will keep retrying automatically; open the app to check its status.',
      });
      n.show();
    } catch {
      /* notifications are best-effort — never let one break recovery */
    }
  }

  private onChildOutput(chunk: string, level: string): void {
    const text = chunk.toString();
    for (const raw of text.split('\n')) {
      const line = raw.trimEnd();
      if (!line.trim()) continue;
      this.log(level, line);
    }
  }

  private setStatus(s: BackendStatus): void {
    this.status = s;
    this.emitStatus();
  }

  private emitStatus(): void {
    const info = this.info();
    for (const h of this.statusHandlers) h(info);
    // Fan out to every renderer.
    for (const win of BrowserWindow.getAllWindows()) {
      if (!win.isDestroyed()) {
        win.webContents.send('backend:info', info);
      }
    }
  }
}

export const pythonBackend = new PythonBackend();
