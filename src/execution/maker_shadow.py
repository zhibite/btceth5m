"""Maker-first shadow simulation for the paper bot.

The paper broker keeps executing as a taker, so the bot's own fill/PnL series
stays continuous. For every taker entry this module runs a parallel, simulated
maker-first policy on the same signal:

  * join the best bid with a GTC buy for the same share count,
  * fill only when incoming SELL prints consume the queue ahead of us,
  * optionally chase the best bid when it moves up,
  * cancel when the fair edge disappears,
  * at a deadline, sweep the ask for the remainder if the edge still holds.

Nothing here touches the real paper position. The result is written to the
`maker_shadow` table so the dashboard can compare both tracks on identical
signals.

Fill model:

  * we join at the back of the level; size already resting there is queue,
  * only SELL prints at or below our bid count; a print above our bid traded
    against a higher bid and cannot fill us,
  * a print below our bid means the sweep passed through our level, so its
    size also counts toward the queue being consumed,
  * cancellations ahead of us are ignored (understates fill rate),
  * our $5 order does not move the book,
  * partial fills are allowed; the remainder waits until the deadline.

The model is an approximation: it cannot see orders that cancel ahead of us
(understates fills), and it infers the volume that passed our level from the
real print stream, which did not contain our shadow order. Calibrate against
recorded book+trades before switching the live execution policy.
"""
from __future__ import annotations

import math
import time
from dataclasses import dataclass
from typing import Any, Optional

from src.config import StrategyParams
from src.infra.logging import get_logger
from src.market.models import OrderBook, PaperPosition, WindowMarket
from src.persistence.db import Database
from src.strategy.fair_value import per_share_fee

log = get_logger(__name__)

_EPS = 1e-9


@dataclass
class MakerQuote:
    """Signal-time snapshot of the book used to simulate the resting order."""

    bid: float
    ask: float
    spread: Optional[float]
    fair: float
    quote_price: float
    queue_ahead: float
    depth_above: float
    target_shares: float


@dataclass
class ShadowAttempt:
    id: int
    asset: str
    start_ts: int
    end_ts: int
    side: str
    token_id: str
    opened_ts: float
    quote: MakerQuote
    taker_price: float
    taker_fee: float
    taker_shares: float
    taker_cost: float
    consumed: float = 0.0
    maker_filled: float = 0.0
    maker_notional: float = 0.0
    first_fill_ts: Optional[float] = None
    last_fill_ts: Optional[float] = None
    fallback_used: bool = False
    fallback_filled: float = 0.0
    fallback_notional: float = 0.0
    fallback_fee: float = 0.0
    fallback_ts: Optional[float] = None
    status: str = "open"
    cancel_reason: str = ""

    @property
    def total_shares(self) -> float:
        return self.maker_filled + self.fallback_filled

    @property
    def total_cost(self) -> float:
        return (
            self.maker_notional
            + self.fallback_notional
            + self.fallback_fee
        )


def _num(value: Any, default: float = 0.0) -> float:
    try:
        if value is None:
            return default
        out = float(value)
    except (TypeError, ValueError):
        return default
    if math.isnan(out) or math.isinf(out):
        return default
    return out


def _mean(values: list[float]) -> Optional[float]:
    return sum(values) / len(values) if values else None


def _safe_div(numerator: float, denominator: float) -> Optional[float]:
    return numerator / denominator if denominator else None


def walk_asks_shares(
    asks: list[tuple[float, float]], shares: float, fee_rate: float
) -> Optional[tuple[float, float, float]]:
    """Walk the ask ladder to buy exactly `shares`.

    Returns (shares, notional, fee) or None if the book is empty. The caller
    decides whether the partial amount is acceptable.
    """
    remaining = float(shares)
    taken = 0.0
    notional = 0.0
    fee = 0.0
    for price, size in sorted(asks, key=lambda kv: kv[0]):
        if not (0.0 < price < 1.0) or size <= 0:
            continue
        take = min(float(size), remaining)
        if take <= 0:
            break
        taken += take
        notional += take * float(price)
        fee += per_share_fee(fee_rate, float(price)) * take
        remaining -= take
        if remaining <= _EPS:
            break
    if taken <= 0:
        return None
    return taken, notional, fee


