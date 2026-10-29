"""Signal model: one explainable score in [-1, 1] per stock per day.

score =  trend      (EMA fast vs slow, in ATR units; trusted more when the market trends)
       + meanrev    (Bollinger z-score + RSI; only used when the market is range-bound)
       + momentum   (risk-adjusted 20-day return)
       + news       (decaying lexicon sentiment)

The regime switch comes from Kaufman's efficiency ratio (ER): with a high ER, price
moves are directional and we follow the trend; with a low ER, price is noisy and we
fade extremes instead. This stops the classic failure of trend systems in sideways
markets (many small losing trades eaten by fees).
"""
from __future__ import annotations

from dataclasses import dataclass, field

from .indicators import clip


@dataclass
class Signal:
    ticker: str
    score: float
    parts: dict[str, float]
    feats: dict[str, float]
    reasons: list[str] = field(default_factory=list)
    weak: bool = False        # relative loser in a confirmed downtrend: no core, short candidate


def regime_state(prev: float | None, f: dict, p) -> float:
    """Long-term regime with hysteresis: +1 uptrend, -1 downtrend, 0 undecided.

    A plain "close above/below the slow EMA" test flips back and forth every time
    price wiggles around the average, and each flip was a round trip of fees and a
    small loss (seen live in the sandbox). Here the price must clear the EMA by
    `regime_band_atr` ATRs to *change* regime; inside the band the previous regime
    is kept.
    """
    band = p.regime_band_atr * f["atr"]
    c, e, slope = f["close"], f["ema_trend"], f["trend_slope"]
    up_break = c > e + band and slope > 0
    down_break = c < e - band and slope < 0
    if prev is None or prev == 0:
        return 1.0 if up_break else -1.0 if down_break else 0.0
    if prev > 0:
        if c < e - band:                      # clearly lost the trend
            return -1.0 if slope < 0 else 0.0
        return 1.0
    if c > e + band:
        return 1.0 if slope > 0 else 0.0
    return -1.0


def evaluate(ticker: str, f: dict, news: float, p, regimes: dict | None = None) -> Signal:
    """Score one stock. `regimes` (ticker -> last regime) carries hysteresis state
    between days; pass the same dict every day (None = no memory)."""
    atr = f["atr"] or 1e-9
    tw = clip(f["er"] / p.er_trend_threshold, 0.0, 1.0)          # trend weight 0..1

    trend = clip((f["ema_fast"] - f["ema_slow"]) / atr / 1.5)
    meanrev = 0.5 * clip(-f["bb_z"] / 2.5) + 0.5 * clip((50.0 - f["rsi"]) / 25.0)
    mom = clip(f["mom"] / 2.5)

    # don't catch a falling knife: bad news cancels a "buy the dip" signal
    if meanrev > 0 and news < -0.3:
        meanrev *= 0.3
    if meanrev < 0 and news > 0.3:
        meanrev *= 0.3

    # regime: +1 confirmed uptrend (clear of a rising slow EMA), -1 confirmed downtrend
    regime = regime_state(regimes.get(ticker) if regimes is not None else None, f, p)
    if regimes is not None:
        regimes[ticker] = regime

    parts = {
        "regime": p.w_regime * regime,
        "trend": p.w_trend * trend * (0.4 + 0.6 * tw),
        "meanrev": p.w_meanrev * meanrev * (1.0 - tw),
        "momentum": p.w_momentum * mom,
        "news": p.w_news * news,
    }
    score = sum(parts.values())
    # a high-volume day confirms the move in the signal's direction
    if f["vol_ratio"] > 1.5 and score * parts["trend"] > 0:
        score *= 1.15
        parts["volume_boost"] = 0.15
    score = clip(score)

    return Signal(ticker, score, parts, f, _reasons(parts, f, tw))


def _reasons(parts: dict, f: dict, tw: float) -> list[str]:
    reasons = [f"{k} {v:+.2f}" for k, v in sorted(parts.items(), key=lambda kv: -abs(kv[1])) if abs(v) >= 0.05]
    reasons.append(f"ER {f['er']:.2f} ({'trend' if tw >= 1 else 'range' if tw < 0.5 else 'mixed'})")
    reasons.append(f"RSI {f['rsi']:.0f}, z {f['bb_z']:+.1f}")
    return reasons


def cross_section(signals: dict[str, Signal], p, weak_state: dict | None = None) -> None:
    """Rank every stock against its peers by risk-adjusted `rs_window` return.

    1. Relative-strength tilt (w_rs): per-stock scores only say whether a stock is going
       up; with 10 stocks and capped exposure the question is *which* to hold. Rank, not
       raw value, so one outlier can't dominate.
    2. Weak flag (weak_bottom_k): a stock in the bottom k of the ranking AND in a confirmed
       downtrend (regime -1, fast EMA below slow), while market breadth is at most
       `weak_max_breadth`, is "weak": the passive core drops it
       (its money goes to the other stocks) and it becomes a short candidate. Hysteresis:
       once weak it stays weak while the downtrend holds and it ranks in the bottom half,
       so a stock doesn't flip in and out of the core on rank noise. `weak_state` carries
       that memory between days (pass the same dict every day).
    """
    if len(signals) < 3:
        return
    ranked = sorted(signals.values(), key=lambda s: s.feats["mom_long"])
    n = len(ranked)
    breadth = sum(1 for s in ranked if s.feats["close"] > s.feats["ema_trend"]) / n
    for i, s in enumerate(ranked):
        if p.w_rs > 0:
            tilt = p.w_rs * (2.0 * i / (n - 1) - 1.0)
            s.parts["rel_strength"] = tilt
            s.score = clip(s.score + tilt)
            tw = clip(s.feats["er"] / p.er_trend_threshold, 0.0, 1.0)
            s.reasons = _reasons(s.parts, s.feats, tw) + [f"RS rank {n - i}/{n}"]
        if p.weak_bottom_k > 0:
            downtrend = s.parts.get("regime", 0.0) < 0
            was = bool(weak_state.get(s.ticker)) if weak_state is not None else False
            if was:
                s.weak = downtrend and i < n // 2
            else:
                s.weak = (downtrend and i < p.weak_bottom_k and s.feats["ema_fast"] < s.feats["ema_slow"]
                          and breadth <= p.weak_max_breadth)
            if weak_state is not None:
                weak_state[s.ticker] = s.weak
            if s.weak:
                s.reasons.append(f"weak: RS rank {n - i}/{n} in a downtrend")
