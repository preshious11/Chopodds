"""
Tests for market-diversity prediction selection.

Verifies that:
1. Market category mapping is robust to naming variants (h2h/moneyline/1x2...).
2. The ranking score applies the market-diversity multiplier correctly.
3. Double Chance never dominates the final list (per-market caps).
4. The 50% threshold is respected (no prediction below 50% in the standard path).
5. One prediction per match is still enforced by the selection step.
6. Fewer than 20 valid predictions are all returned (caps are not quotas).
7. A Double-Chance-only day still returns predictions instead of an empty list.

Run: python -m pytest test_market_diversity.py -v
"""

import unittest
from datetime import datetime, timedelta
from unittest import mock

import config
import odds_client
import predictions
from predictions import (
    generate_daily_predictions,
    get_market_category,
    get_top_picks,
    _ranking_score,
    _select_diverse_predictions,
)


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _no_network(*args, **kwargs):
    """Tripwire: any attempt to hit the real API fails the test."""
    raise AssertionError("Network access attempted during tests!")


def make_candidate(event_id, market_type, confidence, num_bookmakers=3):
    """Build a minimal candidate dict shaped like the real generator output."""
    return {
        "event_id": event_id,
        "match": f"Home {event_id} vs Away {event_id}",
        "sport": "Football",
        "home_team": f"Home {event_id}",
        "away_team": f"Away {event_id}",
        "market_type": market_type,
        "market_label": predictions.MARKET_LABELS.get(market_type, market_type),
        "pick": f"{market_type} pick",
        "odds": round(1 / confidence, 2),
        "confidence": confidence,
        "num_bookmakers": num_bookmakers,
    }


def _dc_ou_candidates(num_dc=15, num_ou=10):
    """A Double-Chance-heavy pool: num_dc DC + num_ou Over/Under candidates."""
    candidates = []
    for i in range(num_dc):
        candidates.append(make_candidate(f"dc-{i}", "double_chance", 0.84))
    for i in range(num_ou):
        candidates.append(make_candidate(f"ou-{i}", "totals", 0.78))
    return candidates


def _out(name, price, point=None):
    outcome = {"name": name, "price": price}
    if point is not None:
        outcome["point"] = point
    return outcome


def _today_kickoff_utc(hour=15):
    """Future kickoff (UTC ISO) that still falls on today's Lagos date."""
    now_utc = datetime.now(predictions.timezone.utc)
    lagos_now = now_utc.astimezone(predictions.LAGOS_TZ)
    candidate = lagos_now.replace(hour=hour, minute=0, second=0, microsecond=0)
    if candidate <= lagos_now:
        candidate = lagos_now + timedelta(minutes=15)
        if candidate.date() != lagos_now.date():
            candidate = lagos_now + timedelta(seconds=30)
    return candidate.astimezone(predictions.timezone.utc).strftime(
        "%Y-%m-%dT%H:%M:%SZ"
    )


def _h2h_only_event(event_id, home="Alpha FC", away="Beta FC"):
    """Event whose only market is a lopsided h2h (yields strong Double Chance)."""
    h2h = {"key": "h2h", "outcomes": [
        _out(home, 1.30), _out("Draw", 5.00), _out(away, 9.00),
    ]}
    return {
        "id": event_id,
        "sport_key": "soccer_test",
        "home_team": home,
        "away_team": away,
        "commence_time": _today_kickoff_utc(),
        "bookmakers": [
            {"key": f"bookie{i}", "title": f"Bookie {i}", "markets": [h2h]}
            for i in range(3)
        ],
    }


def _totals_event(event_id, home="Alpha FC", away="Beta FC"):
    """Event with lopsided h2h AND a strong Over 2.5 totals market."""
    h2h = {"key": "h2h", "outcomes": [
        _out(home, 1.30), _out("Draw", 5.00), _out(away, 9.00),
    ]}
    totals = {"key": "totals", "outcomes": [
        _out("Over", 1.05, point=2.5), _out("Under", 11.00, point=2.5),
    ]}
    return {
        "id": event_id,
        "sport_key": "soccer_test",
        "home_team": home,
        "away_team": away,
        "commence_time": _today_kickoff_utc(),
        "bookmakers": [
            {"key": f"bookie{i}", "title": f"Bookie {i}", "markets": [h2h, totals]}
            for i in range(3)
        ],
    }


