# OCR 引擎升级评估：rapidocr_onnxruntime 1.4.4 → rapidocr 3.x

> 结论先行：可升可不升，**倾向暂不升级**。新版（PP-OCRv6 模型）小字识别
> 有实际提升（误读修正、漏字补全），但事件路径 OCR 慢约 47%~68%，且迁移
> 需重验整条视觉管道的行坐标假设。待真机识别问题积累到值得付出延迟代价
> 时，按本文「复测步骤」重跑探针后决策。

## 测试对象

- 现役：`rapidocr_onnxruntime==1.4.4`（PP-OCRv3/v4 移动端模型，requirements.txt 钉死）
- 候选：`rapidocr` 3.9.2（包名已改为 `rapidocr`，默认 **PP-OCRv6** det/rec small 模型）
- 两包顶层名不同（`rapidocr_onnxruntime` / `rapidocr`），同一 venv 可共存直接对比

## 测试方法

`tools/ocr_upgrade_probe.py`（只读截图，不连微信、不点击、不动窗口状态）：

1. `capture`：PrintWindow 抓微信窗口 3 帧（1300x1610 标定窗口）存 PNG
2. `compare`：两引擎对「消息区裁剪」与「整窗」分别识别，difflib 逐行 diff
   + 平均置信度 + 计时（预热 1 次后计时 3 次取均值）

**公平性要点**：两引擎都必须限线程。rapidocr 3.x 默认
`intra_op_num_threads: -1`（吃满核），不设限直接对比会得到虚高约 4 倍的
耗时。复测务必传：

```python
RapidOCR(params={"EngineConfig.onnxruntime.intra_op_num_threads": 2,
                 "EngineConfig.onnxruntime.inter_op_num_threads": 1})
```

与现役的 `RapidOCR(intra_op_num_threads=2)` 对齐。

## 数据（同屏 3 帧，逐帧结果一致）

| 指标 | 1.4.4 | 3.9.2（限 2 线程） |
|---|---|---|
| 消息区 OCR 耗时 | ~605ms | ~888ms（慢 47%） |
| 整窗 OCR 耗时 | ~1224ms | ~2054ms（慢 68%） |
| 消息区行数 | 18 | 16 |
| 平均置信度 | 0.932 | 0.942 |

## 识别质量对比

新版更好：

- 误读修正：会话列表某条标题的「囚」旧引擎误读成「四」，新版读对
- 漏字补全：同一行内旧引擎漏读的字符（省略号段附近的字），新版读出
- 头像文字碎片噪声更少：旧引擎在消息区多读出两条头像碎片行，新版没有
- 标点更一致（全角冒号/波浪线趋向半角），时间戳保留空格
  （`9月1日22:55` → `9月1日 22:55`，现有 `_is_timestamp` 去空格后两者都兼容）

新版变差 / 需注意：

- 整窗顶栏的合并行（头像文字+会话名+搜索框）旧引擎读成一串，新版只读出
  单个字符——会话列表头部行结构变化，`iter_sessions` 兜底路径需重验
- 发送按钮「发送」被读成带杂前缀的变体——`is_send_button` 的精确匹配
  过滤会漏，靠 sender=self 的窗口过滤兜底（仍安全，但少一道防线）
- 行合并策略不同（行数 18→16）：同一内容行的 y 坐标分组边界会移动

## 对小漓管道的影响（若升级）

OCR 文本与行坐标喂给下游的一切假设都要重验：

- `_is_timestamp` / `_looks_like_file_text` / `_extract_file_name_token`：
  标点全半角变化影响正则命中面——现有正则对两种形态均已兼容
- 气泡归属、文件图标 `exclude_rows` 依赖 OCR 行 y 坐标准确——行合并策略
  变化会移动行 y，必须真机回归
- API 变化：构造参数进 `params` dict；返回从 `(result, elapse)` 变为
  `RapidOCROutput`（`.txts` / `.scores` / `.boxes`，boxes 是 numpy 数组，
  不能做真值判断）——需要一层薄适配封装，回滚 = 换一行 requirements

## 延迟代价与建议

- 0.5s 快档轮询本身不跑 OCR（只有像素 diff），升级只影响：消息处理事件
  的联合 OCR（每次多 ~0.3~0.8s）与红圈条带锚定（~0.3s → ~0.45s）
- 小字识别提升是「更好」不是「质变」，管道回归成本高于当前收益 → 暂缓

## 复测步骤

1. 建 Python 3.12 venv（1.4.4 要求 `<3.13`，不能用 3.13+ 建环境）：
   `pip install rapidocr_onnxruntime==1.4.4 rapidocr pillow numpy`
2. 微信开到目标会话 → `python tools/ocr_upgrade_probe.py capture`
3. `python tools/ocr_upgrade_probe.py compare`（默认两引擎对跑；改线程参数
   见上文公平性要点）
4. 用当期会话的真实截图重验后决策；探针默认全开核计时，注意换算
