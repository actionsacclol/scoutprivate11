# Changelog

## Unreleased

- **Closing-line value is now backfillable** (MEASUREMENT): ESPN's odds
  document already carried each book's declared `open` and `close`, and the
  parser was reading only the flat current `moneyLine` and discarding both.
  New `line_endpoints` table, `espn_odds.parse_line_endpoints` /
  `fetch_line_endpoints`, `jobs/lines.py` to backfill, and
  `tracker.clv_vs_close()` (also merged into `GET /api/clv` as `vs_close`).
  This matters because CLV was previously computable only from
  `odds_snapshots` — which needs this install to have been polling that game,
  on that book, both early and near kickoff. It usually wasn't: **11 of 283
  recorded picks were scoreable**. Every non-tennis pick on a settled game now
  is (19/19), and ESPN still serves the document for month-old games, so the
  history backfills instead of having to accrue. Tennis has no per-match ESPN
  odds document and stays unscoreable this way.
- **Cross-venue CLV confound found and closed** (MEASUREMENT): the first run of
  the above reported **+6.1% CLV on STRONG at a 12-of-12 beat rate**, t≈4.6.
  It was fake. Value picks are priced at Polymarket/Kalshi while ESPN's close
  is DraftKings, and a near-zero-vig exchange beats a sportsbook's vigged close
  on essentially every bet ever placed, win or lose — the number was the spread
  between two venues, reported as skill. `clv_vs_close` now splits same-book
  picks (`price_clv_pct`, vig cancels) from cross-venue ones (`fair_ev_pct`,
  measured against the *devigged* close) and never averages the two. Corrected
  figure on the same picks: **+1.5% ± 1.3%** — not significant, but no longer
  nonsense. `test_line_endpoints.py` pins the split.

- **Releases cut themselves from the version bump** (CI): pushing to `master`
  now tags and releases when `package.json`'s version has no `v<version>` tag
  yet — no `npm version`, no `--follow-tags`, no manual publish. A cheap `plan`
  job makes that call in seconds and stops there on ordinary pushes, so the
  four-OS matrix (and the Actions minutes it eats) only runs on an actual
  version bump. The tag is pushed with `GITHUB_TOKEN`, which deliberately does
  not re-trigger workflows, so the *same* run does the building rather than
  waiting for a trigger that never comes. Releases are created published rather
  than draft, the release object is created once before the matrix (four
  concurrent uploaders can no longer race into duplicates), and each build job
  checks out the tag rather than the branch tip. Hand-pushed `v*` tags still
  work; `workflow_dispatch` still produces a `build-<run>` prerelease.
- **Intel Mac build dropped** (CI): GitHub bills macOS runners at 10x, so on a
  private repo the two mac jobs were ~85% of a release's cost and
  `dist:hardened` (Nuitka) makes them longer still. Removing `macos-15-intel`
  cuts ~40% off every run. Intel users get no download rather than a broken
  one — arm64 and x64 are separate dmgs, so there is nothing to install by
  mistake. README/RELEASING updated to say so plainly. Restore by re-adding the
  matrix entry; the comment at the top of `release.yml` has the block.
- **First hardened release lands as a draft**: v0.3.1 built five clean
  artifacts but ran plain `dist`. This pipeline runs `dist:hardened`, whose
  Nuitka `.so` path has never been built on macOS or Linux, and with
  `fail-fast: false` a mac failure would otherwise publish a release carrying
  only Windows and Linux. Drop `--draft` once one hardened run is green.
- **`backend/version_info.txt` can't drift from `package.json`** again: the
  shipped exe claimed 0.3.1 while the app was 0.3.2, and now that the version
  *is* the release trigger, a stale one ships mislabelled. Synced, and pinned by
  `test_version_sync.py`.
- **`.gitignore` covers sqlite backups** (`*.sqlite.bak`, `*.sqlite.*.bak`).
  `jobs/compact.py` leaves a full 59 MB copy of the live store next to it — real
  bet history, one `git add -A` from a public repo.
- **`py-harden.mjs` restores the source tree on Ctrl-C**: the `finally` block it
  relied on doesn't run on SIGINT, so interrupting the multi-minute Nuitka
  compile left every hardened `.py` sitting in `build-harden-backup/`.

