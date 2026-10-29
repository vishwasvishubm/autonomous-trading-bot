"""Fast prototype (numpy) for cross-sectional allocation ideas, before wiring the
winner into the real planner. Fees + 100% short margin + 10-day short settlement +
a flat 15% tax on net realized gains (rough: only used to rank ideas).

    python research/rank_proto.py
"""
from __future__ import annotations

import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from pathlib import Path

import numpy as np

ROOT = Path(__file__).resolve().parent.parent
WINDOWS = {"tr22_23": ("2022-11-15", "2023-12-31"), "tr24": ("2024-01-01", "2024-12-31"),
           "oos25_26": ("2025-01-01", "2026-12-31"), "sim": ("2024-10-25", "2026-10-23"),
           "full": ("2022-11-15", "2026-12-31")}


def load():
    files = sorted((ROOT / "data" / "history").glob("*.json"))
    data = {f.stem: {b["date"]: b["close"] for b in json.loads(f.read_text())} for f in files}
    dates = sorted(set.intersection(*(set(v) for v in data.values())))
    tick = list(data)
    px = np.array([[data[t][d] for t in tick] for d in dates])
    return dates, tick, px


def ema(x, n):
    out = np.empty_like(x)
    k = 2 / (n + 1)
    out[0] = x[0]
    for i in range(1, len(x)):
        out[i] = x[i] * k + out[i - 1] * (1 - k)
    return out


