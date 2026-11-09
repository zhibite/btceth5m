"""5-minute window lifecycle.

The manager polls Gamma once per second:
  * at each 300s boundary it discovers the new window for every asset
  * it backfills a late priceToBeat for a short grace period after the open
  * it refreshes finalPrice only after the window has ended (it cannot be
    populated before then, so polling for it during the window is wasted)
  * it invokes the engine callback exactly once per newly discovered window
"""
from __future__ import annotations

import asyncio
import time
from typing import Awaitable, Callable, Optional

from src.infra.logging import get_logger
from src.market.gamma import GammaClient
from src.market.models import WindowMarket

log = get_logger(__name__)

WindowCallback = Callable[[str, WindowMarket], Awaitable[None]]


class WindowManager:
    refresh_interval_s = 2.0
    # Gamma sometimes publishes priceToBeat late. Poll it every 2 s for the
    # first minute, then back off to 15 s until the window closes; a 60 s
    # cutoff permanently lost p0 whenever RTDS was in one of its long stalls.
    p0_fast_grace_s = 60.0
    p0_late_grace_s = 300.0
    p0_late_interval_s = 15.0
    # The previous window's finalPrice is an independent fallback; throttle
    # it so a missing p0 does not double the Gamma request rate.
    p0_prev_interval_s = 15.0

    def __init__(
        self,
        gamma: GammaClient,
        assets: list[str],
        window_sec: int = 300,
        on_window: WindowCallback | None = None,
        max_concurrent_fetches: int = 4,
    ) -> None:
        self.gamma = gamma
        self.assets = [a.upper() for a in assets]
        self.window_sec = int(window_sec)
        self.on_window = on_window
        self.current: dict[str, WindowMarket] = {}
        self._running = False
        self._task: asyncio.Task | None = None
        self._last_refresh: dict[str, float] = {}
        self._last_prev_refresh: dict[str, float] = {}
        self._window_seen: dict[str, float] = {}
        # Seven coins polling at a boundary must not all hit Gamma at once,
        # but a sequential loop would let one slow request stall the rest.
        self._fetch_sem = asyncio.Semaphore(max(1, int(max_concurrent_fetches)))

    def start(self) -> None:
        self._running = True
        self._task = asyncio.create_task(self._run(), name="window-manager")
        log.info("window_manager.started", assets=self.assets, window_s=self.window_sec)

    async def stop(self) -> None:
        self._running = False
        if self._task is not None:
            self._task.cancel()
            try:
                await self._task
            except asyncio.CancelledError:
                pass

    def boundary(self, now: float | None = None) -> int:
        ts = now if now is not None else time.time()
        return int(ts // self.window_sec) * self.window_sec

    def current_window(self, asset: str) -> Optional[WindowMarket]:
        return self.current.get(asset.upper())

    async def fetch_window(self, asset: str, start_ts: int) -> Optional[WindowMarket]:
        async with self._fetch_sem:
            return await self.gamma.get_window(asset, start_ts)

    def needs_refresh(
        self,
        market: WindowMarket,
        now: float,
        seen_ts: float | None = None,
    ) -> bool:
        """Whether another Gamma fetch can still add window metadata.

        ``finalPrice`` stays null until the window closes, so including it in
        the condition unconditionally would poll Gamma every couple of
        seconds for the entire five-minute window while nothing can change.

        The p0 grace period runs from ``seen_ts`` (when this process found
        the window) rather than ``start_ts``: after a mid-window restart a
        late priceToBeat is still worth fetching, even though the window
        itself started minutes ago.
        """
        if market.final_price is None and now >= market.end_ts:
            return True
        since = market.start_ts if seen_ts is None else seen_ts
        return (
            market.price_to_beat is None
            and now - since <= self.p0_late_grace_s
        )

    def refresh_interval(
        self,
        market: WindowMarket,
        now: float,
        seen_ts: float | None = None,
    ) -> float:
        """Poll fast early, then back off while p0 is still missing."""
        since = market.start_ts if seen_ts is None else seen_ts
        if market.price_to_beat is None and now - since > self.p0_fast_grace_s:
            return self.p0_late_interval_s
        return self.refresh_interval_s

    async def _run(self) -> None:
        while self._running:
            now = time.time()
            start = self.boundary(now)
            await asyncio.gather(
                *(self._refresh_asset(asset, start, now) for asset in self.assets),
                return_exceptions=True,
            )
            await asyncio.sleep(1.0)

    async def _refresh_asset(self, asset: str, start: int, now: float) -> None:
        """Discover or refresh one asset without blocking the others."""
        try:
            cur = self.current.get(asset)
            if cur is None or cur.start_ts != start:
                market = await self.fetch_window(asset, start)
                if market is None:
                    # Gamma can lag the boundary by a second or two; retry on
                    # the next tick without blocking the other assets.
                    return
                self.current[asset] = market
                self._window_seen[asset] = now
                log.info(
                    "window.discovered",
                    asset=asset,
                    slug=market.slug,
                    p0=market.price_to_beat,
                    volume=market.volume,
                )
                if self.on_window is not None:
                    try:
                        await self.on_window(asset, market)
                    except Exception as exc:  # noqa: BLE001
                        log.exception(
                            "window.callback_failed", asset=asset, error=str(exc)
                        )
                return

            # Late metadata: priceToBeat may be null for a few seconds after
            # the boundary, and finalPrice only appears once the window ended.
            seen = self._window_seen.get(asset)
            if self.needs_refresh(cur, now, seen) and (
                now - self._last_refresh.get(asset, 0.0)
                >= self.refresh_interval(cur, now, seen)
            ):
                self._last_refresh[asset] = now
                fresh = await self.fetch_window(asset, start)
                if fresh is not None:
                    if cur.price_to_beat is None and fresh.price_to_beat is not None:
                        cur.price_to_beat = fresh.price_to_beat
                        cur.p0_source = "gamma"
                        log.info(
                            "window.p0_late",
                            asset=asset,
                            slug=cur.slug,
                            p0=cur.price_to_beat,
                        )
                    if fresh.final_price is not None:
                        cur.final_price = fresh.final_price
                        cur.outcome = fresh.outcome
                        cur.closed = fresh.closed
                    cur.volume = fresh.volume or cur.volume
                if (
                    cur.price_to_beat is None
                    and (fresh is None or fresh.price_to_beat is None)
                    and now - self._last_prev_refresh.get(asset, 0.0)
                    >= self.p0_prev_interval_s
                ):
                    self._last_prev_refresh[asset] = now
                    await self._p0_from_previous_final(asset, cur, start)
        except asyncio.CancelledError:
            raise
        except Exception as exc:  # noqa: BLE001
            log.warning("window.refresh_failed", asset=asset, error=repr(exc))

    async def _p0_from_previous_final(
        self, asset: str, market: WindowMarket, start_ts: int
    ) -> None:
        """Use ``finalPrice(N-1) == priceToBeat(N)`` as a p0 fallback.

        Gamma usually has the previous window's settlement even when the
        current window's p0 is published late (or RTDS is stalled).
        """
        if start_ts <= 0:
            return
        prev = await self.fetch_window(asset, start_ts - self.window_sec)
        if prev is None or prev.final_price is None:
            return
        market.price_to_beat = float(prev.final_price)
        market.p0_source = "prev_final"
        log.info(
            "window.p0_prev_final",
            asset=asset,
            slug=market.slug,
            p0=market.price_to_beat,
        )
