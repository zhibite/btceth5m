"""5m TWAP fair-value strategy engine.

Per asset and per window:
  1. Build the fair Up probability from the live Chainlink prints:
       P(up) = Phi(log(E[close TWAP] / priceToBeat) / sd)
  2. Walk the live CLOB book for the configured paper notional.
  3. Enter only when:
       fair - effective_ask - fee >= MIN_EDGE + MODEL_BUFFER
     and the book/window/risk filters pass.
  4. Hold the position to settlement; settle on the 60s TWAP at the boundary.
"""
from __future__ import annotations

import asyncio
import time
from typing import Any, Optional

from src.config import AppSettings, RuntimeConfig
from src.execution.maker_shadow import MakerShadow
from src.execution.paper import PaperBroker
from src.execution.router import BrokerRouter
from src.feeds.clob_ws import ClobWsFeed
from src.feeds.rtds import RtdsFeed
from src.infra.logging import get_logger
from src.market.models import PaperPosition, SignalSnapshot, WindowMarket
from src.market.window_manager import WindowManager
from src.persistence.db import Database
from src.strategy.fair_value import fair_probability
from src.strategy.risk import KillSwitch, RiskManager

log = get_logger(__name__)

_TIE_REL = 1e-5  # 0.1 bps: wait for the official Gamma finalPrice
_NEAR_MISS_LOG_S = 10.0


