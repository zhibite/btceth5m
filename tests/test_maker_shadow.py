import json
import time

from src.config import StrategyParams
from src.execution.maker_shadow import (
    MakerShadow,
    summarize_shadow,
    walk_asks_shares,
)
from src.feeds.clob_ws import ClobWsFeed
from src.market.models import OrderBook, PaperPosition, WindowMarket
from src.persistence.db import Database


def _params(**overrides) -> StrategyParams:
    data = {
        "maker_shadow_enabled": True,
        "maker_buffer": 0.01,
        "maker_fallback_rel_s": 250,
        "maker_chase_bid": False,
    }
    data.update(overrides)
    return StrategyParams(**data)


def _position(size: float = 10.0, price: float = 0.50, fee: float = 0.10) -> PaperPosition:
    return PaperPosition(
        id=7,
        asset="BTC",
        start_ts=1000,
        end_ts=1300,
        token_id="up",
        side="UP",
        size=size,
        avg_price=price,
        cost=size * price + fee,
        fees=fee,
        entry_ts=1001.0,
        price_to_beat=100.0,
    )


def _book(
    bids: dict[float, float] | None = None,
    asks: dict[float, float] | None = None,
) -> OrderBook:
    book = OrderBook("up")
    book.bids = dict(bids if bids is not None else {0.49: 100.0, 0.48: 200.0})
    book.asks = dict(asks if asks is not None else {0.50: 100.0})
    book.updated_ts = time.time()
    return book


def _open(db, shadow, book=None, fair=0.60, params=None, pos=None):
    return shadow.open_attempt(
        market=WindowMarket(
            asset="BTC",
            start_ts=1000,
            end_ts=1300,
            up_token="up",
            down_token="down",
        ),
        side="UP",
        token_id="up",
        taker_pos=pos or _position(),
        fair=fair,
        now=1001.0,
        params=params or _params(),
        book=book or _book(),
    )


def _trade(price: float, size: float, side: str = "SELL", ts: float = 1002.0):
    return {
        "token_id": "up",
        "side": side,
        "price": price,
        "size": size,
        "ts": ts,
    }


