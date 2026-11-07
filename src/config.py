"""Configuration: config.toml defaults, .env / process env overrides.

Precedence (low -> high):
    code defaults < config.toml < .env < process environment < explicit kwargs

Secrets (CLOB keys) are only read from the environment / .env; they are never
written to config.toml or returned by the dashboard API.
"""
from __future__ import annotations

import json
import os
import tomllib
from functools import lru_cache
from pathlib import Path
from typing import Any, Literal

from dotenv import dotenv_values
from pydantic import BaseModel, ConfigDict, Field
from pydantic_settings import BaseSettings, SettingsConfigDict

ROOT = Path(__file__).resolve().parent.parent
_TOML_PATH = ROOT / "config.toml"


@lru_cache(maxsize=1)
def _toml() -> dict:
    if not _TOML_PATH.exists():
        return {}
    with _TOML_PATH.open("rb") as f:
        return tomllib.load(f)


def _table(name: str) -> dict[str, Any]:
    """Case-insensitive top-level TOML table lookup.

    config.toml has always used upper-case table names (``[STRATEGY]``), so a
    plain ``_toml().get(name.lower())`` lookup silently ignored the whole file.
    Accept either casing.
    """
    for key, value in _toml().items():
        if str(key).lower() == name.lower() and isinstance(value, dict):
            return value
    return {}


def _section(name: str) -> dict[str, Any]:
    return {str(k).upper(): v for k, v in _table(name).items()}


@lru_cache(maxsize=1)
def _env() -> dict[str, str]:
    values: dict[str, str] = {}
    env_file = ROOT / ".env"
    if env_file.exists():
        for k, v in dotenv_values(env_file).items():
            if v is not None:
                values[str(k).upper()] = str(v)
    # Process environment wins over .env.
    values.update({str(k).upper(): v for k, v in os.environ.items()})
    return values


class AppSettings(BaseSettings):
    """Deployment / feed / web settings. Strategy knobs live in StrategyParams."""

    model_config = SettingsConfigDict(env_file=(str(ROOT / ".env"),), extra="ignore")

    assets: str = "BTC,ETH"
    window_sec: int = 300
    twap_window_s: int = 60

    rtds_url: str = "wss://ws-live-data.polymarket.com"
    clob_ws_url: str = "wss://ws-subscriptions-clob.polymarket.com/ws/market"
    gamma_api: str = "https://gamma-api.polymarket.com"
    clob_api: str = "https://clob.polymarket.com"
    price_buffer_s: int = 900
    reconnect_base_s: float = 1.0
    reconnect_max_s: float = 30.0

    web_host: str = "127.0.0.1"
    web_port: int = 8036
    web_username: str = "admin"
    web_password: str = ""
    web_session_ttl_s: int = 7 * 24 * 3600
    web_enabled: bool = True

    paper_trading: bool = True
    live_enabled: bool = False

    # Market-data recording for backtest/replay (see src/recording/recorder.py).
    recording_enabled: bool = True
    recording_dir: str = ""  # empty -> <data_dir>/live
    recording_book_hz: float = 1.0
    recording_book_levels: int = 5
    recording_raw_clob: bool = False
    recording_twap: bool = True
    recording_gzip: bool = True
    recording_retention_days: int = 14
    recording_queue_size: int = 50_000

    data_dir: str = str(ROOT / "data")
    log_level: str = "INFO"

    # Live CLOB credentials (never exposed by the API).
    api_key: str = ""
    api_secret: str = ""
    api_passphrase: str = ""
    wallet_private_key: str = ""
    wallet_address: str = ""
    funder_address: str = ""
    signature_type: int = 0

    @property
    def assets_list(self) -> list[str]:
        return [a.strip().upper() for a in self.assets.split(",") if a.strip()]

    @property
    def db_path(self) -> Path:
        return Path(self.data_dir) / "poly5m.db"

    @property
    def log_dir(self) -> Path:
        return Path(self.data_dir) / "logs"

    @property
    def recording_path(self) -> Path:
        """Where hourly JSONL recordings are written.

        An empty ``RECORDING_DIR`` uses ``<DATA_DIR>/live`` so the recordings
        live on the same persistent volume as the SQLite database. Relative
        paths are resolved from the repository root.
        """
        raw = str(self.recording_dir or "").strip()
        path = Path(raw) if raw else Path(self.data_dir) / "live"
        if not path.is_absolute():
            path = ROOT / path
        return path


