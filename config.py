"""
Config for the sports predictions bot.
Everything sensitive comes from environment variables — never hardcode keys here.
"""

import os
from pathlib import Path
from dotenv import load_dotenv

# Load .env file from project root if it exists
env_path = Path(__file__).resolve().parent / ".env"
if env_path.exists():
    load_dotenv(env_path)

# Get this from @BotFather on Telegram
TELEGRAM_BOT_TOKEN = os.environ.get("TELEGRAM_BOT_TOKEN")
if not TELEGRAM_BOT_TOKEN:
    raise SystemExit("Set TELEGRAM_BOT_TOKEN environment variable first.")

# Get this from https://the-odds-api.com
ODDS_API_KEY = os.environ.get("ODDS_API_KEY")
if not ODDS_API_KEY:
    raise SystemExit("Set ODDS_API_KEY environment variable first.")

# ---------------------------------------------------------------------------
# Multi-provider odds client (with automatic failover)
# ---------------------------------------------------------------------------
# Length-1 list still works exactly like the single-provider setup today.
# Add sportgameodds and sharpapi keys below; leave any out to disable that
# provider. Each provider is tried in list order for every league; if one
# returns a rate-limit/quota-exceeded error or times out, the next is tried
# immediately. Providers that fail for a whole run are skipped for the rest
# of today's scan so we do not keep retrying a dead/slow provider on every
# league.
ODDS_REGIONS = os.environ.get("ODDS_REGIONS", "uk")
ODDS_MARKETS = os.environ.get("ODDS_MARKETS", "h2h,spreads,totals")
ODDS_API_TIMEOUT_SECONDS = float(
    os.environ.get("ODDS_API_TIMEOUT_SECONDS", "6")
)

ODDS_API_PROVIDERS: list[dict] = [
    {
        "name": "the-odds-api",
        "base_url": "https://api.the-odds-api.com/v4",
        "api_key_env": "ODDS_API_KEY",
        # the-odds-api style: GET /sports/{sport_key}/odds?apiKey=...
        "auth_mode": "query",           # key passed as the "apiKey" query param
        "odds_path": "/sports/{sport_key}/odds",
        "response_wrapper": None,       # response body IS the events list
    },
    {
        "name": "sharpapi",
        # SharpAPI v1 (verified against https://docs.sharpapi.io, Sept 2026):
        # base URL is https://api.sharpapi.io/api/v1 and the odds snapshot is
        # GET /odds — NOT the-odds-api-style /sports/{key}/odds. Auth uses the
        # X-API-Key header (an api_key query param also works). The /odds
        # endpoint returns a flat, paginated list of odds rows under a
        # "data" key, filtered by sport/league/market, so odds_client
        # rebuilds the-odds-api-style event/bookmaker/market structure from
        # those rows.
        "base_url": os.environ.get(
            "SHARPAPI_BASE_URL",
            "https://api.sharpapi.io/api/v1",
        ),
        "api_key_env": "SHARPAPI_API_KEY",
        "auth_mode": "header",          # X-API-Key header, not apiKey query param
        "odds_path": "/odds",           # flat snapshot endpoint (no per-sport path)
        "sport_key_style": "split",     # soccer_epl -> sport=soccer, league=epl
        "market_param": "market",       # market values differ from the-odds-api
        # Soccer market ids verified live against GET /api/v1/markets:
        # moneyline / point_spread / total_goals ("Total Goals", the standard
        # Over/Under). NOTE: "total_points" is the catalog id for US sports —
        # for soccer it returns ZERO rows.
        "market_map": {
            "h2h": "moneyline",
            "spreads": "point_spread",
            "totals": "total_goals",
        },
        "odds_format_param": "odds_format",
        "response_wrapper": "data",     # odds rows live under the "data" key
    },    {
        "name": "sportgameodds",
        # SportsGameOdds v2 (verified against https://sportsgameodds.com/docs,
        # Sept 2026 — NOTE the domain is sportsgameodds.com, NOT the
        # misspelled "sportgameodds.com" previously configured, whose server
        # never answered). Soccer odds snapshot is GET /v2/events with
        # leagueID filter (free tier REQUIRES a leagueID/eventID). Auth via
        # the apiKey query param (x-api-key header also works). Response is
        # a {"data": [...]} envelope of event objects whose odds live at
        # odds.<oddID>.byBookmaker.<bookmakerID> with American-format odds.
        # Free tier: 2,500 objects/month, 10 req/min, only 8 leagues
        # (soccer: UEFA_CHAMPIONS_LEAGUE + MLS; EPL needs a paid tier).
        "base_url": os.environ.get(
            "SPORTSGAMEODDS_BASE_URL",
            "https://api.sportsgameodds.com/v2",
        ),
        "api_key_env": "SPORTGAMEODDS_API_KEY",
        "auth_mode": "query",
        "odds_path": "/events",
        "sport_key_style": "sgo",       # sport key -> leagueID mapping below
        "league_map": {
            "soccer_uefa_champs_league": "UEFA_CHAMPIONS_LEAGUE",
            "soccer_usa_mls": "MLS",
            "soccer_epl": "EPL",
        },
        "response_wrapper": "data",
    },

]

