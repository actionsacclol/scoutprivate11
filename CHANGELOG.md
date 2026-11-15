# Changelog

## Unreleased

- **Release build on all three runners** (FIX): the first v0.3.3 run
  failed on all three runners. Linux: the packaging hook that flips the Electron fuses
  looked for a binary called "Krypt Scout", but electron-builder names the
  Linux binary `krypt-scout`; the hook now asks the packager for the name
  (checked by packaging the Linux folder locally). Windows: the tests died
  with "No module named pytest" because the dependency install before them
  had failed and `py:setup` reported success anyway. In CI a failed install
  now fails that step, and the build uses the Python that
  `actions/setup-python` installed (3.11) instead of `py -3`, which picks
  the newest Python on the runner image. macOS: the job never started
  ("The configuration macos-14-arm64-us-default is not supported") because
  GitHub retired the macos-14 image on 2026-11-02; the job now runs on
  macos-15, which is also Apple Silicon.
- **"Setup did not finish … (fetch failed)" on a new install** (FIX): the
  scheduler asked the backend for the first results download with `fetch`,
  which gives up when no answer has arrived after 300 seconds, whatever
  timeout the app sets. The download now takes about 305 seconds on a new
  install (the NHL engine loads the current season too), so it was reported
  as failed at exactly five minutes while the backend carried on. Jobs are
  now sent with `node:http`, which has no such limit, and the refresh cap is
  15 minutes. Verified on a fresh profile: "9/9 engines · 305s".
- **The Discord post carries its record** (NEW): every card that is posted
  is written down (`card_picks`: side, probability shown, cheapest exchange
  price after fees) and graded from final scores, and each post prints the
  running result between the picks and the links: "Record: 12–6 (67%) ·
  +1.4u since Nov 14 · last card (Nov 14) 4–2". One unit a pick at the price
  a reader could have paid; a pick no exchange listed is staked at its fair
  price; a soccer draw is a loss; losing days print too. The count starts
  with the first post sent on this version (nothing is backfilled), a test
  post shows the line without being recorded, and the settings page preview
  shows it. `POST /api/picks/card/posted`, `GET /api/picks/card/record`
  (store/cardrecord.py).
- **The Discord post no longer takes over the webhook** (CHANGE): the app
  used to rename each webhook to "Krypt Scout", replace its picture with the
  Scout mark and post under that name. It now sends neither: the post
  appears under the name and picture the server's owner gave the webhook. A
  webhook already renamed by an earlier version stays that way until it is
  changed in Discord. The closing "Get Krypt Scout — krypt.cc/tools/scout"
  line is gone too; the post ends with the sign-up links.
- **One click runs the picks and sends them** (CHANGE): a send used to post
  from the last capture, up to four hours old. Every send — the Discord
  page's button, now RUN + SEND NOW, and the daily auto-post — runs the
  picks job first, so the card is chosen from prices pulled that minute. If
  the job fails the post still goes out on the picks already recorded.
- **RUN PICKS button** (NEW): the Picks toolbar can run the picks job on
  demand — fresh prices for every sport and today's picks recorded — instead
  of waiting for the next scheduled run. Desktop app only; hidden in demo.
- **Picks are 50% to 85%, everywhere** (CHANGE): the Picks page, the
  Dashboard's top pick and the Terminal no longer list a pick outside that
  band (on 2026-11-14 the Picks page led with 96%, 94% and 93% favourites at
  -2684, -1584 and -1372). Same rule as the Discord card.
- **NHL and European soccer were running on last season** (FIX): both engines
  listed their seasons as literals ending at 2025-26, so no 2026-27 game was
  ever loaded. Every NHL number since late September came from June's
  ratings, and the eight European leagues were frozen at May. Both now derive
  the season window from the date. Soccer also maps the 18 clubs promoted
  for 2026-27.
- **NHL model: `nhl-elo-ctx-v3`** (CHANGE): Elo plus back-to-back flags and a
  shots-on-goal form term. On this season's 312 games its log loss is 0.6452
  against 0.6655 for what was being served, and on the 117 games with a
  Kalshi price it is level with the market (+0.005, interval −0.005 to
  +0.015). The MoneyPuck xG path is off by default: its fit labelled every
  shootout a home loss, and even corrected it does not beat Elo. The file is
  no longer read.
- **Soccer model: `soccer-poisson-v3`** (CHANGE): team strengths are fitted on
  the goals implied by each game's closing odds where the results feed
  carries them (MLS keeps goals scored). Held out on Aug–Nov 2026: log loss
  1.0009 → 0.9891 from reading the current season, then 0.9976 → 0.9841 from
  the new input on the games with odds. Still behind the market.
