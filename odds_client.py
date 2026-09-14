"""
Odds data client with automatic provider failover.

Providers are tried in order (config.ODDS_PROVIDER_ORDER, by default
The Odds API -> SharpAPI -> SportsGameOdds). A provider only receives the
leagues the providers before it could not answer. The Odds API is queried one
league at a time; the fallback providers fetch all of their leagues in one
paginated request, so their free-tier rate limits hold.

The merged result is cached once per Africa/Lagos day by daily_cache, so on a
normal day the providers are called once. Every event is normalized to The
Odds API's shape — {id, home_team, away_team, commence_time, bookmakers:
[{key, markets: [{key, outcomes: [{name, price, point}]}]}]} — and tagged
with the "source" provider that supplied it.
"""

import logging
import time
from collections import Counter
from datetime import datetime, timezone
from typing import Callable

from zoneinfo import ZoneInfo

import requests

import config
from daily_cache import ensure_populated

BASE_URL = "https://api.the-odds-api.com/v4"
LAGOS_TZ = ZoneInfo("Africa/Lagos")

PROVIDER_ODDS_API = "the-odds-api"
PROVIDER_SHARPAPI = "sharpapi"
PROVIDER_SPORTSGAMEODDS = "sportsgameodds"

logger = logging.getLogger(__name__)

# Last observed The Odds API quota info (parsed from response headers).
# Exposed for the admin /status diagnostic.
_last_quota = {
    "remaining": None,
    "used": None,
    "error": None,   # last HTTP error encountered, if any
}

# Outcome of the last daily fetch, for /status:
# {"at": iso, "providers": {name: {...}}, "sources": {sport_key: provider}}
_last_fetch_report: dict = {}

_HTTP_ERRORS = {
    401: "Invalid API key (HTTP 401)",
    429: "Rate limited (HTTP 429)",
}
_TRANSIENT_STATUSES = {408, 500, 502, 503, 504}
# Waits before retrying a burst rate limit (HTTP 429). The Odds API docs:
# "try spacing out requests over several seconds".
_RATE_LIMIT_BACKOFF_SECONDS = (2.0, 5.0, 10.0)
# Longest server-requested wait honoured before giving up on a fallback provider.
_MAX_RETRY_AFTER_SECONDS = 15.0
# The Odds API: stop after this many leagues in a row fail outright.
_MAX_CONSECUTIVE_LEAGUE_FAILURES = 3

# Fallback provider league mappings, keyed by our (The Odds API style) sport
# keys. Tennis entries also match tournament keys by prefix
# (tennis_atp_us_open -> tennis_atp).
# SharpAPI slugs — docs.sharpapi.io/en/api-reference/leagues
# ("League IDs use canonical slug form (england_-_premier_league, not epl)").
_SHARPAPI_LEAGUES = {
    "soccer_epl": "soccer/england_-_premier_league",
    "soccer_spain_la_liga": "soccer/spain_-_la_liga",
    "soccer_italy_serie_a": "soccer/italy_-_serie_a",
    "soccer_germany_bundesliga": "soccer/germany_-_bundesliga",
    "soccer_france_ligue_one": "soccer/france_-_ligue_1",
    "soccer_uefa_champs_league": "soccer/uefa_-_champions_league",
    "soccer_usa_mls": "soccer/usa_-_major_league_soccer",
    "tennis_atp": "tennis/atp",
    "tennis_wta": "tennis/wta",
}
# SharpAPI market ids -> pipeline market keys.
_SHARPAPI_MARKETS = {
    "moneyline": "h2h",
    "point_spread": "spreads",
    "total_goals": "totals",
}
# SportsGameOdds leagueIDs — sportsgameodds.com/docs/data-types/leagues
_SPORTSGAMEODDS_LEAGUES = {
    "soccer_epl": "EPL",
    "soccer_spain_la_liga": "LA_LIGA",
    "soccer_italy_serie_a": "IT_SERIE_A",
    "soccer_germany_bundesliga": "BUNDESLIGA",
    "soccer_france_ligue_one": "FR_LIGUE_1",
    "soccer_uefa_champs_league": "UEFA_CHAMPIONS_LEAGUE",
    "soccer_uefa_europa_league": "UEFA_EUROPA_LEAGUE",
    "soccer_netherlands_eredivisie": "EREDIVISIE",
    "soccer_usa_mls": "MLS",
    "tennis_atp": "ATP",
    "tennis_wta": "WTA",
}


