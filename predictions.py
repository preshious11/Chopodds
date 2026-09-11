"""
Real data generator for sports predictions.
Fetches live odds from The Odds API and converts them to prediction format.
All predictions are strictly for the current day only (Africa/Lagos timezone).
"""

import logging
from datetime import date, datetime, timezone

from zoneinfo import ZoneInfo

import config
from odds_client import get_odds, OddsAPIError
from probability import (
    consensus_probabilities,
    calculate_double_chance_probabilities,
    calculate_btts_probabilities,
)

logger = logging.getLogger(__name__)

LAGOS_TZ = ZoneInfo("Africa/Lagos")

# Confidence tier thresholds
CONFIDENCE_HIGH = 0.70      # >= 70%: High Confidence
CONFIDENCE_MODERATE = 0.50  # 50-69%: Moderate Confidence
CONFIDENCE_VALUE = 0.40     # 40-49%: Value Pick (low-volume days only)


def _today_lagos() -> date:
    """Get today's date in Africa/Lagos timezone."""
    return datetime.now(LAGOS_TZ).date()

# Sport key mapping: our name -> Odds API sport keys
# RESTRICTED to Football (Soccer) and Tennis only — no other sports are scanned
# to conserve API credits. Add more soccer/tennis leagues as needed.
SPORT_KEY_MAP = {
    "Football": [
        {"key": "soccer_epl", "name": "English Premier League", "short": "EPL", "icon": "⚽"},
        {"key": "soccer_spain_la_liga", "name": "La Liga", "short": "La Liga", "icon": "⚽"},
        {"key": "soccer_italy_serie_a", "name": "Serie A", "short": "Serie A", "icon": "⚽"},
        {"key": "soccer_germany_bundesliga", "name": "Bundesliga", "short": "Bundesliga", "icon": "⚽"},
        {"key": "soccer_france_ligue_one", "name": "Ligue 1", "short": "Ligue 1", "icon": "⚽"},
        {"key": "soccer_uefa_europa_league", "name": "UEFA Europa League", "short": "UEL", "icon": "⚽"},
        {"key": "soccer_uefa_champs_league", "name": "UEFA Champions League", "short": "UCL", "icon": "⚽"},
        {"key": "soccer_netherlands_eredivisie", "name": "Eredivisie", "short": "Eredivisie", "icon": "⚽"},
        {"key": "soccer_belgium_first_div", "name": "Belgian Pro League", "short": "Belgium", "icon": "⚽"},
        {"key": "soccer_portugal_primeira_liga", "name": "Primeira Liga", "short": "Portugal", "icon": "⚽"},
        {"key": "soccer_turkey_super_league", "name": "Super Lig", "short": "Turkey", "icon": "⚽"},
        {"key": "soccer_usa_mls", "name": "MLS", "short": "MLS", "icon": "⚽"},
    ],
    "Tennis": [
        {"key": "tennis_atp_us_open", "name": "ATP Tour", "short": "ATP", "icon": "🎾"},
        {"key": "tennis_wta_us_open", "name": "WTA Tour", "short": "WTA", "icon": "🎾"},
    ],
}

# Market type mapping
MARKET_LABELS = {
    # Base markets (from API)
    "h2h": "Match Result",
    "spreads": "Spread",
    "totals": "Over/Under",
    "alternate_spreads": "Alternate Spread",
    "alternate_totals": "Alternate Over/Under",
    "draw_no_bet": "Draw No Bet",
    "tennis_set_betting": "Set Betting",
    "tennis_games": "Total Games",
    # Derived markets (calculated from base markets in probability.py)
    "double_chance": "Double Chance",
    "btts": "Both Teams to Score",
}

# Unit mapping for totals/over-under markets: (sport_name, market_type) -> unit
# Used to display the correct unit based on sport/market instead of hardcoding "Goals".
# Only Football and Tennis are active (restricted in SPORT_KEY_MAP).
MARKET_UNITS = {
    ("Football", "totals"): "Goals",
    ("Football", "alternate_totals"): "Goals",
    ("Football", "corners"): "Corners",
    ("Football", "cards"): "Cards",
    ("American Football", "totals"): "Points",
    ("Basketball", "totals"): "Points",
    ("Ice Hockey", "totals"): "Goals",
    ("Tennis", "totals"): "Games",
    ("Tennis", "alternate_totals"): "Games",
    ("Tennis", "tennis_games"): "Games",
}


def _get_market_unit(sport_name: str, market_type: str) -> str:
    """Get the correct unit for a sport/market combination, or None if no unit applies."""
    return MARKET_UNITS.get((sport_name, market_type))


