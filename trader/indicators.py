"""Technical indicators on plain Python lists (oldest -> newest).

10 stocks x a few hundred bars is tiny, so we recompute from scratch each day:
simpler to reason about and impossible to drift out of sync.
"""
from __future__ import annotations

import math
from typing import Sequence


def ema(values: Sequence[float], window: int) -> float:
    if not values:
        return float("nan")
    k = 2.0 / (window + 1)
    out = values[0]
    for v in values[1:]:
        out = v * k + out * (1 - k)
    return out


def sma(values: Sequence[float], window: int) -> float:
    tail = values[-window:]
    return sum(tail) / len(tail) if tail else float("nan")


def stdev(values: Sequence[float], window: int) -> float:
    tail = values[-window:]
    if len(tail) < 2:
        return 0.0
    m = sum(tail) / len(tail)
    return math.sqrt(sum((x - m) ** 2 for x in tail) / (len(tail) - 1))


def rsi(closes: Sequence[float], window: int = 14) -> float:
    """Wilder's RSI."""
    if len(closes) <= window:
        return 50.0
    gains = losses = 0.0
    for i in range(1, window + 1):
        d = closes[i] - closes[i - 1]
        gains += max(d, 0.0)
        losses += max(-d, 0.0)
    avg_g, avg_l = gains / window, losses / window
    for i in range(window + 1, len(closes)):
        d = closes[i] - closes[i - 1]
        avg_g = (avg_g * (window - 1) + max(d, 0.0)) / window
        avg_l = (avg_l * (window - 1) + max(-d, 0.0)) / window
    if avg_l == 0:
        return 100.0 if avg_g > 0 else 50.0
    rs = avg_g / avg_l
    return 100.0 - 100.0 / (1.0 + rs)


def atr(highs: Sequence[float], lows: Sequence[float], closes: Sequence[float], window: int = 14) -> float:
    """Wilder's average true range."""
    n = len(closes)
    if n < 2:
        return (highs[-1] - lows[-1]) if n else 0.0
    trs = [highs[0] - lows[0]]
    for i in range(1, n):
        trs.append(max(highs[i] - lows[i], abs(highs[i] - closes[i - 1]), abs(lows[i] - closes[i - 1])))
    if n <= window:
        return sum(trs) / len(trs)
    out = sum(trs[:window]) / window
    for tr in trs[window:]:
        out = (out * (window - 1) + tr) / window
    return out


def efficiency_ratio(closes: Sequence[float], window: int = 20) -> float:
    """Kaufman efficiency ratio in [0, 1]: 1 = clean trend, 0 = pure noise."""
    if len(closes) <= window:
        return 0.0
    change = abs(closes[-1] - closes[-1 - window])
    path = sum(abs(closes[i] - closes[i - 1]) for i in range(len(closes) - window, len(closes)))
    return change / path if path > 0 else 0.0


def clip(x: float, lo: float = -1.0, hi: float = 1.0) -> float:
    return lo if x < lo else hi if x > hi else x


def features(bars: Sequence[dict], p) -> dict | None:
    """All indicator values the strategy needs, or None if history is too short."""
    need = max(p.ema_slow, p.bb_window, p.er_window, p.mom_window, p.atr_window, p.ema_trend + 10) + 5
    if len(bars) < need:
        return None
    closes = [b["close"] for b in bars]
    highs = [b["high"] for b in bars]
    lows = [b["low"] for b in bars]
    vols = [b.get("volume", 0) or 0 for b in bars]
    c = closes[-1]

    a = atr(highs, lows, closes, p.atr_window)
    sd = stdev(closes, p.bb_window)
    mid = sma(closes, p.bb_window)
    # daily return volatility over the momentum window, for risk-adjusted momentum
    rets = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - p.mom_window, len(closes))]
    rv = stdev(rets, len(rets)) or 1e-9
    mom = (c / closes[-1 - p.mom_window] - 1) / (rv * math.sqrt(p.mom_window))
    # long-horizon risk-adjusted return, used to rank stocks against each other
    n_rs = min(getattr(p, "rs_window", 100), len(closes) - 1)
    rets_l = [closes[i] / closes[i - 1] - 1 for i in range(len(closes) - n_rs, len(closes))]
    rv_l = stdev(rets_l, len(rets_l)) or 1e-9
    mom_long = (c / closes[-1 - n_rs] - 1) / (rv_l * math.sqrt(n_rs))
    avg_vol = sma(vols[:-1], p.vol_window) if len(vols) > 1 else 0
    # volume flow: share of the last vol_flow_window days' volume traded on up days minus
    # down days, in [-1, 1] (+1 = all volume on up days: accumulation; -1 = distribution)
    n_vf = min(getattr(p, "vol_flow_window", 50), len(closes) - 1)
    up = sum(vols[i] for i in range(len(closes) - n_vf, len(closes)) if closes[i] > closes[i - 1])
    down = sum(vols[i] for i in range(len(closes) - n_vf, len(closes)) if closes[i] < closes[i - 1])
    # long-term regime: where price sits vs the slow EMA, and which way that EMA is sloping
    span = closes[-4 * p.ema_trend:]
    ema_t = ema(span, p.ema_trend)
    ema_t_prev = ema(span[:-10], p.ema_trend)

    return {
        "ema_trend": ema_t,
        "trend_slope": (ema_t / ema_t_prev - 1) / (a / c) if a and ema_t_prev else 0.0,
        "close": c,
        "ema_fast": ema(closes[-4 * p.ema_slow:], p.ema_fast),
        "ema_slow": ema(closes[-4 * p.ema_slow:], p.ema_slow),
        "er": efficiency_ratio(closes, p.er_window),
        "rsi": rsi(closes[-6 * p.rsi_window:], p.rsi_window),
        "bb_z": (c - mid) / sd if sd > 0 else 0.0,
        "atr": a,
        "atr_pct": a / c if c else 0.0,
        "mom": mom,
        "mom_long": mom_long,
        "vol_long": rv_l,                 # stdev of daily returns over rs_window (core weighting)
        "vol_ratio": (vols[-1] / avg_vol) if avg_vol > 0 else 1.0,
        "vol_flow": (up - down) / (up + down) if up + down > 0 else 0.0,
        "ret1": c / closes[-2] - 1 if closes[-2] else 0.0,
    }
