"""Portfolio-level risk guard: drawdown limits, daily loss limit, kill switch.

The planner sizes each trade; this module decides how much risk the *whole book*
may add today. It runs inside the same code path in live trading and in the
backtest, so its cost (missed rebounds) shows up in the backtest numbers.

    normal      full size
    reduced     drawdown >= dd_soft: new positions at half size
    defensive   drawdown >= dd_hard: no new positions, shorts closed, core scaled by dd_core_cut
    cooldown    one-day loss >= daily_loss_limit: no new positions for N days
    halted      STOP file present: no new orders except exits (manual kill switch)
    flatten     FLATTEN file present: close every position, then stay halted

Drawdown is measured from the highest wealth of the last `dd_window` days, so the
guard can't lock the bot out of the market forever after one bad stretch.
"""
from __future__ import annotations

from collections import deque
from dataclasses import dataclass, field
from pathlib import Path


@dataclass
class RiskState:
    mode: str = "normal"
    size_mult: float = 1.0
    allow_entries: bool = True
    close_shorts: bool = False
    core_mult: float = 1.0          # scales the passive core (defensive mode may cut it)
    flatten: bool = False
    drawdown: float = 0.0
    day_return: float = 0.0
    reasons: list[str] = field(default_factory=list)


class RiskGuard:
    def __init__(self, p, control_dir: Path | None = None):
        self.p = p
        self.control_dir = control_dir           # where STOP / FLATTEN files are looked for
        self.history: deque = deque(maxlen=max(1, p.dd_window))
        self.prev_wealth: float | None = None
        self.cooldown_until = -1
        self.state = RiskState()

    def _flag(self, name: str) -> bool:
        return bool(self.control_dir) and (self.control_dir / name).exists()

    def update(self, wealth: float, day: int) -> RiskState:
        p = self.p
        self.history.append(wealth)
        peak = max(self.history)
        dd = 1.0 - wealth / peak if peak > 0 else 0.0
        day_ret = wealth / self.prev_wealth - 1.0 if self.prev_wealth else 0.0
        self.prev_wealth = wealth
        st = RiskState(drawdown=dd, day_return=day_ret)

        if day_ret <= -p.daily_loss_limit:
            self.cooldown_until = day + p.loss_cooldown_days
            st.reasons.append(f"daily loss {day_ret:.1%} <= -{p.daily_loss_limit:.0%}")

        if self._flag("FLATTEN"):
            st.mode, st.flatten, st.allow_entries, st.size_mult = "flatten", True, False, 0.0
            st.reasons.append("FLATTEN file present: closing all positions")
        elif self._flag("STOP"):
            st.mode, st.allow_entries, st.size_mult = "halted", False, 0.0
            st.reasons.append("STOP file present: exits only")
        elif dd >= p.dd_hard:
            st.mode, st.allow_entries, st.close_shorts, st.size_mult = "defensive", False, True, 0.0
            st.core_mult = p.dd_core_cut
            st.reasons.append(f"drawdown {dd:.1%} >= hard limit {p.dd_hard:.0%}")
        elif day < self.cooldown_until:
            st.mode, st.allow_entries, st.size_mult = "cooldown", False, 0.0
            st.reasons.append(f"loss cooldown until day {self.cooldown_until}")
        elif dd >= p.dd_soft:
            st.mode, st.size_mult = "reduced", 0.5
            st.reasons.append(f"drawdown {dd:.1%} >= soft limit {p.dd_soft:.0%}: half size")
        self.state = st
        return st
