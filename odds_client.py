"""
Thin client for The Odds API (https://the-odds-api.com).
Uses a shared daily cache (Africa/Lagos timezone) to minimize API calls.
All users share the same daily dataset — only the first request of the day
triggers API calls. Pagination, stats, and subsequent users read from cache.
"""

import requests
import config
import logging
import os
import time
from concurrent.futures import ThreadPoolExecutor, as_completed
from threading import Lock

from daily_cache import ensure_populated

BASE_URL = "https://api.the-odds-api.com/v4"

logger = logging.getLogger(__name__)

# Max concurrent API requests during cache population
_MAX_WORKERS = 4

# Providers that returned 401 (invalid key) or 429 (quota exhausted) during
# the current daily fetch cycle. Checked before every provider attempt and
# reset at the start of each daily fetch, so it only lives as long as the
# daily cache does. Without it, each of the ~13 concurrent sport fetches
# would independently re-attempt a dead primary provider before falling
# through to the next one.
_dead_providers_today: set[str] = set()
_dead_providers_lock = Lock()


def _mark_provider_dead(provider_name: str) -> None:
    """Record that a provider is dead (401/429) for this daily fetch cycle."""
    with _dead_providers_lock:
        if provider_name not in _dead_providers_today:
            _dead_providers_today.add(provider_name)
            logger.warning(
                "Provider %s marked dead for the rest of this daily fetch "
                "(401/429) — remaining sports will skip straight past it.",
                provider_name,
            )


def _is_provider_dead(provider_name: str) -> bool:
    with _dead_providers_lock:
        return provider_name in _dead_providers_today


def _reset_dead_providers() -> None:
    """Clear the dead-provider set (called at the start of each daily fetch)."""
    with _dead_providers_lock:
        _dead_providers_today.clear()


# ---------------------------------------------------------------------------
# Temporary cooldown (Part 8): a provider that times out / 5xx's / cannot be
# reached is skipped for a short window so we do not hammer it on every sport
# fetch, but it automatically becomes eligible again after the cooldown.
# Unlike _dead_providers_today (401/429 = auth/quota, dead for the daily
# cycle), a cooldown is a short, transient, auto-expiring penalty.
# ---------------------------------------------------------------------------
_PROVIDER_COOLDOWN_SECONDS = float(
    os.environ.get("PROVIDER_COOLDOWN_SECONDS", "60")
)
_provider_cooldowns: dict[str, float] = {}   # name -> monotonic expiry
_cooldowns_lock = Lock()


def _cooldown_active(provider_name: str) -> bool:
    with _cooldowns_lock:
        return _provider_cooldowns.get(provider_name, 0.0) > time.monotonic()


def _start_cooldown(provider_name: str) -> None:
    with _cooldowns_lock:
        _provider_cooldowns[provider_name] = (
            time.monotonic() + _PROVIDER_COOLDOWN_SECONDS
        )
    logger.info(
        "[ODDS] %s temporarily unavailable — cooling down for %ds",
        provider_name, int(_PROVIDER_COOLDOWN_SECONDS),
    )


def _clear_cooldown(provider_name: str) -> None:
    """A successful response proves the provider is healthy again."""
    with _cooldowns_lock:
        _provider_cooldowns.pop(provider_name, None)


def _reset_cooldowns() -> None:
    with _cooldowns_lock:
        _provider_cooldowns.clear()

# Last observed API quota info (parsed from response headers).
# Exposed for the admin /status diagnostic.
_last_quota = {
    "remaining": None,
    "used": None,
    "error": None,   # last HTTP error encountered, if any
}


def get_last_quota() -> dict:
    """Return the last observed Odds API quota info from response headers."""
    return dict(_last_quota)


