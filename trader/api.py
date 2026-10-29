"""REST client for the Order Gateway (port 3002).

Design rules:
- Every authenticated call goes through one token bucket (team limit 120/min).
- Public endpoints (/stage, /simulation/clock, /stocks/:ticker/history) are called
  without the token so they don't eat the team budget.
- Orders are never blindly retried. Outcomes are classified:
    connect timeout          request never reached the gateway -> safe to retry once
    read timeout / reset /   the gateway may have executed it -> look it up in
    HTTP 5xx                   GET /orders (twice, 0.4 s apart); found = filled,
                               not found = UNKNOWN (engine resyncs from /portfolio,
                               never resends blindly)
    filled qty < requested   PARTIAL: the filled part is booked, the rest dropped
  The gateway has no cancel endpoint (market orders fill at once), so "cancel
  failed" can only mean "outcome unknown", handled the same way.
"""
from __future__ import annotations

import logging
import time
from dataclasses import dataclass
from datetime import datetime, timezone
from typing import Any

import requests

from .config import MAX_ORDER_QTY, Config
import re

from .ratelimit import SlidingWindow, TokenBucket

log = logging.getLogger("api")

ACTIONS = ("BUY", "SELL", "SHORT", "COVER")


@dataclass
class OrderResult:
    ok: bool
    status_code: int
    data: dict
    error_code: str | None = None
    message: str = ""
    latency_ms: float = 0.0
    filled_qty: int | None = None      # None = unknown (treat as the requested quantity)
    status: str = ""                   # FILLED | PARTIAL | REJECTED | UNKNOWN | SKIPPED
    recovered: bool = False            # outcome came from the order log after a failure


FILLED_STATES = ("FILLED", "EXECUTED", "OK", "SUCCESS", "COMPLETE", "COMPLETED")
PARTIAL_STATES = ("PARTIAL", "PARTIALLY_FILLED", "PARTIAL_FILL")


def _filled_qty(data: dict, requested: int) -> int:
    for k in ("filled_quantity", "executed_quantity", "filled_qty", "quantity_filled"):
        if data.get(k) is not None:
            try:
                return max(0, min(requested, int(float(data[k]))))
            except (TypeError, ValueError):
                pass
    return requested


class ApiError(Exception):
    def __init__(self, status: int, code: str, message: str):
        super().__init__(f"{status} {code}: {message}")
        self.status = status
        self.code = code
        self.message = message


