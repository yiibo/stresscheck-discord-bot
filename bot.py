import asyncio
import os
import sys
import urllib.parse

import discord
import httpx
import anthropic
from discord.ext import commands
from apscheduler.schedulers.asyncio import AsyncIOScheduler
from pytz import timezone

# Force Python to flush all logs to systemd immediately
sys.stdout.reconfigure(line_buffering=True)

print("[BOOT] Script starting up...")

# 1. Initialization
intents = discord.Intents.default()
intents.message_content = True
bot = commands.Bot(command_prefix="!", intents=intents)

CHANNEL_ID = int(os.environ.get("DISCORD_CHANNEL_ID", 0))
LOCAL_TIMEZONE = timezone(os.environ.get("TIMEZONE", "Australia/Melbourne"))
OWLRACLE_API_KEY = os.environ.get("OWLRACLE_API_KEY")

# Haiku is plenty for formatting a metrics digest into a short report; ~10x cheaper than Sonnet.
# Override with CLAUDE_MODEL if the analysis ever needs deeper reasoning.
MODEL_NAME = os.environ.get("CLAUDE_MODEL", "claude-haiku-4-5-20251001")

# Chains ranked by TVL to break out individually (rest are folded into "other chains").
TOP_CHAIN_COUNT = 5
# Large, widely-integrated stablecoins meant to hold a hard $1 peg. See the docstring
# on build_stablecoin_summary for why this is a curated allowlist rather than a
# price-deviation scan over every pegged asset DefiLlama tracks.
WATCHED_STABLE_SYMBOLS = {"USDT", "USDC", "DAI", "USDe", "FDUSD", "PYUSD", "USDS"}

REPORT_PREFIX = "📊 **Daily Systemic DeFi Stress Check** 📊\n\n"
DISCORD_MAX_LEN = 2000

# Initialize Claude API Client
ai_client = anthropic.AsyncAnthropic()


# 2. Data Fetcher (real DefiLlama / Owlracle endpoints)
def _pct_change(series, days_ago):
    """% change from the point closest to `days_ago` back to the latest point in a
    date-ascending [{"date": unix_seconds, "tvl": float}, ...] series."""
    if not series:
        return None
    latest = series[-1]
    target_ts = latest["date"] - days_ago * 86400
    baseline = min(series, key=lambda p: abs(p["date"] - target_ts))
    if not baseline.get("tvl"):
        return None
    return (latest["tvl"] - baseline["tvl"]) / baseline["tvl"] * 100


async def fetch_global_tvl_trend(client: httpx.AsyncClient):
    """Aggregate TVL across all chains, plus 24h/7d trend. Uses the pre-aggregated
    series instead of summing every protocol client-side (thousands of entries,
    much bigger payload) and gets trend for free from the same call."""
    try:
        resp = await client.get("https://api.llama.fi/v2/historicalChainTvl")
        resp.raise_for_status()
        data = resp.json()
        if not data:
            return None
        return {
            "tvl": data[-1].get("tvl"),
            "change_1d": _pct_change(data, 1),
            "change_7d": _pct_change(data, 7),
        }
    except Exception as e:
        print(f"[ERROR] Global TVL trend fetch failed: {e}")
    return None


def _stablecoin_flow_for_chain(stable_assets, chain_name):
    """Net stablecoin supply on one chain, plus 1d/7d % change. Stablecoins are
    ~$1 by design, so a supply change is actual minting/bridging-in or
    redemption/bridging-out, not price noise -- a cleaner cross-chain capital
    flow signal than a TVL delta, which conflates real flow with price moves in
    whatever's locked (ETH, BTC, etc.) on that chain."""
    current = prev_day = prev_week = 0.0
    for asset in stable_assets:
        cc = asset.get("chainCirculating", {}).get(chain_name)
        if not cc:
            continue
        current += cc.get("current", {}).get("peggedUSD", 0) or 0
        prev_day += cc.get("circulatingPrevDay", {}).get("peggedUSD", 0) or 0
        prev_week += cc.get("circulatingPrevWeek", {}).get("peggedUSD", 0) or 0

    return {
        "total": current if current > 0 else None,
        "change_1d": ((current - prev_day) / prev_day * 100) if prev_day else None,
        "change_7d": ((current - prev_week) / prev_week * 100) if prev_week else None,
    }


