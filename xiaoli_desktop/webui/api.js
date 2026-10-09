/* 小漓 Web 前端 · 桥封装层。
   pywebview 的 js_api 在页面加载完成后才异步注入——启动时等待桥就绪
   （bridgeReady），等待失败才降级 mock（浏览器开发预览）。
   mock 模式必须挂醒目横幅：假数据混进真窗口 = 严重误导（历史事故）。 */
"use strict";

let IS_REAL = false;   // 由 app.js 启动流程在 bridgeReady 后赋值

/* ---------- 等待 pywebview 桥注入（官方 pywebviewready 事件 + 轮询兜底） ---------- */
const bridgeReady = new Promise((resolve) => {
  let settled = false;
  const check = () => {
    if (settled) return true;
    if (window.pywebview && window.pywebview.api &&
        typeof window.pywebview.api.get_status === "function") {
      settled = true;
      IS_REAL = true;
      resolve(true);
      return true;
    }
    return false;
  };
  window.addEventListener("pywebviewready", () => { if (check()) return true; });
  const t0 = Date.now();
  const timer = setInterval(() => {
    if (check() || Date.now() - t0 > 5000) {
      clearInterval(timer);
      if (!settled) { settled = true; IS_REAL = false; resolve(false); }
    }
  }, 60);
});

function call(name, ...args) {
  if (IS_REAL) {
    return Promise.resolve()
      .then(() => window.pywebview.api[name](...args))
      .then((r) => (typeof r === "string" ? JSON.parse(r) : r));
  }
  return Promise.resolve().then(() => {
    const fn = MOCK[name];
    if (!fn) return { ok: false, error: "mock 未实现: " + name };
    return fn(...args);
  });
}

const API = new Proxy({}, {
  get: (_t, name) => (...args) => call(String(name), ...args),
});

/* ---------- 推送事件统一入口（后端 evaluate_js 注入） ---------- */
const PUSH_LISTENERS = [];
window.__push = function (evt, payload) {
  PUSH_LISTENERS.forEach((fn) => {
    try { fn(evt, payload || {}); } catch (e) { console.error(e); }
  });
};
function onPush(fn) { PUSH_LISTENERS.push(fn); }

/* ============================================================
   Mock 数据源（桥等待失败的降级演示，数据全部虚构）
   ============================================================ */
