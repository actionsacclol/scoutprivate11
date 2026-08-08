"""Dollar-ROI strategy backtest — model log × backfilled Polymarket prices.

The market side comes from jobs/backfill.py (pm_markets + pm_price_snapshots:
closed moneyline markets with the last trade at kickoff−offset). The model
side is the engine's persisted walk-forward log rows [date, prob, outcome,
home, away] (5-field rows only — 3-field logs lack team names and can't
join). Rows are joined by US-Eastern game date + team names, sanity-checked
by comparing the market's resolution to the log's outcome, then a strategy —
the same knobs as the live autobet engine — is replayed chronologically.

Execution realism (see the 2026-07 recon):
  fees      taker-only, rate × p(1−p) per share; 0.03 before 2026-07-10 and
            0.05 after (sports_fees_v2) — charged win or lose.
  fills     the stored price is the LAST TRADE, not the executable ask, so the
            simulator prices every strategy at three fill assumptions:
            optimistic = last trade, realistic = +1 tick, pessimistic = +2
            ticks. Trust the pessimistic column.
  gates     min volume and max snapshot staleness drop thin/stale markets
            whose last-trade price would fake edge.
"""
from __future__ import annotations

import json
import math
import re
from datetime import datetime, timedelta
from zoneinfo import ZoneInfo

from kryptscout import config
from kryptscout.store import db

_EASTERN = ZoneInfo("America/New_York")
_FEE_CHANGE = "2026-07-10"  # sports taker fee 0.03 -> 0.05

FILL_TICKS = {"optimistic": 0.0, "realistic": 0.01, "pessimistic": 0.02}


def _fee_rate(game_date: str) -> float:
    return 0.05 if game_date >= _FEE_CHANGE else 0.03


def _norm(name: str) -> str:
    return re.sub(r"[^a-z0-9]", "", (name or "").lower())


def _last_word(name: str) -> str:
    """Lowercased final word ('Minnesota Lynx' -> 'lynx'), or "" when the name
    has no words at all. Guarded because a blank/whitespace outcome name in
    pm_markets would otherwise IndexError straight out of the ROI endpoint."""
    parts = (name or "").split()
    return parts[-1].lower() if parts else ""


def _names_match(a: str, b: str) -> bool:
    """Exact normalized match, or nickname (last word) equality — Polymarket
    sometimes lists nicknames ('Lynx') where the log has full names."""
    na, nb = _norm(a), _norm(b)
    if not na or not nb:
        return False
    if na == nb or na.endswith(nb) or nb.endswith(na):
        return True
    la, lb = _last_word(a), _last_word(b)
    return bool(la) and la == lb


def _eastern_date(game_start: str) -> str | None:
    """Market kickoff (UTC-ish ISO) -> US-Eastern date, matching the log's
    officialDate convention."""
    s = game_start.strip().replace("Z", "+00:00")
    if len(s) >= 3 and s[-3] in "+-" and ":" not in s[-3:]:
        s += ":00"
    try:
        dt = datetime.fromisoformat(s)
    except ValueError:
        return None
    return dt.astimezone(_EASTERN).date().isoformat()


