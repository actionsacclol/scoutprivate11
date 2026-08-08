import { app } from 'electron';
import { existsSync } from 'node:fs';
import { createRequire } from 'node:module';
import { join } from 'node:path';
import type { UpdaterState } from '../../shared/types';

// ──────────────────────────────────────────────────────────────────────
// Auto-update scaffolding — wraps electron-updater's NsisUpdater behind
// the suite's "never throw, never dialog" rules.
//
// Today there is NO publish config (build.publish is null → electron-
// builder emits no app-update.yml into resources), so every packaged
// build boots straight into 'unconfigured' and stays silent. The moment
// a `publish: { provider: "github", … }` block lands in package.json and
// the app is rebuilt, app-update.yml appears next to the exe and this
// module starts working with zero code changes — electron-updater reads
// the feed from that file natively.
//
// Guarantees:
//  • autoDownload is OFF — an available update is only surfaced over IPC
//    ('updater:state'); nothing downloads or installs without an explicit
//    downloadAndInstall() from the renderer (user clicked the button).
//  • Nothing here ever throws or shows a dialog. Every failure mode —
//    no publish config, dev build, offline, 404 feed, private repo — is
//    folded into the state machine with the reason recorded:
//        unconfigured → idle → checking → available → downloading →
//        downloaded  (error from checking/downloading, back to idle/
//        available on the next successful check)
//  • One startup check, delayed 60s, packaged builds only. Everything
//    else is on-demand via check().
// ──────────────────────────────────────────────────────────────────────

/** Delay before the single automatic startup check. */
const STARTUP_CHECK_DELAY_MS = 60_000;

type StateCallback = (state: UpdaterState) => void;

/** Structural view of the electron-updater AppUpdater we rely on — keeps
 *  the import fully lazy (a broken/missing module can never crash boot). */
interface UpdaterImpl {
  autoDownload: boolean;
  autoInstallOnAppQuit: boolean;
  logger: unknown;
  on(event: string, cb: (...args: unknown[]) => void): unknown;
  checkForUpdates(): Promise<unknown>;
  downloadUpdate(): Promise<unknown>;
  quitAndInstall(isSilent?: boolean, isForceRunAfter?: boolean): void;
}

function errMsg(e: unknown): string {
  return e instanceof Error ? e.message : String(e);
}

class Updater {
  private current: UpdaterState = { status: 'unconfigured', error: 'not initialized' };
  private impl: UpdaterImpl | null = null;
  private implLoadFailed = false;
  private startupTimer: NodeJS.Timeout | null = null;
  private stopped = false;
  private stateHandlers: StateCallback[] = [];
  /** Serializes check/download so concurrent IPC calls can't interleave. */
  private inFlight: Promise<void> | null = null;

  // ─── Public API ────────────────────────────────────────────────────

  /**
   * Decide configured/unconfigured and (when packaged + configured) arm
   * the one delayed startup check. Never throws.
   */
  init(): void {
    if (!app.isPackaged) {
      this.setState({ status: 'unconfigured', error: 'dev build — updates disabled' });
      return;
    }
    if (!this.updateConfigPresent()) {
      // build.publish is null → electron-builder wrote no app-update.yml.
      this.setState({
        status: 'unconfigured',
        error: 'no publish configuration (app-update.yml not present)',
      });
      return;
    }
    this.setState({ status: 'idle' });
    this.startupTimer = setTimeout(() => {
      this.startupTimer = null;
      void this.check();
    }, STARTUP_CHECK_DELAY_MS);
  }

  state(): UpdaterState {
    return { ...this.current };
  }

  /**
   * On-demand update check. Resolves with the settled state; every
   * failure (offline, 404, private repo, bad yml) lands in status
   * 'error' with the reason — never a rejection.
   */
  async check(): Promise<UpdaterState> {
    if (this.stopped || this.current.status === 'unconfigured') return this.state();
    // A check or download is already running — just report current state.
    if (this.inFlight) return this.state();

    const run = async (): Promise<void> => {
      const impl = await this.ensureImpl();
      if (!impl) return; // ensureImpl already recorded the failure
      this.setState({ status: 'checking' });
      try {
        await impl.checkForUpdates();
        // Terminal state ('available' | 'idle') was set by the event
        // handlers; if for some reason no event fired, settle to idle.
        if (this.current.status === 'checking') this.setState({ status: 'idle' });
      } catch (e: unknown) {
        this.setState({ status: 'error', error: `update check failed: ${errMsg(e)}` });
      }
    };
    this.inFlight = run();
    try {
      await this.inFlight;
    } finally {
      this.inFlight = null;
    }
    return this.state();
  }

