"""Multi-coin wiring: RTDS multiplexing, CLOB trimming, enabled defaults.

The shipped config adds BNB/SOL/XRP/DOGE/HYPE next to BTC/ETH but starts the
new coins disabled. These tests pin both the WebSocket fan-out design and that
default so a future edit cannot silently subscribe seven heavy CLOB books.
"""
from __future__ import annotations

import asyncio
import json
import time
from types import SimpleNamespace

from src.config import RuntimeConfig, get_asset_strategy_defaults
from src.feeds.clob_ws import ClobWsFeed
from src.feeds.rtds import RtdsFeed, SYMBOLS
from src.market.models import WindowMarket
from src.recording.recorder import Recorder
from src.strategy.engine import StrategyEngine


class _FakeDB:
    def __init__(self):
        self.data = {}

    def get_setting(self, key):
        return self.data.get(key)

    def set_setting(self, key, value):
        self.data[key] = value


class _InactiveBooks:
    """Book source whose active set excludes ETH."""

    active_assets = {"BTC"}
    books = {}

    def book(self, token):
        return self.books.get(token)


class _Windows:
    def __init__(self, markets):
        self.current = {m.asset: m for m in markets}

    def current_window(self, asset):
        return self.current.get(asset)


def _market(asset: str) -> WindowMarket:
    start = 1_700_000_000
    return WindowMarket(
        asset=asset,
        start_ts=start,
        end_ts=start + 300,
        slug=f"{asset.lower()}-updown-5m-{start}",
        up_token=f"{asset}-up",
        down_token=f"{asset}-down",
        price_to_beat=100.0,
    )


# ── RTDS: one connection, every symbol ───────────────────────────────────
def test_rtds_subscriptions_multiplex_every_symbol():
    assets = ["BTC", "ETH", "BNB", "SOL", "XRP", "DOGE", "HYPE"]
    feed = RtdsFeed(assets, "wss://example.invalid", twap_topic=True)

    subs = feed.subscriptions()
    assert len(subs) == len(assets) * 2
    assert {json.loads(sub["filters"])["symbol"] for sub in subs} == {
        "btc/usd", "eth/usd", "bnb/usd", "sol/usd",
        "xrp/usd", "doge/usd", "hype/usd",
    }
    assert set(SYMBOLS) == set(assets)


def test_rtds_routes_multiplexed_messages_by_symbol():
    feed = RtdsFeed(["BTC", "ETH", "SOL"], "wss://example.invalid")
    chainlink = json.dumps(
        {
            "topic": "crypto_prices_chainlink",
            "payload": {"symbol": "sol/usd", "timestamp": 1000, "value": 123.4},
        }
    )
    twap = json.dumps(
        {
            "topic": "crypto_prices_twap_sixty",
            "payload": {"symbol": "sol/usd", "timestamp": 1000, "value": 999.0},
        }
    )

    feed._handle_any(chainlink, 5.0)
    feed._handle_any(twap, 6.0)

    assert feed.buffer("SOL").points() == 1
    assert feed.buffer("SOL").latest_price() == 123.4
    assert feed.buffer("BTC").points() == 0
    assert feed.buffer("ETH").points() == 0
    assert feed.messages["SOL"] == 2
    assert feed.last_msg_ts["SOL"] == 6.0


def test_rtds_accepts_raw_backfill_but_never_twap():
    feed = RtdsFeed(["BTC"], "wss://example.invalid")
    backfill = json.dumps(
        {
            "topic": "crypto_prices",
            "payload": {
                "symbol": "btc/usd",
                "data": [
                    {"timestamp": 1000, "value": 100.0},
                    {"timestamp": 2000, "value": 101.0},
                ],
            },
        }
    )
    twap = json.dumps(
        {
            "topic": "crypto_prices_twap_sixty",
            "payload": {"symbol": "btc/usd", "timestamp": 3000, "value": 999.0},
        }
    )
    feed._handle_any(backfill, 3.0)
    feed._handle_any(twap, 4.0)

    assert feed.buffer("BTC").points() == 2
    assert feed.buffer("BTC").latest_price() == 101.0


def test_rtds_watchdog_tracks_raw_stream_only():
    feed = RtdsFeed(
        ["BTC", "ETH"],
        "wss://example.invalid",
        data_timeout_s=30,
        watch_assets=["BTC", "ETH"],
    )
    chainlink = json.dumps(
        {
            "topic": "crypto_prices_chainlink",
            "payload": {"symbol": "btc/usd", "timestamp": 1000, "value": 100.0},
        }
    )
    twap = json.dumps(
        {
            "topic": "crypto_prices_twap_sixty",
            "payload": {"symbol": "btc/usd", "timestamp": 2000, "value": 999.0},
        }
    )

    feed._handle_any(chainlink, 100.0)
    assert feed.last_raw_ts["BTC"] == 100.0
    # TWAP traffic keeps the socket busy but does not feed the strategy.
    feed._handle_any(twap, 200.0)
    assert feed.last_msg_ts["BTC"] == 200.0
    assert feed.last_raw_ts["BTC"] == 100.0

    assert not feed._raw_stale("BTC", now=129.0)
    assert feed._raw_stale("BTC", now=131.0)
    assert feed.stale_assets(now=131.0) == ["BTC"]
    # ETH has not produced a print yet; a fresh connection must not be
    # treated as stale before its first message.
    assert not feed._raw_stale("ETH", now=131.0)


def test_rtds_watch_assets_can_change_at_runtime():
    feed = RtdsFeed(["BTC", "ETH", "SOL"], "wss://example.invalid")
    feed.set_watch_assets(["ETH", "UNKNOWN"])
    assert feed.watch_assets == ["ETH"]
    feed.set_watch_assets([])
    assert feed.watch_assets == []


