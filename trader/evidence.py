"""Turn the bot's own JSONL logs into reliability / latency / order-success evidence."""
from __future__ import annotations

import json
from collections import Counter
from pathlib import Path


def _read(path: Path) -> list[dict]:
    if not path.exists():
        return []
    rows = []
    for line in path.read_text(encoding="utf-8", errors="ignore").splitlines():
        try:
            rows.append(json.loads(line))
        except ValueError:
            pass
    return rows


def pct(values: list[float], q: float) -> float:
    if not values:
        return 0.0
    v = sorted(values)
    k = min(len(v) - 1, max(0, round(q / 100 * (len(v) - 1))))
    return round(v[k], 1)


def live_evidence(log_dir: Path) -> dict:
    orders = [o for o in _read(log_dir / "orders.jsonl") if o.get("mode") == "live"]
    decisions = _read(log_dir / "decisions.jsonl")
    reconciles = _read(log_dir / "reconcile.jsonl")
    live_days = [d for d in decisions if d.get("mode") == "live"]
    status = Counter(o.get("status") for o in orders)
    sent = sum(status[s] for s in ("FILLED", "PARTIAL", "REJECTED", "UNKNOWN"))
    lat = [o["latency_ms"] for o in orders if o.get("latency_ms") is not None]
    srv = [float(o["server_delay_s"]) for o in orders if o.get("server_delay_s") not in (None, "")]
    total = [d["total_ms"] for d in live_days if "total_ms" in d]
    decide = [d["decide_ms"] for d in live_days if "decide_ms" in d]
    drift = [r for r in reconciles if any(not x.startswith("tax:") for x in r.get("diffs", []))]
    errors = Counter(o.get("error") for o in orders if o.get("status") in ("REJECTED", "SKIPPED", "UNKNOWN"))
    return {
        "log_dir": str(log_dir),
        "live_days": len(live_days),
        "orders_sent": sent,
        "order_status": dict(status),
        "order_success_rate_pct": round(100 * (status["FILLED"] + status["PARTIAL"]) / sent, 2) if sent else None,
        "rejected": status["REJECTED"],
        "unknown_outcome": status["UNKNOWN"],
        "errors": {k: v for k, v in errors.items() if k},
        "order_latency_ms": {"p50": pct(lat, 50), "p95": pct(lat, 95), "max": max(lat) if lat else 0.0},
        "server_decision_delay_s": {"p50": pct(srv, 50), "p95": pct(srv, 95), "max": max(srv) if srv else 0.0},
        "day_total_ms": {"p50": pct(total, 50), "p95": pct(total, 95), "max": max(total) if total else 0.0},
        "decide_ms": {"p50": pct(decide, 50), "p95": pct(decide, 95), "max": max(decide) if decide else 0.0},
        "reconciles": len(reconciles),
        "reconciles_with_drift": len(drift),
        "risk_modes": dict(Counter((d.get("risk") or {}).get("mode", "n/a") for d in live_days)),
    }
