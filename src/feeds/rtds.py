"""Live Chainlink price feed from Polymarket RTDS.

One WebSocket per asset, subscribed to `crypto_prices_chainlink` (the raw
per-second Chainlink print). The 60s TWAP is computed locally from these
prints; the independent TWAP topic can be added later for validation.

Buffer methods used by the strategy:
  * latest_price()
  * mean_between(a, b)          -> observed part of the settlement TWAP
  * twap_ending_at(end, window) -> close reference for settlement
  * sigma(lookback, min_samples)-> per-second log-return volatility
"""
from __future__ import annotations

import asyncio
import json
import math
import time
from collections import deque
from typing import Optional

import numpy as np
import websockets

from src.infra.logging import get_logger

log = get_logger(__name__)

SYMBOLS = {"BTC": "btc/usd", "ETH": "eth/usd"}


class PriceBuffer:
    def __init__(self, retention_s: float = 900.0) -> None:
        self.retention_s = float(retention_s)
        self._pts: deque[tuple[float, float]] = deque()
        self.last_recv_ts: float = 0.0
        self.message_count: int = 0

    def add(self, ts: float, price: float) -> None:
        if not math.isfinite(price) or price <= 0:
            return
        if self._pts and ts <= self._pts[-1][0]:
            # Backfill overlapping the live stream: keep the newer live point.
            return
        self._pts.append((float(ts), float(price)))
        cutoff = float(ts) - self.retention_s
        while self._pts and self._pts[0][0] < cutoff:
            self._pts.popleft()
        self.last_recv_ts = time.time()
        self.message_count += 1

    def latest(self) -> Optional[tuple[float, float]]:
        return self._pts[-1] if self._pts else None

    def latest_price(self) -> Optional[float]:
        return self._pts[-1][1] if self._pts else None

    def count_between(self, a: float, b: float) -> int:
        return sum(1 for ts, _ in self._pts if a <= ts < b)

    def mean_between(self, a: float, b: float) -> Optional[float]:
        vals = [p for ts, p in self._pts if a <= ts < b]
        if not vals:
            return None
        return float(sum(vals) / len(vals))

    def twap_ending_at(self, end_ts: float, window_s: float = 60.0) -> Optional[float]:
        start = end_ts - window_s
        vals = [p for ts, p in self._pts if start <= ts < end_ts]
        # Require most of the window so a partial buffer cannot masquerade as
        # the official TWAP. Settlement falls back to Gamma when this is None.
        if len(vals) < max(15, int(window_s * 0.6)):
            return None
        return float(sum(vals) / len(vals))

    def sigma(self, lookback_s: float, min_samples: int) -> Optional[float]:
        if len(self._pts) < min_samples:
            return None
        now = self._pts[-1][0]
        pts = [(ts, p) for ts, p in self._pts if ts >= now - lookback_s]
        if len(pts) < min_samples:
            return None
        returns: list[float] = []
        for (t0, p0), (t1, p1) in zip(pts, pts[1:]):
            dt = t1 - t0
            if dt <= 0 or dt > 3.0:
                continue
            if p0 > 0 and p1 > 0:
                returns.append(math.log(p1 / p0))
        if len(returns) < 30:
            return None
        value = float(np.std(np.asarray(returns, dtype=float), ddof=1))
        if not math.isfinite(value) or value <= 0:
            return None
        return value

    def points(self) -> int:
        return len(self._pts)

    def health(self) -> dict:
        latest = self.latest()
        return {
            "points": len(self._pts),
            "last_recv_ts": self.last_recv_ts,
            "last_price": latest[1] if latest else None,
            "last_data_ts": latest[0] if latest else None,
            "messages": self.message_count,
        }


