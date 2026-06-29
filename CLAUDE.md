# StocksScraper — Trading Bot Persistent Context

## Owner
Email: graduatespaces@gmail.com  
Robinhood Account: 699589594

---

## Strategy: Seasonal Swing Trading

Three filters applied in order — each one narrows what the next can do:
1. **Regime** — SPY 50d/200d SMA sets the deployment ceiling
2. **Seasonal sizing** — month + quarter-end calendar sets per-symbol allocation
3. **Position rules** — win rate, VIX, consecutive losses adjust the base size

---

## Symbol Universe & Final Seasonal Rules

Derived from 4-version backtest series (Jun 2026), tested 2018–2025.

| Symbol | Type | Negative-bias month rule | All other months |
|--------|------|--------------------------|-----------------|
| NVDA | momentum | **90% size** — trim 10%, never exit | 100% |
| AVGO | momentum | **90% size** — trim 10%, never exit | 100% |
| MSFT | momentum | **90% size** — trim 10%, never exit | 100% |
| META | balanced | **Binary exit** — out first 30% of month, 100% after | 100% |
| GOOGL | balanced-strict | **Binary exit** — out first 60% of month, 100% after | 100% |
| SPY | balanced-strict | **Binary exit** — out first 60% of month, 100% after | 100% |

**Negative-bias months:** Feb, Jun, Sep  
**Cautious months (May/Aug/Oct):** Aggressive mode = stay 100% in. Do not trim.

### Why this split
- Momentum compounders (NVDA/AVGO/MSFT) frequently rip 15–25% through seasonally "weak" months (NVDA Jun 2019 +22.8%, Jun 2021 +23%). Full binary exit destroys CAGR. 90% floor captures upside while keeping 10% dry powder for Q-end dip.
- META/GOOGL have genuine company-specific risk in bad months (META Feb 2022 −33.8%, GOOGL Sep 2022 −12.8%). Binary exit is better. META only needs 30% window; GOOGL/SPY need the full 60%.

---

## Universal Overrides (all symbols, all months)

Priority order — these override the seasonal rules above:

1. **RSI < 45** → 100% size. Oversold is always a buy signal regardless of month.
2. **Q-end window** (last 6 trading days of Mar/Jun/Sep/Dec) → 100% size. Fund manager profit-taking creates the dip; deploy the cash saved during the negative month.
3. **New-Q window** (first 7 trading days of each quarter) → 100% size. Institutional reallocation creates the bounce.

### Computing these windows in-session
- Q-end: fetch SPY 90-day history from Robinhood → identify current quarter → count from the last date backward; flag if within last 6 trading days.
- RSI-14: compute from last 20 closes using the standard EWM formula (gain/loss smoothed with com=13).

---

## Monthly Bias Reference

| Month | Bias | Note |
|-------|------|------|
| Jan | +1 | Accumulate |
| Feb | −1 | **Negative** |
| Mar | +1 | Accumulate |
| Apr | +1 | Accumulate |
| May | 0 | Cautious — aggressive: stay in |
| Jun | −1 | **Negative** (Q-end dip buy late Jun) |
| Jul | +1 | Strongest month historically |
| Aug | 0 | Cautious — aggressive: stay in |
| Sep | −1 | **Most dangerous month** |
| Oct | 0 | Cautious — aggressive: stay in |
| Nov | +1 | Accumulate |
| Dec | +1 | Santa Claus rally |

---

## Regime Definitions

Compute from SPY historical closes (250-bar window from Robinhood `get_equity_historicals`):
- 50d SMA = mean of last 50 closes
- 200d SMA = mean of last 200 closes

### RISK-ON: SPY close > 50d SMA
- Full base sizing applies
- All seasonal rules in effect

### EARLY WARNING: SPY close < 50d SMA (but > 200d SMA)
- Maximum 50% of account deployed in equities
- Quality names only: NVDA, MSFT, GOOGL preferred
- Seasonal rules still apply but capped at 50% base
- Exception: Q-end window or RSI < 45 → cap relaxes to 75% base

### RISK-OFF: SPY close < 200d SMA
- No new entries
- Hold existing positions; exit on any deteriorating signal
- Cash is a position

### DIP BUYING: SPY 5%+ below 200d SMA
- Starter positions: 25% base size
- NVDA, MSFT, GOOGL only
- Requires RSI < 45 for entry

---

## Position Sizing Formula

```
# Step 1: base size
base_size = account_equity × 0.10          # 10% per position, max 6 positions

# Step 2: protective adjustments
if win_rate_30d < 0.35:   base_size *= 0.50    # halve — active as of Jun 2026
if vix > 25:              base_size *= 0.75    # high-fear trim
if consecutive_losses >= 4: skip entry today   # 1-day cooling block

# Step 3: seasonal allocation (0.0–1.0 fraction)
alloc = get_seasonal_alloc(symbol, today, rsi_14)   # see seasonal_sizing.py

# Step 4: regime cap
final_size = base_size × alloc
if EARLY_WARNING:
    cap = 0.75 if (is_qend or rsi_14 < 45) else 0.50
    final_size = min(final_size, base_size × cap)
if RISK_OFF:
    final_size = 0   # no new entries
```

---

## Self-Throttle Rules

| Condition | Action |
|-----------|--------|
| 4+ consecutive losing trades | No new entries for 1 trading day |
| 30-day win rate < 35% | Halve all base sizes (until win rate recovers) |
| VIX 3 consecutive higher closes | Reduce new entries by 25% |
| VIX > 30 | Evaluate all open positions for exit |

---

## Exit Rules

