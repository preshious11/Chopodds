"""
Historical prediction tracking and stats calculation.
Calculates dynamic stats based on bot launch date and user join date.
Uses Africa/Lagos timezone for date determination.

This module delegates all persistence and retrieval to tracking.py (SQLite).
It retains only UI formatting logic (format_stats_message).
"""

import json
from datetime import date, datetime, timezone
from pathlib import Path
from threading import Lock

from zoneinfo import ZoneInfo

import tracking

_lock = Lock()

STATS_FILE = Path(__file__).resolve().parent / "stats.json"
LAGOS_TZ = ZoneInfo("Africa/Lagos")


def _today_lagos() -> date:
    """Get today's date in Africa/Lagos timezone."""
    return datetime.now(LAGOS_TZ).date()


def _load_stats() -> dict:
    """Load stats from JSON file (backward-compat snapshot)."""
    if not STATS_FILE.exists():
        return {"history": {}, "last_updated": None}
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, OSError):
        return {"history": {}, "last_updated": None}


def _save_stats(data: dict) -> None:
    """Save stats to JSON file (backward-compat snapshot)."""
    with open(STATS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def record_prediction_delivery(user_id: int, predictions: list) -> None:
    """Record unique predictions delivered to a user for future settlement.

    Delegates to tracking.record_predictions for the authoritative SQLite-backed
    storage. Also writes a lightweight JSON snapshot for backward compatibility.
    """
    if not predictions:
        return
    tracking.record_predictions(predictions, user_id)
    # Lightweight JSON snapshot — best-effort, non-critical
    with _lock:
        data = _load_stats()
        generated_predictions = data.setdefault("generated_predictions", [])
        users = data.setdefault("users", {})
        user = users.setdefault(
            str(user_id), {"generated": 0, "won": 0, "lost": 0, "delivered": []}
        )
        delivered = set(user.get("delivered", []))
        new_ids = {
            str(prediction.get("event_id") or prediction.get("match"))
            for prediction in predictions
            if prediction.get("event_id") or prediction.get("match")
        } - delivered
        for event_id in new_ids:
            if event_id not in generated_predictions:
                generated_predictions.append(event_id)
        user["delivered"] = sorted(delivered | new_ids)
        user["generated"] = len(user["delivered"])
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        _save_stats(data)


def get_pending_predictions() -> list[dict]:
    """Return predictions delivered to users but not settled yet.

    Delegates to tracking.py for the authoritative SQLite-backed view.
    """
    return tracking.get_pending_predictions()


def record_prediction_settlement(event_id: str, result: str, settled_day: date) -> None:
    """Apply one settlement to global and recipient user statistics.

    Delegates to tracking.settle_prediction for the authoritative SQLite-backed
    settlement. The result string is mapped to the expected status format.
    """
    if result not in {"win", "loss", "void"}:
        return
    status = "SETTLED_WIN" if result == "win" else "SETTLED_LOSS" if result == "loss" else "VOID"
    tracking.settle_prediction(event_id, status)


def get_user_stats(user_id: int) -> dict:
    """Return delivery and settled-result totals for one user.

    Delegates to tracking.py for the authoritative SQLite-backed stats.
    """
    return tracking.get_user_stats(user_id)


def format_stats_message(user_id: int, user_joined_at: datetime = None) -> str:
    """Format global and user-specific performance statistics."""
    bot_stats = tracking.get_global_stats()
    user_stats = tracking.get_user_stats(user_id, user_joined_at)
    message = (
        "📊 <b>Performance Summary</b>\n\n"
        "<b>ALL-TIME BOT STATS</b>\n"
        f"• Predictions generated: <code>{bot_stats['generated']}</code>\n"
        f"• Wins / losses: <code>{bot_stats['wins']} / {bot_stats['losses']}</code>\n"
        f"• Overall win rate: <code>{bot_stats['win_rate']}%</code>\n\n"
        "<b>YOUR PERSONAL STATS</b>\n"
    )
    if user_joined_at is None:
        return message + "• Status: <code>Not subscribed</code>\n"
    joined = user_joined_at.astimezone(LAGOS_TZ).strftime("%Y-%m-%d")
    return message + (
        f"• Active member since: <code>{joined}</code>\n"
        f"• Predictions generated for you: <code>{user_stats['generated']}</code>\n"
        f"• Your predictions settled: <code>{user_stats['won']} / {user_stats['lost']}</code>\n"
        f"• Your win rate: <code>{user_stats['win_rate']}%</code>"
    )