class OddsAPIError(Exception):
    """No odds could be obtained (bad keys, exhausted quotas, outage)."""


def _api_key() -> str:
    """Return the configured The Odds API key without shell/dotenv quoting noise."""
    return str(config.ODDS_API_KEY or "").strip().strip('"\'').strip()


def get_last_quota() -> dict:
    """Return the last observed Odds API quota info from response headers."""
    return dict(_last_quota)


def get_last_fetch_report() -> dict:
    """Return which provider served each league in the last daily fetch."""
    return dict(_last_fetch_report)


def _record_quota(headers, label: str) -> None:
    """Parse and log The Odds API quota headers from a response."""
    remaining = headers.get("x-requests-remaining")
    used = headers.get("x-requests-used")
    if remaining is None:
        return
    _last_quota["remaining"] = remaining
    _last_quota["used"] = used
    if _quota_exhausted(headers):
        logger.critical(
            "[CRITICAL] Odds API quota exhausted (0 requests remaining) after "
            "%s. Used: %s", label, used,
        )
    else:
        logger.info(
            "Odds API quota after %s: %s requests remaining, %s used",
            label, remaining, used,
        )


def _quota_exhausted(headers) -> bool:
    try:
        return int(float(headers.get("x-requests-remaining"))) <= 0
    except (TypeError, ValueError):
        return False


def log_scores_quota(headers, sport_key: str) -> None:
    """Log the ``x-requests-remaining`` header after a scores fetch.

    Called by the settlement engine after every ``/scores`` call so the admin
    can monitor remaining credits in the application logs. The quota state is
    also recorded so the /status diagnostic stays accurate.
    """
    remaining = headers.get("x-requests-remaining")
    used = headers.get("x-requests-used")
    if remaining is not None:
        _last_quota["remaining"] = remaining
        _last_quota["used"] = used
        logger.info(
            "Scores fetch for %s — Odds API requests remaining: %s, used: %s",
            sport_key, remaining, used,
        )


def check_api_key() -> dict:
    """
    Verify the ODDS_API_KEY against the /sports endpoint (does not consume
    usage credits) and return a diagnostic dict:
    {configured, valid, remaining, used, error}

    Blocking — call it via asyncio.to_thread from async handlers.
    """
    result = {
        "configured": bool(_api_key()),
        "valid": False,
        "remaining": None,
        "used": None,
        "error": None,
    }
    if not result["configured"]:
        result["error"] = "ODDS_API_KEY is missing from the environment"
        return result
    try:
        resp = requests.get(
            f"{BASE_URL}/sports", params={"apiKey": _api_key()}, timeout=15,
        )
        _record_quota(resp.headers, "api-key-check")
        result["remaining"] = resp.headers.get("x-requests-remaining")
        result["used"] = resp.headers.get("x-requests-used")
        if resp.status_code == 200:
            result["valid"] = True
        else:
            result["error"] = _HTTP_ERRORS.get(
                resp.status_code, f"HTTP {resp.status_code}: {resp.text[:120]}"
            )
    except requests.RequestException as e:
        result["error"] = f"Network error: {e}"
    return result


def get_sports() -> list[dict]:
    """
    Return the sports currently in season from The Odds API. Does not consume
    usage credits. Raises OddsAPIError on a non-200 response.
    """
    resp = requests.get(
        f"{BASE_URL}/sports",
        params={"apiKey": _api_key()},
        timeout=config.ODDS_API_TIMEOUT_SECONDS,
    )
    if resp.status_code != 200:
        error = _HTTP_ERRORS.get(resp.status_code, f"HTTP {resp.status_code}")
        _last_quota["error"] = error
        raise OddsAPIError(f"Failed to fetch sports list: {error} {resp.text[:200]}")
    payload = resp.json()
    return payload if isinstance(payload, list) else []


