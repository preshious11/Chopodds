"""Tests for the in-memory /stats TTL cache and its settlement invalidation.

Run: python -m pytest test_stats_cache.py -v
"""

import asyncio
from datetime import datetime, timedelta, timezone
from unittest.mock import AsyncMock, MagicMock

import pytest

import settlement
import stats
import tracking


@pytest.fixture(autouse=True)
def in_memory_tracking(monkeypatch):
    monkeypatch.setattr(tracking, "DB_PATH", ":memory:")
    monkeypatch.setattr(tracking, "_MEMORY_CONNECTION", None)
    # Ensure each test starts with an empty stats cache.
    stats._STATS_CACHE.clear()


def _record_pick(event_id="e1", kickoff_ago=timedelta(minutes=120),
                 result_scores=None):
    now = datetime.now(timezone.utc)
    tracking.record_predictions(
        [
            {
                "event_id": event_id,
                "match": "Home FC vs Away FC",
                "sport_key": "soccer_epl",
                "home_team": "Home FC",
                "away_team": "Away FC",
                "market_type": "h2h",
                "commence_time": (now - kickoff_ago).isoformat(),
                "pick": "Home FC to Win",
                "confidence": 0.8,
                "odds": 1.5,
            }
        ]
    )


def _patch_http_client(fake_get):
    mock_client = MagicMock()
    mock_client.get = AsyncMock(side_effect=fake_get)
    enter_ctx = MagicMock()
    enter_ctx.__aenter__ = AsyncMock(return_value=mock_client)
    enter_ctx.__aexit__ = AsyncMock(return_value=False)
    mock_client.return_value = enter_ctx
    original = settlement.httpx.AsyncClient
    settlement.httpx.AsyncClient = mock_client
    return mock_client, original


class TestStatsCache:
    def test_repeated_call_within_ttl_hits_cache(self, monkeypatch):
        counters = {"global": 0}

        real_global = tracking.get_global_stats
        real_user = tracking.get_user_stats

        def counting_global(*a, **k):
            counters["global"] += 1
            return real_global(*a, **k)

        monkeypatch.setattr(tracking, "get_global_stats", counting_global)

        msg1 = stats.format_stats_message(user_id=42)
        msg2 = stats.format_stats_message(user_id=42)
        assert msg1 == msg2
        assert counters["global"] == 1  # second call served from cache

    def test_invalidate_clears_cache(self, monkeypatch):
        counters = {"global": 0}
        real_global = tracking.get_global_stats
        monkeypatch.setattr(
            tracking, "get_global_stats",
            lambda *a, **k: (counters.__setitem__("global", counters["global"] + 1)
                             or real_global(*a, **k)),
        )

        stats.format_stats_message(user_id=42)
        assert counters["global"] == 1
        stats.invalidate_stats_cache()
        stats.format_stats_message(user_id=42)
        assert counters["global"] == 2  # recomputed after invalidation


class TestSettlementInvalidatesCache:
    def test_settlement_run_clears_stats_cache(self, monkeypatch):
        monkeypatch.setattr("config.ODDS_API_KEY", "test-key")
        _record_pick("e1")

        # Populate the cache with the pending view.
        stats.format_stats_message(user_id=42)
        assert stats._STATS_CACHE

        def fake_get(url, params):
            resp = MagicMock()
            resp.json.return_value = [
                {"id": "e1", "completed": True,
                 "scores": [{"name": "Home FC", "score": "2"},
                            {"name": "Away FC", "score": "1"}]}
            ]
            resp.raise_for_status.return_value = None
            return resp

        mock_client, original = _patch_http_client(fake_get)
        try:
            asyncio.run(settlement.settle_pending_predictions())
        finally:
            settlement.httpx.AsyncClient = original

        # Settlement updated a record -> cache emptied.
        assert stats._STATS_CACHE == {}

        # A fresh /stats now reflects the win.
        msg = stats.format_stats_message(user_id=42)
        assert "Wins: 1" in msg