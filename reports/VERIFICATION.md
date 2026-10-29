# Verification run

`python verify.py` at 2026-10-30 01:38

| check | result | detail |
|---|---|---|
| unit + integration + end-to-end tests | PASS | Ran 83 tests in 55.870s (57s) |
| .env.example has no token | PASS |  |
| .env is git-ignored | PASS |  |
| submission excludes .env and logs | PASS | 86 files would ship |
| secret scan of shipped files | PASS |  |
| token configured for the bot (.env) | PASS | 243 chars, value not shown |
| parameter sanity (exposure <= 100%, rate <= 120/min, dd_soft <= dd_hard) | PASS |  |
| no STOP kill-switch file left behind | PASS |  |
| no FLATTEN kill-switch file left behind | PASS |  |
| price history present | PASS | 10 stocks |
| universe screen (liquidity, gaps, bad ticks, volatility) | PASS | all stocks pass |
| accounting + tax audit: default, full | PASS | 47 fills, 0 shorts, 12 harvests, tax 51,031 |
| metrics consistent with equity curve: default, full | PASS | ret 279.4%, after-tax 274.3%, dd 30.49%, sharpe 1.41, B&H 328.93% |
| accounting + tax audit: default, sim_2024_26 | PASS | 11 fills, 0 shorts, 0 harvests, tax 0 |
| metrics consistent with equity curve: default, sim_2024_26 | PASS | ret 44.25%, after-tax 44.25%, dd 24.29%, sharpe 0.84, B&H 44.85% |
| accounting + tax audit: hedged (shorts + margin), sim_2024_26 | PASS | 133 fills, 10 shorts, 23 harvests, tax 1,075 |
| metrics consistent with equity curve: hedged (shorts + margin), sim_2024_26 | PASS | ret 39.77%, after-tax 39.67%, dd 25.5%, sharpe 0.82, B&H 44.85% |
| accounting + tax audit: tactical + shorts, full | PASS | 492 fills, 51 shorts, 4 harvests, tax 146,826 |
| metrics consistent with equity curve: tactical + shorts, full | PASS | ret 88.3%, after-tax 73.62%, dd 13.49%, sharpe 1.22, B&H 328.93% |
| accounting + tax audit: tactical + shorts, oos_2025_26 | PASS | 262 fills, 45 shorts, 2 harvests, tax 17,994 |
| metrics consistent with equity curve: tactical + shorts, oos_2025_26 | PASS | ret 8.69%, after-tax 6.89%, dd 12.19%, sharpe 0.43, B&H 27.35% |
