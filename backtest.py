"""Event-driven backtest using the exact live code path
(strategy -> cross_section -> risk guard -> planner -> ledger).

Exchange model mirrors the platform: fills at the day's close, Rs 20 brokerage,
Rs 15.34 DP on long sells, 100% short margin, shorts squared off every 10 trading
days, STCG/LTCG with Section 112A exemption and loss set-off.

    python backtest.py                       # full history, default params
    python backtest.py --start 2024-01-01    # one window
    python backtest.py --set long_entry=0.25 --set enable_shorts=false
    python backtest.py --windows             # every evaluation window, incl. out-of-sample
    python backtest.py --walkforward         # tune on 2022-24, report the untouched 2025-26
    python backtest.py --replay-live         # replay the live SimTest days; leaderboard-style scorecard

News: headlines recorded from the live feed (data/stream/news.jsonl) are replayed through
the same NewsBook the live bot uses, on the dates they were broadcast. Days without
recorded news score 0, as before.
"""
from __future__ import annotations

import argparse
import copy
import itertools
import json
from datetime import date, timedelta
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

from trader.config import SHORT_SETTLEMENT_DAYS, BROKERAGE_FEE, Config, StrategyParams
from trader.indicators import features
from trader.metrics import buy_and_hold, summarize
from trader.planner import plan, update_stops
from trader.portfolio import Ledger
from trader.risk import RiskGuard
from trader.sentiment import NewsBook, score_news
from trader.strategy import cross_section, evaluate
from trader.universe import screen

WARMUP = 60

# Evaluation windows. Parameters were tuned on TRAIN windows only; OOS (2025-01 ->
# 2026-08) was held out and is reported, never optimised on.
WINDOWS = {
    "train_2022_23": ("2022-11-15", "2023-12-31"),
    "train_2024": ("2024-01-01", "2024-12-31"),
    "oos_2025_26": ("2025-01-01", None),
    "sandbox_period": ("2024-08-01", None),
    "sim_2024_26": ("2024-10-25", "2026-10-23"),   # the exact days of the live SimTest run
    "full": ("2022-11-15", None),
}


def load(data_dir: Path) -> dict[str, list[dict]]:
    out = {}
    for f in sorted((data_dir / "history").glob("*.json")):
        out[f.stem] = json.loads(f.read_text())
    return out


_NEWS: dict[str, dict] = {}


def load_news(data_dir: Path | None = None) -> dict[str, list[tuple[str | None, float, str]]]:
    """Recorded news_flash items by simulated date: [(ticker, sentiment, headline)].
    Sandbox loops replay the same days, so repeats of (ticker, headline) on a date are dropped."""
    path = (data_dir or Config().data_dir) / "stream" / "news.jsonl"
    key = str(path)
    if key not in _NEWS:
        out: dict[str, list] = {}
        seen: set = set()
        if path.exists():
            with path.open(encoding="utf-8") as fh:
                for line in fh:
                    try:
                        ev = json.loads(line)
                    except ValueError:
                        continue
                    data = ev.get("data") or {}
                    d = ev.get("simulated_date") or data.get("date")
                    if ev.get("event_type") != "news_flash" or not d:
                        continue
                    k = (d, data.get("ticker"), data.get("headline"))
                    if k in seen:
                        continue
                    seen.add(k)
                    s, _ = score_news(data.get("headline", ""), data.get("description", ""))
                    out.setdefault(str(d)[:10], []).append((data.get("ticker"), s, data.get("headline", "")))
        _NEWS[key] = out
    return _NEWS[key]


