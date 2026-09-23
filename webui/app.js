"use strict";

/* =========================================================
 * 闲鱼 AutoAgent 控制台前端
 * 纯原生 JS，无外部依赖。所有用户数据都经过 esc() 转义后再插入页面。
 * ========================================================= */

const $ = (sel, root = document) => root.querySelector(sel);
const $$ = (sel, root = document) => [...root.querySelectorAll(sel)];
const esc = (v) => String(v ?? "").replace(/[&<>"']/g, (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

const state = {
  user: null,
  page: "overview",
  status: {},
  logs: [],
  logSeq: 0,
  dirty: false,
  pollers: [],
};

/* ---------------- 工具 ---------------- */

function toast(text, type = "ok") {
  const el = $("#toast");
  el.textContent = text;
  el.className = "show " + type;
  clearTimeout(el._t);
  el._t = setTimeout(() => (el.className = ""), type === "error" ? 4200 : 2400);
}

async function api(path, body) {
  const opts = body === undefined ? {} : {
    method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body),
  };
  const res = await fetch(path, opts);
  let data = {};
  try { data = await res.json(); } catch {}
  if (res.status === 401) { showAuth(); throw new Error("登录已过期，请重新登录"); }
  if (!res.ok) throw new Error(data.error || "请求失败");
  return body === undefined ? data : data.data;
}

async function run(fn, okText) {
  try {
    const r = await fn();
    if (okText) toast(okText);
    return r;
  } catch (e) {
    toast(e.message, "error");
    throw e;
  }
}

function fmtTs(ts) {
  if (!ts) return "-";
  const d = new Date(ts * 1000);
  return d.toLocaleString("zh-CN", { hour12: false, month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" });
}
function fmtDb(ts) {
  if (!ts) return "-";
  const d = new Date(String(ts).replace(" ", "T") + "Z");
  return isNaN(d) ? ts : fmtTs(d.getTime() / 1000);
}
function ago(ts) {
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  return Math.floor(s / 86400) + " 天前";
}
function duration(ts) {
  const s = Math.floor(Date.now() / 1000 - ts);
  const h = Math.floor(s / 3600), m = Math.floor((s % 3600) / 60);
  return h ? `${h} 小时 ${m} 分` : `${m} 分钟`;
}
function formData(form) {
  const data = {};
  for (const el of form.elements) {
    if (!el.name) continue;
    if (el.type === "checkbox") {
      if (el.dataset.multi) (data[el.name] ||= []), el.checked && data[el.name].push(el.value);
      else data[el.name] = el.checked;
    } else data[el.name] = el.value;
  }
  return data;
}
function switchHtml(name, checked, label) {
  return `<label class="switch"><input type="checkbox" name="${name}" ${checked ? "checked" : ""}><span class="track"></span>${label ? `<span>${label}</span>` : ""}</label>`;
}
function emptyHtml(text, btn) {
  return `<div class="empty">${esc(text)}${btn ? `<div>${btn}</div>` : ""}</div>`;
}

/* ---------------- 弹窗 ---------------- */

function modal({ title, body, submitText = "保存", onSubmit, extraFoot = "", wide = false }) {
  const mask = document.createElement("div");
  mask.className = "modal-mask";
  mask.innerHTML = `
    <form class="modal" style="${wide ? "max-width:760px" : ""}" autocomplete="off">
      <div class="m-head"><h3>${esc(title)}</h3><button type="button" class="btn ghost sm" data-close>关闭</button></div>
      <div class="m-body">${body}</div>
      <div class="m-foot">${extraFoot}<span style="flex:1"></span><button type="button" class="btn" data-close>取消</button>
        <button type="submit" class="btn primary">${esc(submitText)}</button></div>
    </form>`;
  document.body.appendChild(mask);
  const form = $("form", mask);
  const close = () => mask.remove();
  $$("[data-close]", mask).forEach((b) => (b.onclick = close));
  mask.addEventListener("mousedown", (e) => { if (e.target === mask) close(); });
  form.onsubmit = async (e) => {
    e.preventDefault();
    const btn = $("button[type=submit]", form);
    btn.disabled = true;
    try {
      await onSubmit(formData(form), form);
      close();
    } catch (err) {
      toast(err.message, "error");
    } finally {
      btn.disabled = false;
    }
  };
  setTimeout(() => $("input:not([type=checkbox]), textarea, select", form)?.focus(), 30);
  return { form, close };
}

function confirmBox(text, onOk, okText = "确定") {
  modal({ title: "请确认", body: `<p style="margin:0 0 16px">${esc(text)}</p>`, submitText: okText, onSubmit: onOk });
}

/* ---------------- 图标 ---------------- */

const ICON_PATHS = {
  overview: "M3 13h8V3H3v10zm0 8h8v-6H3v6zm10 0h8V11h-8v10zm0-18v6h8V3h-8z",
  reply: "M21 12a8 8 0 0 1-11.6 7.1L4 21l1.9-5.4A8 8 0 1 1 21 12z",
  keywords: "M7 7h10M7 12h10M7 17h6M4 4h16v16H4z",
  prompts: "M4 19.5V5a2 2 0 0 1 2-2h14v16H6.5A2.5 2.5 0 0 0 4 21.5zM8 7h8M8 11h6",
  blacklist: "M12 3a9 9 0 1 0 0 18 9 9 0 0 0 0-18zM5.6 5.6l12.8 12.8",
  delivery: "M3 7l9-4 9 4-9 4-9-4zm0 0v10l9 4 9-4V7M12 11v10",
  items: "M20 7H4v13h16V7zM16 7V4H8v3M4 12h16",
  listings: "M12 5v14M5 12h14M4 4h16v16H4z",
  safety: "M12 3l8 3v6c0 5-3.5 8-8 9-4.5-1-8-4-8-9V6l8-3zm-3 9l2 2 4-4",
  chats: "M4 5h16v11H8l-4 4V5z",
  logs: "M5 4h14v16H5zM8 8h8M8 12h8M8 16h5",
  models: "M12 2l3 5 5 1-3.5 4 1 5.5L12 15l-5.5 2.5 1-5.5L4 8l5-1 3-5z",
  account: "M12 12a4 4 0 1 0 0-8 4 4 0 0 0 0 8zm-8 9a8 8 0 0 1 16 0",
  notify: "M18 16V11a6 6 0 1 0-12 0v5l-2 2h16l-2-2zM10 21h4",
  system: "M12 15a3 3 0 1 0 0-6 3 3 0 0 0 0 6zm8-3l2-1-2-4-2 .5-1.5-1.3L16 4h-4l-.5 2.2L10 7.5 8 7 6 11l2 1-2 1 2 4 2-.5 1.5 1.3L12 20h4l.5-2.2 1.5-1.3 2 .5 2-4-2-1z",
};
const icon = (name) => `<svg viewBox="0 0 24 24" fill="none" stroke="currentColor" stroke-width="1.8" stroke-linecap="round" stroke-linejoin="round"><path d="${ICON_PATHS[name] || ""}"/></svg>`;

/* ---------------- 页面注册 ---------------- */

const NAV = [
  ["总览", [["overview", "仪表盘"]]],
  ["客服中心", [["reply", "AI 自动回复"], ["keywords", "关键词回复"], ["prompts", "话术提示词"], ["blacklist", "黑名单"]]],
  ["商品与交易", [["items", "商品管理"], ["listings", "自动上架"], ["delivery", "自动发货"]]],
  ["数据中心", [["chats", "对话记录"], ["logs", "运行日志"]]],
  ["接入配置", [["models", "AI 模型"], ["account", "闲鱼账号"], ["notify", "消息通知"]]],
  ["安全中心", [["safety", "防风控模式"]]],
  ["系统", [["system", "系统设置"]]],
];
const PAGE_SUB = {
  overview: "运行状态与今日数据",
  reply: "AI 开关、人工接管、营业时间",
  keywords: "命中关键词时直接回复固定话术，优先于 AI",
  prompts: "决定 AI 怎么说话、怎么议价",
  blacklist: "黑名单买家的消息不回复、不通知",
  delivery: "买家付款后自动发送虚拟商品内容或卡密",
  items: "在售商品同步与自动擦亮",
  listings: "写好商品排队自动发布，AI 帮写文案",
  safety: "限速、静默、熔断，让自动化更像真人",
  chats: "每位买家的完整对话",
  logs: "机器人实时输出",
  models: "接入多个大模型，Key 轮换、失败自动切换",
  account: "闲鱼登录状态与 Cookie",
  notify: "把重要事件推送到手机",
  system: "启动方式、账号与安全、功能规划",
};
const PAGE_TITLE = Object.fromEntries(NAV.flatMap(([, items]) => items));

/* ---------------- 登录 / 注册 ---------------- */

async function boot() {
  const s = await fetch("/api/auth/state").then((r) => r.json());
  if (s.user) {
    state.user = s.user;
    renderApp();
  } else {
    showAuth(s.has_users);
  }
}

async function showAuth(hasUsers) {
  stopPolling();
  if (hasUsers === undefined) hasUsers = (await fetch("/api/auth/state").then((r) => r.json())).has_users;
  const register = !hasUsers;
  $("#root").innerHTML = `
    <div class="auth"><form class="auth-card" autocomplete="on">
      <div class="logo">🐟</div>
      <h1>${register ? "创建管理员账号" : "登录控制台"}</h1>
      <p>${register ? "第一次使用，请先注册一个账号，之后每次打开控制台需要登录。" : "闲鱼 AutoAgent · 智能客服与自动发货"}</p>
      <label class="field"><span>用户名</span><input type="text" name="username" autocomplete="username" required></label>
      <label class="field"><span>密码</span><input type="password" name="password" autocomplete="${register ? "new-password" : "current-password"}" required></label>
      ${register ? `<label class="field"><span>确认密码</span><input type="password" name="password2" autocomplete="new-password" required></label>` : ""}
      <button class="btn primary block" type="submit" style="height:40px">${register ? "注册并进入" : "登录"}</button>
      <div class="help" style="text-align:center;margin-top:14px">账号只保存在你自己电脑上的 data/console.db 里</div>
    </form></div>`;
  const form = $(".auth-card");
  form.onsubmit = async (e) => {
    e.preventDefault();
    const d = formData(form);
    if (register && d.password !== d.password2) return toast("两次输入的密码不一致", "error");
    try {
      await api(register ? "/api/auth/register" : "/api/auth/login", { username: d.username, password: d.password });
      boot();
    } catch (err) { toast(err.message, "error"); }
  };
}

/* ---------------- 主框架 ---------------- */

function renderApp() {
  const u = state.user;
  $("#root").innerHTML = `
  <div class="app">
    <aside class="sidebar" id="sidebar">
      <div class="brand"><div class="logo">🐟</div><div><b>闲鱼 AutoAgent</b><small>智能客服 · 自动发货</small></div></div>
      <nav class="nav">${NAV.map(([group, items]) => `
        <div class="nav-group"><div>${group}</div>
        ${items.map(([id, name]) => `<button data-page="${id}">${icon(id)}<span>${name}</span></button>`).join("")}
        </div>`).join("")}
      </nav>
      <div class="user-box">
        <div class="avatar">${esc(u.username.slice(0, 1).toUpperCase())}</div>
        <div><b>${esc(u.username)}</b><small>${u.is_admin ? "管理员" : "成员"}</small></div>
        <button class="btn ghost sm" id="logout" title="退出登录">退出</button>
      </div>
    </aside>
    <div class="main">
      <header class="topbar">
        <button class="btn sm menu-btn" id="menuBtn">☰</button>
        <div><h2 id="pageTitle"></h2><div class="sub" id="pageSub"></div></div>
        <span class="spacer"></span>
        <span class="bot-pill"><span class="dot" id="botDot"></span><span id="botText">读取中…</span></span>
        <button class="btn primary" id="botStart">启动机器人</button>
        <button class="btn" id="botRestart">重启</button>
        <button class="btn" id="botStop">停止</button>
      </header>
      <main class="content"><div class="page" id="page"></div></main>
    </div>
  </div>`;
  $$(".nav button").forEach((b) => (b.onclick = () => navigate(b.dataset.page)));
  $("#logout").onclick = async () => { await api("/api/auth/logout", {}); state.user = null; showAuth(true); };
  $("#menuBtn").onclick = () => $("#sidebar").classList.toggle("open");
  $("#botStart").onclick = () => botAction("start", "机器人启动中…");
  $("#botStop").onclick = () => botAction("stop", "机器人已停止");
  $("#botRestart").onclick = () => botAction("restart", "机器人重启中…");
  const initial = location.hash.slice(1);
  navigate(PAGE_TITLE[initial] ? initial : "overview", true);
  startPolling();
}

function navigate(page, force) {
  if (!force && state.dirty && !window.confirm("当前页面有未保存的修改，确定离开吗？")) return;
  clearInterval(state.pagePoll);
  state.dirty = false;
  state.page = page;
  location.hash = page;
  $$(".nav button").forEach((b) => b.classList.toggle("active", b.dataset.page === page));
  $("#pageTitle").textContent = PAGE_TITLE[page];
  $("#pageSub").textContent = PAGE_SUB[page] || "";
  $("#sidebar").classList.remove("open");
  const el = $("#page");
  el.innerHTML = `<div class="empty">加载中…</div>`;
  PAGES[page](el).catch((e) => (el.innerHTML = emptyHtml("加载失败：" + e.message)));
}

window.addEventListener("beforeunload", (e) => { if (state.dirty) { e.preventDefault(); e.returnValue = ""; } });

/* 表单改动后显示「未保存」提示条，保存前离开页面会被拦截 */
function trackDirty(form, onSave) {
  const bar = document.createElement("div");
  bar.className = "save-bar hidden";
  bar.innerHTML = `<span class="grow">● 有未保存的修改</span><button type="button" class="btn" data-reset>放弃修改</button><button type="button" class="btn primary" data-save>保存</button>`;
  form.appendChild(bar);
  const mark = () => { state.dirty = true; bar.classList.remove("hidden"); };
  form.addEventListener("input", mark);
  form.addEventListener("change", mark);
  const save = async () => {
    await run(() => onSave(formData(form)), "已保存");
    state.dirty = false;
    bar.classList.add("hidden");
  };
  $("[data-save]", bar).onclick = save;
  $("[data-reset]", bar).onclick = () => { state.dirty = false; navigate(state.page, true); };
  form.onsubmit = (e) => { e.preventDefault(); save(); };
}

/* ---------------- 机器人状态轮询 ---------------- */

async function botAction(action, text) {
  await run(() => api("/api/bot/" + action, {}), text);
  refreshStatus();
}

function renderStatus(s) {
  state.status = s;
  const dot = $("#botDot"), text = $("#botText");
  if (!dot) return;
  let cls = "", label;
  if (!s.running) {
    cls = s.last_exit_code ? "bad" : "";
    label = s.cookie_invalid ? "已停止 · Cookie 失效" : s.last_exit_code ? "已停止 · 异常退出" : "未运行";
  } else if (s.awaiting_cookie) { cls = "bad"; label = "等待更新 Cookie"; }
  else if (s.online) { cls = "good"; label = "运行中 · " + duration(s.started_at); }
  else { cls = "warn"; label = "连接中…"; }
  dot.className = "dot " + cls;
  text.textContent = label;
  $("#botStart").disabled = s.running;
  $("#botStop").disabled = !s.running;
  $("#botRestart").disabled = !s.running;
  const risk = $("#riskBanner");
  if (s.awaiting_cookie && !risk && $("#page")) {
    const div = document.createElement("div");
    div.id = "riskBanner";
    div.className = "banner bad";
    div.innerHTML = `<div class="grow"><b>⚠️ 触发闲鱼风控，机器人正在等待新的 Cookie</b>
      <div class="help">打开闲鱼网页版 → 点「消息」→ 拖动滑块验证 → 按 F12 重新复制完整 Cookie，粘贴到这里提交。</div>
      <textarea id="riskCookie" rows="2" style="margin-top:8px" placeholder="粘贴新的 Cookie"></textarea></div>
      <button class="btn primary" id="riskSubmit">提交</button>`;
    $("#page").prepend(div);
    $("#riskSubmit").onclick = () => run(() => api("/api/account/cookie", { cookie: $("#riskCookie").value }), "已提交新的 Cookie").then(() => div.remove());
  } else if (!s.awaiting_cookie && risk) risk.remove();
}

async function refreshStatus() {
  try { renderStatus(await api("/api/status")); } catch {}
}

async function pollLogs() {
  try {
    const data = await api("/api/logs?since=" + state.logSeq);
    if (data.last < state.logSeq) { state.logSeq = 0; return; }
    state.logSeq = data.last;
    if (data.lines.length) {
      state.logs.push(...data.lines);
      if (state.logs.length > 5000) state.logs.splice(0, state.logs.length - 5000);
      if (state.page === "logs") appendLogLines(data.lines);
    }
  } catch {}
}

function startPolling() {
  stopPolling();
  refreshStatus();
  pollLogs();
  state.pollers.push(setInterval(refreshStatus, 2500), setInterval(pollLogs, 1200));
}
function stopPolling() {
  state.pollers.forEach(clearInterval);
  state.pollers = [];
}

/* =========================================================
 * 各个页面
 * ========================================================= */

const PAGES = {};

/* ---------------- 仪表盘 ---------------- */

const EVENT_LABEL = { message: "买家消息", ai_reply: "AI 回复", keyword_reply: "关键词回复", away_reply: "离线回复", order: "买家付款", delivery: "自动发货", risk: "风控" };
const EVENT_BADGE = { message: "info", ai_reply: "good", keyword_reply: "accent", away_reply: "", order: "warn", delivery: "good", risk: "bad" };

PAGES.overview = async (el) => {
  const d = await api("/api/overview");
  const t = d.today;
  const acc = d.account;
  const steps = [
    [acc.has_cookie && acc.valid_format, "绑定闲鱼账号", acc.has_cookie ? (acc.nick || acc.user_id || "已填写 Cookie") : "填写闲鱼网页版 Cookie", "account"],
    [!!d.model, "接入 AI 模型", d.model ? `${d.model.name} · ${d.model.model}（共 ${d.model.count} 个可用）` : "至少启用一个模型", "models"],
    [d.counts.keywords > 0, "设置关键词回复（可选）", `${d.counts.keywords} 条规则生效中`, "keywords"],
    [d.counts.channels > 0, "开启手机通知（可选）", `${d.counts.channels} 个通知渠道`, "notify"],
    [d.bot.running && d.bot.online, "启动机器人", d.bot.running ? (d.bot.online ? "运行中" : "连接中") : "点右上角「启动机器人」", null],
  ];
  const max = Math.max(4, ...d.daily.map((x) => x.message));
  const niceMax = Math.ceil(max / 4) * 4;
  const hours = d.settings.business_hours;
  el.innerHTML = `
    ${d.safety.pause.paused ? `<div class="banner bad"><div class="grow"><b>🛡️ 防风控熔断中，擦亮和上架已暂停</b><span class="muted">${esc(d.safety.pause.reason)}（${fmtTs(d.safety.pause.until)} 恢复）</span></div><button class="btn sm" data-go="safety">查看</button></div>` : ""}
    ${!d.bot.running ? `<div class="banner info"><div class="grow"><b>机器人未运行</b><span class="muted">配置好闲鱼账号和 AI 模型后，点右上角「启动机器人」开始自动接待。</span></div></div>` : ""}
    <div class="stats">
      ${[["今日买家消息", t.message], ["今日咨询买家", d.buyers_today], ["AI 回复", t.ai_reply], ["关键词回复", t.keyword_reply], ["付款订单", t.order], ["自动发货", t.delivery]]
        .map(([k, v]) => `<div class="stat"><div class="label">${k}</div><div class="value">${v}</div></div>`).join("")}
    </div>
    <div class="two-col">
      <div class="card">
        <div class="card-head"><h3>近 7 天买家消息</h3><span class="desc">每天收到的买家消息条数</span></div>
        <div class="chart" role="img" aria-label="近7天买家消息柱状图">
          ${[1, 0.5].map((f) => `<div class="gridline" style="bottom:${28 + f * 174}px"><span>${Math.round(niceMax * f)}</span></div>`).join("")}
          ${d.daily.map((x) => `<div class="col"><div class="tip">${x.date}：买家消息 ${x.message} 条，自动回复 ${x.replies} 条</div>
            <div class="bar" style="height:${(x.message / niceMax) * 100}%"></div><span class="x">${x.date}</span></div>`).join("")}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>上线检查</h3></div>
        <div class="checklist">${steps.map(([ok, title, desc, page]) => `
          <div class="check-item"><span class="ic ${ok ? "ok" : "todo"}">${ok ? "✓" : "!"}</span>
          <div class="grow"><b>${title}</b><small>${esc(desc)}</small></div>
          ${page ? `<button class="btn sm" data-go="${page}">${ok ? "查看" : "去设置"}</button>` : ""}</div>`).join("")}
        </div>
      </div>
    </div>
    <div class="two-col">
      <div class="card">
        <div class="card-head"><h3>最近动态</h3><div class="actions"><button class="btn sm" data-go="logs">查看日志</button></div></div>
        <div class="feed">${d.recent.length ? d.recent.map((r) => `
          <div class="feed-item"><span class="badge ${EVENT_BADGE[r.type] || ""}">${EVENT_LABEL[r.type] || r.type}</span>
          <span class="grow">${esc(r.detail)}</span><time>${ago(r.created_at)}</time></div>`).join("") : emptyHtml("还没有动态，机器人运行后这里会显示买家消息和回复")}
        </div>
      </div>
      <div class="card">
        <div class="card-head"><h3>功能状态</h3></div>
        <div class="checklist">
          ${[
            ["AI 自动回复", d.settings.ai_enabled ? `<span class="badge good">已开启</span>` : `<span class="badge">已关闭</span>`, "reply"],
            ["营业时间", hours.enabled ? `<span class="badge info">${esc(hours.start)} - ${esc(hours.end)}</span>` : `<span class="badge">全天接待</span>`, "reply"],
            ["关键词回复", `<span class="badge ${d.counts.keywords ? "accent" : ""}">${d.counts.keywords} 条</span>`, "keywords"],
            ["自动发货", `<span class="badge ${d.counts.delivery ? "good" : ""}">${d.counts.delivery} 条规则</span>`, "delivery"],
            ["防风控模式", d.safety.pause.paused ? `<span class="badge bad">熔断暂停中</span>` : `<span class="badge good">${esc(d.safety.params.label)}</span>`, "safety"],
            ["自动擦亮", d.settings.auto_polish.enabled ? `<span class="badge good">每天 ${esc(d.settings.auto_polish.window_start)}-${esc(d.settings.auto_polish.window_end)}</span>` : `<span class="badge">未开启</span>`, "items"],
            ["异常自动重启", d.settings.auto_restart ? `<span class="badge good">已开启</span>` : `<span class="badge">已关闭</span>`, "system"],
          ].map(([name, badge, page]) => `<div class="check-item"><div class="grow"><b>${name}</b></div>${badge}
            ${page ? `<button class="btn sm ghost" data-go="${page}">设置</button>` : ""}</div>`).join("")}
        </div>
      </div>
    </div>`;
  $$("[data-go]", el).forEach((b) => (b.onclick = () => navigate(b.dataset.go)));
};

/* ---------------- AI 自动回复 ---------------- */

PAGES.reply = async (el) => {
  const s = await api("/api/settings");
  const h = s.business_hours;
  el.innerHTML = `
    <div class="banner info"><div class="grow"><b>🛡️ 防风控发送延迟已固定开启</b>
      <span class="muted">每条自动回复会等待 1.5～12 秒再发出（按字数模拟真人打字），不同买家之间并行处理、互不排队。这项保护写在代码里，不能关闭。</span></div></div>
    <form id="replyForm">
      <div class="card">
        <div class="card-head"><h3>AI 自动回复</h3><span class="desc">改完点保存，几秒内生效，不用重启机器人</span></div>
        <div style="margin-bottom:18px">${switchHtml("ai_enabled", s.ai_enabled, "开启 AI 自动回复（关闭后只处理关键词回复和自动发货）")}</div>
        <div class="grid-2">
          <label class="field"><span>人工接管关键词</span><input type="text" name="toggle_keywords" value="${esc(s.toggle_keywords)}">
            <div class="help">你在闲鱼里对某个买家发送这个内容，该对话切换为人工回复；再发一次交还给 AI。</div></label>
          <label class="field"><span>人工接管自动结束（分钟）</span><input type="number" min="1" name="manual_timeout_minutes" value="${esc(s.manual_timeout_minutes)}">
            <div class="help">超过这个时间没有切回，也会自动交还给 AI。</div></label>
        </div>
        <label class="field"><span>AI 故障兜底话术</span><input type="text" name="fallback_reply" value="${esc(s.fallback_reply)}" placeholder="例如：亲，稍等一下，马上回复您~（留空则不回复）">
          <div class="help">所有 AI 模型都调用失败时发给买家，避免买家长时间没人理。</div></label>
      </div>
      <div class="card">
        <div class="card-head"><h3>营业时间</h3><span class="desc">营业时间外可以发离线提示或不回复</span></div>
        <div style="margin-bottom:18px">${switchHtml("bh_enabled", h.enabled, "启用营业时间")}</div>
        <div class="grid-3">
          <label class="field"><span>开始时间</span><input type="time" name="bh_start" value="${esc(h.start)}"></label>
          <label class="field"><span>结束时间</span><input type="time" name="bh_end" value="${esc(h.end)}"><div class="help">结束早于开始表示跨夜，例如 20:00～02:00</div></label>
          <label class="field"><span>非营业时间</span><select name="bh_mode">
            <option value="away" ${h.mode === "away" ? "selected" : ""}>发送离线提示</option>
            <option value="silent" ${h.mode === "silent" ? "selected" : ""}>不回复</option></select></label>
        </div>
        <label class="field"><span>离线提示内容</span><textarea name="bh_away_message" rows="2">${esc(h.away_message)}</textarea>
          <div class="help">同一个买家 6 小时内只提示一次。</div></label>
      </div>
      <button class="btn primary" type="submit">保存设置</button>
    </form>`;
  trackDirty($("#replyForm"), (d) => api("/api/settings", {
    ai_enabled: d.ai_enabled, toggle_keywords: d.toggle_keywords, manual_timeout_minutes: Number(d.manual_timeout_minutes),
    fallback_reply: d.fallback_reply,
    business_hours: { enabled: d.bh_enabled, start: d.bh_start, end: d.bh_end, mode: d.bh_mode, away_message: d.bh_away_message },
  }));
};

/* ---------------- 关键词回复 ---------------- */

PAGES.keywords = async (el) => {
  const { rules, match_types } = await api("/api/keywords");
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h3>关键词规则</h3><span class="desc">买家消息命中关键词时，直接回复固定内容，不调用 AI</span>
        <div class="actions"><button class="btn primary" id="addKw">＋ 新增规则</button></div></div>
      ${rules.length ? `<div class="table-wrap"><table>
        <thead><tr><th>关键词</th><th>匹配方式</th><th>回复内容</th><th>限定商品</th><th>命中</th><th>状态</th><th></th></tr></thead>
        <tbody>${rules.map((r) => `<tr>
          <td><b>${esc(r.keyword)}</b></td><td>${esc(match_types[r.match_type])}</td>
          <td><div class="clip" title="${esc(r.reply)}">${esc(r.reply)}</div></td>
          <td class="mono">${esc(r.item_id) || `<span class="muted">全部商品</span>`}</td><td>${r.hits}</td>
          <td>${r.enabled ? `<span class="badge good">启用</span>` : `<span class="badge">停用</span>`}</td>
          <td class="actions"><button class="btn sm" data-edit="${r.id}">编辑</button><button class="btn sm danger" data-del="${r.id}">删除</button></td>
        </tr>`).join("")}</tbody></table></div>`
      : emptyHtml("还没有关键词规则。常见用法：「包邮吗」「在吗」「怎么发货」这类高频问题用固定话术回复，又快又省 AI 费用。")}
    </div>`;
  const edit = (r = {}) => modal({
    title: r.id ? "编辑关键词规则" : "新增关键词规则",
    body: `
      <label class="field"><span>关键词</span><input type="text" name="keyword" value="${esc(r.keyword)}" required>
        <div class="help">「包含」模式下用 | 分隔多个词，例如：包邮|邮费|运费</div></label>
      <div class="grid-2">
        <label class="field"><span>匹配方式</span><select name="match_type">${Object.entries(match_types).map(([k, v]) => `<option value="${k}" ${r.match_type === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
        <label class="field"><span>限定商品 ID（可选）</span><input type="text" name="item_id" value="${esc(r.item_id)}" placeholder="留空对所有商品生效"></label>
      </div>
      <label class="field"><span>回复内容</span><textarea name="reply" rows="4" required>${esc(r.reply)}</textarea></label>
      ${switchHtml("enabled", r.id ? !!r.enabled : true, "启用")}`,
    onSubmit: async (d) => { await api("/api/keywords/save", { ...d, id: r.id }); toast("规则已保存"); navigate("keywords", true); },
  });
  $("#addKw").onclick = () => edit({ item_id: state.prefillItem || "" });
  state.prefillItem = null;
  $$("[data-edit]", el).forEach((b) => (b.onclick = () => edit(rules.find((r) => r.id == b.dataset.edit))));
  $$("[data-del]", el).forEach((b) => (b.onclick = () => confirmBox("确定删除这条关键词规则吗？", async () => {
    await api("/api/keywords/delete", { id: b.dataset.del }); toast("已删除"); navigate("keywords", true);
  }, "删除")));
};

/* ---------------- 提示词 ---------------- */

PAGES.prompts = async (el) => {
  const list = await api("/api/prompts");
  let current = state.promptTab || list[0].name;
  const render = () => {
    const p = list.find((x) => x.name === current);
    el.innerHTML = `
      <div class="card">
        <div class="tabs">${list.map((x) => `<button data-tab="${x.name}" class="${x.name === current ? "active" : ""}">${x.label}</button>`).join("")}</div>
        <div class="card-head"><div><h3>${esc(p.label)} ${p.custom ? `<span class="badge accent">已自定义</span>` : `<span class="badge">使用示例模板</span>`}</h3>
          <div class="desc">${esc(p.hint)}</div></div>
          <div class="actions">${p.custom ? `<button class="btn" id="resetPrompt">恢复默认模板</button>` : ""}</div></div>
        <form id="promptForm"><textarea class="prompt-editor" name="content" spellcheck="false">${esc(p.content)}</textarea>
          <div class="help" style="margin:10px 0 14px">修改提示词后需要点右上角「重启」才会生效。</div>
          <button class="btn primary" type="submit">保存提示词</button></form>
      </div>`;
    $$("[data-tab]", el).forEach((b) => (b.onclick = () => {
      if (state.dirty && !window.confirm("当前提示词还没保存，确定切换吗？")) return;
      state.dirty = false; current = state.promptTab = b.dataset.tab; render();
    }));
    trackDirty($("#promptForm"), async (d) => { await api("/api/prompts/save", { name: current, content: d.content }); p.custom = true; p.content = d.content; });
    $("#resetPrompt")?.addEventListener("click", () => confirmBox("恢复为示例模板后，你自定义的内容会被删除，确定吗？", async () => {
      await api("/api/prompts/reset", { name: current }); toast("已恢复默认模板"); state.dirty = false; navigate("prompts", true);
    }));
  };
  render();
};

/* ---------------- 黑名单 ---------------- */

PAGES.blacklist = async (el) => {
  const list = await api("/api/blacklist");
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h3>黑名单</h3><span class="desc">可以在「对话记录」里一键拉黑买家</span>
        <div class="actions"><button class="btn primary" id="addBl">＋ 添加</button></div></div>
      ${list.length ? `<div class="table-wrap"><table><thead><tr><th>买家 ID</th><th>备注</th><th>加入时间</th><th></th></tr></thead>
        <tbody>${list.map((r) => `<tr><td class="mono">${esc(r.user_id)}</td><td>${esc(r.note) || "-"}</td><td>${fmtTs(r.created_at)}</td>
          <td class="actions"><button class="btn sm danger" data-del="${esc(r.user_id)}">移除</button></td></tr>`).join("")}</tbody></table></div>`
      : emptyHtml("黑名单为空")}
    </div>`;
  $("#addBl").onclick = () => modal({
    title: "添加黑名单",
    body: `<label class="field"><span>买家 ID</span><input type="text" name="user_id" required></label>
      <label class="field"><span>备注</span><input type="text" name="note" placeholder="例如：恶意砍价"></label>`,
    onSubmit: async (d) => { await api("/api/blacklist/add", d); toast("已加入黑名单"); navigate("blacklist", true); },
  });
  $$("[data-del]", el).forEach((b) => (b.onclick = async () => {
    await run(() => api("/api/blacklist/delete", { user_id: b.dataset.del }), "已移除"); navigate("blacklist", true);
  }));
};

/* ---------------- 自动发货 ---------------- */

PAGES.delivery = async (el) => {
  const { rules, records, modes } = await api("/api/delivery");
  el.innerHTML = `
    <div class="banner warn"><div class="grow"><b>工作方式（Beta）</b>
      <span class="muted">买家付款后，闲鱼会在会话里推送「我已付款，等待你发货」，机器人识别到后按规则把内容发给买家（人工接管中也会发）。
      同一买家同一商品 24 小时内只发一次。发送内容后仍需你在闲鱼里点「发货」完成订单。建议先用小额商品实测一次。</span></div></div>
    <div class="card">
      <div class="card-head"><h3>发货规则</h3><span class="desc">按商品 ID 精确匹配，或按商品标题关键词匹配</span>
        <div class="actions"><button class="btn primary" id="addRule">＋ 新增规则</button></div></div>
      ${rules.length ? `<div class="table-wrap"><table>
        <thead><tr><th>规则名称</th><th>匹配商品</th><th>发货方式</th><th>库存</th><th>已发货</th><th>状态</th><th></th></tr></thead>
        <tbody>${rules.map((r) => `<tr>
          <td><b>${esc(r.name)}</b></td>
          <td>${r.item_id ? `<span class="mono">${esc(r.item_id)}</span>` : `标题含「${esc(r.title_keyword)}」`}</td>
          <td>${r.mode === "cards" ? `<span class="badge info">卡密</span>` : `<span class="badge">固定内容</span>`}</td>
          <td>${r.mode === "cards" ? `<span class="badge ${r.stock_count <= 3 ? "bad" : "good"}">${r.stock_count} 条</span>` : "不限"}</td>
          <td>${r.delivered}</td>
          <td>${r.enabled ? `<span class="badge good">启用</span>` : `<span class="badge">停用</span>`}</td>
          <td class="actions"><button class="btn sm" data-edit="${r.id}">编辑</button><button class="btn sm danger" data-del="${r.id}">删除</button></td>
        </tr>`).join("")}</tbody></table></div>`
      : emptyHtml("还没有发货规则。适合虚拟商品：网盘资料、激活码、会员卡密、教程链接等。")}
    </div>
    <div class="card">
      <div class="card-head"><h3>发货记录</h3><span class="desc">最近 200 条</span></div>
      ${records.length ? `<div class="table-wrap"><table><thead><tr><th>时间</th><th>买家</th><th>商品</th><th>规则</th><th>发送内容</th></tr></thead>
        <tbody>${records.map((r) => `<tr><td class="nowrap">${fmtTs(r.created_at)}</td><td>${esc(r.buyer_name || r.buyer_id)}</td>
          <td class="mono">${esc(r.item_id)}</td><td>${esc(r.rule_name)}</td><td><div class="clip" title="${esc(r.content)}">${esc(r.content)}</div></td></tr>`).join("")}
        </tbody></table></div>` : emptyHtml("暂无发货记录")}
    </div>`;
  const edit = (r = {}) => {
    const m = modal({
      title: r.id ? "编辑发货规则" : "新增发货规则", wide: true,
      body: `
        <label class="field"><span>规则名称</span><input type="text" name="name" value="${esc(r.name)}" placeholder="例如：Python 教程网盘" required></label>
        <div class="grid-2">
          <label class="field"><span>商品 ID</span><input type="text" name="item_id" value="${esc(r.item_id)}" placeholder="精确匹配，推荐">
            <div class="help">在「商品库」或闲鱼商品链接里的 itemId 找到</div></label>
          <label class="field"><span>或 商品标题关键词</span><input type="text" name="title_keyword" value="${esc(r.title_keyword)}" placeholder="标题包含该词即匹配"></label>
        </div>
        <label class="field"><span>发货方式</span><select name="mode">${Object.entries(modes).map(([k, v]) => `<option value="${k}" ${r.mode === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
        <label class="field"><span data-content-label>发货内容</span><textarea name="content" rows="4" placeholder="例如：感谢购买！资料链接：https://pan.xxx.com/... 提取码：1234">${esc(r.content)}</textarea></label>
        <label class="field" data-stock><span>卡密库存（一行一条）</span><textarea name="stock" rows="6" class="mono" placeholder="KEY-AAAA-0001&#10;KEY-AAAA-0002">${esc((r.stock || []).join("\n"))}</textarea>
          <div class="help">每笔订单发出第一条并从库存中移除；剩余 3 条及以下时会推送库存预警。</div></label>
        ${switchHtml("enabled", r.id ? !!r.enabled : true, "启用")}`,
      onSubmit: async (d) => { await api("/api/delivery/save", { ...d, id: r.id }); toast("规则已保存"); navigate("delivery", true); },
    });
    const sync = () => {
      const cards = m.form.elements.mode.value === "cards";
      $("[data-stock]", m.form).classList.toggle("hidden", !cards);
      $("[data-content-label]", m.form).textContent = cards ? "卡密前面的说明文字（可选）" : "发货内容";
    };
    m.form.elements.mode.onchange = sync;
    sync();
  };
  $("#addRule").onclick = () => edit({ item_id: state.prefillItem || "" });
  state.prefillItem = null;
  $$("[data-edit]", el).forEach((b) => (b.onclick = () => edit(rules.find((r) => r.id == b.dataset.edit))));
  $$("[data-del]", el).forEach((b) => (b.onclick = () => confirmBox("确定删除这条发货规则吗？剩余卡密也会一起删除。", async () => {
    await api("/api/delivery/delete", { id: b.dataset.del }); toast("已删除"); navigate("delivery", true);
  }, "删除")));
};

/* ---------------- 防风控模式 ---------------- */

const SAFETY_ROWS = [
  ["reply_delay_factor", "自动回复延迟", (v) => `基础延迟 × ${v}`],
  ["first_reply_extra", "新买家首条回复额外等待", (v) => `${v[0]}～${v[1]} 秒`],
  ["buyer_hourly_limit", "同一买家每小时自动回复上限", (v) => `${v} 条`],
  ["publish_daily_limit", "每天自动上架上限", (v) => `${v} 个`],
  ["publish_interval_minutes", "两次上架最短间隔", (v) => `${v} 分钟`],
  ["polish_gap", "擦亮商品之间的间隔", (v) => `${v[0]}～${v[1]} 秒`],
  ["quiet_start", "夜间静默（不擦亮、不上架）", (v, p) => `${p.quiet_start} - ${p.quiet_end}`],
  ["pause_hours", "触发风控后暂停后台任务", (v) => `${v} 小时`],
];

PAGES.safety = async (el) => {
  const st = await api("/api/safety");
  const levels = Object.entries(st.levels);
  el.innerHTML = `
    ${st.pause.paused ? `<div class="banner bad"><div class="grow"><b>🛡️ 熔断中：擦亮、上架已暂停</b>
      <span>${esc(st.pause.reason)}，将在 ${fmtTs(st.pause.until)} 自动恢复。建议先打开闲鱼网页版过一下滑块，再更新 Cookie。</span></div>
      <button class="btn" id="resumeBtn">我已处理，立即恢复</button></div>`
      : `<div class="banner info"><div class="grow"><b>🛡️ 防风控保护运行中</b>
      <span class="muted">当前模式「${esc(st.params.label)}」${st.quiet ? " · 现在是夜间静默时段" : ""} · 今天已擦亮 ${st.today.polish} 次、自动上架 ${st.today.publish}/${st.params.publish_daily_limit} 个</span></div></div>`}
    <div class="card">
      <div class="card-head"><h3>选择防风控模式</h3><span class="desc">切换后立即生效，自动回复、擦亮、上架都会按新规则执行</span></div>
      <div class="roadmap">${levels.map(([key, p]) => `
        <div class="road level ${key === st.level ? "selected" : ""}" data-level="${key}" role="button" tabindex="0">
          <b>${key === st.level ? `<span class="badge accent">当前</span>` : ""}${esc(p.label)}</b><p>${esc(p.desc)}</p></div>`).join("")}
      </div>
      <div class="table-wrap" style="margin-top:16px"><table>
        <thead><tr><th>规则</th>${levels.map(([key, p]) => `<th class="${key === st.level ? "hl" : ""}">${esc(p.label)}</th>`).join("")}</tr></thead>
        <tbody>${SAFETY_ROWS.map(([k, name, fmt]) => `<tr><td>${name}</td>${levels.map(([key, p]) => `<td class="${key === st.level ? "hl" : ""}">${esc(fmt(p[k], p))}</td>`).join("")}</tr>`).join("")}</tbody>
      </table></div>
    </div>
    <div class="two-col">
      <div class="card">
        <div class="card-head"><h3>始终开启的保护</h3><span class="desc">写在代码里，不能关闭</span></div>
        <div class="checklist">${[
          ["固定发送延迟", "每条自动回复都按字数模拟打字，最少等 1.5 秒，不会秒回"],
          ["风控自动熔断", "闲鱼一旦要求滑块验证或返回风控码，立即暂停所有后台任务并推送通知"],
          ["后台任务串行", "擦亮、上架一次只做一件事，顺序随机、间隔随机，每天的擦亮时间也随机"],
          ["和网页端一致的请求", "使用与闲鱼网页版相同的签名方式和浏览器标识，不伪造设备、不切换 IP"],
          ["防刷屏", "同一买家短时间消息过多自动停止回复并提醒你；同一订单不重复发货"],
          ["敏感词过滤", "AI 回复中出现微信、QQ、支付宝等站外引导词会被替换，避免违规"],
        ].map(([t, d]) => `<div class="check-item"><span class="ic ok">✓</span><div class="grow"><b>${t}</b><small>${d}</small></div></div>`).join("")}</div>
      </div>
      <div class="card">
        <div class="card-head"><h3>日常建议</h3></div>
        <ol style="margin:0;padding-left:20px;color:var(--text-2);line-height:2">
          <li>新账号先手动正常使用一两周，再逐步开启自动化，先用「谨慎」模式</li>
          <li>一天内不要集中上架大量商品，同类商品标题和图片不要完全一样</li>
          <li>控制台尽量放在你平时登录闲鱼的电脑和网络上运行</li>
          <li>收到风控通知后，先在网页版过滑块、更新 Cookie，别急着恢复</li>
          <li>擦亮每个商品每天一次就够，多擦没有用反而增加风险</li>
          <li>自动化只能降低风险，无法保证绝对不被识别，请遵守闲鱼平台规则</li>
        </ol>
      </div>
    </div>`;
  $$("[data-level]", el).forEach((b) => (b.onclick = async () => {
    if (b.dataset.level === st.level) return;
    await run(() => api("/api/safety/level", { level: b.dataset.level }), "已切换防风控模式");
    navigate("safety", true);
  }));
  $("#resumeBtn")?.addEventListener("click", () => confirmBox("确定已经在闲鱼网页版处理过验证了吗？过早恢复可能再次触发风控。", async () => {
    await api("/api/safety/resume", {}); toast("已恢复"); navigate("safety", true);
  }, "立即恢复"));
};

/* ---------------- 商品管理 ---------------- */

function taskBar(task) {
  if (!task || !task.name) return "";
  return `<div class="banner ${task.running ? "info" : "warn"}" style="margin-bottom:16px"><div class="grow">
    <b>${task.running ? "⏳ " : ""}${esc(task.name)}${task.running ? "进行中" : ""}</b><span class="muted">${esc(task.progress)}</span></div></div>`;
}

PAGES.items = async (el) => {
  const tab = state.itemsTab || "mine";
  const d = await api("/api/shop");
  const conf = d.settings;
  el.innerHTML = `
    <div id="taskBar">${taskBar(d.task)}</div>
    <div class="tabs"><button data-tab="mine" class="${tab === "mine" ? "active" : ""}">我的在售商品</button>
      <button data-tab="asked" class="${tab === "asked" ? "active" : ""}">买家咨询过的商品</button></div>
    <div id="itemsBody"></div>`;
  $$("[data-tab]", el).forEach((b) => (b.onclick = () => { state.itemsTab = b.dataset.tab; navigate("items", true); }));
  const body = $("#itemsBody");
  if (tab === "asked") {
    const items = await api("/api/items");
    body.innerHTML = `<div class="card">${items.length ? `<div class="table-wrap"><table><thead><tr><th>商品</th><th>价格</th><th>商品 ID</th><th>收录时间</th><th></th></tr></thead>
      <tbody>${items.map((i) => `<tr><td><b>${esc(i.title) || "（无标题）"}</b><div class="muted clip">${esc(i.desc)}</div></td>
        <td class="nowrap">¥ ${esc(i.price ?? "-")}</td><td class="mono">${esc(i.item_id)}</td><td class="nowrap">${fmtDb(i.last_updated)}</td>
        <td class="actions">${itemActions(i.item_id)}</td></tr>`).join("")}</tbody></table></div>`
      : emptyHtml("还没有商品。机器人运行并接待买家后，买家咨询的商品会自动出现在这里。")}</div>`;
    bindItemActions(body);
    return;
  }
  body.innerHTML = `
    <form class="card" id="polishForm">
      <div class="card-head"><h3>自动擦亮</h3><span class="desc">每天在时间窗口内随机挑一个时间，把所有在售商品擦亮一遍，提高曝光</span></div>
      <div style="margin-bottom:16px">${switchHtml("enabled", conf.enabled, "开启每日自动擦亮")}</div>
      <div class="grid-3">
        <label class="field"><span>最早开始</span><input type="time" name="window_start" value="${esc(conf.window_start)}"></label>
        <label class="field"><span>最晚开始</span><input type="time" name="window_end" value="${esc(conf.window_end)}"></label>
        <label class="field"><span>今天的执行时间</span><input type="text" disabled value="${conf.enabled ? esc(d.next_polish || "等待控制台安排") : "未开启"}"></label>
      </div>
      <div class="help" style="margin:-6px 0 14px">商品之间会间隔 ${d.safety.params.polish_gap[0]}～${d.safety.params.polish_gap[1]} 秒（由防风控模式决定），夜间静默时段不执行。</div>
      <button class="btn primary" type="submit">保存擦亮设置</button>
    </form>
    <div class="card">
      <div class="card-head"><h3>在售商品</h3><span class="desc">${d.items.length ? `共 ${d.items.length} 个` : "先点「同步在售商品」从闲鱼拉取"}</span>
        <div class="actions"><button class="btn" id="syncBtn">同步在售商品</button><button class="btn primary" id="polishAll" ${d.items.length ? "" : "disabled"}>立即全部擦亮</button></div></div>
      ${d.items.length ? `<div class="table-wrap"><table><thead><tr><th></th><th>商品</th><th>价格</th><th>最近擦亮</th><th></th></tr></thead>
        <tbody>${d.items.map((i) => `<tr>
          <td style="width:64px">${i.pic_url ? `<img src="${esc(i.pic_url)}" alt="" style="width:48px;height:48px;border-radius:8px;object-fit:cover" referrerpolicy="no-referrer">` : ""}</td>
          <td><b>${esc(i.title)}</b><div class="muted mono" style="font-size:12px">${esc(i.item_id)}</div></td>
          <td class="nowrap">${esc(i.price)}</td>
          <td class="nowrap">${i.last_polished_at ? `${ago(i.last_polished_at)} · <span class="${i.last_polish_result === "成功" ? "" : "muted"}">${esc(i.last_polish_result)}</span>` : "-"}</td>
          <td class="actions"><button class="btn sm" data-polish="${esc(i.item_id)}">擦亮</button>${itemActions(i.item_id)}</td></tr>`).join("")}
        </tbody></table></div>` : emptyHtml("还没有同步商品")}
    </div>`;
  trackDirty($("#polishForm"), (f) => api("/api/shop/settings", { enabled: f.enabled, window_start: f.window_start, window_end: f.window_end }));
  $("#syncBtn").onclick = async () => { await run(() => api("/api/shop/sync", {}), "开始同步"); pollTask(); };
  $("#polishAll").onclick = () => confirmBox(`现在擦亮全部 ${d.items.length} 个商品？会按防风控间隔逐个执行，大约需要 ${Math.ceil(d.items.length * (d.safety.params.polish_gap[0] + d.safety.params.polish_gap[1]) / 2 / 60)} 分钟。`, async () => {
    await api("/api/shop/polish_all", {}); toast("开始擦亮"); pollTask();
  }, "开始擦亮");
  $$("[data-polish]", el).forEach((b) => (b.onclick = async () => {
    b.disabled = true;
    const r = await run(() => api("/api/shop/polish", { item_id: b.dataset.polish })).catch(() => null);
    if (r) toast(r === "成功" ? "擦亮成功" : r, r === "成功" ? "ok" : "error");
    navigate("items", true);
  }));
  bindItemActions(el);
  if (d.task.running) pollTask();
};

function itemActions(itemId) {
  return `<a class="btn sm" href="https://www.goofish.com/item?id=${encodeURIComponent(itemId)}" target="_blank" rel="noopener">查看</a>
    <button class="btn sm" data-kw="${esc(itemId)}">关键词</button><button class="btn sm" data-dl="${esc(itemId)}">发货规则</button>`;
}
function bindItemActions(root) {
  $$("[data-kw]", root).forEach((b) => (b.onclick = () => { state.prefillItem = b.dataset.kw; navigate("keywords"); setTimeout(() => $("#addKw")?.click(), 300); }));
  $$("[data-dl]", root).forEach((b) => (b.onclick = () => { state.prefillItem = b.dataset.dl; navigate("delivery"); setTimeout(() => $("#addRule")?.click(), 300); }));
}

/* 后台任务进行中时，每 3 秒刷新一次任务进度，结束后刷新页面 */
function pollTask() {
  clearInterval(state.pagePoll);
  const page = state.page;
  state.pagePoll = setInterval(async () => {
    const d = await api(page === "listings" ? "/api/listings" : "/api/shop").catch(() => null);
    if (!d || state.page !== page) return clearInterval(state.pagePoll);
    const bar = $("#taskBar");
    if (bar) bar.innerHTML = taskBar(d.task);
    if (!d.task.running) { clearInterval(state.pagePoll); if (!state.dirty) navigate(page, true); }
  }, 3000);
}

/* ---------------- 自动上架 ---------------- */

const LISTING_BADGE = { draft: "", queued: "info", publishing: "warn", published: "good", failed: "bad" };

PAGES.listings = async (el) => {
  const d = await api("/api/listings");
  const sp = d.safety;
  el.innerHTML = `
    <div id="taskBar">${taskBar(d.task)}</div>
    <div class="banner warn"><div class="grow"><b>自动上架（Beta）</b>
      <span class="muted">排队中的商品由控制台按防风控规则逐个发布：今天已上架 ${sp.today.publish}/${sp.params.publish_daily_limit} 个，两次间隔至少 ${sp.params.publish_interval_minutes} 分钟，夜间 ${sp.params.quiet_start}-${sp.params.quiet_end} 不发布。
      ${sp.publish_block ? `当前暂不能发布：<b style="display:inline">${esc(sp.publish_block)}</b>。` : "当前可以发布。"}
      闲鱼账号需要先在 App 里设置过发货地址。建议先用一个商品试一次。</span></div></div>
    <div class="card">
      <div class="card-head"><h3>上架队列</h3><span class="desc">控制台要保持运行，排队的商品才会被发布</span>
        <div class="actions"><button class="btn primary" id="newListing">＋ 新建商品</button></div></div>
      ${d.listings.length ? `<div class="table-wrap"><table><thead><tr><th></th><th>商品</th><th>价格</th><th>状态</th><th>计划时间</th><th></th></tr></thead>
        <tbody>${d.listings.map((l) => `<tr>
          <td style="width:64px">${l.images[0] ? `<img src="/uploads/${esc(l.images[0])}" alt="" style="width:48px;height:48px;border-radius:8px;object-fit:cover">` : ""}</td>
          <td><b>${esc(l.title)}</b><div class="muted clip">${esc(l.error || l.description)}</div></td>
          <td class="nowrap">¥ ${esc(l.price)}</td>
          <td><span class="badge ${LISTING_BADGE[l.status]}">${esc(l.status_label)}</span>${l.item_id ? `<div class="mono muted" style="font-size:12px">${esc(l.item_id)}</div>` : ""}</td>
          <td class="nowrap">${l.status === "published" ? fmtTs(l.published_at) : l.scheduled_at ? fmtTs(l.scheduled_at) : "-"}</td>
          <td class="actions">
            ${l.status === "published" && l.item_id ? `<a class="btn sm" href="https://www.goofish.com/item?id=${encodeURIComponent(l.item_id)}" target="_blank" rel="noopener">查看</a>` : ""}
            ${["draft", "failed"].includes(l.status) ? `<button class="btn sm teal" data-queue="${l.id}">加入队列</button>` : ""}
            ${!["publishing", "published"].includes(l.status) ? `<button class="btn sm" data-edit="${l.id}">编辑</button>` : ""}
            <button class="btn sm" data-copy="${l.id}">复制</button>
            ${l.status !== "publishing" ? `<button class="btn sm danger" data-del="${l.id}">删除</button>` : ""}</td></tr>`).join("")}
        </tbody></table></div>` : emptyHtml("还没有商品。点「新建商品」，可以让 AI 根据一句话帮你写标题和描述。")}
    </div>`;
  $("#newListing").onclick = () => editListing({}, d.delivery_choices);
  const find = (id) => d.listings.find((l) => l.id == id);
  $$("[data-edit]", el).forEach((b) => (b.onclick = () => editListing(find(b.dataset.edit), d.delivery_choices)));
  $$("[data-copy]", el).forEach((b) => (b.onclick = () => { const l = { ...find(b.dataset.copy) }; delete l.id; editListing(l, d.delivery_choices); }));
  $$("[data-queue]", el).forEach((b) => (b.onclick = async () => {
    const l = find(b.dataset.queue);
    const r = await run(() => api("/api/listings/save", { ...l, action: "publish" }));
    toast(r.block ? `已加入队列，${r.block}，稍后自动发布` : "已加入队列，稍后自动发布");
    navigate("listings", true);
  }));
  $$("[data-del]", el).forEach((b) => (b.onclick = () => confirmBox("删除这个商品草稿？（已上架的商品不会从闲鱼删除）", async () => {
    await api("/api/listings/delete", { id: b.dataset.del }); toast("已删除"); navigate("listings", true);
  }, "删除")));
  if (d.task.running) pollTask();
};

function localInput(ts) {
  const d = ts ? new Date(ts * 1000) : new Date(Date.now() + 3600e3);
  const pad = (n) => String(n).padStart(2, "0");
  return `${d.getFullYear()}-${pad(d.getMonth() + 1)}-${pad(d.getDate())}T${pad(d.getHours())}:${pad(d.getMinutes())}`;
}

function editListing(l, choices) {
  let images = [...(l.images || [])];
  const m = modal({
    title: l.id ? "编辑商品" : "新建商品", wide: true, submitText: "确定",
    body: `
      <div class="banner info" style="margin-bottom:16px;display:block">
        <b>✨ AI 帮写</b>
        <div style="display:flex;gap:8px;margin-top:8px"><input type="text" id="aiBrief" placeholder="一句话描述，例如：九成新 iPad Air 5 64G 蓝色，带原装充电器，买了半年" style="flex:1">
          <button type="button" class="btn primary" id="aiWrite">生成</button></div>
        <div class="help">会用你在「AI 模型」里配置的模型写标题、描述和类目，生成后可以再改。</div>
      </div>
      <div class="section-title" style="margin-top:0">商品图片（最多 9 张，第一张是封面）</div>
      <div id="imgList" style="display:flex;gap:10px;flex-wrap:wrap;margin-bottom:16px"></div>
      <label class="field"><span>标题</span><input type="text" name="title" maxlength="60" value="${esc(l.title)}" required></label>
      <label class="field"><span>描述</span><textarea name="description" rows="6" required>${esc(l.description)}</textarea></label>
      <div class="grid-3">
        <label class="field"><span>售价（元）</span><input type="number" step="0.01" min="0" name="price" value="${esc(l.price ?? "")}" required></label>
        <label class="field"><span>原价（可选）</span><input type="number" step="0.01" min="0" name="orig_price" value="${esc(l.orig_price ?? "")}"></label>
        <label class="field"><span>类目提示（可选）</span><input type="text" name="category_hint" value="${esc(l.category_hint)}" placeholder="如：平板电脑"></label>
      </div>
      <div class="grid-3">
        <label class="field"><span>运费</span><select name="delivery">${choices.map((c) => `<option ${l.delivery === c ? "selected" : ""}>${c}</option>`).join("")}</select></label>
        <label class="field" data-post><span>邮费（元）</span><input type="number" step="0.01" min="0" name="post_price" value="${esc(l.post_price ?? "")}"></label>
      </div>
      <div class="section-title">保存方式</div>
      <div style="margin-bottom:10px">
        <label class="check"><input type="radio" name="action" value="draft" checked> 存为草稿</label>
        <label class="check"><input type="radio" name="action" value="publish"> 加入上架队列（按防风控节奏尽快发布）</label>
        <label class="check"><input type="radio" name="action" value="schedule"> 定时上架</label>
      </div>
      <label class="field hidden" data-when><span>上架时间</span><input type="datetime-local" name="when" value="${localInput(l.scheduled_at)}"></label>`,
    onSubmit: async (f) => {
      const action = m.form.querySelector("input[name=action]:checked").value;
      const payload = { ...f, id: l.id, images, action };
      delete payload.when;
      if (action === "schedule") payload.scheduled_at = new Date(f.when).getTime() / 1000;
      const r = await api("/api/listings/save", payload);
      toast(action === "draft" ? "草稿已保存" : r.block ? `已加入队列（${r.block}，会自动顺延）` : "已加入上架队列");
      navigate("listings", true);
    },
  });
  const f = m.form;
  const renderImgs = () => {
    $("#imgList", f).innerHTML = images.map((img, i) => `
      <div style="position:relative"><img src="/uploads/${esc(img)}" alt="" style="width:88px;height:88px;border-radius:10px;object-fit:cover;border:1px solid var(--border)">
      ${i === 0 ? `<span class="badge accent" style="position:absolute;left:4px;top:4px">封面</span>` : ""}
      <button type="button" class="btn sm danger" data-rmimg="${i}" style="position:absolute;right:4px;bottom:4px;height:22px;padding:0 6px">删</button></div>`).join("")
      + (images.length < 9 ? `<label class="btn" style="width:88px;height:88px;flex-direction:column"><span style="font-size:22px">＋</span><span style="font-size:12px">上传</span>
        <input type="file" accept="image/*" multiple hidden id="imgInput"></label>` : "");
    $$("[data-rmimg]", f).forEach((b) => (b.onclick = () => { images.splice(Number(b.dataset.rmimg), 1); renderImgs(); }));
    $("#imgInput", f)?.addEventListener("change", async (e) => {
      for (const file of [...e.target.files].slice(0, 9 - images.length)) {
        const data = await new Promise((res) => { const r = new FileReader(); r.onload = () => res(r.result); r.readAsDataURL(file); });
        try { images.push(await api("/api/listings/upload", { name: file.name, data })); renderImgs(); }
        catch (err) { toast(err.message, "error"); }
      }
    });
  };
  renderImgs();
  const syncForm = () => {
    $("[data-post]", f).classList.toggle("hidden", f.elements.delivery.value !== "一口价");
    $("[data-when]", f).classList.toggle("hidden", f.querySelector("input[name=action]:checked").value !== "schedule");
  };
  f.elements.delivery.onchange = syncForm;
  $$("input[name=action]", f).forEach((r) => (r.onchange = syncForm));
  syncForm();
  $("#aiWrite", f).onclick = async (e) => {
    const btn = e.target;
    btn.disabled = true; btn.textContent = "生成中…";
    try {
      const r = await api("/api/listings/ai_write", { brief: $("#aiBrief", f).value });
      f.elements.title.value = r.title;
      f.elements.description.value = r.description;
      if (r.category_hint) f.elements.category_hint.value = r.category_hint;
      toast("已生成，可以再修改");
    } catch (err) { toast(err.message, "error"); }
    btn.disabled = false; btn.textContent = "生成";
  };
}

/* ---------------- 对话记录 ---------------- */

PAGES.chats = async (el) => {
  const chats = await api("/api/chats");
  el.innerHTML = `
    <div class="chat-layout">
      <div class="chat-list"><div class="search"><input type="text" id="chatSearch" placeholder="搜索商品、消息内容"></div><div id="chatRows"></div></div>
      <div class="chat-view" id="chatView">${emptyHtml(chats.length ? "选择左侧的对话查看" : "还没有对话记录")}</div>
    </div>`;
  const renderRows = (kw = "") => {
    const list = chats.filter((c) => !kw || (c.item_title + c.last_message + c.item_id).includes(kw));
    $("#chatRows").innerHTML = list.map((c) => `
      <div class="chat-row ${c.chat_id === state.chatId ? "active" : ""}" data-id="${esc(c.chat_id)}">
        <b>${esc(c.item_title || "商品 " + c.item_id)}</b>
        <div class="meta"><span>${fmtDb(c.last_time)}</span><span>${c.total} 条</span>${c.bargain_count ? `<span class="badge warn">议价 ${c.bargain_count} 次</span>` : ""}</div>
        <span class="last">${esc(c.last_message)}</span></div>`).join("") || emptyHtml("没有匹配的对话");
    $$(".chat-row", el).forEach((r) => (r.onclick = () => { state.chatId = r.dataset.id; renderRows($("#chatSearch").value); openChat(); }));
  };
  const openChat = async () => {
    const c = chats.find((x) => x.chat_id === state.chatId);
    if (!c) return;
    const msgs = await api("/api/messages?chat_id=" + encodeURIComponent(c.chat_id));
    $("#chatView").innerHTML = `
      <div class="head"><div style="min-width:0"><b>${esc(c.item_title || "商品 " + c.item_id)}</b><div class="muted mono" style="font-size:12px">买家 ${esc(c.buyer_id || "-")} · 会话 ${esc(c.chat_id)}</div></div>
        <span style="flex:1"></span>${c.buyer_id ? `<button class="btn sm danger" id="blockBuyer">拉黑买家</button>` : ""}</div>
      <div class="msgs">${msgs.map((m) => `<div class="msg ${m.role}">${esc(m.content)}<time>${m.role === "assistant" ? "我方" : "买家"} · ${fmtDb(m.timestamp)}</time></div>`).join("")}</div>`;
    const view = $("#chatView");
    view.scrollTop = view.scrollHeight;
    $("#blockBuyer")?.addEventListener("click", () => confirmBox(`拉黑买家 ${c.buyer_id}？之后 TA 的消息不会自动回复。`, async () => {
      await api("/api/blacklist/add", { user_id: c.buyer_id, note: `来自对话：${c.item_title || c.item_id}` }); toast("已拉黑");
    }, "拉黑"));
  };
  $("#chatSearch").oninput = (e) => renderRows(e.target.value.trim());
  renderRows();
  if (state.chatId) openChat();
};

/* ---------------- 运行日志 ---------------- */

const CHAT_RE = /用户:|机器人回复|卖家人工回复|已接管会话|已恢复会话|无需回复|命中关键词|自动发货|离线提示|\[控制台\]/;
function logClass(line) {
  if (/\| ERROR|❌|🔴|Traceback|Error:/.test(line)) return "e";
  if (/\| WARNING/.test(line)) return "w";
  if (/机器人回复|命中关键词|自动发货/.test(line)) return "b";
  if (/用户:|卖家人工回复/.test(line)) return "u";
  if (/\| SUCCESS|连接注册完成/.test(line)) return "s";
  if (/^\[控制台\]/.test(line)) return "c";
  return "";
}
function appendLogLines(lines) {
  const box = $("#logBox");
  if (!box) return;
  const onlyChat = $("#onlyChat").checked;
  const frag = document.createDocumentFragment();
  for (const [, ts, line] of lines) {
    const div = document.createElement("div");
    div.className = logClass(line);
    div.innerHTML = `<span class="t">${new Date(ts * 1000).toLocaleTimeString("zh-CN", { hour12: false })}</span>${esc(line)}`;
    if (!CHAT_RE.test(line)) div.dataset.noise = "1";
    if (onlyChat && div.dataset.noise) div.classList.add("hidden");
    frag.appendChild(div);
  }
  box.appendChild(frag);
  while (box.childElementCount > 5000) box.firstChild.remove();
  if ($("#autoScroll").checked) box.scrollTop = box.scrollHeight;
}

PAGES.logs = async (el) => {
  el.innerHTML = `
    <div class="card" style="margin-bottom:0">
      <div class="card-head">
        <label class="check"><input type="checkbox" id="onlyChat"> 只看对话</label>
        <label class="check"><input type="checkbox" id="autoScroll" checked> 自动滚动</label>
        <div class="actions"><button class="btn sm" id="clearLog">清屏</button></div>
      </div>
      <div class="log" id="logBox"></div>
    </div>`;
  appendLogLines(state.logs);
  $("#onlyChat").onchange = () => {
    const only = $("#onlyChat").checked;
    $$("#logBox > div").forEach((d) => d.classList.toggle("hidden", only && !!d.dataset.noise));
  };
  $("#clearLog").onclick = () => { state.logs = []; $("#logBox").innerHTML = ""; };
};

/* ---------------- AI 模型 ---------------- */

PAGES.models = async (el) => {
  const { providers, categories } = await api("/api/providers");
  let filter = state.modelFilter || "all";
  const order = providers.filter((p) => p.enabled && p.key_count);
  const render = () => {
    const list = providers.filter((p) => filter === "all" || (filter === "enabled" ? p.enabled : p.category === filter));
    el.innerHTML = `
      <div class="banner ${order.length ? "info" : "warn"}"><div class="grow">
        ${order.length ? `<b>当前调用顺序（失败自动切换到下一个）</b><span>${order.map((p, i) => `${i + 1}. ${esc(p.name)}${p.key_count > 1 ? `（${p.key_count} 个 Key 轮换）` : ""}`).join(" → ")}</span>`
          : `<b>还没有可用的 AI 模型</b><span class="muted">选一个平台点「配置」，填入 API Key 并启用。推荐国内用户用通义千问或 DeepSeek。</span>`}
      </div></div>
      <div class="card-head" style="margin-bottom:12px">
        <div class="chips" style="margin:0">${[["all", "全部"], ["enabled", "已启用"], ...Object.entries(categories)].map(([k, v]) =>
          `<button class="chip ${filter === k ? "active" : ""}" data-filter="${k}">${v}</button>`).join("")}</div>
        <div class="actions"><button class="btn" id="addCustom">＋ 自定义接口</button></div>
      </div>
      <div class="providers">${list.map(providerCard).join("") || emptyHtml("这个分类下没有平台")}</div>`;
    $$("[data-filter]", el).forEach((b) => (b.onclick = () => { filter = state.modelFilter = b.dataset.filter; render(); }));
    $("#addCustom").onclick = () => configProvider(null);
    $$("[data-config]", el).forEach((b) => (b.onclick = () => configProvider(providers.find((p) => p.id === b.dataset.config))));
    $$("[data-toggle]", el).forEach((b) => (b.onclick = async () => {
      const p = providers.find((x) => x.id === b.dataset.toggle);
      if (!p.enabled && !p.key_count) return configProvider(p);
      await run(() => api("/api/providers/save", { id: p.id, enabled: !p.enabled }), p.enabled ? "已停用" : "已启用");
      navigate("models", true);
    }));
    $$("[data-default]", el).forEach((b) => (b.onclick = async () => {
      await run(() => api("/api/providers/default", { id: b.dataset.default }), "已设为默认模型"); navigate("models", true);
    }));
    $$("[data-test]", el).forEach((b) => (b.onclick = async () => {
      b.disabled = true; b.textContent = "测试中…";
      try {
        const r = await api("/api/providers/test", { id: b.dataset.test });
        r.ok ? toast(`连接成功（${r.latency} 秒）：${r.reply}`) : toast("连接失败：" + r.error, "error");
      } catch (e) { toast(e.message, "error"); }
      navigate("models", true);
    }));
  };
  render();
};

function providerCard(p) {
  const t = p.last_test;
  const testBadge = t ? (t.ok ? `<span class="badge good">测试通过 ${t.latency}s</span>` : `<span class="badge bad" title="${esc(t.error)}">测试失败</span>`) : "";
  return `
    <div class="provider ${p.enabled ? "on" : "off"}">
      <div class="pic">${esc(p.name.slice(0, 1))}</div>
      <div class="pinfo">
        <div class="title">${esc(p.name)}
          ${p.is_default && p.enabled ? `<span class="badge accent">默认</span>` : ""}
          ${p.enabled ? `<span class="badge good">已启用</span>` : `<span class="badge">未启用</span>`}
          <span class="badge">${esc(p.category_label)}</span>${testBadge}</div>
        <div class="desc">${esc(p.description)}</div>
        <div class="meta"><span>模型：<span class="mono">${esc(p.model)}</span></span>
          <span>API Key：${p.key_count ? `<span class="ok">✓ 已配置 ${p.key_count} 个</span>` : `<span class="no">✗ 未配置</span>`}</span>
          ${p.supports_search ? `<span class="badge info">联网搜索</span>` : ""}<span class="muted">优先级 ${p.priority}</span></div>
      </div>
      <div class="ops">
        <button class="btn sm" data-test="${p.id}" ${p.key_count ? "" : "disabled"}>测试</button>
        <button class="btn sm" data-config="${p.id}">配置</button>
        ${p.enabled && !p.is_default ? `<button class="btn sm" data-default="${p.id}">设为默认</button>` : ""}
        <button class="btn sm ${p.enabled ? "danger" : "teal"}" data-toggle="${p.id}">${p.enabled ? "停用" : "启用"}</button>
      </div>
    </div>`;
}

function configProvider(p) {
  const isNew = !p;
  p = p || { name: "", base_url: "", model: "", priority: 500, masked_keys: [], custom: 1, enabled: 0, supports_search: 0 };
  const removed = new Set();
  const m = modal({
    title: isNew ? "添加自定义接口" : `配置 ${p.name}`, wide: true,
    body: `
      ${!isNew && p.key_url ? `<div class="banner info" style="margin-bottom:16px"><div class="grow">还没有 Key？<a href="${esc(p.key_url)}" target="_blank" rel="noopener">去 ${esc(p.name)} 官网申请 →</a></div></div>` : ""}
      <div class="grid-2">
        <label class="field"><span>显示名称</span><input type="text" name="name" value="${esc(p.name)}" required></label>
        <label class="field"><span>模型名称</span><input type="text" name="model" value="${esc(p.model)}" required><div class="help">可换成该平台的其他模型</div></label>
      </div>
      <label class="field"><span>接口地址（OpenAI 兼容）</span><input type="text" name="base_url" value="${esc(p.base_url)}" required class="mono"></label>
      <div class="grid-2">
        <label class="field"><span>优先级（数字越小越先调用）</span><input type="number" name="priority" value="${esc(p.priority)}"></label>
        <label class="field"><span>&nbsp;</span>${switchHtml("supports_search", !!p.supports_search, "支持百炼联网搜索参数")}</label>
      </div>
      <div class="section-title" style="margin-top:4px">API Key</div>
      <div class="key-list">${p.masked_keys.map((k, i) => `<div class="key-row" data-i="${i}"><span class="badge">#${i + 1}</span><span class="mono">${esc(k)}</span>
        <button type="button" class="btn sm danger" data-rm="${i}">删除</button></div>`).join("") || `<div class="help">还没有 Key</div>`}</div>
      <label class="field"><span>添加 Key（可一次粘贴多个，一行一个）</span><textarea name="new_keys" rows="3" class="mono" placeholder="sk-..."></textarea>
        <div class="help">多个 Key 会轮流使用，某个 Key 额度用完或失效时自动换下一个。Key 只保存在你电脑的 data/console.db 里。</div></label>
      ${switchHtml("enabled", isNew ? true : !!p.enabled, "启用这个平台")}`,
    extraFoot: !isNew && p.custom ? `<button type="button" class="btn danger" id="delProvider">删除接口</button>` : "",
    onSubmit: async (d) => {
      const payload = { ...d, id: p.id, priority: Number(d.priority), remove_key_indexes: [...removed] };
      await api(isNew ? "/api/providers/create" : "/api/providers/save", payload);
      toast("已保存");
      navigate("models", true);
    },
  });
  $$("[data-rm]", m.form).forEach((b) => (b.onclick = () => {
    const i = Number(b.dataset.rm);
    const row = b.closest(".key-row");
    removed.has(i) ? removed.delete(i) : removed.add(i);
    row.classList.toggle("removed", removed.has(i));
    b.textContent = removed.has(i) ? "撤销" : "删除";
  }));
  $("#delProvider")?.addEventListener("click", () => confirmBox(`删除「${p.name}」？`, async () => {
    await api("/api/providers/delete", { id: p.id }); m.close(); toast("已删除"); navigate("models", true);
  }, "删除"));
}

/* ---------------- 闲鱼账号 ---------------- */

PAGES.account = async (el) => {
  const a = await api("/api/account");
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h3>账号状态</h3></div>
      <div class="grid-3">
        <div><div class="muted">昵称</div><b>${esc(a.nick || "-")}</b></div>
        <div><div class="muted">用户 ID</div><b class="mono">${esc(a.user_id || "-")}</b></div>
        <div><div class="muted">Cookie</div>${a.has_cookie ? (a.valid_format ? `<span class="badge good">已填写 · ${a.cookie_length} 字符</span>` : `<span class="badge bad">格式不完整</span>`) : `<span class="badge warn">未填写</span>`}</div>
      </div>
      <div class="help" style="margin-top:12px">Cookie 会过期；机器人运行时会自动续期，失效或触发风控时会在顶部提醒你，并推送到已配置的通知渠道。</div>
    </div>
    <div class="card">
      <div class="card-head"><h3>更新 Cookie</h3></div>
      <form id="cookieForm">
        <label class="field"><span>粘贴完整 Cookie</span><textarea name="cookie" rows="5" class="mono" placeholder="cookie2=...; unb=...; ..." required></textarea></label>
        <button class="btn primary" type="submit">保存 Cookie</button>
        <span class="help">保存后点右上角「重启」生效；如果机器人正在等待风控验证，会直接提交给它。</span>
      </form>
    </div>
    <div class="card">
      <div class="card-head"><h3>如何获取 Cookie</h3></div>
      <ol style="margin:0;padding-left:20px;color:var(--text-2);line-height:2">
        <li>用电脑浏览器打开 <a href="https://www.goofish.com" target="_blank" rel="noopener">goofish.com</a> 并登录闲鱼账号</li>
        <li>按 <b>F12</b> 打开开发者工具，切到 <b>Network（网络）</b>，筛选 <b>Fetch/XHR</b></li>
        <li>刷新页面，随便点一个请求，在「请求标头」里找到 <b>cookie</b>，复制它后面的整段内容</li>
        <li>粘贴到上面保存。之后直接关掉网页即可，<b>不要点退出登录</b>，否则 Cookie 会立即失效</li>
      </ol>
    </div>`;
  $("#cookieForm").onsubmit = async (e) => {
    e.preventDefault();
    await run(() => api("/api/account/cookie", formData(e.target)), "Cookie 已保存");
    navigate("account", true);
  };
};

/* ---------------- 消息通知 ---------------- */

PAGES.notify = async (el) => {
  const { channels, types, events } = await api("/api/notify");
  el.innerHTML = `
    <div class="card">
      <div class="card-head"><h3>通知渠道</h3><span class="desc">买家消息、付款、发货、风控时推送到手机</span>
        <div class="actions"><button class="btn primary" id="addCh">＋ 添加渠道</button></div></div>
      ${channels.length ? `<div class="table-wrap"><table><thead><tr><th>名称</th><th>类型</th><th>推送事件</th><th>状态</th><th></th></tr></thead>
        <tbody>${channels.map((c) => `<tr><td><b>${esc(c.name)}</b></td><td>${esc(c.type_label)}</td>
          <td>${c.events.map((e) => `<span class="badge" style="margin:0 4px 4px 0">${esc(events[e])}</span>`).join("") || "-"}</td>
          <td>${c.enabled ? `<span class="badge good">启用</span>` : `<span class="badge">停用</span>`}</td>
          <td class="actions"><button class="btn sm" data-edit="${c.id}">编辑</button><button class="btn sm danger" data-del="${c.id}">删除</button></td></tr>`).join("")}
        </tbody></table></div>` : emptyHtml("还没有通知渠道。推荐：钉钉/飞书/企业微信群机器人，或 iPhone 用 Bark、微信用 Server 酱。")}
    </div>`;
  const edit = (c = {}) => {
    const m = modal({
      title: c.id ? "编辑通知渠道" : "添加通知渠道",
      body: `
        <div class="grid-2">
          <label class="field"><span>类型</span><select name="type">${Object.entries(types).map(([k, v]) => `<option value="${k}" ${c.type === k ? "selected" : ""}>${v}</option>`).join("")}</select></label>
          <label class="field"><span>名称</span><input type="text" name="name" value="${esc(c.name)}" placeholder="例如：我的钉钉群"></label>
        </div>
        <label class="field"><span>Webhook 地址</span><input type="text" name="url" value="${esc(c.url)}" class="mono" required>
          <div class="help" id="urlHelp"></div></label>
        <div class="section-title" style="margin-top:0">推送哪些事件</div>
        <div style="margin-bottom:14px">${Object.entries(events).map(([k, v]) => `<label class="check"><input type="checkbox" name="events" data-multi="1" value="${k}"
          ${(c.events || ["order", "delivery", "stock", "risk"]).includes(k) ? "checked" : ""}>${v}</label>`).join("")}</div>
        ${switchHtml("enabled", c.id ? !!c.enabled : true, "启用")}`,
      extraFoot: `<button type="button" class="btn" id="testCh">发送测试</button>`,
      onSubmit: async (d) => { await api("/api/notify/save", { ...d, id: c.id }); toast("已保存"); navigate("notify", true); },
    });
    const HELP = {
      dingtalk: "钉钉群 → 设置 → 机器人 → 添加自定义机器人，安全设置选「自定义关键词」填：闲鱼助手",
      feishu: "飞书群 → 设置 → 群机器人 → 添加自定义机器人，复制 Webhook 地址",
      wecom: "企业微信群 → 添加群机器人，复制 Webhook 地址",
      bark: "iPhone 安装 Bark App，复制形如 https://api.day.app/你的Key 的地址",
      serverchan: "sct.ftqq.com 登录获取 SendKey，地址填 https://sctapi.ftqq.com/你的SendKey.send",
      webhook: "会以 JSON（title、text、time）POST 到这个地址",
    };
    const sync = () => ($("#urlHelp").textContent = HELP[m.form.elements.type.value]);
    m.form.elements.type.onchange = sync;
    sync();
    $("#testCh").onclick = () => run(() => api("/api/notify/test", { type: m.form.elements.type.value, url: m.form.elements.url.value }), "测试消息已发送，请查看手机");
  };
  $("#addCh").onclick = () => edit();
  $$("[data-edit]", el).forEach((b) => (b.onclick = () => edit(channels.find((c) => c.id == b.dataset.edit))));
  $$("[data-del]", el).forEach((b) => (b.onclick = () => confirmBox("确定删除这个通知渠道吗？", async () => {
    await api("/api/notify/delete", { id: b.dataset.del }); toast("已删除"); navigate("notify", true);
  }, "删除")));
};

/* ---------------- 系统设置 ---------------- */

const ROADMAP = [
  ["done", "AI 智能回复", "意图识别 + 议价/技术/客服多专家"],
  ["done", "多模型接入", "16 个平台，Key 轮换与故障切换"],
  ["done", "关键词回复", "包含 / 完全一致 / 正则，可限定商品"],
  ["done", "自动发货", "固定内容与卡密库存（Beta）"],
  ["done", "营业时间与离线提示", ""],
  ["done", "消息通知", "钉钉 / 飞书 / 企业微信 / Bark / Server 酱"],
  ["done", "账号登录", "本地注册登录，多成员"],
  ["done", "防风控模式", "三档限速、夜间静默、风控自动熔断"],
  ["done", "自动擦亮", "每天随机时间、随机顺序擦亮在售商品"],
  ["done", "自动上架", "草稿、定时、队列发布，AI 帮写文案（Beta）"],
  ["done", "并行接待", "多买家互不排队，同一买家按顺序回复"],
  ["plan", "自动确认发货 / 自动评价", "规划中"],
  ["plan", "多闲鱼账号", "同时托管多个店铺，规划中"],
];

PAGES.system = async (el) => {
  const s = await api("/api/settings");
  const users = state.user.is_admin ? await api("/api/users") : [];
  el.innerHTML = `
    <form id="sysForm" class="card">
      <div class="card-head"><h3>运行方式</h3></div>
      <div style="display:flex;flex-direction:column;gap:14px;margin-bottom:16px">
        ${switchHtml("auto_start_bot", s.auto_start_bot, "打开控制台时自动启动机器人")}
        ${switchHtml("auto_restart", s.auto_restart, "机器人意外退出时自动重启（1 小时内最多 5 次，Cookie 失效时不重启）")}
      </div>
      <button class="btn primary" type="submit">保存</button>
    </form>
    <div class="card">
      <div class="card-head"><h3>修改密码</h3></div>
      <form id="pwdForm" class="grid-3">
        <label class="field"><span>原密码</span><input type="password" name="old_password" autocomplete="current-password" required></label>
        <label class="field"><span>新密码</span><input type="password" name="new_password" autocomplete="new-password" required></label>
        <label class="field"><span>&nbsp;</span><button class="btn primary" type="submit">修改密码</button></label>
      </form>
    </div>
    ${state.user.is_admin ? `<div class="card">
      <div class="card-head"><h3>成员账号</h3><span class="desc">可以给助手、合伙人开账号</span>
        <div class="actions"><button class="btn" id="addUser">＋ 添加成员</button></div></div>
      <div class="table-wrap"><table><thead><tr><th>用户名</th><th>角色</th><th>创建时间</th><th></th></tr></thead>
        <tbody>${users.map((u) => `<tr><td><b>${esc(u.username)}</b></td><td>${u.is_admin ? `<span class="badge accent">管理员</span>` : "成员"}</td>
          <td>${fmtTs(u.created_at)}</td><td class="actions">${u.id !== state.user.id ? `<button class="btn sm danger" data-del="${u.id}">删除</button>` : `<span class="muted">当前账号</span>`}</td></tr>`).join("")}</tbody></table></div>
    </div>` : ""}
    <div class="card">
      <div class="card-head"><h3>功能规划</h3><span class="desc">参考市面上主流闲鱼管理工具整理</span></div>
      <div class="roadmap">${ROADMAP.map(([st, name, desc]) => `<div class="road"><b>${st === "done" ? `<span class="badge good">已上线</span>` : `<span class="badge">规划中</span>`}${esc(name)}</b><p>${esc(desc)}</p></div>`).join("")}</div>
    </div>`;
  trackDirty($("#sysForm"), (d) => api("/api/settings", { auto_start_bot: d.auto_start_bot, auto_restart: d.auto_restart }));
  $("#pwdForm").onsubmit = async (e) => {
    e.preventDefault();
    await run(() => api("/api/users/password", formData(e.target)), "密码已修改");
    e.target.reset();
  };
  $("#addUser")?.addEventListener("click", () => modal({
    title: "添加成员",
    body: `<label class="field"><span>用户名</span><input type="text" name="username" required></label>
      <label class="field"><span>初始密码</span><input type="password" name="password" autocomplete="new-password" required></label>
      ${switchHtml("is_admin", false, "设为管理员")}`,
    onSubmit: async (d) => { await api("/api/users/create", d); toast("已添加"); navigate("system", true); },
  }));
  $$("[data-del]", el).forEach((b) => (b.onclick = () => confirmBox("确定删除这个成员账号吗？", async () => {
    await api("/api/users/delete", { id: Number(b.dataset.del) }); toast("已删除"); navigate("system", true);
  }, "删除")));
};

boot();
