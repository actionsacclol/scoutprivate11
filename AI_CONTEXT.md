# Krypt Scout — AI Context

## What it is

Krypt Scout is a free, open-source Windows desktop app (Electron shell + supervised Python FastAPI backend) that models nine sports — NBA, WNBA, NFL, NCAAF, NCAAB, MLB, NHL, soccer (11 leagues), tennis (ATP/WTA) — converts model win probabilities to fair American lines, and compares them against live market prices (ESPN lines, Polymarket, Kalshi free; The Odds API optional). It derives one recommended pick per game, prices parlays honestly (book vs model fair price), tracks the user's own bets with automatic grading at settlement, records every prediction to SQLite, and measures the models against the devigged closing line (CLV). A Discord webhook sender auto-posts picks. Part of the Krypt free-tools suite (krypt.cc). MIT.

The repo folder is still named `Krypt Goaly` (the pre-rename working title); the product, package (`krypt-scout` / `kryptscout`), and all branding are **Krypt Scout**.

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI + uvicorn, pandas/sklearn/xgboost, SQLite |
| Shell | Electron ^30 (vite-plugin-electron), electron-builder NSIS, electron-updater (dormant) |
| Renderer | React 18 + TypeScript 5.4, Vite 5, Tailwind 3, lucide-react |
| Packaging | PyInstaller --onedir bundle shipped via electron-builder `extraResources` |

## Architecture map

```
backend/kryptscout/
  config.py       KRYPT_SCOUT_USERDATA-aware paths (DATA_DIR/RAW_DIR/MODELS_DIR), .env, ODDS_API_KEY
  core/           schemas.py (unified Game/Prediction/Edge/GameCard, Sport enum)
                  oddsmath.py (American<->decimal<->prob, devig, EV, Kelly)
                  elo.py (shared Elo engine: K, home adv, MOV, season carry)
                  modelio.py (stamps model pickles with the sklearn/xgboost
                  MAJOR.MINOR that wrote them; loaders reject a foreign stamp)
  sports/         base.py (SportEngine ABC + registry; engines load via importlib)
                  nba nhl nfl mlb wnba soccer tennis ncaaf ncaab .py — one engine per sport
                  contract: upcoming_games() / predict() / refresh_data(); each module's
                  __main__ runs a holdout backtest (acc + log loss vs devigged close where data allows)
  datasources/    espn.py (schedules/results, no key), injuries.py (ESPN feeds),
                  weather.py + stadiums.py (Open-Meteo + 62-venue dome table)
  odds/           providers.py (OddsProvider ABC + registry; providers independent & best-effort)
                  espn_odds.py polymarket.py kalshi.py (free) + the_odds_api.py (optional key)
  store/          db.py (short-lived sqlite conns), tracker.py (record/settle/performance/CLV/movement),
                  picks.py (Python port of src/picks.ts derivePick — MUST stay in sync)
  jobs/           capture.py (snapshot predictions+odds), settle.py (fill finals, grade),
                  refresh.py, compact.py (one-off: collapse legacy per-minute
                  odds_snapshots into buckets); recipes in jobs/__init__.py
  api/main.py     /api/board/{sport} /api/picks /api/mybets /api/models /api/performance
                  /api/clv /api/roi /api/movement/{id} /api/injuries /api/weather
                  /api/jobs/{refresh,capture,settle} /api/health

electron/
  main.ts preload.ts ipc.ts        window.scout contextBridge surface
  system/python-backend.ts         THE supervisor: spawns uvicorn (dev: backend/.venv python;
                                   packaged: resources/python/krypt-scout-backend.exe), probes a
                                   free port, health-checks GET /api/health (60s startup window),
                                   restarts on crash, broadcasts BackendStatus to renderer
  system/scheduler.ts              in-app background jobs (capture/settle/refresh cadence)
  system/discord-picks.ts          webhook sender (picks -> Discord embeds)
  system/settings-store.ts         atomic JSON settings in userData
  system/updater.ts                electron-updater wiring — 'unconfigured' until build.publish set
  system/discord.ts webSecurity.ts Rich Presence; nav/permission lockdown

src/  (React renderer — NOTE: root src/, the top-level frontend/ dir is an EMPTY leftover)
  App.tsx                board polling + routing between pages
  components/            DashboardPage TodayPage BoardsPage TerminalPage PicksPage ParlayPage
                         TrackerPage BonusesPage DiscordPage ValuePage PerfPage
                         ModelsPage IntelPage SettingsPage AboutPage Onboarding Sidebar
                         DetailDrawer GameCardTile AsciiBanner BookLink common.tsx
  modelMeta.ts           single source of truth for model names/backtest acc/notes
                         (fallbacks; /api/models overrides at runtime)
  picks.ts parlay.ts mybets.ts oddsmath.ts   pick derivation, slip pricing, user-bet hooks, odds math
  config/referrals.ts    sportsbook directory + referral links; every book click routes through bookLink()
  uiMode.tsx prefs.ts    SIMPLE/PRO nav modes, onboarding persistence
  demo.ts demoIntel.ts   demo slate so the UI works backend-down

shared/types.ts          the IPC contract (KryptApi-style window.scout surface) — keep in sync
                         with electron/ipc.ts and electron/preload.ts
```

## Key conventions (read before editing)

