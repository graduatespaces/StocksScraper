#!/usr/bin/env python3
"""
Seasonal position sizing — Final rule set from v4 backtest (Jun 2026).

Returns target allocation fraction (0.0–1.0) for a given symbol/date/RSI/regime.
Multiply the result by base_position_size to get the final dollar amount.

Derived from 4-version backtest series (2018–2025). Key findings:
  - momentum (NVDA/AVGO/MSFT): 90% floor in neg-bias months → near buy-and-hold CAGR
  - balanced (META): binary exit, 30% exit window → within 0.7% of binary CAGR
  - balanced-strict (GOOGL/SPY): binary exit, 60% exit window → binary B still best

Usage:
    from seasonal_sizing import get_allocation, describe_allocation

    alloc = get_allocation(
        symbol='NVDA',
        month=9,
        rsi=42.0,
        trading_day_of_month=3,       # 1-indexed
        total_trading_days_in_month=20,
        is_qend=False,
        is_newq=False,
        regime='RISK_ON',
    )
    # returns 0.90  (90% floor — Sep is negative-bias, momentum type)
"""

# ─── SYMBOL CLASSIFICATION ────────────────────────────────────────────────────

SYMBOL_TYPE = {
    # momentum: secular compounders — never fully exit, 90% floor in bad months
    'NVDA': 'momentum',
    'AVGO': 'momentum',
    'MSFT': 'momentum',
    # balanced: binary exit, shorter 30% window recovers enough upside
    'META': 'balanced',
    # balanced-strict: binary exit, full 60% window — bad months are genuinely bad
    'GOOGL': 'balanced_strict',
    'SPY':   'balanced_strict',
}

# ─── MONTHLY BIAS ─────────────────────────────────────────────────────────────
# +1 = accumulate   0 = cautious (aggressive: stay in)   -1 = negative-bias

MONTHLY_BIAS = {
    1: 1, 2: -1, 3: 1, 4: 1, 5: 0, 6: -1,
    7: 1, 8: 0,  9: -1, 10: 0, 11: 1, 12: 1,
}

# ─── EXIT WINDOW per type ─────────────────────────────────────────────────────
# Fraction of the month's trading days to stay out during negative-bias months.
# After this window, re-enter at 100%.

NEG_EARLY_CUTOFF = {
    'momentum':       None,   # never exit — uses floor instead
    'balanced':       0.30,   # out first 30% (~6 trading days)
    'balanced_strict': 0.60,  # out first 60% (~13 trading days)
}

# ─── ALLOCATION FLOORS ────────────────────────────────────────────────────────
# Fraction of base position to keep in market under each condition.

ALLOC = {
    'momentum': {
        'positive':  1.00,   # positive month → fully in
        'cautious':  1.00,   # May/Aug/Oct → aggressive: don't trim
        'neg_early': 0.90,   # neg-bias month → 90% floor, never exit
        'neg_late':  1.00,   # after early window → fully in
        'q_end':     1.00,   # Q-end last 6 days → full deploy
        'new_q':     1.00,   # new quarter first 7 days → full
        'oversold':  1.00,   # RSI < 45 → always full
    },
    'balanced': {
        'positive':  1.00,
        'cautious':  1.00,
        'neg_early': 0.00,   # binary exit — first 30% of bad month → cash
        'neg_late':  1.00,   # back in fully after early window
        'q_end':     1.00,
        'new_q':     1.00,
        'oversold':  1.00,
    },
    'balanced_strict': {
        'positive':  1.00,
        'cautious':  1.00,
        'neg_early': 0.00,   # binary exit — first 60% of bad month → cash
        'neg_late':  1.00,
        'q_end':     1.00,
        'new_q':     1.00,
        'oversold':  1.00,
    },
}

# ─── UNIVERSAL THRESHOLDS ────────────────────────────────────────────────────

RSI_OVERSOLD = 45    # override to full size when RSI < this
QEND_DAYS    = 6     # last N trading days of quarter
NEW_Q_DAYS   = 7     # first N trading days of quarter

# ─── REGIME CAPS ──────────────────────────────────────────────────────────────

