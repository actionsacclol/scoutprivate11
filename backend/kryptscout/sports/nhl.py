"""NHL engine — Elo + rest + shot share (v3), over an Elo core (v1).

v3 (PROPOSED 2026-11-14, audit in the models-nhl scratch directory): the
served probability is a 5-coefficient logistic on the team Elo's log-odds,
a back-to-back flag per side (from the schedule) and the home-minus-away
EWMA of shots-on-goal differential (from the gamecenter boxscores this
engine already caches). Free NHL API data only. Coefficients fitted on
regular seasons 2019-2025 (n=8,510) and shipped as constants; walk-forward
(fit on seasons before S, score S, 2021-2026, n=6,872, true results):
0.6588 vs the live Elo's 0.6664, −0.0076 [−0.0105, −0.0046]; vs a
recalibrated Elo −0.0068 [−0.0092, −0.0043]; vs the live xG model on
2021-2025 −0.0081 [−0.0116, −0.0045]. Against the market it is still
behind: 2026-27 games with a Kalshi quote 15+ minutes before the drop
(n=117) 0.6512 vs 0.6463, and it carries no information beyond the price
(coefficient −0.00, z 0.0). A better standalone number, not a betting edge.

Three defects fixed with it:
* SEASONS was the literal ("20242025", "20252026"), so from October 2026
  the engine ingested no 2026-27 game: every served number through
  2026-11-14 came from June 2026 state (158 of 158 stored rows reproduce
  from the frozen state file to 2e-16). Seasons are now derived from the
  date.
* The xG fit labelled every shootout game a home loss (MoneyPuck's
  goalsFor omits the shootout goal, so those rows are tied): training
  home-win rate 0.501 vs the true 0.539, even-strength home probability
  0.506. Tied rows are now dropped from the fit.
* The xG path served whatever the dropped file last contained, with no
  staleness check. Served from a season-old file it loses 0.0145
  [0.0103, 0.0189] to the plain live Elo from day 48 of a season on
  (2019-2025, n=6,236). It now needs a file no older than
  XG_MAX_STALE_DAYS and is OFF by default (XG_ENABLED): on true results
  with a warm Elo the live xG model is +0.0014 [−0.0011, +0.0039] against
  the Elo as shipped and −0.0011 [−0.0036, +0.0016] with the label fix,
  and adds −0.0001 on top of v3. The September −0.0076 was against this
  engine's cold-start Elo log (every team at 1500 in October 2024).

v2 (2026-09-02): when MoneyPuck's team game-by-game file is present under
RAW_DIR/nhl/moneypuck/all_teams.csv, each team carries EWMAs of per-game
expected goals for/against (all situations and 5-on-5), goals for/against,
and PDO components (finishing = goals − xG, goaltending = xGA − GA, both
half-speed and regressed), and a walk-forward logistic on the home-minus-
away differences serves the win probability through a 0.88 logit scale.
Measured on 13,511 regular-season games 2015-26, test 2024-26 (n=2,624):
log loss 0.6739 vs the Elo's 0.6798, −0.0059 [−0.0109, −0.0007], with
resolution 0.010 vs 0.003 — shot quality separates hockey games three
times better than goals do. Replayed through THIS code against the engine's
own live Elo log on the same 2,624 games: 0.6775 vs 0.6851, −0.0076
[−0.0123, −0.0026]. Everything else hockey-specific (OT-adjusted Elo,
Dixon-Coles, margin Kalman, rest, starter GSAx) measured null; dropping the
empty-net/OT situations from the xG changed nothing.

The file is NOT fetched by the app: MoneyPuck's terms are non-commercial
with credit and ask for approval before automated pulls, so the user drops
the listed file in (personal use) or the app fetches it once MoneyPuck's
written approval is in hand. Absent the file, everything below is the v1
Elo engine, unchanged. Credit MoneyPuck.com on the About page.

v1: Elo win probabilities + Poisson goal totals.

Historical results come from the free NHL API (api-web.nhle.com): one
club-schedule-season JSON per team per season, cached under RAW_DIR/nhl/.
Games are deduped (each appears in both clubs' files) and replayed in
chronological order through a shared EloBook (K=6, +50 home ice,
margin-of-victory scaling, 0.7 season carry). Totals use each team's rolling
last-20-game goals-for/against: predicted side score = (own GF + opp GA) / 2,
and P(over 6.5) comes from the Poisson survival function of the summed rate
(sum of independent Poissons is Poisson).

Starting goalies: every completed game's gamecenter boxscore is cached
forever under RAW_DIR/nhl/box/ (~2,800 files, one-time ~2 min backfill,
incremental afterwards) and feeds per-goalie save% EWMAs plus each team's
most recent starter — the MLB pitcher / NFL QB pattern. The resulting Elo
overlay is GATED OFF after failing walk-forward validation (it never beat
the k=0 baseline on the 2024-25 tune season or held-out 2025-26; see the
GOALIE_ADJ_ENABLED comment for the post-mortem). The ratings machinery
still runs and persists so variants can be re-tested cheaply.

Team keys are ESPN displayNames ("Boston Bruins") so predictions join
directly against datasources/espn scoreboard games.
"""
from __future__ import annotations

import asyncio
import csv
import json
import logging
import math
import time
from collections import defaultdict, deque
from dataclasses import dataclass, replace as dc_replace
from datetime import date, datetime, timezone
from pathlib import Path
from typing import Deque, Iterable

import httpx
import numpy as np

from kryptscout.config import DATA_DIR, RAW_DIR
from kryptscout.core.calibrate import temper
from kryptscout.core.elo import EloBook, EloConfig
from kryptscout.core.schemas import Game, Prediction, Sport
from kryptscout.datasources import espn
from kryptscout.sports.base import SportEngine

NHL_API = "https://api-web.nhle.com/v1/club-schedule-season/{abbrev}/{season}"
BOX_API = "https://api-web.nhle.com/v1/gamecenter/{game_id}/boxscore"
# The current season plus the two before it, derived from the date (see
# _seasons). This was the literal ("20242025", "20252026") until 2026-11:
# once 2025-26 became "past" in August nothing was ever fetched again, and
# the engine served June 2026 ratings through the first six weeks of 2026-27.
# Window length is not a tuning knob: 2-season vs full-history burn-in differ
# by −0.0003 [−0.0012, +0.0006] on 2024-2026 (n=2,936).
SEASON_WINDOW = 3
RAW_NHL_DIR = RAW_DIR / "nhl"
STATE_PATH = DATA_DIR / "nhl_state.json"
ROLLING_WINDOW = 20
TOTAL_LINE = 6.5
LEAGUE_AVG_GOALS = 3.0  # fallback per-side rate before a team has history
RAW_MAX_AGE_S = 24 * 3600  # current-season cache TTL

# home_advantage=25 reflects the current era's weak NHL home edge (~52% home
# win rate); param sweep on 2025-26 preferred it to 50 on log loss.
ELO_CONFIG = EloConfig(k=6.0, home_advantage=25.0, mov_scaling=True, season_carry=0.6)

