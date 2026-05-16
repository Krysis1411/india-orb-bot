BACKTEST_STARTING_BALANCE = 2000.0

# ---------------------------------------------------------------------------
# ORB day trading settings
# ---------------------------------------------------------------------------

ORB_SYMBOLS = [
    # Penny / High-Volatility Stocks
    "MULN",  # Mullen Automotive
    "SNDL",  # Sundial Growers
    "ZOM",   # Zomedica
    "CTRM",  # Castor Maritime
    "TNXP",  # Tonix Pharmaceuticals
    "IDEX",  # Ideanomics
    "FCEL",  # FuelCell Energy
    "JAGX",  # Jaguar Health
    "CEI",   # Camber Energy
    "GEVO",  # Gevo, Inc.
    "ATOS",  # Atossa Therapeutics
    "OCGN",  # Ocugen
    "SENS",  # Senseonics
    "BNGO",  # Bionano Genomics
    "ANY",   # Sphere 3D
    "HUT",   # Hut 8
    "NAK",   # Northern Dynasty
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
