"""Market data fetching, audit logging, and LLM prompt compaction."""

import json
import logging
import os
from datetime import datetime

import yfinance as yf

from buckets import Bucket

log = logging.getLogger(__name__)

AUDIT_DIR = os.path.join(os.path.dirname(os.path.abspath(__file__)), "audit")


def compact_stock_data_rows(rows: list, limit: int = 18) -> list:
    """Keep LLM prompts small: only fields useful for ranking, capped rows."""
    out = []
    for r in (rows or [])[:limit]:
        out.append({
            "ticker": r.get("ticker") or r.get("symbol"),
            "price": r.get("current_price"),
            "change_pct": r.get("change_pct") or r.get("percent_change"),
            "volume": r.get("volume"),
            "avg_volume": r.get("avg_volume"),
            "rsi": r.get("rsi"),
            "sma20": r.get("sma20"),
            "sma50": r.get("sma50"),
            "pe": r.get("pe_ratio") or r.get("trailing_pe"),
            "eps": r.get("eps"),
            "yield": r.get("dividend_yield"),
            "ex_dividend": r.get("ex_dividend_date"),
        })
    return out


def audit_log(event, ticker, prompt, raw_response, decision, market_snapshot):
    """Write dated audit record: prompt + response + snapshot per trade decision."""
    try:
        os.makedirs(AUDIT_DIR, exist_ok=True)
        ts = datetime.now().strftime("%Y-%m-%d_%H%M%S")
        fname = os.path.join(AUDIT_DIR, f"{ts}_{ticker}_{event}.log")
        with open(fname, "w", encoding="utf-8") as f:
            f.write(
                f"=== AgentTrade Audit ===\nTime: {datetime.now().isoformat()}\n"
                f"Event: {event}  Ticker: {ticker}\n"
                f"Decision: {json.dumps(decision, indent=2)}\n\n"
                f"=== Market Snapshot ===\n{json.dumps(market_snapshot, indent=2)}\n\n"
                f"=== Prompt ===\n{prompt}\n\n"
                f"=== Raw LLM Response ===\n{str(raw_response)}\n"
            )
    except Exception as e:
        log.warning("[Audit] Could not write for %s: %s", ticker, e)


def keep_audit_clean(max_files: int = 500) -> None:
    try:
        files = sorted(
            [os.path.join(AUDIT_DIR, f) for f in os.listdir(AUDIT_DIR) if f.endswith(".log")],
            key=os.path.getmtime,
        )
        for old in files[:-max_files]:
            os.unlink(old)
    except Exception:
        pass


def _atr(highs, lows, closes, period=14):
    if len(closes) < period + 1:
        return 0.0
    trs = [
        max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1]))
        for i in range(1, min(len(closes), period + 1))
    ]
    return round(sum(trs) / len(trs), 4) if trs else 0.0


def _sma_slope(closes, period=50):
    if len(closes) < period + 5:
        return 0.0
    a = sum(closes[-period:]) / period
    b = sum(closes[-period - 5:-5]) / period
    return round((a - b) / b * 100, 3) if b else 0.0


def fetch_stock_data(ticker: str) -> dict:
    """Lean pre-computed metrics — no raw arrays sent to LLM."""
    try:
        t = yf.Ticker(ticker)
        hist = t.history(period="3mo")
        info = t.info
        if hist.empty:
            return {}

        closes = hist["Close"].tolist()
        volumes = hist["Volume"].tolist()
        price = closes[-1]

        sma_20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None

        rsi = None
        if len(closes) >= 15:
            deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            gains = [d for d in deltas[-14:] if d > 0]
            losses = [-d for d in deltas[-14:] if d < 0]
            avg_gain = sum(gains) / 14 if gains else 0
            avg_loss = sum(losses) / 14 if losses else 0
            rsi = round(100 - (100 / (1 + avg_gain / avg_loss)), 1) if avg_loss else 100.0

        price_change_20d = ((price - closes[-20]) / closes[-20] * 100) if len(closes) >= 20 else None

        highs = hist["High"].tolist()
        lows = hist["Low"].tolist()
        atr14 = _atr(highs, lows, closes, 14)
        atr_pct = round(atr14 / price * 100, 2) if price else None
        sma50_slope = _sma_slope(closes, 50)
        vol_spike = (
            round(volumes[-1] / (sum(volumes[-10:]) / 10), 2)
            if len(volumes) >= 10 and sum(volumes[-10:])
            else None
        )
        pct_5d = round((price - closes[-5]) / closes[-5] * 100, 2) if len(closes) >= 5 else None
        w52_high = info.get("fiftyTwoWeekHigh") or max(closes)
        w52_low = info.get("fiftyTwoWeekLow") or min(closes)
        w52_pos = round((price - w52_low) / (w52_high - w52_low), 2) if w52_high != w52_low else 0.5
        summary_raw = info.get("longBusinessSummary") or ""
        summary = (summary_raw[:120].rsplit(" ", 1)[0] + "…") if len(summary_raw) > 120 else summary_raw

        return {
            "ticker": ticker,
            "current_price": round(price, 2),
            "sma_20": round(sma_20, 2) if sma_20 else None,
            "sma_50": round(sma_50, 2) if sma_50 else None,
            "rsi_14": rsi,
            "price_change_20d": round(price_change_20d, 2) if price_change_20d else None,
            "avg_volume_10d": int(sum(volumes[-10:]) / 10) if len(volumes) >= 10 else None,
            "market_cap": info.get("marketCap"),
            "pe_ratio": info.get("trailingPE"),
            "forward_pe": info.get("forwardPE"),
            "profit_margin": info.get("profitMargins"),
            "revenue_growth": info.get("revenueGrowth"),
            "debt_to_equity": info.get("debtToEquity"),
            "dividend_yield": info.get("dividendYield"),
            "dividend_rate": info.get("dividendRate"),
            "payout_ratio": info.get("payoutRatio"),
            "ex_dividend_date": info.get("exDividendDate"),
            "52w_high": info.get("fiftyTwoWeekHigh"),
            "52w_low": info.get("fiftyTwoWeekLow"),
            "sector": info.get("sector"),
            "atr14": atr14,
            "atr_pct": atr_pct,
            "ohlcv": {
                "highs": [round(x, 4) for x in highs[-30:]],
                "lows": [round(x, 4) for x in lows[-30:]],
                "closes": [round(x, 4) for x in closes[-30:]],
            },
            "sma50_slope": sma50_slope,
            "vol_spike_x": vol_spike,
            "pct_5d": pct_5d,
            "w52_position": w52_pos,
            "summary": summary,
        }
    except Exception as e:
        log.warning("Failed to fetch data for %s: %s", ticker, e)
        return {}