- **macOS builds fixed** (PACKAGING): the first CI run produced no usable
  Apple Silicon build. Two independent causes, both now closed.
  `build.mac.identity: null` disabled code signing *entirely* — including the
  **ad-hoc signature** electron-builder applies to arm64, which Apple Silicon
  requires to `exec` a Mach-O at all; the setting is removed, and
  `CSC_IDENTITY_AUTO_DISCOVERY=false` alone now does the unsigned build while
  leaving ad-hoc signing intact. Separately, xgboost's macOS wheel links
  against OpenMP, which Apple doesn't ship: without `libomp.dylib`,
  `import xgboost` fails and takes the nba/nfl engines — and so the backend —
  with it. CI now runs `brew install libomp`, and the PyInstaller spec copies
  the dylib into the bundle so end users don't need Homebrew.
- **Installers publish to Releases, not artifacts** (CI): four ~200 MB bundles
  exhaust the 500 MB Actions artifact quota inside two runs. Tag builds attach
  to a draft release; manual runs attach to a per-run prerelease
  (`build-<run_number>`). Also switched `npm ci` → `npm install`: npm's
  optional platform-specific binaries are a recurring source of
  lockfile-vs-platform `ci` failures across a four-OS matrix. macOS drops the
  `zip` target (it existed only for electron-updater, which stays dormant).

- **macOS and Linux builds** (PACKAGING): `.github/workflows/release.yml`
  builds installers on four native runners — Windows x64, macOS arm64, macOS
  x64, Linux x64 — and attaches them to a draft GitHub Release on a `v*` tag.
  Four runners rather than one cross-compiling runner because the app ships
  the Python backend as a PyInstaller bundle and **PyInstaller cannot
  cross-compile**: an Intel `.dmg` built on an Apple Silicon runner installs
  fine and then dies the first time the supervisor execs a backend the CPU
  can't run. `build.mac` (dmg + zip, unsigned via `identity: null`) and
  `build.linux` (AppImage + deb) added to package.json; the supervisor and
  the PyInstaller spec were already platform-agnostic and needed no changes.
  Every runner gates on typecheck → full pytest → the frozen bundle's
  `--selftest` before it packages. Install/quarantine notes per OS are in the
  new [RELEASING.md](./RELEASING.md); auto-update stays dormant, since
  updating from a private repo would mean shipping a token in the client.

- **In-play prices can no longer mint edges** (PICKS · VALUE · TOTALS): every
  model probability the app holds is strictly pre-game, so an edge measured
  against a live price is the scoreboard, not market inefficiency — a side
  down 20 in the third quarter quotes -1000 while the model still holds the
  opening coin flip, and the gap reads as a 90% "edge". Two ways live prices
  were reaching the edge math, both now closed at
  `core.schemas.is_in_play`: ESPN publishes a book's in-play feed as its own
  provider ("DraftKings Live Odds" → book key `draftkings_live_odds`), and
  Polymarket/Kalshi trade straight through a game, so any snapshot of theirs
  taken after kickoff is live with nothing in the name to say so. Measured in
  our own capture: 100% of `draftkings_live*` rows were post-kickoff, with
  same-minute home lines diverging from the pre-game feed by as much as -380
  vs -100000; 29% of Polymarket and 11% of Kalshi rows were post-kickoff.
  A game already under way now yields no value edges and falls through to a
  MODEL pick. `core/totals.py` had a book-name-only version of this guard
  since totals shipped; moneylines — which feed PICKS and autobet — never got
  one, and the name check alone misses the prediction markets entirely.
  Scope of the miss: the tracker's freeze rule (`db.upsert_pick` freezes a
  pick at kickoff) meant only **1 of 74** recorded value picks came from a
  post-kickoff price, but the board recomputed edges on every poll with no
  such guard, so the PICKS page itself was unprotected.
- **One book, one key**: ESPN sent DraftKings as both "DraftKings" and
  "Draft Kings", which `book_key()` faithfully turned into two book keys.
  Aliases now collapse onto a canonical key, so a single book is counted once
  by the line shop (which takes the best price *across* books) and groups as
  one series in line movement and CLV. No historical EV inflation: the two
  spellings never appear in the same snapshot — ESPN renamed the provider
  mid-window — so this is a hygiene fix, not a correction.

