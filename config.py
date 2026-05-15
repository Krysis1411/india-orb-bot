SYMBOLS = [
    # Large cap tech
    "AAPL", "MSFT", "GOOGL", "NVDA", "AMZN", "META",
    # ETFs
    "SPY", "QQQ",
    # Small cap / speculative
    "SOFI", "HOOD", "RBLX", "DKNG", "MARA", "RIOT", "IONQ",
]

# RSI settings (swing strategy)
RSI_PERIOD = 14
RSI_OVERSOLD = 35     # Buy when RSI drops below this
RSI_OVERBOUGHT = 65   # Sell when RSI rises above this

# Trend filter — only buy if price is above the N-day MA (uptrend)
MA_TREND_PERIOD = 200

# Exit settings (swing)
STOP_LOSS_PCT = 2.0   # Exit if position is down this many percent

# Position sizing (swing)
TRADE_QUANTITY = 1    # Shares per trade (swing live bot)

# ---------------------------------------------------------------------------
# Backtest settings
# ---------------------------------------------------------------------------

BACKTEST_STARTING_BALANCE = 100_000.0
BACKTEST_SYMBOLS = ["AAPL", "MSFT", "SPY"]

# ---------------------------------------------------------------------------
# ORB day trading settings
# ---------------------------------------------------------------------------

ORB_SYMBOLS = [
    # Mega-cap tech — highest volume, tight spreads, reliable ORB patterns
    "AAPL", "MSFT", "NVDA", "AMZN", "META", "GOOGL", "TSLA",

    # Semiconductors — high beta, big intraday moves
    "AMD", "AVGO", "SMCI",

    # Broad market ETFs — SPY removed (too stable for ORB, 14 trades barely any P&L)
    "QQQ", "IWM",

    # Sector ETFs
    "XLF",   # Financials
    "XLE",   # Energy
    "ARKK",  # ARK Innovation — tracks speculative tech, very volatile

    # High-volatility mid-cap
    "COIN",  # Coinbase — trades like a crypto asset, huge intraday swings
    "PLTR",  # Palantir — AI/defence, high retail interest
    "RIVN",  # Rivian — EV, wide daily ranges
    "LCID",  # Lucid Motors — low float, big % moves
    "UPST",  # Upstart — fintech, one of the most volatile mid-caps
    "DKNG",  # DraftKings — sports betting, event-driven spikes
    "HOOD",  # Robinhood — retail brokerage, moves with market sentiment
    "SOFI",  # SoFi — volatile fintech

    # Crypto miners — extreme beta, move 2-5× market on big days
    "MARA",  # Marathon Digital
    "RIOT",  # Riot Platforms
    "CLSK",  # CleanSpark

    # AI / quantum speculative plays — small float, huge % swings
    "IONQ",  # IonQ — quantum computing
    "SOUN",  # SoundHound AI — very low float, big intraday spikes
    "AI",    # C3.ai
    "BBAI",  # BigBear.ai — micro-cap, extreme volatility
    "RBLX",  # Roblox — gaming/metaverse

    # Meme / high-short-interest — unpredictable but active
    "GME",   # GameStop
    "AMC",   # AMC Entertainment
]

# Opening range = first N × 5-min bars (6 bars = 30 minutes, 9:30–10:00 ET)
ORB_RANGE_BARS = 6

# Default profit target = OR range × this multiplier above the OR high
ORB_PROFIT_MULTIPLIER = 1.5

# Per-symbol profit multipliers (overrides default above)
# Volatile/fast movers: take profit at 1.0× before reversal
# Slow/large movers: wait for 2.0-2.5× as they grind more steadily
ORB_PROFIT_MULTIPLIERS: dict[str, float] = {
    "MARA": 1.0, "RIOT": 1.0, "CLSK": 1.0, "SOUN": 1.0,
    "BBAI": 1.0, "GME":  1.0, "AMC":  1.0, "AI":   1.0,
    "AMZN": 2.5, "PLTR": 2.0, "XLF":  2.0, "DKNG": 2.0,
}

# Minimum OR range as fraction of stock price — skip narrow/indecisive opens
ORB_MIN_OR_PCT = 0.005  # 0.5%

# Breakout bar volume must be >= (avg OR bar volume × this factor)
# Lowered from 1.2 → 1.0: 1.2 blocked all NVDA signals in backtest
ORB_VOLUME_FACTOR = 1.0

# Stop is placed just below the OR low (in dollars)
ORB_STOP_BUFFER = 0.05

# Close all positions at or after this time (ET) — no overnight holds
ORB_CLOSE_HOUR = 15
ORB_CLOSE_MINUTE = 45

# Shares per trade for the ORB bot
# Raised from 10 → 50: 10 shares produced ~$5/trade, not meaningful even on paper
ORB_TRADE_QUANTITY = 50
