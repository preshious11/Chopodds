"""Tests for the daily odds cache and the multi-provider odds failover."""

import json
from datetime import datetime, timedelta, timezone

import pytest

import config
import daily_cache
import odds_client
import predictions

LEAGUES = {
    "Football": [
        {"key": "soccer_epl", "name": "English Premier League", "short": "EPL", "icon": "⚽"},
        {"key": "soccer_spain_la_liga", "name": "La Liga", "short": "La Liga", "icon": "⚽"},
        {"key": "soccer_turkey_super_league", "name": "Super Lig", "short": "Turkey", "icon": "⚽"},
    ],
    "Tennis": [
        {"key_prefix": "tennis_atp_", "name": "ATP Tour", "short": "ATP", "icon": "🎾"},
    ],
}
ACTIVE_SPORTS = ["soccer_epl", "soccer_spain_la_liga", "soccer_turkey_super_league",
                 "tennis_atp_shanghai", "basketball_nba"]


@pytest.fixture(autouse=True)
def isolated_state(monkeypatch, tmp_path):
    """Temp cache dir, reset cache state, no real sleeping, all provider keys set."""
    monkeypatch.setattr(daily_cache, "CACHE_DIR", tmp_path)
    monkeypatch.setattr(daily_cache, "_memory_cache", None)
    monkeypatch.setattr(daily_cache, "_last_failure", None)
    monkeypatch.setattr(odds_client.time, "sleep", lambda seconds: None)
    monkeypatch.setattr(config, "ODDS_API_KEY", "odds-key")
    monkeypatch.setattr(config, "SHARPAPI_API_KEY", "sharp-key")
    monkeypatch.setattr(config, "SPORTSGAMEODDS_API_KEY", "sgo-key")
    monkeypatch.setattr(config, "ODDS_FETCH_RETRY_COOLDOWN", 600)
    monkeypatch.setattr(
        config, "ODDS_PROVIDER_ORDER", ["the-odds-api", "sharpapi", "sportsgameodds"]
    )
    monkeypatch.setattr(predictions, "SPORT_KEY_MAP", LEAGUES)


class Response:
    def __init__(self, status, body=None, headers=None):
        self.status_code = status
        self._body = body
        self.headers = headers or {}
        self.text = json.dumps(body)

    def json(self):
        if self._body is None:
            raise ValueError("no JSON body")
        return self._body


class FakeHTTP:
    """Routes requests.get calls to handlers by URL fragment and records them."""

    def __init__(self, **handlers):
        self.handlers = handlers
        self.calls = []

    def __call__(self, url, params=None, headers=None, timeout=None):
        params = dict(params or {})
        self.calls.append((url, params, dict(headers or {})))
        for provider, fragment in (("odds_api", "the-odds-api.com"),
                                   ("sharpapi", "sharpapi.io"),
                                   ("sgo", "sportsgameodds.com")):
            if fragment in url:
                handler = self.handlers.get(provider)
                if handler is None:
                    raise AssertionError(f"{provider} should not have been called: {url}")
                return handler(url, params)
        raise AssertionError(f"unexpected request {url}")

    def requests_to(self, fragment):
        return [call for call in self.calls if fragment in call[0]]


def later_today_iso(hours_ahead=3):
    """A kick-off later today (Lagos), falling back to minutes ahead near midnight."""
    lagos_now = datetime.now(predictions.LAGOS_TZ)
    kickoff = lagos_now + timedelta(hours=hours_ahead)
    if kickoff.date() != lagos_now.date():
        kickoff = lagos_now + timedelta(minutes=5)
    return kickoff.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def odds_api_event(sport_key):
    return {"id": f"oa-{sport_key}", "home_team": "Home", "away_team": "Away",
            "commence_time": later_today_iso(), "bookmakers": []}


def odds_api(statuses=None, sports_status=200):
    """The Odds API handler. statuses: sport_key -> status or list of statuses."""
    statuses = {key: list(value) if isinstance(value, list) else value
                for key, value in (statuses or {}).items()}

    def handler(url, params):
        if url.endswith("/sports"):
            if sports_status != 200:
                return Response(sports_status, {"message": "nope"})
            return Response(200, [{"key": key} for key in ACTIVE_SPORTS])
        sport_key = url.split("/sports/")[1].split("/")[0]
        status = statuses.get(sport_key, 200)
        if isinstance(status, list):
            status = status.pop(0) if len(status) > 1 else status[0]
        if status == 200:
            return Response(200, [odds_api_event(sport_key)], {"x-requests-remaining": "400"})
        return Response(status, {"message": "limited"}, {"x-requests-remaining": "400"})

    return handler


def all_status(status):
    return {key: status for key in ACTIVE_SPORTS}


