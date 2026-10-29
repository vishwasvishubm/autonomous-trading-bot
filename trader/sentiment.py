"""Financial news sentiment: weighted lexicon with phrases and negation.

Why a lexicon instead of a large NLP model: headlines are short and formulaic,
tickers are masked, the venue has no internet for model downloads, and a jury can
read exactly why a headline scored the way it did. Weights can be re-fitted from
recorded news (see analyze_news.py) without touching the code.
"""
from __future__ import annotations

import math
import re

# multi-word phrases are matched first and their words removed
PHRASES = {
    "record high": 2.0, "all-time high": 2.0, "beats estimates": 2.5, "beat estimates": 2.5,
    "above expectations": 2.0, "raises guidance": 2.5, "upgraded to buy": 2.5, "strong demand": 1.5,
    "order win": 2.0, "wins order": 2.0, "new contract": 1.5, "share buyback": 2.0, "buyback": 1.5,
    "interim dividend": 1.5, "special dividend": 2.0, "bonus issue": 1.5, "market share gain": 1.5,
    "misses estimates": -2.5, "missed estimates": -2.5, "below expectations": -2.0,
    "cuts guidance": -2.5, "lowers guidance": -2.5, "profit warning": -3.0, "downgraded to sell": -2.5,
    "net loss": -2.0, "credit downgrade": -2.5, "rating downgrade": -2.5, "under investigation": -2.5,
    "show cause": -2.0, "class action": -2.0, "data breach": -2.0, "product recall": -2.0,
    "plant shutdown": -2.0, "debt default": -3.0, "steps down": -1.5, "loss widens": -2.5,
    "profit falls": -2.0, "profit drops": -2.0, "profit declines": -2.0, "revenue falls": -1.5,
}

WORDS = {
    # bullish
    "profit": 0.8, "profits": 0.8, "surge": 2.0, "surges": 2.0, "surged": 2.0, "soar": 2.0, "soars": 2.0,
    "jump": 1.5, "jumps": 1.5, "rally": 1.5, "rallies": 1.5, "growth": 1.2, "grows": 1.2, "record": 1.2,
    "beat": 1.5, "beats": 1.5, "expansion": 1.2, "expands": 1.2, "dividend": 1.0, "partnership": 1.2,
    "acquisition": 0.8, "acquires": 0.8, "upgrade": 1.8, "upgraded": 1.8, "outperform": 1.5,
    "approval": 1.2, "approved": 1.2, "launch": 0.8, "launches": 0.8, "strong": 1.0, "robust": 1.0,
    "rise": 1.0, "rises": 1.0, "gain": 1.0, "gains": 1.0, "boost": 1.0, "boosts": 1.0, "wins": 1.2,
    "win": 1.0, "milestone": 1.0, "breakthrough": 1.5, "exports": 0.5, "optimistic": 1.0, "bullish": 1.5,
    "recovery": 0.8, "rebound": 1.0, "accretive": 1.0, "inflows": 0.8,
    # bearish
    "loss": -1.5, "losses": -1.5, "plunge": -2.5, "plunges": -2.5, "plunged": -2.5, "crash": -2.5,
    "drop": -1.5, "drops": -1.5, "fall": -1.2, "falls": -1.2, "decline": -1.2, "declines": -1.2,
    "fraud": -3.0, "scam": -3.0, "scrutiny": -2.0, "probe": -2.0, "raid": -2.0, "resignation": -1.8,
    "resigns": -1.8, "downgrade": -2.0, "downgraded": -2.0, "lawsuit": -1.8, "penalty": -1.5,
    "fine": -1.0, "fined": -1.5, "strike": -1.5, "layoffs": -1.5, "weak": -1.2, "slump": -2.0,
    "slumps": -2.0, "default": -2.5, "bankruptcy": -3.0, "insolvency": -3.0, "recall": -1.5,
    "warning": -1.5, "delay": -1.0, "delays": -1.0, "halt": -1.5, "halts": -1.5, "cut": -1.0,
    "cuts": -1.0, "miss": -1.5, "misses": -1.5, "bearish": -1.5, "outflows": -0.8, "breach": -1.5,
    "investigation": -2.0, "violation": -1.8, "suspend": -1.8, "suspended": -1.8, "tumble": -2.0,
    "tumbles": -2.0, "sinks": -2.0, "pressure": -0.8, "concern": -0.8, "concerns": -0.8,
}

