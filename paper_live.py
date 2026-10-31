"""Live paper trading: the current strategy on the real-time Kafka stream, with paper money.

    python paper_live.py
    python paper_live.py --set core_weighting=volume --set volume_tilt=0.5   # try a variant

Runs next to the official bot without touching it:
  - reads market.ticks / market.news with its own group-less consumer (assigned partitions),
    so it never joins group-<username> and the platform doesn't count it as a consumer;
  - sends no orders, no heartbeats and uses no token (only the public /simulation/clock);
  - writes to logs_paper/, never to logs/.
It decides each day with exactly the code the bot uses (indicators, strategy, risk guard,
planner) and fills at the day's close in a paper ledger with the platform's fees and tax.
The warm-up history comes from data/history, only bars *before* the first live day.
If the sandbox loops back to an earlier date, a new paper game starts.
"""
from __future__ import annotations

import json
import time
from datetime import date, timedelta
from pathlib import Path

import requests

from backtest import load, parse_set
from trader.config import Config
from trader.indicators import features
from trader.planner import plan, update_stops
from trader.portfolio import Ledger
from trader.risk import RiskGuard
from trader.sentiment import NewsBook, score_news
from trader.strategy import cross_section, evaluate
from trader.universe import screen

TOPICS = ("market.ticks", "market.news")
OUT = Path(__file__).resolve().parent / "logs_paper"


class PaperGame:
    def __init__(self, cfg: Config, hist: dict, first_day: str, p=None):
        self.cfg, self.p = cfg, p or cfg.params
        start = date.fromisoformat(first_day)
        lo = (start - timedelta(days=cfg.history_days)).isoformat()
        self.window = {t: [b for b in bars if lo <= b["date"] < first_day][-300:] for t, bars in hist.items()}
        self.universe = set(hist)
        self.led, self.guard = Ledger(), RiskGuard(self.p)
        self.book = NewsBook(self.p.news_half_life_days)
        self.regimes: dict = {}
        self.weak: dict = {}
        self.eligible = set(hist)
        self.day = 0
        self.first_day = first_day
        self.first_px: dict[str, float] = {}
        self.last_px: dict[str, float] = {}
        self.realized = 0.0
        self.news_day = None

    def news(self, d: str, ticker, headline: str, description: str) -> None:
        if self.news_day is not None and d > self.news_day:
            self.book.new_day()
        self.news_day = max(self.news_day or d, d)
        s, _ = score_news(headline, description)
        self.book.add(ticker, s, headline, self.universe)

    def decide(self, d: str, bars: dict[str, dict], days_remaining: int | None) -> list[str]:
        for t, b in bars.items():
            self.window.setdefault(t, []).append(b)
            self.window[t] = self.window[t][-300:]
            self.last_px[t] = b["close"]
            self.first_px.setdefault(t, b["close"])
        if self.day % 5 == 0:
            self.eligible = {t for t, w in self.window.items() if w and screen(t, w, d).ok}
        signals = {}
        for t, w in self.window.items():
            if w and w[-1]["date"] == d:
                f = features(w, self.p)
                if f:
                    signals[t] = evaluate(t, f, self.book.get(t), self.p, self.regimes)
        cross_section(signals, self.p, self.weak)
        risk = self.guard.update(self.led.wealth(self.last_px), self.day)
        update_stops(self.led, signals, self.p)
        intents = plan(self.led, signals, d, self.day, self.p, days_remaining=days_remaining,
                       max_orders=self.cfg.max_orders_per_day, risk=risk, eligible=self.eligible)
        done = []
        for it in intents:
            px = signals[it.ticker].feats["close"]
            if self.led.validate(it.ticker, it.action, it.qty, px):
                done.append(f"REJECTED {it.action} {it.ticker} x{it.qty}")
                continue
            self.realized += self.led.apply_fill(it.ticker, it.action, it.qty, px, d, self.day)
            done.append(f"{it.action} {it.ticker} x{it.qty} @ {px:.2f} | {it.reason}")
        self.day += 1
        return done

    def card(self) -> dict:
        led, px = self.led, self.last_px
        w = led.wealth(px)
        unreal = sum((px[t] - ps.avg_price) * ps.qty for t, ps in led.positions.items() if t in px)
        bh = (sum(px[t] / self.first_px[t] for t in self.first_px) / len(self.first_px) - 1) if self.first_px else 0.0
        return {"wealth": round(w, 2), "return_pct": round(100 * (w / 1_000_000 - 1), 2),
                "buy_hold_pct": round(100 * bh, 2), "cash": round(led.cash, 2), "realized": round(self.realized, 2),
                "unrealized": round(unreal, 2), "charges": round(led.fees_paid, 2),
                "est_tax": round(led.estimated_tax(), 2), "orders": led.orders, "pos": len(led.positions),
                "util_pct": round(100 * led.long_value(px) / w, 1) if w else 0.0}


