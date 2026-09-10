# Sports Picks Telegram Bot

Pulls live odds from real bookmakers (via The Odds API), strips out the
bookmaker margin ("vig"), and surfaces picks where the market consensus
implies a high probability for one outcome. Shows the real percentage
instead of a made-up "90% sure" label.

## Why not 85-90% confidence like the marketing bots promise

No legitimate source hits that consistently across all sports. Sites
that claim it are cherry-picking their record after the fact or
loosely defining "sure." A well-calibrated model or sharp bettor is
happy with 55-65% on straightforward markets — that's already a real
edge. This bot shows you the actual market-implied number so you can
judge for yourself, instead of trusting an inflated claim.

## Setup

1. **Get a Telegram bot token**
   Message [@BotFather](https://t.me/BotFather) on Telegram, run `/newbot`,
   follow the prompts, copy the token it gives you.

2. **Get an Odds API key**
   Sign up free at https://the-odds-api.com (500 requests/month on the
   free tier, plenty for testing). Copy your API key.

3. **Install dependencies**
   ```
   pip install -r requirements.txt
   ```

4. **Set environment variables**
   ```
   export TELEGRAM_BOT_TOKEN="your_token_here"
   export ODDS_API_KEY="your_key_here"
   export TARGET_CHAT_ID="your_channel_or_chat_id"   # optional, for auto-posting
   export MIN_PROBABILITY="0.70"                      # optional, default 0.70
   export MIN_BOOKMAKERS="4"                           # optional, default 4
   ```

5. **Run it**
   ```
   python bot.py
   ```

## Commands

- `/start` — intro and current settings
- `/sports` — list active sport keys you can query
- `/predict soccer_epl` — picks for one sport (swap in any valid sport key)
- `/top` — scans all default sports (EPL, Champions League, NBA, NFL,
  NHL, MMA) and returns the highest-consensus picks
- `/threshold 75` — change the minimum probability for this session

## How it decides what counts as a "pick"

1. Pulls moneyline (h2h) odds from every available bookmaker for a match
2. Converts each bookmaker's odds to implied probability
3. Normalizes ("de-vigs") each bookmaker's numbers so they sum to 100%,
   removing their profit margin
4. Averages the de-vigged probability across all bookmakers offering
   that market
5. Only surfaces the outcome if enough bookmakers agree (`MIN_BOOKMAKERS`)
   and the consensus probability clears your threshold (`MIN_PROBABILITY`)

## Extending it

- `probability.py` is where you'd plug in your own model (Poisson goal
  models, Elo ratings, etc.) instead of relying purely on bookmaker
  consensus
- `picks.py` currently only checks the `h2h` market — you can add
  `spreads` or `totals` by passing a different `market` value into
  `get_odds()`
- Swap `run_polling()` for a webhook setup in `bot.py` if you deploy to
  a server instead of running locally

## Important

Sports outcomes are inherently uncertain. Even a 90% market-implied
probability means the other outcome happens 1 in 10 times. Nothing
here is financial advice, and if you're using this to place bets,
only risk what you can afford to lose. Also check local laws — sports
betting bots and automated tipster services are regulated or
restricted in some jurisdictions.
