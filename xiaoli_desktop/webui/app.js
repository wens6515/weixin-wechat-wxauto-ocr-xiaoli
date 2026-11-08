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
  trigRows: [], engineState: "loading", lastTrend: null, docsUrl: "",
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
  if (confirmResolve) {
    /* 兜底关窗：带下拉/输入框的弹窗取消语义是 null（调用方以 === null 判
       取消），普通确认弹窗维持 false。历史缺陷：一律 false → selectModal
       的「取消」被调用方当成有效选项继续执行 */
    confirmResolve(($("#confirmSelect") || $("#confirmInput")) ? null : false);
    confirmResolve = null;
    confirmInputEl = null;
  }
}

let confirmResolve = null;
let confirmInputEl = null;
/* 带文本输入的确认弹窗（记忆注入 / 编辑）：确定 = 输入内容（trim），
   取消 / 兜底关窗 = null；确定时空串照常返回（由调用方自行校验）。 */
function inputModal(title, label, initial = "", placeholder = "") {
  $("#confirmTitle").textContent = title;
  const ct = $("#confirmText");
  ct.textContent = "";
  const lab = document.createElement("div");
  lab.textContent = label;
  lab.style.margin = "2px 0 6px";
  const ta = document.createElement("textarea");
  ta.id = "confirmInput";
  ta.value = initial;
  ta.placeholder = placeholder;
  ct.appendChild(lab);
  ct.appendChild(ta);
  confirmInputEl = ta;
  openModal("modalConfirm");
  setTimeout(() => ta.focus(), 60);
  return new Promise((res) => (confirmResolve = res));
}
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
   兜底 resolve，顺序反了会把「确定」吞成「取消」——历史缺陷）。
   返回值：带下拉（selectModal）时确定 = 选中项 value、取消 = null；
   无下拉时维持 true / false。历史缺陷：确定写死 true → 绑定角色卡存下
   字符串 "True"（投影找不到该卡、绑定永不生效）、导出格式/目录处理选项
   全部失效、取消还会继续执行。 */
function settleConfirm(value) {
  const r = confirmResolve;
  confirmResolve = null;
  confirmInputEl = null;
  closeModal();
  r?.(value);
}
$("#confirmYes").addEventListener("click",
  () => settleConfirm(confirmSelectValue()
    ?? (confirmInputEl ? confirmInputEl.value.trim() : true)));
