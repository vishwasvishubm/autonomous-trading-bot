"""Record the raw Kafka stream (ticks + news) to data/stream/*.jsonl.

Runs in its own consumer group so it never steals partitions from the bot. News
is only available live, so leave this running in the sandbox to build a dataset
for checking how prices react to headlines (analyze_news.py).

    python record_stream.py
"""
from __future__ import annotations

import queue
import time

from trader.config import Config
from trader.journal import Journal
from trader.stream import KafkaFeed


def main() -> None:
    cfg = Config()
    q: "queue.Queue[tuple[str, dict, float]]" = queue.Queue()
    feed = KafkaFeed(cfg, q, group_id=f"recorder-{cfg.username}", consumers=1)
    out = Journal(cfg.data_dir / "stream")
    feed.start()
    counts = {"ticks": 0, "news": 0}
    last = time.monotonic()
    print("recording to", cfg.data_dir / "stream", "- Ctrl+C to stop")
    try:
        while True:
            try:
                topic, ev, _ = q.get(timeout=1.0)
            except queue.Empty:
                continue
            name = "news" if topic == "market.news" else "ticks"
            out.write(name, ev)
            counts[name] += 1
            if time.monotonic() - last > 15:
                last = time.monotonic()
                print(f"ticks {counts['ticks']}  news {counts['news']}  last date {ev.get('simulated_date')}")
    except KeyboardInterrupt:
        pass
    finally:
        feed.close()
        print("recorded", counts)


if __name__ == "__main__":
    main()
