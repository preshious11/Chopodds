# BetVault — Sports Picks Telegram Bot

Pulls live odds from real bookmakers, strips out the bookmaker margin
("vig"), and surfaces the day's Football and Tennis picks where the market
consensus implies a high probability. Shows the real percentage instead of
a made-up "90% sure" label, and tracks how the picks actually performed.

## Why not 85-90% confidence like the marketing bots promise

No legitimate source hits that consistently across all sports. Sites
that claim it are cherry-picking their record after the fact or
loosely defining "sure." A well-calibrated model or sharp bettor is
happy with 55-65% on straightforward markets — that's already a real
edge. This bot shows you the actual market-implied number so you can
judge for yourself, instead of trusting an inflated claim.

## Deploy on Railway

1. **Create the service** — New Project → Deploy from GitHub repo → pick
   this repository. `railway.json` sets the start command (`python bot.py`)
   and restart policy; `.python-version` pins Python 3.11.
2. **Add a volume** — in the service, add a Volume mounted at `/data`.
   Without it, subscribers, stats and the odds cache are wiped on every
   redeploy.
3. **Set Variables** (service → Variables):

   | Variable | Required | Value |
   | --- | --- | --- |
   | `TELEGRAM_BOT_TOKEN` | yes | from @BotFather |
   | `ODDS_API_KEY` | yes | from the-odds-api.com |
   | `DATA_DIR` | yes on Railway | `/data` (the volume mount path) |
   | `ADMIN_CHAT_ID` | recommended | your Telegram user ID (enables `/status`) |
   | `SHARPAPI_API_KEY` | optional | fallback odds provider |
   | `SPORTSGAMEODDS_API_KEY` | optional | second fallback provider |
   | `DAILY_BROADCAST_TIME` | optional | `09:00` (Africa/Lagos) |

4. **Keep one replica.** The bot uses Telegram long polling; two running
   copies with the same token conflict.
5. After it starts, send `/status` from the admin account: it shows the
   cache, which odds providers have keys and served data, and settlement.

## Run locally

```
pip install -r requirements.txt
cp .env.example .env    # fill in at least TELEGRAM_BOT_TOKEN and ODDS_API_KEY
python bot.py
```

All settings are documented in `.env.example`.

## Commands

- `/start` — subscribe and show the menu
- `/dailypick` — today's top-picks accumulator (~2.00 combined odds)
- `/toppicks` (or `/top`) — the all-picks accumulator, 5 per page
- `/sports` — filter today's picks by sport
- `/stats` — overall and personal results of settled picks
- `/stop` (or `/unsubscribe`) — stop the daily broadcast
- `/help` — help

Admin only (`ADMIN_CHAT_ID`):

- `/status` — cache, providers, last generation diagnostics, settlement
- `/force_refresh_cache` — drop today's cache and regenerate predictions now

## Odds providers and caching

Odds are fetched once per Africa/Lagos day and cached for every user.
Providers are tried in order, and each one only receives the leagues the
previous ones could not supply:

1. **The Odds API** — every in-season configured league, one request each.
   Burst rate limits (HTTP 429) are retried after 2, 5 and 10 seconds.
2. **SharpAPI** — one paginated request for the leagues it covers (EPL,
   La Liga, Serie A, Bundesliga, Ligue 1, Champions League, MLS, ATP, WTA).
3. **SportsGameOdds** — one paginated request for today's matches in the
   leagues it covers (the above plus Europa League and Eredivisie).

If every provider fails, nothing is cached and the bot waits
`ODDS_FETCH_RETRY_COOLDOWN` seconds (default 10 minutes) before trying
again, so an outage cannot drain credits.

**Credits:** The Odds API charges `regions x markets` credits per league
request; the defaults use about 36-42 credits per day (~1,100-1,300 per
month, above the 500 free tier), plus 2 credits per league when settling
results. SharpAPI's free tier allows 12 requests/minute and SportsGameOdds'
10 requests/minute and 2,500 events/month; the fallbacks are only called
when The Odds API fails.

## How picks are chosen

1. For each match kicking off later today and each market (match result,
   handicap, over/under), only bookmakers quoting the most common line are
   used. Each bookmaker's prices are de-vigged so they sum to 100%, then
   averaged into a consensus probability.
2. Derived Football markets:
   - **Double Chance** — the sum of two match-result probabilities, priced
     from the best available match-result odds.
   - **BTTS** — a Poisson goal model fitted to the over/under line and the
     match-result odds; its odds are estimates and shown as `(est.)`.
3. Picks need `MIN_BOOKMAKERS` bookmakers and at least 50%
   (`MIN_PROBABILITY`), dropping to 40% when fewer than 5 matches qualify.
   Per-market floors apply on normal days.
4. The daily list keeps one pick per match and balances markets; the
   `/dailypick` and `/toppicks` accumulators are built from it.

## Settlement and stats

Every pick shown is recorded in SQLite. Every hour, matches that kicked
off 110 minutes to 14 hours ago are checked against The Odds API scores
(picks from a fallback provider are matched by team names and kick-off).
Picks are graded win, loss or void (postponed, or a push on the line);
picks that still have no result 24 hours after kick-off are voided.

## Development

```
pip install -r requirements.txt pytest pytest-asyncio ruff
ruff check . --select E9,F
pytest -q
```

## Important

Sports outcomes are inherently uncertain. Even a 90% market-implied
probability means the other outcome happens 1 in 10 times. Nothing
here is financial advice, and if you're using this to place bets,
only risk what you can afford to lose. Also check local laws — sports
betting bots and automated tipster services are regulated or
restricted in some jurisdictions.
