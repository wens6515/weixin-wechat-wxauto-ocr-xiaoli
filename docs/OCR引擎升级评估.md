# OCR 引擎升级评估与实施记录：rapidocr_onnxruntime 1.4.4 → rapidocr 3.9.2

> 结论：**已按方案 A 实施**——`rapidocr==3.9.2` + PP-OCRv5 mobile 模型 +
> onnxruntime 后端 + `Det.limit_side_len=224`。速度与现役持平（事件路径
> 净变化约 +240ms，条带/标题反而大幅变快），识别质量拿到实测收益（下划线
> 文件名、弯引号、生僻字、漏字、头像幻影行），安装包 +5MB。本文记录两轮
> 调研数据、参数坑与回归结论，供后续再评估（如 OpenVINO 后端）时复用。

## 测试对象

- 现役（已替换）：`rapidocr_onnxruntime==1.4.4`（PP-OCRv4 mobile 模型）
- 现役（已启用）：`rapidocr==3.9.2`（包名已改，PP-OCRv5 mobile det/rec + v4 cls，
  onnxruntime 后端，2 线程）
- 两包顶层名不同（`rapidocr_onnxruntime` / `rapidocr`），同一 venv 可共存对比

## 两轮真机调研数据（1300x1610 标定截图，双端限 2 线程，预热后取均值）

第一轮：引擎 A/B（1x 裁剪口径）

| 区域 | 1.4.4 | 3.9.2 默认(v6 small) | 3.9.2 + v5 mobile |
|---|---|---|---|
| 消息区联合 OCR | 601ms | 915ms（+52%） | 627ms（+4%） |
| 红圈条带锚定 | ~450ms | ~1300ms（2.8 倍） | 632ms |
| 整窗 OCR（兜底路径） | 1222ms | 2032ms（+66%） | 1595ms |

第二轮：推理后端与模型档位扩展（结论——**慢的是 v6 模型不是新框架**；
OpenVINO 后端全面反超但安装包代价 ~100MB 级，暂不采用）

| 配置 | 消息区(1x) | 条带 | 整窗 | 判定 |
|---|---|---|---|---|
| v5 mobile + OpenVINO | 351ms | 290ms | 766ms | 质量与 ORT 完全一致；OpenVINO 库 226MB 未采用 |
| v6 tiny + OpenVINO | 161ms | 180ms | 343ms | 幻影行/低置信度，不采用 |
| v5 server + ORT | 40531ms | — | — | CPU 2 线程不可用 |
| cnocr 2.3.3 | — | — | — | 依赖解析拖 torch+ultralytics 全家桶，出局 |

生产形态（2x 裁剪，实施后复测）：联合 OCR 旧 1048ms → 新 1579ms，
条带 450 → 162ms，标题 820 → 112ms——**标准事件路径（条带+联合，
assume_switched）净 +240ms**；单独读标题的路径反而省 ~465ms。

## 质量对比（v5 mobile 拿到的收益，真机 + 合成双验证）

- 合成痛点 8 样本（28px 深色气泡）：旧引擎对 5 个，v5 mobile 全对——
  含「小漓_深海小剧场.html」下划线（真机事故复现）与弯引号「“强盗”」
  （memory 键归一化的起因）
- 群成员生僻字：旧读「哆拉A萝」，新读「哆菈A夢」全对
- 漏字补全：消息里的「嗯」「♪」「……」旧引擎漏读/误形，新引擎读对
- 头像幻影行：旧引擎消息区多出「色大肥鱼」碎片行 ×2，新引擎零噪声
- 行为差异（无逻辑影响）：颜文字读法略变（`(·_·")`→`(：·)`）；时间戳
  带空格（`9月1日 22:55`，`_is_timestamp` 去空格后兼容）；行合并策略
  不同（1x 口径 18→16 行；生产 2x 口径 22→21 行，远比 1x 口径乐观）

## 参数坑（升级时必踩，已写进 `_get_ocr_engine` 注释与契约测试）

1. **构造参数是枚举**：`Det.ocr_version` 等必须传 `OCRVersion.PPOCRV5` /
   `ModelType.MOBILE`（`rapidocr.utils.typings`），传字符串直接被参数校验拒绝。
2. **线程必须显式限 2**：新框架默认 `-1` 吃满核（旧 CPU 100% 事故根因同源）。
3. **`Det.limit_side_len=224`（关键）**：v5 det 对默认「短边放大到 736」敏感
   ——标题区裁剪（517x70）被放大 10.5 倍后整块检不出文本（返回 0 条，
   v4/v6 无此问题；`read_title` 全挂）。压到 224 后：标题 conf 1.0 恢复、
   条带主名行合并更完整且提速 5 倍、消息区/整窗（min 边 ≥736）本就不放大、
   零影响。
4. **返回结构**：`RapidOCROutput`（`.txts/.scores/.boxes`），boxes 是 numpy
   数组禁真值判断，`ocr_image` 适配层逐字段 None 检查——下游消费的
   `[{text,x,y,w,h}]` 结构不变，业务层零改动。
