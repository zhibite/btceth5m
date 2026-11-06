"""Mode router: per-asset paper/live selection behind a global live gate.

Live execution is allowed only when the process was started with
``LIVE_ENABLED=true`` and ``PAPER_TRADING=false``. Under that gate, each asset
uses its own ``trading_mode`` (saved from the config page). Otherwise every
asset is forced to paper, and :attr:`live_lock_reason` explains why so the
dashboard can show the lock instead of silently pretending to be live.
"""
from __future__ import annotations

from typing import Optional

from src.config import AppSettings, RuntimeConfig
from src.execution.live import LiveBroker
from src.execution.paper import PaperBroker
from src.infra.logging import get_logger
from src.market.models import PaperPosition, WindowMarket

log = get_logger(__name__)


class BrokerRouter:
    def __init__(
        self,
        settings: AppSettings,
        paper: PaperBroker,
        live: Optional[LiveBroker] = None,
        runtime: Optional[RuntimeConfig] = None,
    ) -> None:
        self.settings = settings
        self.paper = paper
        self.live = live
        self.runtime = runtime

    # ── mode resolution ──────────────────────────────────────────────────
    @property
    def live_available(self) -> bool:
        """Whether a live order could actually be submitted right now."""
        if not self.settings.live_enabled or self.settings.paper_trading:
            return False
        if self.live is None:
            return False
        if not getattr(self.live, "configured", True):
            return False
        return bool(getattr(self.live, "order_placement_ready", True))

    @property
    def live_lock_reason(self) -> str:
        """Empty when live is available; otherwise a stable reason code."""
        if not self.settings.live_enabled:
            return "live_disabled"
        if self.settings.paper_trading:
            return "paper_trading"
        if self.live is None:
            return "live_client_missing"
        if not getattr(self.live, "configured", True):
            return "credentials_missing"
        if not getattr(self.live, "order_placement_ready", True):
            return "live_not_implemented"
        return ""

    def configured_mode(self, asset: str | None) -> str:
        """The mode saved for ``asset``, ignoring the global live gate."""
        if self.runtime is None:
            return "paper" if self.settings.paper_trading else "live"
        return self.runtime.params_for(asset).trading_mode

    def mode_for(self, asset: str | None) -> str:
        """The mode an order for ``asset`` would actually use."""
        if not self.live_available:
            return "paper"
        return self.configured_mode(asset)

    @property
    def mode(self) -> str:
        """Overall mode used by the legacy ``/api/status`` field."""
        if any(self.mode_for(asset) == "live" for asset in self.settings.assets_list):
            return "live"
        return "paper"

    # ── execution ────────────────────────────────────────────────────────
    async def buy(
        self,
        *,
        market: WindowMarket,
        side: str,
        token_id: str,
        notional: float,
        asks: list[tuple[float, float]],
        price_to_beat: float,
        reason: str = "",
    ) -> Optional[PaperPosition]:
        if self.mode_for(market.asset) == "live":
            if self.live is None or not self.live_available:
                raise RuntimeError(
                    "live trading was requested but the live broker is not ready"
                )
            return await self.live.buy(
                market=market,
                side=side,
                token_id=token_id,
                notional=notional,
                asks=asks,
                price_to_beat=price_to_beat,
                reason=reason,
            )
        return self.paper.buy(
            market=market,
            side=side,
            token_id=token_id,
            notional=notional,
            asks=asks,
            price_to_beat=price_to_beat,
            reason=reason,
        )
