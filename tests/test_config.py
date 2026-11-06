from src.config import RuntimeConfig, StrategyParams


class _FakeDB:
    def __init__(self):
        self.data = {}

    def get_setting(self, key):
        return self.data.get(key)

    def set_setting(self, key, value):
        self.data[key] = value


def test_strategy_defaults_are_sane():
    params = StrategyParams()
    assert 0 < params.min_edge < 1
    assert 0 < params.model_buffer < 1
    assert params.max_position_usd > 0
    assert params.entry_min_rel_s < params.entry_max_rel_s
    assert params.trading_mode == "paper"
    assert params.trading_enabled is True


def test_runtime_config_persists_and_resets():
    db = _FakeDB()
    runtime = RuntimeConfig(db)
    original = runtime.params.min_edge
    runtime.update({"min_edge": 0.08, "max_position_usd": 7.0})
    assert runtime.params.min_edge == 0.08
    assert runtime.params.max_position_usd == 7.0

    reloaded = RuntimeConfig(db)
    assert reloaded.params.min_edge == 0.08

    reloaded.reset()
    assert reloaded.params.min_edge == original


def test_runtime_config_ignores_unknown_keys():
    db = _FakeDB()
    runtime = RuntimeConfig(db)
    runtime.update({"not_a_field": 123})
    assert runtime.params.min_edge > 0


def test_trading_mode_is_validated_and_persisted():
    db = _FakeDB()
    runtime = RuntimeConfig(db)
    runtime.update({"trading_mode": "live"}, asset="BTC")
    assert runtime.params_for("BTC").trading_mode == "live"

    reloaded = RuntimeConfig(db)
    assert reloaded.params_for("BTC").trading_mode == "live"
    assert reloaded.params_for("ETH").trading_mode == "paper"

    reloaded.reset("BTC")
    assert reloaded.params_for("BTC").trading_mode == "paper"
