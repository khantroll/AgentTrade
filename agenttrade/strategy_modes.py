"""Strategy execution modes — shadow / paper-only (Tier 4)."""

from __future__ import annotations

import json
import logging
import os

log = logging.getLogger(__name__)

# Default modes per bucket name (lowercase). Override via STRATEGY_MODES env JSON.
DEFAULT_STRATEGY_MODES = {
    "growth": "live",
    "dividend": "live",
    "swing": "live",
    "crypto": "live",
    "experimental_reddit": "shadow_mode",
}


def load_strategy_modes() -> dict[str, str]:
    modes = dict(DEFAULT_STRATEGY_MODES)
    raw = os.getenv("STRATEGY_MODES", "")
    if raw:
        try:
            overrides = json.loads(raw)
            if isinstance(overrides, dict):
                for k, v in overrides.items():
                    modes[str(k).lower()] = str(v).lower()
        except (json.JSONDecodeError, TypeError):
            log.warning("[StrategyModes] Invalid STRATEGY_MODES JSON")
    return modes


def get_bucket_execution_mode(bucket_name: str) -> str:
    """Returns: live | paper_only | shadow_mode"""
    modes = load_strategy_modes()
    key = str(bucket_name or "").lower()
    return modes.get(key, modes.get(key.replace(" ", "_"), "live"))


def blocks_live_orders(mode: str) -> bool:
    return mode in ("shadow_mode", "paper_only")


def mode_blocks_order(bucket_name: str, is_paper_account: bool) -> tuple[bool, str]:
    """
    Returns (blocked, reason).
    paper_only blocks when Alpaca is in live mode.
    shadow_mode always blocks real submission.
    """
    mode = get_bucket_execution_mode(bucket_name)
    if mode == "shadow_mode":
        return True, "shadow_mode: hypothetical only"
    if mode == "paper_only" and not is_paper_account:
        return True, "paper_only: live account blocked"
    return False, ""
