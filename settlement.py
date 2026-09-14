"""Asynchronous settlement of delivered predictions from The Odds API scores."""

import logging
import re
from typing import Any

import httpx

import config
import odds_client
import stats
import tracking
from predictions import SPORT_KEY_MAP

logger = logging.getLogger(__name__)
SCORES_URL = "https://api.the-odds-api.com/v4/sports/{sport_key}/scores"


def _league_to_sport_key(league: str) -> str | None:
    """Resolve a stored league name to its Odds API sport key."""
    for leagues in SPORT_KEY_MAP.values():
        for item in leagues:
            if item.get("name") == league:
                return item.get("key")
    return None


def _score_totals(scores: list[dict[str, Any]]) -> dict[str, float]:
    """Convert an Odds API score list to team-name totals."""
    totals = {}
    for score in scores:
        name = score.get("name")
        value = score.get("score")
        if name and value is not None:
            try:
                totals[name] = float(value)
            except (TypeError, ValueError):
                continue
    return totals


def _settle_pick(prediction: dict[str, Any], scores: list[dict[str, Any]]) -> str | None:
    """Return ``win``, ``loss``, or ``void`` when a pick can be settled."""
    if not scores:
        return "void"
    totals = _score_totals(scores)
    if len(totals) < 2:
        return None

    home = prediction.get("home_team", "")
    away = prediction.get("away_team", "")
    home_score = totals.get(home)
    away_score = totals.get(away)
    if home_score is None or away_score is None:
        return None

    pick = str(prediction.get("pick") or prediction.get("selection") or "")
    market = prediction.get("market_type")
    if market == "h2h":
        if home_score == away_score:
            winner = "Draw"
        else:
            winner = home if home_score > away_score else away
        selected = pick.removesuffix(" (Strong Favorite)").removesuffix(" (Value Pick)")
        selected = selected.removesuffix(" to Win").strip()
        return "win" if selected == winner else "loss"

    if market in {"totals", "alternate_totals"}:
        match = re.search(r"\b(Over|Under)\s+([0-9]+(?:\.[0-9])?)", pick)
        if not match:
            return None
        line = float(match.group(2))
        total = home_score + away_score
        won = total > line if match.group(1) == "Over" else total < line
        return "win" if won else "loss"

    if market in {"spreads", "alternate_spreads"}:
        match = re.search(r"([+-][0-9]+(?:\.[0-9])?)", pick)
        if not match:
            return None
        team = pick.split(" ", 1)[0]
        score = totals.get(team)
        opponent = away_score if team == home else home_score if team == away else None
        if score is None or opponent is None:
            return None
        return "win" if score + float(match.group(1)) > opponent else "loss"

    if market == "double_chance":
        if home_score == away_score:
            outcome = "1X"
        elif home_score > away_score:
            outcome = "1X" if home in pick else "X2"
        else:
            outcome = "X2" if away in pick else "1X"
        return "win" if outcome in pick else "loss"

    if market == "btts":
        both_scored = home_score > 0 and away_score > 0
        won = both_scored if "Yes" in pick else not both_scored
        return "win" if won else "loss"

    return None


async def _fetch_scores(client: httpx.AsyncClient, sport_key: str) -> list[dict[str, Any]]:
    """Fetch recent scores for one sport without blocking the event loop."""
    try:
        response = await client.get(
            SCORES_URL.format(sport_key=sport_key),
            params={
                "apiKey": str(config.ODDS_API_KEY).strip().strip('"\'').strip(),
                "daysFrom": 3,
            },
        )
        response.raise_for_status()
        # Monitor remaining credits after every scores fetch.
        odds_client.log_scores_quota(response.headers, sport_key)
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except (httpx.TimeoutException, httpx.HTTPError, ValueError) as exc:
        logger.warning("Could not fetch scores for %s: %s", sport_key, exc)
        return []


async def settle_pending_predictions() -> None:
    """Credit-optimized, kick-off-aware settlement of pending predictions.

    Only matches whose kick-off has elapsed long enough for a result to exist
    (and that are not stale beyond the polling window) are polled. Scores are
    fetched only for the unique sports among those eligible matches, so an idle
    run fetches nothing and costs 0 API credits.
    """
    eligible = tracking.get_eligible_pending_predictions()
    if not eligible:
        logger.info(
            "[Settlement Skipped] 0 pending matches eligible for completion check."
        )
        return

    active_sports = _active_sport_keys(eligible)
    if not active_sports:
        logger.warning(
            "[Settlement Skipped] %d eligible pending matches but no resolvable "
            "sport keys — 0 API credits used.", len(eligible),
        )
        return

    logger.info(
        "[Settlement] %d eligible matches across %d sport(s): %s",
        len(eligible), len(active_sports), ", ".join(active_sports),
    )

    evaluated = wins = losses = voids = 0
    grouped: dict[str, list[dict[str, Any]]] = {}
    for prediction in eligible:
        sport_key = prediction.get("sport_key") or _league_to_sport_key(
            prediction.get("league", "")
        )
        if sport_key:
            grouped.setdefault(sport_key, []).append(prediction)

    async with httpx.AsyncClient(timeout=15.0) as client:
        # Fetch scores ONLY for the sports that have eligible matches.
        for sport_key, predictions in grouped.items():
            scores = await _fetch_scores(client, sport_key)
            scores_by_id = {str(item.get("id")): item for item in scores}
            for prediction in predictions:
                event = scores_by_id.get(str(prediction.get("event_id")))
                if not event:
                    continue
                event_status = str(event.get("status", "")).lower()
                if event_status in {"postponed", "canceled", "cancelled", "void"}:
                    if tracking.settle_prediction(prediction["prediction_id"], "VOID"):
                        evaluated += 1
                        voids += 1
                    continue
                if not event.get("completed"):
                    continue
                result = _settle_pick(prediction, event.get("scores", []))
                if result:
                    status = "SETTLED_WIN" if result == "win" else "SETTLED_LOSS"
                    if tracking.settle_prediction(prediction["prediction_id"], status):
                        evaluated += 1
                        wins += result == "win"
                        losses += result == "loss"
                    logger.info(
                        "Settled %s as %s",
                        prediction.get("event_id"),
                        result,
                    )
    # Invalidate the /stats cache only when records actually changed, so
    # repeated /stats requests reflect newly settled matches immediately.
    if evaluated > 0:
        stats.invalidate_stats_cache()
    tracking.set_last_score_fetch()
    logger.info(
        "Settlement Complete: %s matches evaluated | %s Wins | %s Losses | %s Void",
        evaluated,
        wins,
        losses,
        voids,
    )


def _active_sport_keys(predictions: list[dict[str, Any]]) -> list[str]:
    """Return the ordered, unique sport keys among the eligible matches.

    Prefers the ``sport_key`` stored on each tracked row; falls back to
    resolving the league name when it is absent.
    """
    seen: set[str] = set()
    keys: list[str] = []
    for prediction in predictions:
        sport_key = prediction.get("sport_key") or _league_to_sport_key(
            prediction.get("league", "")
        )
        if sport_key and sport_key not in seen:
            seen.add(sport_key)
            keys.append(sport_key)
    return keys
