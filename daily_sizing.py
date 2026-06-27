#!/usr/bin/env python3
"""
Daily position sizing report.

Fetches live data from Robinhood, applies seasonal + regime + RSI rules
from seasonal_sizing.py, and prints recommended allocations for today.

Usage:
    python daily_sizing.py

Requires:
    pip install robin_stocks numpy

Environment:
    ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD
"""

import json
import os
import sys
from datetime import date, datetime, timezone, timedelta
from pathlib import Path

from seasonal_sizing import get_allocation, SYMBOL_TYPE

CORE_SYMBOLS       = list(SYMBOL_TYPE.keys())   # NVDA, AVGO, MSFT, META, GOOGL, SPY
STOCKS_FILE        = Path(__file__).parent / "stocks.json"
ACCOUNT_NUMBER     = "699589594"
QEND_DAYS          = 6
NEW_Q_DAYS         = 7
QUARTER_END_MONTHS = {3, 6, 9, 12}
QUARTER_STR_MONTHS = {1, 4, 7, 10}
WIN_RATE_HALVE     = True   # ACTIVE as of Jun 2026 — update when win rate recovers


# ── Robinhood login ───────────────────────────────────────────────────────────

def _rh():
    try:
        import robin_stocks.robinhood as rh
        return rh
    except ImportError:
        print("ERROR: robin_stocks not installed. Run: pip install robin_stocks")
        sys.exit(1)


def login(rh):
    username = os.environ.get("ROBINHOOD_USERNAME")
    password = os.environ.get("ROBINHOOD_PASSWORD")
    if not username or not password:
        print("ERROR: Set ROBINHOOD_USERNAME and ROBINHOOD_PASSWORD env vars")
        sys.exit(1)
    rh.login(username, password, store_session=True)


# ── Market data helpers ───────────────────────────────────────────────────────

def fetch_daily_closes(rh, symbol: str, span: str = "year") -> tuple[list[date], list[float]]:
    """Return (dates, closes) for the given span using daily bars."""
    bars = rh.stocks.get_stock_historicals(
        symbol, interval="day", span=span, bounds="regular"
    )
    if not bars:
        return [], []
    dates  = [date.fromisoformat(b["begins_at"][:10]) for b in bars]
    closes = [float(b["close_price"]) for b in bars]
    return dates, closes


def compute_rsi14(closes: list[float]) -> float | None:
    """RSI-14 via EWM smoothing (com=13), requires at least 20 closes."""
    if len(closes) < 20:
        return None
    closes = closes[-20:]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]

    alpha    = 1 / 14
    avg_gain = gains[0]
    avg_loss = losses[0]
    for g, l in zip(gains[1:], losses[1:]):
        avg_gain = alpha * g + (1 - alpha) * avg_gain
        avg_loss = alpha * l + (1 - alpha) * avg_loss

    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


# ── Regime ────────────────────────────────────────────────────────────────────

def compute_regime(closes: list[float]) -> tuple[str, float, float]:
    """Return (regime, sma50, sma200) from SPY closes (need 200+)."""
    if len(closes) < 200:
        return "RISK_ON", 0.0, 0.0
    sma50  = sum(closes[-50:])  / 50
    sma200 = sum(closes[-200:]) / 200
    last   = closes[-1]

    if last < sma200 * 0.95:    # 5%+ below 200d → dip-buying zone
        regime = "DIP_BUYING"
    elif last < sma200:
        regime = "RISK_OFF"
    elif last < sma50:
        regime = "EARLY_WARNING"
    else:
        regime = "RISK_ON"

    return regime, sma50, sma200


# ── Trading calendar helpers ──────────────────────────────────────────────────

def trading_day_info(spy_dates: list[date], today: date) -> tuple[int, int, bool, bool]:
    """
    Derive position-in-month and quarter window flags from SPY trading dates.

    Returns:
        trading_day_of_month        1-indexed day within the current month
        total_trading_days_in_month total trading days in the current month
        is_qend                     True if within last QEND_DAYS of quarter
        is_newq                     True if within first NEW_Q_DAYS of quarter
    """
    # Days in current calendar month from the historical record
    month_days = sorted(d for d in spy_dates if d.year == today.year and d.month == today.month)

    # If today isn't in the record yet (market not closed / weekend), append it
    if today not in month_days:
        month_days = sorted(set(month_days) | {today})

    total_trading_days_in_month = len(month_days)
    trading_day_of_month = month_days.index(today) + 1

    # Q-end: last QEND_DAYS trading days of Mar / Jun / Sep / Dec
    is_qend = False
    if today.month in QUARTER_END_MONTHS:
        remaining = sum(1 for d in month_days if d >= today)
        is_qend   = remaining <= QEND_DAYS

    # New-Q: first NEW_Q_DAYS trading days of Jan / Apr / Jul / Oct
    is_newq = today.month in QUARTER_STR_MONTHS and trading_day_of_month <= NEW_Q_DAYS

    return trading_day_of_month, total_trading_days_in_month, is_qend, is_newq