def is_crypto_bucket(bucket: Bucket) -> bool:
    return getattr(bucket, "asset_class", "us_equity") == "crypto" or bucket.mode == "crypto"


def crypto_to_yfinance(symbol: str) -> str:
    return symbol.upper().replace("/", "-")


def fetch_crypto_data(symbol: str) -> dict:
    """Fetch crypto market data via yFinance; trading symbol stays Alpaca-style BTC/USD."""
    try:
        yf_symbol = crypto_to_yfinance(symbol)
        t = yf.Ticker(yf_symbol)
        hist = t.history(period="90d", interval="1d")
        if hist.empty:
            return {"ticker": symbol, "symbol": symbol, "asset_class": "crypto"}

        closes = hist["Close"].dropna().tolist()
        volumes = hist["Volume"].dropna().tolist() if "Volume" in hist else []
        price = closes[-1]
        change_24h = ((closes[-1] - closes[-2]) / closes[-2] * 100) if len(closes) >= 2 and closes[-2] else None
        change_7d = ((closes[-1] - closes[-8]) / closes[-8] * 100) if len(closes) >= 8 and closes[-8] else None
        change_30d = ((closes[-1] - closes[-31]) / closes[-31] * 100) if len(closes) >= 31 and closes[-31] else None
        sma_20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None

        rsi = None
        if len(closes) >= 15:
            deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
            gains = [d for d in deltas[-14:] if d > 0]
            losses = [-d for d in deltas[-14:] if d < 0]
            avg_gain = sum(gains) / 14 if gains else 0
            avg_loss = sum(losses) / 14 if losses else 0
            rsi = round(100 - (100 / (1 + avg_gain / avg_loss)), 1) if avg_loss else 100.0

        highs = hist["High"].dropna().tolist() if "High" in hist else closes
        lows = hist["Low"].dropna().tolist() if "Low" in hist else closes
        atr14 = _atr(highs, lows, closes, 14)
        atr_pct = round(atr14 / price * 100, 2) if price else None

        return {
            "ticker": symbol,
            "symbol": symbol,
            "asset_class": "crypto",
            "current_price": round(price, 4),
            "change_24h_pct": round(change_24h, 2) if change_24h is not None else None,
            "change_7d_pct": round(change_7d, 2) if change_7d is not None else None,
            "change_30d_pct": round(change_30d, 2) if change_30d is not None else None,
            "sma_20": round(sma_20, 4) if sma_20 else None,
            "sma_50": round(sma_50, 4) if sma_50 else None,
            "rsi_14": rsi,
            "avg_volume_14d": round(sum(volumes[-14:]) / 14, 2) if len(volumes) >= 14 else None,
            "atr14": atr14,
            "atr_pct": atr_pct,
            "ohlcv": {
                "highs": [round(x, 6) for x in highs[-30:]],
                "lows": [round(x, 6) for x in lows[-30:]],
                "closes": [round(x, 6) for x in closes[-30:]],
            },
        }
    except Exception as e:
        log.warning("Failed to fetch crypto data for %s: %s", symbol, e)
        return {"ticker": symbol, "symbol": symbol, "asset_class": "crypto"}


def compact_market_data_rows(rows: list, limit: int = 18) -> list:
    if rows and rows[0].get("asset_class") == "crypto":
        return [
            {
                "symbol": r.get("ticker") or r.get("symbol"),
                "price": r.get("current_price"),
                "change_24h_pct": r.get("change_24h_pct"),
                "change_7d_pct": r.get("change_7d_pct"),
                "change_30d_pct": r.get("change_30d_pct"),
                "rsi": r.get("rsi_14"),
                "sma20": r.get("sma_20"),
                "sma50": r.get("sma_50"),
                "avg_volume_14d": r.get("avg_volume_14d"),
            }
            for r in (rows or [])[:limit]
        ]
    return compact_stock_data_rows(rows, limit=limit)
