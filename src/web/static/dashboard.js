"use strict";

const $ = (id) => document.getElementById(id);
const PAGERS = {
  fills: { limit: 20, offset: 0, total: 0 },
  signals: { limit: 20, offset: 0, total: 0 },
};

const fmt = (n, d = 3) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  return Number(n).toFixed(d);
};
const money = (n) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  const v = Number(n);
  return (v >= 0 ? "+$" : "-$") + Math.abs(v).toFixed(2);
};
const pnlClass = (n) => {
  const v = Number(n);
  if (!Number.isFinite(v) || v === 0) return "muted";
  return v > 0 ? "green" : "red";
};
const pct = (n, d = 1) => {
  if (n === null || n === undefined || Number.isNaN(Number(n))) return "--";
  return (Number(n) * 100).toFixed(d) + "%";
};
const esc = (v) =>
  String(v === null || v === undefined ? "" : v).replace(/[&<>"']/g, (c) => ({
    "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;",
  }[c]));
const clamp = (v, a, b) => Math.max(a, Math.min(b, v));

function assetNames(status) {
  // Configured assets first so the grid renders even before the engine has
  // published a tick for an asset; runtime keys cover any extra assets.
  const names = new Set((status && status.configured_assets) || []);
  Object.keys((status && status.assets) || {}).forEach((a) => names.add(a));
  return Array.from(names).sort();
}

function outcomePill(outcome) {
  if (outcome !== "UP" && outcome !== "DOWN") return "--";
  return `<span class="pill ${outcome === "UP" ? "buy" : "sell"}">${outcome}</span>`;
}

function timeStr(sec) {
  if (sec === null || sec === undefined || !Number.isFinite(Number(sec))) return "--";
  let s = Math.max(0, Math.floor(Number(sec)));
  const m = Math.floor(s / 60);
  s = s % 60;
  return m + ":" + String(s).padStart(2, "0");
}

function utcTime(ts) {
  if (!ts) return "--";
  return new Date(Number(ts) * 1000).toISOString().slice(0, 19).replace("T", " ");
}

function clockStr() {
  return new Date().toISOString().slice(11, 19) + " UTC";
}

function p0SourceLabel(source) {
  const s = String(source || "");
  if (s === "gamma") return "Gamma";
  if (s === "rtds") return "RTDS 推算";
  if (s === "pending") return "待定";
  return s || "--";
}

function statusLabel(raw) {
  const s = String(raw || "");
  if (s.startsWith("entered")) return "已入场";
  if (s === "in_position") return "持仓中";
  if (s === "no_window") return "等待窗口";
  if (s === "warming_up") return "预热中";
  if (s.startsWith("warming_up_sigma")) {
    const m = s.match(/\(([^)]+)\)/);
    return "预热中 · σ 样本 " + (m ? m[1] : "");
  }
  if (s === "waiting_p0") return "等待开盘基准";
  if (s === "stale_feed") return "行情延迟";
  if (s === "fair_unavailable") return "公允价值不可用";
  if (s === "no_book") return "等待盘口";
  if (s === "one_sided_book") return "盘口单边";
  if (s.startsWith("wide_spread")) return "价差过大 " + s.slice(11);
  if (s.startsWith("stale_book")) return "盘口陈旧 " + s.slice(10);
  if (s.startsWith("thin_best")) return "盘口深度不足 " + s.slice(9);
  if (s.startsWith("early")) return "未到入场时间 " + s.slice(5);
  if (s.startsWith("late")) return "已过入场时间 " + s.slice(4);
  if (s.startsWith("edge")) return "Edge 不足 " + s.slice(5);
  if (s.startsWith("filtered")) {
    const gates = s.slice(9).replace(/[()]/g, "").split(",").filter(Boolean);
    const names = { fair: "Fair 过低", price: "入场价过低" };
    return "已过滤 " + gates.map((g) => names[g] || g).join(" · ");
  }
  if (s === "killswitch") return "已停止";
  if (s === "daily_loss_limit") return "触发日损失限制";
  if (s === "fill_rejected") return "成交被拒";
  if (s.startsWith("execution_error")) return "执行错误";
  if (s === "window_discovered") return "新窗口";
  return s || "等待";
}

function statusClass(raw) {
  const s = String(raw || "");
  if (s.startsWith("entered") || s === "in_position") return "pass";
  if (s.includes("killswitch") || s.includes("daily_loss") || s.startsWith("execution_error")) return "fail";
  if (
    s.startsWith("warming") || s === "waiting_p0" || s === "stale_feed" ||
    s === "no_book" || s === "one_sided_book" || s === "stale_book" ||
    s.startsWith("thin_best") || s.startsWith("wide_spread") ||
    s.startsWith("early") || s.startsWith("late") || s.startsWith("filtered")
  ) return "pending";
  return "past_t1";
}

