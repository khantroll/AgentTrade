from pathlib import Path

agent = Path("/opt/trading-agent/agent.py")
dash = Path("/var/www/my_webapp__3/www/dashboard.html")

s = agent.read_text()

helper = r'''
def prepare_dashboard_contract(state):
    """
    Adds stable dashboard/risk fields without changing the raw broker data.
    """
    import os

    def num(v):
        try:
            return float(str(v).replace(",", ""))
        except Exception:
            return 0.0

    if not isinstance(state, dict):
        return state

    cash_raw = num(state.get("cash"))
    portfolio_value = num(state.get("portfolio_value"))
    reserve_pct = float(os.getenv("RESERVE_CASH_PCT", "0.10"))
    allow_margin = os.getenv("ALLOW_MARGIN", "0").lower() in ("1", "true", "yes", "on")

    margin_used = abs(cash_raw) if cash_raw < 0 else 0.0
    cash_effective = max(cash_raw, 0.0)
    reserve_cash = portfolio_value * reserve_pct
    tradable_cash = max(cash_effective - reserve_cash, 0.0)

    buying_enabled = allow_margin or tradable_cash > 0
    if cash_raw < 0 and not allow_margin:
        buying_enabled = False

    state["cash_raw"] = cash_raw
    state["cash_effective"] = cash_effective
    state["margin_used"] = margin_used
    state["tradable_cash"] = tradable_cash
    state["buying_enabled"] = buying_enabled
    state["buying_status"] = "ENABLED" if buying_enabled else "LOCKED"

    state["risk"] = {
        "cash_raw": cash_raw,
        "cash_effective": cash_effective,
        "tradable_cash": tradable_cash,
        "margin_used": margin_used,
        "reserve_cash": reserve_cash,
        "reserve_cash_pct": reserve_pct,
        "allow_margin": allow_margin,
        "buying_enabled": buying_enabled,
        "buying_status": state["buying_status"],
    }

    # Fix recent trade amounts when Alpaca net_amount is missing/zero.
    for key in ("recent_trades", "last_orders"):
        rows = state.get(key, []) or []
        for r in rows:
            amount = num(r.get("amount"))
            price = num(r.get("price"))
            qty = num(r.get("qty"))
            if amount == 0 and price and qty:
                r["amount"] = round(price * qty, 2)
        state[key] = rows

    # Dashboard-friendly placeholders until deeper decision capture is patched.
    state.setdefault("candidates", [])
    state.setdefault("blocked_ideas", [])
    state.setdefault("screener_counts", {
        "momentum": {"scanned": 0, "shortlisted": 0, "accepted": 0, "blocked": 0},
        "movers": {"scanned": 0, "shortlisted": 0, "accepted": 0, "blocked": 0},
        "reddit": {"scanned": 0, "shortlisted": 0, "accepted": 0, "blocked": 0},
        "news": {"scanned": 0, "shortlisted": 0, "accepted": 0, "blocked": 0},
        "congress": {"scanned": 0, "shortlisted": 0, "accepted": 0, "blocked": 0},
    })

    return state
'''

if "def prepare_dashboard_contract(state):" not in s:
    i = s.find("def ")
    if i == -1:
        raise SystemExit("No function insertion point found")
    s = s[:i] + helper + "\n\n" + s[i:]

if "state = prepare_dashboard_contract(state)" not in s:
    s = s.replace(
        "state = prepare_dashboard_state(state)",
        "state = prepare_dashboard_state(state)\n        state = prepare_dashboard_contract(state)",
        1
    )

agent.write_text(s)

d = dash.read_text()

risk_js = r'''
<script id="agenttrade-risk-banner">
async function loadRiskBanner() {
  try {
    const res = await fetch("agent_state.json?v=" + Date.now(), {cache:"no-store"});
    const s = await res.json();
    const r = s.risk || {};

    const money = v => {
      const n = Number(v || 0);
      return "$" + n.toLocaleString(undefined,{minimumFractionDigits:2,maximumFractionDigits:2});
    };

    let el = document.getElementById("risk-banner");
    if (!el) {
      el = document.createElement("section");
      el.id = "risk-banner";
      el.style = "margin:10px 14px;padding:12px;border:1px solid #12313a;background:#07131a;color:#dff;font-family:monospace;";
      const logo = document.querySelector(".logo") || document.body.firstElementChild;
      document.body.insertBefore(el, logo ? logo.nextSibling : document.body.firstChild);
    }

    const locked = r.buying_enabled === false;
    el.innerHTML = `
      <div style="display:grid;grid-template-columns:repeat(5,1fr);gap:10px;align-items:center;">
        <div><b>Risk Status</b><br><span style="color:${locked ? '#ff3b72':'#00ff9c'}">${r.buying_status || s.buying_status || 'UNKNOWN'}</span></div>
        <div><b>Raw Cash</b><br>${money(r.cash_raw ?? s.cash)}</div>
        <div><b>Tradable Cash</b><br>${money(r.tradable_cash ?? s.cash_effective)}</div>
        <div><b>Margin / Deficit</b><br>${money(r.margin_used ?? s.margin_used)}</div>
        <div><b>Reserve</b><br>${money(r.reserve_cash)}</div>
      </div>
      ${locked ? `<div style="margin-top:8px;color:#ffb000;">Buying is locked until cash recovers or positions are liquidated.</div>` : ``}
    `;
  } catch(e) {
    console.warn("risk banner failed", e);
  }
}
document.addEventListener("DOMContentLoaded", loadRiskBanner);
setInterval(loadRiskBanner, 30000);
</script>
'''

if "agenttrade-risk-banner" not in d:
    d = d.replace("</body>", risk_js + "\n</body>")

dash.write_text(d)

print("Patched dashboard risk contract and banner.")