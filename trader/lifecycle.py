"""Background workers: stage polling and pre-flight heartbeat ACKs."""
from __future__ import annotations

import logging
import threading
import time
from collections import deque

import requests

from .api import ApiError, GatewayClient
from .config import Config

log = logging.getLogger("lifecycle")


class StageWatcher(threading.Thread):
    """Polls the public GET /stage every 2.5s and exposes whether we may trade."""

    STALE_OK_S = 10.0                       # tolerate slow/failed polls for this long

    def __init__(self, cfg: Config, api: GatewayClient, interval: float = 2.5):
        super().__init__(name="stage", daemon=True)
        self.cfg, self.api, self.interval = cfg, api, interval
        self.info: dict = {}
        self.last_ok_at = 0.0
        self.poll_failures = 0
        self.stop = threading.Event()
        self.on_change = None               # callback(old_stage, new_stage)

    @property
    def ok(self) -> bool:
        """Last known stage is fresh enough to act on. One slow poll on a loaded
        server must not switch trading off for a whole simulated day."""
        return self.last_ok_at > 0 and time.monotonic() - self.last_ok_at < self.STALE_OK_S

    @property
    def stage(self) -> str:
        return str(self.info.get("stage", "")).lower()

    @property
    def status(self) -> str:
        return str(self.info.get("status", "")).upper()

    @property
    def trading_active(self) -> bool:
        if not self.ok or self.status != "RUNNING":
            return False
        if self.stage == "simulation":
            return True
        return self.cfg.trade_in_sandbox and self.stage == "development"

    @property
    def days_remaining(self) -> int | None:
        if self.stage != "simulation":
            return None                     # sandbox loops forever: no wind-down
        try:
            return int(self.info["total_days"]) - int(self.info["days_completed"])
        except (KeyError, TypeError, ValueError):
            return None

    def poll_once(self) -> None:
        old = self.stage
        try:
            self.info = self.api.stage()
            self.last_ok_at = time.monotonic()
            self.poll_failures = 0
        except (ApiError, requests.RequestException, ValueError) as exc:
            self.poll_failures += 1
            if self.poll_failures == 1 or not self.ok:
                log.warning("stage poll failed (%d in a row, last good %.0fs ago): %s", self.poll_failures,
                            time.monotonic() - self.last_ok_at if self.last_ok_at else -1, exc)
            return
        if self.stage != old:
            log.info("stage %s -> %s (status %s)", old or "?", self.stage, self.status)
            if self.on_change and old:
                self.on_change(old, self.stage)

    def run(self) -> None:
        while not self.stop.is_set():
            self.poll_once()
            self.stop.wait(self.interval)


class HeartbeatWorker(threading.Thread):
    """ACKs every pre-flight sequence number, in order, without blocking the engine.

    The rules (problem statement, Pre-Flight Readiness): "continuously acknowledge
    each sequence number" via POST /bot/heartbeat, at least once every 15 s. So
    every new seq is queued and sent; a seq seen twice within 30 s (e.g. the same
    tick on several partitions, or re-read after a rebalance) is sent once. A failed ACK stays at the head
    of the queue and is retried after 1 s. If the queue backs up (gateway down),
    only the newest MAX_BACKLOG are kept: ACKs for very old ticks are useless.

    Every STATUS_EVERY_S it also reads GET /bot/status, so the log shows what the
    platform's readiness tracker sees (is_ready, consumer count, last seq).
    """

    MAX_BACKLOG = 50
    STATUS_EVERY_S = 30.0

    def __init__(self, api: GatewayClient, min_gap: float = 0.0):
        super().__init__(name="heartbeat", daemon=True)
        self.api, self.min_gap = api, min_gap
        self.latest_seq: int | None = None
        self.acked_seq: int | None = None
        self.acked_count = 0
        self.dropped = 0
        self.last_ack = 0.0
        self.failures = 0
        self.platform_status: dict = {}
        self.last_status_at = 0.0
        self.pending: deque = deque()
        self._seen: deque = deque(maxlen=2000)       # (seq, time) recently queued, for de-duplication
        self._lock = threading.Lock()
        self.stop = threading.Event()
        self._wake = threading.Event()

    DEDUPE_WINDOW_S = 30.0       # a repeat within 30 s is a duplicate; later = pre-flight restarted at 1

    def offer(self, seq: int) -> None:
        now = time.monotonic()
        with self._lock:
            while self._seen and now - self._seen[0][1] > self.DEDUPE_WINDOW_S:
                self._seen.popleft()
            if any(s == seq for s, _ in self._seen):
                return
            self._seen.append((seq, now))
            self.pending.append(seq)
            while len(self.pending) > self.MAX_BACKLOG:
                self.pending.popleft()
                self.dropped += 1
            self.latest_seq = seq
        self._wake.set()

    def ack_pending(self) -> int:
        """Send queued ACKs in order until the queue is empty or one fails. Returns ACKs sent."""
        sent = 0
        while not self.stop.is_set():
            with self._lock:
                if not self.pending:
                    return sent
                seq = self.pending[0]
            gap = time.monotonic() - self.last_ack
            if self.min_gap and gap < self.min_gap:
                self.stop.wait(self.min_gap - gap)
            try:
                self.api.heartbeat(seq)
            except (ApiError, requests.RequestException) as exc:
                self.failures += 1
                log.warning("heartbeat seq=%s failed (%d in a row): %s", seq, self.failures, exc)
                return sent                          # stays queued; retried on the next loop
            with self._lock:
                if self.pending and self.pending[0] == seq:
                    self.pending.popleft()
            self.acked_seq, self.last_ack, self.failures = seq, time.monotonic(), 0
            self.acked_count += 1
            sent += 1
            log.info("heartbeat ACK seq=%s", seq)
        return sent

    def poll_status(self) -> dict:
        try:
            self.platform_status = self.api.bot_status()
        except (ApiError, requests.RequestException, ValueError, AttributeError) as exc:
            self.platform_status = {"error": str(exc)[:120]}
        self.last_status_at = time.monotonic()
        return self.platform_status

    def run(self) -> None:
        while not self.stop.is_set():
            self._wake.wait(1.0)
            self._wake.clear()
            self.ack_pending()
            if self.pending:
                self.stop.wait(1.0)                  # a failed ACK: back off, then retry
                self._wake.set()
            if time.monotonic() - self.last_status_at >= self.STATUS_EVERY_S:
                st = self.poll_status()
                if "error" not in st:
                    log.info("platform readiness: ready=%s kafka_connected=%s consumers=%s heartbeat_received=%s "
                             "last_seq=%s", st.get("is_ready"), st.get("kafka_connected"),
                             st.get("kafka_consumer_count"), st.get("heartbeat_received"), st.get("last_seq"))