def _record_quota(headers, sport_key: str, ok: bool) -> None:
    """Parse and log Odds API quota headers from a response."""
    remaining = headers.get("x-requests-remaining")
    used = headers.get("x-requests-used")
    if remaining is not None:
        _last_quota["remaining"] = remaining
        _last_quota["used"] = used
        try:
            if int(remaining) == 0:
                logger.critical(
                    "[CRITICAL] Odds API quota exhausted (0 requests remaining) "
                    "after fetching %s. Bot cannot fetch new odds until the "
                    "quota resets. Used: %s", sport_key, used,
                )
                return
        except (TypeError, ValueError):
            pass
        logger.info(
            "Odds API quota after %s: %s requests remaining, %s used",
            sport_key, remaining, used,
        )


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
    Verify the ODDS_API_KEY against the /sports endpoint (cheap, no quota
    consumed for invalid keys) and return a diagnostic dict:
    {configured, valid, remaining, used, error}
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
        _record_quota(resp.headers, "api-key-check", resp.status_code == 200)
        result["remaining"] = resp.headers.get("x-requests-remaining")
        result["used"] = resp.headers.get("x-requests-used")
        if resp.status_code == 200:
            result["valid"] = True
        elif resp.status_code == 401:
            result["error"] = "Invalid API key (HTTP 401)"
        elif resp.status_code == 429:
            result["error"] = "Quota exhausted (HTTP 429)"
        else:
            result["error"] = f"HTTP {resp.status_code}: {resp.text[:120]}"
    except requests.RequestException as e:
        result["error"] = f"Network error: {e}"
    return result


class OddsAPIError(Exception):
    pass


def _api_key() -> str:
    """Return the configured API key without shell/ dotenv quoting noise."""
    return str(config.ODDS_API_KEY).strip().strip('"\'').strip()


def _provider_key(provider: dict) -> str | None:
    """Resolve the API key for a provider from the environment."""
    env_var = provider.get("api_key_env", "ODDS_API_KEY")
    key = os.environ.get(env_var, "").strip().strip('"\'').strip()
    return key or None


# Mapping of SharpAPI (flat row) market types back to the-odds-api-style
# market keys the pipeline expects ("h2h", "spreads", "totals"). Exact-match
# on the full market_type string; prop/derived types (team_total_goals etc.)
# deliberately do NOT match any alias and are skipped.
_SHARPAPI_MARKET_ALIASES = {
    "moneyline": "h2h",
    "match_winner": "h2h",
    "h2h": "h2h",
    "point_spread": "spreads",
    "spread": "spreads",
    "handicap": "spreads",
    "asian_handicap": "handicap",
    "total_goals": "totals",     # soccer O/U (verified live catalog id)
    "total_points": "totals",    # US-sports O/U catalog id
    "over_under": "totals",
    "totals": "totals",
}


def _split_sport_key(sport_key: str) -> tuple[str, str]:
    """
    Split a the-odds-api-style sport key (e.g. ``soccer_epl``) into
    (sport, league) for providers that filter by sport + league instead of
    exposing a per-sport odds path (SharpAPI v1).
    """
    if "_" in sport_key:
        sport, _, league = sport_key.partition("_")
        return sport, league
    return sport_key, sport_key


def _build_provider_request(
    sport_key: str, provider: dict, api_key: str
) -> tuple[str, dict, dict]:
    """
    Build (url, params, headers) for a provider-specific odds request.

    the-odds-api-style providers use GET /sports/{sport_key}/odds with the key
    in the "apiKey" query param. SharpAPI v1 uses GET /odds with an X-API-Key
    header plus sport/league/market filters, because it has no per-sport odds
    path — see https://docs.sharpapi.io (base URL .../api/v1, endpoint /odds).
    """
    base_url = provider["base_url"].rstrip("/")
    path = provider.get("odds_path", "/sports/{sport_key}/odds")
    params: dict = {}
    headers: dict = {}

    if provider.get("sport_key_style") == "sgo":
        # SportsGameOdds v2: league-wide snapshot, GET /v2/events?leagueID=...
        # (free tier requires a leagueID). oddsAvailable=true limits to
        # live/upcoming events with odds; oddIDs slims the payload to the
        # full-game main markets the pipeline consumes (ml3way = soccer 1X2,
        # sp = spreads, ou = totals; both "game" and "reg" periods — the
        # normalizer prefers "reg" for soccer where both exist).
        league_map = provider.get("league_map", {})
        league_id = league_map.get(sport_key, sport_key)
        entities = ("home", "away", "all")
        periods = ("game", "reg")
        bets = (
            "ml3way-home", "ml3way-away", "ml3way-draw",
            "ml-home", "ml-away",
            "sp-home", "sp-away", "ou-over", "ou-under",
        )
        odd_ids = ",".join(
            f"points-{entity}-{period}-{bet}"
            for entity in entities
            for period in periods
            for bet in bets
        )
        params["apiKey"] = api_key
        params["leagueID"] = league_id
        params["oddsAvailable"] = "true"
        params["oddIDs"] = odd_ids
        params["limit"] = provider.get("page_limit", 50)
        return f"{base_url}{path}", params, headers

    if "sport_key_style" in provider:
        # Flat snapshot endpoint (SharpAPI): filter by sport/league/market.
        sport, league = _split_sport_key(sport_key)
        params["sport"] = sport
        params["league"] = league
        market_map = provider.get("market_map", {})
        mapped_markets = [
            market_map.get(m, m) for m in config.ODDS_MARKETS.split(",")
        ]
        params[provider.get("market_param", "market")] = ",".join(mapped_markets)
        params[provider.get("odds_format_param", "odds_format")] = "decimal"
        if provider.get("auth_mode", "query") == "header":
            headers["X-API-Key"] = api_key
        else:
            params["api_key"] = api_key
        return f"{base_url}{path}", params, headers

    # the-odds-api-style per-sport endpoint
    url = f"{base_url}{path.format(sport_key=sport_key)}"
    params["apiKey"] = api_key
    params["regions"] = config.ODDS_REGIONS
    params["markets"] = config.ODDS_MARKETS
    params["oddsFormat"] = "decimal"
    return url, params, headers


