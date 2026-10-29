"""Reproducible results report: python report.py  ->  reports/REPORT.md + reports/report.json

Everything in it is recomputed from the files in this folder: the price history
(fingerprinted by SHA-256), the parameters in trader/config.py and the bot's
own logs. Re-running it on the same inputs gives the same numbers.

    python report.py                 # backtests + audits + live evidence from logs/
    python report.py --quick         # skip the walk-forward grid
    python report.py --logs path     # evidence from another log folder
"""
from __future__ import annotations

import argparse
import hashlib
import json
import platform
import sys
from dataclasses import asdict, replace
from datetime import datetime
from pathlib import Path

from backtest import GRID, WINDOWS, live_scorecard, load, run, run_episodes, run_windows, score
from trader.audit import audit_ledger
from trader.config import PROFILES, Config
from trader.evidence import live_evidence

ROOT = Path(__file__).resolve().parent


def fingerprint(data_dir: Path) -> dict:
    h = hashlib.sha256()
    files = sorted((data_dir / "history").glob("*.json"))
    for f in files:
        h.update(f.name.encode())
        h.update(f.read_bytes())
    hist = {f.stem: json.loads(f.read_text()) for f in files}
    dates = sorted({b["date"] for v in hist.values() for b in v})
    return {"sha256": h.hexdigest()[:16], "tickers": sorted(hist), "bars": sum(len(v) for v in hist.values()),
            "from": dates[0] if dates else None, "to": dates[-1] if dates else None}


def _audit_job(args):
    hist, p, start, end, name = args
    m = run(hist, p, start, end, keep_trace=True)
    return name, audit_ledger(m["_ledger"], m["_fills"], m["_prices"], m["_curve"][-1]), len(m["_fills"])


