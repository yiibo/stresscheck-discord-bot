# DeFi Stress Check Bot

A Discord bot that pulls live DeFi market data (DefiLlama, Owlracle) once a day, hands it to
Claude, and posts a short "DeFi Stress Check" report with a 1-100 stress score and a bullet-point
rationale. Can also be triggered manually with `!stresscheck`.

## What it measures

All data comes from free, public endpoints — no paid API keys required for the market data itself.

| Signal | Source | What it's for |
|---|---|---|
| Global TVL + 24h/7d trend | `api.llama.fi/v2/historicalChainTvl` | Overall capital sitting in DeFi, and whether it's growing/shrinking |
| Top 5 chains: TVL share, TVL trend, DEX volume trend, stablecoin supply trend | `api.llama.fi/v2/chains`, `v2/historicalChainTvl/{chain}`, `overview/dexs/{chain}`, stablecoin `chainCirculating` | Cross-chain capital rotation — a chain gaining trading volume/stablecoin supply while losing TVL (or vice versa) is a rotation signal that a single TVL number hides |
| DEX trading volume + 24h/7d trend | `api.llama.fi/overview/dexs` | Onchain activity/flow (TVL is a stock, this is a flow) |
| Stablecoin market cap + peg deviation watch | `stablecoins.llama.fi/stablecoins` | Total stablecoin supply, plus the worst deviation from $1 among a curated set of large, hard-pegged stables (USDT, USDC, DAI, USDe, FDUSD, PYUSD, USDS) — a depeg is a classic DeFi stress event |
| Lending liquidations + 24h/7d trend | `api.llama.fi/overview/liquidations` | Actual deleveraging/cascade volume, not just a price dip |
| ETH gas price + 24h/7d trend + 24h high | `api.owlracle.info/v4/eth/gas`, `v4/eth/history` | Network congestion, including same-day spikes a daily average would hide |

**Known gaps:** true cross-chain bridge volume (DefiLlama's `bridges.llama.fi` now requires a paid
Pro plan) and social/sentiment signals (X, Reddit, etc. — evaluated but not integrated; see
[Optional: social sentiment](#optional-social-sentiment-not-enabled) below).

## Requirements

- Python 3.10+
- An Anthropic API key
- A Discord bot application + token (see setup below)

Install dependencies:

```bash
pip install discord.py httpx anthropic apscheduler pytz
```

## Discord setup

1. Go to the [Discord Developer Portal](https://discord.com/developers/applications) and create a
   **New Application**.
2. Under **Bot**, click **Reset Token** (or **Add Bot** if it's a new app) and copy the token —
   this is your `DISCORD_TOKEN`. Keep it secret; anyone with it can control the bot.
3. Still under **Bot**, enable **Message Content Intent** under *Privileged Gateway Intents*. The
   bot won't start correctly without this (`intents.message_content = True` in `bot.py`).
4. Go to **OAuth2 → URL Generator**:
   - Scopes: `bot`
   - Bot permissions: `Send Messages`, `Read Message History`, `View Channels`
   - Open the generated URL and invite the bot to your server.
5. In Discord, enable **Developer Mode** (User Settings → Advanced), then right-click the channel
   you want reports posted to and **Copy Channel ID** — this is your `DISCORD_CHANNEL_ID`.
6. The `!stresscheck` manual-trigger command requires the invoking *user* to have Administrator
   permission in the server (not the bot itself).

## Environment variables

Set these however your process/service already injects environment variables (systemd
`EnvironmentFile`, Docker `--env-file`, etc.) — the bot doesn't load a `.env` file itself.

| Variable | Required | Default | Notes |
|---|---|---|---|
| `DISCORD_TOKEN` | Yes | — | Bot token from step 2 above |
| `DISCORD_CHANNEL_ID` | Yes | — | Channel ID from step 5 above |
| `ANTHROPIC_API_KEY` | Yes | — | Read automatically by the Anthropic SDK |
| `TIMEZONE` | No | `Australia/Melbourne` | IANA timezone name; controls the 9:00 AM daily post time |
| `CLAUDE_MODEL` | No | `claude-haiku-4-5-20251001` | Haiku is used by default since this is templated formatting over a handful of numbers, not deep reasoning — override if you want a stronger model |
| `OWLRACLE_API_KEY` | No | — | Owlracle's free gas-price tier is rate-limited; add a key if you hit limits |

## Running

```bash
python bot.py
```

On startup the bot logs into Discord, schedules the daily job for 9:00 AM in `TIMEZONE`, and waits.

## Usage

- **Automatic**: posts a stress check to `DISCORD_CHANNEL_ID` every day at 9:00 AM (server's
  `TIMEZONE`).
- **Manual**: an administrator can run `!stresscheck` in any channel to trigger one on demand. This
  is rate-limited to once per 5 minutes per server to avoid runaway API spend from repeated
  triggering.

## Cost notes

- All market-data APIs used are free/public.
- The only recurring paid cost is the Claude API call (one per report, ~800 output tokens max),
  using Haiku by default to keep that cheap. Switch `CLAUDE_MODEL` to a Sonnet/Opus model only if
  you need deeper reasoning than templated formatting over the metrics digest.

## Optional: social sentiment (not enabled)

Adding X/social engagement sentiment for tokens was evaluated but deliberately not wired in — it
requires a paid third-party API and a provider choice:

- **Santiment** — the only option with true per-platform (X-specific, not blended) sentiment;
  free tier is 30-day lagged, real-time needs their Max tier.
- **LunarCrush** — easier integration, but sentiment is blended across platforms rather than
  X-isolated; social data starts at $90/mo.
- **Direct X API v2** — guaranteed X-only data, but pay-per-use pricing plus the engineering cost
  of building sentiment scoring ourselves.

If you want this added later, sign up with whichever provider you pick and set its API key as an
env var (same pattern as `OWLRACLE_API_KEY`) — the integration can be built from there.
# stresscheck-discord-bot
# stresscheck-discord-bot