- **College opponents from lower divisions were priced as average teams**
  (FIX): an FBS team hosting an FCS school, or a D1 basketball team hosting a
  non-D1 one, was priced as if the visitor were a mid-table peer.
  `ncaaf-kalman-v3` and `ncaab-kalman-v3` give those opponents a fitted
  lower-division prior, read D1 membership from the conference id, fix a
  season roll-over bug in basketball and regress toward the conference mean
  between seasons. Held-out log loss: football 0.5397 → 0.4793, basketball
  0.5595 → 0.4883. Also fixed: Charlotte and Troy were filed under two small
  colleges that share their names in ESPN's directory, so neither matched
  its rating on the live board.
- **NFL: the listed starter is the one priced** (FIX): the engine used each
  team's last starting quarterback even when the schedule file already named
  someone else for the coming game (8 team-sides on 2026-11-14). It now reads
  the listed starter and falls back to the last one further out. No
  constants changed.
- **NBA, WNBA, tennis retunes** (CHANGE): NBA home advantage 60 → 40 Elo (the
  home side was overstated by 3.0 points; no log-loss gain claimed) plus a
  25-point back-to-back penalty. WNBA K 28 → 16, home advantage 80 → 50,
  carry 0.75 → 0.5, temperature off: 0.6066 → 0.5979 on 2026. Tennis adds
  Challenger results to the ATP rating pool (0.6302 → 0.6208) and no longer
  stops at a hard-coded year. Each state file is rebuilt once on first
  refresh.
- **Predictions are kept hourly** (CHANGE): one row per game, model and UTC
  hour before kickoff instead of a single sliding row, so a morning number
  survives to be scored later. Nothing after kickoff is stored, as before.
- **A new install rated every team the same** (FIX): the ratings are built
  from past results, and the job that downloads them only ever ran on a 24 h
  timer counted from launch. A new install waited a day for it, and anyone
  who closes the app each night never reached it — their NBA board showed the
  same 58.5% home probability for every game. It hit updates too: an update
  that discards an engine's saved state left that sport flat the same way.
  The refresh now runs at startup whenever it is due (never run, a day old,
  or last run by a different app version), before the first capture so no
  picks are recorded off untrained ratings, and the daily cadence is measured
  from the last success instead of from launch. A first run downloads about
  five minutes of history in the background (measured: 283 s, all nine
  sports, app responsive throughout); a strip under the title bar says so
  and clears itself, or offers TRY AGAIN if a source was unreachable.
- **Less to look at** (CHANGE): the sidebar's eighteen tabs are grouped —
  the four everyday pages, then Betting, Research and Connect, with SETTINGS
  and ABOUT pinned at the bottom. SIMPLE folds Research and Connect until you
  open them (one click, remembered); PRO shows everything. PICKS no longer
  heads every game "Recommended bets / TAKE": prices that beat the fair are
  listed first as "Bets worth taking" (often none, and it says that is
  normal), and favorites with nothing to bet sit below, marked NO BET.
- **The Discord post is a short card, not the slate** (CHANGE): with MODEL on,
  a send posted the first 12 of every favourite recorded — on 2026-11-14 that
  was "…and 142 more", with games repeated (one row per model that rated
  them) and every line reading "(fair 0) · ?% to win". It now posts a card of
  six (PICKS PER POST, 3 to 12): every pick between 50% and 85% to win
  (value picks included; nothing heavier, no underdogs, and the same rule on
  the fallback path; a pick one exchange has just over 85% stays when
  another sells it at 85¢ or under, and that exchange is the bold one),
  value picks first, then favourites an exchange prices
  that the app's own model also backs, one per game, at most two from any sport on a card of six, games no engine
  rates last. That day: 113 games became 6. The ranking is a filter for
  agreement, not a claim of edge. Each line now carries the opponent, the
  real win probability and the kickoff in the reader's own time zone. MODEL is
  on by default for new installs, so there is a post on days with no value
  pick. Built by `POST /api/picks/card` (odds/compare.py).
- **Discord picks compare the exchanges** (NEW): under each pick the post
  lists what Polymarket, Kalshi and Polymarket US charge for that side
  ("Polymarket 68¢ · Kalshi 67¢ · Polymarket US 68¢"), the cheapest after
  each exchange's own fee in bold. An exchange that does not list the game is
  left out; if the card cannot be priced the picks post without it. Needs no
  account.
- **Discord post: sign-up links and a legal footer** (CHANGE): every exchange
  name in the post is that exchange's sign-up (referral) link — the site,
  never a market page — and every post, even an empty day's, closes with
  "Free bonus when you sign up: Polymarket · Kalshi · Polymarket US". The
  three links live in shared/exchange-links.ts, and the app's own buttons
  (BONUSES, onboarding, AUTO-BET sign-up) now use the same three — they
  replace the older Polymarket, Kalshi and Polymarket US links. The
  footer no longer carries the product name: it is the legal line (not
  advice, age and legality, the links are referral links, 1-800-GAMBLER).