class StrategyParams(BaseModel):
    """Runtime-editable strategy and risk knobs."""

    model_config = ConfigDict(extra="ignore")

    # Entry: fair - executable_ask - fee >= min_edge + model_buffer
    min_edge: float = Field(0.05, ge=0.0, le=1.0)
    model_buffer: float = Field(0.02, ge=0.0, le=1.0)
    # Side-level gates, applied before the edge threshold:
    #   * min_fair          — skip a side whose model probability is too low
    #                         (filters lottery tickets around fair≈0.5).
    #   * min_entry_price   — skip a side whose all-in entry price is too cheap;
    #                         a cheap entry means the market disagrees with the
    #                         model, which is dangerous when the model is
    #                         overconfident.
    min_fair: float = Field(0.0, ge=0.0, le=1.0)
    min_entry_price: float = Field(0.0, ge=0.0, le=1.0)
    max_position_usd: float = Field(5.0, gt=0.0, le=100000.0)
    daily_loss_limit_usd: float = Field(20.0, gt=0.0)
    entry_min_rel_s: int = Field(60, ge=0, le=299)
    entry_max_rel_s: int = Field(285, ge=1, le=300)
    max_spread: float = Field(0.03, ge=0.0, le=1.0)
    max_stale_book_s: float = Field(3.0, ge=0.1, le=60.0)
    min_ask_shares: float = Field(5.0, ge=0.0)
    min_order_shares: float = Field(5.0, ge=0.0)
    taker_fee_rate: float = Field(0.07, ge=0.0, le=0.2)
    sigma_lookback_s: int = Field(600, ge=60, le=3600)
    min_sigma_samples: int = Field(120, ge=30, le=3000)
    engine_tick_s: float = Field(0.5, ge=0.1, le=60.0)
    signal_log_min_edge: float = Field(0.02, ge=0.0, le=1.0)

    # Execution mode for this asset. "live" is only honoured when the process
    # runs with LIVE_ENABLED=true and PAPER_TRADING=false; otherwise the router
    # forces paper and the config page shows the reason.
    trading_mode: Literal["paper", "live"] = "paper"
    # Whether this asset may open new positions. Open positions always settle
    # normally; disabling only stops new entries.
    trading_enabled: bool = True

    # Maker shadow: simulate a maker-first policy next to the real taker
    # paper fill. It never changes the paper position or PnL; it only records
    # the counterfactual for comparison.
    maker_shadow_enabled: bool = True
    maker_buffer: float = Field(0.01, ge=0.0, le=0.5)
    maker_fallback_rel_s: int = Field(250, ge=0, le=300)
    maker_chase_bid: bool = True


# Parameters that describe the engine, the protocol, the shared fair-value
# model or the maker-shadow experiment rather than one coin's trading stance.
# They always come from the global params: a per-asset override may still be
# stored (and is what the config page shows for other fields), but these
# fields are ignored there so there is only one source of truth.
COMMON_PARAM_FIELDS: tuple[str, ...] = (
    "max_stale_book_s",
    "min_order_shares",
    "taker_fee_rate",
    "sigma_lookback_s",
    "min_sigma_samples",
    "engine_tick_s",
    "signal_log_min_edge",
    "maker_shadow_enabled",
    "maker_buffer",
    "maker_fallback_rel_s",
    "maker_chase_bid",
)

PER_ASSET_PARAM_FIELDS: tuple[str, ...] = tuple(
    name for name in StrategyParams.model_fields if name not in COMMON_PARAM_FIELDS
)


