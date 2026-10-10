import os
import sys
import requests
import pandas as pd
import yfinance as yf

# Configuration via GitHub Repository Secrets
SPARK_WEBHOOK_URL = os.environ.get("SPARK_WEBHOOK_URL")
SPARK_WEBHOOK_SECRET = os.environ.get("SPARK_WEBHOOK_SECRET")

# Target watchlist: adjust symbols as needed
WATCHLIST = ["SNAP", "SMH", "PYPL", "NVDA", "MAGS", "FTNT", "AMZN", "ADBE"]

def evaluate_ticker(ticker: str) -> dict | None:
    try:
        # Download 1-year daily history to reliably calculate 200 SMA
        df = yf.download(ticker, period="1y", interval="1d", progress=False)
        if df.empty or len(df) < 200:
            return None

        # Clean multi-index columns if returned by yfinance
        if isinstance(df.columns, pd.MultiIndex):
            df.columns = df.columns.get_level_values(0)

        # Technical calculations
        df["SMA_50"] = df["Close"].rolling(window=50).mean()
        df["SMA_200"] = df["Close"].rolling(window=200).mean()
        df["Vol_Avg20"] = df["Volume"].rolling(window=20).mean()

        latest = df.iloc[-1]
        prev = df.iloc[-2]

        current_close = float(latest["Close"])
        sma_50 = float(latest["SMA_50"])
        sma_200 = float(latest["SMA_200"])
        volume_ratio = float(latest["Volume"] / latest["Vol_Avg20"]) if latest["Vol_Avg20"] > 0 else 1.0

        # Screening logic:
        # Condition A: Fresh cross above 50 SMA with elevated volume (>1.2x 20-day avg)
        bullish_cross = (prev["Close"] <= prev["SMA_50"]) and (current_close > sma_50) and (volume_ratio >= 1.2)
        
        # Condition B: Price within 1.5% of 200 SMA test (trend structure check)
        testing_200_sma = abs(current_close - sma_200) / sma_200 <= 0.015

        if bullish_cross or testing_200_sma:
            event_type = "50_SMA_BREAKOUT_VOLUME" if bullish_cross else "200_SMA_SUPPORT_TEST"
            return {
                "secret_key": SPARK_WEBHOOK_SECRET,
                "event": event_type,
                "ticker": ticker,
                "price": round(current_close, 2),
                "sma_50": round(sma_50, 2),
                "sma_200": round(sma_200, 2),
                "volume_ratio": round(volume_ratio, 2),
                "notes": f"Triggered {event_type} at {round(current_close, 2)} with {round(volume_ratio, 2)}x volume."
            }

    except Exception as e:
        print(f"Error processing {ticker}: {e}", file=sys.stderr)
        return None

def main():
    if not SPARK_WEBHOOK_URL or not SPARK_WEBHOOK_SECRET:
        print("Missing SPARK_WEBHOOK_URL or SPARK_WEBHOOK_SECRET environment variables.", file=sys.stderr)
        sys.exit(1)

    alerts = []
    for ticker in WATCHLIST:
        signal = evaluate_ticker(ticker)
        if signal:
            alerts.append(signal)

    for alert in alerts:
        try:
            res = requests.post(SPARK_WEBHOOK_URL, json=alert, timeout=10)
            print(f"Dispatched alert for {alert['ticker']} | HTTP {res.status_code}")
        except requests.RequestException as e:
            print(f"Failed to post alert for {alert['ticker']}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()