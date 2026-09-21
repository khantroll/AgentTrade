"""app_paths.py — Shared app root resolution (no heavy config imports)."""

import os

_PKG_DIR = os.path.dirname(os.path.abspath(__file__))


def resolve_app_dir() -> str:
    """App root for state/config/cache files."""
    for candidate in (
        os.getenv("TRADING_AGENT_DIR"),
        os.getenv("APP_DIR"),
        _PKG_DIR,
        os.getcwd(),
    ):
        if not candidate:
            continue
        root = os.path.abspath(candidate)
        if os.path.isfile(os.path.join(root, "agent_state.json")) or os.path.isfile(
            os.path.join(root, "config.json")
        ):
            return root
    return _PKG_DIR
