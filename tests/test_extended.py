"""Edge-case, failure-recovery, integration and end-to-end tests.

Run everything: python -m unittest discover -s tests -v
"""
from __future__ import annotations

import json
import queue
import random
import sys
import tempfile
import threading
import time
import types
import unittest
from dataclasses import replace
from pathlib import Path

import requests

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
sys.path.insert(0, str(Path(__file__).resolve().parent))

from test_core import FakeApi, FakeStage, HIST  # noqa: E402
from trader.api import GatewayClient, OrderResult  # noqa: E402
from trader.audit import audit_ledger, rebuild_books, reference_tax  # noqa: E402
from trader.config import BROKERAGE_FEE, DP_CHARGE, Config, StrategyParams  # noqa: E402
from trader.engine import Engine  # noqa: E402
from trader.lifecycle import HeartbeatWorker, StageWatcher  # noqa: E402
from trader.metrics import buy_and_hold, max_drawdown, sharpe, sortino  # noqa: E402
from trader.planner import OrderIntent, plan, update_stops  # noqa: E402
from trader.portfolio import FyGains, Ledger, Position  # noqa: E402
from trader.risk import RiskGuard, RiskState  # noqa: E402
from trader.strategy import Signal, cross_section  # noqa: E402
from trader.universe import screen  # noqa: E402


def make_cfg(tmp: str) -> Config:
    cfg = Config()
    cfg.token = "test-token"
    cfg.log_dir, cfg.data_dir, cfg.control_dir = Path(tmp) / "logs", Path(tmp) / "data", Path(tmp)
    cfg.decision_delay_ms = 0
    return cfg


def close(eng: Engine) -> None:
    eng.journal.close()
    eng.recorder.close()


def bars(n=200, start=100.0, drift=0.001, vol=1_000_000, seed=1):
    rnd = random.Random(seed)
    out, px = [], start
    from datetime import date, timedelta
    d = date(2024, 1, 1)
    for _ in range(n):
        d += timedelta(days=1 if d.weekday() < 4 else 3)
        px *= 1 + drift + rnd.uniform(-0.01, 0.01)
        out.append({"date": d.isoformat(), "open": px, "high": px * 1.01, "low": px * 0.99, "close": px,
                    "volume": vol})
    return out


def sig(t, score=0.5, close=100.0, regime=0.5, **f):
    feats = {"close": close, "atr": 2.0, "atr_pct": 0.02, "ema_trend": 95.0, "ema_fast": 101.0, "ema_slow": 99.0,
             "bb_z": 0.0, "rsi": 55.0, "er": 0.4, "mom_long": 0.0, **f}
    return Signal(t, score, {"regime": regime}, feats, ["test"])


# --------------------------------------------------------------------------- tax
class TaxValidationTests(unittest.TestCase):
    def test_ledger_tax_matches_independent_reference_on_random_years(self):
        rnd = random.Random(7)
        for _ in range(300):
            led = Ledger()
            st, lt = {}, {}
            for fy in range(2022, 2022 + rnd.randint(1, 5)):
                st[fy] = rnd.uniform(-200_000, 300_000)
                lt[fy] = rnd.uniform(-150_000, 400_000)
                led.gains[fy] = FyGains(st[fy], lt[fy])
            ref = reference_tax(st, lt)
            for fy, row in led.tax_schedule().items():
                self.assertAlmostEqual(row["tax"], ref[fy], places=6, msg=(st, lt))

    def test_ltcg_boundary_365_vs_366_days(self):
        led = Ledger(cash=10_000_000)
        led.apply_fill("A", "BUY", 100, 100.0, "2024-04-01", 1)
        led.apply_fill("A", "SELL", 100, 200.0, "2025-04-01", 250)          # exactly 365 days: short-term
        led.apply_fill("B", "BUY", 100, 100.0, "2024-04-01", 1)
        led.apply_fill("B", "SELL", 100, 200.0, "2025-04-02", 251)          # 366 days: long-term
        self.assertAlmostEqual(led.gains[2025].st, 10_000)
        self.assertAlmostEqual(led.gains[2025].lt, 10_000)
        self.assertAlmostEqual(led.tax_for_fy(2025), 1_500)                  # LTCG under the 1 L exemption

    def test_short_gains_are_always_short_term(self):
        led = Ledger()
        led.apply_fill("S", "SHORT", 100, 500.0, "2024-05-01", 1)
        led.apply_fill("S", "COVER", 100, 400.0, "2024-05-20", 12)
        self.assertAlmostEqual(led.gains[2024].st, 10_000)
        self.assertAlmostEqual(led.estimated_tax(), 1_500)

    def test_harvest_lowers_tax_and_keeps_the_position(self):
        p = replace(StrategyParams(), core_weight=0.0, tactical=True)
        led = Ledger(cash=2_000_000)
        led.gains[2025] = FyGains(st=80_000)
        led.positions["L"] = Position("L", 1000, 200.0, "2026-01-10", 1, 200.0, stop=100.0)
        before_tax, before_qty = led.estimated_tax(), 1000
        intents = plan(led, {"L": sig("L", 0.2, close=170.0)}, "2026-03-20", 50, p)
        self.assertEqual([(i.action, i.qty) for i in intents], [("SELL", 1000), ("BUY", 1000)])
        cash0 = led.cash
        for it in intents:
            led.apply_fill(it.ticker, it.action, it.qty, 170.0, "2026-03-20", 50)
        self.assertEqual(led.positions["L"].qty, before_qty)
        self.assertAlmostEqual(led.estimated_tax(), before_tax - 0.15 * 30_000)   # 30k loss booked
        self.assertAlmostEqual(cash0 - led.cash, 2 * BROKERAGE_FEE + DP_CHARGE)  # only fees changed hands
        self.assertEqual(intents[0].explain["type"], "tax_harvest")

    def test_after_tax_profit_definition(self):
        led = Ledger()
        led.apply_fill("A", "BUY", 1000, 100.0, "2024-05-01", 1)
        led.apply_fill("A", "SELL", 1000, 150.0, "2024-08-01", 60)
        led.apply_fill("B", "BUY", 1000, 100.0, "2024-08-02", 61)
        w = led.wealth({"B": 120.0})
        after_tax = w - led.estimated_tax()
        self.assertAlmostEqual(after_tax, 1_000_000 + 50_000 + 20_000 - 3 * BROKERAGE_FEE - DP_CHARGE - 7_500)


