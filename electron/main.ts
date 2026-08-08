import { app, BrowserWindow, Menu, screen, session, Tray } from 'electron';
import { existsSync } from 'node:fs';
import { join } from 'node:path';
import type { LogEntry } from '../shared/types';
import { appendLog, registerIpc } from './ipc';
import { startDiscordRpc, stopDiscordRpc } from './system/discord';
import { discordPicks } from './system/discord-picks';
import { pythonBackend } from './system/python-backend';
import { scheduler } from './system/scheduler';
import * as store from './system/settings-store';
import { getSystemConfig, syncLoginItem } from './system/system-settings';
import { updater } from './system/updater';
import { guardWebContents } from './system/webSecurity';

process.env.DIST_ELECTRON = __dirname;
process.env.DIST = join(__dirname, '..', 'dist');
process.env.VITE_PUBLIC = app.isPackaged
  ? process.env.DIST
  : join(__dirname, '..', 'public');

if (process.platform === 'win32') {
  app.setAppUserModelId('cc.krypt.scout');
}

let mainWindow: BrowserWindow | null = null;
let tray: Tray | null = null;
/** Set once a real quit is underway — lets the close handler tell "user hit
 *  X" (hide to tray) apart from "app is quitting" (let the window close). */
let isQuitting = false;
/** Login-item launches pass --hidden: boot silently to the tray. */
const START_HIDDEN = process.argv.includes('--hidden');

/** Window/renderer diagnostics into the same ring buffer users export from
 *  SETTINGS → export logs (appendLog also mirrors it to userData/logs/main.log
 *  for the machines where SETTINGS itself never paints). A renderer that never
 *  paints (the "black screen" report) is otherwise invisible: packaged builds
 *  have no devtools and the window happily shows its #0A0A0F backgroundColor
 *  forever. */
function diagLog(level: string, msg: string): void {
  const entry: LogEntry = { ts: new Date().toISOString(), level, source: 'window', msg };
  appendLog(entry);
  for (const w of BrowserWindow.getAllWindows()) {
    if (!w.isDestroyed()) w.webContents.send('logs:append', entry);
  }
}

function iconPath(): string {
  const candidates = [
    app.isPackaged
      ? join(process.resourcesPath, 'krypt.ico')
      : join(process.cwd(), 'resources', 'krypt.ico'),
    join(__dirname, '..', 'resources', 'krypt.ico'),
  ];
  for (const c of candidates) {
    if (existsSync(c)) return c;
  }
  return candidates[0];
}

