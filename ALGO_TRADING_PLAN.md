# Algorithmic Trading System — Implementation Plan

## Goal

Build a fully automated system that:
1. Scans ~1,600 liquid stocks across NSE, BSE, NYSE, and NASDAQ every night
2. Scores each stock using Weinstein stage analysis + technical indicators
3. Generates a ranked watchlist of actionable setups
4. Sizes positions using a formal risk framework
5. Places and manages orders via broker APIs (Zerodha for India, IBKR for US)
6. Monitors open positions and enforces stops automatically

Do not write code until backtesting validates the signal quality over 30+ independent trades.

---

## Part 1 — Broker and API Selection

### Indian Stocks: Zerodha Kite Connect

**Why Zerodha:**
- Largest retail broker in India by volume — best execution quality and liquidity
- Flat ₹20/trade brokerage on intraday and F&O; ₹0 on equity delivery
- API subscription: ₹2,000/month (~$24). Well worth it for algorithmic access.
- `kiteconnect` Python SDK is the most mature Indian algo library
- Supports equity, F&O, commodities, currency

**Costs per round-trip on delivery equity:**
```
Brokerage:           ₹0 (delivery)
STT (sell side):     0.1% of sell value
Exchange txn charge: 0.00345% (NSE)
SEBI turnover:       0.0001%
GST on brokerage:    18% of ₹0 = ₹0
Stamp duty (buy):    0.015%
Total approx:        0.12–0.15% per round-trip
```

**SEBI algo requirements:**
EOD swing trading via Kite Connect API is classified as "non-HFT algo" and is permissible. If you move to fully automated intraday execution, Zerodha requires you to submit an algo strategy declaration. For EOD-signal-based swing trades placed pre-market, you are in the clear.

**Setup steps:**
1. Open a Zerodha trading account (demat + trading, ~3 days)
2. Enable Kite Connect API access (pay ₹2,000/month in the developer console)
3. Generate API key + secret
4. Daily login flow: OAuth-based. Must log in via web browser once per day to get the access token. Can be automated with a headless browser (Selenium) or by using the `kiteconnect` login URL in a cron script that prompts for OTP.

---

### US Stocks: Interactive Brokers (IBKR)

**Why IBKR:**
- The only realistic choice for Indian residents trading US listed equities algorithmically
- Indian residents CAN open IBKR international accounts (no US Social Security Number required)
- Commission: $0.005/share, minimum $1, maximum 1% of trade value (Pro plan)
- Cost on 100 shares × $50 stock = $5 = 0.10% — competitive
- `ib_insync` Python library provides a clean async interface to the IBKR TWS/Gateway API
- Supports stocks, options, futures, forex globally

**LRS compliance (mandatory):**
Indians can remit up to $250,000 USD per year to foreign accounts under RBI's Liberalized Remittance Scheme (LRS). Fund your IBKR account via wire transfer from an AD Category I bank (HDFC, ICICI, Axis). Bank will ask for Form A2 declaring purpose of remittance as "investment in overseas listed securities." IBKR is explicitly LRS-eligible.

**Pattern Day Trader (PDT) rule:**
If your IBKR account is under $25,000 USD, you cannot make more than 3 day-trades (round-trips in the same stock within the same day) in a rolling 5-day period. For swing trading (holding positions 2+ days), the PDT rule does not apply. Avoid intraday US trading until account exceeds $25k.

**Setup steps:**
1. Apply at interactivebrokers.com (select "Individual" account, country: India)
2. Upload PAN card, Aadhaar, 3 months bank statement, passport-size photo
3. Account approval: 3–10 business days
4. Fund via wire (SWIFT) from your Indian bank account, citing LRS/Form A2
5. Download IBKR TWS (Trader Workstation) or IB Gateway — API connects to this
6. In TWS: Enable API connections (Edit → Global Config → API → Settings → Enable ActiveX and Socket Clients)

