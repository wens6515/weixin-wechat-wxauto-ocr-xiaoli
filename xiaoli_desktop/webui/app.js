/* ============================================================
   小漓 · Web 前端主逻辑
   数据全部经 api.js（真环境 pywebview js_api / 浏览器 mock）。
   主题与装饰层沿用已获认可的概念稿设计语言。
   ============================================================ */
"use strict";

/* 前端 JS 错误只进控制台（--debug 开 devtools 可查；不再落盘排障日志） */
window.addEventListener("error", (e) =>
  console.error(`[前端错误] ${e.message} @ ${e.filename}:${e.lineno}`, e.error));
window.addEventListener("unhandledrejection", (e) =>
  console.error("[未处理的 Promise 拒绝]", e.reason));

const $ = (s, r) => (r || document).querySelector(s);
const $$ = (s, r) => [...(r || document).querySelectorAll(s)];
const esc = (s) => String(s ?? "").replace(/[&<>"']/g,
  (c) => ({ "&": "&amp;", "<": "&lt;", ">": "&gt;", '"': "&quot;", "'": "&#39;" }[c]));

/* 全局状态缓存 */
const S = {
  ui: null, misc: null, voice: null, overrides: {},
  providers: [], cards: [], activeCardId: "", editingCardId: "",
  memChat: "", memMode: "overview", deepOffset: 0, deepQuery: "", memRows: [],
  trigRows: [], engineState: "loading", lastTrend: null,
};

/* ============================================================
   toast / 模态
   ============================================================ */
function toast(msg, type = "") {
  const box = $("#toastBox");
  const t = document.createElement("div");
  t.className = "toast " + type;
  t.textContent = msg;
  box.appendChild(t);
  setTimeout(() => { t.classList.add("out"); setTimeout(() => t.remove(), 320); }, 3600);
}

function openModal(id) {
  $("#modalMask").hidden = false;
  $$(".modal").forEach((m) => (m.hidden = m.id !== id));
}
function closeModal() {
  $("#modalMask").hidden = true;
  $$(".modal").forEach((m) => (m.hidden = true));
  if (confirmResolve) { confirmResolve(false); confirmResolve = null; }
}

let confirmResolve = null;
function confirmModal(title, text) {
  $("#confirmTitle").textContent = title;
  const ct = $("#confirmText");
  ct.textContent = text;
  openModal("modalConfirm");
  return new Promise((res) => (confirmResolve = res));
}
/* 带下拉选择的确认（记忆绑卡 / 导出格式选择） */
function selectModal(title, text, options, selected) {
  $("#confirmTitle").textContent = title;
  const ct = $("#confirmText");
  ct.textContent = text + "\n";
  const sel = document.createElement("select");
  sel.id = "confirmSelect";
  sel.style.marginTop = "10px";
  for (const o of options) {
    const op = document.createElement("option");
    op.value = o.value; op.textContent = o.label;
    if (o.value === selected) op.selected = true;
    sel.appendChild(op);
  }
  ct.appendChild(sel);
  openModal("modalConfirm");
  return new Promise((res) => (confirmResolve = res));
}
function confirmSelectValue() {
  const sel = $("#confirmSelect");
  return sel ? sel.value : null;
}
/* 确认弹窗按钮：先取出 resolver 再关窗（closeModal 对未决 confirm
   兜底 resolve(false)，顺序反了会把「确定」吞成「取消」——历史缺陷） */
function settleConfirm(value) {
  const r = confirmResolve;
  confirmResolve = null;
  closeModal();
  r?.(value);
}
$("#confirmYes").addEventListener("click", () => settleConfirm(true));
$("#confirmNo").addEventListener("click", () => settleConfirm(false));

/* ============================================================
   导航（方向感知动画 + 页面惰性加载）
   ============================================================ */
const nav = $("#nav");
const navInd = $("#navInd");
const pages = $$(".page");
const navBtns = $$(".nav-item", nav);
const PAGE_ORDER = navBtns.map((b) => b.dataset.page);
let curKey = "home";
const PAGE_LOADERS = {
  usage: () => loadUsage(), cards: () => loadCards(),
  models: () => loadModels(), memory: () => loadMemory(),
  logs: () => initLogPage(),
};

function indicatorMode() {
  return (getComputedStyle(document.documentElement)
    .getPropertyValue("--nav-ind") || "pill").trim() || "pill";
}
function moveIndicator(el) {
  const mode = indicatorMode();
  if (mode === "underline") {
    navInd.style.top = el.offsetTop + el.offsetHeight - 4 + "px";
    navInd.style.height = "3px";
  } else if (mode === "bar") {
    navInd.style.top = el.offsetTop + 7 + "px";
    navInd.style.height = el.offsetHeight - 14 + "px";
  } else {
    navInd.style.top = el.offsetTop + "px";
    navInd.style.height = el.offsetHeight + "px";
  }
}
function goPage(key) {
  if (key === curKey) { PAGE_LOADERS[key]?.(); return; }
  const from = PAGE_ORDER.indexOf(curKey), to = PAGE_ORDER.indexOf(key);
  const dir = to > from ? "anim-next" : "anim-prev";
  const old = pages.find((p) => p.dataset.page === curKey);
  const target = pages.find((p) => p.dataset.page === key);
  if (!target) return;
  old?.classList.remove("active", "anim-next", "anim-prev");
  target.classList.add("active", dir);
  $("#content").scrollTop = 0;
  $("#content").classList.toggle("home-fit", key === "home");  // 首页一屏不滚动
  curKey = key;
  navBtns.forEach((b) => b.classList.toggle("active", b.dataset.page === key));
  moveIndicator(navBtns[to]);
  PAGE_LOADERS[key]?.();
}
nav.addEventListener("click", (e) => {
  const btn = e.target.closest(".nav-item");
  if (btn) goPage(btn.dataset.page);
});
$("#anchorRow").addEventListener("click", (e) => {
  const btn = e.target.closest(".anchor");
  if (!btn) return;
  [...btn.parentElement.children].forEach((b) => b.classList.toggle("active", b === btn));
  document.getElementById(btn.dataset.anchor)?.scrollIntoView({ behavior: "smooth", block: "start" });
});
window.addEventListener("resize", () =>
  moveIndicator(navBtns.find((b) => b.dataset.page === curKey) || navBtns[0]));

/* ============================================================
   主题（切肤 + 落盘 + 推荐壁纸联动 + 跟随系统）
   ============================================================ */
const THUMB_GRAD = {
  abyss: "linear-gradient(135deg,#0B2540,#1E4668 55%,#4FB3FF)",
  neon: "linear-gradient(135deg,#0A0E1A,#14203C 55%,#00F0FF 130%)",
  tokyonight: "linear-gradient(135deg,#1A1B26,#2F354D 60%,#7AA2F7)",
  stellar: "linear-gradient(135deg,#1A1230,#3A2E63 55%,#C9A227)",
  moxin: "linear-gradient(135deg,#F6F1E7,#E5D9BF 60%,#B03A2E)",
  cream: "linear-gradient(135deg,#FFF8F0,#FFE3CE 60%,#FF9F6E)",
  mint: "linear-gradient(135deg,#F4FBF8,#CFF0E4 60%,#2EC4A0)",
};
$$(".theme-thumb").forEach((btn) => {
  btn.querySelector(".th-preview").style.background = THUMB_GRAD[btn.dataset.themeKey];
  btn.addEventListener("click", () => pickTheme(btn.dataset.themeKey, true));
});
function markThemeThumb(key) {
  $$(".theme-thumb").forEach((b) =>
    b.classList.toggle("active", b.dataset.themeKey === key));
}
async function pickTheme(key, persist) {
  document.documentElement.dataset.theme = key;
  markThemeThumb(key);
  if (persist) {
    if (S.ui) S.ui.theme = key;
    if (S.ui?.follow_system) {          // 显式选择 = 关闭跟随系统
      S.ui.follow_system = false;
      $("#followSystem").checked = false;
      API.save_config({ follow_system: false });
    }
    const r = await API.set_theme(key);
    if (r.ok && r.wallpaper_applied) {  // 主题推荐壁纸联动（对齐旧版语义）
      S.ui.wallpaper_path = r.wallpaper_applied;
      markWpThumb(r.wallpaper_applied);
      await applyWallpaperLayer(r.wallpaper_applied);
      toast("已应用该主题的推荐壁纸", "ok");
    }
  }
  syncSlidersToTheme();
}
function applyFollowSystem() {
  if (!S.ui?.follow_system) return;
  const dark = matchMedia("(prefers-color-scheme: dark)").matches;
  document.documentElement.dataset.theme = dark ? "tokyonight" : "cream";
  markThemeThumb(document.documentElement.dataset.theme);
  syncSlidersToTheme();
}
$("#followSystem").addEventListener("change", async (e) => {
  if (S.ui) S.ui.follow_system = e.target.checked;
  await API.save_config({ follow_system: e.target.checked });
  applyFollowSystem();
  toast(e.target.checked ? "已跟随系统深浅色" : "已改为手动选择主题", "ok");
});
matchMedia("(prefers-color-scheme: dark)")
  .addEventListener("change", () => applyFollowSystem());

/* ============================================================
   外观滑块（卡片透明度 / 壁纸浓度 / 毛玻璃 / 面板浓度）+ 字号
   语义：cfg 值为 null = 跟随主题推荐（切主题回推荐值）；
   毛玻璃是全局偏好，不随主题重置（0 = 无模糊）。
   ============================================================ */
const debounces = {};
function debounceSave(key, value, delay = 500) {
  clearTimeout(debounces[key]);
  debounces[key] = setTimeout(() => API.save_config({ [key]: value }), delay);
}
function bindSlider(id, apply) {
  const r = document.getElementById(id);
  const out = document.getElementById(id + "V");
  const sync = () => {
    const pct = (r.value - r.min) / (r.max - r.min) * 100;
    r.style.setProperty("--fill", pct + "%");
    if (out) out.textContent = r.value + "%";
  };
  r._sync = sync;
  r.addEventListener("input", () => { sync(); apply(parseFloat(r.value)); });
  r.addEventListener("change", () => { sync(); apply(parseFloat(r.value), true); });
  sync();
}
const cardSlider = $("#cardOpacity"), wallSlider = $("#wallOpacity"),
  blurSlider = $("#blurAmount"), panelSlider = $("#panelOpacity");
function setCardOp(v) { document.documentElement.style.setProperty("--card-op", v); }
function setWallOp(v) { document.documentElement.style.setProperty("--wall-opacity", v); }
function setPanelOp(v) { document.documentElement.style.setProperty("--panel-op", v); }
function setBlur(lv) {
  document.documentElement.style.setProperty("--blur",
    (lv / 100 * 22).toFixed(1) + "px");
}
bindSlider("cardOpacity", (v, final) => {
  setCardOp(v / 100);
  if (S.ui) S.ui.card_opacity = v / 100;
  if (final) debounceSave("card_opacity", v / 100, 200);
});
bindSlider("wallOpacity", (v, final) => {
  setWallOp(v / 100);
  if (S.ui) S.ui.wall_opacity = v / 100;
  if (final) debounceSave("wall_opacity", v / 100, 200);
});
bindSlider("blurAmount", (v, final) => {
  setBlur(v);
  if (S.ui) S.ui.blur_level = v;
  if (final) debounceSave("blur_level", Math.round(v), 200);
});
bindSlider("panelOpacity", (v, final) => {
  setPanelOp(v / 100);
  if (S.ui) S.ui.panel_opacity = v / 100;
  if (final) debounceSave("panel_opacity", v / 100, 200);
});
/* 切主题：cfg 为 null 的键回推荐值并去掉 inline；用户值保持不动 */
function syncSlidersToTheme() {
  const cs = getComputedStyle(document.documentElement);
  const u = S.ui || {};
  const applyOrTheme = (slider, cssSet, cfgVal, themeVal) => {
    if (cfgVal == null) {
      cssSet(themeVal);
      slider.value = Math.round(themeVal * 100);
    } else {
      slider.value = Math.round(cfgVal * 100);
    }
    slider._sync();
  };
  const themeCard = parseFloat(cs.getPropertyValue("--card-alpha")) || 0.66;
  const themeWall = parseFloat(cs.getPropertyValue("--wall-opacity")) || 0.12;
  applyOrTheme(cardSlider, setCardOp, u.card_opacity, themeCard);
  applyOrTheme(wallSlider, setWallOp, u.wall_opacity, themeWall);
  /* 面板浓度：null = 不遮罩（100% 显示原面板底色） */
  if (u.panel_opacity == null) { setPanelOp(1); panelSlider.value = 100; }
  else { setPanelOp(u.panel_opacity); panelSlider.value = Math.round(u.panel_opacity * 100); }
  panelSlider._sync();
  /* 毛玻璃：全局偏好，切主题不动 */
  if (u.blur_level != null) { blurSlider.value = u.blur_level; blurSlider._sync(); }
}
/* 启动时按 cfg 应用外观 */
function applyStartupAppearance() {
  const u = S.ui || {};
  if (u.blur_level != null) setBlur(u.blur_level);
  if (u.card_opacity != null) setCardOp(u.card_opacity);
  if (u.wall_opacity != null) setWallOp(u.wall_opacity);
  if (u.panel_opacity != null) setPanelOp(u.panel_opacity);
  document.documentElement.dataset.fs = u.font_scale || "medium";
  syncSlidersToTheme();
}
$("#fontSeg").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  [...btn.parentElement.children].forEach((b) => b.classList.toggle("active", b === btn));
  document.documentElement.dataset.fs = btn.dataset.v;
  if (S.ui) S.ui.font_scale = btn.dataset.v;
  await API.save_config({ font_scale: btn.dataset.v });
});

