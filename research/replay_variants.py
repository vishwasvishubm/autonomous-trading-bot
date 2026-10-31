"""SimTest replay scorecard per variant: python research/replay_variants.py research/eN.json"""
import json
import sys
from concurrent.futures import ProcessPoolExecutor
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest import WINDOWS, load, run  # noqa: E402
from trader.config import Config  # noqa: E402


def job(args):
    hist, q, name, orders = args
    start, end = WINDOWS["sim_2024_26"]
    m = run(hist, q, start, end, orders, observe_days=1)
    return name, m


if __name__ == "__main__":
    cfg = Config()
    hist = load(cfg.data_dir)
    variants = {n: replace(cfg.params, **kw) for n, kw in json.loads(open(sys.argv[1]).read()).items()}
    with ProcessPoolExecutor(max_workers=8) as ex:
        res = dict(ex.map(job, [(hist, q, n, cfg.max_orders_per_day) for n, q in variants.items()]))
    cols = ("wealth", "realized", "unrealized", "charges", "taxes", "util", "orders")
    print(f"{'variant':30}{'after tax %':>12}{'max DD %':>10}" + "".join(f"{c:>12}" for c in cols))
    for n in variants:
        m, s = res[n], res[n]["scorecard"]
        vals = [s.get({"wealth": "total_wealth", "util": "cash_util_pct"}.get(k, k), "") for k in cols]
        print(f"{n:30}{m['after_tax_pct']:>12}{m['max_drawdown_pct']:>10}"
              + "".join(f"{v:>12,.0f}" if isinstance(v, (int, float)) else f"{str(v):>12}" for v in vals))
