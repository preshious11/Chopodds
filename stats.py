"""
Historical prediction tracking and stats calculation.
Calculates dynamic stats based on bot launch date and user join date.
Uses Africa/Lagos timezone for date determination.
"""

import json
from datetime import date, datetime, timedelta, timezone
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
    """Load stats from JSON file."""
    if not STATS_FILE.exists():
        return {"history": {}, "last_updated": None}
    try:
        with open(STATS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {"history": {}, "last_updated": None}


def _save_stats(data: dict) -> None:
    """Save stats to JSON file."""
    with open(STATS_FILE, "w", encoding="utf-8") as f:
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
        generated_total = len(data.get("generated_predictions", []))

        if not history:
            return {
                "period_days": 0,
                "generated": generated_total,
                "total": 0,
                "won": 0,
                "lost": 0,
                "win_rate": 0,
            }

        today = _today_lagos()

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
            "generated": generated_total,
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
        day_str = (day or _today_lagos()).isoformat()
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


def record_prediction_delivery(user_id: int, predictions: list) -> None:
    """Record unique predictions delivered to a user for future settlement."""
    if not predictions:
        return
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
        pending = data.setdefault("pending", {})
        for prediction in predictions:
            event_id = prediction.get("event_id") or prediction.get("match")
            if not event_id:
                continue
            pending.setdefault(
                str(event_id),
                {
                    "event_id": str(event_id),
                    "league": prediction.get("league", ""),
                    "match": prediction.get("match", ""),
                    "home_team": prediction.get("home_team", ""),
                    "away_team": prediction.get("away_team", ""),
                    "market_type": prediction.get("market_type", ""),
                    "pick": prediction.get("pick", ""),
                    "recipients": [],
                },
            )
            recipients = pending[str(event_id)].setdefault("recipients", [])
            if user_id not in recipients:
                recipients.append(user_id)
        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        _save_stats(data)


def get_pending_predictions() -> list:
    """Return predictions delivered to users but not settled yet."""
    with _lock:
        return list(_load_stats().get("pending", {}).values())


def record_prediction_settlement(event_id: str, result: str, settled_day: date) -> None:
    """Apply one settlement to global and recipient user statistics."""
    if result not in {"win", "loss", "void"}:
        return
    with _lock:
        data = _load_stats()
        pending = data.setdefault("pending", {})
        prediction = pending.pop(str(event_id), None)
        if prediction is None:
            return

        if result == "void":
            data["last_updated"] = datetime.now(timezone.utc).isoformat()
            _save_stats(data)
            return

        history = data.setdefault("history", {})
        entry = history.setdefault(
            settled_day.isoformat(), {"total": 0, "won": 0, "lost": 0, "win_rate": 0}
        )
        entry["total"] += 1
        entry["won" if result == "win" else "lost"] += 1
        entry["win_rate"] = round(entry["won"] / entry["total"] * 100, 1)

        for user_id in prediction.get("recipients", []):
            user = data.setdefault("users", {}).setdefault(
                str(user_id), {"generated": 0, "won": 0, "lost": 0, "delivered": []}
            )
            if result == "win":
                user["won"] += 1
            else:
                user["lost"] += 1

        data["last_updated"] = datetime.now(timezone.utc).isoformat()
        _save_stats(data)


def get_user_stats(user_id: int) -> dict:
    """Return delivery and settled-result totals for one user."""
    with _lock:
        data = _load_stats()
        user = data.get("users", {}).get(str(user_id), {})
        won = user.get("won", 0)
        lost = user.get("lost", 0)
        settled = won + lost
        return {
            "generated": user.get("generated", len(user.get("delivered", []))),
            "won": won,
            "lost": lost,
            "settled": settled,
            "win_rate": round(won / settled * 100, 1) if settled else 0,
        }


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