- **The Discord preview is the real post** (CHANGE): MESSAGE PREVIEW was drawn
  from the boards on screen and could differ from what was sent (it showed
  win probabilities the post did not have). It is now built by the sender
  itself — same recorded picks, same card, same prices — with a REFRESH.
- **Polymarket US found almost no NBA or NHL games** (FIX): it was searched
  by the home team's full name, and the exchange titles games "BKN Nets vs.
  MEM Grizzlies" — "Memphis Grizzlies" returned Memphis college basketball.
  On 2026-11-14 that found 0 of 5 NBA and 0 of 10 NHL games (NFL was fine).
  It now searches both nicknames first, and the two schools plus the sport
  for college games, falling back to full names: 41 of 45 games across five
  sports, against 18. Auto-bet's Polymarket US prices come from the same
  lookup, so before this it was pricing little beyond the NFL there.
- **Replay onboarding in Settings** (CHANGE): SETTINGS → Appearance has
  REPLAY ONBOARDING and REPLAY TOUR (they were only on ABOUT).
- **Auto-bet: accounts first** (CHANGE): the AUTO-BET page opens on "Your
  accounts" — Polymarket, Kalshi and Polymarket US, each with a CONNECT
  button that opens its key form and a SIGN UP link. The "What it bets" card
  is folded into one line under the engine switches, and the two dozen rule
  fields sit behind a RULES line that reads e.g. "$10 a bet · min EV 4% · up
  to 5 a day" (folded in SIMPLE, open in PRO, your choice remembered).
  Polymarket US joins the BONUSES page.
- **First run** (CHANGE): the license screen is three numbered steps with a
  button to krypt.cc; onboarding opens by saying the app already works and
  that a Polymarket account is optional; the tour no longer says "nine tabs".