def _decimal_price(raw) -> float | None:
    """Extract a decimal price from a SharpAPI odds value.

    SharpAPI rows may carry odds as a plain number or as an object like
    {"american": -110, "decimal": 1.91}. Tolerant to both.
    """
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
    return price if price >= 1.01 else None


def _normalize_sharpapi_rows(rows) -> list[dict]:
    """
    Rebuild the-odds-api-style events from SharpAPI flat odds rows.

    SharpAPI /odds returns one row per (event, market, selection, book), e.g.
    {"event_name": "Celtics @ Lakers", "market_type": "moneyline",
     "selection": "Lakers", "book": "draftkings", "odds": {...}}.
    Group those rows into events -> bookmakers -> markets -> outcomes so the
    prediction pipeline can consume the response unchanged.
    """
    events_by_key: dict[str, dict] = {}
    for row in rows if isinstance(rows, list) else []:
        if not isinstance(row, dict):
            continue
        # Verified live: rows carry is_active / is_impossible_scoreline flags.
        if row.get("is_active") is False or row.get("is_impossible_scoreline"):
            continue
        # Pre-match bot: exclude live/in-play rows outright (is_live is a real
        # boolean field verified in live responses). A live row such as
        # "Man Utd @ 101.0" must never enter the prediction pipeline.
        if row.get("is_live"):
            continue
        # Alternate-line protection: even among rows that slip past the live
        # filter, never mix alternate lines (e.g. Over 3.5 @ 81 next to
        # Over 2.5 @ 2.98) that share a market key and would corrupt any
        # consensus/average computed across outcomes.
        if row.get("is_main_line") is False and row.get("is_live"):
            continue
        # Verified live: rows carry no event_name — identify events by
        # event_id (or home/away pair) instead.
        home_name = str(row.get("home_team") or "").strip()
        away_name = str(row.get("away_team") or "").strip()
        event_name = str(
            row.get("event_name") or row.get("matchup") or row.get("event") or ""
        ).strip()
        event_id = str(row.get("event_id") or row.get("id") or "")
        if not event_id and not (home_name or away_name or event_name):
            continue
        if not event_name:
            event_name = " vs ".join(x for x in (home_name, away_name) if x)
        event = events_by_key.get(event_id)
        if event is None:
            home_team, _, away_team = str(
                row.get("home_team") or event_name
            ).partition(" @ ")
            away = row.get("away_team") or (away_team or None)
            events_by_key[event_id] = {
                "id": event_id,
                "sport_key": row.get("sport_key") or row.get("sport") or "",
                "sport_title": (
                    (row.get("league_ref") or {}).get("label")
                    or row.get("league")
                    or ""
                ),
                "commence_time": (
                    row.get("event_start_time")
                    or row.get("start_time")
                    or row.get("commence_time")
                    or ""
                ),
                "home_team": row.get("home_team") or home_team or "Unknown",
                "away_team": row.get("away_team") or away or "Unknown",
                "bookmakers": [],
            }
            event = events_by_key[event_id]
        market_key = _SHARPAPI_MARKET_ALIASES.get(
            str(row.get("market_type", "")).lower(), ""
        )
        if not market_key:
            continue
        book_key = str(
            row.get("book") or row.get("bookmaker")
            or row.get("sportsbook") or "unknown"
        )
        bookmaker = next(
            (b for b in event["bookmakers"] if b["key"] == book_key), None
        )
        if bookmaker is None:
            bookmaker = {"key": book_key, "title": book_key, "markets": []}
            event["bookmakers"].append(bookmaker)
        market = next(
            (m for m in bookmaker["markets"] if m["key"] == market_key), None
        )
        if market is None:
            market = {"key": market_key, "outcomes": []}
            bookmaker["markets"].append(market)
        price = _decimal_price(
            row.get("price")
            if row.get("price") is not None
            else (row.get("odds") if row.get("odds") is not None else row.get("odds_decimal"))
        )
        selection = row.get("selection") or row.get("name")
        point = (
            row.get("point") if row.get("point") is not None else row.get("line")
        )
        if selection is None or price is None:
            continue
        outcome = {"name": str(selection), "price": price}
        if point is not None:
            try:
                outcome["point"] = float(point)
            except (TypeError, ValueError):
                pass
        market["outcomes"].append(outcome)
    return list(events_by_key.values())


