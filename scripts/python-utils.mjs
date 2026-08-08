// Shared helpers for the Python build/setup scripts. Pure Node.js,
// works on any system without PowerShell.
import { spawnSync } from 'node:child_process';
import { existsSync } from 'node:fs';
import { join, dirname } from 'node:path';
import { fileURLToPath } from 'node:url';

export const __dirname = dirname(fileURLToPath(import.meta.url));
export const ROOT = dirname(__dirname);
export const PY_DIR = join(ROOT, 'backend');
export const VENV = join(PY_DIR, '.venv');
export const VENV_PY = process.platform === 'win32'
  ? join(VENV, 'Scripts', 'python.exe')
  : join(VENV, 'bin', 'python');

const isWin = process.platform === 'win32';

/** Pretty-print a command line and run it synchronously, fail-fast on non-zero exit. */
export function run(cmd, args, opts = {}) {
  const display = `${cmd} ${args.map((a) => (a.includes(' ') ? `"${a}"` : a)).join(' ')}`;
  console.log(`> ${display}`);
  const r = spawnSync(cmd, args, {
    stdio: 'inherit',
    cwd: PY_DIR,
    shell: false,
    ...opts,
  });
  if (r.error) {
    throw new Error(`${cmd} could not be spawned: ${r.error.message}`);
  }
  if (r.status !== 0) {
    throw new Error(`${display} failed with exit code ${r.status}`);
  }
}

/** Try a command silently and return whether it succeeded. */
export function tryCmd(cmd, args) {
  try {
    const r = spawnSync(cmd, args, {
      stdio: 'ignore',
      shell: false,
      windowsHide: true,
    });
    return !r.error && r.status === 0;
  } catch {
    return false;
  }
}

/** Resolve a working system Python interpreter. Throws with a helpful message
 *  if none is found. Caches the result. */
let _systemPy = null;
export function findSystemPython() {
  if (_systemPy) return _systemPy;
  // Order: py launcher (Windows), python, python3.
  const candidates = isWin
    ? [
        { cmd: 'py', args: ['-3', '--version'], spawnAs: 'py' },
        { cmd: 'python', args: ['--version'], spawnAs: 'python' },
        { cmd: 'python3', args: ['--version'], spawnAs: 'python3' },
      ]
    : [
        { cmd: 'python3', args: ['--version'], spawnAs: 'python3' },
        { cmd: 'python', args: ['--version'], spawnAs: 'python' },
      ];
  for (const c of candidates) {
    if (tryCmd(c.cmd, c.args)) {
      _systemPy = c.spawnAs;
      return _systemPy;
    }
  }
  throw new Error(
    'No Python 3 interpreter found on PATH. Install Python 3.10+ from https://python.org and try again.',
  );
}

/** Args used to invoke the system python (e.g. `py -3` -> `['-3']`). */
export function systemPyPrefix() {
  return findSystemPython() === 'py' ? ['-3'] : [];
}

/** Ensure the Python venv exists and has all backend deps installed.
 *  No-op if the venv interpreter is already present and core imports work. */
export function ensureVenv({ force = false } = {}) {
  if (!existsSync(VENV) || force) {
    const sys = findSystemPython();
    console.log('>> Creating Python venv at backend/.venv');
    run(sys, [...systemPyPrefix(), '-m', 'venv', '.venv']);
  }
  if (!existsSync(VENV_PY)) {
    throw new Error(`venv created but no interpreter at ${VENV_PY}`);
  }
  // Verify deps; skip pip install only if ALL core imports resolve. This list
  // MUST include a probe for every dep group that can be added to
  // requirements.txt later — the trading stack (eth_account, py_clob_client_v2,
  // keyring, cryptography) was added after the venv already existed, and gating
  // only on the server/model core silently skipped installing it, so the frozen
  // backend shipped unable to sign a Polymarket order.
  const probe =
    'import fastapi, uvicorn, httpx, pandas, ' +
    'eth_account, py_clob_client_v2, keyring, cryptography';
  if (!force && tryCmd(VENV_PY, ['-c', probe])) {
    return;
  }
  console.log('>> Installing Python deps into venv (one-time, a few minutes)');
  run(VENV_PY, ['-m', 'pip', 'install', '--upgrade', 'pip', 'wheel', '--disable-pip-version-check']);
  run(VENV_PY, ['-m', 'pip', 'install', '-r', 'requirements.txt', '--disable-pip-version-check']);
}
