"""No-network unit tests for the ESPN scoreboard datasource.

Covers the catch-up fetch: ESPN's daily leagues (MLB, NBA, NHL, WNBA, NCAAB)
keep serving the previous day's slate well past noon ET, so an undated request
during the morning came back with nothing but yesterday's finals and the board
rendered empty. scoreboard() now notices the stale "day" and pulls today plus
CATCHUP_DAYS, one request per day. Week-based (NFL, NCAAF) and idling-between-
fixtures (soccer, off-season) leagues must NOT trigger it — their default
response is already the right slate, and a forced window would shrink it.

One request PER DAY, never a `dates=A-B` range: ESPN stopped accepting ranges
in November 2026 (400 on every league), which took the NBA/NHL/NCAAB boards
down every morning and MLB/WNBA/soccer down for as long as they idled.

Also covers the failure handling that outage exposed: catch-up days are
best-effort, soccer leagues fail independently, and a sport that cannot be
reached at all is served from its last good slate for a few minutes.

_fetch is the monkeypatch seam, so nothing here touches the network.
"""
from __future__ import annotations

import asyncio
from datetime import date, datetime, timedelta

import httpx
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


@pytest.fixture(autouse=True)
def _fresh_slate_cache():
    """The last-good-slate cache is module state; no test may inherit one."""
    espn._last_slate.clear()
    yield
    espn._last_slate.clear()


def _status_error(code: int) -> httpx.HTTPStatusError:
    request = httpx.Request("GET", "https://site.api.espn.com/x")
    return httpx.HTTPStatusError(
        f"HTTP {code}", request=request, response=httpx.Response(code, request=request)
    )


def _catchup_days(today: date) -> list[str]:
    """The `dates` values the catch-up must request, in order."""
    return [f"{today + timedelta(days=n):%Y%m%d}" for n in range(espn.CATCHUP_DAYS + 1)]


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

        assert "dates" not in recorder.calls[0]
        assert [c["dates"] for c in recorder.calls[1:]] == _catchup_days(today)
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

        assert len(recorder.calls) == 2 + espn.CATCHUP_DAYS
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

        assert len(recorder.calls) == 2 + espn.CATCHUP_DAYS
        for call in recorder.calls:
            assert call["groups"] == "50"
            assert call["limit"] == "400"

    def test_catchup_never_sends_a_date_range(self, monkeypatch, today):
        """The regression itself: ESPN 400s `dates=YYYYMMDD-YYYYMMDD` on every
        league, so no request this module builds may contain one."""
        recorder = _Recorder(_payload(today - timedelta(days=1), []))
        monkeypatch.setattr(espn, "_fetch", recorder)

        for sport in (Sport.NBA, Sport.NHL, Sport.MLB, Sport.WNBA, Sport.NCAAB, Sport.SOCCER):
            _run(sport, recorder)

        dated = [c["dates"] for c in recorder.calls if "dates" in c]
        assert dated, "the stale day should have triggered a catch-up"
        assert all(len(d) == 8 and d.isdigit() for d in dated), dated