def _american_to_decimal(raw) -> float | None:
    """Convert an American odds string ("+133"/"-139") to decimal odds."""
    if raw is None:
        return None
    try:
        a = float(str(raw).strip().replace("+", ""))
    except (TypeError, ValueError):
        return None
    if a >= 100:
        return round(1 + a / 100, 4)
    if a <= -100:
        return round(1 + 100 / abs(a), 4)
    return None


def _parse_line(raw) -> float | None:
    """Parse a spread/total line value ("+1.5"/"-1.5"/"3.5") as a float."""
    if raw is None:
        return None
    try:
        return float(str(raw).strip().replace("+", ""))
    except (TypeError, ValueError):
        return None


def _normalize_sgo_events(events) -> list[dict]:
    """
    Rebuild the-odds-api-style events from a SportsGameOdds /v2/events
    payload (verified live, Sept 2026):

      event = {
        "eventID": ..., "status": {"started": false, "live": false,
        "startsAt": "2026-10-13T16:45:00.000Z", ...},
        "teams": {"home": {"names": {"long": "RC Lens", ...}}, ...},
        "odds": {
          "points-all-reg-ml3way-home": {
            "statID": "points", "statEntityID": "home", "periodID": "reg",
            "betTypeID": "ml3way", "sideID": "home",
            "byBookmaker": {"fanduel": {"odds": "+133", "available": true,
                             "spread": "-1.5", "overUnder": "3.5", ...}},
          }, ...
        }
      }

    Soccer 1X2 lives at periodID "reg" (regulation); totals also exist at
    "game". When both periods offer the same bet type, "reg" wins so the
    same market key never mixes two different lines. Live/ended/cancelled
    events are excluded (pre-match bot). Combined ml3way sides
    (home+draw / not_draw) are skipped — the pipeline expects single-sided
    outcomes only.
    """
    events_out: list[dict] = []
    for ev in events if isinstance(events, list) else []:
        if not isinstance(ev, dict):
            continue
        status = ev.get("status") or {}
        if (status.get("started") or status.get("live")
                or status.get("ended") or status.get("cancelled")
                or status.get("completed")):
            continue
        teams = ev.get("teams") or {}
        home_names = (teams.get("home") or {}).get("names") or {}
        away_names = (teams.get("away") or {}).get("names") or {}
        home_name = home_names.get("long") or home_names.get("medium")
        away_name = away_names.get("long") or away_names.get("medium")
        if not home_name or not away_name:
            continue

        # First pass: bucket full-game entries by (betTypeID, sideID) so a
        # "reg" entry always beats a "game" entry for the same market.
        buckets: dict[tuple, dict] = {}
        for entry in (ev.get("odds") or {}).values():
            if not isinstance(entry, dict):
                continue
            bet = entry.get("betTypeID")
            if bet not in ("ml", "ml3way", "sp", "ou"):
                continue
            period = entry.get("periodID")
            if period not in ("game", "reg"):
                continue
            if entry.get("statEntityID") not in ("home", "away", "all"):
                continue
            side = entry.get("sideID")
            if side in ("home+draw", "away+draw", "not_draw"):
                continue  # combined sides — not pipeline outcomes
            key = (bet, side)
            existing = buckets.get(key)
            if existing is None or period == "reg":
                buckets[key] = entry

        bookmakers: dict[str, dict] = {}
        for (bet, side), entry in buckets.items():
            if bet in ("ml", "ml3way"):
                market_key = "h2h"
                outcome_name = (
                    "Draw" if side == "draw"
                    else home_name if side == "home"
                    else away_name if side == "away"
                    else None
                )
                point = None
            elif bet == "sp":
                market_key = "spreads"
                outcome_name = home_name if side == "home" else (
                    away_name if side == "away" else None
                )
                point = _parse_line(entry.get("spread"))
            else:  # ou
                market_key = "totals"
                outcome_name = "Over" if side == "over" else (
                    "Under" if side == "under" else None
                )
                point = _parse_line(entry.get("overUnder"))
            if outcome_name is None:
                continue
            for book_id, row in (entry.get("byBookmaker") or {}).items():
                if not isinstance(row, dict) or row.get("available") is not True:
                    continue
                price = _american_to_decimal(row.get("odds"))
                if price is None:
                    continue
                # Spread/total lines are per-bookmaker fields in SGO.
                line = (_parse_line(row.get("spread")) if bet == "sp"
                        else _parse_line(row.get("overUnder")) if bet == "ou"
                        else None)
                bookmaker = bookmakers.get(book_id)
                if bookmaker is None:
                    bookmaker = {"key": book_id, "title": book_id,
                                 "markets": []}
                    bookmakers[book_id] = bookmaker
                market = next(
                    (m for m in bookmaker["markets"] if m["key"] == market_key),
                    None,
                )
                if market is None:
                    market = {"key": market_key, "outcomes": []}
                    bookmaker["markets"].append(market)
                outcome = {"name": outcome_name, "price": price}
                if line is not None:
                    outcome["point"] = line
                market["outcomes"].append(outcome)

        if not bookmakers:
            continue
        events_out.append({
            "id": str(ev.get("eventID") or ""),
            "sport_key": str(ev.get("sportID") or "").lower(),
            "sport_title": str(ev.get("leagueID") or ""),
            "commence_time": str(status.get("startsAt") or ""),
            "home_team": home_name,
            "away_team": away_name,
            "bookmakers": list(bookmakers.values()),
        })
    return events_out


