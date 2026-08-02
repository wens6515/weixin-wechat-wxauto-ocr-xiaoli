# 小漓项目交接总结

> 写给下一个接手 agent 的项目现状速览。所有数字来自真实验证记录（2026-08-02）。
> 项目规则见 `AGENTS.md`；本文件是状态快照，动手前先读代码确认。

## 项目是什么

小漓 = 微信 AI 助手桌面应用（Windows）。通过 wxauto4 操作微信 PC 客户端收发消息，LLM 直连官方 API（OpenAI 兼容协议），并内置「天枢任务桥」：把微信里收到的任务请求投递给天枢 agent（运行在 `D:\工作间`，任务目录协议 `D:\工作间\wxauto\<task_id>\task.json` + `result.json` 回传）。

目标用户是**普通小白**：只需要按引导配置 API key、写角色卡，其余一键完成。

## 快速上手（命令）

```bat
:: 桌面版（推荐）
启动小漓.bat                              :: pythonw 无终端启动源码
dist\小漓\小漓.exe                        :: 打包版（16:06 最新，含全部修复）

:: 控制台模式（开发用）
.venv\Scripts\python.exe xiaoli_desktop\xiaoli_bot.py --run

:: 验证
.venv\Scripts\python.exe xiaoli_desktop\xiaoli_bot.py --test        :: 自检 59 通过
cd xiaoli_desktop && ..\.venv\Scripts\python.exe -m unittest discover -s tests   :: 单测 59 通过
.venv\Scripts\python.exe -m compileall -q xiaoli_desktop

:: 打包（PyInstaller --windowed 无终端）
.venv\Scripts\pyinstaller.exe --noconfirm --windowed --name 小漓 xiaoli_desktop\xiaoli_gui.py
```

## 目录结构

```
D:\AI\小漓\
├── xiaoli_desktop\                ← 软件本体（git 跟踪）
│   ├── xiaoli_gui.py              ← GUI 入口（单实例锁 + 启动即空闲）
│   ├── wechat_bot.py              ← WeChatBot 引擎（微信/记忆/去重/LLM/图片/文件）
│   ├── xiaoli_bot.py              ← AgentBot（继承+天枢任务桥）+ 自检 T1-T15 + 窗口/剪贴板工具
│   ├── xiaoli_app\
│   │   ├── config_store.py        ← providers 路由表 + 活跃角色卡 + 旧字段投影 + 迁移
│   │   ├── card_store.py          ← 角色卡 CRUD/导入导出（卡内禁存 key）
│   │   ├── engine.py              ← EngineThread 状态机 + EngineBus 事件队列
│   │   ├── setup.py               ← 环境检测 + 天枢一键安装（zip 流式下载+zip-slip 防护）
│   │   └── ui\                    ← PySide6：tray / main_window（6 Tab）/ pages（HomePage 状态机等）/ APP_QSS
│   └── tests\                     ← 59 个单测（config 10 + card 14 + engine 13 + setup 14 + ui 6 + windowed 2）
├── 启动小漓.bat                   ← pythonw 启动（无终端）
├── config.json / memory.json / processed_ids.json / bot.log   ← 运行时数据（gitignore）
├── calibrate_click.py             ← 图片点击偏移校准工具（独立脚本）
├── .venv\  dist\  build\          ← 环境与打包产物（gitignore）
└── .rivet\plans\                  ← 两期计划（EXECUTED）
```

## 核心架构

```
微信 PC ←wxauto4→ AgentBot（引擎，子线程）──bus 事件──→ PySide6 UI（托盘+6 Tab）
                    │
    config_store（providers+角色卡投影）→ 引擎读到的 cfg 与旧版完全同构
    setup（环境检测/一键安装天枢）      → 首页卡片 + 下载进度
    天枢任务桥（D:\工作间\wxauto 目录协议）→ 投递/回传/成果排除
```

- **状态机**（engine.py）：`idle → initializing → initialized → running ⇄ paused → stopped`；UI 首页大按钮随状态切换（初始化/启动 bot/暂停运行）
- **投影机制**（config_store）：config.json 主存 `providers`（每 provider 独立 base_url+key）+ `active_card_id`；启动时按活跃卡投影出旧字段（ai_api_url 等）——引擎代码零改动读旧字段，角色切换热更新
- **启动即空闲**：GUI 打开不连微信不加载记忆，全部由「初始化」按钮触发（连微信/加载记忆/去重/成果登记），初始化后自动打开并发送首轮提示词给天枢

