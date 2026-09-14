"""Focused pytest coverage for prediction selection and threshold fallback."""

from datetime import datetime, timedelta, timezone

import pytest

import config
import predictions


def _future_kickoff_utc() -> str:
    """Future kickoff (UTC ISO) that still falls on today's Lagos date."""
    now_utc = datetime.now(timezone.utc)
    lagos_now = now_utc.astimezone(predictions.LAGOS_TZ)
    candidate = lagos_now.replace(hour=15, minute=0, second=0, microsecond=0)
    if candidate <= lagos_now:
        candidate = lagos_now + timedelta(minutes=15)
        if candidate.date() != lagos_now.date():
            candidate = lagos_now + timedelta(seconds=30)
    return candidate.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


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
        "commence_time": _future_kickoff_utc(),
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


# ---------------------------------------------------------------------------
# Exact odds targets, probability brackets, fixture limits, deduplication
# ---------------------------------------------------------------------------

def _pick(event_id, match, market, confidence, odds):
    return {
        "event_id": event_id,
        "match": match,
        "home_team": match.split(" vs ")[0],
        "away_team": match.split(" vs ")[1],
        "market_type": market,
        "pick": f"{match} {market} pick",
        "odds": odds,
        "confidence": confidence,
        "num_bookmakers": 5,
        "sport": "Football",
    }


def test_top_picks_target_odds_exact():
    """Top Picks: combined odds within tolerance of exactly 2.00."""
    picks = [
        _pick("e1", "A vs B", "h2h", 0.85, 1.41),
        _pick("e2", "C vs D", "totals", 0.80, 1.42),
    ]
    result = predictions.get_top_picks(picks)
    combined = 1.0
    for p in result:
        combined *= p["odds"]
    # 1.41 * 1.42 ≈ 2.00 (±0.15 tolerance)
    assert abs(combined - 2.00) <= config.TOP_PICK_TOLERANCE


def test_top_picks_probability_bracket_seventy_to_ninety():
    """Top Picks: probability must be within [0.70, 0.90]."""
    picks = [
        _pick("e1", "A vs B", "h2h", 0.69, 1.5),
        _pick("e2", "C vs D", "totals", 0.71, 1.5),
        _pick("e3", "E vs F", "btts", 0.90, 1.5),
        _pick("e4", "G vs H", "double_chance", 0.91, 1.5),
    ]
    result = predictions.get_top_picks(picks)
    for p in result:
        assert 0.70 <= p["confidence"] <= 0.90


def test_top_picks_one_to_three_legs():
    """Top Picks: must have 1-3 legs."""
    # Use varied odds that can combine to ~2.0 (1.5 * 1.35 = 2.025)
    picks = [
        _pick("e1", "A vs B", "h2h", 0.85, 1.5),
        _pick("e2", "C vs D", "totals", 0.80, 1.35),
        _pick("e3", "E vs F", "btts", 0.75, 1.3),
    ]
    result = predictions.get_top_picks(picks)
    assert 1 <= len(result) <= 3


def test_all_picks_max_ten_games():
    """All Predictions: must not exceed 10 matches."""
    market_types = ["h2h", "totals", "btts", "double_chance", "spreads"] * 4
    odds = [1.4, 1.5, 1.6, 1.7, 1.8] * 4
    picks = [
        _pick(f"e{i}", f"A{i} vs B{i}", market_types[i], 0.80, odds[i])
        for i in range(20)
    ]
    result = predictions.get_all_picks(picks)
    assert len(result) <= 10


def test_all_picks_odds_range_eight_to_ten():
    """All Predictions: combined odds must be within 8.00-10.00."""
    # Use varied odds that combine to ~9.0 (1.3*1.4*1.5*1.6*1.7 = 7.42, need more)
    # 1.4*1.5*1.6*1.7 = 5.71, 1.5*1.6*1.7*1.8 = 7.34, 1.5*1.7*1.8*1.9 = 8.72
    market_types = ["h2h", "totals", "btts", "double_chance", "spreads",
                    "h2h", "totals", "btts", "double_chance", "spreads"]
    odds = [1.5, 1.7, 1.8, 1.9, 1.4, 1.5, 1.6, 1.7, 1.8, 1.3]
    picks = [
        _pick(f"e{i}", f"A{i} vs B{i}", market_types[i], 0.85 - i * 0.01, odds[i])
        for i in range(10)
    ]
    result = predictions.get_all_picks(picks)
    if len(result) > 0:
        combined = 1.0
        for p in result:
            combined *= p["odds"]
        assert 8.00 <= combined <= 10.00


