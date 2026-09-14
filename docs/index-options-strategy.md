# NIFTY/BANKNIFTY Index Options Bot — Reference

Backtest: [`backtest/index_options_backtest.py`](../backtest/index_options_backtest.py) (uses a
Black-Scholes simulation via [`ml/gbs.py`](../ml/gbs.py)).
Live (paper): [`index_options_paper_trader.py`](../index_options_paper_trader.py) — no real orders,
ever.

Sibling to the equity zone strategy — same signal pipeline
([`strategies/zone_detector.py`](../strategies/zone_detector.py) +
[`strategies/entry_confirmation.py`](../strategies/entry_confirmation.py), unchanged), applied to
the NIFTY/BANKNIFTY index instead of single stocks. See
[docs/zone-strategy.md](zone-strategy.md) for the shared zone-detection and entry-confirmation
mechanics — this doc only covers what's different for indices.

## What's different from the equity zone strategy

1. **Volume substitution.** The index itself always reports zero traded volume — it's a computed
   value, not a traded instrument. Both the backtest and the live paper trader instead fetch the
   nearest-expiry NFO **futures** contract for the same underlying
   (`AngelOneClient.get_near_month_future`) and substitute its real 5-min volume, since breakout
   and reaction-candle detection both require a real volume series.
2. **A zone signal isn't traded directly — it's translated into a real option contract.** The
   underlying's zone (stop/target/trailing, identical logic to the equity strategy) still decides
   **when** to exit; only the recorded P&L is the option's.
3. **Live paper trading uses the option's real quoted LTP.** `backtest/index_options_backtest.py`
   has no choice but to simulate a theoretical price via Black-Scholes (`ml/gbs.py`) — expired
   option contracts vanish from AngelOne's instrument list, so there's no real quote to look back
   at. `index_options_paper_trader.py` has no such gap and always uses
   `AngelOneClient.resolve_option(...)`'s real live LTP instead.

## Expiry and strike selection

| Underlying | Expiry convention | Strike interval |
|---|---|---|
| NIFTY | nearest **weekly** Tuesday, ≥ `MIN_DTE_DAYS` (2) days out | 50 |
| BANKNIFTY | nearest **monthly** (last Tuesday of the month), ≥ `MIN_DTE_DAYS` (2) days out | 100 |

BANKNIFTY is monthly-only because SEBI's October 2024 single-weekly-expiry-per-exchange rule
discontinued BANKNIFTY weeklies — only the last-Tuesday monthly contract trades now. This is a
regulatory fact, not a tuning choice; don't "fix" it back to weekly if BANKNIFTY looks
under-traded.

Strike is chosen ATM: nearest exchange-listed strike to spot at entry (`round(spot / interval) ×
interval` in the backtest; `AngelOneClient.pick_strike(..., offset_steps=0)` live, which reads the
actual listed strike ladder from ScripMaster rather than assuming the nominal interval always
holds).

- `option_type` follows the equity zone's direction: `"c"`/`CE` for a long (demand) signal,
  `"p"`/`PE` for a short (supply) signal.
- The backtest rejects a resolved option outright if its simulated entry price is `≤ ₹0.01` —
  too far OTM / a degenerate price isn't a realistically tradeable premium.
- A contract can't be held past its own expiry: if the underlying trade's exit would run past
  expiry, the backtest clips the exit to the expiry date and settles at intrinsic value there.

## Backtest pricing model (`ml/gbs.py`)

`ml/gbs.py` is a general closed-form options-pricing library (European/American/Asian/spread
options, adapted from Davis Edwards' *Energy Trading and Investing* books) — only the
European/American Black-Scholes pieces are actually used here, invoked by
`backtest/index_options_backtest.py::_price()`. This is a **simulation**, not a historical option
quote — treat backtest P&L numbers as directionally indicative of what the underlying signal is
worth in options terms, not as a validated tradeable edge, since a real order book (bid/ask
spread, actual IV skew, liquidity at the specific strike) is never consulted.

## Symbol universe

Fixed: `NIFTY` and `BANKNIFTY` only (`brokers.angelone.INDEX_TOKENS`, using AngelOne's
current-generation `99926xxx` index spot tokens — the older `26000`/`26009` tokens are deprecated).
There is no equivalent of `ZONE_SYMBOLS` here since there are only two underlyings.

## Validation status

Same caveats as [docs/zone-strategy.md](zone-strategy.md#validation-status--read-before-trusting-any-number)
apply: no transaction costs modeled anywhere in this pipeline (and for a real options trade,
bid/ask spread plus exchange/brokerage charges matter more than for equities, not less), and the
backtest's Black-Scholes prices are a simulation layered on top of a strategy that is itself still
in-sample-tuned. `index_options_paper_trader.py`'s real-LTP paper trades are the only
out-of-sample, real-market read this bot has.
