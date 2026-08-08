import { app } from 'electron';
import type { SystemConfig } from '../../shared/types';
import * as store from './settings-store';

// ──────────────────────────────────────────────────────────────────────
// System behavior: launch-at-login + close-to-tray. Both DEFAULT ON —
// Scout is an always-on companion (scheduler, auto-posts, auto-bet all
// live in this process), so the installer ships it booting with Windows
// silently in the tray and the X button hiding instead of quitting.
// Persisted in settings.json under `system`; toggled from SETTINGS.
// ──────────────────────────────────────────────────────────────────────

export function getSystemConfig(): SystemConfig {
  const s = store.get().system;
  return {
    autoStartOnLogin: s?.autoStartOnLogin !== false, // default ON
    closeToTray: s?.closeToTray !== false, // default ON
  };
}

export function setSystemConfig(partial: Partial<SystemConfig>): SystemConfig {
  const next: SystemConfig = {
    ...getSystemConfig(),
    ...(partial && typeof partial === 'object' ? partial : {}),
  };
  store.save({ ...store.get(), system: next });
  syncLoginItem();
  return next;
}

/**
 * Register/unregister the OS login item to match the saved setting.
 * Packaged builds only — in dev this would register the bare electron.exe
 * as a Windows startup app. `--hidden` makes the login boot silent: main.ts
 * sees the flag and keeps the window unrevealed (tray-only) on that launch.
 */
export function syncLoginItem(): void {
  if (!app.isPackaged) return;
  try {
    app.setLoginItemSettings({
      openAtLogin: getSystemConfig().autoStartOnLogin,
      args: ['--hidden'],
    });
  } catch (e) {
    console.error('login item sync failed:', e);
  }
}