/* ============================================================
   壁纸（库网格 / 应用 / 导入 / 清除）
   ============================================================ */
function markWpThumb(path) {
  $$(".wp-thumb").forEach((b) =>
    b.classList.toggle("active", (b.dataset.path || "") === (path || "")));
}
async function applyWallpaperLayer(path) {
  const layer = $("#wallpaperLayer");
  if (!path) { layer.style.backgroundImage = "none"; return; }
  const r = await API.wallpaper_data(path);
  if (r.ok && r.data) layer.style.backgroundImage = `url("${r.data}")`;
}
async function pickWallpaper(path) {
  const r = await API.set_wallpaper(path || "");
  if (!r.ok) { toast(r.error || "设置失败", "err"); return; }
  if (S.ui) S.ui.wallpaper_path = path || "";
  markWpThumb(path || "");
  await applyWallpaperLayer(path || "");
  toast(path ? "壁纸已应用" : "已清除壁纸，恢复纯渐变背景", "ok");
}
async function loadWallpaperGrid() {
  const r = await API.list_wallpapers();
  if (!r.ok) return;
  const grid = $("#wpGrid");
  grid.innerHTML = "";
  const none = document.createElement("button");
  none.className = "wp-thumb none-thumb";
  none.dataset.path = "";
  none.textContent = "无壁纸";
  none.addEventListener("click", () => pickWallpaper(""));
  grid.appendChild(none);
  const mk = (it) => {
    const b = document.createElement("button");
    b.className = "wp-thumb";
    b.dataset.path = it.path;
    b.innerHTML = `<img src="${esc(it.thumb)}" alt=""><span class="wp-name">${esc(it.name)}</span>` +
      `<span class="wp-check"><svg class="ic"><use href="#i-check"/></svg></span>`;
    b.addEventListener("click", () => pickWallpaper(it.path));
    grid.appendChild(b);
  };
  (r.items || []).forEach(mk);
  if (r.custom) mk(r.custom);
  markWpThumb(r.current || "");
}
$("#btnWpImport").addEventListener("click", async () => {
  const r = await API.import_wallpaper();
  if (!r.ok) { toast(r.error || "导入失败", "err"); return; }
  if (r.canceled) return;
  if (S.ui) S.ui.wallpaper_path = r.path;
  await loadWallpaperGrid();
  markWpThumb(r.path);
  await applyWallpaperLayer(r.path);
  toast("自定义壁纸已应用", "ok");
});
$("#btnWpClear").addEventListener("click", () => pickWallpaper(""));

/* ============================================================
   标题栏窗口控制
   ============================================================ */
$("#btnMin").addEventListener("click", () => {
  // 收起动画：向底部（任务栏方向）缩退淡出后再交给系统最小化，
  // 动画结束即复位样式——最小化期间窗口不可见，恢复后是干净状态
  const app = $(".app");
  app.classList.add("win-min-out");
  setTimeout(async () => {
    await API.win_min();
    setTimeout(() => app.classList.remove("win-min-out"), 260);
  }, 210);
});
$("#btnMax").addEventListener("click", () => API.win_max_toggle());
/* 关窗 = 弹三选一（对齐旧桌面端）：完全退出 / 最小化托盘 / 取消 */
$("#btnClose").addEventListener("click", () => openModal("modalExit"));
$("#exitToTray").addEventListener("click", async () => {
  closeModal();
  await API.win_hide();
});
$("#exitQuit").addEventListener("click", async () => {
  closeModal();
  await API.quit_app();
});
$("#exitCancel").addEventListener("click", () => closeModal());

/* ============================================================
   引擎状态机（主按钮 / 状态行 / 标题栏 pill）
   ============================================================ */
const STATE_LABEL = {
  idle: ["初始化引擎", "i-power"], initializing: ["初始化中…", "i-power"],
  initialized: ["启动 bot", "i-play"], running: ["暂停回复", "i-pause"],
  paused: ["继续运行", "i-power"], stopped: ["已停止", "i-power"],
  error: ["重新初始化", "i-power"],
};
function applyEngineState(state, paused, error) {
  if (state === "loading") return;
  S.engineState = state;
  const btn = $("#powerBtn");
  const label = STATE_LABEL[state] || ["—", "i-power"];
  btn.disabled = state === "initializing" || state === "stopped";
  btn.classList.remove("paused", "state-idle", "state-error", "state-stopped");
  if (state === "running") btn.classList.add("paused");
  if (state === "idle") btn.classList.add("state-idle");
  if (state === "error") btn.classList.add("state-error");
  if (state === "stopped") btn.classList.add("state-stopped");
  $("#powerIc").innerHTML = `<use href="#${label[1]}"/>`;
  $("#powerLabel").textContent = label[0];
  const stEl = $("#engineState");
  stEl.classList.remove("ok", "warn", "bad");
  if (state === "running") { stEl.textContent = "运行中 · 自动回复开启"; stEl.classList.add("ok"); }
  else if (state === "paused") { stEl.textContent = "已暂停 · 等你回来"; stEl.classList.add("warn"); }
  else if (state === "error") { stEl.textContent = "初始化失败" + (error ? "：" + error : ""); stEl.classList.add("bad"); }
  else if (state === "stopped") stEl.textContent = "引擎已停止";
  else if (state === "initializing") stEl.textContent = "正在初始化…";
  else if (state === "initialized") { stEl.textContent = "已就绪，点击「启动 bot」"; stEl.classList.add("ok"); }
  else stEl.textContent = "尚未初始化";
  const pill = $("#runPill");
  pill.classList.toggle("paused", state === "paused" || state === "error");
  $("#runPillText").textContent =
    state === "running" ? "运行中" :
    state === "paused" ? "已暂停" :
    state === "error" ? "异常" :
    state === "initializing" ? "初始化中" :
    state === "stopped" ? "已停止" : "就绪";
}
$("#powerBtn").addEventListener("click", async () => {
  const st = S.engineState;
  if (st === "idle" || st === "error") {
    const ok = await confirmModal("初始化引擎",
      "初始化会自动定位微信窗口并开始截图监听。\n\n自动化期间请勿操作电脑、勿最小化微信窗口——" +
      "最小化时视觉通道是盲的，动鼠标会读到错误画面。确认现在初始化？");
    if (!ok) return;
    await API.engine_action("initialize");
  } else if (st === "initialized") await API.engine_action("start");
  else if (st === "running") await API.engine_action("pause");
  else if (st === "paused") await API.engine_action("resume");
  const r = await API.get_status();
  if (r.ok) applyEngineState(r.state, r.paused, r.error);
});