def test_open_quotes_best_bid_with_queue(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    attempt = _open(db, shadow)

    assert attempt is not None
    assert attempt.quote.quote_price == 0.49
    assert attempt.quote.queue_ahead == 100.0
    assert attempt.quote.depth_above == 0.0  # best bid has nothing above it
    assert attempt.quote.target_shares == 10.0

    row = db.maker_shadow_row("BTC", 1000)
    assert row is not None
    assert row["status"] == "open"
    assert abs(float(row["quote_price"]) - 0.49) < 1e-9
    db.close()


def test_fill_needs_to_consume_the_queue(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    _open(db, shadow)

    shadow.on_trades([_trade(0.49, 50.0)])  # 50 of 100 ahead
    row = db.maker_shadow_row("BTC", 1000)
    assert float(row["maker_filled"]) == 0.0

    shadow.on_trades([_trade(0.50, 100.0)])  # above our bid: cannot fill us
    row = db.maker_shadow_row("BTC", 1000)
    assert float(row["maker_filled"]) == 0.0

    shadow.on_trades([_trade(0.48, 50.0)])  # sweep passed our level
    row = db.maker_shadow_row("BTC", 1000)
    assert float(row["maker_filled"]) == 0.0  # queue exactly consumed

    shadow.on_trades([_trade(0.48, 10.0)])  # 10 past the queue
    row = db.maker_shadow_row("BTC", 1000)
    assert abs(float(row["maker_filled"]) - 10.0) < 1e-9
    assert abs(float(row["maker_notional"]) - 4.9) < 1e-9
    assert row["status"] == "maker_filled"
    db.close()


def test_sweep_through_bid_fills_maker(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    _open(db, shadow)

    # One print below the bid: the taker swept through our level.
    shadow.on_trades([_trade(0.48, 120.0)])
    row = db.maker_shadow_row("BTC", 1000)
    assert abs(float(row["maker_filled"]) - 10.0) < 1e-9
    assert abs(float(row["maker_notional"]) - 4.9) < 1e-9
    db.close()


def test_requote_does_not_double_count_prior_fills(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    params = _params(maker_chase_bid=True)
    _open(db, shadow, params=params)

    shadow.on_trades([_trade(0.49, 104.0)])  # 4 fill, 6 remain at 0.49

    # The best bid moves up to 0.50 with 50 shares of queue; the remaining
    # 6 shares requote to the back of the new level.
    book = _book(bids={0.50: 50.0, 0.49: 100.0})
    shadow.on_tick(
        now=1010.0,
        fair_by_asset={"BTC": 0.61},
        books={"up": book},
        params=params,
    )
    row = db.maker_shadow_row("BTC", 1000)
    assert abs(float(row["quote_price"]) - 0.50) < 1e-9
    assert abs(float(row["maker_filled"]) - 4.0) < 1e-9

    # Consuming the new queue fills nothing...
    shadow.on_trades([_trade(0.50, 50.0, ts=1011.0)])
    row = db.maker_shadow_row("BTC", 1000)
    assert abs(float(row["maker_filled"]) - 4.0) < 1e-9

    # ...and only prints beyond it fill the remaining 6 shares.
    shadow.on_trades([_trade(0.50, 6.0, ts=1012.0)])
    row = db.maker_shadow_row("BTC", 1000)
    assert abs(float(row["maker_filled"]) - 10.0) < 1e-9
    assert row["status"] == "maker_filled"
    db.close()


def test_partial_maker_then_taker_fallback(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    _open(db, shadow)

    shadow.on_trades([_trade(0.49, 104.0)])  # 4 shares fill, 6 remain
    shadow.on_tick(
        now=1250.0,
        fair_by_asset={"BTC": 0.61},
        books={"up": _book(asks={0.50: 100.0})},
        params=_params(),
    )

    row = db.maker_shadow_row("BTC", 1000)
    assert abs(float(row["maker_filled"]) - 4.0) < 1e-9
    assert abs(float(row["fallback_filled"]) - 6.0) < 1e-9
    assert int(row["fallback_used"]) == 1
    assert row["status"] == "maker_then_taker"

    shadow.settle(_position(), "UP")
    settled = db.maker_shadow_row("BTC", 1000)
    # 4 * 0.49 + 6 * 0.50 + 0.07 * 0.5 * 0.5 * 6 = 5.065
    assert abs(float(settled["total_cost"]) - 5.065) < 1e-9
    assert abs(float(settled["payout"]) - 10.0) < 1e-9
    assert abs(float(settled["pnl"]) - 4.935) < 1e-9
    assert abs(float(settled["taker_pnl"]) - 4.9) < 1e-9
    assert settled["status"] == "settled"
    db.close()


def test_cancel_when_fair_drops(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    _open(db, shadow)

    shadow.on_tick(
        now=1010.0,
        fair_by_asset={"BTC": 0.50},
        books={"up": _book()},
        params=_params(),
    )
    row = db.maker_shadow_row("BTC", 1000)
    assert row["status"] == "canceled"
    assert row["cancel_reason"] == "fair_dropped"
    db.close()


def test_fallback_skipped_when_taker_edge_gone(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    _open(db, shadow)

    # fair - bid = 0.085 (maker threshold 0.08 passes) but
    # fair - effective_ask = 0.0575 < 0.07, so no taker fallback.
    shadow.on_tick(
        now=1250.0,
        fair_by_asset={"BTC": 0.575},
        books={"up": _book(asks={0.50: 100.0})},
        params=_params(),
    )
    row = db.maker_shadow_row("BTC", 1000)
    assert int(row["fallback_used"]) == 0
    assert row["status"] == "unfilled"
    assert row["cancel_reason"] == "deadline_edge_gone"
    db.close()


def test_chase_bid_requotes_upward(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    _open(db, shadow, params=_params(maker_chase_bid=True))

    new_book = _book(bids={0.50: 50.0, 0.49: 100.0}, asks={0.51: 100.0})
    shadow.on_tick(
        now=1010.0,
        fair_by_asset={"BTC": 0.62},
        books={"up": new_book},
        params=_params(maker_chase_bid=True),
    )
    row = db.maker_shadow_row("BTC", 1000)
    assert abs(float(row["quote_price"]) - 0.50) < 1e-9
    assert abs(float(row["queue_ahead"]) - 50.0) < 1e-9
    db.close()


def test_resume_active_attempt_after_restart(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    _open(db, shadow)

    resumed = MakerShadow(db)
    resumed.load_active(now=1100.0)
    assert len(resumed._attempts) == 1
    resumed.on_trades([_trade(0.49, 200.0)])
    row = db.maker_shadow_row("BTC", 1000)
    assert abs(float(row["maker_filled"]) - 10.0) < 1e-9
    db.close()


def test_settle_unfilled_signal_has_zero_maker_pnl(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    _open(db, shadow)

    shadow.settle(_position(), "DOWN")
    row = db.maker_shadow_row("BTC", 1000)
    assert abs(float(row["pnl"])) < 1e-9
    assert abs(float(row["taker_pnl"]) + 5.10) < 1e-9
    assert row["status"] == "settled"
    db.close()


def test_summarize_shadow_tracks_both_tracks(tmp_path):
    db = Database(tmp_path / "shadow.db")
    shadow = MakerShadow(db)
    _open(db, shadow)
    shadow.on_trades([_trade(0.49, 200.0)])
    shadow.settle(_position(), "UP")

    rows = db.recent_maker_shadow(50, 0)
    overall, by_asset = summarize_shadow(rows)
    assert overall["n"] == 1
    assert overall["settled"] == 1
    assert overall["maker_full"] == 1
    assert abs(overall["pnl_maker"] - 5.1) < 1e-9
    assert abs(overall["pnl_taker"] - 4.9) < 1e-9
    assert abs(overall["pnl_diff"] - 0.2) < 1e-9
    assert abs(overall["fee_saved_total"] - 0.1) < 1e-9
    assert by_asset[0]["asset"] == "BTC"
    db.close()


def test_summarize_avg_taker_price_is_share_weighted():
    rows = [
        {
            "asset": "BTC",
            "settled_ts": 1.0,
            "taker_shares": 10.0,
            "taker_price": 0.50,
            "taker_fee": 0.10,
            "total_shares": 10.0,
            "maker_filled": 10.0,
            "fallback_filled": 0.0,
            "pnl": 5.0,
            "taker_pnl": 5.0,
        },
        {
            "asset": "BTC",
            "settled_ts": 1.0,
            "taker_shares": 20.0,
            "taker_price": 0.80,
            "taker_fee": 0.20,
            "total_shares": 0.0,
            "maker_filled": 0.0,
            "fallback_filled": 0.0,
            "pnl": 0.0,
            "taker_pnl": -5.0,
        },
    ]

    overall, _ = summarize_shadow(rows)

    # (0.50 * 10 + 0.80 * 20) / 30 -- not 21.0 / the last row's 20 shares.
    assert abs(overall["avg_taker_price"] - 0.70) < 1e-9
    assert abs(overall["fee_saved_total"] - 0.10) < 1e-9


def test_walk_asks_buys_a_share_target():
    fill = walk_asks_shares([(0.50, 3.0), (0.60, 10.0)], 8.0, 0.07)
    assert fill is not None
    shares, notional, fee = fill
    assert abs(shares - 8.0) < 1e-9
    assert abs(notional - (3 * 0.50 + 5 * 0.60)) < 1e-9
    assert abs(fee - (0.07 * 0.25 * 3 + 0.07 * 0.24 * 5)) < 1e-9


def test_clob_feed_queues_trade_prints():
    feed = ClobWsFeed(["BTC"], "wss://example.invalid")
    feed._handle(
        json.dumps(
            {
                "event_type": "last_trade_price",
                "asset_id": "up",
                "price": "0.49",
                "size": "12.5",
                "side": "SELL",
                "timestamp": "1793931310982",
            }
        )
    )
    events = feed.drain_trades()
    assert len(events) == 1
    assert events[0]["token_id"] == "up"
    assert events[0]["side"] == "SELL"
    assert events[0]["price"] == 0.49
    assert events[0]["size"] == 12.5
    assert abs(events[0]["exchange_ts"] - 1793931310.982) < 1e-6
    assert feed.drain_trades() == []
