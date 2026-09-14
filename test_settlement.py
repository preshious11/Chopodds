"""Edge-case tests for prediction resolution and settlement state changes."""

from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import httpx
import pytest

import settlement
import tracking


@pytest.fixture(autouse=True)
def in_memory_tracking(monkeypatch):
    """Give every test a clean, persistent in-memory SQLite database."""
    monkeypatch.setattr(tracking, "DB_PATH", ":memory:")
    monkeypatch.setattr(tracking, "_MEMORY_CONNECTION", None)


def prediction(market_type, pick, home="Home FC", away="Away FC"):
    return {
        "event_id": "event-1",
        "match": f"{home} vs {away}",
        "home_team": home,
        "away_team": away,
        "market_type": market_type,
        "pick": pick,
    }


def scores(home_score, away_score, home="Home FC", away="Away FC"):
    return [
        {"name": home, "score": str(home_score)},
        {"name": away, "score": str(away_score)},
    ]


@pytest.mark.parametrize(
    ("home_score", "away_score", "pick", "expected"),
    [
        (2, 1, "Home FC to Win", "win"),
        (1, 2, "Home FC to Win", "loss"),
        (1, 2, "Away FC to Win", "win"),
        (1, 1, "Draw", "win"),
        (2, 1, "Draw", "loss"),
    ],
)
def test_h2h_match_winner(home_score, away_score, pick, expected):
    result = settlement._settle_pick(
        prediction("h2h", pick), scores(home_score, away_score)
    )
    assert result == expected


@pytest.mark.parametrize(
    ("home_score", "away_score", "pick", "expected"),
    [
        (2, 1, "Over 2.5 Goals", "win"),
        (1, 1, "Over 2.5 Goals", "loss"),
        (1, 1, "Under 2.5 Goals", "win"),
        (2, 2, "Under 2.5 Goals", "loss"),
    ],
)
def test_totals_settle_from_score_sum(home_score, away_score, pick, expected):
    result = settlement._settle_pick(
        prediction("totals", pick), scores(home_score, away_score)
    )
    assert result == expected


def test_btts_win_when_both_teams_score():
    assert settlement._settle_pick(
        prediction("btts", "BTTS Yes"), scores(1, 1)
    ) == "win"


def test_btts_loss_when_one_team_keeps_clean_sheet():
    assert settlement._settle_pick(
        prediction("btts", "BTTS Yes"), scores(2, 0)
    ) == "loss"
    assert settlement._settle_pick(
        prediction("btts", "BTTS No"), scores(2, 0)
    ) == "win"


@pytest.mark.asyncio
async def test_scores_payload_is_fetched_with_mocked_client():
    response = MagicMock()
    response.json.return_value = [{"id": "event-1", "completed": True}]
    response.raise_for_status.return_value = None
    client = MagicMock()
    client.get = AsyncMock(return_value=response)

    result = await settlement._fetch_scores(client, "soccer_epl")

    assert result == [{"id": "event-1", "completed": True}]
    client.get.assert_awaited_once()


def test_postponed_prediction_becomes_void_without_win_loss_changes():
    tracked = prediction("h2h", "Home FC to Win")
    tracked.update(
        {
            "league": "English Premier League",
            "sport_key": "soccer_epl",
            "confidence": 0.7,
            "odds": 1.5,
        }
    )
    tracking.record_predictions([tracked], user_id=42)
    pending = tracking.get_pending_predictions()
    assert pending[0]["status"] == "PENDING"

    assert tracking.settle_prediction(pending[0]["prediction_id"], "VOID")

    assert tracking.get_pending_predictions() == []
    global_stats = tracking.get_global_stats()
    user_stats = tracking.get_user_stats(42)
    assert global_stats["voids"] == 1
    assert global_stats["wins"] == 0
    assert global_stats["losses"] == 0
    assert user_stats["wins"] == 0
    assert user_stats["losses"] == 0