def _normalize_provider_response(provider: dict, payload) -> list[dict]:
    """
    Convert a provider's raw JSON body into a the-odds-api-style events list.
    the-odds-api-style providers already return that shape; SharpAPI's flat
    "data" rows need to be rebuilt into events.
    """
    wrapper = provider.get("response_wrapper")
    if provider.get("sport_key_style") == "sgo":
        data = payload.get("data") if isinstance(payload, dict) else None
        return _normalize_sgo_events(data)
    if wrapper is None:
        return payload if isinstance(payload, list) else []
    rows = payload.get(wrapper) if isinstance(payload, dict) else None
    return _normalize_sharpapi_rows(rows)


def _fetch_single_sport_from_provider(
    sport_key: str, provider: dict, api_key: str
) -> tuple:
    """
    Fetch odds for a single sport key from a specific provider.
    Returns (sport_key, events_list) or (sport_key, None) on failure.

    Failure classification:
      * 200 with usable odds -> success; cooldown cleared (healthy again).
      * 200 with no usable odds / malformed JSON -> schema problem; NO retry.
      * 401 / 429 -> auth / quota; marked dead for the daily cycle (no retry).
      * 403 / 404 -> permission / endpoint problem; NO retry.
      * 408, 5xx, timeouts, connection errors -> transient; retried with
        backoff (0s, 1s, 3s). If every attempt fails, the provider enters a
        short auto-expiring cooldown so later sports skip it.
    """
    provider_name = provider["name"]
    url, params, headers = _build_provider_request(sport_key, provider, api_key)

    for attempt, backoff in enumerate((0.0, 1.0, 3.0)):
        if backoff:
            time.sleep(backoff)
        try:
            resp = requests.get(
                url,
                params=params,
                headers=headers,
                timeout=config.ODDS_API_TIMEOUT_SECONDS,
            )

            if resp.status_code == 200:
                try:
                    payload = resp.json()
                except ValueError:
                    logger.warning(
                        "[ODDS] %s returned malformed JSON for %s "
                        "(schema problem, not retrying)",
                        provider_name, sport_key,
                    )
                    return sport_key, None
                events = _normalize_provider_response(provider, payload)
                if not events:
                    logger.warning(
                        "[ODDS] %s returned 200 for %s but no usable odds "
                        "(empty/unparseable response, not retrying)",
                        provider_name, sport_key,
                    )
                    return sport_key, None
                logger.info(
                    "[ODDS] %s returned %d usable events for %s",
                    provider_name, len(events), sport_key,
                )
                _record_quota(resp.headers, sport_key, True)
                _clear_cooldown(provider_name)
                return sport_key, events

            elif resp.status_code == 401:
                logger.warning(
                    "[ODDS] %s AUTH FAILED for %s (HTTP 401) — not retrying",
                    provider_name, sport_key,
                )
                _mark_provider_dead(provider_name)
                return sport_key, None

            elif resp.status_code == 403:
                logger.warning(
                    "[ODDS] %s PERMISSION DENIED for %s (HTTP 403) — "
                    "not retrying", provider_name, sport_key,
                )
                return sport_key, None

            elif resp.status_code == 404:
                logger.warning(
                    "[ODDS] %s ENDPOINT NOT FOUND for %s (HTTP 404) — "
                    "not retrying", provider_name, sport_key,
                )
                return sport_key, None
            elif resp.status_code == 429:
                # Respect a short Retry-After; otherwise treat as quota
                # exhaustion for today and stop hammering.
                retry_after = resp.headers.get("retry-after")
                try:
                    wait_s = float(retry_after) if retry_after else None
                except (TypeError, ValueError):
                    wait_s = None
                if wait_s is not None and 0 < wait_s <= 5 and attempt < 2:
                    logger.info(
                        "[ODDS] %s rate limited (429) for %s — retrying "
                        "after %.0fs per Retry-After",
                        provider_name, sport_key, wait_s,
                    )
                    time.sleep(wait_s)
                    continue
                logger.warning(
                    "[ODDS] %s RATE LIMITED for %s (HTTP 429) — marking "
                    "dead for today", provider_name, sport_key,
                )
                _record_quota(resp.headers, sport_key, False)
                _mark_provider_dead(provider_name)
                return sport_key, None

            elif resp.status_code in (408, 500, 502, 503, 504):
                # Transient server problem — retry with backoff.
                logger.warning(
                    "[ODDS] %s returned HTTP %d for %s (attempt %d/3) — "
                    "transient, may retry",
                    provider_name, resp.status_code, sport_key, attempt + 1,
                )
                continue

            else:
                logger.warning(
                    "[ODDS] %s returned HTTP %d for %s — not retrying",
                    provider_name, resp.status_code, sport_key,
                )
                return sport_key, None

        except requests.exceptions.SSLError as e:
            logger.warning(
                "[ODDS] %s TLS/SSL failure for %s: %s",
                provider_name, sport_key, str(e)[:160],
            )
            break   # TLS problems are rarely transient — skip the retries

        except requests.exceptions.Timeout:
            logger.warning(
                "[ODDS] %s request timed out for %s (attempt %d/3)",
                provider_name, sport_key, attempt + 1,
            )
            continue

        except requests.exceptions.ConnectionError as e:
            logger.warning(
                "[ODDS] %s connection failed for %s (attempt %d/3): %s",
                provider_name, sport_key, attempt + 1, str(e)[:160],
            )
            continue

        except requests.RequestException as e:
            logger.warning(
                "[ODDS] %s request failed for %s: %s",
                provider_name, sport_key, str(e)[:200],
            )
            break

        except ValueError as e:
            logger.warning(
                "[ODDS] %s unexpected response for %s: %s",
                provider_name, sport_key, str(e)[:160],
            )
            return sport_key, None

    # Every attempt failed with a transient problem — cool the provider down
    # so the remaining sport fetches skip it instead of hammering it.
    _start_cooldown(provider_name)
    return sport_key, None


