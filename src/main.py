"""Entry point: python -m src.main

Starts the market feeds, window manager, strategy engine, paper broker and
the FastAPI dashboard on the same asyncio loop.
"""
from __future__ import annotations

import asyncio
import contextlib
import signal
import time
from dataclasses import dataclass
from typing import Any

import uvicorn

from src.config import RuntimeConfig, get_settings, get_strategy_defaults
from src.execution.live import LiveBroker
from src.execution.maker_shadow import MakerShadow
from src.execution.paper import PaperBroker
from src.execution.router import BrokerRouter
from src.feeds.clob_ws import ClobWsFeed
from src.feeds.rtds import RtdsFeed
from src.infra.latency import LatencyTracker
from src.infra.logging import configure_logging, get_logger
from src.market.gamma import GammaClient
from src.market.window_manager import WindowManager
from src.persistence.db import Database
from src.strategy.engine import StrategyEngine
from src.strategy.risk import KillSwitch, RiskManager
from src.web.app import app as web_app
from src.web.app import set_state

log = get_logger(__name__)


@dataclass
class AppState:
    settings: Any
    runtime: RuntimeConfig
    db: Database
    killswitch: KillSwitch
    risk: RiskManager
    gamma: GammaClient
    windows: WindowManager
    rtds: RtdsFeed
    books: ClobWsFeed
    paper: PaperBroker
    router: BrokerRouter
    engine: StrategyEngine
    latency: LatencyTracker
    started_at: float


class _WebServer(uvicorn.Server):
    """Keep the bot's signal handlers; uvicorn must not replace them."""

    def capture_signals(self):  # type: ignore[override]
        return contextlib.nullcontext()


async def _run_web(state: AppState, host: str, port: int) -> None:
    set_state(state)
    server = _WebServer(
        uvicorn.Config(
            web_app,
            host=host,
            port=port,
            log_level="warning",
            access_log=False,
            lifespan="off",
        )
    )
    await server.serve()


async def _wait_for_stop() -> None:
    stop = asyncio.Event()
    loop = asyncio.get_running_loop()
    for sig in (signal.SIGINT, signal.SIGTERM):
        try:
            loop.add_signal_handler(sig, stop.set)
        except (NotImplementedError, RuntimeError):
            pass
    await stop.wait()


async def main() -> None:
    settings = get_settings()
    configure_logging(settings.log_level, settings.log_dir)

    if not settings.live_enabled and not settings.paper_trading:
        log.warning(
            "safety.force_paper",
            note="PAPER_TRADING=false but LIVE_ENABLED=false; forcing paper",
        )
        settings.paper_trading = True
    if settings.web_password and settings.web_host not in ("127.0.0.1", "localhost"):
        log.info("web.auth_enabled", host=settings.web_host, port=settings.web_port)
    elif not settings.web_password:
        log.warning(
            "web.no_password",
            note="set WEB_PASSWORD before exposing the dashboard",
        )

    db = Database(settings.db_path)
    latency = LatencyTracker(db)
    runtime = RuntimeConfig(db, get_strategy_defaults())
    kill = KillSwitch(db)
    risk = RiskManager(db, runtime, kill)
    gamma = GammaClient(settings.gamma_api)
    windows = WindowManager(
        gamma,
        settings.assets_list,
        window_sec=settings.window_sec,
    )
    rtds = RtdsFeed(
        settings.assets_list,
        settings.rtds_url,
        buffer_s=settings.price_buffer_s,
        reconnect_base_s=settings.reconnect_base_s,
        reconnect_max_s=settings.reconnect_max_s,
        latency=latency,
    )
    books = ClobWsFeed(
        settings.assets_list,
        settings.clob_ws_url,
        reconnect_base_s=settings.reconnect_base_s,
        reconnect_max_s=settings.reconnect_max_s,
    )
    paper = PaperBroker(db, runtime)
    live = LiveBroker(settings) if settings.live_enabled else None
    router = BrokerRouter(settings, paper, live, runtime)
    maker_shadow = MakerShadow(db)
    engine = StrategyEngine(
        settings=settings,
        runtime=runtime,
        db=db,
        killswitch=kill,
        risk=risk,
        windows=windows,
        rtds=rtds,
        books=books,
        paper=paper,
        router=router,
        latency=latency,
        maker_shadow=maker_shadow,
    )
    windows.on_window = engine.on_window

    state = AppState(
        settings=settings,
        runtime=runtime,
        db=db,
        killswitch=kill,
        risk=risk,
        gamma=gamma,
        windows=windows,
        rtds=rtds,
        books=books,
        paper=paper,
        router=router,
        engine=engine,
        latency=latency,
        started_at=time.time(),
    )

    log.info(
        "boot",
        assets=settings.assets_list,
        mode=router.mode,
        asset_modes={
            asset: router.mode_for(asset) for asset in settings.assets_list
        },
        live_available=router.live_available,
        live_lock_reason=router.live_lock_reason,
        live_enabled=settings.live_enabled,
        db=str(settings.db_path),
        web=f"{settings.web_host}:{settings.web_port}",
        params=runtime.params.model_dump(),
    )

    rtds.start()
    books.start()
    engine.start()
    windows.start()

    web_task: asyncio.Task | None = None
    if settings.web_enabled:
        web_task = asyncio.create_task(
            _run_web(state, settings.web_host, settings.web_port), name="web"
        )

        def _web_done(task: asyncio.Task) -> None:
            if task.cancelled():
                return
            exc = task.exception()
            if exc is not None:
                log.error("web.exited", error=repr(exc))

        web_task.add_done_callback(_web_done)

    try:
        await _wait_for_stop()
    finally:
        log.info("shutdown.begin")
        await engine.stop()
        await windows.stop()
        await books.stop()
        await rtds.stop()
        latency.flush(force=True)
        await gamma.close()
        if web_task is not None:
            web_task.cancel()
            try:
                await web_task
            except asyncio.CancelledError:
                pass
        db.close()
        log.info("shutdown.done")


def run() -> None:
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        pass


if __name__ == "__main__":
    run()