function createMainWindow(reveal = true): BrowserWindow {
  if (mainWindow && !mainWindow.isDestroyed()) {
    if (mainWindow.isMinimized()) mainWindow.restore();
    mainWindow.show();
    mainWindow.focus();
    return mainWindow;
  }
  const state = store.get();
  const bounds = state.windowBounds;
  const primary = screen.getPrimaryDisplay();
  const width = bounds?.width ?? Math.min(1180, primary.workArea.width - 40);
  const height = bounds?.height ?? Math.min(760, primary.workArea.height - 40);

  mainWindow = new BrowserWindow({
    width,
    height,
    minWidth: 980,
    minHeight: 640,
    x: bounds?.x,
    y: bounds?.y,
    backgroundColor: '#0A0A0F',
    show: false,
    frame: false,
    titleBarStyle: 'hidden',
    icon: iconPath(),
    webPreferences: {
      preload: join(__dirname, 'preload.js'),
      contextIsolation: true,
      sandbox: true,
      nodeIntegration: false,
      devTools: !app.isPackaged,
      backgroundThrottling: false,
    },
  });

  // Never let the renderer open child windows or navigate away.
  guardWebContents(mainWindow);

  const url = process.env['VITE_DEV_SERVER_URL'];
  const loadApp = (): void => {
    if (!mainWindow || mainWindow.isDestroyed()) return;
    if (url) void mainWindow.loadURL(url);
    else void mainWindow.loadFile(join(process.env.DIST!, 'index.html'));
  };
  loadApp();

  // Field diagnostics + self-heal. Every way the renderer can silently die is
  // logged into the exportable ring buffer, and load/renderer failures get a
  // bounded number of retries instead of a permanently black window.
  const wc = mainWindow.webContents;
  let recoveries = 0;
  const recover = (why: string): void => {
    diagLog('error', why);
    if (recoveries >= 2) return;
    recoveries += 1;
    setTimeout(loadApp, 1000);
  };
  wc.on('did-fail-load', (_e, code, desc, failedUrl, isMainFrame) => {
    // -3 (ERR_ABORTED) fires on cancelled in-page loads; not a failure.
    if (!isMainFrame || code === -3) return;
    recover(`load failed: ${code} ${desc} (${failedUrl})`);
  });
  wc.on('render-process-gone', (_e, details) => {
    recover(`renderer gone: ${details.reason} (exitCode=${details.exitCode})`);
  });
  wc.on('preload-error', (_e, path, error) => {
    diagLog('error', `preload error in ${path}: ${error.message}`);
  });
  wc.on('console-message', (_e, level, message, line, sourceId) => {
    if (level < 3) return; // errors only — warnings would flood the ring
    diagLog('error', `renderer console: ${message} (${sourceId}:${line})`);
  });

  // Show on ready-to-show, with a fallback so a renderer that never paints
  // (bad GPU, stuck font fetch) still gets a window instead of a ghost app.
  // A --hidden (login-item) boot skips the reveal entirely: the app lives in
  // the tray until the user opens it.
  if (reveal) {
    let shown = false;
    const doReveal = (): void => {
      if (shown) return;
      shown = true;
      if (mainWindow && !mainWindow.isDestroyed()) mainWindow.show();
    };
    mainWindow.once('ready-to-show', doReveal);
    setTimeout(() => {
      if (!shown) diagLog('warn', 'renderer not ready after 4s — revealing window anyway');
      doReveal();
    }, 4000);
  }

  // X hides to the tray (Scout is an always-on companion — scheduler,
  // auto-posts and auto-bet run in this process); quitting for real comes
  // from the tray menu or the OS. Toggleable under SETTINGS → SYSTEM.
  mainWindow.on('close', (e) => {
    if (isQuitting || !getSystemConfig().closeToTray) return;
    e.preventDefault();
    mainWindow?.hide();
  });

  // Persist window bounds, debounced.
  let boundsTimer: NodeJS.Timeout | null = null;
  const persistBounds = (): void => {
    if (boundsTimer) clearTimeout(boundsTimer);
    boundsTimer = setTimeout(() => {
      boundsTimer = null;
      if (!mainWindow || mainWindow.isDestroyed()) return;
      if (mainWindow.isMaximized() || mainWindow.isMinimized()) return;
      const b = mainWindow.getBounds();
      store.save({ ...store.get(), windowBounds: b });
    }, 500);
  };
  mainWindow.on('move', persistBounds);
  mainWindow.on('resize', persistBounds);
  mainWindow.on('maximize', () => mainWindow?.webContents.send('window:maximizeChange', true));
  mainWindow.on('unmaximize', () => mainWindow?.webContents.send('window:maximizeChange', false));
  mainWindow.on('closed', () => {
    mainWindow = null;
  });

  return mainWindow;
}

/**
 * One onHeadersReceived hook for two jobs (Chromium keeps only the LAST
 * listener per session, so they must live together):
 *  1. CORS bridge: the renderer (http://localhost:5183 in dev, file:// when
 *     packaged) fetches the local FastAPI backend cross-origin. The backend's
 *     CORS allowlist doesn't know our origin, so we stamp
 *     Access-Control-Allow-Origin onto 127.0.0.1 responses here instead of
 *     touching backend code.
 *  2. CSP when packaged, per suite convention.
 */
function installWebRequestHooks(): void {
  session.defaultSession.webRequest.onHeadersReceived((details, cb) => {
    const headers: Record<string, string | string[]> = {
      ...(details.responseHeaders as Record<string, string | string[]>),
    };
    try {
      const u = new URL(details.url);
      if (u.hostname === '127.0.0.1') {
        for (const k of Object.keys(headers)) {
          if (k.toLowerCase() === 'access-control-allow-origin') delete headers[k];
        }
        headers['Access-Control-Allow-Origin'] = ['*'];
      }
    } catch {
      /* not a URL we care about */
    }
    if (app.isPackaged) {
      headers['Content-Security-Policy'] = [
        "default-src 'self'; script-src 'self'; " +
        "style-src 'self' 'unsafe-inline' https://fonts.googleapis.com; " +
        "font-src 'self' https://fonts.gstatic.com; img-src 'self' data:; " +
        "connect-src 'self' http://127.0.0.1:*",
      ];
    }
    cb({ responseHeaders: headers });
  });
}

/** Tray icon: the app's persistent home. Click / "Open" shows the window;
 *  Quit is the real exit (sets isQuitting so close-to-tray steps aside). */