def sharp_rows(league, event_id="1", home="Arsenal", away="Chelsea", books=("dk", "fd", "mgm"),
               kickoff=None, sport="soccer"):
    rows = []
    for book in books:
        base = {"event_id": event_id, "sport": sport, "league": league, "home_team": home,
                "away_team": away, "sportsbook": book, "is_live": False, "is_main_line": True,
                "is_alternate_line": False, "event_start_time": kickoff or later_today_iso()}
        rows += [
            {**base, "market_type": "moneyline", "selection": home, "selection_type": "home",
             "odds_decimal": 1.3, "line": None},
            {**base, "market_type": "moneyline", "selection": "Draw", "selection_type": "draw",
             "odds_decimal": 5.0, "line": None},
            {**base, "market_type": "moneyline", "selection": away, "selection_type": "away",
             "odds_decimal": 9.0, "line": None},
            {**base, "market_type": "total_goals", "selection": "Over 2.5", "selection_type": "over",
             "odds_decimal": 1.9, "line": 2.5},
            {**base, "market_type": "total_goals", "selection": "Under 2.5",
             "selection_type": "under", "odds_decimal": 1.95, "line": 2.5},
        ]
    return rows


def sgo_event(league_id, event_id="E1", home="Real Madrid", away="Sevilla", status=None):
    def books(odds, **extra):
        return {book: {"odds": odds, "available": True, **extra}
                for book in ("fanduel", "draftkings", "bet365")}

    def entry(entity, bet, side, quotes, period="reg"):
        return {"statID": "points", "statEntityID": entity, "periodID": period,
                "betTypeID": bet, "sideID": side, "byBookmaker": quotes}

    return {
        "eventID": event_id, "leagueID": league_id,
        "status": status or {"started": False, "startsAt": later_today_iso()},
        "teams": {"home": {"names": {"long": home}}, "away": {"names": {"long": away}}},
        "odds": {
            "points-home-reg-ml3way-home": entry("home", "ml3way", "home", books("-150")),
            "points-all-reg-ml3way-draw": entry("all", "ml3way", "draw", books("+280")),
            "points-away-reg-ml3way-away": entry("away", "ml3way", "away", books("+400")),
            "points-home-game-ml-home": entry("home", "ml", "home", books("-300"), "game"),
            "points-away-game-ml-away": entry("away", "ml", "away", books("+220"), "game"),
            "points-all-reg-ou-over": entry("all", "ou", "over", books("-110", overUnder="2.5")),
            "points-all-reg-ou-under": entry("all", "ou", "under", books("-110", overUnder="2.5")),
            "points-all-game-ou-over": entry("all", "ou", "over",
                                             books("+150", overUnder="3.5"), "game"),
            "yellowCards-all-reg-ou-over": {**entry("all", "ou", "over", books("-120")),
                                            "statID": "yellowCards"},
        },
    }


# ---------------------------------------------------------------------------
# Daily cache
# ---------------------------------------------------------------------------

def test_successful_fetch_is_cached_and_served_without_refetching():
    calls = []

    def fetch():
        calls.append(1)
        return {"soccer_epl": [{"id": "e1"}]}

    assert daily_cache.ensure_populated(fetch) == {"soccer_epl": [{"id": "e1"}]}
    assert daily_cache.ensure_populated(fetch) == {"soccer_epl": [{"id": "e1"}]}
    assert len(calls) == 1
    assert daily_cache.validate_daily_cache()


def test_failed_fetch_is_not_retried_during_cooldown():
    calls = []

    def failing_fetch():
        calls.append(1)
        raise odds_client.OddsAPIError("quota exhausted")

    for _ in range(3):
        with pytest.raises(odds_client.OddsAPIError):
            daily_cache.ensure_populated(failing_fetch)
    assert len(calls) == 1
    assert not daily_cache.validate_daily_cache()


def test_force_refresh_clears_the_cooldown():
    def failing_fetch():
        raise odds_client.OddsAPIError("down")

    with pytest.raises(odds_client.OddsAPIError):
        daily_cache.ensure_populated(failing_fetch)
    daily_cache.clear_today_cache()
    assert daily_cache.ensure_populated(lambda: {"soccer_epl": []}) == {"soccer_epl": []}


# ---------------------------------------------------------------------------
# Provider failover
# ---------------------------------------------------------------------------

def test_healthy_odds_api_needs_no_fallback(monkeypatch):
    http = FakeHTTP(odds_api=odds_api())
    monkeypatch.setattr(odds_client.requests, "get", http)

    data = odds_client._fetch_all_sports_odds()

    assert set(data) == {"soccer_epl", "soccer_spain_la_liga",
                         "soccer_turkey_super_league", "tennis_atp_shanghai"}
    assert not http.requests_to("basketball_nba")
    report = odds_client.get_last_fetch_report()
    assert set(report["sources"].values()) == {"the-odds-api"}


