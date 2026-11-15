"""Auto-betting engine — buys an exchange price that sits under the sharp fair.

WHAT IT BETS. For every upcoming game on the boards, on every exchange the
user switched on (autobet/venues.py: Polymarket, Kalshi, Polymarket US), it
asks one question: does this exchange sell a side for less than ANOTHER
exchange says that side is worth, after this exchange's own fee? The
reference is the sharp fair (core/fair.py) and it is never the venue's own
quote — a venue cannot be cheap against itself. Tier STRONG means Kalshi set
the reference, LEAN means Polymarket did (the same meaning the tiers carry on
the boards). The candidate is then put through every money rule below.

It needs no forecast and no model: the engines' own probabilities do not beat
the close (measured, repeatedly) and are not read here.

WHAT TO EXPECT. Exchanges are arbitraged against each other all day, so a gap
that clears the fee and the EV floor is uncommon and most sweeps place
nothing; `last_sweep.closest` shows how near the best price came. Crossing
the spread has measured at roughly minus one to minus three percent on this
kind of pick. The floor is what stands between that and a bet — dry run is the
default, and stays the default.

Until 2026-11 this tailed the recorded value picks, which since 2026-09-02
are SPORTSBOOK prices: every one was skipped as `not_polymarket_priced` and
the engine placed nothing at all. Order mechanics per venue live behind
autobet/venues.py; game↔market matching is each venue's own odds-board
matcher, so the engine can never bet a market the board wouldn't quote.

Money rules (all enforced per sweep):

  EV floor       the order's limit price is the price at which the pick's
                 NET EV — after Polymarket's sports taker fee, rate 0.05 ×
                 p(1−p) per share (see SPORTS_TAKER_FEE_RATE / _limit_cents)
                 — would fall to `min_ev`, capped at `max_prob`. Orders are
                 FAK (fill-and-kill) at that cap — they cross whatever the
                 book offers at-or-better and the remainder is killed; a
                 zero fill is a clean skip, never a resting order.
  model_prob     the reference exchange's fair probability for the side
                 (historical column name: nothing here is a model). The EV a
                 row carries is fair / ask − 1 at the venue's quoted ask,
                 before fees; the limit price below is what nets the fee out.
  fees           the fee rate comes from the market's own Gamma feeSchedule
                 (live source of truth; falls back to SPORTS_TAKER_FEE_RATE)
                 and is persisted per row so grading survives rate changes.
  depth          the live ask book is read before committing: no crossable
                 depth at the limit skips, thin depth shrinks the order to
                 the executable size (never inflates), and every evaluated
                 candidate's ask/depth is logged (book_observations) as
                 ground truth for the backtest's fill model.
  fills          placed orders reconcile their REAL fill from the trades
                 feed (fill_shares/fill_prob) — a partial FAK fill must not
                 grade as a full fill in P&L or the stops.
  stake          fixed mode: flat `stake_usd` per pick; percent mode:
                 `stake_pct`% of the live cash balance (read once per
                 sweep; a dry run without a readable wallet sizes against
                 the PAPER_BANKROLL_USD fallback instead). shares =
                 floor(stake / limit); sub-minimum orders are refused
                 upstream (never inflated).
  stops          realized-P&L halts, each 0 = off: `daily_stop_loss_usd`
                 (today's realized loss), `stop_loss_usd` (lifetime loss),
                 `stop_profit_usd` (lifetime take-profit). A breached stop
                 halts the whole sweep before anything is scanned.
  dedupe         one live/dry bet per GAME ever, across every venue;
                 failed attempts (error / unmatched) may retry up to
                 MAX_ATTEMPTS. An order that was sent and never answered is
                 recorded `unknown` and holds its game exactly like a placed
                 one — it may be live on the exchange.
  daily cap      at most `max_bets_per_day` placed+dry rows per UTC day.
  best edge      `best_edge_only` ranks every eligible pick by EDGE (model
                 prob − market-implied prob) before a single order is
                 priced, so the day's cap funds the sharpest picks on the
                 board rather than whichever the board listed first.
                 Everything the cap can't reach is skipped as
                 'not_best_edge'. Ranking is per sweep — a bigger edge
                 surfacing later in the day cannot reclaim a slot already
                 spent, so pair it with a tight `max_hours_ahead` to keep
                 each sweep looking at one comparable slate.
  window         games starting within `lead_minutes` are skipped (too
                 close) and games past `max_days_ahead` days are skipped
                 (too far — day-ahead odds go stale before kickoff).
  dry_run        records what WOULD have been bet (status 'dry'), no order,
                 nothing logged to the tracker. Paper rows only exist where a
                 real FAK would have filled: the live ask must cross the
                 EV-floor limit, and the fill is priced by walking the
                 crossable ask levels (persisted as the row's fill_prob) —
                 never at the limit cap. An unreadable book skips the paper
                 bet (dry mode fails CLOSED; live mode stays fail-open — its
                 safety is the FAK cap itself). Dry rows grade W/L/P with
                 paper P&L in recent_log once the game settles, but never
                 count toward the real-money stops. Defaults ON; `enabled`
                 defaults OFF — two explicit switches before real money.

A PLACED order is also logged to user_bets (tracker.log_bet, note
"AUTO · polymarket") at stake_usd/unit_usd units, so autobets grade into
MY BETS alongside hand-tracked bets when games settle.

Settings persist at DATA_DIR/autobet.json — plain JSON, no secrets (wallet
material lives in polymarket_auth's encrypted credential store).
"""
from __future__ import annotations

import asyncio
import json
import logging
import math
from datetime import date, datetime, timedelta, timezone
from typing import Any, Optional

from kryptscout import license
from kryptscout.autobet import polymarket_api as api
from kryptscout.autobet import polymarket_auth as auth
from kryptscout.autobet import venues as venues_mod
from kryptscout.config import DATA_DIR
from kryptscout.core import fair as fair_mod
from kryptscout.core.oddsmath import american_to_decimal, prob_to_american
from kryptscout.core.schemas import Game, GameCard, Sport, is_in_play
from kryptscout.jobs import capture as capture_job
from kryptscout.odds import polymarket
from kryptscout.sports import base as engines
from kryptscout.store import db, tracker
from kryptscout.store.picks import NO_VALUE_SPORTS

logger = logging.getLogger("kryptscout")

# sha256 of the raw Ed25519 public key kryptscout.license verifies tokens with.
#
# Deliberately a second copy — api/main.py holds the same constant. Two compiled
# modules asserting the same fact separately means a replacement license module
# has to satisfy both call sites, not one, and neither of them takes the license
# module's word for its own authenticity. Rotate the signing key and BOTH copies
# change (test_license_gate.py fails if they drift).
_LICENSE_PUBKEY_SHA256 = "2a15d4a1c0498bb563ad21fa92d60a11e9d8c92e8ad1a8df53287711c7293983"


def _live_license_ok() -> bool:
    """The gate for placing real orders.

    Two conditions that fail for different reasons: the license module must be
    the genuine one (it carries the real verifying key), and it must hold a
    valid token. `is_licensed()` on its own was the whole gate, and the whole
    crack was replacing the file that answers it — a stub returning True needs
    no key, no token and no understanding of any of this.

    Deliberately fails CLOSED on any error. This gate only ever blocks placing
    new money; dry-run, the board, open positions and settlement all run
    regardless, so a false negative here costs a user some bets they can place
    by hand, never access to their own funds."""
    try:
        if license.public_key_fingerprint() != _LICENSE_PUBKEY_SHA256:
            logger.warning("autobet: license module failed its integrity check")
            return False
        return license.is_licensed()
    except Exception:  # noqa: BLE001 — a module that throws here is not the real one
        logger.warning("autobet: license module could not be verified")
        return False


SETTINGS_FILE = DATA_DIR / "autobet.json"

DEFAULT_SETTINGS: dict[str, Any] = {
    "enabled": False,       # master switch — OFF until the user flips it
    "dry_run": True,        # paper mode: record would-be bets, place nothing
    # "fixed" ($ per pick) | "percent" (flat % of balance) |
    # "confidence" (% of balance scaled by model confidence)
    "stake_mode": "fixed",
    "stake_usd": 10.0,      # flat spend per pick (fixed mode)
    "stake_pct": 2.0,       # % of cash balance per pick (percent mode)
    "stake_pct_min": 2.0,   # % at the low-confidence end (confidence mode)
    "stake_pct_max": 8.0,   # % at the high-confidence end (confidence mode)
    "kelly_mult": 0.25,     # fraction of full Kelly (kelly mode)
    "kelly_cap_pct": 5.0,   # hard cap on any kelly stake, % of balance
    "unit_usd": 10.0,       # $ per tracker unit (for the user_bets log)
    "min_ev": 0.04,         # EV floor per $1 — also sets the limit price
    "min_win_prob": 0.0,    # skip picks the model rates below this (0 = off)
    "min_edge": 0.0,        # skip picks whose model_prob − market_prob < this (0 = off)
    # Which reference to trust: STRONG = the fair came from Kalshi,
    # LEAN = from Polymarket (the weaker lead, measured). Both on: with only
    # STRONG, Kalshi itself can never be bought (nothing to measure it against),
    # and which reference to trust is the user's call on the AUTO-BET page.
    "tiers": ["STRONG", "LEAN"],
    # Exchanges the engine may buy on. Paper mode needs no account on any of
    # them; live mode skips a venue that is not connected.
    "venues": ["polymarket", "kalshi", "polymarket_us"],
    # Kalshi keeps cash per exchange shard and game markets are not on the one
    # a deposit lands on: let a live order move its own shortfall across.
    "kalshi_auto_move": True,
    # Bets asked for by a connected AI agent (autobet/agent.py). Independent of
    # `enabled` / `dry_run` above: off | paper | live, with its own caps.
    "agent_mode": "off",
    "agent_max_usd": 10.0,
    "agent_max_bets_per_day": 3,
    "sports": [],           # sport allowlist; empty = every sport
    "best_edge_only": False,  # rank the slate by edge; only the best get bet
    "max_bets_per_day": 5,  # placed+dry rows per UTC day
    "min_prob": 0.0,        # never buy below this price — longshot filter (0 = off)
    "max_prob": 0.85,       # never buy above this price (fee/upside drag)
    "lead_minutes": 10,     # skip games starting within this window
    "max_days_ahead": 7,    # never bet games starting further out than this
    "max_hours_ahead": 0,   # tighter window in hours (0 = use max_days_ahead)
    # Halts on REALIZED autobet P&L (see realized_pnl). 0 = off.
    "daily_stop_loss_usd": 0.0,    # today's realized loss reaches this → halt today
    "daily_stop_profit_usd": 0.0,  # today's realized profit reaches this → halt today
    "stop_loss_usd": 0.0,          # lifetime realized loss reaches this → halt
    "stop_profit_usd": 0.0,        # lifetime realized profit reaches this → halt
}