# ---------------------------------------------------------------------------
# Which leagues to fetch
# ---------------------------------------------------------------------------

def _configured_leagues() -> tuple[list[str], list[str]]:
    """(fixed sport keys, tennis key prefixes) from predictions.SPORT_KEY_MAP."""
    # Import here to avoid circular imports at module load time
    from predictions import SPORT_KEY_MAP

    fixed_keys: list[str] = []
    prefixes: list[str] = []
    for leagues in SPORT_KEY_MAP.values():
        for league in leagues:
            if league.get("key"):
                if league["key"] not in fixed_keys:
                    fixed_keys.append(league["key"])
            elif league.get("key_prefix"):
                prefixes.append(league["key_prefix"])
    return fixed_keys, prefixes


def _resolve_fetch_plan() -> tuple[list[str], str | None]:
    """
    Decide which sport keys to fetch today.

    Uses The Odds API's free /sports list to skip off-season leagues and to
    find the tennis tournaments in play. If that list is unavailable, every
    configured league is fetched, with tennis under the generic keys
    ``tennis_atp`` / ``tennis_wta`` that the fallback providers understand.

    Returns (sport keys, reason The Odds API is unusable or None).
    """
    fixed_keys, prefixes = _configured_leagues()
    generic_keys = fixed_keys + [prefix.rstrip("_") for prefix in prefixes]
    if not _api_key():
        return generic_keys, "ODDS_API_KEY not set"
    try:
        active = {sport.get("key") for sport in get_sports()}
    except OddsAPIError as exc:
        logger.warning("[ODDS] The Odds API unusable for today's fetch: %s", exc)
        return generic_keys, str(exc)
    except (requests.RequestException, ValueError) as exc:
        logger.warning(
            "[ODDS] Could not load the in-season sports list (%s); fetching "
            "every configured league", exc,
        )
        return generic_keys, None

    off_season = [key for key in fixed_keys if key not in active]
    if off_season:
        logger.info("[ODDS] Skipping off-season leagues: %s", ", ".join(off_season))
    tournaments = sorted(
        key for key in active
        if key and any(key.startswith(prefix) for prefix in prefixes)
    )
    return [key for key in fixed_keys if key in active] + tournaments, None


def _provider_league(sport_key: str, mapping: dict[str, str]) -> str | None:
    """Map a sport key to a provider league; tennis tournaments match by prefix."""
    if sport_key in mapping:
        return mapping[sport_key]
    for generic, value in mapping.items():
        if generic.startswith("tennis_") and sport_key.startswith(generic + "_"):
            return value
    return None


# ---------------------------------------------------------------------------
# Provider 1: The Odds API
# ---------------------------------------------------------------------------

