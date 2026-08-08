# Releasing Krypt Scout

Installers are built by `.github/workflows/release.yml` on four native runners —
Windows x64, macOS arm64, macOS x64, Linux x64.

**Why four runners and not one with a cross-compile flag:** the app ships the
Python backend as a PyInstaller bundle, and PyInstaller cannot cross-compile.
Each OS/arch must freeze its own backend binary. Build an Intel `.dmg` on an
Apple Silicon runner and it installs cleanly, then dies the first time the
Electron supervisor tries to exec a backend the CPU can't run.

## Cutting a release

```bash
npm version patch      # or minor / major — writes package.json + a v* tag
git push origin master --follow-tags
```

The tag push builds all four targets and attaches them to a **draft** GitHub
Release for that tag. Review the assets, then publish the release manually.

To rehearse without releasing anything, run the workflow from the Actions tab
(`workflow_dispatch`) — same build, artifacts only, no release touched.

Artifacts:

| Platform | File |
|---|---|
| Windows x64 | `Krypt Scout-Setup-<version>.exe` (NSIS) |
| macOS Apple Silicon | `krypt-scout-<version>-mac-arm64.dmg` / `.zip` |
| macOS Intel | `krypt-scout-<version>-mac-x64.dmg` / `.zip` |
| Linux x64 | `krypt-scout-<version>-linux-x64.AppImage` / `.deb` |

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

Before blaming the renderer, confirm the user grabbed the right dmg: an
arm64 dmg on an Intel Mac won't launch at all, and macOS 10.15 is the floor
for the Electron in use.

## Auto-update stays dormant

`build.publish` is deliberately unset, so `electron-updater` reports
`unconfigured` and never phones home. Auto-update from a **private** repo would
require shipping a GitHub token inside the client, which is not acceptable —
so while the repo is private, updates are manual downloads. If the repo is ever
made public, set `build.publish` to the `publishTemplate` block already in
`package.json` and the updater wires itself up.

## What CI verifies before it packages anything

Each runner, in order: `npm run typecheck` → the full backend pytest suite →
`npm run dist`. `dist` runs `py:bundle`, which freezes the backend and then
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
`running` on the dashboard, and load one board — before publishing the draft.
