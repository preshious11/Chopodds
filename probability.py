"""
Turns raw bookmaker odds into honest probability estimates.

Key idea: raw decimal odds always imply MORE than 100% total probability,
because that gap (the "overround" or "vig") is the bookmaker's margin.
We strip that out before treating the number as a real probability.

Also provides derived market calculations for markets not directly offered
by the API but computable from available odds:
- Double Chance (1X, 12, X2) from h2h
- Both Teams to Score (BTTS) from totals market
"""

from collections import defaultdict


def decimal_to_implied(odds: float) -> float:
    """Raw implied probability from a single decimal price. e.g. 1.50 -> 0.667"""
    return 1.0 / odds


def devig_outcomes(raw_probs: dict[str, float]) -> dict[str, float]:
    """
    Given raw implied probabilities for all outcomes in one market from one bookmaker,
    normalize them so they sum to 1.0 (removes the vig).
    raw_probs: dict of {outcome_name: raw_prob}
    """
    total = sum(raw_probs.values())
    if total == 0:
        return raw_probs
    return {name: p / total for name, p in raw_probs.items()}


def consensus_probabilities(event: dict, market: str = "h2h") -> dict[str, dict]:
    """
    Given one event from the Odds API response, compute the de-vigged
    probability for each outcome, averaged across every bookmaker offering it.

    Returns: {
        outcome_name: {"probability": float, "num_bookmakers": int}
    }
    """
    outcome_probs: dict[str, list[float]] = defaultdict(list)

    # Pre-filter bookmakers that have the target market to avoid nested loops
    for bookmaker in event.get("bookmakers", []):
        for mkt in bookmaker.get("markets", []):
            if mkt.get("key") != market:
                continue
            # Convert odds to implied probabilities and devig
            raw = {o["name"]: decimal_to_implied(o["price"]) for o in mkt.get("outcomes", [])}
            fair = devig_outcomes(raw)
            for name, prob in fair.items():
                outcome_probs[name].append(prob)
            break  # Only one market per bookmaker per type

    # Compute average probability per outcome
    return {
        name: {"probability": sum(probs) / len(probs), "num_bookmakers": len(probs)}
        for name, probs in outcome_probs.items()
    }


def calculate_double_chance_probabilities(event: dict) -> dict[str, dict]:
    """
    Calculate Double Chance probabilities from h2h market.
    Double Chance combines two of the three outcomes, giving higher probability.

    Derived from h2h probabilities:
    - "1X" (Home or Draw) = P(Home) + P(Draw)
    - "12" (Away or Draw) = P(Away) + P(Draw)
    - "X2" (Home or Away) = P(Home) + P(Away)

    Returns: {outcome_name: {"probability": float, "num_bookmakers": int}}
    """
    h2h_probs = consensus_probabilities(event, market="h2h")

    home_prob = None
    draw_prob = None
    away_prob = None
    num_bookmakers = 0

    home_name = event.get("home_team", "")
    away_name = event.get("away_team", "")

    for name, data in h2h_probs.items():
        if name.lower() == "draw":
            draw_prob = data["probability"]
            num_bookmakers = data["num_bookmakers"]
        elif name == home_name:
            home_prob = data["probability"]
            if num_bookmakers == 0:
                num_bookmakers = data["num_bookmakers"]
        elif name == away_name:
            away_prob = data["probability"]
            if num_bookmakers == 0:
                num_bookmakers = data["num_bookmakers"]

    if home_prob is None or away_prob is None or draw_prob is None:
        return {}

    if num_bookmakers < 3:
        return {}

    return {
        "1X": {
            "probability": min(home_prob + draw_prob, 0.99),
            "num_bookmakers": num_bookmakers,
        },
        "12": {
            "probability": min(away_prob + draw_prob, 0.99),
            "num_bookmakers": num_bookmakers,
        },
        "X2": {
            "probability": min(home_prob + away_prob, 0.99),
            "num_bookmakers": num_bookmakers,
        },
    }


def calculate_btts_probabilities(event: dict) -> dict[str, dict]:
    """
    Calculate Both Teams to Score probabilities from the totals market.
    Uses Over/Under 2.5 goals as a proxy:
    - BTTS "Yes" correlates with Over 2.5 goals
    - BTTS "No" correlates with Under 2.5 goals

    Returns: {outcome_name: {"probability": float, "num_bookmakers": int}}
    """
    totals_probs = consensus_probabilities(event, market="totals")

    over_data = None
    under_data = None

    for name, data in totals_probs.items():
        if name.lower().startswith("over"):
            over_data = data
        elif name.lower().startswith("under"):
            under_data = data

    if over_data is None or under_data is None:
        return {}

    num_bookmakers = min(over_data["num_bookmakers"], under_data["num_bookmakers"])
    if num_bookmakers < 3:
        return {}

    over_prob = over_data["probability"]
    under_prob = under_data["probability"]

    # BTTS Yes correlates with Over 2.5 (both teams scoring = at least 2 goals)
    # BTTS No correlates with Under 2.5 (one/both teams failing to score)
    btts_yes_prob = min(over_prob * 0.95, 0.99)
    btts_no_prob = min(under_prob * 0.95, 0.99)

    return {
        "BTTS Yes": {
            "probability": btts_yes_prob,
            "num_bookmakers": num_bookmakers,
        },
        "BTTS No": {
            "probability": btts_no_prob,
            "num_bookmakers": num_bookmakers,
        },
    }
