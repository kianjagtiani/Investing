"""
Stock universe definition and full-scan logic.

Universe is fetched dynamically at scan time:
  - NSE:  Full equity list from NSE archives CSV (≈ 2,000 tickers)
  - BSE:  Static curated list of BSE-specific stocks (≈ 200 tickers)
  - NYSE: NYSE common stocks via NASDAQ trader FTP
  - NASDAQ: NASDAQ common stocks via NASDAQ trader FTP

Only Stage 2 (Weinstein markup) stocks are written to the database.
Stocks with open positions are always upserted so P&L stays current.

Run `run_full_scan(app)` from a background thread — it is safe to call
while the Flask app is serving requests.
"""

import datetime
import ftplib
import io
import math
import re
from concurrent.futures import ThreadPoolExecutor, as_completed

import pandas as pd
import requests

from analysis import (
    compute_indicators,
    compute_targets_stoploss,
    detect_phase,
    fetch_data,
    find_support_resistance,
    get_company_info,
)

# ── Dynamic universe fetching ─────────────────────────────────────────────────

_VALID_US_SYM = re.compile(r"^[A-Z][A-Z\-]{0,4}$")  # 1-5 chars, letters + hyphens


def _fetch_nse_tickers():
    """Download the complete NSE equity list from NSE archives.

    Returns a list of raw symbols (no .NS suffix) or None on failure.
    """
    try:
        r = requests.get(
            "https://archives.nseindia.com/content/equities/EQUITY_L.csv",
            headers={"User-Agent": "Mozilla/5.0 (Windows NT 10.0; Win64; x64)"},
            timeout=30,
        )
        r.raise_for_status()
        df = pd.read_csv(io.StringIO(r.text))
        for col in [" SERIES", "SERIES"]:
            if col in df.columns:
                df = df[df[col].str.strip() == "EQ"]
                break
        sym_col = df.columns[0]
        symbols = [str(s).strip() for s in df[sym_col].dropna() if str(s).strip()]
        if len(symbols) > 100:
            print(f"[universe] NSE: fetched {len(symbols)} equity symbols")
            return symbols
    except Exception as exc:
        print(f"[universe] NSE dynamic fetch failed ({exc}), using static fallback")
    return None


def _fetch_us_tickers():
    """Download NYSE and NASDAQ common stocks via NASDAQ trader FTP.

    Returns a list of (ticker, exchange) pairs or None on failure.
    """
    results = []
    try:
        ftp = ftplib.FTP("ftp.nasdaqtrader.com", timeout=30)
        ftp.login()

        # nasdaqlisted.txt columns:
        # Symbol | SecurityName | MarketCategory | TestIssue | FinancialStatus | RoundLotSize | ETF | NextShares
        buf = io.BytesIO()
        ftp.retrbinary("RETR symboldirectory/nasdaqlisted.txt", buf.write)
        for line in buf.getvalue().decode("utf-8", errors="ignore").splitlines()[1:]:
            p = line.split("|")
            if len(p) < 8:
                continue
            sym, test, etf = p[0].strip(), p[3].strip(), p[6].strip()
            if _VALID_US_SYM.match(sym) and test != "Y" and etf != "Y":
                results.append((sym, "NASDAQ"))

        # otherlisted.txt columns:
        # ACT Symbol | Security Name | Exchange | CQS Symbol | ETF | Round Lot Size | Test Issue | NASDAQ Symbol
        buf = io.BytesIO()
        ftp.retrbinary("RETR symboldirectory/otherlisted.txt", buf.write)
        for line in buf.getvalue().decode("utf-8", errors="ignore").splitlines()[1:]:
            p = line.split("|")
            if len(p) < 7:
                continue
            sym, exch, etf, test = p[0].strip(), p[2].strip(), p[4].strip(), p[6].strip()
            # Exchange N = NYSE; skip AMEX (A), ARCA (P), BATS (Z), etc.
            if _VALID_US_SYM.match(sym) and exch == "N" and etf != "Y" and test != "Y":
                results.append((sym, "NYSE"))

        ftp.quit()
        if len(results) > 200:
            print(f"[universe] US: fetched {len(results)} symbols via NASDAQ FTP")
            return results
    except Exception as exc:
        print(f"[universe] NASDAQ FTP fetch failed ({exc}), using static fallback")
    return None


