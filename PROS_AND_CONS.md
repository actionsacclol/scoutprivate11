# Krypt Scout — Pros & Cons

## What's good (Pros)

**1. Actually free — no trial, no tier, no key wall.**
Nine sports, model fair lines, value board, pick tracking, CLV — all local, all free, MIT-licensed. The only optional key (The Odds API, free tier) adds tennis odds and extra books. OddsJam charges $200+/mo for its EV tooling.

**2. The honesty infrastructure is the product.**
Every prediction served is written to SQLite; the settle job grades it against the final score. Accuracy, log loss, ROI, and CLV per sport, computed from history the user can't edit and the developer can't cherry-pick. The About page states in plain text that the closing line beats every v1 model. No competitor selling picks can ship this feature and survive.

**3. Your record, not just the model's.**
TRACKER is a first-class personal bet log: one tap on TRACK, automatic grading at settlement, flat 1u staking, P&L over time. The distinction between "what the model said" (PERFORMANCE) and "what I actually took" (TRACKER) is the distinction most bettors never make.

**4. Free odds stack that line-shops itself.**
ESPN lines + Polymarket + Kalshi merge into one odds table with vig-removed probabilities, EV per dollar, and Kelly fractions. Prediction-market prices are near-vig-free reference points most tools don't show at all. Providers are independent and best-effort — one failing never blanks the board.

**5. Parlay truth-teller.**
The slip is priced two ways — the books' combined price and the model's fair price — with correlated same-game legs blocked outright. It is a feature designed to talk users out of bad bets, which is rare in this category for obvious commercial reasons.

**6. Nine sports behind one clean contract.**
`upcoming_games()` / `predict()` / `refresh_data()` per engine; the API and UI are sport-agnostic. Adding a sport is one Python file plus registry entry. Every engine backtests from the CLI and prints log loss against the devigged close — the benchmark is wired into the dev loop, not bolted on.

**7. Real desktop product, not a notebook.**
Electron supervisor with health-checked, crash-restarted PyInstaller backend; in-app scheduler for capture/settle/refresh; Discord auto-posting; onboarding; SIMPLE/PRO modes; verified NSIS installer pipeline. The packaging notes (onedir vs onefile, xgboost collect flags) show the sharp edges were actually filed down.

**8. Local-first, zero telemetry.**
No account, no cloud, no analytics. Data and models live under the app's userData dir (`KRYPT_SCOUT_USERDATA`); dev and packaged builds can run side-by-side via `KRYPT_SCOUT_TEST_PROFILE`.

**9. Clean data-source hygiene.**
Runtime-download-and-cache, never redistribute; tennis relicensed off CC BY-NC-SA (Sackmann) onto MIT (TennisMyLife) + tennis-data; studied projects documented with a license table distinguishing "may adapt" from "clean-room ideas only."

---

## What sucks (Cons)

**1. The models don't beat the closing line.**
Says so on the tin, but it's still the core limitation: calibrated Elo/Poisson baselines with two gradient-boosted upgrades. The "edges" on the value board are mostly model error plus soft lines, not exploitable alpha. A user who bets every edge at full Kelly will lose money. The app's honesty about this is a virtue; the underlying fact is not.

**2. Unsigned installer = SmartScreen wall.**
"Windows protected your PC" on every fresh install. For a gambling-adjacent tool the trust ask is even steeper than usual. Not fixable in code; needs a paid signing cert.

**3. Windows-only desktop.**
The backend is portable Python and runs anywhere in dev, but the packaged product is a Windows NSIS installer. No macOS/Linux builds, no mobile, no web version — and betting is a phone-first activity for most of the audience.

**4. Heavyweight for what it does.**
Electron shell + ~300 MB bundled pandas/sklearn/xgboost backend; a 224 MB installer for what is, to a casual user, an odds screen. Startup depends on a supervised child process with a health window.

**5. Odds coverage is thinner than paid aggregators.**
ESPN lines + two prediction markets is a great free stack, but OddsJam/Unabated scan dozens of books with push-speed updates. Line-shopping breadth and latency both lose. Tennis odds require the optional key at all.

**6. No bet-level sync with books.**
TRACKER is manual (one tap, but still manual) and flat-stakes. No bet-slip OCR, no book account sync, no custom stake sizing per bet. Serious trackers (Pikkit, etc.) do more here.

**7. Affiliate tension.**
The app is free partly because book/market links are referral links. The disclosure is loud (About page, BONUSES banner, README, DISCLAIMER) and the models can't see the links — but a bonuses page in a tool whose models say "don't bet most of this" is a tension a hostile reviewer will poke at.

**8. Young record.**
CLV tracking and graded history are only as convincing as the sample behind them, and the tool is brand new. The honesty features need months of public run time before they're proof rather than promise.

**9. Solo-dev bus factor.**
Nine data pipelines that break whenever ESPN/NHL/nflverse/football-data change a column, plus an Electron supervisor, plus senders, plus packaging. Free data sources rot; maintenance is unpaid and perpetual.

---

## vs. the competition

| | OddsJam / Unabated ($$$) | Paid trackers (Pikkit etc.) | Capper Discords ($) | Action Network (freemium) | **Krypt Scout (free, OSS)** |
|---|---|---|---|---|---|
| Price | $99–$300+/mo | Free–$10/mo | $30–$100+/mo | Free + sub | **Free, MIT** |
| Own model probabilities | No (market-derived) | No | "Trust me" | Basic projections | **Yes, 9 sports, open code** |
| EV / devig / Kelly math | Yes, best-in-class | No | No | Partial | **Yes, free** |
| Books covered | Dozens, fast | Sync from books | n/a | Many | **ESPN + Polymarket + Kalshi free; more via optional key** |
| Pick record you can audit | n/a | Yours only | Screenshots, deletions | Partial | **Model + user, auto-graded, local DB** |
| Admits its edge is unproven | n/a | n/a | Never | No | **Yes, in the app itself** |
| CLV tracking | Yes | Some | No | No | **Yes, free** |
| Parlay fair pricing | Partial | No | Opposite incentive | No | **Yes, with verdict** |
| Runs locally / no account | No (SaaS) | No (cloud) | No | No | **Yes, SQLite, no telemetry** |
| Open source | No | No | No | No | **Yes** |
| Platform | Web | Mobile | Discord | Web/mobile | **Windows desktop only** |
| Odds speed/breadth | Excellent | n/a | n/a | Good | **Modest** |

**Where Krypt Scout wins:** the free, local, auditable stack — model + market + EV math + auto-graded receipts in one place, open source, no subscription. For a stats-literate recreational bettor who wants the math and the honesty without $200/mo, nothing else does this.

**Where it loses:** odds breadth/latency (paid aggregators), bet syncing (dedicated trackers), platform reach (everyone — it's Windows-only), and proven alpha (nobody — including us; the closing line is still champion).

---

## Verdict

Krypt Scout is the rare betting tool whose most defensible feature is telling you not to trust it too much. The engineering is real — nine engines behind one contract, a supervised local backend, a verified installer pipeline, and grading/CLV infrastructure wired through the whole product rather than bolted on. The models are honest baselines, and the app is structured so that improving them (the v2 gradient-boosted roadmap) is measured against the only benchmark that matters, the closing line.

The structural risks are the usual Krypt-suite ones (unsigned installer, Windows-only, solo maintenance) plus one unique to the category: the product's integrity is its moat, so the affiliate surface must stay visibly subordinate to the honest math. Keep the record public, keep the disclosures loud, sign the installer, and let six months of auto-graded history do the marketing.