/* ============================================================
   环境检查 / 天枢安装 / 首轮提示词 / 更新
   ============================================================ */
function envCard(cardSel, detSel, item) {
  const card = $(cardSel);
  $(detSel).textContent = item.detail || "";
  card.classList.toggle("ok", !!item.ok);
  card.classList.toggle("bad", item.ok === false);
}
async function checkEnv() {
  const r = await API.check_env();
  if (!r.ok) return;
  const rp = r.report;
  envCard("#envWechat", "#envWechatDetail", rp.wechat);
  envCard("#envTianshu", "#envTianshuDetail", rp.tianshu);
  $("#btnInstallTianshu").hidden = rp.tianshu.ok !== false;
  envCard("#envPrompt", "#envPromptDetail", rp.first_prompt);
}
/* 慢操作按钮的 busy 态：转圈 + 禁点，让「点了有没有反应」看得见 */
async function withBusy(btn, fn) {
  if (!btn || btn.disabled) return;
  btn.disabled = true;
  btn.classList.add("busy");
  try {
    await fn();
  } finally {
    btn.disabled = false;
    btn.classList.remove("busy");
  }
}
$$("button[data-env]").forEach((b) =>
  b.addEventListener("click", () => withBusy(b, checkEnv)));
$("#btnInstallTianshu").addEventListener("click", async () => {
  const ok = await confirmModal("安装天枢桌面端",
    "将从 GitHub 下载天枢并解压到用户目录 Tianshu 文件夹，下载进度显示在卡片上。继续？");
  if (!ok) return;
  const r = await API.install_tianshu();
  if (!r.ok) toast(r.error || "安装任务启动失败", "err");
});
function onInstallProgress(p) {
  const bar = $("#installBar");
  if (p.done) {
    bar.hidden = true;
    toast(p.ok ? "天枢安装完成 ✓" : "安装失败：" + (p.error || "未知错误"),
      p.ok ? "ok" : "err");
    if (p.ok) checkEnv();
  } else {
    bar.hidden = false;
    bar.querySelector("span").style.width = (p.pct || 0) + "%";
  }
}
$("#btnSendPrompt").addEventListener("click", async () => {
  const r = await API.send_first_prompt();
  if (r.ok) toast("正在发送首轮提示词…（结果见提示）");
  else toast(r.error || "发送失败", "err");
});
let lastReleaseUrl = "";
async function checkUpdate(silent) {
  const r = await API.check_update();
  if (!r.ok) { if (!silent) toast(r.error || "检查更新失败", "err"); return; }
  $("#verBadge").textContent = "v" + (r.current || "—");
  if (r.newer) {
    $("#verSub").textContent = `发现新版本 v${r.latest}（当前 v${r.current}）`;
    $("#btnGotoRelease").hidden = false;
    lastReleaseUrl = r.url || "";
  } else if (silent) {
    $("#verSub").textContent = `已是最新版本（v${r.current}）`;
  } else {
    $("#verSub").textContent = r.error ? "检查更新失败：" + r.error
      : `已是最新版本（v${r.current}）`;
  }
}
$("#btnCheckUpdate").addEventListener("click", () =>
  withBusy($("#btnCheckUpdate"), () => checkUpdate(false)));
$("#btnGotoRelease").addEventListener("click", () => {
  if (lastReleaseUrl) API.open_external(lastReleaseUrl);
});

/* ============================================================
   日志（首页小窗 + 日志页大窗共享增量流）
   ============================================================ */
function logLevelOf(line) {
  if (/ ERROR /.test(line)) return "err";
  if (/ WARNING /.test(line)) return "warn";
  return "info";
}
function appendLogLines(lines, cap) {
  for (const sel of ["#logBody", "#logPageBody"]) {
    const body = $(sel);
    if (!body) continue;
    const limit = sel === "#logBody" ? 60 : (cap || 800);
    for (const line of lines) {
      const div = document.createElement("div");
      div.className = "log-line";
      const lv = logLevelOf(line);
      div.innerHTML = `<span class="lv ${lv}">${lv.toUpperCase()}</span><span></span>`;
      div.lastChild.textContent = line;
      body.appendChild(div);
    }
    while (body.children.length > limit) body.firstChild.remove();
    body.scrollTop = body.scrollHeight;
  }
}
$("#btnLogClear").addEventListener("click", () => { $("#logBody").innerHTML = ""; });
$("#btnPageLogClear").addEventListener("click", () => { $("#logPageBody").innerHTML = ""; });
function initLogPage() {
  const body = $("#logPageBody");
  if (body.children.length) return;   // 已初始化过：增量推送持续追加
  API.tail_log().then((r) => {
    if (r.ok && r.lines?.length) appendLogLines(r.lines, 800);
  });
}

/* ============================================================
   触发器（首页徽章 + 管理模态）
   ============================================================ */
async function refreshTriggerBadge() {
  const r = await API.list_triggers();
  if (!r.ok) return;
  S.trigRows = r.rows;
  const timeN = r.rows.filter((x) => x.kind === "time" && !x.terminal).length;
  const condN = r.rows.filter((x) => x.kind === "condition" && !x.terminal).length;
  $("#trigTimeNum").textContent = timeN ? timeN + " 个待触发" : "无";
  $("#trigCondNum").textContent = condN ? condN + " 个监视中" : "无";
  $("#trigTimeBar").style.width = Math.min(100, timeN * 33) + "%";
  $("#trigCondBar").style.width = Math.min(100, condN * 33) + "%";
  $("#statTriggers").textContent = r.active ? `活跃 ${r.active} 个` : "无";
}
function trigBrief(r) {
  if (r.kind === "condition") {
    const c = String(r.condition || "");
    return c.length > 40 ? c.slice(0, 40) + "…" : c;
  }
  const c = String(r.content || "").trim();
  if (!c) return "（到点回递 AI 生成回复）";
  return c.length > 40 ? c.slice(0, 40) + "…" : c;
}
function fmtTrigTime(r) {
  const hm = (ts) => ts ? new Date(ts * 1000).toLocaleString("zh-CN",
    { month: "2-digit", day: "2-digit", hour: "2-digit", minute: "2-digit" }) : "—";
  if (r.kind === "condition") {
    if (r.done) return "已结束";
    return `每${r.interval_seconds || 60}s · ` +
      (r.expire_at ? "截止 " + hm(r.expire_at) : "未设截止");
  }
  const prefix = { daily: "每天 ", weekly: "每周 " }[r.repeat] || "";
  return prefix + hm(r.fire_at);
}
function trigDetailText(r) {
  const lines = [`ID：${r.id}`,
    `类型：${r.kind === "condition" ? "状态监视" : "定时"}`,
    `目标聊天：${r.chat}`, `状态：${r.state_text || "—"}`];
  if (r.kind === "condition") {
    lines.push(`条件：${r.condition || ""}`, `轮询目标：${r.url || ""}`);
    if ((r.judge || "local") === "local") {
      const scope = (r.scope_start || r.scope_end)
        ? `；切片 ${r.scope_start || "页首"}～${r.scope_end || "页尾"}` : "";
      lines.push(`判定：本地关键词（${r.match_type || "present"}，关键词：${(r.met_keywords || []).join("、") || "—"}${scope}）`);
    } else lines.push("判定：模型判定（每轮一次小调用）");
    lines.push(`轮询间隔：${r.interval_seconds || 60}s · 连续失败：${r.fail_count || 0}`);
    if (r.evidence) lines.push(`最近证据：${String(r.evidence).slice(0, 200)}`);
  } else {
    lines.push(`触发时间：${fmtTrigTime(r)}`);
    if (r.last_fired) lines.push("上次触发：" + fmtTrigTime({ fire_at: r.last_fired }));
    lines.push(`内容：${r.content || "（到点回递 AI 生成回复）"}`);
  }
  return lines.join("\n");
}
function renderTrigTable() {
  const tb = $("#trigTbody");
  tb.innerHTML = "";
  if (!S.trigRows.length) {
    tb.innerHTML = `<tr><td colspan="6" style="color:var(--muted);text-align:center;padding:20px">
      还没有触发器——对话里让小漓「明天下午3点提醒我…」即可创建</td></tr>`;
    $("#trigDetail").textContent = "";
    return;
  }
  for (const r of S.trigRows) {
    const tr = document.createElement("tr");
    const stText = r.state_text || "—";
    const stCls = r.terminal ? "end" :
      stText === "监视中" || stText === "触发中" || stText === "待触发" ? "on" :
      stText === "已暂停" || stText === "已错过" ? "off" : "mid";
    tr.innerHTML = `<td>${r.kind === "condition" ? "状态监视" : "定时"}</td>
      <td>${esc(r.chat || "")}</td><td>${esc(trigBrief(r))}</td>
      <td><span class="tstate ${stCls}">${stText}</span></td>
      <td>${esc(fmtTrigTime(r))}</td><td></td>`;
    const opTd = tr.lastElementChild;
    const mkBtn = (text, cls, fn) => {
      const b = document.createElement("button");
      b.className = "mini-btn ghost " + cls;
      b.style.padding = "3px 10px";
      b.textContent = text;
      b.addEventListener("click", fn);
      return b;
    };
    if (!r.terminal) {
      opTd.appendChild(mkBtn(r.enabled ? "暂停" : "恢复", "", async () => {
        await API.set_trigger_enabled(r.id, !r.enabled);
        await refreshTriggerBadge(); renderTrigTable();
      }));
    }
    opTd.appendChild(mkBtn("删除", "danger", async () => {
      if (!(await confirmModal("删除触发器", `确定删除「${trigBrief(r)}」？该操作不可撤销。`))) return;
      await API.delete_trigger(r.id);
      await refreshTriggerBadge(); renderTrigTable();
    }));
    tr.addEventListener("click", (e) => {
      if (e.target.closest("button")) return;
      $("#trigDetail").textContent = trigDetailText(r);
    });
    tb.appendChild(tr);
  }
}
$("#btnTriggers").addEventListener("click", async () => {
  await refreshTriggerBadge();
  renderTrigTable();
  openModal("modalTriggers");
});
$("#btnManageTriggers").addEventListener("click", async () => {
  await refreshTriggerBadge();
  renderTrigTable();
  openModal("modalTriggers");
});
$("#btnTrigAddTime").addEventListener("click", () => {
  const dt = new Date(Date.now() + 600000);
  dt.setMinutes(dt.getMinutes() - dt.getTimezoneOffset() * -0);
  const pad = (n) => String(n).padStart(2, "0");
  $("#trigFireAt").value = `${dt.getFullYear()}-${pad(dt.getMonth() + 1)}-${pad(dt.getDate())}T${pad(dt.getHours())}:${pad(dt.getMinutes())}`;
  openModal("modalTrigTime");
});
$("#btnTrigTimeSave").addEventListener("click", async () => {
  const chat = $("#trigChat").value.trim(), content = $("#trigContent").value.trim();
  const fire = $("#trigFireAt").value;
  if (!chat || !fire) { toast("目标聊天和触发时间必填", "err"); return; }
  const ts = new Date(fire).getTime() / 1000;
  const r = await API.add_trigger(chat, content, ts, $("#trigRepeat").value);
  if (!r.ok) { toast(r.error || "创建失败", "err"); return; }
  closeModal();
  await refreshTriggerBadge(); renderTrigTable();
  toast("定时提醒已创建 ✓", "ok");
});
$("#btnTrigAddCond").addEventListener("click", () => openModal("modalTrigCond"));
$("#btnTrigCondSave").addEventListener("click", async () => {
  const chat = $("#condChat").value.trim(), url = $("#condUrl").value.trim();
  const cond = $("#condText").value.trim();
  if (!chat || !url || !cond) { toast("聊天、URL、条件均为必填", "err"); return; }
  const expire = $("#condExpire").value;
  const r = await API.add_condition(chat, $("#condContent").value.trim(), url, cond,
    $("#condJudge").value, $("#condMatch").value,
    $("#condKeywords").value.split(/[,，]/).map((s) => s.trim()).filter(Boolean),
    null, null, parseInt($("#condInterval").value || "60", 10),
    expire ? new Date(expire).getTime() / 1000 : null);
  if (!r.ok) { toast(r.error || "创建失败", "err"); return; }
  closeModal();
  await refreshTriggerBadge(); renderTrigTable();
  toast("状态监视已创建 ✓", "ok");
});