def get_confidence_tier(probability: float) -> str:
    """
    Tag a prediction with a confidence tier based on its probability.

    Tiers:
    - "High Confidence": probability >= 70%
    - "Moderate Confidence": probability 50-69%
    - "Value Pick": probability 40-49% (only used on low-volume days)
    """
    if probability >= CONFIDENCE_HIGH:
        return "High Confidence"
    elif probability >= CONFIDENCE_MODERATE:
        return "Moderate Confidence"
    elif probability >= CONFIDENCE_VALUE:
        return "Value Pick"
    else:
        return "Below Threshold"


def _format_pick_description(market_type, outcome_name, point, home_team, away_team, probability, sport_name=None):
    """
    Format a clear, actionable pick description using the exact line from the Odds API.

    Returns a human-readable string like:
    - "Chelsea to Win" (h2h)
    - "Over 2.5 Goals" (football totals)
    - "Over 214.5 Points" (basketball totals)
    - "Chelsea -1.5" (spreads)
    - "Over 8.5 Corners" (corners totals)

    The unit (Goals/Points/Games/Corners/Cards) is determined by sport and market,
    never hardcoded to a single value.
    """
    # For h2h (match result) - identify the team/player
    if market_type == "h2h":
        if outcome_name.lower() == "draw":
            return "Draw"
        # Add "to Win" for clarity
        suffix = ""
        if probability >= 0.95:
            suffix = " (Strong Favorite)"
        elif probability >= 0.90:
            suffix = " (Value Pick)"
        return f"{outcome_name} to Win{suffix}"

    # For Draw No Bet (derived from h2h - draw outcome removed)
    if market_type == "draw_no_bet":
        if outcome_name.lower() == "draw":
            return None  # Draw No Bet has no draw outcome
        return f"{outcome_name} to Win (Draw No Bet)"

    # For Double Chance (derived from h2h)
    if market_type == "double_chance":
        labels = {
            "1X": f"{home_team} or Draw",
            "12": f"{away_team} or Draw",
            "X2": f"{home_team} or {away_team}",
        }
        return labels.get(outcome_name, outcome_name)

    # For BTTS (derived from totals)
    if market_type == "btts":
        return outcome_name  # "BTTS Yes" or "BTTS No"

    # For totals (Over/Under) - always include the line and correct unit
    if market_type in ("totals", "alternate_totals"):
        if point is not None:
            unit = _get_market_unit(sport_name, market_type)
            if unit:
                return f"{outcome_name} {point} {unit}"
            # Fallback: include point even if unit unknown
            return f"{outcome_name} {point}"
        # Fallback - shouldn't happen with valid API data
        return None

    # For spreads - always include the line
    if market_type in ("spreads", "alternate_spreads"):
        if point is not None:
            return f"{outcome_name} {point:+g}"
        # Fallback - shouldn't happen with valid API data
        return None

    # For any other market type, include point if available
    if point is not None:
        return f"{outcome_name} {point}"

    return outcome_name


def _find_outcome_point(bookmakers, market_type, outcome_name):
    """
    Find the point/line value for a specific outcome from the API data.
    Returns the point value or None if not found.
    """
    for bookmaker in bookmakers:
        for mkt in bookmaker.get("markets", []):
            if mkt.get("key") != market_type:
                continue
            for outcome in mkt.get("outcomes", []):
                if outcome.get("name") == outcome_name:
                    return outcome.get("point")
    return None

def _match_key(event: dict) -> str:
    """
    Unique identifier for a match, used to enforce one prediction per match.
    Prefers the Odds API's unique event id; falls back to teams + kickoff.
    """
    event_id = event.get("id")
    if event_id:
        return str(event_id)
    home = event.get("home_team", "Unknown")
    away = event.get("away_team", "Unknown")
    commence = event.get("commence_time", "")
    return f"{home}|{away}|{commence}"


def dedupe_by_match(predictions: list) -> list:
    """
    Keep only the strongest prediction per match.
    Groups by the API event id stored in 'event_id' (falls back to the
    'match' string) and retains the candidate with the highest confidence,
    then bookmaker agreement, then odds. First-seen order is preserved.
    """
    best_by_match = {}
    order = []
    for pred in predictions:
        key = pred.get("event_id") or pred.get("match")
        if key not in best_by_match:
            best_by_match[key] = pred
            order.append(key)
            continue
        challenger = (
            pred.get("confidence", 0),
            pred.get("num_bookmakers", 0),
            pred.get("odds", 0),
        )
        holder = best_by_match[key]
        holder_rank = (
            holder.get("confidence", 0),
            holder.get("num_bookmakers", 0),
            holder.get("odds", 0),
        )
        if challenger > holder_rank:
            best_by_match[key] = pred
    return [best_by_match[key] for key in order]