- **The honest-model rule.** The product's identity is that it never overclaims: the About page and README state the devigged closing line beats every v1 model; backtests use strictly pre-game info; PERFORMANCE (model record) and TRACKER (user record) are auto-graded and uneditable; the parlay page exists to talk users out of bad slips. Never add copy or features implying guaranteed profit, and never let affiliate concerns touch model output, odds display, or pick selection.
- **Pick derivation exists twice on purpose.** `src/picks.ts` (UI) and `backend/kryptscout/store/picks.py` (ROI tracking) implement the same derivePick logic and must produce identical picks — same tie-breaks, same exclusions (no totals, no completed games). Change one, change both.
- **modelMeta.ts is the only place backtest numbers live in src/.** Three copies drifted twice before; `/api/models` overrides at runtime, the table is the offline fallback. When an engine version bumps, update its one line.
- **Typecheck with `npm run typecheck` (tsc --noEmit on both tsconfigs).** Never run bare `tsc` — it emits `.js` files next to the `.ts` sources, which silently shadow them in the Vite/Electron build (a whole "purge stale .js shadows" cleanup commit exists because of this).
- **`KRYPT_SCOUT_USERDATA`** — set by the supervisor for packaged builds; the backend puts `data/` and `models/` under it (see `backend/kryptscout/config.py`). Unset (dev/CLI/tests), paths are repo-local. Never write inside the frozen bundle dir.
- **`KRYPT_SCOUT_TEST_PROFILE=<dir>`** — gives a packaged build an isolated userData dir AND its own single-instance lock, so smoke tests/CI can run alongside a dev instance.
- **Odds providers are best-effort and independent** (`odds/providers.py`): one provider failing or lacking a key must never block the others; the board merges all entries into `GameCard.odds`.
- **The autobet dedupe needs BOTH guards.** `_attempts()` blocks a game already committed in the DB (across sweeps); `committed_games` blocks one already funded inside the CURRENT sweep. picks are stored per (game, model), so two models disagreeing about one game are both un-committed when the eligibility pass scores them — and on a two-way market those are the complementary YES/NO tokens of the *same* conditionId, so betting both costs `1 + spread` for a $1 payout plus two taker fees. Deleting either guard reopens it.
- **Never pickle a fitted sklearn estimator into a model bundle.** Store the coefficients and apply them (`_apply_platt` in nba/nfl is two floats). Pickled estimators load across versions with only a warning and may silently predict differently; `MODELS_DIR` lives in userData and outlives app updates that bump the bundled libs. `core/modelio` stamps + verifies whatever can't be reduced (the xgboost boosters); a rejected bundle falls back to Elo and `_model_stale` retrains it.
- **`odds_snapshots` is bucketed, not append-per-capture** (`db.SNAPSHOT_MIN_GAP_MINUTES`). `GET /api/board/{sport}` records on every read and the renderer polls all nine sports every 60s, which grew 1,300+ rows for one game. Buckets are fixed wall-clock windows — a window *relative* to the stored row ratchets forward on every poll and collapses the series to a single point.
- **Referral links**: `src/config/referrals.ts` is the only file to edit for monetization; every book click app-wide routes through `bookLink()`. Affiliate disclosure lives on AboutPage, the BonusesPage banner, README, and DISCLAIMER — keep all four when copy changes.
- **PyInstaller gotchas** (scripts/py-bundle.mjs): --onedir only (onefile blows the 60 s health window); `--collect-submodules kryptscout` (importlib engine loading); `--collect-binaries/--collect-data xgboost` but NOT `--collect-all xgboost` (drags in xgboost.testing → uninstalled `hypothesis` → build failure).
- **Data-source hygiene**: runtime-download + cache under `RAW_DIR`, never redistribute files (football-data, tennis-data are personal-use). Tennis is TML (MIT, ATP) + tennis-data.co.uk (WTA) — do not reintroduce Sackmann CSVs (CC BY-NC-SA).
- Settings writes are atomic (stage → rename); sqlite connections are short-lived; the IPC contract lives in `electron/ipc.ts` + `electron/preload.ts` + `shared/types.ts` and the three must stay in sync.

## How to run / build

```sh
npm install
npm run dev          # predev provisions backend/.venv, then Vite + Electron; supervisor spawns uvicorn
npm run typecheck    # tsc -p tsconfig.node.json --noEmit && tsc -p tsconfig.json --noEmit
npm run py:bundle    # PyInstaller --onedir → backend/dist-py/krypt-scout-backend/
npm run dist         # vite build + electron-builder → release/Krypt Scout-Setup-<version>.exe
```

Backend standalone: `cd backend && .venv/Scripts/python -m uvicorn kryptscout.api.main:app --port 8000`.
Engine backtests: `python -m kryptscout.sports.<sport>`. Jobs: `python -m kryptscout.jobs.capture` / `.settle`.

## Current state (as of 2026-07-11)

Version 0.1.0, first public release. Auto-update is scaffolded but dormant (`build.publish: null`; a `publishTemplate` block in package.json documents activation once the GitHub repo is public). Installer is unsigned (SmartScreen warning expected). NBA gradient-boosted model is built but gated; NFL ships xgb-v3. Known structural gaps: Windows-only packaging, no bet-slip sync, odds breadth below paid aggregators, models below closing-line quality (by design of the v1 baselines — v2 per-sport GBMs are the roadmap, benchmarked against the close).
