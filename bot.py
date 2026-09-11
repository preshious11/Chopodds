"""
Sports Prediction Telegram Bot — Production Ready
===================================================
Automated daily predictions across diverse sports and betting markets.
"""

import asyncio
import logging
from datetime import datetime

from zoneinfo import ZoneInfo

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.constants import ChatAction
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.error import BadRequest, TelegramError

import config
from predictions import generate_daily_predictions, get_top_picks, filter_by_sport
from subscribers import (
    add_subscriber,
    get_all_chat_ids,
    get_subscriber_join_date,
    get_bot_launch_date,
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

logging.basicConfig(
    level=logging.INFO,
    format="%(asctime)s [%(levelname)s] %(name)s: %(message)s",
)
log = logging.getLogger(__name__)

# Africa/Lagos timezone for daily cache
LAGOS_TZ = ZoneInfo("Africa/Lagos")

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
            _cached_predictions = generate_daily_predictions(max_predictions=20)
            _cached_date = today_lagos
            log.info(f"Generated {len(_cached_predictions)} predictions for {today_lagos} (Lagos)")
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
            _cached_predictions = []
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


async def stats_command(update: Update, context: ContextTypes.DEFAULT_TYPE):
    """Handle /stats — show dynamic stats based on bot launch date and user join date."""
    chat_id = update.effective_chat.id

    # Get bot launch date
    bot_launch_date = get_bot_launch_date()

    # Get user's join date (if subscribed)
    user_joined_at = get_subscriber_join_date(chat_id)

    # Format stats message with dynamic dates
    msg = format_stats_message(bot_launch_date=bot_launch_date, user_joined_at=user_joined_at)
    await update.message.reply_text(msg, parse_mode="HTML")


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
            sent += 1
        except TelegramError as e:
            log.warning(f"Failed to send broadcast to {chat_id}: {e}")
            failed += 1

    log.info(f"Daily broadcast sent: {sent} success, {failed} failed")


# ============================================================================
# Main Entry Point
# ============================================================================


def main():
    """Start the bot."""
    app = Application.builder().token(config.TELEGRAM_BOT_TOKEN).build()

    # Command handlers
    app.add_handler(CommandHandler("start", start))
    app.add_handler(CommandHandler("help", help_command))
    app.add_handler(CommandHandler("dailypick", dailypick))
    app.add_handler(CommandHandler("toppicks", toppicks))
    app.add_handler(CommandHandler("top", toppicks))
    app.add_handler(CommandHandler("sports", sports_filter))
    app.add_handler(CommandHandler("stats", stats_command))

    # Callback handler for inline buttons
    app.add_handler(CallbackQueryHandler(button_callback))

    # Schedule daily broadcast using PTB's JobQueue
    if app.job_queue:
        # Parse broadcast time
        hour, minute = map(int, config.DAILY_BROADCAST_TIME.split(":"))
        from datetime import time as dt_time

        app.job_queue.run_daily(
            daily_broadcast,
            time=dt_time(hour=hour, minute=minute),
            name="daily_broadcast",
        )
        log.info(f"Daily broadcast scheduled at {config.DAILY_BROADCAST_TIME} UTC")
    else:
        log.warning("Job queue not available — daily broadcast disabled")

    log.info("Bot starting...")
    app.run_polling()


if __name__ == "__main__":
    main()

