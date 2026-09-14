"""Unit tests for the bot startup and graceful shutdown lifecycle."""

from unittest.mock import AsyncMock, MagicMock

import pytest

import bot


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