def days_remaining(cfg: Config) -> int | None:
    try:
        r = requests.get(f"{cfg.gateway}/api/v1/simulation/clock", timeout=3).json()
        return int(r.get("remaining_days")) if r.get("remaining_days") is not None else None
    except Exception:
        return None


def main() -> None:
    import argparse
    from kafka import KafkaConsumer, TopicPartition

    ap = argparse.ArgumentParser()
    ap.add_argument("--set", action="append", default=[], help="strategy parameter k=v (repeatable)")
    args = ap.parse_args()
    cfg = Config()
    params = parse_set(args.set, cfg.params)
    hist = load(cfg.data_dir)
    OUT.mkdir(exist_ok=True)
    log = (OUT / "paper.jsonl").open("a", encoding="utf-8")
    c = KafkaConsumer(bootstrap_servers=[cfg.broker], enable_auto_commit=False, request_timeout_ms=30000,
                      fetch_max_wait_ms=100,
                      value_deserializer=lambda raw: json.loads(raw.decode("utf-8")) if raw else None)
    tps = [TopicPartition(tp, p) for tp in TOPICS for p in (c.partitions_for_topic(tp) or range(10))]
    c.assign(tps)
    c.seek_to_end(*tps)
    print(f"paper trading on the live stream | profile {cfg.profile} {' '.join(args.set)} | "
          f"core weighting {params.core_weighting} | Rs 10,00,000 paper money | "
          f"no orders, no group, no token | log {OUT / 'paper.jsonl'}", flush=True)

    game: PaperGame | None = None
    pending: dict[str, dict[str, dict]] = {}
    news_wait: list = []
    last_decided = ""
    while True:
        for records in c.poll(timeout_ms=200).values():
            for rec in records:
                ev = rec.value or {}
                data = ev.get("data") or {}
                d = str(ev.get("simulated_date") or data.get("date") or "")[:10]
                et = ev.get("event_type") or ev.get("event")
                if not d:
                    continue
                if et == "price_tick" or (rec.topic == "market.ticks" and "close" in data):
                    t = data.get("ticker")
                    if not t or float(data.get("close") or 0) <= 0:
                        continue
                    if last_decided and d < last_decided and d not in pending:
                        print(f"\n=== sandbox looped back to {d}: new paper game ===", flush=True)
                        game, pending, last_decided = None, {}, ""
                    pending.setdefault(d, {})[t] = {
                        "date": d, "open": float(data.get("open") or data["close"]),
                        "high": float(data.get("high") or data["close"]), "low": float(data.get("low") or data["close"]),
                        "close": float(data["close"]), "volume": float(data.get("volume") or 0)}
                elif et == "news_flash" or rec.topic == "market.news":
                    news_wait.append((d, data.get("ticker"), data.get("headline", ""), data.get("description", "")))
        # decide a day once all its ticks are in (or a later day has started)
        for d in sorted(pending):
            later = any(x > d for x in pending)
            if len(pending[d]) < len(hist) and not later:
                continue
            if game is None:
                game = PaperGame(cfg, hist, d, params)
            for item in [n for n in news_wait if n[0] <= d]:
                game.news(*item)
            news_wait = [n for n in news_wait if n[0] > d]
            t0 = time.perf_counter()
            done = game.decide(d, pending.pop(d), days_remaining(cfg))
            ms = (time.perf_counter() - t0) * 1000
            last_decided = d
            card = game.card()
            print(f"{d} day {game.day:3} | wealth {card['wealth']:>12,.0f} ({card['return_pct']:+.2f}%, "
                  f"market {card['buy_hold_pct']:+.2f}%) | realized {card['realized']:>9,.0f} | "
                  f"unrealized {card['unrealized']:>10,.0f} | tax {card['est_tax']:>7,.0f} | "
                  f"charges {card['charges']:>6,.0f} | orders {card['orders']:3} | pos {card['pos']} | "
                  f"util {card['util_pct']}% | {ms:.0f} ms", flush=True)
            for line in done:
                print("    " + line, flush=True)
            log.write(json.dumps({"date": d, "day": game.day, "orders": done, **card}) + "\n")
            log.flush()


if __name__ == "__main__":
    try:
        main()
    except KeyboardInterrupt:
        print("\npaper trading stopped")
