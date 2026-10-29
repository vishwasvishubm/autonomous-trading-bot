"""Does our news sentiment predict returns? Uses recorded stream data.

    python analyze_news.py

Joins data/stream/news.jsonl with closes from data/stream/ticks.jsonl (or
data/history) and reports the average next-day / 3-day return by sentiment bucket,
plus the most frequent headline words we don't score yet (lexicon candidates).
"""
from __future__ import annotations

import json
import re
from collections import Counter, defaultdict

from trader.config import Config
from trader.sentiment import PHRASES, WORDS, score_news


def read_jsonl(path):
    if not path.exists():
        return []
    out = []
    for line in path.read_text(encoding="utf-8").splitlines():
        try:
            out.append(json.loads(line))
        except ValueError:
            pass
    return out


def main() -> None:
    cfg = Config()
    closes: dict[str, dict[str, float]] = defaultdict(dict)
    for f in (cfg.data_dir / "history").glob("*.json"):
        for b in json.loads(f.read_text()):
            closes[f.stem][b["date"]] = b["close"]
    for ev in read_jsonl(cfg.data_dir / "stream" / "ticks.jsonl"):
        d = ev.get("data") or {}
        if d.get("ticker") and d.get("close"):
            closes[d["ticker"]][str(ev.get("simulated_date"))[:10]] = float(d["close"])
    dates = {t: sorted(v) for t, v in closes.items()}

    news = read_jsonl(cfg.data_dir / "stream" / "news.jsonl")
    if not news:
        print("no recorded news yet: run record_stream.py (or the bot) for a while first")
        return
    buckets = defaultdict(lambda: {"n": 0, "r1": 0.0, "r3": 0.0})
    unknown = Counter()
    seen = set()
    for ev in news:
        d = ev.get("data") or {}
        t, day = d.get("ticker"), str(ev.get("simulated_date") or d.get("date"))[:10]
        key = (t, day, d.get("headline"))
        if key in seen or t not in dates or day not in closes[t]:
            continue
        seen.add(key)
        s, _ = score_news(d.get("headline", ""), d.get("description", ""))
        ds = dates[t]
        i = ds.index(day)
        if i + 3 >= len(ds):
            continue
        c0 = closes[t][day]
        b = "strong +" if s > 0.5 else "mild +" if s > 0.1 else "strong -" if s < -0.5 else "mild -" if s < -0.1 else "neutral"
        buckets[b]["n"] += 1
        buckets[b]["r1"] += closes[t][ds[i + 1]] / c0 - 1
        buckets[b]["r3"] += closes[t][ds[i + 3]] / c0 - 1
        for w in re.findall(r"[a-z]+", (d.get("headline") or "").lower()):
            if w not in WORDS and len(w) > 3 and not any(w in p for p in PHRASES):
                unknown[w] += 1
    print(f"{'bucket':10} {'n':>5} {'next-day':>9} {'3-day':>8}")
    for b in ("strong +", "mild +", "neutral", "mild -", "strong -"):
        v = buckets[b]
        if v["n"]:
            print(f"{b:10} {v['n']:5d} {100 * v['r1'] / v['n']:8.2f}% {100 * v['r3'] / v['n']:7.2f}%")
    print("\nfrequent unscored headline words:", ", ".join(f"{w}({c})" for w, c in unknown.most_common(30)))


if __name__ == "__main__":
    main()