class StrategyEngine:
    def __init__(
        self,
        *,
        settings: AppSettings,
        runtime: RuntimeConfig,
        db: Database,
        killswitch: KillSwitch,
        risk: RiskManager,
        windows: WindowManager,
        rtds: RtdsFeed,
        books: ClobWsFeed,
        paper: PaperBroker,
        router: BrokerRouter,
        latency=None,
        maker_shadow: Optional[MakerShadow] = None,
    ) -> None:
        self.settings = settings
        self.latency = latency
        self.maker_shadow = maker_shadow
        self.runtime = runtime
        self.db = db
        self.kill = killswitch
        self.risk = risk
        self.windows = windows
        self.rtds = rtds
        self.books = books
        self.paper = paper
        self.router = router

        self.positions: dict[tuple[str, int], PaperPosition] = {}
        self.latest: dict[str, dict[str, Any]] = {a: {} for a in settings.assets_list}
        self.started_at = time.time()
        self._running = False
        self._tasks: list[asyncio.Task] = []
        self._last_near_miss: dict[str, float] = {}
        self._settle_first_seen: dict[tuple[str, int], float] = {}

    # ── lifecycle ────────────────────────────────────────────────────────
    def start(self) -> None:
        self._load_open_positions()
        if self.maker_shadow is not None:
            self.maker_shadow.load_active()
        self._running = True
        self._tasks = [
            asyncio.create_task(self._tick_loop(), name="engine-ticks"),
            asyncio.create_task(self._settle_loop(), name="engine-settle"),
        ]
        log.info(
            "engine.started",
            assets=self.settings.assets_list,
            mode=self.router.mode,
            asset_modes={
                asset: self.router.mode_for(asset)
                for asset in self.settings.assets_list
            },
            open_positions=len(self.positions),
        )

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

    def _load_open_positions(self) -> None:
        for row in self.db.open_positions():
            pos = PaperPosition(
                id=int(row["id"]),
                asset=str(row["asset"]),
                start_ts=int(row["start_ts"]),
                end_ts=int(row["end_ts"]),
                token_id=str(row["token_id"]),
                side=str(row["side"]),
                size=float(row["size"]),
                avg_price=float(row["avg_price"]),
                cost=float(row["cost"]),
                fees=float(row["fees"]),
                entry_ts=float(row["entry_ts"]),
                price_to_beat=float(row["price_to_beat"]),
            )
            self.positions[(pos.asset, pos.start_ts)] = pos

    # ── window discovery callback ────────────────────────────────────────
    async def on_window(self, asset: str, market: WindowMarket) -> None:
        self.books.set_tokens(asset, [market.up_token, market.down_token])
        self.db.upsert_window(market)
        self.latest.setdefault(asset, {})
        self.latest[asset].update(
            {
                "asset": asset,
                "ts": time.time(),
                "window": market.to_dict(),
                "position": self._position_info(asset, market.start_ts),
                "status": "window_discovered",
            }
        )
        log.info(
            "engine.window",
            asset=asset,
            slug=market.slug,
            p0=market.price_to_beat,
            up=market.up_token[:10],
            down=market.down_token[:10],
        )

    # ── loops ────────────────────────────────────────────────────────────
    async def _tick_loop(self) -> None:
        while self._running:
            try:
                await self.tick()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("engine.tick_failed", error=str(exc))
            await asyncio.sleep(self._tick_interval())

    def _tick_interval(self) -> float:
        """One loop drives every asset; ``engine_tick_s`` is a shared setting."""
        return max(0.1, float(self.runtime.params.engine_tick_s))

    async def _settle_loop(self) -> None:
        while self._running:
            try:
                await self.settle_due()
            except asyncio.CancelledError:
                raise
            except Exception as exc:  # noqa: BLE001
                log.exception("engine.settle_failed", error=str(exc))
            await asyncio.sleep(2.0)

    # ── main tick ────────────────────────────────────────────────────────
    async def tick(self) -> None:
        now = time.time()
        if self.maker_shadow is not None:
            trades = self.books.drain_trades()
            if trades:
                self.maker_shadow.on_trades(trades)
        fair_by_asset: dict[str, Optional[float]] = {}
        for asset in self.settings.assets_list:
            params = self.runtime.params_for(asset)
            market = self.windows.current_window(asset)
            if market is None:
                self._publish(asset, {"status": "no_window"})
                continue

            buf = self.rtds.buffer(asset)
            if buf is None or buf.latest_price() is None:
                self._publish(asset, self._base_payload(market, now, status="warming_up"))
                continue
            if now - buf.last_recv_ts > 5.0:
                self._publish(
                    asset,
                    self._base_payload(market, now, status="stale_feed"),
                )
                continue

            p0 = market.price_to_beat
            if p0 is None:
                p0 = buf.twap_ending_at(market.start_ts, market.twap_lookback or 60)
                if p0 is not None:
                    market.price_to_beat = p0
                    market.p0_source = "rtds"
                    self.db.set_window_p0(asset, market.start_ts, p0, "rtds")
            if p0 is None:
                self._publish(
                    asset,
                    self._base_payload(market, now, status="waiting_p0", spot=buf.latest_price()),
                )
                continue

            spot = buf.latest_price()
            sigma = buf.sigma(params.sigma_lookback_s, params.min_sigma_samples)
            if sigma is None:
                self._publish(
                    asset,
                    self._base_payload(
                        market,
                        now,
                        status=(
                            "warming_up_sigma "
                            f"({buf.points()}/{params.min_sigma_samples})"
                        ),
                        p0=p0,
                        spot=spot,
                    ),
                )
                continue

            fv = fair_probability(
                p0=p0,
                spot=spot,
                sigma=sigma,
                now=now,
                end_ts=market.end_ts,
                twap_window=market.twap_lookback or 60,
                mean_between=buf.mean_between,
            )
            if fv is None:
                self._publish(
                    asset,
                    self._base_payload(
                        market, now, status="fair_unavailable", p0=p0, spot=spot,
                        sigma=sigma,
                    ),
                )
                continue

            fair_up = float(fv["fair"])
            fair_by_asset[asset] = fair_up
            candidates = self._candidates(market, fair_up, now, params)
            eligible = [c for c in candidates if c.get("eligible", True)]
            best = max(eligible, key=lambda c: c["edge"]) if eligible else None
            position = self._position_info(asset, market.start_ts)

            payload = self._base_payload(
                market,
                now,
                p0=p0,
                spot=spot,
                sigma=sigma,
                twap60=buf.twap_ending_at(now, market.twap_lookback or 60),
                fair_up=fair_up,
                fair_detail=fv,
                candidates=candidates,
                best=best,
                position=position,
            )

            status = self._entry_check(market, now, params, best, position, payload)
            if status != "candidate" or best is None:
                payload["status"] = status
                self._publish(asset, payload)
                if status != "disabled":
                    # When both sides are gated, still log the best one so the
                    # near-miss table shows what min_fair/min_entry_price
                    # filtered. A disabled asset intentionally logs nothing.
                    watch = best or max(
                        candidates, key=lambda c: c["edge"], default=None
                    )
                    self._maybe_log_near_miss(asset, market, watch, params, status)
                continue

            # ── execute the winning candidate ────────────────────────────
            side = str(best["side"])
            token = market.token_for(side)
            book = self.books.book(token)
            asks = book.asks_ascending() if book is not None else []
            decision_ts = time.time()
            data_age_ms = (
                (decision_ts - buf.last_recv_ts) * 1000.0 if buf.last_recv_ts else None
            )
            book_age_ms = (
                (decision_ts - book.updated_ts) * 1000.0
                if book is not None and book.updated_ts
                else None
            )
            # Snapshot the touch before the order call so the shadow quote is
            # the book we actually decided on.
            shadow_captured = None
            if self.maker_shadow is not None and params.maker_shadow_enabled:
                shadow_captured = self.maker_shadow.capture_quote(book)
            try:
                pos = await self.router.buy(
                    market=market,
                    side=side,
                    token_id=token,
                    notional=params.max_position_usd,
                    asks=asks,
                    price_to_beat=p0,
                    reason=f"fair={best['fair']:.3f} edge={best['edge']:.3f}",
                )
            except Exception as exc:  # noqa: BLE001
                log.exception("engine.execution_failed", asset=asset, error=str(exc))
                payload["status"] = f"execution_error: {exc}"
                self._publish(asset, payload)
                continue

            fill_ts = time.time()
            if self.latency is not None:
                self.latency.record_decision(
                    ts=decision_ts,
                    asset=asset,
                    window_start=market.start_ts,
                    data_age_ms=data_age_ms,
                    book_age_ms=book_age_ms,
                    fill_ms=(fill_ts - decision_ts) * 1000.0,
                    note=(
                        f"{side} edge={best['edge']:.3f} "
                        f"{'entered' if pos else 'fill_rejected'}"
                    ),
                )

            if pos is None:
                payload["status"] = "fill_rejected"
                self._log_signal(
                    asset,
                    market,
                    side,
                    best,
                    f"fill_rejected edge={best['edge']:.3f}",
                    acted=False,
                )
                self._publish(asset, payload)
                continue

            self.positions[(asset, market.start_ts)] = pos
            if self.maker_shadow is not None:
                self.maker_shadow.open_attempt(
                    market=market,
                    side=side,
                    token_id=token,
                    taker_pos=pos,
                    fair=float(best["fair"]),
                    now=time.time(),
                    params=params,
                    captured=shadow_captured,
                )
            self._log_signal(
                asset,
                market,
                side,
                best,
                f"entered fair={best['fair']:.3f} edge={best['edge']:.3f}",
                acted=True,
            )
            payload["status"] = f"entered {side}"
            payload["position"] = pos.to_dict()
            self._publish(asset, payload)
            log.info(
                "engine.entered",
                asset=asset,
                window=market.start_ts,
                side=side,
                fair=round(float(best["fair"]), 3),
                ask=round(float(best["avg_price"]), 3),
                edge=round(float(best["edge"]), 3),
                shares=round(pos.size, 3),
            )

        if self.maker_shadow is not None:
            self.maker_shadow.on_tick(
                now=time.time(),
                fair_by_asset=fair_by_asset,
                books=self.books.books,
                params_by_asset={
                    a: self.runtime.params_for(a) for a in self.settings.assets_list
                },
            )

    # ── filters / selection ──────────────────────────────────────────────
    def _entry_check(
        self,
        market: WindowMarket,
        now: float,
        params,
        best: Optional[dict],
        position: Optional[dict],
        payload: dict,
    ) -> str:
        if position is not None:
            return "in_position"
        if not params.trading_enabled:
            return "disabled"
        ok, reason = self.risk.can_enter(market.asset)
        if not ok:
            return reason
        rel = now - market.start_ts
        if rel < params.entry_min_rel_s:
            return f"early({rel:.0f}s)"
        if rel > params.entry_max_rel_s:
            return f"late({rel:.0f}s)"
        if best is None:
            gated = sorted(
                {str(c["gate"]) for c in payload.get("candidates") or [] if c.get("gate")}
            )
            if gated:
                return "filtered(" + ",".join(gated) + ")"
            return "no_book"
        spread = best.get("spread")
        if spread is None:
            return "one_sided_book"
        if spread > params.max_spread:
            return f"wide_spread({spread:.3f})"
        age = best.get("age_s")
        if age is None or age > params.max_stale_book_s:
            return f"stale_book({age})"
        if float(best.get("best_size") or 0.0) < params.min_ask_shares:
            return f"thin_best({best.get('best_size')})"
        threshold = params.min_edge + params.model_buffer
        if float(best["edge"]) < threshold:
            return f"edge {best['edge']:.3f} < {threshold:.3f}"
        return "candidate"

    @staticmethod
    def _gate_reason(params, fair: float, effective_price: float) -> Optional[str]:
        """Side-level filters that run before the edge threshold."""
        if fair < params.min_fair:
            return "fair"
        if effective_price < params.min_entry_price:
            return "price"
        return None

    def _candidates(
        self, market: WindowMarket, fair_up: float, now: float, params
    ) -> list[dict]:
        out: list[dict] = []
        for side, token, fair in (
            ("UP", market.up_token, fair_up),
            ("DOWN", market.down_token, 1.0 - fair_up),
        ):
            book = self.books.book(token)
            if book is None:
                continue
            preview = self.paper.preview_buy(
                book.asks_ascending(),
                params.max_position_usd,
                params.taker_fee_rate,
            )
            if preview is None:
                continue
            fair = float(fair)
            gate = self._gate_reason(params, fair, preview.effective_price)
            out.append(
                {
                    "side": side,
                    "fair": fair,
                    "best_bid": book.best_bid(),
                    "best_ask": book.best_ask(),
                    "spread": book.spread(),
                    "avg_price": preview.avg_price,
                    "effective_price": preview.effective_price,
                    "shares": preview.shares,
                    "notional": preview.notional,
                    "fee": preview.fee,
                    "fee_per_share": preview.fee_per_share,
                    "edge": fair - preview.effective_price,
                    "best_size": preview.best_size,
                    "age_s": (now - book.updated_ts) if book.updated_ts else None,
                    "gate": gate,
                    "eligible": gate is None,
                }
            )
        return out

    # ── publishing / signals ─────────────────────────────────────────────
    def _base_payload(
        self,
        market: WindowMarket,
        now: float,
        *,
        status: str = "",
        **extra: Any,
    ) -> dict:
        payload: dict[str, Any] = {
            "asset": market.asset,
            "ts": now,
            "window": market.to_dict(),
            "rel": now - market.start_ts,
            "time_left": market.end_ts - now,
            "position": self._position_info(market.asset, market.start_ts),
            "status": status,
            "mode": self.router.mode_for(market.asset),
            "killswitch": self.kill.to_dict(),
            "daily_pnl": self.risk.daily_pnl(market.asset),
            "params": self.runtime.params_for(market.asset).model_dump(),
        }
        payload.update(extra)
        return payload

    def _publish(self, asset: str, payload: dict) -> None:
        payload["asset"] = asset
        payload["ts"] = time.time()
        self.latest[asset] = payload

    def _position_info(self, asset: str, start_ts: int) -> Optional[dict]:
        pos = self.positions.get((asset, int(start_ts)))
        if pos is None:
            return None
        info = pos.to_dict()
        book = self.books.book(pos.token_id)
        mark = book.best_bid() if book is not None else None
        if mark is not None:
            info["mark"] = mark
            info["unrealized"] = pos.size * mark - pos.cost
        return info

    def _log_signal(
        self,
        asset: str,
        market: WindowMarket,
        side: str,
        best: dict,
        reason: str,
        acted: bool,
    ) -> None:
        try:
            self.db.insert_signal(
                SignalSnapshot(
                    ts=time.time(),
                    asset=asset,
                    start_ts=market.start_ts,
                    side=side,
                    fair=float(best.get("fair") or 0.0),
                    ask=float(best.get("avg_price") or 0.0),
                    edge=float(best.get("edge") or 0.0),
                    fee=float(best.get("fee_per_share") or 0.0),
                    shares=float(best.get("shares") or 0.0),
                    reason=reason,
                    acted=acted,
                )
            )
        except Exception as exc:  # noqa: BLE001
            log.warning("engine.signal_log_failed", error=str(exc))

    def _maybe_log_near_miss(
        self,
        asset: str,
        market: WindowMarket,
        best: Optional[dict],
        params,
        status: str,
    ) -> None:
        if best is None:
            return
        edge = float(best.get("edge") or 0.0)
        if edge < params.signal_log_min_edge:
            return
        now = time.time()
        if now - self._last_near_miss.get(asset, 0.0) < _NEAR_MISS_LOG_S:
            return
        self._last_near_miss[asset] = now
        self._log_signal(
            asset,
            market,
            str(best.get("side") or ""),
            best,
            status,
            acted=False,
        )

    # ── settlement ───────────────────────────────────────────────────────
    async def settle_due(self) -> None:
        now = time.time()
        for key, pos in list(self.positions.items()):
            if now < pos.end_ts + 1.0:
                continue
            outcome, final_price = await self._resolve_outcome(pos, now)
            if outcome is None:
                continue
            payout = pos.size if pos.side == outcome else 0.0
            pnl = payout - pos.cost
            self.db.settle_position(pos.id, outcome, payout, pnl)
            self.db.set_window_final(pos.asset, pos.start_ts, final_price, outcome)
            if self.maker_shadow is not None:
                self.maker_shadow.settle(pos, outcome)
            log.info(
                "engine.settled",
                asset=pos.asset,
                window=pos.start_ts,
                side=pos.side,
                outcome=outcome,
                close=final_price,
                p0=pos.price_to_beat,
                shares=round(pos.size, 4),
                cost=round(pos.cost, 4),
                payout=round(payout, 4),
                pnl=round(pnl, 4),
            )
            self.positions.pop(key, None)
            self._settle_first_seen.pop(key, None)

    async def _resolve_outcome(
        self, pos: PaperPosition, now: float
    ) -> tuple[Optional[str], Optional[float]]:
        key = (pos.asset, pos.start_ts)
        first = self._settle_first_seen.setdefault(key, now)
        buf = self.rtds.buffer(pos.asset)
        close = buf.twap_ending_at(pos.end_ts, 60) if buf is not None else None

        # Prefer the official Gamma finalPrice when it is available.
        try:
            official = await self.windows.fetch_window(pos.asset, pos.start_ts)
        except Exception:
            official = None
        if (
            official is not None
            and official.price_to_beat is not None
            and official.final_price is not None
        ):
            p0 = float(official.price_to_beat)
            fp = float(official.final_price)
            outcome = "UP" if fp >= p0 else "DOWN"
            if close is not None:
                rtds_outcome = "UP" if close >= pos.price_to_beat else "DOWN"
                if rtds_outcome != outcome:
                    log.warning(
                        "engine.settle_disagreement",
                        asset=pos.asset,
                        window=pos.start_ts,
                        rtds_close=close,
                        rtds_p0=pos.price_to_beat,
                        gamma_close=fp,
                        gamma_p0=p0,
                    )
            return outcome, fp

        if close is None:
            return None, None
        p0 = pos.price_to_beat
        near_tie = abs(close - p0) / p0 < _TIE_REL if p0 > 0 else False
        if near_tie and now - first < 60.0:
            # Wait briefly for the official resolution rather than gambling
            # on a sub-0.1bp difference.
            return None, None
        return ("UP" if close >= p0 else "DOWN"), close
