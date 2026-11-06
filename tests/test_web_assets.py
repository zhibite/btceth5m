"""End-to-end checks for per-asset config and per-asset PnL views.

Uses the real FastAPI app with a minimal fake state so the HTTP layer,
query-parameter parsing and RuntimeConfig persistence are all exercised
together.
"""
import time
from types import SimpleNamespace

from fastapi.testclient import TestClient

from src.config import RuntimeConfig
from src.market.models import PaperPosition, WindowMarket
from src.persistence.db import Database
from src.web import app as web_app
from src.web import auth


class _FakeDB:
    def __init__(self):
        self.data = {}

    def get_setting(self, key):
        return self.data.get(key)

    def set_setting(self, key, value):
        self.data[key] = value


class _FakeRouter:
    """Minimal stand-in for BrokerRouter in the HTTP layer tests."""

    def __init__(self, runtime, live_available=False):
        self.runtime = runtime
        self.live_available = live_available
        self.live_lock_reason = "" if live_available else "live_disabled"

    @property
    def mode(self):
        modes = {self.mode_for(a) for a in ("BTC", "ETH")}
        return "live" if "live" in modes else "paper"

    def configured_mode(self, asset):
        return self.runtime.params_for(asset).trading_mode

    def mode_for(self, asset):
        return self.configured_mode(asset) if self.live_available else "paper"


def _client(tmp_path, live_available=False):
    db = Database(tmp_path / "web.db")
    runtime = RuntimeConfig(
        _FakeDB(),
        asset_defaults={"BTC": {"min_fair": 0.65, "entry_min_rel_s": 180}},
    )
    state = SimpleNamespace(
        settings=SimpleNamespace(assets_list=["BTC", "ETH"]),
        runtime=runtime,
        db=db,
        router=_FakeRouter(runtime, live_available),
        killswitch=SimpleNamespace(to_dict=lambda: {"active": False}),
        engine=SimpleNamespace(latest={}),
        rtds=SimpleNamespace(health=lambda: {}),
        books=SimpleNamespace(health=lambda: {}),
        started_at=time.time(),
    )
    web_app.set_state(state)
    client = TestClient(web_app.app)
    client.cookies.set(auth.SESSION_COOKIE, auth.create_session("tester"))
    return client, runtime, db


def _settled_position(db, asset, start_ts, pnl):
    db.upsert_window(
        WindowMarket(
            asset=asset,
            start_ts=start_ts,
            end_ts=start_ts + 300,
            slug=f"{asset.lower()}-updown-5m-{start_ts}",
            up_token="up",
            down_token="down",
            price_to_beat=100.0,
        )
    )
    pid = db.insert_position(
        PaperPosition(
            id=0,
            asset=asset,
            start_ts=start_ts,
            end_ts=start_ts + 300,
            token_id="up",
            side="UP",
            size=10.0,
            avg_price=0.5,
            cost=5.0,
            fees=0.0,
            entry_ts=start_ts + 10,
            price_to_beat=100.0,
        )
    )
    db.settle_position(pid, "UP", payout=10.0 if pnl > 0 else 0.0, pnl=pnl)


def test_config_api_per_asset_override(tmp_path):
    client, runtime, _ = _client(tmp_path)

    global_cfg = client.get("/api/config").json()
    assert global_cfg["asset"] == ""
    assert global_cfg["assets"] == ["BTC", "ETH"]
    assert global_cfg["overridden"] is False
    global_edge = global_cfg["params"]["min_edge"]

    btc_before = client.get("/api/config", params={"asset": "btc"}).json()
    assert btc_before["asset"] == "BTC"
    assert btc_before["overridden"] is False
    assert btc_before["source"] == "asset_default"
    assert btc_before["params"]["min_edge"] == global_edge
    assert btc_before["params"]["min_fair"] == 0.65
    assert btc_before["params"]["entry_min_rel_s"] == 180
    assert btc_before["asset_defaults"]["BTC"]["min_fair"] == 0.65

    saved = client.put(
        "/api/config",
        json={
            "asset": "BTC",
            "params": {"min_edge": 0.11, "max_position_usd": 3.0},
        },
    )
    assert saved.status_code == 200
    body = saved.json()
    assert body["overridden"] is True
    assert body["source"] == "override"
    assert body["params"]["min_edge"] == 0.11
    assert body["params"]["max_position_usd"] == 3.0
    # The saved snapshot keeps the BTC config default for untouched fields.
    assert body["params"]["min_fair"] == 0.65

    # ETH keeps inheriting the global values.
    eth = client.get("/api/config", params={"asset": "ETH"}).json()
    assert eth["overridden"] is False
    assert eth["source"] == "global"
    assert eth["params"]["min_edge"] == global_edge

    # A later global change only affects assets without an override.
    client.put("/api/config", json={"params": {"min_edge": 0.07}})
    assert runtime.params_for("BTC").min_edge == 0.11
    assert runtime.params_for("ETH").min_edge == 0.07

    reset = client.post("/api/config/reset", json={"asset": "BTC"}).json()
    assert reset["overridden"] is False
    assert reset["source"] == "asset_default"
    assert reset["params"]["min_edge"] == 0.07
    assert reset["params"]["min_fair"] == 0.65
    assert not runtime.has_override("BTC")


