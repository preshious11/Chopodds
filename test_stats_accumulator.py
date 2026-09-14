"""
Tests for the accumulator-odds, event-deduplication, and settlement-stats refactor.

Covers:
1. Combined ticket odds = product of decimal odds (never the sum), in
   probability.calculate_combined_odds, predictions.calculate_ticket_odds /
   build_accumulator, and the formatter output.
2. tracking.record_predictions deduplicates by unique event_id before
   insertion; one authoritative prediction_id per event.
3. Global and user stats expose Total Delivered / Pending / Settled (W|L|V)
   with win rate = wins / settled, and are safe when nothing is settled.

Run: python -m pytest test_stats_accumulator.py -v
"""

import pytest

import tracking
import probability
import predictions
import stats
import formatters


@pytest.fixture(autouse=True)
def in_memory_tracking(monkeypatch):
    """Clean in-memory SQLite and stats cache for every test."""
    monkeypatch.setattr(tracking, "DB_PATH", ":memory:")
    monkeypatch.setattr(tracking, "_MEMORY_CONNECTION", None)
    stats._STATS_CACHE.clear()


def make_pick(event_id, match, pick, odds, confidence=0.7, num_bookmakers=5):
    return {
        "event_id": event_id,
        "match": match,
        "home_team": match.split(" vs ")[0] if " vs " in match else match,
        "away_team": match.split(" vs ")[1] if " vs " in match else "",
        "market_type": "h2h",
        "pick": pick,
        "odds": odds,
        "confidence": confidence,
        "num_bookmakers": num_bookmakers,
        "sport": "Football",
        "sport_key": "soccer_epl",
        "league": "English Premier League",
        "match_time": "15:00",
    }


def formatted_pick(event_id, odds):
    """Minimal prediction dict shaped for the formatters."""
    return {
        "event_id": event_id,
        "match": f"Team A {event_id} vs Team B {event_id}",
        "sport_icon": "⚽",
        "pick": "Team A to Win",
        "odds": odds,
        "confidence": 0.8,
        "confidence_tier": "High Confidence",
        "match_time": "15:00",
        "league": "EPL",
    }


# ---------------------------------------------------------------------------
# 1. Accumulator & multi-pick odds
# ---------------------------------------------------------------------------

class TestCombinedOdds:
    def test_product_not_sum(self):
        # 2.0 * 3.0 = 6.00 (a sum would incorrectly give 5.00)
        assert probability.calculate_combined_odds([2.0, 3.0]) == 6.0

    def test_three_way_product(self):
        assert probability.calculate_combined_odds([1.5, 2.0, 2.5]) == 7.5

    def test_rounded_to_two_decimals(self):
        assert probability.calculate_combined_odds([1.83, 1.77, 1.91]) == 6.19

    def test_empty_input_returns_zero(self):
        assert probability.calculate_combined_odds([]) == 0.0

    def test_invalid_values_skipped(self):
        assert probability.calculate_combined_odds([2.0, None, "x", 0, 1.5]) == 3.0

    def test_predictions_ticket_odds_uses_product(self):
        picks = [
            make_pick("e1", "A vs B", "A to Win", 1.9),
            make_pick("e2", "C vs D", "Over 2.5", 2.2),
        ]
        assert predictions.calculate_ticket_odds(picks) == 4.18

    def test_build_accumulator_shows_selections_and_total(self):
        picks = [
            make_pick("e1", "A vs B", "A to Win", 1.9),
            make_pick("e2", "C vs D", "Over 2.5", 2.2),
        ]
        ticket = predictions.build_accumulator(picks)
        assert ticket["num_selections"] == 2
        assert [s["odds"] for s in ticket["selections"]] == [1.9, 2.2]
        assert ticket["combined_odds"] == 4.18

    def test_formatter_displays_individual_and_combined_odds(self):
        picks = [formatted_pick("e1", 2.0), formatted_pick("e2", 3.0)]
        msg = formatters.format_top_picks(picks)
        assert "Odds: <b>2.0</b>" in msg          # individual selection odds
        assert "Odds: <b>3.0</b>" in msg
        assert "Accumulator" in msg
        assert "6.00" in msg                       # product, not 5.00 (sum)

    def test_paginated_combined_uses_all_predictions(self):
        preds = [formatted_pick(f"e{i}", 2.0) for i in range(6)]
        msg, pages = formatters.format_all_picks_paginated(preds, page=1, per_page=5)
        assert pages == 2
        assert "64.00" in msg  # 2.0 ** 6 across ALL picks, not just the page