/* ============================================================
   角色卡页
   ============================================================ */
async function loadCards() {
  const r = await API.list_cards();
  if (!r.ok) return;
  S.cards = r.cards || [];
  S.activeCardId = r.active_id || "";
  $("#cardsCount").textContent = S.cards.length + " 张";
  renderCardList();
  if (!S.editingCardId && S.cards.length)
    fillCardForm(S.cards.find((c) => c.id === S.activeCardId) || S.cards[0]);
}
function renderCardList() {
  const box = $("#cardsList");
  box.innerHTML = "";
  for (const c of S.cards) {
    const b = document.createElement("button");
    b.className = "card-item" + (c.id === S.editingCardId ? " active" : "");
    b.innerHTML = `<span class="ci-emoji">${esc(c.emoji || "🐟")}</span>
      <span class="ci-name">${esc(c.name)}</span>
      ${c.id === S.activeCardId ? '<span class="ci-star">★</span>' : ""}`;
    b.addEventListener("click", () => fillCardForm(c));
    box.appendChild(b);
  }
}
function fillCardForm(c) {
  S.editingCardId = c ? c.id : "";
  $("#cardName").value = c?.name || "";
  $("#cardEmoji").value = c?.emoji || "";
  $("#cardNickname").value = c?.nickname || "小漓";
  $("#cardPrompt").value = c?.system_prompt || "";
  $("#cardTemp").value = c?.temperature ?? 0.7;
  $("#cardTopP").value = c?.top_p ?? 0.9;
  $("#cardHistory").value = c?.max_history ?? 1000;
  $("#cardEditorHint").textContent = c ? c.id : "新卡片";
  renderCardList();
}
function collectCard() {
  const name = $("#cardName").value.trim();
  const sp = $("#cardPrompt").value.trim();
  if (!name) { toast("卡片名称必填", "err"); return null; }
  if (!sp) { toast("人格设定（system prompt）必填", "err"); return null; }
  const old = S.cards.find((c) => c.id === S.editingCardId) || {};
  return {
    id: S.editingCardId || "",
    name, emoji: $("#cardEmoji").value.trim(),
    nickname: $("#cardNickname").value.trim() || "小漓",
    system_prompt: sp,
    chat_provider: old.chat_provider || "", chat_model: old.chat_model || "",
    temperature: parseFloat($("#cardTemp").value) || 0.7,
    top_p: parseFloat($("#cardTopP").value) || 0.9,
    max_history: parseInt($("#cardHistory").value) || 1000,
  };
}
$("#btnCardNew").addEventListener("click", () => fillCardForm(null));
$("#btnCardSave").addEventListener("click", async () => {
  const card = collectCard();
  if (!card) return;
  const r = await API.save_card(card);
  if (!r.ok) { toast(r.error || "保存失败", "err"); return; }
  S.editingCardId = r.card.id;
  await loadCards();
  toast("卡片已保存 ✓", "ok");
});
$("#btnCardActivate").addEventListener("click", async () => {
  if (!S.editingCardId) { toast("请先选择要激活的卡片", "err"); return; }
  const r = await API.activate_card(S.editingCardId);
  if (!r.ok) { toast(r.error || "激活失败", "err"); return; }
  S.activeCardId = S.editingCardId;
  renderCardList();
  toast(r.hot_applied ? "已激活并热切换 ✓" : "已激活（引擎未就绪，启动后生效）", "ok");
});
$("#btnCardDelete").addEventListener("click", async () => {
  if (!S.editingCardId) { toast("请先选择卡片", "err"); return; }
  if (!(await confirmModal("删除角色卡", `确定删除卡片「${$("#cardName").value}」？该操作不可撤销。`))) return;
  const r = await API.delete_card(S.editingCardId);
  if (!r.ok) { toast(r.error || "删除失败", "err"); return; }
  S.editingCardId = "";
  await loadCards();
  fillCardForm(null);
  toast("卡片已删除", "ok");
});
$("#btnCardDuplicate").addEventListener("click", async () => {
  if (!S.editingCardId) { toast("请先选择卡片", "err"); return; }
  const r = await API.duplicate_card(S.editingCardId);
  if (!r.ok) { toast(r.error || "复制失败", "err"); return; }
  S.editingCardId = r.card.id;
  await loadCards();
  toast("已创建副本 ✓", "ok");
});
$("#btnCardExport").addEventListener("click", async () => {
  if (!S.editingCardId) { toast("请先选择卡片", "err"); return; }
  const r = await API.export_card(S.editingCardId);
  if (!r.ok) { toast(r.error || "导出失败", "err"); return; }
  downloadText(`${r.card.id}.json`, JSON.stringify(r.card, null, 2), "application/json");
});
$("#btnCardImport").addEventListener("click", () => $("#filePick").click());
$("#filePick").addEventListener("change", async (e) => {
  const f = e.target.files[0];
  e.target.value = "";
  if (!f) return;
  try {
    const data = JSON.parse(await f.text());
    const r = await API.import_card(data);
    if (!r.ok) { toast(r.error || "导入失败", "err"); return; }
    await loadCards();
    toast(`卡片「${r.card.name}」导入成功 ✓`, "ok");
  } catch (err) {
    toast("文件不是有效 JSON：" + err.message, "err");
  }
});
function downloadText(filename, content, mime) {
  const a = document.createElement("a");
  a.href = URL.createObjectURL(new Blob([content], { type: mime || "text/plain" }));
  a.download = filename;
  a.click();
  setTimeout(() => URL.revokeObjectURL(a.href), 4000);
}

/* ============================================================
   模型页
   ============================================================ */
