"""Grow the price history from what the live feed recorded.

Every run records the raw Kafka ticks to data/stream/ticks.jsonl. This splits them into
runs (a new run starts when the simulated date jumps backwards, e.g. a sandbox loop or a
new simulation), saves each run of at least MIN_RUN_DAYS to data/runs/<first>_<last>/history
(so `python backtest.py --data data/runs/...` replays exactly that run), and merges it into
data/history when it is the same dataset: on every date both have, every price agrees.
A run that disagrees (a different masked dataset) is kept apart, never mixed in.

    python update_history.py
"""
from __future__ import annotations

import json
from pathlib import Path

FIELDS = ("open", "high", "low", "close", "volume")
MIN_RUN_DAYS = 20


def recorded_runs(stream_file: Path) -> list[dict[str, dict[str, dict]]]:
    """price_tick events -> list of runs, each {ticker: {date: bar}}."""
    runs: list[dict[str, dict[str, dict]]] = []
    cur: dict[str, dict[str, dict]] = {}
    last = ""
    if not stream_file.exists():
        return runs
    with stream_file.open(encoding="utf-8") as fh:
        for line in fh:
            try:
                ev = json.loads(line)
            except ValueError:
                continue
            if ev.get("event_type") != "price_tick":
                continue
            data, d = ev.get("data") or {}, str(ev.get("simulated_date") or "")[:10]
            t = data.get("ticker")
            if not t or not d or data.get("close") is None:
                continue
            if last and d < last:                     # looped / new simulation
                runs.append(cur)
                cur = {}
            last = d
            cur.setdefault(t, {})[d] = {"date": d, **{k: float(data.get(k) or 0) for k in FIELDS}}
    if cur:
        runs.append(cur)
    return runs


def _agrees(a: dict, b: dict) -> bool:
    return all(abs(float(a.get(k, 0)) - float(b.get(k, 0))) <= 1e-6 * max(1.0, abs(float(b.get(k, 0))))
               for k in FIELDS)


def merge_stream_into_history(data_dir: Path) -> list[str]:
    """Save every recorded run and merge the consistent ones into data/history. Returns log lines."""
    hist_dir = data_dir / "history"
    hist_dir.mkdir(parents=True, exist_ok=True)
    history = {f.stem: {b["date"]: b for b in json.loads(f.read_text())} for f in hist_dir.glob("*.json")}
    notes = []
    for run in recorded_runs(data_dir / "stream" / "ticks.jsonl"):
        dates = sorted({d for bars in run.values() for d in bars})
        if len(dates) < MIN_RUN_DAYS:
            continue
        name = f"{dates[0]}_{dates[-1]}"
        run_dir = data_dir / "runs" / name / "history"
        run_dir.mkdir(parents=True, exist_ok=True)
        for t, bars in run.items():
            (run_dir / f"{t}.json").write_text(json.dumps([bars[d] for d in sorted(bars)]))
        clash = [(t, d) for t, bars in run.items() for d, b in bars.items()
                 if d in history.get(t, {}) and not _agrees(b, history[t][d])]
        if clash:
            notes.append(f"run {name}: {len(clash)} prices differ from data/history (e.g. {clash[0]}): "
                         f"a different dataset, kept only in data/runs/{name}")
            continue
        added = 0
        for t, bars in run.items():
            h = history.setdefault(t, {})
            for d, b in bars.items():
                if d not in h:
                    h[d] = b
                    added += 1
        notes.append(f"run {name}: {len(dates)} days, {len(run)} stocks, {added} new bars merged into data/history")
    for t, bars in history.items():
        (hist_dir / f"{t}.json").write_text(json.dumps([bars[d] for d in sorted(bars)]))
    return notes