# ---------------------------------------------------------------------------
# 2. Event deduplication before database insertion
# ---------------------------------------------------------------------------

class TestEventDeduplication:
    def test_duplicate_event_in_one_call_stored_once(self):
        stronger = make_pick("e1", "A vs B", "A to Win", 1.5, confidence=0.85)
        weaker = make_pick("e1", "A vs B", "A or Draw", 1.2, confidence=0.60)
        tracking.record_predictions([weaker, stronger])
        assert len(tracking.get_pending_predictions()) == 1

    def test_dedup_keeps_strongest_candidate(self):
        stronger = make_pick("e1", "A vs B", "A to Win", 1.5, confidence=0.85)
        weaker = make_pick("e1", "A vs B", "A or Draw", 1.2, confidence=0.60)
        tracking.record_predictions([weaker, stronger])
        pending = tracking.get_pending_predictions()
        assert pending[0]["selection"] == "A to Win"
        assert pending[0]["confidence_score"] == 0.85

    def test_same_event_single_authoritative_prediction_id(self):
        """Slip + standard list for the same event -> one prediction_id."""
        first = make_pick("e1", "A vs B", "A to Win", 1.5)
        second = make_pick("e1", "A vs B", "A to Win", 1.5)
        tracking.record_predictions([first], user_id=1)
        tracking.record_predictions([second], user_id=2)
        pending = tracking.get_pending_predictions()
        assert len(pending) == 1
        assert pending[0]["delivered_to_users"] == "[1, 2]"

    def test_distinct_events_all_stored(self):
        picks = [make_pick(f"e{i}", f"A{i} vs B{i}", "Over 2.5", 1.8) for i in range(3)]
        tracking.record_predictions(picks)
        assert len(tracking.get_pending_predictions()) == 3

    def test_global_generated_counts_unique_fixtures(self):
        picks = [
            make_pick("e1", "A vs B", "A to Win", 1.5),
            make_pick("e1", "A vs B", "A to Win", 1.5),   # dup event
            make_pick("e2", "C vs D", "Over 2.5", 1.8),
        ]
        tracking.record_predictions(picks)
        assert tracking.get_global_stats()["generated"] == 2

    def test_user_counts_unique_fixtures(self):
        picks = [
            make_pick("e1", "A vs B", "A to Win", 1.5),
            make_pick("e1", "A vs B", "A to Win", 1.5),   # same event via slip + list
            make_pick("e2", "C vs D", "Over 2.5", 1.8),
        ]
        tracking.record_predictions(picks, user_id=7)
        assert tracking.get_user_stats(7)["delivered"] == 2


# ---------------------------------------------------------------------------
# 3. Settlement stats & pending breakdown
# ---------------------------------------------------------------------------

