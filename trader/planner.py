"""Risk manager: turns signals into a short, prioritized list of safe orders.

Priority order (exits always first, so a full request budget can never trap us in
a losing position):
  0. stop-loss / short time-limit / risk-guard exits
  1. signal exits (with LTCG deferral for winners close to 365 days)
  2. tax-loss harvesting near financial year end (March)
  3. new entries and top-ups, best score first, within exposure caps

Every intent is validated against a projected ledger before it is emitted, so the
gateway should never see an order it will reject. Every intent also carries an
`explain` record: the signal, the confidence, how the size was derived and which
risk checks passed or capped it (written to logs/orders.jsonl).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .config import (BROKERAGE_FEE, DP_CHARGE, LTCG_DAYS, LTCG_EXEMPTION, LTCG_RATE, STCG_RATE,
                     StrategyParams)
from .portfolio import Ledger, days_held, fiscal_year, to_date
from .risk import RiskState
from .strategy import Signal


HARVEST_REBUY = 4     # priority of harvest re-buys: must run after the matching SELL fills


@dataclass
class OrderIntent:
    ticker: str
    action: str
    qty: int
    priority: int
    reason: str
    explain: dict = field(default_factory=dict)


def _signal_info(sig: Signal) -> dict:
    f = sig.feats
    return {"score": round(sig.score, 3), "parts": {k: round(v, 3) for k, v in sig.parts.items()},
            "close": f["close"], "rsi": round(f.get("rsi", 0.0), 1), "bb_z": round(f.get("bb_z", 0.0), 2),
            "er": round(f.get("er", 0.0), 2), "atr_pct": round(100 * f.get("atr_pct", 0.0), 2)}


def _short_setup(sig: Signal, p: StrategyParams) -> bool:
    if p.short_style == "rip":
        # sell the rally: price bounced up toward/above its 20-day mean inside a downtrend
        rsi_ok = sig.feats["rsi"] >= p.rip_min_rsi
        z_ok = sig.feats["bb_z"] >= p.rip_min_z
        return (rsi_ok and z_ok) if p.rip_require_both else (rsi_ok or z_ok)
    if p.short_style == "weak":
        # relative loser in a downtrend: short it unless it is already stretched to the downside
        return sig.weak and sig.feats["bb_z"] > p.weak_short_min_z
    return sig.score <= p.short_entry


def update_stops(ledger: Ledger, signals: dict[str, Signal], p: StrategyParams) -> None:
    """Ratchet trailing stops using today's close and ATR."""
    for t, pos in ledger.positions.items():
        sig = signals.get(t)
        if not sig:
            continue
        c, a = sig.feats["close"], sig.feats["atr"]
        if pos.stop <= 0 and t in ledger.harvest_carry:      # harvest re-buy keeps its old stop
            pos.stop, pos.peak = ledger.harvest_carry.pop(t)
        if pos.qty > 0:
            pos.peak = max(pos.peak, c)
            if pos.stop <= 0:
                pos.stop = pos.avg_price - p.stop_atr * a
            pos.stop = max(pos.stop, pos.peak - p.trail_atr * a)   # only ever moves up
        else:
            pos.peak = min(pos.peak, c)
            if pos.stop <= 0:
                pos.stop = pos.avg_price + p.short_stop_atr * a
            pos.stop = min(pos.stop, pos.peak + p.trail_atr * a)   # only ever moves down


def _exit(t: str, pos, action: str, prio: int, reason: str, sig: Signal, check: str) -> OrderIntent:
    return OrderIntent(t, action, abs(pos.qty), prio, reason,
                       {"type": "exit", "signal": _signal_info(sig), "trigger": check,
                        "position": {"qty": pos.qty, "avg": round(pos.avg_price, 2), "stop": round(pos.stop, 2),
                                     "unrealized": round((sig.feats["close"] - pos.avg_price) * pos.qty, 2)}})