**Reject list:**
- Alpaca: US residency required in practice; compliance risk for Indians
- TD Ameritrade / Schwab: require US Social Security Number
- Robinhood: US residents only

---

## Part 2 — Data Infrastructure

### Stock Universe

Start with liquid stocks only. Do not attempt to scan all 13,000+ listed securities on day one.

| Exchange | Universe | Size | Source |
|----------|----------|------|--------|
| NSE | Nifty 500 | 500 stocks | NSE India bulk download or nsepython |
| BSE | BSE 500 | 500 stocks (~80% overlap with NSE) | BSE bulk download |
| NYSE | S&P 500 | 503 stocks | Wikipedia S&P 500 list or FMP API |
| NASDAQ | Nasdaq 100 + Russell 1000 additions | ~600 stocks | Polygon.io tickers endpoint |
| **Total effective** | | **~1,600 unique** | |

**Minimum liquidity filter (applied before scanning):**
- Indian stocks: Average daily volume ≥ ₹2 crore (₹20M) over last 20 days
- US stocks: Average daily volume ≥ 500,000 shares over last 20 days
- Drops illiquid names where slippage would consume all edge

**Expand later:** Once the pipeline is validated, extend to the full NSE (2,000 stocks) and full BSE (5,000 stocks). US can extend to Russell 3000. But start small.

---

### Price Data Sources

| Source | Markets | Cost | Rate Limit | Use |
|--------|---------|------|-----------|-----|
| **yfinance** | NSE (.NS), BSE (.BO), US | Free | ~2,000/hour before throttle | Primary EOD source for all markets |
| **Polygon.io Starter** | NYSE, NASDAQ | $29/month | 5 calls/second | Backup US data + real-time if needed |
| **nsepython** | NSE | Free | Moderate | NSE index constituent lists, option chains |
| **Alpha Vantage** | US | Free (500/day) | 5/min free | US fundamentals backup |
| **FMP (Financial Modeling Prep)** | US + India | $14/month | 250/min | Earnings dates, P/E, sector data |

**Primary approach:** yfinance handles everything at this universe size. Downloading 1,600 stocks of EOD OHLCV nightly takes approximately 8–12 minutes using a thread pool (20 concurrent requests). yfinance is free, returns adjusted prices, and covers both Indian and US markets.

**Fetch pattern:**
```
For Indian stocks: yf.download("RELIANCE.NS", start=..., end=...)
For US stocks:     yf.download("AAPL", start=..., end=...)
Batch download:    yf.download(["AAPL","MSFT","GOOGL"], ...)  — faster
```

**Storage:** SQLite is sufficient for 1,600 stocks × 5 years of daily OHLCV (~8M rows). Use PostgreSQL if you scale to intraday data or full market coverage.

Schema:
```sql
CREATE TABLE prices (
    ticker      TEXT NOT NULL,
    date        DATE NOT NULL,
    open        REAL, high REAL, low REAL, close REAL, volume INTEGER,
    adj_close   REAL,
    PRIMARY KEY (ticker, date)
);
CREATE TABLE indicators (
    ticker      TEXT NOT NULL,
    date        DATE NOT NULL,
    ma50        REAL, ma200 REAL,
    bb_upper    REAL, bb_mid REAL, bb_lower REAL,
    bb_width    REAL, bb_squeeze INTEGER,
    rsi         REAL, macd REAL, macd_sig REAL, macd_hist REAL,
    atr         REAL, obv  REAL,
    support     REAL, resistance REAL,
    phase       INTEGER, signal_score REAL,
    PRIMARY KEY (ticker, date)
);
```

---

### Fundamental Data (Optional but Useful)

| Field | Source | Use |
|-------|--------|-----|
| Sector / Industry | yfinance `.info` | Sector concentration limits |
| Market cap | yfinance `.info` | Liquidity filter |
| P/E, P/B | FMP or yfinance | Quality filter (avoid extreme valuation) |
| Earnings date | FMP | Avoid holding over earnings (gap risk) |
| Promoter / Institution % | yfinance `.info` | Already in current dashboard |

