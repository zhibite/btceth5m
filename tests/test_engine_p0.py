from src.market.models import WindowMarket
from src.strategy.engine import StrategyEngine


class _DB:
    def __init__(self):
        self.calls = []

    def set_window_p0(self, asset, start_ts, p0, source):
        self.calls.append((asset, int(start_ts), float(p0), source))


def test_persist_p0_writes_once_per_value_and_source():
    engine = StrategyEngine.__new__(StrategyEngine)
    engine.db = _DB()
    engine._p0_written = {}

    market = WindowMarket(
        asset="BTC",
        start_ts=1000,
        end_ts=1300,
        price_to_beat=100.0,
        p0_source="prev_final",
    )
    engine._persist_p0("BTC", market, 100.0)
    engine._persist_p0("BTC", market, 100.0)
    assert engine.db.calls == [("BTC", 1000, 100.0, "prev_final")]

    # A different source for the same value is a meaningful change (the
    # dashboard shows where p0 came from), so write it once more.
    market.p0_source = "gamma"
    engine._persist_p0("BTC", market, 100.0)
    assert engine.db.calls[-1] == ("BTC", 1000, 100.0, "gamma")
    assert len(engine.db.calls) == 2