- **Boards that would not load** (FIX): NBA and NFL — and, less often, the
  other ESPN sports — came back empty for many users. ESPN rejects
  `dates=YYYYMMDD-YYYYMMDD` scoreboard ranges from some networks; the schedule
  fetch now asks one day at a time and catches up from the last cached day.
  The three odds providers run concurrently under a per-provider timeout
  instead of in series (one slow venue no longer takes the board past the
  renderer's deadline), Open-Meteo forecasts are cached per venue and day, the
  board request has 30 s instead of 8, and a sport whose request failed now
  says "couldn't load" with the reason and a retry instead of showing "no
  games scheduled".
- **xgboost is gone** (CHANGE): the gradient-boosted overlays on NBA and NFL
  are retired. NBA's last bundle scored log loss 0.608 against 0.567 for the
  Elo underneath it (719 held-out games) and had already gated itself off;
  NFL's blend bought 0.0006 log loss over Elo on 2024-25. NBA is
  `nba-elo-v1`, NFL is `nfl-elo-v2` (Elo + QB adjustment + rest, tempered).
  All nine engines are now ratings replays; nothing is fitted or pickled.
  `core/modelio.py` is deleted, `xgboost` / `scikit-learn` leave
  `requirements.txt`, the macOS `libomp` step leaves CI, and the PyInstaller
  spec excludes xgboost, sklearn, scipy and duckdb — the backend bundle goes
  from 406 MB to 189 MB. Leftover `nba_xgb_*.pkl` / `nfl_xgb_*.pkl` are deleted
  from the models folder at startup. The MODELS page loses its RETRAIN button
  and "Pickle" cell (there is nothing to retrain); REFRESH is unchanged.
- **Auto-bet works, on three exchanges** (FIX / NEW): auto-bet read the
  value picks, and since value picks became sportsbook prices it had nothing
  it could ever buy. It now has its own rule and needs no AI: each sweep
  reads the boards and buys a side on Polymarket, Kalshi or Polymarket US
  when a DIFFERENT exchange's vig-free price says it is worth more after the
  buying exchange's own fee. Reference Kalshi = "VS KALSHI",
  reference Polymarket = "VS POLYMARKET" (the one that lets Kalshi itself be
  bought); both are on by default and each can be switched off. Soccer and
  tennis are excluded.
  Kalshi (API key ID + private key, RSA or Ed25519) and Polymarket US (key ID
  + secret) connect on the AUTO-BET page next to the Polymarket wallet; all
  three are encrypted at rest. Paper mode needs no account and fills off the
  real order book. Orders are immediate at a capped limit and sent once: an
  order that is never answered is held as "check your account" (it keeps the
  game and the daily slot) instead of being retried, and an accepted order is
  only counted once the exchange confirms the fill. Kalshi orders can move
  their own shortfall between your Kalshi cash pools first (switchable). One
  bet per game across all exchanges. The activity card now says what the last
  sweep saw: games, prices checked, why each was passed on, and the six
  prices nearest your floor. The "Validated WNBA" preset and the drift
  monitor are removed (both described the old model-driven rule). Honest
  expectation: exchanges usually agree to within a point, so at the default
  4% floor most sweeps place nothing, and nothing measured says the gap is
  profit. The Kalshi and Polymarket US live order paths are ported from
  working bots and covered by tests, but have not yet placed a real order
  from this app.
- **Agents can bet, if you switch it on** (NEW): two more MCP tools,
  `get_bet_prices` (each exchange's live ask, fee and break-even for one
  game) and `place_bet`. Off by default; AI AGENTS has the switch (off /
  paper / live), a per-bet cap ($10) and a daily allowance (3). A request
  over the cap is refused, not trimmed; the price may not exceed what the
  agent's own stated probability supports after fees, nor reach more than
  two cents through the best offer; one bet per game shared with the rule
  engine; your stops halt both. Every bet and refusal is in the page's log.
- **AI agents (MCP)** (NEW): an MCP server inside the backend, off until
  switched on from the new AI AGENTS page. Thirteen read / bet-log tools: status, a sport's
  board, one game in full, recorded picks, the pick record, the engine record,
  arb sightings, line movement, injuries, the bet log (read, add, remove) and
  auto-bet status (betting is the separate entry above). One
  click adds it to Claude Desktop, Claude Code, Cursor, Codex or Windsurf
  (their own config is edited in place, a timestamped backup is kept, an
  unreadable file is refused rather than overwritten); VS Code is copy/paste.
  Clients launch `krypt-scout-backend --mcp`, a stdio relay that finds the
  running app and reads the access token from the encrypted store, so nothing
  secret is written into another program's config and the entry survives the
  backend's port changing every launch. `/mcp` answers loopback only, refuses
  any request carrying an `Origin` header, requires the token, and is behind
  the license like the rest of the app. Speaks the handshake revisions
  (2024-11-05 … 2025-11-25) and the stateless 2026-07-28 one. Connected
  clients, and every write or refusal, are listed on the page.

- **The "closing-line signal" was the devig** (MEASUREMENT, 2026-09-02): with
  the close backfilled over twice the sample (line_endpoints 431 → 895 games),
  non-soccer exchange picks scored against the DraftKings close read
  **+0.90% ± 0.20 under multiplicative devig and +0.16% ± 0.20 under power
  devig** (n=277). Multiplicative devig over-credits underdogs — dogs +1.8%,
  favourites 0.0%, and the split flips under power devig. Only MLB survives at
  +0.30% ± 0.13, a tenth of the taker cost. Net of Polymarket's taker fee and
  half-spread the same picks sit at about −2.8%; the open-to-close drift on
  their side is −0.74 pts (the market moves *away* from them). A market-
  anchored model (DraftKings + exchange gaps + model residual) adds nothing
  beyond the close on either the live store (residual z −0.18) or 6,648
  archive soccer matches vs Pinnacle closes (z −0.16 to −1.30). Consequences
  below.
- **`clv_vs_close` no longer manufactures a soccer edge** (FIX): soccer picks
  are priced on a 3-way market but ESPN's DraftKings soccer close is 2-way (no
  draw). Devigging two sides against a price set for three read as **+36.7% EV
  at a 99.4% positive rate over 166 picks** and flipped the overall figure to
  "significant". Rows whose close does not carry the pick's full market are now
  counted in `n_unscoreable` and excluded from every column. The scorer also
  reports `fair_ev_power_pct` (power devig) next to `fair_ev_pct`, and
  `significant` is only ever claimed on the power figure.
  `core.oddsmath.remove_vig_power` is new. `test_line_endpoints.py` pins both.
- **Picks keep their first-sight entry price** (FIX): `picks.price` refreshes
  until kickoff, so it was the venue's *last pre-game quote* — 98% of recorded
  Polymarket picks carried the exchange's closing mid — and every "CLV" built
  on it compared one close to another. New `entry_side/entry_book/entry_price/
  entry_at` columns are written once and never refreshed (`db.upsert_pick`,
  migrated on connect); `clv_vs_close` scores the entry when present and
  reports `n_entry_recorded`.
- **Post-kickoff predictions are refused** (FIX): 61% of the live `predictions`
  table had been written after kickoff and scoring it reversed the market-vs-
  model verdict. `db.upsert_prediction(starts_at=…)` returns None and writes
  nothing once the game has started; `performance()` and `clv()` score each
  game on its last *pre-game* row only, so legacy stores stay honest too.
