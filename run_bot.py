"""MANTHAN 2026 trading bot entry point.

    python run_bot.py                  # trade only in the official live simulation
    python run_bot.py --sandbox        # also trade in the development sandbox
    python run_bot.py --dry-run        # decide and log, never send orders
"""
from __future__ import annotations

import argparse
import logging
import sys

from trader.api import GatewayClient
from trader.config import Config
from trader.engine import Engine
from trader.lifecycle import HeartbeatWorker, StageWatcher
from trader.stream import KafkaFeed


def setup_logging(cfg: Config) -> None:
    cfg.log_dir.mkdir(parents=True, exist_ok=True)
    fmt = logging.Formatter("%(asctime)s %(levelname)-7s %(name)-9s %(message)s", "%H:%M:%S")
    root = logging.getLogger()
    root.setLevel(logging.INFO)
    for h in (logging.StreamHandler(sys.stdout), logging.FileHandler(cfg.log_dir / "bot.log", encoding="utf-8")):
        h.setFormatter(fmt)
        root.addHandler(h)
    logging.getLogger("kafka").setLevel(logging.WARNING)


def main() -> None:
    ap = argparse.ArgumentParser()
    ap.add_argument("--sandbox", action="store_true", help="trade in the development sandbox too")
    ap.add_argument("--dry-run", action="store_true", help="never send orders")
    ap.add_argument("--consumers", type=int, help="parallel Kafka consumers (1-10)")
    args = ap.parse_args()

    cfg = Config()
    if args.sandbox:
        cfg.trade_in_sandbox = True
    if args.dry_run:
        cfg.dry_run = True
    if args.consumers:
        cfg.kafka_consumers = args.consumers
    setup_logging(cfg)
    log = logging.getLogger("main")
    log.info("gateway %s | kafka %s | group %s | consumers %d | sandbox trading %s | dry run %s | profile %s",
             cfg.gateway, cfg.broker, cfg.group_id, cfg.kafka_consumers, cfg.trade_in_sandbox, cfg.dry_run,
             cfg.profile)
    log.info("kill switch: create %s (exits only) or %s (close everything)", cfg.control_dir / "STOP",
             cfg.control_dir / "FLATTEN")
    if not cfg.token:
        log.warning("BOT_ACCESS_TOKEN not set: heartbeats and orders are disabled (observe mode)")

    api = GatewayClient(cfg)
    stage = StageWatcher(cfg, api)
    stage.poll_once()
    stage.start()
    hb = HeartbeatWorker(api)
    hb.start()

    engine = Engine(cfg, api, stage, hb)
    engine.bootstrap()
    feed = KafkaFeed(cfg, engine.q)
    feed.start()
    try:
        engine.run(feed)
    except KeyboardInterrupt:
        log.info("stopping (Ctrl+C)")
    finally:
        engine.stop.set()
        stage.stop.set()
        hb.stop.set()
        feed.close()
        engine.write_state()
        log.info("final stats: %s | api: %s", engine.stats, api.stats)
        engine.recorder.close()
        try:                                   # every run's prices feed the next backtest
            from trader.datastore import merge_stream_into_history
            for line in merge_stream_into_history(cfg.data_dir):
                log.info("history: %s", line)
        except Exception:                      # never let bookkeeping hide the shutdown
            log.exception("merging recorded prices into data/history failed")


if __name__ == "__main__":
    main()
