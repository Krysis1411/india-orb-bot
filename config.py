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
MIN_UNDERLYING_PRICE = 10.0 # Skip options on true penny stocks; liquid names (SOFI, AAL, F) trade fine at $10–20
IC_MIN_CREDIT_RATIO = 0.15  # Credit must be ≥ 15% of spread width; loosened from 0.20 to allow more setups
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
# NU: live loss — IC credit ratio only ~7%, options chain too illiquid at this price
ORB_OPTIONS_BLOCKLIST = ['AAPL', 'MSFT', 'IWM', 'META', 'GME', 'NU']

# Skip new entries on Mondays — backtest win rate 14.3% (vs 26–31% Tue–Thu).
# OR ranges on Mondays are noisy (weekend gap, low early volume).
SKIP_MONDAY_ENTRIES = True

# Hard cutoff for new entries — backtest shows declining win rate after 12:30 PM (7.5% after that).
# Extended to 1:00 PM to capture more of the morning window without going into the dead-money zone.
IC_MAX_ENTRY_HOUR   = 13
IC_MAX_ENTRY_MINUTE = 0

# Minimum price distance through the OR boundary before entering a directional spread.
# Backtest: <0.5% breakout → 15% win rate | >1% breakout → 52% win rate.
# Applied only to Bull Call / Bear Put / Straddle entries, not Iron Condors.
MIN_BREAKOUT_STRENGTH_PCT = 0.005  # price must be ≥ 0.5% beyond OR high/low

# ---------------------------------------------------------------------------
# India ORB settings (AngelOne SmartAPI / NSE)
# ---------------------------------------------------------------------------

# Opening range = first 6 × 5-min bars (09:15–09:45 IST)
INDIA_ORB_RANGE_BARS = 6

# EOD close: 15 min before NSE MIS auto-squareoff at 15:30 IST
INDIA_CLOSE_HOUR   = 15
INDIA_CLOSE_MINUTE = 15

# Capital allocation per trade (INR)
INDIA_POSITION_SIZE_INR = 5000    # ₹5,000 per trade
INDIA_MAX_TOTAL_INR     = 15000   # max ₹15,000 deployed at once → 3 concurrent trades

# ORB quality filters
INDIA_ORB_MIN_OR_PCT        = 0.003  # 0.3% min OR range — skip flat/indecisive opens
INDIA_ORB_MAX_OR_PCT        = 0.020  # 2.0% max OR range — skip gap/spike days
                                      # Backtest: >2% OR → 35% win rate, -₹281 (vs 48% overall)
                                      # Wide ranges = noisy, stops blow out too easily
INDIA_ORB_PROFIT_MULTIPLIER = 1.5    # target = OR range × 1.5 beyond breakout level
INDIA_ORB_VOLUME_FACTOR     = 1.0    # breakout volume must be ≥ 1× avg OR bar volume
INDIA_ORB_STOP_BUFFER_PCT   = 0.005  # stop = 0.5% beyond OR boundary (optimizer best result)

# Directional bias — trade both breakout above AND breakout below (short selling intraday)
INDIA_ALLOW_SHORTS = True

# Entry quality filters
INDIA_SKIP_MONDAY_ENTRIES = True   # Mondays tend to have noisier OR in India too
INDIA_MAX_ENTRY_HOUR      = 12
INDIA_MAX_ENTRY_MINUTE    = 30     # no new entries after 12:30 IST
                                   # Backtest: 12:30–13:00 → 42% win, -₹64 across 12 trades

# Daily loss circuit-breaker
INDIA_DAILY_LOSS_LIMIT_PCT = 0.05  # stop new entries if day P&L < -5%

# Number of top symbols to pick from NSE_UNIVERSE each day (by prev-day turnover)
INDIA_SCREENER_LIMIT = 15

# Backtested shortlist — used as FALLBACK when the live screener fails.
# Full-universe NautilusTrader backtest (45 symbols, 60 trading days):
#   BPCL       77.8% win  +₹330  maxDD ₹16   ← small sample (9 trades)
#   ONGC       52.6% win  +₹304  maxDD ₹31
#   IDFCFIRSTB 47.6% win  +₹280  maxDD ₹92   ← new discovery
#   HCLTECH    52.2% win  +₹278  maxDD ₹172
#   DABUR      52.9% win  +₹273  maxDD ₹97   ← new discovery
#   GODREJCP   60.0% win  +₹248  maxDD ₹88   ← new discovery
#   BAJFINANCE 41.7% win  +₹218  maxDD ₹205
#   JSWSTEEL   48.0% win  +₹193  maxDD ₹134
#   DRREDDY    50.0% win  +₹187  maxDD ₹32   ← new discovery
#   SUNPHARMA  56.5% win  +₹169  maxDD ₹102
INDIA_SYMBOLS = [
    "ONGC", "IDFCFIRSTB", "HCLTECH", "DABUR", "GODREJCP",
    "BAJFINANCE", "JSWSTEEL", "DRREDDY", "SUNPHARMA", "BPCL",
]

# Pre-resolved SmartAPI NSE tokens for INDIA_SYMBOLS.
# Avoids repeated searchScrip calls during trading (cuts API calls ~50%).
# Verified live via searchScrip on 2026-06-17. Update if a symbol is renamed.
INDIA_TOKEN_MAP: dict[str, str] = {
    "SUNPHARMA":  "3351",
    "ADANIENT":   "25",
    "JSWSTEEL":   "11723",
    "POWERGRID":  "14977",
    "HCLTECH":    "7229",
    "BAJFINANCE": "317",
    "ONGC":       "2475",
    "RELIANCE":   "2885",
    "BHARTIARTL": "10604",
}

# Symbols proven to lose money on ORB — never trade these.
# Original 9-symbol backtest losers:
#   MARUTI -₹408 | DMART -₹340 | TITAN -₹234 | INFY -₹170 | NTPC -₹167
#   TCS -₹98 (18% win!) | SBIN -₹123 | ICICIBANK -₹114 | KOTAKBANK -₹106
#   WIPRO -₹66 | HDFCLIFE -₹66 | IRCTC -₹71 | BHARTIARTL -₹35
# Full 45-symbol universe backtest additions:
#   ABB -₹740 | EICHERMOT -₹519 | HEROMOTOCO -₹455 | TVSMOTOR -₹256
#   MUTHOOTFIN -₹242 | HAL -₹218 | APOLLOHOSP -₹163 | INDUSINDBK -₹163
#   CHOLAFIN -₹165 | TECHM -₹162 | BAJAJFINSV -₹106 | HAVELLS -₹103
#   AXISBANK -₹76 | TRENT -₹88 | BANKBARODA -₹56 | GRASIM -₹49
INDIA_BLOCKLIST = [
    # Original confirmed losers
    "MARUTI", "DMART", "TITAN", "INFY", "NTPC", "TCS",
    "SBIN", "ICICIBANK", "KOTAKBANK", "WIPRO", "HDFCLIFE",
    "ITC", "IRCTC", "HINDUNILVR", "LT", "HDFCBANK", "BHARTIARTL",
    # Universe backtest new additions
    "ABB", "EICHERMOT", "HEROMOTOCO", "TVSMOTOR", "MUTHOOTFIN",
    "HAL", "APOLLOHOSP", "INDUSINDBK", "CHOLAFIN", "TECHM",
    "BAJAJFINSV", "HAVELLS", "AXISBANK", "TRENT", "BANKBARODA", "GRASIM",
]

