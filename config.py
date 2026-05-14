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

# Only trade the most liquid names — tight spreads matter intraday
ORB_SYMBOLS = ["AAPL", "MSFT", "NVDA", "SPY", "QQQ"]

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
