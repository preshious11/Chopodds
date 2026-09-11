"""
Shared daily cache for odds data based on Africa/Lagos timezone.

Ensures only one API call per day across all users, even with concurrent
requests. The cache is file-persisted so it survives bot restarts.

At midnight Lagos time, the cache automatically expires because the date
changes and a new cache file is created. Old cache files are kept for
historical reference and cleaned up after configurable retention period.
"""

import json
import logging
from datetime import datetime, date, timedelta
from pathlib import Path
from threading import Lock

from zoneinfo import ZoneInfo

logger = logging.getLogger(__name__)

LAGOS_TZ = ZoneInfo("Africa/Lagos")
CACHE_DIR = Path(__file__).resolve().parent / ".daily_cache"
_LOCK = Lock()


def get_lagos_date() -> date:
    """Get current date in Africa/Lagos timezone."""
    return datetime.now(LAGOS_TZ).date()


def get_lagos_date_str() -> str:
    """Get current date string in Africa/Lagos timezone."""
    return get_lagos_date().isoformat()


def _get_cache_path(date_str: str = None) -> Path:
    """Get cache file path for a specific date (default: today Lagos)."""
    CACHE_DIR.mkdir(exist_ok=True)
    if date_str is None:
        date_str = get_lagos_date_str()
    return CACHE_DIR / f"odds_{date_str}.json"


def get_cached_odds() -> dict:
    """
    Get today's cached odds data for all sports.
    Returns a dict mapping sport_key -> odds_data.
    """
    cache_path = _get_cache_path()

    if not cache_path.exists():
        return {}

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("sports", {})
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read daily cache: {e}")
        return {}


def set_cached_odds(sports_data: dict) -> None:
    """
    Store today's odds data for all sports.
    sports_data: dict mapping sport_key -> odds_data
    """
    cache_path = _get_cache_path()

    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": get_lagos_date_str(),
                    "cached_at": datetime.now(LAGOS_TZ).isoformat(),
                    "sports": sports_data,
                },
                f,
            )
        logger.info(
            f"Stored daily odds cache for {get_lagos_date_str()} "
            f"with {len(sports_data)} sports"
        )
    except OSError as e:
        logger.warning(f"Failed to write daily cache: {e}")


def is_cache_valid() -> bool:
    """Check if today's cache exists and is valid (Lagos date)."""
    cache_path = _get_cache_path()
    return cache_path.exists()


def ensure_populated(fetch_fn) -> dict:
    """
    Ensure the daily cache is populated. Thread-safe using double-checked
    locking pattern — only the first concurrent caller triggers the fetch.

    fetch_fn: callable that returns dict mapping sport_key -> odds_data
    Returns the cached sports data (dict of sport_key -> odds_data).
    """
    # Fast path: check without acquiring lock
    if is_cache_valid():
        return get_cached_odds()

    # Slow path: acquire lock and check again (double-checked locking)
    with _LOCK:
        # Another thread may have populated while we waited
        if is_cache_valid():
            return get_cached_odds()

        # We are the first caller — fetch and populate
        logger.info("Populating daily odds cache (first request of the day)...")
        try:
            sports_data = fetch_fn()
            # Persist the completed attempt, including an empty result. Once
            # today's file exists, it is authoritative until the next date.
            set_cached_odds(sports_data or {})
            return sports_data
        except (ConnectionError, TimeoutError, OSError) as e:
            logger.error(f"Failed to populate daily cache: {e}")
            # Return whatever we have (might be empty dict)
            return get_cached_odds()


def cleanup_old_cache(keep_days: int = 30) -> None:
    """
    Remove cache files older than keep_days.
    Keeps historical records for the retention period, then deletes.
    """
    if not CACHE_DIR.exists():
        return

    cutoff = get_lagos_date() - timedelta(days=keep_days)
    for cache_file in CACHE_DIR.glob("odds_*.json"):
        try:
            date_str = cache_file.stem.replace("odds_", "")
            file_date = date.fromisoformat(date_str)
            if file_date < cutoff:
                cache_file.unlink()
                logger.info(f"Removed old cache file: {cache_file.name}")
        except (ValueError, OSError) as e:
            logger.debug(f"Skipping cache file {cache_file.name}: {e}")
