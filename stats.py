"""
Historical prediction tracking and stats calculation.
Calculates dynamic stats based on bot launch date and user join date.
Uses Africa/Lagos timezone for date determination.

This module delegates all persistence and retrieval to tracking.py (SQLite).
It exposes ``format_stats_message`` (with a 5-minute in-memory TTL cache) and
cleans up that cache on ``invalidate_stats_cache()`` when settlement completes.
"""

import time
from datetime import datetime
from threading import Lock
from zoneinfo import ZoneInfo

import tracking

_lock = Lock()
LAGOS_TZ = ZoneInfo("Africa/Lagos")

# In-memory TTL cache for the formatted /stats output (5 minutes). Keyed by
# user_id so the bot can answer repeated /stats requests without re-querying
# SQLite. Automatically invalidated whenever a settlement job updates records.
STATS_CACHE_TTL = 300
_STATS_CACHE: dict[int, dict] = {}


def _settled_line(stats: dict) -> str:
    """Human-readable settled-games breakdown; empty-safe when nothing settled."""
    if not stats["settled"]:
        return "No settled games yet"
    return (
        f"{stats['settled']} (Wins: {stats['wins']} | Losses: {stats['losses']} "
        f"| Void: {stats['voids']})"
    )


def _win_rate_line(stats: dict) -> str:
    """Win rate = wins / settled games * 100; no division by zero when empty."""
    if not stats["settled"]:
        return "No settled games yet"
    return f"{stats['win_rate']}%"


def _render_stats_message(user_id: int, user_joined_at: datetime = None) -> str:
    """Compute (uncached) global and user-specific performance statistics.

    Pulls authoritative counts from ``tracking.get_global_stats`` and
    ``tracking.get_user_stats``, then joins them into the /stats message body.
    """
    bot_stats = tracking.get_global_stats()
    user_stats = tracking.get_user_stats(user_id, user_joined_at)
    message = (
        "📊 <b>Performance Summary</b>\n\n"
        "<b>ALL-TIME BOT STATS</b>\n"
        f"• Total Delivered: <code>{bot_stats['delivered']}</code>\n"
        f"• Pending Results: <code>{bot_stats['pending']}</code>\n"
        f"• Settled Games: <code>{_settled_line(bot_stats)}</code>\n"
        f"• Win Rate: <code>{_win_rate_line(bot_stats)}</code>\n\n"
        "<b>YOUR PERSONAL STATS</b>\n"
    )
    if user_joined_at is None:
        return message + "• Status: <code>Not subscribed</code>\n"
    joined = user_joined_at.astimezone(LAGOS_TZ).strftime("%Y-%m-%d")
    return message + (
        f"• Active member since: <code>{joined}</code>\n"
        f"• Total Delivered: <code>{user_stats['delivered']}</code>\n"
        f"• Pending Results: <code>{user_stats['pending']}</code>\n"
        f"• Settled Games: <code>{_settled_line(user_stats)}</code>\n"
        f"• Win Rate: <code>{_win_rate_line(user_stats)}</code>"
    )


def invalidate_stats_cache() -> None:
    """Clear the in-memory /stats cache (called after settlement completes)."""
    with _lock:
        _STATS_CACHE.clear()


def format_stats_message(user_id: int, user_joined_at: datetime = None) -> str:
    """Return the formatted /stats message, served from a 5-minute TTL cache.

    Within the TTL window a repeated request from the same user is answered
    instantly from memory without querying SQLite. The cache is invalidated by
    ``invalidate_stats_cache()`` whenever a settlement job updates records.
    """
    now = time.monotonic()
    with _lock:
        entry = _STATS_CACHE.get(user_id)
        if (
            entry is not None
            and now - entry["ts"] < STATS_CACHE_TTL
            and entry.get("joined") == user_joined_at
        ):
            return entry["text"]

    text = _render_stats_message(user_id, user_joined_at)

    with _lock:
        _STATS_CACHE[user_id] = {"ts": now, "text": text, "joined": user_joined_at}
    return text
