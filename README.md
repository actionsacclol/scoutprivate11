# Krypt Scout

> The Bloomberg terminal of sports betting — free.

Krypt Scout is a free sports-prediction terminal for Windows. It builds its own win probabilities for nine sports — NBA, WNBA, NFL, NCAAF, NCAAB, MLB, NHL, soccer (11 leagues), and tennis — converts them to fair lines, and stacks them against live prices from ESPN, Polymarket, and Kalshi with no API key required. You get graded picks, a parlay builder that tells you the truth about your slip, and a tracker that logs your own bets and grades them automatically when games settle. A Discord sender auto-posts the day's picks. Everything runs locally — your data is a SQLite file on your machine. Free. No ads. No telemetry. No bullshit.

Part of the **Krypt** free tools suite. See [krypt.cc/tools](https://krypt.cc/tools).

**This is not betting advice.** Read [DISCLAIMER.md](./DISCLAIMER.md) before you wager anything.

---

## Install

Grab your platform's build from the [Releases](../../releases) page. First
launch walks you through a 30-second onboarding (pick your books, SIMPLE or PRO
mode).

**Every build is unsigned** — code-signing certificates cost real money and we
don't have one yet. Each OS complains in its own dialect:

| Platform | File | First-run step |
|---|---|---|
| Windows x64 | `Krypt Scout-Setup-<version>.exe` | SmartScreen says "Windows protected your PC" → **More info → Run anyway** |
| macOS (Apple Silicon) | `krypt-scout-<version>-mac-arm64.dmg` | See the quarantine note below |
| Linux x64 | `krypt-scout-<version>-linux-x64.AppImage` or `.deb` | `chmod +x` the AppImage |

**Intel Macs are not supported.** There is no x64 build — the backend ships as
a PyInstaller bundle, which can't cross-compile, so an Intel build needs its own
Intel runner and those cost 10x on CI. The arm64 dmg will not run on an Intel
Mac; don't try it.

On macOS, Gatekeeper may refuse to open the app or call it "damaged". It isn't —
that's the message macOS shows for an unsigned, un-notarized app that arrived
with a quarantine flag. Clear it:

```bash
xattr -dr com.apple.quarantine "/Applications/Krypt Scout.app"
```

Don't want to run an unsigned binary? Fair. [Build it from source](#build-from-source) — same result, your compiler.

---

## 60-second tour

- **DASHBOARD** — the landing page: today's slate, top pick spotlight, your record, system status. One screen that answers "what should I look at right now?"
- **PICKS** — one recommended bet per game. A VALUE pick is a sportsbook price that sits at least a point under the exchange fair (Kalshi, else Polymarket) — STRONG when Kalshi set the fair, LEAN when Polymarket did; the model's own number is shown as context, never as the reason. When no book beats the fair, the pick is the displayed forecast's favourite, tier MODEL, no price. Measured 2026-09-02: the model's disagreement with the market carries no information; the exchange lead is the one signal that replicates (the book's close moves toward the pick by about 1.75 points, under the vig — a signal, not a profit).
- **TRACK** — the button on every pick card (and on a finished parlay slip). One tap logs the bet at your default stake and the best available price.
- **TRACKER** — two records side by side. **MY BETS**: your bets and parlays, graded automatically when games settle — *your* record with receipts. **BOT PICKS**: every +EV pick the bot has ever recommended at a flat 1u, pending and graded, so you can see the whole bot's P&L — not just the picks you tailed.
- **AUTO-BET** — connect a Polymarket wallet and let the bot place its own picks (dry-run first, hard caps, off by default).
- **TERMINAL** — the mission-control panel: live wire, value board, model rack, CLV readout. The screen you screenshot.

PRO mode adds the rest: TODAY, BOARDS, PARLAY, VALUE, PERFORMANCE, MODELS, INTEL, DISCORD, BONUSES.

---

## The models (honest numbers)

Backtest accuracy is the argmax hit rate on held-out seasons, replayed with strictly pre-game information (no leakage).

