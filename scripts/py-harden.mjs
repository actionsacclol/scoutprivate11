// Build a HARDENED backend bundle: Nuitka-compile the sensitive modules to
// native .pyd, then run the normal PyInstaller freeze so those modules ship as
// machine code with NO recoverable bytecode. See HARDENING.md (Layer 3).
//
// Why native compilation (not PyArmor): the app is unsigned and must not trip
// antivirus. Compiled C looks like normal software; a runtime-unpacking
// obfuscator does not. Nuitka auto-downloads its own zig/clang backend, so no
// Visual Studio / MSVC install is needed.
//
// SAFETY MODEL — the source tree is sacred:
//   1. Each target's .py is MOVED to a backup dir (exact bytes preserved).
//   2. The compiled .pyd is placed where the .py was.
//   3. The normal freeze runs (PyInstaller collects the .pyd, not source).
//   4. A finally block ALWAYS restores every .py and deletes the staged .pyd,
//      even on crash/Ctrl-C — so `pytest` / `npm run dev` are never affected.
// If this ever dies so hard the finally doesn't run, restore by hand from
//   backend/build-harden-backup/   (mirrors the source paths)
//
// Run:  node scripts/py-harden.mjs        (or npm run py:bundle:hardened)
import {
  existsSync, mkdirSync, renameSync, rmSync, readdirSync, copyFileSync,
} from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join, dirname, basename } from 'node:path';
import { PY_DIR, VENV_PY, ROOT, run, tryCmd } from './python-utils.mjs';

// Modules whose ALGORITHMS are the edge — compiled to native .pyd. Paths are
// relative to backend/. START SMALL: validate the frozen build (selftest must
// pass) with a leaf module, then widen. When adding a module that imports a
// third-party package NO remaining .py imports, add that package to
// hiddenimports in krypt-scout-backend.spec (a compiled .pyd is opaque to
// PyInstaller's source analysis, so its imports must be collected explicitly).
const HARDEN_MODULES = [
  // The edge: pricing math, the autobet money rules, the order signer, the
  // game<->market matcher, pick derivation. All use absolute imports (verified)
  // and their third-party deps (httpx/cryptography/eth_*/keyring) are already
  // explicitly collected in krypt-scout-backend.spec, so removing the .py can't
  // drop a dependency PyInstaller needs.
  'kryptscout/license.py',
  // api/main.py holds the app-wide _license_gate middleware. Compiling
  // license.py alone left the CHECK native but its CALL SITE as bytecode —
  // patching the middleware out of main.pyc bypassed the gate without touching
  // the protected module. Requires fastapi/starlette/pydantic in the spec's
  // hiddenimports (see the note there): nothing else imports them from source.
  'kryptscout/api/main.py',
  'kryptscout/core/oddsmath.py',
  'kryptscout/core/elo.py',
  'kryptscout/store/picks.py',
  'kryptscout/odds/polymarket.py',
  'kryptscout/autobet/engine.py',
  'kryptscout/autobet/polymarket_api.py',
  'kryptscout/autobet/polymarket_auth.py',
  // sports/*.py — the model + feature engineering, one engine per sport. This
  // is the bulk of the modeling edge. Prerequisites handled: the spec now names
  // numpy/pandas/sklearn/scipy/xgboost in hiddenimports (a compiled .pyd hides
  // those imports from PyInstaller), and Nuitka --mode=module resolves the
  // relative `from .base import SportEngine` at runtime (verified: build_engine
  // returns a real SportEngine subclass). base.py stays SOURCE — it's the ABC +
  // registry (structural, not the edge) and the import target for the above.
  'kryptscout/sports/nba.py',
  'kryptscout/sports/nfl.py',
  'kryptscout/sports/nhl.py',
  'kryptscout/sports/mlb.py',
  'kryptscout/sports/wnba.py',
  'kryptscout/sports/ncaaf.py',
  'kryptscout/sports/ncaab.py',
  'kryptscout/sports/soccer.py',
  'kryptscout/sports/tennis.py',
];

const BACKUP = join(PY_DIR, 'build-harden-backup');
const NUITKA_OUT = join(PY_DIR, 'build-harden-nuitka');

// Nuitka names its module output after CPython's EXT_SUFFIX: `.pyd` on Windows
// (oddsmath.cp314-win_amd64.pyd), `.so` everywhere else
// (oddsmath.cpython-311-darwin.so). Matching only `.pyd` makes every non-Windows
// run die in compile() with "produced no .pyd" — which is exactly what a macOS
// CI job hits when it calls dist:hardened.
const EXT = process.platform === 'win32' ? '.pyd' : '.so';

function ensureNuitka() {
  if (tryCmd(VENV_PY, ['-m', 'nuitka', '--version'])) return;
  console.log('>> Installing Nuitka into backend/.venv');
  run(VENV_PY, ['-m', 'pip', 'install', 'nuitka>=2.4', '--disable-pip-version-check']);
}

