"""Asynchronous settlement of delivered predictions from The Odds API scores."""

import logging
import re
import unicodedata
from datetime import timedelta
from typing import Any

import httpx

import odds_client
import stats
import tracking
from predictions import SPORT_KEY_MAP

logger = logging.getLogger(__name__)
SCORES_URL = "https://api.the-odds-api.com/v4/sports/{sport_key}/scores"

# The scores endpoint returns games completed within this many days.
SCORES_DAYS_FROM = 3
_SPORT_KEY_PATTERN = re.compile(r"^[a-z0-9]+(?:_[a-z0-9]+)+$")
# Generic tennis keys used by fallback providers have no scores endpoint.
_UNSCORABLE_SPORT_KEYS = {"tennis_atp", "tennis_wta"}
_LINE = r"([+-]?\d+(?:\.\d+)?)"
# Words dropped when comparing team names across providers.
_TEAM_NAME_NOISE = {
    "fc", "cf", "afc", "sc", "ac", "as", "ssc", "cd", "sd", "ud", "club",
    "the", "calcio", "fk", "sk", "bk", "if", "sv",
}
# Kick-off times from different providers may differ slightly.
_KICKOFF_TOLERANCE = timedelta(hours=6)


def _league_to_sport_key(league: str) -> str | None:
    """Resolve a stored league name to its Odds API sport key."""
    for leagues in SPORT_KEY_MAP.values():
        for item in leagues:
            if item.get("name") == league:
                return item.get("key")
    return None


def _infer_market(pick: str) -> str | None:
    """Work out the market of a row recorded before market_type was stored."""
    if pick.startswith("BTTS "):
        return "btts"
    if pick.endswith(" to Win (Draw No Bet)"):
        return "draw_no_bet"
    if re.match(r"^(Over|Under)\s+\d", pick):
        return "totals"
    if pick == "Draw" or " to Win" in pick:
        return "h2h"
    if " or " in pick:
        return "double_chance"
    if re.search(r"\s[+-]\d+(?:\.\d+)?$", pick):
        return "spreads"
    return None


def _with_settlement_fields(row: dict[str, Any]) -> dict[str, Any]:
    """
    Normalize a tracking row for grading. Rows recorded before the teams,
    market, sport and sport key were stored get them inferred from the match
    name, pick text and stored league name.
    """
    prediction = dict(row)
    pick = str(prediction.get("pick") or prediction.get("selection") or "")
    prediction["pick"] = pick
    if not (prediction.get("home_team") and prediction.get("away_team")):
        match_name = str(prediction.get("match_name") or prediction.get("match") or "")
        home, separator, away = match_name.partition(" vs ")
        if separator:
            prediction["home_team"], prediction["away_team"] = home, away
    prediction["market_type"] = prediction.get("market_type") or _infer_market(pick)

    stored_key = str(prediction.get("sport_key") or "")
    if _SPORT_KEY_PATTERN.match(stored_key):
        sport_key = stored_key
    else:
        # Older rows stored the league name in the sport_key column.
        sport_key = (
            _league_to_sport_key(prediction.get("league") or "")
            or _league_to_sport_key(stored_key)
        )
    prediction["sport_key"] = sport_key
    prediction["sport"] = prediction.get("sport") or (
        "Tennis" if (sport_key or "").startswith("tennis") else "Football"
    )
    return prediction


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


def _result(won: bool) -> str:
    return "win" if won else "loss"


def _from_margin(margin: float) -> str:
    """Settle a line bet: a positive margin wins, zero is a push (void).

    Quarter lines (e.g. 2.25) are graded as a full win/loss on whichever
    side of the line the result falls, not as half-stakes.
    """
    if margin > 0:
        return "win"
    if margin < 0:
        return "loss"
    return "void"