class MakerShadow:
    def __init__(self, db: Database) -> None:
        self.db = db
        self._attempts: dict[tuple[str, int], ShadowAttempt] = {}

    # ── lifecycle ────────────────────────────────────────────────────────
    def load_active(self, now: Optional[float] = None) -> None:
        """Resume open attempts after a restart and settle orphans."""
        now = now if now is not None else time.time()
        self._reconcile_ended(now)
        for row in self.db.active_maker_shadow(now):
            attempt = self._from_row(row)
            if attempt is None:
                continue
            self._attempts[(attempt.asset, attempt.start_ts)] = attempt
        if self._attempts:
            log.info("maker_shadow.resumed", attempts=len(self._attempts))

    def _reconcile_ended(self, now: float) -> None:
        """Settle shadow rows whose window ended while we were not running."""
        for row in self.db.ended_unsettled_maker_shadow(now):
            outcome = row.get("window_outcome")
            if outcome is None:
                continue
            self._settle_row(row, str(outcome), now)
            log.info(
                "maker_shadow.reconciled",
                asset=row.get("asset"),
                window=row.get("start_ts"),
                outcome=outcome,
            )

    # ── open ─────────────────────────────────────────────────────────────
    @staticmethod
    def capture_quote(book: Optional[OrderBook]) -> Optional[dict[str, float]]:
        if book is None:
            return None
        bid = book.best_bid()
        ask = book.best_ask()
        if bid is None or ask is None or not (0.0 < bid < 1.0):
            return None
        return {
            "bid": float(bid),
            "ask": float(ask),
            "queue_ahead": float(book.bids.get(float(bid), 0.0)),
            "depth_above": float(
                sum(size for price, size in book.bids.items() if price > bid)
            ),
        }

    def open_attempt(
        self,
        *,
        market: WindowMarket,
        side: str,
        token_id: str,
        taker_pos: PaperPosition,
        fair: float,
        now: float,
        params: StrategyParams,
        book: Optional[OrderBook] = None,
        captured: Optional[dict[str, float]] = None,
    ) -> Optional[ShadowAttempt]:
        if not params.maker_shadow_enabled:
            return None
        key = (market.asset, int(market.start_ts))
        if key in self._attempts:
            return None
        snapshot = captured or self.capture_quote(book)
        if snapshot is None:
            return None
        quote_price = float(snapshot["bid"])
        target_shares = float(taker_pos.size)
        if target_shares <= 0:
            return None
        # The taker signal passed fair - ask - fee >= edge; the maker quote at
        # the bid must pass the *maker* threshold before we rest an order.
        threshold = params.min_edge + params.model_buffer + params.maker_buffer
        if float(fair) - quote_price < threshold:
            return None

        spread = (
            float(snapshot["ask"]) - quote_price
            if snapshot.get("ask") is not None
            else None
        )
        quote = MakerQuote(
            bid=quote_price,
            ask=float(snapshot["ask"]),
            spread=spread,
            fair=float(fair),
            quote_price=quote_price,
            queue_ahead=float(snapshot["queue_ahead"]),
            depth_above=float(snapshot["depth_above"]),
            target_shares=target_shares,
        )
        attempt = ShadowAttempt(
            id=0,
            asset=market.asset,
            start_ts=int(market.start_ts),
            end_ts=int(market.end_ts),
            side=side.upper(),
            token_id=str(token_id),
            opened_ts=float(now),
            quote=quote,
            taker_price=float(taker_pos.avg_price),
            taker_fee=float(taker_pos.fees),
            taker_shares=float(taker_pos.size),
            taker_cost=float(taker_pos.cost),
        )
        attempt.id = self.db.insert_maker_shadow(
            ts=now,
            asset=attempt.asset,
            start_ts=attempt.start_ts,
            end_ts=attempt.end_ts,
            side=attempt.side,
            token_id=attempt.token_id,
            taker_position_id=int(taker_pos.id or 0),
            fair=quote.fair,
            ask=quote.ask,
            bid=quote.bid,
            spread=quote.spread,
            quote_price=quote.quote_price,
            queue_ahead=quote.queue_ahead,
            depth_above=quote.depth_above,
            target_shares=quote.target_shares,
            taker_price=attempt.taker_price,
            taker_fee=attempt.taker_fee,
            taker_shares=attempt.taker_shares,
            taker_cost=attempt.taker_cost,
            status="open",
        )
        self._attempts[key] = attempt
        log.info(
            "maker_shadow.open",
            asset=attempt.asset,
            window=attempt.start_ts,
            side=attempt.side,
            quote=round(quote.quote_price, 4),
            queue=round(quote.queue_ahead, 2),
            target=round(quote.target_shares, 4),
            edge=round(float(fair) - quote.quote_price, 4),
        )
        return attempt

    # ── trade prints → fills ─────────────────────────────────────────────
    def on_trades(self, events: list[dict[str, Any]]) -> None:
        if not events or not self._attempts:
            return
        for event in events:
            token = str(event.get("token_id") or "")
            if not token:
                continue
            # A maker BUY only fills when the taker SELLs into the bid.
            if str(event.get("side") or "").upper() != "SELL":
                continue
            try:
                price = float(event.get("price"))
                size = float(event.get("size") or 0.0)
            except (TypeError, ValueError):
                continue
            if size <= 0:
                continue
            ts = _num(event.get("ts"), time.time())
            for attempt in list(self._attempts.values()):
                if attempt.status != "open" or attempt.token_id != token:
                    continue
                # A print that arrived before our order was resting cannot
                # have filled it.
                if ts + _EPS < attempt.opened_ts:
                    continue
                # Prints above our bid traded against higher bids; they say
                # nothing about the queue at our price. Prints at or below
                # our bid mean the sweep reached / passed our level.
                if price > attempt.quote.quote_price + _EPS:
                    continue
                attempt.consumed += size
                available = attempt.consumed - attempt.quote.queue_ahead
                new_fill = available - attempt.maker_filled
                remaining = attempt.quote.target_shares - attempt.maker_filled
                if new_fill <= _EPS or remaining <= _EPS:
                    continue
                fill = min(remaining, new_fill)
                attempt.maker_filled += fill
                attempt.maker_notional += fill * attempt.quote.quote_price
                if attempt.first_fill_ts is None:
                    attempt.first_fill_ts = ts
                attempt.last_fill_ts = ts
                log.info(
                    "maker_shadow.fill",
                    asset=attempt.asset,
                    window=attempt.start_ts,
                    side=attempt.side,
                    price=round(attempt.quote.quote_price, 4),
                    fill=round(fill, 4),
                    filled=round(attempt.maker_filled, 4),
                    target=round(attempt.quote.target_shares, 4),
                )
                if attempt.maker_filled >= attempt.quote.target_shares - _EPS:
                    attempt.status = "maker_filled"
                self._persist(attempt)
                if attempt.status == "maker_filled":
                    log.info(
                        "maker_shadow.filled",
                        asset=attempt.asset,
                        window=attempt.start_ts,
                        side=attempt.side,
                        shares=round(attempt.maker_filled, 4),
                        wait_s=round(
                            (attempt.first_fill_ts or ts) - attempt.opened_ts, 3
                        ),
                    )

    # ── periodic policy ──────────────────────────────────────────────────
    def on_tick(
        self,
        *,
        now: float,
        fair_by_asset: dict[str, Optional[float]],
        books: dict[str, OrderBook],
        params: StrategyParams | None = None,
        params_by_asset: dict[str, StrategyParams] | None = None,
    ) -> None:
        for attempt in list(self._attempts.values()):
            if attempt.status != "open":
                continue
            # Per-asset config wins; the old single `params` argument remains
            # for callers/tests that still pass one global object.
            p = (params_by_asset or {}).get(attempt.asset) or params
            if p is None:
                continue
            fair_up = fair_by_asset.get(attempt.asset)
            fair_side = None
            if fair_up is not None:
                fair_side = (
                    float(fair_up)
                    if attempt.side == "UP"
                    else 1.0 - float(fair_up)
                )
            book = books.get(attempt.token_id)
            threshold = p.min_edge + p.model_buffer + p.maker_buffer

            # 1) The model no longer supports the resting bid: cancel.
            if fair_side is not None and fair_side - attempt.quote.quote_price < threshold:
                self._cancel(attempt, "fair_dropped", now)
                continue

            # 2) Chase the best bid upward so we keep price priority. A real
            #    maker would cancel/replace; the queue position resets.
            if p.maker_chase_bid and book is not None and fair_side is not None:
                bid = book.best_bid()
                if bid is not None and bid > attempt.quote.quote_price + _EPS:
                    if fair_side - float(bid) < threshold:
                        self._cancel(attempt, "fair_dropped_at_new_bid", now)
                        continue
                    self._requote(attempt, book, float(bid), now)

            # 3) Deadline: take the remainder if the taker edge still holds.
            deadline = min(
                int(p.maker_fallback_rel_s), int(p.entry_max_rel_s)
            )
            if now - attempt.start_ts >= deadline:
                self._deadline(attempt, book, fair_side, p, now)

    def _requote(
        self, attempt: ShadowAttempt, book: OrderBook, new_bid: float, now: float
    ) -> None:
        attempt.quote.quote_price = new_bid
        attempt.quote.queue_ahead = float(book.bids.get(new_bid, 0.0))
        attempt.quote.depth_above = float(
            sum(size for price, size in book.bids.items() if price > new_bid)
        )
        # Re-anchor the consumption counter so the fill rule
        #   new_fill = (consumed - queue_ahead) - maker_filled
        # still measures prints beyond the *new* queue. Zeroing it discards
        # the offset that maker_filled subtracts, so a partial fill at the
        # old price would have to be consumed again at the new level.
        attempt.consumed = attempt.maker_filled
        self._persist(attempt)
        log.info(
            "maker_shadow.requote",
            asset=attempt.asset,
            window=attempt.start_ts,
            side=attempt.side,
            quote=round(new_bid, 4),
            queue=round(attempt.quote.queue_ahead, 2),
        )

    def _cancel(self, attempt: ShadowAttempt, reason: str, now: float) -> None:
        attempt.status = "canceled"
        attempt.cancel_reason = reason
        self._persist(attempt)
        log.info(
            "maker_shadow.cancel",
            asset=attempt.asset,
            window=attempt.start_ts,
            side=attempt.side,
            reason=reason,
            filled=round(attempt.maker_filled, 4),
        )

    def _deadline(
        self,
        attempt: ShadowAttempt,
        book: Optional[OrderBook],
        fair_side: Optional[float],
        params: StrategyParams,
        now: float,
    ) -> None:
        remaining = attempt.quote.target_shares - attempt.maker_filled
        if remaining <= _EPS:
            attempt.status = "maker_filled"
            self._persist(attempt)
            return
        if remaining < params.min_order_shares:
            # A taker top-up below the exchange minimum would be rejected.
            self._finish(attempt, "deadline_min_size")
            return
        if fair_side is None or book is None:
            self._finish(attempt, "deadline_no_fair")
            return
        fill = walk_asks_shares(
            book.asks_ascending(), remaining, params.taker_fee_rate
        )
        if fill is None:
            self._finish(attempt, "deadline_empty_book")
            return
        shares, notional, fee = fill
        if shares < params.min_order_shares:
            self._finish(attempt, "deadline_thin_book")
            return
        effective = (notional + fee) / shares
        if fair_side - effective < params.min_edge + params.model_buffer:
            self._finish(attempt, "deadline_edge_gone")
            return
        attempt.fallback_used = True
        attempt.fallback_filled += shares
        attempt.fallback_notional += notional
        attempt.fallback_fee += fee
        attempt.fallback_ts = now
        attempt.status = (
            "maker_then_taker" if attempt.maker_filled > _EPS else "taker_fallback"
        )
        self._persist(attempt)
        log.info(
            "maker_shadow.fallback",
            asset=attempt.asset,
            window=attempt.start_ts,
            side=attempt.side,
            shares=round(shares, 4),
            price=round(notional / shares, 4),
            fee=round(fee, 5),
            maker_filled=round(attempt.maker_filled, 4),
        )

    def _finish(self, attempt: ShadowAttempt, reason: str) -> None:
        attempt.cancel_reason = reason
        attempt.status = (
            "partial_maker" if attempt.maker_filled > _EPS else "unfilled"
        )
        self._persist(attempt)
        log.info(
            "maker_shadow.finished",
            asset=attempt.asset,
            window=attempt.start_ts,
            side=attempt.side,
            status=attempt.status,
            reason=reason,
            filled=round(attempt.maker_filled, 4),
        )

    # ── persistence / settlement ─────────────────────────────────────────
    def _persist(self, attempt: ShadowAttempt) -> None:
        self.db.update_maker_shadow(
            attempt.id,
            quote_price=attempt.quote.quote_price,
            queue_ahead=attempt.quote.queue_ahead,
            depth_above=attempt.quote.depth_above,
            target_shares=attempt.quote.target_shares,
            consumed=attempt.consumed,
            maker_filled=attempt.maker_filled,
            maker_notional=attempt.maker_notional,
            first_fill_ts=attempt.first_fill_ts,
            last_fill_ts=attempt.last_fill_ts,
            fallback_used=1 if attempt.fallback_used else 0,
            fallback_filled=attempt.fallback_filled,
            fallback_notional=attempt.fallback_notional,
            fallback_fee=attempt.fallback_fee,
            fallback_ts=attempt.fallback_ts,
            status=attempt.status,
            cancel_reason=attempt.cancel_reason,
        )

    def settle(self, pos: PaperPosition, outcome: str) -> None:
        """Settle the shadow row that belongs to a just-settled paper position."""
        row = self.db.maker_shadow_row(pos.asset, pos.start_ts)
        self._attempts.pop((pos.asset, int(pos.start_ts)), None)
        if row is None or row.get("settled_ts") is not None:
            return
        self._settle_row(row, str(outcome), time.time(), taker_pos=pos)

    def _settle_row(
        self,
        row: dict[str, Any],
        outcome: str,
        now: float,
        taker_pos: Optional[PaperPosition] = None,
    ) -> None:
        outcome = outcome.upper()
        shares = _num(row.get("maker_filled")) + _num(row.get("fallback_filled"))
        cost = (
            _num(row.get("maker_notional"))
            + _num(row.get("fallback_notional"))
            + _num(row.get("fallback_fee"))
        )
        payout = shares if str(row.get("side") or "").upper() == outcome else 0.0
        pnl = payout - cost

        if taker_pos is not None:
            taker_shares = float(taker_pos.size)
            taker_cost = float(taker_pos.cost)
            taker_side = str(taker_pos.side).upper()
        else:
            taker_shares = _num(row.get("taker_shares"))
            taker_cost = _num(row.get("taker_cost"))
            taker_side = str(row.get("side") or "").upper()
        taker_payout = taker_shares if taker_side == outcome else 0.0
        taker_pnl = taker_payout - taker_cost

        self.db.settle_maker_shadow(
            int(row["id"]),
            outcome=outcome,
            payout=payout,
            pnl=pnl,
            taker_pnl=taker_pnl,
            settled_ts=now,
        )
        log.info(
            "maker_shadow.settled",
            asset=row.get("asset"),
            window=row.get("start_ts"),
            side=row.get("side"),
            outcome=outcome,
            shares=round(shares, 4),
            pnl=round(pnl, 4),
            taker_pnl=round(taker_pnl, 4),
            diff=round(pnl - taker_pnl, 4),
        )

    def _from_row(self, row: dict[str, Any]) -> Optional[ShadowAttempt]:
        try:
            quote = MakerQuote(
                bid=float(row["bid"]),
                ask=float(row["ask"]),
                spread=(
                    float(row["spread"]) if row.get("spread") is not None else None
                ),
                fair=float(row["fair"]),
                quote_price=float(row["quote_price"]),
                queue_ahead=_num(row.get("queue_ahead")),
                depth_above=_num(row.get("depth_above")),
                target_shares=_num(row.get("target_shares")),
            )
            return ShadowAttempt(
                id=int(row["id"]),
                asset=str(row["asset"]),
                start_ts=int(row["start_ts"]),
                end_ts=int(row["end_ts"]),
                side=str(row["side"]),
                token_id=str(row["token_id"]),
                opened_ts=float(row["ts"]),
                quote=quote,
                taker_price=_num(row.get("taker_price")),
                taker_fee=_num(row.get("taker_fee")),
                taker_shares=_num(row.get("taker_shares")),
                taker_cost=_num(row.get("taker_cost")),
                consumed=_num(row.get("consumed")),
                maker_filled=_num(row.get("maker_filled")),
                maker_notional=_num(row.get("maker_notional")),
                first_fill_ts=row.get("first_fill_ts"),
                last_fill_ts=row.get("last_fill_ts"),
                fallback_used=bool(row.get("fallback_used")),
                fallback_filled=_num(row.get("fallback_filled")),
                fallback_notional=_num(row.get("fallback_notional")),
                fallback_fee=_num(row.get("fallback_fee")),
                fallback_ts=row.get("fallback_ts"),
                status=str(row.get("status") or "open"),
                cancel_reason=str(row.get("cancel_reason") or ""),
            )
        except (KeyError, TypeError, ValueError):
            return None