def _odds_api_league(sport_key: str) -> tuple[str, list | None]:
    """
    Fetch one league from The Odds API.

    Returns ("ok", events) — including a legitimately empty list —,
    ("failed", None) when only this league failed, or ("stop", None) when the
    provider cannot be used any more today (bad key, quota or rate limit).
    """
    params = {
        "apiKey": _api_key(),
        "regions": config.ODDS_REGIONS,
        "markets": config.ODDS_MARKETS,
        "oddsFormat": "decimal",
    }
    backoff = list(_RATE_LIMIT_BACKOFF_SECONDS)
    transient_retry = True
    while True:
        try:
            resp = requests.get(
                f"{BASE_URL}/sports/{sport_key}/odds",
                params=params,
                timeout=config.ODDS_API_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            if transient_retry:
                transient_retry = False
                time.sleep(2.0)
                continue
            _last_quota["error"] = f"Network error: {str(exc)[:160]}"
            return "failed", None

        status = resp.status_code
        if status == 200:
            try:
                events = resp.json()
            except ValueError:
                events = None
            if not isinstance(events, list):
                _last_quota["error"] = "Malformed response"
                return "failed", None
            _record_quota(resp.headers, sport_key)
            logger.info("[ODDS] the-odds-api: %d events for %s", len(events), sport_key)
            return "ok", events
        if status == 404:
            # Unknown or finished sport key: nothing to fetch, not a failure.
            return "ok", []
        if status == 401:
            _last_quota["error"] = _HTTP_ERRORS[401]
            logger.error("[ODDS] the-odds-api rejected the API key (HTTP 401)")
            return "stop", None
        if status == 429:
            _record_quota(resp.headers, sport_key)
            if _quota_exhausted(resp.headers):
                _last_quota["error"] = "Quota exhausted (HTTP 429)"
                return "stop", None
            if backoff:
                wait = backoff.pop(0)
                logger.warning(
                    "[ODDS] the-odds-api burst rate limit on %s (HTTP 429); "
                    "retrying in %.0fs", sport_key, wait,
                )
                time.sleep(wait)
                continue
            _last_quota["error"] = _HTTP_ERRORS[429]
            return "stop", None
        if status in _TRANSIENT_STATUSES and transient_retry:
            transient_retry = False
            time.sleep(2.0)
            continue
        _last_quota["error"] = f"HTTP {status}"
        logger.warning(
            "[ODDS] the-odds-api HTTP %d for %s: %s", status, sport_key, resp.text[:200]
        )
        return "failed", None


def _fetch_from_odds_api(sport_keys: list[str]) -> tuple[dict, str | None]:
    """Fetch leagues one at a time. Returns ({sport_key: events}, last error)."""
    answered: dict[str, list] = {}
    error = None
    consecutive_failures = 0
    for sport_key in sport_keys:
        outcome, events = _odds_api_league(sport_key)
        if outcome == "ok":
            answered[sport_key] = events
            consecutive_failures = 0
            continue
        error = _last_quota["error"]
        if outcome == "stop":
            break
        consecutive_failures += 1
        if consecutive_failures >= _MAX_CONSECUTIVE_LEAGUE_FAILURES:
            logger.warning(
                "[ODDS] the-odds-api failed %d leagues in a row; handing the "
                "rest to the fallback providers", consecutive_failures,
            )
            break
    return answered, error


# ---------------------------------------------------------------------------
# Shared HTTP helpers for the fallback providers
# ---------------------------------------------------------------------------

def _retry_after_seconds(resp) -> float | None:
    """Seconds to wait from a Retry-After header or a JSON retry_after field."""
    raw = resp.headers.get("retry-after")
    if raw is None:
        try:
            body = resp.json()
        except ValueError:
            body = None
        if isinstance(body, dict):
            nested = body.get("error") if isinstance(body.get("error"), dict) else {}
            raw = body.get("retry_after", nested.get("retry_after"))
    try:
        value = float(raw)
    except (TypeError, ValueError):
        return None
    if value > 1e11:        # Unix timestamp in milliseconds (SharpAPI)
        value = value / 1000 - time.time()
    elif value > 1000:      # a duration in milliseconds
        value = value / 1000
    return max(value, 1.0)


def _provider_get(
    provider: str, url: str, params: dict, headers: dict
) -> tuple[object | None, str | None]:
    """GET a fallback-provider endpoint. Returns (json body, None) or (None, error)."""
    transient_retry = True
    rate_limit_retry = True
    while True:
        try:
            resp = requests.get(
                url, params=params, headers=headers,
                timeout=config.ODDS_API_TIMEOUT_SECONDS,
            )
        except requests.RequestException as exc:
            if transient_retry:
                transient_retry = False
                time.sleep(2.0)
                continue
            return None, f"network error: {str(exc)[:160]}"

        status = resp.status_code
        if status == 200:
            try:
                return resp.json(), None
            except ValueError:
                return None, "malformed JSON response"
        if status == 429 and rate_limit_retry:
            wait = _retry_after_seconds(resp) or 5.0
            if wait <= _MAX_RETRY_AFTER_SECONDS:
                rate_limit_retry = False
                logger.warning("[ODDS] %s rate limited; retrying in %.0fs", provider, wait)
                time.sleep(wait)
                continue
        if status in _TRANSIENT_STATUSES and transient_retry:
            transient_retry = False
            time.sleep(2.0)
            continue
        return None, f"HTTP {status}: {resp.text[:200]}"


def _get_paginated(
    provider: str,
    url: str,
    params: dict,
    headers: dict,
    next_cursor: Callable[[dict], str | None],
) -> tuple[list, str | None]:
    """Read every page of a cursor-paginated {"data": [...]} endpoint."""
    items: list = []
    cursor = None
    for _ in range(max(1, config.FALLBACK_MAX_PAGES)):
        page_params = dict(params)
        if cursor:
            page_params["cursor"] = cursor
        body, error = _provider_get(provider, url, page_params, headers)
        if error:
            return items, error
        data = body.get("data") if isinstance(body, dict) else None
        if not isinstance(data, list):
            return items, "unexpected response shape (no data list)"
        items.extend(data)
        cursor = next_cursor(body)
        if not cursor or not data:
            return items, None
    logger.warning(
        "[ODDS] %s: stopped after %d pages; some odds were not read",
        provider, config.FALLBACK_MAX_PAGES,
    )
    return items, None


def _decimal_price(raw) -> float | None:
    """A decimal price from a number or an object like {"decimal": 1.91}."""
    if isinstance(raw, dict):
        for key in ("decimal", "price", "value"):
            if key in raw:
                raw = raw[key]
                break
        else:
            return None
    try:
        price = float(raw)
    except (TypeError, ValueError):
        return None
    return price if price > 1.0 else None


def _american_to_decimal(raw) -> float | None:
    """Convert American odds ("+133" / "-139" / 150) to decimal odds."""
    if raw is None:
        return None
    try:
        value = float(str(raw).strip().replace("+", ""))
    except (TypeError, ValueError):
        return None
    if value >= 100:
        return round(1 + value / 100, 4)
    if value <= -100:
        return round(1 + 100 / abs(value), 4)
    return None


def _parse_line(raw) -> float | None:
    """Parse a spread/total line ("+1.5" / "-1.5" / 3.5) as a float."""
    if raw is None:
        return None
    try:
        return float(str(raw).strip().replace("+", ""))
    except (TypeError, ValueError):
        return None


def _add_outcome(event: dict, book: str, market_key: str, outcome: dict) -> None:
    """Insert an outcome, replacing an earlier one with the same name."""
    bookmaker = next((b for b in event["bookmakers"] if b["key"] == book), None)
    if bookmaker is None:
        bookmaker = {"key": book, "title": book, "markets": []}
        event["bookmakers"].append(bookmaker)
    market = next((m for m in bookmaker["markets"] if m["key"] == market_key), None)
    if market is None:
        market = {"key": market_key, "outcomes": []}
        bookmaker["markets"].append(market)
    market["outcomes"] = [
        o for o in market["outcomes"] if o["name"] != outcome["name"]
    ] + [outcome]


# ---------------------------------------------------------------------------
# Provider 2: SharpAPI
# ---------------------------------------------------------------------------

def _normalize_sharpapi_rows(rows: list) -> list[dict]:
    """
    Rebuild The Odds API style events from SharpAPI /odds rows.

    Each row is one (event, market, selection, sportsbook) price, e.g.
    {"event_id": "33483153", "sport": "soccer", "league": "england_-_premier_league",
     "home_team": ..., "away_team": ..., "market_type": "moneyline",
     "selection": "Arsenal", "selection_type": "home", "odds_decimal": 1.67,
     "line": null, "sportsbook": "draftkings", "is_main_line": true,
     "is_live": false, "event_start_time": "2026-01-26T19:00:00Z"}.
    Live rows and alternate lines are dropped.
    """
    events: dict[str, dict] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict) or row.get("is_live"):
            continue
        if row.get("is_alternate_line") or row.get("is_main_line") is False:
            continue
        market_key = _SHARPAPI_MARKETS.get(str(row.get("market_type", "")).lower())
        event_id = str(row.get("event_id") or "").strip()
        home = str(row.get("home_team") or "").strip()
        away = str(row.get("away_team") or "").strip()
        if not market_key or not event_id or not home or not away:
            continue
        price = (
            _decimal_price(row.get("odds_decimal"))
            or _decimal_price(row.get("odds"))
            or _american_to_decimal(row.get("odds_american"))
        )
        if price is None:
            continue
        side = str(row.get("selection_type") or "").lower()
        name = {
            "home": home, "away": away, "draw": "Draw", "over": "Over", "under": "Under",
        }.get(side) or str(row.get("selection") or "").strip()
        if name.lower() in {"draw", "tie", "x"}:
            name = "Draw"
        if not name:
            continue

        event = events.get(event_id)
        if event is None:
            event = events[event_id] = {
                "id": f"{PROVIDER_SHARPAPI}:{event_id}",
                "source": PROVIDER_SHARPAPI,
                "provider_league": f"{row.get('sport') or ''}/{row.get('league') or ''}",
                "home_team": home,
                "away_team": away,
                "commence_time": str(row.get("event_start_time") or row.get("start_time") or ""),
                "bookmakers": [],
            }
        outcome = {"name": name, "price": price}
        line = _parse_line(row.get("line"))
        if market_key != "h2h" and line is not None:
            outcome["point"] = line
        book = str(row.get("sportsbook") or row.get("book") or row.get("bookmaker") or "unknown")
        _add_outcome(event, book, market_key, outcome)
    return list(events.values())


