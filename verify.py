"""Pre-flight verification: one command, PASS/FAIL per check.

    python verify.py           # offline: tests, config, secrets, data screen, accounting/tax audit, metrics
    python verify.py --live    # + READ-ONLY checks against the gateway (GET requests only, no orders)
    python verify.py --live --expect-ready   # while the bot runs in pre-flight: platform must say READY

Exit code 0 only if every check passed.
"""
from __future__ import annotations

import argparse
import subprocess
import sys
import time
from dataclasses import replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent
sys.path.insert(0, str(ROOT))

from trader.config import Config  # noqa: E402

results: list[tuple[str, bool, str]] = []


def check(name: str, ok: bool, detail: str = "") -> bool:
    results.append((name, ok, detail))
    print(f"[{'PASS' if ok else 'FAIL'}] {name}" + (f"  -- {detail}" if detail else ""))
    return ok


def offline(cfg: Config) -> None:
    # 1. test suite
    t0 = time.time()
    r = subprocess.run([sys.executable, "-m", "unittest", "discover", "-s", "tests"], cwd=ROOT,
                       capture_output=True, text=True)
    tail = (r.stderr or r.stdout).strip().splitlines()
    check("unit + integration + end-to-end tests", r.returncode == 0,
          f"{tail[-3] if len(tail) >= 3 else tail} ({time.time() - t0:.0f}s)" if tail else "")

    # 2. configuration + secrets
    import package
    env_example = (ROOT / ".env.example").read_text(encoding="utf-8")
    token_line = [ln for ln in env_example.splitlines() if ln.startswith("BOT_ACCESS_TOKEN")]
    check(".env.example has no token", bool(token_line) and token_line[0].strip() == "BOT_ACCESS_TOKEN=")
    gi = (ROOT / ".gitignore").read_text(encoding="utf-8").split()
    check(".env is git-ignored", ".env" in gi)
    files = package.collect()
    check("submission excludes .env and logs", not any(str(f) == ".env" or str(f).startswith("logs") for f in files),
          f"{len(files)} files would ship")
    findings = package.scan(files, extra_secrets=package.live_secrets())
    check("secret scan of shipped files", not findings, "; ".join(findings))
    check("token configured for the bot (.env)", bool(cfg.token), "set BOT_ACCESS_TOKEN in .env" if not cfg.token else
          f"{len(cfg.token)} chars, value not shown")
    p = cfg.params
    sane = (0 <= p.core_weight <= p.max_long_exposure and p.max_gross_exposure <= 1.0 and p.max_long_exposure <= p.max_gross_exposure
            and 0 < p.dd_soft <= p.dd_hard and p.risk_per_trade <= 0.03 and cfg.requests_per_minute <= 120
            and cfg.max_orders_per_day >= 2)
    check("parameter sanity (exposure <= 100%, rate <= 120/min, dd_soft <= dd_hard)", sane)
    for name in ("STOP", "FLATTEN"):
        check(f"no {name} kill-switch file left behind", not (cfg.control_dir / name).exists(),
              f"delete {cfg.control_dir / name}" if (cfg.control_dir / name).exists() else "")

    # 3. data + universe screen
    from backtest import WINDOWS, load, run
    from trader.universe import screen
    hist = load(cfg.data_dir)
    check("price history present", len(hist) >= 1, f"{len(hist)} stocks")
    bad = {t: screen(t, [{"open": b.get("open", b["close"]), "high": b.get("high", b["close"]),
                          "low": b.get("low", b["close"]), **b} for b in bars]).reasons
           for t, bars in hist.items()}
    bad = {t: r for t, r in bad.items() if r}
    check("universe screen (liquidity, gaps, bad ticks, volatility)", not bad, str(bad) if bad else "all stocks pass")

    # 4. accounting + tax audit on real backtests (independent replay)
    from trader.audit import audit_ledger
    from trader.config import PROFILES
    hedged = replace(p, **PROFILES["hedged"])
    for label, q, w in (("default", p, "full"), ("default", p, "sim_2024_26"),
                        ("hedged (shorts + margin)", hedged, "sim_2024_26"),
                        ("tactical + shorts", replace(p, core_weight=0.0, tactical=True), "full"),
                        ("tactical + shorts", replace(p, core_weight=0.0, tactical=True), "oos_2025_26")):
        m = run(hist, q, *WINDOWS[w], keep_trace=True)
        issues = audit_ledger(m["_ledger"], m["_fills"], m["_prices"], m["_curve"][-1])
        check(f"accounting + tax audit: {label}, {w}", not issues,
              "; ".join(issues[:3]) if issues else f"{len(m['_fills'])} fills, {m['short_trades']} shorts, "
                                                  f"{m['tax_harvests']} harvests, tax {m['est_tax']:,.0f}")
        # 5. metrics recomputed from the raw curve
        from trader.metrics import max_drawdown, sharpe, daily_returns
        c = m["_curve"]
        ok = (abs(m["return_pct"] - round(100 * (c[-1] / 1e6 - 1), 2)) < 0.011
              and abs(m["max_drawdown_pct"] - round(100 * max_drawdown(c), 2)) < 0.011
              and abs(m["sharpe"] - round(sharpe(daily_returns(c)), 2)) < 0.011
              and abs(m["after_tax_pct"] - round(100 * ((c[-1] - m["est_tax"]) / 1e6 - 1), 2)) < 0.011
              and m["rejects"] == 0)
        check(f"metrics consistent with equity curve: {label}, {w}", ok,
              f"ret {m['return_pct']}%, after-tax {m['after_tax_pct']}%, dd {m['max_drawdown_pct']}%, "
              f"sharpe {m['sharpe']}, B&H {m['buy_hold_pct']}%")


    # 6. the backtester reproduces the live SimTest run (same days, recorded news, same live glitch)
    from backtest import live_scorecard
    live_sc = live_scorecard(cfg.log_dir)
    if live_sc and live_sc.get("date", "") >= WINDOWS["sim_2024_26"][1]:
        as_live = replace(p, core_weight=0.80, max_long_exposure=0.95, max_gross_exposure=0.97, max_stock_weight=1.0, tactical=True)
        m = run(hist, as_live, *WINDOWS["sim_2024_26"], observe_days=1,
                blind_until={t: 6 for t in ("MOTR", "ORCX", "PLNX", "ROUT", "SONA")})
        gap = m["scorecard"]["total_wealth"] / live_sc["total_wealth"] - 1
        check("replay reproduces the live SimTest wealth (within 1%)", abs(gap) < 0.01,
              f"replay {m['scorecard']['total_wealth']:,.0f} vs live {live_sc['total_wealth']:,.0f} ({100 * gap:+.2f}%)")


