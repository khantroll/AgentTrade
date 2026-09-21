"""
buckets.py — Portfolio Bucket Manager
======================================
Divides your total portfolio into named "buckets", each with its own:
  - Capital allocation (% of total portfolio)
  - Strategy mode (growth, dividend, price_range, custom)
  - Price filters, position limits, stop-loss/take-profit overrides
  - Independent universe discovery via screener.py

Think of it like running 3 separate mini-funds inside one Alpaca account.

──────────────────────────────────────────────────
Example bucket layout (edit BUCKETS below):

  Bucket A — "Growth"      50% of portfolio
    Standard momentum/news/reddit screener
    Price $10–$500, normal risk params

  Bucket B — "Dividend"    30% of portfolio
    Dividend quality screen + r/dividends sentiment
    Minimum 2% yield, payout < 90%, near ex-div dates
    Tighter stop-loss (3%) since these are income plays

  Bucket C — "Swing"       20% of portfolio
    Price-range filtered: $5–$50 stocks only
    Higher risk tolerance, wider take-profit target
──────────────────────────────────────────────────

Rebalancing:
  - Soft rebalance: new buys are directed to the most under-weight bucket
  - Hard rebalance: sell excess positions in over-weight buckets (optional)
  - Rebalance check runs at start of every trading cycle

Usage:
  from buckets import BucketManager
  bm = BucketManager()
  for bucket in bm.active_buckets():
      universe, sources = get_universe(bucket_config=bucket.screener_config)
      ...
"""

import json
import logging
from dataclasses import dataclass, field
from typing import Optional
import os

log = logging.getLogger(__name__)


# ─────────────────────────────────────────────────────────────────────────────
# Bucket definition
# ─────────────────────────────────────────────────────────────────────────────

@dataclass
class Bucket:
    """
    A single portfolio bucket definition.

    name            : display name, also used as the tag in logs/dashboard
    allocation_pct  : target % of TOTAL portfolio value (all buckets must sum ≤ 1.0)
    mode            : "growth" | "dividend" | "price_range" | "crypto" | "custom"
    min_price       : minimum stock price for this bucket
    max_price       : maximum stock price for this bucket
    max_positions   : max simultaneous open positions within this bucket
    max_position_pct: max single position size as % of THIS bucket's capital
    stop_loss_pct   : stop-loss % below entry (overrides global default)
    take_profit_pct : take-profit % above entry (overrides global default)
    custom_tickers  : for mode="custom", a fixed list of tickers to trade
    min_dividend_yield: for mode="dividend", minimum acceptable yield (0.02 = 2%)
    enabled         : set False to pause this bucket without deleting it
    asset_class     : "us_equity" | "crypto"; crypto uses Alpaca symbols like BTC/USD
    notes           : free-text description shown in dashboard
    """
    name:               str
    allocation_pct:     float
    mode:               str   = "growth"
    min_price:          float = 5.0
    max_price:          float = 500.0
    max_positions:      int   = 5
    max_position_pct:   float = 0.20   # 20% of bucket's capital per position
    stop_loss_pct:      float = 0.05
    take_profit_pct:    float = 0.10
    custom_tickers:     list  = field(default_factory=list)
    min_dividend_yield: float = 0.02
    enabled:            bool  = True
    asset_class:        str   = "us_equity"
    is_crypto:          bool  = False
    crypto_coins:       list  = field(default_factory=list)
    notes:              str   = ""

    @property
    def screener_config(self) -> dict:
        """Returns the config dict passed into screener.get_universe()."""
        cfg = {
            "mode":      self.mode,
            "min_price": self.min_price,
            "max_price": self.max_price,
            "asset_class": self.asset_class,
        }
        if self.mode == "custom" and self.custom_tickers:
            cfg["tickers"] = self.custom_tickers
        if self.mode == "dividend":
            cfg["min_dividend_yield"] = self.min_dividend_yield
        if self.mode == "crypto" and self.custom_tickers:
            cfg["symbols"] = self.custom_tickers
        return cfg


# ─────────────────────────────────────────────────────────────────────────────
# ✏️  EDIT YOUR BUCKETS HERE
# ─────────────────────────────────────────────────────────────────────────────

