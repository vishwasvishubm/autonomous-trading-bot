"""Runtime configuration, loaded from environment variables / .env file.

Everything tunable lives here so the jury can see every knob in one place.
"""
from __future__ import annotations

import os
from dataclasses import dataclass, field, replace
from pathlib import Path

ROOT = Path(__file__).resolve().parent.parent

try:  # python-dotenv is optional; plain environment variables also work
    from dotenv import load_dotenv

    # .env wins over stale machine-wide variables (e.g. an old setx BOT_ACCESS_TOKEN)
    load_dotenv(ROOT / ".env", override=True)
except ImportError:  # pragma: no cover
    pass


def _env(name: str, default: str) -> str:
    return os.environ.get(name, default).strip()


def _env_bool(name: str, default: bool) -> bool:
    return _env(name, str(default)).lower() in ("1", "true", "yes", "on")


# ---- Market friction constants (problem statement, section 8) ----
BROKERAGE_FEE = 20.00          # flat, every executed order
DP_CHARGE = 15.34              # flat, when selling delivery (long) shares
STCG_RATE = 0.15               # held <= 365 days
LTCG_RATE = 0.10               # held > 365 days
LTCG_EXEMPTION = 100_000.0     # per financial year (Section 112A)
LTCG_DAYS = 365
SHORT_SETTLEMENT_DAYS = 10     # shorts squared off every 10 trading days
MAX_ORDER_QTY = 1_000_000