# -------------------------------------------------------------------- accounting
class AccountingTests(unittest.TestCase):
    def test_random_order_sequences_match_independent_replay(self):
        rnd = random.Random(11)
        for trial in range(40):
            led, fills = Ledger(), []
            prices = {t: 100.0 for t in "ABCD"}
            for day in range(60):
                d = f"2025-{1 + day // 28:02d}-{1 + day % 28:02d}"
                for t in prices:
                    prices[t] *= 1 + rnd.uniform(-0.04, 0.04)
                t = rnd.choice("ABCD")
                pos = led.positions.get(t)
                if pos is None:
                    action = rnd.choice(["BUY", "SHORT"])
                    qty = rnd.randint(1, 500)
                elif pos.qty > 0:
                    action = rnd.choice(["BUY", "SELL"])
                    qty = rnd.randint(1, pos.qty) if action == "SELL" else rnd.randint(1, 300)
                else:
                    action = rnd.choice(["SHORT", "COVER"])
                    qty = rnd.randint(1, -pos.qty) if action == "COVER" else rnd.randint(1, 300)
                px = round(prices[t], 2)
                if led.validate(t, action, qty, px):
                    continue
                led.apply_fill(t, action, qty, px, d, day)
                fills.append({"date": d, "ticker": t, "action": action, "qty": qty, "price": px})
                self.assertGreaterEqual(led.cash, 0)
                self.assertAlmostEqual(led.margin_locked, sum(-p.qty * p.avg_price for p in led.positions.values()
                                                              if p.qty < 0), places=4)
            self.assertEqual(audit_ledger(led, fills, prices), [], f"trial {trial}")
            self.assertAlmostEqual(led.fees_paid, BROKERAGE_FEE * len(fills)
                                   + DP_CHARGE * sum(1 for f in fills if f["action"] == "SELL"), places=6)

    @unittest.skipUnless(HIST.exists(), "needs data/history")
    def test_backtest_books_balance(self):
        from backtest import load, run
        hist = load(ROOT / "data")
        for p in (StrategyParams(), replace(StrategyParams(), core_weight=0.0, tactical=True)):
            m = run(hist, p, "2025-01-01", keep_trace=True)
            self.assertEqual(audit_ledger(m["_ledger"], m["_fills"], m["_prices"], m["_curve"][-1]), [])
            self.assertEqual(m["rejects"], 0)

    def test_server_reconcile_adopts_unknown_and_drops_closed(self):
        led = Ledger()
        led.positions["X"] = Position("X", 10, 50.0, "2025-01-01", 1, 50.0)
        diffs = led.sync_from_server({"available_cash": 990_000.0, "margin_locked": 0},
                                     {"holdings": [{"ticker": "Y", "quantity": 5, "average_buy_price": 20.0}]},
                                     "2025-01-03", 3)
        self.assertEqual(set(led.positions), {"Y"})
        self.assertEqual(led.cash, 990_000.0)
        self.assertTrue(any("Y" in x for x in diffs) and any("X" in x for x in diffs))


