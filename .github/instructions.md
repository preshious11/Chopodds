# Telegram Sports Prediction Bot - Development Instructions

## Core Business & API Rules
- STRICT DAILY CACHING: Do not alter or bypass local daily caching logic (12:00 AM reset). Always serve subsequent user requests from the local cache files (`.daily_cache/odds_YYYY-MM-DD.json` under `DATA_DIR`) to conserve Odds API credits.
- MATCH PREDICTION SELECTION: Enforce the "1 match = 1 top prediction" rule by selecting only the single highest-probability prediction per event across all markets.
- SPORT SCOPE: Restrict Odds API calls exclusively to Football (Soccer) and Tennis endpoints.
- CONFIDENCE TIERS & SAFETY FLOORS:
  - Default confidence floor: >= 50%
  - Slow day fallback floor: >= 40% (triggered only if total matches < 5)
  - Confidence tiers: High (>=70%), Moderate (50%-69%), Value Pick (40%-49%)

## Code Quality & Technical Standards
- Python 3.10+ async/await conventions for Telegram application handlers and API calls.
- Type Hints: Include explicit typing (`typing.Dict`, `typing.List`, `typing.Optional`) on all helper functions.
- Safe Data Access: Always use safe `.get()` calls instead of direct key access when parsing API payloads to avoid `KeyError` crashes.
- Logging: Use standard `logging` rather than print statements.
