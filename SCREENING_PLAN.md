# Technical Analysis Screening Tool — Implementation Plan

## Purpose

This document explains every technical concept used in the screening dashboard: what each indicator is, how it is calculated, what it signals, and how it feeds into the phase/stage classification, stop-loss placement, and target levels. Use this as a reference while reading charts or extending the codebase.

---

## 1. Price and Moving Averages

### 1.1 Simple Moving Average (SMA)

A simple moving average takes the arithmetic mean of the closing price over N days.

```
MA(N, t) = (Close[t] + Close[t-1] + ... + Close[t-N+1]) / N
```

**MA50** — 50-day SMA. Reflects intermediate trend (~2 months). Acts as dynamic support in uptrends, resistance in downtrends.

**MA200** — 200-day SMA. Reflects long-term trend (~9 months). The single most-watched moving average by institutional traders. Price above MA200 = structurally bullish environment.

**Slopes matter as much as position.** A stock above its MA50 but with a declining MA50 is weaker than one above a rising MA50. The screening tool checks both price-relative-to-MA and the sign of the MA slope to classify stages.

**Golden Cross / Death Cross:**
- Golden Cross: MA50 crosses above MA200 → long-term bullish signal
- Death Cross: MA50 crosses below MA200 → long-term bearish signal
These are lagging signals; by the time they trigger, much of the move has happened. They are more useful as regime confirmation than entry timing.

---

## 2. Weinstein Stage Analysis (Phase Detection)

Stan Weinstein's stage analysis is the framework this tool is built around. Every stock cycles through four stages. The goal is to only buy in Stage 2 and exit before Stage 4.

### Stage 1 — Accumulation / Base Building

**Characteristics:**
- Price is trading sideways, roughly flat
- MA50 is flat or slightly rising
- Price oscillates around MA50
- MA200 is flat or starting to flatten after a decline
- Volume is low and uninteresting

**What is happening:** The stock has stopped going down. Informed buyers (institutions) are quietly accumulating shares. Sellers are exhausted. Price goes nowhere because buying equals selling.

**Action:** Watch. Do not buy. The base needs time to form — longer bases lead to bigger moves. A base of less than 6–8 weeks is weak.

**Exact detection criteria in code:**
```python
price_near_ma50 = abs(close / ma50 - 1) < 0.05        # within 5% of MA50
ma50_slope_flat = abs(ma50_slope) < 0.001               # slope nearly zero
not_in_markup = price < ma200 or ma200_slope < 0.001   # not already trending up
```

### Stage 2 — Markup / Uptrend

**Characteristics:**
- Price breaks out above the base (Stage 1) with volume
- MA50 is rising and above MA200 (or approaching a Golden Cross)
- Price is above MA50
- Volume on up-days exceeds volume on down-days
- New 52-week highs are common

**What is happening:** The smart money that accumulated in Stage 1 is now seeing their thesis play out. Momentum traders and eventually the public pile in, driving price higher.

**Action:** BUY — this is the only stage where you should initiate positions. Early Stage 2 (just after the breakout) offers the best R/R. Late Stage 2 (extended, parabolic) is riskier.

**Exact detection criteria in code:**
```python
price_above_ma50  = close > ma50
ma50_above_ma200  = ma50 > ma200
ma50_slope_up     = ma50_slope > 0.001
ma200_slope_up    = ma200_slope >= -0.0005   # can still be flat/slightly down early
```

### Stage 3 — Distribution / Top

**Characteristics:**
- Price starts trading sideways again, but at high levels
- MA50 begins to roll over (flatten or decline)
- Price fluctuates around MA50
- Volume is high but choppy (distribution by institutions)
- New highs are met with selling

**What is happening:** Institutions that bought in Stage 1/2 are now selling to late buyers. Supply equals demand again — at the top.

**Action:** TAKE PROFITS. Sell into strength. Do not add new positions. If you missed the sell, the next sharp breakdown below MA50 on volume is a final exit signal.

**Exact detection criteria:**
```python
price_near_ma50        = abs(close / ma50 - 1) < 0.05
ma50_slope_declining   = ma50_slope < 0.0005              # MA50 rolling over
was_above_ma200        = ma200 > 0 and close > ma200 * 0.95
```

### Stage 4 — Decline / Downtrend

**Characteristics:**
- Price is below MA50 and MA200
- MA50 is below MA200 (Death Cross in effect)
- Both MAs are declining
- Lower highs and lower lows
- Volume on down-days dominates

**What is happening:** Sellers are in control. The stock may look "cheap" but it is in a downtrend. Catching falling knives here is a common mistake.

**Action:** AVOID. Never buy in Stage 4. Short-sellers target this stage. The only reason to watch a Stage 4 stock is to wait for it to eventually base out and enter Stage 1.

