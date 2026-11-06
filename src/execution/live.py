"""Live CLOB execution scaffold.

Paper mode does not import this module's dependencies. Live order placement
is intentionally guarded: it requires LIVE_ENABLED=true, PAPER_TRADING=false,
a per-asset ``trading_mode="live"``, and the optional `py-clob-client-v2`
package. The exact order call must be validated with a tiny real order before
it is trusted; see docs/LIVE_READINESS.md for the remaining gaps.
"""
from __future__ import annotations

from typing import Any, Optional

from src.config import AppSettings
from src.infra.logging import get_logger
from src.market.models import PaperPosition, WindowMarket

log = get_logger(__name__)


class LiveBroker:
    def __init__(self, settings: AppSettings) -> None:
        self.settings = settings
        self._client: Any = None

    @property
    def enabled(self) -> bool:
        return bool(self.settings.live_enabled and not self.settings.paper_trading)

    @property
    def configured(self) -> bool:
        """Whether the minimum signing credential is present.

        The config page uses this only to show the live lock early; the real
        validation happens in :meth:`_ensure_client` when the first order is
        placed.
        """
        return bool(self.settings.wallet_private_key)

    @property
    def order_placement_ready(self) -> bool:
        """False until :meth:`buy` actually posts to the CLOB.

        Keeping this explicit lets the router show "configured live, running
        paper" instead of discovering the scaffold only when the first order
        raises. Flip it together with the real order implementation.
        """
        return False

    def _ensure_client(self) -> Any:
        if not self.enabled:
            raise RuntimeError(
                "live trading is disabled (set LIVE_ENABLED=true and PAPER_TRADING=false)"
            )
        if not self.settings.wallet_private_key:
            raise RuntimeError("WALLET_PRIVATE_KEY is required for live trading")
        if self._client is not None:
            return self._client
        try:
            from py_clob_client.client import ClobClient  # type: ignore
        except ImportError as exc:  # pragma: no cover - optional dependency
            raise RuntimeError(
                "py-clob-client-v2 is not installed; install the [live] extra first"
            ) from exc
        self._client = ClobClient(
            host=self.settings.clob_api,
            key=self.settings.wallet_private_key,
            chain_id=137,
            signature_type=int(self.settings.signature_type),
            funder=self.settings.funder_address or self.settings.wallet_address or None,
        )
        if self.settings.api_key:
            # The client also accepts pre-derived L2 credentials; the exact
            # ApiCreds import path differs across client versions, so this
            # remains deliberately explicit.
            log.warning(
                "live.api_creds_present",
                note="provide pre-derived creds via the client's set_api_creds()",
            )
        return self._client

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
        self._ensure_client()
        # The paper and live brokers share the same call signature. Wiring a
        # market order here is a one-function change; it is not enabled in
        # this phase because a mis-sized live order is an expensive bug.
        raise NotImplementedError(
            "live CLOB order placement is scaffolded but not enabled in the paper phase"
        )
