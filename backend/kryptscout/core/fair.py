"""Sharp fair price — what a boost or promo should be scored against.

The models in this app carry no information beyond the market (measured
repeatedly, last 2026-09-02). The one reference that does is the exchanges:
Kalshi led DraftKings' close 64% of the time for +1.05% same-book CLV,
Polymarket 59%. So "fair" here is the exchange-devigged probability, with a
sportsbook's power-devigged pair as a flagged fallback when no exchange
quotes the game yet.

Where that turns into money is not the model and not DraftKings' base line
(+1% sits under the vig): it is odds boosts, profit-boost tokens and promos,
where a book hands out a price above fair by construction. Scoring those
needs exactly one number per side — this one — and no forecast.

Rules:
* Pre-game quotes only (core.schemas.is_in_play).
* Only full-market quotes count (both sides; all three on a 3-way match).
* An exchange quote carrying order-book context (odds/polymarket,
  odds/kalshi fetch the public books) is priced at its MICROPRICE — the
  size-weighted touch — and a THIN book (touch notional under
  THIN_DEPTH_USD or a spread wider than WIDE_SPREAD_PTS) is demoted below
  any deep exchange and flagged. A big gap between an exchange and
  DraftKings used to be ambiguous: is the exchange stale, or is the book
  wrong? Depth answers that. Without book context an exchange is devigged
  proportionally (its asks sum to 1 + spread; the proportional devig
  recovers the midpoint). A sportsbook pair is devigged by the power
  method, which does not over-credit the underdog.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from kryptscout.core.oddsmath import remove_vig, remove_vig_power
from kryptscout.core.schemas import FairPrices, MoneylineOdds, Sport, is_in_play

EXCHANGES = frozenset({"kalshi", "polymarket"})
# A touch holding less than this (USD notional on the thinner side) or a
# spread wider than this is a stale/empty book, not a reference. Measured
# spreads on NBA/WNBA archives were ~1c; a 3c+ book is nobody quoting.
THIN_DEPTH_USD = 250.0
WIDE_SPREAD_PTS = 3.0


def is_thin(q: MoneylineOdds) -> bool:
    """True when the quote carries book context and it says 'nobody home'."""
    if q.home_depth_usd is not None and q.away_depth_usd is not None:
        if min(q.home_depth_usd, q.away_depth_usd) < THIN_DEPTH_USD:
            return True
    if q.spread_pts is not None and q.spread_pts > WIDE_SPREAD_PTS:
        return True
    return False
# Kalshi first (the stronger lead), Polymarket, then DraftKings as a flagged
# fallback. Any other full-market sportsbook is used only if none of these
# quote the game.
REFERENCE_ORDER = ("kalshi", "polymarket", "draftkings")


def sharp_fair(
    game_id: str,
    sport: Sport,
    odds_list: list[MoneylineOdds],
    starts_at: Optional[datetime] = None,
) -> Optional[FairPrices]:
    """Fair probabilities per side from the sharpest venue quoting the game."""
    pre = [q for q in odds_list if not is_in_play(q, starts_at)]
    three_way = any(q.draw_ml is not None for q in pre)
    keys = ("home_ml", "away_ml", "draw_ml") if three_way else ("home_ml", "away_ml")
    full: dict[str, MoneylineOdds] = {}
    for q in pre:
        if q.book in full:
            continue
        if all(getattr(q, key) is not None for key in keys):
            full[q.book] = q
    probs: dict[str, list[float]] = {}
    for book, q in full.items():
        prices = [getattr(q, key) for key in keys]
        try:
            if book in EXCHANGES and not three_way and q.microprice_home is not None:
                probs[book] = [q.microprice_home, 1.0 - q.microprice_home]
            elif book in EXCHANGES:
                probs[book] = remove_vig(*prices)
            else:
                probs[book] = remove_vig_power(*prices)
        except (ValueError, ZeroDivisionError):
            continue
    if not probs:
        return None
    ordered = [book for book in REFERENCE_ORDER if book in probs]
    deep = [book for book in ordered if book in EXCHANGES and not is_thin(full[book])]
    if deep:
        source = deep[0]
    elif ordered:
        source = ordered[0]
    else:
        source = next(iter(probs))
    q = full[source]
    p = probs[source]
    depth = (
        min(q.home_depth_usd, q.away_depth_usd)
        if q.home_depth_usd is not None and q.away_depth_usd is not None
        else None
    )
    return FairPrices(
        game_id=game_id,
        sport=sport,
        source=source,
        sharp=source in EXCHANGES,
        home=p[0],
        away=p[1],
        draw=p[2] if three_way else None,
        fetched_at=q.fetched_at,
        sources={book: v[0] for book, v in probs.items()},
        thin=source in EXCHANGES and is_thin(q),
        depth_usd=depth,
        spread_pts=q.spread_pts,
    )
