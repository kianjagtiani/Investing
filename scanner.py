"""
Stock universe definition and full-scan logic.

The universe covers a representative sample of NSE Nifty 500 constituents
and S&P 500 / Nasdaq 100 names.  Run `run_full_scan(app)` from a background
thread or a scheduled job — it is safe to call while the Flask app is serving
requests because every DB operation runs inside an explicit application context.
"""

import datetime
import math
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd

from analysis import (
    compute_indicators,
    compute_targets_stoploss,
    detect_phase,
    fetch_data,
    find_support_resistance,
    get_company_info,
)

# ── Universe ──────────────────────────────────────────────────────────────────

_NSE_TICKERS = [
    "RELIANCE", "TCS", "HDFCBANK", "INFY", "ICICIBANK",
    "HINDUNILVR", "ITC", "KOTAKBANK", "LT", "AXISBANK",
    "BAJFINANCE", "BHARTIARTL", "ASIANPAINT", "MARUTI", "NESTLEIND",
    "TITAN", "ULTRACEMCO", "WIPRO", "HCLTECH", "SUNPHARMA",
    "POWERGRID", "NTPC", "TATAMOTORS", "INDUSINDBK", "SBILIFE",
    "BAJAJFINSV", "TECHM", "ADANIPORTS", "ONGC", "COALINDIA",
    "TATASTEEL", "JSWSTEEL", "HINDALCO", "GRASIM", "VEDL",
    "CIPLA", "DRREDDY", "DIVISLAB", "EICHERMOT", "HEROMOTOCO",
    "BAJAJ_AUTO", "BRITANNIA", "PIDILITIND", "HAVELLS", "VOLTAS",
    "TRENT", "NAUKRI", "MCDOWELL_N", "TATACONSUM", "BERGEPAINT",
]

_US_TICKERS = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL",
    "META", "TSLA", "BRK-B", "JPM", "JNJ",
    "V", "PG", "UNH", "HD", "MA",
    "DIS", "BAC", "XOM", "COST", "PFE",
    "ABBV", "TMO", "AVGO", "MRK", "PEP",
    "KO", "WMT", "CSCO", "ACN", "DHR",
    "VZ", "INTC", "CRM", "ADBE", "NEE",
    "TXN", "PM", "BMY", "AMGN", "RTX",
    "QCOM", "UPS", "HON", "SBUX", "GS",
    "MS", "BLK", "SPGI", "ISRG", "MDT",
]


def build_universe():
    """Return a list of (yfinance_ticker, exchange) tuples.

    NSE tickers get the '.NS' suffix and are tagged 'NSE'.
    US tickers are used as-is and tagged 'NYSE/NASDAQ'.
    """
    universe = []
    for t in _NSE_TICKERS:
        universe.append((f"{t}.NS", "NSE"))
    for t in _US_TICKERS:
        universe.append((t, "NYSE/NASDAQ"))
    return universe


# ── Signal scoring ────────────────────────────────────────────────────────────

def compute_signal_score(
    phase: str,
    df: pd.DataFrame,
    levels: dict,
    info: dict,
) -> float:
    """Score a stock 0-100 based on technical setup quality.

    Scoring breakdown:
      +40  Phase 2 (Weinstein markup) confirmed
      +15  Bollinger Band squeeze active in last 10 bars
      +10  RSI between 50 and 70 (healthy momentum, not overbought)
      +5   MACD above signal line (bullish momentum)
      +5   Risk/reward to T1 >= 2.0x
      +10  Price within 5% of 52-week high (relative strength)
      +15  Last-bar volume > 1.5x the 20-day average (institutional interest)
    """
    score = 0.0

    # Phase 2 bonus
    if "Phase 2" in phase:
        score += 40.0

    # BB squeeze in last 10 bars
    if "BB_squeeze" in df.columns:
        if df["BB_squeeze"].iloc[-10:].any():
            score += 15.0

    # RSI 50–70
    if "RSI" in df.columns:
        rsi_val = df["RSI"].iloc[-1]
        if not (isinstance(rsi_val, float) and math.isnan(rsi_val)):
            rsi = float(rsi_val)
            if 50.0 <= rsi <= 70.0:
                score += 10.0

    # MACD bullish crossover
    if "MACD" in df.columns and "MACD_sig" in df.columns:
        macd_val = df["MACD"].iloc[-1]
        sig_val = df["MACD_sig"].iloc[-1]
        try:
            if float(macd_val) > float(sig_val):
                score += 5.0
        except (TypeError, ValueError):
            pass

    # R/R >= 2.0
    rr = levels.get("rr_t1", 0) or 0
    try:
        if float(rr) >= 2.0:
            score += 5.0
    except (TypeError, ValueError):
        pass

    # Price within 5% of 52-week high
    week_52_high = info.get("fiftyTwoWeekHigh")
    if week_52_high:
        try:
            close = float(df["Close"].iloc[-1])
            if close >= float(week_52_high) * 0.95:
                score += 10.0
        except (TypeError, ValueError):
            pass

    # Volume spike: last bar > 1.5x 20-day average
    if "Volume" in df.columns and len(df) >= 20:
        avg_vol = df["Volume"].iloc[-20:].mean()
        last_vol = df["Volume"].iloc[-1]
        try:
            if float(last_vol) > 1.5 * float(avg_vol):
                score += 15.0
        except (TypeError, ValueError):
            pass

    return max(0.0, min(100.0, score))