async function getJSON(url) {
  const r = await fetch(url, { cache: "no-store" });
  if (r.status === 401) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    throw new Error("auth");
  }
  if (!r.ok) throw new Error(url + " " + r.status);
  return r.json();
}

async function postJSON(url) {
  const r = await fetch(url, { method: "POST", cache: "no-store" });
  if (r.status === 401) {
    location.href = "/login?next=" + encodeURIComponent(location.pathname + location.search);
    throw new Error("auth");
  }
  const body = await r.json().catch(() => ({}));
  if (!r.ok) throw new Error(body.detail || ("HTTP " + r.status));
  return body;
}

function toast(message, isError) {
  const wrap = $("toast-wrap");
  if (!wrap) return;
  const el = document.createElement("div");
  el.className = "toast" + (isError ? " err" : "");
  el.textContent = message;
  wrap.appendChild(el);
  setTimeout(() => {
    el.classList.add("out");
    setTimeout(() => el.remove(), 300);
  }, 3200);
}

/* ── Summary / control / feed ─────────────────────────────────────────── */
function setStat(id, value, extraClass) {
  const el = $(id);
  if (!el) return;
  el.textContent = money(value);
  el.className = "stat-value " + (extraClass || pnlClass(value));
}

function renderSummary(status) {
  const names = assetNames(status);
  const today = status.summary?.today || {};
  const total = status.summary?.total || {};
  setStat("today-pnl", today.pnl, pnlClass(today.pnl));
  $("today-sub").textContent = (today.n || 0) + " 笔已结算";
  setStat("total-pnl", total.pnl, pnlClass(total.pnl));
  $("total-sub").textContent = (total.n || 0) + " 笔已结算";

  const wins = Number(total.wins || 0);
  const losses = Number(total.losses || 0);
  const n = wins + losses;
  $("win-rate").textContent = n ? pct(wins / n) : "--";
  $("win-sub").textContent = wins + " 胜 / " + losses + " 负";

  const times = Object.values(status.assets || {})
    .map((a) => Number(a && a.time_left))
    .filter((x) => Number.isFinite(x));
  $("window-remain").textContent = timeStr(times.length ? Math.max(0, ...times) : 0);

  const active = Boolean(status.killswitch?.active);
  $("control-bar").classList.toggle("stopped", active);
  $("control-dot").className = "control-dot " + (active ? "stopped" : "running");
  $("control-label").textContent = active ? "策略已停止" : "策略运行中";
  const assetLabel = names.join("/") || "BTC/ETH";
  $("control-meta").textContent = active
    ? (status.killswitch?.reason || "手动停止")
    : "纸盘 · 自动扫描 " + assetLabel + " 5m 窗口";
  const assetSub = $("asset-sub");
  if (assetSub) assetSub.textContent = (names.join(" / ") || "BTC / ETH") + " 5m 窗口";
  $("stop-btn").hidden = active;
  $("start-btn").hidden = !active;

  const feeds = status.feeds || {};
  const rtds = feeds.rtds || {};
  const rtdsOk = Object.keys(rtds).length > 0 && Object.values(rtds).every((x) => x && x.connected);
  const ok = rtdsOk && Boolean(feeds.clob?.connected);
  $("live-dot").className = "live-dot" + (ok ? "" : " offline");
  $("live-label").textContent = ok ? "在线" : "离线";
}

/* ── Asset cards ──────────────────────────────────────────────────────── */
function tokenItem(cls, label, fair, cand) {
  const edge = cand ? Number(cand.edge) : null;
  const gate = cand && cand.gate
    ? ` <span class="pill idle">${cand.gate === "fair" ? "fair低" : "价低"}</span>`
    : "";
  return `<div class="token-item ${cls}">
    <div class="token-label">${label} · 公允概率</div>
    <div class="token-price">${fmt(fair, 3)}</div>
    <div class="muted mono">可买 ${fmt(cand && cand.avg_price, 3)} · Edge <span class="${edge > 0 ? "green" : "red"}">${fmt(edge, 3)}</span>${gate}</div>
  </div>`;
}

function fairDetail(detail) {
  if (!detail) return "";
  return `<div class="fair-detail">模型：预期结算 TWAP ${fmt(detail.e_close, 2)} · 剩余标准差 ${fmt(detail.sd, 5)} · z=${fmt(detail.z, 2)} · 已观测 ${fmt(detail.observed_s, 0)}s / 剩余 ${fmt(detail.remaining_s, 0)}s</div>`;
}