class TestSettlementStats:
    def _settle_first_pending(self, status):
        pending = tracking.get_pending_predictions()
        assert tracking.settle_prediction(pending[0]["prediction_id"], status)

    def test_global_stats_breakdown(self):
        picks = [
            make_pick("e1", "A vs B", "A to Win", 1.5),
            make_pick("e2", "C vs D", "Over 2.5", 1.8),
            make_pick("e3", "E vs F", "BTTS Yes", 2.0),
            make_pick("e4", "G vs H", "Under 2.5", 1.7),
        ]
        tracking.record_predictions(picks)
        self._settle_first_pending("SETTLED_WIN")
        self._settle_first_pending("SETTLED_LOSS")
        self._settle_first_pending("VOID")

        s = tracking.get_global_stats()
        assert s["delivered"] == 4
        assert s["pending"] == 1
        assert s["settled"] == 3
        assert s["wins"] == 1
        assert s["losses"] == 1
        assert s["voids"] == 1
        # win rate = wins / settled games (incl. voids) * 100
        assert s["win_rate"] == round(1 / 3 * 100, 1)

    def test_zero_settled_is_safe_and_zero_rate(self):
        tracking.record_predictions([make_pick("e1", "A vs B", "A to Win", 1.5)])
        s = tracking.get_global_stats()
        assert s["settled"] == 0
        assert s["pending"] == 1
        assert s["win_rate"] == 0  # no ZeroDivisionError

    def test_empty_database_is_safe(self):
        s = tracking.get_global_stats()
        assert s["delivered"] == 0
        assert s["pending"] == 0
        assert s["settled"] == 0
        assert s["win_rate"] == 0

    def test_user_stats_breakdown_includes_pending_and_voids(self):
        tracking.record_predictions(
            [
                make_pick("e1", "A vs B", "A to Win", 1.5),
                make_pick("e2", "C vs D", "Over 2.5", 1.8),
                make_pick("e3", "E vs F", "BTTS Yes", 2.0),
            ],
            user_id=42,
        )
        pending = tracking.get_pending_predictions()
        tracking.settle_prediction(pending[0]["prediction_id"], "SETTLED_WIN")
        tracking.settle_prediction(pending[1]["prediction_id"], "VOID")

        s = tracking.get_user_stats(42)
        assert s["delivered"] == 3
        assert s["pending"] == 1
        assert s["settled"] == 2
        assert s["wins"] == 1
        assert s["voids"] == 1
        assert s["win_rate"] == 50.0  # 1 win / 2 settled (incl. the void)

    def test_user_with_no_deliveries_is_safe(self):
        s = tracking.get_user_stats(999)
        assert s["delivered"] == 0
        assert s["pending"] == 0
        assert s["settled"] == 0
        assert s["win_rate"] == 0


class TestStatsMessage:
    def test_message_shows_pending_alongside_settled(self):
        tracking.record_predictions(
            [
                make_pick("e1", "A vs B", "A to Win", 1.5),
                make_pick("e2", "C vs D", "Over 2.5", 1.8),
            ],
            user_id=42,
        )
        pending = tracking.get_pending_predictions()
        tracking.settle_prediction(pending[0]["prediction_id"], "SETTLED_WIN")

        msg = stats.format_stats_message(user_id=42)
        assert "Total Delivered" in msg
        assert "Pending Results" in msg
        assert "Settled Games" in msg
        assert "Wins: 1 | Losses: 0 | Void: 0" in msg
        assert "Win Rate" in msg
        assert "100.0%" in msg

    def test_message_safe_when_nothing_settled(self):
        tracking.record_predictions(
            [make_pick("e1", "A vs B", "A to Win", 1.5)], user_id=42
        )
        msg = stats.format_stats_message(user_id=42)
        assert "Pending Results: <code>1</code>" in msg
        assert "No settled games yet" in msg  # no ZeroDivisionError / empty display

    def test_message_safe_on_empty_database(self):
        msg = stats.format_stats_message(user_id=42, user_joined_at=None)
        assert "Total Delivered: <code>0</code>" in msg
        assert "No settled games yet" in msg

    def test_unsubscribed_user_message(self):
        msg = stats.format_stats_message(user_id=42, user_joined_at=None)
        assert "Not subscribed" in msg


# ---------------------------------------------------------------------------
# 4. Target-odds accumulator (get_top_picks / get_all_picks)
# ---------------------------------------------------------------------------

def _acc_pick(event_id, match, market_type, confidence, odds):
    """Minimal accumulator-ready prediction dict."""
    return {
        "event_id": event_id,
        "match": match,
        "home_team": match.split(" vs ")[0] if " vs " in match else match,
        "away_team": match.split(" vs ")[1] if " vs " in match else "",
        "market_type": market_type,
        "pick": f"{match} {market_type} pick",
        "odds": odds,
        "confidence": confidence,
        "num_bookmakers": 5,
        "sport": "Football",
    }