BUCKETS = [
    Bucket(
        name            = "Growth",
        allocation_pct  = 0.50,       # 50% of total portfolio
        mode            = "growth",
        min_price       = 10.0,
        max_price       = 500.0,
        max_positions   = 8,   # v5: was 5
        max_position_pct= 0.20,       # up to 20% of Growth bucket per position
        stop_loss_pct   = 0.07,  # v5: was 0.05
        take_profit_pct = 0.18,  # v5: was 0.10
        notes           = "Momentum + news + Reddit growth plays",
    ),

    Bucket(
        name            = "Dividend",
        allocation_pct  = 0.30,       # 30% of total portfolio
        mode            = "dividend",
        min_price       = 5.0,
        max_price       = 200.0,
        max_positions   = 8,   # v5: was 6
        max_position_pct= 0.18,
        stop_loss_pct   = 0.05,  # v5: was 0.03
        take_profit_pct = 0.12,  # v5: was 0.08       # lower target — we collect yield, not big runs
        min_dividend_yield = 0.02,    # minimum 2% yield
        notes           = "High-yield dividend stocks screened for payout sustainability",
    ),

    Bucket(
        name            = "Swing",
        allocation_pct  = 0.20,       # 20% of total portfolio
        mode            = "price_range",
        min_price       = 5.0,
        max_price       = 50.0,       # only stocks $5–$50
        max_positions   = 6,   # v5: was 4
        max_position_pct= 0.25,
        stop_loss_pct   = 0.08,  # v5: was 0.07
        take_profit_pct = 0.25,  # v5: was 0.15       # higher target to compensate
        notes           = "Lower-priced swing trades, $5–$50 range",
    ),



    Bucket(
        name            = "Crypto",
        allocation_pct  = float(os.getenv("CRYPTO_MAX_ALLOCATION", "0.10")),
        mode            = "crypto",
        custom_tickers  = [s.strip() for s in os.getenv("CRYPTO_SYMBOLS", "BTC/USD,ETH/USD,SOL/USD,LINK/USD").split(",") if s.strip()],
        max_positions   = int(os.getenv("CRYPTO_MAX_POSITIONS", "3")),
        max_position_pct= float(os.getenv("CRYPTO_MAX_POSITION_PCT", "0.25")),
        stop_loss_pct   = float(os.getenv("CRYPTO_STOP_LOSS_PCT", "0.08")),
        take_profit_pct = float(os.getenv("CRYPTO_TAKE_PROFIT_PCT", "0.15")),
        enabled         = os.getenv("ENABLE_CRYPTO", "0").lower() in ("1", "true", "yes", "on"),
        asset_class     = "crypto",
        is_crypto       = True,
        notes           = "Risk-limited 24/7 Alpaca crypto bucket",
    ),

    # ── Uncomment and customize to add another bucket ─────────────────────────
    # Bucket(
    #     name           = "Watchlist",
    #     allocation_pct = 0.0,        # 0% = tracking only, no real trades
    #     mode           = "custom",
    #     custom_tickers = ["AAPL", "MSFT", "NVDA", "TSLA", "META"],
    #     enabled        = False,       # disabled — just for research
    #     notes          = "My personal watchlist — no auto-trading",
    # ),
]


# ─────────────────────────────────────────────────────────────────────────────
# BucketManager
# ─────────────────────────────────────────────────────────────────────────────