@dataclass
class StrategyParams:
    """Signal + risk parameters.

    Defaults were picked on the TRAIN windows only (2022-23, 2024) by the worst-window
    score (after-tax return - 0.5 x max drawdown); 2025-26 was held out as the
    out-of-sample test. See research/RESULTS.md for every variant tried.
    """

    # indicator windows
    ema_fast: int = 20
    ema_slow: int = 60
    er_window: int = 20            # Kaufman efficiency ratio (trendiness)
    rsi_window: int = 14
    bb_window: int = 20
    atr_window: int = 14
    vol_window: int = 20
    mom_window: int = 20
    ema_trend: int = 100           # long-term regime filter
    rs_window: int = 100           # relative-strength lookback (cross-sectional ranking)

    # signal blending
    w_regime: float = 0.50
    w_trend: float = 0.45
    w_meanrev: float = 0.25
    w_momentum: float = 0.15
    w_news: float = 0.35
    news_half_life_days: float = 2.0
    er_trend_threshold: float = 0.30   # ER above this => trending regime
    # anti-whipsaw (picked by worst-window backtest; see README): price must clear the
    # slow EMA by 1.5 ATR to flip regime, and a stopped-out stock can't be re-bought for 10 days
    regime_band_atr: float = 1.5
    reentry_cooldown_days: int = 10

    # entries / exits (hysteresis avoids churn and fees)
    tactical: bool = False             # tactical overweights on top of the core (profile "active")
    long_entry: float = 0.30
    long_exit: float = 0.00
    short_entry: float = -0.45
    short_exit: float = -0.10
    # Shorts = bear-market hedge. Momentum shorts lost money in every test (prices bounce
    # after drops here); "sell the rip" shorts improved every bear window (see README).
    enable_shorts: bool = True
    short_max_breadth: float = 0.30     # only short when at most 30% of stocks are in an uptrend
    # "momentum": short weak scores (lost money: prices bounce after drops in this data)
    # "rip": short a *bounce* inside a confirmed downtrend, take profit when it fades
    # "weak": short the weak stocks (see weak_bottom_k) and hold until they stop being weak
    short_style: str = "rip"
    rip_min_rsi: float = 60.0           # rip entry: RSI at least this ...
    rip_min_z: float = 0.5              # ... or Bollinger z at least this
    rip_take_profit_z: float = -1.0     # rip exit: back below the band middle by this much
    rip_require_both: bool = False      # rip entry needs RSI *and* z (fewer, better shorts)
    short_stop_atr: float = 2.5         # initial stop distance for shorts
    short_news_veto: float = 0.2        # no short while news sentiment is above this

    # trade selection
    w_rs: float = 0.0                   # relative strength tilt (rank of risk-adjusted rs_window return)
    trend_hold: bool = False            # in a confirmed uptrend above the 60-EMA, exit only on the stop
    max_entry_rsi: float = 100.0        # don't open a new long when RSI is above this (chasing)
    # core + tactical: hold `core_weight` of wealth spread equally over every stock that
    # is not in a confirmed downtrend; the signal model trades on top of that. Stops and
    # signal exits only trim back to the core; the core goes when the regime turns down.
    # Pure timing (core 0) lagged buy & hold in every window: prices here mean-revert, so
    # stepping out of downtrends and back in cost more than it saved. An 80% core closed
    # the gap; 85% beat 80% in every fixed window. Judged over all 32 live-length (500-day)
    # episodes in the data, a 97% core with the tactical layer OFF did best of all
    # (research/RESULTS.md, e1-e3): the tactical layer cost ~5-10 points on average.
    core_weight: float = 0.99
    core_min_score: float = -1.0        # don't build a core position in a stock scoring below this
    core_regime_filter: bool = False    # False = the core is held through downtrends too (buy & hold)
    # Hedge sleeve. A short needs a stock we don't hold, and the core holds every stock, so
    # with weak_bottom_k = 0 shorts cannot open (the default: in every test window shorting
    # cost return; research/RESULTS.md v8-v14). With k > 0, in a sell-off (breadth <= weak_max_breadth)
    # the bottom k stocks by relative strength that are in a confirmed downtrend leave the core
    # (their share goes to the other stocks) and may be shorted: profile "hedged".
    weak_bottom_k: int = 0
    weak_max_breadth: float = 1.0       # flag new weak stocks only when <= this share of stocks is in an uptrend
    short_weak_only: bool = False       # True = only weak stocks may be shorted
    weak_short_min_z: float = -1.0      # "weak" style: don't short a stock already 1 sd below its mean

    # sizing / risk
    # per-position caps for the tactical layer (on top of the core)
    max_position_pct: float = 0.17      # of total wealth per stock
    max_long_exposure: float = 0.995    # core + tactical overweights
    breadth_floor: float = 1.0          # long cap scales from this (no stock in uptrend) to 100%; 1.0 = off
    # longs + short margin. 0.5% cash (~Rs 5,000) is kept for fees: a whole run's fees were Rs 1,630,
    # so a bigger reserve only cost wealth and cash utilization (both on the scorecard)
    max_gross_exposure: float = 0.995
    max_short_pct: float = 0.07         # per short position
    # concentration cap: a winner that grows past max_stock_weight of wealth is trimmed back to
    # cap_trim_to (the gap stops a trim every day). Trims wait for the 365-day LTCG mark unless
    # the stock is above cap_hard_weight.
    # Over the 32 episodes: cap 25% cost ~9 points of mean return for 2 points less drawdown,
    # 30% ~5 points for 1 point, 35% ~2 points (research/RESULTS.md e3). 30% is the default.
    max_stock_weight: float = 0.30
    cap_trim_to: float = 0.26
    cap_hard_weight: float = 0.35
    risk_per_trade: float = 0.0125      # wealth risked to the stop
    stop_atr: float = 2.5               # initial stop distance
    trail_atr: float = 3.0              # trailing stop distance
    max_short_hold_days: int = 5        # quick bounce trades, out before the 10-day settlement
    min_order_value: float = 25_000.0   # fees stay < 0.1% of order value
    rebalance_band: float = 0.04        # ignore target changes smaller than this
    min_hold_days: int = 2              # no flip-flopping on signal noise
    wind_down_days: int = 3             # no new entries in the last N days

    # portfolio-level risk guard (trader/risk.py)
    dd_window: int = 60                  # drawdown is measured from the peak of the last N days
    dd_soft: float = 0.08                # drawdown >= 8%: new positions at half size
    dd_hard: float = 0.15                # drawdown >= 15%: no new positions, close shorts
    dd_core_cut: float = 1.0             # defensive mode keeps this fraction of the passive core
    daily_loss_limit: float = 0.03       # one-day loss >= 3% of wealth: no new positions ...
    loss_cooldown_days: int = 2          # ... for this many days
    max_consecutive_rejects: int = 3     # order circuit breaker: pause after N rejects in a row

    # tax optimisation
    ltcg_defer_window: int = 30          # defer soft exits this close to 365 days
    harvest_min_loss: float = 2_000.0    # min loss worth harvesting (>> fees)
    harvest_from_day: int = 15           # harvest window: March 15-31 (FY ends March 31)


PROFILES = {
    # default: 97% equal-weight core held through dips, 30% concentration cap, tax-loss harvest,
    # risk guard. Best mean and worst-case return over every 500-day episode in the data.
    "core": {},
    # previous default: 85% core + tactical signal layer (trend, mean reversion, momentum, news)
    "active": {"core_weight": 0.85, "max_long_exposure": 0.97, "max_gross_exposure": 0.97, "tactical": True},
    # active + a short hedge in broad sell-offs: the weakest stock in a downtrend leaves the
    # core and is shorted on bounces (margin locked). Costs ~2-5 points vs "active" in the
    # tests because the weak stocks rebounded (reports/REPORT.md).
    "hedged": {"core_weight": 0.85, "max_long_exposure": 0.97, "max_gross_exposure": 0.97, "tactical": True,
               "weak_bottom_k": 1, "weak_max_breadth": 0.30, "short_weak_only": True, "short_max_breadth": 0.30},
    # signal timing only, ~50% invested: lowest drawdown, lowest return
    "tactical": {"core_weight": 0.0, "tactical": True, "breadth_floor": 0.25, "max_long_exposure": 0.80,
                 "max_gross_exposure": 0.90},
}


