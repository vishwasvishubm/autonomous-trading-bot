"""Universe screen: a stock is only *entered* if its data is good enough to trust.

Applied to every ticker from GET /stocks (including ones that appear mid-run).
A ticker that fails can still be exited if we already hold it; it just never
gets new money. Thresholds are deliberately loose for the platform's 10 stocks
(all pass) and exist to catch a broken feed or a thin, newly listed stock.
"""
from __future__ import annotations

from dataclasses import dataclass, field
from datetime import date
from statistics import median

MIN_BARS = 120                    # indicators need ~115 bars (100-day EMA + slope)
MIN_MEDIAN_TURNOVER = 5_000_000   # Rs per day (close x volume), median of the last 60 bars
MAX_ZERO_VOLUME_FRAC = 0.05       # share of bars with no volume
MAX_ABS_DAY_MOVE = 0.40           # a 40% one-day move = bad data or a corporate action
MAX_GAP_DAYS = 10                 # calendar days between bars
MAX_ATR_PCT = 0.10                # too volatile to size sensibly
MAX_STALE_DAYS = 10               # last bar must be recent relative to the feed


@dataclass
class ScreenResult:
    ticker: str
    ok: bool
    reasons: list[str] = field(default_factory=list)
    stats: dict = field(default_factory=dict)


def screen(ticker: str, bars: list[dict], as_of: str | None = None, periodic: bool = False) -> ScreenResult:
    """`periodic=True` is the in-run re-check: it skips the history-length and gap
    rules, because a bot restart mid-simulation leaves a gap in *our* copy of the
    bars (GET /history may not include the simulated days already played), and that
    must not lock every stock out of new entries."""
    res = ScreenResult(ticker, True)
    fail = res.reasons.append
    if len(bars) < (30 if periodic else MIN_BARS):
        fail(f"history {len(bars)} bars < {MIN_BARS}")
        res.ok = False
        return res
    tail = bars[-60:]
    closes = [b["close"] for b in bars]
    if any(c <= 0 for c in closes):
        fail("non-positive price")
    bad_ohlc = sum(1 for b in tail if not (b["low"] <= min(b["open"], b["close"]) and
                                           b["high"] >= max(b["open"], b["close"])))
    if bad_ohlc > 3:
        fail(f"{bad_ohlc} bars with inconsistent OHLC")
    turnover = median(b["close"] * b.get("volume", 0) for b in tail)
    zero_vol = sum(1 for b in tail if not b.get("volume")) / len(tail)
    if turnover < MIN_MEDIAN_TURNOVER:
        fail(f"median turnover Rs {turnover:,.0f} < {MIN_MEDIAN_TURNOVER:,.0f}")
    if zero_vol > MAX_ZERO_VOLUME_FRAC:
        fail(f"{zero_vol:.0%} zero-volume days")
    moves = [abs(closes[i] / closes[i - 1] - 1) for i in range(max(1, len(closes) - 60), len(closes))
             if closes[i - 1] > 0]
    if moves and max(moves) > MAX_ABS_DAY_MOVE:
        fail(f"one-day move {max(moves):.0%} > {MAX_ABS_DAY_MOVE:.0%}")
    dates = [date.fromisoformat(b["date"][:10]) for b in tail]
    gap = max(((b - a).days for a, b in zip(dates, dates[1:])), default=0)
    if gap > MAX_GAP_DAYS and not periodic:
        fail(f"{gap}-day gap in data")
    trs = [max(b["high"] - b["low"], abs(b["high"] - p["close"]), abs(b["low"] - p["close"]))
           for p, b in zip(tail, tail[1:])]
    atr_pct = (sum(trs[-14:]) / len(trs[-14:])) / closes[-1] if trs else 0.0
    if atr_pct > MAX_ATR_PCT:
        fail(f"ATR {atr_pct:.1%} of price > {MAX_ATR_PCT:.0%}")
    if as_of and not periodic and (date.fromisoformat(as_of[:10]) - dates[-1]).days > MAX_STALE_DAYS:
        fail(f"last bar {dates[-1]} is stale vs {as_of[:10]}")
    res.ok = not res.reasons
    res.stats = {"bars": len(bars), "median_turnover": round(turnover, 0), "atr_pct": round(100 * atr_pct, 2),
                 "max_day_move_pct": round(100 * max(moves), 1) if moves else 0.0, "max_gap_days": gap}
    return res