@pytest.mark.parametrize(
    ("home_score", "away_score", "pick", "expected"),
    [
        (2, 0, "Home FC or Draw", "win"),
        (1, 1, "Home FC or Draw", "win"),
        (0, 1, "Home FC or Draw", "loss"),
        (0, 1, "Away FC or Draw", "win"),
        (2, 0, "Away FC or Draw", "loss"),
        (2, 0, "Home FC or Away FC", "win"),
        (1, 1, "Home FC or Away FC", "loss"),
    ],
)
def test_double_chance_settles_from_pick_text(home_score, away_score, pick, expected):
    result = settlement._settle_pick(
        prediction("double_chance", pick), scores(home_score, away_score)
    )
    assert result == expected


def test_spreads_with_multi_word_team_names():
    home, away = "Manchester United", "Manchester City"

    def settle(pick, home_score, away_score):
        return settlement._settle_pick(
            prediction("spreads", pick, home=home, away=away),
            scores(home_score, away_score, home=home, away=away),
        )

    assert settle("Manchester United -1.5", 3, 0) == "win"
    assert settle("Manchester United -1.5", 2, 1) == "loss"
    assert settle("Manchester City +1.5", 2, 1) == "win"


def test_exact_line_is_a_push():
    assert settlement._settle_pick(
        prediction("totals", "Over 2.0 Goals"), scores(1, 1)
    ) == "void"
    assert settlement._settle_pick(
        prediction("spreads", "Home FC -1"), scores(2, 1)
    ) == "void"


def test_draw_no_bet_draw_is_void():
    assert settlement._settle_pick(
        prediction("draw_no_bet", "Home FC to Win (Draw No Bet)"), scores(0, 0)
    ) == "void"


def test_tennis_only_grades_match_winner():
    tennis = prediction("totals", "Over 22.5 Games")
    tennis["sport"] = "Tennis"
    assert settlement._settle_pick(tennis, scores(2, 1)) is None


@pytest.mark.parametrize(
    ("pick", "market"),
    [
        ("Chelsea to Win", "h2h"),
        ("Chelsea to Win (Strong Favorite)", "h2h"),
        ("Draw", "h2h"),
        ("Over 2.5 Goals", "totals"),
        ("BTTS No", "btts"),
        ("Chelsea -1.5", "spreads"),
        ("Chelsea to Win (Draw No Bet)", "draw_no_bet"),
        ("Chelsea or Draw", "double_chance"),
    ],
)
def test_market_inferred_for_legacy_rows(pick, market):
    assert settlement._infer_market(pick) == market


def test_legacy_row_without_settlement_fields_can_be_graded():
    row = {
        "prediction_id": "p1",
        "event_id": "event-1",
        "match_name": "Chelsea vs Arsenal",
        "sport_key": "English Premier League",  # older rows stored the league name
        "selection": "Chelsea or Draw",
        "sport": None,
        "league": None,
        "home_team": None,
        "away_team": None,
        "market_type": None,
    }
    normalized = settlement._with_settlement_fields(row)
    assert normalized["home_team"] == "Chelsea"
    assert normalized["away_team"] == "Arsenal"
    assert normalized["market_type"] == "double_chance"
    assert normalized["sport_key"] == "soccer_epl"
    assert settlement._settle_pick(
        normalized, scores(2, 0, home="Chelsea", away="Arsenal")
    ) == "win"


def tracked_prediction(kickoff_ago, **overrides):
    tracked = prediction("double_chance", "Home FC or Draw")
    tracked.update({
        "sport": "Football",
        "sport_key": "soccer_epl",
        "league": "English Premier League",
        "confidence": 0.8,
        "odds": 1.2,
        "commence_time": (datetime.now(timezone.utc) - kickoff_ago).isoformat(),
        "source": "the-odds-api",
    })
    tracked.update(overrides)
    return tracked


@pytest.mark.asyncio
async def test_settle_pending_grades_recorded_predictions(monkeypatch):
    tracking.record_predictions([tracked_prediction(timedelta(hours=3))], user_id=7)
    fetch = AsyncMock(return_value=[
        {"id": "event-1", "completed": True, "scores": scores(1, 1)},
    ])
    monkeypatch.setattr(settlement, "_fetch_scores", fetch)

    await settlement.settle_pending_predictions()

    assert fetch.await_args.args[1] == "soccer_epl"
    assert tracking.get_pending_predictions() == []
    assert tracking.get_global_stats()["wins"] == 1
    assert tracking.get_user_stats(7)["wins"] == 1