def run(hist: dict[str, list[dict]], p: StrategyParams, start: str | None = None, end: str | None = None,
        max_orders: int | None = None, verbose: bool = False, keep_trace: bool = False,
        news: dict | None = None, history_days: int | None = None, observe_days: int = 0,
        blind_until: dict[str, int] | None = None) -> dict:
    """`news`: date -> items (None = the recorded live feed, {} = no news).
    `history_days`: warm-up = the bars of this many calendar days before `start`, like the
    live bot's GET /history?days=HISTORY_DAYS at startup (None = the Config value).
    `observe_days`: decide but don't trade the first N days (live: day 1 of SimTest arrived
    while the stage still said pre-flight).
    `blind_until`: ticker -> first trading day it may be traded (replays a live history-download
    failure; used only to check that the replay reproduces the live run)."""
    news = load_news() if news is None else news
    max_orders = Config().max_orders_per_day if max_orders is None else max_orders
    book = NewsBook(p.news_half_life_days)
    universe = set(hist)
    dates = sorted({b["date"] for bars in hist.values() for b in bars})
    by_date = {t: {b["date"]: b for b in bars} for t, bars in hist.items()}
    trade_dates = [d for d in dates if (not start or d >= start) and (not end or d <= end)]
    # warm-up bars come from before the window, like GET /history at bot startup
    history_days = Config().history_days if history_days is None else history_days
    first = (date.fromisoformat(start) - timedelta(days=history_days)).isoformat() if start else ""
    window: dict[str, list[dict]] = {t: [b for b in bars if start and first <= b["date"] < start][-300:]
                                     for t, bars in hist.items()}

    led = Ledger()
    guard = RiskGuard(p)
    regimes: dict[str, float] = {}
    weak: dict[str, bool] = {}
    eligible: set[str] = set(hist)
    curve, util, rejects, trades, fills, risk_days = [], [], 0, [], [], {}
    short_pnl, short_trades, short_wins, harvests, realized = 0.0, 0, 0, 0, 0.0
    first_prices: dict[str, float] = {}
    last_prices: dict[str, float] = {}

    for day, d in enumerate(trade_dates):
        for t in hist:
            b = by_date[t].get(d)
            if b:
                window[t].append(b)
                window[t] = window[t][-300:]
                last_prices[t] = b["close"]
                first_prices.setdefault(t, b["close"])
        if day > 0:
            book.new_day()
        for tk, sc, hl in news.get(d, ()):
            book.add(tk, sc, hl, universe)
        if day < WARMUP and not start:
            curve.append(led.wealth(last_prices))
            util.append(0.0)
            continue
        if day % 5 == 0:
            eligible = {t for t, bars in window.items() if screen(t, bars, d).ok}

        signals = {}
        for t, bars in window.items():
            if not bars or bars[-1]["date"] != d:
                continue                         # no candle today -> don't trade it
            if blind_until and day < blind_until.get(t, 0):
                continue
            f = features(bars, p)
            if f:
                signals[t] = evaluate(t, f, book.get(t) if news else 0.0, p, regimes)
        cross_section(signals, p, weak)
        risk = guard.update(led.wealth(last_prices), day)
        risk_days[risk.mode] = risk_days.get(risk.mode, 0) + 1
        update_stops(led, signals, p)
        remaining = len(trade_dates) - 1 - day
        intents = plan(led, signals, d, day, p, days_remaining=remaining, max_orders=max_orders,
                       risk=risk, eligible=eligible) if day >= observe_days else []
        for it in intents:
            px = signals[it.ticker].feats["close"]
            if led.validate(it.ticker, it.action, it.qty, px):
                rejects += 1
                continue
            pnl = led.apply_fill(it.ticker, it.action, it.qty, px, d, day)
            realized += pnl
            if keep_trace:
                fills.append({"date": d, "ticker": it.ticker, "action": it.action, "qty": it.qty, "price": px,
                              "settlement": False})
            if it.action in ("SELL", "COVER") and not it.reason.startswith("tax-loss"):
                trades.append(pnl)
            if it.action == "COVER":
                short_pnl += pnl
                short_trades += 1
                short_wins += pnl > 0
            if it.reason.startswith("tax-loss") and it.action == "SELL":
                harvests += 1
            if verbose:
                print(f"{d} {it.action:5} {it.ticker:5} x{it.qty:<6} @ {px:9.2f}  {it.reason}")

        # bi-weekly settlement: all shorts squared off at the close
        if day > 0 and day % SHORT_SETTLEMENT_DAYS == 0:
            for t, pos in list(led.positions.items()):
                if pos.qty < 0 and t in last_prices:
                    qty = -pos.qty
                    pnl = led.apply_fill(t, "COVER", qty, last_prices[t], d, day)
                    realized += pnl
                    if keep_trace:
                        fills.append({"date": d, "ticker": t, "action": "COVER", "qty": qty,
                                      "price": last_prices[t], "settlement": True})
                    trades.append(pnl)
                    short_pnl += pnl
                    short_trades += 1
                    short_wins += pnl > 0
                    led.fees_paid -= BROKERAGE_FEE    # settlement isn't our order
                    led.cash += BROKERAGE_FEE
                    led.orders -= 1

        w = led.wealth(last_prices)
        curve.append(w)
        util.append((led.long_value(last_prices) + led.margin_locked) / w if w > 0 else 0.0)

    tax = led.estimated_tax()
    bench = buy_and_hold(first_prices, last_prices, trade_dates[0], trade_dates[-1])
    liq = copy.deepcopy(led)                    # liquidated basis: sell everything at the last close
    for t, pos in list(liq.positions.items()):
        liq.apply_fill(t, "SELL" if pos.qty > 0 else "COVER", abs(pos.qty), last_prices[t], trade_dates[-1],
                       len(trade_dates))
    m = summarize(curve, util, trades, tax, led.fees_paid, led.orders, rejects, bench,
                  liquidated=liq.cash + liq.margin_locked - liq.estimated_tax())
    m.update({
        "open_positions": len(led.positions),
        "short_trades": short_trades,
        "short_win_rate_pct": round(100 * short_wins / short_trades, 1) if short_trades else 0.0,
        "short_pnl": round(short_pnl, 0),
        "tax_harvests": harvests,
        "risk_days": risk_days,
        "news_days": sum(1 for d in trade_dates if d in news),
        # the platform leaderboard's columns, from the same ledger
        "scorecard": {
            "total_wealth": round(curve[-1], 2), "available_cash": round(led.cash, 2),
            "invested_value": round(led.long_value(last_prices), 2), "margin_locked": round(led.margin_locked, 2),
            "realized": round(realized, 2),
            "unrealized": round(sum((last_prices[t] - ps.avg_price) * ps.qty for t, ps in led.positions.items()
                                    if t in last_prices), 2),
            "charges": round(led.fees_paid, 2), "taxes": round(tax, 2),
            "cash_util_pct": round(100 * (led.long_value(last_prices) + led.margin_locked) / curve[-1], 1),
            "orders": led.orders, "errors": rejects, "pos": len(led.positions),
        },
    })
    if keep_trace:
        m["_fills"], m["_curve"], m["_ledger"], m["_prices"] = fills, curve, led, dict(last_prices)
        m["_dates"] = trade_dates
    return m


