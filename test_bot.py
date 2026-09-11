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
    application.job_queue = None

    builder = MagicMock()
    builder.token.return_value = builder
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