# Config keys that make up a reusable STRATEGY (everything the presets/profiles
# capture). Excludes the arm switches (enabled/dry_run) and the logging-only
# unit_usd so applying a strategy tunes the rules without ever going live.
STRATEGY_KEYS: tuple[str, ...] = (
    "stake_mode", "stake_usd", "stake_pct", "stake_pct_min", "stake_pct_max",
    "kelly_mult", "kelly_cap_pct",
    "min_ev", "min_win_prob", "min_edge", "tiers", "sports", "best_edge_only",
    "max_bets_per_day",
    "min_prob", "max_prob", "lead_minutes", "max_days_ahead", "max_hours_ahead",
    "daily_stop_loss_usd", "daily_stop_profit_usd", "stop_loss_usd",
    "stop_profit_usd",
)

# error/unmatched attempts per (game, side) before the engine gives up.
MAX_ATTEMPTS = 3

# Claimed pick EV above this is treated as a model artifact and skipped —
# the live record shows claimed EV anti-selects: 7/12–7/21 graded picks ran
# +6.3u at EV<0.10 (52% wr) but NEGATIVE in every bucket above 0.10 (EV≥0.30
# won just 30%). Genuine sports-market mispricings are small.
MAX_CLAIMED_EV = 0.12

# Bankroll a dry-run sweep sizes percent/confidence/kelly stakes against when
# no wallet balance is readable (walletless installs must still paper-trade —
# the 7/18 build sat at scanned=0 for days because the sweep halted here).
PAPER_BANKROLL_USD = 1000.0

# Polymarket sports taker fee (sports_fees_v2, verified live on Gamma
# 2026-07-16: rate 0.05, takerOnly — raised from 0.03 on 2026-07-10). Fee per
# share = rate * p * (1 - p) at execution price p, so per dollar staked it is
# rate * (1 - p) — material vs a 4% EV floor. The limit-price math nets it out.
# FALLBACK ONLY: the sweep reads each market's own Gamma feeSchedule (the live
# source of truth — Polymarket mutates it in place when rates change) and only
# uses this constant when the schedule is missing or unparseable.
SPORTS_TAKER_FEE_RATE = 0.05

_sweep_lock = asyncio.Lock()
_last_sweep: Optional[dict] = None


# ------------------------------------------------------------------ settings


def _sanitize(raw: dict, base: Optional[dict] = None) -> dict[str, Any]:
    """Clamp every knob to a sane range; unknown keys are dropped. Invalid
    values fall back to `base` (the previously saved settings on a patch —
    a bad value must never silently reset a knob to its default)."""
    out = dict(base or DEFAULT_SETTINGS)
    if not isinstance(raw, dict):
        return out
    out["enabled"] = raw.get("enabled") is True
    out["dry_run"] = raw.get("dry_run") is not False  # default ON
    def _num(key: str, lo: float, hi: float) -> None:
        try:
            value = float(raw[key])
        except (KeyError, TypeError, ValueError):
            return
        if math.isfinite(value):
            out[key] = min(max(value, lo), hi)
    _num("stake_usd", 1.0, 1000.0)
    _num("stake_pct", 0.1, 50.0)
    _num("stake_pct_min", 0.1, 50.0)
    _num("stake_pct_max", 0.1, 50.0)
    _num("kelly_mult", 0.05, 1.0)
    _num("kelly_cap_pct", 0.5, 25.0)
    _num("unit_usd", 1.0, 1000.0)
    _num("min_ev", 0.0, 0.5)
    _num("min_win_prob", 0.0, 0.99)
    _num("min_edge", 0.0, 0.5)
    _num("min_prob", 0.0, 0.95)
    _num("max_prob", 0.05, 0.99)
    _num("max_bets_per_day", 1, 100)
    _num("lead_minutes", 0, 720)
    _num("max_days_ahead", 1, 30)
    _num("max_hours_ahead", 0, 720)
    _num("daily_stop_loss_usd", 0.0, 1_000_000.0)
    _num("daily_stop_profit_usd", 0.0, 1_000_000.0)
    _num("stop_loss_usd", 0.0, 1_000_000.0)
    _num("stop_profit_usd", 0.0, 1_000_000.0)
    _num("agent_max_usd", 1.0, 1000.0)
    _num("agent_max_bets_per_day", 1, 50)
    out["agent_max_bets_per_day"] = int(out["agent_max_bets_per_day"])
    if raw.get("agent_mode") in ("off", "paper", "live"):
        out["agent_mode"] = raw["agent_mode"]
    out["max_bets_per_day"] = int(out["max_bets_per_day"])
    out["lead_minutes"] = int(out["lead_minutes"])
    out["max_days_ahead"] = int(out["max_days_ahead"])
    out["max_hours_ahead"] = int(out["max_hours_ahead"])
    # Keep the confidence band ordered so low ≤ high regardless of input order.
    if out["stake_pct_min"] > out["stake_pct_max"]:
        out["stake_pct_min"], out["stake_pct_max"] = out["stake_pct_max"], out["stake_pct_min"]
    if raw.get("stake_mode") in ("fixed", "percent", "confidence", "kelly"):
        out["stake_mode"] = raw["stake_mode"]
    # Absent = leave as-is (base/default), so a strategy patch that predates
    # this knob can't silently flip it. Present = strict bool.
    if "best_edge_only" in raw:
        out["best_edge_only"] = raw["best_edge_only"] is True
    if "kalshi_auto_move" in raw:
        out["kalshi_auto_move"] = raw["kalshi_auto_move"] is not False
    wanted_venues = raw.get("venues")
    if isinstance(wanted_venues, list):
        chosen = [v for v in venues_mod.VENUE_KEYS if v in wanted_venues]
        if chosen:  # an empty list would mean "bet nowhere": keep what was saved
            out["venues"] = chosen
    tiers = raw.get("tiers")
    if isinstance(tiers, list):
        wanted = [t for t in ("STRONG", "LEAN") if t in tiers]
        if wanted:
            out["tiers"] = wanted
    sports = raw.get("sports")
    if isinstance(sports, list):
        valid = ("nba", "nhl", "nfl", "mlb", "wnba", "ncaaf", "ncaab",
                 "soccer", "tennis")
        # Empty list is meaningful (= all sports), unlike tiers.
        out["sports"] = [s for s in sports if s in valid]
    return out


def load_settings() -> dict[str, Any]:
    try:
        raw = json.loads(SETTINGS_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return dict(DEFAULT_SETTINGS)
    # A file with no "venues" predates the multi-exchange rule. Its "tiers"
    # were value-pick tiers (a different thing with the same names), so they
    # are not a choice about references: take the current default instead.
    if isinstance(raw, dict) and "venues" not in raw:
        raw = {k: v for k, v in raw.items() if k != "tiers"}
    return _sanitize(raw)


def save_settings(patch: dict) -> dict[str, Any]:
    """Merge a partial update into the saved settings and return the result."""
    current = load_settings()
    merged = {**current, **(patch or {})}
    settings = _sanitize(merged, base=current)
    tmp = SETTINGS_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(settings, indent=2), encoding="utf-8")
    tmp.replace(SETTINGS_FILE)
    return settings


# ----------------------------------------------------------- presets/profiles

PROFILES_FILE = DATA_DIR / "autobet_profiles.json"

