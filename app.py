import math
import pandas as pd
from flask import Flask, jsonify, render_template, request

from analysis import (
    compute_indicators,
    compute_targets_stoploss,
    detect_phase,
    fetch_data,
    find_support_resistance,
    generate_summary,
    get_company_info,
    get_shareholding,
)

app = Flask(__name__)

VALID_PERIODS = {"3mo", "6mo", "1y", "3y"}


def _clean(x):
    """Convert a scalar to JSON-safe value (None for NaN/Inf)."""
    if x is None:
        return None
    try:
        f = float(x)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
    except (TypeError, ValueError):
        return None


def safe_list(series: pd.Series) -> list:
    return [_clean(x) for x in series]


@app.route("/")
def index():
    return render_template("index.html")


@app.route("/api/analyze")
def analyze():
    raw = request.args.get("ticker", "").strip()
    period = request.args.get("period", "1y")

    if not raw:
        return jsonify({"error": "Please enter a ticker symbol."}), 400
    if period not in VALID_PERIODS:
        period = "1y"

    try:
        df, ticker_obj, resolved, info = fetch_data(raw, period)
        df = compute_indicators(df)

        support, resistance = find_support_resistance(df)
        phase, phase_desc = detect_phase(df)
        levels = compute_targets_stoploss(df, support, resistance)
        summary = generate_summary(df, phase, levels)
        shareholding = get_shareholding(info, resolved)
        company = get_company_info(info)

        # Daily price change
        if len(df) >= 2:
            prev = float(df["Close"].iloc[-2])
            curr = float(df["Close"].iloc[-1])
            day_change = round(curr - prev, 4)
            day_pct = round((curr / prev - 1) * 100, 2)
        else:
            day_change = day_pct = 0.0

        dates = [str(d.date()) for d in df.index]

        # Volume colors (green = up day, red = down day)
        close = df["Close"]
        vol_colors = ["#26de81" if close.iloc[i] >= close.iloc[i - 1] else "#fc5c65"
                      for i in range(len(df))]

        return jsonify({
            "ticker": raw.upper(),
            "resolved": resolved,
            "period": period,
            "day_change": day_change,
            "day_pct": day_pct,
            "phase": phase,
            "phase_description": phase_desc,
            "summary": summary,
            "levels": levels,
            "support": support,
            "resistance": resistance,
            "company": company,
            "shareholding": shareholding,
            "ohlcv": {
                "dates": dates,
                "open": safe_list(df["Open"]),
                "high": safe_list(df["High"]),
                "low": safe_list(df["Low"]),
                "close": safe_list(df["Close"]),
                "volume": safe_list(df["Volume"]),
                "vol_colors": vol_colors,
            },
            "indicators": {
                "ma50": safe_list(df["MA50"]),
                "ma200": safe_list(df["MA200"]),
                "bb_upper": safe_list(df["BB_upper"]),
                "bb_mid": safe_list(df["BB_mid"]),
                "bb_lower": safe_list(df["BB_lower"]),
                "bb_width": safe_list(df["BB_width"]),
                "bb_squeeze": [bool(x) for x in df["BB_squeeze"].fillna(False)],
                "rsi": safe_list(df["RSI"]),
                "macd": safe_list(df["MACD"]),
                "macd_sig": safe_list(df["MACD_sig"]),
                "macd_hist": safe_list(df["MACD_hist"]),
                "atr": safe_list(df["ATR"]),
                "obv": safe_list(df["OBV"]),
            },
        })

    except ValueError as e:
        return jsonify({"error": str(e)}), 404
    except Exception as e:
        return jsonify({"error": f"Analysis failed: {e}"}), 500


if __name__ == "__main__":
    import os
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
