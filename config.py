# ---------------------------------------------------------------------------
# India ORB settings (AngelOne SmartAPI / NSE)
# ---------------------------------------------------------------------------

# Opening range = first 6 × 5-min bars (09:15–09:45 IST)
INDIA_ORB_RANGE_BARS = 6

# EOD close: 20 min before NSE MIS auto-squareoff at 15:30 IST.
# Previously 15:15 — orders placed at 15:15-16 were REJECTED because the broker's
# own squareoff system was already competing for the same positions.
# Moving to 15:10 gives a clean 20-min buffer before broker auto-squareoff.
INDIA_CLOSE_HOUR   = 15
INDIA_CLOSE_MINUTE = 10

# Capital allocation per trade (INR)
# Current budget: ₹15,000 total → 3 concurrent positions × ₹5,000 each.
# As account equity grows, increase both values together:
#   ₹30k budget → ₹10k/trade, max 3  |  ₹75k budget → ₹25k/trade, max 3
INDIA_POSITION_SIZE_INR    = 5000   # ₹5,000 per trade
INDIA_MAX_TOTAL_INR        = 15000  # max ₹15,000 deployed at once
INDIA_MAX_OPEN_POSITIONS   = 3      # max concurrent positions

# ORB quality filters
INDIA_ORB_MIN_OR_PCT        = 0.003  # 0.3% min OR range — skip flat/indecisive opens
INDIA_ORB_MAX_OR_PCT        = 0.020  # 2.0% max OR range — skip gap/spike days
                                      # Backtest: >2% OR → 35% win rate, -₹281 (vs 48% overall)
                                      # Wide ranges = noisy, stops blow out too easily
INDIA_ORB_PROFIT_MULTIPLIER = 2.5    # target = OR range × 2.5 beyond breakout level  [grid-search optimised]
INDIA_ORB_VOLUME_FACTOR     = 0.5    # breakout volume must be ≥ 0.5× avg OR bar volume [grid-search optimised]
INDIA_ORB_STOP_BUFFER_PCT        = 0.005  # stop = 0.5% beyond OR boundary  [grid-search: PF 1.96 vs 1.55 at 1.0%]
INDIA_ORB_BREAKOUT_STRENGTH_PCT = 0.0    # min % price must clear OR boundary — 0% wins (walk-forward)

# Dual Thrust OR-range gate (India) — same formula as US bot, applied to .NS yfinance data
INDIA_DUAL_THRUST_DAYS = 5
INDIA_DUAL_THRUST_MAX_MULTIPLE = 2.0

# Parabolic SAR trailing stop (India ORB only)
# Wilder defaults: step=0.02, max=0.20 → SAR accelerates from 2% → 20% per new extreme.
# Replaces the binary "move-to-breakeven at 0.5×target" with a smooth dynamic trail.
INDIA_SAR_AF_STEP = 0.02   # acceleration factor step per new extreme
INDIA_SAR_AF_MAX  = 0.20   # maximum acceleration factor

# Directional bias — trade both breakout above AND breakout below (short selling intraday)
INDIA_ALLOW_SHORTS = True

# Entry quality filters
INDIA_SKIP_MONDAY_ENTRIES = False  # 60-day backtest: Mondays not significantly worse on fixed watchlist
INDIA_MAX_ENTRY_HOUR      = 13
INDIA_MAX_ENTRY_MINUTE    = 0      # no new entries after 13:00 IST  [extended from 12:30 — grid-search validated]

# Daily loss circuit-breaker
INDIA_DAILY_LOSS_LIMIT_PCT = 0.05  # stop new entries if day P&L < -5%

# Number of top symbols to pick from NSE_UNIVERSE each day (by prev-day turnover)
INDIA_SCREENER_LIMIT = 15

# Fixed backtested watchlist — selected by 60-day grid-search across 61-symbol NSE universe.
# stop=0.5%, mult=2.5×, vol=0.5× → combined PF 1.96 (vs 1.55 with original params).
# Ranked by profit factor at optimal params:
#   TORNTPHARM  64.9% win  PF 1.60  +₹420   ← #1 by win rate
#   BHARTIARTL  58.8% win  PF 1.67  +₹159   ← removed from blocklist (short backtest was misleading)
#   JSWSTEEL    60.0% win  PF 1.97  +₹193
#   BAJFINANCE  60.6% win  PF 1.67  +₹218
#   GODREJCP    60.0% win  PF 1.65  +₹248
#   HCLTECH     52.2% win  PF 1.58  +₹278
#   INFY        54.8% win  PF 1.49  +₹201   ← removed from blocklist (60-day data shows profit)
#   VEDL        55.0% win  PF 1.45  +₹174   ← new addition
#   DABUR       52.9% win  PF 1.38  +₹273
#   DRREDDY     50.0% win  PF 1.35  +₹187
#   HINDUNILVR  53.3% win  PF 1.30  +₹118   ← removed from blocklist (60-day data shows profit)
#   ONGC        52.6% win  PF 1.28  +₹304
INDIA_SYMBOLS = [
    "TORNTPHARM", "BHARTIARTL", "JSWSTEEL", "BAJFINANCE", "GODREJCP",
    "HCLTECH", "INFY", "VEDL", "DABUR", "DRREDDY", "HINDUNILVR", "ONGC",
]