# ----------------------------------------------------------------------- metrics
class MetricsTests(unittest.TestCase):
    def test_drawdown_sharpe_sortino(self):
        self.assertAlmostEqual(max_drawdown([100, 120, 90, 130, 117]), 0.25)
        self.assertEqual(max_drawdown([100, 101, 102]), 0.0)
        self.assertEqual(sharpe([0.01] * 10), 0.0)                     # no variance -> defined as 0
        self.assertGreater(sharpe([0.01, -0.005] * 50), 0)
        self.assertLess(sharpe([-0.01, 0.005] * 50), 0)
        self.assertGreater(sortino([0.01, -0.005] * 50), sharpe([0.01, -0.005] * 50))

    def test_buy_and_hold_benchmark(self):
        b = buy_and_hold({"A": 100.0, "B": 50.0}, {"A": 200.0, "B": 50.0}, "2024-01-01", "2024-06-01")
        qa, qb = int((500_000 - 20) // 100), int((500_000 - 20) // 50)
        cash_left = (500_000 - qa * 100 - 20) + (500_000 - qb * 50 - 20)
        self.assertAlmostEqual(b["final"], cash_left + qa * 200 + qb * 50)
        gain = qa * 100
        self.assertAlmostEqual(b["after_tax_final"], b["final"] - 2 * (20 + DP_CHARGE) - 0.15 * gain)


# --------------------------------------------------------------- risk management
class RiskGuardTests(unittest.TestCase):
    def test_drawdown_levels_daily_loss_and_recovery(self):
        p = replace(StrategyParams(), dd_window=5, dd_soft=0.08, dd_hard=0.15, daily_loss_limit=0.03,
                    loss_cooldown_days=2)
        g = RiskGuard(p)
        self.assertEqual(g.update(1_000_000, 0).mode, "normal")
        self.assertEqual(g.update(990_000, 1).mode, "normal")
        self.assertEqual(g.update(960_000, 2).mode, "cooldown")      # -3.0% in one day
        self.assertEqual(g.update(935_000, 3).mode, "cooldown")
        self.assertEqual(g.update(915_000, 4).mode, "reduced")       # 8.5% below the 5-day peak, -2.1% today
        st = g.update(840_000, 5)
        self.assertEqual((st.mode, st.allow_entries, st.close_shorts), ("defensive", False, True))
        for day in range(6, 12):
            st = g.update(840_000, day)                              # the old peak rolls out of the window
        self.assertEqual(st.mode, "normal")

    def test_kill_switch_files(self):
        with tempfile.TemporaryDirectory() as tmp:
            g = RiskGuard(StrategyParams(), Path(tmp))
            self.assertTrue(g.update(1e6, 0).allow_entries)
            (Path(tmp) / "STOP").touch()
            st = g.update(1e6, 1)
            self.assertEqual((st.mode, st.allow_entries, st.flatten), ("halted", False, False))
            (Path(tmp) / "FLATTEN").touch()
            self.assertTrue(g.update(1e6, 2).flatten)

    def test_planner_obeys_risk_state(self):
        p = replace(StrategyParams(), core_weight=0.0, tactical=True)
        led = Ledger()
        led.positions["L"] = Position("L", 100, 100.0, "2025-01-01", 1, 100.0, stop=80.0)
        led.positions["S"] = Position("S", -100, 100.0, "2025-01-01", 1, 100.0, stop=120.0)
        led.cash -= 10_000
        led.margin_locked = 10_000
        sigs = {"L": sig("L", 0.6), "S": sig("S", -0.6, regime=-0.5), "N": sig("N", 0.9)}
        halted = plan(led, sigs, "2025-02-03", 30, p, risk=RiskState(mode="halted", allow_entries=False))
        self.assertFalse([i for i in halted if i.action in ("BUY", "SHORT")])
        defensive = plan(led, sigs, "2025-02-03", 30, p,
                         risk=RiskState(mode="defensive", allow_entries=False, close_shorts=True))
        self.assertIn(("S", "COVER"), [(i.ticker, i.action) for i in defensive])
        flat = plan(led, sigs, "2025-02-03", 30, p, risk=RiskState(mode="flatten", allow_entries=False, flatten=True))
        self.assertEqual(sorted((i.ticker, i.action, i.qty) for i in flat), [("L", "SELL", 100), ("S", "COVER", 100)])
        half = plan(Ledger(), {"N": sig("N", 0.9)}, "2025-02-03", 30, p, risk=RiskState(mode="reduced", size_mult=0.5))
        full = plan(Ledger(), {"N": sig("N", 0.9)}, "2025-02-03", 30, p)
        self.assertAlmostEqual(half[0].qty / full[0].qty, 0.5, delta=0.02)


# ------------------------------------------------------------------- strategy
class StrategyEdgeTests(unittest.TestCase):
    def test_core_is_not_trimmed_when_a_winner_rallies(self):
        p = StrategyParams()                                     # core 80% over 10 stocks
        led = Ledger(cash=840_000)
        # bought the core at 100 (8% of ~Rs 10 L); it doubled and has fallen to its trailing stop
        led.positions["W"] = Position("W", 800, 100.0, "2025-01-01", 1, 260.0, stop=205.0)
        sigs = {f"T{i}": sig(f"T{i}", 0.0) for i in range(9)}
        sigs["W"] = sig("W", -0.2, close=200.0)
        out = plan(led, sigs, "2025-06-02", 100, p)
        self.assertFalse([i for i in out if i.ticker == "W" and i.action == "SELL"])

    def test_tactical_overweight_is_trimmed_back_to_core_on_stop(self):
        p = StrategyParams()
        led = Ledger(cash=100_000)
        led.positions["W"] = Position("W", 2000, 100.0, "2025-01-01", 1, 110.0, stop=98.0)
        sigs = {f"T{i}": sig(f"T{i}", 0.0) for i in range(9)}
        sigs["W"] = sig("W", 0.1, close=97.0)
        out = [i for i in plan(led, sigs, "2025-06-02", 100, p) if i.ticker == "W"]
        self.assertEqual(out[0].action, "SELL")
        self.assertTrue(0 < out[0].qty < 2000)
        self.assertIn("core", out[0].reason)

    def test_relative_strength_tilts_leaders_up(self):
        p = replace(StrategyParams(), w_rs=0.2)
        sigs = {t: sig(t, 0.0, mom_long=m) for t, m in (("A", -2.0), ("B", 0.0), ("C", 3.0))}
        cross_section(sigs, p)
        self.assertAlmostEqual(sigs["C"].score, 0.2)
        self.assertAlmostEqual(sigs["A"].score, -0.2)
        self.assertIn("RS rank 1/3", sigs["C"].reasons)

    def test_no_short_against_positive_news(self):
        p = replace(StrategyParams(), core_weight=0.0, tactical=True, short_max_breadth=1.0)
        s = sig("Z", -0.6, regime=-0.5, ema_fast=95.0, ema_slow=99.0, rsi=65.0, bb_z=0.8)
        self.assertEqual([i.action for i in plan(Ledger(), {"Z": s}, "2025-02-03", 30, p)], ["SHORT"])
        s.parts["news"] = 0.3                                   # bullish headline -> veto
        self.assertEqual(plan(Ledger(), {"Z": s}, "2025-02-03", 30, p), [])


# --------------------------------------------------------------- hedge sleeve (shorts + margin)
def _selloff(weak_close=90.0):
    """10 stocks, 8 below their slow EMA (breadth 20%); only Z is a downtrending relative loser."""
    sigs = {f"T{i}": sig(f"T{i}", 0.0, close=90.0 if i < 7 else 100.0, mom_long=float(i)) for i in range(9)}
    sigs["Z"] = sig("Z", -0.6, close=weak_close, regime=-0.5, ema_fast=95.0, ema_slow=99.0, mom_long=-3.0,
                    rsi=65.0, bb_z=0.8)
    return sigs


class HedgeTests(unittest.TestCase):
    HEDGED = replace(StrategyParams(), **__import__("trader.config", fromlist=["PROFILES"]).PROFILES["hedged"])

    def test_weak_flag_needs_downtrend_bottom_rank_and_selloff(self):
        sigs = _selloff()
        state: dict = {}
        cross_section(sigs, self.HEDGED, state)
        self.assertTrue(sigs["Z"].weak)
        self.assertEqual([t for t, s in sigs.items() if s.weak], ["Z"])
        self.assertIn("weak", sigs["Z"].reasons[-1])
        # healthy market (all stocks above their slow EMA): no new weak flag ...
        calm = _selloff()
        for s in calm.values():
            s.feats["close"] = 100.0
        cross_section(calm, self.HEDGED, {})
        self.assertFalse(calm["Z"].weak)
        # ... but an existing one is kept while the downtrend lasts (hysteresis, no flip-flop)
        calm = _selloff()
        for s in calm.values():
            s.feats["close"] = 100.0
        cross_section(calm, self.HEDGED, state)
        self.assertTrue(calm["Z"].weak)
        # the default profile never flags weak stocks
        d = _selloff()
        cross_section(d, StrategyParams(), {})
        self.assertFalse(any(s.weak for s in d.values()))

    def test_weak_core_is_sold_then_shorted_and_core_moves_to_the_rest(self):
        p = self.HEDGED
        sigs = _selloff()
        cross_section(sigs, p, {})
        led = Ledger(cash=900_000)
        led.positions["Z"] = Position("Z", 1000, 100.0, "2025-01-01", 1, 100.0, stop=60.0)
        out = [i for i in plan(led, sigs, "2025-06-02", 100, p) if i.ticker == "Z"]
        self.assertEqual((out[0].action, out[0].qty), ("SELL", 1000))
        self.assertIn("weak", out[0].reason)
        # next day, no position: the bounce (RSI 65) in a weak stock is shorted, margin locked
        led = Ledger(cash=1_000_000)
        out = plan(led, sigs, "2025-06-03", 101, p, max_orders=10)
        shorts = [i for i in out if i.action == "SHORT"]
        self.assertEqual([i.ticker for i in shorts], ["Z"])
        self.assertIn("weak stock", " ".join(shorts[0].explain["checks"]))
        core = [i for i in out if i.explain.get("type") == "core"]
        self.assertTrue(core and all(i.ticker != "Z" for i in core))
        self.assertIn("over 9 stocks", core[0].reason)
        for i in out:                                           # every order passes the gateway's checks
            self.assertIsNone(led.validate(i.ticker, i.action, i.qty, sigs[i.ticker].feats["close"]))
            led.apply_fill(i.ticker, i.action, i.qty, sigs[i.ticker].feats["close"], "2025-06-03", 101)
        self.assertGreater(led.margin_locked, 0)

    def test_default_profile_keeps_the_core_and_does_not_short(self):
        sigs = _selloff()
        cross_section(sigs, StrategyParams(), {})
        out = plan(Ledger(), sigs, "2025-06-03", 101, StrategyParams(), max_orders=10)
        self.assertFalse([i for i in out if i.action == "SHORT"])
        self.assertTrue([i for i in out if i.ticker == "Z" and i.action == "BUY"])

    @unittest.skipUnless(HIST.exists(), "needs data/history")
    def test_hedged_backtest_shorts_covers_and_balances(self):
        from backtest import WINDOWS, load, run
        m = run(load(ROOT / "data"), self.HEDGED, *WINDOWS["oos_2025_26"], keep_trace=True)
        self.assertGreater(m["short_trades"], 0)
        actions = {f["action"] for f in m["_fills"]}
        self.assertTrue({"SHORT", "COVER"} <= actions)
        self.assertEqual(audit_ledger(m["_ledger"], m["_fills"], m["_prices"], m["_curve"][-1]), [])
        self.assertEqual(m["rejects"], 0)


# --------------------------------------------------------------- explainability
class ExplainabilityTests(unittest.TestCase):
    def test_every_intent_explains_itself(self):
        p = replace(StrategyParams(), core_weight=0.0, tactical=True)
        led = Ledger()
        led.positions["L"] = Position("L", 100, 100.0, "2025-01-01", 1, 100.0, stop=99.0)
        out = plan(led, {"L": sig("L", 0.1, close=98.0), "N": sig("N", 0.8)}, "2025-02-03", 30, p)
        entry = next(i for i in out if i.action == "BUY")
        exit_ = next(i for i in out if i.action == "SELL")
        for k in ("signal", "confidence", "sizing", "checks", "reasons"):
            self.assertIn(k, entry.explain)
        self.assertIn("limited_by", entry.explain["sizing"])
        self.assertGreater(entry.explain["confidence"], 0)
        self.assertEqual(exit_.explain["trigger"], "stop")
        self.assertIn("unrealized", exit_.explain["position"])


# ---------------------------------------------------------------- universe
class UniverseScreenTests(unittest.TestCase):
    def test_good_and_bad_data(self):
        self.assertTrue(screen("OK", bars()).ok)
        self.assertIn("history", screen("NEW", bars(50)).reasons[0])
        self.assertFalse(screen("THIN", bars(vol=100)).ok)                    # turnover ~Rs 10k/day
        b = bars()
        b[-5]["close"] *= 1.8                                                  # bad tick
        self.assertTrue(any("one-day move" in r for r in screen("JUMP", b).reasons))
        b = bars()
        for x in b[-30:]:
            x["volume"] = 0
        self.assertTrue(any("zero-volume" in r for r in screen("DEAD", b).reasons))
        b = bars()
        del b[-40:-25]
        self.assertTrue(any("gap" in r for r in screen("GAP", b).reasons))
        self.assertTrue(any("stale" in r for r in screen("OLD", bars(), as_of="2026-01-01").reasons))
        b = bars(300)
        del b[-40:-25]                                   # our own gap after a mid-run restart
        self.assertTrue(screen("RESTART", b, periodic=True).ok)

    def test_ineligible_ticker_is_never_entered_but_can_be_exited(self):
        p = replace(StrategyParams(), core_weight=0.0, tactical=True)
        led = Ledger()
        led.positions["X"] = Position("X", 100, 100.0, "2025-01-01", 1, 100.0, stop=99.0)
        out = plan(led, {"X": sig("X", 0.9, close=98.0), "Y": sig("Y", 0.9)}, "2025-02-03", 30, p,
                   eligible=set())
        self.assertEqual([(i.ticker, i.action) for i in out], [("X", "SELL")])

    def test_engine_discovers_a_new_listing(self):
        hist = {"AAA": bars(300, seed=1), "BBB": bars(300, seed=2)}
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeApi(hist)
            api.history = lambda t, days, wait=None: hist[t]
            eng = Engine(make_cfg(tmp), api, FakeStage(), HeartbeatWorker(api))
            eng.bootstrap()
            self.assertEqual(eng.eligible, {"AAA", "BBB"})
            hist["NEW"] = bars(40, seed=3)                                     # listed with 40 days of data
            eng.handle("market.ticks", {"event_type": "price_tick", "simulated_date": "2026-01-05",
                                        "data": {"ticker": "NEW", **hist["NEW"][-1]}}, time.monotonic())
            self.assertTrue(eng.refresh_requested)
            eng.load_universe()
            self.assertIn("NEW", eng.universe)
            self.assertNotIn("NEW", eng.eligible)                              # failed the screen
            self.assertFalse(eng.screen_results["NEW"]["ok"])
            close(eng)


# ------------------------------------------------------ order failure handling
class FakeResp:
    def __init__(self, code, body=None):
        self.status_code, self._body = code, body
        self.text = json.dumps(body) if body is not None else ""

    def json(self):
        if self._body is None:
            raise ValueError("no json")
        return self._body


class GatewayFailureTests(unittest.TestCase):
    def client(self, script, order_log=None):
        """script: list of responses/exceptions returned for successive POST /orders."""
        cfg = Config()
        cfg.token = "t"
        c = GatewayClient(cfg)
        self.posts = 0
        log_ = order_log if order_log is not None else []

        def request(method, url, **kw):
            if method == "POST":
                self.posts += 1
                r = script.pop(0)
                if isinstance(r, Exception):
                    raise r
                return r
            return FakeResp(200, {"orders": list(log_)})
        c.session.request = request
        return c

    def now_iso(self):
        from datetime import datetime, timezone
        return datetime.now(timezone.utc).isoformat()

    def test_read_timeout_recovered_from_order_log_without_resending(self):
        log_ = []
        c = self.client([requests.exceptions.ReadTimeout("slow")], log_)
        log_.append({"ticker": "A", "action": "BUY", "quantity": 10, "execution_price": 101.0,
                     "executed_at": "2999-01-01T00:00:00Z"})
        r = c.place_order("A", "BUY", 10, "2025-01-02")
        self.assertEqual((r.ok, r.status, r.recovered, self.posts), (True, "FILLED", True, 1))

    def test_read_timeout_not_in_log_is_unknown_and_never_resent(self):
        c = self.client([requests.exceptions.ReadTimeout("slow"), FakeResp(200, {"status": "FILLED"})])
        r = c.place_order("A", "BUY", 10, "2025-01-02")
        self.assertEqual((r.ok, r.status, self.posts), (False, "UNKNOWN", 1))

    def test_connect_timeout_is_retried_once(self):
        c = self.client([requests.exceptions.ConnectTimeout("no route"),
                         FakeResp(200, {"status": "FILLED", "execution_price": 100.0})])
        r = c.place_order("A", "BUY", 10, "2025-01-02")
        self.assertEqual((r.ok, r.status, self.posts), (True, "FILLED", 2))

    def test_server_error_checks_log(self):
        c = self.client([FakeResp(503, {"error": "UNAVAILABLE"})])
        r = c.place_order("A", "SELL", 5, "2025-01-02")
        self.assertEqual((r.status, self.posts), ("UNKNOWN", 1))

    def test_partial_fill(self):
        c = self.client([FakeResp(200, {"status": "PARTIALLY_FILLED", "filled_quantity": 40,
                                        "execution_price": 99.5})])
        r = c.place_order("A", "BUY", 100, "2025-01-02")
        self.assertEqual((r.ok, r.status, r.filled_qty), (True, "PARTIAL", 40))

    def test_rate_limit_penalizes_bucket(self):
        c = self.client([FakeResp(429, {"error": "RATE_LIMITED"})])
        r = c.place_order("A", "BUY", 1, "2025-01-02")
        self.assertEqual((r.ok, r.status, r.error_code), (False, "REJECTED", "RATE_LIMITED"))
        self.assertEqual(c.stats["rate_limited"], 1)
        self.assertFalse(c.bucket.try_acquire())                           # in the penalty box

    def test_anti_flood_pacing_and_server_block(self):
        from trader.ratelimit import SlidingWindow
        w = SlidingWindow(12, 1.0)
        t0 = time.monotonic()
        for _ in range(24):
            self.assertTrue(w.acquire(5.0))
        took = time.monotonic() - t0
        self.assertGreaterEqual(took, 0.95)              # 24 requests need two 1-second windows
        self.assertLessEqual(w.in_window(), 12)
        c = self.client([FakeResp(429, {"error": "TEAM_TEMPORARILY_BLOCKED",
                                        "message": "excessive API flooding. Unblocking in 2s."})])
        c.place_order("A", "BUY", 1, "2025-01-02")
        self.assertGreater(c.pace.blocked_until - time.monotonic(), 2.5)   # 2 s + 1 s margin
        self.assertFalse(c.pace.acquire(0.1))

    def test_local_validation_never_hits_network(self):
        c = self.client([])
        self.assertEqual(c.place_order("A", "HOLD", 1, None).error_code, "LOCAL_INVALID_ACTION")
        self.assertEqual(c.place_order("A", "BUY", 0, None).error_code, "LOCAL_INVALID_QUANTITY")
        self.assertEqual(self.posts, 0)


class EngineOrderHandlingTests(unittest.TestCase):
    def setUp(self):
        self.tmp = tempfile.TemporaryDirectory()
        self.api = FakeApi({t: [] for t in "ABC"})
        self.api.last_close = {t: 100.0 for t in "ABC"}
        self.eng = Engine(make_cfg(self.tmp.name), self.api, FakeStage(), HeartbeatWorker(self.api))

    def tearDown(self):
        close(self.eng)
        self.tmp.cleanup()

    def test_duplicate_orders_blocked_same_day(self):
        it = [OrderIntent("A", "BUY", 10, 3, "t")]
        self.eng._execute(it, self.api.last_close, "2025-01-02")
        again = self.eng._execute([OrderIntent("A", "BUY", 10, 3, "t")], self.api.last_close, "2025-01-02")
        self.assertEqual(again[0]["error"], "DUPLICATE_BLOCKED")
        self.assertEqual(len(self.api.orders), 1)
        self.eng._execute([OrderIntent("A", "BUY", 10, 3, "t")], self.api.last_close, "2025-01-03")
        self.assertEqual(len(self.api.orders), 2)                          # next day is a new order

    def test_partial_fill_books_only_filled_shares(self):
        self.api.place_order = lambda t, a, q, d: OrderResult(True, 200, {"execution_price": 100.0},
                                                              filled_qty=q // 2, status="PARTIAL")
        res = self.eng._execute([OrderIntent("A", "BUY", 100, 3, "t")], self.api.last_close, "2025-01-02")
        self.assertEqual(res[0]["status"], "PARTIAL")
        self.assertEqual(self.eng.ledger.positions["A"].qty, 50)
        self.assertTrue(self.eng.need_reconcile)

    def test_unknown_outcome_is_not_booked_and_halts_the_day(self):
        self.api.place_order = lambda t, a, q, d: OrderResult(False, 0, {}, "UNKNOWN_OUTCOME", status="UNKNOWN")
        res = self.eng._execute([OrderIntent("A", "BUY", 10, 3, "t")], self.api.last_close, "2025-01-02")
        self.assertEqual(res[0]["status"], "UNKNOWN")
        self.assertNotIn("A", self.eng.ledger.positions)
        self.assertTrue(self.eng.need_reconcile)

    def test_circuit_breaker_after_consecutive_rejects(self):
        self.api.place_order = lambda t, a, q, d: OrderResult(False, 400, {}, "INVALID_TICKER", status="REJECTED")
        self.eng._execute([OrderIntent(t, "BUY", 1, 3, "t") for t in "ABC"], self.api.last_close, "2025-01-02")
        self.assertEqual(self.eng.stats["circuit_breaks"], 1)
        self.assertEqual(self.eng._mode(), "paused")

    def test_rate_limit_reject_pauses_orders(self):
        self.api.place_order = lambda t, a, q, d: OrderResult(False, 429, {}, "RATE_LIMITED", status="REJECTED")
        self.eng._execute([OrderIntent("A", "BUY", 1, 3, "t")], self.api.last_close, "2025-01-02")
        self.assertEqual(self.eng._mode(), "paused")


# --------------------------------------------------------------- error recovery
class FakeKafkaError(Exception):
    pass


class RecoveryTests(unittest.TestCase):
    def test_kafka_disconnect_reconnects_and_keeps_delivering(self):
        made = []

        class Consumer:
            def __init__(self, *topics, **kw):
                made.append(self)
                self.n = len(made)
                self.polls = 0

            def poll(self, timeout_ms=0):
                self.polls += 1
                if self.n == 1:
                    raise FakeKafkaError("broker went away")
                if self.polls == 1:
                    rec = types.SimpleNamespace(topic="market.ticks", value={"event_type": "price_tick"})
                    return {"tp": [rec]}
                time.sleep(0.01)
                return {}

            def close(self):
                pass

        kafka = types.ModuleType("kafka")
        kafka.KafkaConsumer = Consumer
        errors = types.ModuleType("kafka.errors")
        errors.KafkaError = FakeKafkaError
        saved = {k: sys.modules.get(k) for k in ("kafka", "kafka.errors")}
        sys.modules["kafka"], sys.modules["kafka.errors"] = kafka, errors
        try:
            from trader.stream import KafkaFeed
            q: queue.Queue = queue.Queue()
            feed = KafkaFeed(Config(), q, consumers=1)
            feed.start()
            topic, ev, _ = q.get(timeout=5)                          # arrives after the reconnect
            self.assertEqual(topic, "market.ticks")
            self.assertGreaterEqual(len(made), 2)
            self.assertTrue(feed.is_connected)
            feed.close()
        finally:
            for k, v in saved.items():
                if v is None:
                    sys.modules.pop(k, None)
                else:
                    sys.modules[k] = v

    def test_stage_poll_failures_tolerated_briefly(self):
        class Api:
            calls = 0

            def stage(self):
                Api.calls += 1
                if Api.calls > 1:
                    raise requests.exceptions.ReadTimeout("slow")
                return {"stage": "simulation", "status": "RUNNING"}
        w = StageWatcher(Config(), Api())
        w.poll_once()
        w.poll_once()
        w.poll_once()
        self.assertTrue(w.trading_active)                            # last good poll < 10 s ago
        w.last_ok_at -= 11
        self.assertFalse(w.trading_active)                           # stale: stop trading

    def test_reconcile_api_failure_backs_off_and_trading_continues(self):
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeApi({"A": []})
            calls = {"n": 0}

            def boom():
                calls["n"] += 1
                raise requests.exceptions.ConnectionError("down")
            api.cash = boom
            eng = Engine(make_cfg(tmp), api, FakeStage(), HeartbeatWorker(api))
            self.assertFalse(eng.reconcile("resync"))
            self.assertFalse(eng.reconcile("resync"))                # inside back-off: no new request
            self.assertEqual(calls["n"], 1)
            self.assertTrue(eng.need_reconcile)
            close(eng)

    def test_startup_with_gateway_unreachable_recovers(self):
        hist = {"AAA": bars(300, seed=1), "BBB": bars(300, seed=2)}
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeApi(hist)
            api.history = lambda t, days, wait=None: hist[t]
            down = {"on": True}
            real = {n: getattr(api, n) for n in ("stage", "stocks", "cash", "portfolio")}

            def guard(name):
                def call(*a):
                    if down["on"]:
                        raise requests.exceptions.ConnectTimeout("no route to gateway")
                    return real[name](*a)
                return call
            for n in real:
                setattr(api, n, guard(n))
            stage = FakeStage()
            stage.info = {}
            eng = Engine(make_cfg(tmp), api, stage, HeartbeatWorker(api))
            eng.bootstrap()                                   # must not raise
            self.assertEqual(eng.universe, set())
            down["on"] = False                                # network back
            eng.last_universe_try -= 11
            t = threading.Thread(target=eng.run, daemon=True)
            t.start()
            try:
                self.assertTrue(wait_for(lambda: eng.universe == {"AAA", "BBB"} and eng.eligible == {"AAA", "BBB"}))
            finally:
                eng.stop.set()
                t.join(2)
                close(eng)

    @unittest.skipUnless(HIST.exists(), "needs data/history")
    def test_service_restart_resumes_from_server_state(self):
        hist = {f.stem: json.loads(f.read_text()) for f in sorted(HIST.glob("*.json"))}
        dates = sorted({b["date"] for v in hist.values() for b in v[-120:]})
        by = {t: {b["date"]: b for b in v} for t, v in hist.items()}
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeApi(hist)
            api.last_close = {}

            def feed(eng, ds):
                for d in ds:
                    for t in hist:
                        b = by[t].get(d)
                        if b:
                            api.last_close[t] = b["close"]
                            eng.handle("market.ticks", {"event_type": "price_tick", "simulated_date": d,
                                                        "data": {"ticker": t, **b}}, time.monotonic() - 2)
                    eng.maybe_decide()

            first = Engine(make_cfg(tmp), api, FakeStage(), HeartbeatWorker(api))
            first.bootstrap()
            feed(first, dates[:60])
            held = {t: p.qty for t, p in first.ledger.positions.items()}
            self.assertTrue(held)
            close(first)                                             # "crash": local state is gone
            second = Engine(make_cfg(tmp), api, FakeStage(), HeartbeatWorker(api))
            second.bootstrap()
            self.assertEqual({t: p.qty for t, p in second.ledger.positions.items()}, held)
            self.assertEqual(second.eligible, set(hist))         # fresh history: all tradable again
            self.assertAlmostEqual(second.ledger.cash, api.server.cash, places=2)
            self.assertAlmostEqual(second.ledger.estimated_tax(), api.server.estimated_tax(), places=2)
            feed(second, dates[60:])
            self.assertEqual(second.eligible, set(hist), "restart gap must not lock stocks out")
            self.assertEqual(second.stats["orders_rejected"], 0)
            self.assertAlmostEqual(second.ledger.cash, api.server.cash, places=2)
            close(second)


# ------------------------------------------------------------------ end to end
class ChaosApi(FakeApi):
    """Gateway that loses ~10% of responses (order executed, client never told) and
    partially fills ~10% of orders."""

    def __init__(self, hist, seed=3):
        super().__init__(hist)
        self.rnd = random.Random(seed)

    def place_order(self, ticker, action, qty, d):
        roll = self.rnd.random()
        if roll < 0.1 and qty >= 2:
            res = super().place_order(ticker, action, qty // 2, d)
            if res.ok:
                return OrderResult(True, 200, res.data, filled_qty=qty // 2, status="PARTIAL")
            return res
        res = super().place_order(ticker, action, qty, d)
        if roll > 0.9 and res.ok:
            return OrderResult(False, 0, {}, "UNKNOWN_OUTCOME", "read timeout", status="UNKNOWN")
        return res


@unittest.skipUnless(HIST.exists(), "needs data/history")
class ChaosEndToEndTests(unittest.TestCase):
    def test_lost_responses_and_partials_never_desync_or_duplicate(self):
        hist = {f.stem: json.loads(f.read_text()) for f in sorted(HIST.glob("*.json"))}
        dates = sorted({b["date"] for v in hist.values() for b in v[-120:]})
        by = {t: {b["date"]: b for b in v} for t, v in hist.items()}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = make_cfg(tmp)
            api = ChaosApi(hist)
            api.last_close = {}
            eng = Engine(cfg, api, FakeStage(), HeartbeatWorker(api))
            eng.p = replace(eng.p, core_weight=0.0, tactical=True)                 # tactical mode trades more: harder test
            eng.bootstrap()
            for d in dates:
                for t in hist:
                    b = by[t].get(d)
                    if b:
                        api.last_close[t] = b["close"]
                        eng.handle("market.ticks", {"event_type": "price_tick", "simulated_date": d,
                                                    "data": {"ticker": t, **b}}, time.monotonic() - 2)
                eng.maybe_decide()
            self.assertGreater(eng.stats["orders_unknown"] + eng.stats["orders_partial"], 0)
            self.assertEqual(eng.stats["orders_rejected"], 0, "a stale ledger caused a gateway reject")
            keys = [(d, t, a) for d, t, a, _ in api.orders]
            self.assertEqual(len(keys), len(set(keys)), "same order sent twice on one day")
            self.assertTrue(eng.reconcile("final"))
            self.assertAlmostEqual(eng.ledger.cash, api.server.cash, places=2)
            self.assertEqual({t: p.qty for t, p in eng.ledger.positions.items()},
                             {t: p.qty for t, p in api.server.positions.items()})
            rows = [json.loads(x) for x in (cfg.log_dir / "orders.jsonl").read_text().splitlines()]
            self.assertTrue(all("explain" in r for r in rows))
            close(eng)


# ------------------------------------------------------------- pre-flight heartbeat
class HbApi:
    """Records heartbeat ACKs; can be told to fail the next N calls."""

    def __init__(self, fail_next=0, delay=0.0):
        self.acks, self.fail_next, self.delay = [], fail_next, delay
        self.lock = threading.Lock()

    def heartbeat(self, seq):
        if self.delay:
            time.sleep(self.delay)
        with self.lock:
            if self.fail_next:
                self.fail_next -= 1
                raise requests.exceptions.ConnectionError("gateway blip")
            self.acks.append(seq)
        return {"status": "ok"}

    def bot_status(self):
        return {"is_ready": True, "kafka_connected": True, "kafka_consumer_count": 1,
                "heartbeat_received": True, "last_seq": self.acks[-1] if self.acks else None}


def wait_for(cond, timeout=5.0):
    end = time.monotonic() + timeout
    while time.monotonic() < end:
        if cond():
            return True
        time.sleep(0.02)
    return False


class HeartbeatTests(unittest.TestCase):
    def test_every_sequence_number_is_acked_in_order(self):
        api = HbApi()
        hb = HeartbeatWorker(api)
        for seq in range(1, 21):                     # a burst faster than any ACK round trip
            hb.offer(seq)
        hb.ack_pending()
        self.assertEqual(api.acks, list(range(1, 21)))
        self.assertEqual((hb.acked_seq, hb.acked_count, len(hb.pending)), (20, 20, 0))

    def test_duplicates_sent_once_but_a_restarted_preflight_is_acked(self):
        api = HbApi()
        hb = HeartbeatWorker(api)
        for seq in (5, 5, 6, 5, 6, 7):               # same tick read from several partitions
            hb.offer(seq)
        hb.ack_pending()
        self.assertEqual(api.acks, [5, 6, 7])
        hb._seen = type(hb._seen)((s, t - 60) for s, t in hb._seen)   # 60 s later ...
        hb.offer(1)                                  # ... pre-flight restarted from 1
        hb.offer(5)
        hb.ack_pending()
        self.assertEqual(api.acks, [5, 6, 7, 1, 5])

    def test_failed_ack_is_retried_not_skipped(self):
        api = HbApi(fail_next=2)
        hb = HeartbeatWorker(api)
        hb.start()
        try:
            for seq in (1, 2, 3):
                hb.offer(seq)
            self.assertTrue(wait_for(lambda: api.acks == [1, 2, 3], 6.0), api.acks)
            self.assertEqual(hb.failures, 0)
        finally:
            hb.stop.set()

    def test_backlog_is_bounded(self):
        hb = HeartbeatWorker(HbApi())
        for seq in range(200):
            hb.offer(seq)
        self.assertEqual(len(hb.pending), hb.MAX_BACKLOG)
        self.assertEqual(hb.pending[-1], 199)
        self.assertEqual(hb.dropped, 200 - hb.MAX_BACKLOG)

    def test_kafka_preflight_tick_reaches_the_gateway(self):
        """The documented event, end to end: Kafka message -> engine -> heartbeat thread -> POST."""
        api = HbApi(delay=0.05)
        hb = HeartbeatWorker(api)
        hb.start()
        with tempfile.TemporaryDirectory() as tmp:
            fake = FakeApi({"A": []})
            eng = Engine(make_cfg(tmp), fake, FakeStage(), hb)
            try:
                for seq in (14, 15, 16):
                    ev = {"event": "preflight_tick", "event_type": "preflight_tick",
                          "timestamp": "2026-10-20T10:00:00.000Z", "seq": seq,
                          "data": {"status": "STANDBY", "seq": seq}}
                    eng.handle("market.ticks", ev, time.monotonic())
                ev = {"event_type": "preflight_tick", "data": {"status": "STANDBY", "seq": 17}}  # seq only in data
                eng.handle("market.ticks", ev, time.monotonic())
                self.assertTrue(wait_for(lambda: api.acks == [14, 15, 16, 17]), api.acks)
                self.assertTrue(wait_for(lambda: hb.platform_status.get("is_ready") is True))
                self.assertEqual(eng.stats["days"], 0)          # heartbeats never count as trading days
            finally:
                hb.stop.set()
                close(eng)

    def test_one_ack_per_tick_keeps_inside_the_15_second_window(self):
        api = HbApi()
        hb = HeartbeatWorker(api)
        hb.start()
        try:
            t0 = time.monotonic()
            for seq in range(1, 6):
                hb.offer(seq)
                self.assertTrue(wait_for(lambda s=seq: s in api.acks, 2.0))
            self.assertLess(time.monotonic() - t0, 15.0)
        finally:
            hb.stop.set()


# ------------------------------------------------------------------- secrets
class SecretsTests(unittest.TestCase):
    def test_template_and_package(self):
        import package
        text = (ROOT / ".env.example").read_text(encoding="utf-8")
        self.assertIn("BOT_ACCESS_TOKEN=\n", text)
        files = [str(f) for f in package.collect()]
        self.assertNotIn(".env", files)
        self.assertFalse(any(f.startswith("logs") for f in files))
        self.assertEqual(package.scan(package.collect(), extra_secrets=package.live_secrets()), [])

    def test_scanner_catches_planted_secrets(self):
        import package
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            # planted values are built from pieces so this file itself doesn't trip the scanner
            (root / "a.py").write_text('TOKEN = "' + "eyJ" + 'hbGciOiJIUzI1NiJ9.eyJzdWIiOiJ0ZWFtIn0.c2lnbmF0dXJlLXZhbHVl"\n')
            (root / "b.env.example").write_text("BOT_ACCESS_TOKEN=abcdefghijklmnop\n")
            (root / "c.md").write_text("login with " + "Pass" + "word: hunter22\n")
            (root / "d.txt").write_text("the value is SECRET-VALUE-123456 here\n")
            found = package.scan([Path(p) for p in ("a.py", "b.env.example", "c.md", "d.txt")], root,
                                 extra_secrets=["SECRET-VALUE-123456"])
            self.assertEqual(len({f.split(":")[0] for f in found}), 4, found)


if __name__ == "__main__":
    unittest.main()


# --------------------------------------------------------------- concentration cap, data, live replay
class ConcentrationCapTests(unittest.TestCase):
    P = replace(StrategyParams(), max_stock_weight=0.25, cap_trim_to=0.22, cap_hard_weight=0.30)

    def _book(self, qty, entry):
        led = Ledger(cash=100_000)
        led.positions["W"] = Position("W", qty, 50.0, entry, 1, 200.0, stop=100.0)
        sigs = {f"T{i}": sig(f"T{i}", 0.0) for i in range(9)}
        sigs["W"] = sig("W", 0.2, close=200.0)
        for i in range(9):
            led.positions[f"T{i}"] = Position(f"T{i}", 800, 100.0, entry, 1, 100.0, stop=60.0)
        return led, sigs

    def test_long_term_winner_is_trimmed_to_the_target_with_tax_estimate(self):
        led, sigs = self._book(1500, "2024-01-02")      # 3 L of 10.2 L wealth = 29%, held > 1 year
        out = [i for i in plan(led, sigs, "2025-06-02", 300, self.P) if i.ticker == "W"]
        self.assertEqual(out[0].action, "SELL")
        wealth = led.wealth({t: s.feats["close"] for t, s in sigs.items()})
        left = (1500 - out[0].qty) * 200.0 / wealth
        self.assertAlmostEqual(left, self.P.cap_trim_to, delta=0.002)
        self.assertIn("concentration cap", out[0].reason)
        self.assertTrue(out[0].explain["long_term"])
        self.assertEqual(out[0].explain["est_tax"], 0.0)        # the gain fits in the Rs 1 L exemption

    def test_short_term_holding_waits_for_ltcg_unless_above_the_hard_cap(self):
        led, sigs = self._book(1350, "2025-03-01")      # 27%: above the cap, below the 30% hard cap
        self.assertFalse([i for i in plan(led, sigs, "2025-06-02", 300, self.P) if i.ticker == "W"])
        led, sigs = self._book(1800, "2025-03-01")      # 3.6 L of 10.8 L = 33%: trim now, STCG and all
        out = [i for i in plan(led, sigs, "2025-06-02", 300, self.P) if i.ticker == "W"]
        self.assertEqual(out[0].action, "SELL")
        self.assertFalse(out[0].explain["long_term"])
        self.assertGreater(out[0].explain["est_tax"], 0)

    def test_core_never_rebuys_above_the_trim_level(self):
        led, sigs = self._book(1000, "2024-01-02")      # 2 L = 20%, below the cap
        led.cash = 500_000
        out = [i for i in plan(led, sigs, "2025-06-02", 300, self.P) if i.ticker == "W"]
        wealth = led.wealth({t: s.feats["close"] for t, s in sigs.items()})
        bought = sum(i.qty for i in out if i.action == "BUY")
        self.assertLessEqual((1000 + bought) * 200.0 / wealth, self.P.cap_trim_to + 1e-9)

    def test_cap_off_keeps_old_behaviour(self):
        led, sigs = self._book(1500, "2024-01-02")
        p = replace(self.P, max_stock_weight=1.0)
        self.assertFalse([i for i in plan(led, sigs, "2025-06-02", 300, p) if i.ticker == "W" and i.action == "SELL"])


class DataAndReplayTests(unittest.TestCase):
    @staticmethod
    def _ticks(path, days, price, start=0):
        from datetime import date, timedelta
        with path.open("a", encoding="utf-8") as fh:
            for i in range(days):
                d = (date(2025, 1, 1) + timedelta(days=start + i)).isoformat()
                fh.write(json.dumps({"event_type": "price_tick", "simulated_date": d,
                                     "data": {"ticker": "A", "open": price, "high": price, "low": price,
                                              "close": price + i, "volume": 1000}}) + "\n")

    def test_recorded_runs_are_saved_and_only_consistent_ones_merged(self):
        from trader.datastore import merge_stream_into_history
        with tempfile.TemporaryDirectory() as tmp:
            root = Path(tmp)
            (root / "stream").mkdir()
            (root / "history").mkdir()
            ticks = root / "stream" / "ticks.jsonl"
            self._ticks(ticks, 30, 100.0)                 # run 1: new dataset, merged
            self._ticks(ticks, 25, 500.0)                 # run 2: same dates, other prices: kept apart
            self._ticks(ticks, 5, 100.0)                  # too short to be a run
            notes = merge_stream_into_history(root)
            self.assertEqual(len(notes), 2)
            self.assertIn("30 new bars merged", notes[0])
            self.assertIn("different dataset", notes[1])
            merged = json.loads((root / "history" / "A.json").read_text())
            self.assertEqual(len(merged), 30)
            self.assertEqual(merged[0]["close"], 100.0)
            self.assertEqual(len(list((root / "runs").iterdir())), 2)
            self.assertIn("0 new bars", merge_stream_into_history(root)[0])     # idempotent

    @unittest.skipUnless(HIST.exists(), "needs data/history")
    def test_scorecard_matches_the_platform_identity_and_episodes_cover_the_data(self):
        from backtest import WINDOWS, episode_starts, load, run
        hist = load(ROOT / "data")
        m = run(hist, StrategyParams(), *WINDOWS["sim_2024_26"], observe_days=1)
        sc = m["scorecard"]
        # wealth = capital + realized - charges + unrealized (how the portal's columns add up)
        self.assertAlmostEqual(sc["total_wealth"], 1_000_000 + sc["realized"] - sc["charges"] + sc["unrealized"],
                               delta=1.0)
        self.assertEqual(sc["errors"], 0)
        self.assertGreater(m["news_days"], 400)                 # the recorded live headlines were replayed
        eps = episode_starts(hist, 500, 10)
        self.assertGreater(len(eps), 20)
        self.assertTrue(all(e > s for s, e in eps))

    def test_failed_history_refetch_keeps_loaded_bars(self):
        hist = {"AAA": bars(300, seed=1), "BBB": bars(300, seed=2)}
        with tempfile.TemporaryDirectory() as tmp:
            api = FakeApi(hist)
            api.history = lambda t, days, wait=None: hist[t]
            eng = Engine(make_cfg(tmp), api, FakeStage(), HeartbeatWorker(api))
            eng.bootstrap()
            self.assertEqual(len(eng.bars["AAA"]), 300)

            def flaky(t, days, wait=None):
                if t == "AAA":
                    raise ApiErrorForTest(0, "LOCAL_RATE_BUDGET", "skipped")
                return hist[t]
            api.history = flaky
            eng.warm_start()                                    # e.g. the reset at pre-flight -> simulation
            self.assertEqual(len(eng.bars["AAA"]), 300)
            self.assertIn("AAA", eng.eligible)
            close(eng)


from trader.api import ApiError as ApiErrorForTest  # noqa: E402


class PreflightSwitchTests(unittest.TestCase):
    """Live SimTest: day 1 arrived while the stage still said pre-flight; the bot only watched it."""

    class Stage:
        def __init__(self):
            self.info = {"stage": "preflight", "status": "PREFLIGHT", "interval_seconds": 4}
            self.stage, self.status, self.trading_active, self.days_remaining = "preflight", "PREFLIGHT", False, None
            self.on_change = None

        def go_live(self):
            self.info.update(stage="simulation", status="RUNNING")
            self.stage, self.status, self.trading_active = "simulation", "RUNNING", True

    def _engine(self, tmp, hist):
        api = FakeApi(hist)
        api.last_close = {}
        stage = self.Stage()
        cfg = make_cfg(tmp)
        cfg.news_quiet_ms = 0
        eng = Engine(cfg, api, stage, HeartbeatWorker(api))
        eng.bootstrap()
        return eng, api, stage

    def _day(self, eng, api, hist, i, age):
        d = hist["AAA"][i]["date"]
        for t, bs in hist.items():
            api.last_close[t] = bs[i]["close"]
            eng.handle("market.ticks", {"event_type": "price_tick", "simulated_date": d, "data": {"ticker": t, **bs[i]}},
                       time.monotonic() - age)
        eng.maybe_decide()
        return d

    def test_observed_day_is_traded_after_the_switch_without_redownloading(self):
        hist = {t: bars(300, seed=k) for k, t in enumerate(("AAA", "BBB", "CCC"))}
        with tempfile.TemporaryDirectory() as tmp:
            eng, api, stage = self._engine(tmp, hist)
            d = self._day(eng, api, hist, 180, age=1.0)          # day 1, still pre-flight
            self.assertEqual(api.orders, [])
            self.assertIsNotNone(eng.observed)
            calls = []
            api.history = lambda t, days, wait=None: calls.append(t) or hist[t][:-120]
            stage.go_live()
            eng._on_stage_change("preflight", "simulation")
            eng.apply_refresh()
            self.assertEqual(calls, [])                           # history kept, nothing re-downloaded
            self.assertTrue(api.orders and all(o[0] == d for o in api.orders))
            self.assertEqual(len({o[1] for o in api.orders}), 3)  # whole core on day 1
            self.assertEqual(eng.day, 1)
            close(eng)

    def test_observed_day_that_is_over_is_not_traded_late(self):
        hist = {t: bars(300, seed=k) for k, t in enumerate(("AAA", "BBB", "CCC"))}
        with tempfile.TemporaryDirectory() as tmp:
            eng, api, stage = self._engine(tmp, hist)
            self._day(eng, api, hist, 180, age=5.0)               # older than one 4 s day
            stage.go_live()
            eng._on_stage_change("preflight", "simulation")
            eng.apply_refresh()
            self.assertEqual(api.orders, [])
            close(eng)

    def test_stage_polls_are_never_skipped_by_the_local_budget(self):
        cfg = Config()
        cfg.token = "t"
        api = GatewayClient(cfg)
        api.bucket.penalize(60)                                   # budget exhausted / paused
        sent = []
        api.session.request = lambda m, url, **kw: sent.append(url) or FakeResp(200, {"stage": "simulation"})
        self.assertEqual(api.stage()["stage"], "simulation")
        self.assertTrue(sent)


class CashReserveTests(unittest.TestCase):
    def test_default_keeps_only_a_small_fee_reserve(self):
        p = StrategyParams()
        self.assertGreaterEqual(p.max_gross_exposure, 0.99)
        sigs = {f"T{i}": sig(f"T{i}", 0.0, close=100.0 + i) for i in range(10)}
        led = Ledger()
        out = plan(led, sigs, "2025-06-02", 1, p, max_orders=10)
        for i in out:
            led.apply_fill(i.ticker, i.action, i.qty, sigs[i.ticker].feats["close"], "2025-06-02", 1)
        self.assertEqual(len(out), 10)
        self.assertLess(led.cash, 0.02 * 1_000_000)               # >= 98% invested on day 1
        self.assertGreater(led.cash, 0)


class ConservativeHoldingPeriodTests(unittest.TestCase):
    def test_a_recent_top_up_makes_the_cap_trim_wait(self):
        p = replace(StrategyParams(), max_stock_weight=0.25, cap_trim_to=0.22, cap_hard_weight=0.30)
        led = Ledger(cash=100_000)
        led.apply_fill("W", "BUY", 1000, 50.0, "2024-01-02", 1)
        led.apply_fill("W", "BUY", 450, 60.0, "2025-03-01", 280)      # top-up 3 months ago
        for i in range(9):
            led.positions[f"T{i}"] = Position(f"T{i}", 800, 100.0, "2024-01-02", 1, 100.0, stop=60.0)
        sigs = {f"T{i}": sig(f"T{i}", 0.0) for i in range(9)}
        sigs["W"] = sig("W", 0.2, close=200.0)                          # 29 % of wealth, below the hard cap
        led.positions["W"].stop = 100.0
        self.assertEqual(led.positions["W"].last_buy_date, "2025-03-01")
        self.assertFalse([i for i in plan(led, sigs, "2025-06-02", 300, p) if i.ticker == "W"])


class CoreWeightingTests(unittest.TestCase):
    def test_inverse_vol_gives_calm_stocks_more_and_equal_is_default(self):
        from trader.planner import _core_shares
        sigs = {"CALM": sig("CALM", 0.0, vol_long=0.01), "WILD": sig("WILD", 0.0, vol_long=0.04)}
        eq = _core_shares(sigs, StrategyParams())
        self.assertEqual(eq, {"CALM": 0.5, "WILD": 0.5})
        iv = _core_shares(sigs, replace(StrategyParams(), core_weighting="inverse_vol"))
        self.assertAlmostEqual(sum(iv.values()), 1.0)
        self.assertAlmostEqual(iv["CALM"], 0.8)
