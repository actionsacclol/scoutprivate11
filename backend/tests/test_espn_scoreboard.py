"""No-network unit tests for the ESPN scoreboard datasource.

Covers the catch-up fetch: ESPN's daily leagues (MLB, NBA, NHL, WNBA, NCAAB)
keep serving the previous day's slate well past noon ET, so an undated request
during the morning came back with nothing but yesterday's finals and the board
rendered empty. scoreboard() now notices the stale "day" and pulls today plus
CATCHUP_DAYS as a second request. Week-based (NFL, NCAAF) and idling-between-
fixtures (soccer, off-season) leagues must NOT trigger it — their default
response is already the right slate, and a forced window would shrink it.

_fetch is the monkeypatch seam, so nothing here touches the network.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import pytest

from kryptscout.core.schemas import Sport
from kryptscout.datasources import espn


def _event(event_id: str, when: str, home: str, away: str, completed: bool) -> dict:
    """One scoreboard event in ESPN's shape (trimmed to what _parse_event reads)."""
    return {
        "id": event_id,
        "date": when,
        "competitions": [
            {
                "status": {"type": {"completed": completed}},
                "competitors": [
                    {"homeAway": "home", "team": {"displayName": home}, "score": "4"},
                    {"homeAway": "away", "team": {"displayName": away}, "score": "1"},
                ],
            }
        ],
    }


def _payload(day: date | None, events: list[dict]) -> dict:
    """A scoreboard response; day=None is the week-based shape (NFL, NCAAF)."""
    body: dict = {"events": events}
    if day is not None:
        body["day"] = {"date": day.isoformat()}
    return body


class _Recorder:
    """Stands in for espn._fetch, serving canned payloads in order."""

    def __init__(self, *payloads: dict) -> None:
        self._payloads = list(payloads)
        self.calls: list[dict] = []

    async def __call__(self, client, path: str, params: dict) -> dict:
        self.calls.append(params)
        return self._payloads[min(len(self.calls) - 1, len(self._payloads) - 1)]


def _run(sport: Sport, recorder: _Recorder, on: date | None = None):
    return asyncio.run(espn.scoreboard(sport, on))


@pytest.fixture
def today() -> date:
    """The Eastern date scoreboard() itself will compute."""
    return datetime.now(espn._EASTERN).date()


class TestLagsToday:
    """The pure predicate, on fixed dates — no clock involved."""

    TODAY = date(2026, 8, 17)

    def test_yesterday_lags(self):
        assert espn._lags_today(_payload(date(2026, 8, 16), []), self.TODAY)

    def test_today_does_not_lag(self):
        assert not espn._lags_today(_payload(self.TODAY, []), self.TODAY)

    def test_future_day_does_not_lag(self):
        # Soccer between fixtures, or an off-season league, reports the next
        # matchday — nothing is missing, so no second request.
        assert not espn._lags_today(_payload(date(2026, 8, 21), []), self.TODAY)

    def test_missing_day_does_not_lag(self):
        # NFL / NCAAF are week-based and carry no "day" key.
        assert not espn._lags_today(_payload(None, []), self.TODAY)

    def test_unparseable_day_does_not_lag(self):
        assert not espn._lags_today({"day": {"date": "not-a-date"}}, self.TODAY)


class TestScoreboardCatchup:
    def test_stale_day_pulls_today_and_merges(self, monkeypatch, today):
        """Yesterday's finals stay; today's scheduled games arrive alongside."""
        yesterday = today - timedelta(days=1)
        recorder = _Recorder(
            _payload(
                yesterday,
                [_event("1", f"{yesterday}T23:20Z", "Rays", "Orioles", True)],
            ),
            _payload(
                today,
                [_event("2", f"{today}T17:40Z", "Reds", "Cardinals", False)],
            ),
        )
        monkeypatch.setattr(espn, "_fetch", recorder)

        games = _run(Sport.MLB, recorder)

        assert len(recorder.calls) == 2
        assert "dates" not in recorder.calls[0]
        end = today + timedelta(days=espn.CATCHUP_DAYS)
        assert recorder.calls[1]["dates"] == f"{today:%Y%m%d}-{end:%Y%m%d}"
        assert {g.id for g in games} == {"mlb-1", "mlb-2"}
        # The point of the fix: something bettable is on the board.
        assert [g.id for g in games if not g.completed] == ["mlb-2"]

    def test_duplicate_events_collapse_on_id(self, monkeypatch, today):
        """A game listed in both responses appears once."""
        yesterday = today - timedelta(days=1)
        dupe = _event("7", f"{today}T17:40Z", "Reds", "Cardinals", False)
        recorder = _Recorder(
            _payload(yesterday, [dupe]),
            _payload(today, [dupe]),
        )
        monkeypatch.setattr(espn, "_fetch", recorder)

        games = _run(Sport.MLB, recorder)

        assert len(recorder.calls) == 2
        assert [g.id for g in games] == ["mlb-7"]

    def test_current_day_is_one_request(self, monkeypatch, today):
        recorder = _Recorder(
            _payload(today, [_event("3", f"{today}T17:40Z", "Reds", "Cards", False)])
        )
        monkeypatch.setattr(espn, "_fetch", recorder)

        games = _run(Sport.MLB, recorder)

        assert len(recorder.calls) == 1
        assert [g.id for g in games] == ["mlb-3"]

    def test_week_based_league_is_left_alone(self, monkeypatch, today):
        """NFL's default spans its week; a forced day window would shrink it."""
        recorder = _Recorder(
            _payload(None, [_event("4", f"{today}T00:20Z", "Bills", "Jets", False)])
        )
        monkeypatch.setattr(espn, "_fetch", recorder)

        games = _run(Sport.NFL, recorder)

        assert len(recorder.calls) == 1
        assert [g.id for g in games] == ["nfl-4"]

    def test_future_day_league_is_left_alone(self, monkeypatch, today):
        """Soccer idling between fixtures already points at the next matchday."""
        ahead = today + timedelta(days=4)
        recorder = _Recorder(
            _payload(ahead, [_event("5", f"{ahead}T14:00Z", "Arsenal", "Chelsea", False)])
        )
        monkeypatch.setattr(espn, "_fetch", recorder)

        games = _run(Sport.SOCCER, recorder)

        # One request per configured soccer league, and no catch-up on any.
        assert len(recorder.calls) == len(espn.LEAGUE_PATHS[Sport.SOCCER])
        assert all("dates" not in c for c in recorder.calls)

    def test_explicit_date_never_catches_up(self, monkeypatch, today):
        """Backfill asks for one specific day and must get exactly that."""
        wanted = date(2026, 8, 14)
        recorder = _Recorder(
            _payload(
                wanted - timedelta(days=1),  # even a stale-looking day
                [_event("6", f"{wanted}T17:40Z", "Reds", "Cards", True)],
            )
        )
        monkeypatch.setattr(espn, "_fetch", recorder)

        games = _run(Sport.MLB, recorder, on=wanted)

        assert len(recorder.calls) == 1
        assert recorder.calls[0]["dates"] == "20260814"
        assert [g.id for g in games] == ["mlb-6"]

    def test_college_params_survive_the_catchup(self, monkeypatch, today):
        """groups/limit must ride on the second request too, or it returns Top-25."""
        recorder = _Recorder(_payload(today - timedelta(days=1), []))
        monkeypatch.setattr(espn, "_fetch", recorder)

        _run(Sport.NCAAB, recorder)

        assert len(recorder.calls) == 2
        for call in recorder.calls:
            assert call["groups"] == "50"
            assert call["limit"] == "400"