## 关键设计决策（勿轻易推翻）

1. **引擎逻辑基本不动**：微信自动化的坑（图片点击偏移、文件显示名定位、成果排除登记、任务监听暂停/恢复）都在 wechat_bot/xiaoli_bot 里经过实战验证，新功能一律加在 xiaoli_app 层
2. **角色卡不存 key**：只引用 provider id，导出分享不泄密
3. **wxauto4 打进 exe**：小白用户无需装 Python/依赖；只有源码开发模式需要 .venv
4. **天枢一键安装**：从 `https://codeload.github.com/huiliyi37/Tianshu-Tui/zip/refs/heads/main` 流式下载 zip → 安全解压（拒绝 `../` 路径穿越）→ 装到 `%USERPROFILE%\Tianshu`（默认），检测标准：`tianshu-desktop.exe` 存在（显式配置目录优先）+ 窗口标题含「天枢/Tianshu」
5. **QSS 必须显式设文字色**：用户系统是深色模式（palette.WindowText=白色），不设色 = 白字白底隐形（已修复，教训记录）

## 已验证状态（2026-08-02 16:06）

- 自检 59 通过、单测 59 通过、compileall 无错
- PyInstaller `--windowed` 打包成功（exe 16:06，含深色修复 + 单实例锁 + 关窗三选）
- 已修 bug：windowed 模式 sys.stdout=None 崩溃（getattr 防护）、深色模式文字隐形（QSS color）
- git 主线 10 个提交（3982cb3 → abef4bb），工作区 clean

## 遗留与下一步（下一 agent 的起点）

1. **用户桌面实测**：双击新 exe 走全流程——首页「初始化」→「启动 bot」→「暂停运行」；关窗三选弹框；深色模式显示
2. **首轮提示词链路真实验证**（未实测，需天枢运行）：初始化后读 `D:\工作间\首轮提示词.txt` → 系统默认应用打开 → 找天枢窗口（`tianshu_window_title` 或窗口名匹配）→ `send_trigger_to_window` 粘贴发送；失败有「重试发送」按钮
3. **一键安装天枢下载流程未实测**（本机已装不会触发）：测试方法——把 config.json 的 `tianshu_install_dir` 改成错误路径，首页会出现「一键安装天枢」按钮
4. **用户 config.json 仍是 Cherry Studio 网关**（`http://127.0.0.1:23333` + `cs-sk-` key）：需在「模型」页配置官方 provider（DeepSeek：`https://api.deepseek.com/v1/chat/completions`；智谱：`https://open.bigmodel.cn/api/paas/v4/chat/completions`）并在角色卡页切换模型引用——**这是"抛开 Cherry Studio"的最后一步**
5. **安全**：`cs-sk-` API key 曾出现在 git 历史（wechat_bot.py 旧默认值）——建议用户在官方控制台轮换
6. **测试缺口**：HomePage 的一键安装/首轮提示词流程只有 mock 单测；closeEvent 三选弹框未自动化（offscreen 会阻塞）；这两个建议补 e2e 或留手工清单
7. calibrate_click.py（图片点击偏移校准）独立于桌面版，未集成

## 给接手 agent 的坑（血泪教训）

- **venv 是根目录 `.venv`**（不是 wxauto-mcp 的 wechat4_env）；wxauto4 从 PyPI 可复装（==41.1.2）
- **git 提交用 `deliver_task commit=true`**；新建文件需先 `adopt` 再提交（deliver 的 pathspec 机制不认未归属文件）；deliver 机制失败时先核实 index 归属再绕行裸 commit
- **改 wechat_bot/xiaoli_bot 前先跑 `--test` 基线**（59 通过），微信自动化行为脆弱
- **windowed 环境 sys.stdout 是 None**：任何无控制台代码路径都要 getattr 防护（有过崩溃教训）
- **深色系统主题**：新 UI 控件必须显式 QSS 设色，否则白字隐形
- **多会话共享工作区**：杀进程/清场类操作先确认匹配范围（曾误杀含项目路径的 bash 进程）
- **测试纪律**：bugfix 先 RED 复现（子进程隔离，避免模块缓存假绿）再修
