"""Soccer engine — time-decayed Poisson attack/defense model (v2).

The classic Dixon-Coles-style approach (inspired by ProphitBet, MIT): per
league, each club gets an attack and defense strength as the exponentially
time-decayed average of goals scored / conceded relative to the league mean.
Expected goals for a fixture come from multiplying strengths into the league's
home/away scoring averages; the three-way result (and over 2.5) is read off an
independent-Poisson score grid.

Data sources (all cached under RAW_DIR/soccer/):
- football-data.co.uk "main" season CSVs (mmz4281/…): EPL, La Liga,
  Bundesliga, Serie A, Ligue 1, Championship, Eredivisie, Primeira Liga.
- football-data.co.uk "extra" single-file histories (new/BRA.csv, new/MEX.csv)
  with the Country/League/Season/Date/…/Home/Away/HG/AG/Res schema:
  Brasileirão and Liga MX, filtered to Season >= 2024. (Brazil's season runs
  within one calendar year.)
- ESPN scoreboard-by-date for MLS (soccer/usa.1), which football-data does not
  carry; fetched month-by-month for the 2025/2026 seasons (Feb-Nov) and cached
  under RAW_DIR/soccer/mls/. MLS is fitted directly on ESPN display names.

Upcoming fixtures come from the ESPN scoreboard, which uses full display
names — TEAM_NAME_MAP (plus LEAGUE_NAME_OVERRIDES for cross-league ESPN name
collisions) bridges the two vocabularies.

The __main__ backtest also scores the *market* on the held-out slice: main and
extra CSVs carry closing decimal odds, which are devigged by normalizing
implied probabilities (same proportional scheme as core.oddsmath.remove_vig)
and printed side by side with the model.
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import time
from datetime import date, datetime, timezone
from io import StringIO

import httpx
import numpy as np
import pandas as pd

from kryptscout.config import DATA_DIR, RAW_DIR
from kryptscout.core.schemas import Game, Prediction, Sport
from kryptscout.datasources import espn
from kryptscout.sports.base import SportEngine

logger = logging.getLogger("kryptscout.soccer")

CSV_URL = "https://www.football-data.co.uk/mmz4281/{season}/{code}.csv"
EXTRA_CSV_URL = "https://www.football-data.co.uk/new/{code}.csv"
HISTORY_SEASONS = 3  # finished seasons kept alongside the one in progress


def season_codes(today: date | None = None, history: int = HISTORY_SEASONS) -> tuple[str, ...]:
    """football-data season codes ("2627" = 2026-27): the `history` finished
    seasons plus the one in progress. European seasons roll over on 1 July.

    This used to be a literal tuple ending at "2526"; from August 2026 the
    eight European leagues were therefore fitted on nothing later than May
    2026 and every promoted / relegated club was skipped.
    """
    today = today or date.today()
    start = today.year if today.month >= 7 else today.year - 1
    return tuple(f"{y % 100:02d}{(y + 1) % 100:02d}" for y in range(start - history, start + 1))


SEASONS = season_codes()
CURRENT_SEASON = SEASONS[-1]
CURRENT_SEASON_TTL = 24 * 3600  # refresh in-progress caches daily
EXTRA_MIN_SEASON = 2024  # extra files hold full history; only fit recent years

# football-data league code -> league label used by datasources/espn.py.
# Main files: one CSV per season per league (mmz4281 pattern).
MAIN_LEAGUES = {
    "E0": "EPL",
    "SP1": "La Liga",
    "D1": "Bundesliga",
    "I1": "Serie A",
    "F1": "Ligue 1",
    "E1": "Championship",
    "N1": "Eredivisie",
    "P1": "Primeira Liga",
}
# Extra files: one CSV per league with full history and a different schema.
EXTRA_LEAGUES = {
    "BRA": "Brasileirão",
    "MEX": "Liga MX",
}
# Leagues fitted directly on ESPN display names (no football-data coverage).
ESPN_NATIVE_LEAGUES = frozenset({"MLS"})

# Every league code -> label (the union the fitter and tests iterate over).
LEAGUES = {**MAIN_LEAGUES, **EXTRA_LEAGUES, "MLS": "MLS"}

CACHE_DIR = RAW_DIR / "soccer"
MLS_CACHE_DIR = CACHE_DIR / "mls"
STATE_PATH = DATA_DIR / "soccer_state.json"

# One calendar month per request, as `dates=YYYYMM`. The explicit range form
# (`dates=YYYYMMDD-YYYYMMDD`) stopped working in November 2026 — ESPN answers
# 400 "Failed to get events endpoint" for every league — and the month form
# returns the same events.
MLS_SCOREBOARD_URL = (
    espn.BASE + "/soccer/usa.1/scoreboard?dates={month}&limit=400"
)
MLS_SEASONS = (date.today().year - 1, date.today().year)  # rolls with the calendar
MLS_MONTHS = range(2, 12)  # the MLS regular season runs Feb-Nov

HALF_LIFE_DAYS = 180.0  # exponential decay half-life for match weights
PRIOR_MATCHES = 3.0  # shrink thin team samples toward league-average strength
MAX_GOALS = 10  # score grid is 0..MAX_GOALS per side

# Fit input (v3). Where a past match carries closing 1X2 odds, the strengths
# are fitted on the goal expectations that close implied rather than on the
# goals scored: one match's score is a very noisy read of how good two sides
# are, the closing line for that match is not. Walk-forward on 2024-10..2026-06
# picked a 90-day half-life and a 0.5-match prior for this input (goals keep
# 180 / 3.0); held out on 2026-08-01..2026-11-09 it cut log loss by 0.014
# against the goals fit and still trailed the closing market by 0.012.
# Leagues without odds in their result feed (MLS) keep the goals fit.
IMPLIED_HALF_LIFE_DAYS = 90.0
IMPLIED_PRIOR_MATCHES = 0.5
IMPLIED_MIN_SHARE = 0.5  # share of a league's rows that must carry a close
CLOSE_ODDS_CANDIDATES = (
    ("PSCH", "PSCD", "PSCA"),
    ("AvgCH", "AvgCD", "AvgCA"),
    ("B365CH", "B365CD", "B365CA"),
)
OVER_UNDER_LINE = 2.5

# Closing-odds column triples, in preference order, for the market benchmark.
# Main files: AvgH/AvgD/AvgA (market average), then Bet365. Extra files carry
# closing columns instead: AvgCH/…, Pinnacle closing PSCH/… (a.k.a. PH/PD/PA
# in older vintages), then Bet365 closing.
MAIN_ODDS_CANDIDATES = (
    ("AvgH", "AvgD", "AvgA"),
    ("B365H", "B365D", "B365A"),
    ("AvgCH", "AvgCD", "AvgCA"),
    ("PSCH", "PSCD", "PSCA"),
)
EXTRA_ODDS_CANDIDATES = (
    ("AvgCH", "AvgCD", "AvgCA"),
    ("AvgH", "AvgD", "AvgA"),
    ("PSCH", "PSCD", "PSCA"),
    ("PH", "PD", "PA"),
    ("B365CH", "B365CD", "B365CA"),
    ("B365H", "B365D", "B365A"),
)

# football-data short name -> ESPN displayName, every club in the covered
# season files of the football-data leagues (2025-26 mains, Season >= 2024
# current rosters for BRA/MEX). Verified against ESPN's own scoreboards and
# /teams endpoints. Unmapped names fall through with a logged warning. MLS
# needs no mapping (its history is ingested from ESPN itself).
TEAM_NAME_MAP: dict[str, str] = {
    # EPL (E0)
    "Arsenal": "Arsenal",
    "Aston Villa": "Aston Villa",
    "Bournemouth": "AFC Bournemouth",
    "Brentford": "Brentford",
    "Brighton": "Brighton & Hove Albion",
    "Burnley": "Burnley",
    "Chelsea": "Chelsea",
    "Crystal Palace": "Crystal Palace",
    "Everton": "Everton",
    "Fulham": "Fulham",
    "Leeds": "Leeds United",
    "Liverpool": "Liverpool",
    "Man City": "Manchester City",
    "Man United": "Manchester United",
    "Newcastle": "Newcastle United",
    "Nott'm Forest": "Nottingham Forest",
    "Sunderland": "Sunderland",
    "Tottenham": "Tottenham Hotspur",
    "West Ham": "West Ham United",
    "Wolves": "Wolverhampton Wanderers",
    # La Liga (SP1)
    "La Coruna": "Deportivo",
    "Malaga": "Málaga",
    "Santander": "Racing Santander",
    "Alaves": "Alavés",
    "Ath Bilbao": "Athletic Club",
    "Ath Madrid": "Atlético Madrid",
    "Barcelona": "Barcelona",
    "Betis": "Real Betis",
    "Celta": "Celta Vigo",
    "Elche": "Elche",
    "Espanol": "Espanyol",
    "Getafe": "Getafe",
    "Girona": "Girona",
    "Levante": "Levante",
    "Mallorca": "Mallorca",
    "Osasuna": "Osasuna",
    "Oviedo": "Real Oviedo",
    "Real Madrid": "Real Madrid",
    "Sevilla": "Sevilla",
    "Sociedad": "Real Sociedad",
    "Valencia": "Valencia",
    "Vallecano": "Rayo Vallecano",
    "Villarreal": "Villarreal",
    # Bundesliga (D1)
    "Elversberg": "SV Elversberg",
    "Paderborn": "SC Paderborn 07",
    "Schalke 04": "Schalke 04",
    "Augsburg": "FC Augsburg",
    "Bayern Munich": "Bayern Munich",
    "Dortmund": "Borussia Dortmund",
    "Ein Frankfurt": "Eintracht Frankfurt",
    "FC Koln": "FC Cologne",
    "Freiburg": "SC Freiburg",
    "Hamburg": "Hamburg SV",
    "Heidenheim": "1. FC Heidenheim 1846",
    "Hoffenheim": "TSG Hoffenheim",
    "Leverkusen": "Bayer Leverkusen",
    "M'gladbach": "Borussia Mönchengladbach",
    "Mainz": "Mainz",
    "RB Leipzig": "RB Leipzig",
    "St Pauli": "St. Pauli",
    "Stuttgart": "VfB Stuttgart",
    "Union Berlin": "1. FC Union Berlin",
    "Werder Bremen": "Werder Bremen",
    "Wolfsburg": "VfL Wolfsburg",
    # Serie A (I1)
    "Atalanta": "Atalanta",
    "Bologna": "Bologna",
    "Cagliari": "Cagliari",
    "Como": "Como",
    "Cremonese": "Cremonese",
    "Fiorentina": "Fiorentina",
    "Frosinone": "Frosinone",
    "Venezia": "Venezia",
    "Genoa": "Genoa",
    "Inter": "Internazionale",
    "Juventus": "Juventus",
    "Lazio": "Lazio",
    "Lecce": "Lecce",
    "Milan": "AC Milan",
    "Monza": "Monza",  # relegated 2025 but back in ESPN's 2026-27 scoreboard
    "Napoli": "Napoli",
    "Parma": "Parma",
    "Pisa": "Pisa",
    "Roma": "AS Roma",
    "Sassuolo": "Sassuolo",
    "Torino": "Torino",
    "Udinese": "Udinese",
    "Verona": "Hellas Verona",
    # Ligue 1 (F1)
    "Angers": "Angers",
    "Auxerre": "AJ Auxerre",
    "Brest": "Brest",
    "Le Havre": "Le Havre AC",
    "Le Mans": "Le Mans",
    "Troyes": "Troyes",
    "Lens": "Lens",
    "Lille": "Lille",
    "Lorient": "Lorient",
    "Lyon": "Lyon",
    "Marseille": "Marseille",
    "Metz": "Metz",
    "Monaco": "AS Monaco",
    "Nantes": "Nantes",
    "Nice": "Nice",
    "Paris FC": "Paris FC",
    "Paris SG": "Paris Saint-Germain",
    "Rennes": "Stade Rennais",
    "Strasbourg": "Strasbourg",
    "Toulouse": "Toulouse",
    # Championship (E1)
    "Birmingham": "Birmingham City",
    "Bolton": "Bolton Wanderers",
    "Cardiff": "Cardiff City",
    "Lincoln": "Lincoln City",
    "Blackburn": "Blackburn Rovers",
    "Bristol City": "Bristol City",
    "Charlton": "Charlton Athletic",
    "Coventry": "Coventry City",
    "Derby": "Derby County",
    "Hull": "Hull City",
    "Ipswich": "Ipswich Town",
    "Leicester": "Leicester City",
    "Middlesbrough": "Middlesbrough",
    "Millwall": "Millwall",
    "Norwich": "Norwich City",
    "Oxford": "Oxford United",
    "Portsmouth": "Portsmouth",
    "Preston": "Preston North End",
    "QPR": "Queens Park Rangers",
    "Sheffield United": "Sheffield United",
    "Sheffield Weds": "Sheffield Wednesday",
    "Southampton": "Southampton",
    "Stoke": "Stoke City",
    "Swansea": "Swansea City",
    "Watford": "Watford",
    "West Brom": "West Bromwich Albion",
    "Wrexham": "Wrexham",
    # Eredivisie (N1)
    "AZ Alkmaar": "AZ Alkmaar",
    "Cambuur": "SC Cambuur",
    "Den Haag": "ADO Den Haag",
    "Willem II": "Willem II",
    "Ajax": "Ajax Amsterdam",
    "Excelsior": "Excelsior",
    "Feyenoord": "Feyenoord Rotterdam",
    "For Sittard": "Fortuna Sittard",
    "Go Ahead Eagles": "Go Ahead Eagles",
    "Groningen": "FC Groningen",
    "Heerenveen": "Heerenveen",
    "Heracles": "Heracles Almelo",
    "NAC Breda": "NAC Breda",
    "Nijmegen": "NEC Nijmegen",
    "PSV Eindhoven": "PSV Eindhoven",
    "Sparta Rotterdam": "Sparta Rotterdam",
    "Telstar": "Telstar",
    "Twente": "FC Twente",
    "Utrecht": "FC Utrecht",
    "Volendam": "FC Volendam",
    "Zwolle": "PEC Zwolle",
    # Primeira Liga (P1)
    "AVS": "AVS",
    "Academico Viseu": "Académico de Viseu",
    "Maritimo": "Maritimo",
    "Alverca": "Alverca",
    "Arouca": "Arouca",
    "Benfica": "Benfica",
    "Casa Pia": "Casa Pia",
    "Estoril": "Estoril",
    "Estrela": "Estrela",
    "Famalicao": "FC Famalicao",
    "Gil Vicente": "Gil Vicente",
    "Guimaraes": "Vitória de Guimaraes",
    "Moreirense": "Moreirense",
    "Nacional": "C.D. Nacional",
    "Porto": "FC Porto",
    "Rio Ave": "Rio Ave",
    "Santa Clara": "Santa Clara",
    "Sp Braga": "Braga",
    "Sp Lisbon": "Sporting CP",
    "Tondela": "Tondela",
    # Brasileirão (BRA extra file)
    "Athletico-PR": "Athletico-PR",
    "Atletico-MG": "Atlético-MG",
    "Bahia": "Bahia",
    "Botafogo RJ": "Botafogo",
    "Bragantino": "Red Bull Bragantino",
    "Chapecoense-SC": "Chapecoense",
    "Corinthians": "Corinthians",
    "Coritiba": "Coritiba",
    "Cruzeiro": "Cruzeiro",
    "Flamengo RJ": "Flamengo",
    "Fluminense": "Fluminense",
    "Gremio": "Grêmio",
    "Internacional": "Internacional",
    "Mirassol": "Mirassol",
    "Palmeiras": "Palmeiras",
    "Remo": "Remo",
    "Santos": "Santos",
    "Sao Paulo": "São Paulo",
    "Vasco": "Vasco da Gama",
    "Vitoria": "Vitória",
    # Liga MX (MEX extra file) — Santos Laguna lives in LEAGUE_NAME_OVERRIDES
    # because ESPN calls both it and Brazil's Santos plain "Santos".
    "Atl. San Luis": "Atlético de San Luis",
    "Atlante": "Atlante",  # promoted for 2026-27; pre-2024 rows sit below the season filter
    "Atlas": "Atlas",
    "Club America": "América",
    "Club Leon": "León",
    "Club Tijuana": "Tijuana",
    "Cruz Azul": "Cruz Azul",
    "Guadalajara Chivas": "Guadalajara",
    "Juarez": "FC Juarez",
    "Mazatlan FC": "Mazatlán FC",
    "Monterrey": "Monterrey",
    "Necaxa": "Necaxa",
    "Pachuca": "Pachuca",
    "Puebla": "Puebla",
    "Queretaro": "Querétaro",
    "Tigres UANL": "Tigres UANL",
    "Toluca": "Toluca",
    "UNAM Pumas": "Pumas UNAM",
}

ESPN_TO_FD: dict[str, str] = {v: k for k, v in TEAM_NAME_MAP.items()}

# ESPN displayName -> football-data name, consulted per league BEFORE the
# global inverse map, for names ESPN reuses across leagues.
LEAGUE_NAME_OVERRIDES: dict[str, dict[str, str]] = {
    "Liga MX": {"Santos": "Santos Laguna", "FC Juárez": "Juarez"},
}


# --------------------------------------------------------------------------
# Poisson math (pure stdlib — no scipy)
# --------------------------------------------------------------------------

def poisson_pmf(lam: float, k: int) -> float:
    return math.exp(-lam) * lam**k / math.factorial(k)


def outcome_probs(lam_home: float, lam_away: float) -> dict[str, float]:
    """Three-way result + over-2.5 probabilities from an independent-Poisson
    score grid, renormalized over the (0..MAX_GOALS)^2 truncation."""
    ph = [poisson_pmf(lam_home, k) for k in range(MAX_GOALS + 1)]
    pa = [poisson_pmf(lam_away, k) for k in range(MAX_GOALS + 1)]
    home = draw = away = over = total = 0.0
    for i, pi in enumerate(ph):
        for j, pj in enumerate(pa):
            p = pi * pj
            total += p
            if i > j:
                home += p
            elif i == j:
                draw += p
            else:
                away += p
            if i + j > OVER_UNDER_LINE:
                over += p
    return {
        "home": home / total,
        "draw": draw / total,
        "away": away / total,
        "over": over / total,
    }


def devig_decimal(*decimal_odds: float) -> list[float]:
    """Vig-free probabilities from DECIMAL odds for a full market.

    Same proportional devigging as core.oddsmath.remove_vig (which takes
    American prices): normalize the raw implied probabilities 1/odds so they
    sum to 1.
    """
    if any(o <= 1.0 for o in decimal_odds):
        raise ValueError("Decimal odds must exceed 1.0")
    raw = [1.0 / o for o in decimal_odds]
    total = sum(raw)
    return [r / total for r in raw]


_IMPLIED_TABLE: tuple[np.ndarray, np.ndarray, np.ndarray] | None = None


def _implied_table() -> tuple[np.ndarray, np.ndarray, np.ndarray]:
    """(lam_home, lam_away, 3-way probabilities) over a 0.15..4.5 goal grid in
    0.025 steps, from the same truncated independent-Poisson score grid as
    :func:`outcome_probs`. Built once (about 30k rows)."""
    global _IMPLIED_TABLE
    if _IMPLIED_TABLE is None:
        axis = np.arange(0.15, 4.5001, 0.025)
        lam_h, lam_a = (m.ravel() for m in np.meshgrid(axis, axis, indexing="ij"))
        k = np.arange(MAX_GOALS + 1)
        log_fact = np.array([math.lgamma(i + 1) for i in k])
        pmf_h = np.exp(-lam_h[:, None] + k[None, :] * np.log(lam_h[:, None]) - log_fact[None, :])
        pmf_a = np.exp(-lam_a[:, None] + k[None, :] * np.log(lam_a[:, None]) - log_fact[None, :])
        grid = pmf_h[:, :, None] * pmf_a[:, None, :]
        total = grid.sum(axis=(1, 2))
        home = np.tril(grid, -1).sum(axis=(1, 2))
        draw = np.einsum("nii->n", grid)
        away = np.triu(grid, 1).sum(axis=(1, 2))
        _IMPLIED_TABLE = (lam_h, lam_a, np.stack([home, draw, away], axis=1) / total[:, None])
    return _IMPLIED_TABLE


def implied_goals(odds_h, odds_d, odds_a) -> tuple[np.ndarray, np.ndarray]:
    """(lam_home, lam_away) whose Poisson 1X2 is nearest the devigged decimal
    closing odds, per match; NaN where a price is missing or not above 1.0."""
    raw = np.stack(
        [np.asarray(pd.to_numeric(pd.Series(o), errors="coerce"), dtype=float) for o in (odds_h, odds_d, odds_a)],
        axis=1,
    )
    ok = np.isfinite(raw).all(axis=1) & (raw > 1.0).all(axis=1)
    out_h = np.full(len(raw), np.nan)
    out_a = np.full(len(raw), np.nan)
    if ok.any():
        lam_h, lam_a, table = _implied_table()
        inv = 1.0 / raw[ok]
        probs = inv / inv.sum(axis=1, keepdims=True)
        idx = np.where(ok)[0]
        for start in range(0, len(idx), 256):  # bounded memory
            chunk = probs[start:start + 256]
            dist = ((table[None, :, :] - chunk[:, None, :]) ** 2).sum(axis=2)
            best = dist.argmin(axis=1)
            out_h[idx[start:start + 256]] = lam_h[best]
            out_a[idx[start:start + 256]] = lam_a[best]
    return out_h, out_a


# --------------------------------------------------------------------------
# Fitting
# --------------------------------------------------------------------------

def fit_league(df: pd.DataFrame, as_of: datetime) -> dict:
    """Time-decayed attack/defense strengths for one league.

    df needs columns Date (datetime), HomeTeam, AwayTeam, FTHG, FTAG.
    Returns {"home_avg", "away_avg", "teams": {name: {"attack", "defense"}}}.
    """
    df = df[df["Date"] <= as_of]
    if df.empty:
        raise ValueError("no matches to fit on")
    # Market-implied goal expectations stand in for the score wherever the
    # row has them, provided the league's feed carries closes at all.
    implied = (
        {"ImpH", "ImpA"}.issubset(df.columns)
        and df[["ImpH", "ImpA"]].notna().all(axis=1).mean() >= IMPLIED_MIN_SHARE
    )
    if implied:
        has = df[["ImpH", "ImpA"]].notna().all(axis=1)
        df = df.assign(
            FTHG=df["ImpH"].where(has, df["FTHG"]).astype(float),
            FTAG=df["ImpA"].where(has, df["FTAG"]).astype(float),
        )
    half_life = IMPLIED_HALF_LIFE_DAYS if implied else HALF_LIFE_DAYS
    prior = IMPLIED_PRIOR_MATCHES if implied else PRIOR_MATCHES
    days_ago = (as_of - df["Date"]).dt.days.clip(lower=0)
    w = 0.5 ** (days_ago / half_life)
    w_sum = w.sum()

    home_avg = float((w * df["FTHG"]).sum() / w_sum)
    away_avg = float((w * df["FTAG"]).sum() / w_sum)
    # League-average goals per team per game — the strength denominator.
    mu = (home_avg + away_avg) / 2.0

    scored: dict[str, float] = {}
    conceded: dict[str, float] = {}
    weight: dict[str, float] = {}
    for team_col, gf_col, ga_col in (("HomeTeam", "FTHG", "FTAG"), ("AwayTeam", "FTAG", "FTHG")):
        grouped = pd.DataFrame(
            {"gf": w * df[gf_col], "ga": w * df[ga_col], "w": w}
        ).groupby(df[team_col]).sum()
        for team, row in grouped.iterrows():
            scored[team] = scored.get(team, 0.0) + row["gf"]
            conceded[team] = conceded.get(team, 0.0) + row["ga"]
            weight[team] = weight.get(team, 0.0) + row["w"]

    teams = {}
    for team, tw in weight.items():
        # Shrink toward league average (strength 1.0) with a small prior so
        # newly promoted clubs early in a season don't get wild strengths.
        denom = (tw + prior) * mu
        teams[team] = {
            "attack": (scored[team] + prior * mu) / denom,
            "defense": (conceded[team] + prior * mu) / denom,
        }
    return {"home_avg": home_avg, "away_avg": away_avg, "teams": teams}


def expected_goals(league_state: dict, home: str, away: str) -> tuple[float, float]:
    """(λ_home, λ_away) for a fixture; unknown teams get league-average 1.0."""
    teams = league_state["teams"]
    default = {"attack": 1.0, "defense": 1.0}
    h, a = teams.get(home, default), teams.get(away, default)
    lam_home = league_state["home_avg"] * h["attack"] * a["defense"]
    lam_away = league_state["away_avg"] * a["attack"] * h["defense"]
    return lam_home, lam_away


# --------------------------------------------------------------------------
# Data loading (football-data.co.uk + ESPN, cached under RAW_DIR/soccer/)
# --------------------------------------------------------------------------

def _closing_odds(df: pd.DataFrame, candidates: tuple[tuple[str, str, str], ...]) -> pd.DataFrame:
    """MarketH/MarketD/MarketA decimal closing odds, taking the first candidate
    column triple that yields a complete valid (>1.0) row; NaN where none do."""
    out = pd.DataFrame(index=df.index, columns=["MarketH", "MarketD", "MarketA"], dtype=float)
    for cols in candidates:
        if not all(c in df.columns for c in cols):
            continue
        cand = df[list(cols)].apply(pd.to_numeric, errors="coerce")
        fill = out.isna().any(axis=1) & cand.notna().all(axis=1) & (cand > 1.0).all(axis=1)
        if fill.any():
            out.loc[fill, :] = cand.loc[fill].to_numpy()
    return out


def _finish_results(out: pd.DataFrame) -> pd.DataFrame:
    out = out.dropna(subset=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"]).copy()
    out["Date"] = pd.to_datetime(out["Date"], dayfirst=True, format="mixed")
    out["FTHG"] = out["FTHG"].astype(int)
    out["FTAG"] = out["FTAG"].astype(int)
    return out.sort_values("Date").reset_index(drop=True)


def _parse_results_csv(text: str) -> pd.DataFrame:
    """Robustly parse a football-data 'main' season CSV down to the result
    columns plus closing odds."""
    df = pd.read_csv(StringIO(text), on_bad_lines="skip", encoding_errors="replace")
    df.columns = [str(c).lstrip("﻿").strip() for c in df.columns]
    out = df[["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG", "FTR"]].copy()
    out[["MarketH", "MarketD", "MarketA"]] = _closing_odds(df, MAIN_ODDS_CANDIDATES)
    close = _closing_odds(df, CLOSE_ODDS_CANDIDATES)
    out["ImpH"], out["ImpA"] = implied_goals(close["MarketH"], close["MarketD"], close["MarketA"])
    return _finish_results(out)


def _parse_extra_csv(text: str, min_season: int = EXTRA_MIN_SEASON) -> pd.DataFrame:
    """Parse a football-data 'extra' full-history CSV (Country,League,Season,
    Date,Time,Home,Away,HG,AG,Res,+odds) onto the main-file record shape.

    Season may be a single year (Brazil: the season runs within one calendar
    year) or "2024/2025"; rows are kept when the starting year >= min_season.
    """
    df = pd.read_csv(StringIO(text), on_bad_lines="skip", encoding_errors="replace")
    df.columns = [str(c).lstrip("﻿").strip() for c in df.columns]
    start_year = pd.to_numeric(df["Season"].astype(str).str.slice(0, 4), errors="coerce")
    df = df[start_year >= min_season]
    out = pd.DataFrame(
        {
            "Date": df["Date"],
            "HomeTeam": df["Home"],
            "AwayTeam": df["Away"],
            "FTHG": pd.to_numeric(df["HG"], errors="coerce"),
            "FTAG": pd.to_numeric(df["AG"], errors="coerce"),
            "FTR": df["Res"] if "Res" in df.columns else None,
        }
    )
    out[["MarketH", "MarketD", "MarketA"]] = _closing_odds(df, EXTRA_ODDS_CANDIDATES)
    # the extra files' candidates are closing prices already
    out["ImpH"], out["ImpA"] = implied_goals(out["MarketH"], out["MarketD"], out["MarketA"])
    out = _finish_results(out)
    # Res is occasionally blank; the goals are authoritative anyway.
    out["FTR"] = ["H" if h > a else ("A" if a > h else "D") for h, a in zip(out["FTHG"], out["FTAG"])]
    return out


def _parse_mls_scoreboard(payload: dict) -> list[dict]:
    """Completed-game rows from one ESPN scoreboard JSON (usa.1)."""
    rows = []
    for event in payload.get("events", []):
        comp = (event.get("competitions") or [{}])[0]
        if not comp.get("status", {}).get("type", {}).get("completed"):
            continue
        competitors = comp.get("competitors", [])
        home = next((c for c in competitors if c.get("homeAway") == "home"), None)
        away = next((c for c in competitors if c.get("homeAway") == "away"), None)
        if not home or not away or not home.get("score") or not away.get("score"):
            continue
        when = datetime.fromisoformat(event["date"].replace("Z", "+00:00"))
        rows.append(
            {
                "Date": when.astimezone(timezone.utc).replace(tzinfo=None),
                "HomeTeam": home["team"]["displayName"],
                "AwayTeam": away["team"]["displayName"],
                "FTHG": int(home["score"]),
                "FTAG": int(away["score"]),
            }
        )
    return rows


async def _refresh_file(
    client: httpx.AsyncClient, url: str, path, *, mutable: bool, force: bool = False
) -> bool:
    """Ensure `path` holds a copy of `url`; returns whether the file exists.

    Immutable files (finished seasons/months) are cached forever; mutable ones
    are re-fetched once older than CURRENT_SEASON_TTL. Falls back to a stale
    cache when a refresh fails."""
    stale = (
        mutable and (force or time.time() - path.stat().st_mtime > CURRENT_SEASON_TTL)
        if path.exists()
        else True
    )
    if stale:
        try:
            resp = await client.get(url)
            resp.raise_for_status()
            path.write_bytes(resp.content)
        except httpx.HTTPError as exc:
            if not path.exists():
                logger.warning("could not fetch %s: %s", url, exc)
                return False
            logger.warning("refresh failed for %s, using cache: %s", url, exc)
    return True


async def _load_mls_results(client: httpx.AsyncClient, force_refresh: bool = False) -> pd.DataFrame:
    """MLS results from ESPN scoreboard-by-date, one cached JSON per month."""
    MLS_CACHE_DIR.mkdir(parents=True, exist_ok=True)
    now = datetime.now(timezone.utc)
    rows: list[dict] = []
    for year in (now.year - 1, now.year):
        for month in MLS_MONTHS:
            if (year, month) > (now.year, now.month):
                continue
            path = MLS_CACHE_DIR / f"{year}{month:02d}.json"
            url = MLS_SCOREBOARD_URL.format(month=f"{year}{month:02d}")
            is_current = (year, month) == (now.year, now.month)
            if not await _refresh_file(client, url, path, mutable=is_current, force=force_refresh):
                continue
            try:
                rows.extend(_parse_mls_scoreboard(json.loads(path.read_text(encoding="utf-8"))))
            except (json.JSONDecodeError, KeyError, ValueError) as exc:
                logger.warning("bad MLS scoreboard cache %s: %s", path, exc)
    df = pd.DataFrame(rows, columns=["Date", "HomeTeam", "AwayTeam", "FTHG", "FTAG"])
    df["FTR"] = ["H" if h > a else ("A" if a > h else "D") for h, a in zip(df["FTHG"], df["FTAG"])]
    df[["MarketH", "MarketD", "MarketA"]] = float("nan")  # ESPN carries no closing odds
    return df.sort_values("Date").reset_index(drop=True)


async def load_results(force_refresh: bool = False) -> dict[str, pd.DataFrame]:
    """Results per league code, downloading whatever the cache is missing.

    Past seasons/months are cached forever; in-progress files are re-fetched
    once the cached copy is older than CURRENT_SEASON_TTL.
    """
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    out: dict[str, pd.DataFrame] = {}
    async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
        frames: dict[str, list[pd.DataFrame]] = {code: [] for code in MAIN_LEAGUES}
        seasons = season_codes()  # evaluated per refresh: a long-lived process rolls over too
        for season in seasons:
            for code in MAIN_LEAGUES:
                path = CACHE_DIR / f"{season}_{code}.csv"
                url = CSV_URL.format(season=season, code=code)
                if await _refresh_file(
                    client, url, path, mutable=season == seasons[-1], force=force_refresh
                ):
                    try:
                        frames[code].append(_parse_results_csv(path.read_text(encoding="latin-1")))
                    except (KeyError, ValueError) as exc:
                        # e.g. the new season's file before its first matchday
                        logger.warning("unusable season file %s: %s", path.name, exc)
        for code, parts in frames.items():
            if parts:
                out[code] = (
                    pd.concat(parts, ignore_index=True).sort_values("Date").reset_index(drop=True)
                )
        # Extra files hold the in-progress season too, so they are always mutable.
        for code in EXTRA_LEAGUES:
            path = CACHE_DIR / f"extra_{code}.csv"
            url = EXTRA_CSV_URL.format(code=code)
            if await _refresh_file(client, url, path, mutable=True, force=force_refresh):
                df = _parse_extra_csv(path.read_text(encoding="latin-1"))
                if not df.empty:
                    out[code] = df
        mls = await _load_mls_results(client, force_refresh)
        if not mls.empty:
            out["MLS"] = mls
    return out


# --------------------------------------------------------------------------
# Engine
# --------------------------------------------------------------------------

class SoccerEngine(SportEngine):
    sport = Sport.SOCCER
    model_name = "soccer-poisson-v3"

    def __init__(self) -> None:
        self._state: dict | None = None

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        return await espn.scoreboard(Sport.SOCCER, on)

    async def predict(self, games: list[Game]) -> list[Prediction]:
        state = await self._ensure_state()
        now = datetime.now(timezone.utc)
        preds: list[Prediction] = []
        for game in games:
            league_state = state["leagues"].get(game.league)
            if league_state is None:
                logger.warning("no model for league %r, skipping %s", game.league, game.id)
                continue
            fd_home = self._to_fd_name(game.home, game.league)
            fd_away = self._to_fd_name(game.away, game.league)
            # No fitted strengths for a side (promoted club, unmapped name):
            # skip rather than emit league-average probabilities — those turn
            # longshot prices into huge fake edges downstream.
            if fd_home not in league_state["teams"] or fd_away not in league_state["teams"]:
                logger.warning(
                    "no fitted strengths for %s vs %s (%s), skipping %s",
                    game.home, game.away, game.league, game.id,
                )
                continue
            lam_h, lam_a = expected_goals(league_state, fd_home, fd_away)
            probs = outcome_probs(lam_h, lam_a)
            preds.append(
                Prediction(
                    game_id=game.id,
                    sport=self.sport,
                    model=self.model_name,
                    generated_at=now,
                    home_win_prob=probs["home"],
                    draw_prob=probs["draw"],
                    away_win_prob=probs["away"],
                    predicted_home_score=lam_h,
                    predicted_away_score=lam_a,
                    predicted_total=lam_h + lam_a,
                    over_prob=probs["over"],
                )
            )
        return preds

    async def refresh_data(self) -> None:
        results = await load_results()
        as_of = datetime.now()
        leagues = {
            LEAGUES[code]: fit_league(df, as_of) for code, df in results.items()
        }
        self._state = {
            "fitted_at": as_of.isoformat(),
            "leagues": leagues,
            "backtest": _walk_forward_log(results),
        }
        STATE_PATH.write_text(json.dumps(self._state, indent=1), encoding="utf-8")
        logger.info("soccer state refreshed: %d leagues", len(leagues))

    async def _ensure_state(self) -> dict:
        if self._state is None:
            if STATE_PATH.exists():
                self._state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
            else:
                await self.refresh_data()
        return self._state

    def _to_fd_name(self, espn_name: str, league: str) -> str:
        # MLS (and any future ESPN-native league) is fitted on ESPN names.
        if league in ESPN_NATIVE_LEAGUES:
            return espn_name
        override = LEAGUE_NAME_OVERRIDES.get(league, {}).get(espn_name)
        if override is not None:
            return override
        fd = ESPN_TO_FD.get(espn_name)
        if fd is None:
            logger.warning("no football-data mapping for ESPN team %r", espn_name)
            return espn_name
        return fd


def build_engine() -> SoccerEngine:
    return SoccerEngine()


# --------------------------------------------------------------------------
# Backtest / smoke run: python -m kryptscout.sports.soccer
# --------------------------------------------------------------------------

# Walk-forward log start — matches the Polymarket backfill's earliest soccer
# markets. Rows are leakage-safe: each month's fixtures are scored by
# strengths fitted only on earlier matches (the _backtest methodology).
WALK_FORWARD_FROM = datetime(2024, 10, 1)


def _walk_forward_log(results: dict[str, pd.DataFrame]) -> list[list]:
    """Per-fixture walk-forward rows for the dollar-ROI join + backtest panel.

    7 fields: [date, p_home, home_won, home, away, p_draw, p_away] — the
    first five match the cross-sport log contract (store/backtest.py reads
    them as a binary home-win calibration log); the two extra probabilities
    let the soccer join price draw / away-win Yes/No markets.
    """
    rows: list[list] = []
    for code, df in results.items():
        test = df[df["Date"] > WALK_FORWARD_FROM]
        if test.empty:
            continue
        states = {}
        for month_start in {
            datetime(d.year, d.month, 1) for d in test["Date"]
        } | {WALK_FORWARD_FROM}:
            try:
                states[month_start] = fit_league(df, month_start)
            except ValueError:
                # League data starts after this month — those fixtures are
                # skipped below (no prior matches = nothing honest to fit).
                pass
        for row in test.itertuples():
            as_of = max(datetime(row.Date.year, row.Date.month, 1), WALK_FORWARD_FROM)
            if as_of not in states:
                continue
            lam_h, lam_a = expected_goals(states[as_of], row.HomeTeam, row.AwayTeam)
            probs = outcome_probs(lam_h, lam_a)
            rows.append([
                row.Date.date().isoformat(),
                round(probs["home"], 6),
                int(row.FTHG > row.FTAG),
                str(row.HomeTeam),
                str(row.AwayTeam),
                round(probs["draw"], 6),
                round(probs["away"], 6),
                "H" if row.FTHG > row.FTAG else ("A" if row.FTAG > row.FTHG else "D"),
            ])
    rows.sort(key=lambda r: r[0])
    return rows


def _backtest(results: dict[str, pd.DataFrame], cutoff: datetime) -> None:
    """Hold out everything after `cutoff`; refit at each month boundary so a
    test game only ever sees strengths fitted on earlier matches.

    Where the CSVs carry closing odds, the devigged market is scored on the
    same games as the honest benchmark — expect it to beat the model."""
    n = correct = 0
    log_loss = 0.0
    mkt_n = mkt_model_correct = mkt_market_correct = 0
    mkt_model_ll = mkt_market_ll = 0.0
    eps = 1e-12
    outcomes = ("home", "draw", "away")
    for code, df in results.items():
        test = df[df["Date"] > cutoff]
        if test.empty:
            continue
        states = {
            month_start: fit_league(df, month_start)
            for month_start in {datetime(d.year, d.month, 1) for d in test["Date"]} | {cutoff}
        }
        for row in test.itertuples():
            as_of = max(datetime(row.Date.year, row.Date.month, 1), cutoff)
            lam_h, lam_a = expected_goals(states[as_of], row.HomeTeam, row.AwayTeam)
            probs = outcome_probs(lam_h, lam_a)
            actual = "home" if row.FTHG > row.FTAG else ("away" if row.FTAG > row.FTHG else "draw")
            pick = max(outcomes, key=probs.__getitem__)
            n += 1
            correct += pick == actual
            log_loss -= math.log(max(probs[actual], eps))

            odds = (row.MarketH, row.MarketD, row.MarketA)
            if all(isinstance(o, float) and math.isfinite(o) and o > 1.0 for o in odds):
                market = dict(zip(outcomes, devig_decimal(*odds)))
                mkt_n += 1
                mkt_model_correct += pick == actual
                mkt_model_ll -= math.log(max(probs[actual], eps))
                mkt_market_correct += max(outcomes, key=market.__getitem__) == actual
                mkt_market_ll -= math.log(max(market[actual], eps))
    print(f"backtest cutoff {cutoff.date()}: {n} held-out games")
    print(f"  3-way accuracy : {correct / n:.4f}  (naive uniform: 0.3333)")
    print(f"  multiclass log loss: {log_loss / n:.4f}  (naive uniform: {math.log(3):.4f})")
    if mkt_n:
        print(f"  model vs devigged closing market ({mkt_n} games with odds):")
        print(f"                     {'model':>8}  {'market':>8}")
        print(f"    3-way accuracy   {mkt_model_correct / mkt_n:8.4f}  {mkt_market_correct / mkt_n:8.4f}")
        print(f"    log loss         {mkt_model_ll / mkt_n:8.4f}  {mkt_market_ll / mkt_n:8.4f}")


async def _main() -> None:
    logging.basicConfig(level=logging.INFO)
    results = await load_results()
    for code, df in results.items():
        print(
            f"{LEAGUES[code]:<14} {len(df)} matches  "
            f"{df['Date'].min().date()} -> {df['Date'].max().date()}"
        )
    _backtest(results, datetime(2026, 3, 1))

    engine = build_engine()
    await engine.refresh_data()
    print(f"state written to {STATE_PATH}")

    games = await engine.upcoming_games()
    print(f"upcoming games: {len(games)}")
    for pred in await engine.predict(games):
        print(
            f"  {pred.game_id}: H {pred.home_win_prob:.3f} / D {pred.draw_prob:.3f} "
            f"/ A {pred.away_win_prob:.3f}  total {pred.predicted_total:.2f} "
            f"over2.5 {pred.over_prob:.3f}"
        )


if __name__ == "__main__":
    asyncio.run(_main())
