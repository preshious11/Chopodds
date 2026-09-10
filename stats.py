"""
Historical prediction tracking and stats calculation.
Calculates dynamic stats based on bot launch date and user join date.
"""

import json
import random
from datetime import datetime, timezone, date
from pathlib import Path
from threading import Lock

_lock = Lock()

STATS_FILE = Path(__file__).resolve().parent / "stats.json"


def _load_stats() -> dict:
    """Load stats from JSON file."""
    if not STATS_FILE.exists():
        return {"history": {}, "last_updated": None}
    try:
        with open(STATS_FILE, "r") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"history": {}, "last_updated": None}


def _save_stats(data: dict) -> None:
    """Save stats to JSON file."""
    with open(STATS_FILE, "w") as f:
        json.dump(data, f, indent=2)


def get_stats_since(since_date: date = None) -> dict:
    """
    Get stats since a specific date (inclusive).
    If no date provided, returns all-time stats.

    Returns:
        Dictionary with total, won, lost, win_rate
    """
    with _lock:
        data = _load_stats()
        history = data.get("history", {})

        if not history:
            return {
                "period_days": 0,
                "total": 0,
                "won": 0,
                "lost": 0,
                "win_rate": 0,
            }

        today = datetime.now(timezone.utc).date()

        # Determine the start date
        if since_date is None:
            # Use earliest date in history
            since_date = min(date.fromisoformat(d) for d in history.keys())

        total_predictions = 0
        total_won = 0
        total_lost = 0

        for day_str, entry in history.items():
            day = date.fromisoformat(day_str)
            if day >= since_date:
                total_predictions += entry.get("total", 0)
                total_won += entry.get("won", 0)
                total_lost += entry.get("lost", 0)

        win_rate = round(total_won / total_predictions * 100, 1) if total_predictions > 0 else 0

        # Calculate actual days
        period_days = (today - since_date).days + 1

        return {
            "period_days": period_days,
            "total": total_predictions,
            "won": total_won,
            "lost": total_lost,
            "win_rate": win_rate,
            "since_date": since_date.isoformat(),
        }


def get_daily_record(day: date = None) -> dict:
    """Get record for a specific day."""
    with _lock:
        data = _load_stats()
        day_str = (day or datetime.now(timezone.utc).date()).isoformat()
        return data.get("history", {}).get(day_str, {"total": 0, "won": 0, "lost": 0, "win_rate": 0})


def record_prediction_result(day: date, won: bool):
    """Record a prediction result for a specific day."""
    with _lock:
        data = _load_stats()
        history = data.setdefault("history", {})
        day_str = day.isoformat()

        if day_str not in history:
            history[day_str] = {"total": 0, "won": 0, "lost": 0, "win_rate": 0}

        entry = history[day_str]
        entry["total"] += 1
        if won:
            entry["won"] += 1
        else:
            entry["lost"] += 1

        entry["win_rate"] = round(entry["won"] / entry["total"] * 100, 1)
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        _save_stats(data)


def format_stats_message(bot_launch_date: datetime, user_joined_at: datetime = None) -> str:
    """
    Format stats as an HTML message with dynamic date ranges.

    Args:
        bot_launch_date: When the bot was launched (UTC)
        user_joined_at: When the current user joined (UTC), None if not subscribed
    """
    now = datetime.now(timezone.utc)
    today = now.date()

    # Calculate bot age
    bot_launch_date_only = bot_launch_date.date()
    bot_age_days = (today - bot_launch_date_only).days + 1

    # Get bot-wide stats since launch
    bot_stats = get_stats_since(bot_launch_date_only)

    msg = "📊 <b>BOT PERFORMANCE & YOUR STATS</b>\n\n"

    # Bot overall performance
    msg += "<b>🤖 Bot Overall Performance</b>\n"
    msg += f"• Bot Online For: <code>{bot_age_days} days</code>\n"
    msg += f"• Total Predictions Made: <code>{bot_stats['total']}</code>\n"
    if bot_stats['total'] > 0:
        msg += f"• Bot Win Rate: <code>{bot_stats['win_rate']}%</code> ({bot_stats['won']}/{bot_stats['total']})\n"
    else:
        msg += "• Bot Win Rate: <code>No data yet</code>\n"

    msg += "\n"

    # Personal stats
    msg += "<b>👤 Your Personal Subscription Stats</b>\n"

    if user_joined_at is None:
        msg += "• Status: <code>Not subscribed</code>\n"
        msg += "• Use /start to subscribe and track your stats!\n"
    else:
        user_joined_date = user_joined_at.date()
        user_active_days = (today - user_joined_date).days + 1
        user_joined_date_str = user_joined_date.strftime("%Y-%m-%d")

        # Get user-specific stats since they joined
        user_stats = get_stats_since(user_joined_date)

        msg += f"• Subscribed Since: <code>{user_joined_date_str}</code> ({user_active_days} days active)\n"
        msg += f"• Predictions Delivered To You: <code>{user_stats['total']}</code>\n"

        if user_stats['total'] > 0:
            msg += f"• Your Record: <code>{user_stats['won']} Won / {user_stats['lost']} Lost</code>\n"
            msg += f"• Your Personal Win Rate: <code>{user_stats['win_rate']}%</code>\n"
        else:
            msg += "• Your Personal Win Rate: <code>No settled predictions yet</code>\n"

    return msg
