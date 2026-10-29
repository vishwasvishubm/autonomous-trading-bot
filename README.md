# MANTHAN 2026: Autonomous Trading Bot (team MN2602)

An explainable trading bot for the JNNCE MANTHAN 2026 Autonomous Trading Systems Challenge. It
reads the Kafka market stream, decides once per simulated day (~350 ms after the first tick,
measured live), and trades through the Order Gateway. It pre-validates every order, keeps to a
hard REST budget, models Indian capital gains tax, and has portfolio-level risk limits plus a
kill switch.

## Quick start

```bash
# Anaconda Python (has requests, pandas, streamlit); only kafka-python is extra
python -m pip install kafka-python
cp .env.example .env            # paste BOT_ACCESS_TOKEN (Participant Guide, Step 1); never share .env

python verify.py                # pre-flight checklist (offline), must say 17/17
python verify.py --live         # + read-only checks against the gateway (GET only, no orders)
python verify.py --live --expect-ready   # while the bot runs in pre-flight: platform must see us + READY
python run_bot.py --dry-run     # paper trading on the live stream, no orders sent
python run_bot.py --sandbox     # trade in the development sandbox
python run_bot.py               # official run: trades only when stage = simulation / RUNNING
streamlit run dashboard.py      # live dashboard (reads local logs only)
python report.py                # reproducible backtest + audit + live-evidence report -> reports/REPORT.md
python package.py               # submission ZIP without .env / logs, secret-scanned -> dist/
```

### Replaying the live game, and growing the data

| Command | What it does |
|---|---|
| `python backtest.py --replay-live` | Replays the 500 recorded SimTest days with the recorded headlines, day 1 observed like live. Prints the leaderboard's columns (wealth, realized, unrealized, charges, taxes, util, cash, margin, orders, errors, POS) next to the live bot's own numbers. |
| `python backtest.py --episodes` | Scores the current parameters over **every** 500-day run in the data, each with the same 400-day warm-up the live bot gets from `GET /history`. |
| `python update_history.py` | Merges the ticks recorded live (`data/stream/ticks.jsonl`) into `data/history`. It also saves each run to `data/runs/<first>_<last>/` (replay it with `--data`). A run whose prices disagree with the history is a different dataset and is kept apart. **This runs automatically when the bot exits**, so every run's data feeds the next backtest. |

Backtests replay recorded news through the same `NewsBook` the bot uses (`--no-news` turns it off).
`verify.py` checks that the replay reproduces the live SimTest wealth within 1%.

Other tools: `python backtest.py` (`--start/--end/--set k=v/--windows/--walkforward/--verbose`),
`python research/sweep.py research/v7.json` (variant sweeps), `python download_history.py`,
`python record_stream.py`, `python analyze_news.py`, `python -m unittest discover -s tests -v`.

## Architecture

```
 Kafka :9094  market.ticks / market.news (10 partitions)
      |  1 consumer in group-MN2602, auto-reconnect with backoff
      v
  queue --> Engine (single thread, single portfolio state)
              1. batch the day's ticks; decide once all are in and news is quiet for 150 ms
              2. indicators -> score per stock -> cross-sectional rank      (strategy.py)
              3. risk guard: drawdown / daily-loss / kill switch           (risk.py)
              4. planner -> <= 6 prioritized, pre-validated, explained orders (planner.py)
              5. send orders concurrently; book fills, partials; resync on unknown outcomes
              6. journal every decision, order explanation and sync (logs/*.jsonl)
   StageWatcher ---- GET /stage every 2.5 s -> trading on/off, days remaining
   HeartbeatWorker - ACK every preflight seq in order (POST /bot/heartbeat); reads GET /bot/status every 30 s
   Universe screen - every listed stock (incl. new ones) must pass data checks before money goes in
```

| Module | Responsibility |
|---|---|
| `trader/config.py` | every parameter, fee and tax constant; `STRATEGY_PROFILE` switch |
| `trader/api.py` | REST client: token bucket, 429 penalty box, timeout classification, partial fills |
| `trader/stream.py` | Kafka consumers with auto-reconnect, `latest` offsets, no auto-commit |
| `trader/lifecycle.py` | stage polling (tolerates slow polls for 10 s); pre-flight heartbeat: every seq ACKed in order, repeats within 30 s dropped, failed ACKs retried, platform readiness (`GET /bot/status`) logged |
| `trader/engine.py` | day batching, decisions, execution, duplicate guard, circuit breaker, discovery |
| `trader/strategy.py` | explainable score in [-1, 1] + relative-strength ranking |
| `trader/planner.py` | core + tactical sizing, stops, exposure caps, tax logic, order explanations |
| `trader/risk.py` | portfolio drawdown limits, daily loss limit, STOP / FLATTEN kill switch |
| `trader/universe.py` | liquidity, history, gaps, bad ticks, volatility, staleness checks |
| `trader/portfolio.py` | shadow ledger: cash, margin, positions, STCG/LTCG per FY, set-off, carry-forward |
| `trader/audit.py` | independent re-implementation of the books and tax, used only to check the ledger |
| `trader/metrics.py` | one definition of return, Sharpe, Sortino, drawdown, utilization, benchmark |
| `trader/evidence.py` | order success rate, latency percentiles, drift from the bot's own logs |