TEST_SPORT_MAP = {
    "Football": [
        {"key": "soccer_test", "name": "Test League", "short": "TEST", "icon": "⚽"},
    ],
}


# ---------------------------------------------------------------------------
# Market category mapping
# ---------------------------------------------------------------------------

class MarketCategoryMappingTests(unittest.TestCase):
    """Market names must map robustly into diversity categories."""

    def test_match_winner_variants(self):
        for raw in ("h2h", "moneyline", "match_winner", "1x2", "H2H", "Moneyline"):
            self.assertEqual(get_market_category(raw), "1x2", raw)

    def test_over_under_variants(self):
        for raw in ("totals", "alternate_totals", "tennis_games"):
            self.assertEqual(get_market_category(raw), "over_under", raw)

    def test_fixed_categories(self):
        self.assertEqual(get_market_category("btts"), "btts")
        self.assertEqual(get_market_category("double_chance"), "double_chance")
        self.assertEqual(get_market_category("draw_no_bet"), "draw_no_bet")

    def test_handicap_variants(self):
        for raw in ("spreads", "alternate_spreads", "handicap", "asian_handicap"):
            self.assertEqual(get_market_category(raw), "handicap", raw)

    def test_unknown_market_falls_back_to_other(self):
        self.assertEqual(get_market_category("corner_kick_race"), "other")
        self.assertEqual(get_market_category(""), "other")


class RankingScoreTests(unittest.TestCase):
    """ranking_score = probability * market multiplier (display never altered)."""

    def test_double_chance_is_penalised(self):
        candidate = make_candidate("c1", "double_chance", 0.84)
        self.assertAlmostEqual(_ranking_score(candidate), 0.84 * 0.95)

    def test_over_under_unchanged(self):
        candidate = make_candidate("c1", "totals", 0.78)
        self.assertAlmostEqual(_ranking_score(candidate), 0.78)

    def test_btts_boosted(self):
        candidate = make_candidate("c1", "btts", 0.76)
        self.assertAlmostEqual(_ranking_score(candidate), 0.76 * 1.02)

    def test_displayed_probability_untouched(self):
        candidate = make_candidate("c1", "double_chance", 0.84)
        _ranking_score(candidate)
        self.assertEqual(candidate["confidence"], 0.84)


# ---------------------------------------------------------------------------
# Selection behaviour (unit level, direct candidate pools)
# ---------------------------------------------------------------------------

