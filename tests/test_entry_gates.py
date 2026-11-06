"""Side-level entry gates: min_fair and min_entry_price."""
from types import SimpleNamespace

from src.config import StrategyParams
from src.execution.paper import BuyPreview
from src.market.models import WindowMarket
from src.strategy.engine import StrategyEngine


class _Book:
    updated_ts = 100.0

    def __init__(self, price: float) -> None:
        self._price = price

    def asks_ascending(self):
        return [(self._price, 100.0)]

    def best_bid(self):
        return self._price - 0.01

    def best_ask(self):
        return self._price

    def spread(self):
        return 0.01


class _Books:
    def __init__(self, price: float) -> None:
        self._price = price

    def book(self, token):
        return _Book(self._price)


class _Paper:
    def __init__(self, price: float) -> None:
        self._price = price

    def preview_buy(self, asks, notional, fee_rate):
        return BuyPreview(
            shares=10.0,
            notional=10.0 * self._price,
            avg_price=self._price,
            fee=0.0,
            best_ask=self._price,
            best_size=100.0,
            levels=[(self._price, 10.0)],
        )


def _market() -> WindowMarket:
    return WindowMarket(
        asset="BTC",
        start_ts=1000,
        end_ts=1300,
        slug="btc-updown-5m-1000",
        condition_id="0xabc",
        up_token="up",
        down_token="down",
        price_to_beat=100.0,
    )


def _params() -> StrategyParams:
    return StrategyParams(min_fair=0.65, min_entry_price=0.55)


def test_gate_reason():
    params = _params()
    assert StrategyEngine._gate_reason(params, 0.60, 0.60) == "fair"
    assert StrategyEngine._gate_reason(params, 0.70, 0.54) == "price"
    assert StrategyEngine._gate_reason(params, 0.70, 0.60) is None


def test_candidates_mark_each_gate():
    params = _params()
    engine = StrategyEngine.__new__(StrategyEngine)
    engine.books = _Books(0.50)
    engine.paper = _Paper(0.50)
    cands = StrategyEngine._candidates(engine, _market(), 0.30, 100.5, params)
    by_side = {c["side"]: c for c in cands}

    # UP fair 0.30 -> fair gate; DOWN fair 0.70 but price 0.50 -> price gate.
    assert by_side["UP"]["gate"] == "fair"
    assert by_side["DOWN"]["gate"] == "price"
    assert not any(c["eligible"] for c in cands)


def test_candidates_eligible_above_both_gates():
    params = _params()
    engine = StrategyEngine.__new__(StrategyEngine)
    engine.books = _Books(0.60)
    engine.paper = _Paper(0.60)
    cands = StrategyEngine._candidates(engine, _market(), 0.10, 100.5, params)
    by_side = {c["side"]: c for c in cands}

    # DOWN fair 0.90 and price 0.60 pass; UP is still gated on fair.
    assert by_side["UP"]["gate"] == "fair"
    assert by_side["DOWN"]["gate"] is None
    assert by_side["DOWN"]["eligible"] is True


def test_entry_check_reports_filtered_sides():
    params = _params()
    engine = StrategyEngine.__new__(StrategyEngine)
    engine.risk = SimpleNamespace(can_enter=lambda asset: (True, ""))
    engine.books = _Books(0.50)
    engine.paper = _Paper(0.50)
    market = _market()
    cands = StrategyEngine._candidates(engine, market, 0.30, 1180.0, params)

    status = StrategyEngine._entry_check(
        engine, market, 1180.0, params, None, None, {"candidates": cands}
    )
    assert status == "filtered(fair,price)"


def test_disabled_asset_does_not_enter():
    params = StrategyParams(trading_enabled=False)
    engine = StrategyEngine.__new__(StrategyEngine)
    engine.risk = SimpleNamespace(can_enter=lambda asset: (True, ""))
    market = _market()

    status = StrategyEngine._entry_check(
        engine, market, 1180.0, params, {"side": "UP"}, None, {}
    )
    assert status == "disabled"

    # An open position still reports as held so it can settle normally.
    status = StrategyEngine._entry_check(
        engine, market, 1180.0, params, {"side": "UP"}, {"id": 1}, {}
    )
    assert status == "in_position"
