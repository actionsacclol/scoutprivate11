# Hardening

Reverse-engineering resistance for the packaged app. **Read this first:** no
client-side app can be made uncrackable — the CPU must execute your real logic,
so your real logic is recoverable from disk given enough effort. Every measure
here *raises the cost* of cracking/tampering; none is a wall.

## Confirmed bypass — 2026-08-08

A friendly reverser defeated the license on a `dist:hardened` build in an
afternoon. Worth recording exactly, because it hit a gap this document did not
name:

1. Dropped their own `license` module into the unpacked `_internal/` directory,
   replacing the compiled one. `/api/license` then returned `{"licensed": true}`.
2. Confirmed the check was purely local — no server-side confirmation.
3. Trained real models via `POST /api/models/{sport}/refresh`, which runs the
   full training path locally with no server involvement.
4. Drove the backend directly over `127.0.0.1:8000`, which had no authentication
   of any kind.

**What held:** they never decompiled anything. The Elo configs, blend weights,
money rules and matcher stayed opaque — Layer 3 did its job. They went around
it because they never needed to go through it.

**What that means:** the loss was license enforcement, not IP. Those are
separate axes and only one of them is defensible on the client. The structural
fix is in "The limit of all of this" at the bottom.

**Since addressed — partially.** Layer 5 makes findings #1 and #2 cost real
work rather than an afternoon: the module swap is caught by the call sites, and
the check is no longer purely a local boolean but a signature the stub cannot
forge. Finding #4 was closed by Layer 4. Finding #3 stands and always will while
the training path ships — see the bottom.

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

### What Layer 3 does NOT do — compiled is not tamper-proof

**Nuitka protects reading, not replacing.** This distinction is the whole ball
game and it was missing from this document until a real crack found it (see
"Confirmed bypass" up top).

A compiled `license.pyd` cannot be decompiled — but Python still resolves
`from kryptscout import license` by *filename* at runtime, against a directory
the attacker owns. Overwrite the file with a shim that returns `True` and every
compiled caller, `api/main.pyd` included, imports the shim without complaint.
No machine code was patched; nothing had to be understood. The compilation
never entered into it.

So read Layer 3 as exactly one guarantee: **the algorithms don't ship in
recoverable form.** It buys nothing against tampering, and no amount of
additional compiling changes that — the next module is replaceable too.

Closing the swap properly needs a signature the app checks and the attacker
can't forge, which needs a code-signing cert (Layer 0, not happening) or a
server that holds something the client genuinely needs. Local hash manifests
are not a substitute: the manifest is another file in the same writable
directory.

### Why `api/main.py` is on that list

`license.py` compiled alone protected the *check* but left its *call site* as
bytecode: `_license_gate` (the app-wide 402 middleware) lives in `api/main.py`,
so the cheapest *decompile-and-patch* route was never touching the native
license module at all — just patching the middleware out of `main.pyc`. Both
now ship as native code. Note this closes the patching route only; module
replacement (above) still reaches both.

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

## Layer 4 — Local API token (DONE)

Closes finding #4 of the confirmed bypass: the backend used to answer anything
that could find its port.

`PythonBackend.startChild()` mints 32 CSPRNG bytes per launch and passes them
to the Python child as `KRYPT_SCOUT_API_TOKEN`; `api/main._api_token_gate`
(compiled — `api/main.py` is in `HARDEN_MODULES`) rejects any `/api` request
whose `X-Scout-Token` header doesn't `hmac.compare_digest` against it. The
renderer gets port + token together over the `backend:connection` IPC and
sends the header from `apiFetch()` in `src/backendBase.ts`; the two main-process
callers (`scheduler.ts`, `discord-picks.ts`) read `pythonBackend.token()`.

Deliberate choices:

- **`/api/health` stays open.** It is the supervisor's liveness probe, carries
  no data, and must answer even if token plumbing breaks on some platform.
