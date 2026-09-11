"""
Subscriber management - stores chat IDs with UTC join timestamps.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_lock = Lock()

SUBSCRIBERS_FILE = Path(__file__).resolve().parent / "subscribers.json"

# Bot launch date - used as baseline for all stats
BOT_LAUNCH_DATE = datetime(2026, 9, 10, 0, 0, 0, tzinfo=timezone.utc)


def _load_subscribers() -> dict:
    """Load subscribers from JSON file."""
    if not SUBSCRIBERS_FILE.exists():
        return {}
    try:
        with open(SUBSCRIBERS_FILE, "r", encoding="utf-8") as f:
            return json.load(f)
    except (json.JSONDecodeError, IOError):
        return {}


def _save_subscribers(data: dict) -> None:
    """Save subscribers to JSON file."""
    with open(SUBSCRIBERS_FILE, "w", encoding="utf-8") as f:
        json.dump(data, f, indent=2)


def add_subscriber(chat_id: int, username: str = "") -> bool:
    """
    Add a subscriber. Returns True if newly added, False if already exists.
    Records the UTC timestamp when the user first joined.
    """
    with _lock:
        data = _load_subscribers()
        chat_id_str = str(chat_id)
        if chat_id_str in data:
            return False
        data[chat_id_str] = {
            "chat_id": chat_id,
            "username": username,
            "joined_at": datetime.now(timezone.utc).isoformat(),
        }
        _save_subscribers(data)
        return True


def remove_subscriber(chat_id: int) -> bool:
    """Remove a subscriber. Returns True if removed, False if not found."""
    with _lock:
        data = _load_subscribers()
        chat_id_str = str(chat_id)
        if chat_id_str not in data:
            return False
        del data[chat_id_str]
        _save_subscribers(data)
        return True


def get_all_chat_ids() -> list:
    """Return list of all subscribed chat IDs."""
    with _lock:
        data = _load_subscribers()
        return [v["chat_id"] for v in data.values()]


def get_subscriber_count() -> int:
    """Return total number of subscribers."""
    with _lock:
        data = _load_subscribers()
        return len(data)


def get_subscriber_info(chat_id: int) -> dict:
    """
    Get subscriber info including join date.
    Returns None if not found.
    """
    with _lock:
        data = _load_subscribers()
        chat_id_str = str(chat_id)
        if chat_id_str not in data:
            return None
        return data[chat_id_str]


def get_subscriber_join_date(chat_id: int) -> datetime:
    """
    Get the UTC datetime when a user joined.
    Returns None if not found.
    """
    info = get_subscriber_info(chat_id)
    if info and "joined_at" in info:
        return datetime.fromisoformat(info["joined_at"])
    return None


def get_bot_launch_date() -> datetime:
    """Return the bot launch date in UTC."""
    return BOT_LAUNCH_DATE