function assetCard(asset, data) {
  if (!data || !data.window) {
    return `<div class="price-card">
      <div class="price-head">
        <div class="price-title">${esc(asset)}</div>
        <span class="badge ${statusClass(data && data.status)}">${esc(statusLabel(data && data.status))}</span>
      </div>
      <div class="no-data">等待 Gamma 发现 5 分钟窗口…</div>
    </div>`;
  }

  const w = data.window;
  const p0 = data.price_to_beat !== undefined && data.price_to_beat !== null
    ? data.price_to_beat
    : w.price_to_beat;
  const fairUp = (data.fair_up === null || data.fair_up === undefined) ? null : Number(data.fair_up);
  const fairDown = fairUp === null ? null : 1 - fairUp;
  const fairPct = fairUp === null ? 50 : clamp(fairUp * 100, 0, 100);
  const cands = Array.isArray(data.candidates) ? data.candidates : [];
  const bestSide = data.best ? data.best.side : null;
  const up = cands.find((c) => c.side === "UP");
  const down = cands.find((c) => c.side === "DOWN");
  const threshold = data.params
    ? Number(data.params.min_edge) + Number(data.params.model_buffer)
    : null;
  const minFair = data.params ? Number(data.params.min_fair || 0) : 0;
  const minEntryPrice = data.params ? Number(data.params.min_entry_price || 0) : 0;
  const timeLeft = data.time_left !== undefined && data.time_left !== null
    ? data.time_left
    : Number(w.end_ts) - Date.now() / 1000;
  const position = data.position;

  const rows = cands.length
    ? cands.map((c) => {
        const edge = Number(c.edge);
        const gateTag = c.gate
          ? ` <span class="pill idle" title="被 min_fair / min_entry_price 过滤">${
              c.gate === "fair" ? "fair低" : "价低"
            }</span>`
          : "";
        return `<tr class="${c.side === bestSide ? "candidate-best" : ""}">
          <td><span class="pill ${c.side === "UP" ? "buy" : "sell"}">${esc(c.side)}</span>${gateTag}</td>
          <td class="mono">${fmt(c.fair, 3)}</td>
          <td class="mono">${fmt(c.best_bid, 3)}</td>
          <td class="mono">${fmt(c.best_ask, 3)}</td>
          <td class="mono">${fmt(c.spread, 3)}</td>
          <td class="mono">${fmt(c.avg_price, 3)}</td>
          <td class="mono ${edge > 0 ? "green" : "red"}">${fmt(c.edge, 3)}</td>
          <td class="mono">${fmt(c.shares, 1)}</td>
          <td class="mono">${fmt(c.age_s, 1)}</td>
        </tr>`;
      }).join("")
    : '<tr><td colspan="9" class="no-data">等待盘口…</td></tr>';

  const posHtml = position
    ? `<div class="gate-row" style="margin-top:0;padding-top:0;border-top:none">
        <span>持仓 <b>${esc(position.side)}</b></span>
        <span>份额 <b>${fmt(position.size, 2)}</b></span>
        <span>均价 <b>${fmt(position.avg_price, 3)}</b></span>
        <span>成本 <b>$${fmt(position.cost, 2)}</b></span>
        <span>标记价 <b>${fmt(position.mark, 3)}</b></span>
        <span>浮动盈亏 <b class="${pnlClass(position.unrealized)}">${money(position.unrealized)}</b></span>
      </div>`
    : '<div class="no-data">本窗口未持仓</div>';

  return `<div class="price-card">
    <div class="price-head">
      <div>
        <div class="price-title">${esc(asset)} <span class="muted">${esc(w.slug || "")}</span></div>
        <div class="muted" style="font-size:11px;margin-top:2px">开盘基准来源：${esc(p0SourceLabel(w.p0_source || data.p0_source))}</div>
      </div>
      <div class="text-right">
        <div class="asset-clock">${timeStr(timeLeft)}</div>
        <div class="muted" style="font-size:10px">剩余 · rel ${fmt(data.rel, 0)}s</div>
      </div>
    </div>
    <div class="status-line">
      <span class="badge ${statusClass(data.status)}">${esc(statusLabel(data.status))}</span>
      <span>今日盈亏 <span class="${pnlClass(data.daily_pnl)}">${money(data.daily_pnl)}</span></span>
      ${threshold !== null ? `<span>入场阈值 <b class="mono">${fmt(threshold, 3)}</b></span>` : ""}
    </div>
    <div class="price-bar-label"><span>UP <b>${fmt(fairUp, 3)}</b></span><span>DOWN <b>${fmt(fairDown, 3)}</b></span></div>
    <div class="price-bar"><div class="bar-up" style="width:${fairPct}%"></div><div class="bar-down" style="width:${100 - fairPct}%"></div></div>
    <div class="tokens">
      ${tokenItem("up", "UP", fairUp, up)}
      ${tokenItem("down", "DOWN", fairDown, down)}
    </div>
    <div class="gate-row">
      <span>开盘基准 <b>${fmt(p0, 2)}</b></span>
      <span>Chainlink 现价 <b>${fmt(data.spot, 2)}</b></span>
      <span>60s TWAP <b>${fmt(data.twap60, 2)}</b></span>
      <span>σ/秒 <b>${fmt(data.sigma_bps, 2)}</b> bps</span>
      <span>剩余 <b>${timeStr(timeLeft)}</b></span>
      ${minFair > 0 ? `<span>最小 Fair <b class="mono">${fmt(minFair, 2)}</b></span>` : ""}
      ${minEntryPrice > 0 ? `<span>最小入场价 <b class="mono">${fmt(minEntryPrice, 2)}</b></span>` : ""}
    </div>
    <div class="sub-section">
      <h2>盘口与候选</h2>
      <div class="table-wrap">
        <table>
          <thead><tr><th>方向</th><th>公允</th><th>买一</th><th>卖一</th><th>价差</th><th>成交均价</th><th>Edge</th><th>可买份额</th><th>盘口年龄</th></tr></thead>
          <tbody>${rows}</tbody>
        </table>
      </div>
      ${fairDetail(data.fair_detail)}
    </div>
    <div class="sub-section">
      <h2>当前持仓</h2>
      ${posHtml}
    </div>
  </div>`;
}

