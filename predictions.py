"""
Real data generator for sports predictions.
Fetches live odds (The Odds API, with SharpAPI / SportsGameOdds failover) and
converts them to prediction format.
All predictions are strictly for the current day only (Africa/Lagos timezone).
"""

import logging
from datetime import date, datetime, timezone

from zoneinfo import ZoneInfo

import config
from odds_client import OddsAPIError, get_cached_sport_keys, get_odds
from probability import (
    best_price_and_point,
    calculate_btts_probabilities,
    calculate_combined_odds,
    calculate_double_chance_probabilities,
    consensus_probabilities,
    double_chance_odds,
)

logger = logging.getLogger(__name__)

LAGOS_TZ = ZoneInfo("Africa/Lagos")

# Diagnostic stats from the last generate_daily_predictions() run.
# Exposed so the admin /status and /force_refresh_cache commands can report
# exactly where matches were lost (fetch -> today filter -> consensus -> threshold).
_last_generation_stats: dict = {}

# Confidence tier thresholds
CONFIDENCE_HIGH = 0.70      # >= 70%: High Confidence
CONFIDENCE_MODERATE = 0.50  # 50-69%: Moderate Confidence
CONFIDENCE_VALUE = 0.40     # 40-49%: Value Pick (low-volume days only)

# Stored confidence precision. Rounding to whole percent before comparing
# with the floors would let a 49.6% pick pass the 50% floor.
CONFIDENCE_DECIMALS = 4


def _today_lagos() -> date:
    """Get today's date in Africa/Lagos timezone."""
    return datetime.now(LAGOS_TZ).date()