# ── Static fallbacks (used when dynamic fetch fails) ─────────────────────────

_NSE_FALLBACK = [
    # Nifty 50
    "ADANIPORTS", "APOLLOHOSP", "ASIANPAINT", "AXISBANK", "BAJAJ-AUTO",
    "BAJAJFINSV", "BAJFINANCE", "BHARTIARTL", "BPCL", "BRITANNIA",
    "CIPLA", "COALINDIA", "DIVISLAB", "DRREDDY", "EICHERMOT",
    "GRASIM", "HCLTECH", "HDFCBANK", "HDFCLIFE", "HEROMOTOCO",
    "HINDALCO", "HINDUNILVR", "ICICIBANK", "INDUSINDBK", "INFY",
    "ITC", "JSWSTEEL", "KOTAKBANK", "LT", "M&M",
    "MARUTI", "NESTLEIND", "NTPC", "ONGC", "POWERGRID",
    "RELIANCE", "SBILIFE", "SBIN", "SUNPHARMA", "TATACONSUM",
    "TATAMOTORS", "TATASTEEL", "TCS", "TECHM", "TITAN",
    "TRENT", "ULTRACEMCO", "UPL", "WIPRO", "ADANIENT",
    # Nifty Next 50
    "ABB", "AMBUJACEM", "AUROPHARMA", "BANDHANBNK", "BERGEPAINT",
    "BIOCON", "BOSCHLTD", "CANBK", "CHOLAFIN", "COLPAL",
    "CONCOR", "DMART", "GNFC", "GODREJCP", "HAVELLS",
    "ICICIGI", "ICICIPRULI", "IDFCFIRSTB", "INDUSTOWER", "INDIGO",
    "LTF", "LTIM", "LTTS", "LUPIN", "MARICO",
    "MFSL", "MOTHERSON", "MUTHOOTFIN", "NAUKRI", "NMDC",
    "OFSS", "PIIND", "PNB", "RECLTD", "SAIL",
    "SHREECEM", "SIEMENS", "SOLARINDS", "SUNTV", "TATAPOWER",
    "TORNTPHARM", "TVSMOTOR", "VBL", "VEDL", "VOLTAS",
    "ZOMATO", "ADANIGREEN", "ADANIPOWER", "ADANIENSOL", "PIDILITIND",
    # Nifty Midcap
    "ABCAPITAL", "ABFRL", "ACC", "AJANTPHARM", "ALKEM",
    "AMARAJABAT", "ANGELONE", "APLAPOLLO", "ASTRAL", "ATUL",
    "AUBANK", "BALKRISIND", "BANKBARODA", "BEL", "BHARATFORG",
    "BHEL", "CANFINHOME", "CASTROLIND", "CEATLTD", "CESC",
    "COFORGE", "CROMPTON", "CUMMINSIND", "DABUR", "DEEPAKNITRI",
    "DIXON", "EIHOTEL", "EMAMILTD", "ESCORTS", "EXIDEIND",
    "FEDERALBNK", "FORTIS", "GAIL", "GLENMARK", "GODREJAGRO",
    "GODFRYPHLP", "GRANULES", "GSFC", "GUJGASLTD", "HAL",
    "HAPPSTMNDS", "HFCL", "HONAUT", "IBREALEST", "IDFC",
    "INDHOTEL", "INDIAMART", "INDIANB", "IOC", "IRCTC",
    "IRFC", "JSWENERGY", "JUBLFOOD", "KAJARIACER", "KALYANKJIL",
    "KPITTECH", "LALPATHLAB", "LAURUSLABS", "LICHSGFIN", "LUPIN",
    "MANAPPURAM", "MAXHEALTH", "MCX", "METROPOLIS", "MPHASIS",
    "MRF", "MRPL", "NATCOPHARM", "NATIONALUM", "NAM-INDIA",
    "NLCINDIA", "OBEROIRLTY", "OIL", "OLECTRA", "PAGEIND",
    "PERSISTENT", "PETRONET", "PFIZER", "PHOENIX", "POLYCAB",
    "PRESTIGE", "PVRINOX", "RAYMOND", "REDINGTON", "RELAXO",
    "ROUTE", "SCHAEFFLER", "SJVN", "SOBHA", "SONACOMS",
    "STARHEALTH", "SUMICHEM", "SUNDARMFIN", "SUVENPHAR", "SYMPHONY",
    "TANLA", "TATACHEM", "TATACOMM", "TATAELXSI", "TEAMLEASE",
    "THERMAX", "TIMKEN", "TORNTPOWER", "TVSMOTOR", "UJJIVANSFB",
    "UNIONBANK", "UTIAMC", "VGUARD", "VINATIORGA", "WELCORP",
    "WESTLIFE", "WOCKPHARMA", "ZEEL", "ZENSARTECH", "ZYDUSLIFE",
    "MCDOWELL-N", "VIP", "RAIN", "LICI", "LODHA",
    "AFFLE", "DELHIVERY", "DEVYANI", "RVNL", "SBICARD",
    "NUVAMA", "MOTILALOFS", "KFINTECH", "SUZLON", "CDSL",
    "RBLBANK", "KARURVYSYA", "FEDERALBNK", "DCBBANK", "BANKINDIA",
]

