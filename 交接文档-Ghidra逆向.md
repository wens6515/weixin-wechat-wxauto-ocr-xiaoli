# 交接文档：Ghidra 逆向微信 4.1.12.51 数据库主密钥（下一个 AI 直接开工版）

> 本文件专为**下一个执行 Ghidra 逆向的 AI 会话**撰写。看到本文件即可直接开始，无需重新探索。
> 前置阅读（可选但有帮助）：`交接文档.md`（总览 + 14 次失败记录）、`.rivet\scratch\wx4112_reverse_progress3.md`（本会话 10 条验证详录）。
> 最近交接：2026-08 破军域会话——完成验证器自检、推翻 2 个旧推断、建立 crypto 函数图谱。

---

## 1. 任务与硬约束（一句话）

在小漓微信机器人上，**移除 OCR 接收链路**，改为**数据库解密**读取微信 4.1.12.51 的消息。本会话只负责打通**数据库主密钥获取**：用 Ghidra 逆向 `Weixin.dll`，从已确认的 crypto 锚点反查 sqlite3_key 内联实现的密钥设置路径，拿到 SQLCipher 数据库主密钥（验证目标：`message_0.db` 解密 + "王文生"会话消息）。

**★ 硬约束（不得违反）**：**不降级微信**。所有工作必须在 4.1.12.51 上完成。已证 15+ 次尝试拿不到 key 的原因是**密钥不在进程可读内存常规位置**，Ghidra 静态逆向是文档裁决的唯一可行路径。

---

## 2. 本会话（破军）已完成的验证——10 条确定性证据，直接作为逆向锚点

| # | 验证 | 结果 |
|---|---|---|
| 1 | HMAC 验证器自检（合成库已知 password 反向验证 find_key.py 逻辑） | ✅ 正确：对 key 命中、错 key 拒绝。**14+ 次失败不是假阴性** |
| 2 | message_0.db 页面布局（salt@0 / IV@4016 熵4.0 / HMAC@4032 熵5.8） | ✅ 标准 SQLCipher 4，验证公式数据范围正确 |
| 3 | 0x6635670（WCDB open）内部 140 call 目标 | ✅ **无任何 SQLCipher 特征——推翻 run6"sqlite3_key 在其内部"推断** |
| 4 | 0x55ee0b0（曾被称 SQLCipher 大函数） | ✅ **实为 PRAGMA 分发**（hook 抓 r9="journal_mode"/"temp_store"），非 key 函数 |
| 5 | 0x565c000~0x565f000 区域 | ✅ 非 cipher 核心（无 key 写入特征） |
| 6 | SHA-512 init = **0x06f6ccb0**（movabs H0..H7 铁证） | ✅ 8 常量各 7 处 movabs，.rdata 常量表 @0x9399030 |
| 7 | HMAC-SHA512 = **0x06fec222** / 包装 **0x06f6dd10**（key 长度 32） | ✅ |
| 8 | PBKDF2 候选 = **0x06f9aef0**（唯一调 HMAC 的循环） | ⚠️ **spawn 110s 零触发——数据库 key 派生不走此 PBKDF2** |
| 9 | **0x06f28520 = HKDF**（参数 rdx=ASCII "security hdkf expand"） | ✅ 网络用途，非数据库 KDF |
| 10 | deref3 全部候选（159 KEY + 2780 窗口）× 全 17 库 enc_key 直接路径 | ✅ 全未命中——**负结论扩展到全库** |

**核心推断（给 Ghidra 逆向定方向）**：数据库 key 派生**不走进程内可见的 PBKDF2/SHA-512 常规路径**（SHA-512 init 的 54 次 backtrace 主链是 HKDF/网络路径，数据库打开链不在其中）。因此逆向重点不是"找 PBKDF2 调用点"，而是**找 key 从何处加载/复制到 CipherCtx 的路径**——key 极可能是从 key_info.dat 或某存储解密后**直接作为 enc_key 缓存**（不经重新派生）。

---

## 3. 第一步：Ghidra 环境安装（先做这个）

本机**未安装 Java 和 Ghidra**（已核实）。按顺序装：

```bash
# 1. JDK 17+（Ghidra 11 需要 Java 17；本机有 venv Python 3.12，但 Java 独立）
#    下载 Temurin JDK 17: https://adoptium.net/temurin/releases/?version=17
#    或 winget: winget install EclipseAdoptium.Temurin.17.JDK
# 2. Ghidra 11.x（免费开源，NSA）
#    下载: https://github.com/NationalSecurityAgency/ghidra/releases
#    或国内镜像: /mirror china 后 git clone 或 curl -skL 下载
#    解压到 D:\ghidra\（建议固定路径）
# 3. 验证
java -version          # 应显示 17+
D:\ghidra\ghidra_11.x_PUBLIC\support\analyzeHeadless.bat -h   # 无头模式可用
```

