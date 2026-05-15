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

    # Broad market ETFs
    "SPY", "QQQ", "IWM",

    # Sector ETFs
    "XLF",   # Financials
    "XLE",   # Energy
    "ARKK",  # Cathie Wood ARK Innovation — tracks speculative tech, very volatile

    # High-volatility mid-cap
    "COIN",  # Coinbase — trades like a crypto asset, huge intraday swings
    "PLTR",  # Palantir — AI/defence, high retail interest
    "RIVN",  # Rivian — EV, wide daily ranges
    "LCID",  # Lucid Motors — low float, big % moves
    "UPST",  # Upstart — fintech, one of the most volatile mid-caps
    "DKNG",  # DraftKings — sports betting, event-driven spikes
    "HOOD",  # Robinhood — retail brokerage, moves with market sentiment
    "SOFI",  # SoFi — volatile fintech

    # Crypto miners — extreme beta, move 2-5× SPY on big days
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

# Profit target = OR range × this multiplier above the OR high
ORB_PROFIT_MULTIPLIER = 1.5

# Breakout bar volume must be >= (avg OR bar volume × this factor)
ORB_VOLUME_FACTOR = 1.2

# Stop is placed just below the OR low (in dollars)
ORB_STOP_BUFFER = 0.05

# Close all positions at or after this time (ET) — no overnight holds
ORB_CLOSE_HOUR = 15
ORB_CLOSE_MINUTE = 45

# Shares per trade for the ORB bot
ORB_TRADE_QUANTITY = 10
