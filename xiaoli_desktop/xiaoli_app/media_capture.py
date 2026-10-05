# -*- coding: utf-8 -*-
"""媒体捕获混入（MediaCaptureMixin）：微信窗口点击 / 查看器判定 / Ctrl+C
复制原图 / 表情路线裁剪 / 截图压缩存图 / 纯图 vision 单调用。

从 wechat_bot.py 拆出（纯移动，零逻辑改动）：方法体依赖运行时经 self
（self.wx / self.call_vision_api / self._route_vision_result），由
WeChatBot 继承本 Mixin，实例级调用与 mock.patch.object 打点不变。
"""
from __future__ import annotations

import base64
import logging
import os
import tempfile
import time

import pyautogui

from wx_backend.visual_backend import (
    ensure_window_visible,
    find_window_by_title,
    window_rect,
)

logger = logging.getLogger("xiaoli")


# 纯图/表情消息的 vision 单调用指令（与 xiaoli_bot.VISION_ROUTE_PROMPT 同一
# 分流语义的纯图变体：看图后角色化回复，任务材料则投递）。历史缺陷：此路径
# 曾用旧两段式时代的 vision_prompt（「专业图像描述AI，客观复述图片」），把
# 复述文本当角色回复原样发出——已废弃统一到角色化链路。
VISION_IMAGE_PROMPT = (
    "有人给你发来了一张或多张图片（没有附带文字；多张时按发送顺序给出，"
    "可能相互关联，请结合起来看）。照你平时的样子回他——可以评价、接梗、"
    "回答图里的问题；如果图片明显是任务材料（如文档截图、带指令的截图、"
    "需要处理的内容），调用 dispatch_task 工具把活交出去，任务描述写在工具参数里。"
)

