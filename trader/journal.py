"""Append-only JSONL journals: every decision, order and news item is explainable later.

File handles stay open (opening a file per line is slow on Windows and was
delaying tick processing on heavy-news days); each line is flushed immediately
so a crash never loses the record of an order.
"""
from __future__ import annotations

import atexit
import json
import threading
from datetime import datetime, timezone
from pathlib import Path


class Journal:
    def __init__(self, folder: Path):
        folder.mkdir(parents=True, exist_ok=True)
        self.folder = folder
        self._lock = threading.Lock()
        self._files: dict[str, object] = {}
        atexit.register(self.close)

    def write(self, name: str, record: dict) -> None:
        record = {"ts": datetime.now(timezone.utc).isoformat(timespec="milliseconds"), **record}
        line = json.dumps(record, default=str)
        with self._lock:
            fh = self._files.get(name)
            if fh is None:
                fh = open(self.folder / f"{name}.jsonl", "a", encoding="utf-8")
                self._files[name] = fh
            fh.write(line + "\n")
            fh.flush()

    def snapshot(self, name: str, data: dict) -> None:
        """Atomically replace a JSON file (used for the live dashboard state)."""
        path = self.folder / f"{name}.json"
        tmp = path.with_suffix(".tmp")
        tmp.write_text(json.dumps(data, default=str), encoding="utf-8")
        tmp.replace(path)

    def close(self) -> None:
        with self._lock:
            for fh in self._files.values():
                try:
                    fh.close()
                except OSError:
                    pass
            self._files.clear()