# Built-in strategies (read-only). Each is a full STRATEGY_KEYS patch; applying
# one leaves enabled/dry_run untouched, so a preset tunes rules, never arms.
PRESETS: dict[str, dict[str, Any]] = {
    # None of these is a validated strategy: they are starting points for the
    # floors and caps, and none has a positive measured record. (The old
    # "Validated WNBA" preset was fitted to the engines' own probabilities,
    # which this engine no longer reads; it was removed with them.)
    #
    # Only the sharpest few plays on the board each day. The whole slate is
    # ranked by edge and the tiny cap funds the top of it — the tight hours
    # window keeps every sweep looking at one comparable slate, which is what
    # makes a per-sweep ranking mean "best of the day".
    "Best Edge": {
        "stake_mode": "confidence", "stake_pct_min": 1.0, "stake_pct_max": 5.0,
        "min_ev": 0.05, "min_win_prob": 0.0, "min_edge": 0.04,
        "tiers": ["STRONG", "LEAN"], "sports": [],
        "best_edge_only": True,
        "max_bets_per_day": 3, "max_prob": 0.85,
        "lead_minutes": 15, "max_days_ahead": 1, "max_hours_ahead": 12,
        "daily_stop_loss_usd": 0.0, "daily_stop_profit_usd": 0.0,
        "stop_loss_usd": 0.0, "stop_profit_usd": 0.0,
    },
    "Conservative": {
        "stake_mode": "confidence", "stake_pct_min": 1.0, "stake_pct_max": 4.0,
        "min_ev": 0.06, "min_win_prob": 0.60, "min_edge": 0.04,
        "tiers": ["STRONG"], "sports": [], "best_edge_only": False,
        "max_bets_per_day": 3, "max_prob": 0.80,
        "lead_minutes": 15, "max_days_ahead": 2, "max_hours_ahead": 0,
        "daily_stop_loss_usd": 0.0, "daily_stop_profit_usd": 0.0,
        "stop_loss_usd": 0.0, "stop_profit_usd": 0.0,
    },
    "Balanced": {
        "stake_mode": "confidence", "stake_pct_min": 2.0, "stake_pct_max": 8.0,
        "min_ev": 0.04, "min_win_prob": 0.55, "min_edge": 0.03,
        "tiers": ["STRONG", "LEAN"], "sports": [], "best_edge_only": False,
        "max_bets_per_day": 5, "max_prob": 0.85,
        "lead_minutes": 10, "max_days_ahead": 3, "max_hours_ahead": 0,
        "daily_stop_loss_usd": 0.0, "daily_stop_profit_usd": 0.0,
        "stop_loss_usd": 0.0, "stop_profit_usd": 0.0,
    },
    "Aggressive": {
        "stake_mode": "confidence", "stake_pct_min": 3.0, "stake_pct_max": 12.0,
        "min_ev": 0.02, "min_win_prob": 0.50, "min_edge": 0.0,
        "tiers": ["STRONG", "LEAN"], "sports": [], "best_edge_only": False,
        "max_bets_per_day": 12, "max_prob": 0.90,
        "lead_minutes": 5, "max_days_ahead": 7, "max_hours_ahead": 0,
        "daily_stop_loss_usd": 0.0, "daily_stop_profit_usd": 0.0,
        "stop_loss_usd": 0.0, "stop_profit_usd": 0.0,
    },
}


def _strategy_of(settings: dict) -> dict[str, Any]:
    """Just the STRATEGY_KEYS slice of a settings dict (JSON-plain values)."""
    return {k: settings[k] for k in STRATEGY_KEYS if k in settings}


def load_profiles() -> dict[str, dict]:
    """User-saved named strategies from PROFILES_FILE ({name: patch})."""
    try:
        raw = json.loads(PROFILES_FILE.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}
    if not isinstance(raw, dict):
        return {}
    # Keep only strategy keys per profile; drop anything malformed.
    return {
        str(name): {k: v for k, v in patch.items() if k in STRATEGY_KEYS}
        for name, patch in raw.items()
        if isinstance(patch, dict)
    }


def _write_profiles(profiles: dict[str, dict]) -> None:
    tmp = PROFILES_FILE.with_suffix(".json.tmp")
    tmp.write_text(json.dumps(profiles, indent=2), encoding="utf-8")
    tmp.replace(PROFILES_FILE)


def list_strategies() -> dict:
    """Presets + user profiles, each sanitized so the UI can preview them.

    Every strategy is run through _sanitize (over defaults) so a stale saved
    profile still returns clamped, complete values.
    """
    def _clean(patch: dict) -> dict:
        return _strategy_of(_sanitize(patch, base=dict(DEFAULT_SETTINGS)))
    return {
        "presets": [{"name": n, "settings": _clean(p)} for n, p in PRESETS.items()],
        "profiles": [{"name": n, "settings": _clean(p)} for n, p in load_profiles().items()],
    }


def save_profile(name: str, patch: Optional[dict] = None) -> dict:
    """Save a named strategy. `patch` defaults to the current live settings.

    Presets are read-only; a profile may not shadow a preset name. Returns the
    updated strategy list.
    """
    name = (name or "").strip()
    if not name:
        raise ValueError("profile name required")
    if name in PRESETS:
        raise ValueError(f"'{name}' is a built-in preset")
    source = patch if patch is not None else load_settings()
    profiles = load_profiles()
    profiles[name] = _strategy_of(_sanitize(source, base=dict(DEFAULT_SETTINGS)))
    _write_profiles(profiles)
    return list_strategies()


def delete_profile(name: str) -> dict:
    """Remove a user profile (presets can't be deleted). Returns the list."""
    profiles = load_profiles()
    profiles.pop(name, None)
    _write_profiles(profiles)
    return list_strategies()


def apply_strategy(name: str) -> dict[str, Any]:
    """Merge a preset or saved profile's rules into the live settings.

    Only STRATEGY_KEYS are applied — enabled/dry_run/wallet are never touched,
    so applying a strategy tunes the engine without arming it. Returns the new
    full settings.
    """
    patch = PRESETS.get(name) or load_profiles().get(name)
    if patch is None:
        raise KeyError(name)
    return save_settings({k: v for k, v in patch.items() if k in STRATEGY_KEYS})


# -------------------------------------------------------------------- status


def _today_counts(conn, day: date, rules_only: bool = False) -> dict[str, Any]:
    """Today's attempts by status. `rules_only` leaves out bets an AI agent
    asked for (autobet/agent.py): those have their own daily allowance, so
    they must not use up the rule engine's."""
    rows = db.rows(
        conn,
        f"""
        SELECT status, COUNT(*) AS n, COALESCE(SUM(usd), 0) AS usd
        FROM auto_bets WHERE date(created_at) = ?
        {"AND COALESCE(tier, '') != 'AGENT'" if rules_only else ""}
        GROUP BY status
        """,
        (day.isoformat(),),
    )
    by_status = {r["status"]: r["n"] for r in rows}
    return {
        "placed": by_status.get("placed", 0),
        "dry": by_status.get("dry", 0),
        "unmatched": by_status.get("unmatched", 0),
        "errors": by_status.get("error", 0),
        "unknown": by_status.get("unknown", 0),
        "spent_usd": sum(
            r["usd"] for r in rows if r["status"] == "placed"
        ),
    }


def _grade_settled(r: dict) -> tuple[str, float]:
    """(W/L/P result, realized P&L $) for one auto-bet row on a SETTLED game.

    A winning share pays $1: win = shares − usd − fee, loss = −usd − fee,
    push/abandoned = 0 (same outcome rules as the tracker —
    tracker._result_for_side is THE outcome truth). Grades on the recorded
    fill (fill_shares/fill_prob — the reconciled trades-feed fill for placed
    orders, the saved executable ask for dry rows) when present; rows without
    one fall back to intended shares at the limit (conservative: stops fire
    early, never late). The taker fee (rate × p(1−p) per share) is charged at
    fill, win or lose; legacy rows without a persisted fee_rate fall back to
    their fee era (3% before the 2026-07-10 sports_fees_v2 change, 5% after).
    Needs the auto_bets row joined with the game's completed/scores.
    """
    filled = r["fill_shares"] is not None and r["fill_prob"] is not None
    shares = r["fill_shares"] if filled else r["shares"]
    q = float(r["fill_prob"] if filled else (r["limit_prob"] or 0.5))
    usd = round(shares * q, 2) if filled else r["usd"]
    result = tracker._result_for_side(r)
    venue = venues_mod.get(r.get("venue"))
    if r["fee_rate"] is not None:
        rate = r["fee_rate"]
    elif venue.key != venues_mod.LEGACY_VENUE:
        rate = venue.fallback_fee_rate
    else:
        rate = 0.05 if r["created_at"][:10] >= "2026-07-10" else 0.03
    fee = venue.fee_usd(shares, q, rate)
    pnl = (shares - usd - fee) if result == "W" else (
        -usd - fee if result == "L" else 0.0
    )
    return result, pnl


def realized_pnl(conn) -> dict:
    """Realized USD P&L over PLACED auto bets, from settled game results
    (grading rules: _grade_settled). Dry rows never count here — this feeds
    the real-money stops.
    `today_usd` keys off the bet's created_at UTC date (the day the money
    was risked); `open_usd` is the spend still riding on unsettled games.
    """
    rows = db.rows(
        conn,
        """
        SELECT a.usd, a.shares, a.side, a.created_at, a.limit_prob,
               a.fill_shares, a.fill_prob, a.fee_rate, a.venue,
               g.sport, g.completed, g.home_score, g.away_score, g.settled_at
        FROM auto_bets a
        JOIN games g ON g.game_id = a.game_id
        WHERE a.status = 'placed'
        """,
    )
    today = datetime.now(timezone.utc).date().isoformat()
    total = today_usd = open_usd = 0.0
    for r in rows:
        if r["settled_at"] is None:
            filled = r["fill_shares"] is not None and r["fill_prob"] is not None
            open_usd += (
                round(r["fill_shares"] * float(r["fill_prob"]), 2)
                if filled else r["usd"]
            )
            continue
        pnl = _grade_settled(r)[1]
        total += pnl
        if r["created_at"][:10] == today:
            today_usd += pnl
    return {
        "today_usd": round(today_usd, 2),
        "total_usd": round(total, 2),
        "open_usd": round(open_usd, 2),
    }


