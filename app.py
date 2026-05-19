import math
import os
import threading
import datetime

import pandas as pd
from flask import Flask, jsonify, render_template, request, redirect, url_for, abort
from flask_login import LoginManager, login_required, current_user

from models import db, User, ScanResult, Position
from auth import auth_bp
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
from scanner import run_full_scan

app = Flask(__name__)
app.config["SECRET_KEY"] = os.environ.get("SECRET_KEY", "dev-secret-change-in-prod")

# DB: use DATABASE_URL env var (Supabase PostgreSQL) or fall back to local SQLite
database_url = os.environ.get("DATABASE_URL", "sqlite:///local.db")
# SQLAlchemy requires postgresql:// not postgres://
if database_url.startswith("postgres://"):
    database_url = database_url.replace("postgres://", "postgresql://", 1)
app.config["SQLALCHEMY_DATABASE_URI"] = database_url
app.config["SQLALCHEMY_TRACK_MODIFICATIONS"] = False

db.init_app(app)

login_manager = LoginManager(app)
login_manager.login_view = "auth_bp.login"


@login_manager.user_loader
def load_user(user_id):
    return db.session.get(User, int(user_id))


@login_manager.unauthorized_handler
def unauthorized():
    # API routes must return JSON, not an HTML redirect
    if request.path.startswith("/api/"):
        return jsonify({"error": "Authentication required"}), 401
    return redirect(url_for("auth_bp.login"))


app.register_blueprint(auth_bp)

VALID_PERIODS = {"3mo", "6mo", "1y", "3y"}
SCAN_SECRET = os.environ.get("SCAN_SECRET", "")


def _clean(x):
    if x is None:
        return None
    try:
        f = float(x)
        return None if (math.isnan(f) or math.isinf(f)) else round(f, 4)
    except (TypeError, ValueError):
        return None


def safe_list(series: pd.Series) -> list:
    return [_clean(x) for x in series]


with app.app_context():
    db.create_all()


# ── Pages ─────────────────────────────────────────────────────────────────────

@app.route("/")
def index():
    return render_template("research.html")


@app.route("/stock/<ticker>")
def stock_detail(ticker):
    period = request.args.get("period", "1y")
    if period not in VALID_PERIODS:
        period = "1y"
    return render_template("stock.html", ticker=ticker.upper(), period=period)


@app.route("/portfolio")
@login_required
def portfolio():
    return render_template("portfolio.html")


# ── Analysis API (unchanged, called by stock.html) ────────────────────────────

_DISPLAY_DAYS = {"3mo": 95, "6mo": 185, "1y": 370, "3y": 1100}


@app.route("/api/analyze")
def analyze():
    import time
    raw = request.args.get("ticker", "").strip()
    period = request.args.get("period", "1y")
    if not raw:
        return jsonify({"error": "Please enter a ticker symbol."}), 400
    if period not in VALID_PERIODS:
        period = "1y"

    last_err = None
    for attempt in range(2):
        try:
            # Always fetch 3y so MA200 is fully populated; trim to display period after
            df_full, ticker_obj, resolved, info = fetch_data(raw, "3y")
            df_full = compute_indicators(df_full)

            display_days = _DISPLAY_DAYS.get(period, 370)
            cutoff = df_full.index[-1] - pd.Timedelta(days=display_days)
            df = df_full[df_full.index >= cutoff].copy()

            support, resistance = find_support_resistance(df)
            phase, phase_desc = detect_phase(df)
            levels = compute_targets_stoploss(df, support, resistance)
            summary = generate_summary(df, phase, levels)
            shareholding = get_shareholding(info, resolved)
            company = get_company_info(info)

            if len(df) >= 2:
                prev = float(df["Close"].iloc[-2])
                curr = float(df["Close"].iloc[-1])
                day_change = round(curr - prev, 4)
                day_pct = round((curr / prev - 1) * 100, 2)
            else:
                day_change = day_pct = 0.0

            dates = [str(d.date()) for d in df.index]
            close = df["Close"]
            vol_colors = [
                "#26de81" if close.iloc[i] >= close.iloc[i - 1] else "#fc5c65"
                for i in range(len(df))
            ]

            return jsonify({
                "ticker": raw.upper(), "resolved": resolved, "period": period,
                "day_change": day_change, "day_pct": day_pct,
                "phase": phase, "phase_description": phase_desc,
                "summary": summary, "levels": levels,
                "support": support, "resistance": resistance,
                "company": company, "shareholding": shareholding,
                "ohlcv": {
                    "dates": dates, "open": safe_list(df["Open"]),
                    "high": safe_list(df["High"]), "low": safe_list(df["Low"]),
                    "close": safe_list(df["Close"]), "volume": safe_list(df["Volume"]),
                    "vol_colors": vol_colors,
                },
                "indicators": {
                    "ma50": safe_list(df["MA50"]), "ma200": safe_list(df["MA200"]),
                    "bb_upper": safe_list(df["BB_upper"]), "bb_mid": safe_list(df["BB_mid"]),
                    "bb_lower": safe_list(df["BB_lower"]), "bb_width": safe_list(df["BB_width"]),
                    "bb_squeeze": [bool(x) for x in df["BB_squeeze"].fillna(False)],
                    "rsi": safe_list(df["RSI"]), "macd": safe_list(df["MACD"]),
                    "macd_sig": safe_list(df["MACD_sig"]), "macd_hist": safe_list(df["MACD_hist"]),
                    "atr": safe_list(df["ATR"]), "obv": safe_list(df["OBV"]),
                },
            })
        except ValueError as e:
            return jsonify({"error": str(e)}), 404
        except Exception as e:
            last_err = e
            if attempt == 0:
                time.sleep(0.5)

    return jsonify({"error": f"Analysis failed: {last_err}"}), 500


