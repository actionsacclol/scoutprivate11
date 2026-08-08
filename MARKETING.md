# Krypt Scout — Marketing & Content Ideas

## Positioning

**"The Bloomberg terminal of sports betting — free."**

Not another picks service. Not another odds site with a paywall. One dark, dense desktop terminal that builds its own win probabilities for nine sports, compares them to live market prices, and — the wedge — **grades everything automatically**. The model's record and *your* record, settled by the scoreboard, with no delete button.

Secondary angle: **"Every pick graded automatically — receipts built in."** The differentiator is not the models (we say out loud that the closing line beats them). It's the honesty infrastructure: CLV tracking, auto-settled P&L, backtests against the close. Nobody selling picks can survive that feature, which is exactly why we ship it.

### Taglines

- The Bloomberg terminal of sports betting — free.
- Every pick graded automatically. Receipts built in.
- The only picks tool that admits the closing line beats it.
- Nine sports. Zero API keys. Zero subscription.
- Your capper deletes his losses. Scout can't.
- Free. No ads. No telemetry. No bullshit.

## Elevator pitch

Krypt Scout is a free Windows terminal that models nine sports (NBA, WNBA, NFL, NCAAF, NCAAB, MLB, NHL, soccer, tennis), converts its probabilities to fair lines, and scans live prices from ESPN, Polymarket, and Kalshi for positive-EV edges — no API key, no subscription. It recommends one pick per game, prices your parlays honestly (book price vs model fair price), and tracks every bet you take with automatic grading when games settle. Injuries, weather, line movement, CLV — all in one dark mission-control UI, all running locally on your machine.

## Feature bullets (for socials)

- Nine sports, one terminal: NBA · WNBA · NFL · NCAAF · NCAAB · MLB · NHL · soccer (11 leagues) · tennis
- Model fair lines vs live odds — ESPN, Polymarket, Kalshi, free, no API key
- Value board: vig-removed edges, EV per dollar, Kelly sizing
- One recommended pick per game, tiered STRONG/LEAN, line-shopped across books
- Parlay truth-teller: your slip priced by the books AND by the model, with the gap in your face
- TRACK any pick → auto-graded P&L when the game settles. Your record, with receipts
- The model grades itself too: accuracy, log loss, ROI, CLV per sport
- Auto-post picks to Discord
- Injuries, stadium weather, line movement built in
- 100% local: SQLite on your machine, zero telemetry, zero accounts
- Free and open source. MIT.

## Target audiences

**Primary:**
- Recreational bettors who follow capper Discords/Twitter and are tired of deleted losses and "60% lock" claims
- Stats-literate sports fans (the r/sportsbook / r/algobetting crowd) who want EV math without building it themselves
- Line shoppers who currently juggle five sportsbook apps and a spreadsheet
- Prediction-market users (Polymarket/Kalshi) who want model probabilities next to market prices

**Secondary:**
- Data-curious fans who won't bet but love the terminal aesthetic and model race
- Discord server admins who want an automated daily picks feed with a graded record
- Devs who want a clean multi-sport prediction codebase to fork (MIT)

**Not the audience:**
- Professional/syndicate bettors (they have better models and real infrastructure)
- Anyone looking for "locks" — the app will disappoint them on purpose
- Mac/Linux users (Windows-only for now; backend runs anywhere in dev)

## The honest-record differentiator

Lead every piece of content with the thing competitors cannot copy:

> Every capper shows you their wins. Krypt Scout shows you everything — every pick it makes gets recorded to a local database and graded automatically when the game ends. Accuracy, ROI, closing-line value, per sport, no edits, no deletions. It even tells you its own models still lose to the closing line. When was the last time a picks service told you that?

This framing works because it's true, verifiable in the code, and structurally impossible for pay-per-pick services to imitate.

## Content / reel ideas

1. **"I tracked a capper's picks vs a free open-source model for 30 days"** — Scout's auto-graded record vs a screenshot-archaeology capper record. The receipts angle.
2. **"This free app tells you your parlay is bad — with math"** — build an 8-leg lottery slip in PARLAY, show book price vs model fair price and the verdict line. Payoff: "the truth costs $0."
3. **"The value board"** — 30-second screen capture of the terminal scanning nine sports and surfacing the top EV edges of the day. Pure aesthetic + utility.
4. **"Line shopping is free money and nobody does it"** — same pick, five books, the spread between best and worst price, TRACK at the best one.
5. **"What is CLV and why your record is lying to you"** — educational explainer using the CLV page. Positions Scout as the adult in the room.
6. **"I auto-posted a model's picks to my Discord for a week"** — webhook setup in 60 seconds, the channel filling up daily, the graded record at the end.
7. **"Free vs $200/mo"** — feature table vs OddsJam/paid trackers. Same energy as every Krypt tool: "Same core features. $0."

## Screenshot shot-list

For the README, krypt.cc, and store/socials cards — dark theme, real data, PRO mode unless noted:

1. **TERMINAL** — the hero shot: ASCII marquee, live wire, value board, model rack. This is the screenshot people share.
2. **DASHBOARD** — top pick spotlight + today's slate + record sparkline (the "one screen" pitch).
3. **PICKS** — pick cards with STRONG/LEAN tiers and the TRACK BET button visible.
4. **TRACKER** — a settled bet log with green/red P&L column and stat tiles (the receipts shot).
5. **PARLAY** — a slip showing book price vs model fair price with the verdict line (the truth-teller shot).
6. **BOARDS detail drawer** — one game: model probabilities, fair line, per-book odds, injuries, weather.
7. **PERFORMANCE** — accuracy/log-loss/CLV table per sport (the honesty shot; include the "closing line still beats us" note).
8. **MODELS** — engine room with per-engine consoles (the "it's real software" shot for devs).
9. **DISCORD** — a picks message rendered in an actual Discord channel.
10. **Onboarding** — SIMPLE mode first-run for the "it's not intimidating" counter-shot.

## Distribution

- **Reddit:** r/algobetting (code + backtest honesty angle), r/sportsbook (tracker/CLV angle, follow sub rules on self-promo), r/dataisbeautiful (terminal screenshots), r/opensource, r/Python + r/electronjs (architecture posts)
- **Discord:** Krypt suite server (discord.gg/muzFKR657F) as home base; sports-analytics and betting-tools servers
- **GitHub:** topics `sports-betting`, `sports-analytics`, `elo`, `expected-value`, `prediction-markets`, `electron`, `fastapi`; TERMINAL GIF at the top of the README
- **X/Twitter:** daily value-board screenshot thread during football season; quote-tweet capper drama with "or use the free tool that grades itself"
- **YouTube:** "I built the Bloomberg terminal of sports betting" build story; CLV explainer; season-long public model race

## Caption template

```
[Hook — the honest angle, no "check this out"]
[What it does in 1–2 sentences]
[One concrete thing to try: the value board, a parlay verdict, TRACK a pick]
Free. Open source. Windows. Not betting advice — 21+.
Link in bio → krypt.cc/tools

#sportsbetting #sportsanalytics #expectedvalue #linemovement #CLV
#opensource #freetools #python #dataviz #predictionmarkets
```
