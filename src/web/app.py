"""FastAPI dashboard and JSON API.

Pages:
  /login     -> HTML login page (cookie session, no Basic-auth popup)
  /          -> 控制台
  /trades    -> 交易记录
  /config    -> 策略配置 (always requires a login)

Read APIs follow the same rule as the pages: public only when WEB_PASSWORD
is empty. Config and control APIs always require a session.
"""
from __future__ import annotations

import hashlib
import math
import time
from pathlib import Path
from typing import Any
from urllib.parse import quote

from fastapi import Depends, FastAPI, HTTPException, Query, Request, Response
from fastapi.responses import HTMLResponse, RedirectResponse
from fastapi.staticfiles import StaticFiles
from pydantic import BaseModel, ValidationError

from src.config import (
    COMMON_PARAM_FIELDS,
    PER_ASSET_PARAM_FIELDS,
    StrategyParams,
)
from src.execution.maker_shadow import summarize_shadow
from src.infra.logging import get_logger
from src.web import auth

log = get_logger(__name__)

WEB_DIR = Path(__file__).resolve().parent
TEMPLATES_DIR = WEB_DIR / "templates"
STATIC_DIR = WEB_DIR / "static"

app = FastAPI(title="BTCETH5M", docs_url=None, redoc_url=None, openapi_url=None)
if STATIC_DIR.exists():
    app.mount("/static", StaticFiles(directory=str(STATIC_DIR)), name="static")

_state: Any = None


def set_state(state: Any) -> None:
    global _state
    _state = state


def _get_state() -> Any:
    if _state is None:
        raise HTTPException(status_code=503, detail="bot state not attached")
    return _state


def _asset_version() -> str:
    digest = hashlib.md5()
    try:
        for name in (
            "style.css",
            "auth-nav.js",
            "dashboard.js",
            "config.js",
            "trades.js",
            "latency.js",
            "maker.js",
        ):
            path = STATIC_DIR / name
            if path.exists():
                digest.update(path.read_bytes())
    except Exception:  # noqa: BLE001
        return "1"
    return digest.hexdigest()[:10] or "1"


ASSET_VERSION = _asset_version()


def _render(name: str) -> HTMLResponse:
    path = TEMPLATES_DIR / name
    if not path.exists():
        return HTMLResponse(f"<h1>{name} not found</h1>", status_code=500)
    html = path.read_text(encoding="utf-8").replace("__ASSET_VERSION__", ASSET_VERSION)
    return HTMLResponse(html, headers={"Cache-Control": "no-store"})


def _clean(value: Any) -> Any:
    if isinstance(value, dict):
        return {str(k): _clean(v) for k, v in value.items()}
    if isinstance(value, (list, tuple)):
        return [_clean(v) for v in value]
    if isinstance(value, float) and (math.isnan(value) or math.isinf(value)):
        return None
    return value


def _paginated(rows: Any, limit: int, offset: int, total: int) -> dict:
    return {
        "data": _clean(rows),
        "pagination": {
            "limit": int(limit),
            "offset": int(offset),
            "total": int(total),
            "has_more": int(offset) + int(limit) < int(total),
        },
    }


def _safe_next(value: str | None) -> str:
    if value and value.startswith("/") and not value.startswith("//"):
        return value
    return "/"


def _login_redirect(request: Request, *, always: bool = False) -> RedirectResponse | None:
    if (always or auth.auth_required()) and auth.current_session(request) is None:
        path = request.url.path
        if request.url.query:
            path += "?" + request.url.query
        return RedirectResponse(f"/login?next={quote(path)}", status_code=302)
    return None


async def require_read(request: Request) -> None:
    if auth.auth_required() and auth.current_session(request) is None:
        raise HTTPException(status_code=401, detail="需要登录")


async def require_login(request: Request) -> None:
    if auth.current_session(request) is None:
        raise HTTPException(status_code=401, detail="需要登录")


class LoginPayload(BaseModel):
    username: str = ""
    password: str = ""


def _validation_errors(exc: ValidationError) -> list[dict[str, str]]:
    out: list[dict[str, str]] = []
    for error in exc.errors():
        out.append(
            {
                "loc": ".".join(str(x) for x in error.get("loc", [])),
                "msg": str(error.get("msg", "")),
                "type": str(error.get("type", "")),
            }
        )
    return out


# ---------------------------------------------------------------------------
# Auth routes
# ---------------------------------------------------------------------------
@app.get("/login", response_class=HTMLResponse)
async def login_page(request: Request) -> Any:
    if auth.current_session(request) is not None:
        return RedirectResponse(_safe_next(request.query_params.get("next")), status_code=302)
    return _render("login.html")


