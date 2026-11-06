"use strict";

const $ = (id) => document.getElementById(id);

const GROUPS = [
  {
    title: "入场与 Edge",
    fields: [
      { key: "min_edge", label: "最小 Edge", help: "公允概率 − 成交均价 − 手续费 的最低值；低于该值不入场。", step: 0.01 },
      { key: "model_buffer", label: "模型缓冲", help: "额外安全边际，抵消模型的过度自信；入场阈值 = 最小 Edge + 模型缓冲。", step: 0.01 },
      { key: "min_fair", label: "最小 Fair（方向）", help: "只考虑模型概率不低于该值的方向；过滤 fair≈0.5 的彩票票。0 = 关闭。", step: 0.01 },
      { key: "min_entry_price", label: "最小有效入场价", help: "含手续费的平均成交价低于该值时不入场；便宜票意味着市场不认同模型，模型偏自信时尤其危险。0 = 关闭。", step: 0.01 },
      { key: "max_position_usd", label: "单笔名义金额", help: "每个窗口最多投入的 USDC。实盘模式下就是真实下单金额。", step: 0.5, unit: "USDC" },
      { key: "entry_min_rel_s", label: "最早入场（秒）", help: "窗口开始后多少秒才允许入场。", step: 1, unit: "秒" },
      { key: "entry_max_rel_s", label: "最晚入场（秒）", help: "窗口结束前多少秒停止入场。", step: 1, unit: "秒" },
    ],
  },
  {
    title: "风控",
    fields: [
      { key: "daily_loss_limit_usd", label: "日损失上限", help: "该币种当日已实现亏损达到该值后停止新入场；全局默认值同时作为所有币种合计的止损上限。", step: 1, unit: "USDC" },
    ],
  },
  {
    title: "盘口过滤",
    fields: [
      { key: "max_spread", label: "最大价差", help: "买一/卖一价差超过该值不入场。", step: 0.005 },
      { key: "max_stale_book_s", label: "盘口最大年龄", help: "盘口超过该秒数未更新不入场。", step: 0.5, unit: "秒" },
      { key: "min_ask_shares", label: "最小卖一深度", help: "卖一档至少这么多份额。", step: 1, unit: "份" },
      { key: "min_order_shares", label: "最小订单份额", help: "Polymarket 5m 市场最小订单通常为 5 份。", step: 1, unit: "份" },
    ],
  },
  {
    title: "模型与执行",
    fields: [
      { key: "taker_fee_rate", label: "Taker 费率", help: "官方 crypto 费率 0.07；fee = rate × p × (1−p)。", step: 0.01 },
      { key: "sigma_lookback_s", label: "σ 回看窗口", help: "用最近多少秒的 1s 收益估计波动率。", step: 30, unit: "秒" },
      { key: "min_sigma_samples", label: "σ 最少样本", help: "不足时显示预热中，不入场。", step: 10, unit: "个" },
      { key: "engine_tick_s", label: "引擎评估间隔", help: "每多少秒评估一次。", step: 0.1, unit: "秒" },
      { key: "signal_log_min_edge", label: "信号记录阈值", help: "Edge 超过该值时记录未入场信号，便于复盘。", step: 0.01 },
    ],
  },
];

let DATA = null;
const INITIAL = new Map(); // asset -> params snapshot (including trading_mode)
let COMMON_FIELDS = new Set();
let COMMON_INITIAL = {};

const SOURCE_LABELS = {
  override: "已单独设置",
  asset_default: "币种默认",
  global: "继承全局默认",
};

const esc = (v) =>
  String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));

function redirectLogin() {
  location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
}

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (r.status === 401) { redirectLogin(); throw new Error("auth"); }
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

async function sendJSON(url, method, body) {
  const opts = { method, cache: "no-store" };
  if (body !== undefined) {
    opts.headers = { "Content-Type": "application/json" };
    opts.body = JSON.stringify(body);
  }
  const r = await fetch(url, opts);
  if (r.status === 401) { redirectLogin(); throw new Error("auth"); }
  const data = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(formatDetail(data.detail));
  return data;
}

