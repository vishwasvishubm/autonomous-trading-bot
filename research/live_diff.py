"""Compare the live SimTest orders with the backtest replay of the same days."""
import json
import sys
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent
sys.path.insert(0, str(ROOT))
from backtest import WINDOWS, load, run  # noqa: E402
from trader.config import Config  # noqa: E402

cfg = Config()
hist = load(cfg.data_dir)
q = replace(cfg.params, core_weight=0.80, max_long_exposure=0.95, max_gross_exposure=0.97, max_stock_weight=1.0, tactical=True)
kw = dict(arg.split("=") for arg in sys.argv[1:])
m = run(hist, q, *WINDOWS["sim_2024_26"], keep_trace=True, observe_days=int(kw.get("observe", 1)),
        history_days=int(kw.get("hist", 400)),
        # live: at the switch to simulation 5 history downloads were skipped; retried ~5 days later
        blind_until={t: int(kw.get("blind", 0)) for t in ("MOTR", "ORCX", "PLNX", "ROUT", "SONA")})
rep = [(f["date"], f["ticker"], f["action"], f["qty"]) for f in m["_fills"] if not f.get("settlement")]
live = [(o["date"], o["ticker"], o["action"], o.get("filled_qty") or o["qty"])
        for o in map(json.loads, open(ROOT / "logs" / "orders.jsonl")) if o.get("status") == "FILLED"]
print("wealth", m["scorecard"]["total_wealth"])
for i in range(12):
    print(f"{str(live[i]) if i < len(live) else '':45} {rep[i] if i < len(rep) else ''}")
