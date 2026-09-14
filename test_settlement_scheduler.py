"""Tests for the Credit-Optimized, Time-Aware settlement engine.

Covers:
1. prediction_tracking stores UTC commence_time (and reconstructs it from Lagos
   match_date + match_time when only local time is available);
2. get_eligible_pending_predictions returns ONLY pending matches inside the
   [commence+110m, commence+14h] window;
3. settle_pending_predictions skips (logs) and fetches NOTHING when no match is
   eligible (0 API credits used);
4. settle_pending_predictions calls the /scores endpoint ONLY for the unique
   sports among the eligible matches.

Run: python -m pytest test_settlement_scheduler.py -v
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import settlement
import tracking


@pytest.fixture(autouse=True)
def in_memory_tracking(monkeypatch):
    monkeypatch.setattr(tracking, "DB_PATH", ":memory:")
    monkeypatch.setattr(tracking, "_MEMORY_CONNECTION", None)


def make_pick(event_id, commence_time, sport_key="soccer_epl"):
    return {
        "event_id": event_id,
        "match": f"{event_id} Home vs {event_id} Away",
        "sport_key": sport_key,
        "commence_time": commence_time,
        "pick": f"{event_id} Home to Win",
        "confidence": 0.8,
        "odds": 1.5,
    }


def _utc_now():
    return datetime.now(timezone.utc)


# ---------------------------------------------------------------------------
# 1. commence_time storage
# ---------------------------------------------------------------------------
class TestCommenceTimeStorage:
    def test_explicit_commence_time_is_stored(self):
        now = _utc_now()
        tracking.record_predictions(
            [make_pick("e1", now.isoformat(), "soccer_epl")]
        )
        row = tracking.get_pending_predictions()[0]
        assert row["commence_time"] is not None
        # round-trips to the same instant
        stored = tracking._parse_utc(row["commence_time"])
        assert abs((stored - now).total_seconds()) < 2

    def test_commence_time_reconstructed_from_lagos(self):
        """match_date+match_time (Lagos) without an explicit UTC ts is stored."""
        lagos_now = _utc_now().astimezone(tracking.LAGOS_TZ)
        kickoff_lagos = lagos_now - timedelta(hours=2)
        tracking.record_predictions(
            [
                {
                    "event_id": "e9",
                    "match": "X vs Y",
                    "sport_key": "soccer_epl",
                    "match_date": kickoff_lagos.strftime("%Y-%m-%d"),
                    "match_time": kickoff_lagos.strftime("%H:%M"),
                    "pick": "X to Win",
                    "confidence": 0.8,
                    "odds": 1.5,
                }
            ]
        )
        stored = tracking.get_pending_predictions()[0]["commence_time"]
        assert tracking._parse_utc(stored) is not None
        # ~2h before the reference Lagos time, expressed in UTC
        expected = kickoff_lagos.astimezone(timezone.utc)
        assert abs((tracking._parse_utc(stored) - expected).total_seconds()) < 60


# ---------------------------------------------------------------------------
# 2. eligibility window
# ---------------------------------------------------------------------------
class TestEligibilityWindow:
    def test_only_in_window_picks_eligible(self):
        now = _utc_now()
        eligible = now - timedelta(minutes=120)   # >=110m ago
        too_soon = now - timedelta(minutes=30)    # not yet kicked off late enough
        stale = now - timedelta(hours=20)         # beyond the 14h window
        tracking.record_predictions(
            [
                make_pick("e1", eligible.isoformat()),
                make_pick("e2", too_soon.isoformat()),
                make_pick("e3", stale.isoformat()),
            ]
        )
        found = {p["event_id"] for p in tracking.get_eligible_pending_predictions(now=now)}
        assert found == {"e1"}

    def test_pending_only(self):
        now = _utc_now()
        tracking.record_predictions([make_pick("e1", (now - timedelta(minutes=120)).isoformat())])
        tracking.settle_prediction(tracking.get_pending_predictions()[0]["prediction_id"], "SETTLED_WIN")
        assert tracking.get_eligible_pending_predictions(now=now) == []

    def test_skips_rows_without_commence_time(self):
        now = _utc_now()
        tracking.record_predictions(
            [{"event_id": "e8", "match": "X vs Y", "sport_key": "soccer_epl",
              "pick": "X", "confidence": 0.8, "odds": 1.5}]
        )
        assert tracking.get_eligible_pending_predictions(now=now) == []


def _patch_eligible(rows):
    """Context manager swapping tracking.get_eligible_pending_predictions."""
    import contextlib

    tracker = MagicMock(return_value=rows)

    @contextlib.contextmanager
    def cm():
        original = tracking.get_eligible_pending_predictions
        tracking.get_eligible_pending_predictions = tracker
        try:
            yield
        finally:
            tracking.get_eligible_pending_predictions = original

    return cm()


def _patch_http_client(get_side_effect):
    """Return (mock_client, token) after replacing settlement.httpx.AsyncClient.

    get_side_effect: a callable(url, params) -> response, or None to assert the
    client is never used (no __aenter__ when nothing is fetched).
    """
    mock_client = MagicMock()

    if get_side_effect is None:
        # No eligible matches: the client must never be entered/used.
        enter_ctx = MagicMock()
        enter_ctx.__aenter__ = AsyncMock(side_effect=AssertionError(
            "httpx.AsyncClient should not be entered when 0 matches are eligible"))
        enter_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client.return_value = enter_ctx
    else:
        mock_client.get = AsyncMock(side_effect=get_side_effect)
        enter_ctx = MagicMock()
        enter_ctx.__aenter__ = AsyncMock(return_value=mock_client)
        enter_ctx.__aexit__ = AsyncMock(return_value=False)
        mock_client.return_value = enter_ctx

    original = settlement.httpx.AsyncClient
    settlement.httpx.AsyncClient = mock_client
    return mock_client, original


# ---------------------------------------------------------------------------
# 3 & 4. credit-optimized settlement
# ---------------------------------------------------------------------------
class TestCreditOptimizedSettlement:
    def test_skip_when_nothing_eligible_logs(self, monkeypatch):
        messages = []
        monkeypatch.setattr(
            settlement.logger,
            "info",
            lambda msg, *args, **kwargs: messages.append(str(msg)),
        )
        with _patch_eligible([]):
            asyncio.run(settlement.settle_pending_predictions())
        assert any(
            "0 pending matches eligible" in m for m in messages
        )

    def test_active_sport_keys_unique(self):
        now = _utc_now()
        eligible = [
            {**make_pick("e1", (now - timedelta(minutes=120)).isoformat(), "soccer_epl")},
            {**make_pick("e2", (now - timedelta(minutes=130)).isoformat(), "soccer_epl")},
            {**make_pick("e3", (now - timedelta(minutes=140)).isoformat(), "basketball_nba")},
        ]
        assert settlement._active_sport_keys(eligible) == ["soccer_epl", "basketball_nba"]

    def test_zero_fetches_when_nothing_eligible(self):
        with _patch_eligible([]):
            _, original = _patch_http_client(None)
            try:
                asyncio.run(settlement.settle_pending_predictions())
            finally:
                _restore_http_client(original)
        # Reaching here without an AssertionError confirms no fetch was attempted.

    def test_fetches_scores_only_for_active_sports(self):
        now = _utc_now()
        eligible = [
            {**make_pick("e1", (now - timedelta(minutes=120)).isoformat(), "soccer_epl"),
             "prediction_id": "p1", "home_team": "A", "away_team": "B",
             "market_type": "h2h", "pick": "A to Win"},
            {**make_pick("e2", (now - timedelta(minutes=130)).isoformat(), "soccer_epl"),
             "prediction_id": "p2", "home_team": "C", "away_team": "D",
             "market_type": "h2h", "pick": "C to Win"},
        ]
        # A single completed event; both picks resolve against it.
        result = {"id": "e1", "completed": True,
                  "scores": [{"name": "A", "score": "2"}, {"name": "B", "score": "1"}]}

        def fake_get(url, params):
            resp = MagicMock()
            resp.json.return_value = [result]
            resp.raise_for_status.return_value = None
            return resp

        mock_client, original = _patch_http_client(fake_get)
        try:
            with _patch_eligible(eligible):
                asyncio.run(settlement.settle_pending_predictions())
        finally:
            _restore_http_client(original)

        # One sport among eligible matches -> exactly one /scores call.
        assert mock_client.get.call_count == 1
        url = mock_client.get.call_args.args[0]
        assert "soccer_epl/scores" in url


# ---------------------------------------------------------------------------
# 5. full resolution from tracked rows (home/away/market_type/selection stored)
# ---------------------------------------------------------------------------
class TestEndToEndSettlement:
    def test_resolution_fields_are_persisted(self):
        now = _utc_now()
        tracking.record_predictions(
            [
                {
                    "event_id": "e50",
                    "match": "Home FC vs Away FC",
                    "sport_key": "soccer_epl",
                    "home_team": "Home FC",
                    "away_team": "Away FC",
                    "market_type": "h2h",
                    "commence_time": (now - timedelta(minutes=120)).isoformat(),
                    "pick": "Home FC to Win",
                    "confidence": 0.8,
                    "odds": 1.5,
                }
            ]
        )
        row = tracking.get_pending_predictions()[0]
        assert row["home_team"] == "Home FC"
        assert row["away_team"] == "Away FC"
        assert row["market_type"] == "h2h"
        assert row["selection"] == "Home FC to Win"

    def test_settles_to_win_from_stored_row(self, monkeypatch):
        """A completed match read back from the DB resolves to SETTLED_WIN."""
        monkeypatch.setattr("config.ODDS_API_KEY", "test-key")
        now = _utc_now()
        tracking.record_predictions(
            [
                {
                    "event_id": "e51",
                    "match": "Home FC vs Away FC",
                    "sport_key": "soccer_epl",
                    "home_team": "Home FC",
                    "away_team": "Away FC",
                    "market_type": "h2h",
                    "commence_time": (now - timedelta(minutes=120)).isoformat(),
                    "pick": "Home FC to Win",
                    "confidence": 0.8,
                    "odds": 1.5,
                }
            ]
        )

        result = {"id": "e51", "completed": True,
                  "scores": [{"name": "Home FC", "score": "2"},
                             {"name": "Away FC", "score": "1"}]}

        def fake_get(url, params):
            resp = MagicMock()
            resp.json.return_value = [result]
            resp.raise_for_status.return_value = None
            return resp

        mock_client, original = _patch_http_client(fake_get)
        try:
            asyncio.run(settlement.settle_pending_predictions())
        finally:
            _restore_http_client(original)

        assert mock_client.get.call_count == 1
        status = tracking.get_pending_predictions()
        global_stats = tracking.get_global_stats()
        assert status == []
        assert global_stats["wins"] == 1


def _restore_http_client(original):
    settlement.httpx.AsyncClient = original