function formatDetail(detail) {
  if (Array.isArray(detail)) {
    return detail.map((d) => (d.loc ? d.loc + ": " : "") + (d.msg || "")).join("；");
  }
  return detail || "请求失败";
}

/* ── Render ───────────────────────────────────────────────────────────── */
function fieldHtml(field, value) {
  const unit = field.unit ? `<span class="config-unit">${esc(field.unit)}</span>` : "";
  if (field.type === "bool") {
    return `<div class="field" data-field="${esc(field.key)}">
      <label>${esc(field.label)}</label>
      <div style="display:flex;align-items:center;gap:6px">
        <input type="checkbox" data-key="${esc(field.key)}"${value ? " checked" : ""}>
      </div>
      <div class="config-help">${esc(field.help || "")}</div>
      <div class="config-key">${esc(field.key)}</div>
    </div>`;
  }
  return `<div class="field" data-field="${esc(field.key)}">
    <label>${esc(field.label)}</label>
    <div style="display:flex;align-items:center;gap:6px">
      <input type="number" step="${esc(field.step || "any")}" data-key="${esc(field.key)}" value="${esc(value)}">
      ${unit}
    </div>
    <div class="config-help">${esc(field.help || "")}</div>
    <div class="config-key">${esc(field.key)}</div>
  </div>`;
}

function sourceLabel(source) {
  return SOURCE_LABELS[source] || esc(source || "");
}

function modeLockMessage() {
  return DATA.live_lock_message || DATA.live_lock_reason || "实盘通道未启用";
}

function modeNoteHtml(mode, enabled) {
  if (!enabled) {
    return '<span class="orange">该币种交易已关闭：不会开新仓，已有持仓仍会正常结算。</span>';
  }
  if (DATA.live_available) {
    return mode === "live"
      ? '<span class="red">实盘：新入场会使用真实资金，请确认单笔金额与日损失上限。</span>'
      : '<span class="muted">纸盘：使用真实盘口模拟成交，不产生真实订单。</span>';
  }
  if (mode === "live") {
    return `<span class="red">已选择实盘配置，但当前无法执行：${esc(modeLockMessage())}；实际按纸盘运行。</span>`;
  }
  return `<span class="muted">实盘不可用：${esc(modeLockMessage())}。当前按纸盘运行。</span>`;
}

function modeSwitchHtml(asset, mode) {
  return `<div class="mode-switch" role="radiogroup" aria-label="${esc(asset)} 交易模式">
    <label class="mode-option"><input type="radio" name="mode-${esc(asset)}" value="paper"${mode === "paper" ? " checked" : ""}><span>纸盘</span></label>
    <label class="mode-option live"><input type="radio" name="mode-${esc(asset)}" value="live"${mode === "live" ? " checked" : ""}><span>实盘</span></label>
  </div>`;
}

function enabledToggleHtml(enabled) {
  return `<label class="trade-toggle" title="关闭后不开新仓，已有持仓仍会结算">
    <input type="checkbox" data-role="enabled"${enabled ? " checked" : ""}>
    <span class="trade-toggle-track"><span></span></span>
    <span class="trade-toggle-label">${enabled ? "交易开启" : "交易关闭"}</span>
  </label>`;
}

function isCommonField(key) {
  return COMMON_FIELDS.has(key);
}

function groupsHtml(scope, params) {
  return GROUPS.map((group) => {
    const fields = group.fields.filter((f) =>
      scope === "common" ? isCommonField(f.key) : !isCommonField(f.key)
    );
    if (!fields.length) return "";
    return `<div class="config-group">
      <h3><span class="dot"></span>${esc(group.title)}<span class="count">${fields.length} 项</span></h3>
      <div class="config-grid">${fields.map((f) => fieldHtml(f, params[f.key])).join("")}</div>
    </div>`;
  }).join("");
}