function renderAssets(status) {
  const el = $("asset-grid");
  const assets = (status && status.assets) || {};
  el.innerHTML = assetNames(status)
    .map((asset) => assetCard(asset, assets[asset] || null))
    .join("");
}

/* ── Tables ───────────────────────────────────────────────────────────── */
function renderPositions(positions) {
  const rows = (positions || []).map((p) => `<tr>
    <td>${esc(p.asset)}</td>
    <td class="mono">${utcTime(p.start_ts)}</td>
    <td><span class="pill ${p.side === "UP" ? "buy" : "sell"}">${esc(p.side)}</span></td>
    <td class="mono">${fmt(p.size, 2)}</td>
    <td class="mono">${fmt(p.avg_price, 3)}</td>
    <td class="mono">$${fmt(p.cost, 2)}</td>
    <td class="mono">$${fmt(p.fees, 4)}</td>
    <td>${p.status === "open" ? "持仓中" : "已结算"}</td>
  </tr>`).join("");
  $("positions-tbody").innerHTML = rows || '<tr><td colspan="8" class="no-data">无持仓</td></tr>';
  $("position-count").textContent = String((positions || []).length);
}

function renderFills(fills) {
  const rows = (fills || []).map((f) => {
    const outcome = f.window_outcome || f.position_outcome;
    const settled = f.position_status === "settled" && Boolean(outcome);
    const win = settled && f.position_side === outcome;
    const result = settled
      ? `${outcomePill(outcome)} <span class="${win ? "green" : "red"}">${win ? "赢" : "输"}</span>`
      : '<span class="muted">进行中</span>';
    const pnlValue = settled ? f.position_pnl : null;
    const roi = settled && Number(f.position_cost) > 0
      ? Number(f.position_pnl) / Number(f.position_cost)
      : null;
    const pnl = pnlValue === null || pnlValue === undefined
      ? "--"
      : `<span class="${pnlClass(pnlValue)}">${money(pnlValue)}</span>` +
        (roi === null ? "" : ` <span class="muted">(${pct(roi)})</span>`);
    return `<tr>
      <td class="mono">${utcTime(f.ts)}</td>
      <td>${esc(f.asset)}</td>
      <td><span class="pill ${f.side === "BUY" ? "buy" : "sell"}">${f.side === "BUY" ? "买入" : "卖出"}</span></td>
      <td>${outcomePill(f.position_side)}</td>
      <td class="mono">${fmt(f.price, 3)}</td>
      <td class="mono">${fmt(f.size, 2)}</td>
      <td class="mono">$${fmt(f.fee, 4)}</td>
      <td>${f.role === "TAKER" ? "Taker" : f.role === "MAKER" ? "Maker" : esc(f.role || "--")}</td>
      <td>${result}</td>
      <td class="mono">${pnl}</td>
    </tr>`;
  }).join("");
  $("fills-tbody").innerHTML = rows || '<tr><td colspan="10" class="no-data">暂无成交</td></tr>';
}

