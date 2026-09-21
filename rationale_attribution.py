"""
rationale_attribution.py — P/L grouped by buy rationale themes (keyword taxonomy).
"""

import re
from typing import List

from llm_attribution import _group_aggregate, _sum_rows, llm_attribution_from_closed
from pnl_attribution import _float, _load_agent_state


# (theme label, regex patterns) — order defines primary-theme priority
THEME_RULES: List[tuple] = [
    ("Hard rebalance", [
        r"hard rebalance", r"overweight bucket", r"trim overweight",
    ]),
    ("Dual agree", [
        r"dual agree", r"dual-model agreement", r"both models agree",
    ]),
    ("Dual conflict", [
        r"dual conflict", r"models disagree", r"conflict —",
    ]),
    ("Tiered vote", [
        r"tiered multi-model", r"tiered vote", r"tiered:", r"multi-model vote",
    ]),
    ("Low confidence", [
        r"low research confidence", r"confidence \d\.\d+ <", r"below min confidence",
    ]),
    ("Dividend / income", [
        r"dividend", r"yield", r"payout", r"income play", r"\breit\b", r"aristocrat",
    ]),
    ("Earnings", [
        r"earnings", r"\beps\b", r"revenue growth", r"beat estimates", r"guidance raise",
    ]),
    ("Momentum", [
        r"momentum", r"breakout", r"uptrend", r"surging", r"rally", r"52.?week high",
        r"relative strength", r"strong trend",
    ]),
    ("News / catalyst", [
        r"\bnews\b", r"catalyst", r"headline", r"announcement", r"\bfda\b", r"approval",
    ]),
    ("Congress / insider", [
        r"congress", r"insider buying", r"quiver", r"politician", r"congressional",
    ]),
    ("Reddit / social", [
        r"reddit", r"\bwsb\b", r"social sentiment", r"meme", r"crowd",
    ]),
    ("Technical", [
        r"\brsi\b", r"\bsma\b", r"macd", r"overbought", r"oversold", r"bollinger",
        r"moving average", r"technical", r"above sma", r"below sma",
    ]),
    ("Value", [
        r"undervalued", r"value play", r"cheap valuation", r"\bp/e\b", r"pe ratio",
        r"discount to", r"book value",
    ]),
    ("Risk / skip", [
        r"\bskip\b", r"too risky", r"stop.?loss", r"position limit", r"no cash",
        r"overweight", r"at max positions", r"budget exhausted",
    ]),
    ("Crypto", [
        r"\bcrypto\b", r"bitcoin", r"\bbtc\b", r"ethereum", r"\beth\b", r"on-chain",
    ]),
]


def _norm_text(*parts: str) -> str:
    return " ".join(p for p in parts if p).lower()


def extract_themes(
    rationale: str = "",
    dual_status: str = "",
    tiered_source: str = "",
) -> List[str]:
    """Return all matching themes, highest-priority first."""
    text = _norm_text(rationale, tiered_source)
    ds = (dual_status or "").lower()
    matched: List[str] = []

    if ds == "both_buy":
        matched.append("Dual agree")
    elif ds == "conflict":
        matched.append("Dual conflict")

    for theme, patterns in THEME_RULES:
        if theme in matched:
            continue
        for pat in patterns:
            if re.search(pat, text, re.I):
                matched.append(theme)
                break

    if not matched:
        if (rationale or "").strip():
            matched.append("General thesis")
        else:
            matched.append("Untagged")
    return matched


def primary_theme(
    rationale: str = "",
    dual_status: str = "",
    tiered_source: str = "",
) -> str:
    themes = extract_themes(rationale, dual_status, tiered_source)
    return themes[0] if themes else "Untagged"


def annotate_closed_trades(closed: list) -> None:
    """Add primary_theme and themes to closed trade rows in place."""
    for row in closed:
        if not row.get("in_period"):
            continue
        themes = extract_themes(
            row.get("rationale") or "",
            row.get("dual_status") or "",
            row.get("tiered_source") or "",
        )
        row["themes"] = themes
        row["primary_theme"] = themes[0]


def compute_rationale_attribution(closed: list, state: dict = None) -> dict:
    """
    Aggregate realized + unrealized P/L by primary rationale theme per symbol.
    """
    state = state or _load_agent_state()
    _, _, _, sym_rows = llm_attribution_from_closed(closed, state=state)

    for row in sym_rows:
        themes = extract_themes(
            row.get("rationale") or "",
            row.get("dual_status") or "",
            row.get("tiered_source") or "",
        )
        row["themes"] = themes
        row["primary_theme"] = themes[0]

    by_theme = _group_aggregate(
        sym_rows,
        lambda r: r.get("primary_theme") or "Untagged",
        "theme",
    )

    # Secondary view: tag-level counts (P/L split evenly when multiple themes)
    tag_rows: dict = {}
    for row in sym_rows:
        themes = row.get("themes") or ["Untagged"]
        n = max(len(themes), 1)
        share = {
            "realized_pl": _float(row.get("realized_pl")) / n,
            "unrealized_pl": _float(row.get("unrealized_pl")) / n,
            "wins": int(row.get("wins") or 0) / n,
            "losses": int(row.get("losses") or 0) / n,
            "closed_trades": int(row.get("closed_trades") or 0) / n,
            "open_positions": int(row.get("open_positions") or 0) / n,
            "symbol": row.get("symbol"),
        }
        for theme in themes:
            tag_rows.setdefault(theme, []).append(share)

    by_tag = [
        {"theme": theme, **_sum_rows(rows)}
        for theme, rows in tag_rows.items()
    ]
    by_tag.sort(key=lambda x: x["total_pl"], reverse=True)

    tagged = sum(1 for r in sym_rows if (r.get("primary_theme") or "") != "Untagged")
    return {
        "by_theme": by_theme,
        "by_tag": by_tag,
        "tagged_symbols": tagged,
        "total_symbols": len(sym_rows),
    }
