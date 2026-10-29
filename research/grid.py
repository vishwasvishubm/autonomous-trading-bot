"""Run a rank_proto grid given as JSON: python research/grid.py '{"lookback":[5,10]}' '{"contrarian":true}'"""
import itertools
import json
import sys
from concurrent.futures import ProcessPoolExecutor

import rank_proto as rp

if __name__ == "__main__":
    grid = json.loads(sys.argv[1])
    fixed = json.loads(sys.argv[2]) if len(sys.argv) > 2 else {}
    top = int(sys.argv[3]) if len(sys.argv) > 3 else 15
    dates, tick, px = rp.load()
    keys = list(grid)
    cfgs = [{**rp.BASE, **fixed, **dict(zip(keys, v))} for v in itertools.product(*grid.values())]
    with ProcessPoolExecutor(10) as ex:
        res = list(ex.map(rp.job, [(dates, px, c) for c in cfgs]))
    rows = sorted(((min(r["tr22_23"]["at"] - r["tr22_23"]["bh"], r["tr24"]["at"] - r["tr24"]["bh"]), c, r)
                   for c, r in zip(cfgs, res)), key=lambda x: -x[0])
    print("B&H:", {w: round(float(res[0][w]["bh"]), 1) for w in rp.WINDOWS})
    for wt, c, r in rows[:top]:
        print(f"{wt:6.1f} " + " ".join(f"{k}={c[k]}" for k in keys) + " | " +
              " ".join(f"{w}:{r[w]['at']:.0f}/{r[w]['dd']:.0f}" for w in rp.WINDOWS) +
              f" ord{r['full']['orders']} sh{r['full']['shorts']}")
