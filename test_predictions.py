"""Focused pytest coverage for prediction selection and threshold fallback."""

from datetime import datetime, timezone

import pytest

import predictions


@pytest.fixture
def mocked_event_payload():
    """Return one JSON-like event with competing markets."""
    def outcome(name, price, point=None):
        item = {"name": name, "price": price}
        if point is not None:
            item["point"] = point
        return item

    h2h = {
        "key": "h2h",
        "outcomes": [
            outcome("Alpha FC", 1.30),
            outcome("Draw", 5.00),
            outcome("Beta FC", 9.00),
        ],
    }
    totals = {
        "key": "totals",
        "outcomes": [
            outcome("Over", 1.05, 2.5),
            outcome("Under", 11.00, 2.5),
        ],
    }
    bookmakers = [
        {
            "key": f"bookie-{index}",
            "markets": [h2h, totals],
        }
        for index in range(3)
    ]
    return {
        "id": "event-1",
        "home_team": "Alpha FC",
        "away_team": "Beta FC",
        "commence_time": datetime.now(predictions.LAGOS_TZ).replace(
            hour=15, minute=0, second=0, microsecond=0
        ).astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "bookmakers": bookmakers,
    }


@pytest.fixture
def candidate_factory():
    """Build minimal candidates for threshold tests."""
    def build(confidence, event_id):
        return {
            "event_id": event_id,
            "match": f"Home {event_id} vs Away {event_id}",
            "confidence": confidence,
            "num_bookmakers": 3,
            "odds": round(1 / confidence, 2),
        }

    return build


def test_one_match_keeps_strongest_market(monkeypatch, mocked_event_payload):
    """The generator emits one candidate and keeps the strongest market."""
    test_sport_map = {
        "Football": [
            {
                "key": "soccer_test",
                "name": "Test League",
                "short": "TEST",
                "icon": "⚽",
            }
        ]
    }
    monkeypatch.setattr(predictions, "SPORT_KEY_MAP", test_sport_map)
    monkeypatch.setattr(
        predictions,
        "get_odds",
        lambda sport_key, markets=None: [mocked_event_payload],
    )

    result = predictions.generate_daily_predictions(max_predictions=20)

    assert len(result) == 1
    assert result[0]["event_id"] == "event-1"
    assert result[0]["market_type"] == "totals"
    assert result[0]["pick"] == "Over 2.5 Goals"


def test_threshold_uses_50_percent_when_enough_candidates(candidate_factory):
    """At least five qualifying candidates keep the configured 50% cutoff."""
    candidates = [candidate_factory(value, str(index)) for index, value in enumerate(
        (0.50, 0.55, 0.60, 0.65, 0.70)
    )]

    result = predictions._apply_threshold_with_fallback(
        candidates,
        min_threshold=0.50,
        fallback_floor=0.40,
        min_matches=5,
    )

    assert [candidate["confidence"] for candidate in result] == [
        0.50,
        0.55,
        0.60,
        0.65,
        0.70,
    ]


def test_threshold_falls_back_to_40_percent_on_low_volume(candidate_factory):
    """Fewer than five 50% candidates include eligible 40% fallback picks."""
    candidates = [candidate_factory(value, str(index)) for index, value in enumerate(
        (0.40, 0.45, 0.49, 0.55)
    )]

    result = predictions._apply_threshold_with_fallback(
        candidates,
        min_threshold=0.50,
        fallback_floor=0.40,
        min_matches=5,
    )

    assert [candidate["confidence"] for candidate in result] == [
        0.55,
        0.49,
        0.45,
        0.40,
    ]