@pytest.mark.asyncio
async def test_unfinished_matches_do_not_spend_credits(monkeypatch):
    tracking.record_predictions([tracked_prediction(timedelta(minutes=30))], user_id=7)
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(settlement, "_fetch_scores", fetch)

    await settlement.settle_pending_predictions()

    fetch.assert_not_awaited()
    assert len(tracking.get_pending_predictions()) == 1


@pytest.mark.asyncio
async def test_failed_score_fetch_keeps_predictions_pending(monkeypatch):
    tracking.record_predictions([tracked_prediction(timedelta(hours=3))], user_id=7)
    monkeypatch.setattr(settlement, "_fetch_scores", AsyncMock(return_value=None))

    await settlement.settle_pending_predictions()

    assert len(tracking.get_pending_predictions()) == 1
    assert tracking.get_health_metrics()["last_score_fetch"] == "never"


@pytest.mark.asyncio
async def test_completed_match_without_scores_is_void_not_loss(monkeypatch):
    tracking.record_predictions([tracked_prediction(timedelta(hours=3))], user_id=7)
    monkeypatch.setattr(settlement, "_fetch_scores", AsyncMock(return_value=[
        {"id": "event-1", "completed": True, "scores": []},
    ]))

    await settlement.settle_pending_predictions()

    stats = tracking.get_global_stats()
    assert stats["voids"] == 1
    assert stats["losses"] == 0


@pytest.mark.asyncio
async def test_stale_pending_pick_is_voided_without_fetching(monkeypatch):
    tracking.record_predictions([tracked_prediction(timedelta(hours=30))], user_id=7)
    fetch = AsyncMock(return_value=[])
    monkeypatch.setattr(settlement, "_fetch_scores", fetch)

    await settlement.settle_pending_predictions()

    fetch.assert_not_awaited()
    assert tracking.get_pending_predictions() == []
    assert tracking.get_global_stats()["voids"] == 1


@pytest.mark.asyncio
async def test_fallback_pick_is_matched_to_scores_by_team_names(monkeypatch):
    kickoff_ago = timedelta(hours=3)
    tracking.record_predictions([tracked_prediction(
        kickoff_ago,
        event_id="sharpapi:77",
        source="sharpapi",
        sport_key="soccer_spain_la_liga",
        home_team="Atlético Madrid",
        away_team="FC Barcelona",
        match="Atlético Madrid vs FC Barcelona",
        pick="Atlético Madrid or Draw",
    )], user_id=7)
    kickoff = datetime.now(timezone.utc) - kickoff_ago
    monkeypatch.setattr(settlement, "_fetch_scores", AsyncMock(return_value=[
        {"id": "unrelated", "completed": True, "home_team": "Sevilla", "away_team": "Getafe",
         "commence_time": kickoff.isoformat(), "scores": scores(0, 0, "Sevilla", "Getafe")},
        {"id": "odds-api-id", "completed": True, "home_team": "Atletico Madrid",
         "away_team": "Barcelona", "commence_time": (kickoff + timedelta(minutes=5)).isoformat(),
         "scores": scores(1, 1, "Atletico Madrid", "Barcelona")},
    ]))

    await settlement.settle_pending_predictions()

    assert tracking.get_global_stats()["wins"] == 1


def test_team_name_matching_is_not_fooled_by_shared_city_names():
    assert settlement._same_team("FC Barcelona", "Barcelona")
    assert settlement._same_team("Atlético Madrid", "Atletico Madrid")
    assert not settlement._same_team("Manchester United", "Manchester City")


@pytest.mark.asyncio
async def test_scores_fetch_failure_returns_none():
    client = MagicMock()
    client.get = AsyncMock(side_effect=httpx.ConnectError("offline"))
    assert await settlement._fetch_scores(client, "soccer_epl") is None