def sim(dates, px, start, end, cfg):
    L, K, R = cfg["lookback"], cfg["top_k"], cfg["rebal"]
    i0 = next(i for i, d in enumerate(dates) if d >= start)
    i1 = max(i for i, d in enumerate(dates) if d <= end)
    n = px.shape[1]
    e_tr = np.column_stack([ema(px[:, j], cfg["ema"]) for j in range(n)])
    rets = np.vstack([np.zeros(n), px[1:] / px[:-1] - 1])
    cash, qty, short_px, short_age = 1e6, np.zeros(n), np.zeros(n), np.zeros(n)
    avg = np.zeros(n)
    realized, fees, orders, shorts = 0.0, 0.0, 0, 0
    curve = []
    for i in range(i0, i1 + 1):
        p = px[i]
        # settlement: shorts squared off every 10 trading days
        if (i - i0) > 0 and (i - i0) % 10 == 0:
            for j in np.where(qty < 0)[0]:
                pnl = (short_px[j] - p[j]) * -qty[j]
                cash += short_px[j] * -qty[j] + pnl
                realized += pnl
                qty[j] = 0
        longv = (qty.clip(0) * p).sum()
        margin = (short_px * (-qty).clip(0)).sum()
        spnl = ((short_px - p) * (-qty).clip(0)).sum()
        w = cash + longv + margin + spnl
        if (i - i0) % R == 0 or cfg.get("daily_check"):
            lo = max(0, i - L)
            vol = rets[lo + 1:i + 1].std(0) * np.sqrt(max(1, i - lo)) + 1e-9
            mom = (p / px[lo] - 1) / (vol if cfg["risk_adj"] else 1)
            if cfg.get("skip"):
                s = max(0, i - cfg["skip"])
                mom = (px[s] / px[lo] - 1) / (vol if cfg["risk_adj"] else 1)
            up = p > e_tr[i]
            order = np.argsort(mom if cfg.get("contrarian") else -mom)
            tgt = np.zeros(n)
            longs = [j for j in order[:K] if (up[j] or not cfg["trend_filter"])]
            core = cfg["core"]
            tgt[:] = core / n
            for j in longs:
                tgt[j] += (cfg["gross_long"] - core) / max(1, K)
            if cfg["n_short"]:
                cand = [j for j in order[::-1][:cfg["n_short"]] if not up[j] and mom[j] < 0]
                for j in cand:
                    tgt[j] = -cfg["short_w"]
            # trade toward targets, sells first
            cur = np.where(qty > 0, qty * p, -(-qty).clip(0) * short_px) / w
            diff = tgt - cur
            for j in np.argsort(diff):
                if abs(diff[j]) < cfg["band"] and not (tgt[j] < 0 <= qty[j]) and not (tgt[j] >= 0 > qty[j]):
                    continue
                if qty[j] < 0 and tgt[j] >= 0:                       # cover
                    pnl = (short_px[j] - p[j]) * -qty[j]
                    cash += short_px[j] * -qty[j] + pnl - 20
                    realized += pnl
                    fees += 20
                    orders += 1
                    qty[j] = 0
                if tgt[j] < 0 and qty[j] > 0:                        # sell long before shorting
                    cash += qty[j] * p[j] - 35.34
                    realized += (p[j] - avg[j]) * qty[j]
                    fees += 35.34
                    orders += 1
                    qty[j] = 0
                if tgt[j] < 0 and qty[j] == 0:
                    q = int(-tgt[j] * w // p[j])
                    if q * p[j] < 25_000 or cash - q * p[j] < 0.03 * w:
                        continue
                    cash -= q * p[j] + 20
                    fees += 20
                    orders += 1
                    shorts += 1
                    qty[j], short_px[j] = -q, p[j]
                    continue
                if tgt[j] >= 0:
                    want = int(tgt[j] * w // p[j])
                    dq = want - qty[j]
                    if dq < 0 and (-dq * p[j] > 25_000 or want == 0):
                        cash += -dq * p[j] - 35.34
                        realized += (p[j] - avg[j]) * -dq
                        fees += 35.34
                        orders += 1
                        qty[j] = want
                    elif dq > 0 and dq * p[j] > 25_000:
                        dq = min(dq, int((cash - 0.03 * w) // p[j]))
                        if dq * p[j] < 25_000:
                            continue
                        avg[j] = (avg[j] * qty[j] + dq * p[j]) / (qty[j] + dq)
                        cash -= dq * p[j] + 20
                        fees += 20
                        orders += 1
                        qty[j] += dq
        longv = (qty.clip(0) * p).sum()
        margin = (short_px * (-qty).clip(0)).sum()
        spnl = ((short_px - p) * (-qty).clip(0)).sum()
        curve.append(cash + longv + margin + spnl)
    c = np.array(curve)
    unreal = ((px[i1] - avg) * qty.clip(0)).sum()
    tax = 0.15 * max(0.0, realized + unreal)            # liquidated basis, rough
    dd = (1 - c / np.maximum.accumulate(c)).max()
    bh = (px[i1] / px[i0]).mean() - 1
    r = np.diff(c) / c[:-1]
    return {"ret": 100 * (c[-1] / 1e6 - 1), "at": 100 * ((c[-1] - tax) / 1e6 - 1), "dd": 100 * dd,
            "sh": r.mean() / (r.std() + 1e-12) * np.sqrt(250), "bh": 100 * bh, "orders": orders, "shorts": shorts}


BASE = dict(lookback=120, top_k=3, rebal=10, ema=100, risk_adj=True, trend_filter=True, core=0.0,
            gross_long=0.95, n_short=0, short_w=0.1, band=0.03)


def job(a):
    dates, px, cfg = a
    return {w: sim(dates, px, *WINDOWS[w], cfg) for w in WINDOWS}


def main():
    dates, tick, px = load()
    grid = {"lookback": [60, 120, 200], "top_k": [2, 3, 4, 5], "rebal": [5, 10, 20], "core": [0.0, 0.4, 0.6],
            "n_short": [0, 2], "trend_filter": [True]}
    if len(sys.argv) > 1 and sys.argv[1] == "--quick":
        grid = {"lookback": [120], "top_k": [3], "rebal": [10], "core": [0.0], "n_short": [0, 2]}
    keys = list(grid)
    cfgs = [{**BASE, **dict(zip(keys, v))} for v in itertools.product(*grid.values())]
    with ProcessPoolExecutor(10) as ex:
        res = list(ex.map(job, [(dates, px, c) for c in cfgs]))
    rows = []
    for c, r in zip(cfgs, res):
        worst_train = min(r["tr22_23"]["at"] - r["tr22_23"]["bh"], r["tr24"]["at"] - r["tr24"]["bh"])
        rows.append((worst_train, c, r))
    rows.sort(key=lambda x: -x[0])
    bh = {w: res[0][w]["bh"] for w in WINDOWS}
    print("B&H:", {w: round(v, 1) for w, v in bh.items()})
    for wt, c, r in rows[:25]:
        print(f"{wt:6.1f} " + " ".join(f"{k}={c[k]}" for k in keys) + " | " +
              " ".join(f"{w}:{r[w]['at']:.0f}/{r[w]['dd']:.0f}" for w in WINDOWS) + f" sh{r['sim']['shorts']}")


if __name__ == "__main__":
    main()
