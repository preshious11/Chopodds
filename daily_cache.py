"""
Shared daily cache for odds data based on Africa/Lagos timezone.

Ensures only one API fetch per day across all users, even with concurrent
requests. The cache is file-persisted so it survives bot restarts.

At midnight Lagos time, the cache automatically expires because the date
changes and a new cache file is created. Old cache files are removed after
a retention period by cleanup_old_cache().
"""

import json
import logging
import re
import time
from datetime import date, datetime, timedelta, timezone
from pathlib import Path
from threading import Lock
from typing import Callable

from zoneinfo import ZoneInfo

import config

logger = logging.getLogger(__name__)

LAGOS_TZ = ZoneInfo("Africa/Lagos")
CACHE_DIR = config.DATA_DIR / ".daily_cache"
_LOCK = Lock()
_CACHE_FILE_DATE = re.compile(r"^odds_(\d{4}-\d{2}-\d{2})\.json")

# In-memory copy of today's odds, keyed by Lagos date string, so the one
# read per league during generation does not re-parse the JSON file.
_memory_cache: tuple[str, dict] | None = None

# (monotonic time, exception) of the last failed fetch. Within
# config.ODDS_FETCH_RETRY_COOLDOWN seconds callers get that error again
# instead of triggering another full round of API requests.
_last_failure: tuple[float, Exception] | None = None


def get_lagos_date() -> date:
    """Get current date in Africa/Lagos timezone."""
    return datetime.now(LAGOS_TZ).date()


def get_lagos_date_str() -> str:
    """Get current date string in Africa/Lagos timezone."""
    return get_lagos_date().isoformat()


def _get_cache_path(date_str: str = None) -> Path:
    """Get cache file path for a specific date (default: today Lagos)."""
    CACHE_DIR.mkdir(parents=True, exist_ok=True)
    if date_str is None:
        date_str = get_lagos_date_str()
    return CACHE_DIR / f"odds_{date_str}.json"


def get_cached_odds() -> dict:
    """
    Get today's cached odds data for all sports.
    Returns a dict mapping sport_key -> odds_data.
    """
    today = get_lagos_date_str()
    if _memory_cache is not None and _memory_cache[0] == today:
        return _memory_cache[1]

    cache_path = _get_cache_path(today)
    if not cache_path.exists():
        return {}

    try:
        with open(cache_path, "r", encoding="utf-8") as f:
            data = json.load(f)
        return data.get("sports", {}) if isinstance(data, dict) else {}
    except (json.JSONDecodeError, OSError) as e:
        logger.warning(f"Failed to read daily cache: {e}")
        return {}


def validate_daily_cache() -> bool:
    """Validate today's cache file and quarantine it if it is corrupted.

    Returns ``True`` only when the file contains today's date and a mapping of
    sport keys to cached odds. Invalid files are renamed so the next cache
    population can safely regenerate them.
    """
    cache_path = _get_cache_path()
    if not cache_path.exists():
        return False

    try:
        with open(cache_path, "r", encoding="utf-8") as file:
            data = json.load(file)
        if (
            not isinstance(data, dict)
            or data.get("date") != get_lagos_date_str()
            or not isinstance(data.get("sports"), dict)
        ):
            raise ValueError("cache metadata or sports data is invalid")
        return True
    except (json.JSONDecodeError, OSError, ValueError) as exc:
        logger.warning("Daily odds cache is corrupted; regenerating it: %s", exc)
        quarantine_path = cache_path.with_name(
            f"{cache_path.name}.corrupt-"
            f"{datetime.now(timezone.utc).strftime('%Y%m%dT%H%M%SZ')}"
        )
        try:
            cache_path.replace(quarantine_path)
            logger.warning("Quarantined corrupted cache as %s", quarantine_path.name)
        except OSError as quarantine_error:
            logger.error("Could not quarantine corrupted cache: %s", quarantine_error)
        return False


def set_cached_odds(sports_data: dict) -> None:
    """
    Store today's odds data for all sports (in memory and on disk).
    sports_data: dict mapping sport_key -> odds_data
    """
    global _memory_cache
    today = get_lagos_date_str()
    _memory_cache = (today, sports_data)
    cache_path = _get_cache_path(today)

    try:
        with open(cache_path, "w", encoding="utf-8") as f:
            json.dump(
                {
                    "date": today,
                    "cached_at": datetime.now(LAGOS_TZ).isoformat(),
                    "sports": sports_data,
                },
                f,
            )
        logger.info(
            f"Stored daily odds cache for {today} with {len(sports_data)} sports"
        )
    except OSError as e:
        logger.warning(f"Failed to write daily cache: {e}")