- **Soccer joins tennis in `NO_VALUE_SPORTS`** (both `store/picks.py` and
  `src/picks.ts`): STRONG −11.0% walk-forward vs football-data closes
  (n=2,129, CI below zero), live value picks −4.2% at midpoint prices with
  same-book CLV −0.8% ± 0.6, and a slate capped to its best edges returns the
  same ROI at every cap. Soccer now yields MODEL picks only, which also takes
  it out of autobet.
- **Exchange quotes are priced at the executable ask** (FIX): Polymarket and
  Kalshi quotes were the bid/ask *midpoint*, which nobody can trade, so every
  exchange EV on the board overstated by roughly half the spread (~1% of stake
  on a 1c book). `odds/polymarket` now prices outcome 0 at its ask and outcome
  1 at 1 − bid (`Candidate.prob1`, `extract_buy_prices`); `odds/kalshi` prices
  each team at its yes ask. The pair sums to 1 + spread exactly as a book's
  pair sums to 1 + vig; `remove_vig` recovers the mid for consensus purposes.
  Edges that vanish under this change were never real.
- **True arbs, model-free** (NEW): `core/arb.py` finds games where the best
  price on every side across DraftKings, Polymarket and Kalshi buys $1 of
  payout for less than $1 — net of exchange taker fees (0.05·p(1−p)
  Polymarket, 0.07·p(1−p) Kalshi), pre-game quotes only, full-market quotes
  only. `GameCard.arb`, an ARB badge on the tile, a "True arbs" section on
  VALUE (every board, including no-value sports), sightings logged to
  `arb_sightings` (one per game per 15 min) and served by `GET /api/arbs`.
- **Exchange lead — the one signal that survived** (NEW): on 233 live games,
  the DraftKings side that Kalshi favoured ~8h before kickoff predicted the
  direction of DraftKings' own close **64% of the time (CI 58–70%; "favourite
  shortens" gets 57%) for +1.05% ± 0.30 same-book CLV**, +1.37% with 6h+ of
  lead, stable across halves, present on underdogs (Polymarket alone: 59%,
  +0.87%). That is under DraftKings' ~2.2% per-side vig, so it is a
  *reference*, not a pick: `core/lean.py` flags it (`GameCard.lean`, LEAD
  badge), `lean_signals` freezes the first-sight DraftKings pair, and
  `tracker.lean_clv()` (`GET /api/lean`, also `lean` in `/api/clv`, a section
  on PERFORMANCE) scores it against the declared close. Nothing feeds it to
  picks or autobet until the live figure clears the vig at n ≥ 70.
- **Parlay verdict is market-anchored** (FIX): a backtest of auto-generated
  slips from this app's own picks (2026-09-02) showed the model-fair verdict
  stamping 87–100% of them +EV while random 2/3/4-leg slips lost 31/46/40% of
  stake (CIs entirely below zero; the same legs as singles lost 13%). The
  model's claimed hit rate ran 1.5–2.5× actual on EV-ranked slips; the
  market-implied rate tracked reality in every policy. WIN CHANCE and FAIR
  now come from the vig-free market (power devig of each leg's full-market
  quote; `Leg.marketProb`, `ParlayMath.combinedMarketProb/marketFairDecimal/
  marketDrag`), the model's number is a context line, and a "+EV" verdict can
  only come from line shopping beating the vig-free market — never from the
  model. No auto-parlay generator ships; the numbers say it would be a loss
  generator with a rubber stamp.
- **Frozen backend collects the whole ethereum metadata chain** (FIX): the
  first CI run on a fresh venv failed the bundle selftest on every runner with
  "No package metadata was found for py_ecc". A fresh `pip install` resolves
  eth-keyfile 0.10, which newly imports py_ecc, whose `__init__` reads its own
  version through importlib.metadata; the spec copied metadata for three named
  packages only. It now copies metadata recursively from `eth_account` and
  `py_clob_client_v2` and collects py_ecc / eth_keyfile / eth_typing / ckzg
  submodules when present, so the chain can change under us again without
  the bundle noticing. Verified with the local venv upgraded to exactly what CI
  resolved (eth-account 0.14, eth-keys 0.8, eth-keyfile 0.10, py-ecc 8):
  selftest green, 656 tests green.
- **Build survives copy-paste deploys** (CHANGE): both tsconfigs typecheck
  from the real entry points (`src/main.tsx`, `electron/main.ts`,
  `electron/preload.ts`, `vite.config.ts`) instead of whole directories. The
  tree is deployed by copying the folder over another checkout, and a copy
  never deletes: a module removed here survives there and, under
  `include: ["electron"]`, the stale `telegram-picks.ts` broke the release
  typecheck (verified by copying 0.3.2 over a 0.3.1 clone). A file nobody
  imports is now a file nobody checks — the same set Vite bundles. RELEASING.md
  has the copy-paste procedure.