$("#confirmNo").addEventListener("click",
  () => settleConfirm(($("#confirmSelect") || $("#confirmInput")) ? null : false));

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
  logs: () => initLogPage(), tasks: () => loadTasks(),
  persona: () => loadPersona(),
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
  const backTop = $("#btnBackTop");
  if (backTop && key !== "settings") backTop.hidden = true;
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
/* ---- 设置页回到顶部：导航行滚出视口（看不见）后右下角浮出圆形按钮 ---- */
$("#content").addEventListener("scroll", () => {
  const btn = $("#btnBackTop");
  if (!btn) return;
  const row = $("#anchorRow");
  btn.hidden = !(curKey === "settings" && row &&
    row.getBoundingClientRect().bottom < 0);
}, { passive: true });
$("#btnBackTop").addEventListener("click", () => {
  $("#content").scrollTo({ top: 0, behavior: "smooth" });
  $$("#anchorRow .anchor").forEach((b, i) =>
    b.classList.toggle("active", i === 0));
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
  // 启动应用保存的主题（历史缺陷：这里从未设置 data-theme——HTML 默认
  // abyss，换任何主题重启都渲染回深海小漓；配置本身一直存得是对的）。
  // follow_system 开启时紧随其后的 applyFollowSystem 会按系统深浅色覆盖。
  const theme = u.theme || "abyss";
  document.documentElement.dataset.theme = theme;
  markThemeThumb(theme);
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

/* ---- 初始化失败弹窗：原因 + 现场检测 + 修复指引 ---- */
async function showEngineError() {
  const r = await API.engine_error_detail();
  $("#engineErrReason").textContent =
    (r.ok && r.error) ? r.error : "未知原因（详见日志页）";
  const live = [];
  if (r.scale) live.push(`显示缩放 ${r.scale}%（已按档位自适应）`);
  if (r.wechat_found != null)
    live.push(r.wechat_found ? "微信窗口已检测到" : "微信窗口未找到");
  $("#engineErrLive").textContent = live.length ? "当前检测：" + live.join(" · ") : "";
  const ul = $("#engineErrHints");
  ul.innerHTML = "";
  (r.hints || ["确认微信已打开并登录后点「重试初始化」"]).forEach((h) => {
    const li = document.createElement("li");
    li.textContent = h;
    ul.appendChild(li);
  });
  openModal("modalEngineError");
}
$("#btnEngErrRetry").addEventListener("click", async () => {
  closeModal();
  await API.engine_action("initialize");
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
  envCard("#envScale", "#envScaleDetail", rp.scale);
  /* 天枢 CLI / 首轮提示词只在任务桥开启时才有意义——没开就不占位，
     首页恒定横排（2 张或 4 张，等分占满同一行） */
  const taskOn = !!(S.misc || {}).task_enabled;
  $("#envTianshu").hidden = !taskOn;
  $("#envPrompt").hidden = !taskOn;
  if (taskOn) {
    $("#btnInstallTianshu").hidden = rp.tianshu.ok !== false;
    envCard("#envTianshu", "#envTianshuDetail", rp.tianshu);
    envCard("#envPrompt", "#envPromptDetail", rp.first_prompt);
  }
  S.envWechat = rp.wechat;
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
  const ok = await confirmModal("安装天枢 CLI",
    "将运行 npm install -g tianshu-tui 安装天枢 CLI（需已安装 Node.js），"
    + "首次约 1-3 分钟，进度显示在卡片上。继续？");
  if (!ok) return;
  const r = await API.install_tianshu();
  if (!r.ok) toast(r.error || "安装任务启动失败", "err");
});
function onInstallProgress(p) {
  const bars = [$("#installBar"), $("#tgInstallBar")].filter(Boolean);
  if (p.done) {
    bars.forEach((b) => (b.hidden = true));
    toast(p.ok ? "天枢 CLI 安装完成 ✓" : "安装失败：" + (p.error || "未知错误"),
      p.ok ? "ok" : "err");
    if (p.ok) {
      checkEnv();
      // 引导弹窗开着时刷新状态（安装完应能点「打开天枢 CLI」了）
      if (!$("#modalTianshuGuide").hidden) openTianshuGuide();
    }
  } else {
    if (p.text) toast(p.text, "ok");   // CLI 安装无百分比：阶段文案告知别干等
    bars.forEach((b) => {
      b.hidden = false;
      b.querySelector("span").style.width = (p.pct || 0) + "%";
    });
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
    $("#btnInstallUpdate").hidden = false;
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
/* ---- 一键更新：下载（进度推送）→ 校验 → 拉起安装器 → 自动退出 ---- */
$("#btnInstallUpdate").addEventListener("click", async () => {
  const r = await withBusy($("#btnInstallUpdate"),
    () => API.download_and_install_update());
  if (!r.ok) { toast(r.error || "无法开始更新", "err"); return; }
  toast(`v${r.version || ""} 开始下载，完成后自动启动安装器（届时小漓会退出）`,
    "ok");
});
onPush((evt, p) => {
  if (evt !== "update_progress") return;
  if (p.stage === "download" && typeof p.pct === "number") {
    $("#verSub").textContent = `新版本下载中 ${p.pct}%`;
    return;
  }
  if (p.message) {
    toast(p.message, p.ok === false ? "err" : "ok");
    if (p.ok === false) {
      $("#verSub").textContent = "更新失败，可点「前往下载」手动安装";
    }
  }
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
  /* 首页小窗：追加在底、自动滚到最新（旧语义）；
     日志页：**最新在顶**——按自然序逐条 prepend（处理完最新恰在第一行），
     超限从尾部删最旧，不自动滚动（用户在读顶部）。 */
  for (const sel of ["#logBody", "#logPageBody"]) {
    const body = $(sel);
    if (!body) continue;
    const newestTop = sel === "#logPageBody";
    const limit = sel === "#logBody" ? 60 : (cap || 800);
    for (const line of lines) {
      const div = document.createElement("div");
      div.className = "log-line";
      const lv = logLevelOf(line);
      div.innerHTML = `<span class="lv ${lv}">${lv.toUpperCase()}</span><span></span>`;
      div.lastChild.textContent = line;
      if (newestTop) body.prepend(div);
      else body.appendChild(div);
    }
    while (body.children.length > limit)
      body.removeChild(newestTop ? body.lastChild : body.firstChild);
    if (!newestTop) body.scrollTop = body.scrollHeight;
  }
}
$("#btnLogClear").addEventListener("click", () => { $("#logBody").innerHTML = ""; });
$("#btnPageLogClear").addEventListener("click", () => { $("#logPageBody").innerHTML = ""; });
/* ---- 导出诊断包（环境快照 + 脱敏配置 + 日志尾部 + 微信窗口截图） ---- */
$("#btnExportDiag").addEventListener("click", async () => {
  const r = await withBusy($("#btnExportDiag"), () => API.export_diagnostics());
  if (!r.ok) { toast(r.error || "导出失败", "err"); return; }
  toast(`诊断包已导出：${r.path}`, "ok");
  API.reveal_path(r.path);   // 远程模式该方法被服务端拒绝，仅本地打开文件夹
});
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
    models: ["deepseek:deepseek-flash", "deepseek:deepseek-v4-pro"] },
  { id: "zhipu", name: "智谱 GLM", base_url: "https://open.bigmodel.cn/api/paas/v4/chat/completions",
    models: ["zhipu:glm-5.3", "zhipu:glm-5.3-flash"] },
  { id: "qwen", name: "通义千问", base_url: "https://dashscope.aliyuncs.com/compatible-mode/v1/chat/completions",
    models: ["qwen:qwen3.8-max", "qwen:qwen3.8-flash"] },
  { id: "kimi", name: "月之暗面 Kimi", base_url: "https://api.moonshot.cn/v1/chat/completions",
    models: ["kimi:kimi-k3.1-pro", "kimi:kimi-k3"] },
  { id: "doubao", name: "豆包（火山引擎）", base_url: "https://ark.cn-beijing.volces.com/api/v3/chat/completions",
    models: ["doubao:doubao-seed-2.1-pro", "doubao:doubao-seed-2.1-turbo"] },
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
  $("#edCtxBudget").value = (S.misc || {}).max_context_tokens ?? 100000;
  $("#edReplyTokens").value = (S.misc || {}).reply_max_tokens ?? 40000;
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
  const budget = parseInt($("#edCtxBudget").value) || 100000;
  const replyCap = parseInt($("#edReplyTokens").value) || 40000;
  const cr = await API.save_config({
    max_context_tokens: budget,
    reply_max_tokens: replyCap,
  });
  if (cr.ok && S.misc) {
    S.misc.max_context_tokens = budget;
    S.misc.reply_max_tokens = replyCap;
  }
  toast(cr.ok ? "已保存并应用 ✓" : "模型已保存，上下文预算保存失败", cr.ok ? "ok" : "err");
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
  const callsSub = $("#uTodayCallsSub");
  if (callsSub) {
    callsSub.textContent = (c.today_ok != null && c.today_fail != null)
      ? `成功 ${c.today_ok} · 失败 ${c.today_fail}` : "全部模型";
  }
  $("#uTodayCost").textContent = c.today_cost != null ? c.today_cost.toFixed(2) : "—";
  $("#uTodayTokens").textContent = fmtTokens(c.today_tokens);
  $("#uTodayCache").textContent = c.today_cache != null ? (c.today_cache * 100).toFixed(0) + "%" : "—";
  $("#uTokens30").textContent = fmtTokens(c.tokens_30d);
  $("#uCost30").textContent = c.cost_30d != null ? `≈ ¥${c.cost_30d.toFixed(2)}（估算）` : "";
  $("#uFail30").textContent = c.fail_30d ?? "—";
  const tb = $("#modelTbody");
  tb.innerHTML = (r.by_model || []).map((m) => `<tr>
    <td>${esc(m.model)}</td><td class="baloo" title="成功 ${m.ok ?? "—"} · 失败 ${m.fail ?? "—"}">${m.calls}</td>
    <td class="baloo">${fmtTokens(m.prompt + m.completion)}${m.est ? ` <span title="${m.est} 条为本地估算（响应未带 usage）">≈</span>` : ""}</td>
    <td class="baloo">${m.cache != null ? (m.cache * 100).toFixed(0) + "%" : "—"}</td>
    <td class="baloo">${m.avg_reply != null ? m.avg_reply.toFixed(1) + "s" : "—"}</td></tr>`).join("")
    || `<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:16px">暂无数据</td></tr>`;
  const calls = r.days.map((d) =>
    Object.values(r.day_model[d] || {}).reduce((a, b) => a + b, 0));
  S.lastTrend = { calls, days: r.days };
  drawTrend(calls, r.days);
  /* 每日预算进度条（开关开启且上限>0 才显示；估算口径与预算闸一致） */
  const bud = r.budget || {};
  const card = $("#budgetCard");
  if (card) {
    if (bud.enabled && bud.limit > 0) {
      card.hidden = false;
      const pct = Math.min(100, ((bud.today || 0) / bud.limit) * 100);
      $("#budgetBar").style.width = pct.toFixed(1) + "%";
      $("#budgetText").textContent =
        `今日估算 ¥${(bud.today || 0).toFixed(2)} / 上限 ¥${bud.limit.toFixed(2)}` +
        (pct >= 100 ? " —— 已达上限，今天不再调用模型（明天自动恢复）" : "");
    } else {
      card.hidden = true;
    }
  }
}
async function loadTasks() {
  const r = await API.list_tasks();
  if (!r.ok) return;
  $("#taskWaiting").textContent = r.waiting ?? 0;
  $("#taskDone").textContent = r.done ?? 0;
  $("#taskArchived").textContent = r.archived ?? 0;
  $("#taskDirHint").textContent = r.tasks_dir || "（任务目录未配置）";
  const badge = { waiting: ["⏳ 天枢处理中", ""], done: ["✅ 已完成待回传", "ok"],
                  archived: ["📦 已归档", ""] };
  const tb = $("#taskTbody");
  tb.innerHTML = (r.rows || []).map((t) => {
    const [label, cls] = badge[t.state] || [t.state, ""];
    return `<tr>
      <td class="baloo">${esc(t.name)}</td>
      <td><span class="test-result ${cls}">${label}</span></td>
      <td>${esc(t.desc || "—")}</td>
      <td>${esc(t.mtime || "—")}</td>
      <td><button class="row-del" data-task="${esc(t.name)}" title="删除该任务目录"><svg class="ic"><use href="#i-trash"/></svg></button></td>
    </tr>`;
  }).join("")
    || `<tr><td colspan="5" style="color:var(--muted);text-align:center;padding:16px">还没有任务</td></tr>`;
  $$("#taskTbody .row-del").forEach((btn) => btn.addEventListener("click", async () => {
    const name = btn.dataset.task;
    if (!(await confirmModal("删除任务",
        `确定删除任务「${name}」及其全部文件（含附件/成果/归档记录）？`))) return;
    const dr = await API.delete_task(name);
    toast(dr.ok ? "任务已删除 ✓" : "删除失败：" + (dr.error || ""), dr.ok ? "ok" : "err");
    if (dr.ok) loadTasks();
  }));
}
$("#btnTaskRefresh").addEventListener("click", loadTasks);

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
  /* 全局记忆入口（固定置顶，独立于会话列表——全局记忆不属于任何聊天） */
  const g = document.createElement("button");
  g.className = "mem-chat-item global" + (S.memChat === "__global__" ? " active" : "");
  g.dataset.global = "1";
  g.innerHTML = `<b>🌐 全局记忆</b><small>跨聊天常驻 · 重要记忆与关键词索引</small>`;
  g.addEventListener("click", openGlobalMem);
  box.appendChild(g);
  if (!S.memRows.length) {
    box.insertAdjacentHTML("beforeend", `<div class="mem-empty">还没有会话记忆</div>`);
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
  ["#btnMemInject", "#btnMemBind", "#btnMemExport", "#btnMemClearChat"].forEach((s) => ($(s).hidden = false));
  $("#memTabs").children[1].hidden = false;  // 全局模式临时藏起的深层页签，普通会话恢复
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
  if (S.memChat === "__global__") return refreshGlobalMem();
  const r = await API.memory_detail(S.memChat, append ? S.deepOffset : 0, S.deepQuery);
  if (!r.ok) { toast(r.error || "读取失败", "err"); return; }
  if (S.memMode === "overview") renderMemOverview(r);
  else renderMemDeep(r, append);
}
function memItem(text, time, kind, idx, raw) {
  /* raw：编辑初值（important/recent/deep 为正文原文；index 为 {kw, mem}；
     g_important/g_index 为全局记忆条目）。全部类别都支持编辑与删除。 */
  const div = document.createElement("div");
  div.className = "mem-item";
  div.innerHTML = `<span class="mi-text">${esc(text)}${time ? `<span class="mi-time">${esc(time)}</span>` : ""}</span>` +
    `<span class="mi-actions"><button class="mem-edit">编辑</button><button class="mem-del">删除</button></span>`;
  div.querySelector(".mem-edit").addEventListener("click", () => editMemItem(kind, idx, raw));
  div.querySelector(".mem-del").addEventListener("click", async () => {
    if (!(await confirmModal("删除该条记忆", "确定删除这一条吗？该操作不可撤销。"))) return;
    let r;
    if (kind === "g_important") r = await API.memory_global_delete("important", idx);
    else if (kind === "g_index") r = await API.memory_global_delete("index", idx);
    else r = await API.memory_delete(kind, S.memChat, idx);
    toast(r.ok ? "已删除" : "删除失败", r.ok ? "ok" : "err");
    if (r.ok) (S.memChat === "__global__") ? refreshGlobalMem() : refreshMemDetail();
  });
  return div;
}
/* 编辑一条记忆：按类别走对应弹窗与 API；index/全局索引先改内容再改关键词 */
async function editMemItem(kind, idx, raw) {
  let r;
  if (kind === "g_important") {
    const v = await inputModal("编辑全局记忆", "内容：", (raw && raw.content) || "");
    if (v === null || !v) return;
    r = await API.memory_global_edit("important", idx, v);
  } else if (kind === "g_index") {
    const mem = await inputModal("编辑全局索引", "记忆内容：", (raw && raw.mem) || "");
    if (mem === null || !mem) return;
    const kw = await inputModal("编辑全局索引", "关键词（逗号 / 顿号分隔，命中即注入）：", ((raw && raw.kw) || []).join("、"));
    if (kw === null || !kw) return;
    r = await API.memory_global_edit("index", idx, "", kw, mem);
  } else if (kind === "index") {
    const mem = await inputModal("编辑关键词索引", "记忆内容：", (raw && raw.mem) || "");
    if (mem === null || !mem) return;
    const kw = await inputModal("编辑关键词索引", "关键词（逗号 / 顿号分隔，命中即注入）：", ((raw && raw.kw) || []).join("、"));
    if (kw === null || !kw) return;
    r = await API.memory_edit(S.memChat, "index", idx, "", kw, mem);
  } else {
    const v = await inputModal("编辑记忆", "内容：", typeof raw === "string" ? raw : ((raw && raw.content) || ""));
    if (v === null || !v) return;
    r = await API.memory_edit(S.memChat, kind, idx, v);
  }
  toast(r.ok ? "已保存 ✓" : "保存失败", r.ok ? "ok" : "err");
  if (r.ok) (S.memChat === "__global__") ? refreshGlobalMem() : refreshMemDetail();
}
/* ---- 全局记忆视图（固定入口，不走 memory_detail） ---- */
async function openGlobalMem() {
  S.memChat = "__global__";
  $("#memChatName").textContent = "🌐 全局记忆";
  $("#memCardTag").hidden = true;
  $("#btnMemInject").hidden = false;
  ["#btnMemBind", "#btnMemExport", "#btnMemClearChat"].forEach((s) => ($(s).hidden = true));
  $$(".mem-chat-item").forEach((b) =>
    b.classList.toggle("active", b.dataset.global === "1"));
  $("#memTabs").children[1].hidden = true;   // 全局没有深层存档页签
  if (S.memMode === "deep") {
    S.memMode = "overview";
    $("#memTabs").children[0].classList.add("active");
    $("#memTabs").children[1].classList.remove("active");
    $("#memOverview").hidden = false;
    $("#memDeep").hidden = true;
  }
  await refreshGlobalMem();
}
async function refreshGlobalMem() {
  const r = await API.memory_global_detail();
  if (!r.ok) { toast(r.error || "读取失败", "err"); return; }
  const imp = $("#memImportant"), rec = $("#memRecent"), idx = $("#memIndex");
  imp.innerHTML = ""; rec.innerHTML = ""; idx.innerHTML = "";
  (r.important || []).forEach((m, i) =>
    imp.appendChild(memItem(m.content || "", m.time || "", "g_important", i + 1, m)));
  if (!r.important || !r.important.length)
    imp.appendChild(emptyBox("暂无全局重要记忆（点右上「注入记忆」添加）"));
  (r.index || []).forEach((m, i) =>
    idx.appendChild(memItem(`${(m.kw || []).join("、")} → ${m.mem}`, "", "g_index", i + 1, m)));
  if (!r.index || !r.index.length) idx.appendChild(emptyBox("暂无全局关键词索引"));
  rec.appendChild(emptyBox("全局记忆只有重要记忆与索引，没有对话窗口"));
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
    imp.appendChild(memItem(m.content || m, m.time || "", "important", i + 1, m.content || m)));
  if (!d.important?.length) imp.appendChild(emptyBox("暂无重要记忆（点右上「注入记忆」添加）"));
  /* 近期对话**最新在顶**（用户定案）：倒序渲染，idx 仍按存储顺序映射
     （编辑/删除走第 N 条的存储位次，显示位次 ≠ 存储位次） */
  const recList = d.recent || [];
  recList.slice().reverse().forEach((m, j) =>
    rec.appendChild(memItem(`[${m.role === "assistant" ? "小漓" : "用户"}] ${m.content}`,
      m.time, "recent", recList.length - j, m.content)));
  if (!recList.length) rec.appendChild(emptyBox("暂无近期对话"));
  (d.index || []).forEach((m, i) =>
    idx.appendChild(memItem(`${(m.kw || []).join("、")} → ${m.mem}`, "", "index", i + 1, m)));
  if (!d.index?.length) idx.appendChild(emptyBox("暂无关键词索引"));
}
function renderMemDeep(d, append) {
  const body = $("#memDeepBody");
  if (!append) body.innerHTML = "";
  /* 后端已按最新在前给页（offset 从尾部起算）：「加载更多」取更早的一批
     追加在下方，时序仍自上而下变旧 */
  (d.deep || []).forEach((m) =>
    body.appendChild(memItem(`#${m.line_no} [${m.role === "assistant" ? "小漓" : "用户"}] ${m.content}`,
      m.time, "deep", m.line_no, m.content)));
  const hasMore = d.deep_has_more !== undefined
    ? d.deep_has_more : (d.deep || []).length >= 200;
  if (S.deepQuery) {
    $("#deepInfo").textContent =
      `命中 ${d.deep_matched ?? 0} 条（显示最新 ${Math.min(200, d.deep_matched ?? 0)} 条）`;
    $("#btnDeepMore").hidden = !hasMore;
  } else {
    $("#deepInfo").textContent =
      `共 ${d.deep_total ?? 0} 条，已显示最新 ${S.deepOffset + (d.deep || []).length} 条` +
      (hasMore ? "（点「加载更多」看更早的）" : "");
    $("#btnDeepMore").hidden = !hasMore;
  }
  if (!body.children.length) body.appendChild(emptyBox("暂无深层存档"));
}
$("#memTabs").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  if (S.memChat === "__global__" && btn.dataset.v === "deep") return;
  $("#memTabs").children[1].hidden = false;  // 全局模式临时藏起，普通会话恢复
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
/* ---- 注入记忆（会话：重要记忆/深层存档；全局：重要记忆/关键词索引） ---- */
$("#btnMemInject").addEventListener("click", async () => {
  if (!S.memChat) return;
  if (S.memChat === "__global__") {
    const kind = await selectModal("注入全局记忆", "注入类型：",
      [{ value: "important", label: "重要记忆（所有聊天常驻带上）" },
       { value: "index", label: "关键词索引（命中自动注入）" }], "important");
    if (!kind) return;
    let r;
    if (kind === "index") {
      const mem = await inputModal("注入全局索引", "记忆内容：", "");
      if (mem === null || !mem) return;
      const kw = await inputModal("注入全局索引", "关键词（逗号 / 顿号分隔，命中即注入）：", "");
      if (kw === null || !kw) return;
      r = await API.memory_global_add("index", "", kw, mem);
    } else {
      const c = await inputModal("注入全局记忆", "要记住的内容（对任何聊天都成立的事实）：", "");
      if (c === null || !c) return;
      r = await API.memory_global_add("important", c);
    }
    toast(r.ok ? "已注入全局记忆 ✓" : "注入失败", r.ok ? "ok" : "err");
    if (r.ok) await refreshGlobalMem();
    return;
  }
  const kind = await selectModal("注入记忆", `注入到「${S.memChat}」的记忆类型：`,
    [{ value: "important", label: "重要记忆（常驻上下文，压缩不淘汰）" },
     { value: "deep", label: "深层存档（仅检索库，不进上下文）" }], "important");
  if (!kind) return;
  const c = await inputModal("注入记忆", "要记住的内容：", "");
  if (c === null || !c) return;
  const r = await API.memory_add(S.memChat, kind, c);
  toast(r.ok ? "已注入 ✓" : "注入失败", r.ok ? "ok" : "err");
  if (r.ok) { refreshMemDetail(); loadMemory(); }
});

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
   状态页（小漓状态层可视化：时段 + 每聊天心情/精力）
   ============================================================ */
function fmtAgo(ts) {
  if (!ts) return "—";
  const s = Math.max(0, Date.now() / 1000 - ts);
  if (s < 60) return "刚刚";
  if (s < 3600) return Math.floor(s / 60) + " 分钟前";
  if (s < 86400) return Math.floor(s / 3600) + " 小时前";
  return Math.floor(s / 86400) + " 天前";
}
async function loadPersona() {
  const r = await API.persona_state_overview();
  if (!r.ok) return;
  $("#personaPeriod").textContent = r.period || "—";
  $("#personaDisabled").hidden = !!r.enabled;
  const box = $("#personaRows");
  box.innerHTML = "";
  if (!(r.rows || []).length) {
    box.appendChild(emptyBox(
      r.enabled ? "还没有状态记录——聊几轮后（每次回复定量一次）这里会出现小漓的当前感受"
                : "状态层未开启"));
    return;
  }
  for (const row of r.rows) {
    const div = document.createElement("div");
    div.className = "feat-row slim";
    const energy = Number.isFinite(+row.energy)
      ? Math.max(0, Math.min(100, +row.energy)) : null;
    div.innerHTML = `
      <div class="feat-text"><b>${esc(row.chat)}</b>
        <small>${row.mood ? "心情：" + esc(row.mood) : "心情还没定量"} · ${fmtAgo(row.ts)}更新</small></div>
      <div class="persona-energy">
        <div class="trig-bar"><span style="width:${energy ?? 0}%"></span></div>
        <small class="baloo">${energy != null ? "精力 " + energy : "精力 —"}</small>
      </div>`;
    box.appendChild(div);
  }
}

/* ============================================================
   设置页（记忆 / 目录 / 任务桥 / 联网 / 语音 / 覆盖 / 监视）
   ============================================================ */
function refreshSettings() {
  const m = S.misc || {}, v = S.voice || {}, u = S.ui || {};
  $("#cbDeepMem").checked = !!m.memory_deep_enabled;
  $("#cbCompressMem").checked = !!m.memory_compress_enabled;
  $("#spKeepRecent").value = m.memory_keep_recent ?? 30;
  $("#spRollStep").value = m.memory_rolling_step ?? 30;
  $("#spCompressBatch").value = m.memory_compress_batch ?? 30;
  $("#spImportantMax").value = m.memory_important_max ?? 20;
  $("#edCompressModel").value = m.memory_compress_model || "";
  $("#edTasks").textContent = m.tasks_dir || "（未配置）";
  $("#edFiles").textContent = m.file_storage_path || "（未配置）";
  $("#edTianshuWorkdir").textContent = m.tianshu_workdir || "（未配置）";
  $("#edFirstPrompt").textContent = m.first_prompt_path || "内置模板";
  $("#cbTaskBridge").checked = !!m.task_enabled;
  $("#cbWebSearch").checked = m.web_search_enabled !== false;
  $("#cbNoReply").checked = !!m.no_reply_enabled;
  $("#cbGlobalMem").checked = !!m.global_memory_enabled;
  $("#cbSharedPool").checked = !!m.shared_pool_enabled;
  $("#cbPersonaState").checked = !!m.persona_state_enabled;
  $$("#mediaWaitSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.v === (m.media_wait_mode || "fixed")));
  $("#spMediaWaitSec").value = m.media_wait_seconds ?? 10;
  $("#spMediaQuietSec").value = m.media_wait_quiet_seconds ?? 5;
  $("#cbBudget").checked = !!m.daily_cost_limit_enabled;
  $("#edBudgetLimit").value = m.daily_cost_limit ?? 10;
  $("#cbFollowup").checked = !!m.followup_enabled;
  $("#spFuDelayMin").value = m.followup_delay_min ?? 45;
  $("#spFuDelayMax").value = m.followup_delay_max ?? 90;
  $("#spFuCooldown").value = m.followup_cooldown_minutes ?? 120;
  $("#spFuDaily").value = m.followup_daily_max ?? 10;
  $("#cbFuPrivate").checked = m.followup_private_only !== false;
  $$("#groupModeSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.v === (m.group_reply_mode || "mention")));
  $("#cbProactive").checked = !!m.proactive_enabled;
  $("#spProMin").value = m.proactive_interval_min ?? 10;
  $("#spProMax").value = m.proactive_interval_max ?? 20;
  $("#spProIdle").value = m.proactive_idle_minutes ?? 10;
  $("#spProCooldown").value = m.proactive_cooldown_minutes ?? 30;
  $("#spProDays").value = m.proactive_active_days ?? 7;
  $("#cbProPrivate").checked = m.proactive_private_only !== false;
  $("#cbProQuiet").checked = !!m.proactive_quiet_enabled;
  $("#spProQuietStart").value = m.proactive_quiet_start || "23:00";
  $("#spProQuietEnd").value = m.proactive_quiet_end || "08:00";
  $("#edWebProxy").value = m.web_proxy || "";
  $("#cbStateWatch").checked = !!m.state_watch_enabled;
  $("#edNickname").value = m.bot_nickname || "小漓";
  $("#followSystem").checked = !!u.follow_system;
  $$("#trigManageSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.v === (m.model_trigger_manage || "off")));
  $$("#stickerSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.v === (m.sticker_mode || "off")));
  $("#cbSegWait").checked = m.segment_wait_enabled !== false;
  $("#spSegWait").value = m.segment_wait_seconds ?? 2;
  $("#cbSegJitter").checked = !!m.segment_jitter_enabled;
  $("#spSegJitterMin").value = m.segment_jitter_min ?? 1.5;
  $("#spSegJitterMax").value = m.segment_jitter_max ?? 3;
  $("#cbMediaSend").checked = !!m.media_send_enabled;
  $("#spImageGenEndpoint").value = m.image_gen_endpoint || "";
  $("#spImageGenModel").value = m.image_gen_model || "";
  $("#spImageGenKey").value = m.image_gen_api_key || "";
  $("#cbRemoteEnabled").checked = !!m.remote_enabled;
  $("#cbRemoteLan").checked = !!m.remote_lan;
  $("#spRemotePort").value = m.remote_port ?? 8765;
  API.remote_info().then((r) => {
    if (r.ok) $("#spRemoteUrl").value = r.url || "";
  });
  refreshStickerInfo();
  $$("#fontSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.v === (u.font_scale || "medium")));
  $$("#voiceSeg button").forEach((b) =>
    b.classList.toggle("active", b.dataset.v === (v.voice_mode || "off")));
  $("#spVoiceMax").value = v.voice_max_seconds ?? 55;
  $("#spVoiceTimeout").value = v.tts_timeout_seconds ?? 120;
  $("#edVoiceEndpoint").value = v.tts_endpoint || "";
  renderVoiceProfiles();
  updateVoiceReady();
  reloadOvrChats();
}
async function updateVoiceReady() {
  const chip = $("#voiceReadyChip");
  if (!chip) return;
  const r = await API.voice_ready();
  if (!r.ok) { chip.textContent = ""; return; }
  if (r.ready) {
    chip.textContent = "✓ 语音已就绪";
    chip.className = "test-result ok";
  } else {
    chip.textContent = "✗ 未就绪";
    chip.className = "test-result bad";
  }
}
$("#btnVoiceTest").addEventListener("click", async () => {
  const r = await API.voice_selftest();
  if (!r.ok) { toast(r.error || "无法开始自检", "err"); return; }
  toast("自检中……正在用「通用」参考合成一句问候", "");
});
function onVoiceTestResult(p) {
  if (!p.ok) {
    toast(p.message || "自检失败", "err");
    updateVoiceReady();
    return;
  }
  toast(`自检通过 ✓ 合成 ${p.duration}s，试听中`, "ok");
  try {
    const audio = new Audio(p.audio);
    audio.play();
  } catch (e) {
    toast("合成成功，但本机无法播放音频", "err");
  }
  updateVoiceReady();
}
async function saveLongMemory() {
  const stepRaw = parseInt($("#spRollStep").value, 10);
  const rollStep = Number.isFinite(stepRaw) && stepRaw >= 0 ? stepRaw : 30;
  const r = await API.save_config({
    memory_deep_enabled: $("#cbDeepMem").checked,
    memory_compress_enabled: $("#cbCompressMem").checked,
    memory_keep_recent: parseInt($("#spKeepRecent").value) || 30,
    memory_rolling_step: rollStep,
    memory_compress_batch: parseInt($("#spCompressBatch").value) || 30,
    memory_important_max: parseInt($("#spImportantMax").value) || 20,
    memory_compress_model: $("#edCompressModel").value.trim(),
  });
  if (!r.ok) toast("保存失败：" + (r.error || ""), "err");
  return r;
}
async function pickDir(codeSel) {
  const r = await API.pick_folder();
  if (r.canceled || !r.path) return null;
  $(codeSel).textContent = r.path;
  return r.path;
}
$("#btnPickTasks").addEventListener("click", async () => {
  const p = await pickDir("#edTasks");
  if (p) await saveTasksDir();
});
async function saveTasksDir() {
  const newPath = $("#edTasks").textContent.trim();
  if (!newPath || newPath.startsWith("（")) { toast("请先浏览选择目录", "err"); return; }
  const oldPath = (S.misc || {}).tasks_dir || "";
  if (newPath === oldPath) return { ok: true };
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
  if (!r.ok) { toast(r.error || "保存失败", "err"); return r; }
  S.misc.tasks_dir = newPath;
  toast(r.removed_old ? "任务目录已变更，旧目录已清空" : "任务目录已变更 ✓", "ok");
  return r;
}
$("#btnPickFiles").addEventListener("click", async () => {
  const p = await pickDir("#edFiles");
  if (p) await saveFilesDir();
});
async function saveFilesDir() {
  const p = $("#edFiles").textContent.trim();
  if (!p || p === "（未配置）") { toast("请先浏览选择目录", "err"); return; }
  const r = await API.save_config({ file_storage_path: p });
  if (!r.ok) toast("保存失败", "err");
  else toast("微信接收目录已保存并热生效 ✓", "ok");
  return r;
}
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
  if (!r.ok) { toast("保存失败", "err"); return; }
  /* 环境卡跟着开关显隐（天枢 CLI / 首轮提示词只服务任务桥） */
  if (S.misc) S.misc.task_enabled = e.target.checked;
  $("#envTianshu").hidden = !e.target.checked;
  $("#envPrompt").hidden = !e.target.checked;
  if (!e.target.checked) { toast("任务桥已关闭（模型看不到任务工具）", "ok"); return; }
  // 首次开启（天枢还没配置过）→ 走一次性配置引导：CLI 安装/打开 + API key 配置
  const info = await API.tianshu_guide_info();
  if (info && info.ok && info.guided) { toast("任务桥已开启（热生效）", "ok"); return; }
  toast("任务桥已开启——先完成一次天枢配置（会自动唤起 CLI）", "ok");
  openTianshuGuide();
});
$("#cbWebSearch").addEventListener("change", async (e) => {
  const r = await API.save_config({ web_search_enabled: e.target.checked });
  toast(r.ok ? (e.target.checked ? "联网搜索已开启" : "联网搜索已关闭") : "保存失败",
    r.ok ? "ok" : "err");
});
async function saveProxy() {
  const r = await API.save_config({ web_proxy: $("#edWebProxy").value.trim() });
  if (!r.ok) toast("保存失败", "err");
  return r;
}
async function saveNickname() {
  const r = await API.set_nickname($("#edNickname").value);
  if (!r.ok) { toast(r.error || "保存失败", "err"); return r; }
  $("#edNickname").value = r.nickname;
  toast(`微信名称已改为「${r.nickname}」，立即生效 ✓（群里 @ 它就能找到小漓）`, "ok");
  return r;
}
$("#cbStateWatch").addEventListener("change", async (e) => {
  const r = await API.save_config({ state_watch_enabled: e.target.checked });
  toast(r.ok ? (e.target.checked ? "状态监视已开启" : "状态监视已关闭") : "保存失败",
    r.ok ? "ok" : "err");
});
$("#cbNoReply").addEventListener("change", async (e) => {
  const r = await API.save_config({ no_reply_enabled: e.target.checked });
  toast(r.ok ? (e.target.checked ? "智能静默已开启：AI 可自行决定不接某条话" : "智能静默已关闭")
    : "保存失败", r.ok ? "ok" : "err");
});
$("#cbGlobalMem").addEventListener("change", async (e) => {
  const r = await API.save_config({ global_memory_enabled: e.target.checked });
  toast(r.ok ? (e.target.checked ? "全局记忆已开启（记忆页可注入/审计）" : "全局记忆已关闭")
    : "保存失败", r.ok ? "ok" : "err");
});
$("#cbSharedPool").addEventListener("change", async (e) => {
  const r = await API.save_config({ shared_pool_enabled: e.target.checked });
  toast(r.ok ? (e.target.checked ? "共享记忆池已开启：AI 可跨聊天检索记忆" : "共享记忆池已关闭")
    : "保存失败", r.ok ? "ok" : "err");
});
/* ---- 小漓状态层 ---- */
$("#cbPersonaState").addEventListener("change", async (e) => {
  const r = await API.save_config({ persona_state_enabled: e.target.checked });
  toast(r.ok ? (e.target.checked
    ? "状态层已开启：小漓会随时段与情绪起伏（情绪定量每聊天 5 分钟一次，有少量成本）"
    : "状态层已关闭") : "保存失败", r.ok ? "ok" : "err");
});
/* ---- 媒体消息等待（双模式） ---- */
$("#mediaWaitSeg").addEventListener("click", (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  [...btn.parentElement.children].forEach((b) => b.classList.toggle("active", b === btn));
});
async function saveMediaWait() {
  const num = (id, dft) => {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) && v > 0 ? v : dft;
  };
  const r = await API.save_config({
    media_wait_mode: $("#mediaWaitSeg button.active")?.dataset.v || "fixed",
    media_wait_seconds: num("#spMediaWaitSec", 10),
    media_wait_quiet_seconds: num("#spMediaQuietSec", 5),
  });
  if (!r.ok) toast("保存失败", "err");
  return r;
}
/* ---- 每日消费预算 ---- */
async function saveBudget() {
  const enabled = $("#cbBudget").checked;
  const v = parseFloat($("#edBudgetLimit").value);
  const limit = Number.isFinite(v) && v > 0 ? v : 10;
  const r = await API.save_config({ daily_cost_limit_enabled: enabled,
                                    daily_cost_limit: limit });
  if (!r.ok) toast("保存失败", "err");
  if (r.ok) loadUsage();
  return r;
}
/* ---- 主动追尾 ---- */
async function saveFollowup() {
  const num = (id, dft) => {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) && v >= 0 ? v : dft;
  };
  const r = await API.save_config({
    followup_enabled: $("#cbFollowup").checked,
    followup_delay_min: num("#spFuDelayMin", 45),
    followup_delay_max: num("#spFuDelayMax", 90),
    followup_cooldown_minutes: num("#spFuCooldown", 120),
    followup_daily_max: Math.round(num("#spFuDaily", 10)),
    followup_private_only: $("#cbFuPrivate").checked,
  });
  if (!r.ok) toast("保存失败", "err");
  return r;
}
/* ---- 群聊接收模式（mention / auto / hybrid） ---- */
$("#groupModeSeg").addEventListener("click", (e) => {
  const b = e.target.closest("button[data-v]");
  if (!b) return;
  $$("#groupModeSeg button").forEach((x) =>
    x.classList.toggle("active", x === b));
});
async function saveGroupMode() {
  const mode = $("#groupModeSeg button.active")?.dataset.v || "mention";
  const r = await API.save_config({ group_reply_mode: mode });
  if (!r.ok) toast("保存失败", "err");
  return r;
}
/* ---- 主动发起对话（默认关，不限发送条数） ---- */
async function saveProactive() {
  const num = (id, dft) => {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) && v >= 0 ? v : dft;
  };
  let lo = Math.max(1, Math.round(num("#spProMin", 10)));
  let hi = Math.max(1, Math.round(num("#spProMax", 20)));
  if (hi < lo) {
    [lo, hi] = [hi, lo];
    $("#spProMin").value = lo;
    $("#spProMax").value = hi;
  }
  const r = await API.save_config({
    proactive_enabled: $("#cbProactive").checked,
    proactive_interval_min: lo,
    proactive_interval_max: hi,
    proactive_idle_minutes: Math.round(num("#spProIdle", 10)),
    proactive_cooldown_minutes: Math.round(num("#spProCooldown", 30)),
    proactive_active_days: Math.max(1, Math.round(num("#spProDays", 7))),
    proactive_private_only: $("#cbProPrivate").checked,
    proactive_quiet_enabled: $("#cbProQuiet").checked,
    proactive_quiet_start: $("#spProQuietStart").value || "23:00",
    proactive_quiet_end: $("#spProQuietEnd").value || "08:00",
  });
  if (!r.ok) toast("保存失败", "err");
  return r;
}
/* ---- 图片与文件发送（默认关；画图接口化，端点留空 = 无画图能力） ---- */
async function saveMediaSend() {
  const r = await API.save_config({
    media_send_enabled: $("#cbMediaSend").checked,
    image_gen_endpoint: $("#spImageGenEndpoint").value.trim(),
    image_gen_model: $("#spImageGenModel").value.trim(),
    image_gen_api_key: $("#spImageGenKey").value.trim(),
  });
  if (!r.ok) toast("保存失败", "err");
  return r;
}
/* ---- 远程面板（默认关；开关改动即保存，端口用保存按钮） ---- */
async function applyRemotePanel() {
  let enabled = $("#cbRemoteEnabled").checked;
  let lan = $("#cbRemoteLan").checked;
  // 两个开关联动，避免「服务关着但局域网开放亮着」这种自相矛盾的界面：
  // 局域网红必须服务在跑，服务关掉则局域网开放一并关。
  if (lan && !enabled) { enabled = true; $("#cbRemoteEnabled").checked = true; }
  if (!enabled) { lan = false; $("#cbRemoteLan").checked = false; }
  const port = Math.min(65535, Math.max(
    1024, Math.round(parseFloat($("#spRemotePort").value) || 8765)));
  $("#spRemotePort").value = port;
  const r = await API.save_config({
    remote_enabled: enabled, remote_lan: lan, remote_port: port,
  });
  if (!r.ok) { toast(r.error || "保存失败", "err"); return; }
  const info = await API.remote_info();
  if (info.ok) $("#spRemoteUrl").value = info.url || "";
  toast(enabled
    ? (lan ? "远程面板已开启（局域网可访问）"
           : "远程面板已开启（仅本机；手机访问请开「局域网开放」）")
    : "远程面板已关闭", "ok");
}
$("#cbRemoteEnabled").addEventListener("change", applyRemotePanel);
$("#cbRemoteLan").addEventListener("change", applyRemotePanel);
$("#btnRemoteResetToken").addEventListener("click", async () => {
  const r = await withBusy($("#btnRemoteResetToken"),
    () => API.remote_reset_token());
  if (!r.ok) { toast(r.error || "重置失败", "err"); return; }
  $("#spRemoteUrl").value = r.url || "";
  toast("令牌已重置，旧链接立即失效", "ok");
});
$("#btnRemoteCopy").addEventListener("click", async () => {
  const url = $("#spRemoteUrl").value;
  if (!url) { toast("还没生成访问地址：先打开上面的开关", "err"); return; }
  try {
    await navigator.clipboard.writeText(url);
    toast("链接已复制，在手机浏览器打开即可", "ok");
  } catch (e) {
    toast("复制失败（框内手动选择复制）", "err");
  }
});
/* ---- 模型管理触发器（三态，点击即存） ---- */
$("#trigManageSeg").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  [...btn.parentElement.children].forEach((b) => b.classList.toggle("active", b === btn));
  const r = await API.save_config({ model_trigger_manage: btn.dataset.v });
  toast(r.ok ? ({
    off: "已关闭：AI 只能建触发器，不能删改",
    lazy: "已开启（仅工具）：AI 被问到时先查询再操作",
    eager: "已开启（清单注入）：AI 随时知道有哪些约定",
  }[btn.dataset.v] || "已保存") : "保存失败", r.ok ? "ok" : "err");
});
/* ---- 表情包（三态，点击即存） ---- */
$("#stickerSeg").addEventListener("click", async (e) => {
  const btn = e.target.closest("button");
  if (!btn) return;
  [...btn.parentElement.children].forEach((b) => b.classList.toggle("active", b === btn));
  const r = await API.save_config({ sticker_mode: btn.dataset.v });
  toast(r.ok ? ({
    off: "表情包已关闭",
    catalog: "已开启（清单注入）：AI 每次都看得到表情包目录",
    query: "已开启（按需查询）：AI 想发时自己搜或拿全清单",
  }[btn.dataset.v] || "已保存") : "保存失败", r.ok ? "ok" : "err");
});
let stickerItems = [];
async function refreshStickerInfo(rerenderGrid) {
  const el = $("#stickerCount"), dir = $("#stickerDir");
  if (!el) return;
  const r = await API.sticker_info();
  if (!r.ok) { el.textContent = "表情包库不可用"; dir.textContent = ""; return; }
  el.textContent = `表情包库：${r.count} 张`;
  dir.textContent = r.dir || "";
  stickerItems = r.items || [];
  if (rerenderGrid) renderStickerGrid();
}
function renderStickerGrid() {
  const tb = $("#stickerTbody");
  if (!tb) return;
  tb.innerHTML = stickerItems.map((it) => `
    <tr data-file="${esc(it.file)}">
      <td>${it.thumb ? `<img class="sticker-thumb" src="${it.thumb}" alt="">`
                      : '<span class="sticker-thumb empty"></span>'}</td>
      <td class="sticker-file" title="${esc(it.file)}">${esc(it.file)}</td>
      <td><input class="sticker-desc" value="${esc(it.desc)}"></td>
      <td><input class="sticker-tags" value="${esc((it.tags || []).join(", "))}"></td>
      <td><button class="mini-btn sticker-save">保存</button></td>
    </tr>`).join("");
}
$("#btnStickerManage").addEventListener("click", async () => {
  openModal("modalStickers");
  await refreshStickerInfo(true);
});
$("#stickerTbody").addEventListener("click", async (e) => {
  const btn = e.target.closest(".sticker-save");
  if (!btn) return;
  const tr = btn.closest("tr");
  const file = tr.dataset.file;
  const r = await API.sticker_set_desc(
    file, tr.querySelector(".sticker-desc").value.trim(),
    tr.querySelector(".sticker-tags").value.trim());
  toast(r.ok ? `已保存：${file}` : (r.error || "保存失败"), r.ok ? "ok" : "err");
  if (r.ok) refreshStickerInfo(false);
});
$("#btnStickerAddFiles").addEventListener("click", async () => {
  const r = await API.sticker_add_files();
  if (!r.ok) { toast(r.error || "无法添加", "err"); return; }
  toast(r.canceled ? "已取消"
    : `已添加 ${r.added} 张${r.skipped ? `，跳过同名 ${r.skipped} 张` : ""}`,
    "ok");
  refreshStickerInfo(true);
});
$("#btnStickerAddFolder").addEventListener("click", async () => {
  const r = await API.sticker_add_folder();
  if (!r.ok) { toast(r.error || "无法添加", "err"); return; }
  toast(r.canceled ? "已取消"
    : `已添加 ${r.added} 张${r.skipped ? `，跳过同名 ${r.skipped} 张` : ""}`,
    "ok");
  refreshStickerInfo(true);
});
$("#btnStickerOpenDir").addEventListener("click", () => API.sticker_open_dir());
$("#btnStickerPreview").addEventListener("click", async () => {
  const r = await API.sticker_preview();
  if (!r.ok) { toast(r.error || "预览失败", "err"); return; }
  if (!r.count) {
    toast("表情包库为空，清单注入不会带任何内容", "");
    return;
  }
  $("#stickerPreviewMeta").textContent =
    `共 ${r.count} 张，注入正文约 ${r.tokens} token（清单注入模式下每次对话请求都会带上这份）`;
  $("#stickerPreviewText").textContent = r.text;
  openModal("modalStickerPreview");
});
$("#btnStickerRetag").addEventListener("click", async () => {
  const r = await API.sticker_retag();
  if (!r.ok) { toast(r.error || "无法开始打标", "err"); return; }
  $("#retagProgress").hidden = false;
  $("#retagHint").hidden = false;
  $("#retagFill").style.width = "0%";
  $("#retagHint").textContent = "AI 正在逐张看图…";
  $("#btnStickerRetag").disabled = true;
  toast("AI 优化标签已开始", "");
});
function onStickerRetag(p) {
  const btn = $("#btnStickerRetag"), bar = $("#retagProgress"),
    hint = $("#retagHint");
  if (!p.ok) {
    if (bar) bar.hidden = true;
    if (hint) { hint.hidden = false; hint.textContent = p.message || "打标失败"; }
    if (btn) btn.disabled = false;
    toast(p.message || "打标失败", "err");
    return;
  }
  if (p.total && p.done < p.total) {
    if ($("#retagFill"))
      $("#retagFill").style.width = Math.round((p.done / p.total) * 100) + "%";
    if (hint) hint.textContent = `打标中 ${p.done}/${p.total}`;
    return;
  }
  if (bar) bar.hidden = true;
  if (hint) { hint.hidden = false; hint.textContent = p.message || "打标完成"; }
  if (btn) btn.disabled = false;
  toast(p.message || "打标完成", "ok");
  const modal = $("#modalStickers");
  refreshStickerInfo(modal && !modal.hidden);
}
/* ---- 回复节奏（两互斥开关 + 数字参数，批量保存） ---- */
$("#cbSegWait").addEventListener("change", (e) => {
  if (e.target.checked) $("#cbSegJitter").checked = false;
});
$("#cbSegJitter").addEventListener("change", (e) => {
  if (e.target.checked) $("#cbSegWait").checked = false;
});
async function savePace() {
  const jitter = $("#cbSegJitter").checked;
  const num = (id, dft) => {
    const v = parseFloat($(id).value);
    return Number.isFinite(v) && v >= 0 ? v : dft;
  };
  const r = await API.save_config({
    segment_wait_enabled: !jitter && $("#cbSegWait").checked,
    segment_wait_seconds: num("#spSegWait", 2),
    segment_jitter_enabled: jitter,
    segment_jitter_min: num("#spSegJitterMin", 1.5),
    segment_jitter_max: num("#spSegJitterMax", 3),
  });
  if (!r.ok) toast("保存失败", "err");
  return r;
}
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
      saveVoice();
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
  saveVoice();
});
$("#btnVoiceDel").addEventListener("click", async () => {
  const v = S.voice;
  const cur = curVoiceProfile();
  if (!cur) return;
  if (!(await confirmModal("删除音色档案", `确定删除档案「${cur.name}」？`))) return;
  v.voice_profiles = v.voice_profiles.filter((p) => p.id !== cur.id);
  if (v.active_voice_profile_id === cur.id) v.active_voice_profile_id = "";
  renderVoiceProfiles();
  saveVoice();
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
  saveVoice();
});
async function saveVoice() {
  const v = S.voice;
  const cur = curVoiceProfile();
  if (cur) {
    cur.name = $("#edVoiceName").value.trim() || cur.name;
    const newRefs = {};
    $$("#voiceRefsTbody tr").forEach((tr) => {
      const emo = $("input[data-rf='emo']", tr);
      if (!emo) return;
      const ref = {
        ref_audio_path: $("input[data-rf='audio']", tr).value.trim(),
        prompt_text: $("input[data-rf='text']", tr).value.trim(),
      };
      // 通用行只有情绪名锁定，路径/文本稿照常收集——跳过会让通用永远
      // 存不进去（新建档案的通用是空壳，表现为「填了就清空」）
      if (emo.disabled) {
        newRefs["通用"] = ref;
        return;
      }
      newRefs[emo.value.trim() || "未命名"] = ref;
    });
    // 表格渲染的是空态占位行（refs 为空）时没有通用行，保底不误删旧值
    if (!("通用" in newRefs) && cur.refs && cur.refs["通用"]) {
      newRefs["通用"] = cur.refs["通用"];
    }
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
  if (!r.ok) toast("保存失败：" + (r.error || ""), "err");
  if (r.ok && cur) {
    // 只更新下拉里的档案名，不整表重渲染——自动保存是失焦触发的，
    // renderVoiceProfiles 会重建 refs 表格，把用户刚 Tab 进去的输入框
    // 一起销毁（焦点丢失）。
    const opt = Array.from($("#cbVoiceProfile").options)
      .find((o) => o.value === cur.id);
    if (opt) opt.textContent = cur.name || "未命名档案";
  }
  return r;
}
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
async function saveOverride() {
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
  if (!r.ok) toast("保存失败", "err");
  return r;
}
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
   设置页自动保存：保存按钮已移除，字段改动即落盘
   ============================================================ */
let _autoSaveToastAt = 0;
function autoSaveToast() {
  // 连续改多个字段时只提示一次，避免刷屏
  const now = Date.now();
  if (now - _autoSaveToastAt < 2000) return;
  _autoSaveToastAt = now;
  toast("已自动保存", "ok");
}
function bindAutoSave(rootSel, fn, evt = "change",
                      childSel = "input, select, textarea") {
  const root = $(rootSel);
  if (!root) return;
  let timer = null;
  root.addEventListener(evt, (e) => {
    const hit = evt === "click" ? e.target.closest(childSel)
                                : e.target.matches(childSel);
    if (!hit) return;
    clearTimeout(timer);
    timer = setTimeout(async () => {
      try {
        const r = await fn();
        if (r && r.ok) autoSaveToast();
      } catch (err) {
        toast("自动保存失败：" + (err?.message || err), "err");
      }
    }, 250);
  });
}
bindAutoSave("#sec-memory", saveLongMemory);
bindAutoSave("#edNickname", saveNickname);
bindAutoSave("#edWebProxy", saveProxy);
bindAutoSave("#voiceSeg", saveVoice, "click", "button");
bindAutoSave("#edVoiceEndpoint", saveVoice);
bindAutoSave("#spVoiceTimeout", saveVoice);
bindAutoSave("#spVoiceMax", saveVoice);
bindAutoSave("#edVoiceName", saveVoice);
bindAutoSave("#voiceRefsTbody", saveVoice);
bindAutoSave("#cbVoiceActive", saveVoice);
bindAutoSave("#ovrCombos", saveOverride, "change", "select");
bindAutoSave("#mediaWaitSeg", saveMediaWait, "click", "button");
bindAutoSave("#spMediaWaitSec", saveMediaWait);
bindAutoSave("#spMediaQuietSec", saveMediaWait);
bindAutoSave("#cbBudget", saveBudget);
bindAutoSave("#edBudgetLimit", saveBudget);
bindAutoSave("#cbFollowup", saveFollowup);
bindAutoSave("#spFuDelayMin", saveFollowup);
bindAutoSave("#spFuDelayMax", saveFollowup);
bindAutoSave("#spFuCooldown", saveFollowup);
bindAutoSave("#spFuDaily", saveFollowup);
bindAutoSave("#cbFuPrivate", saveFollowup);
bindAutoSave("#groupModeSeg", saveGroupMode, "click", "button");
bindAutoSave("#cbProactive", saveProactive);
bindAutoSave("#spProMin", saveProactive);
bindAutoSave("#spProMax", saveProactive);
bindAutoSave("#spProIdle", saveProactive);
bindAutoSave("#spProCooldown", saveProactive);
bindAutoSave("#spProDays", saveProactive);
bindAutoSave("#cbProPrivate", saveProactive);
bindAutoSave("#cbProQuiet", saveProactive);
bindAutoSave("#spProQuietStart", saveProactive);
bindAutoSave("#spProQuietEnd", saveProactive);
bindAutoSave("#cbSegWait", savePace);
bindAutoSave("#spSegWait", savePace);
bindAutoSave("#cbSegJitter", savePace);
bindAutoSave("#spSegJitterMin", savePace);
bindAutoSave("#spSegJitterMax", savePace);
bindAutoSave("#cbMediaSend", saveMediaSend);
bindAutoSave("#spImageGenEndpoint", saveMediaSend);
bindAutoSave("#spImageGenModel", saveMediaSend);
bindAutoSave("#spImageGenKey", saveMediaSend);
bindAutoSave("#spRemotePort", applyRemotePanel);

/* ============================================================
   新手引导（首启 / 设置页重跑）
   五步：环境 → 模型 → 身份/角色卡 → 微信文件目录（可选）→ 完成
   任务工作目录不在这里：任务桥默认关，开启任务桥后走天枢配置引导。
   ============================================================ */
const DEFAULT_DOCS_URL =
  "https://github.com/wens6515/weixin-wechat-wxauto-ocr-xiaoli/blob/main/docs/使用文档/README.md";
const ONB = { step: 1, status: null, providers: [], provIndex: 0,
              cards: [], defaults: {}, finishing: false };

async function openDocs() {
  const url = S.docsUrl || DEFAULT_DOCS_URL;
  // 远程面板是手机浏览器，直接新开页；本地 pywebview 走系统浏览器
  if (window.__XL_REMOTE__) { window.open(url, "_blank"); return; }
  const r = await API.open_external(url);
  if (!r || !r.ok) {
    toast("打开使用文档失败，可手动访问 GitHub 仓库 docs/使用文档", "err");
  }
}

function onbRow(label, ok, detail, kind) {
  const cls = kind || (ok === true ? "ok" : ok === false ? "bad" : "warn");
  return `<div class="onb-status-row ${cls}"><span class="dot"></span>` +
    `<span><b>${esc(label)}</b> · ${esc(detail || "")}</span></div>`;
}

function renderOnbEnv(status) {
  const st = status || {};
  const w = st.wechat || {};
  const scale = st.scale || {};
  const noWindow = !!w.ok && w.window_found === false;
  const wOk = !!w.ok && !w.minimized && !noWindow;
  const wd = w.minimized
    ? "微信窗口已最小化——运行前请恢复窗口（最小化时视觉通道是盲的）"
    : (noWindow
        ? "微信进程在运行，但没有找到主窗口——请打开微信主窗口"
        : (w.detail || "未检测到微信窗口"));
  $("#onbEnvRows").innerHTML = [
    onbRow("微信 PC 版", wOk, wd, wOk ? undefined : (w.ok ? "warn" : "bad")),
    onbRow("显示缩放", scale.ok === true, scale.detail || "未检测到",
      scale.ok === true ? "ok" : "warn"),
  ].join("");
}

function renderOnbSummary() {
  const st = ONB.status || {};
  const w = st.wechat || {};
  const api = st.api || {};
  const card = st.card || {};
  const files = st.files_dir || {};
  const noWindow = !!w.ok && w.window_found === false;
  const wOk = !!w.ok && !w.minimized && !noWindow;
  const wd = w.minimized ? "微信窗口已最小化"
    : (noWindow ? "微信进程在运行，但没有找到主窗口"
      : (w.detail || "未检测到微信窗口"));
  $("#onbSummary").innerHTML = [
    onbRow("微信", wOk, wd, wOk ? undefined : (w.ok ? "warn" : "bad")),
    onbRow("模型", !!api.ok, api.detail || "未配置", api.ok ? "ok" : "bad"),
    onbRow("角色卡", !!card.ok, card.name || "未找到角色卡",
      card.ok ? "ok" : "bad"),
    onbRow("微信接收目录", !!files.ok,
      files.ok ? files.detail : "未配置（可选，收文件功能不可用）",
      files.ok ? "ok" : "warn"),
  ].join("");
  const eng = st.engine || {};
  $("#onbEngineHint").textContent = eng.state === "running"
    ? "引擎已在运行；如需暂停，回首页点主按钮。"
    : "运行期间请勿最小化微信窗口；小漓会把微信窗口大小固定在工作区右半屏。";
}

async function refreshOnbStatus() {
  const st = await API.onboarding_status();
  if (st && st.ok) {
    ONB.status = st;
    S.docsUrl = st.docs_url || S.docsUrl;
  }
  return ONB.status;
}

function fillOnbProvider() {
  const p = ONB.providers[ONB.provIndex] || {};
  $("#onbBase").value = p.base_url || "";
  $("#onbKey").value = "";
  $("#onbKey").placeholder = p.api_key ? "已保存，留空保持不变" : "sk-...";
  const models = p.models || [];
  $("#onbModelList").innerHTML = models.map(
    (m) => `<option value="${esc(m)}"></option>`).join("");
  const card = (ONB.cards || []).find((c) => c.id === S.activeCardId);
  $("#onbModel").value =
    (card && card.chat_provider === p.id && card.chat_model) ||
    models[0] || "";
}

function mergeOnbProviders(existing) {
  // 新手引导要展示全部预设，而不是只展示 config 里已经落盘的那一个
  // （全新 config 往往只迁移出 DeepSeek，用户会以为没有别家可选）。
  const byId = new Map((existing || []).filter((p) => p && p.id)
    .map((p) => [String(p.id), p]));
  const out = [];
  for (const preset of PRESETS) {
    const cur = byId.get(preset.id);
    if (cur) {
      out.push({
        ...preset, ...cur,
        models: (cur.models && cur.models.length) ? cur.models : [...preset.models],
      });
      byId.delete(preset.id);
    } else {
      out.push({ ...preset, api_key: "", models: [...preset.models] });
    }
  }
  // 用户自建的 Provider（id 不在预设里）排在后面，保留原配置
  for (const p of byId.values()) out.push(p);
  return out;
}

async function showOnboarding() {
  const st = await API.onboarding_status();
  if (!st || !st.ok) {
    toast((st && st.error) || "读取配置状态失败", "err");
    return;
  }
  ONB.status = st;
  S.docsUrl = st.docs_url || S.docsUrl || DEFAULT_DOCS_URL;
  const [pr, cr, fd] = await Promise.all([
    API.get_providers(), API.list_cards(), API.first_run_defaults(),
  ]);
  const existing = (pr && pr.ok && pr.providers) ? pr.providers : [];
  ONB.providers = JSON.parse(JSON.stringify(mergeOnbProviders(existing)));
  ONB.cards = (cr && cr.ok && cr.cards) ? cr.cards : [];
  ONB.defaults = (fd && fd.ok) ? fd : {};
  if (cr && cr.ok && cr.active_id) S.activeCardId = cr.active_id;
  const activeProv = (ONB.cards.find((c) => c.id === S.activeCardId) || {}).chat_provider;
  ONB.provIndex = Math.max(0, ONB.providers.findIndex((p) =>
    p.id === activeProv || (activeProv ? false : !!p.api_key)));
  if (activeProv && ONB.providers[ONB.provIndex]?.id !== activeProv) {
    const hit = ONB.providers.findIndex((p) => p.id === activeProv);
    if (hit >= 0) ONB.provIndex = hit;
  }
  $("#onbProv").innerHTML = ONB.providers.map((p, i) =>
    `<option value="${i}">${esc(p.name || p.id || ("Provider " + (i + 1)))}</option>`).join("");
  $("#onbProv").value = String(ONB.provIndex);
  fillOnbProvider();
  $("#onbNickname").value = st.card?.nickname || "小漓";
  $("#onbCard").innerHTML = ONB.cards.length
    ? ONB.cards.map((c) =>
        `<option value="${esc(c.id)}">${esc(c.name || c.id)}</option>`).join("")
    : `<option value="">（默认角色卡）</option>`;
  $("#onbCard").value = (cr && cr.active_id) || S.activeCardId || "";
  $("#onbFiles").textContent = (ONB.defaults.file_storage_path || "（未配置，可稍后设置）");
  $("#onbTestResult").textContent = "";
  renderOnbEnv(st);
  onbStep(1);
  openModal("modalOnboarding");
}

function onbStep(n) {
  ONB.step = Math.max(1, Math.min(5, n));
  $$(".onb-step").forEach((el) => {
    el.hidden = Number(el.dataset.step) !== ONB.step;
  });
  $$(".onb-dot").forEach((el) => {
    const k = Number(el.dataset.step);
    el.classList.toggle("active", k === ONB.step);
    el.classList.toggle("done", k < ONB.step);
  });
  $("#onbBack").hidden = ONB.step === 1;
  $("#onbNext").hidden = ONB.step === 5;
  if (ONB.step === 5) refreshOnbStatus().then(renderOnbSummary);
}

async function saveOnbModel() {
  const p = ONB.providers[ONB.provIndex];
  if (!p) { toast("没有可用的模型服务商", "err"); return false; }
  const base = $("#onbBase").value.trim();
  const key = $("#onbKey").value.trim();
  const model = $("#onbModel").value.trim() || (p.models || [])[0] || "";
  if (!base) { toast("请填写 Base URL", "err"); return false; }
  if (!key && !p.api_key) {
    toast("还没有填 API Key——可以先继续，稍后到「模型」页补；小漓暂时无法回复", "warn");
  }
  const list = ONB.providers.map((x, i) => {
    if (i !== ONB.provIndex) return x;
    const models = [...(x.models || [])];
    if (model && !models.includes(model)) models.push(model);
    return { ...x, base_url: base, api_key: key || x.api_key || "", models };
  });
  const r = await API.save_providers(list, p.id, model || null);
  if (!r || !r.ok) { toast((r && r.error) || "保存模型配置失败", "err"); return false; }
  ONB.providers = r.providers || list;
  S.providers = ONB.providers;
  return true;
}

async function saveOnbIdentity() {
  const nick = $("#onbNickname").value.trim();
  if (!nick) { toast("微信名称不能为空", "err"); return false; }
  // 先切卡再写昵称：set_nickname 写的是「当前活跃卡」，顺序反了会把
  // 昵称写到旧卡上，新卡仍是原来的称呼。
  const cid = $("#onbCard").value;
  if (cid && cid !== S.activeCardId) {
    const ar = await API.activate_card(cid);
    if (ar && ar.ok) {
      S.activeCardId = cid;
    } else {
      toast((ar && ar.error) || "切换角色卡失败（稍后可在角色卡页重试）", "warn");
    }
  }
  const r = await API.set_nickname(nick);
  if (!r || !r.ok) { toast((r && r.error) || "保存微信名称失败", "err"); return false; }
  return true;
}

async function saveOnbFiles() {
  const raw = $("#onbFiles").textContent.trim();
  const files = raw.startsWith("（") ? "" : raw;
  const d = ONB.defaults || {};
  const r = await API.save_first_run(
    d.tasks_dir || "", files, d.memory_file || "memory.json",
    $("#onbNickname").value.trim() || "小漓");
  if (!r || !r.ok) { toast((r && r.error) || "保存文件目录失败", "err"); return false; }
  if (S.misc) S.misc.file_storage_path = files;
  return true;
}

async function startEngineFromOnboarding() {
  if (S.engineState === "running") { toast("引擎已在运行", "ok"); return; }
  if (S.engineState === "idle" || S.engineState === "error") {
    const r = await API.engine_action("initialize");
    if (!r || !r.ok) { toast((r && r.error) || "初始化请求失败", "err"); return; }
    toast("正在初始化引擎，请稍候…", "");
    for (let i = 0; i < 120; i++) {
      await new Promise((res) => setTimeout(res, 500));
      const st = await API.get_status();
      if (!st || !st.ok) continue;
      if (st.state === "initialized") {
        const sr = await API.engine_action("start");
        toast(sr && sr.ok ? "已开始监听微信消息 ✓" : "初始化完成，请回首页点「启动 bot」",
          sr && sr.ok ? "ok" : "warn");
        return;
      }
      if (st.state === "running") { toast("已开始监听微信消息 ✓", "ok"); return; }
      if (st.state === "error") {
        const d = await API.engine_error_detail();
        toast("初始化失败：" + ((d && d.error) || st.error || "请查看首页提示"), "err");
        return;
      }
    }
    toast("初始化超时，请回首页点主按钮重试", "warn");
    return;
  }
  const r = await API.engine_action("start");
  toast(r && r.ok ? "已开始监听微信消息 ✓" : "启动失败，请回首页重试",
    r && r.ok ? "ok" : "err");
}

async function finishOnboarding(start) {
  if (ONB.finishing) return;
  ONB.finishing = true;
  try {
    const r = await API.onboarding_complete();
    if (!r || !r.ok) { toast((r && r.error) || "保存引导状态失败", "err"); return; }
    closeModal();
    toast("配置完成，欢迎使用小漓 ✓", "ok");
    const cfg = await API.get_config();
    if (cfg && cfg.ok) {
      S.ui = cfg.ui; S.misc = cfg.misc; S.voice = cfg.voice;
      S.docsUrl = cfg.docs_url || S.docsUrl;
      if (S.misc) S.misc.bot_nickname = $("#onbNickname").value.trim() || S.misc.bot_nickname;
      refreshSettings();
    }
    await refreshSetupBanner();
    if (start) await startEngineFromOnboarding();
  } finally {
    ONB.finishing = false;
  }
}

async function refreshSetupBanner() {
  const st = await API.onboarding_status();
  if (!st || !st.ok) { $("#setupBanner").hidden = true; return; }
  S.docsUrl = st.docs_url || S.docsUrl;
  const msgs = [];
  let kind = "settings";
  if (!st.onboarding_done) { msgs.push("新手引导还没完成"); kind = "onboarding"; }
  if (st.api && !st.api.ok) {
    msgs.push("模型未配置，小漓无法回复");
    if (kind !== "onboarding") kind = "models";
  }
  // 文件接收目录是可选项：不在首页常驻提示（首启完成页/设置页已说明），
  // 避免不用文件功能的用户被长期打扰。
  const banner = $("#setupBanner");
  if (!msgs.length) { banner.hidden = true; return; }
  $("#setupBannerText").textContent = msgs.join(" · ");
  banner.dataset.kind = kind;
  banner.hidden = false;
}

$("#onbProv").addEventListener("change", () => {
  ONB.provIndex = Number($("#onbProv").value) || 0;
  fillOnbProvider();
});
$("#onbEnvRetry").addEventListener("click", async () => {
  await refreshOnbStatus();
  renderOnbEnv(ONB.status);
});
$("#onbPickFiles").addEventListener("click", async () => {
  const p = await pickDir("#onbFiles");
  if (p) toast("微信接收目录已选择，点「下一步」保存", "ok");
});
$("#onbTest").addEventListener("click", async () => {
  const p = ONB.providers[ONB.provIndex] || {};
  const key = $("#onbKey").value.trim() || p.api_key || "";
  const el = $("#onbTestResult");
  if (!key) { el.textContent = "请先填写 API Key"; el.className = "test-result bad"; return; }
  el.textContent = "测试中…"; el.className = "test-result";
  const r = await API.test_provider($("#onbBase").value.trim(), key);
  if (!r || !r.ok) {
    el.textContent = "连接失败：" + ((r && r.error) || "未知错误");
    el.className = "test-result bad";
    return;
  }
  const models = r.models || [];
  $("#onbModelList").innerHTML = models.map(
    (m) => `<option value="${esc(m)}"></option>`).join("");
  if (!$("#onbModel").value.trim() && models.length) $("#onbModel").value = models[0];
  el.textContent = `连接成功（${r.total || models.length} 个模型）`;
  el.className = "test-result ok";
});
$("#onbBack").addEventListener("click", () => onbStep(ONB.step - 1));
$("#onbNext").addEventListener("click", async () => {
  const btn = $("#onbNext");
  if (btn.disabled) return;
  btn.disabled = true;
  try {
    const s = ONB.step;
    if (s === 2 && !(await saveOnbModel())) return;
    if (s === 3 && !(await saveOnbIdentity())) return;
    if (s === 4 && !(await saveOnbFiles())) return;
    if (s < 5) onbStep(s + 1);
  } finally {
    btn.disabled = false;
  }
});
$("#onbDocs").addEventListener("click", openDocs);
$("#onbStart").addEventListener("click", () => finishOnboarding(true));
$("#onbLater").addEventListener("click", () => finishOnboarding(false));
$("#onbSkip").addEventListener("click", () => finishOnboarding(false));
$("#btnOpenDocs").addEventListener("click", openDocs);
$("#btnOnboarding").addEventListener("click", async () => {
  const r = await API.onboarding_reset();
  if (!r || !r.ok) { toast((r && r.error) || "重置引导失败", "err"); return; }
  await showOnboarding();
});
$("#setupBannerGo").addEventListener("click", () => {
  const kind = $("#setupBanner").dataset.kind;
  if (kind === "onboarding") showOnboarding();
  else goPage(kind === "models" ? "models" : "settings");
});

/* ---------- 天枢配置引导（任务桥首次开启 / 设置页手动重跑） ---------- */
async function openTianshuGuide() {
  openModal("modalTianshuGuide");
  $("#tgText").textContent = "";
  $("#tgStatus").textContent = "正在检测天枢 CLI…";
  $("#tgInstall").hidden = true;
  const r = await API.tianshu_guide_info();
  if (!r || !r.ok) {
    $("#tgStatus").textContent = (r && r.error) || "检测失败，稍后可从设置页重试";
    return;
  }
  $("#tgText").textContent = r.prompt_text || "";
  $("#tgInstall").hidden = r.installed;
  $("#tgStatus").textContent = r.installed
    ? (r.window
        ? "已检测到天枢 CLI 窗口。若已在窗口里选好模型、填好 API key，点「② 我已配置完成」。"
        : "天枢 CLI 可用。点「① 打开天枢 CLI」，在弹出的窗口里完成配置：选模型 → 输 API key → 回车确认。")
    : "未检测到天枢 CLI（需要 Node.js）。点「一键安装天枢」自动安装（npm install -g tianshu-tui）。";
}
$("#btnTianshuGuide").addEventListener("click", () => openTianshuGuide());
$("#tgLater").addEventListener("click", () => {
  closeModal();
  toast("任务桥保持开启；天枢没配好前，任务会被投递但没人处理——随时可到「设置 → 任务桥 → 天枢配置引导」补配置", "warn");
});

$("#tgInstall").addEventListener("click", async () => {
  const r = await API.install_tianshu();
  if (!r || !r.ok) { toast((r && r.error) || "安装启动失败", "err"); return; }
  $("#tgInstallBar").hidden = false;
  toast("开始安装天枢 CLI（首次约 1-3 分钟），进度见进度条", "ok");
});
$("#tgOpen").addEventListener("click", async () => {
  const r = await API.tianshu_guide_open();
  if (!r || !r.ok) {
    toast((r && r.detail) || "打开失败——请手动打开命令行输入 rivet 启动", "err");
    return;
  }
  toast("已打开天枢 CLI，完成配置后回来点「② 我已配置完成」", "ok");
});
$("#tgDone").addEventListener("click", async () => {
  const r = await API.tianshu_guide_finish();
  if (!r || !r.ok) { toast((r && r.error) || "检测失败", "err"); return; }
  closeModal();
  toast("天枢已就绪 ✓ 已开启全自动模式，任务桥可以放心使用", "ok");
  const cfg = await API.get_config();
  if (cfg.ok) { S.ui = cfg.ui; S.misc = cfg.misc; refreshSettings(); }
});

/* ============================================================
   后端推送接线
   ============================================================ */
onPush((evt, p) => {
  if (evt === "tick") {
    if (p.state) applyEngineState(p.state, p.paused, p.error);
    if (p.bus_error) {
      toast("引擎错误：" + String(p.bus_error).slice(0, 90), "err");
      /* 初始化失败（engine error 态）弹诊断弹窗；运行中的主循环异常只 toast
         （bus 事件一次性，弹窗只在 init 失败那一次出现，不会每秒重复） */
      if (S.engineState === "error") showEngineError();
    }
    if (p.log?.length) appendLogLines(p.log);
  } else if (evt === "balances") {
    renderBalances(p.rows);
  } else if (evt === "install") {
    onInstallProgress(p);
  } else if (evt === "prompt") {
    toast(p.message || (p.ok ? "已发送" : "发送失败"), p.ok ? "ok" : "err");
  } else if (evt === "voice_test") {
    onVoiceTestResult(p);
  } else if (evt === "sticker_retag") {
    onStickerRetag(p);
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
  S.docsUrl = cfg.docs_url || DEFAULT_DOCS_URL;
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
  if (S.envWechat && !S.envWechat.ok) {
    // 保险：微信没开/没登录完成时主动提醒（环境卡的红点太静默）
    toast("没检测到微信窗口——请先打开并登录电脑端微信，小漓才能开始工作", "warn");
  }
  if (!cfg.onboarding_done) showOnboarding();
  else refreshSetupBanner();
})();