def test_empty_league_on_odds_api_does_not_trigger_fallback(monkeypatch):
    def handler(url, params):
        if url.endswith("/sports"):
            return Response(200, [{"key": "soccer_epl"}])
        return Response(200, [])

    monkeypatch.setattr(odds_client.requests, "get", FakeHTTP(odds_api=handler))
    assert odds_client._fetch_all_sports_odds() == {"soccer_epl": []}


def test_odds_api_burst_rate_limit_is_retried_before_falling_back(monkeypatch):
    http = FakeHTTP(odds_api=odds_api({"soccer_epl": [429, 200]}))
    monkeypatch.setattr(odds_client.requests, "get", http)

    data = odds_client._fetch_all_sports_odds()

    assert len(http.requests_to("soccer_epl/odds")) == 2
    assert data["soccer_epl"][0]["id"] == "oa-soccer_epl"


def test_rate_limited_odds_api_fails_over_to_sharpapi_then_sportsgameodds(monkeypatch):
    def sharpapi(url, params):
        return Response(200, {"data": sharp_rows("england_-_premier_league"),
                              "pagination": {"next_cursor": None}})

    def sgo(url, params):
        return Response(200, {"data": [sgo_event("LA_LIGA")], "nextCursor": None})

    http = FakeHTTP(odds_api=odds_api(all_status(429)), sharpapi=sharpapi, sgo=sgo)
    monkeypatch.setattr(odds_client.requests, "get", http)

    data = odds_client._fetch_all_sports_odds()

    # The Odds API: first league retried 3 times, then abandoned for the day.
    assert len(http.requests_to("the-odds-api.com/v4/sports/")) == 4
    # SharpAPI: one request for every league it covers, documented slugs.
    (_, sharp_params, sharp_headers), = http.requests_to("sharpapi.io")
    assert sharp_headers == {"X-API-Key": "sharp-key"}
    assert sharp_params["league"] == "england_-_premier_league,spain_-_la_liga,atp"
    assert sharp_params["market"] == "moneyline,point_spread,total_goals"
    assert sharp_params["is_live"] == "false"
    # SportsGameOdds: only what is still missing, today's matches only.
    (_, sgo_params, _), = http.requests_to("sportsgameodds.com")
    assert sgo_params["leagueID"] == "LA_LIGA,ATP"
    assert sgo_params["apiKey"] == "sgo-key"
    assert sgo_params["startsAfter"] < sgo_params["startsBefore"]

    assert data["soccer_epl"][0]["id"] == "sharpapi:1"
    assert data["soccer_spain_la_liga"][0]["id"] == "sportsgameodds:E1"
    assert data["soccer_turkey_super_league"] == []
    report = odds_client.get_last_fetch_report()
    assert report["sources"] == {"soccer_epl": "sharpapi",
                                 "soccer_spain_la_liga": "sportsgameodds"}


def test_invalid_odds_api_key_skips_it_and_uses_generic_tennis_keys(monkeypatch):
    def sharpapi(url, params):
        rows = sharp_rows("atp", sport="tennis", home="Sinner", away="Alcaraz")
        return Response(200, {"data": rows})

    http = FakeHTTP(odds_api=odds_api(sports_status=401), sharpapi=sharpapi,
                    sgo=lambda url, params: Response(200, {"data": []}))
    monkeypatch.setattr(odds_client.requests, "get", http)

    data = odds_client._fetch_all_sports_odds()

    assert not http.requests_to("the-odds-api.com/v4/sports/")
    assert data["tennis_atp"][0]["home_team"] == "Sinner"
    assert "401" in odds_client.get_last_fetch_report()["providers"]["the-odds-api"]["error"]


def test_missing_fallback_keys_are_reported(monkeypatch):
    monkeypatch.setattr(config, "SHARPAPI_API_KEY", None)
    monkeypatch.setattr(config, "SPORTSGAMEODDS_API_KEY", None)
    monkeypatch.setattr(odds_client.requests, "get", FakeHTTP(odds_api=odds_api(all_status(429))))

    with pytest.raises(odds_client.OddsAPIError, match="API key not set"):
        odds_client._fetch_all_sports_odds()
    assert not odds_client.provider_configured("sharpapi")


def test_total_outage_makes_a_bounded_number_of_requests(monkeypatch):
    down = lambda url, params: Response(503, {"message": "down"})  # noqa: E731
    http = FakeHTTP(odds_api=odds_api(all_status(429)), sharpapi=down, sgo=down)
    monkeypatch.setattr(odds_client.requests, "get", http)

    for _ in range(3):
        assert predictions.generate_daily_predictions() == []

    # /sports + 4 odds attempts + 2 per fallback (one retry), then the
    # cooldown blocks every later request.
    assert len(http.calls) == 1 + 4 + 2 + 2
    assert "No odds provider" in predictions.get_last_generation_stats()["reason_zero"]


