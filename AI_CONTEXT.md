# Krypt Scout — AI Context

## What it is

Krypt Scout is a licensed (Krypt Premium, key from krypt.cc — NOT free, NOT open source) Windows desktop app (Electron shell + supervised Python FastAPI backend) that models nine sports — NBA, WNBA, NFL, NCAAF, NCAAB, MLB, NHL, soccer (11 leagues), tennis (ATP/WTA) — converts model win probabilities to fair American lines, and compares them against live market prices (ESPN lines, Polymarket, Kalshi free; The Odds API optional). It derives one recommended pick per game, prices parlays honestly (book vs model fair price), tracks the user's own bets with automatic grading at settlement, records every prediction to SQLite, and measures the models against the devigged closing line (CLV). A Discord webhook sender auto-posts picks. Part of the Krypt free-tools suite (krypt.cc). MIT.

The repo folder is still named `Krypt Goaly` (the pre-rename working title); the product, package (`krypt-scout` / `kryptscout`), and all branding are **Krypt Scout**.

## Tech stack

| Layer | Technology |
|---|---|
| Backend | Python 3.11+, FastAPI + uvicorn, numpy/pandas, SQLite (no fitted models: every engine is a ratings replay) |
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
                  arb.py / lean.py / fair.py — model-free signals: fee-inclusive
                  true arbs, the exchange-lead reference (Kalshi/PM lead DK),
                  and the sharp fair per side (depth-weighted microprice when
                  the exchange book was read; thin books flagged) that the
                  boost evaluator scores against. sgp.py — soccer same-game
                  joint table (Dixon-Coles on the sharp 1X2 + O/U 2.5).
                  forecast.py — the DISPLAYED probability: market where one
                  exists, else the model labelled pre-line with its error.
                  kalman.py — margin filter behind the college engines;
                  calibrate.py — temperature scaling (WNBA 0.69, NFL 0.84).
                  Display + tracking only; none feed picks/autobet.
  sports/         base.py (SportEngine ABC + registry; engines load via importlib)
                  nba nhl nfl mlb wnba soccer tennis ncaaf ncaab .py — one engine per sport
                  contract: upcoming_games() / predict() / refresh_data(); each module's
                  __main__ runs a holdout backtest (acc + log loss vs devigged close where data allows)
  datasources/    espn.py (schedules/results, no key), injuries.py (ESPN feeds),
                  espn_predictor.py (ESPN's pre-game win probability, the
                  PERFORMANCE benchmark's third reference; jobs/predictor.py backfills),
                  mlb_context.py (home-plate umpire + lineup first-seen; scored
                  with core/context rest days by tracker.context_trackers),
                  weather.py + stadiums.py (Open-Meteo + 62-venue dome table)
  odds/           providers.py (OddsProvider ABC + registry; providers independent & best-effort)
                  espn_odds.py polymarket.py kalshi.py (free) + the_odds_api.py (optional key)
  store/          db.py (short-lived sqlite conns), tracker.py (record/settle/performance/CLV/movement),
                  picks.py (Python port of src/picks.ts derivePick — MUST stay in sync)
  jobs/           capture.py (snapshot predictions+odds), settle.py (fill finals, grade),
                  refresh.py, compact.py (one-off: collapse legacy per-minute
                  odds_snapshots into buckets); recipes in jobs/__init__.py
  mcp/            the MCP server AI clients connect to (off until switched on):
                  catalog.py (name, instructions, the 15 tool definitions — pure
                  data), tools.py (handlers; they call api/main's endpoint
                  functions, so an agent sees exactly what the pages show),
                  protocol.py (hand-rolled JSON-RPC: handshake revisions +
                  stateless 2026-07-28), http.py (POST /mcp: loopback Host only,
                  any Origin refused, bearer token), store.py (switch, token,
                  bridge file, clients, audit), stdio.py (`--mcp` relay)
  autobet/        real-money layer, off + paper by default.
                  engine.py (the rule sweep: settings, scan, money rules, ledger,
                  grading, stops), venues.py (one interface over the three
                  exchanges: resolve / meta / ask_depth / buy / fill / balance),
                  polymarket_api.py + polymarket_auth.py (Polymarket.com CLOB),
                  kalshi_trade.py (Kalshi REST: RSA-PSS / Ed25519 signing, IOC
                  orders, shard funding), pmus_trade.py + pmus_wire.py
                  (Polymarket US; pmus_wire is a verbatim vendor of the user's
                  bot — keep it byte-for-byte), agent.py (one bet at a time for
                  an AI agent, through the same venues and ledger)
  api/main.py     /api/board/{sport} /api/picks /api/mybets /api/models /api/performance
                  /api/clv /api/roi /api/movement/{id} /api/injuries /api/weather
                  /api/jobs/{refresh,capture,settle} /api/health /api/mcp[/settings|/token/rotate]
                  + /mcp (the agent's door; NOT under /api, so it has its own gates)

electron/
  main.ts preload.ts ipc.ts        window.scout contextBridge surface
  system/python-backend.ts         THE supervisor: spawns uvicorn (dev: backend/.venv python;
                                   packaged: resources/python/krypt-scout-backend.exe), probes a
                                   free port, health-checks GET /api/health (60s startup window),
                                   restarts on crash, broadcasts BackendStatus to renderer
  system/scheduler.ts              in-app background jobs (capture/settle/refresh cadence).
                                   Refresh also runs at startup when due — never run, a
                                   full interval old, or stamped by another app version
                                   (AppState.schedulerState) — and BEFORE the capture: an
                                   engine with no results history rates every team the
                                   same, and picks must not be recorded off that
  system/discord-picks.ts          webhook sender (picks -> Discord embeds)
  system/settings-store.ts         atomic JSON settings in userData
  system/updater.ts                electron-updater wiring — 'unconfigured' until build.publish set
  system/mcp-clients.ts            writes/removes our launch entry in other programs' configs
                                   (Claude Desktop, Claude Code, Cursor, Codex, Windsurf)
  system/discord.ts webSecurity.ts Rich Presence; nav/permission lockdown

src/  (React renderer — NOTE: root src/, the top-level frontend/ dir is an EMPTY leftover)
  App.tsx                board polling + routing between pages
  components/            DashboardPage TodayPage BoardsPage TerminalPage PicksPage ParlayPage
                         TrackerPage BonusesPage DiscordPage ValuePage PerfPage
                         ModelsPage IntelPage AgentsPage SettingsPage AboutPage Onboarding Sidebar
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
- **The models carry no information beyond the closing line — measured, repeatedly (last: 2026-09-02).** Every "edge" found so far has been an instrument artifact: cross-venue vig, in-play prices, post-kickoff prediction rows, multiplicative devig on underdogs, and the pick's refreshed price being the venue's close. Before quoting any edge: score at the first-sight `entry_*` price, against a close carrying the pick's full market, under POWER devig (`remove_vig_power`), from strictly pre-game rows, at the executable ask. The only measured signal is microstructure — Kalshi/Polymarket lead DraftKings (`core/lean.py`, ~+1% same-book CLV, under the vig) — and the only deterministic edge is a fee-inclusive true arb (`core/arb.py`). Neither feeds picks or autobet; both are tracked. A model must clear a significant positive residual coefficient vs the close on held-out data before it may drive money.
- **Exchange quotes are executable asks, not midpoints.** `odds/polymarket` and `odds/kalshi` price what a buyer pays; the two sides sum to 1 + spread, and `remove_vig` recovers the mid. Never "fix" this back to the midpoint — it overstates every exchange EV by half the spread.
- **`NO_VALUE_SPORTS` is `{tennis, soccer}`** in both `store/picks.py` and `src/picks.ts`; a sport gets in by measuring negative alpha vs the close and gets out the same way.
- **Predictions are refused after kickoff** (`db.upsert_prediction(starts_at=…)`), and `performance()`/`clv()` score only pre-game rows. Any new consumer of `predictions` must filter `generated_at < start_time` too.
- **Pick derivation exists twice on purpose.** `src/picks.ts` (UI) and `backend/kryptscout/store/picks.py` (ROI tracking) implement the same derivePick logic and must produce identical picks — same tie-breaks, same exclusions (no totals, no completed games). Change one, change both. Since 2026-09-02 value edges are computed ONLY in the backend (`compute_edges`, sportsbook price vs the sharp fair from `core/fair.py`, MIN_GAP_PTS = 1); the TS side reads `card.edges` and mirrors the tiering (STRONG = Kalshi-led, LEAN = Polymarket-led) and the MODEL fallback (the displayed forecast's favourite). Exchanges never produce value picks, and autobet does not read picks at all: it has its own rule (next bullets).
- **modelMeta.ts is the only place backtest numbers live in src/.** Three copies drifted twice before; `/api/models` overrides at runtime, the table is the offline fallback. When an engine version bumps, update its one line.
- **Typecheck with `npm run typecheck` (tsc --noEmit on both tsconfigs).** Never run bare `tsc` — it emits `.js` files next to the `.ts` sources, which silently shadow them in the Vite/Electron build (a whole "purge stale .js shadows" cleanup commit exists because of this).
- **`KRYPT_SCOUT_USERDATA`** — set by the supervisor for packaged builds; the backend puts `data/` and `models/` under it (see `backend/kryptscout/config.py`). Unset (dev/CLI/tests), paths are repo-local. Never write inside the frozen bundle dir.
- **`KRYPT_SCOUT_TEST_PROFILE=<dir>`** — gives a packaged build an isolated userData dir AND its own single-instance lock, so smoke tests/CI can run alongside a dev instance.
- **Odds providers are best-effort and independent** (`odds/providers.py`): one provider failing or lacking a key must never block the others; the board merges all entries into `GameCard.odds`.
- **The autobet dedupe needs BOTH guards.** `_attempts()` blocks a game already committed in the DB (across sweeps); `committed_games` blocks one already funded inside the CURRENT sweep. The scan yields one candidate per (game, side, exchange), so both sides of a game — or one side on two exchanges — are all un-committed when the eligibility pass scores them, and buying both sides of a two-way market costs `1 + spread` for a $1 payout plus two taker fees. Committed means a `dry`, `placed` or `unknown` row on ANY side from EITHER driver (rules or agent). Deleting either guard reopens it.
- **No fitted models, no pickles (since 2026-11).** The xgboost overlays on NBA and NFL were retired: NBA's last bundle scored log loss 0.608 vs 0.567 for the Elo underneath it (719 games) and had gated itself off; NFL's blend bought 0.0006 log loss over Elo on 2024-25. All nine engines are ratings replays (Elo / Kalman / Poisson) that rebuild from cached results. `xgboost`, `scikit-learn`, `scipy` and `duckdb` are excluded from the bundle in `krypt-scout-backend.spec` (406 MB → 189 MB); `jobs/refresh.purge_retired_artifacts()` deletes leftover `nba_xgb_*.pkl` / `nfl_xgb_*.pkl` from `MODELS_DIR` at engine load. Do not bring a fitted model back without a significant held-out residual vs the close (see the rule above) — and if one ever returns, store coefficients, never a pickled estimator.
- **What autobet bets, and what it must never read.** `engine._scan` walks the boards (`capture.build_cards`, reused when under 4 minutes old) and, for each enabled exchange, compares that exchange's executable ask with the sharp fair set by a DIFFERENT exchange (`_reference`; never the venue's own quote, never a thin book). Net EV is `fair/ask − 1 − fee·(1 − ask)` at the venue's own fee; tier STRONG = the reference is Kalshi, LEAN = Polymarket; soccer and tennis are excluded. Polymarket US is not on the boards and is priced from its own search (`PMUS_MAX_LOOKUPS` per sweep). Engine forecasts (`model_prob`, Elo/Kalman) must never re-enter this test — measured, they carry nothing beyond the price. Be honest about the rule too: exchanges usually agree to within a point, so at the default 4% floor most sweeps place nothing, and nothing shows the gap is profit rather than a stale quote. `MAX_CLAIMED_EV` (12%) treats a bigger gap as a data error.
- **An order is sent once.** Every venue's `buy` is an immediate (FAK / IOC) order at a capped limit. A timeout or 5xx after the send raises `OrderOutcomeUnknown` → the row is `unknown`, which holds the game, the daily slot and the cash until a human checks; nothing retries. An accepted order whose fill is not yet known is `placed` with `PENDING_NOTE` and is not logged to MY BETS until `_reconcile_pending_fills` confirms it (a confirmed zero fill flips it to `unmatched`). The Kalshi and Polymarket US live paths are ported from working bots but have not been exercised from this app — Kalshi `immediate_or_cancel` on `/portfolio/events/orders` in particular.
- **The MCP server reads; betting is a separate switch.** `mcp/catalog.py` exposes boards, the fair, picks, records, arbs, movement, injuries and the user's bet log (`log_bet` / `delete_bet`), plus `get_bet_prices` and `place_bet`. `place_bet` goes through `autobet/agent.py`, which refuses unless the user set `agent_mode` to paper or live on the AI AGENTS page, and enforces the per-bet cap (over the cap is refused, never trimmed), the agent's own daily allowance, one bet per game across both drivers, the user's stops, the license gate (live), a readable order book, and a price no higher than the agent's stated probability supports after fees or two cents through the best offer. Do not loosen any of those, and do not add a second path to an exchange that skips them. `/mcp` sits outside `/api`, so the license gate and token check are in the route and in `mcp/http.py` — the renderer's `X-Scout-Token` must never open it and the agent's `ks_…` token must never open `/api`. The backend port changes every launch, so clients connect through the stdio relay (`krypt-scout-backend --mcp`), which finds the app via `<userData>/mcp/bridge.json` (port + pid, no secret) and reads the token from the encrypted store. The relay's stdout is the protocol: nothing on that path may print. `catalog.py` must stay import-free of the rest of `kryptscout` (the relay answers `initialize` / `tools/list` from it with the app closed). Tool handlers call the endpoint functions with every argument positional — FastAPI `Query(...)` defaults are not values.
- **ESPN scoreboards are fetched one day per request.** `dates=YYYYMMDD-YYYYMMDD` ranges are rejected for NBA/NFL from some networks (the "boards fail to fetch for a lot of users" bug); `datasources/espn.py` walks days and catches up from the last cached one. Odds providers run concurrently under a per-provider timeout, and a board that fails says so in the UI (`boardErrors`) instead of rendering as "no games".
- **`odds_snapshots` is bucketed, not append-per-capture** (`db.SNAPSHOT_MIN_GAP_MINUTES`). `GET /api/board/{sport}` records on every read and the renderer polls all nine sports every 60s, which grew 1,300+ rows for one game. Buckets are fixed wall-clock windows — a window *relative* to the stored row ratchets forward on every poll and collapses the series to a single point.
- **Referral links**: `src/config/referrals.ts` is the only file to edit for monetization; every book click app-wide routes through `bookLink()`. Affiliate disclosure lives on AboutPage, the BonusesPage banner, README, and DISCLAIMER — keep all four when copy changes.
- **PyInstaller gotchas** (backend/krypt-scout-backend.spec): --onedir only (onefile blows the 60 s health window); `collect_submodules('kryptscout')` (importlib engine loading); `EXCLUDED_MODULES` keeps xgboost/sklearn/scipy/duckdb out even when a stray lazy import would pull them in. `run_backend.py --selftest` must pass on the frozen exe before packaging (it now also imports the MCP relay modules); `--mcp` is dispatched before any heavy import.
- **Data-source hygiene**: runtime-download + cache under `RAW_DIR`, never redistribute files (football-data, tennis-data are personal-use). MoneyPuck (NHL xG) is stricter: non-commercial with credit and approval required before automated pulls, so the app neither fetches nor ships it — the user-dropped `all_teams.csv` under `RAW_DIR/nhl/moneypuck/` fed `nhl-xg-v2`, which is OFF by default since 2026-11 (`XG_ENABLED = False` in `sports/nhl.py`; the served model is `nhl-elo-ctx-v3`, built from the NHL API alone). Tennis is TML (MIT, ATP) + tennis-data.co.uk (WTA) — do not reintroduce Sackmann CSVs (CC BY-NC-SA).
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

## Current state (as of 2026-11-13)

Version 0.3.3 (+ unreleased work, see CHANGELOG "Unreleased"). Installer is unsigned (SmartScreen / Gatekeeper warnings expected). The model stack is ratings-only (no xgboost); the per-sport GBM roadmap is dropped — measured, no model here beats the close. An MCP server (AI AGENTS page) lets Claude / Cursor / Codex read the app and, if the user switches it on, place capped bets (paper or live). Autobet is rule-driven across Polymarket, Kalshi and Polymarket US (an exchange's ask under another exchange's fair, after fees); it needs no AI. It works end to end in paper mode against live books; the Kalshi and Polymarket US live order paths are untested with real accounts. Taker orders are measured to lose on model picks and nothing shows the cross-exchange rule wins either — do not loosen its floors to make it bet more. Known gaps: no bet-slip sync, odds breadth below paid aggregators, `MLS_SEASONS` in `sports/soccer.py` is a hard-coded tuple that needs bumping each year.
