"""Does the recorded news predict prices? python research/news_check.py

Uses the headlines recorded from the live feed (data/stream/news.jsonl, deduplicated per
date) and the price history. For every stock-day it compares the news sentiment, scored
exactly as the bot scores it, with that stock's return *relative to the 10-stock average*
(so a market-wide move doesn't count as news skill):
  same day      (news and price move together: would show if news was made from prices)
  next day      (what the bot can trade: it buys at the close of the news day)
  next 5 days
"""
from __future__ import annotations

import json
import math
import random
import sys
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backtest import load, load_news  # noqa: E402
from trader.sentiment import NewsBook  # noqa: E402


def corr(xs, ys):
    n = len(xs)
    mx, my = sum(xs) / n, sum(ys) / n
    sx = math.sqrt(sum((x - mx) ** 2 for x in xs))
    sy = math.sqrt(sum((y - my) ** 2 for y in ys))
    return sum((x - mx) * (y - my) for x, y in zip(xs, ys)) / (sx * sy) if sx and sy else 0.0


def rank(v):
    order = sorted(range(len(v)), key=lambda i: v[i])
    r = [0.0] * len(v)
    for k, i in enumerate(order):
        r[i] = k
    return r


def main() -> None:
    hist = load(ROOT / "data")
    news = load_news(ROOT / "data")
    tickers = sorted(hist)
    close = {t: {b["date"]: b["close"] for b in bars} for t, bars in hist.items()}
    dates = sorted(set.intersection(*(set(c) for c in close.values())))
    idx = {d: i for i, d in enumerate(dates)}
    universe = set(tickers)

    def rel_ret(t, d0, d1):
        r = close[t][d1] / close[t][d0] - 1
        m = sum(close[u][d1] / close[u][d0] - 1 for u in tickers) / len(tickers)
        return r - m

    items = [(d, tk, s, h) for d, lst in news.items() for tk, s, h in lst if d in idx]
    tagged = [x for x in items if x[1] in universe]
    print(f"{len(items)} headlines on {len({x[0] for x in items})} days; {len(tagged)} tagged to one of the 10 stocks")
    print(f"sentiment != 0 for {sum(1 for x in tagged if abs(x[2]) > 1e-9)} of the tagged ones")
    random.seed(1)
    print("\nrandom sample of tagged headlines:")
    for d, tk, s, h in random.sample(tagged, min(8, len(tagged))):
        print(f"  {d} {tk:5} {s:+.2f}  {h[:95]}")

    # 1) raw day sentiment per (ticker, day) with news
    day = {}
    for d, tk, s, _ in tagged:
        day.setdefault((tk, d), []).append(s)
    rows = []
    for (tk, d), ss in day.items():
        i = idx[d]
        if i < 1 or i + 5 >= len(dates):
            continue
        rows.append((sum(ss) / len(ss), rel_ret(tk, dates[i - 1], d), rel_ret(tk, d, dates[i + 1]),
                     rel_ret(tk, d, dates[i + 5])))
    # 2) the bot's own decayed NewsBook score, every stock every news day
    book, prev = NewsBook(2.0), None
    rows_bot = []
    for d in sorted(x for x in news if x in idx):
        if prev:
            book.new_day()
        for tk, s, h in news[d]:
            book.add(tk, s, h, universe)
        prev = d
        i = idx[d]
        if i + 1 < len(dates):
            for t in tickers:
                rows_bot.append((book.get_ticker_only(t), rel_ret(t, d, dates[i + 1])))

    print(f"\n{len(rows)} stock-days with tagged news (returns relative to the 10-stock average)")
    print(f"{'horizon':12}{'Pearson':>10}{'Spearman':>10}{'t-stat':>9}   mean return: positive news / negative news")
    for k, name in ((1, "same day"), (2, "next day"), (3, "next 5 days")):
        xs, ys = [r[0] for r in rows], [r[k] for r in rows]
        c = corr(xs, ys)
        t = c * math.sqrt((len(xs) - 2) / max(1e-12, 1 - c * c))
        pos = [y for x, y in zip(xs, ys) if x > 0.1]
        neg = [y for x, y in zip(xs, ys) if x < -0.1]
        print(f"{name:12}{c:>10.3f}{corr(rank(xs), rank(ys)):>10.3f}{t:>9.2f}   "
              f"{100 * sum(pos) / max(1, len(pos)):+.2f}% (n={len(pos)}) / {100 * sum(neg) / max(1, len(neg)):+.2f}% (n={len(neg)})")
    xs, ys = [r[0] for r in rows_bot], [r[1] for r in rows_bot]
    c = corr(xs, ys)
    t = c * math.sqrt((len(xs) - 2) / max(1e-12, 1 - c * c))
    print(f"\nbot's decayed news score vs next-day return, all {len(xs)} stock-days: "
          f"Pearson {c:.3f}, Spearman {corr(rank(xs), rank(ys)):.3f}, t {t:.2f}")
    print("|t| < 2 = indistinguishable from chance")


if __name__ == "__main__":
    main()