def test_sharpapi_pagination_follows_the_cursor(monkeypatch):
    pages = {
        None: {"data": sharp_rows("england_-_premier_league", event_id="1"),
               "pagination": {"next_cursor": "page-2"}},
        "page-2": {"data": sharp_rows("england_-_premier_league", event_id="2",
                                      home="Leeds", away="Spurs"),
                   "pagination": {"next_cursor": None}},
    }
    http = FakeHTTP(
        odds_api=odds_api(all_status(429)),
        sharpapi=lambda url, params: Response(200, pages[params.get("cursor")]),
        sgo=lambda url, params: Response(200, {"data": []}),
    )
    monkeypatch.setattr(odds_client.requests, "get", http)

    data = odds_client._fetch_all_sports_odds()

    assert len(http.requests_to("sharpapi.io")) == 2
    assert [event["id"] for event in data["soccer_epl"]] == ["sharpapi:1", "sharpapi:2"]


def test_sharpapi_rows_drop_live_and_alternate_lines():
    rows = sharp_rows("england_-_premier_league", books=("dk",))
    rows.append({**rows[3], "line": 3.5, "odds_decimal": 81.0,
                 "is_main_line": False, "is_alternate_line": True})
    rows.append({**rows[0], "event_id": "live", "is_live": True})

    events = odds_client._normalize_sharpapi_rows(rows)

    assert [event["id"] for event in events] == ["sharpapi:1"]
    markets = {m["key"]: m["outcomes"] for m in events[0]["bookmakers"][0]["markets"]}
    assert [o["name"] for o in markets["h2h"]] == ["Arsenal", "Draw", "Chelsea"]
    assert markets["totals"] == [{"name": "Over", "price": 1.9, "point": 2.5},
                                 {"name": "Under", "price": 1.95, "point": 2.5}]


def test_sportsgameodds_uses_regulation_three_way_and_main_markets():
    started = sgo_event("EPL", event_id="E2", status={"started": True})
    event = sgo_event("EPL")
    event["odds"]["points-all-reg-ml3way-draw"]["byBookmaker"]["bet365"]["available"] = False

    events = odds_client._normalize_sgo_events([event, started])

    assert [e["id"] for e in events] == ["sportsgameodds:E1"]
    books = {b["key"]: {m["key"]: m["outcomes"] for m in b["markets"]} for b in events[0]["bookmakers"]}
    assert books["fanduel"]["h2h"] == [
        {"name": "Real Madrid", "price": pytest.approx(1.6667, abs=1e-4)},
        {"name": "Draw", "price": 3.8},
        {"name": "Sevilla", "price": 5.0},
    ]
    assert [o["name"] for o in books["bet365"]["h2h"]] == ["Real Madrid", "Sevilla"]
    assert {o["point"] for o in books["fanduel"]["totals"]} == {2.5}


def test_fallback_odds_become_predictions(monkeypatch):
    http = FakeHTTP(
        odds_api=odds_api(all_status(429)),
        sharpapi=lambda url, params: Response(200, {"data": sharp_rows("england_-_premier_league")}),
        sgo=lambda url, params: Response(200, {"data": []}),
    )
    monkeypatch.setattr(odds_client.requests, "get", http)

    picks = predictions.generate_daily_predictions()

    assert picks
    assert {pick["source"] for pick in picks} == {"sharpapi"}
    assert picks[0]["event_id"] == "sharpapi:1"
    assert picks[0]["sport_key"] == "soccer_epl"


def test_generation_stops_after_first_fetch_failure(monkeypatch):
    monkeypatch.setattr(predictions, "SPORT_KEY_MAP", {
        "Football": [
            {"key": f"soccer_{i}", "name": f"League {i}", "short": str(i), "icon": "⚽"}
            for i in range(5)
        ],
    })
    calls = []

    def failing_get_odds(sport_key, markets=None):
        calls.append(sport_key)
        raise odds_client.OddsAPIError("invalid key")

    monkeypatch.setattr(predictions, "get_odds", failing_get_odds)

    assert predictions.generate_daily_predictions() == []
    assert len(calls) == 1
    assert "invalid key" in predictions.get_last_generation_stats()["reason_zero"]


def test_tennis_prefix_leagues_resolve_to_cached_keys(monkeypatch):
    monkeypatch.setattr(
        predictions, "get_cached_sport_keys",
        lambda: ["soccer_epl", "tennis_atp", "tennis_atp_shanghai", "tennis_wta_wuhan"],
    )
    resolved = [league["key"] for sport, league in predictions._resolve_leagues()
                if sport == "Tennis"]
    assert resolved == ["tennis_atp", "tennis_atp_shanghai"]
