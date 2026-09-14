"""
Turns raw bookmaker odds into honest probability estimates.

Key idea: raw decimal odds always imply MORE than 100% total probability,
because that gap (the "overround" or "vig") is the bookmaker's margin.
We strip that out before treating the number as a real probability.

Also provides derived market calculations for markets not offered on the
featured-odds feeds but computable from available odds:
- Double Chance (1X, X2, 12) from h2h
- Both Teams to Score (BTTS) from a Poisson goal model fitted to the
  totals and h2h markets
"""

import math
from collections import Counter, defaultdict

# Goals per team considered by the Poisson model; P(> 10 goals) is negligible.
_MAX_GOALS = 10


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


def _line_signature(market: dict) -> tuple:
    """Identify the line a bookmaker quotes, e.g. (("Over", "2.5"), ("Under", "2.5"))."""
    return tuple(sorted(
        (str(outcome.get("name")), str(outcome.get("point")))
        for outcome in market.get("outcomes", [])
    ))


def main_line_markets(event: dict, market: str) -> list[dict]:
    """
    Return each bookmaker's entry for `market`, keeping only the main line.

    Bookmakers often quote different lines for the same match (Over 2.5 vs
    Over 3.5, Arsenal -1 vs Arsenal -1.5). Probabilities and prices for
    different lines must never be mixed, so only bookmakers quoting the most
    common line are kept. Markets without points (h2h) share one signature,
    so nothing is dropped for them.
    """
    quoted = []
    for bookmaker in event.get("bookmakers", []):
        for mkt in bookmaker.get("markets", []):
            if mkt.get("key") == market:
                quoted.append(mkt)
                break  # Only one market per bookmaker per type
    if not quoted:
        return []
    main_line = Counter(_line_signature(mkt) for mkt in quoted).most_common(1)[0][0]
    return [mkt for mkt in quoted if _line_signature(mkt) == main_line]


def best_price_and_point(
    event: dict, market: str, outcome_name: str
) -> tuple[float | None, float | None]:
    """Best available decimal price and the line for an outcome on the main line."""
    best_price = None
    point = None
    for mkt in main_line_markets(event, market):
        for outcome in mkt.get("outcomes", []):
            if outcome.get("name") != outcome_name:
                continue
            price = outcome.get("price")
            if isinstance(price, (int, float)) and (best_price is None or price > best_price):
                best_price = price
            point = outcome.get("point")
    return best_price, point


def consensus_probabilities(event: dict, market: str = "h2h") -> dict[str, dict]:
    """
    Given one event from the odds feed, compute the de-vigged probability for
    each outcome, averaged across every bookmaker quoting the main line of
    that market.

    Returns: {
        outcome_name: {"probability": float, "num_bookmakers": int}
    }
    """
    outcome_probs: dict[str, list[float]] = defaultdict(list)

    for mkt in main_line_markets(event, market):
        outcomes = mkt.get("outcomes", [])
        raw = {}
        for outcome in outcomes:
            price = outcome.get("price")
            if outcome.get("name") is None or not isinstance(price, (int, float)) or price <= 1.0:
                break
            raw[outcome["name"]] = decimal_to_implied(price)
        if len(raw) != len(outcomes) or len(raw) < 2:
            continue  # An incomplete market cannot be de-vigged
        for name, prob in devig_outcomes(raw).items():
            outcome_probs[name].append(prob)

    # Compute average probability per outcome
    return {
        name: {"probability": sum(probs) / len(probs), "num_bookmakers": len(probs)}
        for name, probs in outcome_probs.items()
    }


def calculate_combined_odds(odds_values) -> float:
    """
    Calculate combined accumulator (parlay/multi-pick) odds.

    Total Odds = Odds_1 * Odds_2 * ... * Odds_N  (product of decimal odds).

    Non-numeric values and non-positive prices are skipped (a decimal price
    is always > 1.0). Returns 0.0 when no valid odds are supplied.
    """
    product = 1.0
    valid = 0
    for value in odds_values:
        try:
            value = float(value)
        except (TypeError, ValueError):
            continue
        if value <= 1.0:
            continue
        product *= value
        valid += 1
    return round(product, 2) if valid else 0.0


def _double_chance_components(event: dict) -> dict[str, tuple[str, str]]:
    """The two h2h outcomes each Double Chance selection covers (standard notation)."""
    home = event.get("home_team", "")
    away = event.get("away_team", "")
    return {"1X": (home, "Draw"), "X2": ("Draw", away), "12": (home, away)}


def calculate_double_chance_probabilities(event: dict) -> dict[str, dict]:
    """
    Calculate Double Chance probabilities from the h2h market.

    - "1X" (Home or Draw) = P(Home) + P(Draw)
    - "X2" (Away or Draw) = P(Away) + P(Draw)
    - "12" (Home or Away) = P(Home) + P(Away)

    Returns: {outcome_name: {"probability": float, "num_bookmakers": int}}
    """
    h2h = {
        name.lower(): data
        for name, data in consensus_probabilities(event, market="h2h").items()
    }
    result = {}
    for code, (first, second) in _double_chance_components(event).items():
        first_data = h2h.get(first.lower())
        second_data = h2h.get(second.lower())
        if first_data is None or second_data is None:
            return {}
        result[code] = {
            "probability": min(first_data["probability"] + second_data["probability"], 0.99),
            "num_bookmakers": min(first_data["num_bookmakers"], second_data["num_bookmakers"]),
        }
    return result