# Bot Configuration
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0").strip() or "0")

# Number of top picks to show in /dailypick and daily broadcast (3-5 recommended)
DAILY_PICK_COUNT = int(os.environ.get("DAILY_PICK_COUNT", "5"))

# Daily broadcast time in 24-hour format (Africa/Lagos timezone)
DAILY_BROADCAST_TIME = os.environ.get("DAILY_BROADCAST_TIME", "09:00")

# Prediction Quality Thresholds
# Minimum probability for a standard prediction (50%)
MIN_PROBABILITY = float(os.environ.get("MIN_PROBABILITY", "0.50"))

# Absolute floor for fallback predictions on low-volume days (40%)
FALLBACK_PROBABILITY_FLOOR = float(os.environ.get("FALLBACK_PROBABILITY_FLOOR", "0.40"))

# Minimum matches meeting MIN_PROBABILITY before fallback kicks in
MIN_MATCHES_FOR_FULL_QUALITY = int(os.environ.get("MIN_MATCHES_FOR_FULL_QUALITY", "5"))

# Maximum predictions per day
MAX_DAILY_PREDICTIONS = int(os.environ.get("MAX_DAILY_PREDICTIONS", "20"))

# Market diversity selection (used by predictions._select_diverse_predictions)
# Total cap for the final daily list.
MAX_TOTAL_PREDICTIONS = int(os.environ.get("MAX_TOTAL_PREDICTIONS", "20"))

# Maximum selections per market category in the final daily list.
# Not quotas: a category with fewer qualifying predictions simply contributes
# fewer. Never invents predictions to satisfy these limits.
MAX_DOUBLE_CHANCE = int(os.environ.get("MAX_DOUBLE_CHANCE", "4"))
MAX_OVER_UNDER = int(os.environ.get("MAX_OVER_UNDER", "5"))
MAX_BTTS = int(os.environ.get("MAX_BTTS", "4"))
MAX_1X2 = int(os.environ.get("MAX_1X2", "3"))
MAX_HANDICAP = int(os.environ.get("MAX_HANDICAP", "3"))
MAX_DRAW_NO_BET = int(os.environ.get("MAX_DRAW_NO_BET", "2"))
MAX_OTHER = int(os.environ.get("MAX_OTHER", "2"))

MARKET_SELECTION_LIMITS = {
    "double_chance": MAX_DOUBLE_CHANCE,
    "over_under": MAX_OVER_UNDER,
    "btts": MAX_BTTS,
    "1x2": MAX_1X2,
    "handicap": MAX_HANDICAP,
    "draw_no_bet": MAX_DRAW_NO_BET,
    "other": MAX_OTHER,
}

