"""
backtest_data.py — Historical price cache and point-in-time screener metrics.

Batch-downloads once, then answers as-of queries without look-ahead bias.
"""

from __future__ import annotations

import logging
from datetime import date, datetime, timedelta
from typing import Dict, List, Optional, Tuple

log = logging.getLogger(__name__)

MIN_AVG_VOL = 500_000


def _parse_day(day: str | date) -> date:
    if isinstance(day, date):
        return day
    return datetime.fromisoformat(str(day)[:10]).date()


def _rsi(closes: List[float], period: int = 14) -> Optional[float]:
    if len(closes) < period + 1:
        return None
    deltas = [closes[i] - closes[i - 1] for i in range(1, len(closes))]
    gains = [d for d in deltas[-period:] if d > 0]
    losses = [-d for d in deltas[-period:] if d < 0]
    avg_gain = sum(gains) / period if gains else 0
    avg_loss = sum(losses) / period if losses else 0
    if not avg_loss:
        return 100.0
    return round(100 - (100 / (1 + avg_gain / avg_loss)), 1)


class PriceHistory:
    """Daily OHLCV bars indexed by calendar date (no future leakage)."""

    def __init__(self, bars: Dict[str, Dict[str, dict]]):
        # symbol -> {YYYY-MM-DD -> {open, high, low, close, volume}}
        self.bars = bars
        self._days = sorted(
            {d for sym in bars for d in bars[sym]}
        )

    @classmethod
    def from_yfinance(
        cls,
        symbols: List[str],
        start: str | date,
        end: str | date,
    ) -> "PriceHistory":
        import yfinance as yf

        start_d = _parse_day(start)
        end_d = _parse_day(end)
        # Extra history for 20d momentum / SMA warm-up
        dl_start = (start_d - timedelta(days=120)).isoformat()
        dl_end = (end_d + timedelta(days=1)).isoformat()

        bars: Dict[str, Dict[str, dict]] = {}
        chunk = 40
        for i in range(0, len(symbols), chunk):
            batch = symbols[i : i + chunk]
            try:
                raw = yf.download(
                    batch,
                    start=dl_start,
                    end=dl_end,
                    interval="1d",
                    group_by="ticker",
                    auto_adjust=True,
                    progress=False,
                    threads=True,
                )
            except Exception as e:
                log.warning("[Backtest] download failed for batch: %s", e)
                continue

            multi = len(batch) > 1
            for sym in batch:
                try:
                    if multi:
                        if sym not in raw.columns.get_level_values(0):
                            continue
                        df = raw[sym].dropna(how="all")
                    else:
                        df = raw.dropna(how="all")
                    sym_bars = {}
                    for idx, row in df.iterrows():
                        day = idx.date().isoformat() if hasattr(idx, "date") else str(idx)[:10]
                        sym_bars[day] = {
                            "open": float(row.get("Open", row.get("Close", 0))),
                            "high": float(row.get("High", row.get("Close", 0))),
                            "low": float(row.get("Low", row.get("Close", 0))),
                            "close": float(row["Close"]),
                            "volume": float(row.get("Volume") or 0),
                        }
                    if sym_bars:
                        bars[sym] = sym_bars
                except Exception:
                    continue

        return cls(bars)

    @classmethod
    def from_synthetic(cls, series: Dict[str, List[Tuple[str, float]]]) -> "PriceHistory":
        """Build from {symbol: [(date, close), ...]} for unit tests."""
        bars = {}
        for sym, points in series.items():
            sym_bars = {}
            for day, close in points:
                sym_bars[str(day)[:10]] = {
                    "open": close,
                    "high": close,
                    "low": close,
                    "close": close,
                    "volume": 1_000_000,
                }
            bars[sym] = sym_bars
        return cls(bars)

    def trading_days(self, start: str | date = None, end: str | date = None) -> List[str]:
        days = self._days
        if start:
            s = _parse_day(start).isoformat()
            days = [d for d in days if d >= s]
        if end:
            e = _parse_day(end).isoformat()
            days = [d for d in days if d <= e]
        return days

    def close_on(self, symbol: str, day: str | date) -> Optional[float]:
        d = _parse_day(day).isoformat()
        bar = self.bars.get(symbol, {}).get(d)
        return float(bar["close"]) if bar else None

    def _closes_through(self, symbol: str, day: str | date) -> Tuple[List[str], List[float], List[float]]:
        d = _parse_day(day).isoformat()
        sym = self.bars.get(symbol) or {}
        days = sorted(k for k in sym if k <= d)
        closes = [sym[k]["close"] for k in days]
        volumes = [sym[k].get("volume") or 0 for k in days]
        return days, closes, volumes

    def metrics_as_of(self, symbol: str, day: str | date) -> dict:
        _, closes, volumes = self._closes_through(symbol, day)
        if not closes:
            return {"ticker": symbol, "current_price": None}
        price = closes[-1]
        sma_20 = sum(closes[-20:]) / 20 if len(closes) >= 20 else None
        sma_50 = sum(closes[-50:]) / 50 if len(closes) >= 50 else None
        mom_20 = (closes[-1] - closes[-20]) / closes[-20] if len(closes) >= 20 else None
        avg_vol = sum(volumes[-20:]) / 20 if len(volumes) >= 20 else 0
        vol_spike = min(volumes[-1] / avg_vol, 3.0) / 3.0 if avg_vol > 0 and volumes else 0
        rsi = _rsi(closes)
        rsi_score = max(0.0, 1.0 - abs((rsi or 50) - 52.5) / 47.5) if rsi is not None else 0.5
        composite = (mom_20 or 0) * 0.5 + vol_spike * 0.3 + rsi_score * 0.2
        return {
            "ticker": symbol,
            "symbol": symbol,
            "current_price": round(price, 4),
            "sma_20": round(sma_20, 4) if sma_20 else None,
            "sma_50": round(sma_50, 4) if sma_50 else None,
            "rsi_14": rsi,
            "momentum_20d": round(mom_20, 4) if mom_20 is not None else None,
            "composite_score": round(composite, 4),
            "avg_volume_20d": avg_vol,
        }

    def momentum_universe(
        self,
        candidates: List[str],
        as_of: str | date,
        min_price: float = 5.0,
        max_price: float = 500.0,
        top_n: int = 30,
        min_avg_vol: float = MIN_AVG_VOL,
    ) -> List[Tuple[str, float]]:
        """Point-in-time momentum screen (mirrors screener.momentum_screen logic)."""
        scored = []
        for ticker in candidates:
            m = self.metrics_as_of(ticker, as_of)
            price = m.get("current_price")
            if price is None:
                continue
            if not (min_price <= price <= max_price):
                continue
            avg_vol = m.get("avg_volume_20d") or 0
            if avg_vol < min_avg_vol:
                continue
            _, closes, _ = self._closes_through(ticker, as_of)
            if len(closes) < 25:
                continue
            scored.append((ticker, float(m.get("composite_score") or 0)))
        scored.sort(key=lambda x: x[1], reverse=True)
        return scored[:top_n]