def _fetch_single_sport(sport_key: str) -> tuple:
    """
    Fetch odds for a single sport key, trying providers in order:
    The Odds API -> SportGameOdds -> SharpAPI. A provider is skipped when it
    is marked dead for the daily cycle (401/429) or inside its short
    auto-expiring cooldown (transient outage). A provider that returns usable
    odds wins outright — providers are NOT merged, and we do not fall through
    merely because one optional market is missing.
    """
    for provider in config.ODDS_API_PROVIDERS:
        if _is_provider_dead(provider["name"]):
            logger.info(
                "[ODDS] Skipping %s (marked dead for today)", provider["name"]
            )
            continue
        if _cooldown_active(provider["name"]):
            logger.info("[ODDS] Skipping %s (in cooldown)", provider["name"])
            continue
        api_key = _provider_key(provider)
        if not api_key:
            continue
        logger.info("[ODDS] Trying %s for %s", provider["name"], sport_key)
        key, events = _fetch_single_sport_from_provider(
            sport_key, provider, api_key
        )
        if events:
            logger.info("[ODDS] Active provider: %s", provider["name"])
            return key, events
        logger.info("[ODDS] Falling back from %s", provider["name"])

    logger.warning("[ODDS] All providers failed for %s", sport_key)
    return sport_key, None


