# 🐟 小漓（Xiaoli）— 微信 AI 机器人 · 视觉版

小漓是一个运行在 Windows 上的微信 AI 机器人：自动回复微信消息（聊天/提问）、识别图片与文件，并把复杂任务投递给 AI 代理（天枢 CLI）处理，处理完成后自动把成果文件回传微信。

> **v2.0.0「视觉版」** · 适配微信 PC 4.1.12+ · 弃用 wxauto4（UIA 通道在 4.1.12 结构性失效），全面迁移到 **OCR 视觉方案**（PrintWindow 截图 + RapidOCR + 像素检测）。

---

## 功能一览

- **微信自动回复**：监听微信消息，用 LLM（默认 DeepSeek）生成回复；私聊/群聊语境区分，群聊仅响应 `@小漓`
- **图片识别**：收到图片自动调用视觉模型（默认智谱 GLM-4V）详细描述内容
- **文件消息处理**：定位微信接收目录中的文件并处理（Word/Excel/PDF 等提取文字）
- **任务桥**：识别任务型请求（如"根据文档做一个网站"）→ 投递到任务目录 → 唤起天枢 CLI 处理 → 轮询回传文本 + 成果文件 → 自动归档
- **对话记忆**：多聊天历史持久化（memory.json）
- **未读红圈驱动**：列表区像素检测未读角标（品牌红 #FA5151），只处理有新消息的会话——等效 wxauto4 的"新消息→处理"事件语义
- **变化检测先行**：截图后本地像素差异检测（毫秒级），无变化跳过识别，不每轮调视觉 API

## 消息处理主循环

```mermaid
flowchart TD
    A[iter_unread_with_type 红圈检测] -->|无红圈| A
    A[iter_unread_with_type 红圈检测] -->|有未读会话 name,type| B[_switch_chat 防重复点击]
    B --> C[判私聊/群聊 _current_is_group]
    C --> |私聊| D[截图 + 气泡色/气泡框/媒体矩形]
    C --> |群聊| R[截图 + 气泡色/气泡框/媒体矩形]
    R --> S[定位 bot 最后回复 y + ocr判断是否有@消息]
    S -->|无@消息| A
    S -->|有@消息| F
    D --> E[定位 bot 最后回复 y]
    E --> F[窗口 = y 在回复之后的对方消息]
    F -->|窗口空| A
    F -->|有文字、无图片、无文件| G[OCR提取文字消息]
    F -->|有图片或文件| H[sleep 10s防话没说完，重新截图分析]
    G --> I[LLM判断是否为任务消息]
    I -->|不是任务| J[发送给ai]
    I -->|是任务| K[投递任务桥]
    H -->L[重新截图 + 定位 bot 最后回复 y]
    L -->|有文字、有图片或文件| M[OCR提取文字，LLM判断任务]
    L -->|无文字、有图片| N[图片→多模态ai→文字描述]
    L -->|有文件| O[文件流程: 回复收到 + sender 关联] -->|收到sender指令| E
    M -->|不是任务| P[图片/文件转文字整合发送给ai]
    M -->|是任务| Q[投递任务桥: 文字+图片+文件全投]
    J & K & N & P & Q --> T[bot回复]
    T -->A
```

> 气泡色/媒体矩形分析无法区分视频/表情/图片消息，统一按图片处理；群聊只有 `@小漓` 才响应；文件消息「回复收到 + 不停摆」，该发送者后续文字指令自动关联待处理文件。

## 快速开始（源码运行）

**前置**：Windows 10+、Python 3.12、已登录的**微信 PC 4.1.12+**（旧版微信 UIA 通道已不兼容）

```bash
# 1. 创建虚拟环境并安装依赖
cd xiaoli_desktop
python -m venv .venv
.venv\Scripts\pip install -r requirements.txt

# 2. 首次使用：圈选 OCR 区域（列表区/消息区/标题区）
python tools\pick_ocr_region.py        # 图形框选，结果写入 wx_ocr_region.json

# 3. （可选）固定微信窗口位置尺寸——视觉坐标依赖窗口稳定
python tools\fix_window.py 50 50 900 920

# 4. 启动（CLI 模式，交互式控制台）
.venv\Scripts\python xiaoli_bot.py --run

# 5. 自检（不连微信，验证环境与核心逻辑）
.venv\Scripts\python xiaoli_bot.py --test
```

> 首次运行会生成 `config.json`（含 `ai_api_key`，已被 .gitignore 排除，不会提交）。建议用 `tools\fix_window.py` 固定微信窗口后再使用——视觉坐标依赖窗口位置稳定。

## 架构

