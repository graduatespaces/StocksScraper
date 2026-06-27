#!/usr/bin/env python3
"""
Rolling threshold calibration — derives adaptive parameters from 5 years
of Robinhood historicals and writes calibrated_params.json.

Run weekly (or manually) to keep thresholds current:
    python calibrate.py

seasonal_sizing.py loads calibrated_params.json at import time and uses
these values instead of its hardcoded constants. If the file is absent or
stale it falls back to the hardcoded defaults gracefully.

Parameters calibrated:
  rsi_threshold          Per-symbol RSI level where oversold signal fires.
                         Replaces the fixed RSI_OVERSOLD = 45.
                         Method: find the RSI band (tested 30–55) that
                         produced the best mean 5-day forward return
                         historically.

  exit_window_fraction   Per-symbol, per negative-bias-month fraction of
                         trading days to stay out. Replaces hardcoded 0.30
                         (META) and 0.60 (GOOGL/SPY).
                         Method: for each month occurrence, find the first
                         trading day where the average forward-to-month-end
                         return turns positive and stays positive.

  qend_days              How many days before quarter-end to start deploying.
                         Replaces hardcoded 6.
                         Method: find the window length (4–10 days) that
                         maximised average quarter-end rally historically.

  newq_days              How many days into a new quarter to stay deployed.
                         Replaces hardcoded 7.
                         Method: find where average New-Q returns go flat.
"""

import json
import os
import sys
from collections import defaultdict
from datetime import date, datetime, timezone
from pathlib import Path

PARAMS_FILE       = Path(__file__).parent / "calibrated_params.json"
NEG_BIAS_MONTHS   = [2, 6, 9]
QUARTER_END_MONTHS = [3, 6, 9, 12]
QUARTER_STR_MONTHS = [1, 4, 7, 10]

# Symbols that use binary exit windows (momentum symbols use a floor, not a window)
BINARY_EXIT_SYMBOLS = ["META", "GOOGL", "SPY"]


# ── Robinhood ─────────────────────────────────────────────────────────────────────────────

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


def fetch_history(rh, symbol: str) -> tuple[list[date], list[float]]:
    """5 years of daily closes for calibration depth."""
    bars = rh.stocks.get_stock_historicals(
        symbol, interval="day", span="5year", bounds="regular"
    )
    if not bars:
        return [], []
    dates  = [date.fromisoformat(b["begins_at"][:10]) for b in bars]
    closes = [float(b["close_price"]) for b in bars]
    return dates, closes


# ── RSI ───────────────────────────────────────────────────────────────────────────────

def _compute_rsi_series(closes: list[float], period: int = 14) -> list[float | None]:
    """Compute RSI-14 for every bar. Returns None for bars without enough history."""
    rsi_series = [None] * len(closes)
    if len(closes) < period + 1:
        return rsi_series

    alpha    = 1 / period
    deltas   = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    avg_gain = sum(max(d, 0) for d in deltas[:period]) / period
    avg_loss = sum(abs(min(d, 0)) for d in deltas[:period]) / period

    def _rsi(ag, al):
        return 100.0 if al == 0 else 100 - (100 / (1 + ag / al))

    rsi_series[period] = _rsi(avg_gain, avg_loss)

    for i in range(period, len(deltas)):
        g = max(deltas[i], 0)
        l = abs(min(deltas[i], 0))
        avg_gain = alpha * g + (1 - alpha) * avg_gain
        avg_loss = alpha * l + (1 - alpha) * avg_loss
        rsi_series[i + 1] = _rsi(avg_gain, avg_loss)

    return rsi_series


