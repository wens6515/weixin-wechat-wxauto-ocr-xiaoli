# -*- coding: utf-8 -*-
"""表情包库（stickers 目录 + manifest.json 描述缓存）：文件扫描/清单/搜索/
视觉打标。

库 = 一个目录下的图片文件（png/jpg/jpeg/gif/webp）+ manifest.json（file →
desc/tags）。描述来源三层：
1. 出厂包自带 manifest（用户提供的默认包）；
2. 文件名播种（零 API）：新增文件没有 manifest 条目时，desc 取文件名主干
   （剥《》书名号与结尾编号——默认包的「生气1」「《呜》」命名本身就是好标签），
   目录即刻可用；
3. 视觉打标（一次 API，可选手动触发或发送时懒补）：AI 看图输出
   {tags, desc}，写回 manifest 缓存。

搜索是纯本地关键词匹配（子串命中计分，零 API）。resolve 只接受真实存在于
库中的裸文件名（防路径逃逸）。"""
import base64
import json
import logging
import os
import re
import sys

logger = logging.getLogger("xiaoli")

STICKER_EXTS = (".png", ".jpg", ".jpeg", ".gif", ".webp")

# 视觉打标提示词（describe_sticker 用；输出 JSON 单对象）
DESCRIBE_PROMPT = (
    "你在给微信表情包建索引。看图输出一个 JSON 对象，不要输出任何其他文字：\n"
    '{"tags": ["情绪或场景标签，2-5 个，如 生气/委屈/猫咪/干饭"], '
    '"desc": "一句话描述这张图适合在什么时候发"}'
)


def default_stickers_dir():
    """表情包库默认位置：应用基目录（或其上一级）下的「表情包」/「stickers」。

    与壁纸库同一发现模式：源码态基目录 = xiaoli_desktop（默认包放仓库根，
    即基目录的上一级）；打包态基目录 = exe 所在目录（包内随发行）。"""
    if getattr(sys, "frozen", False):
        base = os.path.dirname(os.path.abspath(sys.executable))
    else:
        base = os.path.dirname(os.path.dirname(os.path.abspath(__file__)))
    for p in (base, os.path.dirname(base)):
        for name in ("表情包", "stickers"):
            d = os.path.join(p, name)
            if os.path.isdir(d):
                return d
    return os.path.join(base, "表情包")


def _stem_desc(fname):
    """文件名播种描述：剥书名号/括号包裹与结尾编号（生气1→生气、
    《呜》→呜、骄傲2《最肥》→骄傲2《最肥》——只剥外围包裹，内部保留）。"""
    stem = os.path.splitext(fname)[0]
    stem = re.sub(r"^[《【\[]+|[》】\]]+$", "", stem)
    stem = re.sub(r"\d+$", "", stem).strip()
    return stem or os.path.splitext(fname)[0]


def parse_sticker_desc(raw):
    """解析打标模型输出。JSON 解析失败时把整段文本截断当 desc（tags 空）——
    打标是增强不是闸门，绝不因输出不规范丢弃结果。"""
    if not raw:
        return {"tags": [], "desc": ""}
    text = re.sub(r"^```(?:json)?\s*|\s*```$", "", str(raw).strip(), flags=re.S)
    m = re.search(r"\{.*\}", text, flags=re.S)
    if m:
        try:
            data = json.loads(m.group(0))
            if isinstance(data, dict):
                tags = [str(t).strip() for t in (data.get("tags") or [])
                        if str(t).strip()]
                desc = str(data.get("desc") or "").strip()
                return {"tags": tags[:5], "desc": desc}
        except ValueError:
            pass
    return {"tags": [], "desc": text[:40]}


def describe_sticker(post_fn, base_url, api_key, model, image_path):
    """视觉打标一次调用（post_fn = LlmClient.post / bot._post_chat_completions
    同签名）。失败抛异常由调用方决定（懒补时吞掉、手动打标时上报）。"""
    with open(image_path, "rb") as f:
        url = "data:image/png;base64," + base64.b64encode(f.read()).decode()
    headers = {"Authorization": f"Bearer {api_key}",
               "Content-Type": "application/json"}
    messages = [
        {"role": "system", "content": DESCRIBE_PROMPT},
        {"role": "user", "content": [
            {"type": "text", "text": "给这个表情包打标签。"},
            {"type": "image_url", "image_url": {"url": url}},
        ]},
    ]
    payload = {"model": model, "messages": messages,
               "max_tokens": 200, "temperature": 0.2}
    data = post_fn(base_url, headers, payload, 60, label="vision",
                   meta={"kind": "vision", "model": model,
                         "messages": messages})
    raw = (data.get("choices") or [{}])[0].get("message", {}) \
        .get("content", "")
    return parse_sticker_desc(raw)


