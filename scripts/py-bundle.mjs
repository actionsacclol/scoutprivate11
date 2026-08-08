// Bundle the Python backend with PyInstaller for packaging:
//   backend/dist-py/krypt-scout-backend/krypt-scout-backend.exe   (--onedir)
// electron-builder ships the whole dist-py tree under resources/python/ (see
// extraResources in package.json), so the packaged exe lives at
//   resources/python/krypt-scout-backend/krypt-scout-backend.exe
// and the supervisor (electron/system/python-backend.ts) launches it with
// `--port <port>`.
//
// --onedir, not --onefile: onefile has to unpack a ~150MB+ stack
// (pandas/sklearn/xgboost) to a temp dir on EVERY launch, which can blow
// through the supervisor's 60s startup-health window on cold disks/AV scans.
// onedir starts in a few seconds.
//
// The bundle is built from backend/krypt-scout-backend.spec — the SINGLE source
// of truth for what gets collected. CRITICAL: the spec's collect_all/
// copy_metadata pull in the lazy-imported Polymarket signing stack (eth_account
// + py_clob_client_v2 + poly_eip712_structs + py_order_utils + Crypto) that a
// plain CLI invocation silently drops. After the freeze we run the frozen exe's
// `--selftest`, which signs a deposit-wallet order offline — a non-zero exit
// aborts the build so a backend that can't sign an order never ships.
import { existsSync, rmSync } from 'node:fs';
import { spawnSync } from 'node:child_process';
import { join } from 'node:path';
import { PY_DIR, VENV_PY, ensureVenv, run, tryCmd } from './python-utils.mjs';

try {
  ensureVenv();

  // PyInstaller is a packaging-time-only dep (not in requirements.txt). Install
  // it on demand so `npm run py:bundle` is one command — a range, not a pin: an
  // exact pin can rot out of PyPI for a given Python. Any 6.x works here.
  if (!tryCmd(VENV_PY, ['-m', 'PyInstaller', '--version'])) {
    console.log('>> Installing PyInstaller into backend/.venv');
    run(VENV_PY, ['-m', 'pip', 'install', 'pyinstaller>=6.6,<7', '--disable-pip-version-check']);
  }

  console.log('>> Cleaning previous build');
  for (const d of ['dist-py', 'build-py']) {
    const p = join(PY_DIR, d);
    if (existsSync(p)) rmSync(p, { recursive: true, force: true });
  }

  // Build from the spec (NOT inline flags). Only distpath/workpath/noconfirm are
  // legal alongside a .spec — Analysis-shaping flags (--collect-*, --name, …)
  // live in the spec itself.
  console.log('>> Running PyInstaller from spec (this takes ~60s)');
  run(VENV_PY, [
    '-m', 'PyInstaller',
    '--noconfirm',
    '--distpath', 'dist-py',
    '--workpath', 'build-py',
    'krypt-scout-backend.spec',
  ]);

  const exe = join(
    PY_DIR, 'dist-py', 'krypt-scout-backend',
    process.platform === 'win32' ? 'krypt-scout-backend.exe' : 'krypt-scout-backend',
  );
  if (!existsSync(exe)) {
    throw new Error(`PyInstaller finished but ${exe} is missing`);
  }

  // Smoke-test the FROZEN bundle before electron-builder packages it. This is
  // the gate that catches a bundle which built clean but dropped a runtime dep
  // — e.g. eth_account is lazy-imported in polymarket_auth, so PyInstaller can
  // silently drop it and the app then fails on the user's machine at
  // wallet-connect ("eth-account is required"). The frozen exe's --selftest
  // imports the trading deps AND signs a POLY_1271 order offline; a non-zero
  // exit aborts the build so a broken backend can never reach a release.
  console.log('>> Selftest: verifying frozen bundle can import deps + sign an order');
  const st = spawnSync(exe, ['--selftest'], { stdio: 'inherit', windowsHide: true });
  if (st.error) {
    throw new Error(`could not run frozen backend selftest: ${st.error.message}`);
  }
  if (st.status !== 0) {
    console.error(
      '!! Frozen backend FAILED selftest (see report above). The bundle is missing\n' +
      '!! a runtime dependency or the order signer — refusing to ship it. Check that\n' +
      '!! backend/.venv has all of requirements.txt and that the .spec collects the\n' +
      '!! failing package, then rebuild.',
    );
    process.exit(1);
  }

  console.log(`>> OK — backend bundled + selftest PASSED at ${exe}`);
} catch (e) {
  console.error('!! Backend bundle failed:', e.message);
  process.exit(1);
}
