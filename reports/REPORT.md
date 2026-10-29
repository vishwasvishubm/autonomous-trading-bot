# MANTHAN bot: reproducible results

Generated 2026-10-30 01:36 with `python report.py` (Python 3.14.6). Data fingerprint `815ae7a33f53f5cb`: 10 stocks, 10873 daily bars, 2022-06-22 to 2026-10-23.

Parameters were chosen on the two TRAIN windows only; `oos_2025_26` was held out.
`after_tax_pct` = platform basis (tax on realized gains); `liquidated_*` = everything sold on the last day.
Metric definitions: `trader/metrics.py`.

## Default strategy (core 99%, tactical off, cap 30%)

| window | after_tax_pct | buy_hold_pct | excess_vs_buy_hold_pct | liquidated_after_tax_pct | buy_hold_liquidated_pct | max_drawdown_pct | sharpe | sortino | win_rate_pct | avg_utilization_pct | orders | fees | est_tax | rejects |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train_2022_23 | 45.93 | 38.69 | 7.25 | 42.3 | 35.78 | 13.97 | 1.66 | 2.56 | 0.0 | 92.0 | 10 | 200.0 | 0 | 0 |
| train_2024 | 67.29 | 67.96 | -0.67 | 57.16 | 57.73 | 8.06 | 2.8 | 4.76 | 0.0 | 99.1 | 10 | 200.0 | 0 | 0 |
| oos_2025_26 | 26.74 | 27.35 | -0.61 | 25.03 | 25.58 | 21.93 | 0.67 | 1.0 | 0.0 | 98.8 | 10 | 200.0 | 0 | 0 |
| sandbox_period | 77.1 | 88.48 | -11.38 | 71.5 | 80.6 | 25.44 | 1.09 | 1.67 | 100.0 | 99.3 | 44 | 1141.0 | 2686.0 | 0 |
| sim_2024_26 | 44.25 | 44.85 | -0.6 | 40.79 | 41.33 | 24.29 | 0.84 | 1.27 | 100.0 | 99.0 | 11 | 235.0 | 0.0 | 0 |
| full | 274.3 | 328.93 | -54.64 | 254.41 | 297.0 | 30.49 | 1.41 | 2.21 | 100.0 | 96.9 | 47 | 1231.0 | 51031.0 | 0 |

`sim_2024_26` = the exact 500 days of the live SimTest run (live: +44.1% with the previous 80% core + tactical). Fixed windows trade from day 1; the live-replay table below observes day 1 like live did.

## Previous default (core 80%, long cap 95%), for comparison

| window | after_tax_pct | buy_hold_pct | excess_vs_buy_hold_pct | liquidated_after_tax_pct | buy_hold_liquidated_pct | max_drawdown_pct | sharpe | sortino | win_rate_pct | avg_utilization_pct | orders | fees | est_tax | rejects |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train_2022_23 | 42.28 | 38.69 | 3.6 | 39.2 | 35.78 | 13.32 | 1.72 | 2.64 | 44.4 | 86.2 | 26 | 658.0 | 3000.0 | 0 |
| train_2024 | 48.62 | 67.96 | -19.34 | 42.34 | 57.73 | 7.92 | 2.63 | 4.4 | 88.9 | 94.1 | 29 | 733.0 | 12419.0 | 0 |
| oos_2025_26 | 22.79 | 27.35 | -4.56 | 21.54 | 25.58 | 19.09 | 0.66 | 0.98 | 86.7 | 89.9 | 74 | 1956.0 | 3023.0 | 0 |
| sandbox_period | 69.86 | 88.48 | -18.62 | 64.97 | 80.6 | 25.32 | 1.09 | 1.65 | 92.3 | 91.8 | 57 | 1508.0 | 11655.0 | 0 |
| sim_2024_26 | 41.9 | 44.85 | -2.95 | 38.67 | 41.33 | 23.64 | 0.84 | 1.28 | 90.9 | 92.4 | 56 | 1488.0 | 224.0 | 0 |
| full | 310.3 | 328.93 | -18.64 | 280.39 | 297.0 | 34.37 | 1.39 | 2.17 | 50.0 | 94.0 | 30 | 769.0 | 3000.0 | 0 |

