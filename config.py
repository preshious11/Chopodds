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
ODDS_MARKETS = os.environ.get("ODDS_MARKETS", "h2h,spreads,totals")

# Bot Configuration
# Number of top picks to show in /dailypick and daily broadcast (3-5 recommended)
DAILY_PICK_COUNT = int(os.environ.get("DAILY_PICK_COUNT", "5"))

# Daily broadcast time in 24-hour format (UTC)
DAILY_BROADCAST_TIME = os.environ.get("DAILY_BROADCAST_TIME", "09:00")

