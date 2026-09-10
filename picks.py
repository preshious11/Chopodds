"""
Generates and formats picks: scans events, keeps only outcomes that clear
the configured probability + bookmaker-agreement thresholds, formats for Telegram.
"""

from datetime import datetime, timezone
import config
from odds_client import get_odds, OddsAPIError
from probability import consensus_probabilities


def generate_picks_for_sport(sport_key):
    """Returns a list of qualifying picks for one sport."""
    picks = []
    try:
        events = get_odds(sport_key)
    except OddsAPIError as e:
        return [], str(e)

    for event in events:
        home = event.get("home_team", "?")
        away = event.get("away_team", "?")
        commence = event.get("commence_time", "")

        probs = consensus_probabilities(event)
        for outcome_name, data in probs.items():
            if data["num_bookmakers"] < config.MIN_BOOKMAKERS:
                continue
            if data["probability"] < config.MIN_PROBABILITY:
                continue
            picks.append({
                "sport": sport_key,
                "match": f"{home} vs {away}",
                "outcome": outcome_name,
                "probability": data["probability"],
                "num_bookmakers": data["num_bookmakers"],
                "commence_time": commence,
            })

    return picks, None


def generate_all_picks(sport_keys=None):
    """Scan multiple sports, return sorted picks (highest confidence first)."""
    sport_keys = sport_keys or config.DEFAULT_SPORTS
    all_picks = []
    errors = []

    for sk in sport_keys:
        picks, err = generate_picks_for_sport(sk)
        all_picks.extend(picks)
        if err:
            errors.append(f"{sk}: {err}")

    all_picks.sort(key=lambda p: p["probability"], reverse=True)
    return all_picks, errors


def format_pick(pick):
    pct = round(pick["probability"] * 100, 1)
    return (
        f"*{pick['match']}*\n"
        f"Pick: {pick['outcome']}\n"
        f"Market consensus: {pct}% "
        f"(from {pick['num_bookmakers']} bookmakers)\n"
    )


def format_picks_message(picks, sport_label=None):
    if not picks:
        scope = f" for {sport_label}" if sport_label else ""
        return f"No picks{scope} clear the {int(config.MIN_PROBABILITY * 100)}% consensus threshold right now."

    header = f"*Picks — {datetime.now(timezone.utc).strftime('%Y-%m-%d %H:%M UTC')}*\n"
    header += (
        f"_These are bookmaker-consensus implied probabilities, not guarantees. "
        f"Threshold: {int(config.MIN_PROBABILITY * 100)}%+, "
        f"min {config.MIN_BOOKMAKERS} bookmakers agreeing._\n\n"
    )
    body = "\n".join(format_pick(p) for p in picks[:15])
    return header + body