_BSE_TICKERS = [
    "MUKANDLTD", "DYNAMATECH", "GREAVESCOT", "ASHAPURMIN", "RPSGVENT",
    "ANDHRSUGAR", "SESHAPAPER", "WALCHANNAG", "ACCELYA", "AUTOMAXIND",
    "BASF", "BEDMUTHA", "BLKASHYAP", "BORORENEW", "CALCINDIA",
    "CEINSYS", "CENTRUM", "CONTROLPR", "COREEL", "COSMOFIRST",
    "CREATIVEYE", "CUPID", "DAMODARIND", "DBREALTY", "DEEPIND",
    "DONEAR", "EASEMYTRIP", "ELECTCAST", "EMKAY", "EPIGRAL",
    "EQUITAS", "ESSFIL", "ETHOSLTD", "EXPLEO", "FAZE3",
    "FILATFASH", "FLORACORP", "GAEL", "GANESHHOUC", "GARFIBRES",
    "GATEWAY", "GBPL", "GESHIP", "GLOBALVECT", "GMMPFAUDLR",
    "GREENLAM", "GREENPLY", "GULFOILLUB", "HIRECT", "HITECH",
    "IGPL", "IMFA", "INDLMETER", "INDSWFTLAB", "INFIBEAM",
    "JAGSNPHARM", "JISLJALEQS", "JOINDRE", "JPPOWER", "KAKATCEM",
    "KAMDHENU", "KENNAMET", "KOTHARI", "KRISHANA", "LEEL",
    "LLOYDSENGG", "MAITHANALL", "MANAKCOAT", "MANGCHEFER", "MANGLMCEM",
    "MANINFRA", "MAPMYINDIA", "MAXESTATES", "MAWANASUG", "MBECL",
    "MERCK", "MIRCELECTR", "MMTC", "MOLDTKPAC", "MOSCHIP",
    "MSTCLTD", "MUKTAARTS", "NAGAFERT", "NAHARSPING", "NDTV",
    "NEOGEN", "NIAK", "NIITLTD", "NILKAMAL", "NITCO",
    "OMAXE", "ONMOBILE", "ORISSAMINE", "PAPERPROD", "PARACABLES",
    "PATEL", "PHILIPCARB", "PLADAINDUS", "PODDARMENT", "PONNI",
    "PRIMESECU", "QUICKHEAL", "RADHIKAJWE", "RAJSREESUG", "RAMCOIND",
    "RCDL", "REMAGEN", "RMCL", "RPPOWER", "RSWM",
    "RTNINDIA", "RUBYMILLS", "SADBHIN", "SAKSOFT", "SAMBHAAV",
    "SANGHIIND", "SANWARIA", "SATIN", "SAYAJI", "SEAHORSE",
    "SETCO", "SHAHALLOYS", "SHAKTIPUMP", "SHEMAROO", "SHREYAS",
    "SIGACHI", "SILLYMONKS", "SIMRAN", "SMLISUZU", "SORIL",
    "SOUTHBANK", "SPTL", "SRTRANSFIN", "STCINDIA", "STERTOOLS",
    "SUMIT", "SURAJEST", "SURYAROSNI", "SWANENERGY", "SWASTIKA",
    "SWELECTES", "TATACOFFEE", "THANGAMAYL", "TIDEWATER",
    "TORNTPOWER", "TOUCHWOOD", "TRANSINDIA", "TREEHOUSE",
    "TTKHLTCARE", "TULSI", "TVSELECT", "UGARSUGAR", "UJJIVAN",
    "UMANGDAIRY", "UNIENTER", "UNITEDTEA", "VAKRANGEE",
    "VESUVIUS", "VIVIMEDLAB", "VOLTAMP", "WABCOINDIA",
    "WEBELSOLAR", "WELENT", "WELSPUNIND", "WINDLAS",
    "XCHANGING", "YASHO", "ZODIAC",
]