def calibrate_rsi_threshold(
    dates: list[date],
    closes: list[float],
    symbol: str,
    test_range: range = range(30, 56),
    forward_days: int = 5,
) -> float:
    """
    Find the RSI threshold that produced the best mean forward return.

    For each candidate threshold T, collect all days where RSI crossed
    below T (entry signal). Compute mean forward_days return. Return the
    T with highest mean return, constrained to [30, 55].
    """
    rsi_series = _compute_rsi_series(closes)
    best_t, best_mean = 45.0, float("-inf")   # default fallback

    for t in test_range:
        returns = []
        for i, rsi in enumerate(rsi_series):
            if rsi is None:
                continue
            if rsi < t and i + forward_days < len(closes):
                fwd_return = (closes[i + forward_days] - closes[i]) / closes[i]
                returns.append(fwd_return)

        if len(returns) < 5:   # too few samples — skip
            continue
        mean_ret = sum(returns) / len(returns)
        if mean_ret > best_mean:
            best_mean = mean_ret
            best_t = float(t)

    print(f"  {symbol:<6} RSI threshold → {best_t:.0f}  (mean {forward_days}d return: {best_mean:.2%})")
    return best_t


# ── Exit window fraction ─────────────────────────────────────────────────────────

def calibrate_exit_window(
    dates: list[date],
    closes: list[float],
    symbol: str,
) -> dict[str, float]:
    """
    For each negative-bias month, find the trading-day fraction where the
    average forward-to-month-end return first turns and stays positive.

    Returns {str(month): fraction} e.g. {"2": 0.28, "6": 0.35, "9": 0.52}
    Fraction = (re-entry day - 1) / total_days_in_month.
    """
    date_index = {d: i for i, d in enumerate(dates)}
    results: dict[str, float] = {}

    for month in NEG_BIAS_MONTHS:
        # Group trading days by (year, month)
        month_groups: dict[tuple, list[date]] = defaultdict(list)
        for d in dates:
            if d.month == month:
                month_groups[(d.year, d.month)].append(d)

        if len(month_groups) < 3:
            results[str(month)] = 0.50   # not enough history — conservative default
            continue

        # For each (year,month), compute return from day d to last day of month
        day_returns: dict[int, list[float]] = defaultdict(list)

        for (yr, mo), days in month_groups.items():
            days = sorted(days)
            last_idx = date_index.get(days[-1])
            if last_idx is None:
                continue
            last_close = closes[last_idx]

            for pos, d in enumerate(days):
                idx = date_index.get(d)
                if idx is None:
                    continue
                ret = (last_close - closes[idx]) / closes[idx]
                day_returns[pos].append(ret)

        # Find first position where mean return is positive AND stays positive
        total_positions = max(day_returns.keys()) + 1 if day_returns else 1
        re_entry_pos = total_positions  # default: stay out whole month

        for pos in range(total_positions):
            rets = day_returns.get(pos, [])
            if not rets:
                continue
            mean_ret = sum(rets) / len(rets)
            # Check that subsequent positions also have positive mean returns
            remaining_positive = all(
                sum(day_returns.get(p, [0])) / max(len(day_returns.get(p, [1])), 1) >= 0
                for p in range(pos, total_positions)
            )
            if mean_ret > 0 and remaining_positive:
                re_entry_pos = pos
                break

        fraction = round(re_entry_pos / total_positions, 2) if total_positions > 0 else 0.50
        fraction = max(0.10, min(0.80, fraction))   # clamp to [0.10, 0.80]
        results[str(month)] = fraction
        month_name = ["","Jan","Feb","Mar","Apr","May","Jun",
                      "Jul","Aug","Sep","Oct","Nov","Dec"][month]
        print(f"  {symbol:<6} exit window {month_name} → {fraction:.0%}  "
              f"(re-entry at trading day {re_entry_pos}/{total_positions})")

    return results


# ── Quarter-end window ─────────────────────────────────────────────────────────────