class BucketManager:
    def __init__(self, buckets: list = None):
        self.buckets = buckets or BUCKETS
        self._validate()

    def _validate(self):
        total = sum(b.allocation_pct for b in self.buckets if b.enabled)
        if total > 1.001:
            log.warning(
                f"[Buckets] ⚠️  Total allocation {total*100:.1f}% > 100%. "
                "Buckets will be proportionally scaled down."
            )
        log.info(f"[Buckets] Loaded {len(self.buckets)} buckets — "
                 f"{sum(1 for b in self.buckets if b.enabled)} active, "
                 f"total allocation {total*100:.0f}%")

    def active_buckets(self) -> list:
        return [b for b in self.buckets if b.enabled and b.allocation_pct > 0]

    def bucket_capital(self, bucket: Bucket, portfolio_value: float) -> float:
        """Dollar capital allocated to this bucket."""
        total_alloc = sum(b.allocation_pct for b in self.active_buckets())
        # Normalize so buckets always sum to ≤ 100%
        normalized  = bucket.allocation_pct / max(total_alloc, 1.0)
        return portfolio_value * normalized

    def bucket_positions(self, bucket: Bucket, all_positions: list) -> list:
        """
        Return current Alpaca positions that belong to this bucket.
        Tagged by the bucket name stored in a local state file.
        """
        tags = self._load_tags()
        return [p for p in all_positions if tags.get(p["symbol"]) == bucket.name]

    def tag_position(self, ticker: str, bucket_name: str):
        """Record which bucket a ticker was bought into."""
        tags = self._load_tags()
        tags[ticker] = bucket_name
        self._save_tags(tags)

    def untag_position(self, ticker: str):
        """Remove tag when a position is fully closed."""
        tags = self._load_tags()
        tags.pop(ticker, None)
        self._save_tags(tags)

    def rebalance_report(self, all_positions: list, portfolio_value: float) -> dict:
        """
        Generate a rebalance report showing each bucket's current vs target
        allocation and which positions, if any, should be trimmed.

        Returns:
          {
            "bucket_name": {
              "target_pct":   0.50,
              "current_pct":  0.62,
              "target_$":     5000,
              "current_$":    6200,
              "drift_$":      1200,
              "action":       "TRIM" | "ADD" | "OK",
              "positions":    [...]
            }
          }
        """
        tags   = self._load_tags()
        report = {}

        for bucket in self.active_buckets():
            b_positions = [p for p in all_positions if tags.get(p["symbol"]) == bucket.name]
            current_val = sum(float(p.get("market_value", 0)) for p in b_positions)
            target_val  = self.bucket_capital(bucket, portfolio_value)
            drift       = current_val - target_val
            drift_pct   = drift / portfolio_value

            # Trigger a rebalance recommendation if drift > 5% of portfolio
            if abs(drift_pct) < 0.05:
                action = "OK"
            elif drift > 0:
                action = "TRIM"   # bucket is over-weight → suggest selling some
            else:
                action = "ADD"    # bucket is under-weight → direct new buys here

            report[bucket.name] = {
                "target_pct":  round(bucket.allocation_pct * 100, 1),
                "current_pct": round(current_val / max(portfolio_value, 1) * 100, 1),
                "target_$":    round(target_val, 2),
                "current_$":   round(current_val, 2),
                "drift_$":     round(drift, 2),
                "action":      action,
                "positions":   [p["symbol"] for p in b_positions],
            }

        return report

    def prioritize_buckets(self, report: dict) -> list:
        """
        Return active buckets sorted so the most under-weight one gets
        first access to cash each cycle (soft rebalance).
        """
        def under_weight(bucket: Bucket) -> float:
            r = report.get(bucket.name, {})
            # Most negative drift (furthest below target) → highest priority
            return r.get("drift_$", 0)

        return sorted(self.active_buckets(), key=under_weight)

    def should_hard_rebalance(self, report: dict, portfolio_value: float = 0,
                              drift_threshold_pct: float = 0.10) -> list:
        """Returns (bucket, [symbols]) pairs eligible for hard rebalance."""
        trim_list = []
        pv = max(float(portfolio_value or 0), 1.0)
        for bucket in self.active_buckets():
            r = report.get(bucket.name, {})
            if r.get("action") != "TRIM":
                continue
            drift = float(r.get("drift_$", 0))
            if drift <= 0 or drift / pv < drift_threshold_pct:
                continue
            trim_list.append((bucket, r.get("positions", [])))
        return trim_list

    def plan_hard_rebalance(
        self,
        report: dict,
        all_positions: list,
        portfolio_value: float,
        drift_threshold_pct: float = 0.10,
        max_sells_per_bucket: int = 1,
        trim_fraction: float = 0.5,
    ) -> list:
        """
        Build sell plans for buckets overweight beyond drift_threshold_pct of portfolio.
        Trims gradually (trim_fraction of excess) starting with smallest positions.
        """
        pos_by_sym = {p.get("symbol"): p for p in (all_positions or []) if p.get("symbol")}
        plans = []
        pv = max(float(portfolio_value or 0), 1.0)

        for bucket in self.active_buckets():
            r = report.get(bucket.name, {})
            if r.get("action") != "TRIM":
                continue
            drift = float(r.get("drift_$", 0))
            if drift <= 0:
                continue
            drift_pct = drift / pv
            if drift_pct < drift_threshold_pct:
                continue

            excess = drift
            remaining_trim = excess * trim_fraction
            symbols = r.get("positions") or []
            bucket_pos = [pos_by_sym[s] for s in symbols if s in pos_by_sym]
            bucket_pos.sort(key=lambda p: float(p.get("market_value") or 0))

            sells = 0
            for p in bucket_pos:
                if sells >= max_sells_per_bucket or remaining_trim <= 5:
                    break
                sym = p.get("symbol")
                mv = float(p.get("market_value") or 0)
                qty = float(p.get("qty") or 0)
                if not sym or qty <= 0 or mv <= 0:
                    continue
                price = mv / qty
                trim_amt = min(remaining_trim, mv)
                if bucket.is_crypto:
                    sell_qty = min(qty, round(trim_amt / price, 6))
                else:
                    sell_qty = min(int(qty), max(1, int(trim_amt / price)))
                if sell_qty <= 0:
                    continue

                plans.append({
                    "bucket": bucket.name,
                    "symbol": sym,
                    "qty": sell_qty,
                    "side": "sell",
                    "reason": (
                        f"Hard rebalance: {bucket.name} overweight "
                        f"${drift:,.0f} ({drift_pct * 100:.1f}% of portfolio)"
                    ),
                    "asset_class": bucket.asset_class,
                    "is_crypto": bucket.is_crypto,
                    "drift_$": round(drift, 2),
                    "trim_target_$": round(trim_amt, 2),
                })
                remaining_trim -= trim_amt
                sells += 1

        return plans

    def max_position_dollars(self, bucket: Bucket, portfolio_value: float) -> float:
        """Max dollar size for a single position in this bucket."""
        capital = self.bucket_capital(bucket, portfolio_value)
        return capital * bucket.max_position_pct

    def to_dict(self) -> list:
        """Serialize bucket config for saving in agent_state.json."""
        return [
            {
                "name":           b.name,
                "allocation_pct": b.allocation_pct,
                "mode":           b.mode,
                "min_price":      b.min_price,
                "max_price":      b.max_price,
                "max_positions":  b.max_positions,
                "stop_loss_pct":  b.stop_loss_pct,
                "take_profit_pct":b.take_profit_pct,
                "asset_class":    b.asset_class,
                "enabled":        b.enabled,
                "notes":          b.notes,
            }
            for b in self.buckets
        ]

    # ── Internal tag persistence ──────────────────────────────────────────────

    TAG_FILE = "bucket_tags.json"

    def _load_tags(self) -> dict:
        try:
            with open(self.TAG_FILE) as f:
                return json.load(f)
        except Exception:
            return {}

    def _save_tags(self, tags: dict):
        try:
            _t=self.TAG_FILE+".tmp"
            with open(_t,"w") as f: json.dump(tags,f,indent=2)
            os.replace(_t,self.TAG_FILE)
        except Exception as e:
            log.warning(f"[Buckets] Could not save tags: {e}")