def _stop_reason(settings: dict, pnl: dict) -> Optional[str]:
    """Which stop (if any) halts the engine right now. 0 disables a stop."""
    if settings["daily_stop_loss_usd"] > 0 and pnl["today_usd"] <= -settings["daily_stop_loss_usd"]:
        return "daily_stop_loss"
    if settings.get("daily_stop_profit_usd", 0) > 0 and pnl["today_usd"] >= settings["daily_stop_profit_usd"]:
        return "daily_stop_profit"
    if settings["stop_loss_usd"] > 0 and pnl["total_usd"] <= -settings["stop_loss_usd"]:
        return "stop_loss"
    if settings["stop_profit_usd"] > 0 and pnl["total_usd"] >= settings["stop_profit_usd"]:
        return "stop_profit"
    return None


def get_status() -> dict:
    """Settings + wallet + today's activity + realized P&L. Local-only — no
    network (the UI polls this; balance/readiness go through the explicit
    test)."""
    conn = db.connect()
    try:
        today = _today_counts(conn, datetime.now(timezone.utc).date())
        pnl = realized_pnl(conn)
    finally:
        conn.close()
    try:
        wallet = auth.credentials_status()
    except Exception as exc:  # noqa: BLE001 — status must never 500
        wallet = {"hasWalletKey": False, "error": str(exc)}
    settings = load_settings()
    return {
        "settings": settings,
        "wallet": wallet,
        "venues": venues_mod.status(),
        "today": today,
        "pnl": pnl,
        "stopped": _stop_reason(settings, pnl),
        "last_sweep": _last_sweep,
    }


def drift() -> dict:
    """Live-vs-backtest drift: is the autobet record tracking expectations?

    Live side: every graded auto bet (dry + placed) on a settled game, win
    rate per tracker outcome rules. Expected side: the dollar-ROI backtest
    replayed at the CURRENT gates over each sport present in the live record
    (pessimistic fill). A live record far below expectation is the alarm
    that an edge died — or that the backtest was flattering.
    """
    from kryptscout.store import roibacktest

    settings = load_settings()
    conn = db.connect()
    try:
        rows = db.rows(
            conn,
            """
            SELECT a.side, a.sport, g.completed, g.home_score, g.away_score
            FROM auto_bets a JOIN games g ON g.game_id = a.game_id
            WHERE a.status IN ('placed', 'dry') AND g.settled_at IS NOT NULL
            """,
        )
    finally:
        conn.close()
    graded = [r for r in rows if tracker._result_for_side(r) in ("W", "L")]
    wins = sum(1 for r in graded if tracker._result_for_side(r) == "W")
    sports = sorted({r["sport"] for r in graded})

    expected = []
    for sport in sports:
        try:
            joined = roibacktest.joined_rows(sport, min_volume=5000.0)
            sim = roibacktest.simulate(
                joined["rows"], fill="pessimistic",
                min_ev=settings["min_ev"], min_win_prob=settings["min_win_prob"],
                min_edge=settings["min_edge"], min_prob=settings["min_prob"],
                max_prob=settings["max_prob"],
                # The engine refuses claimed EV above this, so the expectation
                # it is graded against must refuse it too — otherwise drift
                # measures live against a bucket it was never allowed to bet
                # (and the one the live record shows losing money).
                max_ev=MAX_CLAIMED_EV,
                max_bets_per_day=settings["max_bets_per_day"],
            )
            if sim["n_bets"] >= 30:
                expected.append({"sport": sport, "win_rate": sim["win_rate"],
                                 "roi_pct": sim["roi_pct"], "n": sim["n_bets"]})
        except Exception:  # noqa: BLE001 — drift is advisory, never a 500
            continue
    exp_rate = (
        sum(e["win_rate"] * e["n"] for e in expected)
        / sum(e["n"] for e in expected)
        if expected else None
    )
    live_rate = wins / len(graded) if graded else None
    verdict = None
    if live_rate is not None and exp_rate is not None and len(graded) >= 20:
        gap = live_rate - exp_rate
        if gap < -0.08:
            verdict = "Live record is running well below backtest expectation — the edge may be dying; keep stakes small."
        elif gap > 0.08:
            verdict = "Live record is running above expectation — likely variance, don't scale up because of it."
        else:
            verdict = "Live record is tracking the backtest — the system is behaving as measured."
    elif graded and exp_rate is None:
        # Enough live bets, but no comparable backtest at the CURRENT gates
        # (each sport needs 30+ simulated bets). Saying "not enough live bets"
        # here blames the wrong side of the comparison.
        verdict = (
            f"{len(graded)} graded live bets, but the backtest has no "
            "comparable sample at these gates — nothing to compare against "
            "yet. Loosen the gates or run the history backfill."
        )
    elif graded:
        verdict = f"Only {len(graded)} graded live bets — drift needs ~20+ to mean anything."
    return {
        "live": {"n_graded": len(graded), "wins": wins, "win_rate": live_rate},
        "expected": {"win_rate": exp_rate, "by_sport": expected},
        "verdict": verdict,
    }


def recent_log(limit: int = 50) -> list[dict]:
    """Newest auto-bet attempts, for the UI log table.

    Dry AND placed rows on settled games carry `result` (W/L/P) and
    `pnl_usd` — paper bets grade exactly like real ones (fees included, on
    the saved executable fill), so the dry-run log is an honest preview of
    live performance rather than a pile of forever-open "dry run" rows.
    """
    conn = db.connect()
    try:
        attempts = db.rows(
            conn,
            """
            SELECT a.*, g.home, g.away, g.start_time AS kickoff,
                   g.completed, g.home_score, g.away_score, g.settled_at
            FROM auto_bets a
            LEFT JOIN games g ON g.game_id = a.game_id
            ORDER BY a.id DESC LIMIT ?
            """,
            (max(1, min(int(limit), 500)),),
        )
    finally:
        conn.close()
    for r in attempts:
        r["venue"] = r.get("venue") or venues_mod.LEGACY_VENUE
        graded = r["status"] in ("placed", "dry") and r["settled_at"] is not None
        result, pnl = _grade_settled(r) if graded else (None, None)
        r["result"] = result
        r["pnl_usd"] = round(pnl, 2) if pnl is not None else None
    return attempts


async def wallet_check() -> dict:
    """Explicit (network) readiness probe: trading-ready checks + balance."""
    ready = await api.check_trading_ready()
    try:
        balance = await api.get_balance()
    except Exception as exc:  # noqa: BLE001 — ready info is still useful
        balance = {"error": str(exc)}
    return {"ready": ready, "balance": balance}


# --------------------------------------------------------------------- sweep


def _model_prob(price: int, ev: float) -> Optional[float]:
    """Recover the model's win probability from a recorded pick: the pick
    stores its price and EV, and EV = p·d − 1 for decimal price d."""
    try:
        decimal = american_to_decimal(int(price))
    except (TypeError, ValueError):
        return None
    if decimal <= 1.0:
        return None
    prob = (1.0 + float(ev)) / decimal
    return prob if 0.0 < prob < 1.0 else None


def _implied_prob(price: int) -> Optional[float]:
    """The market's raw implied probability (1/decimal) for American odds."""
    try:
        decimal = american_to_decimal(int(price))
    except (TypeError, ValueError):
        return None
    return 1.0 / decimal if decimal > 1.0 else None


def _market_edge(price: int, model_prob: float) -> Optional[float]:
    """Edge in probability points: model_prob − market-implied prob (1/decimal).

    The pick's `price` is the book's American odds; the market's raw implied
    probability is 1/decimal. A positive edge means the model rates the side
    higher than the price implies.
    """
    implied = _implied_prob(price)
    return model_prob - implied if implied is not None else None


def _kelly_fraction(p: float, q: float) -> float:
    """Full-Kelly stake fraction for a binary at price q with win prob p.

    Net odds per $1 staked: b = (1−q)/q. f* = p − (1−p)/b. Zero when the bet
    has no edge — Kelly sizes by EDGE, so it accelerates gains only where the
    edge is real (and losses where it's imagined; hence the fractional
    multiplier + hard cap in the settings).
    """
    if q <= 0.0 or q >= 1.0:
        return 0.0
    b = (1.0 - q) / q
    return max(p - (1.0 - p) / b, 0.0)


def _confidence_pct(model_prob: float, lo_pct: float, hi_pct: float) -> float:
    """Stake % scaled linearly with model confidence across [0.50, 0.90].

    A coin-flip (0.50) stakes lo_pct; a near-lock (≥0.90) stakes hi_pct;
    in-between interpolates. Clamped so probs outside the band saturate.
    """
    t = (model_prob - 0.50) / 0.40
    t = min(max(t, 0.0), 1.0)
    return lo_pct + t * (hi_pct - lo_pct)


