import { app } from 'electron';
import { existsSync, mkdirSync, readFileSync, renameSync, writeFileSync } from 'node:fs';
import { dirname, join } from 'node:path';
import type { AppState } from '../../shared/types';

// Scout user data lives under app.getPath('userData') — on Windows that is
// %APPDATA%/Krypt Scout. settings.json holds the whole AppState.

const settingsFile = (): string => join(app.getPath('userData'), 'settings.json');

let cached: AppState | null = null;

function defaults(): AppState {
  return {};
}

export function load(): AppState {
  if (cached) return cached;
  try {
    const txt = readFileSync(settingsFile(), 'utf-8');
    const raw = JSON.parse(txt);
    cached = { ...defaults(), ...raw };
  } catch {
    cached = defaults();
  }
  return cached!;
}

export function get(): AppState {
  return cached ?? load();
}

export function save(next: AppState): AppState {
  cached = next;
  try {
    const file = settingsFile();
    const dir = dirname(file);
    if (!existsSync(dir)) mkdirSync(dir, { recursive: true });
    // Atomic-ish write: temp file then rename, so a crash mid-write can't
    // leave a truncated settings.json.
    const tmp = file + '.tmp';
    writeFileSync(tmp, JSON.stringify(next, null, 2), 'utf-8');
    renameSync(tmp, file);
  } catch (e) {
    console.error('settings save failed:', e);
  }
  return next;
}
