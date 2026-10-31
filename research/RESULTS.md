# Strategy research log (2026-10-29)

Reproduce any row: `python research/sweep.py research/vN.json`. Each cell shows after-tax % /
max drawdown % / win rate % / short P&L / utilization. The windows are train 2022-23, train 2024,
**OOS 2025-26** and the sandbox period. Decisions used the TRAIN columns only.

Note: v1–v4 ran before two fixes (the core was trimmed when a winner rallied, and the shorts'
settlement trace), so rerunning them now gives slightly different numbers. v5–v7 match the
current code.

| Sweep | Question | Finding |
|---|---|---|
| v1 | Which tactical tweaks help? Relative strength, trend hold, RSI cap, stricter / tighter shorts, more exposure | RS helped train (+2–3 pts) but hurt OOS (−3 pts). Trend hold never triggered. The RSI cap, stricter shorts, tighter short stops and more exposure were all worse OOS. The risk guard was about neutral. |
| v2 | Regime-filtered core (exit core in downtrends) | Small gains only (train worst 7.5 → 11.8) |
| v3 | Pure regime-filtered buy & hold | Still far below buy & hold in every window: stepping out of downtrends costs money in this data |
| v4 | Unconditional core (held through downtrends) | Large gain: train worst 7.5 → 32. Win rate 56–90% |
| v5 | Core sizing at cost, not at today's price (stop trimming winners), plus a defensive core cut | Fix +7 pts in 2022-23. A core cut on drawdown cost 9 pts OOS (sells at lows) |
| v6 | Risk-guard thresholds, entry threshold, 75% vs 80% core | Guard costs nothing (slightly positive). 75/80% within noise |
| v7 | Fair comparison on both tax bases | Core 80%: OOS +12.8% vs buy & hold +15.3% (old design +7.3%) |

The chosen default is an 80% core, regime filter off, risk guard on, RS off. The strict
selection rule ranks a 90% core about 1.3 points higher on train. That gap is within noise, so
the lower-exposure option was kept. See the walk-forward table in `reports/REPORT.md`.

## After the live SimTest run (2026-10-29 evening)

The live run made +44.1% against +44.9% for equal-weight buy & hold, with 0 shorts and Margin
Locked always 0. The 500 simulated days (2024-10-25 → 2026-10-23) were recorded from Kafka. On the
days they share they match the sandbox history exactly, so they were merged into `data/history`,
and the backtester replays the run as window `sim_2024_26` (old default: +41.6%, close to live).

**Why no shorts:** a SHORT needs a stock with no position, and the 80% core held all 10 stocks.
That held even in the sell-offs (breadth ≤ 30% for months in 2025-26), so the bear hedge
could never fire.

| Sweep | Question | Finding |
|---|---|---|
| `rank_proto.py` / `grid.py` | Fast numpy prototype: momentum rotation (top-k by 60–200 day risk-adjusted return), short bottom-k, contrarian 5–40 day reversal | Momentum lagged B&H in both train windows; its shorts lost. Reversal results flipped sign with small parameter changes: noise, not edge |
| v8 | Relax the short breadth rule, more short room | 0 shorts in every variant: the core blocks them (diagnosis above) |
| v9 | "Weak" stocks (bottom-k RS + downtrend) leave the core and get shorted, any market | −10 to −25 pts OOS / sim. Shorts lost ₹4k–75k |
| v10 | Same, only in sell-offs (breadth ≤ 20–30%) | Still −10 to −15 pts OOS: the cost is selling the core before the rebound (no-short control loses the same) |
| v11 | Pure mean-reversion tactical layer | Mostly in cash (14% invested), no edge on the money deployed |
| v12–v13 | Core weight 80–95%, long cap 95–98%, position caps, entry thresholds | **85% core + 97% long cap beat 80% in all 4 windows** (+2.6 to +3.8). Neighbours (82–87%, entry 0.25–0.35) score similarly, so it's not a lucky spike. 93–95% was erratic in 2024 |
| v14 | Cheapest working hedge on the 85% core | 1 weak stock, breadth ≤ 30%, rip shorts: −2 to −5 pts vs core and no lower drawdown. Kept as profile `hedged`, not the default |

Default changed to core 85% / long cap 97%. Shorts stay available through `STRATEGY_PROFILE=hedged`.

## Concentration cap, live replay, and tuning on all data (2026-10-30)

**Live replay.** The backtester now replays the recorded headlines (`load_news`). It warms up on
400 calendar days like `GET /history`, and it observes day 1 the way live did (it arrived in
pre-flight). Live, a history re-download at the switch to simulation was skipped by our own
request pacer, and 5 stocks had 0 bars for ~5 days. That's fixed: the bot now waits for the
budget, and keeps loaded bars if a download fails. Replaying that glitch gives 14,38,801 vs
the live 14,40,868 (`verify.py` checks it stays within 1%). The same run with a one-day
difference ends anywhere from 12.2 L to 14.4 L: a single path is too noisy to tune on.