@app.get("/api/auth/me")
async def api_auth_me(request: Request) -> dict:
    session = auth.current_session(request)
    return {
        "authenticated": session is not None,
        "user": session.user if session else None,
        "auth_required": auth.auth_required(),
        "credentials_configured": auth.credentials_configured(),
    }


@app.post("/api/auth/login")
async def api_auth_login(
    payload: LoginPayload, request: Request, response: Response
) -> dict:
    if not auth.credentials_configured():
        raise HTTPException(
            status_code=503,
            detail="管理员尚未设置 WEB_PASSWORD，请在 Coolify 环境变量中配置后重新部署",
        )
    ip = auth.client_ip(request)
    if auth.login_blocked(ip):
        raise HTTPException(status_code=429, detail="登录失败次数过多，请 5 分钟后再试")
    if not auth.verify_credentials(payload.username, payload.password):
        auth.record_login_failure(ip)
        raise HTTPException(status_code=401, detail="用户名或密码错误")
    auth.reset_login_failures(ip)
    user = payload.username or "admin"
    token = auth.create_session(user)
    auth.set_session_cookie(response, token, request)
    log.info("web.login", user=user, ip=ip)
    return {"ok": True, "user": user}


@app.post("/api/auth/logout")
async def api_auth_logout(request: Request, response: Response) -> dict:
    token = request.cookies.get(auth.SESSION_COOKIE)
    auth.delete_session(token)
    auth.clear_session_cookie(response)
    return {"ok": True}


# ---------------------------------------------------------------------------
# Pages
# ---------------------------------------------------------------------------
@app.get("/", response_class=HTMLResponse)
async def page_dashboard(request: Request) -> Any:
    redirect = _login_redirect(request)
    if redirect is not None:
        return redirect
    return _render("dashboard.html")


@app.get("/trades", response_class=HTMLResponse)
async def page_trades(request: Request) -> Any:
    redirect = _login_redirect(request)
    if redirect is not None:
        return redirect
    return _render("trades.html")


@app.get("/config", response_class=HTMLResponse)
async def page_config(request: Request) -> Any:
    redirect = _login_redirect(request, always=True)
    if redirect is not None:
        return redirect
    return _render("config.html")


@app.get("/latency", response_class=HTMLResponse)
async def page_latency(request: Request) -> Any:
    redirect = _login_redirect(request)
    if redirect is not None:
        return redirect
    return _render("latency.html")


@app.get("/maker", response_class=HTMLResponse)
async def page_maker(request: Request) -> Any:
    redirect = _login_redirect(request)
    if redirect is not None:
        return redirect
    return _render("maker.html")


# ---------------------------------------------------------------------------
# Health + read APIs
# ---------------------------------------------------------------------------
@app.get("/api/health")
async def api_health() -> dict:
    return {"ok": True, "ts": time.time()}


@app.get("/api/status")
async def api_status(_: None = Depends(require_read)) -> dict:
    state = _get_state()
    return _clean(
        {
            **_mode_state(state),
            "uptime_s": time.time() - state.started_at,
            "killswitch": state.killswitch.to_dict(),
            "summary": state.db.summary(),
            "assets": state.engine.latest,
            "configured_assets": state.settings.assets_list,
            "enabled_assets": _enabled_assets(state),
            "positions": state.db.open_positions(),
            "feeds": {
                "rtds": state.rtds.health(),
                "clob": state.books.health(),
            },
            "recording": (
                state.recorder.health()
                if getattr(state, "recorder", None) is not None
                else None
            ),
            # The portfolio guard uses the global value; per-asset overrides
            # do NOT raise it. Surface both so a 100/20 mismatch is visible.
            "risk_limits": {
                "global_daily_loss_usd": state.runtime.params.daily_loss_limit_usd,
                "asset_daily_loss_usd": {
                    asset: state.runtime.params_for(asset).daily_loss_limit_usd
                    for asset in state.settings.assets_list
                },
            },
            "config": state.runtime.params.model_dump(),
        }
    )


