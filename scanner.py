import os
import sys
import datetime
import requests
import pandas as pd
import yfinance as yf

# Configuration via GitHub Repository Secrets
SPARK_WEBHOOK_URL = os.environ.get("SPARK_WEBHOOK_URL")
SPARK_WEBHOOK_SECRET = os.environ.get("SPARK_WEBHOOK_SECRET")

# Target watchlist: adjust symbols as needed
WATCHLIST = ["SNAP", "SMH", "PYPL", "NVDA", "MAGS", "FTNT", "AMZN", "ADBE"]

def get_intraday_volume_factor() -> float:
    """
    Calculates expected volume progress based on elapsed regular trading hours (9:30 AM - 4:00 PM EST).
    Prevents false negatives early in the trading session.
    """
    try:
        now_utc = datetime.datetime.now(datetime.timezone.utc)
        # Convert UTC to US/Eastern roughly (-4 hours for EDT, -5 hours for EST)
        # Using 13:30 UTC (9:30 AM EDT) to 20:00 UTC (4:00 PM EDT)
        market_open_minutes = 13 * 60 + 30
        market_close_minutes = 20 * 60
        current_minutes = now_utc.hour * 60 + now_utc.minute

        if current_minutes <= market_open_minutes:
            return 0.10  # Minimum floor at open
        elif current_minutes >= market_close_minutes:
            return 1.0   # Full day elapsed

        elapsed = (current_minutes - market_open_minutes) / (market_close_minutes - market_open_minutes)
        return max(0.10, min(1.0, elapsed))
    except Exception:
        return 1.0

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
        vol_avg20 = float(latest["Vol_Avg20"])
        current_volume = float(latest["Volume"])

        # Pro-rated volume calculation based on time of day
        day_factor = get_intraday_volume_factor()
        expected_intraday_volume = vol_avg20 * day_factor
        pace_ratio = (current_volume / expected_intraday_volume) if expected_intraday_volume > 0 else 1.0

        # Technical structure checks
        prev_close = float(prev["Close"])
        pct_from_200 = (current_close - sma_200) / sma_200

        # Condition 1: Fresh 50 SMA breakout with elevated volume pace (>= 1.2x expected pace)
        # Also requires price to be holding above 200 SMA for broader trend confirmation
        bullish_50_cross = (
            (prev_close <= float(prev["SMA_50"])) and
            (current_close > sma_50) and
            (pace_ratio >= 1.2) and
            (current_close > sma_200)
        )

        # Condition 2: 200 SMA Support Bounce (testing within +1.5% from above and holding)
        support_bounce = (
            (0.0 <= pct_from_200 <= 0.015) and
            (current_close >= prev_close)
        )

        # Condition 3: 200 SMA Breakdown Warning (breaking below within -1.5% from above)
        breakdown_warning = (
            (-0.015 <= pct_from_200 < 0.0) and
            (prev_close >= sma_200)
        )

        event_type = None
        notes = None

        if bullish_50_cross:
            event_type = "50_SMA_BREAKOUT_VOLUME"
            notes = (
                f"Bullish 50 SMA breakout at ${round(current_close, 2)}. "
                f"Pacing at {round(pace_ratio, 2)}x expected volume (day factor {round(day_factor, 2)}). "
                f"Macro trend intact (> 200 SMA)."
            )
        elif support_bounce:
            event_type = "200_SMA_SUPPORT_BOUNCE"
            notes = (
                f"Testing and holding 200 SMA support at ${round(current_close, 2)} "
                f"(+{round(pct_from_200 * 100, 2)}% above 200 SMA). Up from previous close."
            )
        elif breakdown_warning:
            event_type = "200_SMA_BREAKDOWN_WARNING"
            notes = (
                f"Risk alert: Slipping below 200 SMA to ${round(current_close, 2)} "
                f"({round(pct_from_200 * 100, 2)}% below 200 SMA). Prior close was above."
            )

        if event_type:
            return {
                "secret_key": SPARK_WEBHOOK_SECRET,
                "event": event_type,
                "ticker": ticker,
                "price": round(current_close, 2),
                "sma_50": round(sma_50, 2),
                "sma_200": round(sma_200, 2),
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

    alerts = []
    for ticker in WATCHLIST:
        signal = evaluate_ticker(ticker)
        if signal:
            alerts.append(signal)

    if not alerts:
        print("Scan complete: No trigger conditions met.")
        return

    for alert in alerts:
        try:
            res = requests.post(SPARK_WEBHOOK_URL, json=alert, timeout=10)
            print(f"Dispatched alert for {alert['ticker']} ({alert['event']}) | HTTP {res.status_code}")
        except requests.RequestException as e:
            print(f"Failed to post alert for {alert['ticker']}: {e}", file=sys.stderr)

if __name__ == "__main__":
    main()