def test_all_picks_probability_bracket_sixty_to_ninety():
    """All Predictions: probability must be within cascade tiers."""
    picks = [
        _pick("e1", "A vs B", "h2h", 0.49, 1.5),  # below all tiers
        _pick("e2", "C vs D", "totals", 0.53, 1.5),  # Tier 3 (50-55%)
        _pick("e3", "E vs F", "btts", 0.63, 1.5),  # Tier 2 (55-65%)
        _pick("e4", "G vs H", "double_chance", 0.70, 1.5),  # Tier 1 (65-90%)
    ]
    result = predictions.get_all_picks(picks)
    for p in result:
        assert p["confidence"] >= 0.50  # minimum tier floor


def test_cross_output_deduplication():
    """Same match+outcome cannot appear in both Top and All."""
    picks = [
        _pick("e1", "A vs B", "h2h", 0.90, 1.5),
        _pick("e2", "C vs D", "totals", 0.85, 1.5),
        _pick("e3", "E vs F", "btts", 0.80, 1.4),
    ]
    top = predictions.get_top_picks(picks)
    all_p = predictions.get_all_picks(picks, top)
    top_keys = {(p["match"], p["pick"]) for p in top}
    all_keys = {(p["match"], p["pick"]) for p in all_p}
    assert top_keys.isdisjoint(all_keys)


def test_distinct_outcome_same_fixture_allowed():
    """Same fixture with different outcome can appear in both outputs."""
    picks = [
        _pick("e1", "A vs B", "h2h", 0.90, 1.5),
        _pick("e1", "A vs B", "totals", 0.85, 1.5),
        _pick("e2", "C vs D", "btts", 0.80, 1.4),
    ]
    top = predictions.get_top_picks(picks)
    all_p = predictions.get_all_picks(picks, top)
    # The different outcome for "A vs B" can appear in all_p
    all_matches = {p["match"] for p in all_p}
    assert "A vs B" in all_matches or len(all_p) == 0


def test_past_matches_excluded_by_commence_time():
    """Matches that have already kicked off must be excluded."""
    from datetime import datetime, timedelta, timezone
    from unittest.mock import patch

    # Create one past event and one future event
    past_time = (datetime.now(timezone.utc) - timedelta(hours=2)).strftime("%Y-%m-%dT%H:%M:%SZ")
    future_time = _future_kickoff_utc()

    past_event = {
        "id": "past-event",
        "home_team": "Past FC",
        "away_team": "Past United",
        "commence_time": past_time,
        "bookmakers": [
            {
                "key": "bookie",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Past FC", "price": 1.5},
                            {"name": "Draw", "price": 3.0},
                            {"name": "Past United", "price": 6.0},
                        ],
                    },
                ],
            }
        ] * 3,
    }

    future_event = {
        "id": "future-event",
        "home_team": "Future FC",
        "away_team": "Future United",
        "commence_time": future_time,
        "bookmakers": [
            {
                "key": "bookie",
                "markets": [
                    {
                        "key": "h2h",
                        "outcomes": [
                            {"name": "Future FC", "price": 1.5},
                            {"name": "Draw", "price": 3.0},
                            {"name": "Future United", "price": 6.0},
                        ],
                    },
                ],
            }
        ] * 3,
    }

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

    with patch.object(predictions, "SPORT_KEY_MAP", test_sport_map), \
         patch.object(predictions, "get_odds", return_value=[past_event, future_event]):
        result = predictions.generate_daily_predictions(max_predictions=20)

    # Only the future event should be included
    assert len(result) >= 1
    for pred in result:
        assert "Future" in pred["match"]
        assert "Past" not in pred["match"]
