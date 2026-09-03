"""Tests for shared core: odds math, the Elo engine, and the in-play guard."""
import math
from datetime import datetime, timedelta, timezone

import pytest

from kryptscout.core import oddsmath
from kryptscout.core.elo import EloBook, EloConfig
from kryptscout.core.schemas import MoneylineOdds, is_in_play


class TestPowerDevig:
    def test_sums_to_one(self):
        assert oddsmath.remove_vig_power(-110, -110) == pytest.approx([0.5, 0.5])
        assert sum(oddsmath.remove_vig_power(-120, +250, +320)) == pytest.approx(1.0)

    def test_gives_the_longshot_less_than_multiplicative(self):
        """Favourite-longshot bias: proportional devig hands the dog too much
        probability, power devig shaves more off it. Measured on 277 live
        picks the difference alone was a +0.9% phantom edge on underdogs."""
        mult = oddsmath.remove_vig(-300, +240)
        power = oddsmath.remove_vig_power(-300, +240)
        assert power[1] < mult[1] and power[0] > mult[0]

    def test_arb_market_solves_below_k_one(self):
        assert oddsmath.remove_vig_power(+105, +105) == pytest.approx([0.5, 0.5])

    def test_rejects_degenerate_prices(self):
        with pytest.raises(ValueError):
            oddsmath.remove_vig_power()


class TestOddsMath:
    def test_american_decimal_roundtrip(self):
        for odds in (-500, -150, -110, +100, +130, +250, +1200):
            dec = oddsmath.american_to_decimal(odds)
            assert oddsmath.decimal_to_american(dec) == odds

    def test_even_money(self):
        assert oddsmath.american_to_decimal(100) == 2.0
        assert oddsmath.implied_prob(100) == pytest.approx(0.5)

    def test_implied_prob_favorite(self):
        assert oddsmath.implied_prob(-150) == pytest.approx(0.6)
        assert oddsmath.implied_prob(150) == pytest.approx(0.4)

    def test_remove_vig_two_way(self):
        probs = oddsmath.remove_vig(-110, -110)
        assert probs == pytest.approx([0.5, 0.5])
        assert sum(probs) == pytest.approx(1.0)

    def test_remove_vig_three_way(self):
        probs = oddsmath.remove_vig(-120, +250, +320)
        assert sum(probs) == pytest.approx(1.0)
        assert probs[0] > probs[1] > probs[2]

    def test_expected_value_sign(self):
        # 55% true prob at +100 is +EV; at -130 it is -EV
        assert oddsmath.expected_value(0.55, 100) > 0
        assert oddsmath.expected_value(0.55, -130) < 0

    def test_no_edge_no_kelly(self):
        assert oddsmath.kelly_fraction(0.5, -110) == 0.0

    def test_kelly_known_value(self):
        # p=0.6 at even money: f = (0.6*2 - 1)/1 = 0.2
        assert oddsmath.kelly_fraction(0.6, 100) == pytest.approx(0.2)

    def test_prob_to_american(self):
        assert oddsmath.prob_to_american(0.5) == 100  # decimal 2.0 maps to +100
        assert oddsmath.prob_to_american(0.6) == -150
        assert oddsmath.prob_to_american(0.25) == 300

    def test_zero_odds_rejected(self):
        with pytest.raises(ValueError):
            oddsmath.american_to_decimal(0)


