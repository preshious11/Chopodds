"""Asynchronous settlement of delivered predictions from The Odds API scores."""

import logging
import re
from typing import Any

import httpx

import config
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

    pick = str(prediction.get("pick", ""))
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
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except (httpx.TimeoutException, httpx.HTTPError, ValueError) as exc:
        logger.warning("Could not fetch scores for %s: %s", sport_key, exc)
        return []


async def settle_pending_predictions() -> None:
    """Fetch scores once for pending sports and settle completed predictions."""
    pending = tracking.get_pending_predictions()
    if not pending:
        return

    evaluated = wins = losses = voids = 0
    grouped: dict[str, list[dict[str, Any]]] = {}
    for prediction in pending:
        sport_key = _league_to_sport_key(prediction.get("league", ""))
        if sport_key:
            grouped.setdefault(sport_key, []).append(prediction)
        else:
            logger.warning("No sport key found for pending prediction %s", prediction.get("event_id"))

    async with httpx.AsyncClient(timeout=15.0) as client:
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
    tracking.set_last_score_fetch()
    logger.info(
        "Settlement Complete: %s matches evaluated | %s Wins | %s Losses | %s Void",
        evaluated,
        wins,
        losses,
        voids,
    )
