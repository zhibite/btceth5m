from src.market.models import WindowMarket
from src.market.window_manager import WindowManager


class _DummyGamma:
    async def get_window(self, asset, start_ts):
        return None


def _market(**overrides):
    data = {
        "asset": "BTC",
        "start_ts": 1200,
        "end_ts": 1500,
        "price_to_beat": 100.0,
    }
    data.update(overrides)
    return WindowMarket(**data)


def test_boundary_floors_to_5m():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    assert wm.boundary(1000.0) == 900
    assert wm.boundary(1199.9) == 900
    assert wm.boundary(1200.0) == 1200
    assert wm.boundary(1499.0) == 1200


def test_no_refresh_while_open_window_already_has_p0():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    market = _market(price_to_beat=100.0)
    # final_price stays None for the whole window; that alone must not poll.
    assert not wm.needs_refresh(market, now=1201.0)
    assert not wm.needs_refresh(market, now=1499.0)


def test_refresh_backfills_late_p0_until_window_end():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    market = _market(price_to_beat=None, end_ts=1800)
    assert wm.needs_refresh(market, now=1201.0)
    assert wm.needs_refresh(market, now=1499.0)
    assert not wm.needs_refresh(market, now=1500.1)
    assert not wm.needs_refresh(market, now=1600.0)


def test_p0_grace_starts_when_a_mid_window_window_is_first_seen():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    market = _market(price_to_beat=None, end_ts=1800)
    # Process restarted at 1400, 200s into the window: still worth polling
    # Gamma for a late p0 for another grace period.
    assert wm.needs_refresh(market, now=1401.0, seen_ts=1400.0)
    assert wm.needs_refresh(market, now=1699.0, seen_ts=1400.0)
    assert not wm.needs_refresh(market, now=1700.1, seen_ts=1400.0)


def test_refresh_interval_backs_off_while_p0_is_missing():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    market = _market(price_to_beat=None, end_ts=1800)
    assert wm.refresh_interval(market, now=1201.0) == 2.0
    assert wm.refresh_interval(market, now=1261.0) == 15.0
    assert wm.refresh_interval(market, now=1499.0) == 15.0
    market.price_to_beat = 100.0
    assert wm.refresh_interval(market, now=1300.0) == 2.0


async def test_refresh_uses_previous_window_final_when_p0_is_missing():
    class _LateGamma:
        async def get_window(self, asset, start_ts):
            if start_ts == 1200:
                return _market(
                    start_ts=1200,
                    end_ts=1500,
                    price_to_beat=None,
                )
            if start_ts == 900:
                return _market(
                    start_ts=900,
                    end_ts=1200,
                    price_to_beat=100.0,
                    final_price=101.0,
                )
            return None

    wm = WindowManager(_LateGamma(), ["BTC"], window_sec=300)
    market = _market(start_ts=1200, end_ts=1500, price_to_beat=None)
    wm.current["BTC"] = market
    wm._window_seen["BTC"] = 1200.0
    await wm._refresh_asset("BTC", 1200, 1265.0)
    assert market.price_to_beat == 101.0
    assert market.p0_source == "prev_final"


async def test_previous_window_final_fills_missing_p0():
    class _PrevGamma:
        async def get_window(self, asset, start_ts):
            if asset == "BTC" and start_ts == 900:
                return _market(
                    start_ts=900,
                    end_ts=1200,
                    price_to_beat=100.0,
                    final_price=101.0,
                )
            return None

    wm = WindowManager(_PrevGamma(), ["BTC"], window_sec=300)
    market = _market(start_ts=1200, end_ts=1500, price_to_beat=None)
    await wm._p0_from_previous_final("BTC", market, 1200)
    assert market.price_to_beat == 101.0
    assert market.p0_source == "prev_final"


def test_refresh_fetches_final_price_after_window_end():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    market = _market(price_to_beat=100.0, final_price=None)
    assert wm.needs_refresh(market, now=1500.0)
    market.final_price = 101.0
    assert not wm.needs_refresh(market, now=1501.0)
