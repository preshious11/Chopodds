"""
Sports Prediction Telegram Bot — Production Ready
===================================================
Automated daily predictions across diverse sports and betting markets.
"""

import asyncio
import logging
import signal
import sys
import time
from datetime import datetime, timezone
from logging.handlers import TimedRotatingFileHandler

from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.error import BadRequest, TelegramError, NetworkError, TimedOut

import config
from daily_cache import (
    CACHE_DIR,
    clear_today_cache,
    ensure_populated,
    get_cached_odds,
    get_lagos_date_str,
    validate_daily_cache,
)
from odds_client import _fetch_all_sports_odds
import odds_client
import predictions
from settlement import settle_pending_predictions
import tracking
from predictions import generate_daily_predictions, get_top_picks, filter_by_sport
from subscribers import (
    add_subscriber,
    get_all_chat_ids,
    get_subscriber_join_date,
)
from stats import format_stats_message
from formatters import (
    format_top_picks,
    format_all_picks_paginated,
    format_sport_filter_buttons,
    safe_message,
    escape,
    format_single_prediction,
)

def configure_logging() -> None:
    """Send application logs to stdout and a daily rotating log file."""
    formatter = logging.Formatter(
        "%(asctime)s [%(levelname)s] %(name)s: %(message)s"
    )
    root_logger = logging.getLogger()
    root_logger.setLevel(logging.INFO)

    if not any(getattr(handler, "_sportsbot_stdout", False)
               for handler in root_logger.handlers):
        stdout_handler = logging.StreamHandler(sys.stdout)
        stdout_handler._sportsbot_stdout = True
        stdout_handler.setFormatter(formatter)
        root_logger.addHandler(stdout_handler)

    if not any(getattr(handler, "_sportsbot_file", False)
               for handler in root_logger.handlers):
        file_handler = TimedRotatingFileHandler(
            "bot_activity.log",
            when="midnight",
            backupCount=7,
            encoding="utf-8",
            utc=False,
        )
        file_handler._sportsbot_file = True
        file_handler.setFormatter(formatter)
        root_logger.addHandler(file_handler)


configure_logging()
log = logging.getLogger(__name__)

# Africa/Lagos timezone for daily cache
LAGOS_TZ = ZoneInfo("Africa/Lagos")
_BOT_STARTED_AT = datetime.now(timezone.utc)

# Cache predictions per day (Lagos date)
_cached_predictions = None
_cached_date = None
_prediction_lock = asyncio.Lock()  # Prevents duplicate API calls from simultaneous users
_is_generating = False  # Flag to show loading message


async def get_today_predictions() -> list:
    """
    Get today's predictions (cached per day, Africa/Lagos timezone).
    Thread-safe: only the first concurrent caller triggers generation.
    All subsequent calls (pagination, other users) read from cache.
    """
    global _cached_predictions, _cached_date, _is_generating

    # Use Africa/Lagos time for date determination
    today_lagos = datetime.now(LAGOS_TZ).date()

    # Fast path: cache is valid
    if _cached_date == today_lagos and _cached_predictions is not None:
        return _cached_predictions

    # Slow path: acquire lock to prevent duplicate API calls
    async with _prediction_lock:
        # Another task may have populated while we waited
        if _cached_date == today_lagos and _cached_predictions is not None:
            return _cached_predictions

        # We are the first caller — generate predictions
        _is_generating = True
        try:
            predictions = generate_daily_predictions(max_predictions=20)
            if predictions:
                tracking.record_predictions(predictions)
                _cached_predictions = predictions
                _cached_date = today_lagos
                log.info(f"Generated {len(predictions)} predictions for {today_lagos} (Lagos)")
            else:
                # Do NOT cache an empty result for the whole day — leave the
                # date un-set so the next request retries the fetch. This
                # prevents a transient API failure from locking the bot into
                # "no predictions available" until midnight.
                _cached_predictions = None
                log.warning(
                    "Prediction generation returned 0 matches for %s (Lagos); "
                    "result NOT cached — next request will retry.",
                    today_lagos,
                )
        except (
            ConnectionError,
            TimeoutError,
            OSError,
            AttributeError,
            IndexError,
            KeyError,
            TypeError,
            ValueError,
            ZeroDivisionError,
        ) as e:
            log.error(f"Network error generating predictions: {e}")
            _cached_predictions = None
        finally:
            _is_generating = False

    return _cached_predictions


