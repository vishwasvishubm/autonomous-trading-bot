"""Thread-safe token bucket used to stay under the 120 requests/minute team limit."""
from __future__ import annotations

import threading
import time


class TokenBucket:
    def __init__(self, per_minute: int, burst: int | None = None):
        self.rate = per_minute / 60.0                 # tokens per second
        self.capacity = float(burst if burst is not None else max(1, per_minute // 6))
        self.tokens = self.capacity
        self.updated = time.monotonic()
        self.penalty_until = 0.0                      # set after an HTTP 429
        self._lock = threading.Lock()

    def _refill(self, now: float) -> None:
        self.tokens = min(self.capacity, self.tokens + (now - self.updated) * self.rate)
        self.updated = now

    def try_acquire(self) -> bool:
        """Take a token without waiting. False if the budget is exhausted."""
        with self._lock:
            now = time.monotonic()
            if now < self.penalty_until:
                return False
            self._refill(now)
            if self.tokens >= 1.0:
                self.tokens -= 1.0
                return True
            return False

    def acquire(self, timeout: float) -> bool:
        """Wait up to `timeout` seconds for a token."""
        deadline = time.monotonic() + timeout
        while True:
            if self.try_acquire():
                return True
            with self._lock:
                now = time.monotonic()
                wait = max(self.penalty_until - now, (1.0 - self.tokens) / self.rate, 0.01)
            if now + wait > deadline:
                return False
            time.sleep(min(wait, 0.25))

    def available(self) -> float:
        with self._lock:
            self._refill(time.monotonic())
            return self.tokens

    def penalize(self, seconds: float = 30.0) -> None:
        """Server put us in the penalty box: stop all authenticated calls."""
        with self._lock:
            self.penalty_until = time.monotonic() + seconds
            self.tokens = 0.0


class SlidingWindow:
    """At most `limit` requests in ANY `window`-second span, across every endpoint.

    The platform's anti-flood guard (seen live in pre-flight) blocks a team that sends
    "15 unthrottled requests across API endpoints in 5s", counting public endpoints
    too. A token bucket allows bursts that can cross that line; a sliding window
    can't. `block()` honours the server's "Unblocking in Ns" after a 429.
    """

    def __init__(self, limit: int = 12, window: float = 5.0):
        self.limit, self.window = limit, window
        self.stamps: list[float] = []
        self.blocked_until = 0.0
        self._lock = threading.Lock()

    def acquire(self, timeout: float, ignore_block: bool = False) -> bool:
        deadline = time.monotonic() + timeout
        while True:
            with self._lock:
                now = time.monotonic()
                self.stamps = [t for t in self.stamps if now - t < self.window]
                blocked = now < self.blocked_until and not ignore_block
                if not blocked and len(self.stamps) < self.limit:
                    self.stamps.append(now)
                    return True
                wait = max((self.blocked_until - now) if blocked else 0.0,
                           (self.stamps[0] + self.window - now) if len(self.stamps) >= self.limit else 0.0, 0.01)
            if now + wait > deadline:
                return False
            time.sleep(min(wait, 0.1))

    def in_window(self) -> int:
        with self._lock:
            now = time.monotonic()
            return sum(1 for t in self.stamps if now - t < self.window)

    def block(self, seconds: float) -> None:
        with self._lock:
            self.blocked_until = max(self.blocked_until, time.monotonic() + seconds)
