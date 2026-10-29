"""Merge the price ticks recorded during live runs into data/history (see trader/datastore.py).

Runs automatically when run_bot.py exits; run it by hand any time:
    python update_history.py
"""
from trader.config import Config
from trader.datastore import merge_stream_into_history

if __name__ == "__main__":
    for line in merge_stream_into_history(Config().data_dir) or ["no recorded runs of 20+ days yet"]:
        print(line)
