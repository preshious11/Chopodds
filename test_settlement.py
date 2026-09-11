"""Edge-case tests for prediction resolution and settlement state changes."""

from unittest.mock import AsyncMock, MagicMock

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
