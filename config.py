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

# Odds API configuration
# CREDIT COST WARNING: The Odds API charges credits = #regions x #markets
# per request. 14 leagues x 4 regions x 6 markets = 336 credits per daily
# fetch — far above the free tier's 500/month. Keep both lists minimal.
# btts/double_chance are derived locally (probability.py) and cost nothing.
ODDS_REGIONS = os.environ.get("ODDS_REGIONS", "uk")
# Query only base markets actually used. The API returns what's available
# per sport; unsupported markets are silently ignored by the API.
# Derived markets (double_chance, btts) are calculated from base markets
# in probability.py — never request them from the API.
ODDS_MARKETS = os.environ.get("ODDS_MARKETS", "h2h,spreads,totals")

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

# Minimum bookmakers required for consensus
MIN_BOOKMAKERS = int(os.environ.get("MIN_BOOKMAKERS", "3"))

