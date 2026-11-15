"""The daily card: a short list of the day's picks, each priced on every
exchange that lists it.

Two jobs.

**Choosing** (`select_card`). A day records one pick per game — a hundred
and fifty on a college Saturday — and a post of all of them is noise. The
card is the handful worth sending:

* nothing outside the 50-85% band, whatever its tier: under 50% it is an
  underdog, over 85% (-570 and shorter) the price pays too little for anyone
  to want the bet. A pick just over 85% on one exchange stays when another
  sells it at 85c or under (`daily_card`);
* value picks first (a sportsbook price under the exchange fair);
* then favourites, filtered to the ones a reader can act on: an exchange
  prices the game, the app's own model backs the same side, and no one
  sport fills the card. Ranked by the LOWER of the
  exchange's and the model's probability — both have to rate it. Games no
  engine rates (the "model" is the market itself) come after the rest.

That ranking is a filter for agreement, not a claim of edge: nothing measured
says the models add information to the exchange price, and a favourite at its
fair price is a fair bet. What the card buys is fewer, more backable picks.

**Pricing** (`exchange_prices`). A pick names a team; where to buy it is a
separate question, and Polymarket, Kalshi and Polymarket US answer it
differently by a cent or two — and charge different fees. Each selected pick
gets the price a buyer pays on each and the cost with that exchange's taker
fee, so the cheapest all-in can be marked.

Polymarket and Kalshi are quoted on the boards, so their price comes from the
game's card. Polymarket US is not: it costs a lookup per game, which is why
pricing happens after the card is cut down, never for a whole slate.

Nothing here needs an account.
"""
from __future__ import annotations

import asyncio
import logging
import math
from datetime import datetime, timezone
from typing import Any, Optional

from kryptscout.autobet import venues as venues_mod
from kryptscout.core.oddsmath import implied_prob
from kryptscout.core.schemas import Game, GameCard, Sport, is_in_play
from kryptscout.jobs import capture as capture_job

logger = logging.getLogger("kryptscout")

# A board this fresh is reused; older, and the sport's board is rebuilt.
CARD_MAX_AGE_S = 600.0
# The most picks priced in one call — each can cost a Polymarket US lookup.
MAX_PICKS = 25
# The most pick rows a card is chosen from (a full Saturday is ~250).
MAX_CANDIDATES = 1000

DEFAULT_CARD = 6
MAX_CARD = 12
VALUE_TIERS = ("STRONG", "LEAN")
# Every pick on the card, value picks included, has a win probability in this
# band. Owner's rule (2026-11-14): "no 90% plus, no one wants it; 50-85%".
# Above it the price (-570 and shorter) pays too little; below it the pick is
# an underdog, not a call.
CARD_PROB_MIN = 0.50
CARD_PROB_MAX = 0.85
# The stored win probability is ONE exchange's fair (Kalshi first). A game
# Kalshi has at 87 can be 85 on Polymarket US, and to a buyer that is an 85
# pick. So a pick whose probability is just over the cap is priced on every
# exchange, and stays on the card when one of them sells it at the cap or
# under. Further over than this and no cross-venue gap closes it; the
# lookups (one Polymarket US call each) are capped per card.
CARD_RESCUE_MAX = 0.88
MAX_RESCUE_LOOKUPS = 8
# The "model" on a game no engine rates: it IS the market, so its agreeing
# with the market says nothing. Those favourites only fill leftover places.
MARKET_MODEL = "market-consensus"


def key(game_id: str, side: str) -> str:
    """The id a caller matches a pick to its prices by."""
    return f"{game_id}|{side}"


# ------------------------------------------------------------------ choosing


def _unit(value: Any) -> Optional[float]:
    """A probability strictly between 0 and 1, else None."""
    try:
        number = float(value)
    except (TypeError, ValueError):
        return None
    return number if 0.0 < number < 1.0 else None


def _tier(row: dict) -> str:
    return str(row.get("tier") or "").upper()


def pick_prob(row: dict) -> Optional[float]:
    """The win probability to show for a pick: the exchange fair where there
    is one (the best number the app has), else the model's own."""
    fair = _unit(row.get("fair_prob"))
    return fair if fair is not None else _unit(row.get("model_prob"))


def in_band(prob: Optional[float]) -> bool:
    """Whether a win probability is one the card sends. Unknown is not."""
    return prob is not None and CARD_PROB_MIN <= prob <= CARD_PROB_MAX