# Starting-goalie adjustment (the MLB starting-pitcher pattern). Each goalie
# carries an EWMA of per-start save% (saves / shotsAgainst from the gamecenter
# boxscore of the goalie who played the most TOI), initialized at the league
# average so a debutant nets exactly 0. The home-minus-away difference converts
# to Elo points — clamp(GOALIE_K * (home_ewma - away_ewma), ±50) — and shifts
# the pre-game win probability only; team Elo updates never see it, so team
# ratings are identical to v1.
#
# GATED OFF (the honest-model rule). Coarse grid (K 100-3000, span 10-40)
# tuned on 2024-25, validated on 2025-26: NO grid point beat the k=0 baseline
# on either season, and log loss degrades monotonically in K (tune 0.6760
# baseline vs 0.6766 at the best point k=500/span=40; held-out 2025-26 0.6862
# vs 0.6863 there and 0.6890 at k=1500/span=20). Why it fails: per-start
# save% is a ~25-shot binomial (SD ~6%/start), so even a 20-40 start EWMA is
# variance-dominated — Hellebuyck's EWMA sat at .876 against a .907 aggregate
# after one weak stretch — and what stable signal exists is team-confounded
# shot quality that team Elo already prices, so the overlay adds correlated
# noise. The machinery (boxscore cache, EWMAs, per-team starters) stays live
# and persisted so future variants (e.g. goalie value relative to *team*
# save%, or confirmed-starter data) can be re-evaluated cheaply; flipping
# this flag re-enables it end to end and bumps model_name to nhl-elo-v2.
GOALIE_ADJ_ENABLED = False
GOALIE_K = 500.0  # tune-season best-in-grid; still loses to k=0 (see above)
GOALIE_SPAN = 40  # EWMA span in starts (alpha ~0.049)
GOALIE_ADJ_CAP = 50.0  # Elo points on the game-level goalie difference
LEAGUE_AVG_SAVE_PCT = 0.900  # NHL league save% has sat at ~.900 since 2023-24

# ------------------------------------------------------------ context (v3)
# P(home) = σ(w0 + w1·logit(elo) + w2·b2b_home + w3·b2b_away + w4·sog_diff)
#   elo       the team Elo's pre-game home probability (unchanged v1 replay)
#   b2b_*     1 when that side also played the previous calendar day. Raw
#             2018-2026: home wins 53.1% with neither side on a back-to-back
#             (n=7,487), 44.5% when only the home side is (n=508), 59.8% when
#             only the visitor is (n=1,548).
#   sog_diff  home-minus-away EWMA (span 25 games, 0.6 season carry) of
#             shots-on-goal differential per game, from the cached boxscores.
# Nothing here is trained at runtime: the five weights were fitted once
# (ridge logistic, λ=1) on regular seasons 2019-2025 and are refit by hand
# each off-season with the audit's s10_candidates.py. They were stable across
# the six walk-forward fits (elo 0.89-1.11, b2b_home −0.28..−0.46, b2b_away
# +0.16..+0.33, sog 0.034-0.042). No logit scale: held-out slope 0.94.
# Tried and NOT kept (all walk-forward, same games): goal-difference EWMA
# (+0.0000), goalie save% EWMA with the last starter (+0.0000) or the actual
# starter (−0.0000), MoneyPuck xG on top (−0.0001), an Elo knob retune (160
# grid points; the tune-window best, K8/carry .8, is +0.0007 [−0.0022,
# +0.0036] held out).
CTX_MODEL_NAME = "nhl-elo-ctx-v3"
CTX_ENABLED = True
CTX_WEIGHTS = (-0.00193, 0.89205, -0.33697, 0.33158, 0.04207)
CTX_SOG_SPAN = 25
CTX_SEASON_CARRY = 0.6
CTX_MIN_GAMES = 5  # both teams need this many shot-counted games

BOX_CONCURRENCY = 8  # simultaneous boxscore requests during backfill
BOX_RETRIES = 3
TUNE_SEASON = 20242025  # goalie knobs tuned here only
VALIDATE_SEASON = 20252026  # held out; the ship/gate decision season

# NHL API team abbreviation -> ESPN displayName, all 32 active franchises.
# (Utah Hockey Club became the Utah Mammoth in 2025; the NHL API kept "UTA".)
TEAM_NAMES: dict[str, str] = {
    "ANA": "Anaheim Ducks",
    "BOS": "Boston Bruins",
    "BUF": "Buffalo Sabres",
    "CGY": "Calgary Flames",
    "CAR": "Carolina Hurricanes",
    "CHI": "Chicago Blackhawks",
    "COL": "Colorado Avalanche",
    "CBJ": "Columbus Blue Jackets",
    "DAL": "Dallas Stars",
    "DET": "Detroit Red Wings",
    "EDM": "Edmonton Oilers",
    "FLA": "Florida Panthers",
    "LAK": "Los Angeles Kings",
    "MIN": "Minnesota Wild",
    "MTL": "Montreal Canadiens",
    "NSH": "Nashville Predators",
    "NJD": "New Jersey Devils",
    "NYI": "New York Islanders",
    "NYR": "New York Rangers",
    "OTT": "Ottawa Senators",
    "PHI": "Philadelphia Flyers",
    "PIT": "Pittsburgh Penguins",
    "SJS": "San Jose Sharks",
    "SEA": "Seattle Kraken",
    "STL": "St. Louis Blues",
    "TBL": "Tampa Bay Lightning",
    "TOR": "Toronto Maple Leafs",
    "UTA": "Utah Mammoth",
    "VAN": "Vancouver Canucks",
    "VGK": "Vegas Golden Knights",
    "WSH": "Washington Capitals",
    "WPG": "Winnipeg Jets",
}

# Relocated/renamed franchises keep one Elo identity: the Arizona Coyotes
# became Utah in 2024. Used for parsing only, never for fetching.
HISTORICAL_NAMES: dict[str, str] = {"ARI": "Utah Mammoth"}

RollingBook = dict[str, Deque[tuple[int, int]]]
GoalieBook = dict[str, dict]  # goalie id -> {"name", "ewma", "starts"}

# ---------------------------------------------------------------- xG (v2)
# MoneyPuck's listed team game-by-game file (careers/gameByGame/all_teams.csv):
# one row per team per game per situation. Dropped in by the user under the
# raw cache; see the module docstring for why the app does not fetch it.
MONEYPUCK_DIR = RAW_NHL_DIR / "moneypuck"
MONEYPUCK_TEAMS_FILE = "all_teams.csv"
XG_MODEL_NAME = "nhl-xg-v2"
ELO_MODEL_NAME = "nhl-elo-v2" if GOALIE_ADJ_ENABLED else "nhl-elo-v1"
XG_SPAN = 25  # EWMA span in games
XG_SEASON_CARRY = 0.6  # strength kept at a season boundary
XG_LEAGUE_XG = 2.8  # league-average xG per team-game, all situations
XG_LEAGUE_XG5 = 2.0  # at 5-on-5
XG_LEAGUE_GOALS = 2.8
XG_MIN_GAMES = 5  # both teams need this many games before the xG path prices them
XG_MIN_TRAIN_SEASON = 2015  # seasons the fit uses (the measured window)
XG_MIN_TRAIN_ROWS = 100
XG_RIDGE = 1.0
XG_LOGIT_SCALE = 0.88  # test-slope 0.88: the fit is slightly overconfident
XG_FEATURES = ("xg_diff", "xg5_diff", "g_diff", "sh_diff", "sv_diff")
# OFF: see the module docstring. With the label fix the live xG model is
# −0.0011 [−0.0036, +0.0016] against a warm Elo (2019-2025, n=8,428) and
# +0.0054 [+0.0023, +0.0085] against v3; it needs a licence and a hand-fed
# 126 MB file to stay fresh. The machinery stays for re-testing.
XG_ENABLED = False
XG_MAX_STALE_DAYS = 10  # in season, the file's last game must be this recent
# MoneyPuck used dotted codes for four franchises through 2020-21.
MONEYPUCK_ALIASES = {"L.A": "LAK", "N.J": "NJD", "S.J": "SJS", "T.B": "TBL"}


