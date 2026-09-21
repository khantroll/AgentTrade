"""Static local-import closure audit (no third-party runtime required beyond pytest)."""

from __future__ import annotations

import ast
import os

ROOT = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))

SKIP_DIRS = {".pytest_cache", "__pycache__", ".git", ".tools", "venv", ".venv"}


def _iter_py_files():
    for dirpath, dirnames, filenames in os.walk(ROOT):
        dirnames[:] = [d for d in dirnames if d not in SKIP_DIRS]
        for name in filenames:
            if name.endswith(".py"):
                yield os.path.join(dirpath, name)


def _module_name(path: str) -> str:
    rel = os.path.relpath(path, ROOT)[:-3].replace(os.sep, ".")
    if rel.endswith(".__init__"):
        rel = rel[: -len(".__init__")]
    return rel


def test_local_imports_resolve_to_files():
    files = list(_iter_py_files())
    modules = {_module_name(p) for p in files}
    # Package dirs
    for p in files:
        if p.endswith("__init__.py"):
            modules.add(_module_name(p))

    missing = []
    for path in files:
        src = open(path, encoding="utf-8-sig").read()
        tree = ast.parse(src, filename=path)
        for node in ast.walk(tree):
            if isinstance(node, ast.ImportFrom) and node.module:
                root_mod = node.module.split(".")[0]
                if root_mod in {
                    "agenttrade", "agents", "analysis_funnel", "agent_config",
                    "account_sync", "buy_lock", "buy_guard", "buckets",
                    "alpaca_client", "cycle", "agent", "screener", "screener_cache",
                    "llm_router", "market_data", "pnl_attribution", "llm_attribution",
                    "rationale_attribution", "margin_correction", "performance_history",
                    "period_summary", "report_export", "trade_log", "app_paths",
                    "health_check", "config_server", "signal_attribution",
                    "signal_performance", "confidence_engine", "agreement_engine",
                    "order_utils",
                }:
                    if node.module not in modules and node.module.split(".")[0] not in modules:
                        # agents.risk etc.
                        if not any(m == node.module or m.startswith(node.module + ".") for m in modules):
                            missing.append((os.path.relpath(path, ROOT), node.module))
            if isinstance(node, ast.Import):
                for alias in node.names:
                    name = alias.name
                    root_mod = name.split(".")[0]
                    if root_mod in {"agenttrade", "agents"} and name not in modules:
                        if not any(m == name or m.startswith(name + ".") for m in modules):
                            missing.append((os.path.relpath(path, ROOT), name))

    assert "agents.risk" in modules or os.path.isfile(os.path.join(ROOT, "agents", "risk.py"))
    assert not missing, f"Unresolved local imports: {missing[:20]}"
