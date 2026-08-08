# Krypt Scout — YouTube Description Template

Paste everything between the markers into the YouTube description field. Replace ALL `[BRACKETED]` placeholders before publishing. Keep the FTC disclosure ABOVE the fold (first 3 lines are visible before "...more").

---

<!-- BEGIN DESCRIPTION -->

Krypt Scout is a free, open-source sports prediction terminal for Windows. It builds its own win probabilities for nine sports, scans every book for mispriced lines, and — unlike every capper on this platform — grades its own picks automatically, wins AND losses, on a record you can audit.

DOWNLOAD (free): [GITHUB_RELEASES_URL]
SOURCE CODE: [GITHUB_REPO_URL]

DISCLOSURE: Some links in this description and the sportsbook links inside the app are referral/affiliate links. If you sign up through them, the sportsbook pays this project a commission at no cost to you. This is how a free app funds itself. Referral links never influence the model or the picks.

21+ only, where legal. Nothing in this video or app is betting advice. Gambling problem? Call or text 1-800-GAMBLER (US).

---

CHAPTERS
0:00 The record doesn't lie (cold open)
0:40 Install — GitHub download + the SmartScreen warning, explained
1:22 First launch + 3-step setup (books, SIMPLE vs PRO)
2:00 Dashboard + PICKS — TAKE, tiers, EV, and the line shop
3:50 TRACK a bet → the TRACKER (your P&L, auto-graded)
4:40 PARLAY — the truth-teller (what your slip is really worth)
5:30 The TERMINAL (nine sports, live slate field, the wire)
6:20 The honest numbers — backtests, and why Vegas still wins
7:10 PRO mode, the MODELS engine room, and automation
8:00 Auto-post picks to Discord (full setup)
9:30 The catch, said out loud — referrals, 21+, links

---

WHAT KRYPT SCOUT DOES
- Model win probabilities + fair lines for 9 sports: NBA, NFL, NCAAF, NCAAB, MLB, NHL, WNBA, soccer (11 leagues), tennis
- Daily PICKS with plain-English tiers (STRONG / LEAN / MODEL) and expected value per dollar
- Line shop on every pick — every book's price, best flagged, your books pinned
- One-tap bet tracking; every tracked bet auto-grades when the game settles (TRACKER P&L)
- The model's own record auto-grades too (PERFORMANCE: accuracy, log loss, CLV)
- Parlay builder that prices your slip against the model and shows the vig drag
- Free odds out of the box — ESPN lines + Polymarket + Kalshi prediction markets, no API key required (optional free Odds API key adds tennis odds + more books)
- Injuries + weather intel, line movement, quarter-Kelly sizing
- Auto-post daily picks to Discord webhooks on a schedule
- SIMPLE mode (plain English) or PRO mode (full terminal)
- No account, no ads, no telemetry — runs entirely on your PC

THE HONEST PART
Backtest accuracy (held-out seasons, shown inside the app): NCAAF 71.2% · NCAAB 70.9% · NBA 67.8% · NFL 67.5% — and the NFL closing line hits 68.4%, so the market still wins by a point. The weak models (NHL 53.6%, MLB 55.0%) are shown too. The app tracks its own gap to the closing line (CLV) because that gap, not the win percentage, is what actually matters. Anyone who hides that from you is selling something.

NOTE ON THE INSTALLER
The Windows installer is currently unsigned (code-signing certs cost money), so SmartScreen shows an "unrecognized app" warning on first run. Click "More info" → "Run anyway", or audit and build from source: [GITHUB_REPO_URL]

LINKS
Website: https://krypt.cc
Krypt suite (more free desktop tools): https://krypt.cc/tools
Discord: https://discord.gg/muzFKR657F
License: MIT — [GITHUB_REPO_URL]/blob/main/LICENSE

RESPONSIBLE GAMBLING
21+ (or the legal age in your jurisdiction). Sports betting is entertainment, not income — the closing line beats our models and probably beats you too. Never bet money you can't afford to lose. If it stops being fun, stop.
US: 1-800-GAMBLER · https://www.ncpgambling.org
Self-exclude tools are available at every regulated book.

#sportsbetting #opensource #sportsanalytics #bettingmodel #parlay

<!-- END DESCRIPTION -->

---

## Publishing checklist (not part of the description)

- [ ] Replace `[GITHUB_RELEASES_URL]` and `[GITHUB_REPO_URL]` (3 occurrences of the repo URL)
- [ ] Verify chapter timestamps against the final edit (YouTube needs `0:00` first and 3+ chapters)
- [ ] Confirm the model numbers still match the app's ABOUT table (`src/modelMeta.ts` is the source of truth — numbers change on retrains)
- [ ] Confirm the referral disclosure matches which links are actually affiliate links at publish time (`src/config/referrals.ts`)
- [ ] Mark the video's paid-promotion flag in YouTube Studio if any book referral appears on screen
- [ ] Age-restrict per platform gambling policy where required; add region blocks if any featured book demands them
- [ ] Pin a comment: download link + SmartScreen one-liner + 1-800-GAMBLER