def _toml_settings() -> dict[str, Any]:
    g, f, w, e = (
        _section("GENERAL"),
        _section("FEED"),
        _section("WEB"),
        _section("EXECUTION"),
    )
    r = _section("RECORDING")
    return {
        "assets": g.get("ASSETS", "BTC,ETH"),
        "window_sec": g.get("WINDOW_SEC", 300),
        "twap_window_s": g.get("TWAP_WINDOW_S", 60),
        "rtds_url": f.get("RTDS_URL", "wss://ws-live-data.polymarket.com"),
        "clob_ws_url": f.get(
            "CLOB_WS_URL",
            "wss://ws-subscriptions-clob.polymarket.com/ws/market",
        ),
        "gamma_api": f.get("GAMMA_API", "https://gamma-api.polymarket.com"),
        "clob_api": f.get("CLOB_API", "https://clob.polymarket.com"),
        "price_buffer_s": f.get("PRICE_BUFFER_S", 900),
        "reconnect_base_s": f.get("RECONNECT_BASE_S", 1.0),
        "reconnect_max_s": f.get("RECONNECT_MAX_S", 30.0),
        "web_host": w.get("HOST", "127.0.0.1"),
        "web_port": w.get("PORT", 8036),
        "web_username": w.get("USERNAME", "admin"),
        "web_password": w.get("PASSWORD", ""),
        "web_session_ttl_s": w.get("SESSION_TTL_S", 7 * 24 * 3600),
        "paper_trading": e.get("PAPER_TRADING", True),
        "live_enabled": e.get("LIVE_ENABLED", False),
        "recording_enabled": r.get("ENABLED", True),
        "recording_dir": r.get("DIR", ""),
        "recording_book_hz": r.get("BOOK_HZ", 1.0),
        "recording_book_levels": r.get("BOOK_LEVELS", 5),
        "recording_raw_clob": r.get("RAW_CLOB", False),
        "recording_twap": r.get("TWAP", True),
        "recording_gzip": r.get("GZIP", True),
        "recording_retention_days": r.get("RETENTION_DAYS", 14),
        "recording_queue_size": r.get("QUEUE_SIZE", 50_000),
        "data_dir": str(ROOT / "data"),
    }


@lru_cache(maxsize=1)
def get_settings() -> AppSettings:
    data = _toml_settings()
    env = _env()
    for field in AppSettings.model_fields:
        env_name = field.upper()
        if env_name in env:
            data[field] = env[env_name]
    return AppSettings(**data)


@lru_cache(maxsize=1)
def get_strategy_defaults() -> StrategyParams:
    # Skip nested [STRATEGY.<ASSET>] tables; they are parsed separately.
    raw = {
        str(k).lower(): v
        for k, v in _section("STRATEGY").items()
        if not isinstance(v, dict)
    }
    env = _env()
    for field in StrategyParams.model_fields:
        env_name = field.upper()
        if env_name in env:
            raw[field] = env[env_name]
    return StrategyParams(**raw)


@lru_cache(maxsize=1)
def get_asset_strategy_defaults() -> dict[str, dict[str, Any]]:
    """Per-asset defaults from ``[STRATEGY.<ASSET>]`` tables in config.toml.

    The values are partial: fields that are not listed keep inheriting the
    current global default (including edits made from the dashboard). This is
    different from an ``asset_params`` override, which is a full snapshot
    persisted in SQLite and independently editable from the config page.
    """
    raw = _table("STRATEGY")
    known = set(StrategyParams.model_fields)
    base = get_strategy_defaults().model_dump()
    out: dict[str, dict[str, Any]] = {}
    for asset, section in raw.items():
        if not isinstance(section, dict):
            continue
        values = {
            str(key).lower(): value
            for key, value in section.items()
            if str(key).lower() in known
            and str(key).lower() not in COMMON_PARAM_FIELDS
        }
        if not values:
            continue
        # Fail fast at import/startup on an invalid per-asset default.
        StrategyParams.model_validate({**base, **values})
        out[str(asset).upper()] = values
    return out


