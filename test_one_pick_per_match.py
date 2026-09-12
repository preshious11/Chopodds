"""
Tests for the one-prediction-per-match guarantee.

Verifies that:
1. generate_daily_predictions never emits the same match twice.
2. The strongest market (highest consensus probability) is chosen per match.
3. get_top_picks and the full list never share a match.
4. Pagination and sport filters never show a match twice.
5. No live API calls are made (uses the daily cache file only).

Run: python -m pytest test_one_pick_per_match.py -v
"""

import copy
import unittest
from datetime import datetime, timezone
from unittest import mock
from zoneinfo import ZoneInfo

import config  # noqa: F401  (loads .env)
import predictions
from predictions import (
    LAGOS_TZ,
    dedupe_by_match,
    filter_by_sport,
    generate_daily_predictions,
    get_top_picks,
    _format_pick_description,
)
from probability import consensus_probabilities
import odds_client
import daily_cache
import formatters


# ---------------------------------------------------------------------------
# Helpers
# ---------------------------------------------------------------------------

def _no_network(*args, **kwargs):
    """Tripwire: any attempt to hit the real API fails the test."""
    raise AssertionError("Network access attempted during tests!")


def _today_kickoff_utc(hour=15):
    """ISO-8601 UTC kickoff that always falls on today's Lagos date."""
    lagos_today = datetime.now(LAGOS_TZ).replace(
        hour=hour, minute=0, second=0, microsecond=0
    )
    return lagos_today.astimezone(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ")


def _out(name, price, point=None):
    outcome = {"name": name, "price": price}
    if point is not None:
        outcome["point"] = point
    return outcome


def make_event(event_id="evt-1", home="Alpha FC", away="Beta FC", commence=None):
    """
    Synthetic soccer event with all three markets from 3 bookmakers.
    Designed so totals 'Over 2.5' (~91% fair prob) beats h2h home win (~71%)
    and the spread (~50/50).
    """
    h2h = {"key": "h2h", "outcomes": [
        _out(home, 1.30), _out("Draw", 5.00), _out(away, 9.00),
    ]}
    spreads = {"key": "spreads", "outcomes": [
        _out(home, 1.90, point=-1.5), _out(away, 1.90, point=1.5),
    ]}
    totals = {"key": "totals", "outcomes": [
        _out("Over", 1.05, point=2.5), _out("Under", 11.00, point=2.5),
    ]}
    bookmakers = [
        {"key": f"bookie{i}", "title": f"Bookie {i}",
         "markets": [h2h, spreads, totals]}
        for i in range(3)
    ]
    event = {
        "sport_key": "soccer_test",
        "home_team": home,
        "away_team": away,
        "commence_time": commence or _today_kickoff_utc(),
        "bookmakers": bookmakers,
    }
    if event_id is not None:
        event["id"] = event_id
    return event


TEST_SPORT_MAP = {
    "Football": [
        {"key": "soccer_test", "name": "Test League", "short": "TEST", "icon": "⚽"},
    ],
}


# ---------------------------------------------------------------------------
# Unit tests with synthetic payloads
# ---------------------------------------------------------------------------

class OnePickPerMatchUnitTests(unittest.TestCase):
    """Synthetic-payload tests — no network, no cache files."""

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

    def test_multi_market_event_yields_single_strongest_pick(self):
        self.events.append(make_event())
        preds = generate_daily_predictions()
        self.assertEqual(len(preds), 1, "one match must produce exactly one prediction")
        pred = preds[0]
        self.assertEqual(pred["event_id"], "evt-1")
        self.assertEqual(pred["market_type"], "totals")
        self.assertEqual(pred["pick"], "Over 2.5 Goals")
        # confidence equals the strongest consensus probability on the event
        totals_probs = consensus_probabilities(self.events[0], market="totals")
        expected = round(min(totals_probs["Over"]["probability"], 0.99), 2)
        self.assertEqual(pred["confidence"], expected)

    def test_duplicate_event_id_processed_once(self):
        event = make_event()
        self.events.extend([event, copy.deepcopy(event)])
        preds = generate_daily_predictions()
        self.assertEqual(len(preds), 1)

    def test_missing_event_id_falls_back_to_match_key(self):
        e1 = make_event(event_id=None)
        e2 = copy.deepcopy(e1)
        self.events.extend([e1, e2])
        preds = generate_daily_predictions()
        self.assertEqual(len(preds), 1, "identical match without id must still dedupe")

    def test_two_distinct_matches_both_kept(self):
        self.events.append(make_event(event_id="evt-1"))
        self.events.append(make_event(event_id="evt-2", home="Gamma FC", away="Delta FC"))
        preds = generate_daily_predictions()
        self.assertEqual(len(preds), 2)
        matches = [p["match"] for p in preds]
        self.assertEqual(len(matches), len(set(matches)))

    def test_dedupe_by_match_keeps_highest_confidence(self):
        base = {"event_id": "e1", "match": "A vs B"}
        weaker = {**base, "confidence": 0.60, "num_bookmakers": 5, "odds": 1.50}
        stronger = {**base, "confidence": 0.85, "num_bookmakers": 4, "odds": 1.30}
        other = {"event_id": "e2", "match": "C vs D", "confidence": 0.70,
                 "num_bookmakers": 3, "odds": 1.80}
        result = dedupe_by_match([weaker, other, stronger])
        self.assertEqual(len(result), 2)
        e1 = next(p for p in result if p["event_id"] == "e1")
        self.assertEqual(e1["confidence"], 0.85)

    def test_dedupe_by_match_falls_back_to_match_string(self):
        a = {"match": "A vs B", "confidence": 0.60}
        b = {"match": "A vs B", "confidence": 0.75}
        result = dedupe_by_match([a, b])
        self.assertEqual(len(result), 1)
        self.assertEqual(result[0]["confidence"], 0.75)

    def test_lay_markets_are_ignored(self):
        """h2h_lay markets (betting exchange) must be skipped."""
        h2h = {"key": "h2h", "outcomes": [
            _out("Alpha FC", 1.30), _out("Draw", 5.00), _out("Beta FC", 9.00),
        ]}
        h2h_lay = {"key": "h2h_lay", "outcomes": [
            _out("Alpha FC", 1.35), _out("Draw", 5.50), _out("Beta FC", 9.50),
        ]}
        bookmakers = [
            {"key": f"bookie{i}", "title": f"Bookie {i}",
             "markets": [h2h, h2h_lay]}
            for i in range(3)
        ]
        event = {
            "id": "evt-lay",
            "sport_key": "soccer_test",
            "home_team": "Alpha FC",
            "away_team": "Beta FC",
            "commence_time": _today_kickoff_utc(),
            "bookmakers": bookmakers,
        }
        self.events.append(event)
        preds = generate_daily_predictions()
        self.assertEqual(len(preds), 1)
        # The selected market should NOT be h2h_lay (it's filtered out)
        self.assertNotEqual(preds[0]["market_type"], "h2h_lay")
        # The prediction should come from h2h or derived double_chance
        self.assertIn(preds[0]["market_type"], ["h2h", "double_chance"])

    def test_strongest_market_selected_from_many(self):
        """When many markets are present, the highest-probability one wins."""
        h2h = {"key": "h2h", "outcomes": [
            _out("Alpha FC", 1.30), _out("Draw", 5.00), _out("Beta FC", 9.00),
        ]}
        spreads = {"key": "spreads", "outcomes": [
            _out("Alpha FC", 1.90, point=-1.5), _out("Beta FC", 1.90, point=1.5),
        ]}
        totals = {"key": "totals", "outcomes": [
            _out("Over", 1.05, point=2.5), _out("Under", 11.00, point=2.5),
        ]}
        bookmakers = [
            {"key": f"bookie{i}", "title": f"Bookie {i}",
             "markets": [h2h, spreads, totals]}
            for i in range(3)
        ]
        event = {
            "id": "evt-strong",
            "sport_key": "soccer_test",
            "home_team": "Alpha FC",
            "away_team": "Beta FC",
            "commence_time": _today_kickoff_utc(),
            "bookmakers": bookmakers,
        }
        self.events.append(event)
        preds = generate_daily_predictions()
        self.assertEqual(len(preds), 1)
        self.assertEqual(preds[0]["market_type"], "totals")
        self.assertEqual(preds[0]["pick"], "Over 2.5 Goals")

    def test_below_50_percent_rejected(self):
        """Individual outcomes below 50% should not appear as h2h picks.

        Note: Derived markets (Double Chance) may still produce qualifying
        predictions since they combine outcomes for higher probability.
        """
        h2h = {"key": "h2h", "outcomes": [
            _out("Alpha FC", 3.0), _out("Draw", 3.0), _out("Beta FC", 3.0),
        ]}
        bookmakers = [
            {"key": f"bookie{i}", "title": f"Bookie {i}",
             "markets": [h2h]}
            for i in range(3)
        ]
        event = {
            "id": "evt-low",
            "sport_key": "soccer_test",
            "home_team": "Alpha FC",
            "away_team": "Beta FC",
            "commence_time": _today_kickoff_utc(),
            "bookmakers": bookmakers,
        }
        self.events.append(event)
        preds = generate_daily_predictions()
        # No h2h outcome should be selected (all are ~33%)
        for pred in preds:
            self.assertNotEqual(pred["market_type"], "h2h",
                                "individual h2h outcomes below 50% should be rejected")

    def test_only_h2h_available(self):
        """When only h2h is available, it should still produce a prediction."""
        h2h = {"key": "h2h", "outcomes": [
            _out("Alpha FC", 1.30), _out("Draw", 5.00), _out("Beta FC", 9.00),
        ]}
        bookmakers = [
            {"key": f"bookie{i}", "title": f"Bookie {i}",
             "markets": [h2h]}
            for i in range(3)
        ]
        event = {
            "id": "evt-h2h",
            "sport_key": "soccer_test",
            "home_team": "Alpha FC",
            "away_team": "Beta FC",
            "commence_time": _today_kickoff_utc(),
            "bookmakers": bookmakers,
        }
        self.events.append(event)
        preds = generate_daily_predictions()
        self.assertEqual(len(preds), 1)
        # With only h2h, the strongest market should be double_chance (1X or X2)
        # since it combines two outcomes for higher probability
        self.assertIn(preds[0]["market_type"], ["h2h", "double_chance"])
        self.assertGreaterEqual(preds[0]["confidence"], 0.50)

    def test_double_chance_higher_than_h2h(self):
        """Double Chance should be selected when it has higher probability than h2h."""
        h2h = {"key": "h2h", "outcomes": [
            _out("Alpha FC", 1.30), _out("Draw", 5.00), _out("Beta FC", 9.00),
        ]}
        bookmakers = [
            {"key": f"bookie{i}", "title": f"Bookie {i}",
             "markets": [h2h]}
            for i in range(3)
        ]
        event = {
            "id": "evt-dc",
            "sport_key": "soccer_test",
            "home_team": "Alpha FC",
            "away_team": "Beta FC",
            "commence_time": _today_kickoff_utc(),
            "bookmakers": bookmakers,
        }
        self.events.append(event)
        preds = generate_daily_predictions()
        self.assertEqual(len(preds), 1)
        # Double chance 1X (home + draw) should have ~92% probability
        self.assertEqual(preds[0]["market_type"], "double_chance")
        self.assertGreaterEqual(preds[0]["confidence"], 0.80)
        self.assertEqual(preds[0]["confidence_tier"], "High Confidence")

    def test_confidence_tiers(self):
        """Predictions should be tagged with correct confidence tiers."""
        # High confidence: >= 70%
        self.assertEqual(predictions.get_confidence_tier(0.75), "High Confidence")
        self.assertEqual(predictions.get_confidence_tier(0.85), "High Confidence")
        # Moderate confidence: 50-69%
        self.assertEqual(predictions.get_confidence_tier(0.55), "Moderate Confidence")
        self.assertEqual(predictions.get_confidence_tier(0.69), "Moderate Confidence")
        # Value pick: 40-49%
        self.assertEqual(predictions.get_confidence_tier(0.45), "Value Pick")
        self.assertEqual(predictions.get_confidence_tier(0.40), "Value Pick")
        # Below threshold: < 40%
        self.assertEqual(predictions.get_confidence_tier(0.35), "Below Threshold")


# ---------------------------------------------------------------------------
# Tests for prediction text formatting
# ---------------------------------------------------------------------------

class FormattingTests(unittest.TestCase):
    """Tests for sport/market-aware prediction text."""

    def test_football_totals_use_goals(self):
        result = _format_pick_description(
            "totals", "Over", 2.5, "A", "B", 0.70, sport_name="Football"
        )
        self.assertEqual(result, "Over 2.5 Goals")

    def test_basketball_totals_use_points(self):
        result = _format_pick_description(
            "totals", "Over", 214.5, "Lakers", "Celtics", 0.70, sport_name="Basketball"
        )
        self.assertEqual(result, "Over 214.5 Points")

    def test_tennis_totals_use_games(self):
        result = _format_pick_description(
            "totals", "Over", 22.5, "Djokovic", "Alcaraz", 0.70, sport_name="Tennis"
        )
        self.assertEqual(result, "Over 22.5 Games")

    def test_ice_hockey_totals_use_goals(self):
        result = _format_pick_description(
            "totals", "Over", 5.5, "Oilers", "Panthers", 0.70, sport_name="Ice Hockey"
        )
        self.assertEqual(result, "Over 5.5 Goals")

    def test_american_football_totals_use_points(self):
        result = _format_pick_description(
            "totals", "Over", 47.5, "Chiefs", "49ers", 0.70, sport_name="American Football"
        )
        self.assertEqual(result, "Over 47.5 Points")

    def test_h2h_home_win(self):
        result = _format_pick_description(
            "h2h", "Chelsea", None, "Chelsea", "Arsenal", 0.70, sport_name="Football"
        )
        self.assertEqual(result, "Chelsea to Win")

    def test_h2h_draw(self):
        result = _format_pick_description(
            "h2h", "Draw", None, "Chelsea", "Arsenal", 0.55, sport_name="Football"
        )
        self.assertEqual(result, "Draw")

    def test_spread_formatting(self):
        result = _format_pick_description(
            "spreads", "Chelsea", -1.5, "Chelsea", "Arsenal", 0.70, sport_name="Football"
        )
        self.assertEqual(result, "Chelsea -1.5")

    def test_unknown_sport_fallback(self):
        """Unknown sport should still include the point even without a unit."""
        result = _format_pick_description(
            "totals", "Over", 2.5, "A", "B", 0.70, sport_name="Unknown Sport"
        )
        self.assertEqual(result, "Over 2.5")


# ---------------------------------------------------------------------------
# Integration test against today's real daily cache
# ---------------------------------------------------------------------------

class RealCacheIntegrationTests(unittest.TestCase):
    """Runs the full generation pipeline against today's real daily cache."""

    @classmethod
    def setUpClass(cls):
        cls.cache = daily_cache.get_cached_odds()
        if not cls.cache:
            raise unittest.SkipTest("no daily cache file for today")
        cls._p1 = mock.patch.object(
            predictions, "get_odds",
            side_effect=lambda sport_key, markets=None: cls.cache.get(sport_key, []),
        )
        cls._p2 = mock.patch.object(
            odds_client.requests, "get", side_effect=_no_network
        )
        cls._p1.start()
        cls._p2.start()
        cls.predictions = generate_daily_predictions()

    @classmethod
    def tearDownClass(cls):
        cls._p1.stop()
        cls._p2.stop()

    def test_predictions_generated(self):
        self.assertTrue(
            len(self.predictions) > 0,
            "daily cache should yield at least one prediction for today"
        )

    def test_no_match_appears_twice(self):
        matches = [p["match"] for p in self.predictions]
        self.assertEqual(
            len(matches), len(set(matches)),
            f"duplicate matches: {[m for m in set(matches) if matches.count(m) > 1]}"
        )
        ids = [p.get("event_id") for p in self.predictions]
        self.assertEqual(len(ids), len(set(ids)))

    def test_top_picks_and_view_all_never_repeat_match(self):
        preds = self.predictions
        top = get_top_picks(preds, count=5)
        top_matches = {p["match"] for p in top}
        rest = preds[len(top):]
        self.assertTrue(top_matches.isdisjoint(p["match"] for p in rest))

    def test_pagination_never_repeats_a_match(self):
        preds = self.predictions
        per_page = 5
        total_pages = max(1, (len(preds) + per_page - 1) // per_page)
        seen = set()
        for page in range(1, total_pages + 1):
            msg, pages = formatters.format_all_picks_paginated(
                preds, page=page, per_page=per_page
            )
            self.assertEqual(pages, total_pages)
            start = (page - 1) * per_page
            page_matches = [p["match"] for p in preds[start:start + per_page]]
            for m in page_matches:
                self.assertNotIn(m, seen, f"match '{m}' appears on more than one page")
            seen.update(page_matches)
        self.assertEqual(len(seen), len(preds))

    def test_sport_filters_have_no_duplicate_matches(self):
        for sport in {p["sport"] for p in self.predictions}:
            filtered = filter_by_sport(self.predictions, sport)
            matches = [p["match"] for p in filtered]
            self.assertEqual(len(matches), len(set(matches)), f"duplicates within {sport}")

    def test_each_pick_is_strongest_within_its_market(self):
        """Each pick is the strongest candidate of its own market for the match.

        Market diversity selection may pick a non-Double-Chance market for a
        match (or drop a match once DC is capped), so the old "strongest
        market overall" oracle no longer holds. The invariant that must hold:
        whichever market was chosen for a match, the pick is the best outcome
        within that market.
        """
        events_by_key = {}
        for sport_key, events in self.cache.items():
            for event in events:
                events_by_key[predictions._match_key(event)] = event
        for pred in self.predictions:
            event = events_by_key.get(pred["event_id"])
            self.assertIsNotNone(event, f"no cached event for {pred['match']}")
            expected = expected_best_rank(event, only_market_type=pred["market_type"])
            actual = (pred["confidence"], pred["num_bookmakers"], pred["odds"])
            self.assertEqual(
                actual, expected,
                f"{pred['match']}: kept {actual}, expected strongest "
                f"{pred['market_type']} candidate {expected}"
            )

    def test_diverse_selection_respects_market_caps(self):
        """Real cache: Double Chance never exceeds its cap, total never exceeds 20."""
        dc = [p for p in self.predictions if p["market_type"] == "double_chance"]
        self.assertLessEqual(len(dc), config.MAX_DOUBLE_CHANCE)
        self.assertLessEqual(len(self.predictions), config.MAX_TOTAL_PREDICTIONS)


def expected_best_rank(event, only_market_type=None):
    """Recompute the strongest candidate rank for an event (test oracle).

    Evaluates both direct markets (h2h, spreads, totals) and derived markets
    (double_chance, btts) to find the single strongest prediction. If
    only_market_type is given, restricts the oracle to that market only.
    """
    best = None
    bookmakers = event.get("bookmakers", [])
    home_team = event.get("home_team", "Unknown")
    away_team = event.get("away_team", "Unknown")
    num_bookmakers = len(bookmakers)

    # Evaluate direct markets
    market_keys = set()
    for bookmaker in bookmakers:
        for mkt in bookmaker.get("markets", []):
            key = mkt.get("key", "")
            if key and not key.endswith("_lay"):
                market_keys.add(key)

    for market_type in market_keys:
        if only_market_type and market_type != only_market_type:
            continue
        for outcome_name, data in consensus_probabilities(event, market=market_type).items():
            if data["num_bookmakers"] < 3:
                continue
            probability = data["probability"]
            if probability < 0.40:
                continue
            probability = min(probability, 0.99)
            best_odds = None
            for bookmaker in bookmakers:
                for mkt in bookmaker.get("markets", []):
                    if mkt.get("key") != market_type:
                        continue
                    for outcome in mkt.get("outcomes", []):
                        if outcome.get("name") == outcome_name:
                            price = outcome.get("price", 0)
                            if best_odds is None or price > best_odds:
                                best_odds = price
            if best_odds is None or best_odds < 1.01:
                continue
            point = predictions._find_outcome_point(bookmakers, market_type, outcome_name)
            pick = predictions._format_pick_description(
                market_type, outcome_name, point,
                home_team, away_team, probability,
            )
            if pick is None:
                continue
            rank = (round(probability, 2), data["num_bookmakers"], round(best_odds, 2))
            if best is None or rank > best:
                best = rank

    # Evaluate derived markets (double_chance, btts)
    # Double Chance
    dc_probs = (
        predictions.calculate_double_chance_probabilities(event)
        if only_market_type in (None, "double_chance") else {}
    )
    for outcome_name, data in dc_probs.items():
        if data["num_bookmakers"] < 3:
            continue
        probability = data["probability"]
        if probability < 0.40:
            continue
        probability = min(probability, 0.99)
        estimated_odds = round(1.0 / probability, 2) if probability > 0 else None
        if estimated_odds is None or estimated_odds < 1.01:
            continue
        rank = (round(probability, 2), data["num_bookmakers"], estimated_odds)
        if best is None or rank > best:
            best = rank

    # BTTS
    btts_probs = (
        predictions.calculate_btts_probabilities(event)
        if only_market_type in (None, "btts") else {}
    )
    for outcome_name, data in btts_probs.items():
        if data["num_bookmakers"] < 3:
            continue
        probability = data["probability"]
        if probability < 0.40:
            continue
        probability = min(probability, 0.99)
        estimated_odds = round(1.0 / probability, 2) if probability > 0 else None
        if estimated_odds is None or estimated_odds < 1.01:
            continue
        rank = (round(probability, 2), data["num_bookmakers"], estimated_odds)
        if best is None or rank > best:
            best = rank

    return best


if __name__ == "__main__":
    unittest.main(verbosity=2)