- **Best edge only** (AUTO-BET): a new toggle that turns the daily cap into
  a "top N of the board" rule. The sweep now runs in two passes — every
  local gate scores the whole slate first, then the money path walks it in
  edge order (model prob − market-implied prob), so the day's budget funds
  the sharpest picks instead of whichever the board listed first. Picks the
  cap can't reach report `not_best_edge` rather than `daily_cap`. Ranking is
  by EDGE and not claimed EV on purpose: EV = edge / implied price, which
  inflates longshots — the exact bucket `MAX_CLAIMED_EV` exists to distrust.
  Ships with a **Best Edge** preset (edge ranking, 3 bets/day, 12h window —
  a tight window keeps each sweep ranking one comparable slate, since a
  bigger edge appearing later in the day can't reclaim a spent slot).
- **`min_prob` actually saves** (AUTO-BET): the MIN PRICE longshot filter was
  declared in the engine and rendered in the form, but missing from the
  settings request model — pydantic dropped it silently, so the field looked
  saved and never applied. Added, with a test that the endpoint round-trips
  every knob the form can send.
- **`npm run dist` builds again**: `build/installer.nsh` was referenced by
  `build.nsis.include` but absent from the tree, failing electron-builder
  before it could package. Restored — it reaps any lingering
  `krypt-scout-backend.exe` on install and uninstall, which is what the hook
  is needed for (the supervised PyInstaller child holds
  `resources\python\` open and blocks in-place upgrades).
- **Tennis loses its value tier** (PICKS): the tennis model has measured
  negative alpha vs the market (−18% walk-forward ROI, −13.4u live 7/12–21,
  CLV ≈ 0, probabilities ~11pp overconfident), so tennis games now derive
  MODEL picks only — never STRONG/LEAN, never auto-bet, never on the VALUE
  scanner. Mirrored across `store/picks.py`, `src/picks.ts`, and ValuePage.
- **Suspect-EV demotion** (PICKS): value picks claiming more than 15% EV are
  tiered LEAN, not STRONG — the live graded record shows claimed EV
  anti-selects (every bucket above 10% EV lost money; EV≥30% won just 30%).
  The autobet artifact gate tightened to match (`MAX_CLAIMED_EV` 0.20→0.12).
- **Dry runs no longer need a wallet** (AUTO-BET): percent/confidence/kelly
  sizing with no readable balance previously halted every sweep
  (`no_balance`, scanned=0 — walletless installs sat idle for days). A dry
  run now sizes against a $1,000 paper bankroll and keeps recording paper
  bets; live mode still halts, since real money has nothing honest to size
  against.

- **Paper bets now settle** (AUTO-BET): dry-run rows grade W/L/PUSH with
  real P&L (taker fees included) in the log as soon as the game settles —
  the paper record previews live performance instead of sitting at
  "dry run" forever. Dry P&L never touches the real-money stops.
- **Honest paper fills**: a dry run only records a bet where a real FAK
  would actually have filled — the live ask must cross the EV-floor limit —
  and the fill is priced by walking the crossable ask levels (saved as the
  row's fill), never at the max-prob/EV cap. An unreadable book now skips
  the paper bet (`no_book` skip) instead of inventing a fill at the cap;
  live orders keep failing open (the FAK price cap is their safety). Since
  any crossable fill is at-or-below the EV-floor limit, paper entries now
  clear `min_ev` at the executable price, not just the snapshot price.

## 0.2.0 — 2026-07-16

- **Real-book fills in the $-ROI backtest** (BACKTEST): a new
  `jobs/bookfill.py` harvest pulls actual pre-kickoff order books (best
  bid/ask + top-3 depth) from the pmxt public archive (CC BY 4.0, CLOB-V2
  era) into `pm_book_snapshots`; where covered, the backtest adds a **BOOK**
  fill row that prices entries at the real archived ask — no guessed tick
  padding. WNBA + NBA harvested by default; bigger sports gated behind
  `--i-know-the-cost` (measured ~50 MiB transferred per hour-file).
- **Autobet depth gate**: before placing a FAK the engine reads the live ask
  book — no crossable depth skips, thin depth shrinks the order to the
  executable size (never inflates), and every evaluated candidate's
  ask/depth vs the pick's stale price is logged (`book_observations`) as
  ground truth for the backtest's fill model.
- **Real fill reconciliation**: placed orders now read their actual fills
  off the CLOB trades feed (`fill_shares`/`fill_prob`) — a partially filled
  FAK no longer grades as a full fill in autobet P&L or the stop-loss math.
- **Per-market fee schedule**: the EV-floor limit price now uses each
  market's own Gamma `feeSchedule` (the live source of truth — the sports
  taker rate already changed once) with the 5% constant as fallback, and the
  applied rate is persisted per bet so grading survives future fee changes.
- **Min price** (AUTO-BET · selection): never buy shares below this price —
  the longshot filter pairing the existing max-price cap. Wired through the
  live sweep (`below_min_price` skip), the $-ROI backtest simulator, and the
  drift monitor, so backtests replay the gate exactly.
- **Backtest replays your saved settings at click time**: RUN BACKTEST now
  re-fetches the live auto-bet strategy when pressed, so a change saved on the
  AUTO-BET panel is what gets replayed — no page reload needed.
- **Boards capped at 7 days out, soonest first**: predictions further out are
  stale guesses (rosters, lines and ratings all move), so every sport's board
  now drops games beyond a week and serves them in kickoff order.

- **Auto-bet strategy system** (AUTO-BET panel, reorganized into Strategy /
  Sizing / Selection / Timing & caps / Stops): new gates — minimum model win
  probability and minimum edge (model prob − market-implied prob, in points) —
  alongside the EV floor; a **daily take-profit** to pair with the daily
  stop-loss; a tighter **max-hours-to-resolution** window; and a new
  **confidence-scaled sizing** mode that stakes a % of balance interpolated
  between a low- and high-confidence band (e.g. 2% on coin-flips → 8% on
  near-locks). Built-in presets (Conservative / Balanced / Aggressive) plus
  save/load/delete of named strategy profiles — applying a strategy tunes the
  rules but never arms the engine. New endpoints under `/api/autobet/profiles`.
- **BACKTEST panel** (new sidebar page): each model's walk-forward history —
  calibration curve (predicted vs realized, 10 bins), Brier score, and a
  confidence-threshold sweep ("only bet games the model is ≥X% sure of"),
  with your active auto-bet win-prob floor highlighted on the sweep. The live
  pick-record ROI rides alongside; the dollar-ROI strategy backtest unlocks
  as Polymarket price history accrues. New endpoints `GET /api/backtest[/{sport}]`.
- Picks now persist the model's win probability (`picks.model_prob`, additive
  DB migration) so future strategy backtests replay exactly what the model
  believed at capture time.
- **Kelly stake mode** (AUTO-BET · sizing): fractional Kelly sized by EDGE
  (model prob vs limit price), default 0.25× with a hard 5%-of-balance cap —
  it accelerates gains only where edge is real, so keep it behind validated
  gates. Also available in the $-ROI backtest simulator for per-sport
  validation before going live.
- **Drift monitor** (AUTO-BET): live graded autobet record vs the backtest's
  expectation at your current gates — the alarm bell for a dying edge (or a
  flattering backtest). New endpoint `GET /api/autobet/drift`.
- **Slip guardian** (TRACKER): pending bets are checked against the injury
  feeds — OUT/doubtful/IR players on either team of a game you have money on
  are flagged before kickoff. New endpoint `GET /api/mybets/guardian`.
- **CLV coach** (TRACKER · MY BETS): every logged bet is scored against the
  last pregame odds capture — beat-the-close rate, average closing-line value
  (moneyline price % / totals line points), and an honest coaching verdict.
  Sustained positive CLV is the strongest known predictor of long-run
  profit; no book will ever show you this number. New endpoint
  `GET /api/mybets/clv`.
- **Totals distributions measured, not guessed**: walk-forward residuals over
  the cached score history (24k+ games) set the sigmas — NBA 19.0 (n=2,553),
  WNBA 17.5 (n=705), NFL 13.8 (n=7,174) — and caught that MLB runs are
  overdispersed vs Poisson (2.27×), so MLB totals now use Normal(σ=4.5);
  NHL is near-perfectly Poisson (0.87) and stays.
- **Totals (over/under) v1**: every game card now carries O/U edges — the
  model's predicted total converted to P(over) per sport (Poisson for
  NHL/soccer/MLB with exact pushes, Normal with per-sport sigma for
  basketball/football) against the best pregame book line (in-game books
  excluded), with EV both sides. Displayed in the game drawer with one-tap
  OVER/UNDER tracking; totals bets store their line and grade automatically
  (exact hits push). Deliberately kept OUT of picks/autobet until a totals
  walk-forward backtest validates the distributions.
- **Soccer joins the measured universe**: the engine persists a leakage-safe
  per-fixture walk-forward log (monthly refits, 7,306 fixtures, 3-way probs),
  and the dollar-ROI join gained a soccer mode for Polymarket's binary
  "Will X win?" / "end in a draw?" markets (question-text matching with a
  strict club-name matcher; 2,127 joined, 0.8% mismatch). Verdict: −21.8%
  at pessimistic fills — the worst market for the models, completing the
  seven-sport map.
- **NHL + NFL join the measured universe**: both engines now persist
  team-named walk-forward logs (NHL 2,792 games, NFL 5,417), unlocking their
  calibration panels, PROVEN HIT RATE parlay legs, and $-ROI verdicts —
  NHL −9.1% and NFL −5.6% at pessimistic fills, completing the pattern:
  every liquid market beats the models; thin WNBA remains the one edge.
- **Parlay PROVEN HIT RATE**: the slip verdict now shows a track-record-
  adjusted win chance — each leg's model confidence is replaced by the REAL
  hit rate of that confidence bucket from the sport's walk-forward record
  (NBA/WNBA/MLB/tennis, thousands of graded games), so "WIN CHANCE 42%" comes
  with receipts.
- **Auto-bet money-correctness round** (from the self-research swarm): realized
  P&L now subtracts the taker fee on every settled bet so stops fire on true
  numbers; percent/confidence sizing decrements the balance within a sweep
  (one pass can no longer commit more than the bankroll); the sweep re-checks
  edge against the LIVE Polymarket price at bet time (stale-pick adverse
  selection killed); market-consensus picks are never bettable; claimed EV
  above 20% is treated as a model artifact and skipped. New **Validated WNBA**
  preset encodes the only strategy that survived out-of-sample validation.
- **Auto-bet sport allowlist**: pick exactly which sports the engine may bet
  (SELECTION section chips; none lit = all). Driven by the backtest verdicts —
  liquid markets (NBA, tennis) beat the models; thin ones may not.
- **Strategy threshold optimizer** (BACKTEST panel, OPTIMIZE button): grid-
  searches min-EV / min-win% / min-edge over the backfilled history with a
  70/30 chronological tune/validation split and labels each combo HOLDS or
  OVERFIT — only validation-green gates deserve trust. New endpoint
  `GET /api/backtest/{sport}/roi/optimize`.
- Tennis joined the dollar-ROI backtest: the engine now persists a per-match
  walk-forward log (alphabetical orientation, tournament-date tolerance in
  the join), which also gives tennis a full calibration panel.
- **Dollar-ROI strategy backtest** (BACKTEST panel): replay your ACTIVE
  auto-bet strategy against real backfilled Polymarket history. A one-time
  harvest (`POST /api/jobs/backfill`, or the panel's START HISTORY HARVEST
  button) pulls every closed sports moneyline market from Gamma (~36k, back
  to mid-2024) plus pre-kickoff price snapshots (T-15m/1h/6h/24h) from CLOB
  prices-history into new `pm_markets`/`pm_price_snapshots` tables. Games
  join to each model's walk-forward log by date + team names (resolution
  cross-checked), then the strategy replays with era-correct taker fees
  (3% → 5% at 2026-07-10) at three fill assumptions — trust the pessimistic
  column. MLB/NBA/WNBA logs now persist team names to power the join (which
  also gave MLB a full calibration panel). New endpoint
  `GET /api/backtest/{sport}/roi`.
- **Sharp money panel** (INTEL page): per-game Polymarket whale flow (24h,
  ≥$500 prints), each side's biggest holder, and any top-100
  sports-leaderboard wallets positioned — display-only context from the
  public Data API, never an autobet input. New endpoint
  `GET /api/sharpmoney/{sport}`.
- **Auto-bet fee correctness**: Polymarket raised the sports taker fee to 5%
  (sports_fees_v2, 2026-07-10) and the engine's EV math was gross of fees —
  limit prices now net the fee out (rate × p(1−p) per share), so `MIN EV` is
  a true after-fee floor. Also updated the neg-risk adapter address to the
  CLOB v2 collateral adapter (the v1 adapter retired 2026-07-17).
- **MLB model retuned (mlb-elo-v3)**: joint Elo × pitcher parameter sweep
  (tuned on 2025 only, validated walk-forward on untouched 2026): slower team
  Elo (K 4→2) and longer pitcher memory (K_p 8→12, span 20→30). 2026
  validation: accuracy 55.0% → 55.5%, log loss 0.6897 → 0.6884. An XGBoost
  feature overlay (rolling runs, rest days, schedule context) was tested and
  rejected — it lost to raw Elo on both tune and validation years, so the
  extra features stay out.
- Sidebar: every tab now shows in both SIMPLE and PRO modes (the modes still
  change how pages render, just not which tabs exist).

## Unreleased

- **Release CI now builds the hardened bundle.** `release.yml` called plain
  `npm run dist`, so every installer it produced shipped the "protected"
  modules as decompilable `.pyc` — the Nuitka layer existed but was absent
  from the artifacts users actually install. It now calls `dist:hardened`.
- **The license gate ships as native code, not just the license check.**
  `api/main.py` (which holds the app-wide `_license_gate` 402 middleware) is
  now compiled alongside `license.py`; previously the check was native but its
  call site was patchable bytecode. Verified on the frozen bundle: 48 routes
  register, and `/api/board/*` returns 402 unlicensed with no `.pyc` on disk
  for either module.
- **`dist:hardened` can run off Windows.** The harden script matched only
  Nuitka's `.pyd` output, so it aborted on macOS/Linux (which get `.so`);
  the extension is now derived from the platform. Windows is validated —
  the mac/Linux path still needs a CI run to confirm.
- **MODELS · RETRAIN no longer deletes an engine's model without rebuilding
  it.** `nba._model_stale()` only consulted the bundle held in memory, so the
  force-retrain path (delete pickle → `refresh_data()`) saw a "fresh" model and
  skipped the retrain — leaving the pickle gone until the next backend restart.
  It now checks the file on disk too, matching `nfl._model_stale()`.
- **MODELS · tennis refresh survives a bad row upstream.** One typo'd
  `tourney_date` in the TennisMyLife ongoing feed ("202s60807") raised
  ValueError out of `replay()`, so `refresh_data()` never reached
  `_save_state()` and the tennis ratings silently froze for days. Unparseable
  rows are now skipped with a warning.
- MODELS · a refresh that writes no state says so ("no new results · already
  current") instead of reporting "state written". An engine already caught up
  to the end of its history window — nba between the Finals and October — was
  reporting success while the panel's "updated N days ago" kept climbing.
- **Removed the TELEGRAM tab** and its bot sender, to declutter the sidebar.
  Discord webhooks remain the auto-post path. Any saved Telegram config in
  `settings.json` is ignored (harmless leftover; nothing reads it).
- **Always-on companion**: Scout now boots with Windows (silently minimized
  to the system tray) and the X button hides to the tray instead of quitting,
  so scheduled captures, Discord posts and auto-bet sweeps run
  without the window open. Tray menu: Open / Quit. Both behaviors default ON,
  toggleable under SETTINGS → SYSTEM.
- Discord picks posts: sport tag + "% to win" on every line, only
  upcoming games (started/final games and their stale odds are never posted),
  local-timezone slate handling, real-payload TEST sends, and the Scout
  red-diamond avatar auto-applied to Discord webhooks.
- **Polymarket auto-betting** (new AUTO-BET panel in the sidebar): tail the bot's value
  picks with real CLOB orders, powered by the krypt-polybot trading core
  (vendored: EIP-712 signing, deposit-wallet POLY_1271 support, encrypted
  at-rest credentials via DPAPI/keychain). Scout's own game↔market matcher
  resolves the tradable market, so the engine can only bet games the odds
  board would quote. Money rules: EV-floor limit prices (fill-and-kill,
  never resting), fixed-$ **or** %-of-balance stakes, one bet per game, daily
  caps, lead-time and max-price guards, and realized-P&L stops (daily
  stop-loss, lifetime stop-loss, stop-profit) that halt the engine outright.
  Defaults to **disabled + paper mode** — two explicit switches before real
  money. Placed bets auto-log to MY BETS ("AUTO · polymarket") and grade with
  everything else. New endpoints under `/api/autobet`; sweeps run after every
  board capture.

- TRACKER grew a second tab: **BOT PICKS** — every +EV value pick the bot has
  ever recommended (pending + graded, flat 1u) with its own P&L tiles, equity
  curve, and filterable log, so the tracker's "my bets" number and the
  dashboard's model UNITS number finally live side by side. New backend
  endpoint `GET /api/picklog`.
- **Default stake setting** (SETTINGS → TRACKING): every TRACK button — pick
  cards, the game drawer, and the parlay slip — now logs at your chosen stake
  (0.1–100u) instead of a hardcoded 1u. Button labels show the stake when it
  isn't 1u.
- Parlays are now trackable: **TRACK PARLAY** on a fully-priced slip logs it
  to MY BETS at a flat 1u. Slips grade leg-by-leg as games settle — a lost
  leg loses the slip, pushed legs drop out of the payout, all-pushed slips
  push — and count as one bet in your P&L. New tables `user_parlays` /
  `user_parlay_legs`, endpoints `POST /api/mybets/parlay` and
  `DELETE /api/mybets/parlay/{id}`; demo mode fully supported.

## 0.1.0 — 2026-07-12

First public release. Everything below shipped between the first commit and launch.

### Engines & models

- Nine sport engines behind one contract: NBA, WNBA, NFL, NCAAF, NCAAB, MLB, NHL,
  soccer (11 leagues: top-5 European + Championship, Eredivisie, Primeira Liga,
  MLS, Brasileirão, Liga MX), and tennis (ATP/WTA).
- Shared Elo core (K, home advantage, margin-of-victory, season carry) plus
  Dixon-Coles-lite Poisson for soccer and surface-blended Elo for tennis.
- v2/v3 upgrades where they earned their keep: NFL XGBoost blend (QB, rest,
  preseason, weather), MLB pitcher EWMA, neutral-site handling for college.
- Every engine backtests from the CLI (`python -m kryptscout.sports.<sport>`)
  and reports accuracy + log loss against the devigged closing line.
- MODELS engine room in the app: per-engine consoles, retrain/fetch buttons,
  scheduled daily refresh.

### Odds & data

- Free odds stack: ESPN lines, Polymarket, and Kalshi merge into one line-shopped
  odds table — no API key required. The Odds API is an optional add-on (tennis odds).
- Injuries (ESPN feeds), game weather (Open-Meteo + 62-venue dome table), and
  per-book line movement.
- Tennis data relicensed onto TennisMyLife (MIT) + tennis-data.co.uk; all
  runtime-downloaded sources cached locally, never redistributed.

### Picks, tracking & the terminal

- PICKS: one recommended bet per game (highest-EV edge, tiered STRONG/LEAN),
  with line shopping across books.
- TRACKER: hit TRACK on any pick, it lands in your personal bet log and grades
  automatically when the game settles — your P&L is the headline record,
  separate from the model's auto-graded PERFORMANCE page.
- PARLAY: 2–8 leg builder priced two ways — book price vs model fair price —
  with an honest verdict and correlated same-game legs blocked.
- SCOUT TERMINAL mission-control panel, DASHBOARD landing page, TODAY cross-sport
  slate, VALUE board, INTEL panels, CLV + performance history in SQLite.

### Desktop app

- Electron shell supervising a bundled PyInstaller Python backend (health checks,
  crash restarts), in-app scheduler for capture/settle/refresh jobs.
- Discord webhook and Telegram bot senders auto-post the day's picks.
- First-run onboarding, SIMPLE/PRO nav modes, share cards, BONUSES page with
  referral links, Discord Rich Presence.
- Verified NSIS installer pipeline (`py:bundle` + `dist`); auto-update
  scaffolding via electron-updater (dormant until a publish config lands).