**Exact detection criteria:**
```python
price_below_ma50   = close < ma50
ma50_below_ma200   = ma50 < ma200
ma50_slope_down    = ma50_slope < -0.001
```

---

## 3. Support and Resistance

### Definition

- **Support:** A price level where demand has historically been strong enough to stop or reverse a decline. Buyers step in here.
- **Resistance:** A price level where supply has historically been strong enough to stop or reverse an advance. Sellers step in here.

Support and resistance are not precise lines — they are zones.

### How the Tool Calculates Them

The tool uses an adaptive pivot-point method:

```
1. Set window = max(5, total_bars / 30)   — scales with data length
2. Find all local highs: Close[i] = max of window bars around i
3. Find all local lows:  Close[i] = min of window bars around i
4. Cluster nearby levels: merge any two levels within 1.5% of each other
5. Return the nearest resistance above current price
   and the nearest support below current price
```

**Clustering logic:** If two pivot levels are within 1.5% of each other, they represent the same zone. The cluster midpoint becomes the level. This prevents reporting five "resistance levels" that are all essentially the same price.

### Psychological Levels

Round numbers (₹500, ₹1000, $50, $100, $200) act as additional support/resistance because many traders place orders there. The tool does not explicitly model these, but be aware of them when interpreting levels near round numbers.

### Volume at Price

The strongest support/resistance zones are where the most volume has traded — price has to work hard to move through a high-volume zone. The tool does not compute volume-at-price (VWAP-style), but this is a useful concept when manually interpreting charts.

---

## 4. Bollinger Bands

### Calculation

```
Middle Band (BB_mid)  = 20-period SMA of Close
Upper Band (BB_upper) = BB_mid + (2 × 20-period standard deviation of Close)
Lower Band (BB_lower) = BB_mid - (2 × 20-period standard deviation of Close)
BB Width              = (BB_upper - BB_lower) / BB_mid
```

Statistically, ~95% of price action falls within ±2σ, so price touching the upper band is "statistically extended" and touching the lower band is "statistically compressed."

### Interpretation

**Band walking:** In a strong uptrend (Stage 2), price can "walk" along the upper band for weeks. This is normal and not a sell signal by itself. It means momentum is sustained.

**Mean reversion:** In a range-bound stock (Stage 1 or 3), touching the upper band is a short-term sell signal; touching the lower band is a short-term buy signal.

**Squeeze — the most important signal:**

```
BB_squeeze = True  when  BB_width <= min(BB_width, 20-bar lookback)
```

A Bollinger Band squeeze means volatility has contracted to a multi-week low. Volatility is mean-reverting — after a squeeze, an expansion (a big directional move) is coming. The squeeze does not tell you which direction, but when combined with Phase 2 context, it strongly favors upside.

**How to trade the squeeze:**
1. Identify a squeeze (BB width at 20-bar low)
2. Wait for the breakout bar (price closes above the upper band)
3. Enter on the next bar's open
4. Stop below the squeeze low (the lowest point during the squeeze period)

---

## 5. RSI — Relative Strength Index

### Calculation

```
RS   = Average Gain (14 periods) / Average Loss (14 periods)
RSI  = 100 − (100 / (1 + RS))
```

The tool uses exponential moving average (EWM) for the gain/loss smoothing, matching the original Wilder method.

### Interpretation

| RSI Range | Condition | Meaning |
|-----------|-----------|---------|
| 0–30 | Oversold | Selling exhausted; potential reversal zone |
| 30–50 | Weak | Below-average momentum |
| 50–70 | Healthy | Above-average momentum; ideal buy zone |
| 70–80 | Extended | Momentum strong but getting stretched |
| 80–100 | Overbought | High risk of pullback; wait before buying |

**Key rule used in this tool:** Only buy Stage 2 setups where RSI is below 72. RSI above 72 on entry means you are chasing an extended move — the R/R is poor.

**RSI divergence:** If price makes a new high but RSI makes a lower high — bearish divergence. A sign that buying momentum is weakening even as price pushes up. Caution in Stage 3 context.

**RSI as trend indicator:** In a strong Stage 2 uptrend, RSI tends to stay above 40 and repeatedly reach 60–80. In a Stage 4 downtrend, RSI tends to stay below 60 and repeatedly touch 20–40. The RSI "range" tells you the trend as much as the absolute level.

---

## 6. MACD — Moving Average Convergence/Divergence

### Calculation

```
MACD Line    = EMA(12) − EMA(26)
Signal Line  = EMA(9) of MACD Line
MACD Hist    = MACD Line − Signal Line
```

### Interpretation

**Zero-line crossovers:**
- MACD crosses above zero → bullish (short-term trend above long-term trend)
- MACD crosses below zero → bearish