class StickerStore:
    """表情包库读写与搜索。manifest.json 是描述缓存（可删——删了回退文件名
    播种）；文件本身是事实源。"""

    def __init__(self, stickers_dir):
        self.dir = stickers_dir
        self.manifest_path = os.path.join(stickers_dir, "manifest.json")

    # ---------- 基础读写 ----------

    def _load(self):
        try:
            with open(self.manifest_path, "r", encoding="utf-8") as f:
                data = json.load(f)
        except (OSError, ValueError):
            return {}
        return data if isinstance(data, dict) else {}

    def _save(self, items):
        os.makedirs(self.dir, exist_ok=True)
        tmp = self.manifest_path + ".tmp"
        with open(tmp, "w", encoding="utf-8") as f:
            json.dump({"items": items}, f, ensure_ascii=False, indent=2)
        os.replace(tmp, self.manifest_path)

    def list_files(self):
        """库内图片文件名（排序）。目录不存在返回空（库整体不激活）。"""
        try:
            names = sorted(os.listdir(self.dir))
        except OSError:
            return []
        return [n for n in names
                if n.lower().endswith(STICKER_EXTS)
                and os.path.isfile(os.path.join(self.dir, n))]

    def entries(self):
        """全部条目：manifest 已描述的照抄；新文件播种 desc（零 API 即刻
        可用）。文件是事实源——manifest 里的已删文件不出现在结果里。"""
        known = {}
        for it in (self._load().get("items") or []):
            if isinstance(it, dict) and str(it.get("file") or "").strip():
                known[str(it["file"])] = {
                    "file": str(it["file"]),
                    "desc": str(it.get("desc") or "").strip(),
                    "tags": [str(t).strip() for t in (it.get("tags") or [])
                             if str(t).strip()],
                }
        out = []
        for f in self.list_files():
            if f in known:
                out.append(known[f])
            else:
                out.append({"file": f, "desc": _stem_desc(f), "tags": []})
        return out

    def catalog_lines(self):
        """清单注入行：file｜desc｜tags（tags 可空）。"""
        out = []
        for it in self.entries():
            tags = " ".join(it["tags"])
            out.append(f"{it['file']}｜{it['desc']}"
                       + (f"｜{tags}" if tags else ""))
        return out

    def search(self, query, limit=5):
        """关键词搜索：file+desc+tags 拼串后逐关键词子串命中计分，命中数
        降序、文件名升序稳定排序，取前 limit。零命中返回空。"""
        kws = [k for k in re.split(r"[\s,，、]+", str(query or "").strip()) if k]
        if not kws:
            return []
        scored = []
        for it in self.entries():
            hay = (it["file"] + " " + it["desc"] + " " + " ".join(it["tags"])).lower()
            score = sum(1 for k in kws if k.lower() in hay)
            if score:
                scored.append((score, it))
        scored.sort(key=lambda x: (-x[0], x[1]["file"]))
        return [it for _, it in scored[:limit]]

    def resolve(self, fname):
        """文件名 → 绝对路径。安全边界：只接受裸文件名（basename 相等、
        非 manifest.json、扩展名合法），且必须真实存在于库中——模型给的
        任何带路径成分的名字一律拒绝（防路径逃逸）。"""
        name = str(fname or "").strip()
        if not name or os.path.basename(name) != name \
                or name == "manifest.json" \
                or not name.lower().endswith(STICKER_EXTS):
            return None
        path = os.path.join(self.dir, name)
        return path if os.path.isfile(path) else None

    def reindex(self, describe_fn, progress_cb=None):
        """全库视觉打标（设置页「AI 优化标签」按钮）：逐文件调 describe_fn
        （path → {tags, desc}）写回 manifest。单文件失败跳过不中断；
        progress_cb(done, total) 供前端进度。返回 (成功数, 总数)。"""
        files = self.list_files()
        items = []
        ok = 0
        for i, f in enumerate(files, 1):
            try:
                d = describe_fn(os.path.join(self.dir, f))
                it = {"file": f,
                      "desc": str(d.get("desc") or "").strip() or _stem_desc(f),
                      "tags": [str(t).strip() for t in (d.get("tags") or [])
                               if str(t).strip()][:5]}
                ok += 1
            except Exception as e:
                logger.warning(f"[表情包] {f} 打标失败跳过: {e}")
                it = {"file": f, "desc": _stem_desc(f), "tags": []}
            items.append(it)
            if progress_cb is not None:
                try:
                    progress_cb(i, len(files))
                except Exception:
                    pass
        self._save(items)
        return ok, len(files)
