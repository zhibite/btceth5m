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
from src.recording.recorder import Recorder
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
    recorder: Any
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
    books = ClobWsFeed(
        settings.assets_list,
        settings.clob_ws_url,
        reconnect_base_s=settings.reconnect_base_s,
        reconnect_max_s=settings.reconnect_max_s,
    )
    recorder = None
    if settings.recording_enabled:
        # Recording is auxiliary: a bad path or a full disk must not take the
        # trading loop down with it.
        try:
            recorder = Recorder(
                settings.recording_path,
                settings.assets_list,
                books=books,
                windows=windows,
                book_hz=settings.recording_book_hz,
                book_levels=settings.recording_book_levels,
                raw_clob=settings.recording_raw_clob,
                compress=settings.recording_gzip,
                retention_days=settings.recording_retention_days,
                queue_size=settings.recording_queue_size,
            )
        except Exception as exc:  # noqa: BLE001
            log.error(
                "recorder.init_failed",
                path=str(settings.recording_path),
                error=repr(exc),
            )
            recorder = None
        else:
            books.on_trade = recorder.record_trade
            if settings.recording_raw_clob:
                books.on_message = recorder.record_raw_clob
    enabled_assets = runtime.enabled_assets(settings.assets_list)
    rtds = RtdsFeed(
        settings.assets_list,
        settings.rtds_url,
        buffer_s=settings.price_buffer_s,
        reconnect_base_s=settings.reconnect_base_s,
        reconnect_max_s=settings.reconnect_max_s,
        latency=latency,
        on_message=recorder.record_rtds if recorder is not None else None,
        twap_topic=bool(recorder is not None and settings.recording_twap),
        data_timeout_s=settings.rtds_data_timeout_s,
        # Disabled coins still get RTDS prices for display/recording, but a
        # quiet disabled symbol must not force the shared socket to reconnect.
        watch_assets=enabled_assets,
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
    async def _on_window(asset: str, market: Any) -> None:
        # Record discovery before the engine touches the market object, then
        # let the strategy react. The recorder also samples late metadata.
        if recorder is not None:
            recorder.record_window(market)
        await engine.on_window(asset, market)

    windows.on_window = _on_window

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
        recorder=recorder,
        started_at=time.time(),
    )

    global_loss_limit = runtime.params.daily_loss_limit_usd
    higher_asset_limits = {}
    for asset in settings.assets_list:
        asset_limit = runtime.params_for(asset).daily_loss_limit_usd
        if asset_limit > global_loss_limit:
            higher_asset_limits[asset] = asset_limit
    if higher_asset_limits:
        log.warning(
            "risk.daily_loss_limit_mismatch",
            global_daily_loss_usd=global_loss_limit,
            asset_daily_loss_usd=higher_asset_limits,
            note="the portfolio guard uses the global value, not the per-asset one",
        )

    log.info(
        "boot",
        assets=settings.assets_list,
        enabled_assets=enabled_assets,
        mode=router.mode,
        asset_modes={
            asset: router.mode_for(asset) for asset in settings.assets_list
        },
        live_available=router.live_available,
        live_lock_reason=router.live_lock_reason,
        live_enabled=settings.live_enabled,
        db=str(settings.db_path),
        recording=str(settings.recording_path) if recorder is not None else None,
        daily_loss_limit_global=runtime.params.daily_loss_limit_usd,
        daily_loss_limit_by_asset={
            asset: runtime.params_for(asset).daily_loss_limit_usd
            for asset in settings.assets_list
        },
        web=f"{settings.web_host}:{settings.web_port}",
        params=runtime.params.model_dump(),
    )

    if recorder is not None:
        recorder.start()
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
        if recorder is not None:
            await recorder.stop()
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