class TestElo:
    def test_equal_teams_even_odds(self):
        book = EloBook()
        assert book.expected("A", "B") == pytest.approx(0.5)

    def test_home_advantage(self):
        book = EloBook(EloConfig(home_advantage=100))
        assert book.expected("A", "B") > 0.5

    def test_winner_gains_loser_drops(self):
        book = EloBook()
        book.update("A", "B", home_won=True)
        assert book.get("A") > 1500 > book.get("B")

    def test_zero_sum(self):
        book = EloBook(EloConfig(mov_scaling=True))
        for i, (home_won, margin) in enumerate([(True, 12), (False, 3), (True, 30)]):
            book.update("A", "B", home_won=home_won, margin=margin)
        assert book.get("A") + book.get("B") == pytest.approx(3000.0)

    def test_upset_moves_more_than_expected_win(self):
        book = EloBook()
        book.ratings = {"strong": 1700.0, "weak": 1300.0}
        expected_win = EloBook(config=book.config, ratings=dict(book.ratings))
        expected_win.update("strong", "weak", home_won=True)
        upset = EloBook(config=book.config, ratings=dict(book.ratings))
        upset.update("strong", "weak", home_won=False)
        gain_on_expected = expected_win.get("strong") - 1700.0
        loss_on_upset = 1700.0 - upset.get("strong")
        assert loss_on_upset > gain_on_expected

    def test_convergence_beats_coinflip(self):
        # A rigged league: "good" beats "bad" 3 of every 4 games.
        book = EloBook(EloConfig(k=32))
        for i in range(200):
            book.update("good", "bad", home_won=(i % 4 != 0))
        prob = book.expected("good", "bad")
        assert 0.6 < prob < 0.9

    def test_season_carry_regresses_to_mean(self):
        book = EloBook(EloConfig(season_carry=0.75))
        book.ratings = {"A": 1700.0, "B": 1300.0}
        book.new_season()
        assert book.get("A") == pytest.approx(1650.0)
        assert book.get("B") == pytest.approx(1350.0)

    def test_draw_soft_update(self):
        book = EloBook()
        book.ratings = {"fav": 1600.0, "dog": 1400.0}
        book.update("fav", "dog", home_won=False, draw=True)
        # A draw should cost the favorite rating, but less than a loss would
        assert book.get("fav") < 1600.0
        loss_book = EloBook(config=book.config, ratings={"fav": 1600.0, "dog": 1400.0})
        loss_book.update("fav", "dog", home_won=False)
        assert loss_book.get("fav") < book.get("fav")


class TestInPlayGuard:
    """A live price must never be compared to a pre-game model probability."""

    KICKOFF = datetime(2026, 7, 26, 23, 0, tzinfo=timezone.utc)

    def _quote(self, book: str, fetched: datetime) -> MoneylineOdds:
        return MoneylineOdds(book=book, fetched_at=fetched, home_ml=-120, away_ml=100)

    def test_named_live_feed_is_in_play_regardless_of_time(self):
        early = self._quote("draftkings_live_odds", self.KICKOFF - timedelta(hours=3))
        assert is_in_play(early, self.KICKOFF)
        assert is_in_play(early)  # even with no kickoff known

    def test_pregame_quote_before_kickoff_is_not_in_play(self):
        assert not is_in_play(
            self._quote("draftkings", self.KICKOFF - timedelta(minutes=1)), self.KICKOFF
        )

    def test_any_book_quoted_after_kickoff_is_in_play(self):
        # Polymarket and Kalshi keep trading during the game.
        for book in ("polymarket", "kalshi", "draftkings"):
            quote = self._quote(book, self.KICKOFF + timedelta(minutes=1))
            assert is_in_play(quote, self.KICKOFF), book

    def test_quote_exactly_at_kickoff_is_in_play(self):
        assert is_in_play(self._quote("kalshi", self.KICKOFF), self.KICKOFF)

    def test_naive_datetimes_are_treated_as_utc(self):
        # Mixed naive/aware inputs must not raise; naive means UTC here.
        naive_quote = MoneylineOdds(
            book="kalshi",
            fetched_at=self.KICKOFF.replace(tzinfo=None) + timedelta(minutes=5),
            home_ml=-120,
            away_ml=100,
        )
        assert is_in_play(naive_quote, self.KICKOFF)
        assert is_in_play(naive_quote, self.KICKOFF.replace(tzinfo=None))

    def test_without_kickoff_ordinary_books_pass(self):
        assert not is_in_play(self._quote("polymarket", self.KICKOFF))