def generate_daily_predictions(seed_date: date = None, max_predictions: int = None) -> list:
    """
    Generate real daily predictions from The Odds API.

    Logic:
    1. For each event, evaluate ALL available markets (direct + derived):
       - Direct: h2h, spreads, totals (from the API)
       - Derived: double_chance (from h2h), btts (from totals)
    2. Select the single highest-probability prediction per match.
    3. Apply quality threshold (default 50%).
    4. If fewer than MIN_MATCHES_FOR_FULL_QUALITY meet the threshold,
       apply fallback: include top-ranked matches down to FALLBACK_PROBABILITY_FLOOR (40%).
    5. Tag each prediction with a confidence tier.

    One prediction per match is strictly enforced.
    """
    if max_predictions is None:
        max_predictions = config.MAX_DAILY_PREDICTIONS

    today = seed_date or _today_lagos()
    all_candidates = []
    errors = []
    seen_match_keys = set()
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

                        event_dt_lagos = event_dt.astimezone(LAGOS_TZ)
                        if event_dt_lagos.date() != today:
                            continue
                    except (ValueError, TypeError):
                        continue

                    # Group by match — each match processed once
                    match_key = _match_key(event)
                    if match_key in seen_match_keys:
                        continue

                    home_team = event.get("home_team", "Unknown")
                    away_team = event.get("away_team", "Unknown")
                    match_str = f"{home_team} vs {away_team}"

                    bookmakers = event.get("bookmakers", [])
                    if not bookmakers:
                        continue

                    # Evaluate all markets for this match and find the best
                    best_candidate = _evaluate_match_markets(
                        event, bookmakers, sport_name, league,
                        home_team, away_team, match_str, match_key,
                        event_dt_lagos, today,
                    )

                    if best_candidate is not None:
                        seen_match_keys.add(match_key)
                        all_candidates.append(best_candidate)

            except OddsAPIError as e:
                errors.append(f"{league_name}: {e}")
                logger.warning(f"Failed to fetch odds for {league_name}: {e}")
            except (
                AttributeError,
                IndexError,
                KeyError,
                OSError,
                TypeError,
                ValueError,
                ZeroDivisionError,
            ) as e:
                errors.append(f"{league_name}: {e}")
                logger.error(f"Invalid odds data for {league_name}: {e}")

    predictions = _apply_threshold_with_fallback(
        all_candidates,
        min_threshold=config.MIN_PROBABILITY,
        fallback_floor=config.FALLBACK_PROBABILITY_FLOOR,
        min_matches=config.MIN_MATCHES_FOR_FULL_QUALITY,
    )

    for pred in predictions:
        pred["confidence_tier"] = get_confidence_tier(pred["confidence"])

    predictions = dedupe_by_match(predictions)
    predictions.sort(key=lambda x: x["confidence"], reverse=True)

    if errors:
        logger.info(f"Prediction generation completed with {len(errors)} errors")

    return predictions[:max_predictions]


def _evaluate_match_markets(event, bookmakers, sport_name, league,
                            home_team, away_team, match_str, match_key,
                            event_dt_lagos, today):
    """Evaluate all markets for a single match, return best candidate."""
    best_candidate = None
    best_rank = None
    market_keys = set()
    for bookmaker in bookmakers:
        for mkt in bookmaker.get("markets", []):
            key = mkt.get("key", "")
            if key and not key.endswith("_lay"):
                market_keys.add(key)
    for mkt_type in market_keys:
        probs = consensus_probabilities(event, market=mkt_type)
        candidate = _evaluate_market_outcomes(
            probs, bookmakers, mkt_type, sport_name, league,
            home_team, away_team, match_str, match_key, event_dt_lagos, today)
        if candidate is not None:
            rank = (candidate["confidence"], candidate["num_bookmakers"], candidate["odds"])
            if best_rank is None or rank > best_rank:
                best_rank = rank
                best_candidate = candidate
    if sport_name == "Football":
        dc_probs = calculate_double_chance_probabilities(event)
        if dc_probs:
            candidate = _evaluate_derived_outcomes(
                dc_probs, "double_chance", sport_name, league,
                home_team, away_team, match_str, match_key, event_dt_lagos, today)
            if candidate is not None:
                rank = (candidate["confidence"], candidate["num_bookmakers"], candidate["odds"])
                if best_rank is None or rank > best_rank:
                    best_rank = rank
                    best_candidate = candidate
        btts_probs = calculate_btts_probabilities(event)
        if btts_probs:
            candidate = _evaluate_derived_outcomes(
                btts_probs, "btts", sport_name, league,
                home_team, away_team, match_str, match_key, event_dt_lagos, today)
            if candidate is not None:
                rank = (candidate["confidence"], candidate["num_bookmakers"], candidate["odds"])
                if best_rank is None or rank > best_rank:
                    best_rank = rank
                    best_candidate = candidate
    return best_candidate