def calibrate_qend_days(
    dates: list[date],
    closes: list[float],
    test_range: range = range(4, 11),
) -> int:
    """
    Find the number of pre-quarter-end days that maximises the average
    entry-to-quarter-end return across all historical Q-ends.
    """
    date_index = {d: i for i, d in enumerate(dates)}

    # Identify last trading day of each quarter-end month
    qend_last_days: list[date] = []
    by_month: dict[tuple, list[date]] = defaultdict(list)
    for d in dates:
        by_month[(d.year, d.month)].append(d)

    for (yr, mo), days in by_month.items():
        if mo in QUARTER_END_MONTHS:
            qend_last_days.append(sorted(days)[-1])

    best_n, best_mean = 6, float("-inf")

    for n in test_range:
        returns = []
        for qend in qend_last_days:
            # Find the trading day n days before qend
            month_days = sorted(by_month.get((qend.year, qend.month), []))
            if len(month_days) < n + 1:
                continue
            entry_day = month_days[-(n + 1)]
            ei = date_index.get(entry_day)
            qi = date_index.get(qend)
            if ei is None or qi is None:
                continue
            ret = (closes[qi] - closes[ei]) / closes[ei]
            returns.append(ret)

        if not returns:
            continue
        mean_ret = sum(returns) / len(returns)
        if mean_ret > best_mean:
            best_mean = mean_ret
            best_n = n

    print(f"  Q-end window → {best_n} days  (mean return: {best_mean:.2%})")
    return best_n


def calibrate_newq_days(
    dates: list[date],
    closes: list[float],
    test_range: range = range(3, 12),
) -> int:
    """
    Find the number of New-Q days to stay deployed (where avg return goes flat).
    """
    date_index = {d: i for i, d in enumerate(dates)}

    # First trading day of each new quarter
    by_month: dict[tuple, list[date]] = defaultdict(list)
    for d in dates:
        by_month[(d.year, d.month)].append(d)

    newq_starts: list[date] = []
    for (yr, mo), days in by_month.items():
        if mo in QUARTER_STR_MONTHS:
            newq_starts.append(sorted(days)[0])

    best_n, best_mean = 7, float("-inf")

    for n in test_range:
        returns = []
        for start in newq_starts:
            month_days = sorted(by_month.get((start.year, start.month), []))
            if len(month_days) < n:
                continue
            exit_day = month_days[n - 1]
            si = date_index.get(start)
            ei = date_index.get(exit_day)
            if si is None or ei is None:
                continue
            ret = (closes[ei] - closes[si]) / closes[si]
            returns.append(ret)

        if not returns:
            continue
        mean_ret = sum(returns) / len(returns)
        if mean_ret > best_mean:
            best_mean = mean_ret
            best_n = n

    print(f"  New-Q window → {best_n} days  (mean return: {best_mean:.2%})")
    return best_n


# ── Main calibration runner ────────────────────────────────────────────────────────────

def run_calibration(rh) -> dict:
    from seasonal_sizing import SYMBOL_TYPE
    symbols = list(SYMBOL_TYPE.keys())

    params: dict = {
        "calibrated_at":        datetime.now(timezone.utc).strftime("%Y-%m-%dT%H:%M:%SZ"),
        "rsi_threshold":        {},
        "exit_window_fraction": {},
        "qend_days":            6,
        "newq_days":            7,
    }

    spy_dates, spy_closes = None, None

    for symbol in symbols:
        print(f"\n── {symbol} ──")
        dates, closes = fetch_history(rh, symbol)
        if not dates:
            print(f"  WARNING: No data for {symbol} — skipping")
            continue

        if symbol == "SPY":
            spy_dates, spy_closes = dates, closes

        # RSI threshold (all symbols)
        params["rsi_threshold"][symbol] = calibrate_rsi_threshold(dates, closes, symbol)

        # Exit window (binary-exit symbols only)
        if symbol in BINARY_EXIT_SYMBOLS:
            params["exit_window_fraction"][symbol] = calibrate_exit_window(
                dates, closes, symbol
            )

    # Q-end and New-Q windows use SPY as market proxy
    if spy_dates and spy_closes:
        print("\n── Quarter windows (SPY proxy) ──")
        params["qend_days"] = calibrate_qend_days(spy_dates, spy_closes)
        params["newq_days"] = calibrate_newq_days(spy_dates, spy_closes)

    return params


def main():
    rh = _rh()
    login(rh)

    print("\n" + "=" * 60)
    print("  Calibrating thresholds from 5-year Robinhood history")
    print("=" * 60)

    params = run_calibration(rh)

    PARAMS_FILE.write_text(json.dumps(params, indent=2))
    print(f"\n✓ Saved → {PARAMS_FILE}")
    print("\nFinal parameters:")
    print(json.dumps(params, indent=2))


if __name__ == "__main__":
    main()