## Strategy: 99% equal-weight core, concentration cap, tax engine

**How it was chosen.** The live game is one 500-day path, and one path is noisy. Replaying
SimTest with a one-day difference in when 5 stocks became tradable moved final wealth between
₹12.2 L and ₹14.4 L. So every candidate was scored over **all 32 live-length episodes** the data
allows: 500 trading days each, the same 400-day warm-up the bot gets from `GET /history`,
recorded news replayed, day 1 observed. In those episodes, the tactical signal layer cost 5–10
points of mean return, and the plain core did best (research/RESULTS.md, e1–e3).

- **Core** (99%): spread equally over every stock **on day 1** (up to 10 orders a day) and held
  through dips. Only 0.5% (~₹5,000) stays in cash for fees; a whole run's fees were ₹1,630.
  It is sized at the lower of cost and price, so a winner is never trimmed just because it rose.
  It is held more than 365 days where possible (10% LTCG with the ₹1 L exemption instead of 15% STCG).
- **Concentration cap** (30%): a stock that grows past 30% of wealth is trimmed back to 26%.
  - A holding older than 365 days is trimmed at once; the order logs the gain, how much of it
    fits in the ₹1 L LTCG exemption, and the estimated tax.
  - A younger holding waits for the 365-day mark unless it passes 35%.
  - The core never re-buys above 26%.
  - Cost over the 32 episodes: about 5 points of mean return for about 1 point less drawdown.
    25% cost about 9 points; 35% about 2 points.
- **Profiles** (`STRATEGY_PROFILE` in `.env`):

  | Profile | What it is |
  |---|---|
  | `core` (default) | the above |
  | `active` | the previous default: 85% core + tactical layer (below) |
  | `hedged` | `active` + short hedge in sell-offs (below) |
  | `tactical` | signal timing only, about 50% invested |

- **Tactical layer** (profiles `active`, `hedged`, `tactical`): one score per stock per day, built from readable parts:

| Part | Signal |
|---|---|
| regime | ±0.5 for a confirmed up/down trend vs the 100-day EMA, with a 1.5 ATR hysteresis band |
| trend | 20 vs 60-day EMA gap in ATR units, weighted by the Kaufman efficiency ratio |
| meanrev | Bollinger z + RSI, only in range-bound markets; a dip with bad news is not bought |
| momentum | 20-day return / its volatility |
| news | lexicon sentiment, averaged per stock per day, 2-day half-life |
| rel_strength | optional rank among peers (`w_rs`, off by default: helped train, not OOS) |

  A score ≥ +0.30 adds an overweight on top of the core, sized by volatility: hitting the
  2.5 ATR stop loses ~1.25% of wealth. Stops and signal exits trim back to the core and never
  below it.

### Shorts, margin and the `hedged` profile

A SHORT needs a stock we don't hold. The core holds all 10 stocks, so in `core` and `active` no
short can open and **Margin Locked stays at 0**. That is a decision, not a bug: every way of
freeing a stock for shorting was tested (research/RESULTS.md, v8–v14) and all of them cost
return. The losers in this data rebound, so leaving them costs more than the short earns.

`STRATEGY_PROFILE=hedged` turns the hedge on:
- In a broad sell-off (≤ 30% of stocks above their 100-day EMA), the stock with the weakest
  relative strength that is also in a confirmed downtrend is flagged **weak**.
- Its core is sold, and that money is spread over the other 9 stocks.
- On a bounce (RSI ≥ 60 or Bollinger z ≥ +0.5), it is **SHORTed**: 7% of wealth, 100% margin
  locked.
- It is **COVERed** on its stop, when the bounce fades (z ≤ −1), when the downtrend ends, or
  after 5 days. The platform's 10-day settlement squares off anything left.
- Hysteresis: a weak stock stays weak while its downtrend lasts and it ranks in the bottom half,
  so it doesn't flip in and out of the core.

Every order logs why it was sent (`weak stock (bottom relative strength), rip setup …`). In the
replay of the live run, `hedged` made 10 shorts and 10 covers. It finished 4.4 points behind the
85% `active` profile it is built on, and its drawdown was not lower.

