"""Polymarket CLOB market WebSocket: order books and last trades.

The feed subscribes to the union of the current 5m window tokens for every
asset. A token-set change (new window) bumps a version and forces a clean
reconnect so no stale subscription lingers.
"""
from __future__ import annotations

import asyncio
import json
import time
from collections import deque
from typing import Any, Optional

import websockets

from src.infra.logging import get_logger
from src.market.models import OrderBook

log = get_logger(__name__)


class ClobWsFeed:
    def __init__(
        self,
        assets: list[str],
        url: str,
        reconnect_base_s: float = 1.0,
        reconnect_max_s: float = 30.0,
        on_trade=None,
        on_message=None,
    ) -> None:
        self.assets = [a.upper() for a in assets]
        self.url = url
        # Optional recorder taps. on_trade receives the normalized event that
        # is also pushed to trade_events; on_message receives every parsed
        # CLOB message for optional raw capture.
        self.on_trade = on_trade
        self.on_message = on_message
        self.reconnect_base_s = float(reconnect_base_s)
        self.reconnect_max_s = float(reconnect_max_s)
        self.books: dict[str, OrderBook] = {}
        self.last_trade: dict[str, tuple[float, float, float]] = {}
        # Every last_trade_price event since the last drain. The maker shadow
        # needs the full print stream (side/price/size) to simulate queue fills;
        # `last_trade` alone is not enough.
        self.trade_events: deque[dict[str, Any]] = deque(maxlen=50000)
        self._tokens: dict[str, list[str]] = {a: [] for a in self.assets}
        self._version = 0
        self._running = False
        self._task: asyncio.Task | None = None
        self.connected = False
        self.last_error = ""
        self.last_msg_ts = 0.0

    def set_tokens(self, asset: str, tokens: list[str]) -> None:
        asset = asset.upper()
        clean = [str(t) for t in tokens if t]
        if self._tokens.get(asset) == clean:
            return
        self._tokens[asset] = clean
        for token in clean:
            self.books.setdefault(token, OrderBook(token))
        self._version += 1
        log.info("clob.tokens", asset=asset, tokens=[t[:10] for t in clean])

    def book(self, token_id: str) -> Optional[OrderBook]:
        return self.books.get(str(token_id))

    def drain_trades(self) -> list[dict[str, Any]]:
        """Return and clear the trade prints received since the last call."""
        if not self.trade_events:
            return []
        events = list(self.trade_events)
        self.trade_events.clear()
        return events

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="clob-ws")
        log.info("clob.started", url=self.url)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def health(self) -> dict:
        return {
            "connected": self.connected,
            "last_error": self.last_error,
            "last_msg_ts": self.last_msg_ts,
            "tokens": {a: len(t) for a, t in self._tokens.items()},
        }

    async def _run(self) -> None:
        delay = self.reconnect_base_s
        while self._running:
            version = self._version
            tokens = sorted({t for ts in self._tokens.values() for t in ts})
            if not tokens:
                await asyncio.sleep(0.5)
                continue
            try:
                async with websockets.connect(
                    self.url,
                    ping_interval=None,
                    open_timeout=15,
                    close_timeout=5,
                ) as ws:
                    await ws.send(json.dumps({"assets_ids": tokens, "type": "market"}))
                    self.connected = True
                    self.last_error = ""
                    delay = self.reconnect_base_s
                    log.info("clob.connected", tokens=len(tokens))
                    while self._running and self._version == version:
                        try:
                            msg = await asyncio.wait_for(ws.recv(), timeout=10.0)
                        except asyncio.TimeoutError:
                            continue
                        self._handle(msg)
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                self.connected = False
                self.last_error = repr(exc)
                log.warning("clob.reconnect", error=repr(exc), delay_s=delay)
                await asyncio.sleep(delay)
                delay = min(delay * 2.0, self.reconnect_max_s)

    def _handle(self, msg) -> None:
        try:
            data = json.loads(msg)
        except (TypeError, ValueError):
            return
        self.last_msg_ts = time.time()
        if self.on_message is not None:
            try:
                self.on_message(self.last_msg_ts, data)
            except Exception as exc:  # noqa: BLE001
                log.warning("clob.tap_failed", error=repr(exc))
        items = data if isinstance(data, list) else [data]
        for item in items:
            if not isinstance(item, dict):
                continue
            event_type = item.get("event_type")
            if event_type == "last_trade_price" or (
                event_type is None
                and "price" in item
                and "size" in item
                and "asset_id" in item
            ):
                self._on_trade(item)
                continue
            if item.get("asset_id") and "bids" in item and "asks" in item:
                self._on_snapshot(item)
                continue
            for change in item.get("price_changes") or []:
                self._on_change(change)

    def _on_snapshot(self, item: dict) -> None:
        token = str(item.get("asset_id"))
        if not token:
            return
        book = self.books.setdefault(token, OrderBook(token))
        book.apply_snapshot(item.get("bids"), item.get("asks"), time.time())

    def _on_change(self, change: dict) -> None:
        token = str(change.get("asset_id") or "")
        if not token:
            return
        book = self.books.setdefault(token, OrderBook(token))
        try:
            price = float(change.get("price"))
            size = float(change.get("size") or 0.0)
        except (TypeError, ValueError):
            return
        book.apply_price_change(price, size, str(change.get("side") or ""), time.time())

    def _on_trade(self, item: dict) -> None:
        token = str(item.get("asset_id") or "")
        if not token:
            return
        try:
            price = float(item.get("price"))
            size = float(item.get("size") or 0.0)
        except (TypeError, ValueError):
            return
        now = time.time()
        self.last_trade[token] = (price, size, now)
        exchange_ts: Optional[float] = None
        try:
            raw_ts = item.get("timestamp")
            if raw_ts is not None:
                exchange_ts = float(raw_ts) / 1000.0
        except (TypeError, ValueError):
            exchange_ts = None
        event = {
            "token_id": token,
            "side": str(item.get("side") or "").upper(),
            "price": price,
            "size": size,
            "ts": now,
            "exchange_ts": exchange_ts,
        }
        self.trade_events.append(event)
        if self.on_trade is not None:
            try:
                self.on_trade(event)
            except Exception as exc:  # noqa: BLE001
                log.warning("clob.trade_tap_failed", error=repr(exc))