def episode_starts(hist: dict[str, list[dict]], length: int = 500, step: int = 10,
                   history_days: int | None = None) -> list[tuple[str, str]]:
    """Every live-length run the data allows: `length` trading days, each with a full
    warm-up of `history_days` calendar days before it (what GET /history gives the bot)."""
    dates = sorted({b["date"] for bars in hist.values() for b in bars})
    history_days = Config().history_days if history_days is None else history_days
    first_ok = (date.fromisoformat(dates[0]) + timedelta(days=history_days)).isoformat()
    starts = [i for i, d in enumerate(dates) if d >= first_ok and i + length <= len(dates)]
    return [(dates[i], dates[i + length - 1]) for i in starts[::step]]


def _episode_job(args):
    hist, p, start, end, key = args
    m = run(hist, p, start, end, observe_days=1)
    return key, {k: m[k] for k in ("after_tax_pct", "buy_hold_pct", "excess_vs_buy_hold_pct", "max_drawdown_pct",
                                   "avg_utilization_pct", "orders", "short_trades")} | {"scorecard": m["scorecard"]}


def run_episodes(hist, variants: dict[str, StrategyParams], length: int = 500, step: int = 10,
                 workers: int = 10) -> dict[str, dict]:
    """Score parameter sets over every live-length episode. Returns per variant the mean,
    median and worst after-tax return and excess over buy & hold, and mean drawdown."""
    eps = episode_starts(hist, length, step)
    jobs = [(hist, q, s, e, (name, s)) for name, q in variants.items() for s, e in eps]
    with ProcessPoolExecutor(max_workers=workers) as ex:
        res = dict(ex.map(_episode_job, jobs))
    out = {}
    for name in variants:
        rows = [res[(name, s)] for s, _ in eps]
        at = sorted(r["after_tax_pct"] for r in rows)
        ex_ = sorted(r["excess_vs_buy_hold_pct"] for r in rows)
        out[name] = {"episodes": len(rows), "first_start": eps[0][0], "last_start": eps[-1][0],
                     "mean_after_tax_pct": round(sum(at) / len(at), 2), "median_after_tax_pct": at[len(at) // 2],
                     "worst_after_tax_pct": at[0], "mean_excess_pct": round(sum(ex_) / len(ex_), 2),
                     "worst_excess_pct": ex_[0],
                     "beat_buy_hold_pct": round(100 * sum(1 for x in ex_ if x > 0) / len(ex_), 1),
                     "mean_max_dd_pct": round(sum(r["max_drawdown_pct"] for r in rows) / len(rows), 2),
                     "mean_util_pct": round(sum(r["avg_utilization_pct"] for r in rows) / len(rows), 1),
                     "mean_orders": round(sum(r["orders"] for r in rows) / len(rows), 1),
                     "mean_taxes": round(sum(r["scorecard"]["taxes"] for r in rows) / len(rows), 0)}
    return out


def print_episodes(res: dict[str, dict]) -> None:
    cols = ["mean_after_tax_pct", "median_after_tax_pct", "worst_after_tax_pct", "mean_excess_pct",
            "worst_excess_pct", "beat_buy_hold_pct", "mean_max_dd_pct", "mean_util_pct", "mean_orders", "mean_taxes"]
    first = next(iter(res.values()))
    print(f"{first['episodes']} episodes of 500 trading days, starts {first['first_start']} .. {first['last_start']}")
    print(f"{'variant':22}" + "".join(f"{c.replace('_pct', '')[:13]:>14}" for c in cols))
    for n, r in res.items():
        print(f"{n:22}" + "".join(f"{r[c]:>14}" for c in cols))


def parse_set(items: list[str], p: StrategyParams) -> StrategyParams:
    kw = {}
    for it in items:
        k, v = it.split("=", 1)
        cur = getattr(p, k)
        kw[k] = v.lower() in ("1", "true", "yes") if isinstance(cur, bool) else type(cur)(v)
    return replace(p, **kw)


def score(m: dict) -> float:
    """Selection objective: risk-adjusted, after tax, not raw profit."""
    return m["after_tax_pct"] - 0.5 * m["max_drawdown_pct"]


def _job(args):
    hist, p, start, end, name = args
    return name, run(hist, p, start, end)


def run_windows(hist, p: StrategyParams, names=None, workers: int = 6) -> dict[str, dict]:
    names = names or list(WINDOWS)
    jobs = [(hist, p, *WINDOWS[n], n) for n in names]
    with ProcessPoolExecutor(max_workers=min(workers, len(jobs))) as ex:
        return dict(ex.map(_job, jobs))


def print_windows(res: dict[str, dict]) -> None:
    cols = ["after_tax_pct", "buy_hold_pct", "liquidated_after_tax_pct", "buy_hold_liquidated_pct", "max_drawdown_pct", "sharpe", "win_rate_pct",
            "avg_utilization_pct", "orders", "short_trades", "short_pnl", "rejects"]
    print(f"{'window':16}" + "".join(f"{c[:12]:>13}" for c in cols))
    for n, m in res.items():
        print(f"{n:16}" + "".join(f"{m[c]:>13}" for c in cols))


GRID = {
    "core_weight": [0.85, 0.90, 0.97],
    "tactical": [False, True],
    "max_stock_weight": [0.30, 1.0],
}


def walkforward(hist, p: StrategyParams) -> None:
    """Pick parameters by the worst TRAIN window only, then report them out-of-sample."""
    keys = list(GRID)
    combos = [dict(zip(keys, c)) for c in itertools.product(*GRID.values())]
    jobs = [(hist, replace(p, **kw), *WINDOWS[w], (i, w)) for i, kw in enumerate(combos)
            for w in ("train_2022_23", "train_2024")]
    with ProcessPoolExecutor(max_workers=10) as ex:
        res = dict(ex.map(_job, jobs))
    ranked = sorted(range(len(combos)), key=lambda i: -min(score(res[(i, w)]) for w in ("train_2022_23", "train_2024")))
    print("Top 5 by worst-TRAIN-window score, then their untouched out-of-sample result:")
    top = ranked[:5]
    oos_jobs = [(hist, replace(p, **combos[i]), *WINDOWS["oos_2025_26"], i) for i in top]
    with ProcessPoolExecutor(max_workers=5) as ex:
        oos = dict(ex.map(_job, oos_jobs))
    for i in top:
        tr = min(score(res[(i, w)]) for w in ("train_2022_23", "train_2024"))
        o = oos[i]
        print(f"  {combos[i]}\n     train worst score {tr:6.2f} | OOS after-tax {o['after_tax_pct']:6.2f}% "
              f"dd {o['max_drawdown_pct']:5.2f}% sharpe {o['sharpe']:5.2f} | B&H {o['buy_hold_pct']:6.2f}%")


SCORE_COLS = [("total_wealth", "TOTAL WEALTH"), ("realized", "REALIZED"), ("unrealized", "UNREALIZED"),
              ("charges", "CHARGES"), ("taxes", "TAXES"), ("cash_util_pct", "CASH UTIL %"),
              ("available_cash", "CASH"), ("margin_locked", "MARGIN"), ("orders", "ORDERS"),
              ("errors", "ERRORS"), ("pos", "POS")]


def live_scorecard(log_dir: Path) -> dict | None:
    """The same columns from the live bot's own logs (state.json + orders.jsonl)."""
    try:
        st = json.loads((log_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return None
    orders = errors = 0
    path = log_dir / "orders.jsonl"
    if path.exists():
        for line in path.open(encoding="utf-8"):
            o = json.loads(line)
            orders += o.get("status") in ("FILLED", "PARTIAL")
            errors += o.get("status") == "REJECTED"
    pos = [x for x in (st.get("positions") or []) if isinstance(x, dict) and x.get("qty")]
    w = float(st.get("wealth") or 0)
    longv = sum(x["qty"] * x["price"] for x in pos if x["qty"] > 0)
    unreal = sum(float(x.get("pnl") or 0) for x in pos)
    fees = float(st.get("fees") or 0)
    return {"total_wealth": w, "available_cash": st.get("cash"), "invested_value": round(longv, 2),
            "margin_locked": st.get("margin"), "charges": fees, "taxes": st.get("est_tax"),
            # wealth = capital + realized - charges + unrealized
            "realized": round(w - 1_000_000 - unreal + fees, 2), "unrealized": round(unreal, 2),
            "cash_util_pct": round(100 * (longv + float(st.get("margin") or 0)) / w, 1) if w else None,
            "orders": orders, "errors": errors, "pos": len(pos), "date": st.get("date")}


def print_scorecards(rows: dict[str, dict]) -> None:
    print(f"{'':30}" + "".join(f"{h:>14}" for _, h in SCORE_COLS))
    for name, sc in rows.items():
        cells = []
        for k, _ in SCORE_COLS:
            v = sc.get(k)
            cells.append(f"{'-':>14}" if v is None else f"{v:>14,.1f}" if isinstance(v, float) else f"{v:>14}")
        print(f"{name:30}" + "".join(cells))


def replay_live(hist, p: StrategyParams, cfg: Config) -> None:
    """Replay the live SimTest run's 500 days with its recorded news: as it ran live
    (previous parameters) and with today's parameters, next to the live result."""
    start, end = WINDOWS["sim_2024_26"]
    as_live = replace(p, core_weight=0.80, max_long_exposure=0.95, max_gross_exposure=0.97, max_stock_weight=1.0, tactical=True)
    rows = {}
    for name, q in (("replay: parameters run live", as_live), ("replay: current parameters", p)):
        m = run(hist, q, start, end, cfg.max_orders_per_day, observe_days=1)
        rows[name] = m["scorecard"]
        print(f"{name}: after tax {m['after_tax_pct']}% | B&H {m['buy_hold_pct']}% | max DD {m['max_drawdown_pct']}% "
              f"| shorts {m['short_trades']} | news days {m['news_days']}")
    live = live_scorecard(cfg.log_dir)
    if live and live.get("date", "") >= end:
        rows["live bot logs (SimTest)"] = live
    print()
    print_scorecards(rows)
    print("\nPortal leaderboard for the live run (Tit4Tat): wealth 14,40,868.47, realized 77,058.03, unrealized "
          "3,65,453.36, charges 1,629.52, taxes 0.00, util 88.5%, orders 60, errors 0, POS 10")


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--start")
    ap.add_argument("--end")
    ap.add_argument("--set", action="append", default=[])
    ap.add_argument("--verbose", action="store_true")
    ap.add_argument("--windows", action="store_true")
    ap.add_argument("--walkforward", action="store_true")
    ap.add_argument("--replay-live", action="store_true", help="replay the live SimTest days, scorecard vs live")
    ap.add_argument("--episodes", action="store_true", help="every 500-day live-length run in the data")
    ap.add_argument("--no-news", action="store_true", help="ignore recorded news")
    ap.add_argument("--data", help="data folder with history/ (default DATA_DIR), e.g. data/runs/<run>")
    args = ap.parse_args()

    cfg = Config()
    hist = load(Path(args.data) if args.data else cfg.data_dir)
    p = parse_set(args.set, cfg.params)
    if args.replay_live:
        replay_live(hist, p, cfg)
        return
    if args.episodes:
        print_episodes(run_episodes(hist, {"current": p}))
        return
    if args.walkforward:
        walkforward(hist, p)
        return
    if args.windows:
        print_windows(run_windows(hist, p))
        return
    m = run(hist, p, args.start, args.end, cfg.max_orders_per_day, args.verbose, news={} if args.no_news else None)
    sc = m.pop("scorecard")
    for k, v in m.items():
        print(f"{k:>24}: {v}")
    print()
    print_scorecards({"backtest": sc})


if __name__ == "__main__":
    main()