def live(cfg: Config, expect_ready: bool = False) -> None:
    """GET-only checks. Never places, modifies or cancels anything."""
    import requests
    from trader.api import ApiError, GatewayClient
    from trader.audit import rebuild_books
    from trader.evidence import pct
    from trader.universe import screen
    api = GatewayClient(cfg)
    lat: dict[str, list[float]] = {}

    def timed(name, fn, *a):
        t0 = time.perf_counter()
        try:
            return fn(*a)
        finally:
            lat.setdefault(name, []).append((time.perf_counter() - t0) * 1000)

    try:
        st = timed("stage", api.stage)
        check("gateway reachable, /stage", True, f"stage={st.get('stage')} status={st.get('status')} "
                                                  f"day {st.get('days_completed')}/{st.get('total_days')}")
    except (ApiError, requests.RequestException) as exc:
        check("gateway reachable, /stage", False, str(exc))
        return
    try:
        cash = timed("cash", api.cash)
        check("token accepted (/portfolio/cash)", True, f"available cash {cash.get('available_cash')}")
    except ApiError as exc:
        check("token accepted (/portfolio/cash)", False, f"{exc.status} {exc.code}: get a fresh token from the portal")
        return
    stocks = timed("stocks", api.stocks)
    tickers = sorted(s["ticker"] for s in stocks if s.get("ticker"))
    check("GET /stocks", bool(tickers), ", ".join(tickers))
    bad = {}
    for t in tickers:
        bars = timed("history", api.history, t, cfg.history_days)
        bars = [{"date": str(b["date"])[:10], "open": float(b.get("open") or b["close"]),
                 "high": float(b.get("high") or b["close"]), "low": float(b.get("low") or b["close"]),
                 "close": float(b["close"]), "volume": float(b.get("volume") or 0)} for b in bars if b.get("close")]
        r = screen(t, bars)
        if not r.ok:
            bad[t] = r.reasons
    check("live universe screen", not bad, str(bad) if bad else f"{len(tickers)} stocks pass")
    pf = timed("portfolio", api.portfolio)
    orders = timed("orders", api.orders, 500)
    tax = timed("tax", api.tax_report)
    holdings = {h["ticker"]: (-1 if h.get("is_short") else 1) * abs(int(h.get("quantity", 0)))
                for h in pf.get("holdings", []) or [] if int(h.get("quantity", 0) or 0)}
    check("GET /portfolio", True, f"wealth {pf.get('total_wealth')}, holdings {holdings or 'none'}")
    fills = []
    for o in sorted(orders, key=lambda o: str(o.get("executed_at") or o.get("created_at"))):
        if str(o.get("status", "FILLED")).upper() not in ("FILLED", "EXECUTED", "PARTIAL"):
            continue
        d = str(o.get("simulated_date") or o.get("date") or o.get("executed_at") or "")[:10]
        px = o.get("execution_price") or o.get("price")
        if not d or px is None:
            continue
        fills.append({"date": d, "ticker": o["ticker"], "action": str(o["action"]).upper(),
                      "qty": int(o.get("filled_quantity") or o.get("quantity")), "price": float(px)})
    try:
        rb = rebuild_books(fills)
        rq = {t: v["qty"] for t, v in rb["positions"].items()}
        same = rq == holdings
        check("order history replays to the server's holdings", same,
              f"{len(fills)} fills" + ("" if same else f"; replay {rq} vs server {holdings} (expected if the "
                                                       f"account was reset, history is >500 orders, or shorts were "
                                                       f"auto-settled)"))
    except (KeyError, ZeroDivisionError) as exc:
        check("order history replays to the server's holdings", False,
              f"history incomplete ({exc}); expected after a Reset Cash")
    filings = tax.get("annual_filings") or []
    check("GET /tax/report", True, f"{len(filings)} FY filings")
    try:
        bs = timed("botstatus", api.bot_status)
        detail = (f"ready={bs.get('is_ready')} kafka_connected={bs.get('kafka_connected')} "
                  f"consumers={bs.get('kafka_consumer_count')} heartbeat_received={bs.get('heartbeat_received')} "
                  f"last_seq={bs.get('last_seq')} (our group: {cfg.group_id})")
        check("GET /bot/status (platform readiness tracker)", True, detail)
        if expect_ready:
            check("platform sees our Kafka consumer", bool(bs.get("kafka_consumer_count")),
                  "" if bs.get("kafka_consumer_count") else
                  f"0 consumers for {cfg.group_id}: is the bot running? If yes, set TEAM_USERNAME to the exact "
                  f"portal spelling (e.g. lowercase) in .env and restart")
            check("platform says READY (heartbeat + Kafka)", bool(bs.get("is_ready")),
                  "" if bs.get("is_ready") else "needs pre-flight ticks arriving and our ACKs being received")
    except (ApiError, requests.RequestException) as exc:
        check("GET /bot/status (platform readiness tracker)", False, str(exc))
    for name, v in lat.items():
        print(f"       latency {name:9}: p50 {pct(v, 50):.0f} ms, p95 {pct(v, 95):.0f} ms, n={len(v)}")
    slow = {n: pct(v, 95) for n, v in lat.items() if pct(v, 95) > 1500}
    check("gateway GET latency p95 < 1.5 s", not slow, str(slow) if slow else "")


def main() -> int:
    ap = argparse.ArgumentParser()
    ap.add_argument("--live", action="store_true", help="also run read-only checks against the gateway")
    ap.add_argument("--expect-ready", action="store_true",
                    help="with --live, while the bot runs: require the platform to see our consumer + READY")
    args = ap.parse_args()
    cfg = Config()
    offline(cfg)
    if args.live or args.expect_ready:
        live(cfg, args.expect_ready)
    failed = [n for n, ok, _ in results if not ok]
    print(f"\n{len(results) - len(failed)}/{len(results)} checks passed" + (f"; FAILED: {failed}" if failed else ""))
    out = ROOT / "reports"
    out.mkdir(exist_ok=True)
    (out / "VERIFICATION.md").write_text(
        "# Verification run\n\n" + f"`python verify.py{' --live' if args.live else ''}` at {time.strftime('%Y-%m-%d %H:%M')}\n\n"
        "| check | result | detail |\n|---|---|---|\n" +
        "\n".join(f"| {n} | {'PASS' if ok else 'FAIL'} | {d.replace('|', '/')} |" for n, ok, d in results) + "\n",
        encoding="utf-8")
    return 0 if not failed else 1


if __name__ == "__main__":
    sys.exit(main())