def _one_per_game(picks: list[dict]) -> list[dict]:
    """A game can carry several pick rows (one per model that ever rated it).
    Keep one: a value pick over a favourite, then the newest."""
    best: dict[str, dict] = {}
    for row in picks:
        if not isinstance(row, dict) or not row.get("game_id") or not row.get("side"):
            continue

        def rank(r: dict) -> tuple:
            tier = _tier(r)
            order = VALUE_TIERS.index(tier) if tier in VALUE_TIERS else len(VALUE_TIERS)
            return (-order, str(r.get("created_at") or ""))

        held = best.get(str(row["game_id"]))
        if held is None or rank(row) > rank(held):
            best[str(row["game_id"])] = row
    return list(best.values())


def _row_key(row: dict) -> str:
    return key(str(row.get("game_id")), str(row.get("side")))


def _sendable(row: dict, rescued: frozenset[str]) -> bool:
    """In the band, or just over it with an exchange selling at the cap."""
    return in_band(pick_prob(row)) or _row_key(row) in rescued


def _favorite_score(row: dict, rescued: frozenset[str] = frozenset()) -> Optional[float]:
    """How strongly a favourite qualifies, or None when it does not.

    Needs an exchange fair in the band and the model on the same side; the
    score is the lower of the two probabilities, so one confident source
    cannot carry a pick the other is lukewarm on.
    """
    if row.get("side") not in ("home", "away"):
        return None
    fair = _unit(row.get("fair_prob"))
    if fair is None or not _sendable(row, rescued):
        return None
    model = _unit(row.get("model_prob"))
    if model is None or model < 0.5:
        return None
    return min(fair, model)


def select_card(
    picks: list[dict],
    limit: int = DEFAULT_CARD,
    rescued: frozenset[str] = frozenset(),
) -> list[dict]:
    """The picks worth sending, best first, at most `limit` (see module doc).
    Each returned row is a copy carrying `prob`. `rescued` holds the keys of
    picks just over the cap that an exchange sells at the cap or under (see
    `daily_card`). Pure."""
    limit = max(1, min(int(limit), MAX_CARD))
    # An out-of-band value pick is dropped BEFORE the one-per-game cut, so it
    # cannot displace the same game's in-band favourite row.
    rows = _one_per_game([
        r for r in picks[:MAX_CANDIDATES]
        if isinstance(r, dict) and (_tier(r) not in VALUE_TIERS or _sendable(r, rescued))
    ])

    value = sorted(
        (r for r in rows if _tier(r) in VALUE_TIERS),
        key=lambda r: (VALUE_TIERS.index(_tier(r)), -float(r.get("ev") or 0.0)),
    )
    card = [dict(r, prob=pick_prob(r)) for r in value[:limit]]

    scored = [(score, r) for r in rows if _tier(r) not in VALUE_TIERS
              and (score := _favorite_score(r, rescued)) is not None]
    scored.sort(key=lambda sr: (
        str(sr[1].get("model") or "") == MARKET_MODEL,  # rated games first
        -sr[0],
        str(sr[1].get("kickoff") or ""),
    ))
    # No sport takes more than a third of the card (two of six): a college
    # Saturday would otherwise be the whole post.
    per_sport = max(2, math.ceil(limit / 3))
    taken: dict[str, int] = {}
    for _, row in scored:
        if len(card) >= limit:
            break
        sport = str(row.get("sport") or "")
        if taken.get(sport, 0) >= per_sport:
            continue
        taken[sport] = taken.get(sport, 0) + 1
        card.append(dict(row, prob=pick_prob(row)))
    return card


# ------------------------------------------------------------------- pricing


def _game(row: dict) -> Optional[Game]:
    """A Game from a pick row (GET /api/picks shape), or None when it is not
    one a venue could look up."""
    try:
        start = datetime.fromisoformat(str(row["kickoff"]))
        if start.tzinfo is None:
            start = start.replace(tzinfo=timezone.utc)
        return Game(
            id=str(row["game_id"]),
            sport=Sport(row["sport"]),
            league=row.get("league") or "",
            start_time=start,
            home=str(row["home"]),
            away=str(row["away"]),
        )
    except (KeyError, TypeError, ValueError):
        return None


async def _cards(sports: set[Sport]) -> dict[str, GameCard]:
    """Current board cards by game id for these sports."""

    async def one(sport: Sport) -> list[GameCard]:
        board = capture_job.recent_cards(sport, CARD_MAX_AGE_S)
        if board is not None:
            return board
        try:
            return await capture_job.build_cards(sport, True)
        except Exception as exc:  # noqa: BLE001 — one sport must not sink the rest
            logger.warning("compare: could not build the %s board: %s", sport.value, exc)
            return []

    boards = await asyncio.gather(*(one(sport) for sport in sports))
    return {card.game.id: card for board in boards for card in board}


