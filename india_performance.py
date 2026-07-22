"""
Session P&L tracking and performance reporting for the India ORB bot.

Two responsibilities:
  1. Append daily session results to logs/india_pnl_history.csv — one row per
     trading day, accumulated across all sessions.
  2. Generate a quantstats HTML tearsheet from the full history whenever
     requested (called at bot shutdown after the session ends).

Usage (from india_orb_bot.py):
    import india_performance as perf
    perf.log_session(date, pnl_inr, start_equity, trades, wins, losses)
    perf.generate_report()
    perf.print_summary(...)
"""
from __future__ import annotations

import csv
import logging
from datetime import date
from pathlib import Path

log = logging.getLogger(__name__)

_LOG_DIR = Path(__file__).parent / "logs"
_PNL_CSV = _LOG_DIR / "india_pnl_history.csv"
_REPORT_HTML = _LOG_DIR / "india_performance_report.html"
_CSV_FIELDS = ["date", "pnl_inr", "return_pct", "start_equity", "trades", "wins", "losses"]


# ---------------------------------------------------------------------------
# Daily logging
# ---------------------------------------------------------------------------

def log_session(
    session_date: date,
    pnl_inr: float,
    start_equity: float,
    trades: int = 0,
    wins: int = 0,
    losses: int = 0,
) -> None:
    """Append today's session result to the persistent CSV history file."""
    _LOG_DIR.mkdir(exist_ok=True)
    return_pct = (pnl_inr / start_equity) if start_equity > 0 else 0.0
    row = {
        "date": session_date.isoformat(),
        "pnl_inr": round(pnl_inr, 2),
        "return_pct": round(return_pct, 6),
        "start_equity": round(start_equity, 2),
        "trades": trades,
        "wins": wins,
        "losses": losses,
    }
    needs_header = not _PNL_CSV.exists()
    with open(_PNL_CSV, "a", newline="", encoding="utf-8") as f:
        writer = csv.DictWriter(f, fieldnames=_CSV_FIELDS)
        if needs_header:
            writer.writeheader()
        writer.writerow(row)
    log.info(
        f"Session logged → {_PNL_CSV.name}"
        f" | ₹{pnl_inr:+.0f} ({return_pct:+.2%})"
        f" | {wins}W / {losses}L / {trades} trades"
    )


# ---------------------------------------------------------------------------
# Quantstats HTML report
# ---------------------------------------------------------------------------

def generate_report(output_path: Path | None = None) -> Path | None:
    """
    Read the full P&L history CSV and generate a quantstats HTML tearsheet.
    Returns the report path on success, None if skipped or failed.
    """
    try:
        import pandas as pd
        import quantstats_lumi as qs
    except ImportError:
        log.warning("quantstats-lumi not installed — skipping report (pip install quantstats-lumi)")
        return None

    if not _PNL_CSV.exists():
        log.info("No P&L history yet — report will be generated after the first full session")
        return None

    try:
        df = pd.read_csv(_PNL_CSV, parse_dates=["date"], index_col="date")
        df = df[~df.index.duplicated(keep="last")].sort_index()   # keep latest row per date

        if len(df) < 2:
            log.info(f"Only {len(df)} session(s) recorded — skipping report (need ≥2 days)")
            return None

        returns = df["return_pct"].astype(float)
        returns.index = pd.DatetimeIndex(returns.index)
        returns.index.name = "Date"

        target = output_path or _REPORT_HTML
        qs.reports.html(
            returns,
            output=str(target),
            title="India ORB Bot — ORB + VWAP + EMA",
            download_filename=target.name,
        )
        log.info(f"Performance report → {target}")
        return target

    except Exception as exc:
        log.error(f"Report generation failed: {exc}")
        return None


# ---------------------------------------------------------------------------
# Console session summary
# ---------------------------------------------------------------------------

def print_summary(
    pnl_inr: float,
    start_equity: float,
    trades: int,
    wins: int,
    losses: int,
) -> None:
    """Log a clean end-of-session P&L block to the bot logger."""
    ret_pct = pnl_inr / start_equity * 100 if start_equity > 0 else 0.0
    win_rate = wins / trades * 100 if trades > 0 else 0.0
    sep = "=" * 55
    log.info(sep)
    log.info("SESSION COMPLETE")
    log.info(f"  P&L:      ₹{pnl_inr:+,.0f}  ({ret_pct:+.2f}%)")
    log.info(f"  Trades:   {trades}  ({wins}W / {losses}L)  |  Win rate: {win_rate:.0f}%")
    log.info(f"  Equity:   ₹{start_equity:,.0f} → ₹{start_equity + pnl_inr:,.0f}")
    log.info(sep)


# ---------------------------------------------------------------------------
# Standalone: print current history summary
# ---------------------------------------------------------------------------

if __name__ == "__main__":
    import sys
    logging.basicConfig(level=logging.INFO, format="%(asctime)s | %(message)s")

    if not _PNL_CSV.exists():
        print(f"No history file found at {_PNL_CSV}")
        sys.exit(0)

    try:
        import pandas as pd
        df = pd.read_csv(_PNL_CSV, parse_dates=["date"])
        df = df[~df.duplicated("date", keep="last")].sort_values("date")
        print(f"\n{'Date':<12}  {'P&L (₹)':>10}  {'Return':>8}  {'Trades':>7}  {'W/L':>6}")
        print("-" * 55)
        total_pnl = 0.0
        for _, row in df.iterrows():
            wl = f"{int(row['wins'])}W/{int(row['losses'])}L"
            print(
                f"{row['date'].strftime('%Y-%m-%d'):<12}"
                f"  {row['pnl_inr']:>+10.0f}"
                f"  {row['return_pct']:>+7.2%}"
                f"  {int(row['trades']):>7}"
                f"  {wl:>6}"
            )
            total_pnl += row["pnl_inr"]
        print("-" * 55)
        print(f"{'TOTAL':<12}  {total_pnl:>+10.0f}")
        print()

        path = generate_report()
        if path:
            print(f"Report saved → {path}")
    except Exception as e:
        print(f"Error: {e}")