_US_FALLBACK = [
    "AAPL", "MSFT", "AMZN", "NVDA", "GOOGL", "META", "TSLA", "BRK-B", "JPM", "JNJ",
    "V", "PG", "UNH", "HD", "MA", "DIS", "BAC", "XOM", "COST", "PFE",
    "ABBV", "TMO", "AVGO", "MRK", "PEP", "KO", "WMT", "CSCO", "ACN", "DHR",
    "VZ", "INTC", "CRM", "ADBE", "NEE", "TXN", "PM", "BMY", "AMGN", "RTX",
    "QCOM", "UPS", "HON", "SBUX", "GS", "MS", "BLK", "SPGI", "ISRG", "MDT",
    "MMM", "ABT", "AXP", "CAT", "CVX", "DE", "GE", "IBM", "LIN", "LMT",
    "LOW", "MCD", "MO", "NKE", "ORCL", "PYPL", "SCHW", "SHW", "TGT", "TJX",
    "T", "USB", "WFC", "WM", "ZTS", "AON", "ADP", "AMAT", "AMD", "BKNG",
    "C", "CI", "CME", "COP", "DUK", "ECL", "EMR", "ETN", "EW", "F",
    "FCX", "FDX", "GILD", "HCA", "HLT", "HUM", "ICE", "IDXX", "IQV", "ITW",
    "KMB", "LRCX", "MAR", "MCK", "MCO", "MNST", "MPC", "MSCI", "MU", "NSC",
    "NTRS", "NUE", "OKE", "ORLY", "OXY", "PANW", "PCAR", "PCG", "PLD", "PPG",
    "PRU", "PSA", "PSX", "REGN", "RF", "RMD", "ROK", "ROST", "ROP", "SLB",
    "SO", "SPG", "STT", "SYK", "SYY", "TDG", "TEL", "TFC", "TMO", "TROW",
    "TRV", "TSN", "URI", "VLO", "VRSK", "VRTX", "WAB", "WAT", "WEC", "WELL",
    "WYNN", "XEL", "XOM", "XYL", "YUM", "ZBH", "ZBRA", "CRWD", "DDOG", "FTNT",
    "NOW", "SNOW", "TEAM", "WDAY", "ZS", "NFLX", "LULU", "MELI", "SHOP", "TTD",
    "MRVL", "SMCI", "ARM", "PLTR", "ASML", "TSM", "BABA", "JD", "NVO", "SAP",
]


# ── Universe builder ──────────────────────────────────────────────────────────

