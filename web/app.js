"use strict";

const $ = (id) => document.getElementById(id);
const SELECTION_KEY = "cf-selected-activities";
const LABEL_CLASS = {
  "成功": "ok",
  "已领过": "done",
  "未开放或不满足条件": "closed",
  "需手动处理": "bad",
};
const LABEL_TEXT = { "未开放或不满足条件": "未开放" };

const view = { busy: null, login: false, loaded: false, qrLogin: false };
let run = freshRun();

function freshRun() {
  return { index: 0, total: 0, dryRun: false, ok: 0, done: 0, closed: 0, bad: 0, items: 0, finished: null };
}

function el(tag, className, text) {
  const node = document.createElement(tag);
  if (className) node.className = className;
  if (text !== undefined) node.textContent = text;
  return node;
}

function readSelection() {
  try {
    return JSON.parse(localStorage.getItem(SELECTION_KEY)) || [];
  } catch {
    return [];
  }
}

function saveSelection() {
  try {
    localStorage.setItem(SELECTION_KEY, JSON.stringify(selectedUrls()));
  } catch {
    // 无痕模式下可能不能保存，下次重新勾选即可
  }
}

function selectedUrls() {
  return [...document.querySelectorAll("#activities input:checked")].map((box) => box.value);
}

function addFeed(tagText, tagClass, text, meta) {
  const row = el("li");
  row.append(el("span", `tag ${tagClass || ""}`, tagText), el("span", "text", text));
  if (meta) row.append(el("span", "meta", meta));
  const feed = $("feed");
  const nearTop = feed.scrollTop < 40;
  feed.prepend(row);
  if (nearTop) feed.scrollTop = 0;
  $("feed-empty").hidden = true;
}

function renderCounts() {
  $("n-ok").textContent = run.ok;
  $("n-done").textContent = run.done;
  $("n-closed").textContent = run.closed;
  $("n-bad").textContent = run.bad;
  $("item-count").textContent = run.items;
  $("items-empty").hidden = run.items > 0;
}

function renderStatus() {
  const running = view.busy === "run";
  $("bar").hidden = !(running || run.finished);
  $("bar-fill").style.width = run.total ? `${(run.index / run.total) * 100}%` : "0";
  $("progress-text").textContent = run.total ? `活动 ${run.index}/${run.total}` : "";
  if (view.busy === "login") $("headline").textContent = "等待登录";
  else if (view.busy === "refresh") $("headline").textContent = "正在刷新活动列表";
  else if (running) $("headline").textContent = run.dryRun ? "正在预览" : "正在领取";
  else if (run.finished) $("headline").textContent = run.finished;
  else $("headline").textContent = "空闲";
  if (!running) $("current").textContent = "";
  renderControls();
}

function renderControls() {
  const busy = Boolean(view.busy);
  const none = selectedUrls().length === 0;
  $("btn-run").disabled = busy || none;
  $("btn-preview").disabled = busy || none;
  $("btn-login").disabled = busy;
  $("btn-refresh").disabled = busy;
  $("btn-login-window").disabled = busy;
  $("btn-stop").hidden = !(view.busy === "run" || view.busy === "login");
  $("btn-stop").textContent = view.busy === "login" ? "取消登录" : "停止";
  $("login-panel").hidden = view.busy !== "login" || !view.qrLogin;
  if (view.busy !== "login") {
    view.qrLogin = false;
    $("login-qr").hidden = true;
    $("login-qr").removeAttribute("src");
    $("login-status").textContent = "正在获取二维码…";
    clearLoginLinks();
  }
  const login = $("login-state");
  login.textContent = view.login ? "已保存登录" : "未登录";
  login.className = `pill ${view.login ? "ok" : "bad"}`;
}

function clearLoginLinks() {
  $("login-links").hidden = true;
  $("link-qq").removeAttribute("href");
  $("link-direct").removeAttribute("href");
}

async function loadLoginLinks() {
  try {
    const { qq, direct } = await api("/api/login/link");
    // 请求期间登录可能已经结束，这时不能再把链接放回页面
    if (view.busy !== "login") return;
    $("link-qq").href = qq;
    $("link-direct").href = direct;
    $("login-links").hidden = false;
  } catch {
    clearLoginLinks();
  }
}