- **The gate wraps the license gate**, so an unauthenticated caller gets a flat
  401 and is never told the app is merely unlicensed. (Starlette applies
  `@app.middleware` in reverse registration order — the token gate is
  registered second, which is what puts it outside. `test_api_token.py` pins
  this; don't reorder the two blocks.)
- **No token in the env = auth off.** pytest, a hand-run `python -m uvicorn`,
  and plain `vite` dev then need no ceremony. A *frozen* backend that finds no
  token logs a warning instead of refusing to start — a hard failure here would
  brick the app if env propagation ever broke on macOS/Linux, which is a worse
  outcome than an unauthenticated loopback port.

**What this is worth.** It stops another local process, a browser tab, or a
third-party client from driving the backend of a normally-running app. It does
**not** stop the machine's owner: they can read the token out of the process
environment, or just launch the backend binary themselves with a token they
chose. Same ceiling as everything else on the client — see below.

## Layer 5 — Anti-shim + clock anchor + decoys (DONE)

Aimed squarely at the 2026-08-08 bypass, which did not defeat a check — it
replaced the file holding one.

**The gate no longer takes the license module's word for it.** `api/main.py`
and `autobet/engine.py` each carry their own copy of the sha256 of the Ed25519
public key and refuse to open unless `license.public_key_fingerprint()` matches,
alongside the full internal surface being present. A twenty-line stub exporting
`is_licensed = lambda: True` now fails both. To pass, the stub has to ship the
genuine verifying key — at which point it is doing real signature checks and is
not a stub. The check lives at the *call sites*, in compiled modules, because a
module that has been replaced cannot be asked to confirm it hasn't been.

Two call sites with two independent constants, deliberately: the gate is no
longer one boolean in one place. `test_license_gate.py` pins that the two copies
agree with the real key, because rotating the signing key and updating only
`license._PUBLIC_KEY_B64` would 402 every install at once with nothing pointing
at the cause.

**Clock rollback is closed.** Expiry was checked against `time.time()` alone, so
winding the system date back made every token immortal — and an immortal token is
never renewed, which switched off revocation permanently. No reversing, no file
editing: cheaper than the documented crack. `_verify_token` now also anchors to
`iat`, which is inside the signature. Setting the clock to just after issue is
still allowed and gains nothing but one TTL, which is the revocation latency the
design already accepts. An hour of slack absorbs real clock skew, so a dying CMOS
battery does not lock anybody out. Nine tests in `test_license_clock.py` sign real
tokens with a throwaway key to pin all of it.

**Decoys.** `license.py` ends with a block of plausible, better-named,
never-called license functions (`check_license`, `license_valid`,
`_check_activation`, `LICENSE_OK`, …). Each returns the genuine answer, so one
watched under a debugger looks exactly like the gate; patching any of them
changes nothing. The cheapest and most obvious attack now produces a build that
appears cracked and isn't. They are marked in the source and reach no product
code path — cost to a paying customer is zero.

**What this is worth.** A cost, not a wall — someone who reads
`_license_module_intact` can satisfy it. The point is that they now have to read
it first, having probably burned an afternoon on the decoys, and the result is
still a token check they cannot forge without the signing key. The ceiling in
"The limit of all of this" is unchanged.

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
3. Nuitka native-compile of 17 modules (done, `dist:hardened`) — the money
   rules + signer + matcher + all 9 sport engines ship as machine code with no
   recoverable bytecode. Protects the algorithms from being **read**. Does
   nothing against module **replacement**.
4. Local API token (done) — the backend no longer answers strangers on
   loopback.
5. Anti-shim + clock anchor + decoys (done) — the module swap and the
   set-your-clock-back bypass both cost real work now, and the obvious symbols
   to patch are the wrong ones.
6. Accept the SmartScreen prompt (no cert) and don't fight it with packers.

**Release with `npm run dist:hardened`, not `npm run dist`.**

## The limit of all of this

Layers 1–4 protect the code and shut the casual doors. **None of them can
enforce a license**, and no future layer will, because the check runs on
hardware the attacker owns. The 2026-08-08 crack is the proof, and any
re-run of it will succeed again.

The only fix that changes the outcome is architectural: **serve the picks,
don't ship the model.** Today every install trains locally from public data and
computes its own edges, so a bypassed license yields a fully working product.
Move inference behind an authenticated call to krypt.cc (the license server
already exists — `/api/scout/activate|heartbeat|deactivate`) and drop the sport
engines + training path from the shipped build, and the module swap yields an
empty shell instead.

Two things that sound like fixes and aren't:

- **Signing/serving model artifacts.** There is no shipped secret model to
  protect (see "What's actually worth protecting"). Signing the artifact is
  moot while the *training code* sits on their disk.
- **Moving order execution server-side.** The wallet is the user's, DPAPI'd on
  their machine. Anyone who has cracked the client can place the same orders
  without the app. The autobet engine's value is the picks it acts on, which
  reduces this back to inference.

Before spending the rewrite: the ROI backtest over 6,574 bets came back at
−5.2% with the confidence interval entirely below zero. If that still stands,
what a cracker gains is UI access, and the size of the fix should be scaled to
that.
