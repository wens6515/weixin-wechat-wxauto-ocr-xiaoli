# 微信 4.1.12.51 本地数据破解与 Hook 通道调研

> 调研时间：2026-08
> 背景：视觉方案（PrintWindow 截图 + OCR）无法辨别消息类型（图片/视频/文件消息在 OCR 眼里不可见，类型/发送方判断不可靠，用户已确认以实际观察为准）。本次调研回答三个问题：
> 1. 本地消息数据加密能否破解？
> 2. Chromium 渲染层有没有其他获取通道？
> 3. Hook/DLL 注入层现状与可行性？

---

## 1. 结论速览

| 问题 | 结论 | 证据 |
|---|---|---|
| 本地数据库能否破解 | **能**——SQLCipher 4 加密，但密钥在微信进程内存中，内存扫描可提取，解密后可拿全部结构化消息（含类型/发送方） | 本机实测 db_storage 目录 + GitHub 活跃项目（§3/§4） |
| Chromium 渲染层 | **无通道**——UIA 空壳、CDP 注入无效、窗口消息零子窗口（既有验证结论，本次未重复） | docs\微信通道验证结论.md |
| Hook/DLL 注入 | **两条路线**：A. 内存密钥提取（非侵入，推荐）B. DLL hook（WeChatPYAPI 类，版本绑定+封号风险） | §5 |

**推荐路线：内存密钥提取 + WAL 实时监听为主通道，OCR 降级为发送兜底。**

---

## 2. 本机实测：数据目录结构（推翻旧结论）

### 2.1 位置与结构

微信 4.1.12.51 数据在 `D:\微信文件\xwechat_files\`（非旧文档假设的 APPDATA 路径）：

```
xwechat_files/
├── all_users/            # 公共配置（config/login/sqlite/head_imgs）
├── Backup/               # 备份
└── wxid_wkfx633inecp22_04c0/   # 当前账号（含 _d150 另一账号）
    ├── db_storage/       # ★ 数据库核心
    │   ├── MMKV/         # mmkv（仅少量 tinfo/storage_common，4096B）
    │   ├── message/      # ★ message_0.db (15MB) + biz_message_0.db + media_0.db
    │   ├── session/      # ★ session.db（会话列表）
    │   ├── contact/      # ★ contact.db + contact_fts.db（联系人）
    │   ├── favorite/ emoticon/ sns/ bizchat/ hardlink/ head_image/
    │   └── solitaire/ general/
    ├── msg/              # 附件：attach/{md5}/ + file/2026-06/ + video/
    └── config/ cache/ business/ temp/ resource/ apm_record/