| Sport  | Model | Backtest | Approach |
|--------|-------|---------:|----------|
| NBA    | nba-elo-v1 | 67.8% | Elo · MOV + rolling scoring |
| WNBA   | wnba-elo-v1 | 66.3% | Elo · expansion-aware |
| Tennis | tennis-elo-v1 | 63.8% | Surface-blended Elo (ATP/WTA) |
| NFL    | nfl-xgb-v3 | 67.5% | Elo + QB + XGBoost blend |
| NCAAF  | ncaaf-kalman-v2 | 75.9% | Kalman margin filter · blowouts count |
| NCAAB  | ncaab-kalman-v2 | 72.0% | Kalman margin filter · D1 only |
| MLB    | mlb-elo-v3 | 55.5% | Elo + pitcher EWMA |
| NHL    | nhl-elo-v1 · nhl-xg-v2 with MoneyPuck | 53.6% · 57.5% | Elo + Poisson goals · xG/PDO logistic when the file is present |
| Soccer | soccer-poisson-v2 | 48.9% | Dixon-Coles-lite · 3-way · 11 leagues |

Now the part every other tool buries: **the closing line still beats us.** In backtests, the devigged closing line beats every v1 baseline above on log loss (NFL held-out example: our blend hits 67.5% accuracy, the closing line 68.4%). A model that can't approach closing-line log loss has no betting edge, whatever its raw accuracy — 71% in NCAAF mostly means college football has a lot of blowouts. Beating the close is the v2 target, and the built-in **CLV tracking measures the gap** on every captured pick instead of pretending it isn't there.

More house rules:

- Backtests replay history with strictly pre-game information.
- Edges shown are gross of limits/CLV drift; Kelly fractions are full Kelly — bet a quarter of that if you bet at all.
- Every prediction served is recorded to SQLite; the settle job fills in finals and grades the models over time. The record is automatic. There is no delete button for bad picks.

---

## Features

- **Nine sports, one contract** — every engine implements `upcoming_games()`, `predict()`, `refresh_data()`, so the API and UI are sport-agnostic.
- **Model fair lines vs live markets** — vig-removed market probability vs model probability, EV per dollar, Kelly stake fraction; the value board surfaces the top edges across all sports.
- **Free odds stack** — ESPN lines + Polymarket + Kalshi merge into one line-shopped odds table. No key needed. The Odds API is an optional add-on (adds tennis odds and more books).
- **Boost evaluator** — type what a book is offering (a boosted price, or a % profit-boost token on a normal line) and score it against the exchange fair price: effective price, EV per dollar, quarter-Kelly, PLAY / PASS. No model involved — the models can't beat a liquid line, but a boost is the book handing out a price above fair, and that is where recreational bettors actually get paid. One tap tracks it.
- **Parlay truth-teller** — stack as many moneyline legs as you dare; the slip is priced two ways (books' combined price vs the model's fair price) with an honest verdict about the gap. Correlated same-game legs are blocked. Fully-priced slips are trackable: one tap logs the parlay to your record, where it grades leg-by-leg (a lost leg loses the slip, pushed legs drop out of the payout).
- **Your tracked P&L** — TRACK any pick or parlay, it grades automatically at settlement at your default stake (set it once under SETTINGS). And the bot tracks itself: the TRACKER's BOT PICKS tab is its complete value-pick record at flat 1u — the same number the dashboard reports, with no delete button.
- **Model accountability built in** — accuracy, log loss, ROI, and CLV per sport on the PERFORMANCE page; per-engine consoles and retrains on MODELS.
- **Intel** — ESPN injury feeds (all five US leagues), Open-Meteo game weather for outdoor NFL/MLB venues, per-book line movement.
- **Auto-posting** — Discord webhooks send the day's picks on your schedule.
- **Auto-betting (Polymarket)** — the AUTO-BET panel: optionally tail the bot's value picks with real orders on the Polymarket CLOB. Fill-and-kill orders at an EV-floor limit price, flat per-pick stakes, one bet per game, daily caps. Ships **disabled + dry-run by default**; your wallet key is encrypted at rest and never leaves your machine except to sign orders. Placed bets grade into your tracker automatically.
- **All local** — supervised Python backend on localhost, SQLite store in your user directory, zero telemetry.
- **Krypt UI** — dark, dense, monospace. SIMPLE mode if you just want picks; PRO mode if you want the terminal.

