"""Unit + end-to-end tests. Run: python -m unittest discover -s tests -v"""
from __future__ import annotations

import json
import sys
import tempfile
import threading
import time
import unittest
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))

from trader.api import OrderResult  # noqa: E402
from trader.config import BROKERAGE_FEE, DP_CHARGE, Config, StrategyParams  # noqa: E402
from trader.engine import Engine  # noqa: E402
from trader.indicators import atr, ema, rsi  # noqa: E402
from trader.lifecycle import HeartbeatWorker  # noqa: E402
from trader.planner import plan, update_stops  # noqa: E402
from trader.portfolio import Ledger, fiscal_year  # noqa: E402
from trader.ratelimit import TokenBucket  # noqa: E402
from trader.sentiment import score_news  # noqa: E402
from trader.strategy import evaluate  # noqa: E402
from trader.indicators import features  # noqa: E402

HIST = ROOT / "data" / "history"


class LedgerTests(unittest.TestCase):
    def test_long_round_trip_fees_and_stcg(self):
        led = Ledger()
        led.apply_fill("A", "BUY", 100, 1000.0, "2024-05-01", 1)
        self.assertAlmostEqual(led.cash, 1_000_000 - 100_000 - BROKERAGE_FEE)
        pnl = led.apply_fill("A", "SELL", 100, 1100.0, "2024-06-01", 20)
        self.assertAlmostEqual(pnl, 10_000)
        self.assertAlmostEqual(led.cash, 1_000_000 + 10_000 - 2 * BROKERAGE_FEE - DP_CHARGE)
        self.assertAlmostEqual(led.estimated_tax(), 1_500)           # 15% STCG
        self.assertEqual(led.positions, {})

    def test_ltcg_exemption_and_loss_setoff(self):
        led = Ledger(cash=5_000_000)
        led.apply_fill("A", "BUY", 1000, 100.0, "2023-04-10", 1)
        led.apply_fill("A", "SELL", 1000, 400.0, "2024-05-10", 300)  # held > 365d: LTCG 3,00,000
        led.apply_fill("B", "BUY", 1000, 100.0, "2024-06-01", 301)
        led.apply_fill("B", "SELL", 1000, 50.0, "2024-07-01", 320)   # STCL -50,000 offsets LTCG
        # LTCG 3,00,000 - 50,000 = 2,50,000; minus 1,00,000 exemption -> 10% of 1,50,000
        self.assertAlmostEqual(led.tax_for_fy(fiscal_year("2024-05-10")), 15_000)

    def test_short_margin_and_wealth(self):
        led = Ledger()
        led.apply_fill("S", "SHORT", 50, 1000.0, "2024-01-02", 1)
        self.assertAlmostEqual(led.margin_locked, 50_000)
        self.assertAlmostEqual(led.cash, 1_000_000 - 50_000 - 20)   # margin leaves cash (live-verified)
        self.assertAlmostEqual(led.wealth({"S": 900.0}), 1_000_000 - 20 + 5_000)
        pnl = led.apply_fill("S", "COVER", 50, 750.0, "2024-01-04", 3)
        self.assertAlmostEqual(pnl, 12_500)
        self.assertAlmostEqual(led.margin_locked, 0)
        self.assertAlmostEqual(led.cash, 1_000_000 + 12_500 - 40)

    def test_loss_carry_forward(self):
        led = Ledger(cash=5_000_000)
        led.apply_fill("A", "BUY", 1000, 100.0, "2024-05-01", 1)
        led.apply_fill("A", "SELL", 1000, 50.0, "2024-06-01", 20)     # FY2024: STCL 50,000
        led.apply_fill("B", "BUY", 1000, 100.0, "2025-05-01", 250)
        led.apply_fill("B", "SELL", 1000, 180.0, "2025-06-01", 270)   # FY2025: STCG 80,000
        sched = led.tax_schedule()
        self.assertAlmostEqual(sched[2024]["tax"], 0)
        self.assertAlmostEqual(sched[2024]["cf_stcl"], 50_000)
        self.assertAlmostEqual(sched[2025]["tax"], 0.15 * 30_000)       # Sec 74 set-off
        self.assertAlmostEqual(led.taxable_gain_fy("2025-07-01"), 30_000)

    def test_tax_sync_from_server_report(self):
        led = Ledger()
        n = led.sync_tax_from_server({"annual_filings": [
            {"financial_year": "FY 2024-25", "stcg_gains": 54890.64, "stcg_losses": 82967.8,
             "ltcg_gains": 0, "ltcg_losses": 0}]})
        self.assertEqual(n, 1)
        self.assertAlmostEqual(led.gains[2024].st, -28077.16, places=2)

    def test_validate_mirrors_gateway(self):
        led = Ledger(cash=10_000)
        self.assertEqual(led.validate("A", "BUY", 10, 1000.0), "INSUFFICIENT_CAPITAL")
        self.assertEqual(led.validate("A", "COVER", 1, 10.0), "NO_SHORT_POSITION")
        self.assertEqual(led.validate("A", "SELL", 1, 10.0), "NO_LONG_POSITION")
        self.assertIsNone(led.validate("A", "BUY", 9, 1000.0))

    def test_fiscal_year(self):
        self.assertEqual(fiscal_year("2025-03-31"), 2024)
        self.assertEqual(fiscal_year("2025-04-01"), 2025)