class TestScoreboardFailures:
    """What the range outage exposed: one bad request used to 500 the board."""

    def test_failed_catchup_keeps_the_default_slate(self, monkeypatch, today):
        """The catch-up is a bonus. Losing it must not lose the board."""
        yesterday = today - timedelta(days=1)
        default = _payload(
            yesterday, [_event("1", f"{yesterday}T23:20Z", "Rays", "Orioles", True)]
        )

        async def fetch(client, path, params):
            if "dates" in params:
                raise _status_error(400)
            return default

        monkeypatch.setattr(espn, "_fetch", fetch)

        games = asyncio.run(espn.scoreboard(Sport.MLB))

        assert [g.id for g in games] == ["mlb-1"]

    def test_one_failed_league_does_not_sink_the_sport(self, monkeypatch, today):
        """Ten soccer leagues answering must not be discarded because an
        eleventh failed."""
        async def fetch(client, path, params):
            if path == "soccer/eng.1":
                raise _status_error(503)
            league = path.split("/")[1]
            return _payload(
                today, [_event(league, f"{today}T14:00Z", f"{league} H", f"{league} A", False)]
            )

        monkeypatch.setattr(espn, "_fetch", fetch)

        games = asyncio.run(espn.scoreboard(Sport.SOCCER))

        assert len(games) == len(espn.LEAGUE_PATHS[Sport.SOCCER]) - 1
        assert "soccer-eng.1" not in {g.id for g in games}

    def test_total_failure_raises_when_nothing_is_cached(self, monkeypatch):
        async def fetch(client, path, params):
            raise _status_error(503)

        monkeypatch.setattr(espn, "_fetch", fetch)

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(espn.scoreboard(Sport.NBA))

    def test_total_failure_serves_the_last_good_slate(self, monkeypatch, today):
        """An ESPN blip leaves the board standing instead of blank."""
        good = _Recorder(
            _payload(today, [_event("9", f"{today}T23:00Z", "Knicks", "Heat", False)])
        )
        monkeypatch.setattr(espn, "_fetch", good)
        assert [g.id for g in _run(Sport.NBA, good)] == ["nba-9"]

        async def down(client, path, params):
            raise _status_error(503)

        monkeypatch.setattr(espn, "_fetch", down)

        assert [g.id for g in asyncio.run(espn.scoreboard(Sport.NBA))] == ["nba-9"]

    def test_partial_slate_is_not_cached(self, monkeypatch, today):
        """A slate built while a league was down would hide that league's
        games for the whole cache window if it were allowed to stand in."""
        async def fetch(client, path, params):
            if path == "soccer/eng.1":
                raise _status_error(503)
            return _payload(today, [])

        monkeypatch.setattr(espn, "_fetch", fetch)
        asyncio.run(espn.scoreboard(Sport.SOCCER))

        assert Sport.SOCCER not in espn._last_slate

    def test_stale_slate_expires(self, monkeypatch, today):
        """A cached slate is a bridge over a blip, not a replacement feed."""
        good = _Recorder(
            _payload(today, [_event("9", f"{today}T23:00Z", "Knicks", "Heat", False)])
        )
        monkeypatch.setattr(espn, "_fetch", good)
        _run(Sport.NBA, good)
        at, slate = espn._last_slate[Sport.NBA]
        espn._last_slate[Sport.NBA] = (at - espn.STALE_SLATE_MAX_AGE_S - 1, slate)

        async def down(client, path, params):
            raise _status_error(503)

        monkeypatch.setattr(espn, "_fetch", down)

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(espn.scoreboard(Sport.NBA))

    def test_explicit_date_never_falls_back_to_the_cache(self, monkeypatch, today):
        """Settlement asks for a specific day; the current slate is no answer."""
        good = _Recorder(
            _payload(today, [_event("9", f"{today}T23:00Z", "Knicks", "Heat", False)])
        )
        monkeypatch.setattr(espn, "_fetch", good)
        _run(Sport.NBA, good)

        async def down(client, path, params):
            raise _status_error(503)

        monkeypatch.setattr(espn, "_fetch", down)

        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(espn.scoreboard(Sport.NBA, date(2026, 8, 14)))


class TestFetchRetry:
    """_fetch itself, against a stub client."""

    class _Client:
        def __init__(self, *outcomes) -> None:
            self.outcomes = list(outcomes)
            self.calls = 0

        async def get(self, url, params=None):
            self.calls += 1
            outcome = self.outcomes[min(self.calls - 1, len(self.outcomes) - 1)]
            if isinstance(outcome, BaseException):
                raise outcome
            request = httpx.Request("GET", url)
            return httpx.Response(outcome[0], json=outcome[1], request=request)

    @pytest.fixture(autouse=True)
    def _no_backoff(self, monkeypatch):
        monkeypatch.setattr(espn, "RETRY_BACKOFF_S", 0.0)

    def test_gateway_timeout_is_retried(self):
        client = self._Client((504, {}), (200, {"events": []}))
        assert asyncio.run(espn._fetch(client, "basketball/nba", {})) == {"events": []}
        assert client.calls == 2

    def test_transport_error_is_retried(self):
        client = self._Client(httpx.ConnectTimeout("slow"), (200, {"events": []}))
        assert asyncio.run(espn._fetch(client, "basketball/nba", {})) == {"events": []}
        assert client.calls == 2

    def test_bad_request_is_not_retried(self):
        """A 400 is a request built wrong; asking again asks wrong again."""
        client = self._Client((400, {"code": 400}))
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(espn._fetch(client, "basketball/nba", {}))
        assert client.calls == 1

    def test_gives_up_after_the_last_attempt(self):
        client = self._Client((503, {}))
        with pytest.raises(httpx.HTTPStatusError):
            asyncio.run(espn._fetch(client, "basketball/nba", {}))
        assert client.calls == espn.FETCH_ATTEMPTS