## Profile `hedged`: shorts the weakest downtrending stock in broad sell-offs (margin locked)

| window | after_tax_pct | buy_hold_pct | excess_vs_buy_hold_pct | liquidated_after_tax_pct | buy_hold_liquidated_pct | max_drawdown_pct | sharpe | sortino | win_rate_pct | avg_utilization_pct | orders | fees | est_tax | rejects | short_trades | short_pnl |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train_2022_23 | 44.08 | 38.69 | 5.39 | 40.63 | 35.78 | 12.6 | 1.71 | 2.62 | 33.3 | 88.3 | 26 | 658.0 | 0.0 | 0 | 0 | 0.0 |
| train_2024 | 52.96 | 67.96 | -15.0 | 45.63 | 57.73 | 7.88 | 2.66 | 4.47 | 75.0 | 96.9 | 25 | 638.0 | 7792.0 | 0 | 0 | 0.0 |
| oos_2025_26 | 19.68 | 27.35 | -7.67 | 18.59 | 25.58 | 22.66 | 0.58 | 0.87 | 52.3 | 90.4 | 116 | 3010.0 | 1437.0 | 0 | 13 | 2662.0 |
| sandbox_period | 65.49 | 88.48 | -22.99 | 61.29 | 80.6 | 25.63 | 1.05 | 1.59 | 63.4 | 93.0 | 118 | 3066.0 | 9770.0 | 0 | 10 | -6238.0 |
| sim_2024_26 | 39.67 | 44.85 | -5.18 | 36.94 | 41.33 | 25.5 | 0.82 | 1.24 | 63.4 | 91.8 | 130 | 3428.0 | 1075.0 | 0 | 10 | -5326.0 |
| full | 274.2 | 328.93 | -54.73 | 261.08 | 297.0 | 26.48 | 1.51 | 2.38 | 58.1 | 93.2 | 119 | 3132.0 | 133565.0 | 0 | 7 | -19174.0 |

## Every live-length run in the data (500 trading days, 400-day warm-up, day 1 observed)

The live game is one 500-day path, and one path is noisy: replaying SimTest with a one-day
difference in when 5 stocks became tradable moved final wealth between 12.2 L and 14.4 L.
So parameters are judged over every 500-day episode the data allows, not one window.

| variant | episodes | mean after-tax % | median | worst | mean vs B&H | worst vs B&H | beat B&H % of runs | mean max DD % | mean util % | mean orders | mean taxes |
|---|---|---|---|---|---|---|---|---|---|---|---|
| current default | 32 | 101.53 | 72.63 | 42.6 | -7.87 | -24.43 | 12.5 | 24.86 | 98.7 | 22.8 | 4,658 |
| previous (80% core, no cap) | 32 | 92.48 | 70.88 | 30.06 | -16.92 | -52.38 | 9.4 | 23.96 | 93.6 | 48.4 | 4,846 |

## Live SimTest replay, in the leaderboard's columns

Replays the 500 recorded days with the recorded headlines, day 1 observed (it arrived in pre-flight).
Live, a history download failure left 5 stocks untradable for ~5 days (fixed); replaying that
failure gives 14,38,801 vs the live 14,40,868, so the replay reproduces the live run.

| run | total wealth | realized | unrealized | charges | taxes | cash util % | cash | margin | orders | errors | POS |
|---|---|---|---|---|---|---|---|---|---|---|---|
| replay, parameters that ran live | 1,333,500 | 36,231 | 298,385 | 1,116 | 0 | 90.3 | 129,122 | 0 | 42 | 0 | 10 |
| replay, current parameters | 1,436,954 | 48,157 | 389,032 | 235 | 0 | 94.8 | 74,157 | 0 | 11 | 0 | 10 |

## Tactical layer only (previous design, for comparison)

