"""What the MCP server says about itself: name, instructions, tool definitions.

Pure data. Nothing here may import from the rest of kryptscout — the stdio
relay (stdio.py) loads this module to answer `initialize` and `tools/list`
while the app is closed, in a process that must stay small and must never
write to stdout.
"""
from __future__ import annotations

SERVER_NAME = "krypt-scout"
SERVER_TITLE = "Krypt Scout"

# The revisions that open with an initialize handshake, newest first. A client
# asking for one of these gets it back; anything else gets our newest, and the
# client decides whether it can live with that.
PROTOCOL_VERSIONS = ("2025-11-25", "2025-06-18", "2025-03-26", "2024-11-05")
# 2026-07-28 on: no handshake and no sessions. Every request names its version
# and the client's capabilities in _meta and is served on its own.
MODERN_VERSIONS = ("2026-07-28",)
SUPPORTED_VERSIONS = MODERN_VERSIONS + PROTOCOL_VERSIONS

CAPABILITIES = {"tools": {"listChanged": False}}

SPORTS = ("nba", "nhl", "nfl", "mlb", "wnba", "ncaaf", "ncaab", "soccer", "tennis")

INSTRUCTIONS = """\
You are connected to Krypt Scout, a sports odds terminal running on the user's
own computer. It shows nine sports: nba, nhl, nfl, mlb, wnba, ncaaf, ncaab,
soccer, tennis.

Start with get_status, then get_board for a sport.

What the numbers are — read this before quoting any of them:
- `forecast` is the probability the app displays. When a market exists it IS
  the market's number (kind "market": Kalshi or Polymarket devigged, or a
  sportsbook flagged sharp=false). Only before any venue quotes is it the
  engine's own estimate (kind "model"), shown with its measured error.
- `model` is the engine's own rating-based probability (Elo, Kalman, Poisson).
  It is context. Measured over thousands of games it does not beat the closing
  line in any sport, so never present a model-vs-market gap as an edge.
- `value` rows are a sportsbook price sitting at least one probability point
  under the exchange fair. Tier STRONG means Kalshi set the fair, LEAN means
  Polymarket did. Beating a sportsbook's closing line is not the same as
  beating its margin: get_pick_record has what these picks have actually
  returned, and that record is the answer if asked whether they win.
- `arb` is the only guaranteed thing here: every outcome bought across venues
  for less than the payout, after exchange fees. It is rare and short-lived.
- `lean` is a tracked reference (which side the exchanges say a sportsbook
  has underpriced), not a pick.

Conventions:
- Probabilities are 0 to 1. Prices are American odds (-150, +130).
- Exchange prices are executable asks, not midpoints.
- Times are UTC, ISO 8601. Every probability is PRE-GAME: once `started` is
  true the prices on a row are live and no longer comparable to it.
- A null means nobody could produce the value. It is not zero.
- game_id comes from get_board; use it as given.

You can read everything and keep the user's own bet log (log_bet, delete_bet).

Betting. place_bet is OFF unless the user turned it on in the app (AI AGENTS
page): get_status says which mode you are in — off, paper or live — and your
caps. Paper bets are filled off the real order book and move no money. The
loop is get_bet_prices for the game, then place_bet with YOUR OWN probability
that the side wins. A bet is refused when the price is above what that
probability supports after the exchange's fee, above the user's caps, on a
game that already has a bet, or without a readable order book. A refusal says
why; do not split a bet to get under a cap, and do not retry a bet whose
status is "unknown" — tell the user to check their account.

Never place a live bet the user did not ask for in this conversation. Do not
tell the user a bet is "safe", "a lock" or "+EV" on the strength of anything
here: this app's own tracking has not shown these bets to be profitable.
"""


def _sport(description: str = "One of the nine sports.") -> dict:
    return {"type": "string", "enum": list(SPORTS), "description": description}


_DATE = {"type": "string", "description": "A date, YYYY-MM-DD.", "pattern": r"^\d{4}-\d{2}-\d{2}$"}
_NO_ARGS = {"type": "object", "properties": {}, "additionalProperties": False}


