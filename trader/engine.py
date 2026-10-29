"""Decision engine: one thread, one portfolio state, one decision per simulated day.

Timeline of a simulated day (3-5 s of wall clock):
  T+0      price_tick for every stock arrives (spread over 10 partitions)
  T+150ms  news_flash items arrive
  T+~450ms all ticks in and news window passed -> decide + send orders
  ...      idle until the next day's ticks
"""
from __future__ import annotations

import logging
import queue
import threading
import time
from collections import deque
from concurrent.futures import ThreadPoolExecutor
from datetime import date

import requests

from .api import ApiError, GatewayClient
from .config import BROKERAGE_FEE, Config
from .indicators import features
from .journal import Journal
from .lifecycle import HeartbeatWorker, StageWatcher
from .planner import HARVEST_REBUY, plan, update_stops
from .portfolio import Ledger
from .risk import RiskGuard
from .sentiment import NewsBook, score_news
from .strategy import cross_section, evaluate
from .universe import screen

log = logging.getLogger("engine")
MAX_BARS = 300

# gateway rejections -> how the engine reacts
PAUSE_ON = {"ACCOUNT_NOT_VERIFIED": 60, "UNAUTHORIZED": 60, "INVALID_TOKEN": 60, "TOKEN_EXPIRED": 60,
            "SIMULATION_NOT_RUNNING": 5, "RATE_LIMITED": 30, "HTTP_429": 30}
AUTH_ERRORS = {"UNAUTHORIZED", "INVALID_TOKEN", "TOKEN_EXPIRED", "HTTP_401", "HTTP_403"}
RESYNC_ON = {"INSUFFICIENT_CAPITAL", "INSUFFICIENT_MARGIN", "ACCOUNT_BLOCKED", "NO_SHORT_POSITION",
             "NO_LONG_POSITION", "INSUFFICIENT_SHARES", "INSUFFICIENT_HOLDINGS"}


