# Releasing Krypt Scout

Installers are built by `.github/workflows/release.yml` on three native runners
— Windows x64, macOS arm64, Linux x64.

**Why native runners and not one with a cross-compile flag:** the app ships the
Python backend as a PyInstaller bundle, and PyInstaller cannot cross-compile.
Each OS/arch must freeze its own backend binary. Build an Intel `.dmg` on an
Apple Silicon runner and it installs cleanly, then dies the first time the
Electron supervisor tries to exec a backend the CPU can't run. That is also
why dropping the Intel runner means dropping Intel support outright rather
than serving those users some other build.

## Cutting a release

**The version in `package.json` is the trigger.** Bump it, commit, push:

```bash
npm version patch --no-git-tag-version   # or minor / major — package.json only
git commit -am "v0.3.2" && git push
```

On every push to `master` the cheap `plan` job reads `package.json`, and:

- **tag `v<version>` already exists** → logs a notice and stops. The four-OS
  matrix never starts, so ordinary pushes cost roughly no Actions minutes.
  This is the normal case for day-to-day commits.
- **tag doesn't exist** → tags the commit, creates the GitHub Release, and
  builds all four installers straight onto it. No second push, no manual step.

CI cannot tag itself into a loop: a tag pushed with `GITHUB_TOKEN` does not
trigger workflows, which is why the *same* run does the building rather than
waiting to be re-triggered.

Pushing a `v*` tag by hand still works and is honoured as-is — useful for
re-cutting a release from an older commit.

Releases are currently created as **drafts** — review the assets, then hit
publish. That is temporary: v0.3.1 built five clean artifacts but ran plain
`dist`, and this pipeline runs `dist:hardened`, whose Nuitka `.so` path has
never been built on macOS or Linux (HARDENING.md). Since `fail-fast` is off, a
mac failure would otherwise publish a release carrying only Windows and Linux.
**Once one hardened run is green on all four runners, drop `--draft` from the
`gh release create` call in the `release` job** and releases go out untouched.

To rehearse without releasing anything, run the workflow from the Actions tab
(`workflow_dispatch`) — same build, attached to a `build-<run>` **prerelease**,
no real release touched.

Keep `backend/version_info.txt` (Windows exe VERSIONINFO) in sync with the
`package.json` version when you bump it.

Artifacts:

| Platform | File |
|---|---|
| Windows x64 | `Krypt Scout-Setup-<version>.exe` (NSIS) |
| macOS Apple Silicon | `krypt-scout-<version>-mac-arm64.dmg` |
| Linux x64 | `krypt-scout-<version>-linux-x64.AppImage` / `.deb` |

**No Intel Mac build.** The `macos-15-intel` job was removed from the matrix:
GitHub bills macOS at 10x, so on this private repo the two mac jobs were ~85%
of a release's cost and `dist:hardened` makes them longer still. Dropping it
cuts ~40% off every run. Intel users get no download rather than a broken one —
arm64 and x64 are separate dmgs, so there is nothing for them to install by
mistake. To restore, add the job back to the matrix in `release.yml` (the
comment at the top of the file has the exact block).

## Copy-paste deploys (several GitHub accounts)

The tree is deployed by copying the working folder over another checkout in
Explorer, then committing there. That works, and the build is set up so it
keeps working, but know what a copy does and doesn't carry:

1. Copy the whole folder over the other checkout (skip `.git` — the other
   checkout's `.git` is the one that points at the other account; skip
   `node_modules`, `backend/.venv` and `backend/dist-py` too, they are huge
   and rebuilt anyway). Everything git ignores (`data/`, `logs/`, `reference/`,
   `.env`) is ignored on the other side by the same `.gitignore`.
2. In the other checkout: `git add -A`, commit, push. The version in
   `package.json` triggers the release exactly as described above.

**A copy never deletes.** A module removed here stays behind there as a stale
file. Two things make that harmless:

- `tsconfig.json` / `tsconfig.node.json` typecheck from the real entry points
  (`src/main.tsx`, `electron/main.ts`, `electron/preload.ts`) instead of whole
  directories, so a file nobody imports is a file nobody checks — the same set
  Vite bundles. (Verified 2026-09-03 by copying the 0.3.2 tree over a 0.3.1
  clone: the stale Telegram module broke `include: ["electron"]` and passes
  entry-based.) New global `.d.ts` files must be added to the `files` list.
- Python only imports what it imports; a stale module under `kryptscout/` is
  dead weight in the bundle, nothing more.

The one thing that can still bite: a **deleted test file** under
`backend/tests/` — pytest collects the directory, so a stale test that
exercises an API that changed will fail CI. When deleting a test file, leave
an empty stub with a comment for one release, or delete it by hand on the
other side.

## The bundle selftest and a fresh venv

CI builds from a **fresh** `pip install -r requirements.txt`, so it resolves
newer libraries than a local venv that has been sitting for weeks. The first
run from a new account (2026-09-03) failed the frozen-bundle selftest on every
runner because eth-keyfile 0.10 pulled in py_ecc, which reads its own package
metadata at import time and the spec did not carry it. The spec now copies
metadata recursively from the two signing roots, but the lesson generalises:
**before cutting a release, run `npm run py:bundle` locally on a venv that
matches what CI will resolve** (`pip install --dry-run --ignore-installed
--report r.json -r requirements.txt` shows the versions without touching the
venv). A selftest failure in CI costs a full macOS run to discover.

## Two macOS traps (both hit on the first run — don't reintroduce them)

**1. Never set `identity: null` in `build.mac`.** It disables code signing
*entirely*, including the **ad-hoc signature** electron-builder otherwise
applies to arm64. Apple Silicon refuses to `exec` an unsigned Mach-O, so the
build goes green and the app then never launches. `CSC_IDENTITY_AUTO_DISCOVERY:
false` in the workflow is the correct and sufficient way to build unsigned — it
stops electron-builder hunting the keychain for a real identity while leaving
ad-hoc signing intact.