5. **输入通道序**：生产一直喂 RGB np 数组，新引擎 RGB/BGR 实测仅颜文字
   细节形态差异（RGB 略优），保持 RGB 不变。

## 改动清单

- `xiaoli_desktop/requirements.txt`：`rapidocr==3.9.2` + `onnxruntime==1.30.0`
  （新包不再硬依赖 onnxruntime，必须显式钉版本；传递依赖新增 colorlog/
  omegaconf/tqdm，numpy 2.x 兼容实测 OK）
- `wx_backend/visual_backend.py`：`_get_ocr_engine()`（枚举参数 + 线程 +
  limit_side_len=224）、`ocr_image()`（RapidOCROutput 适配），业务层零改动
- `tests/test_visual_backend.py`：假引擎换 RapidOCROutput 契约（numpy boxes）、
  构造契约测试断言模型锁定 + 线程 + limit 参数（防回退）
- `小漓.spec`：`collect_data_files('rapidocr')` 结果按 `.endswith('.onnx')`
  列表过滤（排除 wheel 自带的 v6 模型 32MB——**excludes 参数对 onnx 实测
  不生效，PyInstaller 6.21.0**，必须列表过滤）+ 显式收 `tools/ocr_models/`
  三个模型进 `rapidocr/models/` 原位
- `tools/ocr_models/`：v5 mobile det/rec + cls 三个模型（21MB，gitignored
  打包资产；registry 不随 wheel 分发）
- 保留全部既有 OCR 容错 workaround（分隔符归一化/memory 键剥引号/token
  提取）——新引擎「读得对」，这些是「读错也不出错」的防线

## 回归结论（实施后实测）

- 482 个单测 + 自检 55 项全过（仅 2 处 OCR mock 按新契约修改）
- 真机被动链路：`read_title` 正确读会话名（修复 limit 参数前返回 None）、
  条带锚定读出主名+预览、消息区 2x 21 行内容完整
- 打包：模型随 spec 进包 `rapidocr/models/`（恰好 3 个 onnx，v6 零残留），
  onnxruntime DLL / omegaconf / rapidocr 等 1391 个模块经 PYZ 归档核验在包；
  打包 exe 烟雾测试启动正常。**冻结环境的 rapidocr 懒加载要等第一条真实
  消息才首次执行**——失败模式是优雅降级（`ocr_image` 返回空 + 日志
  "RapidOCR 不可用"），不会崩进程；测试区实测时留意首条消息即可

## 去 2x（引擎升级的第二步）

2x 放大是旧引擎时代的小字补偿。新引擎落定后重审：同一截图三口径对比
（旧@2x 现网基线 / 新@2x / 新@1x），**新@1x 与新@2x 内容等价、零丢失**
——省略号「……」1x 下反而读得更全、头像幻影行 1x 为零，唯一反向是 2x
多读对一个颜文字字符。结论：2x 只剩成本没有收益，整体移除。

- OCR 输入全线 1x 原生：read_title、联合 OCR（get_messages assume 路径）、
  单片路径不再 LANCZOS 放大
- 坐标口径全线回归 1x：气泡框/media 框/头像框不再 ×2，消息 y 不再 ÷2，
  联合路径标题分界线与坐标平移回归 1x
- 几何阈值线性减半（行为不变）：`_BUBBLE_LINE_GAP` 70→35、发送者名紧贴
  判定 150→75、头像 y 对齐容差 35→18（1x 口径，2x 时期数值见 git 历史）

生产路径实测（同机稳态）：联合 OCR 旧@2x 1048ms（消息区口径）→ 新@1x
715ms（更大的联合区口径），read_title 820ms → 134ms，条带锚定 450ms →
~170ms——**完整事件路径 OCR（条带+联合）比引擎升级前快约 40%**，比
「引擎升级但保留 2x」的中间态快约一倍。

回归：482 单测全绿（7 处 ocr_image 夹具坐标按 1x 口径减半，相对几何
不变断言不动）+ 自检 55 项 + 真机被动链路（标题/条带/联合 1x 全读对，
含换行合并与颜文字）。

## 备查：复测步骤

1. 双引擎隔离 venv：`pip install rapidocr_onnxruntime==1.4.4 rapidocr==3.9.2
   pillow numpy psutil`（两包顶层名不同可共存；对比时**两边都必须限 2 线程**，
   新框架默认 -1 吃满核会得到虚高 4 倍的耗时）
2. `python tools/ocr_upgrade_probe.py capture` → `compare`（1x 口径 A/B）
3. 生产形态（2x）与标题区务必单测——`limit_side_len` 坑只在宽扁小裁剪暴露
4. 换模型/后端 = 改 `_get_ocr_engine` 的 params 枚举（v6 tiny/server、
   OpenVINO 都是配置项，registry 支持按需下载）；OpenVINO 路线另需评估
   PyInstaller hook 与安装包体积（库 226MB）
