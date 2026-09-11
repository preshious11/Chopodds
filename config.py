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
ODDS_REGIONS = os.environ.get("ODDS_REGIONS", "uk,eu,us,au")
# Query all supported markets. The API returns what's available per sport;
# unsupported markets are silently ignored by the API.
# Derived markets (double_chance, btts) are calculated from base markets in probability.py.
ODDS_MARKETS = os.environ.get("ODDS_MARKETS", "h2h,spreads,totals,btts,double_chance,draw_no_bet")

# Bot Configuration
ADMIN_CHAT_ID = int(os.environ.get("ADMIN_CHAT_ID", "0").strip() or "0")

# Number of top picks to show in /dailypick and daily broadcast (3-5 recommended)
DAILY_PICK_COUNT = int(os.environ.get("DAILY_PICK_COUNT", "5"))

# Daily broadcast time in 24-hour format (UTC)
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

# Minimum bookmakers required for consensus
MIN_BOOKMAKERS = int(os.environ.get("MIN_BOOKMAKERS", "3"))

