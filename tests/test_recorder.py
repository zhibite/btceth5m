import asyncio
import gzip
import json
import os
import time

import pytest

from src.config import AppSettings
from src.feeds.clob_ws import ClobWsFeed
from src.feeds.rtds import RtdsFeed
from src.market.models import OrderBook, WindowMarket
from src.recording.recorder import Recorder


class _Books:
    def __init__(self):
        self.books = {}

    def book(self, token):
        return self.books.get(token)


class _Windows:
    def __init__(self):
        self.current = {}

    def current_window(self, asset):
        return self.current.get(asset)


def _market(start_ts: int = 1_700_000_000) -> WindowMarket:
    return WindowMarket(
        asset="BTC",
        start_ts=start_ts,
        end_ts=start_ts + 300,
        slug=f"btc-updown-5m-{start_ts}",
        condition_id="0xabc",
        up_token="up",
        down_token="down",
        price_to_beat=100.0,
        volume=1234.0,
    )


async def _wait_written(recorder: Recorder, n: int, timeout: float = 2.0) -> None:
    deadline = time.time() + timeout
    while recorder.written < n and time.time() < deadline:
        await asyncio.sleep(0.01)
    assert recorder.written >= n


def _read(path) -> list[dict]:
    return [
        json.loads(line)
        for line in path.read_text(encoding="utf-8").splitlines()
        if line.strip()
    ]


async def test_recorder_writes_tapped_streams(tmp_path):
    recorder = Recorder(tmp_path, ["BTC"], compress=False, retention_days=0)
    recorder.start()
    try:
        market = _market()
        recorder.record_window(market)
        recorder.record_rtds(
            "BTC",
            1_700_000_001.5,
            {
                "topic": "crypto_prices_chainlink",
                "payload": {"symbol": "btc/usd", "timestamp": 1700000001000, "value": 100.0},
            },
        )
        recorder.record_trade(
            {
                "token_id": "up",
                "side": "BUY",
                "price": 0.55,
                "size": 10.0,
                "ts": 1_700_000_002.0,
                "exchange_ts": 1_700_000_001.9,
            }
        )
        await _wait_written(recorder, 3)
    finally:
        await recorder.stop()

    gamma = _read(next(tmp_path.glob("gamma_*.jsonl")))[0]["payload"]
    assert gamma["slug"] == market.slug
    assert gamma["tokens"] == ["up", "down"]
    assert gamma["price_to_beat"] == 100.0

    rtds = _read(next(tmp_path.glob("rtds_*.jsonl")))[0]
    assert rtds["payload"]["topic"] == "crypto_prices_chainlink"
    assert rtds["recv_ts"] == 1_700_000_001.5

    trade = _read(next(tmp_path.glob("trades_BTC_*.jsonl")))[0]["payload"]
    assert trade["asset_id"] == "up"
    assert trade["price"] == 0.55
    assert trade["timestamp"] == 1_700_000_001_900
    assert trade["recv_ts"] == 1_700_000_002.0


async def test_recorder_book_snapshot_shape(tmp_path):
    books, windows = _Books(), _Windows()
    market = _market()
    windows.current["BTC"] = market
    up = OrderBook("up")
    up.apply_snapshot(
        [{"price": "0.40", "size": "10"}],
        [{"price": "0.60", "size": "5"}],
        ts=1.0,
    )
    down = OrderBook("down")
    down.apply_snapshot(
        [{"price": "0.38", "size": "7"}],
        [{"price": "0.62", "size": "9"}],
        ts=1.0,
    )
    books.books = {"up": up, "down": down}

    recorder = Recorder(
        tmp_path,
        ["BTC"],
        books=books,
        windows=windows,
        book_hz=0.1,
        book_levels=2,
        compress=False,
        retention_days=0,
    )
    recorder.start()
    try:
        recorder._sample_books()
        await _wait_written(recorder, 1)
    finally:
        await recorder.stop()

    payload = _read(next(tmp_path.glob("book_BTC_*.jsonl")))[0]["payload"]
    assert payload["slug"] == market.slug
    assert payload["books"]["up"]["bids"] == [[0.40, 10.0]]
    assert payload["books"]["up"]["asks"] == [[0.60, 5.0]]
    assert payload["books"]["down"]["bids"] == [[0.38, 7.0]]


async def test_recorder_heals_a_partial_line_after_crash(tmp_path):
    hour = time.strftime("%Y%m%d_%H", time.gmtime())
    path = tmp_path / f"rtds_{hour}.jsonl"
    path.write_text('{"partial":', encoding="utf-8")  # no trailing newline

    recorder = Recorder(tmp_path, ["BTC"], compress=False, retention_days=0)
    recorder.start()
    try:
        recorder.record_rtds("BTC", time.time(), {"topic": "x"})
        await _wait_written(recorder, 1)
    finally:
        await recorder.stop()

    lines = path.read_text(encoding="utf-8").splitlines()
    assert lines[0] == '{"partial":'
    assert json.loads(lines[-1])["payload"] == {"topic": "x"}


async def test_maintenance_compresses_and_expires(tmp_path):
    old = tmp_path / "rtds_20200101_00.jsonl"
    old.write_text('{"recv_ts":1,"source":"rtds","payload":{"x":1}}\n', encoding="utf-8")
    expired = tmp_path / "book_BTC_20200101_01.jsonl"
    expired.write_text("{}\n", encoding="utf-8")
    stale = time.time() - 10 * 86400
    os.utime(expired, (stale, stale))

    recorder = Recorder(tmp_path, ["BTC"], compress=True, retention_days=1)
    recorder._maintain()

    assert not old.exists()
    assert not expired.exists()
    compressed = tmp_path / (old.name + ".gz")
    assert compressed.exists()
    with gzip.open(compressed, "rt", encoding="utf-8") as handle:
        assert json.loads(handle.readline())["payload"] == {"x": 1}


def test_recording_path_defaults_to_data_live(tmp_path):
    settings = AppSettings(data_dir=str(tmp_path), recording_dir="")
    assert settings.recording_path == tmp_path / "live"


def test_rtds_tap_records_twap_but_keeps_buffer_clean():
    seen = []
    feed = RtdsFeed(
        ["BTC"],
        "wss://example.invalid",
        on_message=lambda asset, ts, data: seen.append((asset, ts, data)),
        twap_topic=True,
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
            "payload": {"symbol": "btc/usd", "timestamp": 1000, "value": 200.0},
        }
    )

    feed._handle("BTC", chainlink, 1.0)
    feed._handle("BTC", twap, 2.0)

    assert [item[2]["topic"] for item in seen] == [
        "crypto_prices_chainlink",
        "crypto_prices_twap_sixty",
    ]
    buffer = feed.buffer("BTC")
    assert buffer.points() == 1
    assert buffer.latest_price() == 100.0


def test_clob_trade_tap_receives_normalized_event():
    seen = []
    feed = ClobWsFeed(["BTC"], "wss://example.invalid", on_trade=seen.append)
    feed.set_tokens("BTC", ["up", "down"])
    message = json.dumps(
        [
            {
                "event_type": "last_trade_price",
                "asset_id": "up",
                "price": "0.55",
                "size": "10",
                "side": "BUY",
                "timestamp": 1700000000000,
            }
        ]
    )

    feed._handle(message)

    assert len(seen) == 1
    event = seen[0]
    assert event["token_id"] == "up"
    assert event["price"] == pytest.approx(0.55)
    assert event["exchange_ts"] == pytest.approx(1_700_000_000.0)
