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


def test_refresh_backfills_late_p0_only_within_grace():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    market = _market(price_to_beat=None)
    assert wm.needs_refresh(market, now=1201.0)
    assert wm.needs_refresh(market, now=1260.0)
    assert not wm.needs_refresh(market, now=1260.1)
    assert not wm.needs_refresh(market, now=1400.0)


def test_p0_grace_starts_when_a_mid_window_window_is_first_seen():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    market = _market(price_to_beat=None)
    # Process restarted at 1400, 200s into the window: still worth polling
    # Gamma for a late p0 for another grace period.
    assert wm.needs_refresh(market, now=1401.0, seen_ts=1400.0)
    assert wm.needs_refresh(market, now=1460.0, seen_ts=1400.0)
    assert not wm.needs_refresh(market, now=1460.1, seen_ts=1400.0)


def test_refresh_fetches_final_price_after_window_end():
    wm = WindowManager(_DummyGamma(), ["BTC"], window_sec=300)
    market = _market(price_to_beat=100.0, final_price=None)
    assert wm.needs_refresh(market, now=1500.0)
    market.final_price = 101.0
    assert not wm.needs_refresh(market, now=1501.0)