def _fetch_from_sharpapi(sport_keys: list[str]) -> tuple[dict, str | None]:
    """Fetch every mapped league in one paginated SharpAPI /odds request."""
    wanted: dict[str, str] = {}   # "sport/league" -> first requested sport key
    for sport_key in sport_keys:
        target = _provider_league(sport_key, _SHARPAPI_LEAGUES)
        if target and target not in wanted:
            wanted[target] = sport_key
    if not wanted:
        return {}, "none of the remaining leagues are covered"

    params = {
        "sport": ",".join(sorted({target.split("/")[0] for target in wanted})),
        "league": ",".join(target.split("/", 1)[1] for target in wanted),
        "market": ",".join(_SHARPAPI_MARKETS),
        "is_live": "false",
        "limit": 200,
    }
    rows, error = _get_paginated(
        PROVIDER_SHARPAPI,
        f"{config.SHARPAPI_BASE_URL.rstrip('/')}/odds",
        params,
        {"X-API-Key": config.SHARPAPI_API_KEY},
        lambda body: (body.get("pagination") or {}).get("next_cursor"),
    )
    answered: dict[str, list] = {}
    for event in _normalize_sharpapi_rows(rows):
        target = event["provider_league"]
        if target not in wanted and len(wanted) == 1:
            target = next(iter(wanted))
        sport_key = wanted.get(target)
        if sport_key:
            answered.setdefault(sport_key, []).append(event)
    if not answered and not error:
        error = "no odds returned for the requested leagues"
    return answered, error