```
xiaoli_desktop/
├── xiaoli_bot.py            # CLI 入口 + 任务桥 + 消息主循环（--run / --test）
├── wechat_bot.py            # 基础微信机器人（监听/回复/图片/文件/记忆）
├── wx_backend/              # 微信后端协议层（v2.0.0 核心）
│   ├── __init__.py          # 后端注册表 + create_backend("auto")
│   ├── models.py            # WeChatMessage / MessageType
│   └── visual_backend.py    # 视觉后端：截图/OCR/红圈检测/气泡定位/发送
├── xiaoli_app/
│   └── config_store.py      # 配置加载/迁移 + 模型清单 + 人设默认（后端共用）
├── tests/                   # unittest 测试套件（后端 10 个测试文件）
├── wx_ocr_region.json       # OCR 三区域标定（pick_ocr_region 生成）
├── wx_window.json           # 微信窗口固定配置（fix_window 生成）
└── requirements.txt         # Python 3.12 依赖
tools/                       # 配套标定/调试工具
├── pick_ocr_region.py       # OCR 区域图形框选（列表→消息→标题）
├── fix_window.py            # 微信窗口位置固定
├── calibrate_input_box.py   # 输入框坐标标定
├── test_read_messages.py    # 消息读取链路调试
└── cdp_probe.py             # CDP/accessibility 通道验证探针
```

## 配置说明（config.json）

首次运行自动生成，字段含义：

| 字段 | 默认值 | 说明 |
|---|---|---|
| `ai_api_url` | `https://api.deepseek.com/v1/chat/completions` | 聊天 LLM 接口（OpenAI 兼容） |
| `ai_api_key` | 空 | **API Key（勿提交）** |
| `chat_model` | `deepseek:deepseek-v4-flash` | 聊天模型（`provider:model` 格式） |
| `vision_model` | `zhipu:glm-4.6v` | 视觉模型 |
| `system_prompt` | 通用人设 | 小漓人设（发布版不含个人信息，可自定义） |
| `bot_nickname` | 小漓 | 机器人昵称 |
| `tasks_dir` | `%USERPROFILE%\小漓\wxauto` | 任务桥工作目录 |
| `tianshu_window_title` | 空 | 天枢 CLI 窗口标题（空 = 启动时交互选择） |
| `tianshu_trigger_command` | `开始处理` | 唤起天枢后发送的触发指令 |
| `tianshu_poll_interval` | 5 | 任务结果轮询间隔（秒） |
| `file_send_method` | `clipboard` | 成果文件发送方式：clipboard（剪贴板，v2.0.0 唯一方式） |
| `max_history` | 1000 | 单聊天保留的最大历史条数 |
| `cooldown` | 3 | 回复冷却（秒） |
| `start_paused` | true | 启动时是否暂停自动回复 |

控制台可用命令：`pause` / `resume` / `model [名称]` / `vision-model [名称]` / `chat-temp <0~2>` / `chat-top-p <0~1>` / `vision-temp <0~2>` / `tianshu-window` / `task-status` / `clear [聊天ID]` / `del <聊天ID> <序号...>` / `memory <聊天ID>` / `status` / `quit` / `help`

## 发布版安装包（桌面 GUI）

v2.0.0 提供完整桌面端安装包（PySide6 图形界面 + 12 套主题 + 壁纸库 + 托盘常驻），从 [Releases](https://github.com/wens6515/xiaoli-wechat-ai-bot/releases) 下载 `小漓安装包.exe`。桌面端源码（GUI）不在此仓库——本仓库为**后端核心源码**开源。

## 隐私与安全

- `config.json`（含 API Key，DPAPI 加密落盘）、`cards/`（角色卡）、`memory.json`、`processed_ids.json`、`dist/`、`.rivet/` 均被 .gitignore 排除，不会进入版本库
- 源码默认 system_prompt 为通用人设，不含真实姓名/学校/群组信息
- API Key 在界面显示时自动遮蔽（`sk-***1234`）

## 技术说明与已知边界

- **视觉通道唯一**：微信 4.1.12+ 关闭了 UIA/CDP/窗口消息/本地数据读取等全部高效通道（验证过程见 `docs/微信通道验证结论.md`），PrintWindow 截图 + OCR 是唯一可用通道
- **窗口稳定性**：视觉坐标依赖微信窗口位置/尺寸稳定，拖动窗口后需重新固定（`tools/fix_window.py`）或重新圈选 OCR 区域
- **群聊判定**：以标题区 OCR（括号人数）为权威信号，群名不含"群/集团"且无人数时可能漏判
- 中文发送走剪贴板；图片/表情/视频统一按图片走多模态识别
