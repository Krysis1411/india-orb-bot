SYMBOLS = [
    # Large cap tech
    'AAPL', 'MSFT', 'GOOGL', 'NVDA', 'AMZN', 'META',
    # ETFs
    'SPY', 'QQQ',
    # Small cap
    'SOFI', 'HOOD', 'RBLX', 'DKNG', 'MARA', 'RIOT', 'IONQ',
]

# RSI settings
RSI_PERIOD = 14
RSI_OVERSOLD = 35    # Buy when RSI drops below this
RSI_OVERBOUGHT = 65  # Sell when RSI rises above this

# Trend filter — only buy if price is above 200-day MA (uptrend)
MA_TREND_PERIOD = 200

# Exit settings
STOP_LOSS_PCT = 2.0  # Exit if position is down 2%

TRADE_QUANTITY = 1