# ── Account equity ────────────────────────────────────────────────────────────

def fetch_equity(rh) -> float | None:
    try:
        profile = rh.account.build_user_profile()
        return float(profile.get("equity", 0) or 0)
    except Exception:
        return None


# ── Main ──────────────────────────────────────────────────────────────────────

def main():
    rh = _rh()
    login(rh)

    today = date.today()

    print(f"\n{'=' * 72}")
    print(f"  Daily Sizing Report — {today.strftime('%A, %b %d %Y')}")
    print(f"{'=' * 72}\n")

    # SPY: regime + trading calendar
    spy_dates, spy_closes = fetch_daily_closes(rh, "SPY", span="year")
    if not spy_closes:
        print("ERROR: Could not fetch SPY historicals")
        sys.exit(1)

    regime, sma50, sma200 = compute_regime(spy_closes)
    spy_last = spy_closes[-1]

    print(f"  SPY       ${spy_last:>8.2f}  |  50d SMA ${sma50:.2f}  |  200d SMA ${sma200:.2f}")
    print(f"  Regime    {regime}")

    td_of_month, total_td_month, is_qend, is_newq = trading_day_info(spy_dates, today)
    month_name = today.strftime("%B")
    print(f"  Month     {month_name}  — trading day {td_of_month} of {total_td_month}")
    print(f"  Q-end     {'YES ✓ (last 6 trading days of quarter)' if is_qend else 'no'}")
    print(f"  New-Q     {'YES ✓ (first 7 trading days of quarter)' if is_newq else 'no'}")

    # Account equity + base size
    equity    = fetch_equity(rh)
    base_size = None
    if equity:
        base_size = equity * 0.10
        if WIN_RATE_HALVE:
            base_size *= 0.50
        print(f"\n  Account equity  ${equity:>12,.2f}")
        print(f"  Base size (10%){' × 0.5 win-rate halve' if WIN_RATE_HALVE else '':25s}  ${base_size:>10,.2f}")
    else:
        print("\n  (Account equity unavailable — showing fractions only)")

    # Dynamic symbols from stocks.json (treated as balanced_strict)
    dynamic_symbols = []
    if STOCKS_FILE.exists():
        scanner_picks = json.loads(STOCKS_FILE.read_text())
        dynamic_symbols = [s for s in sorted(scanner_picks) if s not in CORE_SYMBOLS]

    all_sections = [
        ("Core strategy symbols", CORE_SYMBOLS),
        ("Scanner picks (balanced_strict rules)", dynamic_symbols),
    ]

    for section_title, symbols in all_sections:
        if not symbols:
            continue
        print(f"\n  {section_title}")
        print(f"  {'Symbol':<7} {'Type':<16} {'RSI':>6}  {'Alloc':>6}  {'$ Size':>10}  Flags")
        print(f"  {'-'*68}")

        for symbol in symbols:
            try:
                _, closes = fetch_daily_closes(rh, symbol, span="3month")
                rsi = compute_rsi14(closes)
            except Exception:
                rsi = None

            alloc = get_allocation(
                symbol=symbol,
                month=today.month,
                rsi=rsi,
                trading_day_of_month=td_of_month,
                total_trading_days_in_month=total_td_month,
                is_qend=is_qend,
                is_newq=is_newq,
                regime=regime,
            )

            rsi_str  = f"{rsi:5.1f}" if rsi is not None else "  N/A"
            size_str = f"${base_size * alloc:>9,.0f}" if base_size is not None else "         —"
            sym_type = SYMBOL_TYPE.get(symbol, "balanced_strict")

            flags = []
            if rsi is not None and rsi < 45:
                flags.append("RSI<45")
            if is_qend:
                flags.append("Q-end")
            if is_newq:
                flags.append("New-Q")

            print(f"  {symbol:<7} {sym_type:<16} {rsi_str}  {alloc:>5.0%}  {size_str}  {', '.join(flags)}")

    print(f"\n  {'=' * 72}")
    if WIN_RATE_HALVE:
        print("  * Win-rate halve ACTIVE (30d win rate < 35%) — base size × 0.5")
    print(f"  $ Size = base_size × seasonal_alloc (regime cap already applied in alloc)")
    print(f"  Max 6 positions. T+1 settlement — do not reuse unsettled proceeds.")
    print(f"  {'=' * 72}\n")


if __name__ == "__main__":
    main()