`STRATEGY_PROFILE=tactical` switches back to the timing-only design. Shorts trade often there
(51 in 2025-26), but at about 50% invested it earns far less.

## Risk management

- **Portfolio guard** (`risk.py`), measured against the peak of the last 60 days:

  | Condition | Effect |
  |---|---|
  | drawdown ≥ 8% | new positions at half size |
  | drawdown ≥ 15% | no new positions, shorts closed |
  | one-day loss ≥ 3% | no new positions for 2 days |

  The core is *not* sold on a drawdown: `dd_core_cut=0.5` was tested and cost 9 points out of
  sample (it sold at lows).
- **Kill switch**: create a file named `STOP` in the bot folder to allow exits only. Create
  `FLATTEN` to close every position. Delete the file to resume. The check runs every simulated
  day, with no restart needed.
- **Order circuit breaker**: 3 rejects in a row pause orders for 60 s and force a resync.
- **Zero-rejection design**: every order is checked against the shadow ledger using the gateway's
  rules, and entries are checked against a running cash budget, before sending. The ledger is
  re-synced with `/portfolio` every 3 days and after any anomaly.
- **Request budget**: token bucket at 100/min (limit 120); public endpoints don't use the token;
  ≤ 10 orders/day; exits go before entries. Heartbeat ACKs and `/stage` polls have priority: the
  local budget never skips them (a skipped stage poll goes stale after 10 s and pauses trading).
- **Pre-flight → simulation switch**: day 1 of the simulation arrives while the stage still says
  pre-flight, so it is decided without trading. When the switch comes, the bot keeps the history
  and news it already has (no re-download), and trades that day at once if it is still current
  (no later tick, less than one day-interval old). Live SimTest lost day 1 this way.
- **Universe screen**: before a stock gets new money it must have:
  - ≥ 120 bars of history;
  - median turnover ≥ ₹50 L/day;
  - ≤ 5% zero-volume days, and no 40% one-day jumps (bad tick or corporate action);
  - no gaps over 10 days, ATR ≤ 10%, and a recent last bar.

  Stocks listed mid-run are discovered from the feed or `GET /stocks`, warm-started and screened.
  A stock that fails can still be exited. In-run re-checks skip the gap rule, so a mid-run restart
  doesn't lock stocks out.

## Order failure handling

| Situation | Handling |
|---|---|
| connect timeout (never reached the gateway) | retried once |
| read timeout, connection reset, HTTP 5xx | the order may have executed: looked up in `GET /orders` twice; found = booked, not found = **UNKNOWN**: not booked, not resent, rest of the day halted, resync from `/portfolio` |
| partial fill (`filled_quantity` < requested) | only the filled shares are booked; harvest re-buys only re-buy what was sold; resync |
| duplicate | a (day, ticker, action) is sent at most once per day |
| 429 / auth / not-verified | pause (30–60 s); token errors disable orders with a clear log line |
| cancel | the gateway has no cancel endpoint (market orders fill at once), so "cancel failed" means "outcome unknown", handled as above |

## Tax

- Per Indian FY (Apr–Mar):
  - **Sec 70**: within a year, short-term losses offset long-term gains.
  - **Sec 74**: unabsorbed losses carry forward.
  - **Sec 112A**: the first ₹1 L of LTCG per FY is exempt.
  - Short covers are always short-term. Holding exactly 365 days is short-term; 366 is long-term.
- Realized gains per FY are loaded from `GET /tax/report` at startup, so a restart doesn't forget them.
- **Cost basis = average cost, like the platform**: the platform's per-order `realized_pnl` over the
  live SimTest sums to ₹77,058.03, our average-cost replay to ₹77,046.57 (FIFO would give ₹1,21,572).
  How the platform dates a partial sale of shares bought on several days could not be checked
  (no taxable sale yet), so the concentration cap treats a holding as long-term only when even its
  latest purchase is over 365 days old.
- **Tax-loss harvesting with re-buy**, March 15–31:
  - Only when the FY still has taxable gains after all set-offs.
  - Sell the losing position and buy it straight back (India has no wash-sale rule).
  - Tax is saved and the position stays the same, for about ₹55 in fees; it runs only if the
    saving is more than 4× the fees.
- **LTCG deferral**: a soft exit is deferred for a winner within 30 days of the 365-day mark.
- **Validated**:
  - An independent tax implementation (`audit.py`) matches the ledger on 300 random multi-year cases.
  - Every backtest's books are replayed independently and match to the paisa: cash, margin,
    positions, average prices, fees, FY gains, tax and wealth (`report.py`, `verify.py`).

