"""Live bot dashboard for monitoring and the jury walkthrough.

    streamlit run dashboard.py

Reads only the bot's own files (logs/state.json, logs/decisions.jsonl), so it adds
zero load on the competition API.
"""
from __future__ import annotations

import json
import os
import time
from collections import deque

import pandas as pd
import streamlit as st

from trader.config import Config

cfg = Config()
st.set_page_config(page_title="MANTHAN Bot", layout="wide")
REFRESH = float(os.environ.get("DASH_REFRESH", "3"))   # seconds; 0 disables auto-refresh


def load_state() -> dict:
    try:
        return json.loads((cfg.log_dir / "state.json").read_text(encoding="utf-8"))
    except (OSError, ValueError):
        return {}


def load_curve(limit: int = 2000) -> pd.DataFrame:
    path = cfg.log_dir / "decisions.jsonl"
    if not path.exists():
        return pd.DataFrame()
    with open(path, encoding="utf-8") as fh:
        rows = deque(fh, maxlen=limit)
    recs = []
    for line in rows:
        try:
            r = json.loads(line)
        except ValueError:
            continue
        if "wealth" in r:
            recs.append({"date": r["date"], "wealth": r["wealth"], "cash": r["cash"], "latency_ms": r["total_ms"]})
    return pd.DataFrame(recs)


s = load_state()
st.title("MANTHAN 2026 Â· Autonomous Trading Bot")
if not s:
    st.info("Waiting for the bot to write logs/state.json â€” start it with `python run_bot.py`.")
else:
    stage = s.get("stage") or {}
    stats = s.get("stats") or {}
    st.caption(f"Stage **{stage.get('stage', '?')} / {stage.get('status', '?')}** Â· mode **{s.get('mode')}** Â· "
               f"sim date **{s.get('date')}** Â· day {s.get('day')} Â· Kafka {s.get('kafka_connected')} Â· "
               f"rate tokens {s.get('rate_tokens')}")
    c = st.columns(6)
    wealth = s.get("wealth", 0)
    c[0].metric("Total wealth (local)", f"â‚¹{wealth:,.0f}", f"{(wealth / 1_000_000 - 1) * 100:+.2f}%")
    c[1].metric("Cash", f"â‚¹{s.get('cash', 0):,.0f}")
    c[2].metric("Cash utilization", f"{(1 - s.get('cash', 0) / wealth) * 100 if wealth else 0:.0f}%")  # deployed / wealth
    c[3].metric("Fees / est. tax", f"â‚¹{s.get('fees', 0):,.0f} / â‚¹{s.get('est_tax', 0):,.0f}")
    c[4].metric("Orders filled / rejected", f"{stats.get('orders_filled', 0)} / {stats.get('orders_rejected', 0)}")
    c[5].metric("Decision latency avg / max", f"{stats.get('latency_ms_avg', 0):.0f} / {stats.get('latency_ms_max', 0):.0f} ms")

    hb = s.get("heartbeat") or {}
    plat = hb.get("platform") or {}
    if plat:
        ready = plat.get("is_ready")
        (st.success if ready else st.info)(
            f"Platform readiness: **{'READY' if ready else 'not ready'}** · Kafka consumers seen "
            f"{plat.get('kafka_consumer_count')} · heartbeat received {plat.get('heartbeat_received')} · "
            f"platform last seq {plat.get('last_seq')} · our last ACK {hb.get('acked_seq')} "
            f"({hb.get('acked')} sent, {hb.get('queued')} queued)")
    risk = s.get("risk") or {}
    msg = (f"Risk guard **{risk.get('mode', 'n/a')}** · drawdown {100 * risk.get('drawdown', 0):.1f}% · "
           f"today {100 * risk.get('day_return', 0):+.2f}% · " + "; ".join(risk.get("reasons") or []))
    (st.warning if risk.get("mode", "normal") != "normal" else st.caption)(msg)
    blocked = {t: r["reasons"] for t, r in (s.get("screen") or {}).items() if not r.get("ok")}
    if blocked:
        st.warning(f"Universe screen blocks new entries in: {blocked}")

    curve = load_curve()
    if not curve.empty:
        st.subheader("Wealth")
        st.line_chart(curve.set_index("date")[["wealth"]], height=220)

    left, right = st.columns([1, 1])
    with left:
        st.subheader("Positions")
        pos = pd.DataFrame(s.get("positions") or [])
        st.dataframe(pos if not pos.empty else pd.DataFrame({"": ["no open positions"]}), hide_index=True,
                     width="stretch")
        st.subheader("Recent orders (why each trade happened)")
        orders = pd.DataFrame(s.get("orders") or [])
        if not orders.empty:
            cols = [k for k in ("date", "action", "ticker", "qty", "price", "status", "error", "latency_ms", "reason")
                    if k in orders]
            st.dataframe(orders[cols], hide_index=True, width="stretch")
            with st.expander("Full explanation of the latest orders (signal, confidence, sizing, risk checks)"):
                for o in (s.get("orders") or [])[:8]:
                    st.markdown(f"**{o.get('date')} {o.get('action')} {o.get('ticker')} x{o.get('qty')}**: "
                                f"{o.get('status')}")
                    st.json(o.get("explain") or {}, expanded=False)
    with right:
        st.subheader("Today's signals")
        sig = s.get("signals") or {}
        if sig:
            rows = [{"ticker": t, "score": v["score"], "close": v["close"], "RSI": v["rsi"], "ER": v["er"],
                     "news": v["news"], **{k: v["parts"].get(k, 0) for k in ("regime", "trend", "meanrev", "momentum")},
                     "why": " Â· ".join(v["reasons"][:3])} for t, v in sig.items()]
            st.dataframe(pd.DataFrame(rows).sort_values("score", ascending=False), hide_index=True,
                         width="stretch")
        st.subheader("News flow")
        news = pd.DataFrame(s.get("news") or [])
        if not news.empty:
            st.dataframe(news[["date", "ticker", "score", "headline"]], hide_index=True, width="stretch")

if REFRESH > 0:
    time.sleep(REFRESH)
    st.rerun()
