"""Unit tests for the bot startup and graceful shutdown lifecycle."""

from datetime import datetime, timedelta
from unittest.mock import AsyncMock, MagicMock

import pytest
from telegram.error import Forbidden, RetryAfter

import bot

PICK = {
    "event_id": "event-1",
    "sport": "Football",
    "sport_icon": "⚽",
    "match": "Alpha FC vs Beta FC",
    "pick": "Alpha FC to Win",
    "market_type": "h2h",
    "odds": 1.5,
    "confidence": 0.7,
    "confidence_tier": "High Confidence",
    "match_time": "15:00",
    "league": "Test League",
}


@pytest.mark.asyncio
async def test_run_bot_starts_and_shuts_down_on_signal(monkeypatch):
    """A captured termination signal stops all Telegram services cleanly."""
    application = MagicMock()
    application.initialize = AsyncMock()
    application.start = AsyncMock()
    application.stop = AsyncMock()
    application.shutdown = AsyncMock()
    application.updater.start_polling = AsyncMock()
    application.updater.stop = AsyncMock()
    application.bot.delete_webhook = AsyncMock()
    application.job_queue = None

    builder = MagicMock()
    builder.token.return_value = builder
    builder.request.return_value = builder
    builder.build.return_value = application
    monkeypatch.setattr(bot.Application, "builder", MagicMock(return_value=builder))
    monkeypatch.setattr(bot.config, "TELEGRAM_BOT_TOKEN", "test-token")
    monkeypatch.setattr(bot, "validate_daily_cache", MagicMock(return_value=True))
    monkeypatch.setattr(bot, "register_handlers", MagicMock(), raising=False)
    monkeypatch.setattr(bot, "register_daily_broadcast", MagicMock(), raising=False)

    loop = bot.asyncio.get_running_loop()
    registered_handlers = []

    def capture_signal_handler(_signal, handler):
        registered_handlers.append(handler)

    monkeypatch.setattr(loop, "add_signal_handler", capture_signal_handler)

    async def trigger_shutdown():
        while not registered_handlers:
            await bot.asyncio.sleep(0)
        registered_handlers[0]()

    shutdown_trigger = bot.asyncio.create_task(trigger_shutdown())
    await bot.run_bot()
    await shutdown_trigger

    builder.token.assert_called_once_with("test-token")
    application.initialize.assert_awaited_once()
    application.start.assert_awaited_once()
    application.updater.start_polling.assert_awaited_once()
    application.updater.stop.assert_awaited_once()
    application.stop.assert_awaited_once()
    application.shutdown.assert_awaited_once()
    assert len(registered_handlers) >= 1