class DiverseSelectionTests(unittest.TestCase):
    """Round-based selection with per-market caps and one pick per match."""

    def test_double_chance_cannot_dominate(self):
        """A: 15 DC + 10 OU must NOT produce 20 DC picks."""
        result = _select_diverse_predictions(_dc_ou_candidates(), 20)
        dc = [p for p in result if p["market_type"] == "double_chance"]
        ou = [p for p in result if p["market_type"] == "totals"]
        self.assertLessEqual(len(dc), config.MAX_DOUBLE_CHANCE)
        self.assertLessEqual(len(ou), config.MAX_OVER_UNDER)
        self.assertLessEqual(len(result), config.MAX_TOTAL_PREDICTIONS)
        self.assertGreater(len(dc), 0)   # DC still represented
        self.assertGreater(len(ou), 0)   # OU still represented

    def test_all_selected_above_min_probability(self):
        """B: no selected prediction below the 50% threshold (no fallback path)."""
        candidates = _dc_ou_candidates(num_dc=5, num_ou=5)
        candidates.append(make_candidate("low-1", "totals", 0.45))
        candidates.append(make_candidate("low-2", "double_chance", 0.41))
        result = _select_diverse_predictions(candidates, 20)
        self.assertGreater(len(result), 0)
        for pred in result:
            self.assertGreaterEqual(pred["confidence"], config.MIN_PROBABILITY)

    def test_one_prediction_per_match(self):
        """C: no match may appear twice, even with many markets per match."""
        candidates = []
        for i in range(6):
            candidates.append(make_candidate(f"m{i}", "double_chance", 0.90))
            candidates.append(make_candidate(f"m{i}", "totals", 0.70))
            candidates.append(make_candidate(f"m{i}", "h2h", 0.60))
        result = _select_diverse_predictions(candidates, 20)
        matches = [p["event_id"] for p in result]
        self.assertEqual(len(matches), len(set(matches)))

    def test_double_chance_never_exceeds_maximum(self):
        """D: 20 DC-only candidates still cap at MAX_DOUBLE_CHANCE."""
        candidates = [
            make_candidate(f"dc-{i}", "double_chance", 0.85) for i in range(20)
        ]
        result = _select_diverse_predictions(candidates, 20)
        dc = [p for p in result if p["market_type"] == "double_chance"]
        self.assertEqual(len(dc), config.MAX_DOUBLE_CHANCE)

    def test_other_markets_are_selected(self):
        """E: valid BTTS/1X2/handicap/DNB candidates beat capped-out DC."""
        candidates = [
            make_candidate(f"dc-{i}", "double_chance", 0.85) for i in range(15)
        ]
        candidates.append(make_candidate("btts-1", "btts", 0.76))
        candidates.append(make_candidate("h2h-1", "h2h", 0.66))
        candidates.append(make_candidate("hcp-1", "spreads", 0.60))
        candidates.append(make_candidate("dnb-1", "draw_no_bet", 0.58))
        result = _select_diverse_predictions(candidates, 20)
        categories = {p["market_type"] for p in result}
        self.assertIn("btts", categories)
        self.assertIn("h2h", categories)
        self.assertIn("spreads", categories)
        self.assertIn("draw_no_bet", categories)

    def test_fewer_than_max_returns_all_valid(self):
        """F: 9 valid candidates under every cap -> all 9 returned, no padding."""
        candidates = (
            [make_candidate(f"dc-{i}", "double_chance", 0.80) for i in range(3)]
            + [make_candidate(f"ou-{i}", "totals", 0.75) for i in range(3)]
            + [make_candidate(f"btts-{i}", "btts", 0.70) for i in range(3)]
        )
        result = _select_diverse_predictions(candidates, 20)
        self.assertEqual(len(result), 9)
        matches = [p["event_id"] for p in result]
        self.assertEqual(len(matches), len(set(matches)))

    def test_double_chance_only_still_returns_predictions(self):
        """G: a DC-only day returns DC picks (up to the cap), never empty."""
        candidates = [
            make_candidate(f"dc-{i}", "double_chance", 0.75 + i * 0.01)
            for i in range(6)
        ]
        result = _select_diverse_predictions(candidates, 20)
        self.assertGreater(len(result), 0)
        self.assertLessEqual(len(result), config.MAX_DOUBLE_CHANCE)
        self.assertTrue(
            all(p["market_type"] == "double_chance" for p in result)
        )

    def test_total_cap_respected(self):
        max_total = 20
        candidates = []
        # Pool spans 5 categories: caps 4+5+4+3+3 = 19 < 20, so 19 is the
        # expected size. If caps summed above MAX_TOTAL_PREDICTIONS the
        # total cap would bind instead.
        categories = ("double_chance", "totals", "btts", "h2h", "spreads")
        expected = min(
            max_total,
            sum(config.MARKET_SELECTION_LIMITS[
                predictions.get_market_category(c)
            ] for c in categories),
        )
        for i in range(40):  # 8 per category, well above every cap
            for market in categories:
                candidates.append(make_candidate(f"c-{i}-{market}", market, 0.55))
        result = _select_diverse_predictions(candidates, max_total)
        self.assertEqual(len(result), expected)
        self.assertLessEqual(len(result), config.MAX_TOTAL_PREDICTIONS)

    def test_total_cap_binds_when_market_caps_sum_higher(self):
        """With all 7 categories present, MAX_TOTAL_PREDICTIONS (20) binds."""
        candidates = []
        for i in range(40):
            for market in (
                "double_chance", "totals", "btts", "h2h", "spreads",
                "draw_no_bet", "corners",
            ):
                candidates.append(make_candidate(f"all-{i}-{market}", market, 0.55))
        result = _select_diverse_predictions(candidates, 20)
        self.assertEqual(len(result), config.MAX_TOTAL_PREDICTIONS)

    def test_stronger_prediction_fills_remaining_slots_first(self):
        """Quality: the strongest remaining candidate fills leftover slots."""
        candidates = (
            [make_candidate(f"dc-{i}", "double_chance", 0.85) for i in range(10)]
            + [make_candidate("elite-ou", "totals", 0.93)]
        )
        result = _select_diverse_predictions(candidates, 3)
        categories = [p["market_type"] for p in result]
        # Round 1 takes one per category (DC + OU), round 2 fills with DC.
        self.assertEqual(categories.count("double_chance"), 2)
        self.assertEqual(categories.count("totals"), 1)