NEGATORS = {"no", "not", "never", "denies", "denied", "without", "rejects", "dismissed", "cleared"}
INTENSIFIERS = {"sharply": 1.4, "massive": 1.4, "huge": 1.3, "significant": 1.2, "major": 1.2, "record": 1.2}
_PCT = re.compile(r"(\d+(?:\.\d+)?)\s*%")
_TOKEN = re.compile(r"[a-z][a-z\-']*")


def _score_text(text: str) -> tuple[float, list[str]]:
    t = " " + text.lower() + " "
    total, hits = 0.0, []
    for phrase, w in PHRASES.items():
        if phrase in t:
            total += w
            hits.append(f"{phrase}({w:+})")
            t = t.replace(phrase, " ")
    tokens = _TOKEN.findall(t)
    for i, tok in enumerate(tokens):
        w = WORDS.get(tok)
        if w is None:
            continue
        window = tokens[max(0, i - 3):i]
        if any(n in NEGATORS for n in window):
            w = -0.5 * w
        if i > 0 and tokens[i - 1] in INTENSIFIERS:
            w *= INTENSIFIERS[tokens[i - 1]]
        total += w
        hits.append(f"{tok}({w:+.1f})")
    # a quoted move ("profit surges 24%") strengthens whatever direction we found
    pcts = [float(m) for m in _PCT.findall(text)]
    if pcts and total != 0:
        total *= 1.0 + min(max(pcts), 50.0) / 100.0
    return total, hits


def score_news(headline: str, description: str = "") -> tuple[float, list[str]]:
    """Return (sentiment in [-1, 1], matched terms)."""
    h, h_hits = _score_text(headline or "")
    d, d_hits = _score_text(description or "")
    raw = h + 0.5 * d
    return math.tanh(raw / 3.0), h_hits + [f"desc:{x}" for x in d_hits]


class NewsBook:
    """Per-ticker sentiment that decays with a half-life measured in trading days.

    The live feed carries dozens of real-world headlines per day, many only loosely
    related to the tagged company. Summing them would saturate the score on busy
    days, so each day's items are *averaged* per ticker, with a floor of
    MIN_COUNT items so one stray mild headline can't dominate either.
    """

    MIN_COUNT = 3

    def __init__(self, half_life_days: float = 2.0):
        self.decay = 0.5 ** (1.0 / half_life_days) if half_life_days > 0 else 0.0
        self.carry: dict[str, float] = {}           # decayed score from previous days
        self.today: dict[str, list[float]] = {}     # this day's item scores
        self.last_headline: dict[str, str] = {}
        self.market_carry = 0.0
        self.market_today: list[float] = []

    def _day_score(self, items: list[float]) -> float:
        return sum(items) / max(self.MIN_COUNT, len(items)) if items else 0.0

    def new_day(self) -> None:
        tickers = set(self.carry) | set(self.today)
        self.carry = {t: self.decay * self.get_ticker_only(t) for t in tickers}
        self.market_carry = self.decay * max(-1.0, min(1.0, self.market_carry + self._day_score(self.market_today)))
        self.today, self.market_today = {}, []

    def add(self, ticker: str | None, score: float, headline: str, universe: set[str]) -> None:
        if ticker and ticker in universe:
            self.today.setdefault(ticker, []).append(score)
            self.last_headline[ticker] = headline
        else:  # market-wide / sector news: small effect on everyone
            self.market_today.append(0.3 * score)

    def count_today(self, ticker: str) -> int:
        return len(self.today.get(ticker, []))

    def get_ticker_only(self, ticker: str) -> float:
        s = self.carry.get(ticker, 0.0) + self._day_score(self.today.get(ticker, []))
        return max(-1.0, min(1.0, s))

    def get(self, ticker: str) -> float:
        market = self.market_carry + self._day_score(self.market_today)
        return max(-1.0, min(1.0, self.get_ticker_only(ticker) + market))