def _limit_cents(
    model_prob: float,
    min_ev: float,
    max_prob: float,
    fee_rate: float = SPORTS_TAKER_FEE_RATE,
) -> Optional[int]:
    """The FAK price cap in cents: the price at which NET EV falls to min_ev.

    Net EV per $1 at price q with model prob m and taker fee rate r:
        m/q - 1 - r*(1 - q)          (fee per share = r*q*(1-q); shares = 1/q)
    Setting that equal to min_ev gives  r*q² - (1+min_ev+r)*q + m = 0, solved
    for the smaller root. Cheaper fills only improve net EV (dEV/dq < 0 in
    range), so capping at this q is the binding worst case. r=0 reduces to the
    old gross formula m/(1+min_ev).
    """
    if fee_rate > 0.0:
        b = 1.0 + min_ev + fee_rate
        disc = b * b - 4.0 * fee_rate * model_prob
        if disc <= 0.0:
            return None
        limit = (b - math.sqrt(disc)) / (2.0 * fee_rate)
    else:
        limit = model_prob / (1.0 + min_ev)
    limit = min(limit, max_prob)
    cents = int(limit * 100)  # floor — never round UP past the EV floor
    return cents if 1 <= cents <= 99 else (99 if cents > 99 else None)


def _paper_fill_prob(book: dict, shares: int, limit_prob: float) -> float:
    """The price a FAK for `shares` would actually pay: share-weighted across
    the book's crossable ask levels (best first). Books without level detail
    fill at the best ask; a malformed book falls back to the limit cap."""
    remaining = float(shares)
    cost = filled = 0.0
    for level in book.get("levels") or []:
        try:
            px, sz = float(level[0]), float(level[1])
        except (TypeError, ValueError, IndexError):
            continue
        take = min(sz, remaining)
        if take <= 0.0:
            continue
        cost += take * px
        filled += take
        remaining -= take
        if remaining <= 1e-9:
            break
    if filled > 0.0:
        return cost / filled
    ask = book.get("best_ask_cents")
    return ask / 100.0 if isinstance(ask, (int, float)) else limit_prob


def _fee_rate_from_meta(meta: Optional[dict],
                        fallback: float = SPORTS_TAKER_FEE_RATE) -> float:
    """The taker-fee rate to gate EV with, from the market's OWN fee schedule
    (Polymarket: Gamma feeSchedule, see polymarket_api._fee_schedule_from_gamma;
    Kalshi: the series' fee multiplier; Polymarket US: feeCoefficient).
    `fallback` is the venue's standing rate.

    Only an affirmative schedule is trusted: enabled with exponent 1 → its
    rate; explicitly fee-free → 0. Anything else (missing, unparseable,
    unknown exponent — the _limit_cents quadratic is only valid for
    exponent 1) falls back to SPORTS_TAKER_FEE_RATE, because a Gamma glitch
    must never silently drop the fee cushion."""
    schedule = (meta or {}).get("fee_schedule")
    if not isinstance(schedule, dict):
        return fallback
    if schedule.get("enabled") is False:
        return 0.0
    rate = schedule.get("rate")
    exponent = schedule.get("exponent", 1.0)
    try:
        if (schedule.get("enabled") and isinstance(rate, (int, float))
                and float(exponent) == 1.0 and 0.0 <= float(rate) <= 0.5):
            rate = float(rate)
            if abs(rate - fallback) > 1e-9:
                logger.warning(
                    "live fee schedule rate %.3f differs from the standing "
                    "rate %.3f — the constant is stale",
                    rate, fallback,
                )
            return rate
    except (TypeError, ValueError):
        pass
    return fallback


def _observe_book(conn, row: dict, ref, book: Optional[dict],
                  limit_cents: Optional[int], now: datetime) -> None:
    """Ground-truth log for fill-model calibration: the pick's (stale) book
    price vs the venue's live best ask + crossable depth, for EVERY candidate
    that resolved a market — skips and dry runs included. Best-effort: a
    logging failure must never touch the money path."""
    try:
        with conn:
            db.insert_book_observation(
                conn,
                sport=row["sport"], game_id=row["game_id"],
                condition_id=ref.condition_id, clob_side=ref.clob_side,
                pick_prob=_implied_prob(row["price"]),
                # `is not None`, not truthiness — a genuine 0.0 venue prob is
                # data, and logging it as NULL corrupts the fill-model sample.
                live_prob=(
                    float(ref.prob) if getattr(ref, "prob", None) is not None
                    else None
                ),
                best_ask_cents=book["best_ask_cents"] if book else None,
                depth_shares=book["depth_shares"] if book else None,
                limit_cents=limit_cents, at=now,
            )
    except Exception:  # noqa: BLE001
        logger.debug("book observation log failed", exc_info=True)


async def _reconcile_fill(
    conn, bet_id: int, order_id: str, venue_key: Optional[str] = None
) -> Optional[tuple[float, float]]:
    """Read the CLOB trades feed for one placed order and persist the REAL
    fill (total shares + share-weighted avg price). Without this, a partial
    FAK fill grades as a full fill — inflating P&L and firing stops late.

    Returns the recorded (shares, avg_prob), or None when nothing has landed
    yet (feed lag — retried next sweep). Shares stay FRACTIONAL: Polymarket
    fills in fractional shares, and rounding a 7.5-share partial up to 8
    invents a share of P&L on every partial. SQLite's INTEGER affinity keeps
    a non-integral value as REAL, so no migration is needed."""
    try:
        fill = await venues_mod.get(venue_key).fill(order_id)
    except Exception as exc:  # noqa: BLE001 — reconcile is best-effort
        logger.debug("fill reconcile failed for %s: %s", order_id, exc)
        return None
    if fill is None:
        return None
    if fill[0] <= 0:
        # The exchange says the order ended with nothing filled: there is no
        # bet. Release the game instead of grading a position that never was.
        with conn:
            conn.execute(
                "UPDATE auto_bets SET status = 'unmatched', error = ? WHERE id = ?",
                ("The exchange ended this order with nothing filled.", bet_id),
            )
        return None
    shares, prob = round(fill[0], 4), round(fill[1], 4)
    with conn:
        db.set_auto_bet_fill(conn, bet_id, shares, prob)
    return shares, prob


# On a placed row whose exchange accepted the order without saying how it
# ended. The row holds its game and its slot; MY BETS hears about it only once
# the fill is confirmed.
PENDING_NOTE = "awaiting fill confirmation"


async def _reconcile_pending_fills(conn, limit: int = 10) -> None:
    """Placed rows still missing a recorded fill (the trades feed can lag a
    just-matched FAK): retry a bounded recent batch each sweep, giving up on
    orders older than a week (feed pagination is shallow by then)."""
    cutoff = (datetime.now(timezone.utc) - timedelta(days=7)).isoformat()
    pending = db.rows(
        conn,
        """
        SELECT id, order_id, created_at, venue, error, game_id, side, limit_prob
        FROM auto_bets
        WHERE status = 'placed' AND order_id IS NOT NULL
          AND fill_shares IS NULL
        ORDER BY id DESC LIMIT ?
        """,
        (max(1, int(limit)),),
    )
    for r in pending:
        if r["created_at"] < cutoff:
            continue
        if not venues_mod.get(r["venue"]).connected():
            continue
        fill = await _reconcile_fill(conn, r["id"], r["order_id"], r["venue"])
        if fill is not None and r["error"] == PENDING_NOTE:
            with conn:
                conn.execute("UPDATE auto_bets SET error = NULL WHERE id = ?", (r["id"],))
            _log_to_tracker(r, int(round(float(r["limit_prob"]) * 100)),
                            round(fill[0] * fill[1], 2), load_settings())


def _attempts(conn, game_id: str, side: str) -> tuple[int, bool]:
    """(attempts on this side, game already committed).

    Committed is GAME-level — a dry or placed row on ANY side blocks the
    whole game, so two models disagreeing can never buy both sides of the
    same market (guaranteed vig loss). Attempt-counting stays side-level.

    NOTE: this only sees rows already in the DB. Two picks on one game inside
    a SINGLE sweep are both un-committed when pass 1 scores them, so pass 2
    re-checks against `committed_games` (which it grows as it records) — see
    _sweep_locked. Both guards are needed: this one across sweeps, that one
    within a sweep."""
    rows = db.rows(
        conn,
        "SELECT side, status FROM auto_bets WHERE game_id = ?",
        (game_id,),
    )
    committed = any(r["status"] in ("placed", "dry", "unknown") for r in rows)
    attempts = sum(1 for r in rows if r["side"] == side)
    return attempts, committed


def _pick_game(row: dict) -> Game:
    """Minimal Game for resolve_market, straight from the pick's joined row."""
    return Game(
        id=row["game_id"],
        sport=Sport(row["sport"]),
        league=row.get("league") or "",
        start_time=datetime.fromisoformat(row["kickoff"]),
        home=row["home"],
        away=row["away"],
    )


def _record(conn, row: dict, ref, *, limit_prob: float, shares: int, usd: float,
            model_prob: Optional[float], order_id: Optional[str], status: str,
            error: Optional[str], now: datetime,
            best_ask_cents: Optional[int] = None,
            book_depth: Optional[float] = None,
            fee_rate: Optional[float] = None) -> int:
    venue = (ref.venue if ref is not None and getattr(ref, "venue", None)
             else row.get("venue") or venues_mod.LEGACY_VENUE)
    with conn:
        return db.insert_auto_bet(
            conn,
            game_id=row["game_id"], sport=row["sport"], side=row["side"],
            team=row["team"], tier=row["tier"], ev=row.get("ev"),
            model_prob=model_prob,
            condition_id=ref.condition_id if ref else "",
            clob_side=ref.clob_side if ref else "",
            slug=ref.slug if ref else None,
            limit_prob=limit_prob, shares=shares, usd=usd,
            order_id=order_id, status=status, error=error, created_at=now,
            best_ask_cents=best_ask_cents, book_depth=book_depth,
            fee_rate=fee_rate, venue=venue, ref_book=row.get("ref_book"),
        )