def _core_shares(signals: dict[str, Signal], p: StrategyParams) -> dict[str, float]:
    """Each core stock's share of the core (sums to 1). Weak stocks get none."""
    core = [s for s in signals.values() if not s.weak] or list(signals.values())
    if p.core_weighting == "inverse_vol":
        raw = {s.ticker: max(s.feats.get("vol_long", 0.0), 1e-4) ** -p.inv_vol_power for s in core}
    else:
        raw = {s.ticker: 1.0 for s in core}
    total = sum(raw.values()) or 1.0
    return {t: v / total for t, v in raw.items()}


def _core_qty(sig: Signal, wealth: float, n: float, p: StrategyParams, avg_price: float | None = None,
              mult: float = 1.0) -> int:
    """Shares of the passive core this stock may keep (0 = no core). `n` = 1 / the stock's
    share of the core (the number of stocks when the core is split equally).

    Valued at the lower of today's price and our cost, so a winner's core is never
    trimmed just because it went up (that would be rebalancing out of the leaders,
    the opposite of buy & hold)."""
    if p.core_weight <= 0 or n <= 0 or sig.weak or (p.core_regime_filter and sig.parts.get("regime", 0.0) < 0):
        return 0
    ref = min(sig.feats["close"], avg_price) if avg_price else sig.feats["close"]
    qty = int(mult * p.core_weight * wealth / n // ref)
    if p.max_stock_weight < 1.0:            # concentration cap: the core never re-buys above the trim level
        qty = min(qty, int(p.cap_trim_to * wealth // sig.feats["close"]))
    return qty


def _concentration_trim(ledger: Ledger, t: str, pos, sig: Signal, wealth: float, d: str,
                        p: StrategyParams) -> OrderIntent | None:
    """Trim a stock that grew past `max_stock_weight` of wealth back to `cap_trim_to`.

    One stock that ran 4x (PLNX in the live run: 27% of wealth) can undo the whole
    portfolio's gain if it crashes. Tax-aware: a holding older than 365 days is trimmed
    at once (10% LTCG, and the first Rs 1 L per FY is exempt); a younger one waits for
    the 365-day mark unless it is above `cap_hard_weight`, where the risk outweighs
    the 15% STCG."""
    c = sig.feats["close"]
    weight = pos.qty * c / wealth if wealth > 0 else 0.0
    if weight <= p.max_stock_weight:
        return None
    # conservative: long-term only if even the latest purchase is > 365 days old. The platform
    # books P&L at average cost (verified against the SimTest order log); how it dates a partial
    # sale of shares bought on several days could not be verified, so never assume the best case
    long_term = days_held(pos.last_buy_date or pos.entry_date, d) > LTCG_DAYS
    if not long_term and weight < p.cap_hard_weight:
        return None
    qty = pos.qty - int(p.cap_trim_to * wealth // c)
    if qty < 1 or qty * c < p.min_order_value:
        return None
    gain = (c - pos.avg_price) * qty
    row = ledger.tax_schedule().get(fiscal_year(d), {})
    exempt_left = row.get("ltcg_exempt_left", LTCG_EXEMPTION) if row else LTCG_EXEMPTION
    if long_term:
        tax = LTCG_RATE * max(0.0, gain - exempt_left)
        basis = f"long-term, Rs {min(max(gain, 0.0), exempt_left):,.0f} of it inside the Rs 1 L exemption"
    else:
        tax = STCG_RATE * max(0.0, gain)
        basis = f"short-term (above the hard cap {p.cap_hard_weight:.0%})"
    it = _exit(t, pos, "SELL", 1, f"concentration cap: {weight:.0%} of wealth > {p.max_stock_weight:.0%}, "
                                  f"trim to {p.cap_trim_to:.0%} (gain {gain:,.0f}, {basis}, est. tax {tax:,.0f})",
               sig, "concentration")
    it.qty = qty
    it.explain.update({"weight": round(weight, 4), "trim_to": p.cap_trim_to, "gain": round(gain, 2),
                       "long_term": long_term, "ltcg_exempt_left": round(exempt_left, 2), "est_tax": round(tax, 2)})
    return it


def plan(ledger: Ledger, signals: dict[str, Signal], d: str, day: int, p: StrategyParams,
         days_remaining: int | None = None, max_orders: int = 6, risk: RiskState | None = None,
         eligible: set[str] | None = None) -> list[OrderIntent]:
    """`risk` comes from RiskGuard.update(); `eligible` = tickers that passed the
    universe screen (None = all). Ineligible tickers can still be exited, never entered."""
    risk = risk or RiskState()
    prices = {t: s.feats["close"] for t, s in signals.items()}
    wealth = ledger.wealth(prices)
    intents: list[OrderIntent] = []
    closing: set[str] = set()
    n_universe = len(signals)
    # the passive core is spread over the stocks that are not weak (weak ones are short candidates)
    n_core = sum(1 for s in signals.values() if not s.weak) or n_universe
    shares = _core_shares(signals, p)
    core_n = {t: 1.0 / v for t, v in shares.items()}         # per stock, for _core_qty

    # ---- 0/1: exits ----------------------------------------------------------
    for t, pos in ledger.positions.items():
        sig = signals.get(t)
        if not sig:
            continue
        c = sig.feats["close"]
        held_days = day - pos.entry_day
        if risk.flatten:
            intents.append(_exit(t, pos, "SELL" if pos.qty > 0 else "COVER", 0,
                                 "emergency flatten (risk guard)", sig, "risk:flatten"))
            closing.add(t)
            continue
        if pos.qty > 0:
            core = _core_qty(sig, wealth, core_n.get(t, 0.0), p, pos.avg_price, risk.core_mult)
            if risk.core_mult < 1.0 and pos.qty > core and t not in closing:
                it = _exit(t, pos, "SELL", 0, f"risk guard {risk.mode}: core cut to x{risk.core_mult:g}",
                           sig, f"risk:{risk.mode}")
                it.qty = pos.qty - core
                intents.append(it)
                closing.add(t)
                continue
            # checked before the signal exits, whose core-only branches skip the rest (a stop sells more: it wins)
            if p.max_stock_weight < 1.0 and not (pos.stop and c <= pos.stop):
                it = _concentration_trim(ledger, t, pos, sig, wealth, d, p)
                if it:
                    intents.append(it)
                    closing.add(t)
                    continue
            if pos.stop and c <= pos.stop:
                if p.reentry_cooldown_days:
                    ledger.no_entry_until[t] = day + p.reentry_cooldown_days
                if core and pos.qty <= core * 1.25:
                    continue                    # only the core is left: it ignores tactical stops
                it = _exit(t, pos, "SELL", 0, f"stop-loss: close {c:.2f} <= stop {pos.stop:.2f}", sig, "stop")
                if core:
                    it.qty = pos.qty - core
                    it.reason += f" (trim to core {core})"
                    it.explain["core_qty"] = core
                intents.append(it)
                closing.add(t)
                continue
            if sig.score < p.long_exit and held_days >= p.min_hold_days:
                if core and pos.qty <= core * 1.25:
                    continue                    # core position: exits only when the regime turns down
                if p.trend_hold and sig.parts.get("regime", 0.0) > 0 and c > sig.feats["ema_slow"]:
                    continue        # uptrend intact: let the trailing stop decide, not signal noise
                cal = days_held(pos.entry_date, d)
                gain = (c - pos.avg_price) * pos.qty
                if (LTCG_DAYS - p.ltcg_defer_window <= cal <= LTCG_DAYS and gain > 0
                        and sig.score > -0.3):
                    continue        # soft exit deferred: wait for 10% LTCG instead of 15% STCG
                it = _exit(t, pos, "SELL", 1, f"signal exit: score {sig.score:+.2f} < {p.long_exit:+.2f}",
                           sig, "signal")
                if core:
                    it.qty = pos.qty - core
                    it.reason += f" (trim to core {core})"
                    it.explain["core_qty"] = core
                elif sig.weak:
                    it.reason += "; weak stock: out of the core"
                intents.append(it)
                closing.add(t)
            elif (p.core_weight > 0 and not core and sig.parts.get("regime", 0.0) < 0
                  and sig.score < p.long_entry and held_days >= p.min_hold_days):
                why = ("core exit: weak (bottom of the relative-strength ranking, in a downtrend)" if sig.weak
                       else "core exit: confirmed downtrend")
                intents.append(_exit(t, pos, "SELL", 1, why, sig, "weak" if sig.weak else "regime"))
                closing.add(t)
        else:
            if risk.close_shorts:
                intents.append(_exit(t, pos, "COVER", 0, f"risk guard {risk.mode}: closing shorts", sig,
                                     f"risk:{risk.mode}"))
                closing.add(t)
            elif pos.stop and c >= pos.stop:
                intents.append(_exit(t, pos, "COVER", 0, f"short stop: close {c:.2f} >= stop {pos.stop:.2f}",
                                     sig, "stop"))
                closing.add(t)
                if p.reentry_cooldown_days:
                    ledger.no_entry_until[t] = day + p.reentry_cooldown_days
            elif held_days >= p.max_short_hold_days:
                intents.append(_exit(t, pos, "COVER", 0, f"short time limit {held_days}d (settlement risk)",
                                     sig, "time_limit"))
                closing.add(t)
            elif p.short_style == "rip" and sig.feats["bb_z"] <= p.rip_take_profit_z:
                intents.append(_exit(t, pos, "COVER", 1, f"short take-profit: bounce faded (z {sig.feats['bb_z']:+.1f})",
                                     sig, "take_profit"))
                closing.add(t)
            elif p.short_style == "weak" and not sig.weak and held_days >= p.min_hold_days:
                intents.append(_exit(t, pos, "COVER", 1, "short exit: no longer weak (rank or trend recovered)",
                                     sig, "regime"))
                closing.add(t)
            elif p.short_style == "rip" and sig.parts.get("regime", 0.0) > 0:
                intents.append(_exit(t, pos, "COVER", 1, "short exit: downtrend ended", sig, "regime"))
                closing.add(t)
            elif p.short_style != "rip" and sig.score > p.short_exit and held_days >= p.min_hold_days:
                intents.append(_exit(t, pos, "COVER", 1, f"signal exit: score {sig.score:+.2f} > {p.short_exit:+.2f}",
                                     sig, "signal"))
                closing.add(t)

    # ---- 2: tax-loss harvesting in the last weeks of the financial year -------
    # Sec 70/71: a realized loss only saves tax this year if there are *taxable* gains
    # left after all set-offs and carried-forward losses. India has no wash-sale rule,
    # so we SELL to book the loss and immediately BUY the same quantity back at the
    # same close: tax saved, position (and its stop) unchanged, cost ~Rs 55 in fees.
    # (Selling without re-buying saved tax but missed the recovery: worse after tax.)
    dt = to_date(d)
    if dt.month == 3 and dt.day >= p.harvest_from_day and not risk.flatten:
        taxable = ledger.taxable_gain_fy(d)
        losers = sorted(((pos.avg_price - prices[t]) * pos.qty, t) for t, pos in ledger.positions.items()
                        if t not in closing and pos.qty > 0 and t in prices)
        for loss, t in sorted(losers, reverse=True):
            if taxable <= 0:
                break
            saved = STCG_RATE * min(loss, taxable)
            round_trip = 2 * BROKERAGE_FEE + DP_CHARGE
            if loss >= p.harvest_min_loss and saved > 4 * round_trip:
                pos = ledger.positions[t]
                info = {"type": "tax_harvest", "loss": round(loss, 2), "taxable_fy_gains": round(taxable, 2),
                        "tax_saved_est": round(saved, 2), "fees": round(round_trip, 2)}
                intents.append(OrderIntent(t, "SELL", pos.qty, 2,
                                           f"tax-loss harvest: book -{loss:,.0f} vs taxable FY gains "
                                           f"{taxable:,.0f} (saves ~{saved:,.0f})", info))
                intents.append(OrderIntent(t, "BUY", pos.qty, HARVEST_REBUY,
                                           "tax-loss harvest re-buy: keep the position, loss is booked", info))
                ledger.harvest_carry[t] = (pos.stop, pos.peak)
                closing.add(t)
                taxable -= loss

    # ---- 3: entries ----------------------------------------------------------
    winding_down = days_remaining is not None and days_remaining <= p.wind_down_days
    if not winding_down and risk.allow_entries:
        cash = ledger.cash - 2 * BROKERAGE_FEE * len(intents)  # never count on sell proceeds
        long_val = sum(pos.qty * prices.get(t, pos.avg_price) for t, pos in ledger.positions.items()
                       if pos.qty > 0 and t not in closing)
        short_val = sum(-pos.qty * pos.avg_price for t, pos in ledger.positions.items()
                        if pos.qty < 0 and t not in closing)
        reserve = (1.0 - p.max_gross_exposure) * wealth
        # market breadth overlay: when few stocks are above their slow EMA, the market
        # is in risk-off mode and we allow less total long exposure
        breadth = (sum(1 for s in signals.values() if s.feats["close"] > s.feats["ema_trend"])
                   / len(signals)) if signals else 1.0
        long_cap = p.max_long_exposure * (p.breadth_floor + (1.0 - p.breadth_floor) * breadth)

        ranked = sorted(signals.values(), key=lambda s: -abs(s.score))
        for sig in ranked:
            t, c, atr_pct = sig.ticker, sig.feats["close"], max(sig.feats["atr_pct"], 1e-4)
            if t in closing or c <= 0 or ledger.no_entry_until.get(t, -1) > day:
                continue
            if eligible is not None and t not in eligible:
                continue
            pos = ledger.positions.get(t)
            conviction = None
            checks: list[str] = []
            core = (_core_qty(sig, wealth, core_n.get(t, 0.0), p, pos.avg_price if pos else None, risk.core_mult)
                    if sig.score > p.core_min_score else 0)
            core_val = core * c
            if (sig.score < p.long_entry or not p.tactical) and core and (pos is None or pos.qty > 0):
                # passive core only: no tactical signal, but the stock isn't in a downtrend
                conviction, cap, action = 1.0, core_val / wealth, "BUY"
                checks.append(f"core: regime not down, score {sig.score:+.2f} > {p.core_min_score:+.2f}")
            elif p.tactical and sig.score >= p.long_entry and (pos is None or pos.qty > 0) and not sig.weak:
                if pos is None and sig.feats["rsi"] > p.max_entry_rsi:
                    continue                                   # overextended: wait for a pullback
                conviction = 0.5 + 0.5 * (sig.score - p.long_entry) / max(1e-9, 1.0 - p.long_entry)
                cap = p.max_position_pct
                action = "BUY"
                checks.append(f"score {sig.score:+.2f} >= entry {p.long_entry:+.2f}")
            elif (p.enable_shorts and pos is None and _short_setup(sig, p)
                  and (sig.weak or not p.short_weak_only)
                  and breadth <= p.short_max_breadth            # only in a broad bear market
                  and sig.parts.get("regime", 0.0) < 0          # stock in a confirmed downtrend
                  and sig.feats["ema_fast"] < sig.feats["ema_slow"]
                  and sig.parts.get("news", 0.0) <= p.short_news_veto * p.w_news):
                if p.short_style in ("rip", "weak"):
                    conviction = 0.75 if p.short_style == "rip" else 1.0
                else:
                    conviction = 0.5 + 0.5 * (p.short_entry - sig.score) / max(1e-9, 1.0 + p.short_entry)
                cap = p.max_short_pct
                action = "SHORT"
                setup = ("weak stock (bottom relative strength) in a downtrend" if p.short_style == "weak"
                         else f"rip setup (RSI {sig.feats['rsi']:.0f}, z {sig.feats['bb_z']:+.1f}) in downtrend")
                if sig.weak and p.short_style != "weak":
                    setup = "weak stock (bottom relative strength), " + setup
                checks.append(f"{setup}, breadth {breadth:.0%} <= {p.short_max_breadth:.0%}")
            if conviction is None:
                continue

            # volatility sizing: lose ~risk_per_trade of wealth if the initial stop is hit
            stop_atr = p.stop_atr if action == "BUY" else p.short_stop_atr
            risk_w = p.risk_per_trade / (stop_atr * atr_pct)
            target = wealth * min(cap, risk_w) * min(1.0, conviction) * risk.size_mult
            limited = "position cap" if cap < risk_w else "volatility sizing"
            if action == "BUY" and core_val > 0 and core_val * risk.size_mult >= target:
                target, limited = core_val * risk.size_mult, "core weight"
            current = pos.qty * c if pos else 0.0
            add = target - current
            if pos is not None:
                if day - pos.entry_day < p.min_hold_days or add < p.rebalance_band * wealth:
                    continue
            if action == "BUY":
                room = long_cap * wealth - long_val
                checks.append(f"breadth {breadth:.0%} -> long cap {long_cap:.0%} of wealth")
            else:
                room = p.max_gross_exposure * wealth - long_val - short_val
                checks.append(f"gross cap {p.max_gross_exposure:.0%}")
            if room < add:
                add, limited = room, "exposure cap"
            spendable = cash - reserve - BROKERAGE_FEE
            if spendable < add:
                add, limited = spendable, f"cash reserve {1 - p.max_gross_exposure:.0%}"
            if add < p.min_order_value:
                continue
            qty = int(add // c)
            if qty < 1:
                continue
            if risk.size_mult < 1.0:
                checks.append(f"risk guard {risk.mode}: size x{risk.size_mult:g}")
            else:
                checks.append("risk guard normal")
            if ledger.no_entry_until.get(t, -1) >= 0:
                checks.append("re-entry cooldown passed")
            explain = {"type": "entry" if pos is None else "top-up", "signal": _signal_info(sig),
                       "confidence": round(min(1.0, conviction), 3),
                       "sizing": {"target_value": round(target, 0), "order_value": round(qty * c, 0),
                                  "risk_weight": round(min(cap, risk_w), 4), "stop_atr": stop_atr,
                                  "risk_at_stop": round(qty * stop_atr * sig.feats["atr"], 0),
                                  "limited_by": limited},
                       "checks": checks, "reasons": sig.reasons}
            if limited == "core weight":
                spread = (f"spread over {n_core} stocks" if p.core_weighting == "equal" else
                          f"weighted by inverse volatility over {n_core} stocks: {shares.get(t, 0):.1%} here")
                reason = (f"core position: {p.core_weight:.0%} of wealth {spread} "
                          f"(held through dips; score {sig.score:+.2f})")
                explain["type"] = "core"
            else:
                reason = f"{'top-up' if pos else 'entry'} score {sig.score:+.2f}: " + "; ".join(sig.reasons[:3])
            intents.append(OrderIntent(t, action, qty, 3, reason, explain))
            cash -= qty * c + BROKERAGE_FEE
            if action == "BUY":
                long_val += qty * c
            else:
                short_val += qty * c

    intents.sort(key=lambda i: i.priority)
    kept = intents[:max_orders]
    # never keep a harvest SELL whose re-buy was cut by the order cap (would dump the position)
    rebuys = {i.ticker for i in kept if i.priority == HARVEST_REBUY}
    orphan = {i.ticker for i in kept if i.priority == 2 and i.ticker not in rebuys}
    for t in orphan:
        ledger.harvest_carry.pop(t, None)
    return [i for i in kept if not (i.priority == 2 and i.ticker in orphan)]