class RuntimeConfig:
    """Strategy params persisted in SQLite so dashboard edits survive restarts.

    Layers, from lowest to highest precedence:
      * ``params`` — global defaults (config.toml / env / DB edit).
      * ``asset_defaults`` — partial ``[STRATEGY.<ASSET>]`` defaults from
        config.toml, merged over the *current* global params.
      * ``asset_params`` — per-asset overrides persisted in SQLite (a full
        snapshot saved from the config page).
    """

    KEY = "strategy_params"
    ASSET_KEY = "asset_strategy_params"

    def __init__(
        self,
        db: Any,
        defaults: StrategyParams | None = None,
        asset_defaults: dict[str, dict[str, Any]] | None = None,
    ) -> None:
        self._db = db
        self._defaults = defaults or get_strategy_defaults()
        self.asset_defaults = {
            str(asset).upper(): dict(values)
            for asset, values in (
                asset_defaults
                if asset_defaults is not None
                else get_asset_strategy_defaults()
            ).items()
        }
        self._effective: dict[str, tuple[StrategyParams, StrategyParams]] = {}
        self.params = self._load(self.KEY, self._defaults)
        self.asset_params: dict[str, StrategyParams] = {}
        raw = db.get_setting(self.ASSET_KEY) if db is not None else None
        if raw:
            try:
                data = json.loads(raw)
                for asset, value in (data or {}).items():
                    self.asset_params[str(asset).upper()] = StrategyParams.model_validate(
                        value
                    )
            except Exception:  # noqa: BLE001
                self.asset_params = {}

    def _load(self, key: str, default: StrategyParams) -> StrategyParams:
        raw = self._db.get_setting(key) if self._db is not None else None
        if raw:
            try:
                return StrategyParams.model_validate_json(raw)
            except Exception:  # noqa: BLE001
                pass
        return default

    def _apply_common(self, params: StrategyParams) -> StrategyParams:
        """Force shared fields back to the global values.

        Existing per-asset snapshots may contain the shared fields (they were
        saved before the common/per-asset split, or by an older client). The
        config page treats those fields as global-only, so ignore the snapshot
        copies here and keep a single source of truth.
        """
        if params is self.params:
            return params
        values = params.model_dump()
        changed = False
        for name in COMMON_PARAM_FIELDS:
            global_value = getattr(self.params, name)
            if values.get(name) != global_value:
                values[name] = global_value
                changed = True
        return StrategyParams.model_validate(values) if changed else params

    def params_for(self, asset: str | None) -> StrategyParams:
        if not asset:
            return self.params
        key = str(asset).upper()
        override = self.asset_params.get(key)
        if override is not None:
            return self._apply_common(override)
        extra = self.asset_defaults.get(key)
        if not extra:
            return self.params
        # ``params`` is replaced (not mutated) on update/reset, so identity is
        # a safe cache key for the merged result.
        cached = self._effective.get(key)
        if cached is not None and cached[0] is self.params:
            return cached[1]
        merged = self.params.model_dump()
        merged.update(extra)
        params = self._apply_common(StrategyParams.model_validate(merged))
        self._effective[key] = (self.params, params)
        return params

    def has_override(self, asset: str | None) -> bool:
        return bool(asset) and str(asset).upper() in self.asset_params

    def source_for(self, asset: str | None) -> str:
        """Where the effective params for ``asset`` come from.

        ``override``      — saved per-asset override in SQLite
        ``asset_default`` — partial [STRATEGY.<ASSET>] defaults in config.toml
        ``global``        — inherits the global defaults
        """
        key = str(asset).upper() if asset else ""
        if not key:
            return "global"
        if key in self.asset_params:
            return "override"
        if key in self.asset_defaults:
            return "asset_default"
        return "global"

    def _save_assets(self) -> None:
        if self._db is None:
            return
        payload = {
            asset: params.model_dump() for asset, params in self.asset_params.items()
        }
        self._db.set_setting(self.ASSET_KEY, json.dumps(payload))

    def update(
        self, patch: dict[str, Any], asset: str | None = None
    ) -> StrategyParams:
        key = str(asset).upper() if asset else None
        if key:
            # Shared fields are global-only; ignore them in an asset patch so
            # a stale client cannot diverge one coin from the common panel.
            patch = {
                name: value
                for name, value in patch.items()
                if name not in COMMON_PARAM_FIELDS
            }
            if not patch:
                return self.params_for(key)
        merged = self.params_for(key).model_dump()
        for name, value in patch.items():
            if name in merged:
                merged[name] = value
        params = StrategyParams.model_validate(merged)
        if key:
            self.asset_params[key] = params
            self._save_assets()
        else:
            self.params = params
            if self._db is not None:
                self._db.set_setting(self.KEY, params.model_dump_json())
        return params

    def reset(self, asset: str | None = None) -> StrategyParams:
        key = str(asset).upper() if asset else None
        if key:
            self.asset_params.pop(key, None)
            self._save_assets()
            return self.params_for(key)
        self.params = self._defaults
        if self._db is not None:
            self._db.set_setting(self.KEY, self.params.model_dump_json())
        return self.params

    def reset_common(self) -> StrategyParams:
        """Reset only the shared fields to their config defaults.

        Non-common global values and every per-asset override are preserved.
        """
        values = self.params.model_dump()
        for name in COMMON_PARAM_FIELDS:
            values[name] = getattr(self._defaults, name)
        self.params = StrategyParams.model_validate(values)
        if self._db is not None:
            self._db.set_setting(self.KEY, self.params.model_dump_json())
        return self.params