@dataclass(frozen=True)
class XgGame:
    """One regular-season game from the MoneyPuck team file, keyed by ESPN names."""

    id: int
    season: int
    date: str  # YYYY-MM-DD
    home: str
    away: str
    xg_home: float
    xg_away: float
    xg5_home: float
    xg5_away: float
    goals_home: int
    goals_away: int


def _moneypuck_team(code: str) -> str:
    code = MONEYPUCK_ALIASES.get(code, code)
    return TEAM_NAMES.get(code) or HISTORICAL_NAMES.get(code) or code


def load_moneypuck_games(path: Path) -> list[XgGame]:
    """Regular-season games with both sides' all-situation and 5-on-5 xG.

    A game needs its 'all' and '5on5' rows for both teams; anything partial
    (a mid-write file, an unfinished game) is skipped. Playoff games are
    excluded by the game id's type digits, like the Elo replay's gameType.
    """
    by_game: dict[int, dict] = {}
    with path.open(newline="", encoding="utf-8") as fh:
        for row in csv.DictReader(fh):
            situation = row.get("situation")
            if situation not in ("all", "5on5"):
                continue
            try:
                gid = int(row["gameId"])
                if (gid // 10000) % 100 != 2:
                    continue  # 02 = regular season
                side = "home" if row["home_or_away"] == "HOME" else "away"
                entry = by_game.setdefault(gid, {"season": int(row["season"]), "date": str(row["gameDate"])})
                entry[side] = _moneypuck_team(row["team"])
                if situation == "all":
                    entry[f"xg_{side}"] = float(row["xGoalsFor"])
                    entry[f"goals_{side}"] = int(float(row["goalsFor"]))
                else:
                    entry[f"xg5_{side}"] = float(row["xGoalsFor"])
            except (KeyError, TypeError, ValueError):
                continue
    games: list[XgGame] = []
    need = ("home", "away", "xg_home", "xg_away", "xg5_home", "xg5_away", "goals_home", "goals_away")
    for gid, e in by_game.items():
        if not all(k in e for k in need):
            continue
        d = e["date"]
        date = f"{d[:4]}-{d[4:6]}-{d[6:8]}" if len(d) == 8 else d
        games.append(XgGame(
            id=gid, season=e["season"], date=date, home=e["home"], away=e["away"],
            xg_home=e["xg_home"], xg_away=e["xg_away"], xg5_home=e["xg5_home"], xg5_away=e["xg5_away"],
            goals_home=e["goals_home"], goals_away=e["goals_away"],
        ))
    games.sort(key=lambda g: (g.date, g.id))
    return games


def _fresh_xg_team() -> dict:
    return {"xgf": 0.0, "xga": 0.0, "xgf5": 0.0, "xga5": 0.0, "gf": 0.0, "ga": 0.0, "sh": 0.0, "sv": 0.0, "n": 0}


def xg_features(state: dict[str, dict], home: str, away: str) -> dict | None:
    """Home-minus-away strength differences, or None until both have history."""
    h, a = state.get(home), state.get(away)
    if not h or not a or h["n"] < XG_MIN_GAMES or a["n"] < XG_MIN_GAMES:
        return None
    return {
        "xg_diff": (h["xgf"] - h["xga"]) - (a["xgf"] - a["xga"]),
        "xg5_diff": (h["xgf5"] - h["xga5"]) - (a["xgf5"] - a["xga5"]),
        "g_diff": (h["gf"] - h["ga"]) - (a["gf"] - a["ga"]),
        "sh_diff": h["sh"] - a["sh"],
        "sv_diff": h["sv"] - a["sv"],
    }


def xg_replay(games: list[XgGame]) -> tuple[dict[str, dict], list[dict]]:
    """Run the games through the EWMAs in order; return (state, feature rows).

    Each row carries the PRE-game features plus season/date/teams/outcome
    and n_min (the thinner side's game count), so the fit and the backtest
    both read strictly pre-game information.
    """
    alpha = 2.0 / (XG_SPAN + 1)
    state: dict[str, dict] = {}
    rows: list[dict] = []
    season: int | None = None
    for g in games:
        if season is not None and g.season != season:
            for team in state.values():
                for key in ("xgf", "xga", "xgf5", "xga5", "gf", "ga", "sh", "sv"):
                    team[key] *= XG_SEASON_CARRY
        season = g.season
        h = state.setdefault(g.home, _fresh_xg_team())
        a = state.setdefault(g.away, _fresh_xg_team())
        rows.append({
            "season": g.season, "date": g.date, "home": g.home, "away": g.away,
            "home_won": g.goals_home > g.goals_away, "n_min": min(h["n"], a["n"]),
            # MoneyPuck's goalsFor omits the shootout goal: a tied row is a
            # shootout game whose winner this file does not say.
            "tied": g.goals_home == g.goals_away,
            "xg_diff": (h["xgf"] - h["xga"]) - (a["xgf"] - a["xga"]),
            "xg5_diff": (h["xgf5"] - h["xga5"]) - (a["xgf5"] - a["xga5"]),
            "g_diff": (h["gf"] - h["ga"]) - (a["gf"] - a["ga"]),
            "sh_diff": h["sh"] - a["sh"], "sv_diff": h["sv"] - a["sv"],
        })
        for team, xgf, xga, xgf5, xga5, gf, ga in (
            (h, g.xg_home, g.xg_away, g.xg5_home, g.xg5_away, g.goals_home, g.goals_away),
            (a, g.xg_away, g.xg_home, g.xg5_away, g.xg5_home, g.goals_away, g.goals_home),
        ):
            team["xgf"] += alpha * (xgf - XG_LEAGUE_XG - team["xgf"])
            team["xga"] += alpha * (xga - XG_LEAGUE_XG - team["xga"])
            team["xgf5"] += alpha * (xgf5 - XG_LEAGUE_XG5 - team["xgf5"])
            team["xga5"] += alpha * (xga5 - XG_LEAGUE_XG5 - team["xga5"])
            team["gf"] += alpha * (gf - XG_LEAGUE_GOALS - team["gf"])
            team["ga"] += alpha * (ga - XG_LEAGUE_GOALS - team["ga"])
            # PDO components at half speed: finishing and goaltending are the
            # noisiest parts of a result and regress hardest.
            team["sh"] += 0.5 * alpha * ((gf - xgf) - team["sh"])
            team["sv"] += 0.5 * alpha * ((xga - ga) - team["sv"])
            team["n"] += 1
    return state, rows


def fit_xg_logit(rows: list[dict], upto_season: int) -> list[float] | None:
    """Ridge logistic on seasons strictly before `upto_season`; None when thin.

    Shootout games (tied in the file) are left out: counting them as home
    losses, as this did until 2026-11, put the training home-win rate at
    0.501 against a true 0.539 and the fitted even-strength home probability
    at 0.506 (0.548 without them).
    """
    train = [
        r for r in rows
        if XG_MIN_TRAIN_SEASON <= r["season"] < upto_season and r["n_min"] >= XG_MIN_GAMES and not r.get("tied")
    ]
    if len(train) < XG_MIN_TRAIN_ROWS:
        return None
    X = np.column_stack([np.ones(len(train))] + [np.array([r[f] for r in train], float) for f in XG_FEATURES])
    y = np.array([1.0 if r["home_won"] else 0.0 for r in train])
    w = np.zeros(X.shape[1])
    for _ in range(60):
        z = X @ w
        p = 1.0 / (1.0 + np.exp(-z))
        grad = X.T @ (p - y) + XG_RIDGE * np.r_[0.0, w[1:]]
        hess = (X * (p * (1.0 - p))[:, None]).T @ X + XG_RIDGE * np.eye(len(w))
        hess[0, 0] -= XG_RIDGE
        step = np.linalg.solve(hess, grad)
        w -= step
        if np.max(np.abs(step)) < 1e-9:
            break
    if not np.all(np.isfinite(w)):
        return None
    return [float(v) for v in w]


def xg_prob(weights: list[float], feats: dict) -> float:
    """Raw (unscaled) logistic probability from the fitted weights."""
    z = weights[0] + sum(w * feats[f] for w, f in zip(weights[1:], XG_FEATURES))
    return 1.0 / (1.0 + math.exp(-z))


@dataclass(frozen=True)
class GoalieStart:
    """The goalie who carried one side of one game (most TOI in the boxscore)."""

    id: int
    name: str | None
    saves: int
    shots: int  # shotsAgainst; 0 means no rateable save% for this start


@dataclass(frozen=True)
class GameRecord:
    """One completed NHL game, deduped and keyed by ESPN team names."""

    id: int
    season: int
    game_type: int  # 2 = regular season, 3 = playoffs
    date: str  # YYYY-MM-DD
    home: str
    away: str
    home_score: int
    away_score: int
    home_goalie: GoalieStart | None = None  # None until a boxscore is cached
    away_goalie: GoalieStart | None = None
    home_sog: int | None = None  # shots on goal, from the same boxscore
    away_sog: int | None = None

    @property
    def home_won(self) -> bool:
        return self.home_score > self.away_score  # no ties post-OT/SO


def _current_season_end_year(today: date | None = None) -> int:
    """NHL seasons run Oct-Jun; from August we consider the next season current."""
    today = today or date.today()
    return today.year + 1 if today.month >= 8 else today.year


def _seasons(today: date | None = None) -> tuple[str, ...]:
    """The current season and the SEASON_WINDOW-1 before it, oldest first."""
    end = _current_season_end_year(today)
    return tuple(f"{y - 1}{y}" for y in range(end - SEASON_WINDOW + 1, end + 1))


# -- context model (v3) ---------------------------------------------------------


def _fresh_shots() -> dict:
    return {"sog": 0.0, "n": 0}


def _is_back_to_back(prev_date: str | None, game_date: str) -> bool:
    """True when `prev_date` is the calendar day before `game_date` (YYYY-MM-DD)."""
    if not prev_date:
        return False
    try:
        return (date.fromisoformat(game_date[:10]) - date.fromisoformat(prev_date[:10])).days == 1
    except ValueError:
        return False


def ctx_prob(elo_prob: float, b2b_home: bool, b2b_away: bool, sog_diff: float) -> float:
    """The v3 probability from the Elo's own number plus rest and shot share."""
    w0, w1, w2, w3, w4 = CTX_WEIGHTS
    q = min(1.0 - 1e-6, max(1e-6, elo_prob))
    z = w0 + w1 * math.log(q / (1.0 - q)) + w2 * float(b2b_home) + w3 * float(b2b_away) + w4 * sog_diff
    return 1.0 / (1.0 + math.exp(-z))


def _ctx_ready(shots: dict[str, dict], home: str, away: str) -> bool:
    h, a = shots.get(home), shots.get(away)
    return bool(h and a and h["n"] >= CTX_MIN_GAMES and a["n"] >= CTX_MIN_GAMES)


def schedule_dates(payloads: Iterable[dict]) -> dict[str, set[str]]:
    """Every regular-season/playoff date on each team's schedule, played or not.

    Read from the same club-schedule JSONs as the results. A back-to-back is
    a fact of the schedule, so an upcoming game is flagged from this even
    when last night's result has not been ingested yet (the current-season
    files refresh once a day).
    """
    dates: dict[str, set[str]] = defaultdict(set)
    for payload in payloads:
        for raw in payload.get("games", []):
            if raw.get("gameType") not in (2, 3):
                continue
            day = str(raw.get("gameDate", ""))[:10]
            for side in ("homeTeam", "awayTeam"):
                ab = (raw.get(side) or {}).get("abbrev", "")
                team = TEAM_NAMES.get(ab) or HISTORICAL_NAMES.get(ab)
                if team and day:
                    dates[team].add(day)
    return dates


def _is_past_season(season: str, today: date | None = None) -> bool:
    return int(season[4:]) < _current_season_end_year(today)


def _poisson_over_prob(lam: float, line: float = TOTAL_LINE) -> float:
    """P(Poisson(lam) > line) via the survival function (scipy-free)."""
    if lam <= 0:
        return 0.0
    cdf = sum(math.exp(-lam) * lam**k / math.factorial(k) for k in range(int(line) + 1))
    return min(max(1.0 - cdf, 0.0), 1.0)


def games_from_payloads(payloads: Iterable[dict]) -> list[GameRecord]:
    """Parse raw club-schedule JSONs into deduped, chronologically sorted records.

    Every game appears in both clubs' schedules, so we dedupe on the league-wide
    game id. Preseason (gameType 1) and games against non-NHL sides (abbrev not
    in TEAM_NAMES, e.g. exhibition opponents) are dropped.
    """
    seen: dict[int, GameRecord] = {}
    for payload in payloads:
        for raw in payload.get("games", []):
            if raw.get("gameType") not in (2, 3):
                continue
            if raw.get("gameState") not in ("FINAL", "OFF"):
                continue
            home_ab = raw.get("homeTeam", {}).get("abbrev", "")
            away_ab = raw.get("awayTeam", {}).get("abbrev", "")
            home = TEAM_NAMES.get(home_ab) or HISTORICAL_NAMES.get(home_ab)
            away = TEAM_NAMES.get(away_ab) or HISTORICAL_NAMES.get(away_ab)
            hs = raw.get("homeTeam", {}).get("score")
            as_ = raw.get("awayTeam", {}).get("score")
            if not home or not away or hs is None or as_ is None:
                continue
            gid = int(raw["id"])
            if gid in seen:
                continue
            seen[gid] = GameRecord(
                id=gid,
                season=int(raw["season"]),
                game_type=int(raw["gameType"]),
                date=str(raw.get("gameDate", "")),
                home=home,
                away=away,
                home_score=int(hs),
                away_score=int(as_),
            )
    return sorted(seen.values(), key=lambda r: (r.season, r.date, r.id))


# -- starting goalies -----------------------------------------------------------


def _toi_seconds(toi) -> int:
    """'59:05' (or OT '64:35') -> seconds; 0 for anything unparseable."""
    try:
        minutes, seconds = str(toi).split(":")
        return int(minutes) * 60 + int(seconds)
    except (ValueError, AttributeError):
        return 0


def starting_goalie(boxscore: dict, side: str) -> GoalieStart | None:
    """One side's rateable goalie from a gamecenter boxscore payload.

    side is "homeTeam" or "awayTeam". The payload shape (verified live) is
    playerByGameStats.<side>.goalies[] with toi ("MM:SS"), saves,
    shotsAgainst, and a starter flag. We take the goalie with the most TOI —
    the one whose performance actually decided the game — with the starter
    flag as tiebreak; None when the list is empty or malformed.
    """
    goalies = (boxscore.get("playerByGameStats") or {}).get(side, {}).get("goalies") or []
    candidates = [g for g in goalies if g.get("playerId") is not None]
    if not candidates:
        return None
    top = max(candidates, key=lambda g: (_toi_seconds(g.get("toi")), bool(g.get("starter"))))
    return GoalieStart(
        id=int(top["playerId"]),
        name=(top.get("name") or {}).get("default"),
        saves=int(top.get("saves") or 0),
        shots=int(top.get("shotsAgainst") or 0),
    )


def attach_goalies(records: list[GameRecord], box_dir: Path) -> list[GameRecord]:
    """Fold cached boxscore starters into records; missing/bad caches stay None."""
    out: list[GameRecord] = []
    for rec in records:
        path = box_dir / f"{rec.id}.json"
        if not path.exists():
            out.append(rec)
            continue
        try:
            box = json.loads(path.read_text(encoding="utf-8"))
        except (json.JSONDecodeError, OSError):
            out.append(rec)
            continue
        out.append(
            dc_replace(
                rec,
                home_goalie=starting_goalie(box, "homeTeam"),
                away_goalie=starting_goalie(box, "awayTeam"),
                home_sog=_team_sog(box, "homeTeam"),
                away_sog=_team_sog(box, "awayTeam"),
            )
        )
    return out


def _team_sog(boxscore: dict, side: str) -> int | None:
    """One side's shots on goal from a gamecenter boxscore; None when absent."""
    value = (boxscore.get(side) or {}).get("sog")
    try:
        return int(value) if value is not None else None
    except (TypeError, ValueError):
        return None


def _goalie_value(goalies: GoalieBook, goalie_id) -> float:
    """Save%-above-league-average of one goalie; 0 when unknown or unseen.

    The EWMA starts at LEAGUE_AVG_SAVE_PCT, so a debutant also nets 0 — the
    value only grows as real starts pull the EWMA off the prior. A cold
    backup's ~0 against an established starter's earned value is the signal.
    """
    if goalie_id is None:
        return 0.0
    entry = goalies.get(str(goalie_id))
    if not entry:
        return 0.0
    return entry["ewma"] - LEAGUE_AVG_SAVE_PCT


def _goalie_diff(
    goalies: GoalieBook, home_goalie_id, away_goalie_id, k_goalie: float = GOALIE_K
) -> float:
    """Elo-point pre-game adjustment for the goalie matchup, clamped to ±cap."""
    adj = k_goalie * (
        _goalie_value(goalies, home_goalie_id) - _goalie_value(goalies, away_goalie_id)
    )
    return max(-GOALIE_ADJ_CAP, min(GOALIE_ADJ_CAP, adj))


def _record_goalie_start(
    goalies: GoalieBook, start: GoalieStart | None, span: int = GOALIE_SPAN
) -> None:
    """Fold one start's save% into the goalie's EWMA.

    Initializing at LEAGUE_AVG_SAVE_PCT is the shrinkage: with
    alpha=2/(span+1), one shutout moves a rookie only ~10% off the prior.
    Starts with zero shots against carry no save% and are skipped.
    """
    if start is None or start.shots <= 0:
        return
    entry = goalies.setdefault(
        str(start.id), {"name": start.name, "ewma": LEAGUE_AVG_SAVE_PCT, "starts": 0}
    )
    alpha = 2.0 / (span + 1.0)
    entry["ewma"] += alpha * (start.saves / start.shots - entry["ewma"])
    entry["starts"] += 1
    if start.name:
        entry["name"] = start.name


def _expected_home_prob(
    book: EloBook, home: str, away: str, adjustment: float = 0.0
) -> float:
    """P(home wins) with the goalie Elo-point difference folded in."""
    diff = book.get(home) + book.config.home_advantage - book.get(away) + adjustment
    return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))


