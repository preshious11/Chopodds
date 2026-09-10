"""
Turns raw bookmaker odds into honest probability estimates.

Key idea: raw decimal odds always imply MORE than 100% total probability,
because that gap (the "overround" or "vig") is the bookmaker's margin.
We strip that out before treating the number as a real probability.
"""

from collections import defaultdict


def decimal_to_implied(odds):
    """Raw implied probability from a single decimal price. e.g. 1.50 -> 0.667"""
    return 1.0 / odds


def devig_outcomes(raw_probs):
    """
    Given raw implied probabilities for all outcomes in one market from one bookmaker,
    normalize them so they sum to 1.0 (removes the vig).
    raw_probs: dict of {outcome_name: raw_prob}
    """
    total = sum(raw_probs.values())
    if total == 0:
        return raw_probs
    return {name: p / total for name, p in raw_probs.items()}


def consensus_probabilities(event, market="h2h"):
    """
    Given one event from the Odds API response, compute the de-vigged
    probability for each outcome, averaged across every bookmaker offering it.

    Returns: {
        outcome_name: {"probability": float, "num_bookmakers": int}
    }
    """
    outcome_probs = defaultdict(list)

    for bookmaker in event.get("bookmakers", []):
        for mkt in bookmaker.get("markets", []):
            if mkt.get("key") != market:
                continue
            raw = {o["name"]: decimal_to_implied(o["price"]) for o in mkt.get("outcomes", [])}
            fair = devig_outcomes(raw)
            for name, prob in fair.items():
                outcome_probs[name].append(prob)

    result = {}
    for name, probs in outcome_probs.items():
        result[name] = {
            "probability": sum(probs) / len(probs),
            "num_bookmakers": len(probs),
        }
    return result