# ----------------------------------------------------------------- candidates

# A captured board younger than this is priced as-is; older, the sweep builds
# its own. The scheduler captures first and sweeps right behind it.
CARD_MAX_AGE_S = 240.0
# Polymarket US is not an odds source on the boards, so pricing it costs one
# search per game (cached ten minutes). Soonest games first, and no more than
# this many a sweep: a full Saturday of college games must not turn a sweep
# into several hundred requests.
PMUS_MAX_LOOKUPS = 80
CLOSEST_SHOWN = 6


async def _load_cards(sports: list[Sport]) -> list[GameCard]:
    """The current boards for these sports — the last capture's when fresh."""
    cards: list[GameCard] = []
    for sport in sports:
        board = capture_job.recent_cards(sport, CARD_MAX_AGE_S)
        if board is None:
            try:
                board = await capture_job.build_cards(sport, True)
            except Exception as exc:  # noqa: BLE001 — one sport must not sink the sweep
                logger.warning("autobet: could not build the %s board: %s", sport.value, exc)
                continue
        cards.extend(board)
    return cards


def _reference(card: GameCard, venue) -> Optional[tuple[str, dict[str, float], bool]]:
    """(reference exchange, fair probability per side, thin?) for pricing
    `venue` on this game — never the venue's own quote. None when no OTHER
    exchange prices the game."""
    fair = card.fair
    if fair is None or not fair.sharp:
        return None
    if fair.source != venue.book:
        probs = {"home": fair.home, "away": fair.away}
        if fair.draw is not None:
            probs["draw"] = fair.draw
        return fair.source, probs, bool(fair.thin)
    if fair.draw is not None:
        return None  # `sources` carries the home side only; a 3-way needs all three
    for book in fair_mod.REFERENCE_ORDER:
        if book in fair_mod.EXCHANGES and book != venue.book and book in fair.sources:
            home = float(fair.sources[book])
            quote = next((q for q in card.odds if q.book == book), None)
            thin = quote is None or fair_mod.is_thin(quote)
            return book, {"home": home, "away": 1.0 - home}, thin
    return None


def _net_ev(fair_prob: float, ask: float, fee_rate: float) -> float:
    """Per $1 staked at price `ask`: fair/ask − 1 − fee_rate·(1 − ask)."""
    return fair_prob / ask - 1.0 - fee_rate * (1.0 - ask)


async def _scan(now: datetime, settings: dict, lead: timedelta, horizon: timedelta,
                skip, summary: dict) -> list[dict]:
    """Every (game, side, venue) an enabled exchange quotes against another
    exchange's fair, as rows the eligibility pass reads. No order is priced
    and nothing is written here."""
    wanted = [venues_mod.VENUES[key] for key in settings["venues"] if key in venues_mod.VENUES]
    allow = set(settings["sports"])
    sports = [
        engine.sport for engine in engines.all_engines()
        if engine.sport.value not in NO_VALUE_SPORTS
        and (not allow or engine.sport.value in allow)
    ]
    cards = sorted(await _load_cards(sports), key=lambda c: c.game.start_time)
    scan = {"games": 0, "with_reference": 0, "quotes": {v.key: 0 for v in wanted}}
    rows: list[dict] = []
    lookups = 0
    for card in cards:
        game = card.game
        if getattr(game, "completed", False):
            continue
        # Out-of-window games still become rows where the board already
        # carries the quote (the eligibility pass reports them as
        # out_of_window); they are never worth a network lookup.
        in_window = now + lead < game.start_time <= now + horizon
        if in_window:
            scan["games"] += 1
        priced = False
        for venue in wanted:
            ref = _reference(card, venue)
            if ref is None:
                continue
            ref_book, probs, thin = ref
            priced = True
            tier = "STRONG" if ref_book == "kalshi" else "LEAN"
            quote = None
            if venue.book is not None:
                quote = next(
                    (q for q in card.odds
                     if q.book == venue.book and not is_in_play(q, game.start_time)),
                    None,
                )
                if quote is None:
                    continue
            elif not in_window:
                continue
            elif lookups >= PMUS_MAX_LOOKUPS:
                skip("lookup_budget")
                continue
            for side, fair_prob in probs.items():
                market = None
                if quote is not None:
                    price = getattr(quote, f"{side}_ml")
                    ask = _implied_prob(price) if price is not None else None
                else:
                    if side == "home":
                        lookups += 1
                    try:
                        market = await venue.resolve(game.sport, game, side)
                    except Exception as exc:  # noqa: BLE001 — discovery is best-effort
                        logger.debug("autobet: %s lookup failed: %s", venue.key, exc)
                        market = None
                    ask = market.prob if market is not None else None
                    price = prob_to_american(ask) if ask is not None and 0.0 < ask < 1.0 else None
                if ask is None or price is None or not (0.0 < ask < 1.0):
                    continue
                scan["quotes"][venue.key] += 1
                rows.append({
                    "game_id": game.id, "sport": game.sport.value,
                    "league": game.league or "", "kickoff": game.start_time.isoformat(),
                    "home": game.home, "away": game.away, "side": side,
                    "team": game.home if side == "home" else game.away if side == "away" else "Draw",
                    "tier": tier, "model": f"sharp-fair:{ref_book}",
                    "book": venue.key, "venue": venue.key, "ref_book": ref_book,
                    "price": int(price), "ask": ask, "fair_prob": float(fair_prob),
                    "ev": float(fair_prob) / ask - 1.0,
                    "net_ev": _net_ev(float(fair_prob), ask, venue.fallback_fee_rate),
                    "thin_reference": thin, "_market": market,
                })
        if priced and in_window:
            scan["with_reference"] += 1
    # The order the money pass walks when it is not ranking by edge: the
    # stronger reference first, then the bigger gap.
    rows.sort(key=lambda r: (r["tier"] != "STRONG", -r["ev"]))
    summary["scan"] = scan
    summary["closest"] = [
        {
            "game_id": r["game_id"], "sport": r["sport"], "team": r["team"],
            "venue": r["venue"], "ref_book": r["ref_book"], "tier": r["tier"],
            "ask": round(r["ask"], 4), "fair_prob": round(r["fair_prob"], 4),
            "net_ev": round(r["net_ev"], 4), "kickoff": r["kickoff"],
        }
        for r in sorted(
            (r for r in rows if not r["thin_reference"] and r["tier"] in settings["tiers"]),
            key=lambda r: r["net_ev"], reverse=True,
        )[:CLOSEST_SHOWN]
    ]
    return rows


async def sweep(force: bool = False) -> dict:
    """One pass over the current boards: place (or dry-run) qualifying bets.

    Runs after every capture when `enabled`; `force=True` (the RUN NOW
    button) runs even while disabled but still honors dry_run and every
    money rule. Serialized by a lock — concurrent sweeps can't double-bet.

    Returns a summary: {scanned, placed, dry, unmatched, errors, skipped:
    {reason: n}} — stored as last_sweep for the status endpoint.
    """
    global _last_sweep
    async with _sweep_lock:
        summary = await _sweep_locked(force)
        _last_sweep = summary
        mode = "live" if not summary.get("dry_run") else "dry-run"
        skipped = ", ".join(f"{k}×{v}" for k, v in summary.get("skipped", {}).items())
        logger.info(
            "autobet sweep done [%s]: scanned=%s placed=%s dry=%s unmatched=%s "
            "errors=%s unknown=%s%s",
            mode, summary.get("scanned", 0), summary.get("placed", 0),
            summary.get("dry", 0), summary.get("unmatched", 0),
            summary.get("errors", 0), summary.get("unknown", 0),
            f" skipped: {skipped}" if skipped else "",
        )
        return summary


