"""Independent re-implementations used to *check* the ledger, never to trade.

Written separately from portfolio.py on purpose (different structure, no shared
code), so a bug in the ledger shows up as a mismatch instead of being copied.

  rebuild_books(fills)        cash / margin / positions / fees / realized P&L by FY
                              from nothing but the list of fills
  reference_tax(gains_by_fy)  Indian capital gains tax: Sec 70 set-off, Sec 74
                              carry-forward, Sec 112A exemption, 15% / 10%
  audit_ledger(ledger, ...)   compare the two, return a list of mismatches
"""
from __future__ import annotations

from datetime import date

BROKERAGE = 20.00
DP = 15.34
STCG, LTCG, EXEMPT = 0.15, 0.10, 100_000.0


def _fy(d: str) -> int:
    y, m = int(d[:4]), int(d[5:7])
    return y if m >= 4 else y - 1


def rebuild_books(fills: list[dict], start_cash: float = 1_000_000.0) -> dict:
    """Replay fills with average-cost accounting. A fill with settlement=True is a
    platform square-off (no brokerage charged to us)."""
    cash, margin, fees = start_cash, 0.0, 0.0
    lots: dict[str, dict] = {}                 # ticker -> {"qty", "cost", "opened"}
    st: dict[int, float] = {}
    lt: dict[int, float] = {}
    for f in fills:
        t, a, q, px, d = f["ticker"], f["action"], int(f["qty"]), float(f["price"]), f["date"]
        fee = 0.0 if f.get("settlement") else BROKERAGE
        lot = lots.get(t)
        if a == "BUY":
            if lot is None:
                lots[t] = lot = {"qty": 0, "cost": 0.0, "opened": d}
            lot["qty"] += q
            lot["cost"] += q * px
            cash -= q * px + fee
        elif a == "SELL":
            avg = lot["cost"] / lot["qty"]
            pnl = (px - avg) * q
            held = (date.fromisoformat(d) - date.fromisoformat(lot["opened"])).days
            bucket = lt if held > 365 else st
            bucket[_fy(d)] = bucket.get(_fy(d), 0.0) + pnl
            lot["cost"] -= avg * q
            lot["qty"] -= q
            cash += q * px - fee - DP
            fees += DP
            if lot["qty"] == 0:
                del lots[t]
        elif a == "SHORT":
            if lot is None:
                lots[t] = lot = {"qty": 0, "cost": 0.0, "opened": d}
            lot["qty"] -= q
            lot["cost"] += q * px                 # cost = short proceeds = margin locked
            cash -= q * px + fee
            margin += q * px
        elif a == "COVER":
            avg = lot["cost"] / -lot["qty"]
            pnl = (avg - px) * q
            st[_fy(d)] = st.get(_fy(d), 0.0) + pnl
            margin -= avg * q
            cash += avg * q + pnl - fee
            lot["cost"] -= avg * q
            lot["qty"] += q
            if lot["qty"] == 0:
                del lots[t]
        else:
            raise ValueError(a)
        fees += fee
    pos = {t: {"qty": v["qty"], "avg": v["cost"] / abs(v["qty"])} for t, v in lots.items()}
    return {"cash": cash, "margin": margin, "fees": fees, "positions": pos, "st": st, "lt": lt}


def reference_tax(st: dict[int, float], lt: dict[int, float]) -> dict[int, float]:
    """Tax per FY from net short/long-term gains per FY."""
    out: dict[int, float] = {}
    carry_st = carry_lt = 0.0                  # unabsorbed losses brought forward (positive numbers)
    for fy in sorted(set(st) | set(lt)):
        s, l = st.get(fy, 0.0), lt.get(fy, 0.0)
        # Sec 70: a net short-term loss may reduce this year's long-term gain
        if s < 0 and l > 0:
            used = min(-s, l)
            s, l = s + used, l - used
        # Sec 74: brought-forward short-term losses reduce STCG first, then LTCG
        if carry_st and s > 0:
            used = min(carry_st, s); s -= used; carry_st -= used
        if carry_st and l > 0:
            used = min(carry_st, l); l -= used; carry_st -= used
        if carry_lt and l > 0:
            used = min(carry_lt, l); l -= used; carry_lt -= used
        if s < 0:
            carry_st += -s
            s = 0.0
        if l < 0:
            carry_lt += -l
            l = 0.0
        out[fy] = STCG * s + LTCG * max(0.0, l - EXEMPT)
    return out


def audit_ledger(ledger, fills: list[dict], prices: dict[str, float], curve_last: float | None = None,
                 start_cash: float = 1_000_000.0, tol: float = 0.01) -> list[str]:
    """Every way the ledger can disagree with an independent replay of its own fills."""
    ref = rebuild_books(fills, start_cash)
    bad: list[str] = []

    def cmp(name, a, b):
        if abs(a - b) > tol:
            bad.append(f"{name}: ledger {a:,.2f} vs replay {b:,.2f}")

    cmp("cash", ledger.cash, ref["cash"])
    cmp("margin", ledger.margin_locked, ref["margin"])
    cmp("fees", ledger.fees_paid, ref["fees"])
    lp = {t: p.qty for t, p in ledger.positions.items()}
    rp = {t: v["qty"] for t, v in ref["positions"].items()}
    if lp != rp:
        bad.append(f"positions: ledger {lp} vs replay {rp}")
    for t, v in ref["positions"].items():
        if t in ledger.positions:
            cmp(f"avg price {t}", ledger.positions[t].avg_price, v["avg"])
    for fy in set(ledger.gains) | set(ref["st"]) | set(ref["lt"]):
        g = ledger.gains.get(fy)
        cmp(f"FY{fy} short-term", g.st if g else 0.0, ref["st"].get(fy, 0.0))
        cmp(f"FY{fy} long-term", g.lt if g else 0.0, ref["lt"].get(fy, 0.0))
    ref_tax = reference_tax(ref["st"], ref["lt"])
    cmp("total tax", ledger.estimated_tax(), sum(ref_tax.values()))
    # wealth identity: cash + margin + long market value + short unrealized P&L
    mv = sum(v["qty"] * prices[t] for t, v in ref["positions"].items() if v["qty"] > 0)
    spnl = sum((v["avg"] - prices[t]) * -v["qty"] for t, v in ref["positions"].items() if v["qty"] < 0)
    cmp("wealth", ledger.wealth(prices), ref["cash"] + ref["margin"] + mv + spnl)
    if curve_last is not None:
        cmp("equity curve end", curve_last, ledger.wealth(prices))
    if ledger.cash < -tol:
        bad.append(f"cash went negative: {ledger.cash:,.2f}")
    return bad