# ── Screener API ──────────────────────────────────────────────────────────────

@app.route("/api/screener")
def screener():
    exchange = request.args.get("exchange", "ALL")
    min_score = float(request.args.get("min_score", 0))
    phase_filter = request.args.get("phase", "")  # e.g. "Phase 2"

    q = ScanResult.query
    if exchange != "ALL":
        q = q.filter(ScanResult.exchange == exchange)
    if phase_filter:
        q = q.filter(ScanResult.phase.contains(phase_filter))
    q = q.filter(ScanResult.signal_score >= min_score)
    results = q.order_by(ScanResult.signal_score.desc()).limit(500).all()

    last_scan = None
    latest = ScanResult.query.order_by(ScanResult.last_scanned.desc()).first()
    if latest:
        last_scan = latest.last_scanned.strftime("%Y-%m-%d %H:%M UTC")

    return jsonify({
        "last_scan": last_scan,
        "count": len(results),
        "results": [{
            "ticker": r.ticker, "resolved": r.resolved, "exchange": r.exchange,
            "company_name": r.company_name, "phase": r.phase,
            "signal_score": r.signal_score, "close": r.close,
            "ma50": r.ma50, "ma200": r.ma200,
            "t1": r.t1, "t2": r.t2, "stop_loss": r.stop_loss, "rr": r.rr,
            "last_scanned": r.last_scanned.strftime("%Y-%m-%d %H:%M") if r.last_scanned else None,
        } for r in results]
    })


# ── Scan trigger ──────────────────────────────────────────────────────────────
# Use a /tmp file so the running state is shared across Gunicorn workers.

import time as _time

_SCAN_LOCK = "/tmp/mkts_scan.lock"
_SCAN_MAX_AGE = 1200  # 20 min — consider stale after this


def _scan_is_running():
    try:
        return (os.path.exists(_SCAN_LOCK) and
                _time.time() - os.path.getmtime(_SCAN_LOCK) < _SCAN_MAX_AGE)
    except OSError:
        return False


@app.route("/api/scan/trigger", methods=["POST"])
def trigger_scan():
    secret = request.headers.get("X-Scan-Secret", "")
    if SCAN_SECRET and secret != SCAN_SECRET:
        abort(403)
    if _scan_is_running():
        return jsonify({"status": "already_running"}), 202

    with open(_SCAN_LOCK, "w") as f:
        f.write(str(_time.time()))

    def _run():
        try:
            run_full_scan(app)
        finally:
            try:
                os.remove(_SCAN_LOCK)
            except OSError:
                pass

    threading.Thread(target=_run, daemon=True).start()
    return jsonify({"status": "started"}), 202


@app.route("/api/scan/status")
def scan_status():
    latest = ScanResult.query.order_by(ScanResult.last_scanned.desc()).first()
    last_scanned = latest.last_scanned.strftime("%Y-%m-%d %H:%M UTC") if latest and latest.last_scanned else None
    return jsonify({
        "status": "running" if _scan_is_running() else "idle",
        "last_scanned": last_scanned,
    })


# ── Positions API ─────────────────────────────────────────────────────────────