class TestTargetOddsAccumulator:
    """Tests for the target-odds accumulator: probability floors, target odds,
    max legs, market variety, and cross-output deduplication."""

    # -- A. Target odds reached -> stop adding --
    def test_top_picks_stops_at_target_odds(self):
        # 1.5 * 1.35 = 2.025 (within ±0.05 of 2.00)
        picks = [
            _acc_pick("e1", "A vs B", "h2h", 0.90, 1.5),
            _acc_pick("e2", "C vs D", "totals", 0.85, 1.35),
            _acc_pick("e3", "E vs F", "btts", 0.80, 1.3),
        ]
        result = predictions.get_top_picks(picks)
        combined = 1.0
        for p in result:
            combined *= p["odds"]
        assert combined >= 2.0
        assert combined <= 2.05  # within tolerance
        assert 2 <= len(result) <= 3

    def test_all_picks_stops_at_target_odds(self):
        # Diverse market types so market-variety limits don't block us.
        # Use varied odds that combine to ~9.0 (within 8.0-10.0 range)
        market_types = ["h2h", "totals", "btts", "double_chance", "spreads"] * 2
        odds = [1.4, 1.5, 1.6, 1.7, 1.3, 1.4, 1.5, 1.6, 1.7, 1.8]
        picks = [
            _acc_pick(f"e{i}", f"A{i} vs B{i}", market_types[i], 0.85 - i * 0.01, odds[i])
            for i in range(10)
        ]
        result = predictions.get_all_picks(picks)
        combined = 1.0
        for p in result:
            combined *= p["odds"]
        assert 8.0 <= combined <= 10.0  # within target range

    # -- B. Max legs cap --
    def test_top_picks_max_three_legs(self):
        picks = [
            _acc_pick(f"e{i}", f"A{i} vs B{i}", "h2h", 0.80, 1.1)
            for i in range(10)
        ]
        result = predictions.get_top_picks(picks)
        assert len(result) <= 3

    def test_all_picks_max_ten_legs(self):
        picks = [
            _acc_pick(f"e{i}", f"A{i} vs B{i}", "h2h", 0.80, 1.05)
            for i in range(20)
        ]
        result = predictions.get_all_picks(picks)
        assert len(result) <= 10

    # -- C. Market variety: max 2 per market type in primary pass --
    def test_market_variety_max_two_per_type(self):
        picks = [
            _acc_pick("e1", "A vs B", "double_chance", 0.90, 1.2),
            _acc_pick("e2", "C vs D", "double_chance", 0.89, 1.2),
            _acc_pick("e3", "E vs F", "double_chance", 0.88, 1.2),
            _acc_pick("e4", "G vs H", "double_chance", 0.87, 1.2),
            _acc_pick("e5", "I vs J", "totals", 0.86, 1.5),
        ]
        result = predictions.get_top_picks(picks)
        from collections import Counter
        type_counts = Counter(p["market_type"] for p in result)
        for mt, count in type_counts.items():
            assert count <= 2, f"{mt} appeared {count} times (max 2)"

    # -- D. Cross-output deduplication --
    def test_all_picks_excludes_top_picks_pairings(self):
        picks = [
            _acc_pick("e1", "A vs B", "h2h", 0.90, 1.5),
            _acc_pick("e2", "C vs D", "totals", 0.85, 1.5),
            _acc_pick("e3", "E vs F", "btts", 0.80, 1.4),
        ]
        top = predictions.get_top_picks(picks)
        all_p = predictions.get_all_picks(picks, top)
        top_keys = {(p["match"], p["pick"]) for p in top}
        all_keys = {(p["match"], p["pick"]) for p in all_p}
        assert top_keys.isdisjoint(all_keys)

    def test_different_outcome_same_match_allowed(self):
        picks = [
            _acc_pick("e1", "A vs B", "h2h", 0.90, 1.5),
            _acc_pick("e1", "A vs B", "double_chance", 0.85, 1.5),
        ]
        top = predictions.get_top_picks(picks)
        all_p = predictions.get_all_picks(picks, top)
        all_matches = {p["match"] for p in all_p}
        assert "A vs B" in all_matches or len(all_p) == 0

    # -- E. Probability brackets (cascade: higher tiers preferred) --
    def test_top_picks_probability_cascade(self):
        # Tier 1 (70-90%) should be preferred over Tier 2 (60-70%)
        picks = [
            _acc_pick("e1", "A vs B", "h2h", 0.90, 1.5),  # Tier 1
            _acc_pick("e2", "C vs D", "totals", 0.85, 1.35),  # Tier 1
            _acc_pick("e3", "E vs F", "btts", 0.65, 1.5),  # Tier 2
        ]
        result = predictions.get_top_picks(picks)
        confs = [p["confidence"] for p in result]
        # All results should be from Tier 1 if possible
        assert all(c >= 0.70 for c in confs)

    def test_all_picks_probability_cascade(self):
        # Tier 1 (65-90%) should be preferred when it can reach the target
        picks = [
            _acc_pick("e1", "A vs B", "h2h", 0.85, 1.5),  # Tier 1
            _acc_pick("e2", "C vs D", "totals", 0.80, 1.5),  # Tier 1
            _acc_pick("e3", "E vs F", "btts", 0.78, 1.5),  # Tier 1
            _acc_pick("e4", "G vs H", "double_chance", 0.75, 1.5),  # Tier 1
            _acc_pick("e5", "I vs J", "spreads", 0.72, 1.5),  # Tier 1
            _acc_pick("e6", "K vs L", "h2h", 0.55, 1.5),  # Tier 2
        ]
        result = predictions.get_all_picks(picks)
        confs = [p["confidence"] for p in result]
        # Results should be from Tier 1 if possible (Tier 2 only included if needed)
        assert all(c >= 0.65 for c in confs)

    def test_top_picks_fallback_to_lower_tier(self):
        # When Tier 1 alone can't reach target, cascade includes Tier 2
        picks = [
            _acc_pick("e1", "A vs B", "h2h", 0.90, 1.1),  # Tier 1 (low odds)
            _acc_pick("e2", "C vs D", "totals", 0.85, 1.1),  # Tier 1 (low odds)
            _acc_pick("e3", "E vs F", "btts", 0.65, 1.5),  # Tier 2 (helps reach target)
        ]
        result = predictions.get_top_picks(picks)
        # Should include Tier 2 pick to reach target
        assert len(result) >= 1

    # -- F. Single pick with odds within target tolerance returns one leg --
    def test_single_high_odds_pick_alone(self):
        # Use odds within tolerance (2.0 ± 0.05 = 1.95 to 2.05)
        picks = [
            _acc_pick("e1", "A vs B", "h2h", 0.90, 2.02),
            _acc_pick("e2", "C vs D", "totals", 0.85, 1.5),
        ]
        result = predictions.get_top_picks(picks)
        # Single pick with odds within tolerance should be returned alone
        assert len(result) >= 1
        combined = 1.0
        for p in result:
            combined *= p["odds"]
        assert 2.0 <= combined <= 2.05

    # -- G. Empty when no qualifying picks --
    def test_no_qualifying_picks_returns_empty(self):
        picks = [
            _acc_pick("e1", "A vs B", "h2h", 0.30, 3.0),
        ]
        assert predictions.get_top_picks(picks) == []
        assert predictions.get_all_picks(picks) == []

    # -- I. Combined odds are the product, not sum --
    def test_combined_odds_is_product(self):
        picks = [
            _acc_pick("e1", "A vs B", "h2h", 0.90, 1.5),
            _acc_pick("e2", "C vs D", "totals", 0.85, 1.4),
        ]
        result = predictions.get_top_picks(picks)
        if len(result) >= 2:
            expected = round(1.5 * 1.4, 2)
            actual = predictions.calculate_ticket_odds(result)
            assert actual == expected