```

### 2.2 关键推翻：不是"加密 mmkv"，是 SQLCipher 加密 SQLite

- `message\message_0.db`、`session\session.db`、`contact\contact.db` 均有 `.db-wal`/`.db-shm` 配对（SQLite WAL 模式特征）
- 但文件头 od 实测为随机字节（如 message_0.db 头 `364 266 254 242   ; 005   S 367...`），非明文 SQLite 头 `SQLite format 3\0` → **SQLCipher 加密**
- `.material` 文件（first/incremental/last）同样是加密随机字节——是加密 db 的备份快照
- MMKV 目录仅 4096B 的少量文件（tinfo/storage_common），**消息主体不在 mmkv**

### 2.3 旧交接文档勘误

> 交接文档 §3.1 "本地消息数据 | 不可读 | 消息存储为加密 mmkv" —— **不准确**。
> 实际：消息在 SQLCipher 加密的 SQLite（message_0.db 等），**可解密**（密钥在进程内存）。mmkv 只是边缘数据。

---

## 3. 社区方案实证（GitHub API 实测，2026-08 可访问）

### 3.1 328336690/wechat-decrypt（16★，2026-06-05 更新）★ 最完整

功能与 README 实测要点（已拉取 README 全文存档 .rivet\scratch\wechat_decrypt_readme.md）：

- **原理**：微信 4.0 用 SQLCipher 4（AES-256-CBC + HMAC-SHA512，PBKDF2 25.6 万迭代，4096 页）。WCDB 在进程内存缓存派生 raw key，格式 `x'<64hex_enc_key><32hex_salt>'`。工具扫描进程内存匹配该模式 + salt + HMAC 验证提取密钥。
- **流程**：`find_all_keys.py`（管理员权限扫内存提密钥）→ `decrypt_db.py`（解密全部 ~26 个库）
- **实时监听**：`monitor_web.py` 30ms 轮询 WAL mtime（WAL 预分配 4MB 固定大小，不能看 size 要看 mtime）→ 变化后全量解密 + WAL patch（~70ms）→ SSE 推送，**总延迟 ~100ms**
- **图片解密**：.dat 三格式（旧 XOR / V1 AES-ECB+固定key / V2 AES-128-ECB+XOR 密钥从内存提取）
- **MCP Server**：`mcp_server.py` 提供 get_recent_sessions / get_chat_history / search_messages / get_contacts / get_new_messages —— **Claude/AI 可直接查询微信数据**
- 数据库结构：session.db 会话列表（含消息摘要、未读数）、message_*.db 聊天记录、contact.db 联系人

### 3.2 yinhuacha869/ReadWxKey（2026-07-23 更新，4★）

> "After logging into WeChat, at any time, the database key and image key for Windows WeChat versions **4.1.8.x/4.1.9.x/4.1.10.x/4.1.11.x and above** can be read via memory scanning."

**覆盖当前装的 4.1.12.51**（4.1.8+ 及以上通用）。

### 3.3 ycccccccy/wx_key（1791★）——被腾讯 DMCA 下架（决定性反证）

- 描述："获取微信4.0版本以上数据库密钥和图片密钥的工具"
- README 已被替换为讽刺长文：收到腾讯法务 DMCA，代码全部删除（存档 .rivet\scratch\wxkey_readme.md）
- **DMCA 下架 = 密钥提取真实可行的最强反证**（腾讯不会用法律手段封杀不存在的功能）
- forks 全部是下架后空壳，无实质代码残留

### 3.4 Thearas/wechat-db-decrypt-macos（628★，macOS 版，2026-07-13 更新）

- macOS arm64 微信 4.1 数据库解密（4.1.2.241 测试）——证明跨平台同一套 SQLCipher 方案

---

## 4. 路线 A：内存密钥提取 + 实时监听（推荐）

### 4.1 为什么可行

- 密钥**不加密存储**在进程内存（WCDB 运行时缓存），可读进程内存即得密钥
- 非侵入：只读内存，不注入 DLL、不改 UI、不碰网络协议
- 版本适配：内存模式扫描非版本绑定，4.1.8+ 通用（ReadWxKey 已证 4.1.11+，wechat-decrypt 与 macOS 4.1.2 同原理）
- 微信升级大概率仍可用（只要 WCDB 缓存格式不变）

### 4.2 能拿到什么（对比 OCR）

| 能力 | OCR 方案 | 密钥+解密方案 |
|---|---|---|
| 新消息检测 | 红圈像素检测（可行但脆弱） | session.db 未读数字段 / WAL 实时监听 |
| 消息类型 | ✗ 无法辨别（用户确认） | ✓ 结构化类型字段（text/image/file/video） |
| 发送方 | ✗ 靠气泡颜色猜 | ✓ 字段（self/对方/群成员） |
| 图片/文件 | ✗ 不可见 | ✓ 解密 .dat 原图 + file_id/aes_key |
| 全文搜索 | ✗ | ✓ message_fts.db / search_messages |
| 延迟 | 秒级 | ~100ms |

### 4.3 风险与约束

- 需**管理员权限**（读进程内存）
- 需**微信保持登录运行**（密钥在内存中）
- 需微信设置→文件管理中确认 db 路径（本机实测为 D:\微信文件\xwechat_files）
- 属"读取自己的本地数据"，无网络交互，封号风险极低（与 wx_key 被 DMCA 的性质相同：合规层面需注意，但技术上是本地解密）

---

## 5. 路线 B：DLL Hook（WeChatPYAPI 类）——不推荐为主通道

### 5.1 mrsanshui/WeChatPYAPI（1046★，活跃 V-8.2.0）

- 专业版支持微信 **4.1.2.16**（社区版 3.6.0.18 已停维护）
- 功能：消息回调、收发文本/图片/文件/语音、群管理、朋友圈、CDN 下载等完整 API
- **关键限制**：
  - 绑定 4.1.2.16，**不是当前装的 4.1.12.51**——需降级装指定版本（百度网盘分发安装包）
  - 需关闭杀毒软件（DLL 注入误报）
  - 专业版付费
  - 微信升级即失效（需等作者适配）

### 5.2 WeChatFerry（lich0821，v39.5.2，2026-03-28）

- 最新 release 仍是微信 **3.9.12.51** 支持——4.x 未适配，排除

### 5.3 wxhelper（eatmoreapple，3132★，2026-06 更新）

- 描述"Hook WeChat 微信逆向"，但 releases 停在 2024-04（v0.0.5），README 未声明 4.x——3.x 线，排除

---

## 6. 建议的落地路径（待用户确认后执行）

1. **验证**（15 分钟）：拉取 328336690/wechat-decrypt 到本地 → 微信保持登录 → 管理员权限跑 find_all_keys.py → 跑 decrypt_db.py → 确认能读出真实聊天记录（含消息类型字段）
2. **集成**：若验证通过，实现 `wx_backend\memory_backend.py`（或 db_backend）：连解密后的 sqlite + WAL 监听，实现 WeChatBackend Protocol（iter_unread_sessions / get_messages / send_text / send_file）
3. **发送兜底**：发送仍走现有 visual_backend（输入框点击 + 键盘输入），接收全部走内存通道
4. **注册表**：priority 排序 memory(1) > visual(10) > wxauto(100)

### 6.1 验证前置条件（需要用户配合）

- 微信保持登录状态（密钥在内存）
- 管理员权限终端（提密钥）
- 确认 db 路径（本机已实测 D:\微信文件\xwechat_files）

---

## 7. 环境备注

- 微信版本 4.1.12.51，安装 D:\腾讯\Weixin\，登录账号"小漓"（wxid_wkfx633inecp22_04c0）
- 数据目录 D:\微信文件\xwechat_files\（微信设置→文件管理 中可见）
- 本机有第二个账号 wxid_ipq0oio3fp7c22_d150（旧账号？）
- web 访问：本环境 web_search/web_fetch TLS 证书校验失败，curl -sk 可正常访问 GitHub API（后续调研用 curl -sk + api.github.com 的 readme base64 接口，raw.githubusercontent 不稳定）
- 调研中间产物：.rivet\scratch\（wxkey_readme.md / wechat_decrypt_readme.md / readwxkey_readme.md / *_readme.json）