**Episodes.** `backtest.py --episodes` / `research/episodes.py`: every 500-day run in the data,
32 of them (starts every 10 days, 2023-07-27 → 2024-10-18).

| Variant (32 episodes) | Mean after-tax % | Worst | Mean vs B&H | Mean max DD | Mean taxes |
|---|---|---|---|---|---|
| old live default (80% + tactical, no cap) | 93.0 | 30.1 | −16.4 | 24.0 | 4,704 |
| 85% + tactical, no cap | 96.6 | 31.0 | −12.8 | 24.6 | 3,556 |
| 85% + tactical, cap 25% | 90.1 | 31.1 | −19.3 | 23.0 | 9,413 |
| 85% + tactical, cap 30% | 93.9 | 34.3 | −15.5 | 23.9 | 6,973 |
| 85% + tactical + hedge shorts | 88.3 | 30.8 | −21.1 | 22.7 | 16,244 |
| 97% + tactical, no cap | 97.8 | 32.2 | −11.6 | 25.1 | 467 |
| 97% core only, no cap | **104.6** | 38.6 | −4.9 | 25.5 | 0 |
| 97% core only, cap 35% | 102.5 | 38.6 | −6.9 | 25.2 | 3,258 |
| **97% core only, cap 30% (default)** | 99.6 | 38.2 | −9.8 | 24.6 | 4,112 |
| 97% core only, cap 25% | 95.4 | 38.2 | −14.0 | 23.4 | 10,684 |

Findings: the tactical layer lowered mean return in every pairing. The cap trades return for a
smaller single-stock exposure, and 30% is the middle point. A core top-up band of 1–3% only
mattered over 4-year windows (v16). Default changed to 97% core, tactical off, cap 30%; the old
design is profile `active`.

## Scorecard fixes (2026-10-30)

| Change | Why |
|---|---|
| Cash reserve 2–3% → 0.5% (core 99%, caps 99.5%) | A whole run's fees were ₹1,630; the reserve only cost wealth and utilization |
| 6 → 10 orders a day | The whole core is bought on day 1 (utilization is a time-average) |
| Observed pre-flight day 1 is traded after the switch, history and news kept | Live SimTest bought nothing until day 2 and re-downloaded history at the switch |
| `/stage` polls bypass the local request budget | A skipped poll at startup; > 10 s stale would pause trading |
| Cap trims count as long-term only if the latest buy is > 365 days old | Platform uses average cost (verified, ₹11 rounding); its partial-sale dating is unverified |

32 episodes (e4): 97% core / 2% cash: mean +99.9%, worst +41.6%, util 97.2%. After: mean
**+101.5%**, worst **+42.6%**, util **98.7%**. Both rows ran with 10 orders/day (`.env` overrides the
environment), so the day-1 effect is in both; the earlier 6-orders run (e3) had worst +38.2%.

## Two last candidates (2026-10-30)

**Inverse-volatility core** (`core_weighting="inverse_vol"`, e5, 32 episodes): calm stocks get more money.

| Weighting | Mean after-tax % | Worst | Mean max DD |
|---|---|---|---|
| equal (default) | **101.5** | **42.6** | 24.9 |
| inverse vol, power 0.5 | 84.4 | 31.4 | 23.0 |
| inverse vol, power 1 | 65.4 | 22.3 | 20.6 |

Lower drawdowns, but much lower returns: in this data the volatile stocks (PLNX) were the winners. Rejected; the option stays, off.

**Does the news predict prices?** (`research/news_check.py`, 8,125 recorded headlines over the 500 SimTest days)
- Most headlines are unrelated world news tagged to a ticker (e.g. US politics under ORCX, an album review under SONA). 56% score exactly 0.
- Returns relative to the 10-stock average, 2,895 stock-days with news:

| Horizon | Pearson | t-stat | After positive / negative news |
|---|---|---|---|
| same day | 0.046 | 2.48 | +0.22% / −0.07% |
| next day (what the bot can trade) | 0.010 | 0.56 | −0.01% / −0.07% |
| next 5 days | 0.052 | 2.77 | +0.26% / −0.38% |

- The bot's decayed news score vs the next day's return: t = 1.29.
- Verdict: no edge on the day the bot can trade. The 5-day hint is weak, because overlapping windows and testing 3 horizons inflate the t-stat, and it comes from one dataset. Trading it would mean selling the core (fees, 15% STCG), so news stays a logged, explained input, not a trade trigger.

## Profit-taking (2026-10-30, e6-e10)

Asked: realize more profit instead of holding it unrealized, exit winners at the right spot.
Every variant keeps the 30% cap. 32 episodes (mean / median / worst after tax) and the SimTest replay.

