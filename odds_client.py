"""
Thin client for The Odds API (https://the-odds-api.com).
Uses a shared daily cache (Africa/Lagos timezone) to minimize API calls.
All users share the same daily dataset — only the first request of the day
triggers API calls. Pagination, stats, and subsequent users read from cache.
"""

import requests
import config
import logging
from concurrent.futures import ThreadPoolExecutor, as_completed

from daily_cache import ensure_populated

BASE_URL = "https://api.the-odds-api.com/v4"

logger = logging.getLogger(__name__)

# Max concurrent API requests during cache population
_MAX_WORKERS = 4


class OddsAPIError(Exception):
    pass


def _api_key() -> str:
    """Return the configured API key without shell/ dotenv quoting noise."""
    return str(config.ODDS_API_KEY).strip().strip('"\'').strip()


def _fetch_single_sport(sport_key: str) -> tuple:
    """
    Fetch odds for a single sport key from the API.
    Returns (sport_key, events_list) or (sport_key, None) on failure.
    """
    try:
        resp = requests.get(
            f"{BASE_URL}/sports/{sport_key}/odds",
            params={
                "apiKey": _api_key(),
                "regions": config.ODDS_REGIONS,
                "markets": config.ODDS_MARKETS,
                "oddsFormat": "decimal",
            },
            timeout=15,
        )

        if resp.status_code == 200:
            events = resp.json()
            logger.info(f"Fetched odds for {sport_key}: {len(events)} events")
            return sport_key, events
        elif resp.status_code == 401:
            logger.warning(f"Auth failed for {sport_key}")
        elif resp.status_code == 429:
            logger.warning(f"Rate limited on {sport_key}")
        else:
            logger.warning(
                f"Failed to fetch odds for {sport_key}: {resp.status_code}"
            )
    except (requests.RequestException, ValueError) as e:
        logger.warning(f"Request failed for {sport_key}: {e}")

    return sport_key, None


def _fetch_all_sports_odds() -> dict:
    """
    Fetch odds for all configured sports from the API concurrently.
    Called only once per day (Lagos time) when the cache is first populated.
    Returns a dict mapping sport_key -> list of events.

    Uses ThreadPoolExecutor for concurrent requests — fetches up to 4 sports
    simultaneously, reducing total fetch time from ~13s to ~4s for 13 sports.
    """
    # Import here to avoid circular imports at module load time
    from predictions import SPORT_KEY_MAP

    # Collect all unique sport keys from the predictions config
    all_sport_keys = []
    for sport_name, leagues in SPORT_KEY_MAP.items():
        for league in leagues:
            key = league["key"]
            if key not in all_sport_keys:
                all_sport_keys.append(key)

    # Keep failed sports in the cache as empty lists so they are not retried
    # by later user requests on the same day.
    sports_data = {sport_key: [] for sport_key in all_sport_keys}

    # Fetch sports concurrently using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        future_to_key = {
            executor.submit(_fetch_single_sport, sport_key): sport_key
            for sport_key in all_sport_keys
        }

        for future in as_completed(future_to_key):
            sport_key = future_to_key[future]
            try:
                _, events = future.result()
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                logger.warning("Unexpected response for %s: %s", sport_key, exc)
                events = None
            if events is not None:
                sports_data[sport_key] = events

    return sports_data


def get_sports():
    """Return the list of all sport keys currently in season."""
    resp = requests.get(
        f"{BASE_URL}/sports",
        params={"apiKey": _api_key()},
        timeout=15,
    )
    if resp.status_code != 200:
        raise OddsAPIError(
            f"Failed to fetch sports list: {resp.status_code} {resp.text}"
        )
    return resp.json()


def get_odds(sport_key, markets="h2h"):
    """
    Fetch current odds for a sport.
    markets: 'h2h' (moneyline/win-draw-win), 'spreads', or 'totals'
    Returns a list of events, each with bookmaker odds.

    Uses the shared daily cache — only the first request of the day
    (Africa/Lagos time) triggers API calls. All subsequent calls
    (pagination, other users, stats) read from the cached dataset.
    """
    # Ensure the daily cache is populated (thread-safe, only fetches once per day)
    sports_data = ensure_populated(_fetch_all_sports_odds)

    # The dated cache is authoritative for the entire day. Missing keys mean
    # that sport failed or had no data during the daily fetch; do not retry it.
    return sports_data.get(sport_key, [])