# Sport key mapping: our name -> Odds API sport keys
# RESTRICTED to Football (Soccer) and Tennis only — no other sports are scanned
# to conserve API credits. Leagues that are off-season are skipped at fetch time.
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
    # Tennis tournament keys change through the season (tennis_atp_us_open,
    # tennis_atp_paris_masters, ...), so tennis is matched by key prefix
    # against the tournaments in today's odds data.
    "Tennis": [
        {"key_prefix": "tennis_atp_", "name": "ATP Tour", "short": "ATP", "icon": "🎾"},
        {"key_prefix": "tennis_wta_", "name": "WTA Tour", "short": "WTA", "icon": "🎾"},
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


# ---------------------------------------------------------------------------
# Market category mapping (for market-diversity selection)
# ---------------------------------------------------------------------------
# Maps every market_type this project can produce (direct from the odds feed or
# derived locally in probability.py) to a diversity category. Robust to naming
# variants (h2h/moneyline/match_winner/1x2 all map to "1x2").
_MARKET_CATEGORY_MAP = {
    # 1X2 / Match Winner
    "h2h": "1x2",
    "moneyline": "1x2",
    "match_winner": "1x2",
    "1x2": "1x2",
    # Over/Under Goals
    "totals": "over_under",
    "alternate_totals": "over_under",
    "tennis_games": "over_under",
    # BTTS (derived from totals + h2h)
    "btts": "btts",
    # Double Chance (derived from h2h)
    "double_chance": "double_chance",
    # Handicap / Asian Handicap
    "spreads": "handicap",
    "alternate_spreads": "handicap",
    "handicap": "handicap",
    "asian_handicap": "handicap",
    # Draw No Bet
    "draw_no_bet": "draw_no_bet",
}

MARKET_CATEGORY_LABELS = {
    "double_chance": "Double Chance",
    "over_under": "Over/Under",
    "btts": "BTTS",
    "1x2": "1X2",
    "handicap": "Handicap",
    "draw_no_bet": "Draw No Bet",
    "other": "Other",
}


def get_market_category(market_type: str) -> str:
    """Map a raw market type (Odds API key or derived market) to its diversity category."""
    return _MARKET_CATEGORY_MAP.get(str(market_type).lower(), "other")


def _market_floor(market_type: str) -> float:
    """
    Minimum probability a candidate must clear for its market category
    (config.MARKET_MIN_PROBABILITY_FLOORS). Unknown categories fall back to
    the global MIN_PROBABILITY, i.e. no extra floor beyond the existing rule.
    """
    return config.MARKET_MIN_PROBABILITY_FLOORS.get(
        get_market_category(market_type), config.MIN_PROBABILITY
    )


def _ranking_score(prediction: dict) -> float:
    """
    Internal ranking score = probability * market diversity multiplier.

    Used only to order/select predictions. The displayed probability
    ("confidence") is never modified.
    """
    multiplier = config.MARKET_DIVERSITY_MULTIPLIERS.get(
        get_market_category(prediction.get("market_type", "")), 1.0,
    )
    return prediction.get("confidence", 0.0) * multiplier


def _select_diverse_predictions(
    candidates: list[dict],
    max_total: int,
    min_probability: float | None = None,
) -> list[dict]:
    """
    Build the final daily list with market diversity, in rounds.

    Priorities (in order): probability threshold, one prediction per match,
    market diversity, ranking score (probability * market multiplier).

    Round 1: select the highest-ranked eligible prediction from each market
             category that has candidates.
    Rounds 2-3: fill remaining slots with the highest-ranked remaining
             predictions, always respecting per-market limits, the total cap,
             and one prediction per match.

    Candidates below min_probability are never selected. The caller passes the
    effective threshold so the existing low-volume fallback rule (down to
    FALLBACK_PROBABILITY_FLOOR) keeps working on quiet days.

    Never invents predictions: categories with few candidates simply
    contribute few picks.
    """
    if min_probability is None:
        min_probability = config.MIN_PROBABILITY
    limits = config.MARKET_SELECTION_LIMITS
    eligible = [
        p for p in candidates if p.get("confidence", 0.0) >= min_probability
    ]
    ranked = sorted(
        eligible,
        key=lambda p: (
            -_ranking_score(p),
            -p.get("confidence", 0.0),
            -p.get("num_bookmakers", 0),
        ),
    )
    selected: list[dict] = []
    selected_match_keys: set[str] = set()
    category_counts: dict[str, int] = {}

    def try_select(pred: dict, require_fresh_category: bool) -> None:
        category = get_market_category(pred.get("market_type", ""))
        match_key = pred.get("event_id") or pred.get("match")
        if match_key in selected_match_keys:
            return
        if require_fresh_category and category_counts.get(category, 0) > 0:
            return
        if category_counts.get(category, 0) >= limits.get(category, limits.get("other", 2)):
            return
        selected.append(pred)
        selected_match_keys.add(match_key)
        category_counts[category] = category_counts.get(category, 0) + 1

    # Round 1: one prediction per market category (best-ranked first).
    for pred in ranked:
        try_select(pred, require_fresh_category=True)
        if len(selected) >= max_total:
            return selected
    # Rounds 2-3: fill remaining slots purely by ranking score.
    for pred in ranked:
        if len(selected) >= max_total:
            break
        try_select(pred, require_fresh_category=False)
    return selected


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


def _format_pick_description(
    market_type: str,
    outcome_name: str,
    point: float | None,
    home_team: str,
    away_team: str,
    probability: float,
    sport_name: str | None = None,
) -> str | None:
    """
    Format a clear, actionable pick description using the exact line from the odds feed.

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
        suffix = " (Strong Favorite)" if probability >= 0.95 else ""
        return f"{outcome_name} to Win{suffix}"

    # For Draw No Bet (derived from h2h - draw outcome removed)
    if market_type == "draw_no_bet":
        if outcome_name.lower() == "draw":
            return None  # Draw No Bet has no draw outcome
        return f"{outcome_name} to Win (Draw No Bet)"

    # For Double Chance (derived from h2h), standard notation
    if market_type == "double_chance":
        labels = {
            "1X": f"{home_team} or Draw",
            "X2": f"{away_team} or Draw",
            "12": f"{home_team} or {away_team}",
        }
        return labels.get(outcome_name, outcome_name)

    # For BTTS (derived)
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


def _match_key(event: dict) -> str:
    """
    Unique identifier for a match, used to enforce one prediction per match.
    Prefers the feed's unique event id; falls back to teams + kickoff.
    """
    event_id = event.get("id")
    if event_id:
        return str(event_id)
    home = event.get("home_team", "Unknown")
    away = event.get("away_team", "Unknown")
    commence = event.get("commence_time", "")
    return f"{home}|{away}|{commence}"


def dedupe_by_match(predictions: list[dict]) -> list[dict]:
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


def _resolve_leagues() -> list[tuple[str, dict]]:
    """
    Expand SPORT_KEY_MAP into (sport_name, league) pairs with concrete keys.

    Prefix entries (tennis) are matched against today's fetched sport keys —
    tournament keys from The Odds API, or the generic ``tennis_atp`` /
    ``tennis_wta`` keys used when a fallback provider supplied tennis. This
    only touches the odds cache when such entries are configured.
    Raises OddsAPIError if today's odds could not be fetched.
    """
    resolved = []
    prefixed = []
    for sport_name, leagues in SPORT_KEY_MAP.items():
        for league in leagues:
            if league.get("key"):
                resolved.append((sport_name, league))
            elif league.get("key_prefix"):
                prefixed.append((sport_name, league))
    if prefixed:
        available = sorted(get_cached_sport_keys())
        for sport_name, league in prefixed:
            prefix = league["key_prefix"]
            for key in available:
                if key == prefix.rstrip("_") or key.startswith(prefix):
                    resolved.append((sport_name, {**league, "key": key}))
    return resolved


def _parse_kickoff(commence_time: str | None) -> datetime | None:
    """Parse a commence_time into an aware UTC datetime."""
    if not commence_time:
        return None
    try:
        if commence_time.endswith("Z"):
            commence_time = commence_time[:-1] + "+00:00"
        kickoff = datetime.fromisoformat(commence_time)
    except (AttributeError, TypeError, ValueError):
        return None
    if kickoff.tzinfo is None:
        kickoff = kickoff.replace(tzinfo=timezone.utc)
    return kickoff.astimezone(timezone.utc)


def generate_daily_predictions(
    seed_date: date | None = None,
    max_predictions: int | None = None,
) -> list[dict]:
    """
    Generate real daily predictions from today's odds.

    Logic:
    1. For each event, evaluate ALL available markets (direct + derived):
       - Direct: h2h, spreads, totals (from the feed)
       - Derived: double_chance (from h2h), btts (Poisson model)
    2. Apply quality threshold (default 50%); if fewer than
       MIN_MATCHES_FOR_FULL_QUALITY matches meet it, apply fallback:
       include top-ranked matches down to FALLBACK_PROBABILITY_FLOOR (40%).
    3. Market-diversity selection: rank candidates by
       probability * market-diversity multiplier, then build the final list
       in rounds so no single market (e.g. Double Chance) dominates,
       respecting per-market limits, the total cap (default 20), and
       one prediction per match.
    4. Tag each prediction with a confidence tier.

    One prediction per match is strictly enforced. Blocking (network on the
    first call of the day) — call it via asyncio.to_thread from async code.
    """
    if max_predictions is None:
        max_predictions = config.MAX_DAILY_PREDICTIONS

    today = seed_date or _today_lagos()
    all_candidates = []
    errors = []
    fetch_error = None
    seen_match_keys = set()
    raw_events_seen = 0
    events_today = 0
    events_no_bookmakers = 0
    per_sport_events: dict[str, int] = {}

    try:
        leagues_to_scan = _resolve_leagues()
    except OddsAPIError as e:
        fetch_error = str(e)
        leagues_to_scan = []

    for sport_name, league in leagues_to_scan:
        sport_key = league["key"]
        try:
            odds_data = get_odds(sport_key)
        except OddsAPIError as e:
            # Every league reads the same shared daily fetch, so a failure
            # here applies to all of them. Stop instead of re-triggering the
            # fetch once per league.
            fetch_error = str(e)
            break

        if league.get("key_prefix"):
            title = next(
                (event.get("sport_title") for event in odds_data if event.get("sport_title")),
                None,
            )
            if title:
                league = {**league, "name": title}

        try:
            for event in odds_data:
                raw_events_seen += 1
                kickoff = _parse_kickoff(event.get("commence_time"))
                # STRICT GUARD: exclude matches that have already kicked off
                # or finished. Only future matches today are eligible.
                if kickoff is None or kickoff <= datetime.now(timezone.utc):
                    continue
                if kickoff.astimezone(LAGOS_TZ).date() != today:
                    continue

                events_today += 1
                per_sport_events[sport_key] = per_sport_events.get(sport_key, 0) + 1

                # Group by match — each match processed once
                match_key = _match_key(event)
                if match_key in seen_match_keys:
                    continue

                if not event.get("bookmakers"):
                    events_no_bookmakers += 1
                    continue

                # Evaluate all markets for this match; the diverse
                # selection step later keeps at most one per match.
                base = _candidate_base(event, sport_name, league, match_key, kickoff, today)
                match_candidates = _evaluate_match_markets(event, sport_name, base)

                if match_candidates:
                    seen_match_keys.add(match_key)
                    all_candidates.extend(match_candidates)

        except (
            AttributeError,
            IndexError,
            KeyError,
            OSError,
            TypeError,
            ValueError,
            ZeroDivisionError,
        ) as e:
            errors.append(f"{league['name']}: {e}")
            logger.error(f"Invalid odds data for {league['name']}: {e}")

    if fetch_error:
        errors.append(f"Odds fetch failed: {fetch_error}")
        logger.warning("Odds fetch failed; no odds to predict from: %s", fetch_error)

    # Low-volume rule: the 40% fallback applies when fewer than
    # MIN_MATCHES_FOR_FULL_QUALITY distinct matches reach MIN_PROBABILITY
    # (several markets of the same match count once).
    qualifying_matches = {
        c["event_id"] for c in all_candidates if c["confidence"] >= config.MIN_PROBABILITY
    }
    low_volume_day = len(qualifying_matches) < config.MIN_MATCHES_FOR_FULL_QUALITY

    # Per-market probability floors (config.MARKET_MIN_PROBABILITY_FLOORS):
    # on the standard path a candidate must clear its own market category's
    # floor BEFORE it is eligible for the threshold/diversity selection below.
    # The low-volume fallback rule is deliberately untouched so quiet days
    # still surface picks.
    threshold_candidates = all_candidates
    if not low_volume_day:
        threshold_candidates = [
            c for c in all_candidates
            if c["confidence"] >= _market_floor(c["market_type"])
        ]

    predictions = _apply_threshold_with_fallback(
        threshold_candidates,
        min_threshold=config.MIN_PROBABILITY,
        fallback_floor=config.FALLBACK_PROBABILITY_FLOOR,
        min_matches=config.MIN_MATCHES_FOR_FULL_QUALITY,
    )

    # Market-diversity selection: rank by probability * market multiplier,
    # then build the final list in rounds (one pick per market category
    # first, then fill by ranking score) while respecting per-market limits,
    # the total cap, and one prediction per match. The selection floor mirrors
    # the threshold/fallback decision.
    selection_floor = (
        config.FALLBACK_PROBABILITY_FLOOR
        if low_volume_day
        else config.MIN_PROBABILITY
    )
    predictions = _select_diverse_predictions(
        predictions, max_predictions, min_probability=selection_floor,
    )

    for pred in predictions:
        pred["confidence_tier"] = get_confidence_tier(pred["confidence"])

    predictions = dedupe_by_match(predictions)
    predictions.sort(key=lambda x: x["confidence"], reverse=True)

    # Diagnostic summary — makes it possible to see exactly where matches
    # were lost when investigating "no predictions available" on Railway.
    before_threshold = len(all_candidates)
    _last_generation_stats.clear()
    _last_generation_stats.update({
        "date": today.isoformat(),
        "raw_events": raw_events_seen,
        "events_today": events_today,
        "events_no_bookmakers": events_no_bookmakers,
        "candidates": before_threshold,
        "returned": len(predictions),
        "errors": len(errors),
        "error_details": errors[:10],
        "per_sport_events": dict(sorted(per_sport_events.items())),
        "fallback_applied": low_volume_day,
    })
    if before_threshold == 0:
        if fetch_error:
            reason = f"Odds fetch failed: {fetch_error}"
        elif raw_events_seen == 0:
            reason = "No events returned for any configured league."
        elif events_today == 0:
            reason = (
                f"0 upcoming matches kick off today ({today}, Africa/Lagos) — "
                f"raw events: {raw_events_seen}. The rest are other days or started."
            )
        else:
            reason = (
                f"{events_today} matches today but 0 passed market/consensus "
                f"evaluation ({events_no_bookmakers} had no bookmakers; "
                f"others lacked >= {config.MIN_BOOKMAKERS} bookmakers or "
                f"odds >= {config.FALLBACK_PROBABILITY_FLOOR:.0%})."
            )
        _last_generation_stats["reason_zero"] = reason
        logger.warning(
            "Prediction generation produced 0 predictions. Reason: %s", reason,
        )
    logger.info(
        "Prediction summary for %s: %d raw events fetched, %d events "
        "kick off today (Lagos), %d candidates passed market/consensus "
        "evaluation, %d passed the threshold/fallback filter "
        "(capped at %d). %d errors.",
        today, raw_events_seen, events_today, len(all_candidates),
        len(predictions), max_predictions, len(errors),
    )

    return predictions[:max_predictions]


def get_last_generation_stats() -> dict:
    """Return diagnostic stats from the last generate_daily_predictions run."""
    return dict(_last_generation_stats)


def _candidate_base(
    event: dict,
    sport_name: str,
    league: dict,
    match_key: str,
    kickoff: datetime,
    today: date,
) -> dict:
    """Match-level fields shared by every candidate for one event."""
    home_team = event.get("home_team", "Unknown")
    away_team = event.get("away_team", "Unknown")
    return {
        "event_id": match_key,
        "source": event.get("source", "the-odds-api"),
        "sport": sport_name,
        "sport_key": league["key"],
        "sport_icon": league["icon"],
        "league": league["name"],
        "league_short": league["short"],
        "match": f"{home_team} vs {away_team}",
        "home_team": home_team,
        "away_team": away_team,
        "date": today.isoformat(),
        "match_time": kickoff.astimezone(LAGOS_TZ).strftime("%H:%M"),
        "match_date": today.isoformat(),
        "kickoff_utc": kickoff.isoformat(),
        "commence_time": kickoff.isoformat(),
    }


def _evaluate_match_markets(event: dict, sport_name: str, base: dict) -> list[dict]:
    """
    Evaluate all markets for a single match.

    Returns one best candidate per market type (direct markets from the feed
    plus derived double_chance/btts). The final per-match selection is made
    later by _select_diverse_predictions, which enforces one prediction per
    match and market diversity.
    """
    candidates: dict[str, dict] = {}
    market_keys = set()
    for bookmaker in event.get("bookmakers", []):
        for mkt in bookmaker.get("markets", []):
            key = mkt.get("key", "")
            if key and not key.endswith("_lay"):
                market_keys.add(key)

    for mkt_type in sorted(market_keys):
        probs = consensus_probabilities(event, market=mkt_type)
        prices = {
            name: best_price_and_point(event, mkt_type, name) for name in probs
        }
        candidate = _best_candidate(probs, prices, mkt_type, sport_name, base)
        if candidate is not None:
            candidates[mkt_type] = candidate

    if sport_name == "Football":
        dc_probs = calculate_double_chance_probabilities(event)
        if dc_probs:
            dc_odds = double_chance_odds(event)
            prices = {name: (dc_odds.get(name), None) for name in dc_probs}
            candidate = _best_candidate(dc_probs, prices, "double_chance", sport_name, base)
            if candidate is not None:
                candidates["double_chance"] = candidate
        btts_probs = calculate_btts_probabilities(event)
        if btts_probs:
            # No bookmaker prices for BTTS on the featured-odds feeds:
            # show the model's fair odds and flag them as estimated.
            prices = {
                name: (1.0 / min(data["probability"], 0.99), None)
                for name, data in btts_probs.items()
                if data["probability"] > 0
            }
            candidate = _best_candidate(
                btts_probs, prices, "btts", sport_name, base, odds_estimated=True,
            )
            if candidate is not None:
                candidates["btts"] = candidate
    return list(candidates.values())


def _best_candidate(
    probs: dict[str, dict],
    prices: dict[str, tuple[float | None, float | None]],
    market_type: str,
    sport_name: str,
    base: dict,
    odds_estimated: bool = False,
) -> dict | None:
    """Return the strongest outcome of one market as a candidate, if any qualifies."""
    best_candidate = None
    best_rank = None
    for outcome_name, data in probs.items():
        if data["num_bookmakers"] < config.MIN_BOOKMAKERS:
            continue
        probability = data["probability"]
        if probability < config.FALLBACK_PROBABILITY_FLOOR:
            continue
        probability = min(probability, 0.99)
        odds, point = prices.get(outcome_name, (None, None))
        if odds is None or odds < 1.01:
            continue
        pick = _format_pick_description(
            market_type, outcome_name, point,
            base["home_team"], base["away_team"], probability, sport_name=sport_name)
        if pick is None:
            continue
        rank = (probability, data["num_bookmakers"], odds)
        if best_rank is None or rank > best_rank:
            best_rank = rank
            best_candidate = {
                **base,
                "market_type": market_type,
                "market_label": MARKET_LABELS.get(market_type, market_type),
                "pick": pick,
                "odds": round(odds, 2),
                "odds_estimated": odds_estimated,
                "confidence": round(probability, CONFIDENCE_DECIMALS),
                "num_bookmakers": data["num_bookmakers"],
            }
    return best_candidate


def _apply_threshold_with_fallback(candidates, min_threshold, fallback_floor, min_matches):
    """Apply quality threshold with fallback for low-volume days.

    ``min_matches`` counts distinct matches, not market candidates.
    """
    qualifying = [c for c in candidates if c["confidence"] >= min_threshold]
    matches = {c.get("event_id") or c.get("match") for c in qualifying}
    if len(matches) >= min_matches:
        logger.info(f"Threshold filter: {len(matches)} matches meet {min_threshold:.0%}")
        return qualifying
    logger.info(f"Low-volume day: {len(matches)} matches (need {min_matches}). Fallback to {fallback_floor:.0%}")
    sorted_candidates = sorted(candidates, key=lambda x: x["confidence"], reverse=True)
    fallback = [c for c in sorted_candidates if c["confidence"] >= fallback_floor]
    logger.info(f"Fallback: returning {len(fallback)} candidates")
    return fallback


def _match_pick_key(pred: dict) -> tuple[str, str]:
    """Build a (match, pick) tuple used as a unique identity for a selection."""
    return (pred.get("match", ""), pred.get("pick", ""))


def _odds_value(pred: dict) -> float:
    """Safely extract a decimal odds float from a prediction dict."""
    try:
        return float(pred.get("odds", 1.0))
    except (TypeError, ValueError):
        return 1.0


def _passes_market_variety(
    pred: dict,
    market_counts: dict[str, int],
    limit: int = 2,
) -> bool:
    """True if adding ``pred`` would not exceed the per-market ``limit``."""
    mt = pred.get("market_type", "other")
    return market_counts.get(mt, 0) < limit


def _build_exact_accumulator(
    candidates: list[dict],
    *,
    target: float,
    tolerance: float,
    min_legs: int,
    max_legs: int,
    market_limit: int,
    min_odds: float | None = None,
    max_odds: float | None = None,
    excluded: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Build an accumulator whose combined odds are as close to ``target`` as
    possible while respecting market variety, leg count bounds, and an optional
    ``[min_odds, max_odds]`` clamp.

    For small ``max_legs`` (≤ 4) we use brute-force enumeration of all
    combinations — this finds the exact best fit.  For larger ``max_legs`` we
    fall back to a greedy best-fit with local-search refinement.
    """
    if excluded is None:
        excluded = set()
    if not candidates:
        return []
    if max_legs <= 4:
        return _brute_force_accumulator(
            candidates, target, tolerance, min_legs, max_legs,
            market_limit, min_odds, max_odds, excluded,
        )
    return _greedy_exact_accumulator(
        candidates, target, tolerance, min_legs, max_legs,
        market_limit, min_odds, max_odds, excluded,
    )


def _brute_force_accumulator(
    candidates: list[dict],
    target: float,
    tolerance: float,
    min_legs: int,
    max_legs: int,
    market_limit: int,
    min_odds: float | None,
    max_odds: float | None,
    excluded: set[tuple[str, str]],
) -> list[dict]:
    """Enumerate all combinations up to ``max_legs`` and return the one whose
    combined odds are at or above ``target`` with minimal overshoot.

    The hard floor (``min_odds``) is set to ``target`` by callers, so any
    combination below the target is rejected.  Among the remaining candidates
    we pick the one closest to the target — i.e. the smallest product that
    is still >= target."""
    from itertools import combinations

    floor = min_odds if min_odds is not None else target
    ceiling = max_odds if max_odds is not None else (target + tolerance)

    best_combo: tuple[dict, ...] = ()
    best_gap = float("inf")
    best_below: tuple[dict, ...] = ()
    best_below_product = 0.0

    for size in range(1, max_legs + 1):
        for combo in combinations(candidates, size):
            keys = [_match_pick_key(p) for p in combo]
            if any(k in excluded for k in keys):
                continue
            mkt_counts: dict[str, int] = {}
            variety_ok = True
            for p in combo:
                mt = p.get("market_type", "other")
                mkt_counts[mt] = mkt_counts.get(mt, 0) + 1
                if mkt_counts[mt] > market_limit:
                    variety_ok = False
                    break
            if not variety_ok:
                continue
            product = 1.0
            for p in combo:
                product *= _odds_value(p)
            if product > ceiling:
                continue
            if product >= floor:
                gap = product - target
                if gap < best_gap:
                    best_gap = gap
                    best_combo = combo
            elif product > best_below_product:
                best_below_product = product
                best_below = combo

    if best_combo:
        return list(best_combo)
    # Fall back to the best product below the floor (never empty if candidates exist)
    if best_below:
        return list(best_below)
    return []


def _greedy_exact_accumulator(
    candidates: list[dict],
    target: float,
    tolerance: float,
    min_legs: int,
    max_legs: int,
    market_limit: int,
    min_odds: float | None,
    max_odds: float | None,
    excluded: set[tuple[str, str]],
) -> list[dict]:
    """Greedy best-fit with local-search refinement for larger leg counts.

    The algorithm keeps adding legs until the combined product reaches the
    ``target`` (within ``tolerance``) or ``max_legs`` is reached.  Unlike a
    simple greedy search, it does NOT stop at a local minimum — if the
    product is still below ``min_odds`` it MUST keep adding, even when every
    remaining candidate temporarily widens the gap."""
    selected: list[dict] = []
    market_counts: dict[str, int] = {}
    used: set[tuple[str, str]] = set(excluded)
    product = 1.0
    pool = list(candidates)
    floor = min_odds if min_odds is not None else target

    while len(selected) < max_legs:
        # Stop early only when we're inside the acceptance band.
        if product >= target and (product - target) <= tolerance:
            break

        best_idx = None
        best_gap = float("inf")

        for idx, pred in enumerate(pool):
            mp = _match_pick_key(pred)
            if mp in used:
                continue
            if not _passes_market_variety(pred, market_counts, market_limit):
                continue
            new_product = product * _odds_value(pred)
            if max_odds is not None and new_product > max_odds:
                continue
            # While we're below the floor, prefer the largest product that
            # doesn't overshoot max_odds (gets us to the target fastest).
            if product < floor:
                gap = max_odds - new_product if max_odds is not None else -new_product
            else:
                gap = abs(new_product - target)
            if gap < best_gap:
                best_gap = gap
                best_idx = idx

        if best_idx is None:
            break

        chosen = pool[best_idx]
        mp = _match_pick_key(chosen)
        selected.append(chosen)
        used.add(mp)
        mt = chosen.get("market_type", "other")
        market_counts[mt] = market_counts.get(mt, 0) + 1
        product *= _odds_value(chosen)

    # Local search: try swapping each selected pick with an unused candidate
    improved = True
    while improved and len(selected) > 0:
        improved = False
        current_gap = abs(product - target)
        for i, sel in enumerate(selected):
            sel_key = _match_pick_key(sel)
            sel_odds = _odds_value(sel)
            remaining_product = product / sel_odds
            for pred in pool:
                mp = _match_pick_key(pred)
                if mp in used:
                    continue
                new_product = remaining_product * _odds_value(pred)
                if max_odds is not None and new_product > max_odds:
                    continue
                new_mt = pred.get("market_type", "other")
                old_mt = sel.get("market_type", "other")
                if new_mt != old_mt:
                    if market_counts.get(new_mt, 0) >= market_limit:
                        continue
                gap = abs(new_product - target)
                if gap < current_gap - 0.001:
                    used.discard(sel_key)
                    used.add(mp)
                    if new_mt != old_mt:
                        market_counts[old_mt] = market_counts.get(old_mt, 1) - 1
                        market_counts[new_mt] = market_counts.get(new_mt, 0) + 1
                    selected[i] = pred
                    product = new_product
                    current_gap = gap
                    improved = True
                    break
            if improved:
                break

    # Never return empty if we have candidates — always return the best effort
    return selected


def build_target_odds_accumulator(
    predictions: list[dict],
    min_probability: float = 0.75,
    target_odds: float = 2.0,
    max_legs: int = 4,
    excluded_match_picks: set[tuple[str, str]] | None = None,
) -> list[dict]:
    """Backwards-compatible wrapper around ``_build_exact_accumulator``.

    Retained for any external callers; ``get_top_picks`` / ``get_all_picks``
    now use ``_build_exact_accumulator`` directly so the probability ceiling,
    tolerance, and odds clamp can be set precisely.
    """
    if excluded_match_picks is None:
        excluded_match_picks = set()
    eligible = sorted(
        [p for p in predictions if p.get("confidence", 0.0) >= min_probability],
        key=lambda p: p.get("confidence", 0.0),
        reverse=True,
    )
    return _build_exact_accumulator(
        eligible,
        target=target_odds,
        tolerance=config.TOP_PICK_TOLERANCE,
        min_legs=config.TOP_PICK_MIN_LEGS,
        max_legs=max_legs,
        market_limit=2,
        excluded=excluded_match_picks,
    )


def get_top_picks(predictions: list[dict] | None, count: int = 5) -> list[dict]:
    """Return the top-picks accumulator with probability fallback cascade.

    Probability tiers: 70-90% → 60-70% → 50-60%.
    Combined odds target: exactly 2.00 (±config.TOP_PICK_TOLERANCE).
    Legs: 2-3 (config.TOP_PICK_MIN/MAX_LEGS).

    ``count`` is accepted for backward-compatibility but the accumulator
    always uses the configured leg bounds.
    """
    from probability import filter_by_probability_bracket

    predictions = predictions or []
    tiers = config.TOP_PICK_PROBABILITY_TIERS
    best_result: list[dict] = []

    for tier_idx in range(len(tiers)):
        # Combine all candidates from tiers tried so far (higher tiers first)
        cumulative = []
        seen_keys: set[tuple[str, str]] = set()
        for t_lo, t_hi in tiers[: tier_idx + 1]:
            for p in filter_by_probability_bracket(predictions, t_lo, t_hi):
                key = _match_pick_key(p)
                if key not in seen_keys:
                    seen_keys.add(key)
                    cumulative.append(p)

        cumulative = sorted(
            cumulative,
            key=lambda p: p.get("confidence", 0.0),
            reverse=True,
        )[: config.ACCUMULATOR_MAX_CANDIDATES]

        result = _build_exact_accumulator(
            cumulative,
            target=config.TOP_PICK_TARGET_ODDS,
            tolerance=config.TOP_PICK_TOLERANCE,
            min_legs=config.TOP_PICK_MIN_LEGS,
            max_legs=config.TOP_PICK_MAX_LEGS,
            market_limit=2,
            min_odds=config.TOP_PICK_MIN_ODDS,
            max_odds=config.TOP_PICK_TARGET_ODDS + config.TOP_PICK_TOLERANCE,
        )

        if result:
            best_result = result
            # Check if we hit the target within tolerance
            product = 1.0
            for p in result:
                product *= _odds_value(p)
            if abs(product - config.TOP_PICK_TARGET_ODDS) <= config.TOP_PICK_TOLERANCE:
                return result  # Target hit — stop cascade

    return best_result


def get_all_picks(predictions: list[dict] | None, top_picks: list[dict] | None = None) -> list[dict]:
    """Return the all-picks accumulator with probability fallback cascade.

    Probability tiers: 65-90% → 55-65% → 50-55%.
    Combined odds target: 8.00-10.00 (config.ALL_PICK_MIN/MAX_ODDS).
    Legs: 5-10 (config.ALL_PICK_MIN/MAX_LEGS).

    Excludes exact match+pick pairings already used in ``top_picks``. If the
    same fixture has a *different* outcome available, that outcome is eligible.
    If no distinct outcome exists, the fixture is skipped entirely.
    """
    from probability import filter_by_probability_bracket

    predictions = predictions or []
    excluded: set[tuple[str, str]] = {
        _match_pick_key(p) for p in (top_picks or [])
    }

    tiers = config.ALL_PICK_PROBABILITY_TIERS
    best_result: list[dict] = []

    for tier_idx in range(len(tiers)):
        # Combine all candidates from tiers tried so far (higher tiers first)
        cumulative: list[dict] = []
        seen_keys: set[tuple[str, str]] = set()
        for t_lo, t_hi in tiers[: tier_idx + 1]:
            for p in filter_by_probability_bracket(predictions, t_lo, t_hi):
                key = _match_pick_key(p)
                if key not in seen_keys and key not in excluded:
                    seen_keys.add(key)
                    cumulative.append(p)

        cumulative = sorted(
            cumulative,
            key=lambda p: p.get("confidence", 0.0),
            reverse=True,
        )[: config.ACCUMULATOR_MAX_CANDIDATES]

        result = _build_exact_accumulator(
            cumulative,
            target=config.ALL_PICK_TARGET_ODDS,
            tolerance=config.ALL_PICK_TOLERANCE,
            min_legs=config.ALL_PICK_MIN_LEGS,
            max_legs=config.ALL_PICK_MAX_LEGS,
            market_limit=2,
            min_odds=config.ALL_PICK_MIN_ODDS,
            max_odds=config.ALL_PICK_MAX_ODDS,
            excluded=excluded,
        )

        if result:
            best_result = result
            product = 1.0
            for p in result:
                product *= _odds_value(p)
            if config.ALL_PICK_MIN_ODDS <= product <= config.ALL_PICK_MAX_ODDS:
                return result  # Target hit — stop cascade

    return best_result


def calculate_ticket_odds(picks: list[dict]) -> float:
    """
    Combined odds for a multi-pick daily ticket (accumulator).

    Total Odds = Odds_1 * Odds_2 * ... * Odds_N — the mathematical product of
    each selection's decimal odds, never their sum.
    """
    return calculate_combined_odds([p.get("odds") for p in picks])


def build_accumulator(picks: list[dict]) -> dict:
    """
    Build a multi-pick daily ticket summary.

    Returns the individual selections (each with its own decimal odds) plus
    the calculated combined ticket total, so callers can display both.
    """
    selections = [
        {
            "match": p.get("match"),
            "pick": p.get("pick"),
            "odds": p.get("odds"),
            "confidence": p.get("confidence"),
        }
        for p in picks
    ]
    return {
        "num_selections": len(picks),
        "selections": selections,
        "combined_odds": calculate_ticket_odds(picks),
    }


def filter_by_sport(predictions: list[dict] | None, sport: str) -> list[dict]:
    """Filter predictions by sport name."""
    return [p for p in predictions or [] if p["sport"].lower() == sport.lower()]