@pytest.mark.asyncio
async def test_cache_warmup_is_non_blocking_and_noop_when_valid(monkeypatch):
    """Valid cache -> warm-up is a non-blocking no-op (no worker thread used)."""
    to_thread_called = []

    async def fake_to_thread(fn, *args, **kwargs):
        to_thread_called.append(fn)
        return {}

    monkeypatch.setattr(bot.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(bot, "validate_daily_cache", MagicMock(return_value=True))

    # Must return immediately (fire-and-forget), not await the fetch.
    bot._schedule_cache_warmup()
    await bot.asyncio.sleep(0.05)  # let any task finish

    assert to_thread_called == []
    bot._BACKGROUND_TASKS.clear()


@pytest.mark.asyncio
async def test_cache_warmup_runs_fetch_in_worker_thread_when_missing(monkeypatch):
    """Missing cache -> warming is delegated to asyncio.to_thread (off loop)."""
    captured = {}

    async def fake_to_thread(fn, *args, **kwargs):
        captured["fn"] = fn
        return {}

    monkeypatch.setattr(bot.asyncio, "to_thread", fake_to_thread)
    monkeypatch.setattr(bot, "validate_daily_cache", MagicMock(return_value=False))

    await bot._warm_daily_cache_background()

    assert captured.get("fn") is bot.ensure_populated


@pytest.fixture
def broadcast_env(monkeypatch):
    """Isolate daily_broadcast from the database, subscribers and generation."""
    env = {"marks": MagicMock(), "recorded": MagicMock(), "removed": MagicMock()}
    monkeypatch.setattr(bot.tracking, "has_daily_broadcast_run", lambda day: False)
    monkeypatch.setattr(bot.tracking, "mark_daily_broadcast_run", env["marks"])
    monkeypatch.setattr(bot.tracking, "record_predictions", env["recorded"])
    monkeypatch.setattr(bot, "remove_subscriber", env["removed"])
    monkeypatch.setattr(bot, "get_today_predictions", AsyncMock(return_value=[PICK]))
    return env


def _context(send_side_effect):
    context = MagicMock()
    context.bot.send_message = AsyncMock(side_effect=send_side_effect)
    return context


@pytest.mark.asyncio
async def test_daily_broadcast_continues_past_blocked_subscriber(monkeypatch, broadcast_env):
    """A blocked chat is unsubscribed and everyone else still gets the picks."""
    monkeypatch.setattr(bot, "get_all_chat_ids", lambda: [1, 2, 3])
    context = _context([Forbidden("blocked"), None, None])

    await bot.daily_broadcast(context)

    assert context.bot.send_message.await_count == 3
    broadcast_env["removed"].assert_called_once_with(1)
    assert [call.args[1] for call in broadcast_env["recorded"].call_args_list] == [2, 3]
    broadcast_env["marks"].assert_called_once()


@pytest.mark.asyncio
async def test_broadcast_retries_after_telegram_flood_limit(monkeypatch, broadcast_env):
    monkeypatch.setattr(bot, "get_all_chat_ids", lambda: [1])
    context = _context([RetryAfter(0), None])

    await bot.daily_broadcast(context)

    assert context.bot.send_message.await_count == 2
    assert broadcast_env["recorded"].call_args.args[1] == 1


@pytest.mark.asyncio
async def test_broadcast_is_sent_at_most_once_per_day(monkeypatch, broadcast_env):
    monkeypatch.setattr(bot.tracking, "has_daily_broadcast_run", lambda day: True)
    monkeypatch.setattr(bot, "get_all_chat_ids", lambda: [1])
    context = _context([None])

    await bot.daily_broadcast(context)

    context.bot.send_message.assert_not_awaited()


@pytest.mark.asyncio
async def test_missed_broadcast_catch_up_waits_for_the_scheduled_time(monkeypatch):
    monkeypatch.setattr(bot.asyncio, "sleep", AsyncMock())
    monkeypatch.setattr(bot.tracking, "has_daily_broadcast_run", lambda day: False)
    app = MagicMock()

    monkeypatch.setattr(bot, "_broadcast_time_today",
                        lambda: datetime.now(bot.LAGOS_TZ) + timedelta(hours=1))
    await bot._check_missed_broadcast(app)
    app.job_queue.run_once.assert_not_called()

    monkeypatch.setattr(bot, "_broadcast_time_today",
                        lambda: datetime.now(bot.LAGOS_TZ) - timedelta(hours=1))
    await bot._check_missed_broadcast(app)
    app.job_queue.run_once.assert_called_once()


@pytest.mark.asyncio
async def test_page_button_edits_message_instead_of_sending_new_one():
    """CallbackQuery also has .message, so it must not be treated as a command."""
    query = MagicMock()
    query.edit_message_text = AsyncMock()
    query.message.reply_text = AsyncMock()

    await bot.send_paginated_predictions(query, MagicMock(), [PICK] * 6, page=2)

    query.edit_message_text.assert_awaited_once()
    query.message.reply_text.assert_not_awaited()


@pytest.mark.asyncio
async def test_failed_generation_returns_empty_list(monkeypatch):
    """Handlers slice and iterate the result, so it must never be None."""
    monkeypatch.setattr(bot, "_cached_predictions", None)
    monkeypatch.setattr(bot, "_cached_date", None)
    monkeypatch.setattr(bot, "_prediction_lock", bot.asyncio.Lock())

    def broken_generation():
        raise ValueError("bad payload")

    monkeypatch.setattr(bot, "generate_daily_predictions", broken_generation)

    assert await bot.get_today_predictions() == []