---

## Part 3 — Signal Engine

### Nightly Pipeline Flow

```
10:00 PM IST (after NSE/BSE close) — Indian data run
 └─ Download OHLCV for all Indian tickers
 └─ Compute indicators
 └─ Classify stages
 └─ Score signals
 └─ Store results to DB

11:30 PM IST (after NYSE/NASDAQ close, 2:00 PM ET) — US data run
 └─ Download OHLCV for all US tickers
 └─ Compute indicators
 └─ Classify stages
 └─ Score signals
 └─ Store results to DB

12:00 AM IST — Report generation
 └─ Merge Indian + US results
 └─ Rank by signal_score descending
 └─ Apply portfolio-level filters
 └─ Send Telegram notification with top 5–10 setups
```

Scheduler: Linux `cron` or Python `APScheduler`. Run on a VPS (AWS t3.micro is free-tier eligible, or Hetzner CX11 at €3.49/month) so it runs overnight without your laptop being on.

---

### Signal Scoring (0–100 Scale)

Each stock gets a score each night based on the following components:

| Component | Condition | Points |
|-----------|-----------|--------|
| Phase 2 confirmed | Price > MA50 > MA200, MA50 slope positive | +40 |
| BB Squeeze recent | Squeeze active in last 10 days | +15 |
| Volume breakout | Today's volume > 1.5× 20-day avg AND price up | +15 |
| 52-week high proximity | Price within 5% of 52-week high | +10 |
| RSI healthy | RSI between 50 and 70 | +10 |
| Minimum R/R | (T1 − price) / (price − stop) ≥ 2.0 | +5 |
| MACD bullish | MACD > MACD signal | +5 |

**Threshold for watchlist inclusion: ≥ 65 points**
**Threshold for auto-execution eligibility: ≥ 80 points** (only after paper trading validation)

**Additional disqualifiers (score immediately to 0):**
- Phase 4 (downtrend)
- Earnings within 5 trading days (gap risk)
- Average daily volume below liquidity floor
- Market index (Nifty 50 or S&P 500) below its own MA200 (regime filter)

---

### Market Regime Filter

Before executing any trades, check the broader market:

```python
if nifty50.close < nifty50.ma200:
    halt_all_new_indian_longs()

if sp500.close < sp500.ma200:
    halt_all_new_us_longs()
```

Jegadeesh & Titman (1993) momentum strategies underperform significantly during market downturns. Trading individual Stage 2 stocks while the index is in Stage 4 is fighting a powerful headwind. This single filter materially improves Sharpe ratio and drawdown.

---

## Part 4 — Position Sizing and Risk Management

### Three-Layer Risk Framework

**Layer 1 — Per Trade**
- Maximum risk: 1.5% of total portfolio per trade
- Position size: `shares = (portfolio × 0.015) / (entry − stop)`
- Maximum capital in one stock: 7% of portfolio (prevents oversized bets)
- Stop loss placed as a standing order at time of entry (GTC)

**Layer 2 — Portfolio**
- Maximum simultaneous open positions: 15
- Maximum single sector exposure: 20% of portfolio
- Maximum India / US allocation split: decided at the portfolio level, not per-trade
- No new positions when monthly drawdown exceeds 8%

**Layer 3 — Drawdown Circuit Breaker**
- Monthly drawdown > 8%: halt new entries, only manage existing positions
- Total drawdown > 15%: reduce all positions to 50% size
- Total drawdown > 25%: close everything, go to cash, restart evaluation

### Kelly Criterion Check

Using historical Weinstein stage performance as reference:
```
Estimated win rate (p):   55%
Estimated avg R/R (b):    2.0
Loss rate (q):            45%

Full Kelly: f* = (0.55 × 2.0 − 0.45) / 2.0 = 0.325 (32.5% per trade)
Half-Kelly:                                     0.163 (16.3% per trade)
```