function cardHtml(card) {
  const asset = card.asset;
  const params = card.params || {};
  const mode = params.trading_mode === "live" ? "live" : "paper";
  const enabled = params.trading_enabled !== false;
  const effective = card.effective_mode === "live" ? "live" : "paper";
  return `<section class="asset-config-card" data-asset="${esc(asset)}" data-source="${esc(card.source)}" data-overridden="${card.overridden ? 1 : 0}">
    <div class="asset-config-head">
      <div class="asset-config-title">
        <span class="asset-symbol">${esc(asset)}</span>
        <span class="badge ${effective}" data-role="effective">${effective === "live" ? "实盘" : "纸盘"}</span>
        <span class="config-source" data-role="source">${sourceLabel(card.source)}</span>
      </div>
      <div class="asset-config-controls">
        ${enabledToggleHtml(enabled)}
        ${modeSwitchHtml(asset, mode)}
      </div>
    </div>
    <div class="mode-note" data-role="mode-note">${modeNoteHtml(mode, enabled)}</div>
    ${groupsHtml("asset", params)}
    <div class="asset-config-actions">
      <span class="save-msg muted" data-role="msg">没有待保存的修改</span>
      <button class="btn btn-ghost" type="button" data-action="reset"${card.overridden ? "" : " disabled"}>恢复默认</button>
      <button class="btn btn-primary" type="button" data-action="save" disabled>保存 ${esc(asset)}</button>
    </div>
  </section>`;
}

function commonCardHtml() {
  const params = DATA.common_params || {};
  return `<section class="asset-config-card common-config-card" data-scope="common">
    <div class="asset-config-head">
      <div class="asset-config-title">
        <span class="asset-symbol">通用设置</span>
        <span class="badge paper">BTC / ETH 共享</span>
      </div>
    </div>
    <div class="mode-note"><span class="muted">费率、σ、引擎节奏、信号记录与盘口新鲜度等所有币种共用的参数；修改后保存即对所有币种生效。</span></div>
    ${groupsHtml("common", params)}
    <div class="asset-config-actions">
      <span class="save-msg muted" data-role="msg">没有待保存的修改</span>
      <button class="btn btn-ghost" type="button" data-action="reset">恢复通用默认</button>
      <button class="btn btn-primary" type="button" data-action="save" disabled>保存通用设置</button>
    </div>
  </section>`;
}

function renderLiveState() {
  const banner = $("live-banner");
  const state = $("live-state");
  const lock = $("live-lock");
  if (DATA.live_available) {
    if (state) state.textContent = "已启用";
    if (lock) lock.textContent = "按币种独立选择";
    if (banner) {
      banner.innerHTML = '<div class="alert ok">实盘通道已启用。每个币种可单独切换纸盘/实盘；保存后从下一个 tick 生效。</div>';
    }
    return;
  }
  if (state) state.textContent = "未启用";
  if (lock) lock.textContent = modeLockMessage();
  if (banner) {
    banner.innerHTML = `<div class="alert warn">实盘通道未启用：${esc(modeLockMessage())}。仍可为单个币种选择实盘配置，但实际会按纸盘运行。</div>`;
  }
}

function renderCommon() {
  const el = $("common-card");
  if (!el) return;
  if (!COMMON_FIELDS.size) {
    el.innerHTML = "";
    return;
  }
  el.innerHTML = commonCardHtml();
  const cardEl = el.querySelector(".common-config-card");
  COMMON_INITIAL = Object.assign({}, DATA.common_params || {});
  bindCommon(cardEl);
  updateCommonState(cardEl);
}

function render() {
  COMMON_FIELDS = new Set(DATA.common_fields || []);
  renderLiveState();
  renderCommon();
  const cards = DATA.cards || [];
  const wrap = $("asset-cards");
  if (!cards.length) {
    wrap.innerHTML = '<div class="no-data">没有配置币种</div>';
    return;
  }
  wrap.innerHTML = cards.map(cardHtml).join("");
  cards.forEach((card) => {
    INITIAL.set(card.asset, Object.assign({}, card.params));
    bindCard(wrap.querySelector(`.asset-config-card[data-asset="${card.asset}"]`));
  });
}