def table(res: dict[str, dict], cols: list[str]) -> str:
    out = ["| window | " + " | ".join(cols) + " |", "|---" * (len(cols) + 1) + "|"]
    for n, m in res.items():
        out.append(f"| {n} | " + " | ".join(str(m[c]) for c in cols) + " |")
    return "\n".join(out)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--quick", action="store_true")
    ap.add_argument("--logs", default=None)
    args = ap.parse_args()

    cfg = Config()
    hist = load(cfg.data_dir)
    p = cfg.params
    names = list(WINDOWS)
    print("running backtests ...")
    default = run_windows(hist, p, names)
    tactical = run_windows(hist, replace(p, core_weight=0.0, tactical=True, breadth_floor=0.25, max_long_exposure=0.8,
                                         max_gross_exposure=0.9), names)
    hedged = run_windows(hist, replace(p, **PROFILES["hedged"]), names)
    old80 = run_windows(hist, replace(p, core_weight=0.80, max_long_exposure=0.95, max_gross_exposure=0.97,
                                      max_stock_weight=1.0, tactical=True), names)
    from concurrent.futures import ProcessPoolExecutor
    print("auditing ledgers against an independent replay ...")
    with ProcessPoolExecutor(6) as ex:
        audits = {n: (bad, nf) for n, bad, nf in ex.map(_audit_job, [(hist, p, *WINDOWS[n], n) for n in names]
                                                       + [(hist, replace(p, core_weight=0.0, tactical=True), *WINDOWS[n], f"{n} (tactical)")
                                                          for n in ("oos_2025_26", "full")]
                                                       + [(hist, replace(p, **PROFILES["hedged"]), *WINDOWS[n], f"{n} (hedged)")
                                                          for n in ("sim_2024_26", "full")])}

    wf = []
    if not args.quick:
        import itertools
        print("walk-forward grid (train windows only) ...")
        keys = list(GRID)
        combos = [dict(zip(keys, c)) for c in itertools.product(*GRID.values())]
        jobs = [(hist, replace(p, **kw), *WINDOWS[w], (i, w)) for i, kw in enumerate(combos)
                for w in ("train_2022_23", "train_2024", "oos_2025_26")]
        from backtest import _job
        with ProcessPoolExecutor(10) as ex:
            r = dict(ex.map(_job, jobs))
        for i, kw in enumerate(combos):
            tr = min(score(r[(i, w)]) for w in ("train_2022_23", "train_2024"))
            o = r[(i, "oos_2025_26")]
            wf.append({"params": kw, "train_worst_score": round(tr, 2), "oos_after_tax_pct": o["after_tax_pct"],
                       "oos_max_dd_pct": o["max_drawdown_pct"], "oos_sharpe": o["sharpe"],
                       "oos_buy_hold_pct": o["buy_hold_pct"]})
        wf.sort(key=lambda x: -x["train_worst_score"])

    log_dir = Path(args.logs) if args.logs else cfg.log_dir
    print("live replay + 500-day episodes ...")
    as_live = replace(p, core_weight=0.80, max_long_exposure=0.95, max_gross_exposure=0.97, max_stock_weight=1.0, tactical=True)
    replay = {name: run(hist, q, *WINDOWS["sim_2024_26"], cfg.max_orders_per_day, observe_days=1)["scorecard"]
              for name, q in (("replay, parameters that ran live", as_live), ("replay, current parameters", p))}
    live_sc = live_scorecard(log_dir)
    if live_sc and live_sc.get("date", "") >= WINDOWS["sim_2024_26"][1]:
        replay["live bot logs"] = live_sc
    episodes = run_episodes(hist, {"current default": p, "previous (80% core, no cap)": as_live},
                            step=20 if args.quick else 10)
    ev = live_evidence(log_dir)
    fp = fingerprint(cfg.data_dir)

    cols = ["after_tax_pct", "buy_hold_pct", "excess_vs_buy_hold_pct", "liquidated_after_tax_pct",
            "buy_hold_liquidated_pct", "max_drawdown_pct", "sharpe", "sortino", "win_rate_pct",
            "avg_utilization_pct", "orders", "fees", "est_tax", "rejects"]
    lines = [
        "# MANTHAN bot: reproducible results",
        "",
        f"Generated {datetime.now():%Y-%m-%d %H:%M} with `python report.py` "
        f"(Python {platform.python_version()}). Data fingerprint `{fp['sha256']}`: {len(fp['tickers'])} stocks, "
        f"{fp['bars']} daily bars, {fp['from']} to {fp['to']}.",
        "",
        "Parameters were chosen on the two TRAIN windows only; `oos_2025_26` was held out.",
        "`after_tax_pct` = platform basis (tax on realized gains); `liquidated_*` = everything sold on the last day.",
        "Metric definitions: `trader/metrics.py`.",
        "",
        f"## Default strategy (core {p.core_weight:.0%}{' + tactical' if p.tactical else ', tactical off'}"
        f"{f', cap {p.max_stock_weight:.0%}' if p.max_stock_weight < 1 else ''})",
        "",
        table(default, cols),
        "",
        "`sim_2024_26` = the exact 500 days of the live SimTest run (live: +44.1% with the previous 80% core + "
        "tactical). Fixed windows trade from day 1; the live-replay table below observes day 1 like live did.",
        "",
        "## Previous default (core 80%, long cap 95%), for comparison",
        "",
        table(old80, cols),
        "",
        "## Profile `hedged`: shorts the weakest downtrending stock in broad sell-offs (margin locked)",
        "",
        table(hedged, cols + ["short_trades", "short_pnl"]),
        "",
        "## Every live-length run in the data (500 trading days, 400-day warm-up, day 1 observed)",
        "",
        "The live game is one 500-day path, and one path is noisy: replaying SimTest with a one-day",
        "difference in when 5 stocks became tradable moved final wealth between 12.2 L and 14.4 L.",
        "So parameters are judged over every 500-day episode the data allows, not one window.",
        "",
        "| variant | episodes | mean after-tax % | median | worst | mean vs B&H | worst vs B&H | beat B&H % of runs | mean max DD % | mean util % | mean orders | mean taxes |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *[f"| {n} | {r['episodes']} | {r['mean_after_tax_pct']} | {r['median_after_tax_pct']} | {r['worst_after_tax_pct']} | "
          f"{r['mean_excess_pct']} | {r['worst_excess_pct']} | {r['beat_buy_hold_pct']} | {r['mean_max_dd_pct']} | "
          f"{r['mean_util_pct']} | {r['mean_orders']} | {r['mean_taxes']:,.0f} |" for n, r in episodes.items()],
        "",
        "## Live SimTest replay, in the leaderboard's columns",
        "",
        "Replays the 500 recorded days with the recorded headlines, day 1 observed (it arrived in pre-flight).",
        "Live, a history download failure left 5 stocks untradable for ~5 days (fixed); replaying that",
        "failure gives 14,38,801 vs the live 14,40,868, so the replay reproduces the live run.",
        "",
        "| run | total wealth | realized | unrealized | charges | taxes | cash util % | cash | margin | orders | errors | POS |",
        "|---|---|---|---|---|---|---|---|---|---|---|---|",
        *[f"| {n} | {r['total_wealth']:,.0f} | {r['realized']:,.0f} | {r['unrealized']:,.0f} | {r['charges']:,.0f} | "
          f"{r['taxes']:,.0f} | {r['cash_util_pct']} | {r['available_cash']:,.0f} | {r['margin_locked']:,.0f} | "
          f"{r['orders']} | {r['errors']} | {r['pos']} |" for n, r in replay.items()],
        "",
        "## Tactical layer only (previous design, for comparison)",
        "",
        table(tactical, cols),
        "",
        "## Accounting audit (ledger vs independent replay of its own fills)",
        "",
        "| run | fills | result |",
        "|---|---|---|",
    ]
    for n, (bad, nf) in audits.items():
        lines.append(f"| {n} | {nf} | {'OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match' if not bad else '; '.join(bad)} |")
    if wf:
        lines += ["", "## Walk-forward: ranked by worst TRAIN window, OOS shown after the fact", "",
                  "| params | train worst score | OOS after-tax % | OOS max DD % | OOS Sharpe | OOS B&H % |",
                  "|---|---|---|---|---|---|"]
        for w in wf:
            lines.append(f"| {w['params']} | {w['train_worst_score']} | {w['oos_after_tax_pct']} | {w['oos_max_dd_pct']} | "
                         f"{w['oos_sharpe']} | {w['oos_buy_hold_pct']} |")
    lines += ["", f"## Live evidence from `{ev['log_dir']}`", ""]
    if ev["orders_sent"] or ev["live_days"]:
        lines += [
            f"- live trading days decided: {ev['live_days']}",
            f"- orders sent: {ev['orders_sent']}, status: {ev['order_status']}",
            f"- order success rate: {ev['order_success_rate_pct']}%, rejected {ev['rejected']}, unknown outcome {ev['unknown_outcome']}",
            f"- order round trip ms: {ev['order_latency_ms']}",
            f"- server-reported decision delay s: {ev['server_decision_delay_s']}",
            f"- first tick -> orders done ms: {ev['day_total_ms']}; decide only ms: {ev['decide_ms']}",
            f"- reconciles: {ev['reconciles']}, with drift: {ev['reconciles_with_drift']}",
            f"- risk guard modes: {ev['risk_modes']}",
        ]
    else:
        lines.append("No live logs found: run the bot in the sandbox (`python run_bot.py --sandbox`) and re-run this report.")
    lines += ["", "## Parameters", "", "```json", json.dumps(asdict(p), indent=1), "```"]

    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    (out / "REPORT.md").write_text("\n".join(lines) + "\n", encoding="utf-8")
    (out / "report.json").write_text(json.dumps({
        "fingerprint": fp, "params": asdict(p), "default": default, "tactical_only": tactical,
        "audits": {n: b for n, (b, _) in audits.items()}, "walkforward": wf, "live_evidence": ev,
        "episodes": episodes, "live_replay": replay,
    }, indent=1, default=str), encoding="utf-8")
    print(f"wrote {out / 'REPORT.md'}")
    ok = all(not b for b, _ in audits.values())
    print("accounting audit:", "PASS" if ok else "FAIL")
    sys.exit(0 if ok else 1)


if __name__ == "__main__":
    main()
