import sys, json
sys.path.insert(0, r"D:\trading_bot\manthan-bot")
from dataclasses import replace
from concurrent.futures import ProcessPoolExecutor
from backtest import load, run, WINDOWS, score
from trader.config import Config

OFF = dict(dd_soft=1.0, dd_hard=1.0, daily_loss_limit=1.0)
WINS = ["train_2022_23", "train_2024", "oos_2025_26", "sim_2024_26", "full"]

def job(a):
    name, kw, w = a
    p = replace(Config().params, **kw)
    return (name, w), run(HIST, p, *WINDOWS[w])

HIST = load(Config().data_dir)

def main(variants):
    jobs = [(n, kw, w) for n, kw in variants.items() for w in WINS]
    with ProcessPoolExecutor(12) as ex:
        res = dict(ex.map(job, jobs))
    print(f"{'variant':28}" + "".join(f"{w[:14]:>31}" for w in WINS) + "   trainWorst")
    for n in variants:
        cells = []
        for w in WINS:
            m = res[(n, w)]
            cells.append(f"{m['after_tax_pct']:5.1f}|{m['liquidated_after_tax_pct']:5.1f}/{m['max_drawdown_pct']:4.1f}/{m['win_rate_pct']:4.0f}/{m['short_pnl']/1000:5.1f}k/{m['avg_utilization_pct']:3.0f}u")
        tw = min(score(res[(n, w)]) for w in WINS[:2])
        print(f"{n:28}" + "".join(f"{c:>31}" for c in cells) + f"   {tw:6.2f}")
    bh = res[(next(iter(variants)), WINS[0])]
    first = next(iter(variants))
    print("B&H platform basis:", {w: res[(first, w)]["buy_hold_pct"] for w in WINS})
    print("B&H liquidated    :", {w: res[(first, w)]["buy_hold_liquidated_pct"] for w in WINS})
    print("cells = after-tax platform|liquidated % / maxDD% / win% / short P&L / utilization%")

if __name__ == "__main__":
    variants = json.loads(open(sys.argv[1]).read())
    main(variants)