- **NBA engine no longer frozen at the 2025-26 Finals** (FIX): a hard
  `HISTORY_END = 2026-06-30` stopped `refresh_data` from processing any day
  after June 30, so no refresh ever rewrote `nba_state.json` (the Engine
  Room showed it stale all summer, RETRAIN rebuilt only the XGB pickle — which
  the validation gate then parks behind Elo, so nothing visible changed) and
  the 2026-27 season would never have been ingested. The replay now runs
  through yesterday like WNBA. STATE_VERSION 3→4 so the ratings are rebuilt
  under the retuned Elo constants (K 12, home 60, carry 0.5) from the cached
  scoreboards instead of carrying K-20 ratings forward.
- **Value picks are priced against the exchanges, not the model** (CHANGE):
  `store/picks.compute_edges` now needs the sharp fair (`core/fair.py`) and
  emits an edge only for a SPORTSBOOK price sitting at least one probability
  point under it (`MIN_GAP_PTS`), both exchanges agreeing when both quote; an
  exchange never beats its own fair. STRONG = Kalshi set the fair, LEAN =
  Polymarket did; a claimed EV above SUSPECT_EV still demotes (a huge gap to
  an exchange is a stale exchange). EV and Kelly are at the fair; the model's
  own probability is carried as context (`Edge.model_prob`, `Pick.model_prob`)
  and the fair as `Edge.fair_prob` / `Pick.fair_prob` (new picks column).
  MODEL picks are the displayed forecast's favourite, so a card and its pick
  never disagree. Why: every measurement this repo has run says model-vs-
  market disagreement carries no information (claimed +9% EV measured +0.55%,
  median 0, against the Kalshi fair; tailing it returns the vig), while this
  exact rule replayed capture-by-capture over 45 days of the live store (656
  games, 475 with both a DraftKings and an exchange quote) fired 59 times —
  1.4 a day, 12% of eligible games — and the same-book close then moved
  toward the pick by +1.75 ± 0.45 pts (59% toward / 10% flat / 31% against;
  Kalshi-led +1.85, Polymarket-led +1.34). Realized ROI on the 51 graded
  fires: −14.6%, noise at that n (σ ≈ 13 pts) but not profit — the edge sits
  under DraftKings' vig. The tier is honest about being a closing-line signal.
  Autobet gains a `not_polymarket_priced` skip: no value pick is exchange-
  priced any more, so it idles by design until a maker mode exists. Copy on
  PICKS, DASHBOARD, VALUE and the drawer names the reference instead of "our
  model".
- **Schedule-shaped trackers** (NEW): a `game_context` row per game with
  rest days for both sides (from the store's own schedule, `core/context.py`,
  written by `record_cards`), the MLB home-plate umpire and lineup postings
  with first-seen timestamps (one hydrated MLB Stats API schedule call per
  capture, `datasources/mlb_context.py`), and a referee-crew slot for the
  NBA once a verified feed exists. `tracker.context_trackers()`
  (`GET /api/trackers`) scores them against the market: the short-rest
  soccer side at the last pre-game 3-way reference (the one large effect the
  information sweep found, +7–11 pts vs Pinnacle on 478 games, fading by
  season — n≈300 confirms or retires it), and actual-minus-close totals by
  umpire. Never a model input; expected forecast gain is zero by design.
- **NHL xG path, `nhl-xg-v2`** (NEW, opt-in by data): when MoneyPuck's
  listed team game file sits under `raw/nhl/moneypuck/all_teams.csv`, the
  NHL engine rates teams on expected goals instead of goals — EWMAs (span
  25, season carry 0.6) of xG for/against at all situations and 5-on-5,
  goals for/against, and half-speed PDO components — and serves a
  walk-forward ridge logistic on the home-minus-away differences through a
  0.88 logit scale. Measured on 13,511 regular-season games 2015-26, test
  2024-26 (n=2,624): log loss 0.6739 vs Elo's 0.6798, −0.0059 [−0.0109,
  −0.0007], resolution 0.010 vs 0.003; through the shipped code against the
  engine's own live Elo log on the same 2,624 games, 0.6775 vs 0.6851
  (−0.0076 [−0.0123, −0.0026]). Starter GSAx, OT-adjusted Elo,
  Dixon-Coles, a margin Kalman and rest all measured null and are not
  built. The app never fetches or ships the file: MoneyPuck's terms are
  non-commercial with credit and ask for approval before automated pulls;
  without the file the engine is the unchanged v1 Elo. Credit on About.
  No free historical NHL closing lines exist any more (the SBRO archive is
  dead), so the gap to the market is scored forward from October via the
  lines backfill. ESPN's predictor answers 400 for hockey and NHL was
  removed from its supported set.
