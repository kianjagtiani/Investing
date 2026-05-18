import math
import datetime
from typing import Optional
import yfinance as yf
import pandas as pd
import numpy as np


# Period string → calendar days (use generous count to capture all trading days)
PERIOD_DAYS = {"3mo": 95, "6mo": 185, "1y": 370, "3y": 1100}


# ── Ticker resolution ───────────────────────────────────────────────────────

def _download(ticker: str, start: str, end: str) -> pd.DataFrame:
    """Download and flatten yfinance MultiIndex columns."""
    df = yf.download(ticker, start=start, end=end, progress=False, auto_adjust=True)
    if isinstance(df.columns, pd.MultiIndex):
        df.columns = df.columns.get_level_values(0)
    return df


def resolve_ticker(raw: str) -> Optional[str]:
    """Try ticker as-is, then .NS (NSE), then .BO (BSE) for Indian stocks."""
    ticker = raw.upper().strip()
    end = datetime.date.today().strftime("%Y-%m-%d")
    start = (datetime.date.today() - datetime.timedelta(days=7)).strftime("%Y-%m-%d")
    for t in [ticker, f"{ticker}.NS", f"{ticker}.BO"]:
        try:
            df = _download(t, start, end)
            if len(df) > 2:
                return t
        except Exception:
            continue
    return None


def fetch_data(raw: str, period: str = "1y"):
    resolved = resolve_ticker(raw)
    if not resolved:
        raise ValueError(
            f"Cannot find '{raw}'. For Indian stocks try RELIANCE, TCS, INFY — "
            "the .NS suffix is added automatically."
        )

    days = PERIOD_DAYS.get(period, 370)
    end_date = datetime.date.today()
    start_date = end_date - datetime.timedelta(days=days)

    df = _download(resolved, start_date.strftime("%Y-%m-%d"), end_date.strftime("%Y-%m-%d"))
    df = df.dropna(how="all")
    df.index = pd.to_datetime(df.index)

    ticker_obj = yf.Ticker(resolved)
    try:
        info = ticker_obj.info or {}
    except Exception:
        info = {}

    return df, ticker_obj, resolved, info


# ── Individual indicators ────────────────────────────────────────────────────

def compute_rsi(series: pd.Series, period: int = 14) -> pd.Series:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.ewm(com=period - 1, min_periods=period).mean()
    avg_loss = loss.ewm(com=period - 1, min_periods=period).mean()
    rs = avg_gain / avg_loss.replace(0, np.nan)
    return 100 - (100 / (1 + rs))


def compute_macd(series: pd.Series):
    ema12 = series.ewm(span=12, adjust=False).mean()
    ema26 = series.ewm(span=26, adjust=False).mean()
    macd = ema12 - ema26
    signal = macd.ewm(span=9, adjust=False).mean()
    return macd, signal, macd - signal