# ---------------------------------------------------------------------------
# Dashboard / report aggregation
# ---------------------------------------------------------------------------
def _stats(rows: list[dict[str, Any]]) -> dict[str, Any]:
    n = len(rows)
    settled = [r for r in rows if r.get("settled_ts") is not None]
    maker_any = [r for r in rows if _num(r.get("maker_filled")) > 0]
    maker_full = [
        r
        for r in rows
        if _num(r.get("maker_filled")) > 0
        and _num(r.get("fallback_filled")) <= 0
        and _num(r.get("maker_filled")) >= _num(r.get("target_shares")) - 1e-6
    ]
    fallback = [r for r in rows if _num(r.get("fallback_filled")) > 0]
    unfilled = [r for r in settled if _num(r.get("total_shares")) <= 0]
    traded = [r for r in settled if _num(r.get("total_shares")) > 0]

    waits = [
        _num(r.get("wait_s"))
        for r in rows
        if r.get("first_fill_ts") is not None and r.get("wait_s") is not None
    ]
    maker_shares = sum(_num(r.get("maker_filled")) for r in rows)
    maker_notional = sum(_num(r.get("maker_notional")) for r in rows)
    taker_shares = sum(_num(r.get("taker_shares")) for r in settled)
    taker_price_num = sum(
        _num(r.get("taker_price")) * _num(r.get("taker_shares")) for r in settled
    )

    pnl_maker = sum(_num(r.get("pnl")) for r in settled)
    pnl_taker = sum(_num(r.get("taker_pnl")) for r in settled)
    maker_wins = sum(1 for r in traded if _num(r.get("pnl")) > 0)
    taker_wins = sum(1 for r in settled if _num(r.get("taker_pnl")) > 0)
    # Compare like-for-like: the taker fee is pro-rated to the shares the
    # maker track actually bought. A signal the maker track skipped must not
    # show up as "fee saved".
    fee_saved = 0.0
    for r in settled:
        taker_shares = _num(r.get("taker_shares"))
        filled = _num(r.get("total_shares"))
        if taker_shares <= 0 or filled <= 0:
            continue
        fee_saved += (
            _num(r.get("taker_fee")) * (filled / taker_shares)
            - _num(r.get("fallback_fee"))
        )

    filled_settled = [r for r in settled if _num(r.get("maker_filled")) > 0]
    filled_shares = sum(_num(r.get("total_shares")) for r in filled_settled)
    filled_pnl = sum(_num(r.get("pnl")) for r in filled_settled)
    filled_taker_shares = sum(_num(r.get("taker_shares")) for r in filled_settled)
    filled_taker_pnl = sum(_num(r.get("taker_pnl")) for r in filled_settled)
    filled_maker_ps = _safe_div(filled_pnl, filled_shares)
    filled_taker_ps = _safe_div(filled_taker_pnl, filled_taker_shares)

    return {
        "n": n,
        "settled": len(settled),
        "maker_any": len(maker_any),
        "maker_full": len(maker_full),
        "fallback": len(fallback),
        "unfilled": len(unfilled),
        "maker_fill_rate": _safe_div(float(len(maker_any)), float(n)),
        "full_fill_rate": _safe_div(float(len(maker_full)), float(n)),
        "fallback_rate": _safe_div(float(len(fallback)), float(n)),
        "avg_wait_s": _mean(waits),
        "avg_quote": _mean([_num(r.get("quote_price")) for r in rows]),
        "avg_maker_price": _safe_div(maker_notional, maker_shares),
        "avg_taker_price": _safe_div(taker_price_num, taker_shares),
        "fee_saved_total": fee_saved,
        "fee_saved_per_signal": _safe_div(fee_saved, float(len(settled))),
        "pnl_maker": pnl_maker,
        "pnl_taker": pnl_taker,
        "pnl_diff": pnl_maker - pnl_taker,
        "avg_pnl_maker": _safe_div(pnl_maker, float(len(settled))),
        "avg_pnl_taker": _safe_div(pnl_taker, float(len(settled))),
        "avg_pnl_diff": _safe_div(pnl_maker - pnl_taker, float(len(settled))),
        "win_rate_maker": _safe_div(float(maker_wins), float(len(traded))),
        "win_rate_taker": _safe_div(float(taker_wins), float(len(settled))),
        "filled_subset": len(filled_settled),
        "filled_pnl_ps": filled_maker_ps,
        "filled_taker_pnl_ps": filled_taker_ps,
        "adverse_gap": (
            filled_maker_ps - filled_taker_ps
            if filled_maker_ps is not None and filled_taker_ps is not None
            else None
        ),
    }


def summarize_shadow(
    rows: list[dict[str, Any]]
) -> tuple[dict[str, Any], list[dict[str, Any]]]:
    """Return (overall, by_asset) maker-vs-taker statistics."""
    overall = _stats(rows)
    assets = sorted(
        {str(r.get("asset")) for r in rows if r.get("asset")}
    )
    by_asset: list[dict[str, Any]] = []
    for asset in assets:
        stats = _stats([r for r in rows if str(r.get("asset")) == asset])
        stats["asset"] = asset
        by_asset.append(stats)
    return overall, by_asset