Half-Kelly (16%) is still aggressive for a single position in a concentrated strategy. The 1.5% risk-per-trade rule is more conservative and correct for a system that has not been validated live. After 50+ live trades, recalibrate Kelly using your actual win rate and R/R.

### Position Sizing Example

```
Portfolio:  ₹10,00,000
Entry:      ₹480
Stop:       ₹444
Risk/share: ₹36

Max risk:   ₹10,00,000 × 0.015 = ₹15,000
Shares:     ₹15,000 / ₹36 = 417 shares

Capital check: 417 × ₹480 = ₹2,00,160 (20% of portfolio)
7% cap applies: ₹10,00,000 × 0.07 = ₹70,000 / ₹480 = 145 shares

Final position: 145 shares
```

---

## Part 5 — Execution Engine

### Entry Orders

For swing trades, do not chase breakouts at market open. Use limit orders:

```
Indian (Kite): Limit order at previous close + 0.3%
               Placed pre-market (after login at 8:45 AM IST)
               Time-in-force: DAY (cancel if unfilled by 1 PM)

US (IBKR):     Limit order at previous close + 0.2%
               Placed pre-open (8:00 AM ET, 2 hours before open)
               Time-in-force: DAY
```

If the order does not fill, skip the trade that day. Do not chase — another setup will come.

### Stop Loss Orders

Place a stop loss order immediately after the entry fill is confirmed:

```
Indian (Kite): SL-Market order (triggered when bid falls below stop price)
               Store order ID so it can be cancelled if position is exited for profit

US (IBKR):     OCA (One-Cancels-All) bracket order:
               Parent: Limit buy at entry
               Take-profit: Limit sell at T1
               Stop: Stop-Market sell at stop price
               All three are linked — filling one cancels the others
```

### Trailing Stops

Managed nightly after EOD data is downloaded:

```
After +5% gain:    Move stop to breakeven (entry price)
After +10% gain:   Move stop to T1 * 0.98 (lock in 98% of T1 gain)
After T1 reached:  Sell 40% of position at T1; let 60% run to T2
At T2:             Sell remaining position
```

Alternatively, use a trailing stop of `2 × ATR(14)` below the highest close since entry. Whichever is higher (tighter) wins.

### Exit Rules

| Signal | Action |
|--------|--------|
| Price closes below MA50 on weekly chart | Exit 100% of position |
| Phase changes from 2 to 3 | Begin exiting; full exit within 2 days |
| Stop loss triggered | Exit immediately (broker handles automatically) |
| T2 reached | Exit remaining position |
| Pre-earnings (5 days before) | Exit if holding >10% gain; hold if small position with wide stop |
| Monthly drawdown breaker triggered | Exit all positions |

---

## Part 6 — System Architecture

### Component Map

```
┌─────────────────────────────────────────────────────────────┐
│  VPS (Linux, runs 24/7)                                     │
│                                                             │
│  ┌──────────────────┐    ┌──────────────────────────────┐  │
│  │  Nightly Cron    │    │  Position Monitor (intraday) │  │
│  │  10 PM + 11:30PM │    │  Runs every 30 min           │  │
│  │                  │    │  during market hours         │  │
│  │  1. Data pull    │    │                              │  │
│  │  2. Indicators   │    │  1. Poll open positions      │  │
│  │  3. Stage class  │    │  2. Check trailing stops     │  │
│  │  4. Scoring      │    │  3. Update stop orders       │  │
│  │  5. Notify       │    │  4. Enforce circuit breakers │  │
│  └────────┬─────────┘    └──────────────┬───────────────┘  │
│           │                             │                   │
│           ▼                             ▼                   │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  SQLite / PostgreSQL                                  │  │
│  │  - prices table (OHLCV history)                      │  │
│  │  - indicators table (computed signals)               │  │
│  │  - positions table (open trades)                     │  │
│  │  - trades table (closed P&L history)                 │  │
│  └──────────────────────────────────────────────────────┘  │
│           │                                                 │
│           ▼                                                 │
│  ┌──────────────────────────────────────────────────────┐  │
│  │  Execution Layer                                      │  │
│  │  - Zerodha kiteconnect  (Indian stocks)              │  │
│  │  - IBKR ib_insync       (US stocks)                  │  │
│  └──────────────────────────────────────────────────────┘  │
└─────────────────────────────────────────────────────────────┘
           │
           ▼
  Telegram Bot → Daily morning report with top signals + open position status
  Flask Dashboard → Existing visualization tool (read from same DB)
```

