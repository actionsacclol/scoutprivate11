import { shell, type BrowserWindow } from 'electron';

/**
 * Web-content hardening shared by every window. The app only ever loads its
 * own local HTML (file:// in prod, the Vite dev server in dev), and every
 * link out of the app must go through the system browser — so anything else
 * (a stray in-app navigation, a drag-dropped URL, a non-web scheme handed to
 * the OS) is a bug or an attack and gets dropped.
 */

const EXTERNAL_SCHEMES = new Set(['http:', 'https:', 'mailto:']);

/** Open a URL in the system browser — but only web/mail schemes. `file:`,
 *  `smb:`, `ms-*:` and custom protocol handlers are refused: passed to the
 *  OS they can read files or launch registered applications. */
export function openExternalSafe(url: string): Promise<void> {
  let scheme = '';
  try {
    scheme = new URL(url).protocol;
  } catch {
    /* not a URL at all */
  }
  if (!EXTERNAL_SCHEMES.has(scheme)) {
    console.warn(`webSecurity: blocked openExternal to non-web URL: ${String(url).slice(0, 120)}`);
    return Promise.resolve();
  }
  return shell.openExternal(url);
}

/** True for origins the app itself is served from. */
function isAppUrl(url: string): boolean {
  if (url.startsWith('file://')) return true;
  const dev = process.env.VITE_DEV_SERVER_URL;
  if (dev) {
    try {
      return new URL(url).origin === new URL(dev).origin;
    } catch {
      return false;
    }
  }
  return false;
}

/** Lock a window to app-local content: top-frame navigation anywhere else is
 *  cancelled (external http(s) targets open in the system browser instead),
 *  and window.open is always denied (external URLs → system browser). A
 *  remote page must never gain access to the preload's window.scout surface. */
export function guardWebContents(win: BrowserWindow): void {
  win.webContents.on('will-navigate', (e, url) => {
    if (isAppUrl(url)) return;
    e.preventDefault();
    void openExternalSafe(url);
  });
  win.webContents.setWindowOpenHandler(({ url }) => {
    void openExternalSafe(url);
    return { action: 'deny' };
  });
}
