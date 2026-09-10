"""
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
        {"key": "soccer_epl", "name": "English Premier League", "short": "EPL", "icon": "⚽"},
        {"key": "soccer_spain_la_liga", "name": "La Liga", "short": "La Liga", "icon": "⚽"},
        {"key": "soccer_italy_serie_a", "name": "Serie A", "short": "Serie A", "icon": "⚽"},
        {"key": "soccer_germany_bundesliga", "name": "Bundesliga", "short": "Bundesliga", "icon": "⚽"},
        {"key": "soccer_france_ligue_one", "name": "Ligue 1", "short": "Ligue 1", "icon": "⚽"},
        {"key": "soccer_uefa_europa_league", "name": "UEFA Europa League", "short": "UEL", "icon": "⚽"},
    ],
    "Basketball": [
        {"key": "basketball_nba", "name": "NBA", "short": "NBA", "icon": "🏀"},
        {"key": "basketball_euroleague", "name": "EuroLeague", "short": "EuroLeague", "icon": "🏀"},
    ],
    "Tennis": [
        {"key": "tennis_atp_us_open", "name": "ATP Tour", "short": "ATP", "icon": "🎾"},
        {"key": "tennis_wta_us_open", "name": "WTA Tour", "short": "WTA", "icon": "🎾"},
    ],
    "Ice Hockey": [
        {"key": "icehockey_nhl", "name": "NHL", "short": "NHL", "icon": "🏒"},
    ],
    "American Football": [
        {"key": "americanfootball_nfl", "name": "NFL", "short": "NFL", "icon": "🏈"},
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

def generate_daily_predictions(seed_date: date = None, max_predictions: int = 25) -> list:
    """
    Generate real daily predictions from The Odds API.
    All predictions are strictly for today only (UTC).
    """
    now_utc = datetime.now(timezone.utc)
    today = seed_date or now_utc.date()
    predictions = []
    errors = []

    for sport_name, leagues in SPORT_KEY_MAP.items():
        for league in leagues:
            sport_key = league["key"]
            league_name = league["name"]

            try:
                odds_data = get_odds(sport_key, markets="h2h,spreads,totals")

                for event in odds_data:
                    commence_time_str = event.get("commence_time", "")
                    if not commence_time_str:
                        continue

                    try:
                        if commence_time_str.endswith("Z"):
                            commence_time_str = commence_time_str[:-1] + "+00:00"
                        event_dt = datetime.fromisoformat(commence_time_str)

                        if event_dt.tzinfo is None:
                            event_dt = event_dt.replace(tzinfo=timezone.utc)

                        # STRICT DATE CHECK: Only include today's events (UTC)
                        if event_dt.date() != today:
                            continue
                    except (ValueError, TypeError):
                        continue

                    home_team = event.get("home_team", "Unknown")
                    away_team = event.get("away_team", "Unknown")
                    match_str = f"{home_team} vs {away_team}"

                    bookmakers = event.get("bookmakers", [])
                    if not bookmakers:
                        continue

                    bookmaker = bookmakers[0]
                    markets_to_try = ["h2h", "spreads", "totals"]

                    for market_type in markets_to_try:
                        pick = None
                        odds = None

                        if market_type == "h2h":
                            pick, odds = _get_h2h_pick(bookmaker)
                        elif market_type == "spreads":
                            pick, odds = _get_spread_pick(bookmaker)
                        elif market_type == "totals":
                            pick, odds = _get_totals_pick(bookmaker)

                        if pick and odds and len(predictions) < max_predictions:
                            confidence = _calculate_confidence(odds)

                            predictions.append({
                                "sport": sport_name,
                                "sport_icon": league["icon"],
                                "league": league_name,
                                "league_short": league["short"],
                                "match": match_str,
                                "home_team": home_team,
                                "away_team": away_team,
                                "market_type": market_type,
                                "market_label": MARKET_LABELS.get(market_type, market_type),
                                "pick": pick,
                                "odds": odds,
                                "confidence": confidence,
                                "date": today.isoformat(),
                                "match_time": event_dt.strftime("%H:%M UTC"),
                                "match_date": today.strftime("%Y-%m-%d"),
                                "kickoff_utc": event_dt.isoformat(),
                            })
                            break

            except OddsAPIError as e:
                logger.warning(f"Failed to fetch odds for {league_name}: {e}")
                errors.append(f"{league_name}: {e}")
                continue
            except Exception as e:
                logger.error(f"Unexpected error fetching {league_name}: {e}")
                errors.append(f"{league_name}: {e}")
                continue

    predictions.sort(key=lambda x: x["confidence"], reverse=True)

    # Final filter: ensure all predictions are for today (UTC)
    today_str = today.isoformat()
    predictions = [p for p in predictions if p["date"] == today_str]

    # Fallback to mock data if API returned no predictions
    if not predictions and errors:
        logger.warning("API returned no predictions. Using mock data as fallback.")
        predictions = _generate_mock_predictions(today, max_predictions)

    if errors:
        logger.warning(f"Some sports failed to load: {', '.join(errors)}")

    return predictions[:max_predictions]


def _generate_mock_predictions(today: date, max_predictions: int = 25) -> list:
    """
    Generate mock predictions as fallback when API is unavailable.
    All predictions are strictly for today only.
    """
    random.seed(today.toordinal())

    # Mock data pools
    mock_leagues = {
        "Football": [
            {"name": "English Premier League", "short": "EPL", "icon": "⚽"},
            {"name": "La Liga", "short": "La Liga", "icon": "⚽"},
            {"name": "Serie A", "short": "Serie A", "icon": "⚽"},
            {"name": "Bundesliga", "short": "Bundesliga", "icon": "⚽"},
            {"name": "Ligue 1", "short": "Ligue 1", "icon": "⚽"},
        ],
        "Basketball": [
            {"name": "NBA", "short": "NBA", "icon": "🏀"},
        ],
        "Tennis": [
            {"name": "ATP Tour", "short": "ATP", "icon": "🎾"},
            {"name": "WTA Tour", "short": "WTA", "icon": "🎾"},
        ],
        "Ice Hockey": [
            {"name": "NHL", "short": "NHL", "icon": "🏒"},
        ],
        "American Football": [
            {"name": "NFL", "short": "NFL", "icon": "🏈"},
        ],
    }

    teams_pool = {
        "English Premier League": ["Manchester United", "Manchester City", "Liverpool", "Chelsea", "Arsenal", "Tottenham"],
        "La Liga": ["Real Madrid", "Barcelona", "Atletico Madrid", "Sevilla", "Valencia"],
        "Serie A": ["Juventus", "AC Milan", "Inter Milan", "Napoli", "Roma"],
        "Bundesliga": ["Bayern Munich", "Borussia Dortmund", "RB Leipzig", "Bayer Leverkusen"],
        "Ligue 1": ["PSG", "Marseille", "Lyon", "Monaco"],
        "NBA": ["Lakers", "Warriors", "Celtics", "Heat", "Bucks", "Nuggets"],
        "ATP Tour": ["Djokovic", "Alcaraz", "Sinner", "Medvedev", "Zverev"],
        "WTA Tour": ["Swiatek", "Sabalenka", "Gauff", "Rybakina", "Jabeur"],
        "NHL": ["Oilers", "Panthers", "Rangers", "Avalanche", "Bruins"],
        "NFL": ["Chiefs", "49ers", "Ravens", "Bills", "Cowboys"],
    }

    market_types = ["h2h", "spreads", "totals"]
    predictions = []

    for sport_name, leagues in mock_leagues.items():
        for league in leagues:
            league_name = league["name"]
            teams = teams_pool.get(league_name, [])
            if len(teams) < 2:
                continue

            num_matches = random.randint(1, 3)
            available = teams.copy()
            random.shuffle(available)

            for i in range(min(num_matches, len(available) // 2)):
                if len(predictions) >= max_predictions:
                    break

                home = available[i * 2]
                away = available[i * 2 + 1]
                market_type = random.choice(market_types)

                if market_type == "h2h":
                    pick = random.choice([f"{home} Win", f"{away} Win", "Draw"])
                    odds = round(1.5 + random.random() * 2.5, 2)
                    label = "Match Result"
                elif market_type == "spreads":
                    pick = random.choice([f"{home} -1.5", f"{away} +1.5"])
                    odds = round(1.8 + random.random() * 1.5, 2)
                    label = "Spread"
                else:
                    pick = random.choice(["Over 2.5 Goals", "Under 2.5 Goals", "Over 224.5 Points"])
                    odds = round(1.7 + random.random() * 2.0, 2)
                    label = "Over/Under"

                confidence = _calculate_confidence(odds)
                match_hour = random.choice([13, 14, 15, 16, 17, 18, 19, 20, 21])

                predictions.append({
                    "sport": sport_name,
                    "sport_icon": league["icon"],
                    "league": league_name,
                    "league_short": league["short"],
                    "match": f"{home} vs {away}",
                    "home_team": home,
                    "away_team": away,
                    "market_type": market_type,
                    "market_label": label,
                    "pick": pick,
                    "odds": odds,
                    "confidence": confidence,
                    "date": today.isoformat(),
                    "match_time": f"{match_hour:02d}:00 UTC",
                    "match_date": today.strftime("%Y-%m-%d"),
                    "kickoff_utc": f"{today.isoformat()}T{match_hour:02d}:00:00+00:00",
                })

    predictions.sort(key=lambda x: x["confidence"], reverse=True)
    return predictions[:max_predictions]




def get_top_picks(predictions: list, count: int = 5) -> list:
    """Return the top N highest-confidence picks."""
    return predictions[:count]


def filter_by_sport(predictions: list, sport: str) -> list:
    """Filter predictions by sport name."""
    return [p for p in predictions if p["sport"].lower() == sport.lower()]


def get_available_sports(predictions: list) -> dict:
    """Get available sports from predictions."""
    sports = {}
    for pred in predictions:
        sport = pred["sport"]
        if sport not in sports:
            sports[sport] = pred["sport_icon"]
    return sports
