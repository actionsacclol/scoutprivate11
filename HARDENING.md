# Hardening

Reverse-engineering resistance for the packaged app. **Read this first:** no
client-side app can be made uncrackable — the CPU must execute your real logic,
so your real logic is recoverable from disk given enough effort. Every measure
here *raises the cost* of cracking/tampering; none is a wall.

## Two governing constraints (this project)

1. **No code-signing certificate, ever.** So we can't remove the SmartScreen
   "unknown publisher" prompt (that needs a cert or download-reputation), and
   we can't cryptographically prevent exe replacement. We optimize for the
   *other* half: not getting **quarantined/deleted** by antivirus.
2. **Must not trip AV false positives.** This RULES OUT the aggressive options.
   Unsigned + a runtime-unpacking guard + anti-debug is the exact profile AV
   heuristics quarantine — so **PyArmor is off the table**, as is UPX packing
   and onefile self-extraction. The AV-safe way to protect code is to *compile*
   it: native code is what normal software looks like.

| Approach | Protection | AV-flag risk (unsigned) | Verdict |
|---|---|---|---|
| PyArmor + anti-debug | high | **high** | ✗ violates constraint 2 |
| UPX packing | low | **very high** | ✗ (already disabled in the spec) |
| onefile self-extract | none | medium | ✗ (already onedir) |
| **Nuitka → native code** | high (no bytecode to recover) | **very low** | ✓ the real protection |
| Bytecode `optimize=2` | low | none | ✓ free, done |
| Embedded exe metadata | none | **lowers** risk | ✓ done |

## What's actually worth protecting here

**There is no secret model file.** `models/*.pkl` is gitignored and ships with
nothing — every install trains its own model under `userData/` from PUBLIC data
(nflverse, ESPN, tennis/football-data). So the edge is not a weight file an
attacker copies; it is **reproducible from the code**. That means:

