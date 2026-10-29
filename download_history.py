"""Download daily OHLCV history for every active ticker into data/history/.

Uses only public endpoints (no token needed). Anti-lookahead means we get every
bar before the sandbox's current simulated date, so run it again later in the
sandbox loop to collect more.

    python download_history.py [--days 1500]
"""
from __future__ import annotations

import argparse
import json

from trader.api import GatewayClient
from trader.config import Config


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--days", type=int, default=1500)
    args = ap.parse_args()

    cfg = Config()
    api = GatewayClient(cfg)
    stage = api.stage()
    tickers = stage.get("tickers") or []
    out = cfg.data_dir / "history"
    out.mkdir(parents=True, exist_ok=True)
    print(f"dataset: {stage.get('active_dataset_name')}  sim date: {stage.get('current_date')}")

    for t in tickers:
        new = api.history(t, days=args.days)
        path = out / f"{t}.json"
        old = json.loads(path.read_text()) if path.exists() else []
        merged = {b["date"]: b for b in old}
        merged.update({b["date"]: b for b in new})        # merge across sandbox loops
        bars = [merged[k] for k in sorted(merged)]
        path.write_text(json.dumps(bars))
        print(f"{t}: {len(bars)} bars {bars[0]['date']} -> {bars[-1]['date']}")


if __name__ == "__main__":
    main()