class GatewayClient:
    def __init__(self, cfg: Config, bucket: TokenBucket | None = None):
        self.cfg = cfg
        self.base = cfg.gateway + "/api/v1"
        self.bucket = bucket or TokenBucket(cfg.requests_per_minute)
        self.pace = SlidingWindow(cfg.requests_per_5s, 5.0)   # every request, public or not
        self.session = requests.Session()
        self.public = requests.Session()        # separate pool, never carries the token
        self.session.headers.update({"Content-Type": "application/json"})
        if cfg.token:
            self.session.headers["Authorization"] = f"Bearer {cfg.token}"
        self.stats = {"requests": 0, "rate_limited": 0, "errors": 0, "orders_ok": 0, "orders_rejected": 0}

    # ------------------------------------------------------------------ helpers
    @staticmethod
    def _parse_error(resp: requests.Response) -> tuple[str, str]:
        try:
            body = resp.json()
        except ValueError:
            return f"HTTP_{resp.status_code}", resp.text[:200]
        code = body.get("error") or body.get("code") or body.get("error_code") or f"HTTP_{resp.status_code}"
        if isinstance(code, dict):
            code = code.get("code", f"HTTP_{resp.status_code}")
        return str(code), str(body.get("message", ""))

    def _auth_request(self, method: str, path: str, *, wait: float = 2.0, timeout: float = 3.0,
                      essential: bool = False, **kwargs) -> requests.Response:
        # essential (heartbeat ACKs): never held back by a local pause after someone
        # else's 429; a missed ACK window costs the pre-flight READY status
        if essential:
            self.bucket.try_acquire()
            self.pace.acquire(wait, ignore_block=True)
        elif not self.bucket.acquire(wait) or not self.pace.acquire(wait):
            raise ApiError(0, "LOCAL_RATE_BUDGET", "request budget exhausted, skipped")
        self.stats["requests"] += 1
        resp = self.session.request(method, self.base + path, timeout=(1.5, timeout), **kwargs)
        if resp.status_code == 429:
            self._on_429(resp)
        return resp

    def _on_429(self, resp: requests.Response) -> None:
        """Back off for as long as the server says ("Unblocking in 8s"), else 30 s."""
        self.stats["rate_limited"] += 1
        if "'ip:" in (resp.text or ""):
            log.warning("HTTP 429 for a shared IP, not our team: not pausing (%s)", (resp.text or "")[:120])
            return
        m = re.search(r"[Uu]nblocking in (\d+)", resp.text or "")
        secs = float(m.group(1)) + 1.0 if m else 30.0
        self.bucket.penalize(secs)
        self.pace.block(secs)
        log.warning("HTTP 429 from gateway: all requests paused %.0fs", secs)

    def _auth_json(self, method: str, path: str, **kwargs) -> dict:
        resp = self._auth_request(method, path, **kwargs)
        if resp.status_code >= 400:
            self.stats["errors"] += 1
            code, msg = self._parse_error(resp)
            raise ApiError(resp.status_code, code, msg)
        return resp.json()

    def _public_json(self, path: str, params: dict | None = None, timeout: float = 3.0, wait: float = 2.0,
                     essential: bool = False) -> dict:
        if self.cfg.token:
            # Measured live: token-less calls are rate-limited per *IP* ("Team 'ip:172.18.0.1'"),
            # an address shared with other teams, so they got blocked while our authenticated
            # calls went through. With the token they count as ours (and in our own budget).
            resp = self._auth_request("GET", path, params=params, wait=wait, timeout=timeout, essential=essential)
            if resp.status_code >= 400:
                code, msg = self._parse_error(resp)
                raise ApiError(resp.status_code, code, msg)
            return resp.json()
        if not self.pace.acquire(timeout + 5.0):
            raise ApiError(0, "LOCAL_RATE_BUDGET", "request pacing: skipped")
        self.stats["requests"] += 1
        resp = self.public.get(self.base + path, params=params, timeout=(1.5, timeout))
        if resp.status_code == 429:
            self._on_429(resp)
        if resp.status_code >= 400:
            code, msg = self._parse_error(resp)
            raise ApiError(resp.status_code, code, msg)
        return resp.json()

    # ------------------------------------------------------------- public APIs
    def stage(self) -> dict:
        # essential: a skipped poll goes stale after 10 s and switches trading off (seen live at
        # startup, when the history downloads had used the request budget)
        return self._public_json("/stage", timeout=3.0, essential=True)

    def clock(self) -> dict:
        return self._public_json("/simulation/clock", timeout=2.0)

    def history(self, ticker: str, days: int = 365, wait: float = 2.0) -> list[dict]:
        data = self._public_json(f"/stocks/{ticker}/history", params={"days": days}, timeout=8.0, wait=wait)
        return data.get("prices", [])

    # -------------------------------------------------------- authenticated APIs
    def stocks(self) -> list[dict]:
        return self._auth_json("GET", "/stocks", wait=10.0).get("stocks", [])

    def portfolio(self) -> dict:
        return self._auth_json("GET", "/portfolio", wait=1.0)

    def cash(self) -> dict:
        return self._auth_json("GET", "/portfolio/cash", wait=1.0)

    def tax_report(self) -> dict:
        return self._auth_json("GET", "/tax/report", wait=2.0, timeout=5.0)

    def orders(self, limit: int = 20) -> list[dict]:
        return self._auth_json("GET", "/orders", params={"limit": limit}, wait=1.0).get("orders", [])

    def heartbeat(self, seq: int) -> dict:
        return self._auth_json("POST", "/bot/heartbeat", json={"seq": int(seq)}, wait=1.0, timeout=2.0,
                               essential=True)

    def bot_status(self) -> dict:
        """What the platform's pre-flight readiness tracker sees: is_ready, kafka_connected,
        kafka_consumer_count, heartbeat_received, last_heartbeat_at, last_seq."""
        return self._auth_json("GET", "/bot/status", wait=1.0, timeout=2.0)

    def place_order(self, ticker: str, action: str, quantity: int, date: str | None) -> OrderResult:
        """Validate locally, then submit one market order.

        Never raises: the engine gets an OrderResult it can log and react to.
        """
        action = action.upper()
        if action not in ACTIONS:
            return OrderResult(False, 0, {}, "LOCAL_INVALID_ACTION", action, status="SKIPPED")
        if not isinstance(quantity, int) or not (1 <= quantity <= MAX_ORDER_QTY):
            return OrderResult(False, 0, {}, "LOCAL_INVALID_QUANTITY", str(quantity), status="SKIPPED")

        body: dict[str, Any] = {"ticker": ticker, "action": action, "quantity": quantity}
        if date:
            body["date"] = date
        sent_at = datetime.now(timezone.utc)
        t0 = time.perf_counter()

        def elapsed() -> float:
            return (time.perf_counter() - t0) * 1000

        for attempt in range(2):
            try:
                resp = self._auth_request("POST", "/orders", json=body, wait=1.5, timeout=3.0)
            except ApiError as exc:            # local budget exhausted: skip, don't queue stale orders
                return OrderResult(False, 0, {}, exc.code, exc.message, status="SKIPPED")
            except requests.exceptions.ConnectTimeout as exc:
                # never connected, so the order can't have been placed: one retry is safe
                log.warning("order %s %s x%d connect timeout (attempt %d)", action, ticker, quantity, attempt + 1)
                if attempt == 0:
                    continue
                self.stats["errors"] += 1
                return OrderResult(False, 0, {}, "NETWORK_ERROR", str(exc), elapsed(), status="SKIPPED")
            except requests.RequestException as exc:
                # sent but no answer: it may have executed. Ask the order log, never resend.
                log.warning("order %s %s x%d %s; checking order log", action, ticker, quantity,
                            exc.__class__.__name__)
                return self._resolve_unknown(ticker, action, quantity, sent_at, elapsed, "TIMEOUT", str(exc))

            if resp.status_code >= 500:
                code, msg = self._parse_error(resp)
                log.warning("order %s %s x%d HTTP %d; checking order log", action, ticker, quantity,
                            resp.status_code)
                return self._resolve_unknown(ticker, action, quantity, sent_at, elapsed, code, msg)
            if resp.status_code < 400:
                try:
                    data = resp.json()
                except ValueError:
                    return self._resolve_unknown(ticker, action, quantity, sent_at, elapsed, "BAD_RESPONSE",
                                                 resp.text[:200])
                status = str(data.get("status", "FILLED")).upper()
                filled = _filled_qty(data, quantity)
                if status in FILLED_STATES or status in PARTIAL_STATES:
                    self.stats["orders_ok"] += 1
                    kind = "PARTIAL" if (status in PARTIAL_STATES or filled < quantity) else "FILLED"
                    if kind == "PARTIAL":
                        self.stats["orders_partial"] = self.stats.get("orders_partial", 0) + 1
                    return OrderResult(filled > 0, resp.status_code, data, latency_ms=elapsed(),
                                       filled_qty=filled, status=kind)
                self.stats["orders_rejected"] += 1
                return OrderResult(False, resp.status_code, data, status, str(data.get("message", "")), elapsed(),
                                   filled_qty=0, status="REJECTED")
            code, msg = self._parse_error(resp)
            self.stats["orders_rejected"] += 1
            return OrderResult(False, resp.status_code, {}, code, msg, elapsed(), filled_qty=0, status="REJECTED")
        return OrderResult(False, 0, {}, "UNREACHABLE", "", status="SKIPPED")

    def _resolve_unknown(self, ticker: str, action: str, qty: int, sent_at: datetime, elapsed, code: str,
                         msg: str) -> OrderResult:
        for i in range(2):
            found = self._find_recent_order(ticker, action, qty, sent_at)
            if found is not None:
                self.stats["orders_ok"] += 1
                self.stats["orders_recovered"] = self.stats.get("orders_recovered", 0) + 1
                filled = _filled_qty(found, qty)
                return OrderResult(True, 200, found, latency_ms=elapsed(), filled_qty=filled,
                                   status="PARTIAL" if filled < qty else "FILLED", recovered=True)
            if i == 0:
                time.sleep(0.4)
        self.stats["errors"] += 1
        self.stats["orders_unknown"] = self.stats.get("orders_unknown", 0) + 1
        return OrderResult(False, 0, {}, "UNKNOWN_OUTCOME", f"{code}: {msg}", elapsed(), status="UNKNOWN")

    def _find_recent_order(self, ticker: str, action: str, qty: int, since: datetime) -> dict | None:
        try:
            recent = self.orders(limit=10)
        except (ApiError, requests.RequestException):
            return None
        for o in recent:
            if o.get("ticker") != ticker or str(o.get("action", "")).upper() != action:
                continue
            if int(o.get("quantity", 0) or 0) not in (qty, _filled_qty(o, qty)):
                continue
            ts = o.get("executed_at") or o.get("created_at")
            try:
                when = datetime.fromisoformat(str(ts).replace("Z", "+00:00"))
            except ValueError:
                continue
            if when >= since.replace(microsecond=0):
                return o
        return None