- **ESPN's predictor captured as a third reference** (NEW):
  `datasources/espn_predictor.py` reads ESPN's pre-game win probability from
  the same unauthenticated core API as the odds document (no key, no
  published limit; concurrency 4, once per game, never on the board poll).
  The capture job stores it for every upcoming supported game (MLB, WNBA,
  NBA, NCAAF, NCAAB — the NFL and NHL endpoints answer 400) and
  `python -m kryptscout.jobs.predictor` backfills settled history, since ESPN
  serves the document for completed events frozen before kickoff (documents
  modified after kickoff are stored with pre_game = 0 and never scored).
  `tracker.espn_benchmark()` (`GET /api/benchmark`, a section on PERFORMANCE)
  scores ESPN, Scout's pre-game forecast, the last pre-game exchange quote
  and DraftKings' devigged close on the same games. Measured 2026-09-02:
  Scout level with ESPN in MLB (0.6711 vs 0.6748), behind in WNBA (0.5250 vs
  0.4996); nothing beats the close.
- **The displayed forecast is the market** (CHANGE): every card now carries
  `GameCard.forecast` (`core/forecast.py`) and the terminal's side
  probabilities and fair lines read it. Where any full-market fair exists —
  Kalshi, Polymarket, or DraftKings flagged as a book — that vig-free number
  is the forecast and the model becomes context; before any venue quotes,
  the model is shown labelled "pre-line ±N pts" with its measured error
  against the eventual open (MLB 4, WNBA 8, soccer 10, NFL 10, NCAAF 16).
  Measured 2026-09-02 on the engines' own logs and the live store: the
  market beats every engine, every variant tried and ESPN's predictor, and
  no blend survives out of sample (model coefficient beside the market −0.10,
  z −0.4); switching the displayed number cuts held-out log loss by 0.013
  (MLB), 0.028 (WNBA) and 0.067 (tennis). Picks, parlay legs and the boost
  evaluator are unchanged — they already priced off the market.
- **College engines move to a Kalman margin filter** (`ncaaf-kalman-v2`,
  `ncaab-kalman-v2`; `core/kalman.py`): a state-space rating on score
  margins with a tuned noise model, replacing Elo for the served win
  probability (the Elo book still drives score nudges and FCS/non-D1
  handling). Walk-forward on the engines' archives with paired bootstrap:
  NCAAF log loss 0.4774 vs 0.5251 (−0.0477 [−0.0556, −0.0389], n=3,829),
  NCAAB 0.5288 vs 0.5513 (−0.0225 [−0.0281, −0.0171], n=5,319). Elo's
  margin-of-victory multiplier compresses the blowouts that carry most of
  college's information; and college is where the exchanges quote almost
  nothing, so the standalone number is the number users see. Replayed through
  the shipped engine on its own FBS-vs-FBS 2025 record (n=808): 0.5327 vs
  Elo's 0.5584. Old state files load with a fresh filter until the next
  refresh replays the archive.
- **NBA Elo retuned, NBA/WNBA/NFL recalibrated** (FIX): the calibration
  audit found NBA and WNBA overstating favourites by 8 and 6 points (slopes
  0.82 / 0.78) and the NFL by 3. NBA's constants go to K 12 / home 60 /
  carry 0.5 (−0.0137 [−0.0235, −0.0035] held-out log loss; the 100-point
  home edge was the overconfidence). WNBA applies temperature 0.69 and the
  NFL 0.84 on the served log-odds (`core/calibrate.py`). MLB, NHL, soccer
  and tennis were already calibrated and are untouched. Measured, not
  built: gradient boosting, Glicko, K schedules, per-team home advantage,
  rest/weather/referee/QB features and market blends all lost or were noise.
- **Depth-weighted fair from the public order books** (NEW): `odds/polymarket`
  reads the CLOB `/book` for each matched two-way market and `odds/kalshi`
  reads `/markets/{ticker}/orderbook` for the home team's market (cached 90 s,
  five in flight, best-effort — a failed fetch leaves the quote without
  depth). Each exchange quote now carries `microprice_home` (the size-
  weighted touch), `home/away_depth_usd` and `spread_pts`; `core/fair.py`
  prices an exchange at its microprice, demotes a THIN book (touch under
  $250 or spread over 3 pts) below any deep exchange and flags it, and
  `core/lean.py` refuses a thin reference outright. This is the answer to
  the lead rule's largest-gap bucket returning nothing: a big gap usually
  meant the exchange was stale, not that DraftKings was wrong.
