"""Tennis engine — surface-blended Elo over TennisMyLife (ATP) and
tennis-data.co.uk (WTA) match results.

v1 model ("tennis-elo-v1"): one overall EloBook per tour (ATP / WTA) plus one
EloBook per surface (Hard / Clay / Grass). Match win probability is the
logistic of a blended rating difference:

    diff = 0.7 * (overall rating diff) + 0.3 * (surface rating diff)
    P(player1 wins) = 1 / (1 + 10 ** (-diff / 400))

falling back to the overall diff alone when the surface is unknown. There is
no home advantage in tennis, so ``home``/``away`` on :class:`Game` simply mean
player1/player2. No totals are predicted (left ``None``).

Unknown players: when either player cannot be resolved against the ratings
book, we still emit a prediction of 0.5 / 0.5 with ``confidence=0.1`` so the
terminal renders a row rather than silently dropping the match. For resolved
matchups, ``confidence = 2 * |p - 0.5|`` (distance from a coin flip).

Data sources & licensing:

* **ATP** — TennisMyLife's match database (stats.tennismylife.org, formerly
  the Tennismylife/TML-Database GitHub repo), MIT licensed and updated daily.
  Per-year CSVs (``/data/{year}.csv``) carry completed tournaments in a
  Sackmann-compatible layout (tourney_date/surface/winner_name/loser_name/
  score, full player names, official ATP player IDs); the companion
  ``ongoing_tourneys.csv`` feed adds finished matches of tournaments still in
  progress, so ratings stay current mid-event. Files are cached under
  ``RAW_DIR/tennis/``; past-year files are kept forever, the current-year and
  ongoing files are re-downloaded when older than 24 hours, and download
  failures fall back to whatever cache exists.
* **WTA** — tennis-data.co.uk per-year result workbooks (the same xlsx files
  the odds benchmark uses: Date / Surface / Winner / Loser / closing odds in
  one file, cached under ``RAW_DIR/tennis/odds/`` with the same TTL rules).
  tennis-data publishes no formal license — the site states its data files
  are "completely FREE" / "free to use" (ad-funded). We conservatively treat
  them as personal-use-only and never redistribute the files: like our
  football-data.co.uk usage, each user's app downloads them at runtime; we
  never ship the data. Note the site updates in weekly-ish batches (during
  Wimbledon 2026 it sat ~2 weeks behind), so WTA ratings can lag live play
  more than the daily TML ATP feed.

WTA name keys: tennis-data abbreviates given names ("Alcaraz C." style), and
full-name recovery is impossible from that format, so WTA ratings are keyed
by :func:`wta_rating_key` — ``"surname|initials"`` (e.g. ``"sakkari|m"``,
``"pliskova|ka"``). ESPN's full display names are resolved onto those keys by
prefix-matching the given name against the stored initials (tennis-data's own
multi-letter disambiguation — "Pliskova Ka." vs "Pliskova Kr." — survives in
the key, so "Karolina Pliskova" still resolves uniquely). This is marginally
more collision-prone than the ATP full-name index; anything ambiguous
resolves to ``None`` and falls into the unknown-player 0.5 path. In the
2022-2026 files the only same-key clashes are spelling variants of a single
player (e.g. "Ruse E-G." / "Ruse E.G."), which the key deliberately merges.

Upcoming matches come from ESPN's tennis scoreboards, whose JSON differs from
team sports: each *event* is a tournament, matches live under
``event.groupings[].competitions[]`` and competitors are athletes. Surfaces
are inferred from tournament names (Wimbledon → Grass, etc., default Hard)
and remembered per game id for ``predict``.

Market benchmark (measurement only — no effect on predictions): the __main__
backtest scores the model head-to-head against historical closing odds from
tennis-data.co.uk. For WTA the odds live in the same rows as the results, so
each backtest record simply carries its own devigged closing probability.
For ATP the odds workbooks are separate from the TML results, so rows are
joined by normalized player-name keys (surname + first initial — tennis-data
uses "Alcaraz C." style) plus a date window: TML's ``tourney_date`` is the
*tournament start*, tennis-data's ``Date`` is the actual match date, so a row
matches when its date falls in [start - 1 day, start + 20 days]. Ambiguous
joins are dropped. Market probabilities are the devigged (proportionally
normalized) inverses of the best available decimal closing pair per row:
Pinnacle (PSW/PSL), else Bet365 (B365W/B365L), else the book average
(AvgW/AvgL).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
import sys
import time
import unicodedata
from dataclasses import dataclass, field
from pathlib import Path
from datetime import date, datetime, timedelta, timezone
from typing import Iterable, Optional

import httpx
import pandas as pd

from kryptscout.config import DATA_DIR, RAW_DIR
from kryptscout.core.elo import EloBook, EloConfig
from kryptscout.core.schemas import Game, Prediction, Sport
from kryptscout.sports.base import SportEngine

logger = logging.getLogger("kryptscout")

TOURS = ("ATP", "WTA")
SURFACES = ("Hard", "Clay", "Grass")
OVERALL_WEIGHT = 0.7
SURFACE_WEIGHT = 0.3
ELO_K = 32.0
DATA_FROM_YEAR = 2022
BACKTEST_FROM = 20250101  # tourney_date cutoff for validation metrics


def data_years(today: Optional[date] = None) -> range:
    """DATA_FROM_YEAR .. the current calendar year, inclusive.

    This was the literal range(2022, 2027): on 1 January 2027 the engine
    would have stopped reading new results for both tours.
    """
    today = today or datetime.now(timezone.utc).date()
    return range(DATA_FROM_YEAR, today.year + 1)


DATA_YEARS = data_years()

RAW_TENNIS_DIR = RAW_DIR / "tennis"
STATE_PATH = DATA_DIR / "tennis_state.json"
CSV_MAX_AGE_S = 24 * 3600  # current-year files only

# --- ATP results (TennisMyLife, MIT) ---------------------------------------
TML_YEAR_URL = "https://stats.tennismylife.org/data/{year}.csv"
TML_ONGOING_URL = "https://stats.tennismylife.org/data/ongoing_tourneys.csv"
# ATP Challenger results, same source / licence / layout (tourney_level "C").
# They feed the ratings only: a player arriving at tour level from the
# Challenger circuit then has a rating built on 2-3x more matches. Held out on
# 2025-01..2026-11 tour-level matches this cut log loss 0.6302 -> 0.6208.
TML_CHALLENGER_URL = "https://stats.tennismylife.org/data/{year}_challenger.csv"
CHALLENGER_LEVEL = "C"

# --- WTA results + closing odds (tennis-data.co.uk) -------------------------
ODDS_DIR = RAW_TENNIS_DIR / "odds"
# ATP odds benchmark only — aligned with BACKTEST_FROM
ODDS_YEARS = tuple(y for y in data_years() if y >= BACKTEST_FROM // 10000)
# ATP lives at /{year}/{year}.xlsx, WTA at /{year}w/{year}.xlsx
TENNIS_DATA_URL = "http://www.tennis-data.co.uk/{year}{suffix}/{year}.xlsx"
_ODDS_TOUR_SUFFIX = {"ATP": "", "WTA": "w"}
# Decimal closing-odds column pairs, tried per row in preference order.
ODDS_PRICE_PAIRS = (("PSW", "PSL"), ("B365W", "B365L"), ("AvgW", "AvgL"))
# tennis-data Date (actual match day) relative to TML tourney_date (tournament
# START day): allow 1 day of clock skew before, and up to 20 days after
# (Grand Slam finals land ~13 days into the event).
ODDS_JOIN_WINDOW_DAYS = (-1, 20)

ESPN_SCOREBOARD_URL = (
    "https://site.api.espn.com/apis/site/v2/sports/tennis/{tour}/scoreboard"
)

# ESPN grouping slug → tour. Doubles / mixed are skipped entirely.
_SINGLES_SLUGS = {"mens-singles": "ATP", "womens-singles": "WTA"}

# Tournament-name keywords → surface (case-insensitive substring match).
# Anything unmatched defaults to Hard, the most common tour surface.
_SURFACE_KEYWORDS: list[tuple[str, str]] = [
    ("wimbledon", "Grass"),
    ("halle", "Grass"),
    ("queen", "Grass"),
    ("cinch", "Grass"),
    ("eastbourne", "Grass"),
    ("hertogenbosch", "Grass"),
    ("libema", "Grass"),
    ("mallorca", "Grass"),
    ("newport", "Grass"),
    ("bad homburg", "Grass"),
    ("berlin", "Grass"),
    ("nottingham", "Grass"),
    ("birmingham", "Grass"),
    ("stuttgart", "Grass"),  # BOSS Open (June, grass)
    ("roland garros", "Clay"),
    ("french open", "Clay"),
    ("monte carlo", "Clay"),
    ("monte-carlo", "Clay"),
    ("madrid", "Clay"),
    ("rome", "Clay"),
    ("italian open", "Clay"),
    ("hamburg", "Clay"),
    ("barcelona", "Clay"),
    ("nordea", "Clay"),  # Båstad
    ("bastad", "Clay"),
    ("gstaad", "Clay"),
    ("umag", "Clay"),
    ("kitzbuhel", "Clay"),
    ("estoril", "Clay"),
    ("munich", "Clay"),
    ("geneva", "Clay"),
    ("lyon", "Clay"),
    ("houston", "Clay"),
    ("marrakech", "Clay"),
    ("bucharest", "Clay"),
    ("charleston", "Clay"),
    ("rabat", "Clay"),
    ("palermo", "Clay"),
    ("iasi", "Clay"),
]


def normalize_name(name: str) -> str:
    """Accent-stripped, lowercased, hyphen/period-free key for a player name."""
    decomposed = unicodedata.normalize("NFKD", name)
    ascii_name = decomposed.encode("ascii", "ignore").decode("ascii")
    ascii_name = ascii_name.replace("-", " ").replace(".", " ").replace("'", "")
    return " ".join(ascii_name.lower().split())


def initial_key(name: str) -> Optional[str]:
    """Fallback key ``"c alcaraz"`` — first initial + last token."""
    parts = normalize_name(name).split()
    if len(parts) < 2:
        return None
    return f"{parts[0][0]} {parts[-1]}"


def wta_rating_key(name: str) -> Optional[str]:
    """Rating key ``"sakkari|m"`` from a tennis-data name like ``"Sakkari M."``.

    Dot-marked tokens are given-name abbreviations; every abbreviated letter
    is kept ("Pliskova Ka." → "pliskova|ka", "Wang Xin." → "wang|xin",
    "Del Potro J.M." → "potro|jm") so tennis-data's own disambiguation of
    players sharing surname + first initial survives in the key. The surname
    part is the last non-abbreviated token after :func:`normalize_name`,
    which lines up with the last token of an ESPN full name ("Leyre Romero
    Gormaz" → "gormaz"). Returns None when the name has no dotted
    abbreviation (i.e. is not in tennis-data's format).
    """
    surname_tokens: list[str] = []
    initials: list[str] = []
    for token in name.split():
        letters = "".join(normalize_name(token).split())
        if not letters:
            continue
        if "." in token:
            initials.append(letters)
        else:
            surname_tokens.append(letters)
    if not surname_tokens or not initials:
        return None
    return f"{surname_tokens[-1]}|{''.join(initials)}"


def surface_for_tournament(tournament: str) -> str:
    lowered = tournament.lower()
    for keyword, surface in _SURFACE_KEYWORDS:
        if keyword in lowered:
            return surface
    return "Hard"


# --------------------------------------------------------- market benchmark
# Everything below down to _make_book is measurement-only: it never touches
# the Elo books or the prediction pathway.


def devig_decimal(dec_a: float, dec_b: float) -> tuple[float, float]:
    """Vig-free probabilities for a 2-way market quoted in decimal odds.

    Proportional (multiplicative) devigging, same convention as
    :func:`kryptscout.core.oddsmath.remove_vig` but for decimal prices.
    """
    if dec_a <= 1.0 or dec_b <= 1.0:
        raise ValueError("Decimal odds must exceed 1.0")
    raw_a, raw_b = 1.0 / dec_a, 1.0 / dec_b
    total = raw_a + raw_b
    return raw_a / total, raw_b / total


def tennis_data_key(name: str) -> Optional[str]:
    """Join key ``"alcaraz|c"`` from a tennis-data name like ``"Alcaraz C."``.

    tennis-data writes surname first, then dot-separated given-name initials
    ("Del Potro J.M.", "Auger-Aliassime F.").  After :func:`normalize_name`
    the initials become trailing 1-2 letter tokens (2 letters covers the
    disambiguated "Pliskova Ka." style); everything before them is the
    surname, of which we keep the last token to line up with full-name keys.
    Returns None when no initial can be found.
    """
    tokens = normalize_name(name).split()
    initials: list[str] = []
    while len(tokens) > 1 and len(tokens[-1]) <= 2:
        initials.insert(0, tokens.pop())
    if not initials:
        return None
    return f"{tokens[-1]}|{initials[0][0]}"


def sackmann_join_key(name: str) -> Optional[str]:
    """Join key ``"alcaraz|c"`` from a full name like ``"Carlos Alcaraz"``:
    last surname token + first-name initial (mirrors :func:`tennis_data_key`).
    """
    parts = normalize_name(name).split()
    if len(parts) < 2:
        return None
    return f"{parts[-1]}|{parts[0][0]}"


def _row_price_pair(row) -> Optional[tuple[float, float]]:
    """Best available (winner, loser) decimal closing pair for an odds row."""
    for col_w, col_l in ODDS_PRICE_PAIRS:
        try:
            dec_w = float(getattr(row, col_w))
            dec_l = float(getattr(row, col_l))
        except (AttributeError, TypeError, ValueError):
            continue
        if dec_w > 1.0 and dec_l > 1.0:  # NaN fails the comparison
            return dec_w, dec_l
    return None


def build_odds_index(
    odds: pd.DataFrame,
) -> dict[tuple[str, str], list[tuple[date, float, float]]]:
    """(winner_key, loser_key) -> [(match_date, dec_winner, dec_loser), ...]."""
    index: dict[tuple[str, str], list[tuple[date, float, float]]] = {}
    required = {"Date", "Winner", "Loser"}
    if odds.empty or not required.issubset(odds.columns):
        return index
    for row in odds.itertuples(index=False):
        if pd.isna(row.Date):
            continue
        try:
            when = pd.Timestamp(row.Date).date()
        except (TypeError, ValueError):
            continue
        w_key = tennis_data_key(str(row.Winner))
        l_key = tennis_data_key(str(row.Loser))
        prices = _row_price_pair(row)
        if w_key is None or l_key is None or prices is None:
            continue
        index.setdefault((w_key, l_key), []).append((when, *prices))
    return index


def _yyyymmdd_to_date(value: int) -> date:
    return date(value // 10000, (value // 100) % 100, value % 100)


def join_market(
    records: list[dict],
    odds_index: dict[tuple[str, str], list[tuple[date, float, float]]],
) -> list[dict]:
    """Attach devigged market win probabilities to backtest match records.

    ``records`` are replay backtest rows ({date, winner, loser, p}).  A record
    joins when exactly one unconsumed odds row shares its (winner, loser) name
    keys inside the date window — anything ambiguous (rematches in-window,
    colliding name keys) is dropped rather than guessed at.
    """
    lo, hi = ODDS_JOIN_WINDOW_DAYS
    joined: list[dict] = []
    used: set[tuple[str, str, int]] = set()
    for rec in records:
        w_key = sackmann_join_key(rec["winner"])
        l_key = sackmann_join_key(rec["loser"])
        if w_key is None or l_key is None:
            continue
        start = _yyyymmdd_to_date(rec["date"])
        candidates = [
            (i, row)
            for i, row in enumerate(odds_index.get((w_key, l_key), ()))
            if timedelta(days=lo) <= row[0] - start <= timedelta(days=hi)
            and (w_key, l_key, i) not in used
        ]
        if len(candidates) != 1:
            continue  # unmatched, or ambiguous — drop
        i, (_, dec_w, dec_l) = candidates[0]
        used.add((w_key, l_key, i))
        joined.append({**rec, "market_p": devig_decimal(dec_w, dec_l)[0]})
    return joined


def market_benchmark(joined: list[dict]) -> dict[str, float]:
    """Model-vs-market accuracy and log loss on the joined subset.

    Both ``p`` (model) and ``market_p`` are probabilities assigned to the
    actual winner, so accuracy is simply the share above 0.5, mirroring the
    replay convention.
    """
    n = len(joined)
    if n == 0:
        return {"n": 0}
    return {
        "n": n,
        "model_accuracy": sum(r["p"] > 0.5 for r in joined) / n,
        "model_log_loss": -sum(math.log(max(r["p"], 1e-12)) for r in joined) / n,
        "market_accuracy": sum(r["market_p"] > 0.5 for r in joined) / n,
        "market_log_loss": -sum(math.log(r["market_p"]) for r in joined) / n,
    }


async def download_tennis_data(tour: str, years: Iterable[int]) -> list[Path]:
    """Fetch/cache tennis-data.co.uk yearly workbooks for one tour.

    These serve double duty: WTA results (the primary WTA rating source,
    ``DATA_YEARS``) and ATP closing odds for the __main__ benchmark
    (``ODDS_YEARS``). Past-year files are cached forever; the current-year
    file is re-downloaded when older than 24h. Download failures fall back to
    any cached copy.
    """
    ODDS_DIR.mkdir(parents=True, exist_ok=True)
    current_year = datetime.now(timezone.utc).year
    out: list[Path] = []
    async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
        for year in years:
            path = ODDS_DIR / f"{tour.lower()}_{year}.xlsx"
            stale = (
                year >= current_year
                and path.exists()
                and time.time() - path.stat().st_mtime > CSV_MAX_AGE_S
            )
            if not path.exists() or stale:
                url = TENNIS_DATA_URL.format(
                    year=year, suffix=_ODDS_TOUR_SUFFIX[tour]
                )
                try:
                    resp = await client.get(url)
                    resp.raise_for_status()
                    path.write_bytes(resp.content)
                except httpx.HTTPError as exc:
                    # keep whatever cache we have, but leave a trace: a feed
                    # that refuses us freezes the ratings without any error
                    logger.warning("tennis-data download failed (%s): %s", url, exc)
            if path.exists():
                out.append(path)
    return out


def load_odds(paths: Iterable) -> pd.DataFrame:
    """Concatenate cached odds workbooks (requires openpyxl for xlsx)."""
    frames = [pd.read_excel(p) for p in paths]
    if not frames:
        return pd.DataFrame()
    return pd.concat(frames, ignore_index=True)


def _make_book() -> EloBook:
    return EloBook(config=EloConfig(k=ELO_K, home_advantage=0.0))


@dataclass
class TourBook:
    """Overall + per-surface Elo books for one tour, with a name index.

    ``abbreviated_names=True`` (WTA) switches to tennis-data-style canonical
    keys ("sakkari|m", see :func:`wta_rating_key`) resolved via
    ``abbrev_index``; otherwise canonical names are full names (ATP/TML)
    resolved via ``name_index`` with an initial-key fallback.
    """

    abbreviated_names: bool = False
    overall: EloBook = field(default_factory=_make_book)
    surfaces: dict[str, EloBook] = field(
        default_factory=lambda: {s: _make_book() for s in SURFACES}
    )
    # normalized full name -> canonical name (full-name tours)
    name_index: dict[str, str] = field(default_factory=dict)
    # "c alcaraz" style fallback; ambiguous keys are dropped (None sentinel)
    initial_index: dict[str, Optional[str]] = field(default_factory=dict)
    # surname -> {initials -> canonical key} (abbreviated tours)
    abbrev_index: dict[str, dict[str, str]] = field(default_factory=dict)

    def index_player(self, canonical: str) -> None:
        if self.abbreviated_names:
            surname, _, initials = canonical.partition("|")
            if surname and initials:
                self.abbrev_index.setdefault(surname, {})[initials] = canonical
            return
        self.name_index.setdefault(normalize_name(canonical), canonical)
        ikey = initial_key(canonical)
        if ikey is None:
            return
        if ikey in self.initial_index and self.initial_index[ikey] != canonical:
            self.initial_index[ikey] = None  # ambiguous — disable fallback
        else:
            self.initial_index.setdefault(ikey, canonical)

    def resolve(self, name: str) -> Optional[str]:
        if self.abbreviated_names:
            return self._resolve_abbreviated(name)
        canonical = self.name_index.get(normalize_name(name))
        if canonical is not None:
            return canonical
        ikey = initial_key(name)
        if ikey is not None:
            return self.initial_index.get(ikey)
        return None

    def _resolve_abbreviated(self, name: str) -> Optional[str]:
        """Resolve a rating key, tennis-data name, or ESPN full name.

        Candidates are bucket entries whose stored initials are a prefix of
        either the first given-name token ("karolina" matches "ka" but not
        "kr") or the given-name acronym ("Juan Martin del Potro" → "jmd"
        matches "jm"). Anything other than exactly one candidate is
        ambiguous → None (unknown-player 0.5 path).
        """
        if "|" in name:  # already a canonical rating key
            surname, _, first = name.partition("|")
            acronym = first
        else:
            key = wta_rating_key(name)
            if key is not None:  # tennis-data style ("Sakkari M.")
                surname, _, first = key.partition("|")
                acronym = first
            else:  # full name (ESPN style)
                parts = normalize_name(name).split()
                if len(parts) < 2:
                    return None
                surname, first = parts[-1], parts[0]
                acronym = "".join(p[0] for p in parts[:-1])
        if not surname or not first:
            return None
        candidates = {
            canonical
            for initials, canonical in self.abbrev_index.get(surname, {}).items()
            if first.startswith(initials) or acronym.startswith(initials)
        }
        if len(candidates) == 1:
            return next(iter(candidates))
        return None

    def match_prob(self, p1: str, p2: str, surface: Optional[str]) -> float:
        """P(p1 beats p2), canonical names, surface-blended when known."""
        diff = self.overall.get(p1) - self.overall.get(p2)
        if surface in self.surfaces:
            book = self.surfaces[surface]
            surface_diff = book.get(p1) - book.get(p2)
            diff = OVERALL_WEIGHT * diff + SURFACE_WEIGHT * surface_diff
        return 1.0 / (1.0 + 10.0 ** (-diff / 400.0))

    def record(self, winner: str, loser: str, surface: Optional[str]) -> None:
        self.index_player(winner)
        self.index_player(loser)
        self.overall.update(winner, loser, home_won=True)
        if surface in self.surfaces:
            self.surfaces[surface].update(winner, loser, home_won=True)


def _make_tour_book(tour: str) -> TourBook:
    return TourBook(abbreviated_names=(tour == "WTA"))


class TennisEngine(SportEngine):
    sport = Sport.TENNIS
    model_name = "tennis-elo-v1"

    def __init__(self) -> None:
        self.books: dict[str, TourBook] = {
            tour: _make_tour_book(tour) for tour in TOURS
        }
        self.backtest: dict[str, dict[str, float]] = {}
        # per-tour list of backtest rows ({date, winner, loser, p} and, for
        # WTA rows with usable closing prices, market_p) from the last replay
        # — feeds the market benchmark in __main__ only; never persisted,
        # never used by predict().
        self.backtest_log: dict[str, list[dict]] = {}
        # game id -> inferred surface, remembered from upcoming_games()
        self._game_surfaces: dict[str, str] = {}
        self._loaded = False

    # ------------------------------------------------------------------ data

    async def _download_atp_csvs(self) -> list[bytes]:
        """Fetch/cache TennisMyLife ATP CSVs (per-year + in-progress feed)."""
        RAW_TENNIS_DIR.mkdir(parents=True, exist_ok=True)
        current_year = datetime.now(timezone.utc).year
        targets: list[tuple[Path, str, bool]] = [
            (
                RAW_TENNIS_DIR / f"tml_{year}.csv",
                TML_YEAR_URL.format(year=year),
                year >= current_year,
            )
            for year in data_years()
        ]
        targets += [
            (
                RAW_TENNIS_DIR / f"tml_{year}_challenger.csv",
                TML_CHALLENGER_URL.format(year=year),
                year >= current_year,
            )
            for year in data_years()
        ]
        # Finished matches of tournaments still in progress; always volatile.
        targets.append((RAW_TENNIS_DIR / "tml_ongoing.csv", TML_ONGOING_URL, True))
        out: list[bytes] = []
        async with httpx.AsyncClient(timeout=60, follow_redirects=True) as client:
            for path, url, volatile in targets:
                stale = (
                    volatile
                    and path.exists()
                    and time.time() - path.stat().st_mtime > CSV_MAX_AGE_S
                )
                if not path.exists() or stale:
                    try:
                        resp = await client.get(url)
                        resp.raise_for_status()
                        path.write_bytes(resp.content)
                    except httpx.HTTPError as exc:
                        logger.warning("TennisMyLife download failed (%s): %s", url, exc)
                if path.exists():
                    out.append(path.read_bytes())
        return out

    @staticmethod
    def _load_matches(csvs: Iterable[bytes]) -> pd.DataFrame:
        """Concatenate raw CSVs into a chronologically sorted match frame."""
        import io

        frames = []
        cols = [
            "tourney_id",
            "tourney_level",
            "tourney_date",
            "match_num",
            "surface",
            "winner_name",
            "loser_name",
            "score",
        ]
        for raw in csvs:
            df = pd.read_csv(io.BytesIO(raw), usecols=lambda c: c in cols)
            frames.append(df)
        if not frames:
            return pd.DataFrame(columns=cols)
        matches = pd.concat(frames, ignore_index=True)
        matches = matches.dropna(subset=["winner_name", "loser_name"])
        if "score" in matches.columns:
            # Walkovers were never played — no rating signal.
            matches = matches[
                ~matches["score"].fillna("").str.contains("W/O", case=False)
            ]
        if {"tourney_id", "match_num"}.issubset(matches.columns):
            # The ongoing-tournaments feed overlaps the year file for up to a
            # cache-TTL once a tournament completes; year-file rows win.
            matches = matches.drop_duplicates(
                subset=["tourney_id", "match_num"], keep="first"
            )
        # Challenger rows move the ratings but are not part of the backtest
        # (the metrics and the odds benchmark stay tour-level, as before).
        if "tourney_level" in matches.columns:
            matches["scored"] = (
                matches["tourney_level"].astype(str).str.upper() != CHALLENGER_LEVEL
            )
        else:
            matches["scored"] = True
        sort_cols = ["tourney_date"]
        if "match_num" in matches.columns:
            sort_cols.append("match_num")
        return matches.sort_values(sort_cols, kind="stable").reset_index(drop=True)

    @staticmethod
    def _load_wta_results(paths: Iterable[Path]) -> pd.DataFrame:
        """tennis-data workbooks → replay frame keyed by :func:`wta_rating_key`.

        Output columns mirror :meth:`_load_matches` (``tourney_date`` int
        yyyymmdd — here the *actual match date* — ``match_num``, ``surface``,
        ``winner_name``, ``loser_name``) plus ``market_p``: the devigged
        closing probability of the actual winner (NaN when no usable price
        pair), which replay carries into backtest logs for the market
        benchmark. Walkover rows are dropped (never played); retirements stay,
        matching the ATP W/O-only filter.
        """
        out_cols = ["tourney_date", "match_num", "surface",
                    "winner_name", "loser_name", "market_p"]
        rows: list[dict] = []
        for path in paths:
            df = pd.read_excel(path)
            if not {"Date", "Winner", "Loser"}.issubset(df.columns):
                continue
            for row in df.itertuples(index=False):
                if "walkover" in str(getattr(row, "Comment", "")).lower():
                    continue
                if pd.isna(row.Date):
                    continue
                try:
                    when = pd.Timestamp(row.Date)
                except (TypeError, ValueError):
                    continue
                if when > pd.Timestamp.now() + pd.Timedelta(days=2):
                    continue  # mistyped date in the feed
                winner = wta_rating_key(str(row.Winner))
                loser = wta_rating_key(str(row.Loser))
                if winner is None or loser is None or winner == loser:
                    continue
                surface = getattr(row, "Surface", None)
                prices = _row_price_pair(row)
                rows.append({
                    "tourney_date": when.year * 10000 + when.month * 100 + when.day,
                    "surface": surface if surface in SURFACES else None,
                    "winner_name": winner,
                    "loser_name": loser,
                    "market_p": (
                        devig_decimal(*prices)[0] if prices else float("nan")
                    ),
                })
        frame = pd.DataFrame(rows, columns=out_cols)
        frame["match_num"] = range(len(frame))
        return frame.sort_values(
            ["tourney_date", "match_num"], kind="stable"
        ).reset_index(drop=True)

    def replay(self, tour: str, matches: pd.DataFrame) -> dict[str, float]:
        """Replay matches chronologically; backtest on BACKTEST_FROM onwards.

        Returns accuracy / log-loss of the *pre-update* blended probability
        assigned to the actual winner. Each scored row is also appended to
        ``self.backtest_log[tour]`` for the __main__ market benchmark; rows
        carrying a usable ``market_p`` column (WTA) keep it.

        A row whose tourney_date will not parse is skipped rather than fatal:
        the upstream feeds are hand-maintained and a single typo'd cell (e.g.
        "202s60807") would otherwise take the whole refresh down, freezing the
        ratings at their last good save.
        """
        book = self.books[tour]
        log = self.backtest_log[tour] = []
        n = correct = 0
        loss = 0.0
        skipped = 0
        for row in matches.itertuples(index=False):
            winner = str(row.winner_name)
            loser = str(row.loser_name)
            surface = getattr(row, "surface", None)
            surface = surface if surface in SURFACES else None
            try:
                tourney_date = int(row.tourney_date)
            except (TypeError, ValueError):
                skipped += 1
                continue
            if tourney_date >= BACKTEST_FROM and bool(getattr(row, "scored", True)):
                p = book.match_prob(winner, loser, surface)
                n += 1
                correct += p > 0.5
                loss -= math.log(max(p, 1e-12))
                entry = {"date": tourney_date, "winner": winner,
                         "loser": loser, "p": p}
                market_p = getattr(row, "market_p", None)
                if market_p is not None and not pd.isna(market_p):
                    entry["market_p"] = float(market_p)
                log.append(entry)
            book.record(winner, loser, surface)
        if skipped:
            logger.warning(
                "%s: skipped %d match(es) with an unparseable tourney_date",
                tour, skipped,
            )
        if n == 0:
            return {"n": 0, "accuracy": 0.0, "log_loss": 0.0}
        return {"n": n, "accuracy": correct / n, "log_loss": loss / n}

    async def refresh_data(self) -> None:
        """Download latest results, rebuild ratings from scratch, persist."""
        atp_csvs = await self._download_atp_csvs()
        wta_paths = await download_tennis_data("WTA", data_years())
        self.books = {tour: _make_tour_book(tour) for tour in TOURS}
        self.backtest = {
            "ATP": self.replay("ATP", self._load_matches(atp_csvs)),
            "WTA": self.replay("WTA", self._load_wta_results(wta_paths)),
        }
        self._save_state()
        self._loaded = True

    # ----------------------------------------------------------- persistence

    def _walk_forward_rows(self) -> list[list]:
        """5-field walk-forward rows [date, prob, outcome, side_a, side_b].

        Orientation is ALPHABETICAL (outcome-independent), so the Backtest
        panel's calibration stays honest and the dollar-ROI join
        (store/roibacktest.py) can match players to Polymarket outcomes.
        `date` is the tournament START date (Sackmann tourney_date) — the
        join allows day tolerance for multi-week events.
        """
        rows: list[list] = []
        for tour in TOURS:
            for rec in self.backtest_log.get(tour, []):
                raw = str(rec["date"])
                if len(raw) != 8:
                    continue
                iso = f"{raw[:4]}-{raw[4:6]}-{raw[6:]}"
                winner, loser, p = rec["winner"], rec["loser"], float(rec["p"])
                a, b = sorted((winner, loser))
                rows.append([iso, p if a == winner else 1.0 - p,
                             int(a == winner), a, b])
        rows.sort(key=lambda r: r[0])
        return rows

    def _save_state(self) -> None:
        state = {
            "model": self.model_name,
            "generated_at": datetime.now(timezone.utc).isoformat(),
            # Per-tour metrics under "metrics" (the MODELS panel reads it);
            # "backtest" is the per-match walk-forward log (panel + ROI join).
            "metrics": self.backtest,
            "backtest": self._walk_forward_rows(),
            "tours": {
                tour: {
                    "overall": self.books[tour].overall.ratings,
                    "surfaces": {
                        s: b.ratings for s, b in self.books[tour].surfaces.items()
                    },
                }
                for tour in TOURS
            },
        }
        STATE_PATH.write_text(json.dumps(state), encoding="utf-8")

    def _load_state(self) -> bool:
        if not STATE_PATH.exists():
            return False
        state = json.loads(STATE_PATH.read_text(encoding="utf-8"))
        # "metrics" is the tour-metrics dict; older states kept it under
        # "backtest" (which now holds the per-match log list instead).
        metrics = state.get("metrics")
        if not isinstance(metrics, dict):
            legacy = state.get("backtest")
            metrics = legacy if isinstance(legacy, dict) else {}
        self.backtest = metrics
        for tour in TOURS:
            payload = state.get("tours", {}).get(tour)
            if payload is None:
                return False
            book = _make_tour_book(tour)
            book.overall.ratings = dict(payload["overall"])
            for s in SURFACES:
                book.surfaces[s].ratings = dict(payload["surfaces"].get(s, {}))
            for player in book.overall.ratings:
                book.index_player(player)
            self.books[tour] = book
        return True

    async def _ensure_loaded(self) -> None:
        if self._loaded:
            return
        if self._load_state():
            self._loaded = True
        else:
            await self.refresh_data()

    # -------------------------------------------------------------- schedule

    async def upcoming_games(self, on: date | None = None) -> list[Game]:
        """Today's (or ``on``'s) scheduled/live singles matches from ESPN."""
        target = on or datetime.now(timezone.utc).date()
        payloads = []
        async with httpx.AsyncClient(timeout=30, follow_redirects=True) as client:
            for tour in ("atp", "wta"):
                url = ESPN_SCOREBOARD_URL.format(tour=tour)
                resp = await client.get(url, params={"dates": target.strftime("%Y%m%d")})
                resp.raise_for_status()
                payloads.append(resp.json())
        return self._parse_scoreboards(payloads, target)

    def _parse_scoreboards(self, payloads: list[dict], target: date) -> list[Game]:
        games: list[Game] = []
        seen: set[str] = set()
        for payload in payloads:
            for event in payload.get("events", []):
                tournament = event.get("name", "")
                for grouping in event.get("groupings", []):
                    slug = grouping.get("grouping", {}).get("slug", "")
                    league = _SINGLES_SLUGS.get(slug)
                    if league is None:
                        continue  # doubles / mixed
                    for comp in grouping.get("competitions", []):
                        game = self._parse_competition(comp, tournament, league, target)
                        if game is not None and game.id not in seen:
                            seen.add(game.id)
                            self._game_surfaces[game.id] = surface_for_tournament(
                                tournament
                            )
                            games.append(game)
        games.sort(key=lambda g: g.start_time)
        return games

    @staticmethod
    def _parse_competition(
        comp: dict, tournament: str, league: str, target: date
    ) -> Optional[Game]:
        state = comp.get("status", {}).get("type", {}).get("state")
        if state not in ("pre", "in"):
            return None
        try:
            start = datetime.strptime(comp["date"], "%Y-%m-%dT%H:%MZ").replace(
                tzinfo=timezone.utc
            )
        except (KeyError, ValueError):
            return None
        if start.date() != target:
            return None
        home = away = None
        for competitor in comp.get("competitors", []):
            name = (competitor.get("athlete") or {}).get("displayName")
            if not name or name.upper() == "TBD":
                return None
            if competitor.get("homeAway") == "home":
                home = name
            else:
                away = name
        if home is None or away is None:
            return None
        return Game(
            id=f"tennis-{comp['id']}",
            sport=Sport.TENNIS,
            league=league,
            start_time=start,
            home=home,
            away=away,
        )

    # ------------------------------------------------------------ prediction

    async def predict(self, games: list[Game]) -> list[Prediction]:
        await self._ensure_loaded()
        now = datetime.now(timezone.utc)
        predictions = []
        for game in games:
            book = self.books.get(game.league, self.books["ATP"])
            p1 = book.resolve(game.home)
            p2 = book.resolve(game.away)
            if p1 is None or p2 is None:
                prob, confidence = 0.5, 0.1  # unknown player fallback
            else:
                surface = self._game_surfaces.get(game.id, "Hard")
                prob = book.match_prob(p1, p2, surface)
                confidence = min(1.0, 2.0 * abs(prob - 0.5))
            predictions.append(
                Prediction(
                    game_id=game.id,
                    sport=Sport.TENNIS,
                    model=self.model_name,
                    generated_at=now,
                    home_win_prob=prob,
                    away_win_prob=1.0 - prob,
                    confidence=confidence,
                )
            )
        return predictions


def build_engine() -> TennisEngine:
    return TennisEngine()


async def _main() -> None:
    engine = build_engine()
    print("Refreshing data (TML ATP CSVs + tennis-data WTA workbooks, "
          "2022-2026) and replaying Elo...")
    await engine.refresh_data()
    for tour in TOURS:
        bt = engine.backtest[tour]
        print(
            f"  {tour}: backtest 2025+ n={bt['n']} "
            f"accuracy={bt['accuracy']:.4f} log_loss={bt['log_loss']:.4f}"
        )

    print("\nMarket benchmark vs tennis-data.co.uk closing odds "
          f"({ODDS_YEARS[0]}-{ODDS_YEARS[-1]}, PS > B365 > Avg, devigged)...")
    for tour in TOURS:
        records = engine.backtest_log.get(tour, [])
        if tour == "WTA":
            # Results and odds share rows — market_p rides along in the log.
            joined = [r for r in records if "market_p" in r]
            joined = [r for r in joined if r["date"] >= ODDS_YEARS[0] * 10000]
        else:
            odds = load_odds(await download_tennis_data(tour, ODDS_YEARS))
            if not records or odds.empty:
                print(f"  {tour}: no odds data available — benchmark skipped")
                continue
            joined = join_market(records, build_odds_index(odds))
        bench = market_benchmark(joined)
        if bench["n"] == 0:
            print(f"  {tour}: 0 matches joined — benchmark skipped")
            continue
        print(
            f"  {tour}: joined {bench['n']}/{len(records)} backtest matches "
            f"({bench['n'] / len(records):.1%})"
        )
        print(f"    MODEL  accuracy={bench['model_accuracy']:.4f} "
              f"log_loss={bench['model_log_loss']:.4f}")
        print(f"    MARKET accuracy={bench['market_accuracy']:.4f} "
              f"log_loss={bench['market_log_loss']:.4f}")

    print("\nFetching today's slate from ESPN...")
    games = await engine.upcoming_games()
    if not games:
        print("  no scheduled singles matches today")
        return
    predictions = await engine.predict(games)
    for game, pred in zip(games, predictions):
        print(
            f"  [{game.league}] {game.start_time:%H:%M}Z  "
            f"{game.home} vs {game.away}  "
            f"p1={pred.home_win_prob:.3f} p2={pred.away_win_prob:.3f} "
            f"conf={pred.confidence:.2f}  ({game.id})"
        )


if __name__ == "__main__":
    sys.exit(asyncio.run(_main()))