### Files to Build

```
blr_investing/
├── app.py                    (existing — Flask dashboard)
├── analysis.py               (existing — TA logic)
├── templates/index.html      (existing — frontend)
│
├── scanner/
│   ├── universe.py           — ticker lists for NSE/BSE/US
│   ├── data_pull.py          — nightly OHLCV downloader (yfinance + Polygon)
│   ├── indicators.py         — vectorized batch indicator computation
│   ├── scorer.py             — signal scoring + stage classification
│   └── report.py             — watchlist report + Telegram notification
│
├── execution/
│   ├── zerodha.py            — Kite Connect wrapper (login, place, cancel, status)
│   ├── ibkr.py               — ib_insync wrapper (connect, bracket orders, TWS)
│   ├── order_manager.py      — unified interface over both brokers
│   └── position_monitor.py   — trailing stop management, circuit breakers
│
├── risk/
│   ├── sizer.py              — position size calculator (Kelly, ATR, % risk)
│   └── portfolio.py          — portfolio-level exposure checks
│
├── db/
│   ├── schema.sql            — table definitions
│   └── store.py              — read/write helpers
│
├── scheduler.py              — APScheduler cron jobs (data pull, monitor, report)
├── config.py                 — API keys, thresholds, universe toggles
├── SCREENING_PLAN.md         (this repo — reference doc)
└── ALGO_TRADING_PLAN.md      (this repo — reference doc)
```

---

## Part 7 — Regulatory and Tax

### India — Trading Indian Stocks

| Item | Detail |
|------|--------|
| SEBI algo registration | EOD swing via Kite Connect API is non-HFT, no special registration needed |
| Securities Transaction Tax (STT) | 0.1% on equity delivery sell side. Automatically deducted. |
| Short-term capital gains (STCG) | 15% flat on profits from stocks held < 12 months |
| Long-term capital gains (LTCG) | 10% on gains > ₹1.25 lakh per year, for stocks held > 12 months |
| Reporting | Report in ITR-2 or ITR-3 under Capital Gains. Zerodha provides annual P&L statement. |

### India — Trading US Stocks via IBKR

| Item | Detail |
|------|--------|
| LRS remittance | Max $250,000 USD/year via RBI LRS. Use Form A2 at your bank. |
| FEMA compliance | Investment in listed foreign securities is permitted under LRS. |
| W-8BEN form | File with IBKR → declares non-US status → no US capital gains tax on stocks |
| US dividend withholding | 25% withheld at source. India-US DTAA allows credit against Indian tax. |
| Indian tax on US stocks | STCG (held < 24 months): taxed at slab rate. LTCG (> 24 months): 20% with indexation. |
| Reporting | Declare in Schedule FA (Foreign Assets) of ITR. IBKR provides annual tax statement. |
| Currency risk | USD/INR fluctuation affects returns. 1% INR depreciation/year has been the historical trend (beneficial for USD holdings). |

---

## Part 8 — Phased Rollout

### Phase 0 — Accounts and Infrastructure (Weeks 1–2)

- [ ] Open Zerodha account and activate Kite Connect API
- [ ] Apply for IBKR international account
- [ ] Wire initial capital to IBKR via LRS
- [ ] Spin up a VPS (AWS free tier or Hetzner)
- [ ] Set up PostgreSQL (or SQLite to start)
- [ ] Create Telegram bot for notifications

### Phase 1 — Data Pipeline (Week 3)

