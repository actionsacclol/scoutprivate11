import { BrowserWindow, app, dialog, ipcMain } from 'electron';
import {
  appendFileSync,
  existsSync,
  mkdirSync,
  readFileSync,
  renameSync,
  statSync,
  writeFileSync,
} from 'node:fs';
import { join } from 'node:path';
import type {
  DiscordConfig,
  LogEntry,
  LogExportResult,
  SchedulerConfig,
  SchedulerJob,
} from '../shared/types';
import { discordPicks } from './system/discord-picks';
import { pythonBackend } from './system/python-backend';
import { scheduler } from './system/scheduler';
import * as store from './system/settings-store';
import { getSystemConfig, setSystemConfig } from './system/system-settings';
import { updater } from './system/updater';
import { openExternalSafe } from './system/webSecurity';

// In-memory rolling ring buffer of backend stdout/stderr so a future Logs
// surface can hydrate on open (window.scout.backend.logs()).
const LOG_MAX = 2000;
const logBuffer: LogEntry[] = [];

// Every entry is also mirrored to userData/logs/main.log as it arrives, so a
// machine whose window never paints (SETTINGS → export logs unreachable — the
// black-screen case) still leaves a trace the user can pull straight off disk:
//   macOS    ~/Library/Application Support/Krypt Scout/logs/main.log
//   Windows  %APPDATA%\Krypt Scout\logs\main.log
// Lazily resolved because userData can be redirected (KRYPT_SCOUT_TEST_PROFILE)
// after this module is imported. One failed write disables mirroring for the
// rest of the run — a full disk must not take the ring buffer down with it.
const MAIN_LOG_ROTATE_BYTES = 5 * 1024 * 1024;
let mainLogPath: string | null | undefined;

function mirrorToDisk(entry: LogEntry): void {
  try {
    if (mainLogPath === undefined) {
      const dir = join(app.getPath('userData'), 'logs');
      mkdirSync(dir, { recursive: true });
      const p = join(dir, 'main.log');
      if (existsSync(p) && statSync(p).size > MAIN_LOG_ROTATE_BYTES) {
        renameSync(p, join(dir, 'main.log.1'));
      }
      appendFileSync(
        p,
        `=== run start pid=${process.pid} v${app.getVersion()} ${new Date().toISOString()} ===\n`,
        'utf-8',
      );
      mainLogPath = p;
    }
    appendFileSync(mainLogPath!, fmtLogLine(entry) + '\n', 'utf-8');
  } catch {
    mainLogPath = null;
  }
}

export function appendLog(entry: LogEntry): void {
  logBuffer.push(entry);
  if (logBuffer.length > LOG_MAX) logBuffer.splice(0, logBuffer.length - LOG_MAX);
  if (mainLogPath !== null) mirrorToDisk(entry);
}

/** One `HH:MM:SS LEVEL source msg` line — matches the in-app log view. */
function fmtLogLine(l: LogEntry): string {
  return `${l.ts} ${l.level.toUpperCase().padEnd(6)} ${l.source} ${l.msg}`;
}

/**
 * Assemble a single support-ready text blob: an environment header, the full
 * Electron-captured ring buffer (backend stdout/stderr + scheduler/discord),
 * and the backend's own rotating backend.log from disk (the deeper
 * startup/crash history the ring can't hold). Built entirely from local state
 * so it works even while the backend is unreachable — the exact case users hit.
 */
function buildLogExport(): string {
  const info = pythonBackend.info();
  const rec = pythonBackend.recoveryState();
  const header = [
    'Krypt Scout — backend log export',
    `generated:    ${new Date().toISOString()}`,
    `app version:  ${app.getVersion()}`,
    `platform:     ${process.platform} ${process.arch} · electron ${process.versions.electron} · node ${process.versions.node}`,
    `gpu:          hardwareAcceleration=${store.get().disableGpu ? 'disabled (crash fallback)' : 'on'}`,
    `backend:      status=${info.status} pid=${info.pid ?? '—'} port=${info.port ?? '—'} pythonOk=${info.pythonOk}`,
    `recovery:     gaveUp=${rec.gaveUp} retrying=${rec.retrying}`,
    info.startedAt ? `startedAt:    ${info.startedAt}` : '',
    info.lastError ? `lastError:    ${info.lastError}` : '',
  ]
    .filter(Boolean)
    .join('\n');

  const ring =
    `=== Electron-captured log buffer (last ${logBuffer.length}) ===\n` +
    (logBuffer.length ? logBuffer.map(fmtLogLine).join('\n') : '(empty)');

  const readLogFile = (name: string): string => {
    const p = join(app.getPath('userData'), 'logs', name);
    if (!existsSync(p)) return `=== ${name} ===\n(not found at ${p})`;
    try {
      return `=== ${name} (${p}) ===\n` + readFileSync(p, 'utf-8');
    } catch (err) {
      return `=== ${name} (${p}) ===\n(unreadable: ${(err as Error)?.message ?? err})`;
    }
  };

  // backend.log: the backend writes this under userData/logs (config.LOG_DIR
  // mirrors the Electron userData path via KRYPT_SCOUT_USERDATA).
  // main.log: the on-disk mirror of the ring — its value here is the history
  // from PREVIOUS runs (crashes, GPU fallback) that the in-memory ring lost.
  return [header, '', ring, '', readLogFile('backend.log'), '', readLogFile('main.log'), ''].join(
    '\n',
  );
}