# Pre-resolved SmartAPI NSE tokens for INDIA_SYMBOLS.
# Avoids repeated searchScrip calls during trading (cuts API calls ~50%).
# Verified live via searchScrip on 2026-06-17. Update if a symbol is renamed.
INDIA_TOKEN_MAP: dict[str, str] = {
    # Tokens verified via searchScrip / ScripMaster for fixed 12-symbol watchlist.
    # ScripMaster auto-resolves any missing entries at startup; this map just speeds up init.
    "JSWSTEEL":   "11723",
    "BAJFINANCE": "317",
    "ONGC":       "2475",
    "BHARTIARTL": "10604",
    "HCLTECH":    "7229",
    "INFY":       "1594",
    "DRREDDY":    "881",
    "HINDUNILVR": "1394",
    "VEDL":       "3063",
}

# Symbols proven to lose money on ORB — never trade these.
# Original 9-symbol backtest losers (short backtest, some later vindicated by 60-day data):
#   MARUTI -₹408 | DMART -₹340 | TITAN -₹234 | NTPC -₹167
#   TCS -₹98 (18% win!) | SBIN -₹123 | ICICIBANK -₹114 | KOTAKBANK -₹106
#   WIPRO -₹66 | HDFCLIFE -₹66 | IRCTC -₹71
# NOTE: BHARTIARTL, HINDUNILVR, INFY were here but removed after 60-day grid-search
#   showed positive PF (1.67, 1.30, 1.49 respectively) — short early backtest was misleading.
# Full 45-symbol universe backtest additions:
#   ABB -₹740 | EICHERMOT -₹519 | HEROMOTOCO -₹455 | TVSMOTOR -₹256
#   MUTHOOTFIN -₹242 | HAL -₹218 | APOLLOHOSP -₹163 | INDUSINDBK -₹163
#   CHOLAFIN -₹165 | TECHM -₹162 | BAJAJFINSV -₹106 | HAVELLS -₹103
#   AXISBANK -₹76 | TRENT -₹88 | BANKBARODA -₹56 | GRASIM -₹49
INDIA_BLOCKLIST = [
    # Original confirmed losers
    "MARUTI", "DMART", "TITAN", "NTPC", "TCS",
    "SBIN", "ICICIBANK", "KOTAKBANK", "WIPRO", "HDFCLIFE",
    "ITC", "IRCTC", "LT", "HDFCBANK",
    # Universe backtest new additions
    "ABB", "EICHERMOT", "HEROMOTOCO", "TVSMOTOR", "MUTHOOTFIN",
    "HAL", "APOLLOHOSP", "INDUSINDBK", "CHOLAFIN", "TECHM",
    "BAJAJFINSV", "HAVELLS", "AXISBANK", "TRENT", "BANKBARODA", "GRASIM",
]

# ---------------------------------------------------------------------------
# Supply/Demand Zone strategy — symbol universe (strategies/zone_detector.py)
# ---------------------------------------------------------------------------
# Deliberately SEPARATE from INDIA_SYMBOLS/INDIA_BLOCKLIST above — those were
# curated and validated specifically for the ORB strategy (grid-search PF,
# blocklist entries tied to ORB backtest losses). The zone strategy is a
# different trading style with no reason to inherit ORB-specific exclusions
# -- e.g. ICICIBANK is ORB-blocklisted but produced a clean RBD reversal zone
# (strength 8.04) that matched a hand-verified real chart example.
#
# No blocklist here (yet) -- nothing's been backtested long enough on the
# zone strategy to justify excluding a symbol the way INDIA_BLOCKLIST does.
ZONE_SYMBOLS = [
    # Original 12-symbol ORB watchlist (kept — no reason to exclude a priori)
    "TORNTPHARM", "BHARTIARTL", "JSWSTEEL", "BAJFINANCE", "GODREJCP",
    "HCLTECH", "INFY", "VEDL", "DABUR", "DRREDDY", "HINDUNILVR", "ONGC",
    # 45-symbol universe from the ORB universe backtest (reused as a
    # convenient liquid-NSE-stock starting point, not because it's ORB-tuned)
    "BPCL", "IDFCFIRSTB", "SUNPHARMA", "HINDALCO", "TATACONSUM", "POWERGRID",
    "ADANIENT", "MARICO", "BRITANNIA", "RELIANCE", "DIVISLAB", "SIEMENS",
    "PIDILITIND", "COALINDIA", "TATASTEEL", "CIPLA", "BEL", "FEDERALBNK",
    "ULTRACEMCO", "GRASIM", "BANKBARODA", "AXISBANK", "TRENT", "HAVELLS",
    "BAJAJFINSV", "TECHM", "INDUSINDBK", "APOLLOHOSP", "CHOLAFIN", "HAL",
    "MUTHOOTFIN", "TVSMOTOR", "HEROMOTOCO", "EICHERMOT", "ABB",
    # Added after user-provided reference chart examples (BANDHANBNK,
    # ICICIBANK) confirmed the detector against real reversal zones
    "BANDHANBNK", "ICICIBANK",
    # Broader Nifty50/Nifty100 liquid names not yet covered above — grows the
    # candidate pool for the confluence backtest (small sample size was the
    # limiting factor, not the detector logic).
    # TATAMOTORS and LTIM excluded — yfinance returns no data under either
    # ticker as of 2026-07, likely stale post-split/rename; not worth chasing.
    "HDFCBANK", "TCS", "SBIN", "KOTAKBANK", "ITC", "LT", "MARUTI", "WIPRO",
    "HDFCLIFE", "NTPC", "ASIANPAINT", "NESTLEIND", "TITAN", "BAJAJ-AUTO",
    "ADANIPORTS", "SBILIFE", "M&M", "UPL", "SHREECEM",
    "TATAPOWER", "NAUKRI", "GAIL", "IOC", "AMBUJACEM", "LUPIN",
    "AUROPHARMA", "COLPAL", "DLF",
]