| window | after_tax_pct | buy_hold_pct | excess_vs_buy_hold_pct | liquidated_after_tax_pct | buy_hold_liquidated_pct | max_drawdown_pct | sharpe | sortino | win_rate_pct | avg_utilization_pct | orders | fees | est_tax | rejects |
|---|---|---|---|---|---|---|---|---|---|---|---|---|---|---|
| train_2022_23 | 12.98 | 38.69 | -25.7 | 11.96 | 35.78 | 11.05 | 1.06 | 1.6 | 26.7 | 50.6 | 113 | 2874.0 | 11481.0 | 0 |
| train_2024 | 35.33 | 67.96 | -32.63 | 32.75 | 57.73 | 9.0 | 2.4 | 3.98 | 42.9 | 66.0 | 100 | 2537.0 | 32560.0 | 0 |
| oos_2025_26 | 4.51 | 27.35 | -22.84 | 4.5 | 25.58 | 9.83 | 0.33 | 0.47 | 36.7 | 46.5 | 244 | 5892.0 | 9159.0 | 0 |
| sandbox_period | 27.94 | 88.48 | -60.55 | 27.93 | 80.6 | 10.09 | 1.09 | 1.68 | 37.6 | 50.7 | 291 | 7063.0 | 52994.0 | 0 |
| sim_2024_26 | 12.02 | 44.85 | -32.83 | 12.01 | 41.33 | 10.69 | 0.6 | 0.92 | 37.3 | 48.6 | 271 | 6555.0 | 24015.0 | 0 |
| full | 53.18 | 328.93 | -275.76 | 53.17 | 297.0 | 11.05 | 1.08 | 1.66 | 37.2 | 52.5 | 468 | 11646.0 | 101215.0 | 0 |

## Accounting audit (ledger vs independent replay of its own fills)

| run | fills | result |
|---|---|---|
| train_2022_23 | 10 | OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match |
| train_2024 | 10 | OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match |
| oos_2025_26 | 10 | OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match |
| sandbox_period | 44 | OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match |
| sim_2024_26 | 11 | OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match |
| full | 47 | OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match |
| oos_2025_26 (tactical) | 262 | OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match |
| full (tactical) | 492 | OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match |
| sim_2024_26 (hedged) | 133 | OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match |
| full (hedged) | 121 | OK: cash, margin, positions, avg prices, fees, FY gains, tax, wealth match |

## Walk-forward: ranked by worst TRAIN window, OOS shown after the fact

| params | train worst score | OOS after-tax % | OOS max DD % | OOS Sharpe | OOS B&H % |
|---|---|---|---|---|---|
| {'core_weight': 0.85, 'tactical': True, 'max_stock_weight': 0.3} | 38.8 | 25.97 | 20.01 | 0.7 | 27.35 |
| {'core_weight': 0.85, 'tactical': True, 'max_stock_weight': 1.0} | 38.8 | 25.97 | 20.01 | 0.7 | 27.35 |
| {'core_weight': 0.9, 'tactical': True, 'max_stock_weight': 0.3} | 38.66 | 22.11 | 23.95 | 0.59 | 27.35 |
| {'core_weight': 0.9, 'tactical': True, 'max_stock_weight': 1.0} | 38.66 | 22.11 | 23.95 | 0.59 | 27.35 |
| {'core_weight': 0.97, 'tactical': False, 'max_stock_weight': 0.3} | 38.21 | 27.06 | 22.52 | 0.67 | 27.35 |
| {'core_weight': 0.97, 'tactical': False, 'max_stock_weight': 1.0} | 38.21 | 27.06 | 22.52 | 0.67 | 27.35 |
| {'core_weight': 0.9, 'tactical': False, 'max_stock_weight': 0.3} | 36.2 | 22.49 | 21.5 | 0.62 | 27.35 |
| {'core_weight': 0.9, 'tactical': False, 'max_stock_weight': 1.0} | 36.2 | 22.49 | 21.5 | 0.62 | 27.35 |
| {'core_weight': 0.85, 'tactical': False, 'max_stock_weight': 0.3} | 30.26 | 20.74 | 20.34 | 0.61 | 27.35 |
| {'core_weight': 0.85, 'tactical': False, 'max_stock_weight': 1.0} | 30.26 | 20.74 | 20.34 | 0.61 | 27.35 |
| {'core_weight': 0.97, 'tactical': True, 'max_stock_weight': 0.3} | 29.52 | 28.06 | 22.47 | 0.7 | 27.35 |
| {'core_weight': 0.97, 'tactical': True, 'max_stock_weight': 1.0} | 29.52 | 28.06 | 22.47 | 0.7 | 27.35 |