**推荐无头模式（analyzeHeadless）**——适合 AI 自动化，避免 GUI：
```bash
# 创建项目并导入 Weixin.dll（首次导入+分析，约 10-30 分钟，185MB DLL）
D:\ghidra\ghidra_11.x_PUBLIC\support\analyzeHeadless.bat D:\ghidra\projects wx4112 -import "D:\腾讯\Weixin\4.1.12.51\Weixin.dll" -overwrite
# 后续脚本化分析用 -process + -postScript
```

⚠️ 网络注意：本环境 web_search/web_fetch TLS 失败，用 `curl -sk` 下载；GitHub releases 下载慢可先 `/mirror china`。

---

## 4. 逆向锚点地图（全部经本会话实证，按优先级）

```
SQLCipher/WCDB 配置链（run4 backtrace 证实）：
  0x6635670  WCDB Handle::open 统一入口（98 次触发）——已证非 key 路径，但它是配置链外层
  → 0x056665xx 函数 X（0x0566655f call open / 0x056666c1 call PRAGMA 分发）
  → 0x55ee0b0  PRAGMA 分发（0x055ee4b0~0x055ef729 配置大函数区）
  → 0x055EF5D4 PRAGMA 迭代数写入点（mov edx,0x3e800，cipher_ctx 配置）

crypto 函数图谱（本会话确认）：
  0x06f6ccb0  SHA-512 init（movabs H0..H7）
  0x06f6dd10  SHA-512/HMAC 包装（对象方法）
  0x06fec222  HMAC-SHA512（key 长度 32 @ r8d）
  0x06f9aef0  PBKDF2 候选（⚠️ spawn 零触发——数据库不走此路径，但 Ghidra 仍应确认其调用者）
  0x06f28520  HKDF（"security hdkf expand"——网络用途，排除数据库）

CipherCtx 结构（run7 发现）：
  offset 0x00 = algorithm(1)
  offset 0x04 = key_len（0x20=32）
  offset 0x08 = key 指针（指向的数据对全库 HMAC 失败——结构可能还有别层）
```

---

## 5. Ghidra 逆向执行步骤（按此顺序，从锚点反查 key 路径）

### 步骤 A：导入 + 自动分析
analyzeHeadless 导入 Weixin.dll，等分析完成（函数识别 + 引用分析）。image_base=0x180000000，.text VA=0x1000 Raw=0x400。

### 步骤 B：从 SHA-512 init（0x06f6ccb0）做 xref 反查（最高优先级）
1. 在 Ghidra 中跳到 0x1806f6ccb0（VA = 0x180000000 + RVA）
2. 列出**所有引用它的函数**（xref）——应包含 0x06fec222（HMAC）、0x06f28520（HKDF）及**任何数据库相关调用者**
3. 逐个用 decompiler 看伪代码，找：接收 key/password 参数 + 写 CipherCtx（offset 0x04=0x20 / offset 0x08=指针）的函数
4. **重点**：找不经过 PBKDF2 就写入 CipherCtx.key 的路径——这就是微信直接加载 enc_key 的位置

### 步骤 C：从 PRAGMA 配置区（0x055ef729 向上）反查 key 设置
1. 看 0x055ef729 之前整个配置大函数的伪代码（decompiler 会自动识别函数边界，比 capstone 线性扫描强）
2. 找它**读取 password/key 的来源**（参数、全局变量、key_info.dat 解密结果）
3. 特别关注：**WCDB 的 key 管理类**（可能是 `WCDB::Cipher` 或类似，接受 key 的字节数组 + 长度）

### 步骤 D：从 0x6635670 内部调用树反查（备选）
0x6635670 内 140 个 call 目标无 SQLCipher 特征（本会话已扫），但 Ghidra 能看它**内部更深层的间接调用**（call rax / 函数指针表）——静态字节扫描扫不到间接调用，Ghidra 的引用分析可以。

### 步骤 E：确认 sqlite3_key 内联实现
SQLCipher 的 sqlite3_key 特征：
- 接收 (sqlite3*, key_ptr, key_len) 三参数
- 内部调用 PBKDF2 或直接拷贝 key 到 cipher_ctx
- 之后设置 PRAGMA 配置（page_size/iter/hmac）
微信导出表无 sqlite3_key 符号（静态链接内联），找到后记录其完整调用链和 key 来源。

### 步骤 F：验证拿到的 key
```bash
cd D:\AI\小漓\.rivet\scratch\wechat-4.1.12-decrypt
D:\AI\小漓\.venv\Scripts\python.exe decrypt_all.py <key_hex> D:\微信文件\xwechat_files\wxid_wkfx633inecp22_04c0\db_storage decrypted
```
**HMAC 验证标准（find_key.py 已实现，自检通过）**：候选 key 对 message_0.db page1 的 salt 派生 mac_key，HMAC-SHA512(mac_key, page1[16:4032]+page1) 比对 page1[4032:4096]。

---

## 6. 排除清单（禁止重试——全部已验证）