function resetRun(total, dryRun) {
  run = freshRun();
  run.total = total;
  run.dryRun = dryRun;
  $("items").replaceChildren();
  $("feed").replaceChildren();
  $("feed-empty").hidden = false;
  renderCounts();
}

const STATUS_TEXT = {
  needs_manual: "遇到需要手动处理的弹窗，已停止本活动",
  failed: "未能处理完成",
  need_login: "需要重新登录",
  stopped: "已停止",
};

function summarizeButtons(buttons) {
  const counts = new Map();
  for (const text of buttons) counts.set(text, (counts.get(text) || 0) + 1);
  const parts = [...counts].map(([text, count]) => (count > 1 ? `${text} ×${count}` : text));
  return `${buttons.length} 个按钮：${parts.join("、")}`;
}

function handleEvent(event) {
  switch (event.type) {
    case "state":
      view.busy = event.busy;
      view.login = event.login;
      renderStatus();
      if (!event.busy && $("history-box").open) loadHistory();
      break;
    case "run_start":
      resetRun(event.total, event.dry_run);
      addFeed("开始", "", event.dry_run ? `预览 ${event.total} 个活动，不会点击` : `处理 ${event.total} 个活动`, event.ts);
      break;
    case "activity_start":
      run.index = event.index;
      run.total = event.total;
      $("current").textContent = event.activity;
      break;
    case "click":
      if (!event.clicked) addFeed("未点中", "bad", event.text, `${event.activity} · ${event.ts}`);
      break;
    case "message": {
      const cls = LABEL_CLASS[event.label] || "";
      if (cls === "ok") run.ok += 1;
      else if (cls === "done") run.done += 1;
      else if (cls === "closed") run.closed += 1;
      addFeed(LABEL_TEXT[event.label] || event.label, cls, event.text, `${event.activity} · ${event.ts}`);
      break;
    }
    case "item": {
      run.items += 1;
      const row = el("li");
      row.append(el("span", "name", event.item), el("span", "meta", `${event.activity} · ${event.ts}`));
      $("items").prepend(row);
      break;
    }
    case "preview":
      addFeed("预览", "", event.buttons.length ? summarizeButtons(event.buttons) : "没有可点的按钮", event.activity);
      break;
    case "activity_done":
      if (STATUS_TEXT[event.status]) {
        if (event.status !== "stopped") run.bad += 1;
        addFeed(event.status === "stopped" ? "已停止" : "未完成", event.status === "stopped" ? "" : "bad", STATUS_TEXT[event.status], event.activity);
      }
      break;
    case "notice":
      addFeed(event.level === "error" ? "错误" : "提示", event.level === "error" ? "bad" : "", event.text, event.ts);
      break;
    case "run_done":
      if (event.stopped) run.finished = "已停止";
      else if (run.dryRun) run.finished = "预览完成";
      else if (event.code === 2) run.finished = "需要重新登录";
      else if (event.claimed > 0) run.finished = `领取完成，领到 ${event.claimed} 项`;
      else run.finished = "结束，本次没有领到道具";
      addFeed("结束", event.claimed > 0 ? "ok" : "", run.finished, event.ts);
      break;
    case "login_qr":
      // 重放历史事件时可能收到旧二维码，只有正在登录才显示
      if (view.busy === "login") {
        view.qrLogin = true;
        $("login-qr").src = `/api/login/qr.png?v=${event.seq}`;
        $("login-qr").hidden = false;
        $("login-status").textContent = "等待扫码";
        loadLoginLinks();
      }
      break;
    case "login_scanned":
      if (view.busy === "login") $("login-status").textContent = "已扫描，请在手机上确认";
      break;
    case "activities_updated":
      loadActivities();
      addFeed("提示", "", `从官网发现 ${event.count} 个活动`, event.ts);
      break;
    default:
      break;
  }
  renderCounts();
  renderStatus();
}

