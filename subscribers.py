"""
Subscriber management - stores chat IDs with UTC join timestamps.
"""

import json
from datetime import datetime, timezone
from pathlib import Path
from threading import Lock

_lock = Lock()

SUBSCRIBERS_FILE = Path(__file__).resolve().parent / "subscribers.json"


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


def get_subscriber_join_date(chat_id: int) -> datetime | None:
    """Return the UTC join timestamp for a subscriber, or None if not found."""
    with _lock:
        data = _load_subscribers()
        chat_id_str = str(chat_id)
        info = data.get(chat_id_str)
        if info is None:
            return None
        joined_iso = info.get("joined_at")
        if not joined_iso:
            return None
        try:
            return datetime.fromisoformat(joined_iso).replace(tzinfo=timezone.utc)
        except (ValueError, TypeError):
            return None


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