  /**
   * User-confirmed install path: download the update that check() found,
   * then quit and run the installer. Calling it again once the update is
   * already downloaded installs immediately. No-op in any other state.
   */
  async downloadAndInstall(): Promise<void> {
    if (this.stopped || !this.impl) return;
    if (this.current.status === 'downloaded') {
      this.quitAndInstall();
      return;
    }
    if (this.current.status !== 'available' || this.inFlight) return;

    const run = async (): Promise<void> => {
      const impl = this.impl;
      if (!impl) return;
      this.setState({ status: 'downloading', version: this.current.version, progress: 0 });
      try {
        await impl.downloadUpdate();
        // 'update-downloaded' has set status to 'downloaded'.
        this.quitAndInstall();
      } catch (e: unknown) {
        this.setState({ status: 'error', error: `download failed: ${errMsg(e)}` });
      }
    };
    this.inFlight = run();
    try {
      await this.inFlight;
    } finally {
      this.inFlight = null;
    }
  }

  onState(cb: StateCallback): () => void {
    this.stateHandlers.push(cb);
    return () => {
      this.stateHandlers = this.stateHandlers.filter((c) => c !== cb);
    };
  }

  /** Cancel the pending startup check. Called from main.ts on before-quit. */
  stop(): void {
    this.stopped = true;
    if (this.startupTimer) {
      clearTimeout(this.startupTimer);
      this.startupTimer = null;
    }
  }

  // ─── Internals ─────────────────────────────────────────────────────

  /** electron-builder drops app-update.yml next to the app only when a
   *  publish provider is configured — its presence IS the feed config. */
  private updateConfigPresent(): boolean {
    try {
      return existsSync(join(process.resourcesPath, 'app-update.yml'));
    } catch {
      return false;
    }
  }

  private setState(next: UpdaterState): void {
    // Drop no-op progress churn (NSIS fires many progress events).
    if (
      next.status === this.current.status &&
      next.version === this.current.version &&
      next.error === this.current.error &&
      Math.round(next.progress ?? -1) === Math.round(this.current.progress ?? -1)
    ) {
      return;
    }
    this.current = next;
    const copy = this.state();
    for (const h of this.stateHandlers) {
      try {
        h(copy);
      } catch {
        /* a listener must never break the machine */
      }
    }
  }

  /**
   * Lazy-load electron-updater and wire its events exactly once. Returns
   * null (recording the reason) if the module can't load — packaged apps
   * missing the dep must degrade, not crash.
   */
  private async ensureImpl(): Promise<UpdaterImpl | null> {
    if (this.impl) return this.impl;
    if (this.implLoadFailed) return null;
    try {
      // On Windows `autoUpdater` from electron-updater IS an NsisUpdater;
      // it reads resources/app-update.yml for the feed natively. Loaded
      // lazily via createRequire so the dep stays external to the vite
      // bundle (see vite.config.ts) and resolves through the same plain
      // CJS path as discord-rpc — deterministic inside the packaged asar.
      const mod = createRequire(__filename)('electron-updater') as {
        autoUpdater: UpdaterImpl;
      };
      const impl = mod.autoUpdater;
      impl.autoDownload = false; // surface over IPC; user decides
      impl.autoInstallOnAppQuit = true; // a downloaded update applies on quit
      impl.logger = null; // no electron-log, no console spam

      impl.on('checking-for-update', () => {
        this.setState({ status: 'checking' });
      });
      impl.on('update-available', (info: unknown) => {
        const version =
          info && typeof info === 'object' &&
          typeof (info as Record<string, unknown>).version === 'string'
            ? ((info as Record<string, unknown>).version as string)
            : undefined;
        this.setState({ status: 'available', version });
      });
      impl.on('update-not-available', () => {
        this.setState({ status: 'idle' });
      });
      impl.on('download-progress', (p: unknown) => {
        const percent =
          p && typeof p === 'object' &&
          typeof (p as Record<string, unknown>).percent === 'number'
            ? ((p as Record<string, unknown>).percent as number)
            : 0;
        this.setState({
          status: 'downloading',
          version: this.current.version,
          progress: Math.max(0, Math.min(100, percent)),
        });
      });
      impl.on('update-downloaded', () => {
        this.setState({ status: 'downloaded', version: this.current.version });
      });
      impl.on('error', (e: unknown) => {
        // Belt-and-braces: checkForUpdates/downloadUpdate rejections are
        // handled where awaited; this catches anything emitted outside
        // those promises so it can never become an uncaught 'error'.
        if (this.current.status === 'checking' || this.current.status === 'downloading') {
          this.setState({ status: 'error', error: errMsg(e) });
        }
      });

      this.impl = impl;
      return impl;
    } catch (e: unknown) {
      this.implLoadFailed = true;
      this.setState({
        status: 'error',
        error: `electron-updater unavailable: ${errMsg(e)}`,
      });
      return null;
    }
  }

  private quitAndInstall(): void {
    try {
      // Not silent (NSIS UI ok for a user-confirmed install), relaunch after.
      this.impl?.quitAndInstall(false, true);
    } catch (e: unknown) {
      this.setState({ status: 'error', error: `install failed: ${errMsg(e)}` });
    }
  }
}

export const updater = new Updater();