def candidate_pool(mode: str = "growth", limit: int = 60) -> List[str]:
    """Universe seed list aligned with live screener pools (equity backtest only)."""
    from screener import ALL_CANDIDATES, DIVIDEND_UNIVERSE

    if mode == "dividend":
        pool = list(DIVIDEND_UNIVERSE)
    else:
        pool = list(ALL_CANDIDATES)
    return pool[:limit]


def universe_as_of(
    prices: PriceHistory,
    bucket_config: dict,
    as_of: str | date,
    max_universe: int = 20,
) -> Tuple[List[str], dict]:
    """Replay screener universe for a bucket on a historical date (momentum-only)."""
    cfg = bucket_config or {}
    mode = cfg.get("mode", "growth")
    min_price = float(cfg.get("min_price", 5.0))
    max_price = float(cfg.get("max_price", 500.0))

    if mode == "custom":
        pool = cfg.get("tickers") or candidate_pool(mode)
    elif mode == "price_range":
        pool = candidate_pool("growth")
    elif mode == "dividend":
        pool = candidate_pool("dividend")
    else:
        pool = candidate_pool("growth")

    scored = prices.momentum_universe(
        pool, as_of, min_price=min_price, max_price=max_price, top_n=max_universe,
    )
    universe = [t for t, _ in scored]
    sources = {
        "mode": mode,
        "momentum": len(universe),
        "as_of": _parse_day(as_of).isoformat(),
        "backtest": True,
    }
    return universe, sources