def is_cache_valid() -> bool:
    """Check if today's cache exists and is valid (Lagos date)."""
    return validate_daily_cache()


def _raise_if_cooling_down() -> None:
    """Re-raise the last fetch error while its retry cooldown is running."""
    if _last_failure is None:
        return
    failed_at, error = _last_failure
    remaining = config.ODDS_FETCH_RETRY_COOLDOWN - (time.monotonic() - failed_at)
    if remaining > 0:
        logger.info(
            "Odds fetch failed recently; not retrying for another %ds", remaining
        )
        raise error.with_traceback(None)


def ensure_populated(fetch_fn: Callable[[], dict]) -> dict:
    """
    Ensure the daily cache is populated. Thread-safe: only the first
    concurrent caller triggers the fetch.

    A fetch that raises (e.g. invalid API key, exhausted quota, outage) is
    NOT cached and the error propagates. For the next
    config.ODDS_FETCH_RETRY_COOLDOWN seconds the same error is raised again
    without calling the API, so an outage cannot burn credits on every
    user request; after that the next request retries.

    fetch_fn: callable that returns dict mapping sport_key -> odds_data
    Returns the cached sports data (dict of sport_key -> odds_data).
    """
    global _last_failure, _memory_cache
    today = get_lagos_date_str()
    if _memory_cache is not None and _memory_cache[0] == today:
        return _memory_cache[1]

    with _LOCK:
        # Another thread may have populated while we waited
        if _memory_cache is not None and _memory_cache[0] == today:
            return _memory_cache[1]
        if is_cache_valid():
            sports_data = get_cached_odds()
            _memory_cache = (today, sports_data)
            return sports_data

        _raise_if_cooling_down()

        # We are the first caller — fetch and populate
        logger.info("Populating daily odds cache (first request of the day)...")
        try:
            sports_data = fetch_fn() or {}
        except Exception as exc:  # noqa: BLE001 — recorded for the cooldown, then re-raised
            _last_failure = (time.monotonic(), exc)
            logger.error(
                "Daily odds fetch failed; not caching. Retrying no sooner than "
                "%ds from now: %s", config.ODDS_FETCH_RETRY_COOLDOWN, exc,
            )
            raise

        _last_failure = None
        set_cached_odds(sports_data)
        total_events = sum(
            len(events) if isinstance(events, list) else 0
            for events in sports_data.values()
        )
        logger.info(
            "Daily cache populated: %d raw matches fetched across %d sports "
            "-> cache file: %s",
            total_events, len(sports_data), _get_cache_path(today),
        )
        return sports_data


def clear_today_cache() -> bool:
    """
    Delete today's cache (file and memory) and any fetch-failure cooldown so
    the next request triggers a fresh fetch. Used by the admin
    /force_refresh_cache command. Returns True if a file was removed.
    """
    global _last_failure, _memory_cache
    _memory_cache = None
    _last_failure = None
    today = get_lagos_date_str()
    cache_path = _get_cache_path(today)
    removed = False
    if cache_path.exists():
        try:
            cache_path.unlink()
            removed = True
            logger.info("Cleared today's cache file: %s", cache_path)
        except OSError as e:
            logger.error("Failed to clear cache file %s: %s", cache_path, e)
    # Also clear quarantined/corrupt variants for today so they cannot linger
    for stale in CACHE_DIR.glob(f"odds_{today}.json.corrupt-*"):
        try:
            stale.unlink()
            logger.info("Cleared stale cache artifact: %s", stale.name)
        except OSError:
            pass
    return removed


def cleanup_old_cache(keep_days: int = 30) -> None:
    """
    Remove cache files (including quarantined ones) older than keep_days.
    """
    if not CACHE_DIR.exists():
        return

    cutoff = get_lagos_date() - timedelta(days=keep_days)
    for cache_file in CACHE_DIR.glob("odds_*"):
        match = _CACHE_FILE_DATE.match(cache_file.name)
        if not match:
            continue
        try:
            if date.fromisoformat(match.group(1)) < cutoff:
                cache_file.unlink()
                logger.info(f"Removed old cache file: {cache_file.name}")
        except (ValueError, OSError) as e:
            logger.debug(f"Skipping cache file {cache_file.name}: {e}")