def dutch_odds(prices: list[float | None]) -> float | None:
    """
    Price for backing several mutually exclusive outcomes together.

    Splitting the stake in proportion to 1/price pays the same whichever
    outcome happens, so the combined price is 1 / sum(1/price).
    """
    if not prices or any(price is None or price <= 1.0 for price in prices):
        return None
    return 1.0 / sum(1.0 / price for price in prices)


def double_chance_odds(event: dict) -> dict[str, float]:
    """
    Achievable Double Chance prices built from the best available h2h prices.

    Unlike 1/probability, these include the bookmakers' margin, so they are
    what a bettor could actually get.
    """
    odds = {}
    for code, names in _double_chance_components(event).items():
        combined = dutch_odds(
            [best_price_and_point(event, "h2h", name)[0] for name in names]
        )
        if combined is not None:
            odds[code] = combined
    return odds


def _poisson_pmfs(rate: float, max_goals: int) -> list[float]:
    """P(goals = k) for k in 0..max_goals under a Poisson distribution."""
    pmfs = [math.exp(-rate)]
    for goals in range(1, max_goals + 1):
        pmfs.append(pmfs[-1] * rate / goals)
    return pmfs


def _fair_over_probability(rate: float, line: float) -> float:
    """P(total > line) with pushes (total == line) removed, as de-vigged odds imply."""
    over = under = 0.0
    for goals, prob in enumerate(_poisson_pmfs(rate, _MAX_GOALS * 2)):
        if goals > line:
            over += prob
        elif goals < line:
            under += prob
    return over / (over + under) if over + under else 0.0


def _home_win_share(home_rate: float, away_rate: float) -> float:
    """Home wins as a share of decisive (non-draw) results."""
    home_pmfs = _poisson_pmfs(home_rate, _MAX_GOALS)
    away_pmfs = _poisson_pmfs(away_rate, _MAX_GOALS)
    home_win = away_win = 0.0
    for home_goals, home_prob in enumerate(home_pmfs):
        for away_goals, away_prob in enumerate(away_pmfs):
            if home_goals > away_goals:
                home_win += home_prob * away_prob
            elif away_goals > home_goals:
                away_win += home_prob * away_prob
    return home_win / (home_win + away_win) if home_win + away_win else 0.5


def _solve_increasing(fn, target: float, low: float, high: float) -> float:
    """Find x in [low, high] with fn(x) == target, for an increasing fn (bisection)."""
    for _ in range(40):
        mid = (low + high) / 2
        if fn(mid) < target:
            low = mid
        else:
            high = mid
    return (low + high) / 2


def calculate_btts_probabilities(event: dict) -> dict[str, dict]:
    """
    Estimate Both Teams to Score probabilities with an independent Poisson
    goal model (a standard football approximation):

    1. Fit expected total goals to the consensus Over/Under main line.
    2. Split them between the teams so the model's home-vs-away win balance
       matches the consensus h2h odds (an even split if h2h is missing).
    3. P(BTTS Yes) = P(home scores >= 1) * P(away scores >= 1).

    BTTS prices are not available on the featured-odds feeds, so this is a
    model estimate rather than a market price.

    Returns: {outcome_name: {"probability": float, "num_bookmakers": int}}
    """
    totals = {
        name.lower(): data
        for name, data in consensus_probabilities(event, market="totals").items()
    }
    over = totals.get("over")
    under = totals.get("under")
    line = best_price_and_point(event, "totals", "Over")[1]
    if over is None or under is None or line is None:
        return {}
    try:
        line = float(line)
    except (TypeError, ValueError):
        return {}
    p_over = over["probability"]
    if not 0.0 < p_over < 1.0:
        return {}

    total_rate = _solve_increasing(
        lambda rate: _fair_over_probability(rate, line), p_over, 0.05, 10.0
    )

    share = 0.5
    h2h = {
        name.lower(): data
        for name, data in consensus_probabilities(event, market="h2h").items()
    }
    home = h2h.get(str(event.get("home_team", "")).lower())
    away = h2h.get(str(event.get("away_team", "")).lower())
    if home and away and home["probability"] + away["probability"] > 0:
        target = home["probability"] / (home["probability"] + away["probability"])
        share = _solve_increasing(
            lambda s: _home_win_share(total_rate * s, total_rate * (1 - s)),
            target, 0.02, 0.98,
        )

    home_rate = total_rate * share
    away_rate = total_rate - home_rate
    btts_yes = (1 - math.exp(-home_rate)) * (1 - math.exp(-away_rate))
    num_bookmakers = min(over["num_bookmakers"], under["num_bookmakers"])

    return {
        "BTTS Yes": {"probability": min(btts_yes, 0.99), "num_bookmakers": num_bookmakers},
        "BTTS No": {"probability": min(1 - btts_yes, 0.99), "num_bookmakers": num_bookmakers},
    }


def is_probability_in_bracket(probability: float, min_p: float, max_p: float) -> bool:
    """Return True when ``probability`` falls within the inclusive bracket."""
    return min_p <= probability <= max_p


def filter_by_probability_bracket(
    candidates: list[dict],
    min_p: float,
    max_p: float,
) -> list[dict]:
    """Keep only candidates whose confidence falls within ``[min_p, max_p]``."""
    return [c for c in candidates if min_p <= c.get("confidence", 0.0) <= max_p]