---

## Build from source

One command runs the whole app in dev (Node 18+, Python 3.11+ on PATH):

```bash
npm install
npm run dev   # sets up backend/.venv + deps (predev), then Vite + Electron;
              # the Electron supervisor spawns uvicorn on a probed local port
```

Optional: extra odds via The Odds API (free key at the-odds-api.com) — `echo ODDS_API_KEY=yourkey > .env`.

The backend also runs standalone for API poking and backtests:

```bash
cd backend
.venv/Scripts/python -m uvicorn kryptscout.api.main:app --port 8000
```

First board request per sport triggers a data refresh (cached under `data/`). Each engine has a standalone backtest — `python -m kryptscout.sports.nfl` etc. prints holdout accuracy and log loss (NFL, soccer, and tennis also compare against devigged closing lines).

### Tracking jobs

```bash
python -m kryptscout.jobs.capture   # snapshot predictions + odds (run 2-4x/day)
python -m kryptscout.jobs.settle    # fill finals, print performance + CLV
```

Line movement and CLV need multiple capture runs per day. The desktop app schedules these for you; for headless setups, Task Scheduler / cron recipes are in `backend/kryptscout/jobs/__init__.py`.

### Packaging (installers)

Two stages: bundle the Python backend with PyInstaller, then build the
installer with electron-builder. The same two commands work on all three
platforms and produce that platform's format — NSIS on Windows, dmg/zip on
macOS, AppImage/deb on Linux.

**You can only build for the OS you're on.** PyInstaller cannot cross-compile,
so each OS *and architecture* has to freeze its own backend binary — that's why
CI runs four native runners (Windows x64, macOS arm64, macOS x64, Linux x64)
instead of one. See [RELEASING.md](./RELEASING.md).

```bash
# one-time: PyInstaller is a packaging-only dep, not in requirements.txt
backend/.venv/Scripts/python -m pip install pyinstaller

npm run py:bundle   # PyInstaller --onedir → backend/dist-py/krypt-scout-backend/
npm run dist        # vite build + electron-builder → release/Krypt Scout-Setup-<version>.exe
```

Notes:

- `py:bundle` builds **--onedir** (not onefile): onefile unpacks the ~300 MB pandas/sklearn/xgboost stack to a temp dir on every launch and can blow the supervisor's 60 s startup-health window; onedir starts in ~3 s. electron-builder ships the folder via `extraResources` and the supervisor launches `krypt-scout-backend.exe --port <port>` from there.
- The bundle needs `--collect-submodules kryptscout` (engines load via importlib) and `--collect-binaries/--collect-data xgboost`. Do **not** use `--collect-all xgboost` — it drags in `xgboost.testing`, which imports the uninstalled `hypothesis` package and fails the build.
- Packaged, the backend reads/writes `data/` and `models/` under `KRYPT_SCOUT_USERDATA` (set by the supervisor to the app's userData dir) — see `backend/kryptscout/config.py`. Without the env var, paths are the repo `data/`/`models/` as in dev.
- `KRYPT_SCOUT_TEST_PROFILE=<dir>` gives a packaged build an isolated userData dir (own settings/DB **and** single-instance lock), so it can run alongside a dev instance for smoke tests/CI.
- Silent install/uninstall for testing: `MSYS_NO_PATHCONV=1 "./Krypt Scout-Setup-x.y.z.exe" /S '/D=C:\path'` (quote the /D argument — unquoted backslashes get eaten by Git Bash and NSIS receives a mangled path) and `"Uninstall Krypt Scout.exe" /currentuser /S`.

---

## Data sources & credits

Krypt Scout downloads data at runtime, caches it locally, and never redistributes it. Credit where it's due:

| Source | Used for | License / terms |
|--------|----------|-----------------|
| ESPN public JSON APIs | Schedules, results, injuries, ESPN lines (all sports) | Unofficial public endpoints, no key; not affiliated |
| MLB Stats API (statsapi.mlb.com) | MLB schedules/results | Free public API, MLB Advanced Media terms |
| NHL API (api-web.nhle.com) | NHL schedules/results | Free public API |
| MoneyPuck.com (optional) | NHL expected goals per team-game — powers `nhl-xg-v2` when you drop `all_teams.csv` under `data/raw/nhl/moneypuck/` | Free for non-commercial use with credit; automated pulls need MoneyPuck's approval, so the app never fetches or ships it |
| nflverse (games.csv) | NFL history incl. closing lines for backtests | Open data, MIT-licensed tooling — credit nflverse |
| football-data.co.uk | Soccer results + closing odds (10 of 11 leagues) | Free for personal use; downloaded per-user, never shipped |
| TennisMyLife (TML) | ATP match history | MIT |
| tennis-data.co.uk | WTA results + tennis closing odds | Free (ad-funded), treated as personal-use; downloaded per-user |
| sportsdataverse (cfbfastR-data) | NCAAF/NCAAB schedule data | MIT |
| Polymarket | Prediction-market prices | Public market-data API |
| Kalshi | Prediction-market prices (CFTC-regulated) | Public market-data API |
| Open-Meteo | Game weather (outdoor NFL/MLB venues) | CC BY 4.0, free for non-commercial use |
| The Odds API (optional) | Extra books + tennis odds | Free tier with your own key |

---

## FAQ

**Is this betting advice?**
No. Krypt Scout is a statistics terminal. The models are honest baselines that lose to the closing line, and the app tells you so on its own About page. You must be 21+ (18+/19+ in some regions) to bet, and only where it's legal. If gambling stops being fun, stop — in the US, call or text 1-800-GAMBLER. Full text: [DISCLAIMER.md](./DISCLAIMER.md).

**Do I need API keys?**
No. Schedules, results, injuries, weather, and the ESPN/Polymarket/Kalshi odds stack all work with zero keys. An optional free key from the-odds-api.com adds tennis odds and more sportsbooks to the line-shop table.

**Where's my data?**
Local. Predictions, odds history, your tracked bets, and settings live in SQLite under your Windows user-data directory (`%APPDATA%/krypt-scout` when packaged; the repo `data/` folder in dev). Nothing is uploaded anywhere. No telemetry, no analytics, no account.

**Why does my antivirus / SmartScreen flag it?**
The installer is unsigned — Windows treats every unsigned exe from the internet as "unrecognized." That's the whole story. Verify it yourself: the code is all here, `npm run py:bundle && npm run dist` produces the same installer from source. A code-signing cert is on the roadmap.

**Will this make me money?**
Statistically? No. Read [the honest-numbers section](#the-models-honest-numbers) again. It will tell you what the models think, what the market thinks, and — unlike the guy selling picks — it will keep score on itself and on you.

---

## Affiliate disclosure

Sportsbook and market links in Krypt Scout are referral links — the project may earn a commission when you sign up through them. This never affects model output, odds display, or pick selection. The models don't know the links exist; the BONUSES page is the only surface built around them, and every other number in the app is computed the same whether you click them or not.

---

## Licensing of studied projects

We researched five open-source predictors before building. Summary of what we can legally reuse:

| Project | License | Our use |
|---------|---------|---------|
| saccofrancesco/deepshot (NBA) | MIT | May adapt code/approach |
| kochlisGit/ProphitBet (soccer) | MIT | May adapt code/approach |
| gmalbert/tennis-predictions | GPL-3.0 | Ideas only (copyleft) — reimplemented clean-room |
| gmalbert/hockey-predictions | none (all rights reserved) | Ideas only — reimplemented clean-room; its data sources (NHL API, ESPN) are public |
| kyleskom/NBA-ML-Sports-Betting | none (all rights reserved) | Ideas only |

---

## License

MIT. See [LICENSE](./LICENSE). Copyright 2026 Krypt.

---

**Krypt Scout** is part of the [Krypt](https://krypt.cc) suite of free desktop tools. Questions, bugs, feature requests: [Discord](https://discord.gg/muzFKR657F) or open an issue. Free. No ads. No telemetry. No bullshit.
