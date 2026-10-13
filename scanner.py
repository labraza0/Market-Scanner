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

# Fallback default core watchlist if Sheet is unreachable
DEFAULT_CORE = ["NVDA", "AMZN", "SMH", "PYPL", "MAGS", "FTNT", "ADBE", "SNAP"]

CACHE_FILE = ".scanner_cache.json"

# Key liquid benchmark tickers from S&P 500 and Nasdaq 100 to scan for dynamic breakouts
TOP_UNIVERSE_DISCOVERY = [
    # Mega-Caps & Tech
    "AAPL", "MSFT", "GOOGL", "META", "TSLA", "AVGO", "AMD", "QCOM", "TXN",
    # Software & Cloud
    "CRM", "NOW", "INTU", "ORCL", "PANW", "CRWD", "PLTR",
    # Financials & Payment
    "JPM", "V", "MA", "GS",
    # Consumer & Industrial
    "COST", "NFLX", "WMT", "CAT", "GE",
    # Top Sector ETFs
    "QQQ", "SPY", "XLK", "XBI", "SOXX"
]

def load_alert_cache() -> set:
    """Loads previously dispatched alerts for the current UTC trading date."""
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
    """Saves dispatched alerts for the current date."""
    today = datetime.datetime.now(datetime.timezone.utc).strftime("%Y-%m-%d")
    try:
        with open(CACHE_FILE, "w") as f:
            json.dump({"date": today, "dispatched": list(dispatched_set)}, f, indent=2)
    except Exception as e:
        print(f"Warning: Could not save alert cache: {e}", file=sys.stderr)

def fetch_watchlist_from_google_sheet() -> list:
    """Fetches ACTIVE tickers from the synchronized Google Sheet CSV export."""
    csv_url = f"https://docs.google.com/spreadsheets/d/{SPREADSHEET_ID}/export?format=csv"
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
                print(f"Synced {len(active_tickers)} active tickers from Google Sheet: {active_tickers}")
                return active_tickers
    except Exception as e:
        print(f"Warning: Could not fetch from Google Sheet ({e}). Using default core watchlist.", file=sys.stderr)
    return DEFAULT_CORE

def get_intraday_volume_factor() -> float:
    """Calculates expected volume progress based on elapsed trading hours (9:30 AM - 4:00 PM EST)."""
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

def get_spy_performance() -> float:
    try:
        spy_df = yf.download("SPY", period="1mo", interval="1d", progress=False)
        if isinstance(spy_df.columns, pd.MultiIndex):
            spy_df.columns = spy_df.columns.get_level_values(0)
        if len(spy_df) >= 20:
            return float((spy_df["Close"].iloc[-1] - spy_df["Close"].iloc[-20]) / spy_df["Close"].iloc[-20] * 100)
    except Exception:
        pass
    return 0.0

def evaluate_ticker(ticker: str, spy_perf_20d: float, is_discovery: bool = False) -> dict | None:
    try:
        df = yf.download(ticker, period="1y", interval="1d", progress=False)
        if df.empty or len(df) < 200:
            return None

        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

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

        rsi_14 = calculate_rsi(df["Close"])
        atr_14 = calculate_atr(df)
        ticker_perf_20d = float((current_close - df["Close"].iloc[-20]) / df["Close"].iloc[-20] * 100)
        spy_relative_strength = round(ticker_perf_20d - spy_perf_20d, 2)

        day_factor = get_intraday_volume_factor()
        expected_vol = vol_avg20 * day_factor
        pace_ratio = (current_volume / expected_vol) if expected_vol > 0 else 1.0

        pct_from_200 = (current_close - sma_200) / sma_200

        # For automated discovery universe, require higher conviction (pace >= 1.5x)
        min_volume_pace = 1.5 if is_discovery else 1.2

        bullish_50_cross = (
            (prev_close <= float(prev["SMA_50"])) and
            (current_close > sma_50) and
            (pace_ratio >= min_volume_pace) and
            (current_close > sma_200) and
            (rsi_14 < 72.0)
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
        tag = "[Discovery Universe] " if is_discovery else ""

        if bullish_50_cross:
            event_type = "50_SMA_BREAKOUT_VOLUME"
            notes = (
                f"{tag}Bullish 50 SMA breakout at ${round(current_close, 2)}. "
                f"Pacing at {round(pace_ratio, 2)}x volume. "
                f"RSI {round(rsi_14, 1)}, Rel Strength vs SPY +{spy_relative_strength}%."
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
                "volume_ratio": round(pace_ratio, 2),
                "notes": notes
            }

        return None

    except Exception as e:
        print(f"Error processing {ticker}: {e}", file=sys.stderr)
        return None

def main():
    if not SPARK_WEBHOOK_URL or not SPARK_WEBHOOK_SECRET:
        print("Missing SPARK_WEBHOOK_URL or SPARK_WEBHOOK_SECRET environment variables.", file=sys.stderr)
        sys.exit(1)

    dispatched_cache = load_alert_cache()
    spy_perf_20d = get_spy_performance()

    # 1. Fetch core watchlist synchronized from Google Sheet (updated via Telegram / Sheets)
    core_watchlist = fetch_watchlist_from_google_sheet()
    
    # 2. Combine with S&P 500 / Nasdaq 100 discovery universe (deduplicated)
    all_tickers = []
    for sym in core_watchlist:
        if sym not in all_tickers:
            all_tickers.append((sym, False))
    for sym in TOP_UNIVERSE_DISCOVERY:
        if sym not in [t[0] for t in all_tickers]:
            all_tickers.append((sym, True))

    print(f"Scanning total of {len(all_tickers)} tickers ({len(core_watchlist)} core + {len(TOP_UNIVERSE_DISCOVERY)} discovery)...")

    alerts_to_send = []
    for ticker, is_discovery in all_tickers:
        signal = evaluate_ticker(ticker, spy_perf_20d, is_discovery=is_discovery)
        if signal:
            alert_key = f"{ticker}_{signal['event']}"
            if alert_key in dispatched_cache:
                print(f"Skipping duplicate alert for {alert_key} (already dispatched today).")
                continue
            alerts_to_send.append((alert_key, signal))

    if not alerts_to_send:
        print("Scan complete: No new alerts to dispatch.")
        return

    for alert_key, alert in alerts_to_send:
        try:
            res = requests.post(SPARK_WEBHOOK_URL, json=alert, timeout=10)
            if res.status_code == 200:
                dispatched_cache.add(alert_key)
                print(f"Dispatched alert for {alert['ticker']} ({alert['event']}) | HTTP 200")
            else:
                print(f"Failed to post alert for {alert['ticker']}: HTTP {res.status_code}", file=sys.stderr)
        except requests.RequestException as e:
            print(f"Failed to post alert for {alert['ticker']}: {e}", file=sys.stderr)

    save_alert_cache(dispatched_cache)

if __name__ == "__main__":
    main()
