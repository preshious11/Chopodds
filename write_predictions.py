# write_predictions.py - Run this to generate predictions.py
import pathlib

content = '''"""
Real data generator for sports predictions.
Fetches live odds from The Odds API and converts them to prediction format.
All predictions are strictly for the current day only (UTC).
"""

import random
import logging
from datetime import datetime, date, timezone, timedelta

import config
from odds_client import get_odds, OddsAPIError

logger = logging.getLogger(__name__)

# Sport key mapping: our name -> Odds API sport keys
SPORT_KEY_MAP = {
    "Football": [
        {"key": "soccer_epl", "name": "English Premier League", "short": "EPL", "icon": "\\u26bd"},
        {"key": "soccer_spain", "name": "La Liga", "short": "La Liga", "icon": "\\u26bd"},
        {"key": "soccer_italy_serie_a", "name": "Serie A", "short": "Serie A", "icon": "\\u26bd"},
        {"key": "soccer_germany_bundesliga", "name": "Bundesliga", "short": "Bundesliga", "icon": "\\u26bd"},
        {"key": "soccer_france_ligue_one", "name": "Ligue 1", "short": "Ligue 1", "icon": "\\u26bd"},
        {"key": "soccer_champions_league", "name": "UEFA Champions League", "short": "UCL", "icon": "\\u26bd"},
        {"key": "soccer_uefa_europa_league", "name": "UEFA Europa League", "short": "UEL", "icon": "\\u26bd"},
    ],
    "Basketball": [
        {"key": "basketball_nba", "name": "NBA", "short": "NBA", "icon": "\\U0001f3c0"},
        {"key": "basketball_euroleague", "name": "EuroLeague", "short": "EuroLeague", "icon": "\\U0001f3c0"},
    ],
    "Tennis": [
        {"key": "tennis_atp_aus_open_singles", "name": "ATP Tour", "short": "ATP", "icon": "\\U0001f3be"},
        {"key": "tennis_wta_aus_open_singles", "name": "WTA Tour", "short": "WTA", "icon": "\\U0001f3be"},
    ],
    "Ice Hockey": [
        {"key": "icehockey_nhl", "name": "NHL", "short": "NHL", "icon": "\\U0001f392"},
    ],
    "Cricket": [
        {"key": "cricket_ipl", "name": "IPL", "short": "IPL", "icon": "\\U0001f3cf"},
        {"key": "cricket_big_bash", "name": "BBL", "short": "BBL", "icon": "\\U0001f3cf"},
    ],
    "American Football": [
        {"key": "americanfootball_nfl", "name": "NFL", "short": "NFL", "icon": "\\U0001f3c8"},
    ],
}

# Market type mapping
MARKET_LABELS = {
    "h2h": "Match Result",
    "spreads": "Spread",
    "totals": "Over/Under",
}


def _get_h2h_pick(bookmaker):
    """Get match result pick from h2h market."""
    for market in bookmaker.get("markets", []):
        if market.get("key") == "h2h":
            outcomes = market.get("outcomes", [])
            if outcomes:
                best = min(outcomes, key=lambda x: x.get("price", 999))
                name = best.get("name", "Unknown")
                price = best.get("price", 0)
                if price < 1.5:
                    pick = f"{name} (Strong Favorite)"
                elif price < 2.5:
                    pick = f"{name} (Value Pick)"
                else:
                    pick = f"{name} (Underdog)"
                return pick, price
    return None, None


def _get_spread_pick(bookmaker):
    """Get spread pick from spreads market."""
    for market in bookmaker.get("markets", []):
        if market.get("key") == "spreads":
            outcomes = market.get("outcomes", [])
            if outcomes:
                best = min(outcomes, key=lambda x: x.get("price", 999))
                name = best.get("name", "Unknown")
                point = best.get("point", 0)
                price = best.get("price", 0)
                return f"{name} {point}", price
    return None, None


def _get_totals_pick(bookmaker):
    """Get over/under pick from totals market."""
    for market in bookmaker.get("markets", []):
        if market.get("key") == "totals":
            outcomes = market.get("outcomes", [])
            if outcomes:
                best = min(outcomes, key=lambda x: x.get("price", 999))
                name = best.get("name", "Unknown")
                point = best.get("point", 0)
                price = best.get("price", 0)
                return f"{name} {point}", price
    return None, None


def _calculate_confidence(odds):
    """Calculate confidence based on odds value."""
    if odds < 1.3:
        return round(random.uniform(0.85, 0.95), 2)
    elif odds < 1.8:
        return round(random.uniform(0.70, 0.85), 2)
    elif odds < 2.5:
        return round(random.uniform(0.55, 0.75), 2)
    else:
        return round(random.uniform(0.45, 0.65), 2)