REGIME_CAPS = {
    'RISK_ON':        1.00,
    'EARLY_WARNING':  0.50,   # normal cap in EW
    'EARLY_WARNING_OVERRIDE': 0.75,   # Q-end or RSI<45 in EW mode
    'RISK_OFF':       0.00,   # no new entries
    'DIP_BUYING':     0.25,   # starter positions only
}


# ─── MAIN FUNCTION ────────────────────────────────────────────────────────────

def get_allocation(
    symbol: str,
    month: int,
    rsi: float | None,
    trading_day_of_month: int,
    total_trading_days_in_month: int,
    is_qend: bool,
    is_newq: bool,
    regime: str = 'RISK_ON',
) -> float:
    """
    Return target allocation fraction (0.0–1.0).

    Args:
        symbol:                     ticker (must be in SYMBOL_TYPE or defaults to 'balanced_strict')
        month:                      calendar month (1–12)
        rsi:                        RSI-14 value, or None if unavailable
        trading_day_of_month:       1-indexed position within the month
        total_trading_days_in_month: total trading days in current month
        is_qend:                    True if within last QEND_DAYS trading days of quarter
        is_newq:                    True if within first NEW_Q_DAYS trading days of quarter
        regime:                     RISK_ON | EARLY_WARNING | RISK_OFF | DIP_BUYING

    Returns:
        float between 0.0 and 1.0 — multiply by base_position_size
    """
    # Regime hard stops
    if regime == 'RISK_OFF':
        return 0.0

    sym_type = SYMBOL_TYPE.get(symbol, 'balanced_strict')
    cfg      = ALLOC[sym_type]
    bias     = MONTHLY_BIAS.get(month, 0)
    cutoff   = NEG_EARLY_CUTOFF[sym_type]
    oversold = (rsi is not None) and (rsi < RSI_OVERSOLD)

    # Priority: overrides first, then seasonal
    is_override = False
    if oversold:
        seasonal = cfg['oversold']
        is_override = True
    elif is_qend:
        seasonal = cfg['q_end']
        is_override = True
    elif is_newq:
        seasonal = cfg['new_q']
        is_override = True
    elif bias == -1:
        if cutoff is None:
            # momentum: use floor, never exit
            seasonal = cfg['neg_early']
        else:
            in_early_window = trading_day_of_month <= max(1, int(total_trading_days_in_month * cutoff))
            seasonal = cfg['neg_early'] if in_early_window else cfg['neg_late']
    elif bias == 1:
        seasonal = cfg['positive']
    else:   # bias == 0, cautious month
        seasonal = cfg['cautious']

    # Regime cap
    if regime == 'DIP_BUYING':
        cap = REGIME_CAPS['DIP_BUYING']
    elif regime == 'EARLY_WARNING':
        cap = REGIME_CAPS['EARLY_WARNING_OVERRIDE'] if is_override else REGIME_CAPS['EARLY_WARNING']
    else:
        cap = REGIME_CAPS['RISK_ON']

    return min(seasonal, cap)


# ─── HELPER: HUMAN-READABLE EXPLANATION ──────────────────────────────────────

