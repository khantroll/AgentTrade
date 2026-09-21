"""Agent pipeline stages — research, analysis, risk, execution."""

__all__ = [
    "research_agent",
    "analysis_agent",
    "risk_agent",
    "execution_agent",
    "hard_rebalance_agent",
    "review_positions",
]


def __getattr__(name: str):
    """Lazy imports so `from agents.risk import risk_agent` does not load analysis/market_data."""
    if name == "research_agent":
        from agents.research import research_agent
        return research_agent
    if name == "analysis_agent":
        from agents.analysis import analysis_agent
        return analysis_agent
    if name == "risk_agent":
        from agents.risk import risk_agent
        return risk_agent
    if name == "execution_agent":
        from agents.execution import execution_agent
        return execution_agent
    if name == "hard_rebalance_agent":
        from agents.execution import hard_rebalance_agent
        return hard_rebalance_agent
    if name == "review_positions":
        from agents.position_review import review_positions
        return review_positions
    raise AttributeError(f"module {__name__!r} has no attribute {name!r}")