# ---------------------------------------------------------------------------
# Provider 3: SportsGameOdds
# ---------------------------------------------------------------------------

def _sgo_team_name(team) -> str | None:
    if not isinstance(team, dict):
        return None
    names = team.get("names") or {}
    return names.get("long") or names.get("medium") or names.get("short") or team.get("name")


def _normalize_sgo_events(events: list) -> list[dict]:
    """
    Rebuild The Odds API style events from a SportsGameOdds /v2/events payload.

    Odds live at event["odds"][oddID], where oddID is
    statID-statEntityID-periodID-betTypeID-sideID (e.g.
    "points-home-reg-ml3way-home", "points-all-reg-ou-over"), with prices per
    bookmaker in byBookmaker (American odds, per-book spread/overUnder lines).
    Full-match "points" markets only: regulation time ("reg") is preferred
    over "game" per bet type, and the 3-way moneyline replaces the 2-way one
    when both exist. Started, live, finished or cancelled events are dropped.
    """
    normalized: list[dict] = []
    for ev in events if isinstance(events, list) else []:
        if not isinstance(ev, dict):
            continue
        status = ev.get("status") or {}
        if any(status.get(flag) for flag in
               ("started", "live", "ended", "completed", "cancelled", "finalized")):
            continue
        teams = ev.get("teams") or {}
        home = _sgo_team_name(teams.get("home"))
        away = _sgo_team_name(teams.get("away"))
        if not home or not away:
            continue

        entries = [
            entry for entry in (ev.get("odds") or {}).values()
            if isinstance(entry, dict)
            and entry.get("statID") == "points"
            and entry.get("statEntityID") in ("home", "away", "all")
            and entry.get("betTypeID") in ("ml", "ml3way", "sp", "ou")
            and entry.get("periodID") in ("game", "reg")
            and entry.get("sideID") in ("home", "away", "draw", "over", "under")
        ]
        has_three_way = any(e.get("betTypeID") == "ml3way" for e in entries)
        periods = {
            bet: "reg" if any(e["betTypeID"] == bet and e["periodID"] == "reg" for e in entries)
            else "game"
            for bet in {e["betTypeID"] for e in entries}
        }

        event = {
            "id": f"{PROVIDER_SPORTSGAMEODDS}:{ev.get('eventID') or ''}",
            "source": PROVIDER_SPORTSGAMEODDS,
            "provider_league": str(ev.get("leagueID") or ""),
            "home_team": home,
            "away_team": away,
            "commence_time": str(status.get("startsAt") or ""),
            "bookmakers": [],
        }
        for entry in entries:
            bet, side = entry["betTypeID"], entry["sideID"]
            if entry["periodID"] != periods[bet] or (bet == "ml" and has_three_way):
                continue
            if bet in ("ml", "ml3way"):
                market_key = "h2h"
                name = {"home": home, "away": away, "draw": "Draw"}.get(side)
            elif bet == "sp":
                market_key = "spreads"
                name = {"home": home, "away": away}.get(side)
            else:
                market_key = "totals"
                name = {"over": "Over", "under": "Under"}.get(side)
            if name is None:
                continue
            for book, quote in (entry.get("byBookmaker") or {}).items():
                if not isinstance(quote, dict) or quote.get("available") is False:
                    continue
                price = _american_to_decimal(quote.get("odds"))
                if price is None:
                    continue
                outcome = {"name": name, "price": price}
                line = (
                    _parse_line(quote.get("spread")) if bet == "sp"
                    else _parse_line(quote.get("overUnder")) if bet == "ou"
                    else None
                )
                if line is not None:
                    outcome["point"] = line
                _add_outcome(event, str(book), market_key, outcome)
        if event["bookmakers"] and ev.get("eventID"):
            normalized.append(event)
    return normalized