def describe_allocation(
    symbol: str,
    month: int,
    rsi: float | None,
    trading_day_of_month: int,
    total_trading_days_in_month: int,
    is_qend: bool,
    is_newq: bool,
    regime: str = 'RISK_ON',
) -> float:
    """Print explanation and return allocation fraction."""
    alloc    = get_allocation(symbol, month, rsi, trading_day_of_month,
                              total_trading_days_in_month, is_qend, is_newq, regime)
    sym_type = SYMBOL_TYPE.get(symbol, 'balanced_strict')
    bias     = MONTHLY_BIAS.get(month, 0)
    bias_str = {1: 'positive', 0: 'cautious', -1: 'negative'}[bias]
    cutoff   = NEG_EARLY_CUTOFF[sym_type]

    reasons = []
    if regime in ('RISK_OFF', 'DIP_BUYING'):
        reasons.append(f'regime={regime}')
    if rsi is not None and rsi < RSI_OVERSOLD:
        reasons.append(f'RSI={rsi:.1f} < {RSI_OVERSOLD} (oversold override)')
    if is_qend:
        reasons.append(f'Q-end window (last {QEND_DAYS} trading days of quarter)')
    if is_newq:
        reasons.append(f'New-Q window (first {NEW_Q_DAYS} trading days of quarter)')
    if not reasons:
        if bias == -1 and cutoff is not None:
            window_days = max(1, int(total_trading_days_in_month * cutoff))
            in_early = trading_day_of_month <= window_days
            label = f'neg-bias month early window (day {trading_day_of_month}/{window_days})' if in_early \
                    else f'neg-bias month late window (day {trading_day_of_month}, past {window_days}-day block)'
            reasons.append(label)
        elif bias == -1:
            reasons.append(f'neg-bias month, momentum floor (day {trading_day_of_month})')
        else:
            reasons.append(f'{bias_str} month')

    month_name = ['','Jan','Feb','Mar','Apr','May','Jun',
                  'Jul','Aug','Sep','Oct','Nov','Dec'][month]
    print(f'{symbol:<6} [{sym_type:<14}] | {regime:<16} | {month_name} day {trading_day_of_month} '
          f'| {", ".join(reasons)} → {alloc:.0%} of base size')
    return alloc


# ─── QUICK SANITY CHECK ───────────────────────────────────────────────────────

if __name__ == '__main__':
    print('=' * 90)
    print('Seasonal Allocation — v4 Aggressive Rule Set')
    print('=' * 90)

    MONTH_NAMES = {2:'Feb',6:'Jun',9:'Sep'}

    scenarios = [
        # Current: Q-end window active, Jun, EARLY WARNING
        dict(label='─── Current (Jun 27 2026, Q-end active, EARLY WARNING) ───',
             tests=[
                 ('NVDA',  6, 38.4, 20, 21, True,  False, 'EARLY_WARNING'),
                 ('AVGO',  6, 41.0, 20, 21, True,  False, 'EARLY_WARNING'),
                 ('MSFT',  6, 44.0, 20, 21, True,  False, 'EARLY_WARNING'),
                 ('META',  6, 36.0, 20, 21, True,  False, 'EARLY_WARNING'),
                 ('GOOGL', 6, 39.0, 20, 21, True,  False, 'EARLY_WARNING'),
                 ('SPY',   6, 38.4, 20, 21, True,  False, 'EARLY_WARNING'),
             ]),
        dict(label='─── Sep early (day 3/20), RISK-ON ───',
             tests=[
                 ('NVDA',  9, 55.0,  3, 20, False, False, 'RISK_ON'),
                 ('AVGO',  9, 55.0,  3, 20, False, False, 'RISK_ON'),
                 ('MSFT',  9, 55.0,  3, 20, False, False, 'RISK_ON'),
                 ('META',  9, 55.0,  3, 20, False, False, 'RISK_ON'),
                 ('GOOGL', 9, 55.0,  3, 20, False, False, 'RISK_ON'),
                 ('SPY',   9, 55.0,  3, 20, False, False, 'RISK_ON'),
             ]),
        dict(label='─── Sep late (day 14/20), RISK-ON ───',
             tests=[
                 ('NVDA',  9, 55.0, 14, 20, False, False, 'RISK_ON'),
                 ('META',  9, 55.0, 14, 20, False, False, 'RISK_ON'),
                 ('GOOGL', 9, 55.0, 14, 20, False, False, 'RISK_ON'),
             ]),
        dict(label='─── Sep oversold (RSI=42), RISK-ON ───',
             tests=[
                 ('META',  9, 42.0,  3, 20, False, False, 'RISK_ON'),
                 ('GOOGL', 9, 42.0,  8, 20, False, False, 'RISK_ON'),
             ]),
    ]

    for scenario in scenarios:
        print(f'\n{scenario["label"]}')
        for t in scenario['tests']:
            describe_allocation(*t)

    print('\n' + '=' * 90)
    print('Effective size = base_size × alloc_fraction')
    print(f'Current bot: base_size × 0.5 (win-rate halve) × alloc → then regime-capped')
    print('=' * 90)