## Results (`python report.py`, data fingerprint in `reports/REPORT.md`)

**Every live-length run in the data** (32 episodes of 500 trading days, 400-day warm-up,
recorded news, day 1 observed, after tax on the platform basis):

| | Mean | Median | Worst | Mean vs B&H | Mean max DD | Mean util | Mean orders | Mean taxes |
|---|---|---|---|---|---|---|---|---|
| **default: 99% core, cap 30%, 10 orders/day** | **+101.5%** | +72.6% | **+42.6%** | −7.9 | 24.9% | 98.7% | 23 | ₹4,658 |
| same, 97% core / 2% cash | +99.9% | +71.7% | +41.6% | −9.5 | 24.6% | 97.2% | 22 | ₹4,287 |
| as run live (80% + tactical) | +93.0% | +70.9% | +30.1% | −16.4 | 24.0% | 93.7% | 47 | ₹4,704 |

**Live SimTest replay** (`python backtest.py --replay-live`), in the leaderboard's columns:

| Run | Wealth | Realized | Unrealized | Charges | Taxes | Util | Orders | Errors | POS |
|---|---|---|---|---|---|---|---|---|---|
| live (portal) | 14,40,868 | 77,058 | 3,65,453 | 1,630 | 0 | 88.5% | 60 | 0 | 10 |
| replay, settings that ran live | 13,33,507 | 36,540 | 2,98,084 | 1,116 | 0 | 90.3% | 42 | 0 | 10 |
| replay, current default | 14,27,401 | 43,018 | 3,84,619 | 235 | 0 | 93.7% | 11 | 0 | 10 |

- The live run did better than its own settings replay because of luck: a history glitch (now fixed)
  delayed 5 stocks by about 5 days, and that delay happened to pay off (one day shorter or longer
  replays to 12.5 L or 12.2 L). Replaying the actual glitch gives 14,38,801, within 0.14% of
  live. The fair comparison is replay vs replay: the new default is +9.4 points.
- Fixed windows (trade from day 1), default: 2022-23 +46.5%, 2024 +66.9%, 2025-26 +25.8%,
  SimTest days +43.2%, full 2022–26 +271.9% (buy & hold +38.7 / +68.0 / +27.4 / +44.9 / +328.9).
- **Honest limits**:
  - Over the 500-day episodes, buy & hold of every stock with 100% invested still did better
    on average (−9.8 points). The gap is the 3% kept in cash and the concentration cap, which
    sells part of the biggest winner (PLNX, 13× in the data). Without the cap the gap is −4.9.
  - All data is now used for choosing parameters, so there is no untouched hold-out set any more.
  - 10 stocks over 4 years is a small sample.

## Live sandbox measurements (earlier runs, before this change)

- **Stream**: ticks arrive within ~100 ms; with 1 consumer, 0 late ticks; decisions take
  348 ms on average and 526 ms at most over 20 days.
- **Orders**:
  - 65+ real orders including SHORT/COVER, with 0 rejected, 0 errors and 0 rate-limit hits.
  - The ledger matched `/portfolio` at every reconcile.
  - A 1-share short round trip matched the server to the paisa.
- **Gateway round trip**: 85 ms idle, up to ~1.8 s under load.
- **Server clock**: the server's clock is IST but labelled UTC. The platform's `decision_delay`
  includes 1.6–4.6 s of publishing lag that is the same for every team.
- These numbers come from earlier sessions whose logs aren't in this folder. To regenerate the
  evidence, run in the sandbox, then run `python report.py`. The "Live evidence" section reads
  `logs/` and computes:
  - order success rate;
  - p50/p95/max latency;
  - unknown outcomes;
  - reconcile drift.

## What the bot logs

| File | Contents |
|---|---|
| `logs/decisions.jsonl` | per day: every stock's score and parts, risk state, intents, latency, wealth |
| `logs/orders.jsonl` | every order with an `explain` record: signal, confidence, sizing, risk checks, status, latency |
| `logs/screen.jsonl` | universe screen results and stats per stock |
| `logs/reconcile.jsonl` | every server sync and any differences found |
| `logs/news.jsonl` | every headline, its score and matched words |
| `logs/state.json` | live snapshot for `dashboard.py` |

## Secrets

- The token lives only in `.env`, which is git-ignored and excluded by `package.py`.
- `.env.example` ships with an empty token.
- `package.py` refuses to build if any shipped file contains:
  - a JWT-like string;
  - a filled-in token or Bearer credential;
  - a password assignment;
  - the live token value.
- **If a ZIP containing `.env` was ever shared, or the token was pasted anywhere, regenerate the
  token on the portal and update `.env`.**