/** Compile one backend-relative .py to a native module, return its abs path. */
function compile(relPath) {
  console.log(`>> Nuitka compiling ${relPath}`);
  run(VENV_PY, [
    '-m', 'nuitka', '--mode=module', relPath,
    `--output-dir=${NUITKA_OUT}`,
    '--assume-yes-for-downloads', '--no-pyi-file', '--remove-output', '--quiet',
  ]);
  const base = basename(relPath, '.py');
  // Nuitka emits <base>.<abi-tag><EXT> (oddsmath.cp314-win_amd64.pyd,
  // oddsmath.cpython-311-darwin.so).
  const hit = readdirSync(NUITKA_OUT).find(
    (f) => (f === `${base}${EXT}` || f.startsWith(`${base}.`)) && f.endsWith(EXT),
  );
  if (!hit) throw new Error(`Nuitka produced no ${EXT} for ${relPath}`);
  return join(NUITKA_OUT, hit);
}

const staged = []; // { srcAbs, backupAbs, pydAbs }

function stage(relPath) {
  const srcAbs = join(PY_DIR, relPath);
  if (!existsSync(srcAbs)) throw new Error(`target not found: ${relPath}`);
  const pydSrc = compile(relPath);

  const backupAbs = join(BACKUP, relPath);
  mkdirSync(dirname(backupAbs), { recursive: true });
  renameSync(srcAbs, backupAbs); // move the .py out of the tree

  const pydAbs = join(dirname(srcAbs), basename(pydSrc));
  copyFileSync(pydSrc, pydAbs); // drop the .pyd where the .py was
  // Nuke any stale bytecode so nothing shadows the .pyd.
  const pyc = join(dirname(srcAbs), '__pycache__');
  if (existsSync(pyc)) rmSync(pyc, { recursive: true, force: true });

  staged.push({ srcAbs, backupAbs, pydAbs });
  console.log(`   staged ${relPath} -> ${basename(pydAbs)}`);
}

function restore() {
  for (const { srcAbs, backupAbs, pydAbs } of staged) {
    try { if (existsSync(pydAbs)) rmSync(pydAbs, { force: true }); } catch {}
    try { if (existsSync(backupAbs)) renameSync(backupAbs, srcAbs); } catch (e) {
      console.error(`!! FAILED to restore ${srcAbs} from ${backupAbs}: ${e.message}`);
      console.error('!! Restore it by hand before running the app or tests.');
    }
  }
  for (const d of [BACKUP, NUITKA_OUT]) {
    try { if (existsSync(d)) rmSync(d, { recursive: true, force: true }); } catch {}
  }
}

// A `finally` block does NOT run on Ctrl-C — Node's default SIGINT handling
// terminates the process without unwinding. Nuitka takes minutes, which is
// exactly when a person reaches for Ctrl-C, and at that moment the source tree
// is gutted: the .py files live in build-harden-backup/ and nothing is left to
// put them back. Installing a handler is what makes the guarantee at the top of
// this file true. Once ANY listener exists Node stops killing the process, so
// the explicit exit is required or Ctrl-C would do nothing at all.
for (const sig of ['SIGINT', 'SIGTERM']) {
  process.on(sig, () => {
    console.error(`\n!! ${sig} — restoring the source tree before exiting`);
    restore();
    process.exit(130);
  });
}

function sanityImport() {
  // Confirm every compiled module still imports (as the package module) with
  // the .py gone — a fast fail BEFORE the multi-minute freeze.
  const names = HARDEN_MODULES.map(
    (p) => p.replace(/\.py$/, '').replace(/\//g, '.'),
  );
  const probe = names.map((n) => `import ${n}`).join('; ');
  console.log('>> Sanity: importing compiled modules');
  run(VENV_PY, ['-c', probe]);
}

try {
  ensureNuitka();
  for (const d of [BACKUP, NUITKA_OUT]) {
    if (existsSync(d)) rmSync(d, { recursive: true, force: true });
  }
  console.log(`>> Hardening ${HARDEN_MODULES.length} module(s) to native ${EXT}`);
  for (const m of HARDEN_MODULES) stage(m);
  sanityImport();

  console.log('>> Running the normal frozen build against the compiled tree');
  const r = spawnSync('node', [join(ROOT, 'scripts', 'py-bundle.mjs')], {
    stdio: 'inherit', cwd: ROOT, shell: false,
  });
  if (r.status !== 0) throw new Error(`py-bundle failed (exit ${r.status})`);

  console.log(`>> HARDENED bundle built — sensitive modules ship as native ${EXT}`);
} catch (e) {
  console.error('!! Hardened bundle failed:', e.message);
  process.exitCode = 1;
} finally {
  restore();
  console.log(`>> Source tree restored (.py back, staged ${EXT} removed)`);
}