def replay(
    records: list[GameRecord],
    book: EloBook,
    rolling: RollingBook,
    last_season: int | None = None,
    eval_season: int | None = None,
    goalies: GoalieBook | None = None,
    team_goalie: dict[str, str] | None = None,
    k_goalie: float | None = None,
    goalie_span: int = GOALIE_SPAN,
    log: list | None = None,
    shots: dict[str, dict] | None = None,
    last_played: dict[str, str] | None = None,
    use_ctx: bool | None = None,
) -> tuple[int | None, list[tuple[float, float, GameRecord]]]:
    """Run records through Elo + rolling goals in order, mutating book/rolling.

    Calls book.new_season() at every season boundary. Returns the last season
    processed and, when eval_season is given, the pre-update (home_win_prob,
    predicted_total) alongside each of that season's games for backtesting.

    goalies/team_goalie (mutated in place like book/rolling) accumulate the
    per-goalie save% EWMAs and each team's most recent starter. Walk-forward
    honest: the eval probability folds in the goalie diff computed from
    strictly *prior* starts — this game's save% enters the EWMA only after
    the game is scored — and team Elo updates never see the goalie overlay,
    so team ratings are identical to a goalie-less replay.

    shots/last_played (mutated in place too) carry the v3 context: each
    team's EWMA of shots-on-goal differential and the date of its last game.
    With use_ctx (default CTX_ENABLED) the logged/evaluated probability is
    ctx_prob() over the Elo number once both teams have CTX_MIN_GAMES
    shot-counted games, and the plain Elo number before that. Team Elo
    updates never see it.
    """
    if goalies is None:
        goalies = {}
    if team_goalie is None:
        team_goalie = {}
    if k_goalie is None:
        k_goalie = GOALIE_K if GOALIE_ADJ_ENABLED else 0.0
    if shots is None:
        shots = {}
    if last_played is None:
        last_played = {}
    if use_ctx is None:
        use_ctx = CTX_ENABLED
    sog_alpha = 2.0 / (CTX_SOG_SPAN + 1.0)
    evals: list[tuple[float, float, GameRecord]] = []
    current = last_season
    for r in records:
        if current is not None and r.season > current:
            book.new_season()
            for entry in shots.values():
                entry["sog"] *= CTX_SEASON_CARRY
            last_played.clear()  # the summer is not a back-to-back
        current = r.season
        if log is not None or (eval_season is not None and r.season == eval_season):
            gdiff = _goalie_diff(
                goalies,
                r.home_goalie.id if r.home_goalie else None,
                r.away_goalie.id if r.away_goalie else None,
                k_goalie,
            )
            prob = _expected_home_prob(book, r.home, r.away, gdiff)
            if use_ctx and _ctx_ready(shots, r.home, r.away):
                prob = ctx_prob(
                    prob,
                    _is_back_to_back(last_played.get(r.home), r.date),
                    _is_back_to_back(last_played.get(r.away), r.date),
                    shots[r.home]["sog"] - shots[r.away]["sog"],
                )
            if log is not None:
                # 5-field walk-forward row — pre-update prob, team names for
                # the dollar-ROI price join (store/roibacktest.py).
                log.append([r.date, round(prob, 6), int(r.home_won), r.home, r.away])
            if eval_season is not None and r.season == eval_season:
                gh, ga = _predicted_goals(rolling, r.home, r.away)
                evals.append((prob, gh + ga, r))
        book.update(
            r.home,
            r.away,
            home_won=r.home_won,
            margin=abs(r.home_score - r.away_score),
        )
        rolling[r.home].append((r.home_score, r.away_score))
        rolling[r.away].append((r.away_score, r.home_score))
        for team, start in ((r.home, r.home_goalie), (r.away, r.away_goalie)):
            _record_goalie_start(goalies, start, goalie_span)
            if start is not None:
                team_goalie[team] = str(start.id)
        if r.home_sog is not None and r.away_sog is not None:
            margin = float(r.home_sog - r.away_sog)
            for team, value in ((r.home, margin), (r.away, -margin)):
                entry = shots.setdefault(team, _fresh_shots())
                entry["sog"] += sog_alpha * (value - entry["sog"])
                entry["n"] += 1
        last_played[r.home] = r.date
        last_played[r.away] = r.date
    return current, evals


