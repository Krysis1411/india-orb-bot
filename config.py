BACKTEST_STARTING_BALANCE = 2000.0

# ---------------------------------------------------------------------------
# RSI swing trading settings (used by bot.py)
# ---------------------------------------------------------------------------

SYMBOLS = [
    # Large cap tech
    'AAPL', 'MSFT', 'GOOGL', 'NVDA', 'AMZN', 'META',
    # ETFs
    'SPY', 'QQQ',
    # Small cap
    'SOFI', 'HOOD', 'RBLX', 'DKNG', 'MARA', 'RIOT', 'IONQ',
]

RSI_PERIOD = 14
RSI_OVERSOLD = 35
RSI_OVERBOUGHT = 65

MA_TREND_PERIOD = 200

STOP_LOSS_PCT = 2.0  # Exit if position is down 2%

TRADE_QUANTITY = 1

# ---------------------------------------------------------------------------
# ORB day trading settings
# ---------------------------------------------------------------------------

# Number of active stocks to fetch from screener daily
ORB_SCREENER_LIMIT = 20

# Static fallback symbol list used when the live screener fails
ORB_SYMBOLS = [
    "AAPL", "MSFT", "META", "GOOGL", "TSLA",
    "AMD", "AVGO", "SMCI",
    "QQQ", "IWM",
    "COIN", "DKNG", "HOOD", "SOFI",
    "CLSK", "IONQ", "SOUN", "RBLX",
    "GME", "AMC",
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

# Dollar amount to deploy per ORB trade — qty = floor(ORB_POSITION_SIZE / entry_price)
ORB_POSITION_SIZE = 500

# Maximum total investment budget for the bot across all trades (in dollars)
MAX_TOTAL_INVESTMENT = 2000

# ---------------------------------------------------------------------------
# Options-specific settings
# ---------------------------------------------------------------------------
ORB_OPTIONS_POSITION_SIZE = 500  # Target allocation per options strategy trade
MAX_OPTIONS_INVESTMENT = 2000     # Total maximum budget for all options positions
ORB_OPTIONS_IV_THRESHOLD = 0.45   # Implied Volatility threshold to select between buying/selling premium

# ---------------------------------------------------------------------------
# Risk controls (adapted from trading00money/QuantTrading risk_engine.py)
# ---------------------------------------------------------------------------
DAILY_LOSS_LIMIT_PCT   = 0.05   # Kill new entries if today's P&L < -5% of last equity
MAX_DRAWDOWN_PCT       = 0.20   # Hard kill-switch: close all if drawdown > 20%
MAX_RISK_PER_TRADE_PCT = 0.02   # Max 2% of account per trade (used for position sizing)
MIN_RR_RATIO           = 1.5    # Skip trade if reward:risk < 1.5 (from rr_engine.py)

# ---------------------------------------------------------------------------
# Iron Condor quality filters (address root causes of consistent losses)
# ---------------------------------------------------------------------------
IC_MAX_DTE          = 1     # Only enter ICs expiring today or tomorrow (0–1 DTE)
                            # Multi-day condors breach almost always — 4-day holds = 2× daily sigma exposure
MIN_UNDERLYING_PRICE = 20.0 # Skip options on stocks under $20 — illiquid chains, wide spreads
IC_MIN_CREDIT_RATIO = 0.20  # Credit must be ≥ 20% of spread width; below this R:R is negative EV
IC_SIGMA_MULTIPLE   = 1.5   # Short strikes must be ≥ 1.5× expected move away from current price
IC_PROFIT_TARGET_PCT = 0.50 # Close IC when unrealised P&L reaches 50% of credit received
IC_PNL_STOP_MULTIPLE = 2.0  # P&L stop: close if total loss > 2× credit received

# ---------------------------------------------------------------------------
# Entry quality filters (derived from backtest analysis)
# ---------------------------------------------------------------------------

# Symbols that consistently lose money in backtest regardless of strategy.
# Large-caps / ETFs with low IV rank get routed to debit spreads/straddles
# which need a 1.5× OR-range move that these tickers never deliver.
# Backtest losses: META -$2,734 | IWM -$1,872 | MSFT -$1,071 | AAPL -$100 | GME -$197
ORB_OPTIONS_BLOCKLIST = ['AAPL', 'MSFT', 'IWM', 'META', 'GME']

# Skip new entries on Mondays — backtest win rate 14.3% (vs 26–31% Tue–Thu).
# OR ranges on Mondays are noisy (weekend gap, low early volume).
SKIP_MONDAY_ENTRIES = True

# Hard cutoff for new entries — backtest win rate after 12:30 PM is 7.5%
# (3/40 trades hit target; 37 expire at EOD with near-zero theta collected).
IC_MAX_ENTRY_HOUR   = 12
IC_MAX_ENTRY_MINUTE = 30

# Minimum price distance through the OR boundary before entering a directional spread.
# Backtest: <0.5% breakout → 15% win rate | >1% breakout → 52% win rate.
# Applied only to Bull Call / Bear Put / Straddle entries, not Iron Condors.
MIN_BREAKOUT_STRENGTH_PCT = 0.005  # price must be ≥ 0.5% beyond OR high/low