def _model_log(sport: str) -> list[list]:
    """The engine's persisted walk-forward log; 5-field rows only."""
    path = config.DATA_DIR / f"{sport}_state.json"
    if not path.exists():
        return []
    try:
        state = json.loads(path.read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return []
    log = state.get("backtest")
    if not isinstance(log, list):
        return []
    return [r for r in log if isinstance(r, list) and len(r) >= 5]


# Per-sport slack between the market's game date and the log's date. Tennis
# logs carry the TOURNAMENT start date (multi-week events), so matches land
# up to ~2 weeks after it; team-sport logs carry the exact game date.
DATE_TOLERANCE_DAYS = {"tennis": 16}


def _pair_key(a: str, b: str) -> tuple[str, str]:
    """Order-free key from the two sides' last words (nickname / surname)."""
    return tuple(sorted((_last_word(a), _last_word(b))))


# Generic club-name tokens that carry no identity ("FC", "United" DOES carry
# identity but only weakly — it stays, suffix junk goes).
_CLUB_JUNK = {"fc", "cf", "afc", "sc", "ec", "ac", "club", "cd", "sad", "if"}


def _club_key(name: str) -> str:
    words = [w for w in re.sub(r"[^a-z0-9 ]", "", name.lower()).split()
             if w not in _CLUB_JUNK]
    return "".join(words)


def _names_match_soccer(a: str, b: str) -> bool:
    """Strict club matching — the nickname (last-word) fallback used for US
    teams matches every '... FC' to every other, so soccer requires key
    equality or long-substring containment (>=7 chars)."""
    ka, kb = _club_key(a), _club_key(b)
    if not ka or not kb:
        return False
    if ka == kb:
        return True
    shorter, longer = sorted((ka, kb), key=len)
    return len(shorter) >= 7 and shorter in longer


_Q_WIN = re.compile(r"^Will (.+?) win on ", re.IGNORECASE)
_Q_DRAW = re.compile(r"^Will (.+?) vs\.? (.+?) end in a draw", re.IGNORECASE)
_SLUG_DATE = re.compile(r"\d{4}-\d{2}-\d{2}")


def _join_soccer(sport: str, offset_minutes: int, min_volume: float) -> dict:
    """Soccer markets are binary Yes/No questions ('Will X win on ...?',
    'Will A vs. B end in a draw?'); the log carries 8-field rows
    [date, p_home, home_won, home, away, p_draw, p_away, result HDA].
    Each market joins as (model p(question), Yes price, question outcome)."""
    log = [r for r in _model_log(sport) if len(r) >= 8]
    stats = {"log_rows": len(log), "markets": 0, "joined": 0, "thin": 0,
             "stale": 0, "outcome_mismatch": 0, "with_book": 0}
    if not log:
        return {"rows": [], "stats": stats}
    by_date: dict[str, list] = {}
    for r in log:
        by_date.setdefault(str(r[0]), []).append(r)

    conn = db.connect()
    try:
        markets = db.rows(
            conn,
            """
            SELECT m.*, s.price_a FROM pm_markets m
            JOIN pm_price_snapshots s ON s.condition_id = m.condition_id
                AND s.offset_minutes = ?
            WHERE m.sport = ? AND m.winner IS NOT NULL AND m.question IS NOT NULL
            """,
            (offset_minutes, sport),
        )
    finally:
        conn.close()
    stats["markets"] = len(markets)

    rows: list[dict] = []
    for m in markets:
        if (m["volume"] or 0.0) < min_volume:
            stats["thin"] += 1
            continue
        md = _SLUG_DATE.search(m["slug"] or "")
        if not md:
            continue
        mdate = datetime.fromisoformat(md.group(0)).date()
        win = _Q_WIN.match(m["question"])
        draw = _Q_DRAW.match(m["question"])
        found = None
        for delta in (0, -1, 1):
            for r in by_date.get((mdate + timedelta(days=delta)).isoformat(), []):
                home, away, result = r[3], r[4], r[7]
                if win:
                    team = win.group(1)
                    if _names_match_soccer(team, home):
                        found = (float(r[1]), result == "H")
                    elif _names_match_soccer(team, away):
                        found = (float(r[6]), result == "A")
                elif draw:
                    a, b = draw.group(1), draw.group(2)
                    if (_names_match_soccer(a, home) and _names_match_soccer(b, away)) or (
                        _names_match_soccer(b, home) and _names_match_soccer(a, away)
                    ):
                        found = (float(r[5]), result == "D")
                if found:
                    break
            if found:
                break
        if not found:
            continue
        model_p, yes_true = found
        if yes_true != (m["winner"] == "a"):
            stats["outcome_mismatch"] += 1
            continue
        rows.append({
            "date": mdate.isoformat(), "home": m["question"], "away": "",
            "game_start": m["game_start"] or "",
            "model_home_prob": model_p,
            "price_home": float(m["price_a"]),
            "home_won": yes_true,
            "volume": float(m["volume"] or 0.0),
        })
    stats["joined"] = len(rows)
    rows.sort(key=lambda r: (r["date"], r["game_start"]))
    return {"rows": rows, "stats": stats}


def joined_rows(
    sport: str,
    offset_minutes: int = 60,
    min_volume: float = 1000.0,
    max_stale_minutes: float = 45.0,
) -> dict:
    """(model_prob, polymarket price, outcome) rows + join-quality counters."""
    if sport == "soccer":
        return _join_soccer(sport, offset_minutes, min_volume)
    log = _model_log(sport)
    stats = {"log_rows": len(log), "markets": 0, "joined": 0, "thin": 0,
             "stale": 0, "outcome_mismatch": 0, "with_book": 0}
    if not log:
        return {"rows": [], "stats": stats}
    tol = DATE_TOLERANCE_DAYS.get(sport, 0)

    # Pair-key index; each log row joins at most once (doubleheaders/rematches
    # consume rows nearest-date-first instead of double-matching the first).
    by_key: dict[tuple, list] = {}
    for i, r in enumerate(log):
        by_key.setdefault(_pair_key(r[3], r[4]), []).append(
            (i, str(r[0]), float(r[1]), int(r[2]), r[3], r[4])
        )
    used: set[int] = set()

    conn = db.connect()
    try:
        markets = db.rows(
            conn,
            """
            SELECT m.*, s.price_a, s.at_ts, b.best_bid_a, b.best_ask_a
            FROM pm_markets m
            JOIN pm_price_snapshots s ON s.condition_id = m.condition_id
                AND s.offset_minutes = ?
            LEFT JOIN pm_book_snapshots b ON b.condition_id = m.condition_id
                AND b.offset_minutes = ?
            WHERE m.sport = ? AND m.winner IS NOT NULL
            """,
            (offset_minutes, offset_minutes, sport),
        )
    finally:
        conn.close()
    stats["markets"] = len(markets)

    rows: list[dict] = []
    for m in markets:
        if (m["volume"] or 0.0) < min_volume:
            stats["thin"] += 1
            continue
        edate = _eastern_date(m["game_start"] or "")
        if edate is None:
            continue
        mdate = datetime.fromisoformat(edate).date()
        match = None
        best_diff = None
        for i, ldate, prob, outcome, home, away in by_key.get(
            _pair_key(m["outcome_a"], m["outcome_b"]), []
        ):
            if i in used:
                continue
            try:
                diff = (mdate - datetime.fromisoformat(ldate).date()).days
            except ValueError:
                continue
            # Team logs carry the exact game date (diff 0); tennis logs carry
            # the tournament start, so the match lands 0..tol days after it.
            if not (-1 <= diff <= max(tol, 0)):
                continue
            a_home = _names_match(m["outcome_a"], home)
            b_home = _names_match(m["outcome_b"], home)
            a_away = _names_match(m["outcome_a"], away)
            b_away = _names_match(m["outcome_b"], away)
            if not ((a_home and b_away) or (b_home and a_away)):
                continue
            if best_diff is None or abs(diff) < best_diff:
                match = (i, prob, outcome, home, away, a_home)
                best_diff = abs(diff)
        if match is None:
            continue
        idx, prob, outcome, home, away, a_is_home = match

        # Staleness: how old was the last trade at the snapshot instant.
        kick = _eastern_date_dt(m["game_start"])
        try:
            at = datetime.fromisoformat(m["at_ts"])
        except ValueError:
            continue
        if kick is not None:
            target = kick - timedelta(minutes=offset_minutes)
            if (target - at).total_seconds() > max_stale_minutes * 60:
                stats["stale"] += 1
                continue

        price_home = float(m["price_a"]) if a_is_home else 1.0 - float(m["price_a"])
        market_home_won = (m["winner"] == "a") == a_is_home
        if market_home_won != bool(outcome):
            stats["outcome_mismatch"] += 1
            continue
        used.add(idx)
        # Real book (jobs/bookfill.py) where harvested: home-side quotes are
        # side a's directly, or the complement of side a's when a is away
        # (b's ask = 1 − a's bid on a binary market).
        ask_a, bid_a = m.get("best_ask_a"), m.get("best_bid_a")
        if a_is_home:
            ask_home, bid_home = ask_a, bid_a
        else:
            ask_home = round(1.0 - bid_a, 4) if bid_a is not None else None
            bid_home = round(1.0 - ask_a, 4) if ask_a is not None else None
        if ask_home is not None and bid_home is not None:
            stats["with_book"] += 1
        rows.append({
            "date": edate, "home": home, "away": away,
            "game_start": m["game_start"] or "",
            "model_home_prob": float(prob),
            "price_home": price_home,
            "ask_home": ask_home,
            "bid_home": bid_home,
            "home_won": bool(outcome),
            "volume": float(m["volume"] or 0.0),
        })
    stats["joined"] = len(rows)
    # Kickoff order inside the day: the live engine fires `lead_minutes` before
    # each game, so any per-day cap must consume the day's slots chronologically
    # (sorting by date alone would let row order decide which bets get taken).
    rows.sort(key=lambda r: (r["date"], r["game_start"]))
    return {"rows": rows, "stats": stats}


def _eastern_date_dt(game_start: str | None) -> datetime | None:
    if not game_start:
        return None
    s = game_start.strip().replace("Z", "+00:00")
    if len(s) >= 3 and s[-3] in "+-" and ":" not in s[-3:]:
        s += ":00"
    try:
        return datetime.fromisoformat(s)
    except ValueError:
        return None


def simulate(
    rows: list[dict],
    *,
    min_ev: float = 0.04,
    max_ev: float = 0.0,
    min_win_prob: float = 0.0,
    min_edge: float = 0.0,
    min_prob: float = 0.0,
    max_prob: float = 0.85,
    stake_mode: str = "fixed",   # fixed | percent | confidence | kelly
    stake_usd: float = 10.0,
    stake_pct: float = 2.0,
    stake_pct_min: float = 2.0,
    stake_pct_max: float = 8.0,
    bankroll_usd: float = 1000.0,
    daily_stop_loss_usd: float = 0.0,
    daily_stop_profit_usd: float = 0.0,
    stop_loss_usd: float = 0.0,
    stop_profit_usd: float = 0.0,
    max_bets_per_day: int = 0,
    fill: str = "realistic",
) -> dict:
    """Replay one strategy chronologically over joined rows.

    Mirrors the live engine's rules: per-side net-EV gate (era-correct taker
    fee), `max_ev` ceiling (0 = off — the engine's MAX_CLAIMED_EV, which
    refuses implausibly large claimed edges as model artifacts; leaving it off
    replays a bucket the live engine never bets, which is exactly the bucket
    the live record shows losing money), win-prob/edge floors, min/max price,
    sizing modes (percent/confidence size off the RUNNING bankroll),
    daily/total realized-P&L stops that halt the rest of the day / the whole
    run, and the live engine's `max_bets_per_day` cap (0 = unlimited). Any
    caller comparing a live record against this simulation (see
    autobet.engine.drift) MUST pass the same ceiling, or it is grading live
    performance against an expectation the engine was never allowed to earn.

    The cap fires in the joined rows'
    chronological order, which is by DATE only — within a day the live engine
    takes picks as their games approach, so which 5 of a day's qualifying
    games get taken can differ; the count per day is faithful.

    fill="book" prices every entry at the REAL archived best ask (see
    jobs/bookfill.py) with zero padding — rows without a harvested book are
    skipped (counted in skipped["no_book"]) so mixed samples stay honest.
    """
    ticks = FILL_TICKS.get(fill, 0.01)
    bank = bankroll_usd
    total_pnl = 0.0
    staked = 0.0
    n = wins = 0
    equity: list[dict] = []
    bets: list[dict] = []
    skipped = {"gates": 0, "stopped": 0, "bank_broke": 0, "no_book": 0,
               "day_cap": 0}
    day_pnl = 0.0
    day_bets = 0
    cur_day: str | None = None
    halted_day: str | None = None
    halted_run = False

    for r in rows:
        if r["date"] != cur_day:
            if cur_day is not None:
                equity.append({"date": cur_day, "pnl": round(day_pnl, 2),
                               "cum": round(total_pnl, 2)})
            cur_day, day_pnl, day_bets = r["date"], 0.0, 0
        if halted_run or halted_day == r["date"]:
            skipped["stopped"] += 1
            continue
        if max_bets_per_day > 0 and day_bets >= max_bets_per_day:
            skipped["day_cap"] += 1
            continue

        rate = _fee_rate(r["date"])
        if fill == "book" and (r.get("ask_home") is None or r.get("bid_home") is None):
            skipped["no_book"] += 1
            continue
        best = None  # (ev_net, side, price_eff, model_p)
        for side, model_p, price in (
            ("home", r["model_home_prob"], r["price_home"]),
            ("away", 1.0 - r["model_home_prob"], 1.0 - r["price_home"]),
        ):
            if fill == "book":
                # Executable ask for this side: home buys at the home ask;
                # away buys the complement side, whose ask is 1 − home bid.
                q = r["ask_home"] if side == "home" else round(1.0 - r["bid_home"], 4)
            else:
                q = price + ticks
            if q <= 0.01 or q >= 0.99 or q < min_prob or q > max_prob:
                continue
            ev_net = model_p / q - 1.0 - rate * (1.0 - q)
            if ev_net < min_ev:
                continue
            # Implausibly large claimed edge = model artifact, not market
            # inefficiency (engine.MAX_CLAIMED_EV). Gate on the GROSS claimed
            # EV, which is what the live engine screens the recorded pick on.
            if max_ev > 0 and (model_p / q - 1.0) > max_ev:
                continue
            if min_win_prob > 0 and model_p < min_win_prob:
                continue
            if min_edge > 0 and (model_p - price) < min_edge:
                continue
            if best is None or ev_net > best[0]:
                best = (ev_net, side, q, model_p)
        if best is None:
            skipped["gates"] += 1
            continue
        _, side, q, model_p = best

        if stake_mode == "fixed":
            stake = stake_usd
        elif stake_mode == "percent":
            stake = bank * stake_pct / 100.0
        elif stake_mode == "kelly":
            # 0.25x fractional Kelly capped at 5% of bankroll (engine defaults).
            b = (1.0 - q) / q
            f = max(model_p - (1.0 - model_p) / b, 0.0) * 0.25
            stake = bank * min(f, 0.05)
        else:  # confidence — scale across [0.50, 0.90] like the live engine
            t = min(max((model_p - 0.50) / 0.40, 0.0), 1.0)
            stake = bank * (stake_pct_min + t * (stake_pct_max - stake_pct_min)) / 100.0
        stake = round(stake, 2)
        if stake < 1.0 or stake > bank:
            skipped["bank_broke"] += 1
            continue

        shares = stake / q
        fee = shares * rate * q * (1.0 - q)  # charged win or lose
        won = (side == "home") == r["home_won"]
        pnl = (shares - stake - fee) if won else (-stake - fee)

        n += 1
        day_bets += 1
        wins += won
        staked += stake
        total_pnl += pnl
        day_pnl += pnl
        bank += pnl
        bets.append({
            "date": r["date"], "home": r["home"], "away": r["away"],
            "side": side, "model_p": round(model_p, 4), "price": round(q, 4),
            "stake": stake, "won": won, "pnl": round(pnl, 2),
            "fee": round(fee, 2), "bank_after": round(bank, 2),
        })

        if daily_stop_loss_usd > 0 and day_pnl <= -daily_stop_loss_usd:
            halted_day = r["date"]
        if daily_stop_profit_usd > 0 and day_pnl >= daily_stop_profit_usd:
            halted_day = r["date"]
        if stop_loss_usd > 0 and total_pnl <= -stop_loss_usd:
            halted_run = True
        if stop_profit_usd > 0 and total_pnl >= stop_profit_usd:
            halted_run = True
    if cur_day is not None:
        equity.append({"date": cur_day, "pnl": round(day_pnl, 2),
                       "cum": round(total_pnl, 2)})

    peak = max_dd = 0.0
    for point in equity:
        peak = max(peak, point["cum"])
        max_dd = max(max_dd, peak - point["cum"])

    return {
        "fill": fill,
        "n_bets": n,
        "wins": wins,
        "win_rate": (wins / n) if n else None,
        "staked_usd": round(staked, 2),
        "pnl_usd": round(total_pnl, 2),
        "roi_pct": round(100.0 * total_pnl / staked, 2) if staked else None,
        "end_bankroll_usd": round(bank, 2),
        "max_drawdown_usd": round(max_dd, 2),
        "equity": equity,
        "bets": bets,
        "skipped": skipped,
    }


def optimize(
    sport: str,
    offset_minutes: int = 60,
    min_volume: float = 5000.0,
    max_stale_minutes: float = 45.0,
    stake_mode: str = "fixed",
    top_n: int = 5,
) -> dict:
    """Grid-search gate thresholds with an honest time split.

    The joined rows are split chronologically 70/30: every (min_ev,
    min_win_prob, min_edge) combo is scored on the TUNE window at the
    pessimistic fill, ranked by ROI (min 30 bets), and only the top few are
    then shown against the untouched VALIDATION window. A combo that only
    shines in tune is overfit — trust validation, and prefer combos whose
    two columns agree.
    """
    joined = joined_rows(sport, offset_minutes, min_volume, max_stale_minutes)
    rows = joined["rows"]
    out = {"sport": sport, "join": joined["stats"], "results": [],
           "split_date": None,
           "note": "Ranked by tune-window ROI at the pessimistic fill; "
                   "validation is untouched by selection — trust it."}
    if len(rows) < 200:
        out["note"] = "Too few joined rows to optimize honestly (<200)."
        return out
    cut = rows[int(len(rows) * 0.7)]["date"]
    tune = [r for r in rows if r["date"] < cut]
    val = [r for r in rows if r["date"] >= cut]
    out["split_date"] = cut

    combos = []
    for min_ev in (0.02, 0.04, 0.06, 0.08, 0.10):
        for min_win in (0.0, 0.55, 0.60, 0.65):
            for min_edge in (0.0, 0.03, 0.05, 0.08):
                s = simulate(tune, min_ev=min_ev, min_win_prob=min_win,
                             min_edge=min_edge, stake_mode=stake_mode,
                             fill="pessimistic")
                if s["n_bets"] >= 30 and s["roi_pct"] is not None:
                    combos.append((s["roi_pct"], min_ev, min_win, min_edge, s))
    combos.sort(reverse=True)
    for roi, min_ev, min_win, min_edge, tune_sim in combos[:top_n]:
        v = simulate(val, min_ev=min_ev, min_win_prob=min_win,
                     min_edge=min_edge, stake_mode=stake_mode,
                     fill="pessimistic")
        out["results"].append({
            "min_ev": min_ev, "min_win_prob": min_win, "min_edge": min_edge,
            "tune": {"n_bets": tune_sim["n_bets"], "roi_pct": tune_sim["roi_pct"],
                     "pnl_usd": tune_sim["pnl_usd"]},
            "validation": {"n_bets": v["n_bets"], "roi_pct": v["roi_pct"],
                           "pnl_usd": v["pnl_usd"]},
        })
    return out


def report(sport: str, offset_minutes: int = 60, min_volume: float = 1000.0,
           max_stale_minutes: float = 45.0, **strategy) -> dict:
    """Join + the three-fill simulation for one sport, panel-ready. When
    harvested books cover part of the sample (jobs/bookfill.py), a fourth
    BOOK sim fills at the real archived ask — the measured, no-guess row."""
    joined = joined_rows(sport, offset_minutes, min_volume, max_stale_minutes)
    sims = {
        fill: simulate(joined["rows"], fill=fill, **strategy)
        for fill in ("pessimistic", "realistic", "optimistic")
    }
    note = (
        "Prices are last trades from backfilled Polymarket history; trust "
        "the pessimistic fill column. Fees are era-correct (3% pre "
        "2026-07-10, 5% after)."
    )
    if joined["stats"].get("with_book"):
        sims["book"] = simulate(joined["rows"], fill="book", **strategy)
        note += (
            f" BOOK fills at the real archived ask "
            f"({joined['stats']['with_book']} of {joined['stats']['joined']} "
            "games have harvested books) — the measured row."
        )
    return {
        "sport": sport,
        "offset_minutes": offset_minutes,
        "join": joined["stats"],
        "sims": sims,
        "note": note,
    }