def compute_atr(df: pd.DataFrame, period: int = 14) -> pd.Series:
    high, low, close = df["High"], df["Low"], df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat(
        [high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1
    ).max(axis=1)
    return tr.rolling(period).mean()


def compute_obv(df: pd.DataFrame) -> pd.Series:
    direction = np.sign(df["Close"].diff().fillna(0))
    return (direction * df["Volume"]).cumsum()


# ── Full indicator suite ─────────────────────────────────────────────────────

def compute_indicators(df: pd.DataFrame) -> pd.DataFrame:
    df = df.copy()
    close = df["Close"]

    df["MA50"] = close.rolling(50).mean()
    df["MA200"] = close.rolling(200).mean()

    # Bollinger Bands (20-period, 2 std)
    df["BB_mid"] = close.rolling(20).mean()
    bb_std = close.rolling(20).std()
    df["BB_upper"] = df["BB_mid"] + 2 * bb_std
    df["BB_lower"] = df["BB_mid"] - 2 * bb_std
    df["BB_width"] = (df["BB_upper"] - df["BB_lower"]) / df["BB_mid"]

    # Squeeze: BB width at its narrowest in last 20 bars (low vol before big move)
    df["BB_squeeze"] = df["BB_width"] < df["BB_width"].rolling(20).min().shift(1)

    df["RSI"] = compute_rsi(close)
    df["MACD"], df["MACD_sig"], df["MACD_hist"] = compute_macd(close)
    df["ATR"] = compute_atr(df)
    df["OBV"] = compute_obv(df)

    return df


# ── Support & Resistance ─────────────────────────────────────────────────────

def _cluster(levels, tolerance=0.015):
    """Merge nearby S/R levels that are within tolerance% of each other."""
    if not levels:
        return []
    levels = sorted(levels)
    out = [levels[0]]
    for lvl in levels[1:]:
        if (lvl - out[-1]) / out[-1] > tolerance:
            out.append(lvl)
        else:
            out[-1] = (out[-1] + lvl) / 2  # average the cluster
    return out


def find_support_resistance(df: pd.DataFrame, num_levels: int = 5):
    n = len(df)
    window = max(8, n // 25)  # adaptive: wider window on longer series

    highs = df["High"].values
    lows = df["Low"].values

    raw_res, raw_sup = [], []
    for i in range(window, n - window):
        h_win = highs[i - window : i + window + 1]
        l_win = lows[i - window : i + window + 1]
        if highs[i] == h_win.max():
            raw_res.append(float(highs[i]))
        if lows[i] == l_win.min():
            raw_sup.append(float(lows[i]))

    support = _cluster(raw_sup)[-num_levels:]
    resistance = _cluster(raw_res)[-num_levels:]
    return support, resistance


# ── Phase detection (Weinstein Stage Analysis) ───────────────────────────────

def detect_phase(df: pd.DataFrame) -> tuple[str, str]:
    n = len(df)
    if n < 50:
        return "Insufficient Data", "Need at least 50 bars."

    close = float(df["Close"].iloc[-1])
    ma50_val = df["MA50"].iloc[-1]
    if pd.isna(ma50_val):
        return "Insufficient Data", "Need at least 50 bars for MA50."

    ma50 = float(ma50_val)
    has_200 = n >= 200 and not pd.isna(df["MA200"].iloc[-1])
    ma200 = float(df["MA200"].iloc[-1]) if has_200 else None

    slope_bars = min(20, n - 1)
    ma50_slope = (float(df["MA50"].iloc[-1]) - float(df["MA50"].iloc[-slope_bars])) / float(
        df["MA50"].iloc[-slope_bars]
    )
    ma200_slope = (
        (float(df["MA200"].iloc[-1]) - float(df["MA200"].iloc[-slope_bars]))
        / float(df["MA200"].iloc[-slope_bars])
        if has_200
        else 0
    )

    if has_200 and ma200:
        if close > ma50 > ma200 and ma50_slope > 0.005 and ma200_slope > 0:
            return (
                "Phase 2 — Markup (Uptrend)",
                "Price above rising 50 DMA, above rising 200 DMA. "
                "Classic Weinstein Stage 2 — the strongest sustained buy zone. "
                "Look for pullbacks to the 50 DMA as low-risk entries.",
            )
        elif close > ma50 and ma200_slope > 0 and abs(ma50_slope) < 0.005:
            return (
                "Phase 3 — Distribution",
                "50 DMA flattening while price is extended above it. "
                "Trend losing momentum. Tighten stops, reduce new longs, "
                "watch for a rollover below the 50 DMA.",
            )
        elif close < ma50 < ma200 and ma50_slope < -0.005:
            return (
                "Phase 4 — Decline (Downtrend)",
                "Price below falling 50 DMA, which is below the 200 DMA. "
                "Avoid long positions. Wait patiently for a base to form.",
            )
        elif abs(ma200_slope) < 0.005 and 0.93 < (close / ma50) < 1.05:
            return (
                "Phase 1 — Accumulation (Base Building)",
                "200 DMA flat, price consolidating near support. "
                "Smart money is quietly accumulating. Watch for a high-volume "
                "breakout above the 50 DMA — that's your Stage 2 entry signal.",
            )
        else:
            return (
                "Transition / Consolidation",
                "Mixed signals between moving averages. No clear trend. "
                "Wait for a confirmed Phase 2 breakout before committing capital.",
            )
    else:
        if close > ma50 and ma50_slope > 0.005:
            return "Uptrend (above rising 50 DMA)", "Short-term bullish — price above rising 50 DMA."
        elif close < ma50 and ma50_slope < -0.005:
            return "Downtrend (below falling 50 DMA)", "Short-term bearish — price below falling 50 DMA."
        else:
            return "Sideways / Near 50 DMA", "No clear directional conviction — price hovering near 50 DMA."


# ── Targets & Stop Loss ──────────────────────────────────────────────────────

def compute_targets_stoploss(df: pd.DataFrame, support: list, resistance: list) -> dict:
    close = float(df["Close"].iloc[-1])
    ma50 = float(df["MA50"].iloc[-1]) if not pd.isna(df["MA50"].iloc[-1]) else close * 0.97

    # Targets: nearest resistance levels above current price
    above = sorted(r for r in resistance if r > close * 1.003)
    t1 = round(above[0], 2) if above else round(close * 1.08, 2)
    t2 = round(above[1], 2) if len(above) > 1 else round(close * 1.16, 2)

    # Stop loss: just below strongest support beneath price, or 3% below 50 DMA
    below = sorted(s for s in support if s < close * 0.997)
    sl_support = round(below[-1] * 0.992, 2) if below else None
    sl_ma = round(ma50 * 0.97, 2)
    stop_loss = round(max(sl_support, sl_ma), 2) if sl_support else sl_ma

    risk = close - stop_loss
    return {
        "current": round(close, 2),
        "t1": t1,
        "t2": t2,
        "stop_loss": stop_loss,
        "rr_t1": round((t1 - close) / risk, 2) if risk > 0 else 0,
        "rr_t2": round((t2 - close) / risk, 2) if risk > 0 else 0,
        "upside_t1_pct": round((t1 / close - 1) * 100, 1),
        "upside_t2_pct": round((t2 / close - 1) * 100, 1),
        "downside_sl_pct": round((stop_loss / close - 1) * 100, 1),
    }


# ── Shareholding ─────────────────────────────────────────────────────────────

def get_shareholding(info: dict, resolved: str) -> Optional[dict]:
    try:
        insider = (info.get("heldPercentInsiders") or 0) * 100
        institution = (info.get("heldPercentInstitutions") or 0) * 100
        total = min(insider + institution, 99.0)
        public = round(100 - total, 1)

        is_indian = resolved.endswith(".NS") or resolved.endswith(".BO")
        return {
            "labels": [
                "Promoter / Insider" if is_indian else "Insider (Officers & Directors)",
                "FII + DII + Mutual Funds" if is_indian else "Institutions",
                "Public / Retail",
            ],
            "values": [round(insider, 1), round(institution, 1), public],
        }
    except Exception:
        return None


# ── Company metadata ─────────────────────────────────────────────────────────

def get_company_info(info: dict) -> dict:
    def safe(key, default=None):
        v = info.get(key)
        if v is None:
            return default
        if isinstance(v, float) and (math.isnan(v) or math.isinf(v)):
            return default
        return v

    mc = safe("marketCap")
    if mc:
        if mc >= 1e12:
            mc_str = f"{mc/1e12:.2f}T"
        elif mc >= 1e9:
            mc_str = f"{mc/1e9:.2f}B"
        else:
            mc_str = f"{mc/1e6:.2f}M"
    else:
        mc_str = None

    dy = safe("dividendYield")

    return {
        "name": safe("longName") or safe("shortName"),
        "sector": safe("sector"),
        "industry": safe("industry"),
        "currency": safe("currency", "USD"),
        "market_cap": mc_str,
        "pe_ratio": safe("trailingPE"),
        "pb_ratio": safe("priceToBook"),
        "dividend_yield": round(dy * 100, 2) if dy else None,
        "week_52_high": safe("fiftyTwoWeekHigh"),
        "week_52_low": safe("fiftyTwoWeekLow"),
        "avg_volume": safe("averageVolume"),
        "beta": safe("beta"),
        "eps": safe("trailingEps"),
    }


# ── Summary & Investment Plan ────────────────────────────────────────────────

def generate_summary(df: pd.DataFrame, phase: str, levels: dict) -> dict:
    """Rule-based 2-line summary + investment plan from current indicator snapshot."""
    close = levels["current"]

    # Current bar values
    rsi = float(df["RSI"].iloc[-1]) if not pd.isna(df["RSI"].iloc[-1]) else None
    macd = float(df["MACD"].iloc[-1]) if not pd.isna(df["MACD"].iloc[-1]) else None
    macd_sig = float(df["MACD_sig"].iloc[-1]) if not pd.isna(df["MACD_sig"].iloc[-1]) else None
    bb_w = float(df["BB_width"].iloc[-1]) if not pd.isna(df["BB_width"].iloc[-1]) else None
    bb_upper = float(df["BB_upper"].iloc[-1]) if not pd.isna(df["BB_upper"].iloc[-1]) else None
    bb_lower = float(df["BB_lower"].iloc[-1]) if not pd.isna(df["BB_lower"].iloc[-1]) else None
    atr = float(df["ATR"].iloc[-1]) if not pd.isna(df["ATR"].iloc[-1]) else None
    squeeze = bool(df["BB_squeeze"].iloc[-1])

    macd_bull = macd is not None and macd_sig is not None and macd > macd_sig

    # RSI label
    if rsi is None:        rsi_label = "unknown momentum"
    elif rsi > 72:         rsi_label = f"overbought (RSI {rsi:.0f})"
    elif rsi > 55:         rsi_label = f"strong bullish momentum (RSI {rsi:.0f})"
    elif rsi > 45:         rsi_label = f"neutral momentum (RSI {rsi:.0f})"
    elif rsi > 28:         rsi_label = f"bearish momentum (RSI {rsi:.0f})"
    else:                  rsi_label = f"oversold (RSI {rsi:.0f})"

    # BB position label
    if bb_upper and close > bb_upper * 0.98:
        bb_label = "riding the upper Bollinger Band (extended)"
    elif bb_lower and close < bb_lower * 1.02:
        bb_label = "testing the lower Bollinger Band (potential bounce)"
    else:
        bb_label = None

    # Trend phrase
    if "Phase 2" in phase or "Uptrend" in phase:
        trend_phrase = "in a confirmed uptrend (Weinstein Stage 2)"
    elif "Phase 1" in phase or "Accumulation" in phase:
        trend_phrase = "building a base — Stage 1 accumulation underway"
    elif "Phase 3" in phase or "Distribution" in phase:
        trend_phrase = "showing distribution signs — trend losing momentum"
    elif "Phase 4" in phase or "Decline" in phase:
        trend_phrase = "in a confirmed downtrend (Weinstein Stage 4)"
    else:
        trend_phrase = "in a sideways / consolidation pattern"

    # MACD phrase
    macd_phrase = "MACD bullish (momentum building)" if macd_bull else "MACD bearish (momentum fading)"

    # One-liner
    line1 = f"Stock is {trend_phrase}, with {rsi_label}."
    extras = []
    if bb_label:
        extras.append(bb_label)
    if squeeze:
        extras.append("Bollinger Band squeeze detected — a big directional move is loading")
    extras.append(macd_phrase)
    line2 = ". ".join(extras[:2]) + "."

    # Investment plan
    t1, t2, sl, rr = levels["t1"], levels["t2"], levels["stop_loss"], levels["rr_t1"]

    if "Phase 2" in phase or "Uptrend" in phase:
        if rsi and rsi > 72:
            action = "WAIT FOR PULLBACK"
            color = "yellow"
            plan = (
                f"Uptrend is intact but RSI is overbought at {rsi:.0f} — chasing here is risky. "
                f"Wait for a pullback toward the 50 DMA before entering. "
                f"On a healthy dip: entry near 50 DMA, target T1 {t1:,.2f}, stop {sl:,.2f}."
            )
        elif bb_upper and close > bb_upper * 0.99:
            action = "HOLD / PARTIAL PROFIT"
            color = "yellow"
            plan = (
                f"Price is extended at the upper Bollinger Band. "
                f"If long, consider booking 30–50% at current levels. "
                f"Hold remainder with stop at {sl:,.2f}, targeting T2 {t2:,.2f}."
            )
        elif rr and rr >= 1.5:
            action = "BUY / ACCUMULATE"
            color = "green"
            plan = (
                f"Strong Stage 2 uptrend with acceptable risk/reward ({rr}x). "
                f"Target T1 {t1:,.2f} (+{levels['upside_t1_pct']}%), T2 {t2:,.2f} (+{levels['upside_t2_pct']}%). "
                f"Stop loss at {sl:,.2f} ({levels['downside_sl_pct']}%)."
            )
        else:
            action = "BUY (SMALL POSITION)"
            color = "green"
            plan = (
                f"Uptrend confirmed but R/R is modest ({rr}x). "
                f"Consider a smaller position size. T1 {t1:,.2f}, stop {sl:,.2f}."
            )

    elif "Phase 1" in phase or "Accumulation" in phase:
        action = "WATCH — NOT YET"
        color = "blue"
        plan = (
            f"Base building in progress. Do NOT buy yet — patience is the edge here. "
            f"Set a price alert at the 50 DMA breakout level. "
            f"Entry only on a confirmed breakout above 50 DMA with above-average volume."
        )

    elif "Phase 3" in phase or "Distribution" in phase:
        action = "TAKE PROFITS"
        color = "yellow"
        plan = (
            f"Trend is losing momentum — distribution phase. "
            f"If long, raise stop to {sl:,.2f} and consider booking 50%+ profits. "
            f"Avoid adding new long positions here."
        )

    elif "Phase 4" in phase or "Decline" in phase:
        action = "AVOID / STAY FLAT"
        color = "red"
        plan = (
            f"Active downtrend — no buy case. "
            f"If holding, cut losses or set a hard stop at {sl:,.2f}. "
            f"Revisit when price reclaims and holds above the 50 DMA for 2+ weeks."
        )

    else:  # consolidation / transition
        if squeeze:
            action = "WATCH — SQUEEZE SETUP"
            color = "blue"
            plan = (
                f"Low volatility squeeze detected — a significant move is coming. "
                f"Set alerts for a breakout above resistance ({t1:,.2f}) or breakdown below support ({sl:,.2f}). "
                f"Trade in the direction of the breakout."
            )
        else:
            action = "NEUTRAL — WAIT"
            color = "gray"
            plan = (
                f"No clear trend conviction. Preserve capital and wait for a Phase 2 setup. "
                f"Key levels to watch: resistance {t1:,.2f}, support {sl:,.2f}."
            )

    # ATR context for position sizing
    if atr:
        atr_pct = round(atr / close * 100, 1)
        plan += f" (Daily ATR: {atr:,.2f} = {atr_pct}% of price — size accordingly.)"

    return {
        "line1": line1,
        "line2": line2,
        "action": action,
        "action_color": color,
        "plan": plan,
        "rsi": round(rsi, 1) if rsi else None,
        "macd_bullish": macd_bull,
        "bb_squeeze": squeeze,
    }