def test_status_exposes_configured_assets(tmp_path):
    client, _, _ = _client(tmp_path)
    body = client.get("/api/status").json()
    assert body["configured_assets"] == ["BTC", "ETH"]
    assert body["assets"] == {}


def test_config_api_rejects_bad_value(tmp_path):
    client, _, _ = _client(tmp_path)
    r = client.put(
        "/api/config",
        json={"asset": "BTC", "params": {"min_edge": 5.0}},
    )
    assert r.status_code == 400
    assert r.json()["detail"]


def test_config_page_has_no_global_selector(tmp_path):
    client, _, _ = _client(tmp_path)
    html = client.get("/config").text
    assert "asset-select" not in html
    assert "全局默认" not in html
    assert 'id="asset-cards"' in html


def test_config_assets_returns_one_card_per_asset(tmp_path):
    client, _, _ = _client(tmp_path)
    body = client.get("/api/config/assets").json()
    assert body["assets"] == ["BTC", "ETH"]
    assert [card["asset"] for card in body["cards"]] == ["BTC", "ETH"]
    assert body["live_available"] is False
    assert body["live_lock_reason"] == "live_disabled"
    assert "taker_fee_rate" in body["common_fields"]
    assert "min_edge" in body["per_asset_fields"]
    assert "min_edge" not in body["common_fields"]
    assert body["common_params"]["taker_fee_rate"] == 0.07
    btc = body["cards"][0]
    assert btc["source"] == "asset_default"
    assert btc["overridden"] is False
    assert btc["params"]["trading_mode"] == "paper"
    assert btc["configured_mode"] == "paper"
    assert btc["effective_mode"] == "paper"
    eth = body["cards"][1]
    assert eth["source"] == "global"
    assert eth["params"]["entry_min_rel_s"] != 180


def test_common_config_update_applies_to_every_asset(tmp_path):
    client, runtime, _ = _client(tmp_path)
    client.put(
        "/api/config",
        json={"asset": "BTC", "params": {"min_edge": 0.11}},
    )

    r = client.put("/api/config", json={"params": {"taker_fee_rate": 0.05}})
    assert r.status_code == 200
    body = r.json()
    assert body["common_params"]["taker_fee_rate"] == 0.05
    assert runtime.params_for("BTC").taker_fee_rate == 0.05
    assert runtime.params_for("ETH").taker_fee_rate == 0.05
    assert runtime.params_for("BTC").min_edge == 0.11

    # A common field cannot be overridden per asset.
    client.put(
        "/api/config",
        json={"asset": "BTC", "params": {"taker_fee_rate": 0.09}},
    )
    assert runtime.params_for("BTC").taker_fee_rate == 0.05
    assert runtime.params_for("ETH").taker_fee_rate == 0.05


def test_reset_common_keeps_per_asset_overrides(tmp_path):
    client, runtime, _ = _client(tmp_path)
    client.put(
        "/api/config",
        json={"asset": "BTC", "params": {"min_edge": 0.11}},
    )
    client.put(
        "/api/config",
        json={"params": {"taker_fee_rate": 0.05, "min_edge": 0.09}},
    )

    r = client.post("/api/config/reset", json={"scope": "common"})
    assert r.status_code == 200
    default_fee = runtime._defaults.taker_fee_rate
    assert runtime.params.taker_fee_rate == default_fee
    assert runtime.params.min_edge == 0.09
    assert runtime.params_for("BTC").min_edge == 0.11
    assert runtime.params_for("BTC").taker_fee_rate == default_fee


