#!/usr/bin/env python3
"""
Daily position sizing report.

Fetches live data from Robinhood, applies seasonal + regime + bear-signal
rules from seasonal_sizing.py and bear_score.py, and prints recommended
allocations for today.

Usage:
    python daily_sizing.py

Requires:
    pip install robin_stocks

Environment:
    ROBINHOOD_USERNAME, ROBINHOOD_PASSWORD
"""

import json
import os
import sys
from datetime import date, timedelta
from pathlib import Path

from seasonal_sizing import get_allocation, SYMBOL_TYPE
from bear_score import (
    compute_rsi14,
    compute_bear_score,
    size_multiplier,
    describe_bear_score,
)

CORE_SYMBOLS       = list(SYMBOL_TYPE.keys())   # NVDA, AVGO, MSFT, META, GOOGL, SPY
STOCKS_FILE        = Path(__file__).parent / "stocks.json"
MACRO_FILE         = Path(__file__).parent / "macro_signals.json"
ACCOUNT_NUMBER     = "699589594"
QUARTER_END_MONTHS = {3, 6, 9, 12}
QUARTER_STR_MONTHS = {1, 4, 7, 10}
QEND_DAYS          = 6
NEW_Q_DAYS         = 7
WIN_RATE_HALVE     = True   # ACTIVE as of Jun 2026 — set False when win rate recovers
VIX_INSTRUMENT_ID  = "3b912aa2-88f9-4682-8ae3-e39520bdf4db"


# ── Macro signals ─────────────────────────────────────────────────────────────

def print_macro_consensus(lookback_days: int = 7):
    if not MACRO_FILE.exists():
        return
    signals = json.loads(MACRO_FILE.read_text())
    if not signals:
        return

    cutoff = (date.today() - timedelta(days=lookback_days)).isoformat()
    recent = [s for s in signals if s.get("date", "") >= cutoff]
    if not recent:
        return

    bias_counts = {"bullish": 0, "bearish": 0, "neutral": 0}
    vix_counts  = {"rising": 0, "falling": 0, "stable": 0}
    sector_agg: dict[str, list[str]] = {}
    all_risks: list[str] = []

    for s in recent:
        if s.get("market_bias") in bias_counts:
            bias_counts[s["market_bias"]] += 1
        if s.get("vix_outlook") in vix_counts:
            vix_counts[s["vix_outlook"]] += 1
        for sector, lean in (s.get("sectors") or {}).items():
            sector_agg.setdefault(sector, []).append(lean)
        all_risks.extend(s.get("risks") or [])

    total = len(recent)
    bull  = bias_counts["bullish"]
    bear  = bias_counts["bearish"]
    net   = "BULLISH" if bull > bear else ("BEARISH" if bear > bull else "MIXED")

    print(f"\n  Analyst Macro Consensus  (last {lookback_days}d — {total} signal(s))")
    print(f"  {'-'*68}")
    print(f"  Market bias   {net}  ({bull} bullish / {bear} bearish / {bias_counts['neutral']} neutral)")

    vix_top = max(vix_counts, key=lambda k: vix_counts[k])
    if vix_counts[vix_top] > 0:
        print(f"  VIX outlook   {vix_top.upper()}  ({vix_counts[vix_top]} of {total} analysts)")

    if sector_agg:
        sector_summary = []
        for sector, leans in sorted(sector_agg.items()):
            dominant = max(set(leans), key=leans.count)
            sector_summary.append(f"{sector}:{dominant}")
        print(f"  Sector leans  {',  '.join(sector_summary)}")

    if all_risks:
        from collections import Counter
        top_risks = [r for r, _ in Counter(all_risks).most_common(4)]
        print(f"  Key risks     {' | '.join(top_risks)}")

    print(f"\n  Recent summaries:")
    for s in sorted(recent, key=lambda x: x["date"], reverse=True)[:4]:
        print(f"    [{s['date']}] {s['source']}: {s['summary']}")


# ── Robinhood ─────────────────────────────────────────────────────────────────

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


# ── Market data ───────────────────────────────────────────────────────────────

def fetch_daily_closes(rh, symbol: str, span: str = "year") -> tuple[list[date], list[float]]:
    bars = rh.stocks.get_stock_historicals(
        symbol, interval="day", span=span, bounds="regular"
    )
    if not bars:
        return [], []
    dates  = [date.fromisoformat(b["begins_at"][:10]) for b in bars]
    closes = [float(b["close_price"]) for b in bars]
    return dates, closes


def fetch_vix(rh) -> float | None:
    try:
        quotes = rh.get_index_quotes([VIX_INSTRUMENT_ID])
        if quotes:
            return float(quotes[0].get("value") or quotes[0].get("last_trade_price") or 0) or None
    except Exception:
        pass
    return None


# ── Regime (hard stop layer, separate from bear score) ───────────────────────

def compute_regime(closes: list[float]) -> tuple[str, float, float]:
    """Hard regime: drives the no-new-entry block in RISK_OFF / DIP_BUYING."""
    if len(closes) < 200:
        return "RISK_ON", 0.0, 0.0
    sma50  = sum(closes[-50:])  / 50
    sma200 = sum(closes[-200:]) / 200
    last   = closes[-1]
    if last < sma200 * 0.95:
        regime = "DIP_BUYING"
    elif last < sma200:
        regime = "RISK_OFF"
    elif last < sma50:
        regime = "EARLY_WARNING"
    else:
        regime = "RISK_ON"
    return regime, sma50, sma200


