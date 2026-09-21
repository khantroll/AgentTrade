"""Smoke-import production modules with broker/LLM/network packages stubbed."""

from __future__ import annotations

import importlib
import pkgutil

import pytest


def test_smoke_import_agent_cycle_and_agents(monkeypatch):
    monkeypatch.setenv("ALPACA_API_KEY", "test-key")
    monkeypatch.setenv("ALPACA_SECRET_KEY", "test-secret")
    monkeypatch.setenv("ALPACA_PAPER", "true")

    import agent  # noqa: F401
    import cycle  # noqa: F401

    import agents as agents_pkg

    loaded = []
    for mod in pkgutil.iter_modules(agents_pkg.__path__):
        name = f"agents.{mod.name}"
        imported = importlib.import_module(name)
        loaded.append(name)
        assert imported is not None

    assert "agents.research" in loaded
    assert "agents.analysis" in loaded
    assert "agents.risk" in loaded
    assert "agents.execution" in loaded
    assert "agents.position_review" in loaded
    assert hasattr(cycle, "run_trading_cycle")
    assert hasattr(agent, "run_trading_cycle")