function renderSignals(signals) {
  const rows = (signals || []).map((s) => `<tr>
    <td class="mono">${utcTime(s.ts)}</td>
    <td>${esc(s.asset)}</td>
    <td><span class="pill ${s.side === "UP" ? "buy" : "sell"}">${esc(s.side || "--")}</span></td>
    <td class="mono">${fmt(s.fair, 3)}</td>
    <td class="mono">${fmt(s.ask, 3)}</td>
    <td class="mono ${Number(s.edge) > 0 ? "green" : "red"}">${fmt(s.edge, 3)}</td>
    <td>${s.acted ? '<span class="green">✓</span>' : "--"}</td>
    <td class="muted">${esc(String(s.reason || "").slice(0, 40))}</td>
  </tr>`).join("");
  $("signals-tbody").innerHTML = rows || '<tr><td colspan="8" class="no-data">暂无信号</td></tr>';
}

/* ── Pagination ───────────────────────────────────────────────────────── */
function updatePager(prefix, state) {
  const page = Math.floor(state.offset / state.limit) + 1;
  const pages = Math.max(1, Math.ceil(state.total / state.limit));
  const info = $(prefix + "-page-info");
  if (info) info.textContent = `第 ${page} / ${pages} 页 · 共 ${state.total} 条`;
  const prev = $(prefix + "-prev");
  const next = $(prefix + "-next");
  if (prev) prev.disabled = state.offset <= 0;
  if (next) next.disabled = state.offset + state.limit >= state.total;
}

async function loadFills() {
  const state = PAGERS.fills;
  const res = await getJSON(`/api/fills?limit=${state.limit}&offset=${state.offset}`);
  renderFills(res.data || []);
  state.total = Number(res.pagination?.total || 0);
  updatePager("fills", state);
}

async function loadSignals() {
  const state = PAGERS.signals;
  const res = await getJSON(`/api/signals?limit=${state.limit}&offset=${state.offset}`);
  renderSignals(res.data || []);
  state.total = Number(res.pagination?.total || 0);
  updatePager("signals", state);
}

/* ── Refresh loop ─────────────────────────────────────────────────────── */
let lastTables = 0;

async function refresh() {
  try {
    const status = await getJSON("/api/status");
    renderSummary(status);
    renderAssets(status);
    renderPositions(status.positions || []);
    if (Date.now() - lastTables > 5000) {
      lastTables = Date.now();
      await Promise.all([loadFills(), loadSignals()]);
    }
  } catch (e) {
    if (String(e.message) === "auth") return;
    $("live-label").textContent = "接口错误";
  }
}

/* ── Controls ─────────────────────────────────────────────────────────── */
function askStop() {
  const dlg = $("confirm-dialog");
  dlg.returnValue = "";
  dlg.showModal();
  return new Promise((resolve) => {
    dlg.addEventListener("close", () => resolve(dlg.returnValue === "ok"), { once: true });
  });
}

async function stopStrategy() {
  if (!(await askStop())) return;
  try {
    await postJSON("/api/control/stop");
    toast("已停止新入场；持仓仍会结算");
    await refresh();
  } catch (e) {
    toast("停止失败：" + e.message, true);
  }
}

async function startStrategy() {
  try {
    await postJSON("/api/control/start");
    toast("已恢复运行");
    await refresh();
  } catch (e) {
    toast("恢复失败：" + e.message, true);
  }
}

document.addEventListener("DOMContentLoaded", () => {
  $("stop-btn").addEventListener("click", stopStrategy);
  $("start-btn").addEventListener("click", startStrategy);
  $("fills-prev").addEventListener("click", () => {
    PAGERS.fills.offset = Math.max(0, PAGERS.fills.offset - PAGERS.fills.limit);
    loadFills().catch(() => {});
  });
  $("fills-next").addEventListener("click", () => {
    PAGERS.fills.offset += PAGERS.fills.limit;
    loadFills().catch(() => {});
  });
  $("signals-prev").addEventListener("click", () => {
    PAGERS.signals.offset = Math.max(0, PAGERS.signals.offset - PAGERS.signals.limit);
    loadSignals().catch(() => {});
  });
  $("signals-next").addEventListener("click", () => {
    PAGERS.signals.offset += PAGERS.signals.limit;
    loadSignals().catch(() => {});
  });
  $("clock").textContent = clockStr();
  setInterval(() => { $("clock").textContent = clockStr(); }, 1000);
  refresh();
  setInterval(refresh, 2000);
});