class MediaCaptureMixin:
    """媒体捕获方法集（由 WeChatBot 继承；依赖经 self 解析）。"""

    # 视觉模型输入最长边上限：屏幕截图（可能 4K 全窗口）base64 直发体积过大
    MAX_IMAGE_EDGE = 2048

    def _save_screenshot_compressed(self, image, path):
        """缩放 + JPEG 压缩保存截图（pillow 已在依赖）。返回文件字节数。
        最长边超 MAX_IMAGE_EDGE 时等比缩放到上限内；PIL 不可用/失败时
        退回原样 PNG 保存（保证图片处理链路不因压缩失败中断）。"""
        try:
            from PIL import Image
            img = image.convert("RGB")
            w, h = img.size
            longest = max(w, h)
            if longest > self.MAX_IMAGE_EDGE:
                scale = self.MAX_IMAGE_EDGE / longest
                img = img.resize((max(1, int(w * scale)), max(1, int(h * scale))),
                                 Image.LANCZOS)
            img.save(path, format="JPEG", quality=88, optimize=True)
        except Exception as e:
            logger.error(f"[处理] 图片压缩失败，退回原样保存: {e}")
            try:
                image.save(path, format="PNG")
            except Exception as e2:
                logger.error(f"[处理] 退回保存也失败: {e2}")
        return os.path.getsize(path) if os.path.isfile(path) else 0

    def _capture_media_images(self, chat_name, min_top=None, exclude_rows=None):
        """捕获消息区对方本轮全部新媒体，返回临时文件路径列表（时间正序）。

        min_top：消息区 1x 下沿阈值（上层传 analyze_window 的 bot_bottom =
        分析区上沿）——只捕获 top ≥ 阈值的媒体框，排除 bot 自己的历史媒体；
        None = 不过滤（全量）。
        exclude_rows：消息区 1x 坐标 y 区间 [(top, bottom), ...]，与该区间
        垂直相交的媒体框剔除；保留仅为兼容旧调用——文件卡片的类型图标已在
        像素层剔除（面板内部的框一律不算媒体），调用不再需要传。

        每张独立走「点击 → 查看器判定分支」——微信 PC 每条消息各带一个
        头像，多张图片是多个独立媒体框（连通域按背景缝隙切分，不会被粘连）：
        - 真图片：点击打开「图片和视频」查看器 → Ctrl+C 复制原图进剪贴板
          （CF_HDROP 原始分辨率，替代旧预览窗截屏——截屏受窗口尺寸/DPI
          限制且被遮挡会截到遮挡物）→ ESC 关查看器。ESC 只在确认查看器
          存在时才按（表情路径按 ESC 会关掉微信主窗口，真机事故）。剪贴板
          为空（复制失败）→ 落表情路线兜底（先关查看器，避免遮挡裁剪区）
        - 没开（表情包）：全程不碰 ESC/Ctrl+C，截微信主窗口按媒体矩形
          裁剪表情本体送视觉模型（动图取当前帧）
        单张失败（复制为空/裁剪越界/异常）跳过该张，不拖垮整批。

        微信 RWTemp 临时文件生命周期不受控，复制一份到自己的临时文件再返回。
        """
        boxes_fn = getattr(self.wx, "media_screen_boxes", None)
        rects = (boxes_fn(min_top=min_top, exclude_rows=exclude_rows)
                 if boxes_fn is not None else [])
        paths = []
        for (ml, mt, mr, mb) in rects:
            try:
                pyautogui.click((ml + mr) // 2, (mt + mb) // 2)
                time.sleep(1.0)  # 等预览窗打开
                viewer_open = find_window_by_title("图片和视频") is not None
                copied = self._copy_image_from_viewer() if viewer_open else None
                if viewer_open:
                    pyautogui.press('esc')  # 关查看器（确认存在才按，全库唯一 ESC 点）
                    time.sleep(0.3)
                if copied:
                    paths.append(copied)
                    continue
                if viewer_open:
                    logger.warning("[图片复制] 剪贴板复制失败，落表情路线兜底")
                p = self._crop_media_region(ml, mt, mr, mb)
                if p:
                    paths.append(p)
            except Exception as e:
                logger.error(f"[图片捕获] 单张失败，跳过: {e}")
        return paths

    def _copy_image_from_viewer(self):
        """查看器已打开时：Ctrl+C 复制原图 → 读剪贴板 → 返回临时文件路径。

        不负责开关查看器（调用方确认存在并统一 ESC 关闭）；返回 None =
        复制失败，调用方落表情路线。"""
        tmp_path = None
        try:
            pyautogui.hotkey('ctrl', 'c')
            time.sleep(0.5)
            from PIL import ImageGrab
            grabbed = ImageGrab.grabclipboard()
            if isinstance(grabbed, (list, tuple)):
                files = [x for x in grabbed
                         if isinstance(x, str) and os.path.isfile(x)]
                if not files:
                    logger.warning("[图片复制] 剪贴板无有效文件路径")
                    return None
                import shutil
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmpfile:
                    tmp_path = tmpfile.name
                shutil.copyfile(files[0], tmp_path)
                return tmp_path
            if grabbed is not None:
                # 兜底：剪贴板直接是位图（非文件路径），压缩保存
                with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmpfile:
                    tmp_path = tmpfile.name
                self._save_screenshot_compressed(grabbed, tmp_path)
                return tmp_path
            logger.warning("[图片复制] 剪贴板为空（可能未复制成功）")
            return None
        except Exception as e:
            logger.error(f"[图片复制] 异常: {e}")
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                except Exception:
                    pass
            return None

    def _crop_media_region(self, ml, mt, mr, mb):
        """表情路线：截微信主窗口并按媒体矩形裁剪表情本体，返回临时文件路径。

        点击无查看器的媒体 = 表情包——截主窗口当前画面裁出该块（动图取
        当前帧）。矩形是点击前测得的屏幕坐标，表情点击不改变布局，仍有效。"""
        try:
            hwnd = find_window_by_title("微信")
            if not hwnd:
                logger.warning("[表情] 未找到微信主窗口")
                return None
            ensure_window_visible(hwnd)
            wr = window_rect(hwnd)
            if not wr:
                return None
            shot = pyautogui.screenshot(region=wr)
            # 屏幕坐标平移到主窗口图内坐标并夹紧边界（表情不可能越界，
            # 夹紧只是防测量瞬间窗口移动的脏数据）
            l = max(0, ml - wr[0])
            t = max(0, mt - wr[1])
            r = min(shot.width, mr - wr[0])
            b = min(shot.height, mb - wr[1])
            if r - l < 10 or b - t < 10:
                logger.warning("[表情] 媒体矩形越界，放弃裁剪")
                return None
            crop = shot.crop((l, t, r, b))
            with tempfile.NamedTemporaryFile(suffix='.jpg', delete=False) as tmpfile:
                tmp_path = tmpfile.name
            self._save_screenshot_compressed(crop, tmp_path)
            logger.info(f"[表情] 点击无查看器，按表情路线裁剪 ({r - l}x{b - t})")
            return tmp_path
        except Exception as e:
            logger.error(f"[表情] 裁剪异常: {e}")
            return None

    def _process_pure_image(self, chat_name, min_top=None):
        """纯图/表情消息处理：捕获对方本轮全部新媒体 → vision 单调用（全部
        图 + 人设 + 历史，一次看图角色化回复/任务判定）→ dict 原样路由给
        _route_vision_result（不压文本；img_paths 由调用方 finally 清理，
        覆写方需在返回前同步消费）。sender 无独立来源，以 chat_name 兜底。
        与图+文路径（_vision_route）同一链路语义：角色化回复而非旧两段式
        的客观图片复述。"""
        tmp_paths = self._capture_media_images(chat_name, min_top=min_top)
        if not tmp_paths:
            return None
        content = [{"type": "text", "text": VISION_IMAGE_PROMPT}]
        try:
            for p in tmp_paths:
                with open(p, 'rb') as f:
                    img_bytes = f.read()
                content.append({
                    "type": "image_url",
                    "image_url": {
                        "url": f"data:image/png;base64,{base64.b64encode(img_bytes).decode()}"}},
                )
            result = self.call_vision_api(content, chat_id=chat_name)
            if result:
                return self._route_vision_result(chat_name, chat_name, result,
                                                 img_paths=tmp_paths)
            return None
        except Exception as e:
            logger.error(f"[图片处理] 异常: {e}")
            return None
        finally:
            for p in tmp_paths:
                try:
                    os.unlink(p)
                except Exception:
                    pass