/* ── Card state ───────────────────────────────────────────────────────── */
function cardParams(cardEl) {
  const params = {};
  cardEl.querySelectorAll("[data-key]").forEach((el) => {
    params[el.dataset.key] = el.type === "checkbox" ? el.checked : Number(el.value);
  });
  params.trading_mode = selectedMode(cardEl);
  params.trading_enabled = enabledChecked(cardEl);
  return params;
}

function selectedMode(cardEl) {
  const checked = cardEl.querySelector('input[type="radio"]:checked');
  return checked ? checked.value : "paper";
}

function enabledChecked(cardEl) {
  const el = cardEl.querySelector('[data-role="enabled"]');
  return el ? el.checked : true;
}

function fieldChanged(el, initial) {
  if (el.type === "checkbox") return el.checked !== Boolean(initial);
  return Number(el.value) !== Number(initial);
}

function updateCardState(cardEl) {
  const asset = cardEl.dataset.asset;
  const initial = INITIAL.get(asset) || {};
  let changed = 0;
  cardEl.querySelectorAll("[data-key]").forEach((el) => {
    const diff = fieldChanged(el, initial[el.dataset.key]);
    const box = el.closest(".field");
    if (box) box.classList.toggle("changed", diff);
    if (diff) changed += 1;
  });
  const mode = selectedMode(cardEl);
  const modeChanged = mode !== (initial.trading_mode || "paper");
  const switchBox = cardEl.querySelector(".mode-switch");
  if (switchBox) switchBox.classList.toggle("changed", modeChanged);
  const enabled = enabledChecked(cardEl);
  const enabledChanged = enabled !== (initial.trading_enabled !== false);
  const toggle = cardEl.querySelector(".trade-toggle");
  if (toggle) {
    toggle.classList.toggle("changed", enabledChanged);
    const label = toggle.querySelector(".trade-toggle-label");
    if (label) label.textContent = enabled ? "交易开启" : "交易关闭";
  }
  const note = cardEl.querySelector('[data-role="mode-note"]');
  if (note) note.innerHTML = modeNoteHtml(mode, enabled);
  const total = changed + (modeChanged ? 1 : 0) + (enabledChanged ? 1 : 0);
  cardEl.querySelector('[data-action="save"]').disabled = total === 0;
  cardEl.querySelector('[data-role="msg"]').textContent = total
    ? total + " 项待保存"
    : "没有待保存的修改";
}

function applyParams(cardEl, params) {
  cardEl.querySelectorAll("[data-key]").forEach((el) => {
    const value = params[el.dataset.key];
    if (el.type === "checkbox") el.checked = Boolean(value);
    else el.value = value;
  });
  const mode = params.trading_mode === "live" ? "live" : "paper";
  cardEl.querySelectorAll('input[type="radio"]').forEach((el) => {
    el.checked = el.value === mode;
  });
  const enabledEl = cardEl.querySelector('[data-role="enabled"]');
  if (enabledEl) enabledEl.checked = params.trading_enabled !== false;
  cardEl.querySelectorAll(".field").forEach((el) => el.classList.remove("changed"));
  const switchBox = cardEl.querySelector(".mode-switch");
  if (switchBox) switchBox.classList.remove("changed");
  const toggle = cardEl.querySelector(".trade-toggle");
  if (toggle) toggle.classList.remove("changed");
  INITIAL.set(cardEl.dataset.asset, Object.assign({}, params));
  updateCardState(cardEl);
}

function applyMeta(cardEl, payload) {
  if (payload.source !== undefined) {
    cardEl.dataset.source = payload.source;
    cardEl.dataset.overridden = payload.overridden ? "1" : "0";
    cardEl.querySelector('[data-role="source"]').textContent = sourceLabel(payload.source);
    cardEl.querySelector('[data-action="reset"]').disabled = !payload.overridden;
  }
  if (payload.effective_mode !== undefined) {
    const effective = payload.effective_mode === "live" ? "live" : "paper";
    const badge = cardEl.querySelector('[data-role="effective"]');
    badge.className = "badge " + effective;
    badge.textContent = effective === "live" ? "实盘" : "纸盘";
    const dataCard = (DATA.cards || []).find((c) => c.asset === cardEl.dataset.asset);
    if (dataCard) dataCard.effective_mode = effective;
  }
}