class Engine:
    def __init__(self, cfg: Config, api: GatewayClient, stage: StageWatcher, heartbeat: HeartbeatWorker):
        self.cfg, self.api, self.stage, self.hb = cfg, api, stage, heartbeat
        self.p = cfg.params
        self.q: "queue.Queue[tuple[str, dict, float]]" = queue.Queue()
        self.journal = Journal(cfg.log_dir)
        self.recorder = Journal(cfg.data_dir / "stream")
        self.stop = threading.Event()

        self.universe: set[str] = set()
        self.names: dict[str, str] = {}
        self.bars: dict[str, list[dict]] = {}
        self.news = NewsBook(self.p.news_half_life_days)
        self.news_day: str | None = None
        self.regimes: dict[str, float] = {}           # hysteresis state per ticker
        self.observed: tuple[str, dict] | None = None # last day decided without trading (pre-flight)
        self.switch_from = ""
        self.weak: dict[str, bool] = {}               # weak-stock flag memory (cross_section)
        self.pool = ThreadPoolExecutor(max_workers=max(6, min(10, cfg.max_orders_per_day)), thread_name_prefix="order")
        self.ledger = Ledger()
        self.guard = RiskGuard(self.p, cfg.control_dir)
        self.eligible: set[str] = set()              # passed the universe screen (may be entered)
        self.screen_results: dict[str, dict] = {}
        self.sent_today: tuple[str | None, set] = (None, set())   # duplicate-order guard
        self.consecutive_rejects = 0
        self.last_discovery = 0.0
        self.last_universe_try = time.monotonic()
        self.last_warm_try = time.monotonic()
        self.day = 0
        self.last_date: str | None = None
        self.pending: dict[str, dict] = {}
        self.last_signals: dict = {}
        self.recent_orders: deque = deque(maxlen=50)
        self.recent_news: deque = deque(maxlen=30)

        self.orders_paused_until = 0.0
        self.need_reconcile = True
        self.last_reconcile_day = -10**9
        self.reconcile_retry_at = 0.0
        self.token_ok: bool | None = None
        self.news_last: dict[str, float] = {}      # date -> arrival time of latest news item
        self.refresh_requested = False
        self.reset_requested = False
        self.stats = {"days": 0, "orders_sent": 0, "orders_filled": 0, "orders_rejected": 0,
                      "orders_skipped_local": 0, "news": 0, "late_ticks": 0, "latency_ms_avg": 0.0,
                      "latency_ms_max": 0.0, "loops_detected": 0, "orders_partial": 0, "orders_unknown": 0,
                      "orders_duplicate_blocked": 0, "circuit_breaks": 0, "new_tickers": 0}
        stage.on_change = self._on_stage_change

    # ---------------------------------------------------------------- startup
    def bootstrap(self) -> None:
        self.verify_token()
        self.load_universe()
        self.warm_start()
        self.reconcile("startup")

    def verify_token(self) -> None:
        """One cheap authenticated call so a bad token is obvious at startup."""
        if not self.cfg.token:
            self.token_ok = False
            return
        try:
            info = self.api.cash()
            self.token_ok = True
            log.info("token OK: available cash %s", info.get("available_cash", info.get("capital_remaining")))
        except ApiError as exc:
            self.token_ok = exc.code.upper() not in AUTH_ERRORS
            if not self.token_ok:
                log.error("BOT_ACCESS_TOKEN rejected (%s %s): copy the token again from the portal "
                          "(Participant Guide, Step 1) into .env", exc.status, exc.code)
        except requests.RequestException as exc:
            log.warning("token check failed (network): %s", exc)

    def load_universe(self) -> None:
        stocks: list[dict] = []
        if self.cfg.token:
            try:
                stocks = self.api.stocks()
            except (ApiError, requests.RequestException) as exc:
                log.warning("GET /stocks failed (%s); falling back to /stage tickers", exc)
        old = set(self.universe)
        if stocks:
            self.universe = {s["ticker"] for s in stocks if s.get("ticker")}
            self.names = {s["ticker"]: s.get("name", "") for s in stocks}
        else:
            info = self.stage.info
            if not info:
                try:
                    info = self.api.stage()
                except (ApiError, requests.RequestException, ValueError) as exc:
                    # gateway unreachable at startup: keep running (Kafka + heartbeats don't
                    # need it) and let run() retry the universe every 10 s
                    log.warning("stock universe unavailable (%s): retrying every 10 s", exc.__class__.__name__)
                    info = {}
            self.universe = set(info.get("tickers") or []) or self.universe
        log.info("universe (%d): %s", len(self.universe), ", ".join(sorted(self.universe)))
        added = self.universe - old
        if old and added:
            # dynamic discovery: a stock listed mid-run gets history + the screen before any money
            self.stats["new_tickers"] += len(added)
            log.info("new tickers discovered: %s", ", ".join(sorted(added)))
            self.warm_start(sorted(added))

    def screen_universe(self, tickers=None, periodic: bool = False) -> None:
        for t in tickers or sorted(self.universe):
            r = screen(t, self.bars.get(t, []), self.last_date, periodic=periodic)
            self.screen_results[t] = {"ok": r.ok, "reasons": r.reasons, "stats": r.stats}
            if r.ok:
                self.eligible.add(t)
            else:
                self.eligible.discard(t)
                log.warning("screen: %s not tradable for new entries: %s", t, "; ".join(r.reasons))
        self.journal.write("screen", {"date": self.last_date, "results": self.screen_results})

    def warm_start(self, tickers=None) -> None:
        """Pre-load indicator history so we can trade from day 1 (public endpoint, fetched in parallel)."""
        def fetch(t: str) -> tuple[str, list[dict]]:
            try:
                # wait for the request budget instead of skipping: live, at the switch to the
                # simulation, heartbeats had drained it and 5 of 10 downloads were skipped
                return t, self.api.history(t, self.cfg.history_days, wait=8.0)
            except (ApiError, requests.RequestException, ValueError) as exc:
                log.warning("history %s failed: %s", t, exc)
                return t, []

        with ThreadPoolExecutor(max_workers=5) as pool:
            for t, bars in pool.map(fetch, tickers or sorted(self.universe)):
                fresh = [self._bar(b) for b in bars if b.get("close")][-MAX_BARS:]
                if fresh or t not in self.bars:
                    self.bars[t] = fresh
                elif self.bars[t]:
                    # a failed re-download never throws away history we already have (live, that left
                    # 5 stocks with 0 bars and untradable for ~5 simulated days)
                    log.warning("history %s: download failed, keeping the %d bars already loaded", t, len(self.bars[t]))
        sizes = {t: len(b) for t, b in self.bars.items()}
        log.info("warm start bars: %s", sizes)
        self.screen_universe(tickers)

    def reconcile(self, why: str) -> bool:
        # dry run = paper trading on the local ledger; the server account isn't ours to mirror
        if not self.cfg.token or self.cfg.dry_run or self.token_ok is False:
            return False
        if self.api.bucket.available() < 3 or time.monotonic() < self.reconcile_retry_at:
            return False
        try:
            cash = self.api.cash()
            pf = self.api.portfolio()
        except (ApiError, requests.RequestException, ValueError) as exc:
            self.reconcile_retry_at = time.monotonic() + 30.0      # back off, don't burn the budget
            if isinstance(exc, ApiError) and exc.code.upper() in AUTH_ERRORS:
                self.token_ok = False
                log.error("token rejected during reconcile (%s): orders disabled until restart", exc.code)
            else:
                log.warning("reconcile (%s) failed: %s (retry in 30s)", why, exc)
            return False
        diffs = self.ledger.sync_from_server(cash, pf, self.last_date or date.today().isoformat(), self.day)
        self.need_reconcile = False
        self.last_reconcile_day = self.day
        if why != "scheduled":
            # realized gains per FY drive loss harvesting; take them from the platform's
            # own tax engine so a restart doesn't forget earlier trades
            try:
                n = self.ledger.sync_tax_from_server(self.api.tax_report())
                diffs.append(f"tax: {n} FY filings loaded, est tax {self.ledger.estimated_tax():,.0f}")
            except (ApiError, requests.RequestException, ValueError) as exc:
                log.warning("tax report sync failed: %s", exc)
        if diffs:
            log.info("reconcile (%s): %s", why, "; ".join(diffs))
        self.journal.write("reconcile", {"why": why, "diffs": diffs, "cash": self.ledger.cash,
                                         "margin": self.ledger.margin_locked,
                                         "server_wealth": pf.get("total_wealth")})
        return True

    # --------------------------------------------------------------- messages
    @staticmethod
    def _bar(b: dict) -> dict:
        return {"date": str(b.get("date"))[:10], "open": float(b.get("open") or b["close"]),
                "high": float(b.get("high") or b["close"]), "low": float(b.get("low") or b["close"]),
                "close": float(b["close"]), "volume": float(b.get("volume") or 0)}

    def _roll_news_day(self, d: str) -> None:
        if self.news_day is None or d > self.news_day:
            if self.news_day is not None:
                self.news.new_day()
            self.news_day = d
        elif d < self.news_day:            # sandbox looped back to day 1
            self.news = NewsBook(self.p.news_half_life_days)
            self.news_day = d

    def handle(self, topic: str, ev: dict, t_recv: float) -> None:
        lag_ms = (time.monotonic() - t_recv) * 1000      # time spent waiting in our queue
        if lag_ms > self.stats.get("queue_lag_ms_max", 0.0):
            self.stats["queue_lag_ms_max"] = round(lag_ms, 1)
        et = ev.get("event_type") or ev.get("event")
        data = ev.get("data") or {}
        if et == "preflight_tick":
            seq = ev.get("seq", data.get("seq"))
            if seq is not None:
                self.hb.offer(int(seq))
            return
        d = str(ev.get("simulated_date") or data.get("date") or "")[:10]
        if et == "price_tick" or (topic == "market.ticks" and "close" in data):
            self.recorder.write("ticks", ev)
            t = data.get("ticker")
            if t and self.universe and t not in self.universe and time.monotonic() - self.last_discovery > 30:
                self.last_discovery = time.monotonic()
                self.refresh_requested = True          # unknown ticker on the feed: re-read GET /stocks
            try:
                bar = self._bar({**data, "date": d})
            except (KeyError, TypeError, ValueError):
                log.warning("malformed tick ignored: %s", ev)
                return
            if not t or not d or bar["close"] <= 0:
                return
            if self.last_date and d == self.last_date:
                # day already decided: too late to trade on, but keep the bar so
                # indicators never have gaps
                self.stats["late_ticks"] += 1
                series = self.bars.setdefault(t, [])
                if not series or series[-1]["date"] < d:
                    series.append(bar)
                    del series[:-MAX_BARS]
                return
            self._roll_news_day(d)
            slot = self.pending.setdefault(d, {"first": t_recv, "bars": {}})
            slot["bars"][t] = bar
        elif et == "news_flash" or topic == "market.news":
            self.recorder.write("news", ev)
            if d:
                self._roll_news_day(d)
                self.news_last[d] = t_recv
            headline = data.get("headline", "")
            s, hits = score_news(headline, data.get("description", ""))
            self.news.add(data.get("ticker"), s, headline, self.universe)
            self.stats["news"] += 1
            item = {"date": d, "ticker": data.get("ticker"), "headline": headline, "score": round(s, 3),
                    "terms": hits}
            self.recent_news.appendleft(item)
            self.journal.write("news", item)

    def _on_stage_change(self, old: str, new: str) -> None:
        # called from the stage thread: only set flags, the engine thread acts on them
        if new in ("preflight", "simulation", "ready"):
            self.refresh_requested = True
        if new == "simulation":
            self.reset_requested = True
            self.switch_from = old

    def apply_refresh(self) -> None:
        """A stage change or a new listing asked for a fresh universe (and maybe a reset)."""
        self.refresh_requested = False
        self.load_universe()
        same_data = False
        if self.reset_requested:
            self.reset_requested = False
            # pre-flight streams the simulation's own day 1, so its history and news
            # are the simulation's: keep them (re-downloading cost day 1 live)
            same_data = self.switch_from == "preflight" and all(
                len(self.bars.get(t, [])) >= 120 for t in self.universe)
            self.last_date = None
            if not same_data:               # same data: a later day already queued is a sim day, keep it
                self.pending = {}
                self.news = NewsBook(self.p.news_half_life_days)
            self.regimes.clear()
            self.weak.clear()
            self.need_reconcile = True
        if same_data:
            missing = sorted(t for t in self.universe if len(self.bars.get(t, [])) < 120)
            if missing:
                self.warm_start(missing)
            self.catch_up_observed_day()
        else:
            self.observed = None
            self.warm_start()

    def catch_up_observed_day(self) -> bool:
        """Trade the day that was only observed because the stage still said pre-flight.

        Live, SimTest's day 1 arrived 2 s before the stage switched to simulation, so the
        bot watched it and bought nothing until day 2. If that day is still the current
        one (no later tick yet, and less than one day-interval old), decide it again now."""
        if not self.observed:
            return False
        d, slot = self.observed
        self.observed = None
        interval = float(self.stage.info.get("interval_seconds") or 4.0)
        if any(p > d for p in self.pending) or time.monotonic() - slot["first"] > interval - 0.5:
            log.info("observed day %s is over: trading starts with the next day", d)
            return False
        log.info("trading the observed pre-flight day %s now that the simulation is running", d)
        self.day = max(0, self.day - 1)                # the same day again, not a new one
        self.stats["days"] = max(0, self.stats["days"] - 1)
        self.decide(d, slot)
        return True

    # --------------------------------------------------------------- decision
    def maybe_decide(self) -> None:
        if not self.pending:
            return
        now = time.monotonic()
        min_wait = self.cfg.decision_delay_ms / 1000.0
        quiet = self.cfg.news_quiet_ms / 1000.0
        max_wait = self.cfg.decision_max_wait_ms / 1000.0
        news_cap = self.cfg.news_max_wait_ms / 1000.0
        days = sorted(self.pending)
        for i, d in enumerate(days):
            slot = self.pending[d]
            age = now - slot["first"]
            complete = bool(self.universe) and self.universe.issubset(slot["bars"])
            news_quiet = now - self.news_last.get(d, 0.0) >= quiet
            newer_started = i < len(days) - 1          # never decide days out of order
            if complete and age >= min_wait and (news_quiet or age >= news_cap):
                ready = True
            else:
                ready = age >= max_wait or newer_started
            if ready:
                del self.pending[d]
                self.news_last.pop(d, None)
                self.decide(d, slot)

    def decide(self, d: str, slot: dict) -> None:
        t0 = slot["first"]
        if self.last_date and d < self.last_date:
            log.warning("date went back %s -> %s: sandbox loop, re-warming indicators", self.last_date, d)
            self.stats["loops_detected"] += 1
            self.regimes.clear()
            self.weak.clear()
            self.warm_start()
            self.need_reconcile = True
        elif self.last_date and d == self.last_date:
            return
        self.day += 1
        self.stats["days"] += 1
        prev_date, self.last_date = self.last_date, d

        for t, bar in slot["bars"].items():
            series = self.bars.setdefault(t, [])
            if series and series[-1]["date"] >= d:
                series[:] = [b for b in series if b["date"] < d]
            series.append(bar)
            del series[:-MAX_BARS]

        signals = {}
        for t in slot["bars"]:
            if self.universe and t not in self.universe:
                continue
            f = features(self.bars[t], self.p)
            if f:
                signals[t] = evaluate(t, f, self.news.get(t), self.p, self.regimes)
        cross_section(signals, self.p, self.weak)
        prices = {t: s.feats["close"] for t, s in signals.items()}
        if self.day % 20 == 0:
            self.screen_universe(periodic=True)        # data quality can change during the run

        # weekly roll: shorts may have been settled by the platform on Friday
        if prev_date and date.fromisoformat(d).weekday() < date.fromisoformat(prev_date).weekday():
            if any(p.qty < 0 for p in self.ledger.positions.values()):
                self.need_reconcile = True
        # an anomaly must be fixed before we size orders; routine syncs run after the orders
        if self.need_reconcile:
            self.reconcile("resync")

        risk = self.guard.update(self.ledger.wealth(prices) if prices else self.ledger.cash, self.day)
        if risk.mode != "normal":
            log.warning("risk guard %s: %s", risk.mode, "; ".join(risk.reasons))
        update_stops(self.ledger, signals, self.p)
        intents = plan(self.ledger, signals, d, self.day, self.p, self.stage.days_remaining,
                       self.cfg.max_orders_per_day, risk=risk,
                       eligible=self.eligible if self.screen_results else None)
        decided_ms = (time.monotonic() - t0) * 1000

        mode = self._mode()
        self.observed = (d, slot) if mode.startswith("observe") else None
        if mode == "live":
            results = self._execute(intents, prices, d)
        else:
            results = []
            for it in intents:
                px = prices[it.ticker]
                rec = {"date": d, "ticker": it.ticker, "action": it.action, "qty": it.qty, "price": px,
                       "reason": it.reason, "mode": mode, "status": "NOT_SENT", "explain": it.explain}
                if mode == "dry_run":
                    # paper trading: same validation, filled locally at today's close
                    err = self.ledger.validate(it.ticker, it.action, it.qty, px)
                    if err:
                        rec.update(status="PAPER_SKIPPED", error=err)
                    else:
                        self.ledger.apply_fill(it.ticker, it.action, it.qty, px, d, self.day)
                        rec.update(status="PAPER_FILLED", fill=px)
                        log.info("PAPER %s %s x%d @ %.2f | %s", it.action, it.ticker, it.qty, px, it.reason)
                results.append(rec)
        for r in results:
            self.recent_orders.appendleft(r)
            self.journal.write("orders", r)

        total_ms = (time.monotonic() - t0) * 1000
        n = self.stats["days"]
        self.stats["latency_ms_avg"] = round(self.stats["latency_ms_avg"] + (total_ms - self.stats["latency_ms_avg"]) / n, 1)
        self.stats["latency_ms_max"] = round(max(self.stats["latency_ms_max"], total_ms), 1)

        self.last_signals = {t: {"score": round(s.score, 3), "close": s.feats["close"],
                                 "parts": {k: round(v, 3) for k, v in s.parts.items()},
                                 "rsi": round(s.feats["rsi"], 1), "er": round(s.feats["er"], 2),
                                 "news": round(self.news.get(t), 2), "reasons": s.reasons}
                             for t, s in signals.items()}
        wealth = self.ledger.wealth(prices) if prices else self.ledger.cash
        self.journal.write("decisions", {"date": d, "day": self.day, "mode": mode, "decide_ms": round(decided_ms, 1),
                                         "total_ms": round(total_ms, 1), "n_ticks": len(slot["bars"]),
                                         "wealth": round(wealth, 2), "cash": round(self.ledger.cash, 2),
                                         "risk": vars(risk), "signals": self.last_signals,
                                         "intents": [vars(i) for i in intents]})
        log.info("%s day %d | %s | %d ticks | %d intents | %.0f ms | wealth %.0f cash %.0f pos %d",
                 d, self.day, mode, len(slot["bars"]), len(intents), total_ms, wealth, self.ledger.cash,
                 len(self.ledger.positions))
        if self.day - self.last_reconcile_day >= self.cfg.reconcile_every_days:
            self.reconcile("scheduled")          # off the latency path: orders are already out
        self.write_state(prices)

    def _execute(self, intents, prices: dict[str, float], d: str) -> list[dict]:
        """Send all of today's orders concurrently: one gateway round trip, not N.

        This is safe because the planner sizes entries from cash *before* any sells
        (never counting on same-day proceeds) and never exits and re-enters the same
        stock on one day, so exits and entries are independent. Entries are also
        pre-checked against a running cash budget, so concurrent fills can't
        overspend. (Measured live: sequential exit-then-entry batches stacked two
        slow gateway calls into a 3.6 s decision on a loaded server.)

        Failure handling: a (date, ticker, action) is sent at most once per day; a
        PARTIAL fill books only the filled shares; an UNKNOWN outcome is never
        booked or resent, it triggers a resync from GET /portfolio; N rejects in a
        row trip a circuit breaker that pauses orders for 60 s.
        """
        results: list[dict] = []
        if self.sent_today[0] != d:
            self.sent_today = (d, set())
        sent = self.sent_today[1]
        rebuys = [i for i in intents if i.priority == HARVEST_REBUY]   # need their SELL filled first
        first = [i for i in intents if i.priority != HARVEST_REBUY]
        exits = [i for i in first if i.action in ("SELL", "COVER")]
        entries = [i for i in first if i.action in ("BUY", "SHORT")]
        halted = False
        sold: dict[str, int] = {}
        for batch in (exits + entries, rebuys):
            if batch is rebuys:
                # only if the harvest SELL filled, and only as many shares as were sold
                batch = [i for i in rebuys if sold.get(i.ticker)]
                for i in batch:
                    i.qty = min(i.qty, sold[i.ticker])
            if not batch:
                continue
            ready = []
            budget = self.ledger.cash
            for it in batch:
                px = prices[it.ticker]
                rec = {"date": d, "ticker": it.ticker, "action": it.action, "qty": it.qty, "price": px,
                       "reason": it.reason, "mode": "live", "explain": it.explain}
                key = (it.ticker, it.action)
                if key in sent:
                    self.stats["orders_duplicate_blocked"] += 1
                    rec.update(status="SKIPPED", error="DUPLICATE_BLOCKED")
                    results.append(rec)
                    continue
                if halted or time.monotonic() < self.orders_paused_until:
                    rec["status"] = "NOT_SENT"
                    results.append(rec)
                    continue
                err = self.ledger.validate(it.ticker, it.action, it.qty, px)
                if not err and it.action in ("BUY", "SHORT"):
                    need = it.qty * px + BROKERAGE_FEE
                    if need > budget:
                        err = "INSUFFICIENT_CAPITAL" if it.action == "BUY" else "INSUFFICIENT_MARGIN"
                    else:
                        budget -= need
                if err:
                    self.stats["orders_skipped_local"] += 1
                    self.need_reconcile = True
                    rec.update(status="SKIPPED", error=err)
                    results.append(rec)
                    continue
                sent.add(key)
                ready.append((it, rec, px))
            if not ready:
                continue

            self.stats["orders_sent"] += len(ready)
            futures = [self.pool.submit(self.api.place_order, it.ticker, it.action, it.qty, d)
                       for it, _, _ in ready]
            for (it, rec, px), fut in zip(ready, futures):
                res = fut.result()
                rec["latency_ms"] = round(res.latency_ms, 1)
                if res.ok:
                    self.consecutive_rejects = 0
                    qty = res.filled_qty if res.filled_qty is not None else it.qty
                    fill = float(res.data.get("execution_price") or res.data.get("price") or px)
                    try:
                        self.ledger.apply_fill(it.ticker, it.action, qty, fill, d, self.day)
                    except ValueError as exc:
                        log.warning("ledger out of sync: %s", exc)
                        self.need_reconcile = True
                    self.stats["orders_filled"] += 1
                    status = res.status or "FILLED"
                    if status == "PARTIAL":
                        self.stats["orders_partial"] += 1
                        self.need_reconcile = True
                        log.warning("PARTIAL %s %s: %d of %d filled", it.action, it.ticker, qty, it.qty)
                    if it.action == "SELL":
                        sold[it.ticker] = qty
                    rec.update(status=status, filled_qty=qty, fill=fill, order_id=res.data.get("order_id"),
                               realized_pnl=res.data.get("realized_pnl"), recovered=res.recovered,
                               server_delay_s=res.data.get("decision_delay"))
                    log.info("%s %s %s x%d @ %.2f | %s", status, it.action, it.ticker, qty, fill, it.reason)
                elif res.status == "UNKNOWN":
                    # might have executed: don't book it, don't resend it, ask the server
                    self.stats["orders_unknown"] += 1
                    self.need_reconcile = True
                    halted = True
                    rec.update(status="UNKNOWN", error=res.error_code, message=res.message)
                    log.error("UNKNOWN outcome %s %s x%d (%s): resync before next order", it.action, it.ticker,
                              it.qty, res.message)
                elif res.status == "SKIPPED":
                    self.stats["orders_skipped_local"] += 1
                    rec.update(status="SKIPPED", error=res.error_code, message=res.message)
                    if self._on_reject(res.error_code or "", it.ticker):
                        halted = True
                else:
                    self.stats["orders_rejected"] += 1
                    self.consecutive_rejects += 1
                    rec.update(status="REJECTED", error=res.error_code, message=res.message)
                    log.warning("REJECTED %s %s x%d: %s %s", it.action, it.ticker, it.qty,
                                res.error_code, res.message)
                    if self._on_reject(res.error_code or "", it.ticker):
                        halted = True        # don't start the next batch today
                    if self.consecutive_rejects >= self.p.max_consecutive_rejects:
                        self.stats["circuit_breaks"] += 1
                        self.orders_paused_until = max(self.orders_paused_until, time.monotonic() + 60)
                        self.need_reconcile = True
                        halted = True
                        log.error("circuit breaker: %d rejects in a row, orders paused 60 s",
                                  self.consecutive_rejects)
                        self.consecutive_rejects = 0
                results.append(rec)
        return results

    def _mode(self) -> str:
        if self.cfg.dry_run:
            return "dry_run"
        if not self.cfg.token or self.token_ok is False:
            return "no_token"
        if not self.stage.trading_active:
            return f"observe:{self.stage.stage or 'unknown'}"
        if time.monotonic() < self.orders_paused_until:
            return "paused"
        return "live"

    def _on_reject(self, code: str, ticker: str) -> bool:
        """React to a gateway rejection. True = stop sending orders for today."""
        code = code.upper()
        if code in PAUSE_ON:
            self.orders_paused_until = time.monotonic() + PAUSE_ON[code]
            if code == "ACCOUNT_NOT_VERIFIED":
                log.error("account not verified: get verified at the registration desk")
            return True
        if code in RESYNC_ON:
            self.need_reconcile = True
            return True
        if code == "INVALID_TICKER":
            self.universe.discard(ticker)
        if code in ("LOCAL_RATE_BUDGET", "NETWORK_ERROR", "UNREACHABLE"):
            self.need_reconcile = True
            return True
        return False

    # ------------------------------------------------------------------ state
    def write_state(self, prices: dict[str, float] | None = None) -> None:
        prices = prices or {t: b[-1]["close"] for t, b in self.bars.items() if b}
        led = self.ledger
        positions = [{"ticker": t, "qty": p.qty, "avg": round(p.avg_price, 2), "price": prices.get(t),
                      "stop": round(p.stop, 2), "entry_date": p.entry_date,
                      "pnl": round(led.unrealized(t, prices.get(t, p.avg_price)), 2)}
                     for t, p in led.positions.items()]
        self.journal.snapshot("state", {
            "date": self.last_date, "day": self.day, "mode": self._mode(),
            "stage": self.stage.info, "kafka_connected": getattr(self, "kafka_connected", None),
            "wealth": round(led.wealth(prices), 2), "cash": round(led.cash, 2),
            "margin": round(led.margin_locked, 2), "fees": round(led.fees_paid, 2),
            "est_tax": round(led.estimated_tax(), 2), "positions": positions,
            "risk": vars(self.guard.state), "screen": self.screen_results,
            "heartbeat": {"acked_seq": self.hb.acked_seq, "acked": getattr(self.hb, "acked_count", 0),
                          "queued": len(getattr(self.hb, "pending", ())), "failures": self.hb.failures,
                          "platform": getattr(self.hb, "platform_status", {})},
            "signals": self.last_signals, "orders": list(self.recent_orders),
            "news": list(self.recent_news), "stats": {**self.stats, **self.api.stats},
            "rate_tokens": round(self.api.bucket.available(), 1),
        })

    # ------------------------------------------------------------------- loop
    def run(self, feed=None) -> None:
        last_status = time.monotonic()
        while not self.stop.is_set():
            try:
                topic, ev, t_recv = self.q.get(timeout=0.05)
                try:
                    self.handle(topic, ev, t_recv)
                except Exception:
                    log.exception("error handling %s message", topic)
            except queue.Empty:
                pass
            try:
                if not self.universe and time.monotonic() - self.last_universe_try > 10:
                    self.last_universe_try = time.monotonic()
                    self.refresh_requested = True        # started while the gateway was unreachable
                missing = [t for t in self.universe if len(self.bars.get(t, [])) < 120]
                if missing and not self.refresh_requested and time.monotonic() - self.last_warm_try > 20:
                    self.last_warm_try = time.monotonic()      # a history download failed: retry it
                    log.info("retrying price history for %s", ", ".join(sorted(missing)))
                    self.warm_start(sorted(missing))
                if self.refresh_requested:
                    self.apply_refresh()
                self.maybe_decide()
            except Exception:
                log.exception("decision loop error (continuing)")
            if time.monotonic() - last_status > 30:
                last_status = time.monotonic()
                self.kafka_connected = feed.is_connected if feed else None
                ps = getattr(self.hb, "platform_status", {}) or {}
                log.info("status: stage=%s/%s kafka=%s msgs=%s days=%d filled=%d rejected=%d hb_seq=%s hb_acks=%s "
                         "hb_queue=%s | platform: ready=%s consumers=%s last_seq=%s | tokens=%.0f",
                         self.stage.stage, self.stage.status, self.kafka_connected,
                         feed.messages if feed else "-", self.stats["days"], self.stats["orders_filled"],
                         self.stats["orders_rejected"], self.hb.acked_seq, getattr(self.hb, "acked_count", "-"),
                         len(getattr(self.hb, "pending", ())), ps.get("is_ready"), ps.get("kafka_consumer_count"),
                         ps.get("last_seq"), self.api.bucket.available())
                self.write_state()
