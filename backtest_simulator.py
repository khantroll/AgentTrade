"""
backtest_simulator.py — Simulated portfolio, fills, stops, and equity curve.
"""

from __future__ import annotations

from dataclasses import dataclass, field
from typing import Dict, List, Optional

from backtest_data import PriceHistory


@dataclass
class SimPosition:
    symbol: str
    qty: float
    entry_price: float
    entry_date: str
    bucket: str
    stop_loss_price: float
    take_profit_price: float


@dataclass
class SimTrade:
    date: str
    symbol: str
    side: str
    qty: float
    price: float
    bucket: str
    rationale: str = ""
    pnl: Optional[float] = None


class BacktestPortfolio:
    def __init__(self, initial_capital: float):
        self.initial_capital = float(initial_capital)
        self.cash = float(initial_capital)
        self.positions: Dict[str, SimPosition] = {}
        self.trades: List[SimTrade] = []
        self.equity_curve: List[dict] = []
        self.bucket_tags: Dict[str, str] = {}
        self.daily_trades = 0
        self._trade_day: Optional[str] = None

    def _reset_daily_trades(self, day: str) -> None:
        if self._trade_day != day:
            self._trade_day = day
            self.daily_trades = 0

    def portfolio_value(self, prices: PriceHistory, day: str) -> float:
        pv = self.cash
        for sym, pos in self.positions.items():
            px = prices.close_on(sym, day)
            if px is not None:
                pv += pos.qty * px
            else:
                pv += pos.qty * pos.entry_price
        return round(pv, 2)

    def to_alpaca_positions(self, prices: PriceHistory, day: str) -> List[dict]:
        out = []
        for sym, pos in self.positions.items():
            px = prices.close_on(sym, day) or pos.entry_price
            mv = pos.qty * px
            unreal = (px - pos.entry_price) * pos.qty
            out.append({
                "symbol": sym,
                "qty": str(pos.qty),
                "market_value": mv,
                "unrealized_pl": unreal,
                "avg_entry_price": pos.entry_price,
                "current_price": px,
                "bucket": pos.bucket,
            })
        return out

    def to_account(self, prices: PriceHistory, day: str) -> dict:
        pv = self.portfolio_value(prices, day)
        return {"portfolio_value": pv, "cash": self.cash}

    def record_equity(self, prices: PriceHistory, day: str) -> None:
        self.equity_curve.append({
            "date": day,
            "portfolio_value": self.portfolio_value(prices, day),
            "cash": round(self.cash, 2),
            "positions": len(self.positions),
        })

    def check_exits(self, prices: PriceHistory, day: str, max_daily_trades: int = 999) -> List[SimTrade]:
        """Stop-loss / take-profit exits at daily close."""
        self._reset_daily_trades(day)
        closed = []
        for sym in list(self.positions.keys()):
            if self.daily_trades >= max_daily_trades:
                break
            pos = self.positions[sym]
            px = prices.close_on(sym, day)
            if px is None:
                continue
            reason = None
            if px <= pos.stop_loss_price:
                reason = "stop_loss"
            elif px >= pos.take_profit_price:
                reason = "take_profit"
            if not reason:
                continue

            proceeds = pos.qty * px
            pnl = (px - pos.entry_price) * pos.qty
            self.cash += proceeds
            closed.append(SimTrade(
                date=day, symbol=sym, side="sell", qty=pos.qty, price=px,
                bucket=pos.bucket, rationale=reason, pnl=round(pnl, 2),
            ))
            self.trades.append(closed[-1])
            del self.positions[sym]
            self.bucket_tags.pop(sym, None)
            self.daily_trades += 1
        return closed

    def buy(
        self,
        decision: dict,
        prices: PriceHistory,
        day: str,
        max_daily_trades: int,
    ) -> Optional[SimTrade]:
        self._reset_daily_trades(day)
        if self.daily_trades >= max_daily_trades:
            return None

        sym = decision["ticker"]
        shares = int(decision.get("shares") or 0)
        price = float(decision.get("current_price") or prices.close_on(sym, day) or 0)
        if shares <= 0 or price <= 0:
            return None

        cost = shares * price
        if cost > self.cash:
            shares = max(1, int(self.cash / price))
            cost = shares * price
        if cost > self.cash or shares <= 0:
            return None

        stop = float(decision.get("stop_loss_price") or price * 0.93)
        tp = float(decision.get("take_profit_price") or price * 1.10)
        bucket = decision.get("bucket") or "Unassigned"

        self.cash -= cost
        if sym in self.positions:
            old = self.positions[sym]
            new_qty = old.qty + shares
            avg = (old.entry_price * old.qty + price * shares) / new_qty
            old.qty = new_qty
            old.entry_price = avg
            old.stop_loss_price = stop
            old.take_profit_price = tp
        else:
            self.positions[sym] = SimPosition(
                symbol=sym, qty=shares, entry_price=price, entry_date=day,
                bucket=bucket, stop_loss_price=stop, take_profit_price=tp,
            )

        self.bucket_tags[sym] = bucket
        trade = SimTrade(
            date=day, symbol=sym, side="buy", qty=shares, price=price,
            bucket=bucket, rationale=decision.get("rationale") or "",
        )
        self.trades.append(trade)
        self.daily_trades += 1
        return trade

    def closed_trades_for_attribution(self) -> List[dict]:
        """Pair sells with rationale for reporting."""
        rows = []
        for t in self.trades:
            if t.side != "sell":
                continue
            rows.append({
                "symbol": t.symbol,
                "side": "sell",
                "qty": t.qty,
                "price": t.price,
                "time": t.date,
                "bucket": t.bucket,
                "rationale": t.rationale,
                "realized_pl_hint": t.pnl,
            })
        return rows

    def summary_stats(self) -> dict:
        sells = [t for t in self.trades if t.side == "sell" and t.pnl is not None]
        wins = [t for t in sells if (t.pnl or 0) > 0]
        losses = [t for t in sells if (t.pnl or 0) < 0]
        total_pnl = sum(t.pnl or 0 for t in sells)
        return {
            "total_trades": len(self.trades),
            "closed_round_trips": len(sells),
            "wins": len(wins),
            "losses": len(losses),
            "win_rate": round(len(wins) / len(sells) * 100, 1) if sells else None,
            "realized_pl": round(total_pnl, 2),
        }