def _fetch_from_sportsgameodds(sport_keys: list[str]) -> tuple[dict, str | None]:
    """Fetch today's events for every mapped league in one paginated request."""
    wanted: dict[str, str] = {}   # leagueID -> first requested sport key
    for sport_key in sport_keys:
        league_id = _provider_league(sport_key, _SPORTSGAMEODDS_LEAGUES)
        if league_id and league_id not in wanted:
            wanted[league_id] = sport_key
    if not wanted:
        return {}, "none of the remaining leagues are covered"

    now = datetime.now(timezone.utc)
    end_of_day = datetime.now(LAGOS_TZ).replace(
        hour=23, minute=59, second=59, microsecond=0
    ).astimezone(timezone.utc)
    params = {
        "apiKey": config.SPORTSGAMEODDS_API_KEY,
        "leagueID": ",".join(wanted),
        "oddsAvailable": "true",
        # Only today's upcoming matches: the free tier counts every event returned.
        "startsAfter": now.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "startsBefore": end_of_day.strftime("%Y-%m-%dT%H:%M:%SZ"),
        "limit": 50,
    }
    raw_events, error = _get_paginated(
        PROVIDER_SPORTSGAMEODDS,
        f"{config.SPORTSGAMEODDS_BASE_URL.rstrip('/')}/events",
        params,
        {},
        lambda body: body.get("nextCursor"),
    )
    answered: dict[str, list] = {}
    for event in _normalize_sgo_events(raw_events):
        sport_key = wanted.get(event["provider_league"])
        if sport_key:
            answered.setdefault(sport_key, []).append(event)
    if not answered and not error:
        error = "no odds returned for the requested leagues"
    return answered, error


# ---------------------------------------------------------------------------
# Failover orchestration
# ---------------------------------------------------------------------------

