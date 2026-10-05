# -*- coding: utf-8 -*-
"""webbridge 冒烟测试：临时目录假配置，验证方法面可用（不连微信、不碰真实 config）。"""
import json
import os
import sys
import tempfile

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app import config_store
from xiaoli_app.webbridge import (BridgeApi,
                                  count_active_triggers, trigger_state_text,
                                  app_base_dir, list_wallpapers)
from xiaoli_web import WebContext

fails = []


def check(name, cond, detail=""):
    print(("PASS " if cond else "FAIL ") + name + (f" | {detail}" if detail else ""))
    if not cond:
        fails.append(name)


with tempfile.TemporaryDirectory() as td:
    _orig_cwd = os.getcwd()
    os.chdir(td)          # 相对路径（memory.json 等）落进临时目录
    cfg_path = os.path.join(td, "config.json")
    cards_dir = os.path.join(td, "cards")
    os.makedirs(cards_dir, exist_ok=True)
    cfg = config_store.load_config_store(cfg_path, cards_dir)
    ctx = WebContext(cfg_path, cards_dir)
    ctx.cfg = cfg
    ctx.bus = None
    api = BridgeApi(ctx)

    # 配置面
    r = api.get_config()
    check("get_config", r.get("ok") and "ui" in r and "misc" in r and "voice" in r)
    check("first_run_needed", r.get("first_run_needed") is True)
    r = api.save_config({"theme": "tokyonight", "bot_nickname": "小漓",
                         "web_proxy": "", "not_allowed_key": 1})
    check("save_config 白名单", r.get("ok") and "not_allowed_key" not in r.get("applied", []))
    check("save_config 落盘", json.load(open(cfg_path, encoding="utf-8")).get("theme") == "tokyonight")
    r = api.set_theme("neon")
    check("set_theme", r.get("ok") and r.get("wallpaper_applied", "").endswith("赛博朋克-壁纸.jpg"))

    # 壁纸面（真实仓库根壁纸目录）
    wps = list_wallpapers()
    check("list_wallpapers 扫描", len(wps) >= 8, f"{len(wps)} 张")
    r = api.list_wallpapers()
    check("list_wallpapers api", r.get("ok") and len(r.get("items", [])) == len(wps))
    check("thumb dataURI", (r["items"][0]["thumb"] or "").startswith("data:image/jpeg;base64,"))
    r = api.wallpaper_data("小漓主题.jpg")
    check("wallpaper_data 裸名解析", r.get("ok") and (r.get("data") or "").startswith("data:image/jpeg;base64,"))
    r = api.set_wallpaper("")
    check("set_wallpaper 清除", r.get("ok") and cfg.get("wallpaper_path") == "")

    # 卡片面（save_card 内 reproject_and_push 会替换 ctx.cfg 引用，
    # 断言一律走 ctx.cfg 动态读取）
    r = api.save_card({"name": "测试卡", "system_prompt": "你是测试。", "emoji": "T"})
    check("save_card 新建", r.get("ok") and "_" in r["card"]["id"])
    cid = r["card"]["id"]
    r = api.activate_card(cid)
    check("activate_card", r.get("ok") and ctx.cfg.get("active_card_id") == cid)
    r = api.list_cards()
    check("list_cards", r.get("ok") and any(c["id"] == cid for c in r["cards"]))
    r = api.delete_card(cid)
    check("delete_card 活跃卡拒绝", r.get("ok") is False)
    api.save_card({"id": cid + "b", "name": "备用", "system_prompt": "x"})
    api.activate_card(cid + "b")
    r = api.delete_card(cid)
    check("delete_card 非活跃", r.get("ok") is True)

    # Provider 面
    r = api.save_providers([{"id": "deepseek", "name": "DeepSeek",
                             "base_url": "https://api.deepseek.com/v1/chat/completions",
                             "api_key": "", "models": "deepseek:deepseek-chat, deepseek:deepseek-reasoner"}])
    check("save_providers", r.get("ok") and len(r["providers"][0]["models"]) == 2)
    r = api.get_providers()
    check("get_providers", r.get("ok") and len(r["providers"]) == 1)

    # 记忆面（文件通道）
    r = api.memory_overview()
    check("memory_overview 空库", r.get("ok") and r.get("rows") == [])
    r = api.memory_detail("某聊天")
    check("memory_detail 空库", r.get("ok"))

    # 触发器面
    r = api.list_triggers()
    check("list_triggers", r.get("ok") and isinstance(r.get("rows"), list))
    r = api.add_trigger("某群", "测试提醒", __import__("time").time() + 600, "once")
    check("add_trigger", r.get("ok") and r["item"]["kind"] == "time")
    rid = r["item"]["id"]
    check("trigger_state_text 待触发", trigger_state_text(r["item"]) == "待触发")
    api.set_trigger_enabled(rid, False)
    check("trigger_state_text 已暂停", trigger_state_text(api.list_triggers()["rows"][0]) == "已暂停")
    check("count_active_triggers", count_active_triggers(api.list_triggers()["rows"]) == 0)
    check("delete_trigger", api.delete_trigger(rid).get("ok") is True)

    # 用量面（只读聚合；UsageStore 默认路径为用户目录真实库）
    r = api.get_usage()
    check("get_usage", r.get("ok") and "cards" in r and "by_model" in r and "days" in r
          and len(r["days"]) == 7)

    # 首启默认
    r = api.first_run_defaults()
    check("first_run_defaults", r.get("ok") and r.get("bot_nickname") == "小漓")
    r = api.save_first_run(os.path.join(td, "tasks"), os.path.join(td, "files"),
                           "memory.json", "小漓")
    check("save_first_run", r.get("ok") and ctx.cfg.get("tasks_dir") == os.path.join(td, "tasks"))
    r = api.set_tasks_dir(os.path.join(td, "tasks2"), remove_old=True)
    check("set_tasks_dir", r.get("ok") and ctx.cfg.get("tasks_dir").endswith("tasks2"))

    # 日志 tail
    r = api.tail_log()
    check("tail_log 返回形状", r.get("ok") and isinstance(r.get("lines"), list))

    os.chdir(_orig_cwd)   # 必须在 with 内退出临时目录，否则 Windows 清理失败

check("app_base_dir", os.path.isfile(os.path.join(app_base_dir(), "xiaoli_bot.py")))

print("\n" + ("全部通过 ✓" if not fails else f"失败 {len(fails)} 项: {fails}"))
sys.exit(1 if fails else 0)