# ─────────────────────────────────────────────────────────────────────────────
# Standalone test
# ─────────────────────────────────────────────────────────────────────────────

if __name__ == "__main__":
    logging.basicConfig(level=logging.INFO, format="%(asctime)s [%(levelname)s] %(message)s")

    bm = BucketManager()
    print("\n📦 Bucket Configuration:")
    for b in bm.active_buckets():
        cap = bm.bucket_capital(b, 10000)
        print(f"  {b.name:12} {b.allocation_pct*100:.0f}%  ${cap:,.0f}  "
              f"mode={b.mode}  ${b.min_price}–${b.max_price}  "
              f"SL={b.stop_loss_pct*100:.0f}%  TP={b.take_profit_pct*100:.0f}%")

    # Simulate a rebalance report with fake positions
    fake_positions = [
        {"symbol": "AAPL",  "market_value": "3500"},
        {"symbol": "NVDA",  "market_value": "1500"},
        {"symbol": "O",     "market_value": "800"},
        {"symbol": "ARCC",  "market_value": "500"},
        {"symbol": "PLTR",  "market_value": "900"},
    ]

    # Manually tag them for the demo
    bm.tag_position("AAPL", "Growth")
    bm.tag_position("NVDA", "Growth")
    bm.tag_position("O",    "Dividend")
    bm.tag_position("ARCC", "Dividend")
    bm.tag_position("PLTR", "Swing")

    report = bm.rebalance_report(fake_positions, 10000)
    print("\n📊 Rebalance Report:")
    for name, r in report.items():
        print(f"  {name:12} target={r['target_pct']}%  current={r['current_pct']}%  "
              f"drift=${r['drift_$']:+,.0f}  → {r['action']}")