function connect() {
  const source = new EventSource("/api/events");
  source.addEventListener("open", () => {
    $("conn").textContent = "实时连接";
    $("conn").className = "conn on";
  });
  source.addEventListener("error", () => {
    $("conn").textContent = "连接中断，正在重连";
    $("conn").className = "conn off";
  });
  source.addEventListener("state", (message) => handleEvent({ type: "state", ...JSON.parse(message.data) }));
  source.addEventListener("message", (message) => handleEvent(JSON.parse(message.data)));
}

async function api(path, options) {
  const response = await fetch(path, options);
  if (!response.ok) {
    const error = new Error(String(response.status));
    error.status = response.status;
    throw error;
  }
  return response.json();
}

function showError(text) {
  $("action-error").textContent = text;
  $("action-error").hidden = !text;
}

async function post(path, body) {
  showError("");
  try {
    await api(path, { method: "POST", headers: { "Content-Type": "application/json" }, body: JSON.stringify(body || {}) });
    return true;
  } catch (error) {
    if (error.status === 409) showError("已有任务在运行，请等它结束。");
    else if (error.status === 400) showError("活动列表已变化，请刷新页面后重选。");
    else showError("操作失败，请检查和电脑的连接。");
    return false;
  }
}

async function loadActivities() {
  const { activities } = await api("/api/activities");
  const selected = new Set(view.loaded ? selectedUrls() : readSelection());
  view.loaded = true;
  const list = $("activities");
  list.replaceChildren();
  for (const activity of activities) {
    const box = el("input");
    box.type = "checkbox";
    box.value = activity.url;
    box.checked = selected.has(activity.url);
    box.addEventListener("change", () => {
      saveSelection();
      renderControls();
    });
    const label = el("label");
    label.append(box, el("span", "", activity.name || activity.url));
    const row = el("li");
    row.append(label);
    list.append(row);
  }
  $("activities-empty").hidden = activities.length > 1;
  renderControls();
}

async function loadHistory() {
  const { items } = await api("/api/history");
  const list = $("history");
  list.replaceChildren();
  for (const entry of items) {
    const row = el("li");
    row.append(el("span", "name", entry.item), el("span", "meta", `${entry.activity} · ${entry.time}`));
    list.append(row);
  }
  $("history-empty").hidden = items.length > 0;
}

async function loadAccess() {
  const { urls } = await api("/api/access");
  const list = $("phone-list");
  list.replaceChildren();
  urls.forEach((entry, index) => {
    const box = el("div", "access");
    const qr = el("img", "qr");
    qr.src = `/api/qr.svg?i=${index}`;
    qr.alt = `${entry.label}的二维码`;
    box.append(el("h3", "", entry.label), qr, el("p", "url small", entry.url));
    list.append(box);
  });
  $("phone-off").hidden = urls.length > 0;
  $("phone-remote-hint").hidden = urls.length !== 1;
}

function setAll(checked) {
  for (const box of document.querySelectorAll("#activities input")) box.checked = checked;
  saveSelection();
  renderControls();
}

async function startRun(dryRun) {
  const started = await post("/api/run", { urls: selectedUrls(), dry_run: dryRun, show: $("opt-show").checked });
  // 按钮在页面下方，回到顶部才看得到进度和道具
  if (started) window.scrollTo({ top: 0 });
}

$("btn-run").addEventListener("click", () => startRun(false));
$("btn-preview").addEventListener("click", () => startRun(true));
$("btn-stop").addEventListener("click", () => post("/api/stop"));
$("btn-login").addEventListener("click", async () => {
  view.qrLogin = true;
  if (!(await post("/api/login"))) view.qrLogin = false;
  renderControls();
});
$("btn-login-window").addEventListener("click", () => post("/api/login", { window: true }));
$("btn-refresh").addEventListener("click", () => post("/api/refresh"));
$("btn-all").addEventListener("click", () => setAll(true));
$("btn-none").addEventListener("click", () => setAll(false));
$("history-box").addEventListener("toggle", () => {
  if ($("history-box").open) loadHistory();
});
$("phone-box").addEventListener("toggle", () => {
  if ($("phone-box").open) loadAccess();
});

renderCounts();
renderStatus();
loadActivities().finally(connect);
