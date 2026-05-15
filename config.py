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
    # Mega-cap tech
    "AAPL", "MSFT", "META", "GOOGL", "TSLA",

    # Semiconductors
    "AMD", "AVGO", "SMCI",

    # ETFs
    "QQQ", "IWM",

    # Sector ETF
    "XLE",   # Energy

    # ARK Innovation
    "ARKK",

    # High-volatility mid-cap
    "COIN",  # Coinbase
    "LCID",  # Lucid Motors
    "UPST",  # Upstart
    "DKNG",  # DraftKings
    "HOOD",  # Robinhood
    "SOFI",  # SoFi

    # Crypto miner (profitable)
    "CLSK",  # CleanSpark

    # AI / quantum
    "IONQ",  # IonQ
    "SOUN",  # SoundHound AI
    "RBLX",  # Roblox

    # Meme
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
    # Volatile/fast movers — take profit quickly before reversal
    "CLSK": 1.0, "SOUN": 1.0, "GME": 1.0, "AMC": 1.0,
    # Slower movers — give more room to reach target
    "DKNG": 2.0,
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
