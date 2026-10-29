"""Performance metrics, one definition shared by the backtest, reports and tests.

Conventions (stated once so every number in the README means the same thing):
  - returns are on the platform's starting capital of Rs 10,00,000
  - daily returns are close-to-close changes of total wealth
  - Sharpe / Sortino: mean daily return / stdev (downside stdev) * sqrt(252), risk-free 0
  - max drawdown: largest fall from a running peak of the wealth curve
  - utilization: capital deployed (long market value + short margin) / wealth, daily average
  - two tax bases, always compared like with like:
      platform basis   wealth minus tax on gains *realized* so far (how the platform
                       taxes: unrealized gains are not taxed). Buy & hold realizes
                       nothing, so its platform-basis value is just its final wealth.
      liquidated basis every open position sold at the last close, fees and tax paid
  - benchmark: equal-weight buy & hold of every stock from the first day, Rs 20 per buy
"""
from __future__ import annotations

import math

from .config import BROKERAGE_FEE, DP_CHARGE, LTCG_DAYS, LTCG_EXEMPTION, LTCG_RATE, STCG_RATE

START_CAPITAL = 1_000_000.0


def daily_returns(curve: list[float]) -> list[float]:
    return [curve[i] / curve[i - 1] - 1 for i in range(1, len(curve)) if curve[i - 1] > 0]


def max_drawdown(curve: list[float]) -> float:
    peak, mdd = (curve[0] if curve else 0.0), 0.0
    for w in curve:
        peak = max(peak, w)
        if peak > 0:
            mdd = max(mdd, 1 - w / peak)
    return mdd


def sharpe(rets: list[float]) -> float:
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    sd = math.sqrt(sum((r - m) ** 2 for r in rets) / (len(rets) - 1))
    return m / sd * math.sqrt(252) if sd > 0 else 0.0


def sortino(rets: list[float]) -> float:
    if len(rets) < 2:
        return 0.0
    m = sum(rets) / len(rets)
    dd = math.sqrt(sum(min(r, 0.0) ** 2 for r in rets) / len(rets))
    return m / dd * math.sqrt(252) if dd > 0 else 0.0


def cagr(start: float, end: float, days: int) -> float:
    if start <= 0 or end <= 0 or days <= 0:
        return 0.0
    return (end / start) ** (252 / days) - 1


def buy_and_hold(first: dict[str, float], last: dict[str, float], first_date: str, last_date: str,
                 capital: float = START_CAPITAL) -> dict:
    """Equal-weight buy & hold with platform fees; after-tax value if sold on the last day."""
    from .portfolio import days_held
    if not first:
        return {"final": capital, "after_tax_final": capital}
    alloc = capital / len(first)
    final, gain = 0.0, 0.0
    for t, p0 in first.items():
        qty = int((alloc - BROKERAGE_FEE) // p0)
        cash_left = alloc - qty * p0 - BROKERAGE_FEE
        final += cash_left + qty * last.get(t, p0)
        gain += qty * (last.get(t, p0) - p0)
    sell_fees = len(first) * (BROKERAGE_FEE + DP_CHARGE)
    long_term = days_held(first_date, last_date) > LTCG_DAYS
    tax = (LTCG_RATE * max(0.0, gain - LTCG_EXEMPTION)) if long_term else STCG_RATE * max(0.0, gain)
    return {"final": final, "after_tax_final": final - sell_fees - tax}


def summarize(curve: list[float], util: list[float], trades: list[float], tax: float, fees: float,
              orders: int, rejects: int, bench: dict, capital: float = START_CAPITAL,
              liquidated: float | None = None) -> dict:
    rets = daily_returns(curve)
    final = curve[-1] if curve else capital
    wins = [x for x in trades if x > 0]
    losses = [x for x in trades if x <= 0]
    mdd = max_drawdown(curve)
    ret = final / capital - 1
    return {
        "days": len(curve),
        "final_wealth": round(final, 0),
        "return_pct": round(100 * ret, 2),
        "after_tax_pct": round(100 * ((final - tax) / capital - 1), 2),
        "buy_hold_pct": round(100 * (bench["final"] / capital - 1), 2),
        "excess_vs_buy_hold_pct": round(100 * ((final - tax) - bench["final"]) / capital, 2),
        "liquidated_after_tax_pct": round(100 * ((liquidated if liquidated is not None else final - tax)
                                                 / capital - 1), 2),
        "buy_hold_liquidated_pct": round(100 * (bench["after_tax_final"] / capital - 1), 2),
        "cagr_pct": round(100 * cagr(capital, final, len(curve)), 2),
        "max_drawdown_pct": round(100 * mdd, 2),
        "sharpe": round(sharpe(rets), 2),
        "sortino": round(sortino(rets), 2),
        "calmar": round(cagr(capital, final, len(curve)) / mdd, 2) if mdd > 0 else 0.0,
        "orders": orders,
        "rejects": rejects,
        "fees": round(fees, 0),
        "est_tax": round(tax, 0),
        "closed_trades": len(trades),
        "win_rate_pct": round(100 * len(wins) / len(trades), 1) if trades else 0.0,
        "profit_factor": round(sum(wins) / -sum(losses), 2) if losses and sum(losses) < 0 else 0.0,
        "avg_win": round(sum(wins) / len(wins), 0) if wins else 0.0,
        "avg_loss": round(sum(losses) / len(losses), 0) if losses else 0.0,
        "avg_utilization_pct": round(100 * sum(util) / len(util), 1) if util else 0.0,
    }