def _team_rates(rolling: RollingBook, team: str) -> tuple[float, float]:
    """Rolling last-20 (goals-for, goals-against) per game, league avg fallback."""
    window = rolling.get(team)
    if not window:
        return LEAGUE_AVG_GOALS, LEAGUE_AVG_GOALS
    n = len(window)
    return sum(gf for gf, _ in window) / n, sum(ga for _, ga in window) / n


def _predicted_goals(rolling: RollingBook, home: str, away: str) -> tuple[float, float]:
    home_gf, home_ga = _team_rates(rolling, home)
    away_gf, away_ga = _team_rates(rolling, away)
    return (home_gf + away_ga) / 2.0, (away_gf + home_ga) / 2.0


def _new_rolling() -> RollingBook:
    return defaultdict(lambda: deque(maxlen=ROLLING_WINDOW))


def _score_evals(evals: list[tuple[float, float, GameRecord]]) -> dict[str, float]:
    """Accuracy / log loss / total MAE / goalie coverage over replay evals."""
    if not evals:
        return {"games": 0}
    eps = 1e-12
    correct = 0
    log_loss = 0.0
    total_err = 0.0
    covered = 0
    for prob, pred_total, rec in evals:
        y = 1.0 if rec.home_won else 0.0
        correct += int((prob >= 0.5) == rec.home_won)
        log_loss -= y * math.log(max(prob, eps)) + (1 - y) * math.log(
            max(1 - prob, eps)
        )
        total_err += abs(pred_total - (rec.home_score + rec.away_score))
        covered += int(rec.home_goalie is not None and rec.away_goalie is not None)
    n = len(evals)
    return {
        "games": n,
        "accuracy": correct / n,
        "log_loss": log_loss / n,
        "total_mae": total_err / n,
        "goalie_coverage": covered / n,
    }


