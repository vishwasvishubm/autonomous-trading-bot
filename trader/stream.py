"""Kafka ingestion: N consumer threads in the team group, feeding one queue.

Both topics have 10 partitions; consumers sharing group-<username> split them, so
ingestion runs in parallel while all decisions still happen in one engine thread
with one portfolio state (no cross-thread races on cash).
"""
from __future__ import annotations

import json
import logging
import queue
import threading
import time

from .config import Config

log = logging.getLogger("stream")
TOPICS = ("market.ticks", "market.news")
NO_PARTITIONS_S = 20.0      # rejoin the group if no partitions are assigned for this long


def _decode(raw: bytes):
    try:
        return json.loads(raw.decode("utf-8"))
    except (ValueError, UnicodeDecodeError):
        return None


class KafkaFeed:
    def __init__(self, cfg: Config, out: "queue.Queue[tuple[str, dict, float]]", group_id: str | None = None,
                 consumers: int | None = None):
        self.cfg = cfg
        self.out = out
        self.group_id = group_id or cfg.group_id
        self.n = max(1, min(10, consumers or cfg.kafka_consumers))
        self.stop = threading.Event()
        self.threads: list[threading.Thread] = []
        self.connected = [False] * self.n
        self.messages = 0

    def start(self) -> None:
        try:
            import kafka  # noqa: F401
        except ImportError as exc:
            raise SystemExit("kafka-python is not installed: pip install kafka-python") from exc
        for i in range(self.n):
            t = threading.Thread(target=self._run, args=(i,), name=f"kafka-{i}", daemon=True)
            t.start()
            self.threads.append(t)

    @property
    def is_connected(self) -> bool:
        return any(self.connected)

    def _run(self, idx: int) -> None:
        from kafka import KafkaConsumer
        from kafka.errors import KafkaError

        backoff = 1.0
        while not self.stop.is_set():
            consumer = None
            try:
                consumer = KafkaConsumer(
                    *TOPICS,
                    bootstrap_servers=[self.cfg.broker],
                    group_id=self.group_id,
                    client_id=f"{self.group_id}-{idx}",
                    auto_offset_reset="latest",
                    enable_auto_commit=False,      # always resume at live data, skip backlog
                    value_deserializer=_decode,
                    session_timeout_ms=15000,
                    heartbeat_interval_ms=3000,
                    max_poll_interval_ms=60000,
                    fetch_max_wait_ms=100,          # low latency: don't wait to fill batches
                    request_timeout_ms=30000,
                )
                self.connected[idx] = True
                backoff = 1.0
                log.info("consumer %d connected to %s as %s", idx, self.cfg.broker, self.group_id)
                last_assigned = time.monotonic()
                while not self.stop.is_set():
                    batch = consumer.poll(timeout_ms=200)
                    now = time.monotonic()
                    # a join that raced the topic metadata can end with no partitions and stay
                    # that way: no ticks, ever. Rejoin if we hold none for NO_PARTITIONS_S.
                    if consumer.assignment():
                        last_assigned = now
                    elif now - last_assigned > NO_PARTITIONS_S:
                        log.warning("consumer %d has no partitions after %.0fs: rejoining", idx, NO_PARTITIONS_S)
                        break
                    for records in batch.values():
                        for rec in records:
                            if rec.value is not None:
                                self.messages += 1
                                self.out.put((rec.topic, rec.value, now))
            except KafkaError as exc:
                log.warning("consumer %d kafka error: %s (reconnect in %.0fs)", idx, exc, backoff)
            except Exception:                      # never let a consumer thread die silently
                log.exception("consumer %d crashed (reconnect in %.0fs)", idx, backoff)
            finally:
                self.connected[idx] = False
                if consumer is not None:
                    try:
                        consumer.close()
                    except Exception:
                        pass
            self.stop.wait(backoff)
            backoff = min(backoff * 2, 15.0)

    def close(self) -> None:
        self.stop.set()
        for t in self.threads:
            t.join(timeout=3)
