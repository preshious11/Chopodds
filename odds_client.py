"""
Thin client for The Odds API (https://the-odds-api.com).
Handles fetching live odds for a sport with caching to reduce API calls.
"""

import requests
import config
import logging
import json
from pathlib import Path
from datetime import datetime, timezone, timedelta

BASE_URL = "https://api.the-odds-api.com/v4"
CACHE_DIR = Path(__file__).resolve().parent / ".cache"
CACHE_TTL_MINUTES = 60  # Cache odds for 1 hour

logger = logging.getLogger(__name__)


class OddsAPIError(Exception):
    pass


def _get_cache_path(sport_key):
    """Get cache file path for a sport."""
    CACHE_DIR.mkdir(exist_ok=True)
    return CACHE_DIR / f"{sport_key}.json"


def _get_cached_odds(sport_key):
    """Get cached odds if they exist and are fresh."""
    cache_path = _get_cache_path(sport_key)
    if not cache_path.exists():
        return None
    
    try:
        with open(cache_path, 'r') as f:
            cached = json.load(f)
        
        cached_time = datetime.fromisoformat(cached.get('cached_at', ''))
        if datetime.now(timezone.utc) - cached_time < timedelta(minutes=CACHE_TTL_MINUTES):
            logger.info(f"Using cached odds for {sport_key}")
            return cached.get('data')
    except Exception:
        pass
    
    return None


def _set_cached_odds(sport_key, data):
    """Cache odds data."""
    cache_path = _get_cache_path(sport_key)
    try:
        with open(cache_path, 'w') as f:
            json.dump({
                'cached_at': datetime.now(timezone.utc).isoformat(),
                'data': data
            }, f)
    except Exception as e:
        logger.warning(f"Failed to cache odds for {sport_key}: {e}")


def get_sports():
    """Return the list of all sport keys currently in season."""
    resp = requests.get(
        f"{BASE_URL}/sports",
        params={"apiKey": config.ODDS_API_KEY},
        timeout=15,
    )
    if resp.status_code != 200:
        raise OddsAPIError(f"Failed to fetch sports list: {resp.status_code} {resp.text}")
    return resp.json()


def get_odds(sport_key, markets="h2h"):
    """
    Fetch current odds for a sport.
    markets: 'h2h' (moneyline/win-draw-win), 'spreads', or 'totals'
    Returns a list of events, each with bookmaker odds.
    Uses caching to reduce API calls.
    """
    # Check cache first
    cached = _get_cached_odds(sport_key)
    if cached is not None:
        return cached
    
    resp = requests.get(
        f"{BASE_URL}/sports/{sport_key}/odds",
        params={
            "apiKey": config.ODDS_API_KEY,
            "regions": config.ODDS_REGIONS,
            "markets": markets,
            "oddsFormat": "decimal",
        },
        timeout=15,
    )
    
    if resp.status_code == 401:
        raise OddsAPIError("Invalid Odds API key.")
    if resp.status_code == 429:
        raise OddsAPIError("Odds API rate limit / quota exceeded.")
    if resp.status_code != 200:
        # Check for quota exceeded (returns 401 with specific message)
        try:
            error_data = resp.json()
            if error_data.get('error_code') == 'OUT_OF_USAGE_CREDITS':
                raise OddsAPIError("Odds API quota exceeded. Free plan: 500 requests/month. Upgrade at https://the-odds-api.com")
        except (json.JSONDecodeError, ValueError):
            pass
        raise OddsAPIError(f"Odds API error: {resp.status_code} {resp.text}")
    
    data = resp.json()
    
    # Cache the results
    _set_cached_odds(sport_key, data)
    
    return data