def _tool(name: str, title: str, description: str, schema: dict, *, writes: bool = False,
          external: bool = False) -> dict:
    """`external` marks a tool that reaches outside the app (an exchange)."""
    return {
        "name": name,
        "title": title,
        "description": description,
        "inputSchema": schema,
        "annotations": {
            "title": title,
            "readOnlyHint": not writes,
            "destructiveHint": False,
            "idempotentHint": not writes,
            "openWorldHint": external,
        },
    }


TOOLS: list[dict] = [
    _tool(
        "get_status", "App status",
        "Version, which sport engines are loaded, whether the license is active, "
        "and the state of auto-betting. Call this first.",
        _NO_ARGS,
    ),
    _tool(
        "get_board", "Board for one sport",
        "The slate for one sport over the next 7 days, soonest first: each game "
        "with the displayed forecast, the engine's own probability, the sharp "
        "fair, every venue's price, and any value / arb / lean signal on it. "
        "Fetches live prices, so allow a few seconds.",
        {
            "type": "object",
            "properties": {
                "sport": _sport(),
                "only": {
                    "type": "string",
                    "enum": ["all", "upcoming", "priced", "value", "arb", "lean"],
                    "description": "Filter: upcoming = not started yet; priced = at least one "
                                   "venue quotes it; value / arb / lean = games carrying that "
                                   "signal. Default all, which includes games already under way "
                                   "or finished today.",
                },
                "limit": {"type": "integer", "minimum": 1, "maximum": 200,
                          "description": "Most games to return. Default 40."},
            },
            "required": ["sport"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "get_game", "One game in full",
        "Everything on one game: the full board row, each book's recorded line "
        "movement, and (NFL/MLB outdoor venues) the forecast weather.",
        {
            "type": "object",
            "properties": {"sport": _sport(), "game_id": {"type": "string"}},
            "required": ["sport", "game_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "get_picks", "Recorded value picks",
        "The value picks the app recorded for one day (default today, UTC), "
        "with tier, price, the fair they were measured against and, once "
        "graded, the result.",
        {
            "type": "object",
            "properties": {
                "date": _DATE,
                "sport": _sport("Only this sport."),
                "tiers": {"type": "array", "items": {"type": "string", "enum": ["STRONG", "LEAN", "MODEL"]},
                          "description": "Only these tiers."},
            },
            "additionalProperties": False,
        },
    ),
    _tool(
        "get_pick_record", "How the picks have done",
        "The app's own track record: every graded value pick with flat-stake "
        "P&L, closing-line value by tier, and return on investment. This is "
        "the honest answer to \"do these picks win\".",
        {
            "type": "object",
            "properties": {"sport": _sport("Only this sport."), "since": _DATE},
            "additionalProperties": False,
        },
    ),
    _tool(
        "get_model_record", "How the engines have done",
        "Accuracy, log loss and Brier score of each engine over settled games, "
        "next to ESPN's predictor and the market on the same games.",
        {
            "type": "object",
            "properties": {"sport": _sport("Only this sport."), "since": _DATE},
            "additionalProperties": False,
        },
    ),
    _tool(
        "get_arbs", "Arbitrage sightings",
        "Cross-venue arbitrages the app has logged, newest first: the legs, "
        "the gross margin and the margin after exchange fees.",
        {
            "type": "object",
            "properties": {"limit": {"type": "integer", "minimum": 1, "maximum": 200,
                                     "description": "Default 25."}},
            "additionalProperties": False,
        },
    ),
    _tool(
        "get_line_movement", "Line movement for one game",
        "Every recorded price for one game, per book, oldest first.",
        {
            "type": "object",
            "properties": {"game_id": {"type": "string"}},
            "required": ["game_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "get_injuries", "Injury report",
        "ESPN's current injury report for one sport, by team.",
        {
            "type": "object",
            "properties": {"sport": _sport(), "team": {"type": "string",
                           "description": "Only teams whose name contains this text."}},
            "required": ["sport"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "get_my_bets", "The user's bet log",
        "Bets the user logged in the app, with results, units won, return on "
        "investment and closing-line value.",
        {
            "type": "object",
            "properties": {"since": _DATE},
            "additionalProperties": False,
        },
    ),
    _tool(
        "log_bet", "Log a bet the user placed",
        "Record a bet the USER says they placed, so the app grades it and "
        "tracks its closing-line value. This places nothing anywhere. The game "
        "must be on a board the app has served (call get_board first).",
        {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "side": {"type": "string", "enum": ["home", "away", "draw", "over", "under"]},
                "price": {"type": "integer", "description": "American odds, e.g. -110 or 135."},
                "book": {"type": "string", "description": "Where it was placed, e.g. draftkings."},
                "stake_units": {"type": "number", "minimum": 0.1, "maximum": 100,
                                "description": "Stake in units. Default 1."},
                "line": {"type": "number", "description": "The total line. Required for over / under."},
                "note": {"type": "string", "maxLength": 200},
            },
            "required": ["game_id", "side", "price"],
            "additionalProperties": False,
        },
        writes=True,
    ),
    _tool(
        "delete_bet", "Remove a logged bet",
        "Remove a bet from the user's log. Only a bet that has not been graded "
        "yet can be removed.",
        {
            "type": "object",
            "properties": {"bet_id": {"type": "integer"}},
            "required": ["bet_id"],
            "additionalProperties": False,
        },
        writes=True,
    ),
    _tool(
        "get_bet_prices", "Live prices for one game",
        "What Polymarket, Kalshi and Polymarket US would each charge for each "
        "side of one game right now, read off their live order books: best "
        "ask in cents, size at that price, the fee, the win probability at "
        "which the price breaks even, and (when another exchange prices the "
        "game) that exchange's fair probability. Also your betting mode and "
        "caps. Call this before place_bet.",
        {
            "type": "object",
            "properties": {"game_id": {"type": "string"}},
            "required": ["game_id"],
            "additionalProperties": False,
        },
    ),
    _tool(
        "place_bet", "Place one bet",
        "Buy one side of one game on one exchange, immediately, at a capped "
        "price. Works only when the user switched agent betting on in the "
        "app. In paper mode (or with paper=true) nothing is sent: the bet is "
        "recorded at the price the live book would have filled. In live mode "
        "this spends the user's real money. Returns status paper, placed, "
        "no_fill or unknown.",
        {
            "type": "object",
            "properties": {
                "game_id": {"type": "string"},
                "side": {"type": "string", "enum": ["home", "away"]},
                "venue": {"type": "string", "enum": ["polymarket", "kalshi", "polymarket_us"]},
                "usd": {"type": "number", "minimum": 1,
                        "description": "Dollars to spend. Must be within the user's per-bet cap."},
                "max_price_cents": {"type": "integer", "minimum": 1, "maximum": 99,
                                    "description": "The most to pay per share, in cents."},
                "win_prob": {"type": "number", "minimum": 0.01, "maximum": 0.99,
                             "description": "Your own probability that this side wins. The "
                                            "order is capped at the price this supports "
                                            "after fees."},
                "rationale": {"type": "string", "maxLength": 300,
                              "description": "One or two sentences on why. Kept in the "
                                             "app's audit log for the user."},
                "paper": {"type": "boolean",
                          "description": "Record as a paper bet even in live mode."},
            },
            "required": ["game_id", "side", "venue", "usd", "max_price_cents", "win_prob"],
            "additionalProperties": False,
        },
        writes=True, external=True,
    ),
    _tool(
        "get_autobet", "Auto-betting status",
        "Auto-bet settings, which exchanges are connected, your own betting "
        "mode and caps, today's activity and the most recent attempts (the "
        "rule engine's and agents') with what happened to each.",
        {
            "type": "object",
            "properties": {"log_limit": {"type": "integer", "minimum": 0, "maximum": 200,
                                         "description": "Recent attempts to include. Default 20."}},
            "additionalProperties": False,
        },
    ),
]

TOOL_NAMES = frozenset(t["name"] for t in TOOLS)
WRITE_TOOLS = frozenset(t["name"] for t in TOOLS if not t["annotations"]["readOnlyHint"])


def list_tools() -> list[dict]:
    return [dict(t) for t in TOOLS]