async def fetch_chain_breakdown(client: httpx.AsyncClient, stable_assets, top_n=TOP_CHAIN_COUNT):
    """Top chains by current TVL share, each with its own TVL/DEX-volume trend and
    stablecoin flow. This is the free proxy for cross-chain capital rotation:
    DefiLlama's actual bridge-volume API (bridges.llama.fi) now returns 402
    Payment Required (Pro-tier only), so these three deltas together -- TVL,
    trading activity, and stablecoin supply -- are the closest free signal we
    have for "money moving between chains" rather than raw price moves."""
    try:
        resp = await client.get("https://api.llama.fi/v2/chains")
        resp.raise_for_status()
        chains = [c for c in resp.json() if c.get("tvl")]
    except Exception as e:
        print(f"[ERROR] Chain snapshot fetch failed: {e}")
        return []

    chains.sort(key=lambda c: c["tvl"], reverse=True)
    total_tvl = sum(c["tvl"] for c in chains)
    top = chains[:top_n]

    async def get_tvl_change(name):
        try:
            resp = await client.get(
                f"https://api.llama.fi/v2/historicalChainTvl/{urllib.parse.quote(name, safe='')}"
            )
            resp.raise_for_status()
            return _pct_change(resp.json(), 1)
        except Exception as e:
            print(f"[ERROR] Chain TVL history fetch failed for {name}: {e}")
            return None

    async def get_dex_volume(name):
        try:
            resp = await client.get(
                f"https://api.llama.fi/overview/dexs/{urllib.parse.quote(name, safe='')}",
                params={"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"},
            )
            resp.raise_for_status()
            data = resp.json()
            return {"change_1d": data.get("change_1d"), "change_7d": data.get("change_7d")}
        except Exception as e:
            print(f"[ERROR] Chain DEX volume fetch failed for {name}: {e}")
            return None

    async def with_trend(chain):
        name = chain["name"]
        tvl_change_1d, dex_volume = await asyncio.gather(get_tvl_change(name), get_dex_volume(name))
        return {
            "name": name,
            "tvl": chain["tvl"],
            "share_pct": (chain["tvl"] / total_tvl * 100) if total_tvl else None,
            "change_1d": tvl_change_1d,
            "dex_volume": dex_volume,
            "stable_flow": _stablecoin_flow_for_chain(stable_assets, name),
        }

    return await asyncio.gather(*(with_trend(c) for c in top))