def build_universe():
    """Return a deduplicated list of (yfinance_ticker, exchange) tuples."""
    seen = set()
    universe = []

    nse_syms = _fetch_nse_tickers() or _NSE_FALLBACK
    for sym in nse_syms:
        ticker = f"{sym}.NS"
        if ticker not in seen:
            seen.add(ticker)
            universe.append((ticker, "NSE"))

    for sym in _BSE_TICKERS:
        ticker = f"{sym}.BO"
        if ticker not in seen:
            seen.add(ticker)
            universe.append((ticker, "BSE"))

    us_pairs = _fetch_us_tickers() or [(t, "NYSE/NASDAQ") for t in _US_FALLBACK]
    for ticker, exchange in us_pairs:
        if ticker not in seen:
            seen.add(ticker)
            universe.append((ticker, exchange))

    return universe


# ── Signal scoring (Stage 2 only) ────────────────────────────────────────────

def compute_signal_score(phase: str, df: pd.DataFrame, levels: dict, info: dict) -> float:
    """Score a Stage 2 stock 0–100 by setup quality.

    Only Stage 2 stocks are stored, so this ranks them among themselves.

    Points breakdown:
      BB squeeze breakout (recent consolidation → expansion)  0-25
      RSI quality (55-70 ideal, penalise overbought)          0-15
      MACD histogram accelerating bullish                     0-15
      Volume expansion vs 50-day avg                          0-20
      Risk/reward to T1                                       0-15
      Price near 52-week high (leadership)                    0-10
      ──────────────────────────────────────────────────────  0-100
    """
    if "Phase 2" not in phase:
        return 0.0

    score = 0.0

    # ── BB squeeze breakout ───────────────────────────────────────────────────
    if "BB_squeeze" in df.columns and len(df) >= 25:
        sq = df["BB_squeeze"]
        was_squeezing = bool(sq.iloc[-25:-5].any())
        now_open = not bool(sq.iloc[-5:].all())
        if was_squeezing and now_open:
            score += 25.0  # Fresh breakout from base
        elif bool(sq.iloc[-10:].any()):
            score += 12.0  # Currently building a base (potential breakout)

    # ── RSI quality ──────────────────────────────────────────────────────────
    if "RSI" in df.columns:
        try:
            rsi = float(df["RSI"].iloc[-1])
            if 55.0 <= rsi <= 70.0:
                score += 15.0
            elif 50.0 <= rsi < 55.0 or 70.0 < rsi <= 75.0:
                score += 8.0
            elif rsi > 80.0:
                score -= 5.0  # Overbought — higher risk of reversal
        except (TypeError, ValueError):
            pass

    # ── MACD momentum ────────────────────────────────────────────────────────
    if "MACD_hist" in df.columns and len(df) >= 4:
        try:
            h1 = float(df["MACD_hist"].iloc[-1])
            h2 = float(df["MACD_hist"].iloc[-2])
            h3 = float(df["MACD_hist"].iloc[-3])
            if h1 > h2 > h3 > 0:
                score += 15.0  # Accelerating bullish momentum
            elif h1 > 0 and h1 > h2:
                score += 8.0   # Bullish and strengthening
            elif h1 > 0:
                score += 4.0   # Bullish but not accelerating
        except (TypeError, ValueError):
            pass

    # ── Volume expansion ─────────────────────────────────────────────────────
    if "Volume" in df.columns and len(df) >= 50:
        try:
            avg50 = float(df["Volume"].iloc[-50:].mean())
            avg5 = float(df["Volume"].iloc[-5:].mean())
            if avg50 > 0:
                ratio = avg5 / avg50
                if ratio >= 2.0:
                    score += 20.0  # Institutional accumulation
                elif ratio >= 1.5:
                    score += 12.0
                elif ratio >= 1.2:
                    score += 6.0
        except (TypeError, ValueError):
            pass

    # ── Risk / reward ────────────────────────────────────────────────────────
    try:
        rr = float(levels.get("rr_t1") or 0)
        if rr >= 3.0:
            score += 15.0
        elif rr >= 2.0:
            score += 8.0
        elif rr >= 1.5:
            score += 4.0
    except (TypeError, ValueError):
        pass

    # ── Price near 52-week high ───────────────────────────────────────────────
    high52 = info.get("fiftyTwoWeekHigh")
    if high52:
        try:
            close = float(df["Close"].iloc[-1])
            pct_from_high = close / float(high52) - 1
            if pct_from_high >= -0.05:
                score += 10.0  # Within 5% of 52-week high
            elif pct_from_high >= -0.15:
                score += 5.0   # Within 15% of 52-week high
        except (TypeError, ValueError):
            pass

    return max(0.0, min(100.0, score))