const MOCK = (() => {
  const now = () => Date.now() / 1000;
  const state = {
    engine: "running", paused: false,
    cfgUI: { theme: "abyss", follow_system: false, wallpaper_path: "assets/wallpaper.jpg",
             card_opacity: null, panel_opacity: null, wall_opacity: null, blur_level: null,
             font_scale: "medium" },
    misc: { memory_deep_enabled: true, memory_compress_enabled: false,
            memory_keep_recent: 30, memory_compress_batch: 30, memory_important_max: 20,
            memory_compress_model: "", file_storage_path: "C:\\演示\\Documents\\xwechat_files",
            tasks_dir: "D:\\演示\\wxauto", tianshu_workdir: "D:\\演示",
            web_proxy: "", first_prompt_path: "", bot_nickname: "小漓", start_paused: true,
            web_search_enabled: true, task_enabled: true, state_watch_enabled: false,
            model_trigger_manage: "off", sticker_mode: "off",
            segment_wait_enabled: true, segment_wait_seconds: 2,
            segment_jitter_enabled: false, segment_jitter_min: 1.5, segment_jitter_max: 3,
            tianshu_guided: false },
    voice: { voice_mode: "off", tts_endpoint: "http://演示:9880/tts",
             tts_timeout_seconds: 120, voice_max_seconds: 55,
             voice_profiles: [{ id: "p-demo", name: "演示音色",
                                refs: { "通用": { ref_audio_path: "演示/base.wav", prompt_text: "你好呀" } } }],
             active_voice_profile_id: "p-demo" },
    overrides: {},
    providers: [
      { id: "deepseek", name: "DeepSeek（演示）", base_url: "https://api.deepseek.com/v1",
        api_key: "", models: ["deepseek:deepseek-chat", "deepseek:deepseek-reasoner"] },
    ],
    active_card_id: "xiaoli",
    cards: [
      { id: "xiaoli", name: "小漓（演示卡）", emoji: "🐟", nickname: "小漓",
        system_prompt: "这是演示数据，不是你的真实角色卡。", chat_provider: "deepseek",
        chat_model: "deepseek:deepseek-chat", temperature: 0.7, top_p: 0.9, max_history: 1000 },
    ],
    wallpapers: ["示例壁纸.jpg"],
    memory: {
      "示例会话 · 阿白": { recent: 8, deep: 12, important: 2, index: 3 },
      "示例群聊 · 冒险岛": { recent: 5, deep: 6, important: 1, index: 2 },
    },
    triggers: [
      { id: "t1", kind: "time", chat: "示例会话 · 阿白", content: "（演示）提醒取快递",
        fire_at: now() + 2400, repeat: "once", enabled: true, last_fired: null, missed: false },
    ],
    usage: {
      cards: { today_calls: 0, today_tokens: 0, today_cache: null,
               today_cost: 0, tokens_30d: 0, fail_30d: 0, cost_30d: 0 },
      by_model: [],
      days: ["D-6", "D-5", "D-4", "D-3", "D-2", "D-1", "今天"],
      day_model: {},
    },
    envReport: {
      wechat: { ok: true, detail: "（演示）检测到微信窗口「微信」" },
      tianshu: { ok: true, detail: "（演示）CLI(rivet) ✓" },
      first_prompt: { ok: true, detail: "（演示）内置模板" },
    },
  };

  const LOG_POOL = [
    "（演示日志）真窗口不会看到这行——看到说明后端桥未接通",
    "（演示日志）示例：🤖 → [示例会话]: 收到～",
  ];

  const M = {
    get_status: () => ({ ok: true, state: state.engine, error: null,
      bot_ready: state.engine !== "idle", paused: state.paused,
      version: "演示", trigger_active: 0 }),
    engine_action: (a) => {
      if (a === "initialize") state.engine = "initialized";
      else if (a === "start") { state.engine = "running"; state.paused = false; }
      else if (a === "pause") { state.engine = "paused"; state.paused = true; }
      else if (a === "resume") { state.engine = "running"; state.paused = false; }
      return { ok: true, state: state.engine };
    },
    tail_log: () => ({ ok: true, lines: LOG_POOL }),
    voice_ready: () => ({ ok: true, ready: true, missing: [] }),
    voice_selftest: () => ({ ok: true, started: true }),
    sticker_info: () => ({ ok: true, dir: "D:\\演示\\表情包", count: 2,
      items: [{ file: "生气1.png", desc: "气到冒烟", tags: ["生气"], thumb: "" },
              { file: "开心.gif", desc: "开心到飞起", tags: ["开心"], thumb: "" }] }),
    sticker_set_desc: () => ({ ok: true }),
    sticker_add_files: () => ({ ok: true, added: 0, skipped: 0, canceled: true }),
    sticker_add_folder: () => ({ ok: true, added: 0, skipped: 0, canceled: true }),
    sticker_open_dir: () => ({ ok: true }),
    sticker_preview: () => ({ ok: true, count: 2, tokens: 86,
      text: "（演示）你可以发送表情包（调 send_sticker……）：\n生气1.png｜气到冒烟｜生气\n开心.gif｜开心到飞起｜开心" }),
    sticker_retag: () => {
      setTimeout(() => window.__push("sticker_retag",
        { ok: true, done: 2, total: 2, indexed: 2, message: "打标完成：2/2" }), 500);
      return { ok: true, started: true };
    },
    set_nickname: (v) => ({ ok: true, nickname: (v || "小漓").trim(),
                            card_written: true }),
    tianshu_guide_info: () => ({ ok: true, installed: true,
      detail: "（演示）C:\\npm\\rivet.cmd", window: null, guided: false,
      prompt_text: "（演示）已为您打开天枢 CLI（命令行窗口）。\n\n请在弹出的窗口中完成配置：\n  ① 选择模型\n  ② 输入 API key\n  ③ 按回车确认" }),
    tianshu_guide_open: () => ({ ok: true, detail: "（演示）已启动" }),
    tianshu_guide_finish: () => ({ ok: true }),
    list_tasks: () => ({ ok: true, rows: [], waiting: 0, done: 0, archived: 0,
                         tasks_dir: "D:\\演示\\wxauto" }),
    delete_task: () => ({ ok: true }),
    get_config: () => ({ ok: true, ui: { ...state.cfgUI }, misc: { ...state.misc },
      voice: JSON.parse(JSON.stringify(state.voice)), overrides: JSON.parse(JSON.stringify(state.overrides)),
      active_card_id: state.active_card_id, first_run_needed: false,
      cfg_path: "C:\\演示\\config.json" }),
    save_config: (patch) => ({ ok: true, applied: Object.keys(patch || {}), bot_hot: [] }),
    set_theme: (key) => ({ ok: true, theme: key, wallpaper_applied: "" }),
    list_wallpapers: () => ({
      ok: true,
      items: state.wallpapers.map((n) => ({ name: n.replace(/\.[a-z]+$/i, ""), path: n, thumb: "assets/wallpaper.jpg" })),
      custom: null, current: state.cfgUI.wallpaper_path,
      current_resolved: state.cfgUI.wallpaper_path,
      current_data: state.cfgUI.wallpaper_path ? "assets/wallpaper.jpg" : null }),
    set_wallpaper: (p) => { state.cfgUI.wallpaper_path = p; return { ok: true }; },
    wallpaper_data: () => ({ ok: true, path: "assets/wallpaper.jpg", data: "assets/wallpaper.jpg" }),
    import_wallpaper: () => ({ ok: false, error: "演示模式不支持文件对话框" }),
    list_triggers: () => ({
      ok: true,
      rows: (() => {
        const n = Date.now() / 1000;
        const terminalStates = ["已触发", "已错过", "已达成", "已到期", "已失效"];
        return JSON.parse(JSON.stringify(state.triggers)).map((r) => {
          if (r.kind === "condition") {
            r.state_text = r.done ? { met: "已达成", expired: "已到期", dead: "已失效" }[r.done]
              : (r.enabled ? "监视中" : "已暂停");
          } else {
            r.state_text = !r.enabled ? (r.missed ? "已错过" : (r.last_fired ? "已触发" : "已暂停"))
              : ((r.fire_at || 0) <= n ? "触发中" : "待触发");
          }
          r.terminal = terminalStates.includes(r.state_text);
          return r;
        });
      })(),
      active: 0,
    }),
    add_trigger: () => ({ ok: true }), add_condition: () => ({ ok: true }),
    set_trigger_enabled: () => ({ ok: true }), delete_trigger: () => ({ ok: true }),
    list_cards: () => ({ ok: true, cards: JSON.parse(JSON.stringify(state.cards)),
      active_id: state.active_card_id }),
    get_card: (id) => ({ ok: true, card: JSON.parse(JSON.stringify(state.cards.find((c) => c.id === id))) }),
    save_card: (card) => ({ ok: true, card, active_card_id: state.active_card_id }),
    delete_card: () => ({ ok: true }), duplicate_card: () => ({ ok: true }),
    activate_card: () => ({ ok: true, hot_applied: true }),
    export_card: (id) => ({ ok: true, card: state.cards.find((c) => c.id === id) }),
    import_card: () => ({ ok: false, error: "演示模式不支持" }),
    get_providers: () => ({ ok: true, providers: JSON.parse(JSON.stringify(state.providers)) }),
    save_providers: (providers) => ({ ok: true, providers }),
    test_provider: () => ({ ok: true, models: ["demo-model-a"], total: 1 }),
    memory_overview: () => ({
      ok: true,
      rows: Object.entries(state.memory).map(([chat, c]) => ({ chat, ...c, card_name: null })),
    }),
    memory_detail: (chat) => ({ ok: true, chat,
      recent: [...Array(4)].map((_, i) => ({ role: i % 2 ? "assistant" : "user",
        content: `（演示）第 ${4 - i} 条消息`, time: "10-05 12:0" + i })),
      important: [{ content: "（演示）重要记忆示例" }],
      index: [{ kw: ["演示"], mem: "（演示）索引示例" }],
      deep_total: 6, deep: [...Array(3)].map((_, i) => ({ line_no: 10 + i,
        role: i % 2 ? "assistant" : "user", content: `（演示）深层存档第 ${i + 1} 条`, time: "09-2" + i + " 14:00" })),
      deep_matched: 3 }),
    memory_delete: () => ({ ok: true }), memory_clear_chat: () => ({ ok: true }),
    memory_clear_all: () => ({ ok: true }),
    memory_export: (chat, mode) => ({ ok: true, filename: chat + "-记忆." + mode, content: "演示数据" }),
    memory_bind_card: () => ({ ok: true }),
    get_usage: () => ({ ok: true, ...JSON.parse(JSON.stringify(state.usage)) }),
    refresh_balances: () => {
      setTimeout(() => window.__push("balances", { rows: [
        { name: "DeepSeek（演示）", supported: true, ok: false, error: "演示数据，无真实余额" }] }), 500);
      return { ok: true, started: true };
    },
    clear_usage: () => ({ ok: true }),
    check_env: () => ({ ok: true, report: JSON.parse(JSON.stringify(state.envReport)) }),
    check_update: () => ({ ok: true, current: "演示", latest: "演示", newer: false, url: "" }),
    install_tianshu: () => ({ ok: true, started: true }),
    send_first_prompt: () => {
      setTimeout(() => window.__push("prompt", { ok: true, message: "（演示）已发送" }), 500);
      return { ok: true, started: true };
    },
    first_run_defaults: () => ({ ok: true, tasks_dir: "", file_storage_path: "",
      memory_file: "memory.json", bot_nickname: "小漓" }),
    save_first_run: () => ({ ok: true }),
    set_tasks_dir: () => ({ ok: true }),
    pick_folder: () => ({ ok: false, error: "演示模式不支持" }),
    pick_file: () => ({ ok: false, error: "演示模式不支持" }),
    open_external: () => ({ ok: true }),
    win_min: () => ({ ok: true }), win_max_toggle: () => ({ ok: true }),
    win_hide: () => ({ ok: true }), quit_app: () => ({ ok: true }),
  };
  // 便于外部填充 day_model（原写法在 IIFE 内自引用 MOCK，TDZ 让整个
  // mock 对象初始化失败——浏览器开发预览全挂，真窗口 IS_REAL 通道不受影响）
  M.usage = state.usage;
  state.usage.day_model = {};
  return M;
})();

/* ---------- mock 降级：醒目横幅 + 模拟日志流（仅在桥等待失败时启用） ---------- */
function enableMock() {
  const banner = document.createElement("div");
  banner.id = "mockBanner";
  banner.innerHTML = "<b>演示模式</b> — 当前界面未连接到小漓后端，所有数据均为<b>虚构示例</b>" +
    "（不是你的真实配置 / 聊天记录 / API Key）。此模式仅供浏览器开发预览。";
  document.body.prepend(banner);
  let li = 0;
  setInterval(() => {
    const pool = MOCK.tail_log().lines;
    window.__push("tick", { state: "running", error: null, paused: false,
      log: [pool[li++ % pool.length]] });
  }, 3400);
}