def test_live_mode_can_be_saved_but_stays_paper_while_locked(tmp_path):
    client, runtime, _ = _client(tmp_path)
    r = client.put(
        "/api/config",
        json={"asset": "BTC", "params": {"trading_mode": "live"}},
    )
    assert r.status_code == 200
    body = r.json()
    assert body["params"]["trading_mode"] == "live"
    assert body["configured_mode"] == "live"
    # The preference is saved, but execution stays on paper until the server
    # gates open.
    assert body["effective_mode"] == "paper"
    assert body["asset_modes"] == {"BTC": "paper", "ETH": "paper"}
    assert body["configured_modes"] == {"BTC": "live", "ETH": "paper"}
    assert runtime.params_for("BTC").trading_mode == "live"


def test_trading_can_be_toggled_per_asset(tmp_path):
    client, runtime, _ = _client(tmp_path)
    r = client.put(
        "/api/config",
        json={"asset": "BTC", "params": {"trading_enabled": False}},
    )
    assert r.status_code == 200
    assert r.json()["params"]["trading_enabled"] is False
    assert runtime.params_for("BTC").trading_enabled is False
    assert runtime.params_for("ETH").trading_enabled is True

    cards = client.get("/api/config/assets").json()["cards"]
    by_asset = {card["asset"]: card for card in cards}
    assert by_asset["BTC"]["params"]["trading_enabled"] is False
    assert by_asset["ETH"]["params"]["trading_enabled"] is True

    reset = client.post("/api/config/reset", json={"asset": "BTC"}).json()
    assert reset["params"]["trading_enabled"] is True


def test_live_mode_saves_per_asset_when_available(tmp_path):
    client, runtime, _ = _client(tmp_path, live_available=True)
    r = client.put(
        "/api/config",
        json={
            "asset": "BTC",
            "params": {"trading_mode": "live", "max_position_usd": 3.0},
        },
    )
    assert r.status_code == 200
    body = r.json()
    assert body["params"]["trading_mode"] == "live"
    assert body["configured_mode"] == "live"
    assert body["effective_mode"] == "live"
    assert body["asset_modes"] == {"BTC": "live", "ETH": "paper"}
    assert body["configured_modes"] == {"BTC": "live", "ETH": "paper"}
    assert runtime.params_for("BTC").trading_mode == "live"
    assert runtime.params_for("ETH").trading_mode == "paper"

    reset = client.post("/api/config/reset", json={"asset": "BTC"}).json()
    assert reset["params"]["trading_mode"] == "paper"
    assert reset["effective_mode"] == "paper"
    assert runtime.params_for("BTC").trading_mode == "paper"


def test_config_api_rejects_unknown_trading_mode(tmp_path):
    client, _, _ = _client(tmp_path, live_available=True)
    r = client.put(
        "/api/config",
        json={"asset": "BTC", "params": {"trading_mode": "yolo"}},
    )
    assert r.status_code == 400


def test_pnl_api_filters_by_asset(tmp_path):
    client, _, db = _client(tmp_path)
    _settled_position(db, "BTC", 1000, 4.0)
    _settled_position(db, "BTC", 1300, -1.0)
    _settled_position(db, "ETH", 1000, -2.0)

    all_pnl = client.get("/api/pnl").json()["summary"]["total"]
    assert int(all_pnl["n"]) == 3
    assert abs(float(all_pnl["pnl"]) - 1.0) < 1e-9

    btc = client.get("/api/pnl", params={"asset": "btc"}).json()["summary"]["total"]
    assert int(btc["n"]) == 2
    assert abs(float(btc["pnl"]) - 3.0) < 1e-9
    assert int(btc["wins"]) == 1

    eth = client.get("/api/pnl", params={"asset": "ETH"}).json()["summary"]["total"]
    assert int(eth["n"]) == 1
    assert abs(float(eth["pnl"]) + 2.0) < 1e-9

    # Every table endpoint accepts the same filter.
    fills = client.get("/api/fills", params={"asset": "BTC"}).json()
    assert fills["pagination"]["total"] == 0  # no fills inserted in this test
    positions = client.get("/api/positions", params={"asset": "BTC"}).json()
    assert positions["pagination"]["total"] == 2
    db.close()