def is_generating() -> bool:
    """Check if predictions are currently being generated."""
    return _is_generating


def needs_refresh() -> bool:
    """Check if predictions need to be fetched (cache is invalid or empty)."""
    today_lagos = datetime.now(LAGOS_TZ).date()
    return _cached_date != today_lagos or _cached_predictions is None

async def _send_loading(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Send immediate feedback to the user while predictions are being fetched.
    Always shows a typing indicator in the chat header.
    Sends a visible loading message only when a fresh API fetch is expected.
    Returns the loading message object (or None) so the caller can delete it later.
    """
    # Always show typing indicator — gives instant feedback even for cached responses
    await context.bot.send_chat_action(
        chat_id=update.effective_chat.id,
        action=ChatAction.TYPING,
    )

    # Send a visible loading message only when a fresh fetch is expected
    loading_msg = None
    if needs_refresh() or is_generating():
        loading_msg = await update.message.reply_text(
            "🔍 Scanning markets for today's predictions...\n"
            "⏳ This may take a moment..."
        )
    return loading_msg


async def _clear_loading(loading_msg):
    """Delete the loading message if it exists."""
    if loading_msg:
        try:
            await loading_msg.delete()
        except TelegramError:
            pass


# ============================================================================
# Command Handlers
# ============================================================================


async def start(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /start command — welcome user and save subscriber."""
    chat_id = update.effective_chat.id
    user = update.effective_user
    username = user.username or user.first_name or ""

    is_new = add_subscriber(chat_id, username)
    subscriber_count = len(get_all_chat_ids())

    welcome_msg = (
        f"🏆 <b>Welcome to BetVault!</b>\n\n"
        f"{'✅ You are now subscribed!' if is_new else '👋 Welcome back!'}\n"
        f"📊 Subscribers: <b>{subscriber_count}</b>\n\n"
        f"<b>Available Commands:</b>\n"
        f"/dailypick — Top 3-5 picks for today\n"
        f"/toppicks — All predictions (paginated)\n"
        f"/sports — Filter by sport\n"
        f"/stats — Historical performance\n"
        f"/help — Show this menu\n\n"
        f"<i>You will receive daily picks automatically at {config.DAILY_BROADCAST_TIME} UTC</i>"
    )

    await update.message.reply_text(welcome_msg, parse_mode="HTML")




async def help_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /help command."""
    help_msg = (
        "📖 <b>Sports Predictions Bot — Help</b>\n\n"
        "<i>⚠️ DEMO MODE — Mock predictions for testing purposes only</i>\n\n"
        "<b>Commands:</b>\n"
        "/start — Subscribe & show welcome\n"
        "/dailypick — Top 3-5 confidence picks today\n"
        "/toppicks — Browse all picks (paginated, 5 per page)\n"
        "/sports — Filter predictions by sport\n"
        "/stats — View historical win rate\n"
        "/help — This message\n\n"
        "<b>Features:</b>\n"
        "• Predictions refresh daily at midnight\n"
        "• Automated broadcast at 09:00 UTC\n"
        "• Markets: 1X2, O/U, BTTS, Spread, Corners, Cards\n"
        "• Sports: Football, Basketball, Tennis, NHL, Cricket, NFL\n\n"
        "<i>Disclaimer: Predictions are for informational purposes only.</i>"
    )
    await update.message.reply_text(help_msg, parse_mode="HTML")


async def dailypick(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /dailypick — show top 3-5 predictions for today."""
    loading_msg = await _send_loading(update, context)
    predictions = await get_today_predictions()
    top = get_top_picks(predictions, count=config.DAILY_PICK_COUNT)

    if not top:
        await _clear_loading(loading_msg)
        await update.message.reply_text(
            "⚠️ No predictions available for today's matches. Check back tomorrow morning!"
        )
        return

    msg = format_top_picks(top, count=config.DAILY_PICK_COUNT)

    # Add inline button to view all
    keyboard = [
        [InlineKeyboardButton("📅 View All Today's Predictions", callback_data="top:p:1")],
    ]
    reply_markup = InlineKeyboardMarkup(keyboard)

    await _clear_loading(loading_msg)
    output, is_file = safe_message(msg)
    if is_file:
        await update.message.reply_document(
            document=output,
            caption="📋 Top picks for today (file format due to length)",
            parse_mode="HTML",
        )
    else:
        await update.message.reply_text(output, parse_mode="HTML", reply_markup=reply_markup)
    record_prediction_delivery(update.effective_user.id, top)
    tracking.record_predictions(top, update.effective_user.id)

async def toppicks(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /toppicks — show paginated predictions."""
    loading_msg = await _send_loading(update, context)
    predictions = await get_today_predictions()

    if not predictions:
        await _clear_loading(loading_msg)
        await update.message.reply_text(
            "⚠️ No predictions available for today's matches. Check back tomorrow morning!"
        )
        return

    await send_paginated_predictions(update, context, predictions, page=1, loading_msg=loading_msg)


async def send_paginated_predictions(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    predictions: list,
    page: int = 1,
    loading_msg=None,
):
    """Send or edit paginated predictions message."""
    msg, total_pages = format_all_picks_paginated(predictions, page=page, per_page=5)

    # Build navigation buttons
    buttons = []
    nav_row = []

    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"top:p:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"top:p:{page + 1}"))

    if nav_row:
        buttons.append(nav_row)

    # Sport filter buttons
    sport_row = []
    seen_sports = set()
    for pred in predictions:
        sport = pred["sport"]
        if sport not in seen_sports and len(sport_row) < 3:
            sport_row.append(
                InlineKeyboardButton(
                    f"{pred['sport_icon']} {sport}",
                    callback_data=f"sport:{sport}:1",
                )
            )
            seen_sports.add(sport)
    if sport_row:
        buttons.append(sport_row)

    reply_markup = InlineKeyboardMarkup(buttons)

    # Delete loading message if present
    if loading_msg:
        try:
            await loading_msg.delete()
        except TelegramError:
            pass

    try:
        if hasattr(update_or_query, "message"):
            # Called from command
            await update_or_query.message.reply_text(
                msg, parse_mode="HTML", reply_markup=reply_markup
            )
        else:
            # Called from callback query
            await update_or_query.edit_message_text(
                msg, parse_mode="HTML", reply_markup=reply_markup
            )
        if hasattr(update_or_query, "message"):
            record_prediction_delivery(update_or_query.effective_user.id, predictions)
            tracking.record_predictions(predictions, update_or_query.effective_user.id)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return  # Ignore double-click
        raise



async def sports_filter(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /sports — show sport filter buttons."""
    loading_msg = await _send_loading(update, context)
    predictions = await get_today_predictions()

    # Build sport buttons
    sports = {}
    for pred in predictions:
        sport = pred["sport"]
        if sport not in sports:
            sports[sport] = pred["sport_icon"]

    keyboard = []
    row = []
    for sport, icon in sports.items():
        row.append(InlineKeyboardButton(f"{icon} {sport}", callback_data=f"sport:{sport}:1"))
        if len(row) == 2:
            keyboard.append(row)
            row = []
    if row:
        keyboard.append(row)

    await _clear_loading(loading_msg)
    reply_markup = InlineKeyboardMarkup(keyboard)
    await update.message.reply_text(
        format_sport_filter_buttons(predictions), parse_mode="HTML", reply_markup=reply_markup
    )
    record_prediction_delivery(update.effective_user.id, predictions)
    tracking.record_predictions(predictions, update.effective_user.id)


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Show global performance and this user's delivery statistics."""
    chat_id = update.effective_chat.id
    user_joined_at = get_subscriber_join_date(chat_id)
    msg = format_stats_message(user_id=chat_id, user_joined_at=user_joined_at)
    await update.message.reply_text(msg, parse_mode="HTML")


async def status_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Report bot health and today's cache metrics to the administrator."""
    user = update.effective_user
    if user is None or user.id != config.ADMIN_CHAT_ID:
        return

    cache_path = CACHE_DIR / f"odds_{get_lagos_date_str()}.json"
    cache_exists = cache_path.exists()
    cached_odds = get_cached_odds() if cache_exists else {}
    cached_sports = sum(1 for events in cached_odds.values() if events)
    cached_matches = sum(
        len(events) for events in cached_odds.values() if isinstance(events, list)
    )
    uptime = datetime.now(timezone.utc) - _BOT_STARTED_AT
    uptime_seconds = max(0, int(uptime.total_seconds()))
    uptime_days, remainder = divmod(uptime_seconds, 86400)
    uptime_hours, remainder = divmod(remainder, 3600)
    uptime_minutes, seconds = divmod(remainder, 60)
    uptime_text = (
        f"{uptime_days}d {uptime_hours}h {uptime_minutes}m {seconds}s"
    )
    status = "HEALTHY" if cache_exists else "DEGRADED"
    cache_size = cache_path.stat().st_size if cache_exists else 0
    tracking_health = tracking.get_health_metrics()

    # Odds API diagnostics
    api_status = odds_client.check_api_key()
    gen_stats = predictions.get_last_generation_stats()
    api_key_display = (
        "Configured" if api_status["configured"] else "❌ MISSING"
    )
    api_valid_display = (
        "✅ Valid" if api_status["valid"] else f"❌ {api_status['error'] or 'Not verified'}"
    )
    remaining_display = (
        api_status["remaining"]
        if api_status["remaining"] is not None
        else "unknown (no API call made yet)"
    )

    message = (
        "🩺 <b>Bot Status</b>\n\n"
        f"<b>Overall:</b> {status}\n"
        f"<b>Uptime:</b> {uptime_text}\n"
        f"<b>Cache date:</b> {get_lagos_date_str()}\n"
        f"<b>Cache file:</b> {cache_path.name}\n"
        f"<b>Cache size:</b> {cache_size:,} bytes\n"
        f"<b>Cached sports:</b> {cached_sports}\n"
        f"<b>Cached matches:</b> {cached_matches}\n"
        "<b>— Odds API —</b>\n"
        f"<b>API key:</b> {api_key_display}\n"
        f"<b>API key check:</b> {api_valid_display}\n"
        f"<b>Requests remaining:</b> {remaining_display}\n"
        f"<b>Requests used:</b> {api_status['used'] or 'unknown'}\n"
        "<b>— Last generation —</b>\n"
    )
    if gen_stats:
        message += (
            f"<b>Date:</b> {gen_stats.get('date', 'n/a')}\n"
            f"<b>Raw matches from API:</b> {gen_stats.get('raw_events', 0)}\n"
            f"<b>Kick off today (Lagos):</b> {gen_stats.get('events_today', 0)}\n"
            f"<b>No bookmakers:</b> {gen_stats.get('events_no_bookmakers', 0)}\n"
            f"<b>Passed consensus eval:</b> {gen_stats.get('candidates', 0)}\n"
            f"<b>Final predictions:</b> {gen_stats.get('returned', 0)}\n"
            f"<b>League errors:</b> {gen_stats.get('errors', 0)}\n"
            f"<b>Fallback floor applied:</b> "
            f"{'Yes' if gen_stats.get('fallback_applied') else 'No'}\n"
        )
        if gen_stats.get("reason_zero"):
            message += f"<b>⚠️ Zero-prediction reason:</b> {escape(gen_stats['reason_zero'])}\n"
        per_sport = gen_stats.get("per_sport_events") or {}
        if per_sport:
            sport_lines = "\n".join(
                f"  {escape(k)}: {v}" for k, v in per_sport.items()
            )
            message += f"<b>Events today per league:</b>\n{sport_lines}\n"
    else:
        message += "<i>No generation run yet since last restart.</i>\n"
    message += (
        "<b>— Settlement —</b>\n"
        f"<b>Pending settlements:</b> {tracking_health['pending']}\n"
        f"<b>Settled predictions:</b> {tracking_health['settled']}\n"
        f"<b>Last score fetch:</b> {tracking_health['last_score_fetch']}"
    )
    await update.message.reply_text(message, parse_mode="HTML")


def _is_admin(update: Update) -> bool:
    """Check whether the command caller is the configured admin."""
    user = update.effective_user
    return (
        user is not None
        and config.ADMIN_CHAT_ID
        and user.id == config.ADMIN_CHAT_ID
    )


async def force_refresh_cache(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """
    Admin-only: clear today's odds cache and regenerate predictions
    immediately. Useful for debugging on Railway without waiting for
    the midnight (Lagos) cache rollover.
    """
    if not _is_admin(update):
        await update.message.reply_text(
            "⛔ This command is restricted to the bot administrator."
        )
        return

    loading = await update.message.reply_text(
        "🔄 Clearing today's cache and re-fetching odds from the API..."
    )

    # 1. Drop in-memory prediction cache so a fresh fetch is triggered
    global _cached_predictions, _cached_date
    _cached_predictions = None
    _cached_date = None

    # 2. Delete today's on-disk cache file
    removed = clear_today_cache()
    log.info("Admin force-refresh: cache file removed=%s", removed)

    # 3. Force a fresh API fetch + prediction regeneration
    try:
        predictions = await get_today_predictions()
    except Exception as e:  # noqa: BLE001 — report any failure to the admin
        log.error(f"Force refresh failed: {e}")
        await loading.edit_text(f"❌ Force refresh failed: {escape(str(e))}")
        return

    if predictions:
        preview = "\n".join(
            f"• {escape(p['match'])} — {escape(p['pick'])} "
            f"({p['confidence']:.0%})"
            for p in predictions[:5]
        )
        await loading.edit_text(
            f"✅ Cache refreshed — {len(predictions)} predictions regenerated.\n\n"
            f"<b>Top picks preview:</b>\n{preview}",
            parse_mode="HTML",
        )
    else:
        # Credits available but nothing generated — report the exact reason.
        quota = odds_client.get_last_quota()
        gen_stats = predictions.get_last_generation_stats()
        reason = gen_stats.get(
            "reason_zero", "no specific reason recorded — check bot logs"
        )
        api_note = ""
        try:
            api_status = odds_client.check_api_key()
            if not api_status["valid"]:
                api_note = (
                    f"\n<b>⚠️ API key problem:</b> {escape(api_status['error'])}"
                )
            elif api_status["remaining"] is not None and int(
                api_status["remaining"]
            ) == 0:
                api_note = (
                    "\n<b>🔴 [CRITICAL] Odds API quota exhausted "
                    "(0 requests remaining)</b>"
                )
            else:
                api_note = (
                    f"\n<b>API credits:</b> {api_status['remaining']} requests "
                    "remaining — key is valid."
                )
        except Exception as e:  # noqa: BLE001
            api_note = f"\n<b>API check failed:</b> {escape(str(e))}"

        await loading.edit_text(
            "⚠️ Cache cleared but 0 predictions generated.\n"
            f"<b>Reason:</b> {escape(reason)}"
            f"{api_note}\n\n"
            f"<b>Generation stats:</b> raw={gen_stats.get('raw_events', 0)}, "
            f"today={gen_stats.get('events_today', 0)}, "
            f"consensus={gen_stats.get('candidates', 0)}, "
            f"quota_remaining={quota.get('remaining', 'unknown')}",
            parse_mode="HTML",
        )


# ============================================================================
# Callback Query Handlers
# ============================================================================


async def button_callback(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle inline button callbacks for pagination and sport filtering."""
    query = update.callback_query
    await query.answer()

    data = query.data

    # No-op button (page indicator)
    if data == "noop":
        return

    # Parse callback data
    parts = data.split(":")

    # Handle "top:p:X" — paginated all predictions
    if parts[0] == "top" and parts[1] == "p":
        page = int(parts[2])
        predictions = await get_today_predictions()
        await send_paginated_predictions(query, context, predictions, page=page)
        return

    # Handle "sport:NAME:X" — filtered by sport with pagination
    if parts[0] == "sport":
        sport = parts[1]
        page = int(parts[2])
        predictions = await get_today_predictions()
        filtered = filter_by_sport(predictions, sport)
        await send_filtered_predictions(query, context, filtered, sport, page=page)
        return


async def send_filtered_predictions(
    query,
    context: ContextTypes.DEFAULT_TYPE,
    predictions: list,
    sport: str,
    page: int = 1,
):
    """Send filtered predictions with pagination."""
    per_page = 5
    total_pages = max(1, (len(predictions) + per_page - 1) // per_page)
    page = max(1, min(page, total_pages))

    start = (page - 1) * per_page
    end = start + per_page
    page_picks = predictions[start:end]

    msg = f"🔍 <b>{escape(sport)} Predictions</b>\n"
    msg += f"Page {page}/{total_pages} | Showing {len(page_picks)} of {len(predictions)} picks\n\n"

    for i, pred in enumerate(page_picks, start + 1):
        msg += format_single_prediction(pred, i)
        msg += "\n"

    # Navigation
    buttons = []
    nav_row = []
    if page > 1:
        nav_row.append(InlineKeyboardButton("⬅️ Prev", callback_data=f"sport:{sport}:{page - 1}"))
    nav_row.append(InlineKeyboardButton(f"📄 {page}/{total_pages}", callback_data="noop"))
    if page < total_pages:
        nav_row.append(InlineKeyboardButton("Next ➡️", callback_data=f"sport:{sport}:{page + 1}"))

    if nav_row:
        buttons.append(nav_row)

    # Back to all picks
    buttons.append([InlineKeyboardButton("🔙 View All Picks", callback_data="top:p:1")])

    reply_markup = InlineKeyboardMarkup(buttons)

    try:
        await query.edit_message_text(msg, parse_mode="HTML", reply_markup=reply_markup)
    except BadRequest as e:
        if "Message is not modified" in str(e):
            return
        raise

# ============================================================================
# Scheduler
# ============================================================================


async def daily_broadcast(context: ContextTypes.DEFAULT_TYPE):
    """Send daily broadcast to all subscribers."""
    chat_ids = get_all_chat_ids()
    if not chat_ids:
        log.info("No subscribers for daily broadcast")
        return

    predictions = await get_today_predictions()
    top = get_top_picks(predictions, count=config.DAILY_PICK_COUNT)

    if not top:
        # Send empty state message to all subscribers
        for chat_id in chat_ids:
            try:
                await context.bot.send_message(
                    chat_id=chat_id,
                    text="⚠️ No predictions available for today's matches. Check back tomorrow morning!",
                    parse_mode="HTML",
                )
            except TelegramError as e:
                log.warning(f"Failed to send broadcast to {chat_id}: {e}")
        log.warning("No predictions available for daily broadcast - sent empty state to subscribers")
        return

    msg = format_top_picks(top, count=config.DAILY_PICK_COUNT)
    msg += "\n\n<i>Use /dailypick for detailed view or /toppicks for all predictions</i>"

    sent = 0
    failed = 0

    for chat_id in chat_ids:
        try:
            await context.bot.send_message(
                chat_id=chat_id,
                text=msg,
                parse_mode="HTML",
            )
            record_prediction_delivery(chat_id, top)
            tracking.record_predictions(top, chat_id)
            sent += 1
        except TelegramError as e:
            log.warning(f"Failed to send broadcast to {chat_id}: {e}")
            failed += 1

    log.info(f"Daily broadcast sent: {sent} success, {failed} failed")


async def settlement_job(context: ContextTypes.DEFAULT_TYPE) -> None:
    """Run the non-blocking pending-prediction settlement check."""
    await settle_pending_predictions()


# ============================================================================
# Main Entry Point
# ============================================================================


async def log_handler_error(update: object, context: ContextTypes.DEFAULT_TYPE):
    """Log exceptions raised inside command/button handlers.

    Includes the update that triggered it and the full traceback so
    Railway logs show exactly which handler failed and why.
    """
    log.error(
        "Handler exception while processing update",
        exc_info=context.error,
    )
    try:
        if isinstance(update, Update) and update.effective_message:
            origin = ""
            if update.callback_query and update.callback_query.data:
                origin = f" callback={update.callback_query.data!r}"
            elif update.effective_message:
                text = update.effective_message.text or ""
                origin = f" message={text[:60]!r}"
            log.error(f"  from chat_id={chat.id if chat else '?'} ... {origin}")
    except Exception:
        pass
    # Users never see error details — the failure is logged for the admin
    # and surfaced later via /status. The handler simply stays silent.


async def run_bot() -> None:
    """Start the bot and shut it down cleanly on termination signals."""
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()
    shutdown_event = asyncio.Event()

    def request_shutdown() -> None:
        log.info("Shutdown signal received; shutting down gracefully...")
        shutdown_event.set()

    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, getattr(signal, "SIGTERM", None)):
        if sig is None:
            continue
        try:
            loop.add_signal_handler(sig, request_shutdown)
        except NotImplementedError:
            signal.signal(
                sig,
                lambda _signal_number, _frame: loop.call_soon_threadsafe(
                    request_shutdown
                ),
            )

    if not validate_daily_cache():
        log.info("Daily odds cache is missing or invalid; regenerating it")
        await asyncio.to_thread(ensure_populated, _fetch_all_sports_odds)

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("dailypick", dailypick))
    app.add_handler(CommandHandler("toppicks", toppicks))
    app.add_handler(CommandHandler("top", toppicks))
    app.add_handler(CommandHandler("sports", sports_filter))
    app.add_handler(CommandHandler("stats", stats_command))
    app.add_handler(CommandHandler("status", status_command))
    app.add_handler(CommandHandler("force_refresh_cache", force_refresh_cache))

    # Callback handler for inline buttons
    app.add_handler(CallbackQueryHandler(button_callback))

    # Log handler exceptions with full tracebacks — without this, errors
    # like the /stats KeyError vanished from the logs (fixed 2026-09-12).
    app.add_error_handler(log_handler_error)

    # Schedule daily broadcast using PTB's JobQueue
    if app.job_queue:
        # Parse broadcast time — interpreted in Africa/Lagos timezone
        # explicitly, so container/host timezone (e.g. UTC on Railway) does
        # not shift the daily delivery time.
        hour, minute = map(int, config.DAILY_BROADCAST_TIME.split(":"))
        from datetime import time as dt_time

        app.job_queue.run_daily(
            daily_broadcast,
            time=dt_time(hour=hour, minute=minute, tzinfo=LAGOS_TZ),
            name="daily_broadcast",
        )
        app.job_queue.run_repeating(
            settlement_job,
            interval=24 * 60 * 60,
            first=120,
            name="settle_pending_predictions",
        )
        log.info(
            f"Daily broadcast scheduled at {config.DAILY_BROADCAST_TIME} "
            f"Africa/Lagos"
        )
    else:
        log.warning("Job queue not available — daily broadcast disabled")

    log.info("Bot starting...")
    # Retry the Telegram handshake — a single network blip during get_me()
    # used to crash the whole process (seen in local test 2026-09-12).
    handshake_attempts = 5
    for attempt in range(1, handshake_attempts + 1):
        try:
            await app.initialize()
            await app.start()
            await app.updater.start_polling()
            break
        except (TimedOut, NetworkError) as exc:
            if attempt == handshake_attempts:
                raise
            wait = attempt * 5
            log.warning(
                f"Telegram connection failed on startup attempt {attempt}/"
                f"{handshake_attempts} ({exc!r}); retrying in {wait}s"
            )
            await asyncio.sleep(wait)

    try:
        await shutdown_event.wait()
    finally:
        log.info("Stopping Telegram polling and scheduled jobs")
        await app.updater.stop()
        await app.stop()
        await app.shutdown()
        log.info("Bot shutdown complete")


def main() -> None:
    """Run the asynchronous bot lifecycle, self-healing after fatal errors.

    Transient network loss or a Telegram outage previously killed the
    process permanently (Railway then sat idle until manually restarted).
    Now the lifecycle is retried indefinitely with a capped backoff so the
    bot recovers on its own. A Telegram 'Conflict' (two instances polling
    with the same token) is retried on a longer delay because it usually
    means the old container has not fully terminated yet.
    """
    delay = 10
    while True:
        wait = delay
        try:
            asyncio.run(run_bot())
            log.info("Bot lifecycle ended cleanly")
            return
        except KeyboardInterrupt:
            log.info("Bot interrupted by keyboard")
            return
        except TelegramError as exc:
            if "Conflict" in str(exc):
                # Another instance is polling with this token (e.g. the
                # previous Railway container has not terminated yet).
                wait = 60
                log.error(
                    f"Telegram Conflict (another instance polling?): {exc!r}; "
                    f"retrying in {wait}s"
                )
            else:
                log.error(
                    f"Telegram error ended the bot lifecycle: {exc!r}; "
                    f"restarting in {wait}s"
                )
        except Exception:
            log.exception(
                f"Unexpected fatal error ended the bot lifecycle; "
                f"restarting in {wait}s"
            )
        time.sleep(wait)
        delay = min(max(delay * 2, 10), 300)
        wait = min(wait, delay)


if __name__ == "__main__":
    main()