- **Parlay promos priced against the market fair** (NEW): `src/boost.ts`
  gains the exact formulas for a profit boost with a cap, a no-sweat token
  (refund in site credit at 70% of face), parlay insurance (refund only when
  exactly one leg loses), a bonus-bet stake, and the plain slip; a PROMO
  panel under the parlay verdict prices the current slip under each. Backtest
  (2026-09-02): plain and auto-generated slips lose 13–46%; a no-sweat SGP
  token is worth ≈ +46% of a $25 cap even at a 20% hold, a 33% profit boost
  on three −110 legs +11.6% (break-even 13/17/22% for 2/3/4 legs), insurance
  ≈ +13%. Promos are the only parlay arithmetic that comes out positive.
- **Soccer same-game joint table** (NEW): `core/sgp.py` solves each match's
  (λ_home, λ_away, ρ) exactly from the sharp fair 1X2 and a real over/under
  2.5 quote — two Poisson rates cannot hit 1X2 and O/U together, so the
  Dixon-Coles low-score parameter is the third unknown — with a global
  high-score diagonal δ = 0.30 fitted on 5,794 matches and validated on
  1,455. Draw & under 2.5 runs +49% above the independent product in the
  archive and the grid reproduces it; every result × O/U-2.5 pair sits
  inside the observed 95% interval per league and per favourite strength.
  `GameCard.sgp` carries the singles and every result|total joint; a
  SAME-GAME PRICER on the PARLAY page prices a pair, shows the independent
  product it corrects, and scores a typed SGP quote. Same-game legs stay
  out of the independent builder. Not an edge: books hold 15–25% on SGPs and
  already price this; it keeps the app's fair honest for SGP promos.
  `test_sgp.py` pins eight archive vectors to 3e-4.
- **Totals: validated, gated, and one placeholder removed** (FIX): the
  never-run totals validation ran on the live store (soccer n=108: model
  total MAE 1.28 vs the close's 1.31, P(over) log loss 0.688 vs 0.679,
  edges −2.8% [−26, +20]; pooled model residual z −1.3; where the model
  disagreed with the market by 10+ points the market was right both ways).
  The board had flagged an edge on 66% of games, so `core/totals.py` now
  requires the model to sit at least a sport-specific MIN_DISAGREEMENT from
  the line (NBA/WNBA/NCAAB 3, NFL 2, NCAAF 2.5, MLB 0.75, NHL 0.5, soccer
  0.35) before showing one, and the drawer leads with the market's devigged
  P(over). Every engine now emits NO total when either team lacks scoring
  history: WNBA had been shipping the placeholder 164.0 (2 × league average)
  and the board printed UNDER edges against a constant.
- **Intraday overreaction: tested, not tracked** (MEASUREMENT): on 451 games
  the negative autocorrelation of DraftKings' pre-game moves replicates
  (r −0.19 on non-zero changes) but half of it is exact quote undo in the
  relayed feed, the move-so-far reverses by the close at β ≈ −0.03, and every
  fade rule (6/3/1 h, 1–3 pt thresholds, exchange-unconfirmed subset) earns
  0.0–0.4% same-book CLV on n up to 281. Not worth a feature.
- **Boost evaluator** (NEW): `core/fair.py` puts a sharp fair probability on
  every card (`GameCard.fair`: Kalshi, then Polymarket, then a flagged
  sportsbook fallback; pre-game, full-market quotes only). The VALUE page
  scores an odds boost or a profit-boost token against it — effective price,
  EV per $1, edge in points, quarter-Kelly, EV at the promo's max stake, and
  a PLAY / MARGINAL / PASS verdict (PLAY needs +2%, because the reference is
  only good to about a point) — and TRACK logs it to MY BETS at the effective
  price so it grades like any bet. This is the one place the app can point
  at money: the model has no edge against a liquid line, but a boost is the
  book handing out a price above the sharp fair by construction.
- **Maker mode, measured before built** (MEASUREMENT): reconstructing resting
  Polymarket bids over 211 recorded picks, joining the bid fills 39% of orders
  (median 3h), earns the 0.5c half-spread and gives back 0.54c ± 0.20 of
  adverse post-fill drift — fills are the picks the market was leaving.
  Maker beats the current FAK path by +5.7 ± 1.6 pts of stake on filled
  orders (the avoided fee), and collapses on 2c books. It turns a −3.5% cost
  drag into ≈ −1% adverse selection with no model edge underneath: better,
  not profitable. Not built; the dry-run spec (queue position, fill time from
  the trades feed, +5/+30/+60 min drift) is in the session notes.

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