function createTray(): void {
  if (tray) return;
  tray = new Tray(iconPath());
  tray.setToolTip('Krypt Scout');
  tray.setContextMenu(
    Menu.buildFromTemplate([
      {
        label: 'Open Krypt Scout',
        click: () => {
          createMainWindow().focus();
        },
      },
      { type: 'separator' },
      {
        label: 'Quit Krypt Scout',
        click: () => {
          isQuitting = true;
          app.quit();
        },
      },
    ]),
  );
  tray.on('click', () => {
    createMainWindow().focus();
  });
}

async function bootstrap(): Promise<void> {
  registerIpc();
  installWebRequestHooks();
  store.load();

  const fanOutLog = (entry: LogEntry): void => {
    appendLog(entry);
    for (const w of BrowserWindow.getAllWindows()) {
      if (!w.isDestroyed()) w.webContents.send('logs:append', entry);
    }
  };
  pythonBackend.onLog(fanOutLog);
  scheduler.onLog(fanOutLog);
  discordPicks.onLog(fanOutLog);

  // Arm the scheduler BEFORE starting the backend so the very first
  // "running" transition is observed.
  scheduler.init();
  // Daily Discord picks auto-post (timer only needs the wall clock; each
  // send resolves the backend port at fire time).
  discordPicks.init();

  // Auto-update scaffolding: 'unconfigured' while build.publish is null;
  // once a publish provider ships, this arms one delayed startup check.
  // State changes are pushed to the renderer; nothing installs unopposed.
  updater.onState((state) => {
    for (const w of BrowserWindow.getAllWindows()) {
      if (!w.isDestroyed()) w.webContents.send('updater:state', state);
    }
  });
  updater.init();

  void pythonBackend.start();
  createTray();
  // Keep the Windows Run-key registration in sync with the saved setting
  // (also repairs it if the user moved/reinstalled the app).
  syncLoginItem();
  // A login-item (--hidden) boot creates the window but never reveals it:
  // everything (backend, scheduler, auto-posts, auto-bet) runs from the tray.
  createMainWindow(!START_HIDDEN);

  // Rich presence is best-effort; a missing Discord must never slow launch.
  void startDiscordRpc();
}

// Test/CI escape hatch: point userData at an isolated profile so a packaged
// build can run alongside a dev instance without sharing settings, logs, the
// backend SQLite, or (crucially) the single-instance lock — the lock is keyed
// on the userData path, so this must happen BEFORE requestSingleInstanceLock.
const testProfile = process.env.KRYPT_SCOUT_TEST_PROFILE;
if (testProfile) {
  app.setPath('userData', testProfile);
}

// GPU black-screen guard. On some machines (macOS in particular) the GPU
// process crash-loops and the window never paints anything but its
// backgroundColor. If a previous run flagged that (below), or the user passes
// --disable-gpu, render on CPU. Must run before app ready — and after the
// test-profile redirect, since settings live under userData.
if (store.load().disableGpu || process.argv.includes('--disable-gpu')) {
  app.disableHardwareAcceleration();
}

// Two GPU-process crashes in one run = compositing is broken here, not a
// one-off: persist the fallback and relaunch on CPU rendering. Chromium
// restarts a crashed GPU process on its own, so a single crash just gets
// logged and left alone.
let gpuCrashes = 0;
app.on('child-process-gone', (_e, details) => {
  if (details.type !== 'GPU' || details.reason === 'clean-exit') return;
  gpuCrashes += 1;
  diagLog('error', `GPU process gone: ${details.reason} (exitCode=${details.exitCode}, #${gpuCrashes})`);
  if (gpuCrashes < 2 || store.get().disableGpu) return;
  store.save({ ...store.get(), disableGpu: true });
  isQuitting = true;
  app.relaunch();
  app.quit();
});

const gotLock = app.requestSingleInstanceLock();
if (!gotLock) {
  app.quit();
} else {
  app.on('second-instance', () => {
    const w = createMainWindow();
    if (w.isMinimized()) w.restore();
    w.show();
    w.focus();
  });
  void app.whenReady().then(bootstrap);
}

app.on('window-all-closed', () => {
  // Only reachable when close-to-tray is OFF (the close handler prevents
  // the window from ever closing otherwise) — then X means quit.
  app.quit();
});

app.on('before-quit', async () => {
  isQuitting = true;
  tray?.destroy();
  tray = null;
  updater.stop();
  scheduler.stop();
  discordPicks.stop();
  stopDiscordRpc();
  await pythonBackend.stop();
});