async def fetch_dex_volume(client: httpx.AsyncClient):
    """Onchain trading activity proxy (TVL is a stock; this is a flow)."""
    try:
        resp = await client.get(
            "https://api.llama.fi/overview/dexs",
            params={"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "volume_24h": data.get("total24h"),
            "change_1d": data.get("change_1d"),
            "change_7d": data.get("change_7d"),
        }
    except Exception as e:
        print(f"[ERROR] DEX volume fetch failed: {e}")
    return None


async def fetch_stablecoin_assets(client: httpx.AsyncClient):
    """Raw pegged-asset list, fetched once and reused for both the global
    stablecoin summary and the per-chain stablecoin flow in fetch_chain_breakdown
    -- avoids pulling this ~500KB list twice."""
    try:
        resp = await client.get(
            "https://stablecoins.llama.fi/stablecoins",
            params={"includePrices": "true"},
        )
        resp.raise_for_status()
        return resp.json().get("peggedAssets", [])
    except Exception as e:
        print(f"[ERROR] Stablecoin fetch failed: {e}")
        return []


def build_stablecoin_summary(assets):
    """Total stablecoin supply plus the worst peg deviation among the stablecoins
    DeFi actually treats as hard $1 collateral. A depeg is the textbook DeFi
    stress event and can show up here well before it moves the aggregate market
    cap number.

    We deliberately don't scan *every* pegged asset for |price - 1|: DefiLlama's
    list also includes yield-accruing tokens (e.g. USDY) that are supposed to
    trade above $1 by design, non-USD pegs, and thin/illiquid wrappers whose
    price just wobbles on low volume. None of those are a "stress" signal, and
    there's no metadata field that reliably tells them apart from a real hard
    peg. WATCHED_STABLE_SYMBOLS is the curated set of large, widely-integrated
    hard-$1 stables where a real depeg would cascade into lending liquidations.
    """
    if not assets:
        return None

    total = sum(a.get("circulating", {}).get("peggedUSD", 0) for a in assets)

    # For each watched symbol, use whichever listed entry has the largest
    # circulating supply (some symbols appear more than once across chains/wrappers).
    best_by_symbol = {}
    for a in assets:
        symbol = a.get("symbol")
        price = a.get("price")
        cap = a.get("circulating", {}).get("peggedUSD", 0)
        if symbol not in WATCHED_STABLE_SYMBOLS or not cap or not isinstance(price, (int, float)):
            continue
        if symbol not in best_by_symbol or cap > best_by_symbol[symbol]["cap"]:
            best_by_symbol[symbol] = {"cap": cap, "price": price}

    worst_depeg = None
    for symbol, info in best_by_symbol.items():
        deviation = abs(info["price"] - 1.0)
        if worst_depeg is None or deviation > worst_depeg["deviation"]:
            worst_depeg = {"symbol": symbol, "price": info["price"], "deviation": deviation}

    return {"total_mcap": total if total > 0 else None, "worst_depeg": worst_depeg}


async def fetch_liquidations(client: httpx.AsyncClient):
    """Onchain lending liquidation volume. The most direct available signal for an
    actual deleveraging cascade, as opposed to a price dip that never triggers one."""
    try:
        resp = await client.get(
            "https://api.llama.fi/overview/liquidations",
            params={"excludeTotalDataChart": "true", "excludeTotalDataChartBreakdown": "true"},
        )
        resp.raise_for_status()
        data = resp.json()
        return {
            "volume_24h": data.get("total24h"),
            "change_1d": data.get("change_1d"),
            "change_7d": data.get("change_7d"),
        }
    except Exception as e:
        print(f"[ERROR] Liquidations fetch failed: {e}")
    return None


def _gas_trend(candles):
    """candles: Owlracle daily candles ordered newest-first. Returns day-over-day
    and week-over-week % change on the daily close, plus today's intraday high
    (a single close can hide a same-day spike that already stressed the network)."""
    if not candles:
        return None

    def close_at(idx):
        return candles[idx]["gasPrice"]["close"] if idx < len(candles) else None

    latest_close = close_at(0)
    prev_close = close_at(1)
    week_ago_close = close_at(7)

    change_1d = ((latest_close - prev_close) / prev_close * 100) if latest_close and prev_close else None
    change_7d = (
        ((latest_close - week_ago_close) / week_ago_close * 100) if latest_close and week_ago_close else None
    )
    return {"change_1d": change_1d, "change_7d": change_7d, "high_24h": candles[0]["gasPrice"].get("high")}


async def fetch_eth_gas(client: httpx.AsyncClient):
    params = {"apikey": OWLRACLE_API_KEY} if OWLRACLE_API_KEY else {}
    price = None
    trend = None

    try:
        resp = await client.get("https://api.owlracle.info/v4/eth/gas", params=params)
        resp.raise_for_status()
        speeds = resp.json().get("speeds") or []
        if speeds:
            price = speeds[0].get("baseFee")
    except Exception as e:
        print(f"[ERROR] Gas tracker exception: {e}")

    try:
        hist_resp = await client.get(
            "https://api.owlracle.info/v4/eth/history",
            params={**params, "candles": 8, "timeframe": 1440},
        )
        hist_resp.raise_for_status()
        trend = _gas_trend(hist_resp.json().get("candles") or [])
    except Exception as e:
        print(f"[ERROR] Gas history fetch failed: {e}")

    return {
        "price": price,
        "change_1d": trend["change_1d"] if trend else None,
        "change_7d": trend["change_7d"] if trend else None,
        "high_24h": trend["high_24h"] if trend else None,
    }


async def fetch_defi_metrics():
    print("[RUNNING] Fetching live metrics from verified endpoints...")
    timeout = httpx.Timeout(12.0)

    async with httpx.AsyncClient(timeout=timeout) as client:
        # Fetched first (not in the gather below) so both the global stablecoin
        # summary and the per-chain flow calc in fetch_chain_breakdown can reuse
        # the same asset list instead of each fetching it independently.
        stable_assets = await fetch_stablecoin_assets(client)

        tvl_trend, chain_breakdown, dex_volume, liquidations, gas = await asyncio.gather(
            fetch_global_tvl_trend(client),
            fetch_chain_breakdown(client, stable_assets),
            fetch_dex_volume(client),
            fetch_liquidations(client),
            fetch_eth_gas(client),
        )
        stablecoins = build_stablecoin_summary(stable_assets)

    print("[SUCCESS] Core System Parameters Engine Data collected successfully.")
    return {
        "tvl_trend": tvl_trend,
        "chain_breakdown": chain_breakdown,
        "dex_volume": dex_volume,
        "stablecoins": stablecoins,
        "liquidations": liquidations,
        "gas": gas,
    }


# 3. Metrics -> prompt formatting
def _fmt_usd(x):
    return f"${x:,.0f}" if isinstance(x, (int, float)) else "Unavailable"


def _fmt_pct(x):
    return f"{x:+.1f}%" if isinstance(x, (int, float)) else "N/A"


def build_metrics_block(metrics):
    lines = []

    tvl_trend = metrics["tvl_trend"]
    if tvl_trend:
        lines.append(
            f"- Global DeFi TVL: {_fmt_usd(tvl_trend['tvl'])} "
            f"(24h: {_fmt_pct(tvl_trend['change_1d'])}, 7d: {_fmt_pct(tvl_trend['change_7d'])})"
        )
    else:
        lines.append("- Global DeFi TVL: Unavailable")

    chain_breakdown = metrics["chain_breakdown"]
    if chain_breakdown:
        lines.append("- Top chains by TVL share (24h TVL / DEX volume / stablecoin supply change):")
        for c in chain_breakdown:
            share = f"{c['share_pct']:.1f}%" if isinstance(c["share_pct"], (int, float)) else "N/A"
            dex_1d = c["dex_volume"]["change_1d"] if c["dex_volume"] else None
            stable_1d = c["stable_flow"]["change_1d"] if c["stable_flow"] else None
            lines.append(
                f"  - {c['name']}: {share} share | TVL {_fmt_pct(c['change_1d'])} "
                f"| DEX vol {_fmt_pct(dex_1d)} | Stablecoin supply {_fmt_pct(stable_1d)}"
            )
    else:
        lines.append("- Per-chain TVL breakdown: Unavailable")

    dex_volume = metrics["dex_volume"]
    if dex_volume:
        lines.append(
            f"- DEX Trading Volume (24h): {_fmt_usd(dex_volume['volume_24h'])} "
            f"(24h: {_fmt_pct(dex_volume['change_1d'])}, 7d: {_fmt_pct(dex_volume['change_7d'])})"
        )
    else:
        lines.append("- DEX Trading Volume: Unavailable")

    stablecoins = metrics["stablecoins"]
    if stablecoins:
        lines.append(f"- Stablecoin Supply Market Cap: {_fmt_usd(stablecoins['total_mcap'])}")
        depeg = stablecoins["worst_depeg"]
        if depeg:
            lines.append(
                f"- Largest Stablecoin Peg Deviation: {depeg['symbol']} trading at "
                f"${depeg['price']:.4f} ({depeg['deviation'] * 100:.2f}% off $1 peg)"
            )
        else:
            lines.append("- Largest Stablecoin Peg Deviation: None detected among major stablecoins")
    else:
        lines.append("- Stablecoin Supply Market Cap: Unavailable")

    liquidations = metrics["liquidations"]
    if liquidations:
        lines.append(
            f"- Lending Liquidations (24h): {_fmt_usd(liquidations['volume_24h'])} "
            f"(24h: {_fmt_pct(liquidations['change_1d'])}, 7d: {_fmt_pct(liquidations['change_7d'])})"
        )
    else:
        lines.append("- Lending Liquidations: Unavailable")

    gas = metrics["gas"]
    gas_str = f"{gas['price']:.1f} Gwei" if gas and isinstance(gas["price"], (int, float)) else "Unknown Gwei"
    if gas and (gas["change_1d"] is not None or gas["change_7d"] is not None):
        high_str = f", 24h high: {gas['high_24h']:.1f} Gwei" if isinstance(gas.get("high_24h"), (int, float)) else ""
        lines.append(
            f"- Ethereum Base Gas Price: {gas_str} "
            f"(24h: {_fmt_pct(gas['change_1d'])}, 7d: {_fmt_pct(gas['change_7d'])}{high_str})"
        )
    else:
        lines.append(f"- Ethereum Base Gas Price: {gas_str}")

    return "\n".join(lines)


# 4. Core LLM Analytical Pipeline
async def daily_stress_check(target_channel=None):
    print("[RUNNING] Launching Systemic DeFi Stress Check pipeline...")

    # Resolve channel fallback sequence
    if target_channel is None:
        target_channel = bot.get_channel(CHANNEL_ID)
        if not target_channel:
            print(f"[WARN] Channel ID {CHANNEL_ID} not in cache. Fetching via API...")
            try:
                target_channel = await bot.fetch_channel(CHANNEL_ID)
            except Exception as fe:
                print(f"[CRITICAL] API Fetch failed for channel {CHANNEL_ID}: {fe}")
                return

    metrics = await fetch_defi_metrics()
    metrics_block = build_metrics_block(metrics)
    print("[RUNNING] Packaging inputs and submitting to Claude API...")

    prompt = f"""
    Analyze the following current DeFi ecosystem indicators and output a concise, expert "Daily DeFi Stress Check".

    Current Metrics:
    {metrics_block}

    Calculate a systemic 'DeFi Stress Score' from 1 to 100 (where 1 is perfectly calm, and 100 is maximum panic/liquidation crisis).
    Weigh trend and flow signals (TVL/DEX volume deltas, chain share shifts, peg deviations, liquidation volume, gas spikes)
    more heavily than static levels, since levels alone don't indicate stress. Provide a brief, 3-5 bullet analysis
    justifying the score, calling out any cross-chain capital rotation, peg risk, or liquidation cascade by name if present.
    Keep the layout highly readable using professional Discord markdown format. Avoid fluff.
    """

    try:
        response = await ai_client.messages.create(
            model=MODEL_NAME,
            max_tokens=800,
            system="You are a senior decentralized finance (DeFi) risk analyst. Format everything cleanly for Discord chat boxes.",
            messages=[{"role": "user", "content": prompt}],
        )

        # Safely extract text content, filtering out any non-text blocks
        report = "".join(block.text for block in response.content if hasattr(block, "text"))

        max_report_len = DISCORD_MAX_LEN - len(REPORT_PREFIX)
        if len(report) > max_report_len:
            report = report[: max_report_len - 3] + "..."

        await target_channel.send(f"{REPORT_PREFIX}{report}")
        print("[SUCCESS] Report published successfully to Discord channel.")

    except Exception as e:
        print(f"[CRASH] Failed to invoke Anthropic API: {e}")
        await target_channel.send(f"❌ **API Error:** Failed to call Claude API. Error details: `{str(e)}`")


# 5. Process Automation Core Triggers
@bot.event
async def on_ready():
    print(f"[BOOT] Logged into Discord Gateway successfully as {bot.user.name}")

    scheduler = AsyncIOScheduler(timezone=LOCAL_TIMEZONE)
    scheduler.add_job(daily_stress_check, "cron", hour=9, minute=0)
    scheduler.start()
    print(f"[BOOT] Cron loop registered for 9:00 AM every morning ({LOCAL_TIMEZONE}).")


@bot.command(name="stresscheck")
@commands.has_permissions(administrator=True)
@commands.cooldown(1, 300, commands.BucketType.guild)
async def manual_trigger(ctx):
    print(f"[COMMAND] Manual '!stresscheck' override triggered in text channel: {ctx.channel.id}")
    await ctx.send("🔄 *Manual Override Triggered.* Gathering DeFiLlama metrics and invoking Claude API...")
    try:
        await daily_stress_check(target_channel=ctx.channel)
    except Exception as err:
        print(f"[COMMAND ERROR] Manual script sequence failed: {err}")
        await ctx.send(f"❌ Execution crash: `{str(err)}`")


@manual_trigger.error
async def manual_trigger_error(ctx, error):
    if isinstance(error, commands.CommandOnCooldown):
        await ctx.send(f"⏳ Stress check already ran recently. Try again in {error.retry_after:.0f}s.")
    elif isinstance(error, commands.MissingPermissions):
        await ctx.send("❌ You need administrator permissions to run this.")
    else:
        print(f"[COMMAND ERROR] Unhandled error in manual_trigger: {error}")
        await ctx.send(f"❌ Execution crash: `{str(error)}`")


if __name__ == "__main__":
    TOKEN = os.environ.get("DISCORD_TOKEN")
    if not TOKEN:
        print("[CRITICAL] DISCORD_TOKEN is missing from your environment config file!")
    elif not CHANNEL_ID:
        print("[CRITICAL] DISCORD_CHANNEL_ID is missing or invalid in your environment config file!")
    else:
        print("[BOOT] Booting Discord gateway application run...")
        bot.run(TOKEN)