async def _venue_quote(venue, card: Optional[GameCard], game: Game, side: str) -> Optional[dict]:
    """{price, cost} for one side on one exchange, or None.

    `price` is what the screen shows; `cost` adds the taker fee per share
    (rate x p x (1 - p) on all three, at the venue's standard rate).
    """
    price: Optional[float] = None
    if venue.book is not None:
        quote = None if card is None else next(
            (q for q in card.odds
             if q.book == venue.book and not is_in_play(q, game.start_time)),
            None,
        )
        american = getattr(quote, f"{side}_ml", None) if quote is not None else None
        if american is None:
            return None
        price = implied_prob(american)
    else:
        try:
            market = await venue.resolve(game.sport, game, side)
        except Exception as exc:  # noqa: BLE001 — discovery is best-effort
            logger.debug("compare: %s lookup failed: %s", venue.key, exc)
            return None
        price = market.prob if market is not None else None
    if price is None or not (0.0 < price < 1.0):
        return None
    fee = venue.fallback_fee_rate * price * (1.0 - price)
    return {"price": round(float(price), 4), "cost": round(float(price) + fee, 4)}


async def exchange_prices(picks: list[dict]) -> dict[str, dict[str, dict]]:
    """{"<game_id>|<side>": {venue: {"price", "cost"}}} for home/away picks.

    A venue that does not list the game, or whose quote is from a game already
    under way, is simply absent. Never raises: a comparison that cannot be
    read is an empty one, and the caller posts the pick without it.
    """
    wanted: list[tuple[dict, Game]] = []
    for row in picks[:MAX_PICKS]:
        if not isinstance(row, dict) or row.get("side") not in ("home", "away"):
            continue
        game = _game(row)
        if game is not None:
            wanted.append((row, game))
    if not wanted:
        return {}

    cards = await _cards({game.sport for _, game in wanted})
    out: dict[str, dict[str, dict]] = {}
    for row, game in wanted:
        side = row["side"]
        card = cards.get(game.id)
        # The board's own Game carries the names every venue matcher was
        # written against; the pick row's is the fallback.
        target = card.game if card is not None else game
        quotes: dict[str, dict] = {}
        for venue in venues_mod.VENUES.values():
            quote = await _venue_quote(venue, card, target, side)
            if quote is not None:
                quotes[venue.key] = quote
        if quotes:
            out[key(game.id, side)] = quotes
    return out


def cheapest(quotes: dict[str, dict]) -> Optional[str]:
    """The exchange with the lowest all-in cost. With one exchange there is
    nothing to compare, but it is still where the pick can be bought."""
    if not quotes:
        return None
    return min(quotes, key=lambda venue: (quotes[venue]["cost"], venue))


def _border_rows(picks: list[dict]) -> list[dict]:
    """Picks whose probability is just over the cap: worth pricing, because
    one exchange may sell them at the cap or under. Closest to the cap first,
    at most MAX_RESCUE_LOOKUPS."""
    rows = []
    for row in _one_per_game(picks[:MAX_CANDIDATES]):
        prob = pick_prob(row)
        if row.get("side") not in ("home", "away") or prob is None:
            continue
        if not (CARD_PROB_MAX < prob <= CARD_RESCUE_MAX):
            continue
        if _tier(row) not in VALUE_TIERS:
            model = _unit(row.get("model_prob"))
            if _unit(row.get("fair_prob")) is None or model is None or model < 0.5:
                continue  # would not make the card as a favourite anyway
        rows.append(row)
    rows.sort(key=lambda r: (pick_prob(r), str(r.get("kickoff") or "")))
    return rows[:MAX_RESCUE_LOOKUPS]


async def daily_card(picks: list[dict], limit: int = DEFAULT_CARD) -> dict:
    """{"card": [...], "candidates": n}: the selected picks, each with `prob`,
    `exchanges` ({venue: {price, cost}}) and `best` (the cheapest venue
    after fees, or None when no exchange lists it). `candidates` is how many
    games the card was chosen from."""
    border = _border_rows(picks)
    prices = await exchange_prices(border) if border else {}
    rescued = frozenset(
        k for k, quotes in prices.items()
        if min(q["price"] for q in quotes.values()) <= CARD_PROB_MAX
    )
    card = select_card(picks, limit, rescued)
    unpriced = [row for row in card if _row_key(row) not in prices]
    if unpriced:
        prices = {**prices, **(await exchange_prices(unpriced))}
    for row in card:
        quotes = prices.get(_row_key(row), {})
        row["exchanges"] = quotes
        row["best"] = cheapest(quotes)
    return {"card": card, "candidates": len(_one_per_game(picks[:MAX_CANDIDATES]))}