class NhlEngine(SportEngine):
    sport = Sport.NHL

    @property
    def model_name(self) -> str:  # type: ignore[override]
        if self._xg_serving():
            return XG_MODEL_NAME
        return CTX_MODEL_NAME if CTX_ENABLED else ELO_MODEL_NAME

    def _xg_serving(self) -> bool:
        """The xG path prices games only when enabled AND its file is current.

        Current = the file's last game is within XG_MAX_STALE_DAYS of the
        newest result this engine has replayed from the NHL API. A file left
        over from last season fails that from the second week of October.
        """
        if not (XG_ENABLED and self._xg_weights and self._xg_last_date):
            return False
        if not self._latest_result:
            return True
        try:
            gap = (date.fromisoformat(self._latest_result) - date.fromisoformat(self._xg_last_date)).days
        except ValueError:
            return False
        return gap <= XG_MAX_STALE_DAYS

    def __init__(self, state_path: Path = STATE_PATH, raw_dir: Path = RAW_NHL_DIR):
        self._state_path = state_path
        self._raw_dir = raw_dir
        self._box_dir = raw_dir / "box"
        # Context path (v3): shots-on-goal EWMAs, last game date per team,
        # and every scheduled date per team (for upcoming back-to-backs).
        self._shots: dict[str, dict] = {}
        self._last_played: dict[str, str] = {}
        self._team_dates: dict[str, set[str]] = {}
        self._latest_result: str | None = None  # newest replayed game date
        self._xg_last_date: str | None = None  # newest game in the MoneyPuck file
        # xG path (v2): team EWMAs + fitted weights, present only when the
        # MoneyPuck file has been dropped in. See _refresh_xg.
        self._xg_state: dict[str, dict] = {}
        self._xg_weights: list[float] | None = None
        self._xg_season: int | None = None
        self._xg_mtime: float | None = None
        self._xg_metrics: dict = {}
        self._book = EloBook(config=ELO_CONFIG)
        self._rolling: RollingBook = _new_rolling()
        self._goalies: GoalieBook = {}
        self._team_goalie: dict[str, str] = {}  # team -> most recent starter id
        self._processed: set[int] = set()
        self._last_season: int | None = None
        # Walk-forward per-game log [date, prob, outcome, home, away] —
        # persisted for the backtest panel + the dollar-ROI price join.
        self._backtest_log: list = []
        self._loaded = False

    # ------------------------------------------------------------------ data

    async def refresh_data(self) -> None:
        """Fetch/refresh raw schedules + boxscores, replay unseen games, persist."""
        self._ensure_loaded()
        await self._fetch_raw()
        payloads = self._load_payloads()
        self._team_dates = schedule_dates(payloads)
        records = games_from_payloads(payloads)
        await self._fetch_boxscores([r.id for r in records])
        records = attach_goalies(records, self._box_dir)
        new = [r for r in records if r.id not in self._processed]
        self._last_season, _ = replay(
            new,
            self._book,
            self._rolling,
            last_season=self._last_season,
            goalies=self._goalies,
            team_goalie=self._team_goalie,
            log=self._backtest_log,
            shots=self._shots,
            last_played=self._last_played,
        )
        self._processed.update(r.id for r in new)
        if new:
            self._latest_result = max([self._latest_result or ""] + [r.date for r in new])
        if XG_ENABLED:
            try:
                self._refresh_xg()
            except Exception:  # noqa: BLE001 — the engine must never depend on the v2 file
                logging.getLogger("kryptscout").exception("nhl: xG refresh failed; serving without it")
        self._save_state()

    def _refresh_xg(self) -> bool:
        """Rebuild the xG state and weights from the MoneyPuck file if it changed.

        Returns True when the xG path was (re)fitted. Weights are fitted on
        seasons strictly before the file's latest season, so the current
        season is scored walk-forward; `_xg_metrics` reports that score.
        """
        path = self._raw_dir / "moneypuck" / MONEYPUCK_TEAMS_FILE
        if not path.exists():
            return False
        mtime = path.stat().st_mtime
        if self._xg_weights and self._xg_mtime == mtime:
            return False
        games = load_moneypuck_games(path)
        if not games:
            return False
        state, rows = xg_replay(games)
        season = max(g.season for g in games)
        weights = fit_xg_logit(rows, upto_season=season) or fit_xg_logit(rows, upto_season=season + 1)
        if weights is None:
            return False
        # Shootout games carry no winner in this file (see fit_xg_logit), so
        # the walk-forward score here is over the games it can label.
        scored = [r for r in rows if r["season"] == season and r["n_min"] >= XG_MIN_GAMES and not r.get("tied")]
        metrics: dict = {"season": season, "games": len(scored), "fitted_on_seasons_before": season}
        if scored:
            probs = [temper(xg_prob(weights, r), XG_LOGIT_SCALE) for r in scored]
            metrics["log_loss"] = -sum(
                math.log(max(1e-9, p if r["home_won"] else 1.0 - p)) for p, r in zip(probs, scored)
            ) / len(scored)
            metrics["accuracy"] = sum((p >= 0.5) == r["home_won"] for p, r in zip(probs, scored)) / len(scored)
        self._xg_state, self._xg_weights, self._xg_season = state, weights, season
        self._xg_mtime, self._xg_metrics = mtime, metrics
        self._xg_last_date = games[-1].date
        return True

    async def _fetch_boxscores(self, game_ids: list[int]) -> None:
        """Backfill gamecenter boxscores for uncached completed games.

        Past games are immutable, so each JSON is cached forever under
        RAW_DIR/nhl/box/{gameId}.json. Cold start is ~2,800 requests (a few
        minutes at BOX_CONCURRENCY); a warm refresh only fetches the handful
        of newly completed games. Failures after retries are skipped — that
        game simply carries no goalie signal until the next refresh retries.
        """
        self._box_dir.mkdir(parents=True, exist_ok=True)
        missing = [g for g in game_ids if not (self._box_dir / f"{g}.json").exists()]
        if not missing:
            return
        print(f"  [nhl] backfilling {len(missing)} boxscores ...")
        started = time.time()
        semaphore = asyncio.Semaphore(BOX_CONCURRENCY)
        done = 0

        async def fetch(client: httpx.AsyncClient, gid: int) -> None:
            nonlocal done
            async with semaphore:
                for attempt in range(BOX_RETRIES):
                    try:
                        resp = await client.get(BOX_API.format(game_id=gid))
                        if resp.status_code == 404:
                            # No boxscore exists for this id; cache the miss.
                            (self._box_dir / f"{gid}.json").write_text(
                                "{}", encoding="utf-8"
                            )
                            break
                        resp.raise_for_status()
                        (self._box_dir / f"{gid}.json").write_text(
                            resp.text, encoding="utf-8"
                        )
                        break
                    except Exception:
                        if attempt == BOX_RETRIES - 1:
                            print(f"  [nhl] boxscore {gid} failed; will retry next refresh")
                        else:
                            await asyncio.sleep(1.5 * (attempt + 1))
                done += 1
                if done % 200 == 0:
                    print(f"  [nhl] boxscores {done}/{len(missing)} "
                          f"({time.time() - started:.0f}s)")

        async with httpx.AsyncClient(timeout=30) as client:
            await asyncio.gather(*(fetch(client, gid) for gid in missing))
        print(f"  [nhl] boxscore backfill done: {len(missing)} games "
              f"in {time.time() - started:.0f}s")

    async def _fetch_raw(self) -> None:
        """Download club-schedule JSONs, honouring the cache rules.

        Past seasons never change once cached; current-season files are
        refreshed when older than RAW_MAX_AGE_S.
        """
        self._raw_dir.mkdir(parents=True, exist_ok=True)
        now = time.time()
        async with httpx.AsyncClient(timeout=30) as client:
            for season in _seasons():
                for abbrev in TEAM_NAMES:
                    path = self._raw_path(abbrev, season)
                    if path.exists():
                        if _is_past_season(season):
                            continue
                        if now - path.stat().st_mtime < RAW_MAX_AGE_S:
                            continue
                    resp = await client.get(
                        NHL_API.format(abbrev=abbrev, season=season)
                    )
                    if resp.status_code == 404:
                        # Club didn't exist that season (e.g. UTA before 2024);
                        # its games still appear in opponents' schedules.
                        path.write_text('{"games": []}', encoding="utf-8")
                        continue
                    resp.raise_for_status()
                    path.write_text(resp.text, encoding="utf-8")

    def _raw_path(self, abbrev: str, season: str) -> Path:
        return self._raw_dir / f"{abbrev}_{season}.json"

    def _load_payloads(self) -> list[dict]:
        payloads = []
        for path in sorted(self._raw_dir.glob("*.json")):
            try:
                payloads.append(json.loads(path.read_text(encoding="utf-8")))
            except (json.JSONDecodeError, OSError):
                continue  # a half-written cache file is refetched next refresh
        return payloads

    def _load_records(self, with_goalies: bool = True) -> list[GameRecord]:
        records = games_from_payloads(self._load_payloads())
        if with_goalies:
            records = attach_goalies(records, self._box_dir)
        return records

    # ----------------------------------------------------------------- state

    def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        self._loaded = True
        if not self._state_path.exists():
            return
        state = json.loads(self._state_path.read_text(encoding="utf-8"))
        if "goalies" not in state or "backtest" not in state or "shots" not in state:
            # Pre-goalie (v1), pre-log (v2) or pre-context (v3) state: ignore
            # it so the next refresh replays everything from the local caches
            # and rebuilds goalie ratings, shot EWMAs and the walk-forward log.
            return
        self._shots = {t: dict(v) for t, v in state["shots"].items()}
        self._last_played = dict(state.get("last_played") or {})
        self._latest_result = state.get("latest_result")
        self._book.ratings = {t: float(r) for t, r in state["ratings"].items()}
        self._rolling = _new_rolling()
        for team, window in state["rolling"].items():
            self._rolling[team].extend((int(gf), int(ga)) for gf, ga in window)
        self._goalies = dict(state["goalies"])
        self._team_goalie = dict(state.get("team_goalie", {}))
        self._processed = set(state["processed_ids"])
        self._last_season = state.get("last_season")
        self._backtest_log = list(state.get("backtest") or [])
        xg = state.get("xg") or {}
        if xg.get("weights"):
            self._xg_state = {t: dict(v) for t, v in (xg.get("state") or {}).items()}
            self._xg_weights = [float(v) for v in xg["weights"]]
            self._xg_season = xg.get("season")
            self._xg_mtime = xg.get("mtime")
            self._xg_metrics = dict(xg.get("metrics") or {})
            self._xg_last_date = xg.get("last_date")

    def _save_state(self) -> None:
        state = {
            "model": self.model_name,
            "updated_at": datetime.now(timezone.utc).isoformat(),
            "last_season": self._last_season,
            "ratings": self._book.ratings,
            "rolling": {t: list(w) for t, w in self._rolling.items()},
            "goalies": self._goalies,
            "team_goalie": self._team_goalie,
            "processed_ids": sorted(self._processed),
            "backtest": self._backtest_log,
            "shots": self._shots,
            "last_played": self._last_played,
            "latest_result": self._latest_result,
            "xg": {
                "state": self._xg_state,
                "weights": self._xg_weights,
                "season": self._xg_season,
                "mtime": self._xg_mtime,
                "metrics": self._xg_metrics,
                "last_date": self._xg_last_date,
            },
        }
        self._state_path.write_text(json.dumps(state), encoding="utf-8")

    # ------------------------------------------------------------ predictions

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        return await espn.scoreboard(Sport.NHL, on)

    def _slate_goalie_diff(self, home: str, away: str) -> float:
        """Pre-game goalie Elo adjustment for an upcoming game.

        Starters come from each team's most recent starter in the replay (the
        NFL QB fallback): a freshly announced starter is priced only once he
        has actually carried a game. TODO: overlay DailyFaceoff confirmed
        starters (https://www.dailyfaceoff.com/starting-goalies — Next.js
        __NEXT_DATA__, robots allows the page). Skipped for now: it's the
        off-season so the slate is empty/unverifiable, and mapping its
        display names to NHL player ids is fragile without a live slate.
        """
        if not GOALIE_ADJ_ENABLED:
            return 0.0
        return _goalie_diff(
            self._goalies, self._team_goalie.get(home), self._team_goalie.get(away)
        )

    def _played_day_before(self, team: str, day: date) -> bool:
        """Is `team` on the second night of a back-to-back on `day` (Eastern)?

        Read from the schedule (every dated game, played or not) so it holds
        before last night's result is ingested; falls back to the replay's
        last game date when the schedule cache is empty.
        """
        yesterday = date.fromordinal(day.toordinal() - 1).isoformat()
        dates = self._team_dates.get(team)
        if dates:
            return yesterday in dates
        return self._last_played.get(team, "")[:10] == yesterday

    async def predict(self, games: list[Game]) -> list[Prediction]:
        self._ensure_loaded()
        now = datetime.now(timezone.utc)
        predictions: list[Prediction] = []
        xg_live = self._xg_serving()
        if CTX_ENABLED and not self._team_dates:
            # Served from saved state before the first refresh of this run.
            self._team_dates = schedule_dates(self._load_payloads())
        for game in games:
            feats = xg_features(self._xg_state, game.home, game.away) if xg_live else None
            if feats is not None:
                home_prob = temper(xg_prob(self._xg_weights, feats), XG_LOGIT_SCALE)
                model = XG_MODEL_NAME
            else:
                home_prob = _expected_home_prob(
                    self._book,
                    game.home,
                    game.away,
                    self._slate_goalie_diff(game.home, game.away),
                )
                model = ELO_MODEL_NAME
                if CTX_ENABLED and _ctx_ready(self._shots, game.home, game.away):
                    day = game.start_time.astimezone(espn._EASTERN).date()
                    home_prob = ctx_prob(
                        home_prob,
                        self._played_day_before(game.home, day),
                        self._played_day_before(game.away, day),
                        self._shots[game.home]["sog"] - self._shots[game.away]["sog"],
                    )
                    model = CTX_MODEL_NAME
            goals_home, goals_away = _predicted_goals(
                self._rolling, game.home, game.away
            )
            total = goals_home + goals_away
            # A team with no scoring history would price at the league-average
            # placeholder — a constant, not a forecast. The board printed UNDER
            # edges against exactly that (WNBA, 2026-09-02). Emit no total.
            scored = bool(self._rolling.get(game.home)) and bool(self._rolling.get(game.away))
            predictions.append(
                Prediction(
                    game_id=game.id,
                    sport=Sport.NHL,
                    model=model,
                    generated_at=now,
                    home_win_prob=home_prob,
                    away_win_prob=1.0 - home_prob,
                    predicted_home_score=round(goals_home, 2) if scored else None,
                    predicted_away_score=round(goals_away, 2) if scored else None,
                    predicted_total=round(total, 2) if scored else None,
                    over_prob=_poisson_over_prob(total) if scored else None,
                )
            )
        return predictions

    # -------------------------------------------------------------- backtest

    def backtest(
        self,
        eval_season: int = VALIDATE_SEASON,
        k_goalie: float | None = None,
        goalie_span: int = GOALIE_SPAN,
        use_ctx: bool = False,
        regular_only: bool = False,
    ) -> dict[str, float]:
        """Fresh in-memory replay of all cached games, scoring eval_season.

        Uses pre-update probabilities only (no leakage; goalie ratings are
        built strictly from prior starts); does not touch the persisted
        engine state. k_goalie=0.0 scores the pure-Elo baseline; use_ctx
        scores the v3 context model over that Elo. Note the first cached
        season is a cold start (every team at 1500) for both.
        """
        records = self._load_records()
        book = EloBook(config=ELO_CONFIG)
        _, evals = replay(
            records,
            book,
            _new_rolling(),
            eval_season=eval_season,
            k_goalie=k_goalie,
            goalie_span=goalie_span,
            use_ctx=use_ctx,
        )
        if regular_only:
            evals = [e for e in evals if e[2].game_type == 2]
        return _score_evals(evals)