def _settle_pick(prediction: dict[str, Any], scores: list[dict[str, Any]]) -> str | None:
    """Return ``win``, ``loss``, or ``void`` when a pick can be settled."""
    if not scores:
        return "void"
    totals = _score_totals(scores)

    home = prediction.get("home_team") or ""
    away = prediction.get("away_team") or ""
    home_score = totals.get(home)
    away_score = totals.get(away)
    if home_score is None or away_score is None:
        return None

    pick = str(prediction.get("pick") or prediction.get("selection") or "")
    market = prediction.get("market_type")
    if prediction.get("sport") == "Tennis" and market != "h2h":
        # Tennis totals and handicaps are counted in games, which the scores
        # feed does not break down, so only match-winner picks are graded.
        return None

    if home_score > away_score:
        outcome = "home"
    elif away_score > home_score:
        outcome = "away"
    else:
        outcome = "draw"

    if market == "h2h":
        selected = pick.removesuffix(" (Strong Favorite)").removesuffix(" (Value Pick)")
        selected = selected.removesuffix(" to Win").strip()
        winner = {"home": home, "away": away, "draw": "Draw"}[outcome]
        return _result(selected == winner)

    if market == "draw_no_bet":
        if outcome == "draw":
            return "void"
        selected = pick.removesuffix(" to Win (Draw No Bet)").strip()
        return _result(selected == (home if outcome == "home" else away))

    if market in {"totals", "alternate_totals"}:
        match = re.search(rf"\b(Over|Under)\s+{_LINE}", pick)
        if not match:
            return None
        margin = home_score + away_score - float(match.group(2))
        return _from_margin(margin if match.group(1) == "Over" else -margin)

    if market in {"spreads", "alternate_spreads"}:
        # Longest name first so "Inter" never matches a pick on "Inter Miami".
        sides = sorted(
            ((home, home_score, away_score), (away, away_score, home_score)),
            key=lambda side: len(side[0]),
            reverse=True,
        )
        for team, team_score, opponent_score in sides:
            if team and pick.startswith(f"{team} "):
                line = re.fullmatch(rf"\s*{_LINE}", pick[len(team):])
                if not line:
                    return None
                return _from_margin(team_score + float(line.group(1)) - opponent_score)
        return None

    if market == "double_chance":
        covered = {
            f"{home} or Draw": {"home", "draw"},
            f"{away} or Draw": {"away", "draw"},
            f"{home} or {away}": {"home", "away"},
        }.get(pick)
        if covered is None:
            return None
        return _result(outcome in covered)

    if market == "btts":
        both_scored = home_score > 0 and away_score > 0
        return _result(both_scored if "Yes" in pick else not both_scored)

    return None


def _normalize_team(name: Any) -> str:
    """Comparable form of a team name: ASCII, lower case, no club suffixes."""
    text = unicodedata.normalize("NFKD", str(name or "")).encode("ascii", "ignore").decode()
    tokens = re.split(r"[^a-z0-9]+", text.lower().replace("&", " and "))
    return " ".join(token for token in tokens if token and token not in _TEAM_NAME_NOISE)


def _same_team(first: Any, second: Any) -> bool:
    """Whether two providers' names refer to the same team."""
    a, b = _normalize_team(first), _normalize_team(second)
    if not a or not b:
        return False
    if a == b:
        return True
    shorter, longer = sorted((set(a.split()), set(b.split())), key=len)
    return shorter <= longer


def _find_score_event(
    prediction: dict[str, Any],
    scores: list[dict[str, Any]],
    scores_by_id: dict[str, dict[str, Any]],
) -> dict[str, Any] | None:
    """
    Find the scores entry for a pick: by event id, or — for picks whose odds
    came from a fallback provider with different ids — by both team names
    and a kick-off within a few hours.
    """
    event = scores_by_id.get(str(prediction.get("event_id")))
    if event:
        return event
    kickoff = tracking._parse_utc(prediction.get("commence_time"))
    for item in scores:
        if not (
            _same_team(prediction.get("home_team"), item.get("home_team"))
            and _same_team(prediction.get("away_team"), item.get("away_team"))
        ):
            continue
        item_kickoff = tracking._parse_utc(item.get("commence_time"))
        if kickoff and item_kickoff and abs(item_kickoff - kickoff) > _KICKOFF_TOLERANCE:
            continue
        return item
    return None


def _align_team_names(prediction: dict[str, Any], event: dict[str, Any]) -> dict[str, Any]:
    """Rewrite a pick in the scores feed's team names so it can be graded."""
    new_home, new_away = event.get("home_team"), event.get("away_team")
    old_home, old_away = prediction.get("home_team"), prediction.get("away_team")
    if not (new_home and new_away and old_home and old_away):
        return prediction
    if (old_home, old_away) == (new_home, new_away):
        return prediction
    pick = str(prediction.get("pick") or "")
    # Placeholders stop a replaced name from being matched again.
    for old, placeholder in sorted(
        ((old_home, "\x00HOME\x00"), (old_away, "\x00AWAY\x00")),
        key=lambda item: len(item[0]),
        reverse=True,
    ):
        pick = pick.replace(old, placeholder)
    pick = pick.replace("\x00HOME\x00", new_home).replace("\x00AWAY\x00", new_away)
    return {**prediction, "home_team": new_home, "away_team": new_away, "pick": pick}