# ---------------------------------------------------------------------------
# Pipeline integration (mocked API payloads, no network)
# ---------------------------------------------------------------------------

class PipelineDiversityTests(unittest.TestCase):
    """generate_daily_predictions end-to-end with mocked odds payloads."""

    def setUp(self):
        self.events = []
        self.get_odds_patcher = mock.patch.object(
            predictions, "get_odds",
            side_effect=lambda sport_key, markets=None: self.events,
        )
        self.map_patcher = mock.patch.dict(
            predictions.SPORT_KEY_MAP, TEST_SPORT_MAP, clear=True
        )
        self.net_patcher = mock.patch.object(
            odds_client.requests, "get", side_effect=_no_network
        )
        self.get_odds_patcher.start()
        self.map_patcher.start()
        self.net_patcher.start()

    def tearDown(self):
        self.get_odds_patcher.stop()
        self.map_patcher.stop()
        self.net_patcher.stop()

    def test_dc_heavy_day_is_capped_and_keeps_one_per_match(self):
        """15 h2h-only matches -> DC capped, other markets still surface."""
        for i in range(15):
            self.events.append(_h2h_only_event(f"evt-{i}", home=f"H{i}", away=f"A{i}"))
        preds = generate_daily_predictions()
        self.assertGreater(len(preds), 0)
        matches = [p["event_id"] for p in preds]
        self.assertEqual(len(matches), len(set(matches)))
        dc = [p for p in preds if p["market_type"] == "double_chance"]
        self.assertLessEqual(len(dc), config.MAX_DOUBLE_CHANCE)
        h2h = [p for p in preds if p["market_type"] == "h2h"]
        self.assertLessEqual(len(h2h), config.MAX_1X2)
        for pred in preds:
            self.assertGreaterEqual(pred["confidence"], config.MIN_PROBABILITY)

    def test_totals_beats_double_chance_on_strongest_match(self):
        """Single multi-market match still resolves to its strongest market."""
        self.events.append(_totals_event("evt-totals"))
        preds = generate_daily_predictions()
        self.assertEqual(len(preds), 1)
        self.assertEqual(preds[0]["market_type"], "totals")
        self.assertEqual(preds[0]["pick"], "Over 2.5 Goals")

    def test_top_picks_come_from_final_diverse_list(self):
        """Top highlights are a subset of the final list, strongest first."""
        for i in range(8):
            self.events.append(_totals_event(f"evt-{i}", home=f"H{i}", away=f"A{i}"))
        preds = generate_daily_predictions()
        top = get_top_picks(preds, count=config.DAILY_PICK_COUNT)
        self.assertLessEqual(len(top), config.DAILY_PICK_COUNT)
        top_matches = [p["event_id"] for p in top]
        self.assertEqual(len(top_matches), len(set(top_matches)))
        for pred in top:
            self.assertIn(pred, preds)
        # Predictions are sorted by confidence, so top picks are the strongest.
        confidences = [p["confidence"] for p in preds]
        self.assertEqual(confidences, sorted(confidences, reverse=True))

    def test_no_value_pick_range_below_50_when_volume_is_high(self):
        """High-volume day: strict 50% threshold — no 40-49% picks leak in."""
        for i in range(5):
            self.events.append(_h2h_only_event(f"evt-{i}", home=f"H{i}", away=f"A{i}"))
        # A balanced totals market yields Over ~47% / Under ~53%. With 5+
        # qualifying matches the strict threshold applies, so the ~47% Over
        # side must never surface (only reachable via the low-volume fallback).
        weak = _totals_event("evt-weak", home="Weak H", away="Weak A")
        for bookmaker in weak["bookmakers"]:
            bookmaker["markets"] = [
                {"key": "totals", "outcomes": [
                    _out("Over", 2.00, point=2.5), _out("Under", 1.80, point=2.5),
                ]},
            ]
        self.events.append(weak)
        preds = generate_daily_predictions()
        self.assertGreaterEqual(len(preds), 5)  # high volume -> strict threshold
        matches = [p["event_id"] for p in preds]
        self.assertEqual(len(matches), len(set(matches)))
        for pred in preds:
            self.assertGreaterEqual(pred["confidence"], config.MIN_PROBABILITY)
        value_pick_range = [
            p for p in preds if 0.40 <= p["confidence"] < 0.50
        ]
        self.assertEqual(value_pick_range, [])

    # ------------------------------------------------------------------
    # Regression: sub-50% selection bug fix
    # ------------------------------------------------------------------

    def test_stronger_eligible_beats_sub50_diversity_candidate(self):
        """72% DC must beat 48% BTTS — market diversity cannot displace >=50%."""
        cands = [
            make_candidate("m1", "double_chance", 0.72),
            make_candidate("m1", "btts", 0.48),
        ]
        preds = _select_diverse_predictions(cands, max_total=5, min_probability=0.40)
        self.assertEqual(len(preds), 1, "one pick per match")
        self.assertAlmostEqual(preds[0]["confidence"], 0.72, places=4)
        self.assertEqual(preds[0]["market_type"], "double_chance")

    def test_multiple_eligible_markets_diversify_above_50(self):
        """Four markets all >=50%: diversify while keeping all >=50%."""
        cands = [
            make_candidate("m1", "double_chance", 0.72),
            make_candidate("m1", "btts", 0.55),
            make_candidate("m2", "totals", 0.53),
            make_candidate("m3", "h2h", 0.51),
        ]
        preds = _select_diverse_predictions(cands, max_total=10, min_probability=0.50)
        for pred in preds:
            self.assertGreaterEqual(pred["confidence"], 0.50)
        self.assertGreaterEqual(len(preds), 3)

    def test_no_above_50_candidates_uses_fallback_floor(self):
        """When no candidate reaches 50%, the 40% fallback floor still applies."""
        cands = [
            make_candidate("m1", "double_chance", 0.48),
            make_candidate("m2", "btts", 0.45),
            make_candidate("m3", "totals", 0.42),
        ]
        preds = _select_diverse_predictions(cands, max_total=5, min_probability=0.40)
        self.assertGreater(len(preds), 0, "fallback must still produce picks")
        for pred in preds:
            self.assertGreaterEqual(pred["confidence"], 0.40)
            self.assertLess(pred["confidence"], 0.50)

    def test_low_volume_day_keeps_50pct_candidates_through_market_floors(self):
        """Low-volume protection: >=50% candidates pass even if below market floors.

        Fewer than MIN_MATCHES_FOR_FULL_QUALITY matches with >=50% candidates
        bypasses per-market floors, but the plain 50% rule still applies —
        sub-50% candidates must NOT become valid.
        """
        cands = [
            make_candidate("m1", "double_chance", 0.72),
            make_candidate("m2", "totals", 0.58),
            make_candidate("m3", "btts", 0.55),
            make_candidate("m4", "h2h", 0.52),
            make_candidate("m5", "btts", 0.48),  # < 50%, must NOT qualify
        ]
        preds = _select_diverse_predictions(cands, max_total=10, min_probability=0.50)
        confidences = [p["confidence"] for p in preds]
        self.assertGreaterEqual(len(preds), 4, "at least the 4 >=50% candidates")
        for c in confidences:
            self.assertGreaterEqual(c, 0.50,
                                    f"sub-50% candidate leaked: {c:.0%}")
        self.assertNotIn(0.48, confidences,
                         "48% BTTS must not be selected when >=50% candidates exist")

    def test_top_picks_never_reintroduce_sub50(self):
        """Top Picks come from final valid/diversified set, never sub-50%."""
        cands = [
            make_candidate("m1", "double_chance", 0.72),
            make_candidate("m2", "btts", 0.65),
            make_candidate("m3", "totals", 0.58),
        ]
        preds = _select_diverse_predictions(cands, max_total=10, min_probability=0.50)
        top = get_top_picks(preds, count=2)
        self.assertGreaterEqual(len(top), 1)
        for pick in top:
            self.assertGreaterEqual(pick["confidence"], 0.50,
                                    f"Top Pick sub-50%: {pick['confidence']:.0%}")
            self.assertIn(pick, preds,
                          "Top Pick must come from the final prediction set")



if __name__ == "__main__":
    unittest.main()


