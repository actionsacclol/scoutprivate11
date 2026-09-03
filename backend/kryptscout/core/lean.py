"""Exchange lead — a sharp reference the app measured, not modelled.

Kalshi and Polymarket move before DraftKings. Measured 2026-09-02 on 233
live non-soccer games with DraftKings quoted ~8h before kickoff: the side
Kalshi favoured relative to DraftKings' devigged price predicted the
direction DraftKings' CLOSE moved 64% of the time (95% CI 58–70%; the naive
"favourite shortens" rule manages 57%) and earned +1.05% ± 0.30 of SAME-BOOK
closing-line value at DraftKings — +1.37% ± 0.42 with 6h+ of lead, +1.06% in
MLB alone, stable across both halves of the sample, present on underdogs.
Polymarket alone: 59%, +0.87%. Both exchanges agreeing: 64%, +1.15%. Neither
the cross-venue nor the devig confound applies: entry and close are the same
DraftKings feed. Every model this repo has ever shipped scores zero on the
same test.

What it is NOT: a profit. +1% sits under DraftKings' ~2.2% per-side vig, so
realised ROI at DraftKings is indistinguishable from zero, and the ESPN-
relayed DraftKings price may lag the real board. What it IS: the first
reference price in this codebase that identifies which venue is wrong — line
shopping against a consensus of three correlated venues lost 2.6% for want of
exactly this — and the benchmark any soft or stale line should be judged
against. So it is recorded at first sight (db.upsert_lean), scored against
the declared close (tracker.lean_clv), shown on the card, and NOT fed to
picks or autobet until its live same-book CLV clears the vig at n ≥ 70.
"""
from __future__ import annotations

from datetime import datetime
from typing import Optional

from kryptscout.core.fair import is_thin
from kryptscout.core.oddsmath import remove_vig, remove_vig_power
from kryptscout.core.schemas import Lean, MoneylineOdds, Sport, is_in_play

TARGET_BOOK = "draftkings"
# Preference order. Kalshi led DraftKings more reliably than Polymarket in
# the measurement (64% vs 59% directional); when both quote, both must agree.
REFERENCE_BOOKS = ("kalshi", "polymarket")
# Devigged probability points the reference must sit from DraftKings before
# a lean is called. The observed mean |gap| was 0.77 pts; a 1-point floor
# keeps spread noise on a 1c book from minting leans.
MIN_GAP_PTS = 1.0


def _two_way(q: MoneylineOdds) -> bool:
    return q.home_ml is not None and q.away_ml is not None and q.draw_ml is None


def find_lean(
    game_id: str,
    sport: Sport,
    odds_list: list[MoneylineOdds],
    starts_at: Optional[datetime] = None,
) -> Optional[Lean]:
    """Which DraftKings side the exchanges say is underpriced, or None.

    Two-way markets only (the measurement excluded soccer). DraftKings is
    devigged by the power method (a sportsbook pair); exchange asks are
    devigged proportionally, which on a 1-2c book is the midpoint. The
    primary reference is the first of REFERENCE_BOOKS present; if the other
    is also quoting and points the other way there is no lean.
    """
    pre = [q for q in odds_list if not is_in_play(q, starts_at)]
    target = next((q for q in pre if q.book == TARGET_BOOK and _two_way(q)), None)
    if target is None:
        return None
    refs: dict[str, MoneylineOdds] = {}
    for q in pre:
        # A thin exchange book is the likeliest reason for a large gap, and
        # a stale reference is no reference: skip it rather than lean on it.
        if q.book in REFERENCE_BOOKS and _two_way(q) and not is_thin(q):
            refs.setdefault(q.book, q)
    if not refs:
        return None
    dk_home = remove_vig_power(target.home_ml, target.away_ml)[0]
    gaps = {
        book: (_ref_home(q) - dk_home) * 100.0
        for book, q in refs.items()
    }
    primary = next(book for book in REFERENCE_BOOKS if book in gaps)
    gap = gaps[primary]
    if abs(gap) < MIN_GAP_PTS:
        return None
    if any((other > 0) != (gap > 0) for other in gaps.values()):
        return None  # the two exchanges disagree: no reference to lean on
    side = "home" if gap > 0 else "away"
    idx = 0 if side == "home" else 1
    ref = refs[primary]
    return Lean(
        game_id=game_id,
        sport=sport,
        side=side,
        book=TARGET_BOOK,
        price=target.home_ml if side == "home" else target.away_ml,
        home_ml=target.home_ml,
        away_ml=target.away_ml,
        book_prob=dk_home if side == "home" else 1.0 - dk_home,
        ref_book=primary,
        ref_prob=_ref_home(ref) if side == "home" else 1.0 - _ref_home(ref),
        gap_pts=abs(gap),
        agree=len(gaps) == 2,
    )


def _ref_home(q: MoneylineOdds) -> float:
    """The reference's home probability: microprice when the book was read,
    else the proportional devig of its asks (the midpoint)."""
    if q.microprice_home is not None:
        return q.microprice_home
    return remove_vig(q.home_ml, q.away_ml)[0]