| Variant | Mean | Median | Worst | Mean tax | SimTest replay | Realized on replay |
|---|---|---|---|---|---|---|
| base (99% core, cap 30%) | 101.5 | 72.6 | 42.6 | 4,658 | 14,36,954 | 48,157 |
| trailing stop 15-30% after +10-50% | 60-79 | 44-58 | 15-27 | 30-78 k | - | - |
| trend-break exit after +20% | 103.4 | 75.6 | 28.7 | 78,983 | 12,72,444 | 2,53,279 |
| RSI 75-80 exit | 61-75 | 49-57 | 26-27 | 1 L+ | - | - |
| rebalance to 11-15% | 75-86 | 58-63 | 28-30 | 14-16 k | - | - |
| scale-out 25% at +50/+100%, long-term only | 94.3 | 74.8 | 41.5 | 7,868 | 14,48,862 | 1,01,056 |
| trend + cap (20% and trend broken) | no trigger | | | | 14,36,954 | 48,157 |
| idle cash redeployed (2%) | 102.1 | 72.6 | 42.6 | 4,722 | 14,36,954 | 48,157 |
| **tax-free gain harvest (March + last 5 days, skip >20% holdings)** | **101.5** | 72.6 | 42.2 | 4,530 | **14,36,677** | **1,96,915** |

Adopted as default: tax-free gain harvest (`gain_harvest`, `gh_final_days=5`, `gh_max_weight=0.20`) and
`deploy_idle_cash=0.02`. The harvest re-buys at once, so wealth is unchanged and the realized share of
profit rises 4x at no tax. Harvesting in March without the 20% limit cost ~4 points on the replay: the
re-buy restarts the 365-day clock, so the cap could not trim the biggest winner at 10%.
Scale-out is built (`so_levels`, `pt_long_term_only`) but off: it gains on the SimTest data and in the
median but costs 7 points of mean return (it sells part of the 10x winners).

## Volume strategy (2026-10-30, e11-e12)

New features: `vol_flow` (up-day minus down-day volume share over 50 days, in [-1, 1]) and `ret1`.
Two uses: the core weighted by volume flow (`core_weighting="volume"`, share = max(0.2, 1 + tilt x flow)),
and a distribution exit (sell on a heavy-volume down day in a distribution phase, back on a new uptrend).

| Variant | Mean | Median | Worst | Beat B&H | Mean tax | SimTest replay |
|---|---|---|---|---|---|---|
| equal weights (with gain harvest) | 102.0 | 72.6 | 42.2 | 12.5% | 4,684 | 14,36,677 (+43.7%) |
| volume tilt 0.25 | 103.3 | 71.0 | 43.5 | 18.8% | 4,263 | 14,61,781 |
| volume tilt 0.5 | 104.2 | 70.7 | 44.1 | 25.0% | 5,087 | 14,77,178 |
| **volume tilt 1.0 (default)** | **108.3** | **79.3** | **42.8** | **56.2%** | 12,079 | **14,97,998 (+49.8%)** |
| tilt 0.5, 100-day flow | 103.9 | 72.2 | 44.0 | 21.9% | 4,128 | 14,68,179 |
| tilt 0.5, 20-day flow | 103.4 | 76.8 | 37.4 | 25.0% | 10,338 | 14,73,653 |
| distribution exit (4 variants) | - | - | - | - | - | 11.99-12.72 L (-18 to -24 points) |

The distribution exit is built (`vd_volume_ratio`) but off. Caveat: on the single 4-year window traded from
2022 the volume core returned +254% after tax vs +290% for equal weights; the live-length episodes are the
basis for the choice.

## Smart core entry (2026-10-30, e13-e14)

Asked: don't buy everything on day 1, buy a stock only when it looks set to gain.

| Entry rule | Mean | Median | Worst | SimTest replay |
|---|---|---|---|---|
| all on day 1 (volume core) | 108.3 | 79.3 | 42.8 | 14,97,998 |
| **volume flow > 0, else buy after 20 days (default)** | **108.6** | **80.7** | **42.8** | 14,94,245 |
| volume flow > 0, no time limit | 108.5 | 81.1 | 42.8 | 14,84,341 |
| uptrend (close > 20-EMA > 60-EMA), 20-day limit | 104.8 | 78.8 | 43.6 | 14,67,415 |
| buy in 10 / 30 / 60 daily steps | 104.9 (10d) | 76.9 | 36.9 | 13.96 / 12.51 / 11.91 L |
| dip (RSI < 50 in an uptrend) | - | - | - | 12.38 L |

Volume-gated entry ties buying on day 1 (+0.3 mean, -Rs 3.7 k on the replay) and was adopted as asked.
Waiting has a visible cost on big up days: in the live paper run the bot was 45% invested on 2025-04-09
when the market rose +10.4%.
