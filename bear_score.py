#!/usr/bin/env python3
"""
Continuous bear-signal score: 0.0 = full bull, 1.0 = full bear.

Replaces the binary RISK_ON / EARLY_WARNING / RISK_OFF regime with a
signal that scales position sizes down gradually as conditions deteriorate.
A hard RISK_OFF (SPY < 200d SMA) in daily_sizing.py still blocks new entries;
this score handles everything in between.

Components (all from Robinhood historicals — no external data needed):

  sma_cross  0.30   SPY 20d/50d/200d SMA alignment + death-cross check
  rsi        0.20   SPY RSI-14 position (declining momentum = bear signal)
  vix        0.25   VIX level vs calibrated fear thresholds
  breadth    0.25   Fraction of core symbols trading below their 50d SMA

Usage:
    score, detail = compute_bear_score(spy_closes, symbol_closes, vix)
    multiplier = size_multiplier(score)   # 1.0 → 0.60 as score goes 0 → 1
"""

# Contribution of each component to the final score
WEIGHTS = {
    'sma_cross': 0.30,
    'rsi':       0.20,
    'vix':       0.25,
    'breadth':   0.25,
}

# Maximum position-size reduction from bear score alone.
# At score=1.0:  size × (1 - 0.40) = 60% of normal.
# Hard stops (RISK_OFF) can still zero out new entries independently.
SENSITIVITY = 0.40


# ── RSI helper (shared with daily_sizing) ────────────────────────────────────

def compute_rsi14(closes: list[float]) -> float | None:
    """RSI-14 via EWM smoothing (com=13). Requires at least 20 closes."""
    if len(closes) < 20:
        return None
    closes = closes[-20:]
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains  = [max(d, 0.0) for d in deltas]
    losses = [abs(min(d, 0.0)) for d in deltas]
    alpha  = 1 / 14
    avg_gain, avg_loss = gains[0], losses[0]
    for g, l in zip(gains[1:], losses[1:]):
        avg_gain = alpha * g + (1 - alpha) * avg_gain
        avg_loss = alpha * l + (1 - alpha) * avg_loss
    if avg_loss == 0:
        return 100.0
    return 100 - (100 / (1 + avg_gain / avg_loss))


# ── Component scorers ─────────────────────────────────────────────────────────

def _sma_cross_score(spy_closes: list[float]) -> float:
    """
    Checks 5 SMA signals; each adds 0.20 to the score.
      - price < 20d SMA     (short-term trend broken)
      - price < 50d SMA     (medium-term trend broken)
      - price < 200d SMA    (long-term trend broken)
      - 20d SMA < 50d SMA   (short below mid — momentum declining)
      - 50d SMA < 200d SMA  (death cross — full bear alignment)
    """
    if len(spy_closes) < 200:
        return 0.0
    price  = spy_closes[-1]
    sma20  = sum(spy_closes[-20:])  / 20
    sma50  = sum(spy_closes[-50:])  / 50
    sma200 = sum(spy_closes[-200:]) / 200
    signals = [
        price < sma20,
        price < sma50,
        price < sma200,
        sma20 < sma50,
        sma50 < sma200,
    ]
    return sum(signals) / len(signals)


def _rsi_score(spy_closes: list[float]) -> float:
    """
    Converts SPY RSI-14 into a bear-pressure reading.
    Strong momentum (RSI > 60) = 0.0.
    Weakening / declining (RSI 40-60) = 0.1-0.4.
    Bearish (RSI 30-40) = 0.7.
    Deeply oversold (RSI < 30) = 0.85 — dangerous but not a clean buy signal.
    """
    rsi = compute_rsi14(spy_closes)
    if rsi is None:
        return 0.0
    if rsi >= 60:
        return 0.00
    elif rsi >= 50:
        return 0.10
    elif rsi >= 40:
        return 0.40
    elif rsi >= 30:
        return 0.70
    else:
        return 0.85


def _vix_score(vix: float | None) -> float:
    """
    VIX fear gauge mapped to 0-1.
    < 15  = complacency / low fear   → 0.0
    15-20 = normal                   → 0.20
    20-25 = elevated                 → 0.45
    25-30 = high fear                → 0.70
    > 30  = crisis / panic           → 1.00
    """
    if vix is None:
        return 0.0
    if vix < 15:
        return 0.00
    elif vix < 20:
        return 0.20
    elif vix < 25:
        return 0.45
    elif vix < 30:
        return 0.70
    else:
        return 1.00


def _breadth_score(symbol_closes: dict[str, list[float]]) -> float:
    """
    Fraction of core symbols trading below their 50d SMA.
    0/6 below → 0.0  (full bull breadth)
    6/6 below → 1.0  (full bear breadth)
    """
    below = total = 0
    for closes in symbol_closes.values():
        if len(closes) < 50:
            continue
        total += 1
        sma50 = sum(closes[-50:]) / 50
        if closes[-1] < sma50:
            below += 1
    return below / total if total else 0.0


# ── Public API ────────────────────────────────────────────────────────────────

def compute_bear_score(
    spy_closes: list[float],
    symbol_closes: dict[str, list[float]],
    vix: float | None,
) -> tuple[float, dict[str, float]]:
    """
    Compute composite bear score and return (score, components).

    Args:
        spy_closes:     List of SPY daily closes (200+ recommended)
        symbol_closes:  {symbol: [daily closes]} for breadth calculation
        vix:            Current VIX reading, or None if unavailable

    Returns:
        score:      float 0.0-1.0
        components: dict with individual scores for transparency
    """
    components = {
        'sma_cross': _sma_cross_score(spy_closes),
        'rsi':       _rsi_score(spy_closes),
        'vix':       _vix_score(vix),
        'breadth':   _breadth_score(symbol_closes),
    }
    score = sum(components[k] * WEIGHTS[k] for k in WEIGHTS)
    return round(score, 4), components


def size_multiplier(bear_score: float) -> float:
    """
    Convert bear score to a position-size multiplier.
    bear_score=0.0 → 1.00 (full size)
    bear_score=1.0 → 0.60 (SENSITIVITY=0.40 applied)
    """
    return round(1.0 - bear_score * SENSITIVITY, 4)


def describe_bear_score(score: float, components: dict[str, float], vix: float | None) -> None:
    """Print a human-readable breakdown of the bear score."""
    mult = size_multiplier(score)
    label = (
        "BULL"        if score < 0.20 else
        "MILD CAUTION" if score < 0.40 else
        "CAUTION"     if score < 0.60 else
        "WARNING"     if score < 0.80 else
        "BEAR"
    )
    print(f"\n  Bear Signal Score: {score:.2f}  [{label}]  → size multiplier {mult:.0%}")
    print(f"  {'Component':<14} {'Score':>6}  {'Weight':>7}  {'Contribution':>13}")
    print(f"  {'-'*46}")
    for k, w in WEIGHTS.items():
        contrib = components[k] * w
        print(f"  {k:<14} {components[k]:>6.2f}  {w:>7.0%}  {contrib:>13.3f}")
    if vix is not None:
        print(f"  VIX: {vix:.2f}")