| Rule | Trigger | Action |
|------|---------|--------|
| Stop loss | −7% from entry price | Market exit at open |
| Seasonal signal | Binary OUT signal fires | Exit at next open |
| Regime breach | SPY breaks 200d SMA | No new entries; evaluate holds |
| VIX spike | VIX > 30 | Review all positions |
| NVDA/AVGO/MSFT | Seasonal 90% floor | Trim to 90%, do NOT fully exit |

---

## T+1 Settlement (Cash Account)

- Proceeds from today's sale are available **tomorrow**
- Never allocate unsettled cash to a new position
- Track settlement dates — do not assume same-day reuse of proceeds

---

## Q-End Checklist (runs automatically during last 6 trading days of quarter)

1. Deploy cash saved from negative-bias months → add to existing positions
2. Check each holding for RSI < 45 dip-buy opportunity
3. Review 30-day win rate → set size halve flag for next quarter if < 35%
4. Record regime state transition if any (log for CLAUDE.md update)

---

## VIX Source

Robinhood index quote — instrument ID: `3b912aa2-88f9-4682-8ae3-e39520bdf4db`  
Call via: `mcp__robinhood-trading__get_index_quotes`  
Yahoo Finance VIX endpoint blocked by proxy — use Robinhood only.

---

## Current State (update at end of each session)

**As of Jun 27, 2026:**
- Regime: **EARLY WARNING** (SPY ~$732 vs 50d SMA ~$734, 200d SMA ~$690)
- VIX: 18.73 (low — no VIX adjustment needed)
- Win-rate halve: **ACTIVE** (win rate < 35%)
- Q-end window: **ACTIVE** (Jun 23–30 = last 6 trading days of Q2 2026)
- Effective multiplier: Q-end override (100%) × EARLY_WARNING cap (75%) × win-rate halve (50%) = **37.5% of normal base size**

---

## Backtesting Reference

4-version series, 2018–2025, scripts at `/home/user/backtest_seasonal_v*.py`

| Version | Key change | NVDA C CAGR | META C vs B |
|---------|-----------|------------|------------|
| v1 | Binary 60% window | +37.5% | B wins by 7.5% |
| v2 | Size-based, 50% balanced floor | +48.9% | B wins by 5.0% |
| v3 | Size-based, 25% balanced floor | +48.9% | B wins by 3.0% |
| v4 ✅ | 90% momentum, 30% META, 60% GOOGL/SPY | +56.0% | B wins by 0.7% |

v4 also reduced trade count from ~220 to ~50 per symbol over 7 years.

---

## Proxy Notes

- GitHub git clone/push: **blocked** (org policy). Use GitHub web UI to update this file.
- Yahoo Finance API (v7/v8): **blocked**. Use Robinhood MCP for all market data.
- raw.githubusercontent.com: **accessible** for file fetches.

---

## Scanner Architecture

### Files
| File | Purpose |
|------|---------|
| `scanner.py` | Main scanner — YouTube + Alpha Vantage → `stocks.json` + `macro_signals.json` |
| `daily_sizing.py` | Daily sizing report — fetches live Robinhood data, applies seasonal/regime/bear rules |
| `seasonal_sizing.py` | Seasonal allocation logic per symbol/month |
| `bear_score.py` | Bear score computation and size multiplier |
| `stocks.json` | Dynamic stock watchlist built by scanner (rolling, no expiry) |
| `macro_signals.json` | Macro signals from forecast channels (rolling 14-day window) |
| `seen_content.json` | Tracks processed video/article IDs to avoid re-processing |
| `youtube_channels.txt` | YouTube channels to scan |
| `scheduler.sh` + `com.stockscanner.daily.plist` | Mac launchd — runs `scanner.py` daily at 8am local time |

### 8am Run (Mac launchd — local, NOT GitHub Actions)
- Scans YouTube channels for last 2 days of videos
- Two Claude passes per video:
  1. **Stock pass** (Sonnet) — extracts explicit buy/sell ticker signals → `stocks.json`
  2. **Macro pass** (Haiku) — extracts market bias, VIX outlook, sector leans, key risks → `macro_signals.json`
- Scans Alpha Vantage news sentiment for core watchlist tickers
- Commits and pushes updated `stocks.json`, `seen_content.json`, `macro_signals.json` to GitHub

### YouTube Channels (as of Jun 2026)
| Channel | Type |
|---------|------|
| @MeetKevin | Stock picks + macro |
| @RickOrford | Stock picks |
| @MarketBeatMedia | Stock picks |
| @CNBCtelevision | Stock picks + macro |
| @SchwabNetwork | Stock picks + macro |
| @DumbMoneyLive | Stock picks |
| @InvestingWithTom | Stock picks |
| @InvestorsBusinessDaily | Momentum stocks |
| @GoldmanSachs | Macro / regime |
| @morningstar | Macro / regime |
| @BloombergTelevision | Macro / regime |
| @CathieWood | Tech / momentum |
| @StockAnalysis | Stock analysis |

---

## Session Log

### Jun 29, 2026
- Confirmed 8am automated run executed (commit `690185a` by Stock Scanner Bot at 16:36 UTC)
- Today's new picks: **TER** (Teradyne), **MRVL** (Marvell) — both from Rick Orford
- Added macro signal extraction: second Claude Haiku pass per video → `macro_signals.json` (14-day rolling)
- `daily_sizing.py` now shows "Analyst Macro Consensus" section (7-day lookback) at top of report
- Fixed duplicate channels: removed second entries for @MeetKevin and @AndreiJikh
- Refreshed channel list: removed @AndreiJikh, @GrahamStephan, @JosephCarlson, @NewMoneyYouTube; added @BloombergTelevision, @InvestorsBusinessDaily, @CathieWood, @StockAnalysis
