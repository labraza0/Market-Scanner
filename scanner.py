import os
import sys
import csv
import io
import json
import datetime
import urllib.request
import requests
import pandas as pd
import yfinance as yf

# Configuration via GitHub Repository Secrets
SPARK_WEBHOOK_URL = os.environ.get("SPARK_WEBHOOK_URL")
SPARK_WEBHOOK_SECRET = os.environ.get("SPARK_WEBHOOK_SECRET")
SPREADSHEET_ID = os.environ.get("SPREADSHEET_ID", "104Ppc5PLFXMvbn6irukd5KJVY53An25sNvX8V1KbdXA")

DEFAULT_CORE = ["NVDA", "AMZN", "SMH", "PYPL", "MAGS", "FTNT", "ADBE", "SNAP", "OKLO"]
CACHE_FILE = ".scanner_cache.json"

TOP_UNIVERSE_DISCOVERY = [
    # Mega-Caps & Tech
    "AAPL", "MSFT", "GOOGL", "META", "TSLA", "AVGO", "AMD", "QCOM", "TXN",
    # Software & Cloud
    "CRM", "NOW", "INTU", "ORCL", "PANW", "CRWD", "PLTR",
    # Financials & Payment
    "JPM", "V", "MA", "GS",
    # Consumer & Industrial
    "COST", "NFLX", "WMT", "CAT", "GE",
    # Sector ETFs
    "QQQ", "SPY", "XLK", "XBI", "SOXX"
]

SECTOR_MAP = {
    "NVDA": "SMH", "AMD": "SMH", "AVGO": "SMH", "QCOM": "SMH", "TXN": "SMH",
    "AAPL": "XLK", "MSFT": "XLK", "GOOGL": "XLK", "META": "XLK", "CRM": "XLK",
    "NOW": "XLK", "INTU": "XLK", "ORCL": "XLK", "PANW": "XLK", "CRWD": "XLK",
    "PLTR": "XLK", "ADBE": "XLK", "FTNT": "XLK",
    "JPM": "XLF", "V": "XLF", "MA": "XLF", "GS": "XLF", "PYPL": "XLF",
    "COST": "XLP", "WMT": "XLP",
    "CAT": "XLI", "GE": "XLI",
    "TSLA": "XLY", "AMZN": "XLY"
}

def load_alert_cache() -> set:
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    if os.path.exists(CACHE_FILE):
        try:
            with open(CACHE_FILE, "r") as f:
                data = json.load(f)
                if data.get("date") == today:
                    return set(data.get("dispatched", []))
        except Exception:
            pass
    return set()

def save_alert_cache(dispatched_set: set):
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"date": today, "dispatched": list(dispatched_set)}, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save alert cache: {e}", file=sys.stderr)