# Internal ranking adjustment so Double Chance (naturally high probability)
# does not automatically dominate the list. Only used for ranking — the
# displayed probability is never altered.
MARKET_DIVERSITY_MULTIPLIERS = {
    "double_chance": 0.95,
    "over_under": 1.00,
    "btts": 1.02,
    "1x2": 1.03,
    "handicap": 1.02,
    "draw_no_bet": 1.01,
    "other": 1.00,
}

# Per-market minimum probability floors, layered ON TOP of MIN_PROBABILITY,
# MARKET_SELECTION_LIMITS (quotas) and MARKET_DIVERSITY_MULTIPLIERS (ranking
# nudges). A candidate must clear its own market category's floor before it
# is eligible for the quota/diversity selection at all. This stops a weak
# pick in a naturally-high-scoring market (e.g. Double Chance) from slipping
# through just because that market's probabilities run high on average.
# Applied on the standard (non-low-volume) path; the low-volume fallback
# rule (down to FALLBACK_PROBABILITY_FLOOR) is deliberately untouched.
MARKET_MIN_PROBABILITY_FLOORS = {
    "double_chance": 0.82,
    "draw_no_bet": 0.75,
    "1x2": 0.68,
    "over_under": 0.65,
    "handicap": 0.65,
    "btts": 0.65,
    "other": 0.65,
}

# Minimum bookmakers required for consensus
MIN_BOOKMAKERS = int(os.environ.get("MIN_BOOKMAKERS", "3"))

# ---------------------------------------------------------------------------
# Exact odds targets & probability brackets
# ---------------------------------------------------------------------------
# TOP PICKS: 2-3 legs, combined odds target EXACTLY 2.00 (±0.05 tolerance).
# Probability fallback cascade: 70-90% → 60-70% → 50-60%.
TOP_PICK_TARGET_ODDS = float(os.environ.get("TOP_PICK_TARGET_ODDS", "2.00"))
TOP_PICK_TOLERANCE = float(os.environ.get("TOP_PICK_TOLERANCE", "0.05"))
TOP_PICK_MIN_LEGS = int(os.environ.get("TOP_PICK_MIN_LEGS", "2"))
TOP_PICK_MAX_LEGS = int(os.environ.get("TOP_PICK_MAX_LEGS", "3"))
TOP_PICK_MIN_ODDS = float(os.environ.get("TOP_PICK_MIN_ODDS", "2.00"))
# Probability tiers for fallback cascade (min, max) — tried in order.
TOP_PICK_PROBABILITY_TIERS = [
    (0.70, 0.90),  # Tier 1: Primary
    (0.60, 0.70),  # Tier 2: Fallback 1
    (0.50, 0.60),  # Tier 3: Fallback 2
]

# ALL PREDICTIONS: 5-10 legs, combined odds target 8.00-10.00.
# Probability fallback cascade: 65-90% → 55-65% → 50-55%.
ALL_PICK_MIN_ODDS = float(os.environ.get("ALL_PICK_MIN_ODDS", "8.00"))
ALL_PICK_MAX_ODDS = float(os.environ.get("ALL_PICK_MAX_ODDS", "10.00"))
ALL_PICK_TARGET_ODDS = float(os.environ.get("ALL_PICK_TARGET_ODDS", "9.00"))
ALL_PICK_TOLERANCE = float(os.environ.get("ALL_PICK_TOLERANCE", "1.00"))
ALL_PICK_MIN_LEGS = int(os.environ.get("ALL_PICK_MIN_LEGS", "5"))
ALL_PICK_MAX_LEGS = int(os.environ.get("ALL_PICK_MAX_LEGS", "10"))
# Probability tiers for fallback cascade (min, max) — tried in order.
ALL_PICK_PROBABILITY_TIERS = [
    (0.65, 0.90),  # Tier 1: Primary
    (0.55, 0.65),  # Tier 2: Fallback 1
    (0.50, 0.55),  # Tier 3: Fallback 2
]

# How many candidates the accumulator algorithm will consider. Keeping this
# bounded avoids pathological O(n^3) behaviour on high-volume days.
ACCUMULATOR_MAX_CANDIDATES = int(os.environ.get("ACCUMULATOR_MAX_CANDIDATES", "30"))