# ── Per-ticker scan ───────────────────────────────────────────────────────────

def scan_ticker(ticker: str, exchange: str, app):  # returns dict or None
    """Fetch data and compute all signals for a single ticker.

    Returns a dict matching the ScanResult model fields, or None on any error.
    Must run inside an app.application_context so ORM imports do not fail.
    """
    with app.application_context():
        try:
            df, _ticker_obj, resolved, info = fetch_data(ticker, "1y")
        except Exception as exc:
            print(f"[scan] {ticker}: fetch failed — {exc}")
            return None

        try:
            df = compute_indicators(df)
            support, resistance = find_support_resistance(df)
            phase, _phase_desc = detect_phase(df)
            levels = compute_targets_stoploss(df, support, resistance)
            score = compute_signal_score(phase, df, levels, info)
            company = get_company_info(info)

            def _safe(val):
                if val is None:
                    return None
                try:
                    f = float(val)
                    return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
                except (TypeError, ValueError):
                    return None

            return {
                "ticker": ticker,
                "resolved": resolved,
                "exchange": exchange,
                "company_name": company.get("name"),
                "phase": phase,
                "signal_score": round(score, 2),
                "close": _safe(df["Close"].iloc[-1]),
                "ma50": _safe(df["MA50"].iloc[-1]) if "MA50" in df.columns else None,
                "ma200": _safe(df["MA200"].iloc[-1]) if "MA200" in df.columns else None,
                "t1": _safe(levels.get("t1")),
                "t2": _safe(levels.get("t2")),
                "stop_loss": _safe(levels.get("stop_loss")),
                "rr": _safe(levels.get("rr_t1")),
                "last_scanned": datetime.datetime.utcnow(),
            }
        except Exception as exc:
            print(f"[scan] {ticker}: analysis failed — {exc}")
            return None


# ── Full scan ─────────────────────────────────────────────────────────────────

def run_full_scan(app) -> None:
    """Scan every ticker in the universe and upsert results into the database.

    Runs with up to 10 concurrent threads so the full scan completes in a
    few minutes rather than hours.  After all tickers are processed, open
    positions are checked against their stop-loss and target levels.
    """
    from models import db, ScanResult  # imported here to avoid circular import

    start = datetime.datetime.utcnow()
    print(f"[scan] Starting full scan at {start.isoformat()} UTC")

    universe = build_universe()
    results = []

    with ThreadPoolExecutor(max_workers=10) as executor:
        futures = {
            executor.submit(scan_ticker, ticker, exchange, app): ticker
            for ticker, exchange in universe
        }
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                print(f"[scan] {ticker}: unexpected error — {exc}")
                result = None

            if result is not None:
                results.append(result)

    # Upsert into database
    with app.application_context():
        for data in results:
            existing = ScanResult.query.filter_by(ticker=data["ticker"]).first()
            if existing:
                for key, value in data.items():
                    setattr(existing, key, value)
            else:
                row = ScanResult(**data)
                db.session.add(row)
        db.session.commit()

    check_position_alerts(app)

    elapsed = (datetime.datetime.utcnow() - start).total_seconds()
    print(
        f"[scan] Complete — {len(results)} tickers scanned in {elapsed:.1f}s"
    )


# ── Position alerts ───────────────────────────────────────────────────────────

def check_position_alerts(app) -> None:
    """Send Telegram alerts when an open position hits its stop or target."""
    from models import ScanResult, Position  # avoid circular import
    from alerts import send_alert

    with app.application_context():
        open_positions = Position.query.filter_by(closed_at=None).all()
        for position in open_positions:
            scan_result = ScanResult.query.filter_by(ticker=position.ticker).first()
            if not scan_result or scan_result.close is None:
                continue

            price = scan_result.close
            user = position.user  # backref from models

            chat_id = user.telegram_chat_id if user else None
            if not chat_id:
                continue

            if position.stop_loss is not None and price <= position.stop_loss:
                send_alert(
                    chat_id,
                    f"STOP LOSS HIT: {position.ticker} at {price}",
                )
            elif position.target1 is not None and price >= position.target1:
                send_alert(
                    chat_id,
                    f"T1 HIT: {position.ticker} at {price}",
                )