**Signal-line crossovers:**
- MACD crosses above signal → buy signal (momentum accelerating up)
- MACD crosses below signal → sell signal (momentum decelerating)

**Histogram:**
- Growing histogram bars → momentum accelerating in the direction of the trend
- Shrinking histogram bars → momentum weakening (potential reversal)

**MACD divergence:** Like RSI divergence, if price makes new highs but MACD histogram makes lower highs, the uptrend is losing steam.

**In this tool:** The summary checks `macd > macd_sig` as a bullish confirmation filter. A buy setup requires both Phase 2 AND MACD bullish (above its signal line).

---

## 7. ATR — Average True Range

### Calculation

```
True Range = max(
    High − Low,
    abs(High − Previous Close),
    abs(Low  − Previous Close)
)
ATR(14) = 14-period EWM average of True Range
```

### Why ATR Matters

ATR measures how much a stock typically moves in a day. It is a **volatility measure**, not a directional indicator. Its primary use here is stop-loss sizing.

**Key principle:** Stop losses placed using ATR are adaptive to the stock's actual volatility. A tight stop on a volatile stock gets triggered by random noise; a wide stop on a calm stock risks too much capital.

A typical ATR-based stop: `Stop = Entry − 2 × ATR(14)`

The tool uses a combined stop (described in Section 9) that takes the higher of the support-based stop and the MA50-based stop.

---

## 8. OBV — On-Balance Volume

### Calculation

```
OBV[0] = Volume[0]
OBV[t] = OBV[t-1] + Volume[t]   if Close[t] >= Close[t-1]
OBV[t] = OBV[t-1] − Volume[t]   if Close[t] <  Close[t-1]
```

### Interpretation

OBV is a running total that adds volume on up-days and subtracts it on down-days. It reveals whether volume is flowing into or out of a stock.

**OBV leads price:** Institutional accumulation in Stage 1 shows up as rising OBV while price goes sideways. This is a key signal that smart money is building a position before the breakout.

**Confirmation:** In Stage 2, OBV should be making new highs alongside price. If price makes new highs but OBV does not — divergence — the breakout lacks volume support and may fail.

**Warning:** OBV is not comparable across stocks (it is an absolute running total). Look at its trend and slope, not its absolute value.

---

## 9. Stop-Loss Calculation

The tool computes stop loss using three candidate levels and takes the most protective one that still makes structural sense:

```
Candidate 1: Support-based stop  = nearest_support × 0.992   (2% below support)
Candidate 2: MA50-based stop     = ma50 × 0.970              (3% below MA50)
Candidate 3: ATR-based stop      = close − 2.0 × atr14       (2 ATR below entry)

Stop = max(Candidate 1, Candidate 2, Candidate 3)
```

Taking the maximum means using the highest (closest to price) of the three candidates — this gives the tightest structurally-valid stop.

**Why stop below support?** A break below support means the thesis is wrong — the buying that was propping up the level has been absorbed. A 2% buffer below support accounts for wicks and stop-hunting.

**Why stop below MA50?** In a healthy Stage 2 uptrend, price should not close significantly below MA50. A weekly close below MA50 is an exit signal.

**Trade invalidation rule:** If the calculated stop is more than 10% below the current price, the R/R is likely too poor. Either skip the trade or use a smaller position.

---

## 10. Target Levels (T1 and T2)

### T1 — First Target

The nearest resistance level above the current price.

```python
T1 = resistance_levels_above_price[0]   # closest resistance
```

**Usage:** Partial profit-taking zone. Take 30–50% of the position off at T1. Move stop to breakeven. Let the rest run toward T2.

### T2 — Second Target

The second resistance level above the current price.

```python
T2 = resistance_levels_above_price[1]   # second closest resistance
```

If fewer than two resistance levels are found above the current price (stock at all-time highs), T2 is calculated as:

```python
T2 = close + 2.0 × (close − stop)   # 2× the initial risk distance
```

This ensures T2 always represents at least a 2:1 R/R if T1 is not a clean level.

### Risk/Reward Ratio

```
R/R = (T1 − Entry) / (Entry − Stop)
```

**Minimum acceptable R/R: 1.5**. The tool flags setups as buy-worthy only when R/R ≥ 1.5. At a 50% win rate, you need R/R > 1.0 to be profitable. At 1.5, you are profitable even with only a 40% win rate.

---

## 11. Summary Logic — How the Action Label Is Generated

The tool produces a single action label from the combined indicator state:

| Condition | Action | Color |
|-----------|--------|-------|
| Phase 2 AND RSI < 72 AND R/R ≥ 1.5 | BUY / ACCUMULATE | Green |
| Phase 2 AND RSI > 72 | WAIT FOR PULLBACK | Amber |
| Phase 2 AND price near upper BB | HOLD / PARTIAL PROFIT | Amber |
| Phase 1 | WATCH — NOT YET | Blue |
| Phase 3 | TAKE PROFITS | Amber |
| Phase 4 | AVOID / STAY FLAT | Red |
| BB Squeeze AND not Phase 2 | WATCH — SQUEEZE SETUP | Blue |
| Everything else | NEUTRAL — WAIT | Gray |

These rules are evaluated in priority order — Phase 2 + RSI check takes precedence over all others.

---

## 12. How to Read a Chart Setup — Worked Example

Given a stock where:
- Price = ₹480
- MA50 = ₹455, MA200 = ₹420 (price > MA50 > MA200) → Stage 2
- MA50 slope = +0.3% per day → rising
- RSI = 62 → healthy, not overextended
- BB squeeze was active 5 days ago, now breaking out
- Support = ₹445, Resistance = ₹510, ₹560
- ATR(14) = ₹18

**Calculations:**
```
Stop = max(445 × 0.992, 455 × 0.970, 480 − 2×18)
     = max(441.5, 441.4, 444.0)
     = 444.0                                      ← ATR-based stop wins

T1   = 510                                        ← first resistance
T2   = 560                                        ← second resistance

R/R  = (510 − 480) / (480 − 444)
     = 30 / 36
     = 0.83                                       ← below 1.5, skip trade

Position size on ₹10L portfolio:
Risk = ₹10,00,000 × 0.015 = ₹15,000
Shares = 15,000 / (480 − 444) = 417 shares → round to 400
Capital used = 400 × 480 = ₹1,92,000 (19.2% of portfolio — too high, cap at 7%)
Capped shares = 70,000 / 480 = 145 shares
```

In this example, the R/R of 0.83 means you would skip the trade even though the setup is technically Stage 2. The resistance at ₹510 is too close relative to the stop. Wait for price to consolidate closer to ₹445–455 and re-enter with a higher R/R.

---

## 13. Indicator Reference Card

| Indicator | Parameters | Signal | Source in Code |
|-----------|-----------|--------|----------------|
| MA50 | 50-period SMA | Trend direction | `df['MA50'] = df['Close'].rolling(50).mean()` |
| MA200 | 200-period SMA | Long-term trend | `df['MA200'] = df['Close'].rolling(200).mean()` |
| BB Upper/Lower | 20-period, 2σ | Volatility envelope | `compute_indicators()` in analysis.py |
| BB Width | (Upper−Lower)/Mid | Volatility level | Used for squeeze detection |
| BB Squeeze | Width at 20-bar min | Breakout incoming | `df['BB_squeeze']` |
| RSI | 14-period EWM | Momentum strength | `df['RSI']` |
| MACD | 12/26/9 EMA | Momentum direction | `df['MACD']`, `df['MACD_sig']`, `df['MACD_hist']` |
| ATR | 14-period | Volatility (for stops) | `df['ATR']` |
| OBV | Cumulative vol | Volume trend | `df['OBV']` |
| Support | Pivot lows, clustered | Stop anchor | `find_support_resistance()` |
| Resistance | Pivot highs, clustered | Target anchor | `find_support_resistance()` |
| T1 / T2 | Nearest resistances | Profit targets | `compute_targets_stoploss()` |
| Stop | max(support, MA50, ATR) stop | Risk limit | `compute_targets_stoploss()` |
| Phase | Stage 1–4 classification | Trade eligibility | `detect_phase()` |

---

## 14. Common Mistakes When Reading These Signals

**1. Buying Phase 1 because it "looks cheap"**
Stage 1 stocks can stay in base for months or years. "Cheap" and "ready to move" are different things. Wait for the Stage 2 confirmation before buying.

**2. Ignoring volume on breakouts**
A breakout on low volume is a failed breakout 60–70% of the time. Volume should be at least 1.5× the 20-day average on the breakout day.

**3. Using tight stops on volatile stocks**
If ATR is large, a tight stop gets triggered by normal daily fluctuation. Use position size to control risk, not stop distance.

**4. Averaging down in Stage 4**
Never add to a losing position in a downtrend. Every dip looks like a buying opportunity until it is not. Let Stage 4 stocks go.

**5. Confusing RSI overbought with sell signal**
A stock with RSI of 80 in a Stage 2 uptrend is not automatically a sell. Strong trends maintain elevated RSI for extended periods. The sell signal is RSI divergence + Phase 3 signs, not just a high RSI number.

**6. Ignoring the broader market**
If the index (Nifty 50 or S&P 500) is below its MA200, individual stock setups fail at a much higher rate. Always check the market's stage before trading individual stocks.