# ── Trading calendar ──────────────────────────────────────────────────────────

def trading_day_info(spy_dates: list[date], today: date) -> tuple[int, int, bool, bool]:
    month_days = sorted(d for d in spy_dates if d.year == today.year and d.month == today.month)
    if today not in month_days:
        month_days = sorted(set(month_days) | {today})

    total   = len(month_days)
    td      = month_days.index(today) + 1

    is_qend = (
        today.month in QUARTER_END_MONTHS and
        sum(1 for d in month_days if d >= today) <= QEND_DAYS
    )
    is_newq = today.month in QUARTER_STR_MONTHS and td <= NEW_Q_DAYS

    return td, total, is_qend, is_newq


# ── Account ───────────────────────────────────────────────────────────────────

def fetch_equity(rh) -> float | None:
    try:
        profile = rh.account.build_user_profile()
        return float(profile.get("equity", 0) or 0) or None
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

    # ── Analyst macro consensus (from scanner's macro_signals.json) ──────────
    print_macro_consensus()

    # ── SPY: regime, calendar, bear score inputs ──────────────────────────────
    spy_dates, spy_closes = fetch_daily_closes(rh, "SPY", span="year")
    if not spy_closes:
        print("ERROR: Could not fetch SPY historicals")
        sys.exit(1)

    regime, sma50, sma200 = compute_regime(spy_closes)
    spy_last = spy_closes[-1]

    print(f"  SPY       ${spy_last:>8.2f}  |  50d SMA ${sma50:.2f}  |  200d SMA ${sma200:.2f}")
    print(f"  Regime    {regime}  (hard stop layer)")

    td_of_month, total_td_month, is_qend, is_newq = trading_day_info(spy_dates, today)
    print(f"  Month     {today.strftime('%B')}  — trading day {td_of_month} of {total_td_month}")
    print(f"  Q-end     {'YES ✓' if is_qend else 'no'}   |   New-Q  {'YES ✓' if is_newq else 'no'}")

    # ── Fetch all symbol closes (needed for breadth + RSI) ───────────────────
    all_closes: dict[str, list[float]] = {'SPY': spy_closes}
    for sym in CORE_SYMBOLS:
        if sym == 'SPY':
            continue
        _, c = fetch_daily_closes(rh, sym, span="year")
        if c:
            all_closes[sym] = c

    # ── VIX ──────────────────────────────────────────────────────────────────
    vix = fetch_vix(rh)

    # ── Bear score ────────────────────────────────────────────────────────────
    bear, components = compute_bear_score(spy_closes, all_closes, vix)
    bear_mult = size_multiplier(bear)
    describe_bear_score(bear, components, vix)

    # ── Account equity + base size ────────────────────────────────────────────
    equity    = fetch_equity(rh)
    base_size = None
    if equity:
        base_size = equity * 0.10
        adjustments = []
        if WIN_RATE_HALVE:
            base_size *= 0.50
            adjustments.append("×0.50 win-rate halve")
        base_size *= bear_mult
        adjustments.append(f"×{bear_mult:.2f} bear score")
        print(f"\n  Account equity  ${equity:>12,.2f}")
        print(f"  Base size       ${base_size:>12,.2f}  (10% {' '.join(adjustments)})")
    else:
        print("\n  (Account equity unavailable — showing fractions only)")

    # ── Dynamic symbols from stocks.json ─────────────────────────────────────
    dynamic_symbols: list[str] = []
    if STOCKS_FILE.exists():
        picks = json.loads(STOCKS_FILE.read_text())
        dynamic_symbols = [s for s in sorted(picks) if s not in CORE_SYMBOLS]

    # ── Per-symbol sizing ─────────────────────────────────────────────────────
    sections = [
        ("Core strategy symbols", CORE_SYMBOLS),
        ("Scanner picks  (balanced_strict rules)", dynamic_symbols),
    ]

    for title, symbols in sections:
        if not symbols:
            continue
        print(f"\n  {title}")
        print(f"  {'Symbol':<7} {'Type':<16} {'RSI':>6}  {'Alloc':>6}  {'$ Size':>10}  Flags")
        print(f"  {'-'*68}")

        for symbol in symbols:
            closes = all_closes.get(symbol)
            if closes is None:
                try:
                    _, closes = fetch_daily_closes(rh, symbol, span="3month")
                    all_closes[symbol] = closes
                except Exception:
                    closes = []

            rsi = compute_rsi14(closes) if closes else None

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
        print("  * Win-rate halve ACTIVE — set WIN_RATE_HALVE=False when 30d rate recovers above 35%")
    print(f"  $ Size = base_size × seasonal_alloc  (bear score already baked into base_size)")
    print(f"  Max 6 positions.  T+1 settlement — never reuse unsettled proceeds.")
    print(f"  {'=' * 72}\n")


if __name__ == "__main__":
    main()