- Encrypting model files would protect nothing (they're regenerated locally).
- The IP is entirely in the **code** — feature engineering, the Elo configs and
  blend weights, the autobet money rules, the game↔market matcher.
- Protecting the edge therefore means protecting *code*, and the only AV-safe
  way to do that is **native compilation** (see Layer 3). Everything short of
  that hardens the shell but leaves the algorithms decompilable.

## Layer 1 — Electron Fuses (DONE)

`build/afterPack.cjs` (wired via `build.afterPack` in package.json) burns fuse
bits into the packaged Electron binary after electron-builder lays it down.
Fuses live in the executable, not in `app.asar`, so they survive an attacker
unpacking and repacking the asar.

| Fuse | Set to | Why |
|---|---|---|
| `RunAsNode` | **off** | Closes the #1 hole: `ELECTRON_RUN_AS_NODE=1 "Krypt Scout.exe" -e "<js>"` would otherwise run arbitrary Node **inside the app** — full FS access, your IPC surface, and DPAPI-decryptable wallet creds. |
| `EnableNodeOptionsEnvironmentVariable` | off | No `NODE_OPTIONS` injection at launch. |
| `EnableNodeCliInspectArguments` | off | No `--inspect` debugger attach on the shipped app. |
| `OnlyLoadAppFromAsar` | on | Loads only the packed asar, never a swapped-in `app/` directory. |
| `EnableCookieEncryption` | on | Encrypts the on-disk cookie store. |
| `EnableEmbeddedAsarIntegrityValidation` | opt-in | See below. |

All safe for this app: it spawns the Python backend as a separate process and
never relaunches its own binary as Node (verified in
`electron/system/python-backend.ts` — no `process.execPath`, `RUN_AS_NODE`, or
`NODE_OPTIONS` anywhere).

### asar integrity (opt-in until verified)

`EnableEmbeddedAsarIntegrityValidation` makes a tampered `app.asar` refuse to
load — real anti-tamper. It requires electron-builder to inject the asar header
hash into the binary, which is platform/version-sensitive, so it is **off by
default** to guarantee the app launches. To enable and verify:

```sh
SCOUT_ASAR_INTEGRITY=1 npm run dist
# then install and launch the build. If it opens normally, the integrity
# fuse is working. If it fails to launch, this electron-builder version
# isn't injecting the hash on this platform — leave it off.
```

Once a build launches clean with it on, make it the default in `afterPack.cjs`.

### Verify the fuses took

After `npm run dist`, against the unpacked binary
(`release/win-unpacked/Krypt Scout.exe`):

```sh
npx @electron/fuses read --app "release/win-unpacked/Krypt Scout.exe"
# RunAsNode should read `false`.
# Sanity smoke: this must NOT print node's version anymore —
set ELECTRON_RUN_AS_NODE=1 && "release\win-unpacked\Krypt Scout.exe" -e "console.log(process.version)"
```

## Layer 2 — Python bytecode strip + legit exe metadata (DONE)

In `backend/krypt-scout-backend.spec`:

- **`optimize=2`** on the `Analysis` — strips asserts + docstrings from the
  frozen `.pyc`. Verified functionally safe (no runtime asserts; only cosmetic
  `/docs` summaries and CLI `--help` text use `__doc__`). The point: this
  codebase's docstrings spell out the money rules in plain English, so stripping
  them removes the free reading material from anything that decompiles the pyc.
  It does NOT stop decompilation.
- **`version='version_info.txt'` + `icon=`** on the `EXE` — embeds real Windows
  VERSIONINFO (CompanyName/ProductName/version) and the app icon. A bare,
  metadata-less unsigned binary is a textbook AV heuristic trigger; giving it
  provenance measurably lowers false-positive quarantine risk. Keep the version
  in `backend/version_info.txt` in sync with `package.json`.

## Layer 3 — Native compilation with Nuitka (DONE — 7 modules)

The one step that actually protects the algorithms, and AV-safe (native code,
no runtime guard, no packer). Nuitka auto-downloads its own **zig/clang**
backend — no Visual Studio / MSVC needed (the reason it fits the "won't install
a toolchain" constraint where Cython does not).

**Build it:**

```sh
npm run py:bundle:hardened      # hardened backend only (Nuitka + PyInstaller + selftest)
npm run dist:hardened           # full hardened installer (this is the release command)
```

**Platform note:** Nuitka names module output after CPython's `EXT_SUFFIX` —
`.pyd` on Windows, `.so` on macOS/Linux (`license.cpython-311-darwin.so`).
`py-harden.mjs` derives the extension from `process.platform`; it used to match
`.pyd` only, which made `dist:hardened` die with "Nuitka produced no .pyd" on
every non-Windows runner. **The macOS/Linux path has not yet been built** —
Windows is validated, the other two need a CI run to confirm.

**How it works** (`scripts/py-harden.mjs`): compiles each module in
`HARDEN_MODULES` to a native `.pyd`/`.so`, MOVES the `.py` out of the tree,
places the compiled module, runs the normal freeze (PyInstaller ships the `.pyd`, no `.py`/`.pyc`),
then a `finally` block ALWAYS restores the source and deletes the `.pyd`. So
`pytest` / `npm run dev` see pristine `.py`; only the shipped bundle is compiled.
If it ever dies mid-run, restore by hand from `backend/build-harden-backup/`.

**Currently compiled — 17 modules** (validated: frozen `--selftest` passes incl.
offline order signing; the frozen exe boots and serves `/api/autobet` through
the compiled engine/auth AND `/api/sports` with all 9 engines loaded from their
`.pyd`, including `nfl-xgb-v3` — proving the compiled engines + the
xgboost/sklearn stack resolve correctly):

```
license.py  api/main.py
core/oddsmath.py  core/elo.py  store/picks.py  odds/polymarket.py
autobet/engine.py  autobet/polymarket_api.py  autobet/polymarket_auth.py
sports/{nba,nfl,nhl,mlb,wnba,ncaaf,ncaab,soccer,tennis}.py
```

### Why `api/main.py` is on that list

`license.py` compiled alone protected the *check* but left its *call site* as
bytecode: `_license_gate` (the app-wide 402 middleware) lives in `api/main.py`,
so the cheapest bypass was never touching the native license module at all —
just patching the middleware out of `main.pyc`. Both now ship as native code.

Validated end to end on the frozen bundle: 48 routes register, `/api/health`
and `/api/license` answer 200, and `/api/board/*` + `/api/picks` return
`402 License required` from the compiled middleware.

Prerequisite this needed: `fastapi`/`starlette`/`pydantic` are imported ONLY by
`api/main.py` (`run_backend.py`, the Analysis entry point, imports just uvicorn
and the app), so compiling it hid them from PyInstaller. They are now collected
via `collect_submodules` in the spec — **not** bare names, which ship a fastapi
package without `fastapi.middleware.cors` and fail the selftest at import.

Proof the protection is real: `find backend/dist-py -iname "nfl.py*"` (or any of
the above) returns only the `.pyd` — no bytecode ships for these modules.

Prereqs that made the sports tier work, now baked in:
- `krypt-scout-backend.spec` names `numpy/pandas/sklearn/scipy/xgboost` in
  `hiddenimports` — those are imported ONLY by `sports/*.py`, so a compiled
  `.pyd` (opaque to source analysis) would otherwise drop the whole stack.
- Nuitka `--mode=module` resolves the relative `from .base import SportEngine`
  in `nba.py`/`wnba.py` at runtime (verified: `build_engine()` returns a real
  `SportEngine` subclass). `base.py` stays SOURCE — it's the ABC + registry.

**Further candidates** (not yet compiled; lower IP density): `core/totals.py`,
`core/schemas.py`, `datasources/*.py`. Same procedure: confirm imports, add to
`HARDEN_MODULES`, run `npm run py:bundle:hardened`, confirm the selftest.

**CI note:** the release job calls `dist:hardened` (`.github/workflows/
release.yml`) — it previously called plain `dist`, which meant every installer
CI produced shipped all of the above as decompilable `.pyc` with the Layer-3
protection absent. Its runners must allow Nuitka's one-time zig download
(cached after first run). Python 3.14 is
"experimentally supported" by Nuitka 4.1.3 — validated here by exhaustive
output comparison (1.4M cases, bit-identical) + the frozen boot test, but pin
Nuitka and re-run those checks on any interpreter/Nuitka bump.

## Layer 0 — The one thing a cert would buy (won't be done)

Without an OV/EV cert: the SmartScreen "unknown publisher" prompt stays on every
install, and nothing cryptographically stops exe replacement. That's *reputation
+ tamper-proofing*, a different axis from AV quarantine. Accepted, documented in
the README. If a cert is ever obtained, wire `build.win.signtoolOptions` /
`afterSign` and turn on `EnableEmbeddedAsarIntegrityValidation` (Layer 1).

## Already strong

- **Renderer + main JS**: minified by esbuild, no sourcemaps shipped.
- **Wallet credentials**: DPAPI (Windows) / OS keychain (mac/Linux) encrypted
  at rest, `chmod 0600` — see `backend/kryptscout/autobet/polymarket_auth.py`.
- **webPreferences**: `contextIsolation`, `sandbox`, `nodeIntegration: false`,
  devTools off in production, navigation locked to app-local content.

## Deliberately NOT doing

- **PyArmor / any anti-debug packer** — violates the no-AV-flags constraint
  (see the table up top). This is the trap: it feels like "more protection" but
  gets your unsigned app quarantined, which is worse than decompilable.
- **Heavy JS obfuscation** (control-flow flattening etc.) — marginal gain over
  the existing esbuild minify (identifiers are already mangled, no sourcemaps),
  and heavily-obfuscated JS is itself an AV heuristic signal. Not worth it here.
- **Model-file encryption** — pointless; there is no shipped secret model (see
  "What's actually worth protecting").

## Summary — the realistic posture for an unsigned, flag-averse build

1. Fuses (done) — kills `RUN_AS_NODE` arbitrary code exec, the biggest hole.
2. `optimize=2` + exe metadata (done) — strips docstrings, lowers AV suspicion.
3. Nuitka native-compile of 16 modules (done, `dist:hardened`) — the real,
   AV-safe protection; the money rules + signer + matcher + all 9 sport engines
   ship as machine code with no recoverable bytecode.
4. Accept the SmartScreen prompt (no cert) and don't fight it with packers.

**Release with `npm run dist:hardened`, not `npm run dist`.**
