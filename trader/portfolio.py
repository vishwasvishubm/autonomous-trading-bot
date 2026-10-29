"""Local shadow ledger: cash, margin, positions, realized gains and tax estimate.

The live bot keeps this in sync with fills and periodic GET /portfolio calls, so
order sizing never needs a REST round-trip. The backtester uses the same class as
its exchange model, so backtest and live accounting can't diverge.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date

from .config import (BROKERAGE_FEE, DP_CHARGE, LTCG_DAYS, LTCG_EXEMPTION, LTCG_RATE, STCG_RATE)


def to_date(s: str) -> date:
    return date.fromisoformat(str(s)[:10])


def fiscal_year(d: str) -> int:
    """Indian FY: April 1 -> March 31. FY2025 means Apr 2025 - Mar 2026."""
    dt = to_date(d)
    return dt.year if dt.month >= 4 else dt.year - 1


def days_held(entry_date: str, today: str) -> int:
    return (to_date(today) - to_date(entry_date)).days


@dataclass
class Position:
    ticker: str
    qty: int                      # > 0 long, < 0 short
    avg_price: float
    entry_date: str
    entry_day: int                # trading-day index when opened
    peak: float                   # best close since entry (high for long, low for short)
    stop: float = 0.0
    last_buy_date: str = ""       # most recent BUY into this position ("" = entry_date)

    @property
    def is_short(self) -> bool:
        return self.qty < 0


@dataclass
class FyGains:
    st: float = 0.0               # net short-term gains (can be negative)
    lt: float = 0.0               # net long-term gains


@dataclass
class Ledger:
    cash: float = 1_000_000.0
    margin_locked: float = 0.0
    positions: dict[str, Position] = field(default_factory=dict)
    gains: dict[int, FyGains] = field(default_factory=dict)
    fees_paid: float = 0.0
    orders: int = 0
    no_entry_until: dict[str, int] = field(default_factory=dict)   # ticker -> trading day (re-entry cooldown)
    harvest_carry: dict[str, tuple] = field(default_factory=dict)  # ticker -> (stop, peak) across a harvest re-buy

    # ---------------------------------------------------------------- valuation
    def long_value(self, prices: dict[str, float]) -> float:
        return sum(p.qty * prices.get(t, p.avg_price) for t, p in self.positions.items() if p.qty > 0)

    def short_pnl(self, prices: dict[str, float]) -> float:
        return sum((p.avg_price - prices.get(t, p.avg_price)) * -p.qty
                   for t, p in self.positions.items() if p.qty < 0)

    def wealth(self, prices: dict[str, float]) -> float:
        # Matches the gateway (verified with a live 1-share short): the margin was taken
        # out of cash, so a short adds only its unrealized P&L on top of cash + margin.
        return self.cash + self.margin_locked + self.long_value(prices) + self.short_pnl(prices)

    def unrealized(self, ticker: str, price: float) -> float:
        p = self.positions.get(ticker)
        if not p:
            return 0.0
        return (price - p.avg_price) * p.qty

    # ------------------------------------------------------------------ checks
    def validate(self, ticker: str, action: str, qty: int, price: float) -> str | None:
        """Mirror the gateway's rejection rules. Returns an error code or None."""
        pos = self.positions.get(ticker)
        if qty < 1:
            return "INVALID_QUANTITY"
        if action in ("BUY", "SHORT") and self.cash < 0:
            return "ACCOUNT_BLOCKED"
        if action == "BUY" and self.cash < qty * price + BROKERAGE_FEE:
            return "INSUFFICIENT_CAPITAL"
        if action == "SHORT" and self.cash < qty * price + BROKERAGE_FEE:
            return "INSUFFICIENT_MARGIN"
        if action == "SELL" and (not pos or pos.qty < qty):
            return "NO_LONG_POSITION"
        if action == "COVER" and (not pos or -pos.qty < qty):
            return "NO_SHORT_POSITION"
        return None

    # ------------------------------------------------------------------- fills
    def _fy(self, d: str) -> FyGains:
        return self.gains.setdefault(fiscal_year(d), FyGains())

    def apply_fill(self, ticker: str, action: str, qty: int, price: float, d: str, day: int) -> float:
        """Update the ledger for an executed order. Returns realized P&L (pre-tax)."""
        realized = 0.0
        self.fees_paid += BROKERAGE_FEE
        self.orders += 1
        pos = self.positions.get(ticker)
        value = qty * price

        if action == "BUY":
            self.cash -= value + BROKERAGE_FEE
            if pos and pos.qty > 0:
                total = pos.qty + qty
                pos.avg_price = (pos.avg_price * pos.qty + value) / total
                pos.qty = total
                pos.last_buy_date = d
            else:
                self.positions[ticker] = Position(ticker, qty, price, d, day, price)
        elif action == "SELL":
            if not pos or pos.qty <= 0:
                raise ValueError(f"SELL {ticker}: no long position")
            qty = min(qty, pos.qty)
            value = qty * price
            self.cash += value - BROKERAGE_FEE - DP_CHARGE
            self.fees_paid += DP_CHARGE
            realized = (price - pos.avg_price) * qty
            g = self._fy(d)
            if days_held(pos.entry_date, d) > LTCG_DAYS:
                g.lt += realized
            else:
                g.st += realized
            pos.qty -= qty
            if pos.qty == 0:
                del self.positions[ticker]
        elif action == "SHORT":
            # 100% of the trade value moves from available cash into locked margin
            self.cash -= value + BROKERAGE_FEE
            self.margin_locked += value
            if pos and pos.qty < 0:
                total = -pos.qty + qty
                pos.avg_price = (pos.avg_price * -pos.qty + value) / total
                pos.qty = -total
            else:
                self.positions[ticker] = Position(ticker, -qty, price, d, day, price)
        elif action == "COVER":
            if not pos or pos.qty >= 0:
                raise ValueError(f"COVER {ticker}: no short position")
            qty = min(qty, -pos.qty)
            locked = qty * pos.avg_price
            realized = (pos.avg_price - price) * qty
            self.margin_locked -= locked
            self.cash += locked + realized - BROKERAGE_FEE      # margin released +/- P&L
            self._fy(d).st += realized
            pos.qty += qty
            if pos.qty == 0:
                del self.positions[ticker]
        else:
            raise ValueError(action)
        return realized

    # -------------------------------------------------------------------- tax
    def tax_schedule(self) -> dict[int, dict]:
        """Per-FY tax the way the platform files it (Income-tax Act):
          Sec 70  - within a year, excess STCL sets off LTCG (LTCL only sets off LTCG)
          Sec 74  - unabsorbed losses carry forward: STCL vs STCG then LTCG, LTCL vs LTCG
          Sec 112A - first Rs 1,00,000 of LTCG per FY is exempt
        """
        cf_st = cf_lt = 0.0
        out: dict[int, dict] = {}
        for fy in sorted(self.gains):
            g = self.gains[fy]
            st, lt = max(g.st, 0.0), max(g.lt, 0.0)
            stcl, ltcl = max(-g.st, 0.0), max(-g.lt, 0.0)
            use = min(stcl, lt); lt -= use; stcl -= use           # intra-year set-off
            use = min(cf_st, st); st -= use; cf_st -= use         # brought-forward STCL
            use = min(cf_st, lt); lt -= use; cf_st -= use
            use = min(cf_lt, lt); lt -= use; cf_lt -= use         # brought-forward LTCL
            cf_st += stcl
            cf_lt += ltcl
            lt_taxable = max(0.0, lt - LTCG_EXEMPTION)
            out[fy] = {"taxable_st": st, "taxable_lt": lt_taxable, "ltcg_exempt_left": max(0.0, LTCG_EXEMPTION - lt),
                       "tax": STCG_RATE * st + LTCG_RATE * lt_taxable, "cf_stcl": cf_st, "cf_ltcl": cf_lt}
        return out

    def tax_for_fy(self, fy: int) -> float:
        return self.tax_schedule().get(fy, {}).get("tax", 0.0)

    def estimated_tax(self) -> float:
        return sum(v["tax"] for v in self.tax_schedule().values())

    def taxable_gain_fy(self, d: str) -> float:
        """Gains in d's FY that would actually be taxed (after all set-offs)."""
        row = self.tax_schedule().get(fiscal_year(d))
        return (row["taxable_st"] + row["taxable_lt"]) if row else 0.0

    def fy_gains(self, d: str) -> FyGains:
        return self.gains.get(fiscal_year(d), FyGains())

    def sync_tax_from_server(self, report: dict) -> int:
        """Load realized gains per FY from GET /tax/report (survives bot restarts)."""
        n = 0
        for row in report.get("annual_filings") or []:
            label = str(row.get("financial_year", ""))            # e.g. "FY 2024-25"
            try:
                fy = int(label.replace("FY", "").strip()[:4])
            except ValueError:
                continue
            self.gains[fy] = FyGains(st=float(row.get("stcg_gains", 0)) - float(row.get("stcg_losses", 0)),
                                     lt=float(row.get("ltcg_gains", 0)) - float(row.get("ltcg_losses", 0)))
            n += 1
        return n

    # ------------------------------------------------------- server reconcile
    def sync_from_server(self, cash_info: dict, portfolio: dict, d: str, day: int) -> list[str]:
        """Overwrite cash/positions with server truth. Returns human-readable diffs."""
        diffs: list[str] = []
        cash = cash_info.get("available_cash", cash_info.get("capital_remaining"))
        if cash is not None and abs(float(cash) - self.cash) > 1.0:
            diffs.append(f"cash {self.cash:.2f} -> {float(cash):.2f}")
            self.cash = float(cash)
        if "margin_locked" in cash_info:
            self.margin_locked = float(cash_info["margin_locked"] or 0.0)

        seen = set()
        for h in portfolio.get("holdings", []) or []:
            t = h.get("ticker")
            q = int(h.get("quantity", 0))
            if not t or q == 0:
                continue
            if h.get("is_short") and q > 0:
                q = -q
            seen.add(t)
            avg = float(h.get("average_buy_price") or h.get("avg_price") or 0.0)
            pos = self.positions.get(t)
            if pos is None:
                diffs.append(f"{t}: unknown position {q} adopted")
                px = float(h.get("current_price") or avg)
                self.positions[t] = Position(t, q, avg, d, day, px)
            elif pos.qty != q:
                diffs.append(f"{t}: qty {pos.qty} -> {q}")
                pos.qty = q
                pos.avg_price = avg or pos.avg_price
        for t in list(self.positions):
            if t not in seen:
                diffs.append(f"{t}: closed on server (settlement/liquidation)")
                del self.positions[t]
        return diffs