function bindCard(cardEl) {
  if (!cardEl) return;
  cardEl.querySelectorAll("[data-key], input[type=radio], [data-role=enabled]").forEach((el) => {
    el.addEventListener("input", () => updateCardState(cardEl));
    el.addEventListener("change", () => updateCardState(cardEl));
  });
  cardEl.querySelector('[data-action="save"]').addEventListener("click", () => saveCard(cardEl));
  cardEl.querySelector('[data-action="reset"]').addEventListener("click", () => resetCard(cardEl));
  updateCardState(cardEl);
}

/* ── Common settings card ─────────────────────────────────────────────── */
function commonParams(cardEl) {
  const params = {};
  cardEl.querySelectorAll("[data-key]").forEach((el) => {
    params[el.dataset.key] = el.type === "checkbox" ? el.checked : Number(el.value);
  });
  return params;
}

function syncCommonToCards(params) {
  const values = {};
  (DATA.common_fields || []).forEach((key) => { values[key] = params[key]; });
  (DATA.cards || []).forEach((card) => {
    if (card.params) Object.assign(card.params, values);
  });
  Object.assign(DATA.common_params || {}, values);
}

function updateCommonState(cardEl) {
  let changed = 0;
  cardEl.querySelectorAll("[data-key]").forEach((el) => {
    const diff = fieldChanged(el, COMMON_INITIAL[el.dataset.key]);
    const box = el.closest(".field");
    if (box) box.classList.toggle("changed", diff);
    if (diff) changed += 1;
  });
  cardEl.querySelector('[data-action="save"]').disabled = changed === 0;
  cardEl.querySelector('[data-role="msg"]').textContent = changed
    ? changed + " 项待保存"
    : "没有待保存的修改";
}

function applyCommonParams(cardEl, params) {
  cardEl.querySelectorAll("[data-key]").forEach((el) => {
    const value = params[el.dataset.key];
    if (el.type === "checkbox") el.checked = Boolean(value);
    else el.value = value;
    const box = el.closest(".field");
    if (box) box.classList.remove("changed");
  });
  COMMON_INITIAL = Object.assign({}, params);
  syncCommonToCards(params);
  updateCommonState(cardEl);
}

function bindCommon(cardEl) {
  if (!cardEl) return;
  cardEl.querySelectorAll("[data-key]").forEach((el) => {
    el.addEventListener("input", () => updateCommonState(cardEl));
    el.addEventListener("change", () => updateCommonState(cardEl));
  });
  cardEl.querySelector('[data-action="save"]').addEventListener("click", () => saveCommon(cardEl));
  cardEl.querySelector('[data-action="reset"]').addEventListener("click", () => resetCommon(cardEl));
  updateCommonState(cardEl);
}

async function saveCommon(cardEl) {
  const btn = cardEl.querySelector('[data-action="save"]');
  const msg = cardEl.querySelector('[data-role="msg"]');
  btn.disabled = true;
  msg.textContent = "保存中...";
  try {
    const params = commonParams(cardEl);
    const res = await sendJSON("/api/config", "PUT", { params });
    DATA.common_params = Object.assign({}, res.params);
    applyCommonParams(cardEl, res.params);
    msg.textContent = "已保存（通用设置）";
    setTimeout(() => updateCommonState(cardEl), 3000);
  } catch (e) {
    if (String(e.message) === "auth") return;
    msg.textContent = "保存失败：" + e.message;
    btn.disabled = false;
  }
}