- [ ] Build `universe.py` — static ticker lists for NSE Nifty 500, BSE 500, S&P 500, Nasdaq 100
- [ ] Build `data_pull.py` — nightly downloader with retry logic and gap detection
- [ ] Run nightly cron for 1 week, verify data completeness
- [ ] Apply liquidity filter — drop tickers below volume floor

### Phase 2 — Signal Engine (Week 4)

- [ ] Port `compute_indicators()` from existing `analysis.py` to batch mode (vectorized over all tickers)
- [ ] Build `scorer.py` — 0–100 scoring system
- [ ] Build `report.py` — Telegram message with top 10 signals each morning
- [ ] Manually review signals for 2 weeks. Check against charts. Calibrate scoring weights.

### Phase 3 — Paper Trading (Month 2)

- [ ] Log each signal as a hypothetical trade (entry = next day open, stop as calculated)
- [ ] Track outcome: did the trade hit T1? Hit stop? Stage change?
- [ ] After 30+ trades: compute win rate, average R/R, Sharpe on simulated returns
- [ ] **Gate:** Only proceed to live trading if net Sharpe > 1.0 on paper trades

### Phase 4 — Live Trading, Small Size (Month 3)

- [ ] Build `zerodha.py` and `ibkr.py` execution wrappers
- [ ] Build `order_manager.py` and `position_monitor.py`
- [ ] Deploy with small capital: ₹2L Indian + $2,000 IBKR
- [ ] Maximum 5 simultaneous positions
- [ ] Monitor every trade manually. Confirm system behavior matches expectations.

### Phase 5 — Scale (Month 4+)

- [ ] After 50+ live trades with positive expectancy and Sharpe > 1.0
- [ ] Gradually increase position sizes (20% increase per month if performing well)
- [ ] Expand universe to full NSE + BSE, then full Russell 1000
- [ ] Consider adding a second strategy layer (mean reversion on Stage 1 bases) for diversification

---

## Part 9 — Known Risks and Failure Modes

| Risk | Likelihood | Mitigation |
|------|-----------|-----------|
| Strategy stops working during flat/bear market | High — all momentum strategies suffer | Market regime filter: no new longs when index < MA200 |
| Overfitting to historical data | Medium | Validate on out-of-sample data only; limit parameters |
| Zerodha API downtime | Low-Medium | Manual fallback: check watchlist and place orders manually on market days |
| IBKR TWS disconnects | Medium | Auto-reconnect in `ib_insync`; VPS with process monitor (systemd) |
| Flash crash / black swan | Low but severe | Portfolio stops (25% drawdown = full liquidation); no leverage |
| LRS annual limit exhausted | Low | Track cumulative remittances; $250k/year is generous for this strategy size |
| Tax reporting complexity | Certain | Use a CA familiar with LRS/foreign assets for first ITR filing |
| Data gaps / bad ticks | Medium | Validate price data before computing indicators; flag stocks where MA50 cannot be computed |
| False breakouts | High (inherent) | Volume confirmation filter + trailing stops limit damage to 1.5% per trade |

---

## Part 10 — What to Read

**To deeply understand the strategy's academic basis:**
- Jegadeesh & Titman (1993) — *Returns to Buying Winners and Selling Losers* (momentum is real)
- Carhart (1997) — *On Persistence in Mutual Fund Performance* (momentum as a systematic factor)
- Stan Weinstein — *Secrets for Profiting in Bull and Bear Markets* (the stage analysis framework)

**To understand execution and portfolio management:**
- Almgren & Chriss (2001) — *Optimal Execution of Portfolio Transactions* (market impact)
- Ernest Chan — *Quantitative Trading* (practical implementation guide)
- Marcos López de Prado — *Advances in Financial Machine Learning* (rigorous ML + backtest methodology)

**To understand risk:**
- Bailey & López de Prado (2014) — *The Deflated Sharpe Ratio* (why most backtests are overfit)
- Harvey, Liu & Zhu (2016) — *...and the Cross-Section of Expected Returns* (multiple testing problem)