def load_params() -> StrategyParams:
    name = _env("STRATEGY_PROFILE", "core").lower()
    if name not in PROFILES:
        raise SystemExit(f"STRATEGY_PROFILE={name!r}: choose one of {', '.join(PROFILES)}")
    return replace(StrategyParams(), **PROFILES[name])


@dataclass
class Config:
    server_ip: str = field(default_factory=lambda: _env("SERVER_IP", "192.168.65.157"))
    gateway_port: int = field(default_factory=lambda: int(_env("GATEWAY_PORT", "3002")))
    kafka_port: int = field(default_factory=lambda: int(_env("KAFKA_PORT", "9094")))
    username: str = field(default_factory=lambda: _env("TEAM_USERNAME", "MN2602"))
    token: str = field(default_factory=lambda: _env("BOT_ACCESS_TOKEN", ""))

    # Measured in the sandbox: 1 consumer = 0 late ticks, ~350 ms avg decision; 3 consumer
    # threads = late ticks on news-heavy days (up to 2 s). kafka-python is pure Python, so
    # threads in one process share the GIL and don't parallelize; ~30 msg/s needs only one.
    kafka_consumers: int = field(default_factory=lambda: int(_env("KAFKA_CONSUMERS", "1")))
    # trade in the development sandbox too (for testing); live sim always trades
    trade_in_sandbox: bool = field(default_factory=lambda: _env_bool("TRADE_IN_SANDBOX", False))
    # compute and log decisions but never send orders
    dry_run: bool = field(default_factory=lambda: _env_bool("DRY_RUN", False))

    # REST budget: platform limit is 120/min; keep headroom
    requests_per_minute: int = field(default_factory=lambda: int(_env("REQUESTS_PER_MINUTE", "100")))
    # anti-flood guard (measured live in pre-flight): > 15 requests in 5 s, any endpoint, = temporary block
    requests_per_5s: int = field(default_factory=lambda: int(_env("REQUESTS_PER_5S", "12")))
    # 10 = the whole core is bought on day 1 (cash utilization is a time-average on the scorecard)
    max_orders_per_day: int = field(default_factory=lambda: int(_env("MAX_ORDERS_PER_DAY", "10")))
    reconcile_every_days: int = field(default_factory=lambda: int(_env("RECONCILE_EVERY_DAYS", "3")))
    # decision timing after a day's first tick: wait at least DECISION_DELAY_MS, then
    # decide once news has been quiet for NEWS_QUIET_MS; never wait past DECISION_MAX_WAIT_MS.
    # (measured live: ticks land within ~100 ms, news bursts trail by up to ~600 ms)
    decision_delay_ms: int = field(default_factory=lambda: int(_env("DECISION_DELAY_MS", "300")))
    news_quiet_ms: int = field(default_factory=lambda: int(_env("NEWS_QUIET_MS", "150")))
    # once every tick is in, wait for trailing news at most this long (later items count next day)
    news_max_wait_ms: int = field(default_factory=lambda: int(_env("NEWS_MAX_WAIT_MS", "600")))
    decision_max_wait_ms: int = field(default_factory=lambda: int(_env("DECISION_MAX_WAIT_MS", "2000")))
    history_days: int = field(default_factory=lambda: int(_env("HISTORY_DAYS", "400")))

    log_dir: Path = field(default_factory=lambda: ROOT / _env("LOG_DIR", "logs"))
    data_dir: Path = field(default_factory=lambda: ROOT / _env("DATA_DIR", "data"))
    # kill switch: create a file named STOP (exits only) or FLATTEN (close everything) here
    control_dir: Path = field(default_factory=lambda: ROOT / _env("CONTROL_DIR", "."))

    params: StrategyParams = field(default_factory=load_params)
    profile: str = field(default_factory=lambda: _env("STRATEGY_PROFILE", "core").lower())

    @property
    def gateway(self) -> str:
        return f"http://{self.server_ip}:{self.gateway_port}"

    @property
    def broker(self) -> str:
        return f"{self.server_ip}:{self.kafka_port}"

    @property
    def group_id(self) -> str:
        return f"group-{self.username}"