async def _fetch_scores(
    client: httpx.AsyncClient, sport_key: str
) -> list[dict[str, Any]] | None:
    """Fetch recent scores for one sport. Returns None if the request failed."""
    try:
        response = await client.get(
            SCORES_URL.format(sport_key=sport_key),
            params={"apiKey": odds_client._api_key(), "daysFrom": SCORES_DAYS_FROM},
        )
        response.raise_for_status()
        # Monitor remaining credits after every scores fetch.
        odds_client.log_scores_quota(response.headers, sport_key)
        payload = response.json()
        return payload if isinstance(payload, list) else []
    except (httpx.HTTPError, ValueError) as exc:
        logger.warning("Could not fetch scores for %s: %s", sport_key, exc)
        return None


async def settle_pending_predictions() -> None:
    """Credit-optimized, kick-off-aware settlement of pending predictions.

    Only matches whose kick-off has elapsed long enough for a result to exist
    (and that are not stale beyond the polling window) are polled. Scores are
    fetched only for the unique sports among those eligible matches, so an idle
    run fetches nothing and costs 0 API credits. Picks that outlive the
    polling window without a result are voided.
    """
    changed = tracking.void_stale_pending()

    eligible = [
        _with_settlement_fields(row)
        for row in tracking.get_eligible_pending_predictions()
    ]
    if not eligible:
        logger.info(
            "[Settlement Skipped] 0 pending matches eligible for completion check."
        )
        if changed:
            stats.invalidate_stats_cache()
        return

    grouped: dict[str, list[dict[str, Any]]] = {}
    for prediction in eligible:
        sport_key = prediction.get("sport_key")
        if sport_key and sport_key not in _UNSCORABLE_SPORT_KEYS:
            grouped.setdefault(sport_key, []).append(prediction)
    if not grouped:
        logger.warning(
            "[Settlement Skipped] %d eligible pending matches but no resolvable "
            "sport keys — 0 API credits used.", len(eligible),
        )
        if changed:
            stats.invalidate_stats_cache()
        return

    logger.info(
        "[Settlement] %d eligible matches across %d sport(s): %s",
        len(eligible), len(grouped), ", ".join(grouped),
    )

    wins = losses = voids = 0
    fetched_any = False
    async with httpx.AsyncClient(timeout=15.0) as client:
        # Fetch scores ONLY for the sports that have eligible matches.
        for sport_key, predictions in grouped.items():
            scores = await _fetch_scores(client, sport_key)
            if scores is None:
                continue  # Retry on the next run
            fetched_any = True
            scores_by_id = {str(item.get("id")): item for item in scores}
            for prediction in predictions:
                event = _find_score_event(prediction, scores, scores_by_id)
                if not event:
                    continue
                event_status = str(event.get("status", "")).lower()
                if event_status in {"postponed", "canceled", "cancelled", "void"}:
                    result = "void"
                elif not event.get("completed"):
                    continue
                else:
                    result = _settle_pick(
                        _align_team_names(prediction, event), event.get("scores") or []
                    )
                if result is None:
                    continue  # Voided later by void_stale_pending if never gradable
                status = {"win": "SETTLED_WIN", "loss": "SETTLED_LOSS"}.get(result, "VOID")
                if tracking.settle_prediction(prediction["prediction_id"], status):
                    changed += 1
                    wins += result == "win"
                    losses += result == "loss"
                    voids += result == "void"
                    logger.info(
                        "Settled %s (%s) as %s",
                        prediction.get("event_id"), prediction.get("pick"), result,
                    )

    # Invalidate the /stats cache only when records actually changed, so
    # repeated /stats requests reflect newly settled matches immediately.
    if changed:
        stats.invalidate_stats_cache()
    if fetched_any:
        tracking.set_last_score_fetch()
    logger.info(
        "Settlement Complete: %s matches evaluated | %s Wins | %s Losses | %s Void",
        wins + losses + voids,
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