def fetch_watchlist_from_google_sheet() -> list:
    csv_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv&gid=0"
    try:
        req = urllib.request.Request(csv_url, headers={"User-Agent": "MarketScanner"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            reader = csv.DictReader(io.StringIO(content))
            active_tickers = [
                row["Ticker"].strip().upper()
                for row in reader
                if row.get("Ticker") and row.get("Status", "").strip().upper() == "ACTIVE"
            ]
            if active_tickers:
                return active_tickers
    except Exception as e:
        print(f"Warning: Could not fetch from Google Sheet ({e}). Using default core.", file=sys.stderr)
    return DEFAULT_CORE

def fetch_open_trades_from_journal() -> list:
    """Fetches open trades from 'Trades Journal' tab to track take-profit and stop-loss levels."""
    csv_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/gviz/tq?tqx=out:csv&sheet=Trades%20Journal"
    trades = []
    try:
        req = urllib.request.Request(csv_url, headers={"User-Agent": "MarketScanner"})
        with urllib.request.urlopen(req, timeout=10) as resp:
            content = resp.read().decode("utf-8")
            reader = csv.DictReader(io.StringIO(content))
            for row in reader:
                status = row.get("Status", "").strip().upper()
                if status in ["EXECUTED", "ACTIVE"] and row.get("Ticker"):
                    try:
                        p_str = row.get("Price", "0").replace("$", "").replace(",", "").strip()
                        trades.append({
                            "date": row.get("Date"),
                            "ticker": row.get("Ticker").strip().upper(),
                            "event": row.get("Event"),
                            "entry_price": float(p_str),
                            "row": row
                        })
                    except Exception:
                        pass
    except Exception as e:
        print(f"Warning: Could not fetch open trades from journal ({e}).", file=sys.stderr)
    return trades

def check_earnings_blackout(ticker: str) -> dict:
    """Checks if an earnings report is scheduled within 5 days (Blackout window)."""
    try:
        t = yf.Ticker(ticker)
        calendar = t.calendar
        earnings_date = None

        if isinstance(calendar, pd.DataFrame) and not calendar.empty:
            if "Earnings Date" in calendar.index:
                val = calendar.loc["Earnings Date"].iloc[0]
                if isinstance(val, (datetime.date, datetime.datetime, pd.Timestamp)):
                    earnings_date = pd.to_datetime(val).tz_localize(None)
        elif isinstance(calendar, dict) and "Earnings Date" in calendar:
            vals = calendar["Earnings Date"]
            if vals and len(vals) > 0:
                earnings_date = pd.to_datetime(vals[0]).tz_localize(None)

        if earnings_date is not None:
            now = pd.to_datetime(datetime.datetime.now(datetime.timezone.utc).date())
            days_until = (earnings_date - now).days
            if 0 <= days_until <= 5:
                return {"blackout": True, "days_until": days_until}
            return {"blackout": False, "days_until": days_until}
    except Exception:
        pass
    return {"blackout": False, "days_until": None}

def check_weekly_trend(ticker: str) -> bool:
    """Verifies that the ticker is trading above its Weekly 20 EMA."""
    try:
        w_df = yf.download(ticker, period="1y", interval="1wk", progress=False)
        if isinstance(w_df.columns, pd.MultiIndex): w_df.columns = w_df.columns.get_level_values(0)
        if len(w_df) >= 20:
            w_df["EMA_20"] = w_df["Close"].ewm(span=20, adjust=False).mean()
            return float(w_df["Close"].iloc[-1]) >= float(w_df["EMA_20"].iloc[-1])
    except Exception:
        pass
    return True

def check_ttm_squeeze(df: pd.DataFrame) -> bool:
    """Calculates whether Bollinger Bands are inside Keltner Channels (TTMSqueeze compression)."""
    try:
        if len(df) < 20: return False
        sma_20 = df["Close"].rolling(20).mean()
        std_20 = df["Close"].rolling(20).std()
        bb_upper = sma_20 + 2.0 * std_20
        bb_lower = sma_20 - 2.0 * std_20

        high = df["High"]
        low = df["Low"]
        close = df["Close"]
        prev_close = close.shift(1)
        tr = pd.concat([high - low, (high - prev_close).abs(), (low - prev_close).abs()], axis=1).max(axis=1)
        atr_20 = tr.rolling(20).mean()
        kc_upper = sma_20 + 1.5 * atr_20
        kc_lower = sma_20 - 1.5 * atr_20

        squeeze = (bb_lower.iloc[-1] > kc_lower.iloc[-1]) and (bb_upper.iloc[-1] < kc_upper.iloc[-1])
        return bool(squeeze)
    except Exception:
        return False

def get_market_regime() -> dict:
    regime = {
        "status": "NORMAL",
        "spy_price": 0.0,
        "spy_above_50": True,
        "vix_level": 0.0,
        "vix_extreme": False,
        "allow_long_breakouts": True
    }
    try:
        spy_df = yf.download("SPY", period="6mo", interval="1d", progress=False)
        if isinstance(spy_df.columns, pd.MultiIndex): spy_df.columns = spy_df.columns.get_level_values(0)
        if len(spy_df) >= 50:
            spy_df["SMA_50"] = spy_df["Close"].rolling(50).mean()
            current_spy = float(spy_df["Close"].iloc[-1])
            spy_sma50 = float(spy_df["SMA_50"].iloc[-1])
            regime["spy_price"] = round(current_spy, 2)
            regime["spy_above_50"] = current_spy >= spy_sma50

        vix_df = yf.download("^VIX", period="5d", interval="1d", progress=False)
        if isinstance(vix_df.columns, pd.MultiIndex): vix_df.columns = vix_df.columns.get_level_values(0)
        if not vix_df.empty:
            vix_val = float(vix_df["Close"].iloc[-1])
            regime["vix_level"] = round(vix_val, 2)
            regime["vix_extreme"] = vix_val >= 28.0

        if not regime["spy_above_50"] or regime["vix_extreme"]:
            regime["status"] = "RISK_OFF"
            regime["allow_long_breakouts"] = False

        print(f"Market Regime: {regime['status']} | SPY: ${regime['spy_price']} (Above 50: {regime['spy_above_50']}) | VIX: {regime['vix_level']}")
    except Exception as e:
        print(f"Warning checking market regime: {e}", file=sys.stderr)
    return regime

def get_intraday_volume_factor() -> float:
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        market_open_minutes = 13 * 60 + 30
        market_close_minutes = 20 * 60
        current_minutes = now_utc.hour * 60 + now_utc.minute

        if current_minutes <= market_open_minutes:
            return 0.10
        elif current_minutes >= market_close_minutes:
            return 1.0

        elapsed = (current_minutes - market_open_minutes) / (market_close_minutes - market_open_minutes)
        return max(0.10, min(1.0, elapsed))
    except Exception:
        return 1.0

def calculate_rsi(series: pd.Series, period: int = 14) -> float:
    delta = series.diff()
    gain = delta.clip(lower=0)
    loss = -delta.clip(upper=0)
    avg_gain = gain.rolling(window=period).mean()
    avg_loss = loss.rolling(window=period).mean()
    rs = avg_gain / avg_loss.replace(0, 0.0001)
    rsi = 100 - (100 / (1 + rs))
    return float(rsi.iloc[-1])

def calculate_atr(df: pd.DataFrame, period: int = 14) -> float:
    high = df["High"]
    low = df["Low"]
    close = df["Close"]
    prev_close = close.shift(1)
    tr = pd.concat([
        high - low,
        (high - prev_close).abs(),
        (low - prev_close).abs()
    ], axis=1).max(axis=1)
    atr = tr.rolling(window=period).mean()
    return float(atr.iloc[-1])

def get_sector_performance(sector_etf: str) -> float:
    try:
        df = yf.download(sector_etf, period="5d", interval="1d", progress=False)
        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
        if len(df) >= 2:
            return round(float((df["Close"].iloc[-1] - df["Close"].iloc[-2]) / df["Close"].iloc[-2] * 100), 2)
    except Exception:
        pass
    return 0.0

def evaluate_ticker(ticker: str, spy_perf_20d: float, regime: dict, is_discovery: bool = False) -> dict | None:
    try:
        # 1. Earnings Blackout Check
        earnings_info = check_earnings_blackout(ticker)
        if earnings_info["blackout"]:
            print(f"Skipping {ticker}: Earnings announcement in {earnings_info['days_until']} days (Blackout window).")
            return None

        df = yf.download(ticker, period="1y", interval="1d", progress=False)
        if df.empty or len(df) < 200:
            return None

        if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)

        df["SMA_50"] = df["Close"].rolling(window=50).mean()
        df["SMA_200"] = df["Close"].rolling(window=200).mean()
        df["Vol_Avg20"] = df["Volume"].rolling(window=20).mean()

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        current_close = float(latest["Close"])
        prev_close = float(prev["Close"])
        sma_50 = float(latest["SMA_50"])
        sma_200 = float(latest["SMA_200"])
        vol_avg20 = float(latest["Vol_Avg20"])
        current_volume = float(latest["Volume"])

        # 2. Multi-timeframe Weekly Trend check for 50 SMA breakouts
        weekly_bullish = check_weekly_trend(ticker)

        # 3. TTMSqueeze Compression Check
        squeeze_active = check_ttm_squeeze(df)

        rsi_14 = calculate_rsi(df["Close"])
        atr_14 = calculate_atr(df)
        ticker_perf_20d = float((current_close - df["Close"].iloc[-20]) / df["Close"].iloc[-20] * 100)
        spy_relative_strength = round(ticker_perf_20d - spy_perf_20d, 2)

        # 4. Sector Confirmation
        sector_etf = SECTOR_MAP.get(ticker, "SPY")
        sector_perf = get_sector_performance(sector_etf)

        day_factor = get_intraday_volume_factor()
        expected_vol = vol_avg20 * day_factor
        pace_ratio = (current_volume / expected_vol) if expected_vol > 0 else 1.0

        pct_from_200 = (current_close - sma_200) / sma_200
        min_volume_pace = 1.5 if is_discovery else 1.2
        if not regime["allow_long_breakouts"]:
            min_volume_pace = max(min_volume_pace, 1.8)

        bullish_50_cross = (
            (prev_close <= float(prev["SMA_50"])) and
            (current_close > sma_50) and
            (pace_ratio >= min_volume_pace) and
            (current_close > sma_200) and
            (rsi_14 < 72.0) and
            weekly_bullish  # MTF Gate
        )

        support_bounce = (
            (0.0 <= pct_from_200 <= 0.015) and
            (current_close >= prev_close) and
            (rsi_14 <= 55.0)
        )

        breakdown_warning = (
            (-0.015 <= pct_from_200 < 0.0) and
            (prev_close >= sma_200)
        )

        event_type = None
        notes = None
        tag = "[Discovery] " if is_discovery else ""

        if bullish_50_cross:
            event_type = "50_SMA_BREAKOUT_VOLUME"
            squeeze_note = " · TTMSqueeze Active!" if squeeze_active else ""
            notes = (
                f"{tag}Bullish 50 SMA breakout at ${round(current_close, 2)}. "
                f"Pacing at {round(pace_ratio, 2)}x volume. RSI {round(rsi_14, 1)}, Rel Strength vs SPY +{spy_relative_strength}%. "
                f"Weekly Trend: BULLISH (>20w EMA). Sector ({sector_etf}): {sector_perf}%{squeeze_note}."
            )
        elif support_bounce:
            event_type = "200_SMA_SUPPORT_BOUNCE"
            notes = (
                f"{tag}Holding 200 SMA support at ${round(current_close, 2)} "
                f"(+{round(pct_from_200 * 100, 2)}% above 200 SMA). RSI {round(rsi_14, 1)}."
            )
        elif breakdown_warning:
            event_type = "200_SMA_BREAKDOWN_WARNING"
            notes = (
                f"{tag}Risk alert: Pierced 200 SMA support to ${round(current_close, 2)} "
                f"({round(pct_from_200 * 100, 2)}% below 200 SMA)."
            )

        if event_type:
            return {
                "secret_key": SPARK_WEBHOOK_SECRET,
                "event": event_type,
                "ticker": ticker,
                "price": round(current_close, 2),
                "sma_50": round(sma_50, 2),
                "sma_200": round(sma_200, 2),
                "rsi": round(rsi_14, 1),
                "atr": round(atr_14, 2),
                "spy_rel_strength": spy_relative_strength,
                "sector": sector_etf,
                "sector_perf": sector_perf,
                "weekly_bullish": weekly_bullish,
                "volume_ratio": round(pace_ratio, 2),
                "earnings_days": earnings_info.get("days_until"),
                "squeeze_active": squeeze_active,
                "notes": notes,
                "market_regime": regime["status"],
                "vix": regime["vix_level"]
            }

        return None
    except Exception as e:
        print(f"Error processing {ticker}: {e}", file=sys.stderr)
        return None

def monitor_open_trades():
    """Monitors journaled active trades for Take-Profit (+1.5R / +2.5R) or Stop-Loss hits."""
    trades = fetch_open_trades_from_journal()
    if not trades or not SPARK_WEBHOOK_URL or not SPARK_WEBHOOK_SECRET:
        return

    for t in trades:
        sym = t["ticker"]
        entry = t["entry_price"]
        try:
            df = yf.download(sym, period="1mo", interval="1d", progress=False)
            if isinstance(df.columns, pd.MultiIndex): df.columns = df.columns.get_level_values(0)
            if df.empty or len(df) < 14: continue
            cur_price = round(float(df["Close"].iloc[-1]), 2)
            pct_gain = round((cur_price - entry) / entry * 100, 2)
            atr_val = calculate_atr(df, period=14)
            stop_dist = max(1.0, atr_val * 1.5)

            tp1 = round(entry + stop_dist * 1.5, 2)
            tp2 = round(entry + stop_dist * 2.5, 2)
            sl = round(entry - stop_dist, 2)

            if cur_price >= tp1:
                target_str = "TP2 (+2.5R)" if cur_price >= tp2 else "TP1 (+1.5R)"
                payload = {
                    "secret_key": SPARK_WEBHOOK_SECRET,
                    "event": "TRADE_MANAGEMENT_UPDATE",
                    "ticker": sym,
                    "price": cur_price,
                    "notes": f"🎯 Take Profit Alert ({target_str}): {sym} reached ${cur_price} (+{pct_gain}% from entry ${entry}). Suggest trailing stop to breakeven (${entry}) or taking partial profits."
                }
                requests.post(SPARK_WEBHOOK_URL, json=payload, timeout=10)
            elif cur_price <= sl:
                payload = {
                    "secret_key": SPARK_WEBHOOK_SECRET,
                    "event": "TRADE_MANAGEMENT_UPDATE",
                    "ticker": sym,
                    "price": cur_price,
                    "notes": f"⚠️ Stop-Loss Risk Hit: {sym} has declined to ${cur_price} ({pct_gain}% from entry ${entry}), reaching the calculated stop level (${sl}). Protect capital."
                }
                requests.post(SPARK_WEBHOOK_URL, json=payload, timeout=10)
        except Exception as e:
            print(f"Error tracking open trade {sym}: {e}", file=sys.stderr)

def main():
    if len(sys.argv) > 1 and sys.argv[1] == "--digest":
        run_eod_digest()
        return

    if not SPARK_WEBHOOK_URL or not SPARK_WEBHOOK_SECRET:
        print("Missing SPARK_WEBHOOK_URL or SPARK_WEBHOOK_SECRET.", file=sys.stderr)
        sys.exit(1)

    regime = get_market_regime()
    dispatched_cache = load_alert_cache()

    # Track open trades first
    monitor_open_trades()

    try:
        spy_df = yf.download("SPY", period="1mo", interval="1d", progress=False)
        if isinstance(spy_df.columns, pd.MultiIndex): spy_df.columns = spy_df.columns.get_level_values(0)
        spy_perf_20d = float((spy_df["Close"].iloc[-1] - spy_df["Close"].iloc[-20]) / spy_df["Close"].iloc[-20] * 100)
    except Exception:
        spy_perf_20d = 0.0

    core_watchlist = fetch_watchlist_from_google_sheet()
    all_tickers = []
    for sym in core_watchlist:
        if sym not in all_tickers: all_tickers.append((sym, False))
    for sym in TOP_UNIVERSE_DISCOVERY:
        if sym not in [t[0] for t in all_tickers]: all_tickers.append((sym, True))

    print(f"Evaluating {len(all_tickers)} tickers with Multi-Timeframe, TTMSqueeze, Sector Confluence, and Earnings Blackout...")
    alerts_to_send = []
    for ticker, is_discovery in all_tickers:
        signal = evaluate_ticker(ticker, spy_perf_20d, regime, is_discovery=is_discovery)
        if signal:
            alert_key = f"{ticker}_{signal['event']}"
            if alert_key in dispatched_cache:
                print(f"Skipping duplicate alert for {alert_key} today.")
                continue
            alerts_to_send.append((alert_key, signal))

    if not alerts_to_send:
        print("Scan complete: No new alerts to dispatch.")
        return

    for alert_key, alert in alerts_to_send:
        try:
            res = requests.post(SPARK_WEBHOOK_URL, json=alert, timeout=15)
            if res.status_code == 200:
                dispatched_cache.add(alert_key)
                print(f"Dispatched alert for {alert['ticker']} ({alert['event']}) | HTTP 200")
            else:
                print(f"Failed to post alert for {alert['ticker']}: HTTP {res.status_code}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"Failed to post alert for {alert['ticker']}: {e}", file=sys.stderr)

    save_alert_cache(dispatched_cache)

def run_eod_digest():
    if not SPARK_WEBHOOK_URL or not SPARK_WEBHOOK_SECRET:
        return
    core = fetch_watchlist_from_google_sheet()
    regime = get_market_regime()
    digest_payload = {
        "secret_key": SPARK_WEBHOOK_SECRET,
        "event": "EOD_DIGEST",
        "ticker": "MARKET_SUMMARY",
        "price": regime.get("spy_price", 0.0),
        "notes": f"Daily Market Close Summary | SPY: ${regime.get('spy_price')} | VIX: {regime.get('vix_level')} | Watched {len(core)} core tickers."
    }
    try:
        res = requests.post(SPARK_WEBHOOK_URL, json=digest_payload, timeout=20)
        print(f"Dispatched EOD Digest | HTTP {res.status_code}")
    except Exception as e:
        print(f"Failed to post EOD Digest: {e}")

if __name__ == "__main__":
    main()