def test_rtds_watchdog_accepts_raw_backfill_as_data():
    feed = RtdsFeed(
        ["BTC"],
        "wss://example.invalid",
        data_timeout_s=30,
        watch_assets=["BTC"],
    )
    backfill = json.dumps(
        {
            "topic": "crypto_prices",
            "payload": {
                "symbol": "btc/usd",
                "data": [{"timestamp": 1000, "value": 100.0}],
            },
        }
    )
    feed._handle_any(backfill, 150.0)
    assert feed.last_raw_ts["BTC"] == 150.0
    assert not feed._raw_stale("BTC", now=179.0)
    assert feed._raw_stale("BTC", now=181.0)


def test_rtds_single_asset_falls_back_when_symbol_is_missing():
    feed = RtdsFeed(["BTC"], "wss://example.invalid")
    msg = json.dumps(
        {
            "topic": "crypto_prices_chainlink",
            "payload": {"timestamp": 1000, "value": 100.0},
        }
    )
    feed._handle_any(msg, 1.0)
    assert feed.buffer("BTC").points() == 1


# ── CLOB: only active coins keep their book subscriptions ────────────────
def test_clob_active_assets_trim_the_token_union():
    feed = ClobWsFeed(["BTC", "ETH", "SOL"], "wss://example.invalid")
    feed.set_tokens("BTC", ["b1", "b2"])
    feed.set_tokens("ETH", ["e1", "e2"])
    feed.set_tokens("SOL", ["s1", "s2"])
    assert feed._token_union() == ["b1", "b2", "e1", "e2", "s1", "s2"]

    feed.set_active_assets({"BTC", "SOL"})
    assert feed.active_assets == {"BTC", "SOL"}
    assert feed._token_union() == ["b1", "b2", "s1", "s2"]
    health = feed.health()
    assert health["active_assets"] == ["BTC", "SOL"]


def test_clob_prunes_books_from_the_finished_window():
    feed = ClobWsFeed(["BTC"], "wss://example.invalid")
    feed.set_tokens("BTC", ["old-up", "old-down"])
    feed.books["stray"] = object()
    feed.set_tokens("BTC", ["new-up", "new-down"])

    assert "old-up" not in feed.books
    assert "old-down" not in feed.books
    assert "stray" not in feed.books
    assert {"new-up", "new-down"} <= set(feed.books)


def test_clob_disable_drops_books_and_enable_restores_them():
    feed = ClobWsFeed(["BTC", "ETH"], "wss://example.invalid")
    feed.set_tokens("BTC", ["b1"])
    feed.set_tokens("ETH", ["e1"])
    assert {"b1", "e1"} <= set(feed.books)

    feed.set_active_assets({"BTC"})
    assert "b1" in feed.books
    assert "e1" not in feed.books

    # Re-enabling recreates the book objects from the stored token ids.
    feed.set_active_assets({"BTC", "ETH"})
    assert "e1" in feed.books


def test_clob_active_set_change_bumps_version_once():
    feed = ClobWsFeed(["BTC", "ETH"], "wss://example.invalid")
    version = feed._version
    feed.set_active_assets({"BTC"})
    assert feed._version == version + 1
    feed.set_active_assets({"BTC"})
    assert feed._version == version + 1


# ── Enabled-by-default policy and engine active set ──────────────────────
def test_shipped_new_coins_start_disabled():
    defaults = get_asset_strategy_defaults()
    for asset in ("BNB", "SOL", "XRP", "DOGE", "HYPE"):
        assert defaults[asset]["trading_enabled"] is False

    runtime = RuntimeConfig(_FakeDB())
    assert runtime.params_for("BTC").trading_enabled is True
    assert runtime.params_for("BNB").trading_enabled is False
    assert runtime.enabled_assets(["BTC", "ETH", "BNB", "SOL"]) == ["BTC", "ETH"]

    runtime.update({"trading_enabled": True}, asset="BNB")
    assert runtime.enabled_assets(["BTC", "BNB", "SOL"]) == ["BTC", "BNB"]


def test_engine_active_set_keeps_open_position_assets():
    runtime = RuntimeConfig(_FakeDB())
    runtime.update({"trading_enabled": False}, asset="BTC")
    engine = SimpleNamespace(
        settings=SimpleNamespace(assets_list=["BTC", "ETH", "BNB"]),
        runtime=runtime,
        positions={("BTC", 1000): SimpleNamespace(asset="BTC")},
    )
    # BTC is disabled but still holds a position; ETH is enabled; BNB off.
    assert StrategyEngine._active_assets(engine) == {"BTC", "ETH"}


# ── Recorder: disabled coins do not write empty book snapshots ───────────
async def _wait_written(recorder: Recorder, n: int, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while recorder.written < n and time.time() < deadline:
        await asyncio.sleep(0.01)
    assert recorder.written >= n


async def test_recorder_skips_book_snapshots_for_inactive_assets(tmp_path):
    btc, eth = _market("BTC"), _market("ETH")
    recorder = Recorder(
        tmp_path,
        ["BTC", "ETH"],
        books=_InactiveBooks(),
        windows=_Windows([btc, eth]),
        book_hz=0.1,
        book_levels=1,
        compress=False,
        retention_days=0,
    )
    recorder.start()
    try:
        recorder._sample_books()
        await _wait_written(recorder, 1)
    finally:
        await recorder.stop()

    names = {path.name.split("_")[1] for path in tmp_path.glob("book_*.jsonl")}
    assert names == {"BTC"}