**2. macOS needs `libomp` for xgboost.** The xgboost PyPI wheel links against
OpenMP, which Apple's toolchain doesn't ship. Without it `import xgboost`
raises `Library not loaded: @rpath/libomp.dylib`, which takes down the nba/nfl
engines and therefore the whole backend — pytest and the frozen `--selftest`
both fail before packaging. Two halves to the fix, and you need both:

- CI runs `brew install libomp` on the macOS runners (build-time).
- `backend/krypt-scout-backend.spec` copies `libomp.dylib` into the bundle
  (run-time) so users don't need Homebrew. It checks
  `/opt/homebrew/opt/libomp/lib` then `/usr/local/opt/libomp/lib`, and prints a
  loud warning if neither exists rather than silently shipping a broken app.

Polybot's mac build doesn't hit this because it has no model stack — xgboost is
specific to Krypt Scout.

## These builds are unsigned

No code-signing certificate is configured on any platform. That is a cost
decision, not an oversight, and it shows up differently per OS:

- **Windows** — SmartScreen shows "Windows protected your PC". *More info →
  Run anyway.*
- **macOS** — Gatekeeper refuses to open the app ("damaged", or "cannot be
  opened because the developer cannot be verified"). The app is not damaged;
  that is the message macOS shows for an unsigned, un-notarized bundle whose
  download carried a quarantine attribute. Users clear it with:

  ```bash
  xattr -dr com.apple.quarantine "/Applications/Krypt Scout.app"
  ```

  Right-click → Open also works on some macOS versions. **Ship this command in
  the release notes** or macOS users will assume the download is corrupt.
- **Linux** — no signing expectation. Mark the AppImage executable:

  ```bash
  chmod +x krypt-scout-*-linux-x64.AppImage
  ```

Signing later: Windows needs an OV/EV certificate (`CSC_LINK` + `CSC_KEY_PASSWORD`
secrets); macOS needs a paid Apple Developer ID plus notarization
(`APPLE_ID`, `APPLE_APP_SPECIFIC_PASSWORD`, `APPLE_TEAM_ID`). Adding either is
secrets-and-config only — no code changes. Drop `identity: null` from
`build.mac` in `package.json` when a Developer ID exists.

## Field-debugging a black window

A window that opens but stays black means the renderer never painted — the
user is looking at the BrowserWindow's `#0A0A0F` backgroundColor. The main
process now logs every way that can happen (`did-fail-load`,
`render-process-gone`, `preload-error`, renderer console errors, GPU-process
crashes, and the 4-second reveal fallback firing) into the ring buffer behind
SETTINGS → export logs — and mirrors every entry to disk, because a user
staring at a black window can't reach SETTINGS. The first ask is therefore
the logs folder itself:

- macOS: `~/Library/Application Support/Krypt Scout/logs/`
- Windows: `%APPDATA%\Krypt Scout\logs\`

It holds `main.log` (window/renderer/GPU diagnostics + backend stdout, all
runs, rotates at 5 MB to `main.log.1`) and `backend.log` (the backend's own
rotating file). macOS Console.app / `log show` captures are useless here —
the app's diagnostics never enter the unified log, so those dumps contain
only AppKit noise.

Escape hatches, in order:

1. **GPU auto-fallback** — two GPU-process crashes in one run persist
   `disableGpu` in settings.json and relaunch with hardware acceleration off.
   Broken-compositor machines (the classic macOS black screen) self-heal on
   the second launch.
2. **`--disable-gpu`** — forces the same thing manually:
   `open -a "Krypt Scout" --args --disable-gpu` (macOS) or
   `"Krypt Scout.exe" --disable-gpu` (Windows).

DevTools stay compiled out of packaged builds — diagnosis goes through the
log export, not an inspector.

Before blaming the renderer, confirm the machine is Apple Silicon at all: the
arm64 dmg won't launch on an Intel Mac, and since the Intel build was dropped
there is no dmg that will. `uname -m` returning `x86_64` ends that support
thread. macOS 10.15 is the floor for the Electron in use.

## Auto-update stays dormant

`build.publish` is deliberately unset, so `electron-updater` reports
`unconfigured` and never phones home. Auto-update from a **private** repo would
require shipping a GitHub token inside the client, which is not acceptable —
so while the repo is private, updates are manual downloads. If the repo is ever
made public, set `build.publish` to the `publishTemplate` block already in
`package.json` and the updater wires itself up.

## What CI verifies before it packages anything

Each runner, in order: `npm run typecheck` → the full backend pytest suite →
`npm run dist:hardened` (Nuitka-compiles the `HARDEN_MODULES` list before
freezing — see HARDENING.md; plain `dist` would ship those modules as
decompilable `.pyc`). `dist:hardened` runs `py:bundle:hardened`, which
freezes the backend and then
executes the frozen binary's `--selftest` (imports the trading deps and signs a
Polymarket order offline). A non-zero selftest aborts the build, so a bundle
that dropped a lazy-imported dependency can never reach a release.

## First-run caveat for macOS and Linux

The Windows bundle is the only one with production mileage. The backend spec
(`backend/krypt-scout-backend.spec`) and the supervisor
(`electron/system/python-backend.ts`) are both written platform-agnostically
and the keychain packages are collected best-effort per platform, but the first
green macOS/Linux run in CI is also the first time those bundles have ever been
built. Smoke-test each artifact — launch it, confirm the backend reaches
`running` on the dashboard, and load one board. Because releases now publish
themselves, do that smoke test on the `workflow_dispatch` prerelease *before*
bumping the version, not after the real release is already public.