class RtdsFeed:
    TOPIC = "crypto_prices_chainlink"
    TWAP_TOPIC = "crypto_prices_twap_sixty"

    def __init__(
        self,
        assets: list[str],
        url: str,
        buffer_s: float = 900.0,
        reconnect_base_s: float = 1.0,
        reconnect_max_s: float = 30.0,
        latency=None,
        on_message=None,
        twap_topic: bool = False,
    ) -> None:
        self.assets = [a.upper() for a in assets]
        self.url = url
        self.latency = latency
        # Optional recorder tap: called with (asset, recv_ts, raw_message)
        # for every parsed RTDS message, before it reaches the price buffer.
        self.on_message = on_message
        self.twap_topic = bool(twap_topic)
        self.reconnect_base_s = float(reconnect_base_s)
        self.reconnect_max_s = float(reconnect_max_s)
        self.buffers = {a: PriceBuffer(buffer_s) for a in self.assets}
        self.connected = {a: False for a in self.assets}
        self.last_error = {a: "" for a in self.assets}
        self._running = False
        self._tasks: list[asyncio.Task] = []

    def buffer(self, asset: str) -> Optional[PriceBuffer]:
        return self.buffers.get(asset.upper())

    def start(self) -> None:
        self._running = True
        for asset in self.assets:
            self._tasks.append(
                asyncio.create_task(self._run_asset(asset), name=f"rtds-{asset}")
            )
        log.info("rtds.started", assets=self.assets, url=self.url)

    async def stop(self) -> None:
        self._running = False
        for task in self._tasks:
            task.cancel()
        for task in self._tasks:
            try:
                await task
            except asyncio.CancelledError:
                pass
        self._tasks.clear()

    def health(self) -> dict:
        return {
            a: {
                "connected": self.connected.get(a, False),
                "last_error": self.last_error.get(a, ""),
                **self.buffers[a].health(),
            }
            for a in self.assets
        }

    async def _run_asset(self, asset: str) -> None:
        symbol = SYMBOLS.get(asset, f"{asset.lower()}/usd")
        filters = json.dumps({"symbol": symbol}, separators=(",", ":"))
        topics = [self.TOPIC]
        if self.twap_topic:
            topics.append(self.TWAP_TOPIC)
        subscription = {
            "action": "subscribe",
            "subscriptions": [
                {
                    "topic": topic,
                    "type": "update",
                    "filters": filters,
                }
                for topic in topics
            ],
        }
        delay = self.reconnect_base_s
        while self._running:
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=None,
                    open_timeout=15,
                    close_timeout=5,
                ) as ws:
                    await ws.send(json.dumps(subscription))
                    self.connected[asset] = True
                    self.last_error[asset] = ""
                    delay = self.reconnect_base_s
                    last_ping = 0.0
                    log.info("rtds.connected", asset=asset, symbol=symbol)
                    while self._running:
                        if time.time() - last_ping >= 5.0:
                            await ws.send("PING")
                            last_ping = time.time()
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            continue
                        self._handle(asset, msg, time.time())
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected[asset] = False
                self.last_error[asset] = repr(exc)
                log.warning(
                    "rtds.reconnect", asset=asset, error=repr(exc), delay_s=delay
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, self.reconnect_max_s)

    def _handle(self, asset: str, msg, recv_ts: float | None = None) -> None:
        try:
            data = json.loads(msg)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        recv_ts = float(recv_ts if recv_ts is not None else time.time())
        if self.on_message is not None:
            try:
                self.on_message(asset, recv_ts, data)
            except Exception as exc:  # noqa: BLE001
                log.warning("rtds.tap_failed", asset=asset, error=repr(exc))
        # The optional TWAP topic shares the connection but must never enter
        # the raw-price buffer: two values per second would collide on
        # timestamp and corrupt the settlement model.
        topic = data.get("topic")
        if topic is not None and str(topic) != self.TOPIC:
            return
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return
        buf = self.buffers[asset]
        if "data" in payload:
            for item in payload.get("data") or []:
                try:
                    buf.add(float(item["timestamp"]) / 1000.0, float(item["value"]))
                except (KeyError, TypeError, ValueError):
                    continue
        elif "timestamp" in payload and "value" in payload:
            try:
                data_ts_ms = float(payload["timestamp"])
                buf.add(data_ts_ms / 1000.0, float(payload["value"]))
                publish_ts_ms = float(data.get("timestamp") or 0.0)
                if self.latency is not None and publish_ts_ms > 0:
                    self.latency.record_rtds(
                        asset, self.TOPIC, data_ts_ms, publish_ts_ms, recv_ts
                    )
            except (KeyError, TypeError, ValueError):
                pass