**历史 14 次**（详见交接文档.md §4）+ **本会话 10 条**（§2 上表）：
- 内存扫描 salt / 二进制 salt 探针：0 命中（两次独立验证）
- 全内存高熵 32B 窗口 × 全 17 库 HMAC：2939 候选全否（enc_key 直接路径）
- hook CipherCtx 解引用（deref3）：175 窗口对 message_0.db 全否（本轮扩展全库仍否）
- hook 0x6635670（WCDB open）：98 次触发但 key 不在对象头，内部无 SQLCipher 特征
- hook 0x055EF5D4（PRAGMA 迭代写入）：rcx=CipherCtx 配置结构，key 指针间接引用不在可读路径
- hook 0x06f9aef0（PBKDF2）：110s 零触发
- hook 0x06f28520（HKDF）：触发但非数据库 KDF
- 降级微信：**禁止**

---

## 7. 拿到密钥后的固定流程（工具已备好）

```bash
# 1. 密钥写入 key.txt（decrypt_all.py 参数）
# 2. 解密全部库
cd D:\AI\小漓\.rivet\scratch\wechat-4.1.12-decrypt
D:\AI\小漓\.venv\Scripts\python.exe decrypt_all.py <password_hex> D:\微信文件\xwechat_files\wxid_wkfx633inecp22_04c0\db_storage decrypted
# 3. 导出"王文生"消息（⚠️ sender 映射逐库不同：message_0 用 Name2Id rowid，旧库用 1/2 且可能相反，必须锚点句验证）
D:\AI\小漓\.venv\Scripts\python.exe export_msgs.py
# 4. 实现 wx_backend\memory_backend.py 接入 wechat_bot
#    Protocol: WeChatBackend（__init__.py L57-93）含 name/connect/iter_sessions/get_messages/send_text/send_file/locate_message/close，可选 iter_unread_sessions()
#    priority: memory=0 > visual=1 > wxauto=100（memory 用 0 不改 visual）
#    发送仍走 visual（输入框点击），接收走内存通道；connect 失败必须抛 BackendUnavailableError 使 auto 链降级
# 5. 测试：D:\AI\小漓\.venv\Scripts\python.exe -m unittest xiaoli_desktop.tests.test_xxx 逐个模块跑（discover 报 __file__ None）
# 6. 更新交接文档.md 与本文档
```

---

## 8. 脚本与工具清单（.rivet\scratch\）

| 脚本 | 用途 | 状态 |
|---|---|---|
| `wechat-4.1.12-decrypt\`（克隆）★ | decrypt_all.py（HMAC 自验证）/ export_msgs.py（sender 逐库） | 直接可用 |
| `find_key.py` | frida spawn + hook + 滑动窗口 HMAC 验证框架 | 框架可复用，偏移需更新 |
| `wx4112_hook_sha512.py` | SHA-512 init hook + backtrace | 可用（本轮建） |
| `wx4112_hook_pbkdf2.py` / `wx4112_hook_pbkdf2b.py` | PBKDF2 / HKDF hook（已证负） | 保留作参考 |
| `wx4112_open_inner.py` / `wx4112_56663f0.py` / `wx4112_565xxx.py` / `wx4112_filter_targets.py` | capstone 静态反汇编工具 | 可用 |
| `deref3_cands.txt` | 488 条候选（已全库验证） | 负结论参考 |
| `wx4112_reverse_progress3.md` | 本会话 10 条验证详录 | 完整 |
| `stalker.log` / `hook_sha512.log` 等 | 运行时追踪原始记录 | 证据 |

---

## 9. 环境注意事项

- **frida 17.17.0**：`Memory.readByteArray` 已移除，用 `ptr.readByteArray()`；`Memory.scanSync` 可用
- **微信 spawn**：重启自动登录（key_info.dat 保留），无需扫码；spawn 后用 `Interceptor.attach(base.add(offset))`，offset 是 RVA
- **ASLR**：每次启动基址漂移，换算用运行时模块基址（frida `Process.getModuleByName("Weixin.dll").base`）
- **Python**：`.venv\Scripts\python.exe`（Git Bash 下路径不同：`.venv/Scripts/python.exe`）
- **测试**：unittest 逐个模块跑（discover 报 __file__ None）
- **网络**：web_search/web_fetch TLS 失败；`curl -sk` 可用；GitHub 慢可 `/mirror china`
- **Git**：`.rivet\` 被 gitignore，scratch 工作不污染仓库

---

## 10. 下一个 AI 的启动动作（最优先）

1. 安装 JDK 17 + Ghidra 11（§3），验证 analyzeHeadless
2. analyzeHeadless 导入 Weixin.dll，启动自动分析（后台跑，期间读 §4-5 的锚点细节）
3. 从 SHA-512 init（0x06f6ccb0）xref 反查（§5 步骤 B）——**最高优先级路径**
4. 重点找"enc_key 直接加载"路径（不经 PBKDF2）——这是本轮负结论指向的 key 存在形态
5. 拿到候选 → HMAC 验证（§5 步骤 F）→ 命中即写 key.txt → 走 §7 固定流程
6. 每步记录到 .rivet\scratch\wx4112_reverse_progress4.md（延续编号）