class IndicatorTests(unittest.TestCase):
    def test_basic_values(self):
        self.assertAlmostEqual(ema([1.0] * 50, 10), 1.0)
        self.assertEqual(rsi([float(i) for i in range(30)], 14), 100.0)
        self.assertGreater(atr([11.0] * 20, [9.0] * 20, [10.0] * 20, 14), 1.9)


TACTICAL = replace(StrategyParams(), core_weight=0.0, tactical=True)     # harvest logic without the passive core


class HarvestTests(unittest.TestCase):
    def _setup(self):
        from trader.portfolio import Position
        from trader.strategy import Signal
        led = Ledger(cash=500_000)
        led.gains[2025] = __import__("trader.portfolio", fromlist=["FyGains"]).FyGains(st=60_000)
        led.positions["L"] = Position("L", 500, 200.0, "2026-01-10", 1, 200.0, stop=150.0)
        feats = {"close": 170.0, "atr": 4.0, "atr_pct": 4 / 170, "ema_trend": 160.0, "ema_fast": 172.0,
                 "ema_slow": 171.0, "bb_z": 0.0, "rsi": 50.0}
        sigs = {"L": Signal("L", 0.2, {"regime": 0.5}, feats)}
        return led, sigs

    def test_sell_and_rebuy_pair(self):
        led, sigs = self._setup()
        out = plan(led, sigs, "2026-03-20", 50, TACTICAL, max_orders=6)
        self.assertEqual([(i.action, i.qty, i.priority) for i in out], [("SELL", 500, 2), ("BUY", 500, 4)])
        self.assertIn("L", led.harvest_carry)

    def test_order_cap_never_orphans_the_sell(self):
        led, sigs = self._setup()
        out = plan(led, sigs, "2026-03-20", 50, TACTICAL, max_orders=1)
        self.assertEqual(out, [])
        self.assertNotIn("L", led.harvest_carry)

    def test_no_harvest_without_taxable_gains(self):
        led, sigs = self._setup()
        led.gains.clear()
        self.assertEqual(plan(led, sigs, "2026-03-20", 50, TACTICAL, max_orders=6), [])


class RegimeTests(unittest.TestCase):
    def test_hysteresis_band(self):
        from trader.strategy import regime_state
        p = replace(StrategyParams(), regime_band_atr=1.5)
        f = lambda c, slope: {"close": c, "ema_trend": 100.0, "trend_slope": slope, "atr": 2.0}
        self.assertEqual(regime_state(None, f(102, 0.1), p), 0.0)    # inside band: undecided
        self.assertEqual(regime_state(None, f(104, 0.1), p), 1.0)    # clears +1.5 ATR, rising
        self.assertEqual(regime_state(1.0, f(98, -0.1), p), 1.0)     # dips under EMA: keeps uptrend
        self.assertEqual(regime_state(1.0, f(96, -0.1), p), -1.0)    # clearly broken and falling
        self.assertEqual(regime_state(-1.0, f(101, 0.1), p), -1.0)   # small bounce: stays down