def signal_label(score: float) -> str:
    """Human-readable signal label for a given score."""
    if score >= 75:
        return "STRONG BUY"
    if score >= 55:
        return "BUY"
    if score >= 35:
        return "WATCH"
    return "WEAK SETUP"


# ── Per-ticker scan ───────────────────────────────────────────────────────────

def scan_ticker(ticker: str, exchange: str, app):
    """Fetch data and compute all signals for a single ticker.

    Returns a dict matching the ScanResult model fields, or None on any error.
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
    """Scan every ticker in the universe.

    Only Stage 2 stocks (and any stock with an open position) are written to
    the database.  This keeps the screener focused and the DB lean.
    """
    from models import db, ScanResult, Position

    start = datetime.datetime.utcnow()
    universe = build_universe()
    print(f"[scan] Starting scan of {len(universe)} tickers at {start.isoformat()} UTC")

    results = []
    with ThreadPoolExecutor(max_workers=30) as executor:
        futures = {executor.submit(scan_ticker, t, e, app): t for t, e in universe}
        for future in as_completed(futures):
            ticker = futures[future]
            try:
                result = future.result()
            except Exception as exc:
                print(f"[scan] {ticker}: unexpected error — {exc}")
                result = None
            if result is not None:
                results.append(result)

    stage2 = [r for r in results if "Phase 2" in (r.get("phase") or "")]
    print(f"[scan] Found {len(stage2)} Stage 2 stocks out of {len(results)} scanned")

    with app.application_context():
        # Always upsert tickers with open positions so P&L stays current
        open_tickers = {p.ticker for p in Position.query.filter_by(closed_at=None).all()}

        upsert_count = 0
        for data in results:
            is_stage2 = "Phase 2" in (data.get("phase") or "")
            has_position = data["ticker"] in open_tickers
            if not is_stage2 and not has_position:
                continue

            existing = ScanResult.query.filter_by(ticker=data["ticker"]).first()
            if existing:
                for key, value in data.items():
                    setattr(existing, key, value)
            else:
                db.session.add(ScanResult(**data))
            upsert_count += 1

        db.session.commit()

    check_position_alerts(app)

    elapsed = (datetime.datetime.utcnow() - start).total_seconds()
    print(
        f"[scan] Complete — {upsert_count} records upserted "
        f"({len(stage2)} Stage 2 + position overrides) "
        f"in {elapsed:.1f}s ({elapsed/60:.1f} min)"
    )


# ── Position alerts ───────────────────────────────────────────────────────────

def check_position_alerts(app) -> None:
    """Send Telegram alerts when an open position hits its stop or target."""
    from models import ScanResult, Position
    from alerts import send_alert

    with app.application_context():
        open_positions = Position.query.filter_by(closed_at=None).all()
        for position in open_positions:
            scan_result = ScanResult.query.filter_by(ticker=position.ticker).first()
            if not scan_result or scan_result.close is None:
                continue

            price = scan_result.close
            user = position.user
            chat_id = user.telegram_chat_id if user else None
            if not chat_id:
                continue

            if position.stop_loss is not None and price <= position.stop_loss:
                send_alert(chat_id, f"STOP LOSS HIT: {position.ticker} at {price}")
            elif position.target1 is not None and price >= position.target1:
                send_alert(chat_id, f"T1 HIT: {position.ticker} at {price}")