async function resetCommon(cardEl) {
  if (!confirm("恢复通用设置为默认值？币种的独立参数不会被删除。")) return;
  const msg = cardEl.querySelector('[data-role="msg"]');
  msg.textContent = "恢复中...";
  try {
    const res = await sendJSON("/api/config/reset", "POST", { scope: "common" });
    DATA.common_params = Object.assign({}, res.params);
    applyCommonParams(cardEl, res.params);
    msg.textContent = "已恢复通用默认";
    setTimeout(() => updateCommonState(cardEl), 3000);
  } catch (e) {
    if (String(e.message) === "auth") return;
    msg.textContent = "恢复失败：" + e.message;
  }
}

async function saveCard(cardEl) {
  const asset = cardEl.dataset.asset;
  const params = cardParams(cardEl);
  const initial = INITIAL.get(asset) || {};
  if (params.trading_mode === "live" && initial.trading_mode !== "live") {
    const text = DATA.live_available
      ? `${asset} 将切换为实盘：保存后新入场会使用真实资金。确认保存？`
      : `${asset} 将保存为实盘配置；当前实盘通道不可用（${modeLockMessage()}），实际仍按纸盘运行。确认保存？`;
    if (!confirm(text)) return;
  }
  const btn = cardEl.querySelector('[data-action="save"]');
  const msg = cardEl.querySelector('[data-role="msg"]');
  btn.disabled = true;
  msg.textContent = "保存中...";
  try {
    const res = await sendJSON("/api/config", "PUT", { asset, params });
    applyParams(cardEl, res.params);
    applyMeta(cardEl, res);
    msg.textContent = "已保存（" + asset + "）";
    setTimeout(() => updateCardState(cardEl), 3000);
  } catch (e) {
    if (String(e.message) === "auth") return;
    msg.textContent = "保存失败：" + e.message;
    btn.disabled = false;
  }
}

async function resetCard(cardEl) {
  const asset = cardEl.dataset.asset;
  if (!confirm(`恢复 ${asset} 的默认参数？当前独立设置会被删除。`)) return;
  const msg = cardEl.querySelector('[data-role="msg"]');
  msg.textContent = "恢复中...";
  try {
    const res = await sendJSON("/api/config/reset", "POST", { asset });
    applyParams(cardEl, res.params);
    applyMeta(cardEl, res);
    msg.textContent = `已恢复 ${asset} 的默认`;
    setTimeout(() => updateCardState(cardEl), 3000);
  } catch (e) {
    if (String(e.message) === "auth") return;
    msg.textContent = "恢复失败：" + e.message;
  }
}

function hasUnsaved() {
  return Array.from(document.querySelectorAll(".asset-config-card")).some((cardEl) => {
    return !cardEl.querySelector('[data-action="save"]').disabled;
  });
}

/* ── Nav ──────────────────────────────────────────────────────────────── */
function clockStr() {
  return new Date().toISOString().slice(11, 19) + " UTC";
}

async function refreshNav() {
  try {
    const s = await getJSON("/api/status");
    const feeds = s.feeds || {};
    const rtds = feeds.rtds || {};
    const ok = Object.keys(rtds).length > 0 &&
      Object.values(rtds).every((x) => x && x.connected) &&
      Boolean(feeds.clob?.connected);
    $("live-dot").className = "live-dot" + (ok ? "" : " offline");
    $("live-label").textContent = ok ? "在线" : "离线";
  } catch (e) {
    /* 配置页不因状态接口失败而中断 */
  }
}

async function load() {
  try {
    DATA = await getJSON("/api/config/assets");
    render();
  } catch (e) {
    if (String(e.message) === "auth") return;
    $("asset-cards").innerHTML =
      '<div class="alert warn">读取配置失败：' + esc(e.message) + "</div>";
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("clock").textContent = clockStr();
  setInterval(() => { $("clock").textContent = clockStr(); }, 1000);
  window.addEventListener("beforeunload", (e) => {
    if (!hasUnsaved()) return;
    e.preventDefault();
    e.returnValue = "";
  });
  load();
  refreshNav();
  setInterval(refreshNav, 5000);
});