## Live evidence from `D:\trading_bot\manthan-bot\logs`

- live trading days decided: 848
- orders sent: 70, status: {'FILLED': 70, 'SKIPPED': 3}
- order success rate: 100.0%, rejected 0, unknown outcome 0
- order round trip ms: {'p50': 28.7, 'p95': 218.2, 'max': 1098.0}
- server-reported decision delay s: {'p50': 0.5, 'p95': 1.0, 'max': 1.646}
- first tick -> orders done ms: {'p50': 375.2, 'p95': 473.9, 'max': 1491.7}; decide only ms: {'p50': 374.5, 'p95': 466.7, 'max': 653.7}
- reconciles: 285, with drift: 0
- risk guard modes: {'normal': 549, 'cooldown': 24, 'reduced': 222, 'defensive': 53}

## Parameters

```json
{
 "ema_fast": 20,
 "ema_slow": 60,
 "er_window": 20,
 "rsi_window": 14,
 "bb_window": 20,
 "atr_window": 14,
 "vol_window": 20,
 "mom_window": 20,
 "ema_trend": 100,
 "rs_window": 100,
 "w_regime": 0.5,
 "w_trend": 0.45,
 "w_meanrev": 0.25,
 "w_momentum": 0.15,
 "w_news": 0.35,
 "news_half_life_days": 2.0,
 "er_trend_threshold": 0.3,
 "regime_band_atr": 1.5,
 "reentry_cooldown_days": 10,
 "tactical": false,
 "long_entry": 0.3,
 "long_exit": 0.0,
 "short_entry": -0.45,
 "short_exit": -0.1,
 "enable_shorts": true,
 "short_max_breadth": 0.3,
 "short_style": "rip",
 "rip_min_rsi": 60.0,
 "rip_min_z": 0.5,
 "rip_take_profit_z": -1.0,
 "rip_require_both": false,
 "short_stop_atr": 2.5,
 "short_news_veto": 0.2,
 "w_rs": 0.0,
 "trend_hold": false,
 "max_entry_rsi": 100.0,
 "core_weight": 0.99,
 "core_min_score": -1.0,
 "core_regime_filter": false,
 "weak_bottom_k": 0,
 "weak_max_breadth": 1.0,
 "short_weak_only": false,
 "weak_short_min_z": -1.0,
 "max_position_pct": 0.17,
 "max_long_exposure": 0.995,
 "breadth_floor": 1.0,
 "max_gross_exposure": 0.995,
 "max_short_pct": 0.07,
 "max_stock_weight": 0.3,
 "cap_trim_to": 0.26,
 "cap_hard_weight": 0.35,
 "risk_per_trade": 0.0125,
 "stop_atr": 2.5,
 "trail_atr": 3.0,
 "max_short_hold_days": 5,
 "min_order_value": 25000.0,
 "rebalance_band": 0.04,
 "min_hold_days": 2,
 "wind_down_days": 3,
 "dd_window": 60,
 "dd_soft": 0.08,
 "dd_hard": 0.15,
 "dd_core_cut": 1.0,
 "daily_loss_limit": 0.03,
 "loss_cooldown_days": 2,
 "max_consecutive_rejects": 3,
 "ltcg_defer_window": 30,
 "harvest_min_loss": 2000.0,
 "harvest_from_day": 15
}
```
