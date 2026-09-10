"""
Sports Prediction Telegram Bot — Production Ready
===================================================
Automated daily predictions across diverse sports and betting markets.
"""

import asyncio
import html
import logging
from datetime import datetime, date, timezone

from telegram import Update, InlineKeyboardButton, InlineKeyboardMarkup
from telegram.ext import (
    Application,
    CommandHandler,
    CallbackQueryHandler,
    ContextTypes,
)
from telegram.error import BadRequest, TelegramError

import config
from predictions import generate_daily_predictions, get_top_picks, filter_by_sport
from subscribers import add_subscriber, remove_subscriber, get_all_chat_ids, get_subscriber_join_date, get_bot_launch_date
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

# Cache predictions per day
_cached_predictions = None
_cached_date = None


def get_today_predictions() -> list:
    """Get today's predictions (cached per day). STRICTLY FILTERED TO TODAY (UTC) ONLY."""
    global _cached_predictions, _cached_date

    # Use UTC time for strict date comparison
    now_utc = datetime.now(timezone.utc)
    today_utc = now_utc.date()

    if _cached_date != today_utc or _cached_predictions is None:
        _cached_predictions = generate_daily_predictions(max_predictions=25)
        _cached_date = today_utc
        log.info(f"Generated {len(_cached_predictions)} predictions for {today_utc} (UTC)")

    # STRICT FILTER: Ensure ALL predictions are for today (UTC) only
    today_utc_str = today_utc.isoformat()
    filtered = [p for p in _cached_predictions if p.get("date") == today_utc_str]

    if len(filtered) != len(_cached_predictions):
        log.warning(
            f"Filtered out {len(_cached_predictions) - len(filtered)} non-today predictions"
        )
        _cached_predictions = filtered

    return _cached_predictions

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
        f"🏆 <b>Welcome to Sports Predictions Bot!</b>\n\n"
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
    predictions = get_today_predictions()
    top = get_top_picks(predictions, count=config.DAILY_PICK_COUNT)

    if not top:
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
    predictions = get_today_predictions()

    if not predictions:
        await update.message.reply_text(
            "⚠️ No predictions available for today's matches. Check back tomorrow morning!"
        )
        return

    await send_paginated_predictions(update, context, predictions, page=1)


async def send_paginated_predictions(
    update_or_query,
    context: ContextTypes.DEFAULT_TYPE,
    predictions: list,
    page: int = 1,
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
    predictions = get_today_predictions()

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
        predictions = get_today_predictions()
        await send_paginated_predictions(query, context, predictions, page=page)
        return

    # Handle "sport:NAME:X" — filtered by sport with pagination
    if parts[0] == "sport":
        sport = parts[1]
        page = int(parts[2])
        predictions = get_today_predictions()
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

    if data == "noop":
        return

# ============================================================================
# Scheduler
# ============================================================================


async def daily_broadcast(context: ContextTypes.DEFAULT_TYPE):
    """Send daily broadcast to all subscribers."""
    chat_ids = get_all_chat_ids()
    if not chat_ids:
        log.info("No subscribers for daily broadcast")
        return

    predictions = get_today_predictions()
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