const PRESETS = [
  { id: "deepseek", name: "DeepSeek 深度求索", base_url: "https://api.deepseek.com/v1/chat/completions",
    models: ["deepseek:deepseek-v4-flash", "deepseek:deepseek-v4-pro", "deepseek:deepseek-v4-flash-vision-exp"] },
  { id: "zhipu", name: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    models: ["zhipu:glm-5.3", "zhipu:glm-5.3-flash"] },
  { id: "qwen", name: "通义千问", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    models: ["qwen:qwen3.8-max", "qwen:qwen3.8-flash"] },
  { id: "kimi", name: "月之暗面 Kimi", base_url: "https://api.moonshot.cn/v1/chat/completions",
    models: ["kimi:kimi-k3"] },
  { id: "doubao", name: "豆包（火山引擎）", base_url: "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    models: ["doubao:doubao-seed-evolving", "doubao:doubao-seed-2.0-mini"] },
  { id: "siliconflow", name: "硅基流动 SiliconFlow", base_url: "https://api.siliconflow.cn/v1/chat/completions",
    models: ["siliconflow:deepseek-ai/deepseek-v4-flash-0731", "siliconflow:deepseek-ai/deepseek-v4-pro"] },
];
async function loadModels() {
  const r = await API.get_providers();
  if (r.ok) S.providers = r.providers || [];
  renderProvTable();
  let card = S.cards.find((c) => c.id === S.activeCardId);
  if (!card) {                      // 卡片页可能尚未加载过
    const cr = await API.list_cards();
    if (cr.ok) {
      S.cards = cr.cards || [];
      S.activeCardId = cr.active_id || "";
      card = S.cards.find((c) => c.id === S.activeCardId);
    }
  }
  fillProvSelect(card?.chat_provider || (S.providers[0] || {}).id,
    card?.chat_model || "");
}
function renderProvTable() {
  const tb = $("#provTbody");
  tb.innerHTML = "";
  S.providers.forEach((p, i) => {
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><input data-f="name" value="${esc(p.name || "")}"></td>
      <td><input data-f="base_url" value="${esc(p.base_url || "")}"></td>
      <td><input data-f="api_key" type="password" value="${esc(p.api_key || "")}"></td>
      <td><input data-f="models" value="${esc((p.models || []).join(", "))}"></td>
      <td><input data-f="id" value="${esc(p.id || "")}"></td>
      <td><button class="row-del" title="删除该行"><svg class="ic"><use href="#i-trash"/></svg></button></td>`;
    tr.querySelector(".row-del").addEventListener("click", () => {
      S.providers.splice(i, 1);
      renderProvTable();
    });
    tb.appendChild(tr);
  });
  if (!S.providers.length) {
    tb.innerHTML = `<tr><td colspan="6" style="color:var(--muted);text-align:center;padding:18px">
      还没有 Provider——点上方「添加 Provider」或用预设快捷添加</td></tr>`;
  }
}
function collectProv() {
  const rows = $$("#provTbody tr");
  const out = [];
  for (const tr of rows) {
    const ins = $$("input", tr);
    if (ins.length < 5) continue;
    const get = (f) => ($(`input[data-f="${f}"]`, tr)?.value || "").trim();
    if (!get("base_url") && !get("name")) continue;
    out.push({
      id: get("id") || "p" + (out.length + 1),
      name: get("name"), base_url: get("base_url"),
      api_key: get("api_key"),
      models: get("models").split(/[,，]/).map((s) => s.trim()).filter(Boolean),
    });
  }
  return out;
}
$("#btnProvAdd").addEventListener("click", () => {
  S.providers = collectProv();
  S.providers.push({ id: "p" + (S.providers.length + 1), name: "", base_url: "", api_key: "", models: [] });
  renderProvTable();
});
$("#presetChips").innerHTML = PRESETS.map((p) =>
  `<button class="chip" data-pid="${p.id}">+ ${esc(p.name.split(" ")[0])}</button>`).join("");
$("#presetChips").addEventListener("click", (e) => {
  const chip = e.target.closest(".chip");
  if (!chip) return;
  const preset = PRESETS.find((p) => p.id === chip.dataset.pid);
  S.providers = collectProv();
  if (S.providers.some((p) => p.id === preset.id)) { toast("该预设已存在", "err"); return; }
  S.providers.push({ ...preset, api_key: "" });
  renderProvTable();
  toast(`已添加预设「${preset.name}」，填入 API Key 后保存`, "ok");
});
async function saveProviders() {
  const provs = collectProv();
  const cp = $("#cmbProv").value, cm = $("#cmbModel").value.trim();
  const r = await API.save_providers(provs, cp || null, cm || null);
  if (!r.ok) { toast(r.error || "保存失败", "err"); return; }
  S.providers = r.providers || provs;
  renderProvTable();
  toast("已保存并应用 ✓", "ok");
}
$("#btnProvSave").addEventListener("click", saveProviders);
$("#btnModelSave").addEventListener("click", saveProviders);
$("#btnProvTest").addEventListener("click", async () => {
  const provs = collectProv();
  const p = provs[0];
  const el = $("#provTestResult");
  if (!p) { el.textContent = "先添加一行 Provider"; return; }
  el.textContent = "测试中…"; el.className = "test-result";
  const r = await API.test_provider(p.base_url, p.api_key);
  if (r.ok) {
    el.textContent = `✓ 连接成功（${r.total} 个模型）`;
    el.className = "test-result ok";
    const pref = p.id + ":";
    p.models = r.models.map((m) => m.startsWith(pref) ? m : pref + m);
    S.providers = provs;
    renderProvTable();
    fillProvSelect($("#cmbProv").value, $("#cmbModel").value);
  } else {
    el.textContent = "✗ " + r.error;
    el.className = "test-result bad";
  }
});
function fillProvSelect(sel, model) {
  const cb = $("#cmbProv");
  cb.innerHTML = S.providers.map((p) =>
    `<option value="${esc(p.id)}">${esc(p.name || p.id)}</option>`).join("");
  if (sel) cb.value = sel;
  fillModelDatalist();
  if (model) $("#cmbModel").value = model;
}
function fillModelDatalist() {
  const pid = $("#cmbProv").value;
  const p = S.providers.find((x) => x.id === pid);
  $("#modelList").innerHTML = (p?.models || [])
    .map((m) => `<option value="${esc(m)}">`).join("");
}
$("#cmbProv").addEventListener("change", fillModelDatalist);

/* ============================================================
   用量页
   ============================================================ */
function fmtTokens(v) {
  if (v == null) return "—";
  if (v >= 1e6) return (v / 1e6).toFixed(2) + "M";
  if (v >= 1e4) return (v / 1e3).toFixed(1) + "K";
  return String(v);
}
async function loadUsage() {
  const r = await API.get_usage();
  if (!r.ok) return;
  const c = r.cards;
  $("#uTodayCalls").textContent = c.today_calls ?? "—";
  $("#uTodayCost").textContent = c.today_cost != null ? c.today_cost.toFixed(2) : "—";
  $("#uTodayTokens").textContent = fmtTokens(c.today_tokens);
  $("#uTodayCache").textContent = c.today_cache != null ? (c.today_cache * 100).toFixed(0) + "%" : "—";
  $("#uTokens30").textContent = fmtTokens(c.tokens_30d);
  $("#uCost30").textContent = c.cost_30d != null ? `≈ ¥${c.cost_30d.toFixed(2)}（估算）` : "";
  $("#uFail30").textContent = c.fail_30d ?? "—";
  const tb = $("#modelTbody");
  tb.innerHTML = (r.by_model || []).map((m) => `<tr>
    <td>${esc(m.model)}</td><td class="baloo">${m.calls}</td>
    <td class="baloo">${fmtTokens(m.prompt + m.completion)}</td>
    <td class="baloo">${m.cache != null ? (m.cache * 100).toFixed(0) + "%" : "—"}</td>
    <td class="baloo">${m.avg_reply != null ? m.avg_reply.toFixed(1) + "s" : "—"}</td></tr>`).join("")
    || `<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:16px">暂无数据</td></tr>`;
  const calls = r.days.map((d) =>
    Object.values(r.day_model[d] || {}).reduce((a, b) => a + b, 0));
  S.lastTrend = { calls, days: r.days };
  drawTrend(calls, r.days);
}
function drawTrend(calls, dayLabels) {
  const svg = $("#trendChart");
  if (!svg || !calls?.length) return;
  const W = 560, H = 200, PAD = 30;
  const max = Math.max(...calls, 1) * 1.25;
  const stepX = (W - PAD * 2) / Math.max(1, calls.length - 1);
  const pt = (v, i) => [PAD + i * stepX, H - PAD - (v / max) * (H - PAD * 2)];
  const pts = calls.map(pt);
  const path = pts.map((p, i) => (i ? "L" : "M") + p[0].toFixed(1) + " " + p[1].toFixed(1)).join(" ");
  const area = `${path} L${pts[pts.length - 1][0]} ${H - PAD} L${pts[0][0]} ${H - PAD} Z`;
  const accent = getComputedStyle(document.documentElement).getPropertyValue("--p1").trim() || "#4FB3FF";
  const glow = getComputedStyle(document.documentElement).getPropertyValue("--p2").trim() || "#7FD4FF";
  const grid = [0.25, 0.5, 0.75, 1].map((f) => {
    const y = H - PAD - f * (H - PAD * 2);
    return `<line x1="${PAD}" y1="${y}" x2="${W - PAD}" y2="${y}" stroke="currentColor" opacity=".07"/>`;
  }).join("");
  const labels = (dayLabels || []).map((d, i) => {
    if (calls.length > 8 && i % 2) return "";
    return `<text x="${pts[i][0]}" y="${H - 9}" text-anchor="middle" font-size="9"
      fill="currentColor" opacity=".45">${esc(d.slice(5))}</text>`;
  }).join("");
  svg.innerHTML = `<g>${grid}</g>
    <path d="${area}" fill="url(#tgrad)" opacity=".35"/>
    <path d="${path}" fill="none" stroke="${accent}" stroke-width="2.6" stroke-linecap="round"/>
    ${pts.map((p, i) => `<circle cx="${p[0]}" cy="${p[1]}" r="${i === pts.length - 1 ? 5 : 3.2}"
        fill="${i === pts.length - 1 ? glow : accent}" stroke="rgba(255,255,255,.85)" stroke-width="1.4"></circle>`).join("")}
    ${labels}
    <defs><linearGradient id="tgrad" x1="0" y1="0" x2="0" y2="1">
      <stop offset="0" stop-color="${accent}" stop-opacity=".55"/>
      <stop offset="1" stop-color="${accent}" stop-opacity="0"/>
    </linearGradient></defs>`;
}
$("#btnBalances").addEventListener("click", async () => {
  $("#balLine").textContent = "正在查询各平台余额…";
  const r = await API.refresh_balances();
  if (!r.ok) $("#balLine").textContent = "查询启动失败：" + (r.error || "");
});
function renderBalances(rows) {
  const el = $("#balLine");
  if (!rows?.length) { el.textContent = "没有配置了 API Key 的 Provider"; return; }
  el.innerHTML = rows.map((r) => {
    if (!r.supported) return `${esc(r.name)}：该平台不支持 key 查询，请到控制台查看`;
    if (r.ok) return `<b>${esc(r.name)}</b>：¥${r.balance.toFixed(2)}`;
    return `${esc(r.name)}：${esc(r.error || "查询失败")}`;
  }).join("　·　");
}
$("#btnUsageClear").addEventListener("click", async () => {
  if (!(await confirmModal("清空用量统计", "将删除全部调用记录（含历史趋势），该操作不可撤销。确定清空？"))) return;
  const r = await API.clear_usage();
  toast(r.ok ? "已清空 ✓" : "清空失败（稍后再试）", r.ok ? "ok" : "err");
  if (r.ok) loadUsage();
});

/* ============================================================
   记忆页
   ============================================================ */
async function loadMemory() {
  const r = await API.memory_overview();
  if (!r.ok) return;
  S.memRows = r.rows || [];
  const box = $("#memChats");
  box.innerHTML = "";
  if (!S.memRows.length) {
    box.innerHTML = `<div class="mem-empty">还没有会话记忆</div>`;
    return;
  }
  for (const row of S.memRows) {
    const b = document.createElement("button");
    b.className = "mem-chat-item" + (row.chat === S.memChat ? " active" : "");
    b.innerHTML = `<b>${esc(row.chat)}</b>
      <small>近期 ${row.recent ?? 0} · 深层 ${row.deep ?? 0} · 重要 ${row.important ?? 0} · 索引 ${row.index ?? 0}${row.card_name ? " · 🎭 " + esc(row.card_name) : ""}</small>`;
    b.addEventListener("click", () => openMemChat(row.chat));
    box.appendChild(b);
  }
}
async function openMemChat(chat) {
  S.memChat = chat;
  S.deepOffset = 0;
  S.deepQuery = "";
  $("#deepQuery").value = "";
  $("#memChatName").textContent = chat;
  ["#btnMemBind", "#btnMemExport", "#btnMemClearChat"].forEach((s) => ($(s).hidden = false));
  const row = S.memRows.find((r) => r.chat === chat);
  const tag = $("#memCardTag");
  if (row?.card_name) { tag.hidden = false; tag.textContent = "🎭 " + row.card_name; }
  else tag.hidden = true;
  $$(".mem-chat-item").forEach((b) =>
    b.classList.toggle("active", b.querySelector("b").textContent === chat));
  await refreshMemDetail();
}
async function refreshMemDetail(append) {
  if (!S.memChat) return;
  const r = await API.memory_detail(S.memChat, append ? S.deepOffset : 0, S.deepQuery);
  if (!r.ok) { toast(r.error || "读取失败", "err"); return; }
  if (S.memMode === "overview") renderMemOverview(r);
  else renderMemDeep(r, append);
}
function memItem(text, time, kind, idx) {
  const div = document.createElement("div");
  div.className = "mem-item";
  div.innerHTML = `<span class="mi-text">${esc(text)}${time ? `<span class="mi-time">${esc(time)}</span>` : ""}</span>` +
    `<button class="mem-del">删除</button>`;
  div.querySelector(".mem-del").addEventListener("click", async () => {
    if (!(await confirmModal("删除该条记忆", "确定删除这一条吗？该操作不可撤销。"))) return;
    const r = await API.memory_delete(kind, S.memChat, idx);
    toast(r.ok ? "已删除" : "删除失败", r.ok ? "ok" : "err");
    if (r.ok) refreshMemDetail();
  });
  return div;
}
function emptyBox(text) {
  const d = document.createElement("div");
  d.className = "mem-empty";
  d.textContent = text;
  return d;
}
function renderMemOverview(d) {
  const imp = $("#memImportant"), rec = $("#memRecent"), idx = $("#memIndex");
  imp.innerHTML = ""; rec.innerHTML = ""; idx.innerHTML = "";
  (d.important || []).forEach((m, i) =>
    imp.appendChild(memItem(m.content || m, "", "important", i + 1)));
  if (!d.important?.length) imp.appendChild(emptyBox("暂无重要记忆"));
  (d.recent || []).forEach((m, i) =>
    rec.appendChild(memItem(`[${m.role === "assistant" ? "小漓" : "用户"}] ${m.content}`,
      m.time, "recent", i + 1)));
  if (!d.recent?.length) rec.appendChild(emptyBox("暂无近期对话"));
  (d.index || []).forEach((m, i) =>
    idx.appendChild(memItem(`${(m.kw || []).join("、")} → ${m.mem}`, "", "index", i + 1)));
  if (!d.index?.length) idx.appendChild(emptyBox("暂无关键词索引"));
}
function renderMemDeep(d, append) {
  const body = $("#memDeepBody");
  if (!append) body.innerHTML = "";
  (d.deep || []).forEach((m) =>
    body.appendChild(memItem(`#${m.line_no} [${m.role === "assistant" ? "小漓" : "用户"}] ${m.content}`,
      m.time, "deep", m.line_no)));
  if (S.deepQuery) {
    $("#deepInfo").textContent = `命中 ${d.deep_matched ?? 0} 条（最多显示 200 条）`;
    $("#btnDeepMore").hidden = true;
  } else {
    $("#deepInfo").textContent =
      `共 ${d.deep_total ?? 0} 条，显示第 ${S.deepOffset + 1}-${S.deepOffset + (d.deep || []).length} 条`;
    $("#btnDeepMore").hidden = (d.deep || []).length < 200;
  }
  if (!body.children.length) body.appendChild(emptyBox("暂无深层存档"));
}
$("#memTabs").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  [...btn.parentElement.children].forEach((b) => b.classList.toggle("active", b === btn));
  S.memMode = btn.dataset.v;
  $("#memOverview").hidden = S.memMode !== "overview";
  $("#memDeep").hidden = S.memMode !== "deep";
  await refreshMemDetail();
});
$("#btnDeepSearch").addEventListener("click", async () => {
  S.deepQuery = $("#deepQuery").value.trim();
  S.deepOffset = 0;
  await refreshMemDetail();
});
$("#btnDeepReset").addEventListener("click", async () => {
  $("#deepQuery").value = "";
  S.deepQuery = "";
  S.deepOffset = 0;
  await refreshMemDetail();
});
$("#btnDeepMore").addEventListener("click", async () => {
  S.deepOffset += 200;
  await refreshMemDetail(true);
});
$("#btnMemExport").addEventListener("click", async () => {
  if (!S.memChat) return;
  const mode = await selectModal("导出记忆", "选择「" + S.memChat + "」的导出格式：",
    [{ value: "md", label: "Markdown（.md）" }, { value: "json", label: "JSON（.json）" }], "md");
  if (mode === null) return;
  const r = await API.memory_export(S.memChat, mode);
  if (!r.ok) { toast(r.error || "导出失败", "err"); return; }
  downloadText(r.filename, r.content,
    mode === "json" ? "application/json" : "text/markdown");
});
$("#btnMemClearChat").addEventListener("click", async () => {
  if (!S.memChat) return;
  if (!(await confirmModal("清空此会话记忆",
    `将删除「${S.memChat}」的全部记忆（含深层存档），该操作不可撤销。确定？`))) return;
  const r = await API.memory_clear_chat(S.memChat);
  toast(r.ok ? "已清空" : "清空失败", r.ok ? "ok" : "err");
  if (r.ok) { await loadMemory(); S.memChat = ""; $("#memChatName").textContent = "选择左侧会话"; }
});
async function clearAllMemory() {
  if (!(await confirmModal("清空全部记忆",
    "将删除所有会话的全部记忆（含深层存档），小漓会彻底失忆，该操作不可撤销。确定？"))) return;
  const r = await API.memory_clear_all();
  toast(r.ok ? "已清空全部记忆" : "清空失败", r.ok ? "ok" : "err");
  if (r.ok) { await loadMemory(); S.memChat = ""; $("#memChatName").textContent = "选择左侧会话"; }
}
$("#btnMemClearAll").addEventListener("click", clearAllMemory);
$("#btnMemClearAll2").addEventListener("click", clearAllMemory);
$("#btnMemBind").addEventListener("click", async () => {
  if (!S.memChat) return;
  const opts = [{ value: "", label: "跟随全局活跃卡（解除绑定）" }]
    .concat(S.cards.map((c) => ({ value: c.id, label: `${c.emoji || ""} ${c.name}`.trim() })));
  const cur = (S.memRows.find((r) => r.chat === S.memChat) || {}).card_name;
  const pick = await selectModal("绑定角色卡", `为「${S.memChat}」选择角色卡：`, opts, "");
  if (pick === null) return;
  const r = await API.memory_bind_card(S.memChat, pick);
  toast(r.ok ? "已更新绑定 ✓" : "绑定失败", r.ok ? "ok" : "err");
  if (r.ok) loadMemory();
});

/* ============================================================
   设置页（记忆 / 目录 / 任务桥 / 联网 / 语音 / 覆盖 / 监视）
   ============================================================ */
function refreshSettings() {
  const m = S.misc || {}, v = S.voice || {}, u = S.ui || {};
  $("#cbDeepMem").checked = !!m.memory_deep_enabled;
  $("#cbCompressMem").checked = !!m.memory_compress_enabled;
  $("#spKeepRecent").value = m.memory_keep_recent ?? 30;
  $("#spCompressBatch").value = m.memory_compress_batch ?? 30;
  $("#spImportantMax").value = m.memory_important_max ?? 20;
  $("#edCompressModel").value = m.memory_compress_model || "";
  $("#edTasks").textContent = m.tasks_dir || "（未配置）";
  $("#edFiles").textContent = m.file_storage_path || "（未配置）";
  $("#edTianshuWorkdir").textContent = m.tianshu_workdir || "（未配置）";
  $("#edFirstPrompt").textContent = m.first_prompt_path || "内置模板";
  $("#cbTaskBridge").checked = m.task_enabled !== false;
  $("#cbWebSearch").checked = m.web_search_enabled !== false;
  $("#edWebProxy").value = m.web_proxy || "";
  $("#cbStateWatch").checked = !!m.state_watch_enabled;
  $("#followSystem").checked = !!u.follow_system;
  $$("#fontSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.v === (u.font_scale || "medium")));
  $$("#voiceSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.v === (v.voice_mode || "off")));
  $("#spVoiceMax").value = v.voice_max_seconds ?? 55;
  $("#spVoiceTimeout").value = v.tts_timeout_seconds ?? 120;
  $("#edVoiceEndpoint").value = v.tts_endpoint || "";
  renderVoiceProfiles();
  reloadOvrChats();
}
$("#btnMemSave").addEventListener("click", async () => {
  const r = await API.save_config({
    memory_deep_enabled: $("#cbDeepMem").checked,
    memory_compress_enabled: $("#cbCompressMem").checked,
    memory_keep_recent: parseInt($("#spKeepRecent").value) || 30,
    memory_compress_batch: parseInt($("#spCompressBatch").value) || 30,
    memory_important_max: parseInt($("#spImportantMax").value) || 20,
    memory_compress_model: $("#edCompressModel").value.trim(),
  });
  toast(r.ok ? "长记忆设置已保存并热生效 ✓" : "保存失败：" + (r.error || ""),
    r.ok ? "ok" : "err");
});
async function pickDir(codeSel) {
  const r = await API.pick_folder();
  if (r.canceled || !r.path) return null;
  $(codeSel).textContent = r.path;
  return r.path;
}
$("#btnPickTasks").addEventListener("click", () => pickDir("#edTasks"));
$("#btnSaveTasks").addEventListener("click", async () => {
  const newPath = $("#edTasks").textContent.trim();
  if (!newPath || newPath.startsWith("（")) { toast("请先浏览选择目录", "err"); return; }
  const oldPath = (S.misc || {}).tasks_dir || "";
  if (newPath === oldPath) { toast("目录未变化", "ok"); return; }
  let removeOld = false;
  if (oldPath) {
    const choice = await selectModal("变更任务工作目录",
      `新目录：${newPath}\n\n旧目录「${oldPath}」的内容如何处理？`,
      [{ value: "1", label: "清空旧目录内容（桌面版默认行为）" },
       { value: "0", label: "保留旧目录，仅切换配置" }], "1");
    if (choice === null) { $("#edTasks").textContent = oldPath; return; }
    removeOld = choice === "1";
  }
  const r = await API.set_tasks_dir(newPath, removeOld);
  if (!r.ok) { toast(r.error || "保存失败", "err"); return; }
  S.misc.tasks_dir = newPath;
  toast(r.removed_old ? "任务目录已变更，旧目录已清空" : "任务目录已变更 ✓", "ok");
});
$("#btnPickFiles").addEventListener("click", () => pickDir("#edFiles"));
$("#btnSaveFiles").addEventListener("click", async () => {
  const p = $("#edFiles").textContent.trim();
  if (!p || p === "（未配置）") { toast("请先浏览选择目录", "err"); return; }
  const r = await API.save_config({ file_storage_path: p });
  toast(r.ok ? "微信接收目录已保存并热生效 ✓" : "保存失败", r.ok ? "ok" : "err");
});
$("#btnPickPrompt").addEventListener("click", async () => {
  const r = await API.pick_file("选择首轮提示词文件",
    ["文本文件 (*.txt;*.md)", "所有文件 (*.*)"]);
  if (r.canceled || !r.path) return;
  const sv = await API.save_config({ first_prompt_path: r.path });
  if (sv.ok) { S.misc.first_prompt_path = r.path; $("#edFirstPrompt").textContent = r.path; toast("首轮提示词文件已设置 ✓", "ok"); }
});
$("#btnClearPrompt").addEventListener("click", async () => {
  const sv = await API.save_config({ first_prompt_path: "" });
  if (sv.ok) { S.misc.first_prompt_path = ""; $("#edFirstPrompt").textContent = "内置模板"; toast("已恢复内置模板", "ok"); }
});
$("#btnOpenTasks").addEventListener("click", () => {
  const p = (S.misc || {}).tasks_dir;
  if (p) API.open_external("file:///" + p.replace(/\\/g, "/"));
});
$("#cbTaskBridge").addEventListener("change", async (e) => {
  const r = await API.save_config({ task_enabled: e.target.checked });
  toast(r.ok ? (e.target.checked ? "任务桥已开启（热生效）" : "任务桥已关闭（模型看不到任务工具）")
    : "保存失败", r.ok ? "ok" : "err");
});
$("#cbWebSearch").addEventListener("change", async (e) => {
  const r = await API.save_config({ web_search_enabled: e.target.checked });
  toast(r.ok ? (e.target.checked ? "联网搜索已开启" : "联网搜索已关闭") : "保存失败",
    r.ok ? "ok" : "err");
});
$("#btnSaveProxy").addEventListener("click", async () => {
  const r = await API.save_config({ web_proxy: $("#edWebProxy").value.trim() });
  toast(r.ok ? "代理已保存并即时生效 ✓" : "保存失败", r.ok ? "ok" : "err");
});
$("#cbStateWatch").addEventListener("change", async (e) => {
  const r = await API.save_config({ state_watch_enabled: e.target.checked });
  toast(r.ok ? (e.target.checked ? "状态监视已开启" : "状态监视已关闭") : "保存失败",
    r.ok ? "ok" : "err");
});
/* ---- 语音 ---- */
$("#voiceSeg").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  [...btn.parentElement.children].forEach((b) => b.classList.toggle("active", b === btn));
});
function curVoiceProfile() {
  const v = S.voice || {};
  return (v.voice_profiles || []).find(
    (p) => p.id === (v.active_edit_id || v.active_voice_profile_id))
    || (v.voice_profiles || [])[0] || null;
}
function renderVoiceProfiles() {
  const v = S.voice || {};
  const cb = $("#cbVoiceProfile");
  cb.innerHTML = (v.voice_profiles || []).map((p) =>
    `<option value="${esc(p.id)}">${esc(p.name || "未命名档案")}</option>`).join("");
  if (!v.voice_profiles?.length) {
    cb.innerHTML = `<option value="">（还没有档案）</option>`;
    $("#edVoiceName").value = "";
    renderVoiceRefs(null);
    return;
  }
  const cur = curVoiceProfile();
  cb.value = cur.id;
  v.active_edit_id = cur.id;
  $("#edVoiceName").value = cur.name || "";
  $("#cbVoiceActive").checked = v.active_voice_profile_id === cur.id;
  renderVoiceRefs(cur);
}
function renderVoiceRefs(profile) {
  const tb = $("#voiceRefsTbody");
  tb.innerHTML = "";
  if (!profile) {
    tb.innerHTML = `<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:14px">新建或选择一个音色档案后编辑情绪参考</td></tr>`;
    return;
  }
  const refs = profile.refs || {};
  const entries = Object.entries(refs);
  if (!entries.length) {
    tb.innerHTML = `<tr><td colspan="4" style="color:var(--muted);text-align:center;padding:14px">暂无情绪参考——点下方「添加情绪参考」</td></tr>`;
    return;
  }
  for (const [emo, ref] of entries) {
    const fixed = emo === "通用";
    const tr = document.createElement("tr");
    tr.innerHTML = `<td><input data-rf="emo" value="${esc(emo)}" ${fixed ? "disabled" : ""}></td>
      <td><input data-rf="audio" value="${esc(ref.ref_audio_path || "")}" placeholder="TTS 服务器上的音频路径"></td>
      <td><input data-rf="text" value="${esc(ref.prompt_text || "")}" placeholder="该音频对应的文本稿"></td>
      <td><button class="row-del" ${fixed ? "disabled title='通用行不可删除'" : ""}><svg class="ic"><use href="#i-trash"/></svg></button></td>`;
    if (!fixed) tr.querySelector(".row-del").addEventListener("click", () => {
      delete profile.refs[emo];
      renderVoiceRefs(profile);
    });
    tb.appendChild(tr);
  }
}
$("#cbVoiceProfile").addEventListener("change", (e) => {
  const v = S.voice;
  v.active_edit_id = e.target.value;
  renderVoiceProfiles();
});
$("#btnVoiceNew").addEventListener("click", () => {
  const v = S.voice;
  if (!v.voice_profiles) v.voice_profiles = [];
  const id = "vp-" + Math.random().toString(16).slice(2, 8);
  v.voice_profiles.push({ id, name: "新档案", refs: { "通用": { ref_audio_path: "", prompt_text: "" } } });
  v.active_edit_id = id;
  renderVoiceProfiles();
});
$("#btnVoiceDel").addEventListener("click", async () => {
  const v = S.voice;
  const cur = curVoiceProfile();
  if (!cur) return;
  if (!(await confirmModal("删除音色档案", `确定删除档案「${cur.name}」？`))) return;
  v.voice_profiles = v.voice_profiles.filter((p) => p.id !== cur.id);
  if (v.active_voice_profile_id === cur.id) v.active_voice_profile_id = "";
  renderVoiceProfiles();
});
$("#cbVoiceActive").addEventListener("change", (e) => {
  const cur = curVoiceProfile();
  if (cur) S.voice.active_voice_profile_id = e.target.checked ? cur.id : "";
});
$("#btnVoiceRefAdd").addEventListener("click", () => {
  const cur = curVoiceProfile();
  if (!cur) { toast("先新建音色档案", "err"); return; }
  cur.refs = cur.refs || {};
  let name = "开心", n = 1;
  while (cur.refs[name]) name = "开心" + (++n);
  cur.refs[name] = { ref_audio_path: "", prompt_text: "" };
  renderVoiceRefs(cur);
});
$("#btnVoiceSave").addEventListener("click", async () => {
  const v = S.voice;
  const cur = curVoiceProfile();
  if (cur) {
    cur.name = $("#edVoiceName").value.trim() || cur.name;
    const newRefs = {};
    $$("#voiceRefsTbody tr").forEach((tr) => {
      const emo = $("input[data-rf='emo']", tr);
      if (!emo || emo.disabled) {
        if (emo && emo.disabled && cur.refs["通用"]) newRefs["通用"] = cur.refs["通用"];
        return;
      }
      newRefs[emo.value.trim() || "未命名"] = {
        ref_audio_path: $("input[data-rf='audio']", tr).value.trim(),
        prompt_text: $("input[data-rf='text']", tr).value.trim(),
      };
    });
    cur.refs = newRefs;
  }
  const r = await API.save_config({
    voice_mode: $("#voiceSeg button.active")?.dataset.v || "off",
    tts_endpoint: $("#edVoiceEndpoint").value.trim(),
    tts_timeout_seconds: parseInt($("#spVoiceTimeout").value) || 120,
    voice_max_seconds: parseInt($("#spVoiceMax").value) || 55,
    voice_profiles: v.voice_profiles || [],
    active_voice_profile_id: v.active_voice_profile_id || "",
  });
  toast(r.ok ? "语音设置已保存并热生效 ✓" : "保存失败：" + (r.error || ""),
    r.ok ? "ok" : "err");
  if (r.ok) renderVoiceProfiles();
});
/* ---- 按聊天覆盖 ---- */
async function reloadOvrChats() {
  const sel = $("#cbOvrChat");
  const r = await API.memory_overview();
  const chats = new Set((r.rows || []).map((x) => x.chat));
  Object.keys(S.overrides || {}).forEach((k) => chats.add(k));
  const list = [...chats].sort();
  sel.innerHTML = list.map((c) => `<option value="${esc(c)}">${esc(c)}</option>`).join("")
    || `<option value="">（还没有会话）</option>`;
  fillOvr();
}
function fillOvr() {
  const chat = $("#cbOvrChat").value;
  const ov = (S.overrides || {})[chat] || {};
  $$("#ovrCombos select").forEach((sel) => {
    const val = ov[sel.dataset.feature];
    sel.value = val === true ? "on" : val === false ? "off" : "follow";
  });
}
$("#cbOvrChat").addEventListener("change", fillOvr);
$("#btnOvrRefresh").addEventListener("click", reloadOvrChats);
$("#btnOvrSave").addEventListener("click", async () => {
  const chat = $("#cbOvrChat").value;
  if (!chat) { toast("还没有会话可配置", "err"); return; }
  const patch = {};
  $$("#ovrCombos select").forEach((sel) => {
    if (sel.value !== "follow") patch[sel.dataset.feature] = sel.value === "on";
  });
  S.overrides = { ...(S.overrides || {}) };
  if (Object.keys(patch).length) S.overrides[chat] = patch;
  else delete S.overrides[chat];
  const r = await API.save_config({ chat_feature_overrides: S.overrides });
  toast(r.ok ? `「${chat}」的功能覆盖已保存并热生效 ✓` : "保存失败", r.ok ? "ok" : "err");
});
$("#btnOvrClear").addEventListener("click", async () => {
  const chat = $("#cbOvrChat").value;
  if (!chat) return;
  S.overrides = { ...(S.overrides || {}) };
  delete S.overrides[chat];
  const r = await API.save_config({ chat_feature_overrides: S.overrides });
  if (r.ok) { fillOvr(); toast(`已清除「${chat}」的全部例外（恢复跟随全局）`, "ok"); }
  else toast("保存失败", "err");
});

/* ============================================================
   首启引导
   ============================================================ */
async function showFirstRun() {
  const r = await API.first_run_defaults();
  if (!r.ok) return;
  $("#frTasks").textContent = r.tasks_dir || "";
  $("#frFiles").textContent = r.file_storage_path || "";
  $("#frMemory").textContent = r.memory_file || "memory.json";
  $("#frNickname").value = r.bot_nickname || "小漓";
  openModal("modalFirstRun");
}
$("#frPickTasks").addEventListener("click", () => pickDir("#frTasks"));
$("#frPickFiles").addEventListener("click", () => pickDir("#frFiles"));
$("#frSave").addEventListener("click", async () => {
  const r = await API.save_first_run(
    $("#frTasks").textContent.trim(),
    $("#frFiles").textContent.trim(),
    $("#frMemory").textContent.trim(),
    $("#frNickname").value.trim());
  if (!r.ok) { toast(r.error || "保存失败", "err"); return; }
  closeModal();
  toast("欢迎，配置已就绪 ✓ 到「模型」页填入 API Key 即可开始", "ok");
  const cfg = await API.get_config();
  if (cfg.ok) { S.ui = cfg.ui; S.misc = cfg.misc; refreshSettings(); }
});

/* ============================================================
   后端推送接线
   ============================================================ */
onPush((evt, p) => {
  if (evt === "tick") {
    if (p.state) applyEngineState(p.state, p.paused, p.error);
    if (p.bus_error) toast("引擎错误：" + String(p.bus_error).slice(0, 90), "err");
    if (p.log?.length) appendLogLines(p.log);
  } else if (evt === "balances") {
    renderBalances(p.rows);
  } else if (evt === "install") {
    onInstallProgress(p);
  } else if (evt === "prompt") {
    toast(p.message || (p.ok ? "已发送" : "发送失败"), p.ok ? "ok" : "err");
  }
});

/* ============================================================
   装饰（气泡 / 星野） + 启动
   ============================================================ */
(function makeBubbles() {
  const field = $("#bubbleField");
  for (let i = 0; i < 16; i++) {
    const b = document.createElement("span");
    const size = 4 + Math.random() * 16;
    b.className = "bubble";
    b.style.width = b.style.height = size + "px";
    b.style.left = Math.random() * 100 + "%";
    b.style.animationDuration = 11 + Math.random() * 16 + "s";
    b.style.animationDelay = -Math.random() * 20 + "s";
    b.style.setProperty("--bo", (0.25 + Math.random() * 0.4).toFixed(2));
    field.appendChild(b);
  }
})();
(function makeStars() {
  const field = $("#starField");
  if (!field) return;
  for (let i = 0; i < 64; i++) {
    const s = document.createElement("span");
    const size = 1 + Math.random() * 2.2;
    s.className = "star";
    s.style.width = s.style.height = size + "px";
    s.style.left = Math.random() * 100 + "%";
    s.style.top = Math.random() * 68 + "%";
    s.style.setProperty("--td", (2 + Math.random() * 4).toFixed(1) + "s");
    s.style.setProperty("--to", (0.4 + Math.random() * 0.55).toFixed(2));
    s.style.animationDelay = -Math.random() * 5 + "s";
    field.appendChild(s);
  }
})();
/* 吉祥物语料轮播 */
const BUBBLES = [
  "我在听哦，随时喊我～",
  "任务交给天枢啦，做完叫你 (๑•̀ㅁ•́๑)",
  "今天也想被夸是大肥鱼吗？",
  "红圈亮了！有人找你～",
];
let bubbleIdx = 0;
setInterval(() => {
  const el = $("#mascotBubble");
  el.style.opacity = 0;
  setTimeout(() => {
    bubbleIdx = (bubbleIdx + 1) % BUBBLES.length;
    el.textContent = BUBBLES[bubbleIdx];
    el.style.opacity = 1;
  }, 320);
}, 5200);

/* 模态遮罩点击关闭 + data-close 按钮 */
$("#modalMask").addEventListener("click", (e) => {
  if (e.target === $("#modalMask")) closeModal();
});
$$("[data-close]").forEach((b) => b.addEventListener("click", closeModal));

/* 换主题：重绘折线图 + 指示器重算 */
new MutationObserver(() => {
  if (S.lastTrend) drawTrend(S.lastTrend.calls, S.lastTrend.days);
  requestAnimationFrame(() =>
    moveIndicator(navBtns.find((b) => b.dataset.page === curKey) || navBtns[0]));
}).observe(document.documentElement, { attributes: true, attributeFilter: ["data-theme"] });
requestAnimationFrame(() =>
  moveIndicator(navBtns.find((b) => b.classList.contains("active")) || navBtns[0]));

/* ---------- 启动 ---------- */
(async function init() {
  // pywebview 的 js_api 晚于页面加载注入：必须等桥就绪再拉数据；
  // 等待失败 = 浏览器直开或桥异常 → 降级演示模式（醒目横幅 + 虚构数据）
  IS_REAL = await bridgeReady;
  if (!IS_REAL) enableMock();

  const cfg = await API.get_config();
  if (!cfg.ok) { toast("配置加载失败：" + (cfg.error || ""), "err"); return; }
  S.ui = cfg.ui;
  S.misc = cfg.misc;
  S.voice = cfg.voice;
  S.overrides = cfg.overrides || {};
  applyStartupAppearance();
  applyFollowSystem();
  const st = await API.get_status();
  if (st.ok) {
    applyEngineState(st.state, st.paused, st.error);
    $("#verBadge").textContent = "v" + st.version;
    $("#statPaused").textContent = (S.misc.start_paused ? "启动即暂停（点主按钮开始）" : "启动即运行");
  }
  await Promise.all([
    checkEnv(), checkUpdate(true), refreshTriggerBadge(),
    applyWallpaperLayer(S.ui.wallpaper_path || ""), loadWallpaperGrid(),
  ]);
  refreshSettings();
  setInterval(refreshTriggerBadge, 8000);
  if (cfg.first_run_needed) showFirstRun();
})();