@app.route("/api/positions", methods=["GET"])
@login_required
def get_positions():
    try:
        positions = (
            Position.query
            .filter_by(user_id=current_user.id)
            .order_by(Position.opened_at.desc())
            .all()
        )
    except Exception as exc:
        # Table may not exist yet — try to create it and retry once
        try:
            db.create_all()
            positions = (
                Position.query
                .filter_by(user_id=current_user.id)
                .order_by(Position.opened_at.desc())
                .all()
            )
        except Exception as exc2:
            import traceback
            return jsonify({"_error": str(exc2), "_trace": traceback.format_exc()}), 500
    result = []
    for p in positions:
        scan = ScanResult.query.filter_by(ticker=p.ticker).first()
        current_price = scan.close if scan else None
        pnl = None
        pnl_pct = None
        if current_price:
            pnl = round((current_price - p.entry_price) * p.shares, 2)
            pnl_pct = round((current_price / p.entry_price - 1) * 100, 2)
        result.append({
            "id": p.id, "ticker": p.ticker, "exchange": p.exchange,
            "company_name": p.company_name, "entry_price": p.entry_price,
            "shares": p.shares, "stop_loss": p.stop_loss,
            "target1": p.target1, "target2": p.target2, "notes": p.notes,
            "opened_at": p.opened_at.strftime("%Y-%m-%d"),
            "closed_at": p.closed_at.strftime("%Y-%m-%d") if p.closed_at else None,
            "close_price": p.close_price, "realized_pnl": p.realized_pnl,
            "is_open": p.is_open,
            "current_price": current_price, "pnl": pnl, "pnl_pct": pnl_pct,
        })
    return jsonify(result)


@app.route("/api/positions", methods=["POST"])
@login_required
def add_position():
    data = request.get_json()
    p = Position(
        user_id=current_user.id,
        ticker=data["ticker"].upper(),
        exchange=data.get("exchange", ""),
        company_name=data.get("company_name"),
        entry_price=float(data["entry_price"]),
        shares=float(data["shares"]),
        stop_loss=float(data["stop_loss"]) if data.get("stop_loss") else None,
        target1=float(data["target1"]) if data.get("target1") else None,
        target2=float(data["target2"]) if data.get("target2") else None,
        notes=data.get("notes"),
    )
    db.session.add(p)
    db.session.commit()
    return jsonify({"id": p.id}), 201


@app.route("/api/positions/<int:position_id>", methods=["PUT"])
@login_required
def update_position(position_id):
    p = Position.query.filter_by(id=position_id, user_id=current_user.id).first_or_404()
    data = request.get_json()
    if "close_price" in data:
        close_price = float(data["close_price"])
        shares_to_close = float(data.get("shares_to_close") or p.shares)
        shares_to_close = min(shares_to_close, p.shares)

        if shares_to_close < p.shares:
            # Partial close: create a closed record for the sold shares
            closed_leg = Position(
                user_id=p.user_id,
                ticker=p.ticker,
                exchange=p.exchange,
                company_name=p.company_name,
                entry_price=p.entry_price,
                shares=shares_to_close,
                stop_loss=p.stop_loss,
                target1=p.target1,
                target2=p.target2,
                notes=p.notes,
                opened_at=p.opened_at,
                closed_at=datetime.datetime.utcnow(),
                close_price=close_price,
                realized_pnl=round((close_price - p.entry_price) * shares_to_close, 2),
            )
            db.session.add(closed_leg)
            p.shares = round(p.shares - shares_to_close, 8)
        else:
            # Full close
            p.close_price = close_price
            p.closed_at = datetime.datetime.utcnow()
            p.realized_pnl = round((close_price - p.entry_price) * p.shares, 2)
    if "stop_loss" in data:
        p.stop_loss = float(data["stop_loss"]) if data["stop_loss"] else None
    if "target1" in data:
        p.target1 = float(data["target1"]) if data["target1"] else None
    if "target2" in data:
        p.target2 = float(data["target2"]) if data["target2"] else None
    if "notes" in data:
        p.notes = data["notes"]
    db.session.commit()
    return jsonify({"status": "updated"})


@app.route("/api/positions/<int:position_id>", methods=["DELETE"])
@login_required
def delete_position(position_id):
    p = Position.query.filter_by(id=position_id, user_id=current_user.id).first_or_404()
    db.session.delete(p)
    db.session.commit()
    return jsonify({"status": "deleted"})


# ── Profile (telegram chat ID) ────────────────────────────────────────────────

@app.route("/api/profile", methods=["POST"])
@login_required
def update_profile():
    data = request.get_json()
    if "telegram_chat_id" in data:
        current_user.telegram_chat_id = data["telegram_chat_id"]
        db.session.commit()
    return jsonify({"status": "updated"})


if __name__ == "__main__":
    port = int(os.environ.get("PORT", 5000))
    app.run(host="0.0.0.0", port=port)