def _providers() -> dict[str, tuple[Callable[[list[str]], tuple[dict, str | None]], bool]]:
    """name -> (fetch function, whether an API key is configured)."""
    return {
        PROVIDER_ODDS_API: (_fetch_from_odds_api, bool(_api_key())),
        PROVIDER_SHARPAPI: (_fetch_from_sharpapi, bool(config.SHARPAPI_API_KEY)),
        PROVIDER_SPORTSGAMEODDS: (
            _fetch_from_sportsgameodds, bool(config.SPORTSGAMEODDS_API_KEY),
        ),
    }


def provider_configured(name: str) -> bool:
    """Whether a provider has an API key set."""
    return _providers().get(name, (None, False))[1]


def _fetch_all_sports_odds() -> dict:
    """
    Fetch today's odds for every configured league, failing over between
    providers. Called once per Lagos day when the cache is first populated.
    Returns a dict mapping sport_key -> list of events (empty when no provider
    had data for that league).

    Raises OddsAPIError when no provider could answer any league, so the
    caller does not mistake a total outage for an empty match day.
    """
    sport_keys, odds_api_problem = _resolve_fetch_plan()
    sports_data: dict[str, list] = {sport_key: [] for sport_key in sport_keys}
    pending = list(sport_keys)
    report: dict = {
        "at": datetime.now(timezone.utc).isoformat(timespec="seconds"),
        "providers": {},
        "sources": {},
    }
    registry = _providers()

    for name in config.ODDS_PROVIDER_ORDER:
        if name not in registry:
            logger.warning("[ODDS] Unknown provider %r in ODDS_PROVIDER_ORDER", name)
            continue
        fetch, configured = registry[name]
        entry = {"configured": configured, "attempted": 0, "answered": 0, "error": None}
        report["providers"][name] = entry
        if not pending:
            continue
        if not configured:
            entry["error"] = "API key not set"
            continue
        if name == PROVIDER_ODDS_API and odds_api_problem:
            entry["error"] = odds_api_problem
            continue

        entry["attempted"] = len(pending)
        logger.info("[ODDS] Trying %s for %d league(s)", name, len(pending))
        try:
            answered, error = fetch(pending)
        except Exception as exc:  # noqa: BLE001 — one provider's bug must not break failover
            logger.exception("[ODDS] %s crashed while fetching odds", name)
            answered, error = {}, f"unexpected error: {exc}"
        entry["answered"] = len(answered)
        entry["error"] = error
        for sport_key, events in answered.items():
            sports_data[sport_key] = events
            report["sources"][sport_key] = name
        pending = [key for key in pending if key not in answered]
        if error:
            logger.warning(
                "[ODDS] %s answered %d league(s); %d left (%s)",
                name, len(answered), len(pending), error,
            )

    _last_fetch_report.clear()
    _last_fetch_report.update(report)

    if sport_keys and not report["sources"]:
        problems = "; ".join(
            f"{name}: {entry['error']}"
            for name, entry in report["providers"].items() if entry["error"]
        )
        raise OddsAPIError(f"No odds provider could supply data ({problems})")

    total_events = sum(len(events) for events in sports_data.values())
    by_provider = Counter(report["sources"].values())
    logger.info(
        "[ODDS] Daily fetch: %d/%d leagues answered (%s), %d raw matches%s",
        len(report["sources"]), len(sport_keys),
        ", ".join(f"{name}: {count}" for name, count in by_provider.items()),
        total_events,
        f"; no data for {', '.join(pending)}" if pending else "",
    )
    return sports_data


def get_odds(sport_key: str, markets: str | None = None) -> list[dict]:
    """
    Return today's events (with bookmaker odds) for one sport.

    Uses the shared daily cache — only the first request of the day
    (Africa/Lagos time) triggers API calls. All subsequent calls (pagination,
    other users, stats) read from the cached dataset. ``markets`` is accepted
    for backward compatibility; the fetched markets come from config.
    Raises OddsAPIError if the daily fetch failed.
    """
    sports_data = ensure_populated(_fetch_all_sports_odds)

    # The dated cache is authoritative for the entire day. Missing keys mean
    # that sport failed or had no data during the daily fetch; do not retry it.
    return sports_data.get(sport_key, [])


def get_cached_sport_keys() -> list[str]:
    """Sport keys in today's dataset (fetching it if needed)."""
    return list(ensure_populated(_fetch_all_sports_odds).keys())