class SentimentTests(unittest.TestCase):
    def test_direction(self):
        self.assertGreater(score_news("ALPHACORP Q3 NET PROFIT SURGES 24% YOY")[0], 0.5)
        self.assertLess(score_news("BETATECH under investigation for accounting fraud")[0], -0.5)
        self.assertLess(score_news("Company reports net loss, cuts guidance")[0], -0.5)

    def test_news_flood_does_not_saturate(self):
        from trader.sentiment import NewsBook
        book = NewsBook(2.0)
        for _ in range(60):                       # busy day of mildly positive noise
            book.add("A", 0.2, "x", {"A"})
        self.assertAlmostEqual(book.get("A"), 0.2, places=6)
        book.add("B", -0.9, "fraud probe", {"B"})  # one strong headline still counts
        self.assertLess(book.get("B"), -0.25)
        book.new_day()
        self.assertAlmostEqual(book.get("A"), 0.2 * book.decay, places=6)

    def test_negation_softens(self):
        plain = score_news("probe into company")[0]
        negated = score_news("regulator denies probe into company")[0]
        self.assertLess(plain, 0)
        self.assertGreater(negated, plain)


class RateLimitTests(unittest.TestCase):
    def test_bucket_and_penalty(self):
        b = TokenBucket(60, burst=3)
        self.assertTrue(all(b.try_acquire() for _ in range(3)))
        self.assertFalse(b.try_acquire())
        b.penalize(0.2)
        self.assertFalse(b.acquire(0.05))


@unittest.skipUnless(HIST.exists(), "run download_history.py first")
class PlannerTests(unittest.TestCase):
    def test_planner_never_emits_invalid_orders(self):
        hist = {f.stem: json.loads(f.read_text()) for f in HIST.glob("*.json")}
        p = replace(StrategyParams(), enable_shorts=True)
        led = Ledger()
        dates = sorted({b["date"] for v in hist.values() for b in v})[-400:]
        idx = {t: {b["date"]: i for i, b in enumerate(v)} for t, v in hist.items()}
        for day, d in enumerate(dates):
            sigs = {}
            for t, bars in hist.items():
                i = idx[t].get(d)
                if i is None:
                    continue
                f = features(bars[max(0, i - 299):i + 1], p)
                if f:
                    sigs[t] = evaluate(t, f, 0.0, p)
            update_stops(led, sigs, p)
            for it in plan(led, sigs, d, day, p, max_orders=6):
                px = sigs[it.ticker].feats["close"]
                self.assertIsNone(led.validate(it.ticker, it.action, it.qty, px), (d, vars(it)))
                self.assertGreaterEqual(it.qty, 1)
                led.apply_fill(it.ticker, it.action, it.qty, px, d, day)
            self.assertGreaterEqual(led.cash, 0)


class FakeApi:
    """Stands in for the gateway: fills at the requested price, counts calls."""

    def __init__(self, hist):
        self.hist = hist
        self.orders = []
        self.bucket = TokenBucket(100)
        self.stats = {}
        self.server = Ledger()          # the "exchange's" own books
        self.lock = threading.Lock()
        self.delay = 0.0

    def stage(self):
        return {"stage": "simulation", "status": "RUNNING", "tickers": sorted(self.hist), "total_days": 200,
                "days_completed": 0}

    def stocks(self):
        return [{"ticker": t, "name": t} for t in self.hist]

    def history(self, t, days, wait=None):
        return self.hist[t][:-120]

    def cash(self):
        return {"available_cash": self.server.cash, "margin_locked": self.server.margin_locked}

    def tax_report(self):
        return {"annual_filings": [{"financial_year": f"FY {fy}-{(fy + 1) % 100:02d}",
                                   "stcg_gains": max(g.st, 0), "stcg_losses": max(-g.st, 0),
                                   "ltcg_gains": max(g.lt, 0), "ltcg_losses": max(-g.lt, 0)}
                                  for fy, g in self.server.gains.items()]}

    def portfolio(self):
        return {"holdings": [{"ticker": t, "quantity": p.qty, "average_buy_price": p.avg_price,
                              "is_short": p.qty < 0} for t, p in self.server.positions.items()]}

    def heartbeat(self, seq):
        return {"status": "ok"}

    def place_order(self, ticker, action, qty, d):
        if self.delay:
            time.sleep(self.delay)                 # simulated gateway round trip
        with self.lock:                            # orders now arrive from several threads
            self.orders.append((d, ticker, action, qty))
            px = self.last_close[ticker]
            err = self.server.validate(ticker, action, qty, px)
            if err:
                return OrderResult(False, 400, {}, err)
            self.server.apply_fill(ticker, action, qty, px, d, len(self.orders))
            return OrderResult(True, 200, {"status": "FILLED", "execution_price": px,
                                           "capital_remaining": self.server.cash})