def _evaluate_market_outcomes(probs, bookmakers, market_type, sport_name, league,
                              home_team, away_team, match_str, match_key,
                              event_dt_lagos, today):
    """Evaluate all outcomes in a single market, return best candidate."""
    best_candidate = None
    best_rank = None
    for outcome_name, data in probs.items():
        if data["num_bookmakers"] < config.MIN_BOOKMAKERS:
            continue
        probability = data["probability"]
        if probability < config.FALLBACK_PROBABILITY_FLOOR:
            continue
        probability = min(probability, 0.99)
        best_odds = None
        for bookmaker in bookmakers:
            for mkt in bookmaker.get("markets", []):
                if mkt.get("key") != market_type:
                    continue
                for outcome in mkt.get("outcomes", []):
                    if outcome.get("name") == outcome_name:
                        price = outcome.get("price", 0)
                        if best_odds is None or price > best_odds:
                            best_odds = price
        if best_odds is None or best_odds < 1.01:
            continue
        point = _find_outcome_point(bookmakers, market_type, outcome_name)
        pick = _format_pick_description(
            market_type, outcome_name, point,
            home_team, away_team, probability, sport_name=sport_name)
        if pick is None:
            continue
        rank = (probability, data["num_bookmakers"], best_odds)
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_candidate = {
                "event_id": match_key, "sport": sport_name,
                "sport_icon": league["icon"], "league": league["name"],
                "league_short": league["short"], "match": match_str,
                "home_team": home_team, "away_team": away_team,
                "market_type": market_type,
                "market_label": MARKET_LABELS.get(market_type, market_type),
                "pick": pick, "odds": round(best_odds, 2),
                "confidence": round(probability, 2),
                "num_bookmakers": data["num_bookmakers"],
                "date": today.isoformat(),
                "match_time": event_dt_lagos.strftime("%H:%M"),
                "match_date": today.strftime("%Y-%m-%d"), "kickoff_utc": None,
            }
    return best_candidate


def _evaluate_derived_outcomes(probs, market_type, sport_name, league,
                               home_team, away_team, match_str, match_key,
                               event_dt_lagos, today):
    """Evaluate outcomes for a derived market (double_chance or btts)."""
    best_candidate = None
    best_rank = None
    for outcome_name, data in probs.items():
        if data["num_bookmakers"] < config.MIN_BOOKMAKERS:
            continue
        probability = data["probability"]
        if probability < config.FALLBACK_PROBABILITY_FLOOR:
            continue
        probability = min(probability, 0.99)
        estimated_odds = round(1.0 / probability, 2) if probability > 0 else None
        if estimated_odds is None or estimated_odds < 1.01:
            continue
        pick = _format_pick_description(
            market_type, outcome_name, None,
            home_team, away_team, probability, sport_name=sport_name)
        if pick is None:
            continue
        rank = (probability, data["num_bookmakers"], estimated_odds)
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_candidate = {
                "event_id": match_key, "sport": sport_name,
                "sport_icon": league["icon"], "league": league["name"],
                "league_short": league["short"], "match": match_str,
                "home_team": home_team, "away_team": away_team,
                "market_type": market_type,
                "market_label": MARKET_LABELS.get(market_type, market_type),
                "pick": pick, "odds": estimated_odds,
                "confidence": round(probability, 2),
                "num_bookmakers": data["num_bookmakers"],
                "date": today.isoformat(),
                "match_time": event_dt_lagos.strftime("%H:%M"),
                "match_date": today.strftime("%Y-%m-%d"), "kickoff_utc": None,
            }
    return best_candidate


def _apply_threshold_with_fallback(candidates, min_threshold, fallback_floor, min_matches):
    """Apply quality threshold with fallback for low-volume days."""
    qualifying = [c for c in candidates if c["confidence"] >= min_threshold]
    if len(qualifying) >= min_matches:
        logger.info(f"Threshold filter: {len(qualifying)} matches meet {min_threshold:.0%}")
        return qualifying
    logger.info(f"Low-volume day: {len(qualifying)} matches (need {min_matches}). Fallback to {fallback_floor:.0%}")
    sorted_candidates = sorted(candidates, key=lambda x: x["confidence"], reverse=True)
    fallback = [c for c in sorted_candidates if c["confidence"] >= fallback_floor]
    logger.info(f"Fallback: returning {len(fallback)} matches")
    return fallback


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
