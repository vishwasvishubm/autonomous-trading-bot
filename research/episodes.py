"""Score variants over every 500-day episode: python research/episodes.py research/eN.json [step]"""
import json
import sys
from dataclasses import replace
from pathlib import Path

sys.path.insert(0, str(Path(__file__).resolve().parent.parent))
from backtest import load, print_episodes, run_episodes  # noqa: E402
from trader.config import Config  # noqa: E402

if __name__ == "__main__":
    cfg = Config()
    variants = {n: replace(cfg.params, **kw) for n, kw in json.loads(open(sys.argv[1]).read()).items()}
    step = int(sys.argv[2]) if len(sys.argv) > 2 else 10
    print_episodes(run_episodes(load(cfg.data_dir), variants, step=step))