async def _sweep_locked(force: bool) -> dict:
    now = datetime.now(timezone.utc)
    settings = load_settings()
    summary: dict[str, Any] = {
        "at": now.isoformat(), "scanned": 0, "placed": 0, "dry": 0,
        "unmatched": 0, "errors": 0, "unknown": 0, "skipped": {},
        "dry_run": settings["dry_run"],
    }

    def skip(reason: str) -> None:
        summary["skipped"][reason] = summary["skipped"].get(reason, 0) + 1

    if not settings["enabled"] and not force:
        summary["skipped"]["disabled"] = 1
        return summary
    live_venues = [
        key for key in settings["venues"] if venues_mod.get(key).connected()
    ]
    if not settings["dry_run"] and not live_venues:
        logger.warning("autobet sweep skipped: live mode but no exchange account is connected")
        summary["skipped"]["no_wallet"] = 1
        return summary
    venues_mod.VENUES["kalshi"].auto_move_funds = bool(settings["kalshi_auto_move"])
    # LICENSE GATE — live betting requires an active Krypt Scout license.
    # FUNDS-SAFETY CARVE-OUT: this gates ONLY the placing of new live orders.
    # Dry-run sweeps, the board, MY BETS, and settlement are never gated, so a
    # lapsed license can never trap money or hide open Polymarket positions.
    #
    # Checked here independently of the API middleware, and through the token
    # payload rather than the module's one-word summary of it — see
    # _live_license_ok. Two gates that fail for different reasons are two things
    # to find; `is_licensed()` in both places was one.
    if not settings["dry_run"] and not _live_license_ok():
        logger.warning("autobet sweep skipped: no active Krypt Scout license (live mode)")
        summary["skipped"]["unlicensed"] = 1
        return summary
    logger.info(
        "autobet sweep start [%s]%s: venues=%s tiers=%s stake_mode=%s min_ev=%.3f "
        "max/day=%s window=%s..%sd",
        "live" if not settings["dry_run"] else "dry-run",
        " (forced RUN NOW)" if force else "",
        ",".join(settings["venues"]),
        ",".join(settings["tiers"]), settings["stake_mode"], settings["min_ev"],
        settings["max_bets_per_day"], settings["lead_minutes"] / 60.0,
        settings["max_days_ahead"],
    )

    # Users cap how far out the engine may commit money (prices that far
    # ahead are stale by kickoff); the scan below gates on actual start time.
    today = now.date()
    days_ahead = int(settings["max_days_ahead"])
    lead = timedelta(minutes=settings["lead_minutes"])
    # max_hours_ahead tightens the window to an hours horizon when set (>0);
    # otherwise the day-granularity max_days_ahead applies.
    max_hours = int(settings.get("max_hours_ahead", 0))
    horizon = timedelta(hours=max_hours) if max_hours > 0 else timedelta(days=days_ahead)

    conn = db.connect()
    try:
        # Reconcile lagging fills FIRST so the stops below grade on the real
        # numbers, not the intended-shares assumption.
        if live_venues:
            try:
                await _reconcile_pending_fills(conn)
            except Exception:  # noqa: BLE001 — advisory, never blocks a sweep
                logger.debug("pending fill reconcile failed", exc_info=True)

        # Stops fire on REALIZED P&L before anything else — a breached stop
        # halts the whole sweep (dry-run included: the engine is stopped).
        pnl = realized_pnl(conn)
        summary["pnl"] = pnl
        stop = _stop_reason(settings, pnl)
        if stop is not None:
            logger.warning(
                "autobet halted by %s: today=%.2f total=%.2f open=%.2f",
                stop, pnl["today_usd"], pnl["total_usd"], pnl["open_usd"],
            )
            summary["skipped"][stop] = 1
            return summary

        # Sizing. fixed → flat $/pick; percent/confidence → % of live cash
        # balance (read once per sweep). Live mode with no readable balance
        # halts — there's nothing honest to size real money against. A dry
        # run falls back to a paper bankroll instead: a walletless install
        # must keep recording paper bets, not silently scan nothing.
        # Confidence mode scales the % between stake_pct_min and
        # stake_pct_max by the pick's model_prob, so the actual $ is
        # resolved per pick inside the loop.
        mode = settings["stake_mode"]
        # Cash per venue, read once per sweep and only for a venue an order is
        # about to be sized on. None = unreadable in live mode.
        cash: dict[str, Optional[float]] = {}

        async def cash_for(venue) -> Optional[float]:
            if mode == "fixed":
                return 0.0
            if venue.key not in cash:
                try:
                    cash[venue.key] = float(await venue.balance_usd())
                except Exception as exc:  # noqa: BLE001 — no balance, no sizing
                    if settings["dry_run"]:
                        cash[venue.key] = PAPER_BANKROLL_USD
                        logger.info(
                            "autobet: %s balance unavailable (%s) — dry run sizing "
                            "against $%.0f paper bankroll",
                            venue.label, exc, PAPER_BANKROLL_USD,
                        )
                    else:
                        logger.warning("autobet: %s balance read failed: %s", venue.label, exc)
                        cash[venue.key] = None
            return cash[venue.key]

        def stake_for(model_prob: float, limit_prob: float = 0.5,
                      cash_usd: float = PAPER_BANKROLL_USD) -> float:
            if mode == "fixed":
                return float(settings["stake_usd"])
            if mode == "percent":
                return round(cash_usd * float(settings["stake_pct"]) / 100.0, 2)
            if mode == "kelly":
                f = _kelly_fraction(model_prob, limit_prob) * float(settings["kelly_mult"])
                f = min(f, float(settings["kelly_cap_pct"]) / 100.0)
                return round(cash_usd * max(f, 0.0), 2)
            pct = _confidence_pct(
                model_prob, settings["stake_pct_min"], settings["stake_pct_max"]
            )
            return round(cash_usd * pct / 100.0, 2)

        # A representative stake for the summary (confidence varies per pick):
        # against the first enabled venue's cash, the paper bankroll if unread.
        first_cash = None
        if mode != "fixed" and settings["venues"]:
            first_cash = await cash_for(venues_mod.get(settings["venues"][0]))
            if first_cash is None and len(settings["venues"]) == 1:
                summary["skipped"]["no_balance"] = 1
                return summary
        summary["stake_usd"] = stake_for(
            0.75, 0.65, first_cash if first_cash is not None else PAPER_BANKROLL_USD)

        placed_today = _today_counts(conn, today, rules_only=True)
        budget = (settings["max_bets_per_day"] - placed_today["placed"]
                  - placed_today["dry"] - placed_today["unknown"])

        picks = await _scan(now, settings, lead, horizon, skip, summary)

        # Pass 1 — eligibility. Every gate here is local arithmetic, so the
        # whole slate can be scored before a single order is priced; that is
        # what lets best_edge_only rank picks against each other instead of
        # committing to whichever the board listed first.
        candidates: list[tuple[float, float, dict]] = []
        for row in picks:
            summary["scanned"] += 1
            # Eligibility first, budget second — an ineligible pick should
            # report WHY it was ineligible, not blame the daily cap.
            if settings["sports"] and row["sport"] not in settings["sports"]:
                skip("sport_filtered")
                continue
            if row["tier"] not in settings["tiers"]:
                skip("tier_filtered")
                continue
            # A reference with nobody quoting it (thin touch, wide spread) is
            # a stale number, and a gap against a stale number is not a price.
            if row.get("thin_reference"):
                skip("thin_reference")
                continue
            if row.get("ev") is None or row.get("price") is None:
                skip("no_price")
                continue
            # Implausibly high claimed EV anti-selects (model artifact, not
            # market inefficiency) — the live paper record confirms it.
            if float(row["ev"]) > MAX_CLAIMED_EV:
                skip("ev_implausible")
                continue
            if float(row["ev"]) < settings["min_ev"] - 1e-9:
                skip("below_min_ev")
                continue
            start = datetime.fromisoformat(row["kickoff"])
            if start <= now + lead or start > now + horizon:
                skip("out_of_window")
                continue
            if not settings["dry_run"] and not venues_mod.get(row.get("venue")).connected():
                skip("venue_not_connected")
                continue
            attempts, committed = _attempts(conn, row["game_id"], row["side"])
            if committed:
                skip("already_bet")
                continue
            if attempts >= MAX_ATTEMPTS:
                skip("max_attempts")
                continue
            model_prob = row.get("fair_prob") or _model_prob(row["price"], row["ev"])
            if model_prob is None or not (0.0 < model_prob < 1.0):
                skip("bad_pick_math")
                continue
            if settings["min_win_prob"] > 0 and model_prob < settings["min_win_prob"]:
                skip("below_min_winprob")
                continue
            edge = _market_edge(row["price"], model_prob)
            if edge is None:
                skip("bad_pick_math")
                continue
            if settings["min_edge"] > 0 and edge < settings["min_edge"]:
                skip("below_min_edge")
                continue
            # min_prob filters longshots: the market's implied prob for the
            # pick's price must be at least this — the counterpart of max_prob.
            if settings["min_prob"] > 0:
                implied = _implied_prob(row["price"])
                if implied is None or implied < settings["min_prob"]:
                    skip("below_min_price")
                    continue
            candidates.append((edge, model_prob, row))

        # BEST EDGE — sort the eligible slate by edge (model prob − market
        # implied prob) so the day's budget buys the sharpest picks on the
        # board. Stable, so equal edges keep the board's own STRONG-then-EV
        # order. Ranking by EDGE and not claimed EV is deliberate: at a fixed
        # price the two agree, but across prices EV = edge / implied, which
        # inflates longshots — exactly the bucket MAX_CLAIMED_EV exists to
        # distrust.
        best_edge = bool(settings["best_edge_only"])
        if best_edge:
            candidates.sort(key=lambda c: c[0], reverse=True)
            logger.info(
                "autobet best-edge ranking: %s eligible, top edge %.3f, "
                "budget %s",
                len(candidates), candidates[0][0] if candidates else 0.0, budget,
            )

        # Pass 2 — the money path. Every network call and every order lives
        # here, walked in ranked order, and stops the moment the cap is spent.
        #
        # Games this sweep has already committed money (or paper) to. Pass 1's
        # _attempts() check only sees the DB, and nothing is written until this
        # loop runs — so without this set, two models disagreeing about ONE
        # game both clear pass 1 and both get bet. On a two-way market those
        # are the complementary YES/NO tokens of the SAME condition_id: buying
        # both crosses the ask twice (cost 1 + spread for a $1 payout) and pays
        # two taker fees. Ranked order means the sharper edge claims the game.
        committed_games: set[str] = set()
        for edge, model_prob, row in candidates:
            if row["game_id"] in committed_games:
                skip("already_bet")
                continue
            if budget <= 0:
                skip("not_best_edge" if best_edge else "daily_cap")
                continue
            # Resolve the tradable market BEFORE pricing: the limit price and
            # the depth gate both need the market's own metadata (fee
            # schedule, live book), not just the recorded pick.
            venue = venues_mod.get(row.get("venue"))
            ref = row.get("_market")
            if ref is None:
                try:
                    ref = await venue.resolve(
                        Sport(row["sport"]), _pick_game(row), row["side"]
                    )
                except Exception as exc:  # noqa: BLE001 — discovery is best-effort
                    logger.debug("autobet: %s resolve failed: %s", venue.key, exc)
                    ref = None
            if ref is None:
                skip("no_market")
                continue
            # Re-check the edge against the LIVE venue price — the pick's
            # edge was computed against a possibly stale/other book, and
            # betting stale edges into a moved market is adverse selection.
            live_prob = getattr(ref, "prob", None)
            if settings["min_edge"] > 0 and live_prob:
                if (model_prob - float(live_prob)) < settings["min_edge"]:
                    skip("below_live_edge")
                    continue

            # EV-floor limit price at the market's OWN taker-fee rate.
            try:
                meta = await venue.meta(ref)
            except Exception:  # noqa: BLE001 — fee falls back to the constant
                meta = None
            fee_rate = _fee_rate_from_meta(meta, venue.fallback_fee_rate)
            cents = _limit_cents(model_prob, settings["min_ev"],
                                 settings["max_prob"], fee_rate=fee_rate)
            if cents is None:
                skip("price_out_of_range")
                continue
            limit_prob = cents / 100.0
            cash_usd = await cash_for(venue)
            if cash_usd is None:
                skip("no_balance")
                continue
            stake_usd = stake_for(model_prob, limit_prob, cash_usd)
            if stake_usd < 1.0:
                skip("stake_too_small")
                continue
            shares = int(stake_usd / limit_prob)
            if shares <= 0:
                skip("stake_too_small")
                continue
            usd = round(shares * limit_prob, 2)

            # Depth gate: can the book actually absorb this order at-or-below
            # our limit? Logged for every candidate (ground truth for the
            # backtest's fill model); fail-open on a book-fetch hiccup — the
            # FAK price cap remains the real safety mechanism.
            try:
                book = await venue.ask_depth(ref, cents)
            except Exception:  # noqa: BLE001
                book = None
            _observe_book(conn, row, ref, book, cents, now)
            best_ask_cents = book["best_ask_cents"] if book else None
            book_depth = book["depth_shares"] if book else None
            if book is not None:
                if book_depth < 1.0:
                    skip("no_depth")
                    continue
                if book_depth < shares:
                    # Shrink to what is actually executable — never inflate.
                    shares = int(book_depth)
                    usd = round(shares * limit_prob, 2)
            # Pre-empt the CLOB's minimum-size rejection (place_limit_order
            # refuses sub-minimum buys loudly) — skip cleanly instead.
            min_size = float((meta or {}).get("min_size") or 0.0)
            if shares < 1 or (min_size > 0 and shares < min_size):
                skip("below_min_size")
                continue

            if settings["dry_run"]:
                # Paper fills must reflect REAL execution: an unreadable book
                # leaves no honest price to fill at, so dry mode fails CLOSED
                # (live mode stays fail-open — its safety is the FAK cap).
                # The depth gate above already guarantees the ask crosses the
                # limit, so anything recorded here would have filled live and
                # its net EV at the executable price still clears min_ev.
                if book is None:
                    skip("no_book")
                    continue
                fill_prob = round(_paper_fill_prob(book, shares, limit_prob), 4)
                usd = round(shares * fill_prob, 2)
                bet_id = _record(conn, row, ref, limit_prob=limit_prob,
                                 shares=shares, usd=usd, model_prob=model_prob,
                                 order_id=None, status="dry", error=None,
                                 now=now, best_ask_cents=best_ask_cents,
                                 book_depth=book_depth, fee_rate=fee_rate)
                # Persist the executable price as the row's fill, so grading
                # the paper bet uses what a real FAK would have paid.
                with conn:
                    db.set_auto_bet_fill(conn, bet_id, shares, fill_prob)
                committed_games.add(row["game_id"])
                summary["dry"] += 1
                budget -= 1
                if mode != "fixed":
                    cash[venue.key] = max(cash_usd - usd, 0.0)
                continue

            # The regulated venues require a price collar on automated orders,
            # and the book read is the collar: no book, no order.
            if book is None and venue.live_needs_book:
                skip("no_book")
                continue
            try:
                result = await venue.buy(ref, shares, cents)
            except venues_mod.OrderOutcomeUnknown as exc:
                # Sent, never answered: it may be live on the exchange. Hold
                # the game and the day's slot exactly like a placed order, and
                # never send a second one behind it.
                logger.error(
                    "autobet: %s order for %s %s got no answer (%s) — check the "
                    "account; the game is held", venue.label, row["game_id"],
                    row["side"], exc,
                )
                _record(conn, row, ref, limit_prob=limit_prob, shares=shares,
                        usd=usd, model_prob=model_prob, order_id=None,
                        status="unknown", error=str(exc)[:300], now=now,
                        best_ask_cents=best_ask_cents, book_depth=book_depth,
                        fee_rate=fee_rate)
                committed_games.add(row["game_id"])
                summary["unknown"] += 1
                budget -= 1
                if mode != "fixed":
                    cash[venue.key] = max(cash_usd - usd, 0.0)
                continue
            except Exception as exc:  # noqa: BLE001 — record + move on
                logger.warning("autobet: %s order failed for %s %s: %s",
                               venue.label, row["game_id"], row["side"], exc)
                _record(conn, row, ref, limit_prob=limit_prob, shares=shares,
                        usd=usd, model_prob=model_prob, order_id=None,
                        status="error", error=venue.error_text(exc), now=now,
                        best_ask_cents=best_ask_cents, book_depth=book_depth,
                        fee_rate=fee_rate)
                summary["errors"] += 1
                continue

            if (result or {}).get("note"):
                logger.info("autobet: %s", result["note"])
                summary.setdefault("notes", []).append(result["note"])
            order_id = (result or {}).get("order_id") or ""
            if not order_id:
                # FAK with zero fill — clean no-trade, retryable next sweep.
                _record(conn, row, ref, limit_prob=limit_prob, shares=shares,
                        usd=usd, model_prob=model_prob, order_id=None,
                        status="unmatched", error=None, now=now,
                        best_ask_cents=best_ask_cents, book_depth=book_depth,
                        fee_rate=fee_rate)
                summary["unmatched"] += 1
                continue

            pending = bool(result.get("pending"))
            bet_id = _record(conn, row, ref, limit_prob=limit_prob, shares=shares,
                             usd=usd, model_prob=model_prob, order_id=order_id,
                             status="placed", error=PENDING_NOTE if pending else None,
                             now=now, best_ask_cents=best_ask_cents,
                             book_depth=book_depth, fee_rate=fee_rate)
            # The REAL fill, right away (partials must not grade as full
            # fills): the exchange's own answer when it gave one, else its
            # fills feed; feed lag is retried next sweep.
            fill = None
            if pending:
                pass  # the venue just asked and could not tell: next sweep
            elif result.get("filled") and result.get("avg_prob"):
                fill = (round(float(result["filled"]), 4), round(float(result["avg_prob"]), 4))
                with conn:
                    db.set_auto_bet_fill(conn, bet_id, fill[0], fill[1])
            else:
                fill = await _reconcile_fill(conn, bet_id, order_id, venue.key)
            committed_games.add(row["game_id"])
            # What actually left the wallet: the reconciled fill when the feed
            # has it, else the (post-depth-shrink) intended spend. NEVER the
            # pre-shrink `stake_usd` — that overstates every shrunk or
            # partially-filled order in MY BETS.
            spent_usd = round(fill[0] * fill[1], 2) if fill else usd
            logger.info(
                "autobet PLACED on %s %s %s: %s shares @ %s¢ ($%.2f) order=%s",
                venue.label, row["game_id"], row["side"], shares, cents, usd, order_id,
            )
            summary["placed"] += 1
            budget -= 1
            if mode != "fixed":
                # Committed money leaves the sizing base immediately, so one
                # sweep can never stake more than the balance it started with.
                cash[venue.key] = max(cash_usd - usd, 0.0)
            if not pending:
                _log_to_tracker(row, cents, spent_usd, settings, venue)
        return summary
    finally:
        conn.close()


def _log_to_tracker(row: dict, cents: int, spent_usd: float, settings: dict,
                    venue=None) -> None:
    """Mirror a PLACED autobet into the user's bet log so it grades into
    MY BETS when the game settles. Best-effort — a logging failure must
    never fail the sweep (the order is already live).

    `spent_usd` is what the order ACTUALLY cost (reconciled fill, else the
    post-depth-shrink intended spend) — not the requested stake, which would
    overstate units on every shrunk or partially-filled order."""
    venue = venue or venues_mod.get(row.get("venue"))
    try:
        stake_units = max(0.1, round(spent_usd / settings["unit_usd"], 2))
        tracker.log_bet(
            row["game_id"], row["side"], prob_to_american(cents / 100.0),
            book=venue.key, stake_units=stake_units,
            note=f"AUTO · {venue.label.lower()}",
        )
    except Exception as exc:  # noqa: BLE001
        logger.warning("autobet: tracker log failed for %s: %s", row["game_id"], exc)