class FakeStage:
    info = {"stage": "simulation", "status": "RUNNING"}
    stage = "simulation"
    status = "RUNNING"
    trading_active = True
    days_remaining = None
    on_change = None


class ParallelOrderTests(unittest.TestCase):
    def test_batches_are_concurrent_and_budgeted(self):
        from trader.planner import OrderIntent
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.token = "t"
            cfg.log_dir, cfg.data_dir = Path(tmp) / "logs", Path(tmp) / "data"
            api = FakeApi({t: [] for t in "ABCDE"})
            api.delay = 0.2
            api.last_close = {t: 100.0 for t in "ABCDE"}
            eng = Engine(cfg, api, FakeStage(), HeartbeatWorker(api))
            eng.ledger.cash = api.server.cash = 250_000
            intents = [OrderIntent(t, "BUY", 600, 3, "test") for t in "ABCDE"]   # 5 x 60k > 250k cash
            t0 = time.perf_counter()
            res = eng._execute(intents, api.last_close, "2026-01-05")
            took = time.perf_counter() - t0
            eng.journal.close()
            eng.recorder.close()
        filled = [r for r in res if r["status"] == "FILLED"]
        self.assertEqual(len(filled), 4)                       # 5th skipped locally, never sent
        self.assertEqual([r["status"] for r in res].count("SKIPPED"), 1)
        self.assertEqual(eng.stats["orders_rejected"], 0)
        self.assertLess(took, 0.45, f"4 orders x 200 ms took {took:.2f}s: not parallel")
        self.assertAlmostEqual(eng.ledger.cash, api.server.cash, places=2)


@unittest.skipUnless(HIST.exists(), "run download_history.py first")
class EngineReplayTests(unittest.TestCase):
    def test_end_to_end_replay(self):
        hist = {f.stem: json.loads(f.read_text()) for f in sorted(HIST.glob("*.json"))}
        with tempfile.TemporaryDirectory() as tmp:
            cfg = Config()
            cfg.token = "test-token"
            cfg.log_dir = Path(tmp) / "logs"
            cfg.data_dir = Path(tmp) / "data"
            cfg.decision_delay_ms = 0
            api = FakeApi(hist)
            api.last_close = {}
            eng = Engine(cfg, api, FakeStage(), HeartbeatWorker(api))
            eng.bootstrap()
            dates = sorted({b["date"] for v in hist.values() for b in v[-120:]})
            by = {t: {b["date"]: b for b in v} for t, v in hist.items()}
            t_start = time.perf_counter()
            for d in dates:
                for t in hist:
                    b = by[t].get(d)
                    if b:
                        api.last_close[t] = b["close"]
                        eng.handle("market.ticks", {"event_type": "price_tick", "simulated_date": d,
                                                    "data": {"ticker": t, **b}}, time.monotonic() - 2)
                eng.handle("market.news", {"event_type": "news_flash", "simulated_date": d,
                                           "data": {"ticker": "AERX", "headline": "AERX profit surges 30%",
                                                    "date": d}}, time.monotonic())
                eng.handle("market.ticks", {"event_type": "preflight_tick", "seq": 3}, time.monotonic())
                eng.maybe_decide()
            elapsed = time.perf_counter() - t_start
            self.assertEqual(eng.stats["days"], len(dates))
            self.assertGreater(len(api.orders), 0)
            self.assertEqual(eng.stats["orders_rejected"], 0)
            self.assertEqual(eng.stats["orders_skipped_local"], 0)
            self.assertLess(eng.stats["latency_ms_max"] - 2000, 200, "decision took too long")
            self.assertTrue((cfg.log_dir / "decisions.jsonl").exists())
            diffs = [[x for x in json.loads(l)["diffs"] if not x.startswith("tax:")]
                     for l in (cfg.log_dir / "reconcile.jsonl").read_text().splitlines()]
            self.assertTrue(all(not d for d in diffs), f"ledger drifted from server: {diffs}")
            self.assertAlmostEqual(eng.ledger.cash, api.server.cash, places=2)
            state = json.loads((cfg.log_dir / "state.json").read_text())
            self.assertIn("wealth", state)
            eng.journal.close()          # Windows can't delete the temp dir with open files
            eng.recorder.close()
            print(f"\n  replayed {len(dates)} days in {elapsed:.2f}s, {len(api.orders)} orders, "
                  f"wealth {state['wealth']:,.0f}, avg decide {elapsed / len(dates) * 1000:.1f} ms/day")


if __name__ == "__main__":
    unittest.main()
