"""Per-asset paper/live selection and the global live gate."""
import asyncio
from types import SimpleNamespace

from src.config import RuntimeConfig
from src.execution.router import BrokerRouter
from src.market.models import WindowMarket


class _FakeDB:
    def __init__(self):
        self.data = {}

    def get_setting(self, key):
        return self.data.get(key)

    def set_setting(self, key, value):
        self.data[key] = value


class _Paper:
    def __init__(self):
        self.calls = []

    def buy(self, **kwargs):
        self.calls.append(kwargs)
        return "paper-fill"


class _Live:
    def __init__(self, configured=True, ready=True):
        self.calls = []
        self.configured = configured
        self.order_placement_ready = ready

    async def buy(self, **kwargs):
        self.calls.append(kwargs)
        return "live-fill"


def _settings(**overrides):
    values = {
        "live_enabled": True,
        "paper_trading": False,
        "assets_list": ["BTC", "ETH"],
    }
    values.update(overrides)
    return SimpleNamespace(**values)


def _runtime():
    runtime = RuntimeConfig(_FakeDB())
    runtime.update({"trading_mode": "live"}, asset="BTC")
    return runtime


def _buy(router, asset="BTC"):
    market = WindowMarket(asset=asset, start_ts=0, end_ts=300)
    return asyncio.run(
        router.buy(
            market=market,
            side="UP",
            token_id="token",
            notional=5.0,
            asks=[(0.5, 10.0)],
            price_to_beat=100.0,
            reason="test",
        )
    )


def test_mode_for_requires_global_live_gate():
    runtime = _runtime()

    disabled = BrokerRouter(_settings(live_enabled=False), _Paper(), _Live(), runtime)
    assert disabled.live_available is False
    assert disabled.live_lock_reason == "live_disabled"
    assert disabled.mode_for("BTC") == "paper"
    assert disabled.mode == "paper"

    forced_paper = BrokerRouter(_settings(paper_trading=True), _Paper(), _Live(), runtime)
    assert forced_paper.live_available is False
    assert forced_paper.live_lock_reason == "paper_trading"
    assert forced_paper.mode_for("BTC") == "paper"

    no_client = BrokerRouter(_settings(), _Paper(), None, runtime)
    assert no_client.live_available is False
    assert no_client.live_lock_reason == "live_client_missing"
    assert no_client.mode_for("BTC") == "paper"

    no_creds = BrokerRouter(_settings(), _Paper(), _Live(configured=False), runtime)
    assert no_creds.live_available is False
    assert no_creds.live_lock_reason == "credentials_missing"
    assert no_creds.mode_for("BTC") == "paper"

    not_implemented = BrokerRouter(
        _settings(), _Paper(), _Live(ready=False), runtime
    )
    assert not_implemented.live_available is False
    assert not_implemented.live_lock_reason == "live_not_implemented"
    assert not_implemented.mode_for("BTC") == "paper"


def test_mode_is_resolved_per_asset_when_live_is_available():
    runtime = _runtime()
    router = BrokerRouter(_settings(), _Paper(), _Live(), runtime)

    assert router.live_available is True
    assert router.live_lock_reason == ""
    assert router.configured_mode("BTC") == "live"
    assert router.mode_for("BTC") == "live"
    assert router.mode_for("ETH") == "paper"
    assert router.mode == "live"


def test_buy_dispatches_to_the_asset_broker():
    runtime = _runtime()
    paper = _Paper()
    live = _Live()
    router = BrokerRouter(_settings(), paper, live, runtime)

    assert _buy(router, "BTC") == "live-fill"
    assert len(live.calls) == 1
    assert paper.calls == []

    assert _buy(router, "ETH") == "paper-fill"
    assert len(paper.calls) == 1
    assert len(live.calls) == 1


def test_locked_live_mode_never_falls_through_to_live():
    runtime = _runtime()
    paper = _Paper()
    live = _Live()
    router = BrokerRouter(
        _settings(live_enabled=False), paper, live, runtime
    )

    # The saved BTC mode is still "live", but the gate forces paper.
    assert router.configured_mode("BTC") == "live"
    assert _buy(router, "BTC") == "paper-fill"
    assert live.calls == []
