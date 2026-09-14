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
from datetime import date, datetime, timedelta, timezone
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
    return validate_daily_cache()


def ensure_populated(fetch_fn) -> dict:
    """
    Ensure the daily cache is populated. Thread-safe using double-checked
    locking pattern — only the first concurrent caller triggers the fetch.

    IMPORTANT: an empty/failed fetch is NOT cached. If every sport returned
    no data (e.g. invalid API key -> 401, rate limit -> 429, or network
    outage), we return the empty result without persisting it so the very
    next request retries the fetch instead of being locked out for the
    whole day with "no predictions available".

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
        # Local import to avoid a circular module-level dependency
        # (odds_client imports daily_cache at module load time).
        from odds_client import OddsAPIError

        try:
            sports_data = fetch_fn() or {}
            total_events = sum(
                len(events) if isinstance(events, list) else 0
                for events in sports_data.values()
            )
            if not sports_data or total_events == 0:
                # Do NOT cache an empty result — the next request will retry.
                logger.warning(
                    "Daily odds fetch returned NO data (%d sports, %d events); "
                    "NOT caching empty result — next request will retry. "
                    "Check ODDS_API_KEY validity (401) or rate limits (429).",
                    len(sports_data), total_events,
                )
                return sports_data

            cache_path = _get_cache_path()
            set_cached_odds(sports_data)
            logger.info(
                "Daily cache populated: %d raw matches fetched across %d sports "
                "-> cache file: %s",
                total_events, len(sports_data), cache_path,
            )
            return sports_data
        except (ConnectionError, TimeoutError, OSError, OddsAPIError) as e:
            # OddsAPIError covers the total-provider-outage case (every
            # provider failed for every sport). Handle it like the other
            # failure types: log, return whatever we have (possibly empty) and
            # let the next user's request retry — never crash the request.
            logger.error(f"Failed to populate daily cache: {e}")
            # Return whatever we have (might be empty dict)
            return get_cached_odds()


def clear_today_cache() -> bool:
    """
    Delete today's cache file (if present) so the next request triggers a
    fresh fetch. Used by the admin /force_refresh_cache command.
    Returns True if a file was removed.
    """
    cache_path = _get_cache_path()
    removed = False
    if cache_path.exists():
        try:
            cache_path.unlink()
            removed = True
            logger.info("Cleared today's cache file: %s", cache_path)
        except OSError as e:
            logger.error("Failed to clear cache file %s: %s", cache_path, e)
    # Also clear quarantined/corrupt variants for today so they cannot linger
    for stale in CACHE_DIR.glob(f"odds_{get_lagos_date_str()}*.json"):
        try:
            stale.unlink()
            logger.info("Cleared stale cache artifact: %s", stale.name)
        except OSError:
            pass
    return removed


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