@app.get("/api/latency")
async def api_latency(
    hours: int = Query(24, ge=1, le=168),
    _: None = Depends(require_read),
) -> dict:
    state = _get_state()
    tracker = getattr(state, "latency", None)
    if tracker is None:
        raise HTTPException(status_code=503, detail="latency tracker not attached")
    return _clean(
        {
            "summary": tracker.summary(900),
            "decision": tracker.decision_summary(3600),
            "buckets": state.db.recent_latency_buckets(hours, limit=500),
            "events": state.db.recent_latency_events(100),
            "feeds": {"rtds": state.rtds.health(), "clob": state.books.health()},
            "note": (
                "publish→recv 包含本机与 RTDS 的时钟偏差；"
                "client 最小值可作为网络+解析+偏差的上界。"
            ),
        }
    )


@app.get("/api/windows")
async def api_windows(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    key = _asset_key(asset)
    return _paginated(
        db.recent_windows(limit, offset, key),
        limit,
        offset,
        db.count_windows(key),
    )


@app.get("/api/signals")
async def api_signals(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    key = _asset_key(asset)
    return _paginated(
        db.recent_signals(limit, offset, key),
        limit,
        offset,
        db.count_signals(key),
    )


@app.get("/api/orders")
async def api_orders(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    key = _asset_key(asset)
    return _paginated(
        db.recent_orders(limit, offset, key),
        limit,
        offset,
        db.count_orders(key),
    )


@app.get("/api/fills")
async def api_fills(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    key = _asset_key(asset)
    return _paginated(
        db.recent_fills(limit, offset, key),
        limit,
        offset,
        db.count_fills(key),
    )


@app.get("/api/positions")
async def api_positions(
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    key = _asset_key(asset)
    return _paginated(
        db.recent_positions(limit, offset, key),
        limit,
        offset,
        db.count_positions(key),
    )


@app.get("/api/pnl")
async def api_pnl(
    days: int = Query(7, ge=1, le=90),
    asset: str | None = Query(None),
    _: None = Depends(require_read),
) -> dict:
    state = _get_state()
    key = _asset_key(asset)
    return {
        "summary": _clean(state.db.summary(key)),
        "by_day": _clean(state.db.pnl_by_day(days, key)),
    }


@app.get("/api/maker-shadow")
async def api_maker_shadow(
    days: int = Query(14, ge=1, le=90),
    limit: int = Query(50, ge=1, le=500),
    offset: int = Query(0, ge=0),
    _: None = Depends(require_read),
) -> dict:
    db = _get_state().db
    rows = db.maker_shadow_rows(days)
    summary, by_asset = summarize_shadow(rows)
    total = db.count_maker_shadow(days)
    payload = _paginated(
        db.recent_maker_shadow(limit, offset, days), limit, offset, total
    )
    payload.update(
        {
            "days": int(days),
            "summary": _clean(summary),
            "by_asset": _clean(by_asset),
        }
    )
    return payload


# ---------------------------------------------------------------------------
# Config + control APIs (always require a login)
# ---------------------------------------------------------------------------
def _asset_key(asset: str | None) -> str | None:
    value = str(asset or "").strip().upper()
    return value or None


_LIVE_LOCK_LABELS = {
    "live_disabled": "服务端 LIVE_ENABLED=false",
    "paper_trading": "服务端 PAPER_TRADING=true（强制纸盘）",
    "live_client_missing": "实盘客户端未初始化",
    "credentials_missing": "缺少 WALLET_PRIVATE_KEY",
    "live_not_implemented": "实盘下单尚未实现",
}


def _enabled_assets(state: Any) -> list[str]:
    fn = getattr(state.runtime, "enabled_assets", None)
    if callable(fn):
        return list(fn(state.settings.assets_list))
    return list(state.settings.assets_list)


def _asset_modes(state: Any) -> dict[str, str]:
    fn = getattr(state.router, "mode_for", None)
    if not callable(fn):
        mode = str(getattr(state.router, "mode", "paper") or "paper")
        return {asset: mode for asset in state.settings.assets_list}
    return {asset: str(fn(asset)) for asset in state.settings.assets_list}


def _configured_modes(state: Any) -> dict[str, str]:
    fn = getattr(state.router, "configured_mode", None)
    if not callable(fn):
        return _asset_modes(state)
    return {asset: str(fn(asset)) for asset in state.settings.assets_list}


def _live_available(state: Any) -> bool:
    return bool(getattr(state.router, "live_available", False))


def _live_lock_reason(state: Any) -> str:
    return str(getattr(state.router, "live_lock_reason", "") or "")


def _mode_state(state: Any) -> dict[str, Any]:
    reason = _live_lock_reason(state)
    return {
        "mode": str(getattr(state.router, "mode", "paper") or "paper"),
        "asset_modes": _asset_modes(state),
        "configured_modes": _configured_modes(state),
        "live_available": _live_available(state),
        "live_lock_reason": reason,
        "live_lock_message": _LIVE_LOCK_LABELS.get(reason, ""),
    }


def _config_payload(state: Any, key: str | None, params: Any) -> dict[str, Any]:
    configured = _configured_modes(state).get(key or "", params.trading_mode)
    return {
        "params": params.model_dump(),
        "global_params": state.runtime.params.model_dump(),
        "common_params": state.runtime.params.model_dump(),
        "asset_defaults": {
            asset: dict(values) for asset, values in state.runtime.asset_defaults.items()
        },
        "assets": state.settings.assets_list,
        "asset": key or "",
        "source": state.runtime.source_for(key),
        "overridden": bool(key and state.runtime.has_override(key)),
        "configured_mode": configured,
        "effective_mode": _asset_modes(state).get(key or "", configured),
        "common_fields": list(COMMON_PARAM_FIELDS),
        "per_asset_fields": list(PER_ASSET_PARAM_FIELDS),
        **_mode_state(state),
    }


@app.get("/api/config")
async def api_get_config(
    asset: str | None = Query(None), _: None = Depends(require_login)
) -> dict:
    state = _get_state()
    key = _asset_key(asset)
    params = state.runtime.params_for(key) if key else state.runtime.params
    return _config_payload(state, key, params)


@app.get("/api/config/assets")
async def api_config_assets(_: None = Depends(require_login)) -> dict:
    """Every configured asset as one card for the config page."""
    state = _get_state()
    cards = []
    for asset in state.settings.assets_list:
        params = state.runtime.params_for(asset)
        cards.append(
            {
                "asset": asset,
                "params": params.model_dump(),
                "source": state.runtime.source_for(asset),
                "overridden": state.runtime.has_override(asset),
                "configured_mode": _configured_modes(state).get(
                    asset, params.trading_mode
                ),
                "effective_mode": _asset_modes(state).get(asset, "paper"),
            }
        )
    return {
        "assets": state.settings.assets_list,
        "enabled_assets": _enabled_assets(state),
        "cards": cards,
        "common_params": state.runtime.params.model_dump(),
        "common_fields": list(COMMON_PARAM_FIELDS),
        "per_asset_fields": list(PER_ASSET_PARAM_FIELDS),
        **_mode_state(state),
    }


@app.put("/api/config")
async def api_put_config(
    payload: dict[str, Any], _: None = Depends(require_login)
) -> dict:
    state = _get_state()
    key = _asset_key(payload.get("asset"))
    patch = payload.get("params")
    if not isinstance(patch, dict):
        # Backward compatibility: old clients sent a flat patch (global only).
        patch = {k: v for k, v in payload.items() if k != "asset"}
    try:
        params = state.runtime.update(patch, asset=key)
    except ValidationError as exc:
        raise HTTPException(status_code=400, detail=_validation_errors(exc)) from exc
    log.info(
        "web.config_updated",
        asset=key or "GLOBAL",
        mode=params.trading_mode,
        patch=list(patch.keys()),
    )
    return {"ok": True, **_config_payload(state, key, params)}


@app.post("/api/config/reset")
async def api_reset_config(
    payload: dict[str, Any] | None = None, _: None = Depends(require_login)
) -> dict:
    state = _get_state()
    body = payload or {}
    if body.get("scope") == "common":
        params = state.runtime.reset_common()
        log.info("web.config_reset", asset="COMMON", mode=params.trading_mode)
        return {"ok": True, **_config_payload(state, None, params)}
    key = _asset_key(body.get("asset"))
    params = state.runtime.reset(key)
    log.info("web.config_reset", asset=key or "GLOBAL", mode=params.trading_mode)
    return {"ok": True, **_config_payload(state, key, params)}


@app.get("/api/config/schema")
async def api_config_schema(_: None = Depends(require_login)) -> dict:
    return {
        "fields": [
            {"name": name, "default": field.default}
            for name, field in StrategyParams.model_fields.items()
        ]
    }


@app.get("/api/control")
async def api_control(_: None = Depends(require_login)) -> dict:
    return _get_state().killswitch.to_dict()


@app.post("/api/control/stop")
async def api_control_stop(_: None = Depends(require_login)) -> dict:
    state = _get_state()
    state.killswitch.stop("dashboard")
    return {"ok": True, **state.killswitch.to_dict()}


@app.post("/api/control/start")
async def api_control_start(_: None = Depends(require_login)) -> dict:
    state = _get_state()
    state.killswitch.start()
    return {"ok": True, **state.killswitch.to_dict()}
