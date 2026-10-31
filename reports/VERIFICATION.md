# Verification run

`python verify.py` at 2026-10-30 12:13

| check | result | detail |
|---|---|---|
| unit + integration + end-to-end tests | PASS | Ran 96 tests in 78.960s (80s) |
| .env.example has no token | PASS |  |
| .env is git-ignored | PASS |  |
| submission excludes .env and logs | PASS | 303 files would ship |
| secret scan of shipped files | PASS |  |
| token configured for the bot (.env) | PASS | 243 chars, value not shown |
| parameter sanity (exposure <= 100%, rate <= 120/min, dd_soft <= dd_hard) | PASS |  |
| no STOP kill-switch file left behind | PASS |  |
| no FLATTEN kill-switch file left behind | PASS |  |
| price history present | PASS | 50 stocks |
| universe screen (liquidity, gaps, bad ticks, volatility) | PASS | all stocks pass |
| accounting + tax audit: default, full | PASS | 10 fills, 0 shorts, 0 harvests, tax 0 |
| metrics consistent with equity curve: default, full | PASS | ret -0.4%, after-tax -0.4%, dd 3.47%, sharpe -0.01, B&H 61.52% |
| accounting + tax audit: default, sim_2024_26 | PASS | 23 fills, 0 shorts, 0 harvests, tax 0 |
| metrics consistent with equity curve: default, sim_2024_26 | PASS | ret 47.94%, after-tax 47.94%, dd 25.0%, sharpe 0.87, B&H 44.85% |
| accounting + tax audit: hedged (shorts + margin), sim_2024_26 | PASS | 154 fills, 10 shorts, 24 harvests, tax 6,303 |
| metrics consistent with equity curve: hedged (shorts + margin), sim_2024_26 | PASS | ret 36.61%, after-tax 35.98%, dd 26.46%, sharpe 0.78, B&H 44.85% |
| accounting + tax audit: tactical + shorts, full | PASS | 21 fills, 0 shorts, 0 harvests, tax 0 |
| metrics consistent with equity curve: tactical + shorts, full | PASS | ret -3.72%, after-tax -3.72%, dd 5.55%, sharpe -0.29, B&H 61.52% |
| accounting + tax audit: tactical + shorts, oos_2025_26 | PASS | 285 fills, 45 shorts, 1 harvests, tax 18,603 |
| metrics consistent with equity curve: tactical + shorts, oos_2025_26 | PASS | ret 8.96%, after-tax 7.1%, dd 12.06%, sharpe 0.43, B&H 27.35% |