def build_engine() -> NhlEngine:
    return NhlEngine()


def _print_metrics(label: str, m: dict) -> None:
    print(f"  {label:34s} acc {m['accuracy']:.4f}   log loss {m['log_loss']:.4f}   "
          f"coverage {m.get('goalie_coverage', 0.0):.3f}   ({m['games']} games)")


async def _main() -> None:
    engine = build_engine()
    print(f"[{engine.model_name}] refreshing data ...")
    await engine.refresh_data()
    print(f"  ratings tracked: {len(engine._book.ratings)} teams, "
          f"{len(engine._goalies)} goalies")
    print(f"  seasons in the window: {', '.join(_seasons())}; newest replayed result: {engine._latest_result}")
    if engine._xg_serving():
        m = engine._xg_metrics
        print(f"  xG path active (nhl-xg-v2): season {m.get('season')} walk-forward "
              f"n={m.get('games')} log loss {m.get('log_loss', float('nan')):.4f} "
              f"acc {m.get('accuracy', float('nan')):.3f}")
    elif XG_ENABLED:
        print(f"  xG path NOT serving: file missing or stale (its last game {engine._xg_last_date})")
    else:
        print("  xG path off (XG_ENABLED = False)")

    print("\n  context model v3 vs the Elo it sits on (regular season, pre-update, this code):")
    for season in sorted({int(s) for s in _seasons()}):
        elo_m = engine.backtest(eval_season=season, k_goalie=0.0, regular_only=True)
        ctx_m = engine.backtest(eval_season=season, k_goalie=0.0, use_ctx=True, regular_only=True)
        if not elo_m.get("games"):
            continue
        print(f"    {season}: Elo {elo_m['log_loss']:.4f}  v3 {ctx_m['log_loss']:.4f}  "
              f"({ctx_m['log_loss'] - elo_m['log_loss']:+.4f}, {elo_m['games']} games; "
              f"acc {elo_m['accuracy']:.3f} -> {ctx_m['accuracy']:.3f})")

    print(f"\n  goalie tune on {TUNE_SEASON} (log loss; baseline k=0):")
    baseline_tune = engine.backtest(eval_season=TUNE_SEASON, k_goalie=0.0)
    print(f"    baseline: {baseline_tune['log_loss']:.4f}")
    best = (math.inf, None, None)
    for k_goalie in (500.0, 1000.0, 1500.0, 2000.0, 3000.0):
        row = []
        for span in (10, 20, 40):
            m = engine.backtest(
                eval_season=TUNE_SEASON, k_goalie=k_goalie, goalie_span=span
            )
            row.append(f"span {span:2d}: {m['log_loss']:.4f}")
            if m["log_loss"] < best[0]:
                best = (m["log_loss"], k_goalie, span)
        print(f"    k {k_goalie:6.0f}  " + "   ".join(row))
    print(f"    tune pick: k={best[1]:.0f}, span={best[2]} "
          f"(configured: k={GOALIE_K:.0f}, span={GOALIE_SPAN}, "
          f"enabled={GOALIE_ADJ_ENABLED})")

    print(f"\n  validation on {VALIDATE_SEASON} (held out, pre-update probabilities):")
    _print_metrics("baseline (team Elo, v1)",
                   engine.backtest(eval_season=VALIDATE_SEASON, k_goalie=0.0))
    status = "SHIPPED" if GOALIE_ADJ_ENABLED else "GATED OFF"
    _print_metrics(
        f"+goalie k={GOALIE_K:.0f} span={GOALIE_SPAN} [{status}]",
        engine.backtest(
            eval_season=VALIDATE_SEASON, k_goalie=GOALIE_K, goalie_span=GOALIE_SPAN
        ),
    )

    rated = [g for g in engine._goalies.values() if g["starts"] >= 15]
    for label, sign in (("top", True), ("bottom", False)):
        picks = sorted(rated, key=lambda g: g["ewma"], reverse=sign)[:5]
        print(f"\n  {label} 5 goalie EWMA save% (15+ starts):")
        for g in picks:
            print(f"    {g['ewma']:.4f}  {g['name']} ({g['starts']} starts)")

    slate = await engine.upcoming_games()
    print(f"\n  upcoming games today: {len(slate)}")
    if slate:
        for pred, game in zip(await engine.predict(slate), slate):
            print(
                f"    {game.away} @ {game.home}: "
                f"home {pred.home_win_prob:.3f}, total {pred.predicted_total}"
            )


if __name__ == "__main__":
    asyncio.run(_main())
