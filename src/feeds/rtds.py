"""Live Chainlink price feed from Polymarket RTDS.

One multiplexed WebSocket subscribes to every configured asset for the raw
Chainlink print and, optionally, the official 60s TWAP. RTDS accepts a list
of subscriptions on a single connection, so seven coins use one socket
instead of seven; messages are routed to the right price buffer by
``payload.symbol``.

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

# RTDS symbol filters. Unknown assets fall back to "<asset>/usd" so adding a
# new coin is a one-line change here (or none, if the convention holds).
SYMBOLS = {
    "BTC": "btc/usd",
    "ETH": "eth/usd",
    "BNB": "bnb/usd",
    "SOL": "sol/usd",
    "XRP": "xrp/usd",
    "DOGE": "doge/usd",
    "HYPE": "hype/usd",
}


def symbol_for(asset: str) -> str:
    key = str(asset).upper()
    return SYMBOLS.get(key, f"{key.lower()}/usd")


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
    # On subscribe the server also sends a 60-point raw-price backfill under
    # the shorter topic. Accept it so a reconnect refills sigma quickly; it is
    # the same Chainlink series as TOPIC, not the TWAP.
    BACKFILL_TOPICS = frozenset({"crypto_prices"})

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
        data_timeout_s: float = 30.0,
        watch_assets: list[str] | None = None,
    ) -> None:
        self.assets = [a.upper() for a in assets]
        self.url = url
        self.latency = latency
        # Optional recorder tap: called with (asset, recv_ts, raw_message)
        # for every parsed RTDS message, before it reaches the price buffer.
        self.on_message = on_message
        self.twap_topic = bool(twap_topic)
        # A half-open RTDS socket can stay "connected" for 20-100 minutes
        # while sending nothing. Watchdog the raw Chainlink stream (TWAP-only
        # traffic is not enough for the strategy) and reconnect on silence.
        self.data_timeout_s = max(5.0, float(data_timeout_s))
        self.last_raw_ts = {a: 0.0 for a in self.assets}
        self.reconnect_base_s = float(reconnect_base_s)
        self.reconnect_max_s = float(reconnect_max_s)
        self.symbols = {a: symbol_for(a) for a in self.assets}
        self._asset_by_symbol = {
            symbol.lower(): asset for asset, symbol in self.symbols.items()
        }
        self.buffers = {a: PriceBuffer(buffer_s) for a in self.assets}
        watch = self.assets if watch_assets is None else list(watch_assets)
        self.watch_assets = [
            str(a).upper() for a in watch if str(a).upper() in self.buffers
        ]
        self.connected = {a: False for a in self.assets}
        self.last_error = {a: "" for a in self.assets}
        self.messages = {a: 0 for a in self.assets}
        self.last_msg_ts = {a: 0.0 for a in self.assets}
        self.reconnects = 0
        self._running = False
        self._task: asyncio.Task | None = None

    def buffer(self, asset: str) -> Optional[PriceBuffer]:
        return self.buffers.get(asset.upper())

    def set_watch_assets(self, assets) -> None:
        """Update the watchdog set when a coin is enabled/disabled at runtime."""
        clean = [str(a).upper() for a in assets]
        self.watch_assets = [a for a in clean if a in self.buffers]

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="rtds")
        log.info(
            "rtds.started",
            assets=self.assets,
            symbols=list(self.symbols.values()),
            url=self.url,
        )

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass
            self._task = None
        for asset in self.assets:
            self.connected[asset] = False

    def health(self) -> dict:
        now = time.time()
        out = {}
        for asset in self.assets:
            last_raw = self.last_raw_ts.get(asset, 0.0)
            watched = asset in self.watch_assets
            out[asset] = {
                "connected": self.connected.get(asset, False),
                "last_error": self.last_error.get(asset, ""),
                "messages": self.messages.get(asset, 0),
                "last_msg_ts": self.last_msg_ts.get(asset, 0.0),
                "last_raw_ts": last_raw,
                "raw_age_s": (now - last_raw) if last_raw and watched else None,
                "data_timeout_s": self.data_timeout_s,
                "watched": watched,
                **self.buffers[asset].health(),
            }
        return out

    def _raw_stale(self, asset: str, now: float | None = None) -> bool:
        last = self.last_raw_ts.get(str(asset).upper(), 0.0)
        if not last:
            # Before the first print a fresh connection is warming up, not
            # stale; the connect-time reset below starts the timer.
            return False
        now = time.time() if now is None else float(now)
        return now - last > self.data_timeout_s

    def stale_assets(self, now: float | None = None) -> list[str]:
        """Watched assets whose raw Chainlink stream has gone silent."""
        return [a for a in self.watch_assets if self._raw_stale(a, now)]

    # ── subscription ─────────────────────────────────────────────────────
    def subscriptions(self) -> list[dict]:
        """All symbol/topic subscriptions sent on the single connection."""
        topics = [self.TOPIC]
        if self.twap_topic:
            topics.append(self.TWAP_TOPIC)
        return [
            {
                "topic": topic,
                "type": "update",
                "filters": json.dumps(
                    {"symbol": self.symbols[asset]}, separators=(",", ":")
                ),
            }
            for asset in self.assets
            for topic in topics
        ]

    async def _run(self) -> None:
        subscription = {
            "action": "subscribe",
            "subscriptions": self.subscriptions(),
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
                    connected_at = time.time()
                    for asset in self.assets:
                        self.connected[asset] = True
                        self.last_error[asset] = ""
                        # Start the watchdog from connect, not from the last
                        # print of the previous socket.
                        self.last_raw_ts[asset] = connected_at
                    delay = self.reconnect_base_s
                    last_ping = 0.0
                    log.info(
                        "rtds.connected",
                        assets=self.assets,
                        subscriptions=len(subscription["subscriptions"]),
                        watch_assets=self.watch_assets,
                        data_timeout_s=self.data_timeout_s,
                    )
                    while self._running:
                        if time.time() - last_ping >= 5.0:
                            await ws.send("PING")
                            last_ping = time.time()
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=2.0)
                        except asyncio.TimeoutError:
                            msg = None
                        if msg is not None:
                            self._handle_any(msg, time.time())
                        stale = self.stale_assets()
                        if stale:
                            raise ConnectionError(
                                "rtds raw data timeout: "
                                + ",".join(stale)
                                + f" (>{self.data_timeout_s:.0f}s)"
                            )
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.reconnects += 1
                for asset in self.assets:
                    self.connected[asset] = False
                    self.last_error[asset] = repr(exc)
                log.warning(
                    "rtds.reconnect",
                    error=repr(exc),
                    delay_s=delay,
                    assets=self.assets,
                )
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, self.reconnect_max_s)
            finally:
                for asset in self.assets:
                    self.connected[asset] = False

    # ── message handling ─────────────────────────────────────────────────
    def _handle_any(self, msg, recv_ts: float | None = None) -> None:
        """Route one multiplexed RTDS message by its payload symbol."""
        try:
            data = json.loads(msg)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        payload = data.get("payload")
        symbol = payload.get("symbol") if isinstance(payload, dict) else None
        asset = self._asset_by_symbol.get(str(symbol).lower()) if symbol else None
        if asset is None:
            # A single-asset feed cannot be ambiguous; keep old behavior for
            # custom deployments and tests that replay messages without one.
            if len(self.assets) != 1:
                return
            asset = self.assets[0]
        self._process(asset, data, recv_ts)

    def _handle(self, asset: str, msg, recv_ts: float | None = None) -> None:
        """Handle a message already attributed to ``asset`` (test/replay API)."""
        try:
            data = json.loads(msg)
        except (TypeError, ValueError):
            return
        if not isinstance(data, dict):
            return
        self._process(str(asset).upper(), data, recv_ts)

    def _process(
        self, asset: str, data: dict, recv_ts: float | None = None
    ) -> None:
        if asset not in self.buffers:
            return
        recv_ts = float(recv_ts if recv_ts is not None else time.time())
        self.messages[asset] = self.messages.get(asset, 0) + 1
        self.last_msg_ts[asset] = recv_ts
        if self.on_message is not None:
            try:
                self.on_message(asset, recv_ts, data)
            except Exception as exc:  # noqa: BLE001
                log.warning("rtds.tap_failed", asset=asset, error=repr(exc))
        # The optional TWAP topic shares the connection but must never enter
        # the raw-price buffer: two values per second would collide on
        # timestamp and corrupt the settlement model. The subscribe backfill
        # (``crypto_prices``) is the same raw Chainlink series, so accept it.
        topic = data.get("topic")
        if topic is not None:
            topic = str(topic)
            # TWAP shares the socket but must never enter the raw-price
            # buffer; only TOPIC and the subscribe backfill are raw prints.
            if topic != self.TOPIC and topic not in self.BACKFILL_TOPICS:
                return
        payload = data.get("payload")
        if not isinstance(payload, dict):
            return
        buf = self.buffers[asset]
        added = False
        if "data" in payload:
            for item in payload.get("data") or []:
                try:
                    buf.add(float(item["timestamp"]) / 1000.0, float(item["value"]))
                    added = True
                except (KeyError, TypeError, ValueError):
                    continue
        elif "timestamp" in payload and "value" in payload:
            try:
                data_ts_ms = float(payload["timestamp"])
                buf.add(data_ts_ms / 1000.0, float(payload["value"]))
                added = True
                publish_ts_ms = float(data.get("timestamp") or 0.0)
                if self.latency is not None and publish_ts_ms > 0:
                    self.latency.record_rtds(
                        asset, self.TOPIC, data_ts_ms, publish_ts_ms, recv_ts
                    )
            except (KeyError, TypeError, ValueError):
                pass
        if added:
            self.last_raw_ts[asset] = recv_ts