export function registerIpc(): void {
  // ── app ──────────────────────────────────────────────────────────
  ipcMain.handle('app:version', () => app.getVersion());
  ipcMain.handle('app:openExternal', (_e, url: string) => openExternalSafe(String(url)));
  ipcMain.handle('app:systemConfig', () => getSystemConfig());
  ipcMain.handle('app:setSystemConfig', (_e, partial) =>
    setSystemConfig(partial && typeof partial === 'object' ? partial : {}),
  );

  // ── backend lifecycle ────────────────────────────────────────────
  ipcMain.handle('backend:info', () => pythonBackend.info());
  ipcMain.handle('backend:port', () => pythonBackend.waitForPort());
  ipcMain.handle('backend:restart', () => pythonBackend.restart());
  ipcMain.handle('backend:logs', (_e, limit?: number) => {
    const n = Math.max(1, Math.min(limit ?? 500, LOG_MAX));
    return logBuffer.slice(-n);
  });
  ipcMain.handle('backend:exportLogs', async (e): Promise<LogExportResult> => {
    try {
      const win = BrowserWindow.fromWebContents(e.sender) ?? undefined;
      // File-name-safe timestamp: 2026-07-12T03-41-34.
      const stamp = new Date().toISOString().slice(0, 19).replace(/[:]/g, '-');
      const opts = {
        title: 'Export backend logs',
        defaultPath: join(
          app.getPath('downloads'),
          `krypt-scout-backend-log-${stamp}.txt`,
        ),
        filters: [{ name: 'Log file', extensions: ['txt', 'log'] }],
      };
      const res = win
        ? await dialog.showSaveDialog(win, opts)
        : await dialog.showSaveDialog(opts);
      if (res.canceled || !res.filePath) return { ok: false, canceled: true };
      writeFileSync(res.filePath, buildLogExport(), 'utf-8');
      return { ok: true, path: res.filePath };
    } catch (err) {
      return { ok: false, error: (err as Error)?.message ?? String(err) };
    }
  });

  // ── background job scheduler ─────────────────────────────────────
  ipcMain.handle('scheduler:info', () => scheduler.info());
  ipcMain.handle('scheduler:setConfig', (_e, partial: Partial<SchedulerConfig>) =>
    scheduler.setConfig(partial && typeof partial === 'object' ? partial : {}),
  );
  ipcMain.handle('scheduler:runNow', (_e, job: SchedulerJob) => scheduler.runNow(job));

  // ── discord picks webhooks ───────────────────────────────────────
  ipcMain.handle('discord:info', () => discordPicks.info());
  ipcMain.handle('discord:setConfig', (_e, partial: Partial<DiscordConfig>) =>
    discordPicks.setConfig(partial && typeof partial === 'object' ? partial : {}),
  );
  ipcMain.handle('discord:sendNow', () => discordPicks.sendNow());
  ipcMain.handle('discord:test', (_e, url: string) => discordPicks.test(String(url)));

  // ── auto-updates (scaffolding — 'unconfigured' until publish lands) ─
  ipcMain.handle('updater:state', () => updater.state());
  ipcMain.handle('updater:check', () => updater.check());
  ipcMain.handle('updater:downloadAndInstall', () => updater.downloadAndInstall());

  // ── window controls ──────────────────────────────────────────────
  ipcMain.on('window:minimize', (e) => BrowserWindow.fromWebContents(e.sender)?.minimize());
  ipcMain.on('window:maximize', (e) => {
    const w = BrowserWindow.fromWebContents(e.sender);
    if (!w) return;
    if (w.isMaximized()) w.unmaximize();
    else w.maximize();
  });
  ipcMain.on('window:close', (e) => BrowserWindow.fromWebContents(e.sender)?.close());
  ipcMain.handle('window:isMaximized', (e) =>
    BrowserWindow.fromWebContents(e.sender)?.isMaximized() ?? false,
  );
}