def _fetch_all_sports_odds() -> dict:
    """
    Fetch odds for all configured sports from the API concurrently.
    Called only once per day (Lagos time) when the cache is first populated.
    Returns a dict mapping sport_key -> list of events.

    Raises OddsAPIError when EVERY sport failed (e.g. invalid API key or
    quota exhausted) so the caller does not mistake a total outage for a
    legitimately empty match day.

    Uses ThreadPoolExecutor for concurrent requests — fetches up to 4 sports
    simultaneously, reducing total fetch time from ~13s to ~4s for 13 sports.
    """
    # Import here to avoid circular imports at module load time
    from predictions import SPORT_KEY_MAP

    # A new daily fetch means a fresh chance for every provider — clear any
    # 401/429 marks and transient cooldowns from the previous day's fetch.
    _reset_dead_providers()
    _reset_cooldowns()

    # Collect all unique sport keys from the predictions config
    all_sport_keys = []
    for sport_name, leagues in SPORT_KEY_MAP.items():
        for league in leagues:
            key = league["key"]
            if key not in all_sport_keys:
                all_sport_keys.append(key)

    # Keep failed sports in the cache as empty lists so they are not retried
    # by later user requests on the same day.
    sports_data = {sport_key: [] for sport_key in all_sport_keys}

    # Fetch sports concurrently using ThreadPoolExecutor
    with ThreadPoolExecutor(max_workers=_MAX_WORKERS) as executor:
        future_to_key = {
            executor.submit(_fetch_single_sport, sport_key): sport_key
            for sport_key in all_sport_keys
        }

        for future in as_completed(future_to_key):
            sport_key = future_to_key[future]
            try:
                _, events = future.result()
            except (AttributeError, KeyError, TypeError, ValueError) as exc:
                logger.warning("Unexpected response for %s: %s", sport_key, exc)
                events = None
            if events is not None:
                sports_data[sport_key] = events

    succeeded = sum(1 for events in sports_data.values() if events)
    if succeeded == 0:
        raise OddsAPIError(
            "All sport fetches failed — likely an invalid ODDS_API_KEY (401) "
            "or exhausted API quota (429). Not caching this empty result."
        )

    total_events = sum(len(events) for events in sports_data.values())
    regions = len(config.ODDS_REGIONS.split(","))
    markets = len(config.ODDS_MARKETS.split(","))
    credits_used = regions * markets * len(all_sport_keys)
    logger.info(
        "Fetched odds summary: %d/%d sports returned data, "
        "%d total raw matches. Credit cost: %d regions x %d markets x "
        "%d leagues = %d credits this fetch.",
        succeeded, len(all_sport_keys), total_events,
        regions, markets, len(all_sport_keys), credits_used,
    )
    return sports_data


def get_sports():
    """Return the list of all sport keys currently in season."""
    resp = requests.get(
        f"{BASE_URL}/sports",
        params={"apiKey": _api_key()},
        timeout=15,
    )
    if resp.status_code != 200:
        raise OddsAPIError(
            f"Failed to fetch sports list: {resp.status_code} {resp.text}"
        )
    return resp.json()


def get_odds(sport_key, markets="h2h"):
    """
    Fetch current odds for a sport.
    markets: 'h2h' (moneyline/win-draw-win), 'spreads', or 'totals'
    Returns a list of events, each with bookmaker odds.

    Uses the shared daily cache — only the first request of the day
    (Africa/Lagos time) triggers API calls. All subsequent calls
    (pagination, other users, stats) read from the cached dataset.
    """
    # Ensure the daily cache is populated (thread-safe, only fetches once per day)
    sports_data = ensure_populated(_fetch_all_sports_odds)

    # The dated cache is authoritative for the entire day. Missing keys mean
    # that sport failed or had no data during the daily fetch; do not retry it.
    return sports_data.get(sport_key, [])
