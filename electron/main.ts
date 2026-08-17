import { app, BrowserWindow, Menu, nativeImage, screen, session, Tray } from 'electron';
import type { NativeImage } from 'electron';
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

/** Locate a file from resources/ in both layouts: packaged (inside the asar,
 *  next to dist-electron) and dev (the repo's resources/ directory). */
function resourcePath(file: string): string | null {
  const candidates = [
    app.isPackaged
      ? join(process.resourcesPath, file)
      : join(process.cwd(), 'resources', file),
    join(__dirname, '..', 'resources', file),
  ];
  return candidates.find((c) => existsSync(c)) ?? null;
}

/** .ico is a WINDOWS format. macOS/Linux nativeImage decodes PNG/JPEG only, so
 *  handing them krypt.ico yields an EMPTY image — which is how the mac build
 *  ended up with an invisible tray icon (and, since `new Tray()` rejects an
 *  empty image, a throw that took the rest of bootstrap down with it). Every
 *  platform gets a format it can actually read. */
function iconFile(): string {
  return process.platform === 'win32' ? 'krypt.ico' : 'krypt.png';
}

function iconPath(): string | undefined {
  return resourcePath(iconFile()) ?? undefined;
}

/** Tray image, or null if this platform can't produce one. Never throws. */
function trayImage(): NativeImage | null {
  const p = resourcePath(iconFile());
  if (!p) return null;
  try {
    const img = nativeImage.createFromPath(p);
    if (img.isEmpty()) return null;
    // The macOS menu bar wants a small image; a full-resolution PNG renders
    // as an oversized icon (or gets dropped entirely).
    return process.platform === 'darwin' ? img.resize({ width: 18, height: 18 }) : img;
  } catch {
    return null;
  }
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
 *  Quit is the real exit (sets isQuitting so close-to-tray steps aside).
 *
 *  Best-effort by construction. A tray is a nicety; the window is the app. An
 *  unreadable icon or a desktop environment with no tray area must never cost
 *  the user their window, which is exactly what happened while this ran before
 *  createMainWindow() and was allowed to throw. */
function createTray(): void {
  if (tray) return;
  const image = trayImage();
  if (!image) {
    diagLog('warn', `no usable tray icon (${iconFile()}) — running without a tray`);
    return;
  }
  try {
    tray = new Tray(image);
  } catch (e) {
    diagLog('error', `tray creation failed: ${(e as Error)?.message ?? e}`);
    tray = null;
    return;
  }
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

/** Run one subsystem's init so a failure in it can never cost the user the
 *  window (or any of the other subsystems). Everything here is best-effort:
 *  the app is still usable without the tray, the scheduler or Discord. */
function safely(what: string, fn: () => void): void {
  try {
    fn();
  } catch (e) {
    diagLog('error', `${what} failed during startup: ${(e as Error)?.message ?? e}`);
  }
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

  // THE WINDOW COMES FIRST. It used to be the last thing bootstrap did, behind
  // the scheduler, the updater and the tray — so anything that threw on the way
  // (the mac tray icon did) left the process alive, the backend running and no
  // window ever created, with the rejection swallowed by the `void` on
  // whenReady().then(). A launch that gets this far always gets a window.
  //
  // A login-item (--hidden) boot creates the window but never reveals it:
  // everything (backend, scheduler, auto-posts, auto-bet) runs from the tray.
  createMainWindow(!START_HIDDEN);

  // Arm the scheduler BEFORE starting the backend so the very first
  // "running" transition is observed.
  safely('scheduler init', () => scheduler.init());
  // Daily Discord picks auto-post (timer only needs the wall clock; each
  // send resolves the backend port at fire time).
  safely('discord picks init', () => discordPicks.init());

  // Auto-update scaffolding: 'unconfigured' while build.publish is null;
  // once a publish provider ships, this arms one delayed startup check.
  // State changes are pushed to the renderer; nothing installs unopposed.
  safely('updater init', () => {
    updater.onState((state) => {
      for (const w of BrowserWindow.getAllWindows()) {
        if (!w.isDestroyed()) w.webContents.send('updater:state', state);
      }
    });
    updater.init();
  });

  void pythonBackend.start();
  safely('tray', createTray);
  // Keep the Windows Run-key registration in sync with the saved setting
  // (also repairs it if the user moved/reinstalled the app).
  safely('login item sync', syncLoginItem);

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
  // macOS: clicking the Dock/Finder icon of an ALREADY-RUNNING app does not
  // start a second process, so 'second-instance' — the Windows path back to a
  // hidden window — never fires here; the OS emits 'activate' instead. Without
  // this handler an app that booted to the tray (login item passes --hidden,
  // and autoStartOnLogin defaults ON) or that was hidden by close-to-tray had
  // NO way back on macOS: it stayed running and permanently invisible.
  app.on('activate', () => {
    const w = createMainWindow();
    if (w.isMinimized()) w.restore();
    w.show();
    w.focus();
  });
  void app.whenReady().then(bootstrap).catch((e) => {
    // Last resort. bootstrap guards each subsystem, so reaching here means
    // something structural broke — still better to show a window (the renderer
    // reports the backend as down) than to leave a running, invisible process.
    diagLog('error', `bootstrap failed: ${(e as Error)?.message ?? e}`);
    try {
      createMainWindow(!START_HIDDEN);
    } catch {
      /* nothing left to try */
    }
  });
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
