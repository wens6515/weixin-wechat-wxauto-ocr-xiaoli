# -*- coding: utf-8 -*-
import time
import json
import os
import logging
import re
import threading
import traceback
import requests
import base64
import pyautogui
import tempfile
from wxauto4 import WeChat
from wxauto4.msgs.mtype import ImageMessage, FileMessage


def strip_model_prefix(model):
    """剥离模型名的厂商前缀：'deepseek:deepseek-v4-flash' → 'deepseek-v4-flash'。

    配置/卡片里模型 id 沿用「厂商:模型」前缀格式（UI 分组展示用），
    但 API 只认纯模型名——透传带前缀的 model 会得到 400。
    """
    if not model or ":" not in model:
        return model
    return model.split(":", 1)[-1]


def estimate_tokens(text):
    """粗略估算 token 数：统一按 1 字符 ≈ 1 token 保守估算。

    混合内容取保守上界（宁可多估不超限）——请求超限会得到
    API 400 "maximum context length"，低估反而更危险。
    """
    return len(text) if text else 0


def fit_messages_in_budget(messages, budget=100000, reserve=2000):
    """把 messages 裁剪到 token 预算内（从最旧历史开始丢弃）。

    根因：文件识别把超大文件全文拼进 prompt（实测请求 272 万 token，
    模型上限 104 万 → API 400 "maximum context length"）。
    规则：
    - system 消息永不丢弃（裁剪其内容到预算 20%）
    - 历史消息从最旧开始丢，直到总 token ≤ budget - reserve
    - 最后一条 user 消息若仍超预算，截断其内容到预算 60%
    返回裁剪后的 messages（原列表不修改）。
    """
    budget = max(1000, budget)
    cap = max(500, budget - reserve)
    out = []
    total = 0
    # system 优先保留（裁剪到预算 20%）
    for m in messages:
        if m.get("role") == "system":
            content = str(m.get("content") or "")
            if total + estimate_tokens(content) > max(500, budget // 5):
                # 保留开头（system prompt 语义在前）
                keep = max(500, budget // 5)
                content = content[:keep]
            out.append({"role": "system", "content": content})
            total += estimate_tokens(content)
    # 历史 + user：从最旧开始，超预算的旧消息丢弃、继续往后；
    # 最后一条 user 若仍超限则截断（可能含文件全文）
    tail = [m for m in messages if m.get("role") != "system"]
    for i, m in enumerate(tail):
        content = str(m.get("content") or "")
        tokens = estimate_tokens(content)
        is_last = i == len(tail) - 1
        if total + tokens > cap:
            if is_last and content:
                # 最后一条 user（可能含文件全文）截断到预算 60%，带省略号标记
                keep = max(500, int(budget * 0.6))
                content = content[:keep] + "[内容过长已截断]…"
                tokens = estimate_tokens(content)
                out.append({"role": m.get("role", "user"), "content": content})
                total += tokens
            continue  # 旧消息超预算 → 丢弃该条，继续尝试更近的消息
        out.append({"role": m.get("role", "user"), "content": content})
        total += tokens
    return out

LOG_FILE = os.path.join(os.path.dirname(os.path.abspath(__file__)), "bot.log")

# 清空旧日志，确保每次运行都是新的
with open(LOG_FILE, "w", encoding="utf-8") as _f:
    _f.write("")

logging.basicConfig(
    level=logging.DEBUG,
    format='%(asctime)s [%(levelname)s] %(message)s',
    handlers=[
        logging.FileHandler(LOG_FILE, encoding="utf-8"),
        logging.StreamHandler()
    ],
    force=True
)
# 终端只显示 INFO 及以上，日志文件保留 DEBUG
logging.getLogger().handlers[1].setLevel(logging.INFO)
logger = logging.getLogger("xiaoli")


def load_config(path="config.json"):
    default_cfg = {
        "bot_nickname": "小漓",
        "ai_api_url": "https://api.deepseek.com/v1/chat/completions",
        "ai_api_key": "",
        "chat_model": "deepseek:deepseek-v4-flash",
        "chat_temperature": 0.7,
        "chat_top_p": 0.9,
        "vision_model": "zhipu:glm-4.6v",
        "vision_temp": 0.7,
        "vision_max_tokens": 10000,
        "vision_prompt": "你是一个专业的图像描述AI。请详细、客观地描述这张图片的内容，包括主要物体、人物动作、表情、场景氛围、文字信息等。不要加入主观评价或建议，只输出观察到的客观事实。描述语言简洁但信息丰富，但是一定要详细描述图片的每一个内容，方便后续处理。",
        "system_prompt": "你叫小漓，是一个很会聊天、很可爱的人。你是用户创建的微信 AI 助手，陪用户聊天、帮忙处理任务。\n每次说话的风格要有变化，不要固定。注意区分私聊和群聊，不要在私聊里面聊群，不要在群里面聊私聊的东西。\n说话的时候不要用 emoji，用颜文字表情。\n回复要简短，不要虚构不知道的事情；如果发消息的人你不认识，那就是你的新朋友，友好地回应对方。",
        "max_history": 1000,
        "cooldown": 3,
        "api_retry": 2,
        "api_timeout": 60,
        "start_paused": True,
        "memory_file": "memory.json"
    }
    if not os.path.exists(path):
        with open(path, "w", encoding="utf-8") as f:
            json.dump(default_cfg, f, indent=4, ensure_ascii=False)
        return default_cfg
    with open(path, "r", encoding="utf-8") as f:
        cfg = json.load(f)
    changed = False
    for k, v in default_cfg.items():
        if k not in cfg:
            cfg[k] = v
            changed = True
    if changed:
        with open(path, "w", encoding="utf-8") as f:
            json.dump(cfg, f, indent=4, ensure_ascii=False)
    return cfg


CONFIG = load_config()


class WeChatBot:
    def __init__(self, cfg):
        self.nickname = cfg["bot_nickname"]
        self.api_url = cfg["ai_api_url"]
        self.api_key = cfg["ai_api_key"]
        # 视觉模型可指向独立端点（角色卡跨 provider 时由 config_store 投影生成）
        self.vision_api_url = cfg.get("vision_api_url") or self.api_url
        self.vision_api_key = cfg.get("vision_api_key") or self.api_key
        self.chat_model = strip_model_prefix(cfg["chat_model"])
        self.chat_temperature = cfg.get("chat_temperature", 0.7)
        self.chat_top_p = cfg.get("chat_top_p", 0.9)
        self.vision_model = strip_model_prefix(cfg["vision_model"])
        self.vision_temp = cfg.get("vision_temp", 0.7)
        self.vision_max_tokens = cfg.get("vision_max_tokens", 10000)
        self.vision_prompt = cfg["vision_prompt"]
        self.system_prompt = cfg["system_prompt"]
        self.max_history = cfg["max_history"]
        self.cooldown = cfg["cooldown"]
        self.api_retry = cfg["api_retry"]
        self.api_timeout = cfg["api_timeout"]
        self.paused = cfg.get("start_paused", True)
        self.memory_file = cfg.get("memory_file", "memory.json")
        # 文件处理配置
        self.file_model = strip_model_prefix(cfg.get("file_model", self.chat_model))
        self.file_temp = cfg.get("file_temp", 1.0)
        self.file_max_tokens = cfg.get("file_max_tokens", 10000)
        self.file_prompt = cfg.get("file_prompt", self.system_prompt)
        self.file_storage_path = cfg.get("file_storage_path", "")
        # 图片消息点击偏移校准（竖图点击偏位时手动校正，格式 [dx, dy]，存 config.json）
        self.image_click_offset = cfg.get("image_click_offset", [0, 0])

        self._model_lock = threading.RLock()

        self.memory_db = {}
        self._load_memory()
        self.last_reply_time = 0
        self.recent_msg_ids = set()
        self.wx = None
        self._connect_wx()

        # 持久化去重：用消息 id（每一条消息唯一）
        self.processed_ids_file = os.path.join(os.path.dirname(os.path.abspath(__file__)), "processed_ids.json")
        self.processed_ids = set()
        self._load_processed_ids()
        # 启动时把所有已存在的消息 id 加入去重集合，防止重启后重复处理
        self._seed_existing_ids()

    def set_chat_temperature(self, value):
        with self._model_lock:
            self.chat_temperature = float(value)

    def set_chat_top_p(self, value):
        with self._model_lock:
            self.chat_top_p = float(value)

    def set_vision_temperature(self, value):
        with self._model_lock:
            self.vision_temp = float(value)

    def _load_processed_ids(self):
        if os.path.exists(self.processed_ids_file):
            try:
                with open(self.processed_ids_file, "r", encoding="utf-8") as f:
                    data = json.load(f)
                self.processed_ids = set(data) if isinstance(data, list) else set()
            except Exception as e:
                logger.error(f"加载去重记录失败: {e}")
                self.processed_ids = set()

    def _save_processed_id(self, msg_id):
        self.processed_ids.add(msg_id)
        try:
            with open(self.processed_ids_file, "w", encoding="utf-8") as f:
                json.dump(list(self.processed_ids), f, ensure_ascii=False)
        except Exception as e:
            logger.error(f"保存去重记录失败: {e}")

    def _seed_existing_ids(self):
        """启动时把所有已存在消息的 id 加入去重集合，防止重启后重复处理"""
        try:
            sessions = self.wx.GetSession()
            for session in sessions:
                chat_name = session.name if hasattr(session, 'name') else str(session)
                if not chat_name:
                    continue
                self.wx.ChatWith(chat_name)
                time.sleep(0.1)
                msgs = self.wx.GetAllMessage()
                if not msgs:
                    continue
                for msg in msgs:
                    msg_id = getattr(msg, 'id', None)
                    if msg_id:
                        self.processed_ids.add(msg_id)
                        self.recent_msg_ids.add(f"{chat_name}_{msg_id}")
            with open(self.processed_ids_file, "w", encoding="utf-8") as f:
                json.dump(list(self.processed_ids), f, ensure_ascii=False)
            logger.info(f"✅ 已加载 {len(self.processed_ids)} 条历史消息（防重复）")


        except Exception as e:
            logger.warning(f"加载历史消息失败: {e}")

    def _load_memory(self):
        if os.path.exists(self.memory_file):
            try:
                with open(self.memory_file, "r", encoding="utf-8") as f:
                    self.memory_db = json.load(f)
                for chat in self.memory_db:
                    if len(self.memory_db[chat]) > self.max_history:
                        self.memory_db[chat] = self.memory_db[chat][-self.max_history:]
            except Exception as e:
                logger.error(f"加载记忆失败: {e}，将使用空白记忆")
                self.memory_db = {}

    def _save_memory(self):
        try:
            with open(self.memory_file, "w", encoding="utf-8") as f:
                json.dump(self.memory_db, f, ensure_ascii=False, indent=2)
        except Exception as e:
            logger.error(f"保存记忆失败: {e}")

    def _connect_wx(self):
        while True:
            try:
                self.wx = WeChat(resize=False, ads=False)
                logger.info("✅ 微信连接成功")
                return
            except Exception as e:
                logger.error(f"微信连接失败：{e}，10秒后重试...")
                time.sleep(10)

    def _get_history(self, chat_id):
        if chat_id not in self.memory_db:
            self.memory_db[chat_id] = []
        return self.memory_db[chat_id]

    def _add_history(self, chat_id, role, content):
        hist = self._get_history(chat_id)
        hist.append({
            "role": role,
            "content": content,
            "time": time.strftime("%Y-%m-%d %H:%M:%S")
        })
        if len(hist) > self.max_history:
            self.memory_db[chat_id] = hist[-self.max_history:]
        self._save_memory()

    def clear_history(self, chat_id=None):
        if chat_id:
            self.memory_db.pop(chat_id, None)
            logger.info(f"已清空聊天 {chat_id} 的历史")
        else:
            self.memory_db.clear()
            logger.info("已清空全部对话历史")
        self._save_memory()

    def delete_messages(self, chat_id, indices):
        if chat_id not in self.memory_db:
            logger.warning(f"❌ 聊天 {chat_id} 不存在于记忆中")
            return False
        hist = self.memory_db[chat_id]
        total = len(hist)
        to_delete = []
        for idx in indices:
            if 1 <= idx <= total:
                to_delete.append(idx - 1)
            else:
                logger.warning(f"序号 {idx} 超出范围（1-{total}），已忽略")
        if not to_delete:
            return False
        to_delete = sorted(set(to_delete), reverse=True)
        deleted_msgs = []
        for i in to_delete:
            deleted_msgs.append(hist.pop(i))
        self._save_memory()
        logger.info(f"已从聊天 {chat_id} 中删除 {len(deleted_msgs)} 条消息")
        for msg in deleted_msgs:
            role = "用户" if msg["role"] == "user" else "小漓"
            ts = msg.get("time", "未知时间")
            logger.info(f"  删除: [{ts}] {role}: {msg['content'][:50]}...")
        return True

    def call_vision_api(self, image_bytes, prompt_text):
        img_base64 = base64.b64encode(image_bytes).decode()
        headers = {"Authorization": f"Bearer {self.vision_api_key}", "Content-Type": "application/json"}
        messages = [
            {"role": "user", "content": [
                {"type": "image_url", "image_url": {"url": f"data:image/png;base64,{img_base64}"}},
                {"type": "text", "text": prompt_text}
            ]}
        ]
        with self._model_lock:
            model = self.vision_model
            temp = self.vision_temp
        payload = {
            "model": model,
            "messages": messages,
            "max_tokens": self.vision_max_tokens,
            "temperature": temp
        }
        try:
            resp = requests.post(self.vision_api_url, headers=headers, json=payload, timeout=60)
            if resp.status_code == 200:
                data = resp.json()
                choices = data.get("choices", [])
                if choices:
                    reply = choices[0].get("message", {}).get("content", "").strip()
                    if not reply:
                        reply = "（无有效描述）"
                    return reply
                else:
                    logger.warning("视觉模型返回无 choices")
                    return None
            else:
                logger.warning(f"视觉模型API错误: {resp.status_code} - {resp.text}")
                return None
        except Exception as e:
            logger.error(f"视觉模型调用失败: {e}")
            return None

    def _process_image(self, chat_name, sender, msg_obj):
        """点击图片消息打开预览 → 截图 → 关预览 → 送视觉模型"""
        msg_class = type(msg_obj).__name__
        logger.info(f"📷 收到 {sender} 的图片，开始识别...")
        logger.debug(f"[处理] sender={sender} chat={chat_name} msg_class={msg_class}")
        tmp_path = None
        try:
            # 1. 确保消息可见
            msg_obj.roll_into_view()
            time.sleep(0.3)

            # 2. 获取控件（用 getattr，不用 __dict__——修复根因1）
            control = getattr(msg_obj, 'control', None)
            logger.debug(f"[处理] control 存在: {control is not None}, "
                        f"类型: {type(control).__name__ if control else 'N/A'}")

            if not control or not hasattr(control, 'BoundingRectangle'):
                logger.error("[处理] 无法定位图片消息控件")
                self._send_text("图片处理失败：无法定位消息", chat_name)
                return False

            # 3. 点击控件中心打开图片预览（应用竖图偏移校准）
            rect = control.BoundingRectangle
            off = self.image_click_offset or [0, 0]
            click_x = rect.left + rect.width() // 2 + int(off[0])
            click_y = rect.top + rect.height() // 2 + int(off[1])
            logger.debug(f"[处理] 点击图片: ({click_x}, {click_y}) 偏移={off}")
            pyautogui.click(click_x, click_y)
            time.sleep(1.0)

            # 4. 查找预览窗口 → 截图（修复根因2：只有确认预览开着才按ESC）
            import uiautomation as auto
            preview = auto.WindowControl(Name="图片和视频", ClassName="mmui::PreviewWindow")
            preview_open = preview.Exists(0, 0)

            if preview_open:
                r = preview.BoundingRectangle
                logger.debug(f"[处理] 预览窗口: ({r.left},{r.top}) {r.width()}x{r.height()}")
                screenshot = pyautogui.screenshot(region=(r.left, r.top, r.width(), r.height()))
                # 只在确认预览开着时才关闭
                pyautogui.press('esc')
                time.sleep(0.3)
            else:
                logger.warning("[处理] 未找到预览窗口，使用主窗口截图")
                wechat_win = auto.WindowControl(Name="微信", ClassName="mmui::MainWindow")
                wr = wechat_win.BoundingRectangle
                screenshot = pyautogui.screenshot(region=(wr.left, wr.top, wr.width(), wr.height()))
                # 不要按 ESC——没有预览窗口，ESC 会关掉微信主窗口

            # 5. 保存截图到临时文件
            with tempfile.NamedTemporaryFile(suffix='.png', delete=False) as tmpfile:
                screenshot.save(tmpfile.name, format='PNG')
                tmp_path = tmpfile.name
            logger.debug(f"[处理] 截图已保存 ({os.path.getsize(tmp_path)} bytes)")

            # 6. 调用视觉模型
            with open(tmp_path, 'rb') as f:
                img_bytes = f.read()
            logger.debug(f"[处理] 调用视觉模型 ({len(img_bytes)} bytes)...")
            vision_reply = self.call_vision_api(img_bytes, self.vision_prompt)

            if vision_reply:
                logger.info(f"📷 图片识别完成 ({len(vision_reply)} 字符)")
                return self._reply_with_vision(chat_name, sender, vision_reply)
            else:
                logger.warning("[处理] 视觉模型返回空")
                self._send_text("图片识别失败了，可能是什么地方出了问题～", chat_name)
                return False

        except Exception as e:
            logger.error(f"[处理] 异常: {e}", exc_info=True)
            self._send_text("图片处理出错，请重试", chat_name)
            return False
        finally:
            if tmp_path and os.path.exists(tmp_path):
                try:
                    os.unlink(tmp_path)
                    logger.debug(f"[处理] 已清理临时文件")
                except Exception as e:
                    logger.warning(f"[处理] 清理临时文件失败: {e}")

    def _reply_with_vision(self, chat_name, sender, vision_reply):
        """根据视觉模型返回的图片描述，生成回复并发送"""
        logger.debug(f"[图片处理] 视觉模型返回: {vision_reply[:100]}...")
        self._add_history(chat_name, "assistant", f"[图片描述] {vision_reply}")
        refine_prompt = f"用户发来一张图片，内容描述如下：{vision_reply}\n请根据这个描述，以{self.nickname}的身份回复用户。"
        is_group = '群' in chat_name or '集团' in chat_name
        final_reply = self.call_chat_ai(chat_name, refine_prompt, sender_name=sender, is_group=is_group)
        self._send_text(final_reply, chat_name)
        return True

    def _extract_file_text(self, filepath):
        """从文件中提取文本内容，支持纯文本、docx、xlsx、pdf"""
        filename = os.path.basename(filepath)
        ext = os.path.splitext(filepath)[1].lower()

        # 纯文本文件
        text_extensions = {
            '.txt', '.py', '.java', '.js', '.ts', '.html', '.css', '.json',
            '.xml', '.yaml', '.yml', '.md', '.csv', '.log', '.ini', '.cfg',
            '.sh', '.bat', '.c', '.cpp', '.h', '.hpp', '.rs', '.go', '.rb',
            '.php', '.sql', '.r', '.m', '.swift', '.kt', '.scala', '.lua',
            '.toml', '.tex', '.svg', '.pl', '.ps1', '.conf', '.properties',
        }
        if ext in text_extensions:
            for enc in ('utf-8', 'gbk', 'gb2312', 'latin-1'):
                try:
                    with open(filepath, 'r', encoding=enc) as f:
                        return f.read()
                except (UnicodeDecodeError, UnicodeError):
                    continue
            logger.warning(f"[文件] 无法以任何编码读取: {filename}")
            return None

        # .docx
        if ext == '.docx':
            try:
                import docx
                doc = docx.Document(filepath)
                text = '\n'.join([para.text for para in doc.paragraphs])
                return text if text.strip() else None
            except ImportError:
                logger.warning("[文件] 未安装 python-docx 库")
                return None
            except Exception as e:
                logger.warning(f"[文件] 读取 docx 失败: {e}")
                return None

        # .doc（旧版 Word）
        if ext == '.doc':
            text = self._extract_office_com_text(filepath, 'Word.Application')
            if text:
                return text
            return None

        # .pptx
        if ext == '.pptx':
            try:
                import pptx
                prs = pptx.Presentation(filepath)
                slides_text = []
                for i, slide in enumerate(prs.slides, 1):
                    slide_lines = [f"--- 幻灯片 {i} ---"]
                    for shape in slide.shapes:
                        if shape.has_text_frame:
                            for para in shape.text_frame.paragraphs:
                                if para.text.strip():
                                    slide_lines.append(para.text)
                    if len(slide_lines) > 1:
                        slides_text.append('\n'.join(slide_lines))
                result = '\n\n'.join(slides_text)
                return result if result.strip() else None
            except ImportError:
                logger.warning("[文件] 未安装 python-pptx 库")
                return None
            except Exception as e:
                logger.warning(f"[文件] 读取 pptx 失败: {e}")
                return None

        # .ppt（旧版 PowerPoint）
        if ext == '.ppt':
            text = self._extract_office_com_text(filepath, 'PowerPoint.Application')
            if text:
                return text
            return None

        # .xlsx
        if ext == '.xlsx':
            try:
                import openpyxl
                wb = openpyxl.load_workbook(filepath, read_only=True, data_only=True)
                all_text = []
                for sheet_name in wb.sheetnames:
                    ws = wb[sheet_name]
                    sheet_lines = [f"--- 工作表: {sheet_name} ---"]
                    for row in ws.iter_rows(values_only=True):
                        row_text = '\t'.join([
                            str(cell) if cell is not None else '' for cell in row
                        ])
                        if row_text.strip():
                            sheet_lines.append(row_text)
                    all_text.append('\n'.join(sheet_lines))
                wb.close()
                result = '\n\n'.join(all_text)
                return result if result.strip() else None
            except ImportError:
                logger.warning("[文件] 未安装 openpyxl 库")
                return None
            except Exception as e:
                logger.warning(f"[文件] 读取 xlsx 失败: {e}")
                return None

        # .xls（旧版 Excel）
        if ext == '.xls':
            # 优先用 xlrd，失败了用 Excel COM
            try:
                import xlrd
                wb = xlrd.open_workbook(filepath)
                all_text = []
                for sheet in wb.sheets():
                    sheet_lines = [f"--- 工作表: {sheet.name} ---"]
                    for row_idx in range(sheet.nrows):
                        row_values = sheet.row_values(row_idx)
                        row_text = '\t'.join([
                            str(cell) if cell != '' else '' for cell in row_values
                        ])
                        if row_text.strip():
                            sheet_lines.append(row_text)
                    all_text.append('\n'.join(sheet_lines))
                result = '\n\n'.join(all_text)
                return result if result.strip() else None
            except ImportError:
                pass
            except Exception as e:
                logger.debug(f"[文件] xlrd 读取失败: {e}")
            # 回退到 Excel COM
            text = self._extract_office_com_text(filepath, 'Excel.Application')
            if text:
                return text
            return None

        # .pdf

        # 未知扩展名，尝试按文本读取
        try:
            with open(filepath, 'r', encoding='utf-8') as f:
                text = f.read()
            if text.strip():
                logger.debug(f"[文件] 未知扩展名 .{ext}，按文本读取成功")
                return text
        except Exception:
            pass

        return None

    def _reply_with_file(self, chat_name, sender, file_text, filename):
        """根据文件内容生成回复并发送"""
        logger.debug(f"[文件处理] 内容前100字符: {file_text[:100]}...")
        self._add_history(chat_name, "assistant",
                          f"[文件内容: {filename}] {file_text}")
        refine_prompt = (
            f"用户发来一个文件（{filename}），内容如下：\n\n"
            f"{file_text}\n\n"
            f"请根据这个文件内容，以{self.nickname}的身份回复用户。"
        )
        is_group = '群' in chat_name or '集团' in chat_name
        final_reply = self.call_chat_ai(chat_name, refine_prompt,
                                        sender_name=sender, is_group=is_group)
        self._send_text(final_reply, chat_name)
        return True

    def _has_bot_reply_after(self, msgs, idx, window=5):
        """检查 msgs[idx] 之后 window 条内是否已有 bot 自己的回复。
        用于跳过已处理过的图片/文件消息，防止重复处理。"""
        end = min(idx + 1 + window, len(msgs))
        for j in range(idx + 1, end):
            m = msgs[j]
            s = getattr(m, "sender", None)
            if s is None:
                continue
            if s == "self" or s == self.nickname or "Self" in str(type(m)):
                return True
        return False

    def _find_latest_file(self, directory):
        """递归查找目录下最近修改的文件（显示名定位失败的兜底扫描）"""
        latest_path = None
        latest_mtime = 0
        try:
            for root, dirs, files in os.walk(directory):
                for fname in files:
                    full_path = os.path.join(root, fname)
                    try:
                        mtime = os.path.getmtime(full_path)
                        if mtime > latest_mtime:
                            latest_mtime = mtime
                            latest_path = full_path
                    except OSError:
                        continue
        except Exception as e:
            logger.error(f"[文件] 遍历目录失败: {e}")
            return None
        return latest_path

    def _extract_file_display_name(self, msg):
        """从 FileMessage 提取显示文件名。
        wxauto4 的 content 格式：'文件\\n<文件名>\\n[<大小>\\n]微信电脑版'
        （实测：'文件\\n养生规划表.html\\n微信电脑版'）
        返回文件名或 None"""
        try:
            content = getattr(msg, "content", "") or ""
            m = re.search(r"^文件\n([^\n]+)", content, flags=re.M)
            if m:
                return m.group(1).strip()
            # 兜底：按 repattern 解析
            rep = getattr(msg, "repattern", None)
            if rep:
                m2 = re.search(rep, content)
                if m2 and m2.group(1):
                    return m2.group(1).strip()
        except Exception as e:
            logger.error(f"[文件] 提取文件名失败: {e}")
        return None

    def _find_file_by_display_name(self, display_name):
        """按消息中的显示文件名在接收目录精确定位（微信 4.0 下载命名
        '<hash>_<msgid>_m_<原名>'，目录文件名包含原名；bot 回传的成果副本
        是干净原名，不同名时天然不命中）。多个候选（重名 (1)(2)…）取时间戳最新。
        返回路径或 None"""
        if not display_name:
            return None
        file_dir = self.file_storage_path
        if not file_dir or not os.path.isdir(file_dir):
            return None
        dstem = re.sub(r"\(\d+\)$", "", os.path.splitext(display_name)[0])
        best = None
        best_key = (-1, -1)  # (ctime, 微信重名编号)：ctime 相同（微信保留源时间戳）时取编号最大 = 最近下载
        try:
            for root, dirs, files in os.walk(file_dir):
                for fname in files:
                    fstem = re.sub(r"\(\d+\)$", "", os.path.splitext(fname)[0])
                    if dstem not in fstem:
                        continue
                    full = os.path.join(root, fname)
                    try:
                        ts = os.path.getctime(full)
                    except OSError:
                        continue
                    m_dup = re.search(r"\((\d+)\)$", os.path.splitext(fname)[0])
                    dup = int(m_dup.group(1)) if m_dup else 0
                    key = (ts, dup)
                    if key > best_key:
                        best_key = key
                        best = full
        except Exception as e:
            logger.error(f"[文件] 按文件名定位失败: {e}")
            return None
        if best:
            logger.info(f"[文件] 按消息文件名定位: {os.path.basename(best)}")
        return best

    def _process_file(self, chat_name, sender, msg_obj):
        """处理文件消息：按消息显示名在微信接收目录定位（wxauto4 download() 不可用）
        -> 提取文字 -> 发送给 AI"""
        logger.info(f"📁 收到 {sender} 的文件，开始处理...")

        try:
            # 1. 从消息提取显示文件名并定位接收目录中的文件
            display_name = self._extract_file_display_name(msg_obj)
            file_path = self._find_file_by_display_name(display_name) if display_name else None
            if not file_path:
                logger.error(f"[文件] 未在接收目录定位到文件: {display_name!r}")
                self._send_text("文件下载失败，请重试～", chat_name)
                return False

            filename = os.path.basename(file_path)
            file_size = os.path.getsize(file_path)
            logger.info(f"📁 文件已定位: {filename} ({file_size} bytes)")

            # 2. 提取文本
            text_content = self._extract_file_text(file_path)
            if text_content is None:
                logger.warning(f"[文件] 无法提取文本（格式不支持或内容为空）")
                self._send_text(f"收到文件「{filename}」，但这个格式我看不懂呢～", chat_name)
                return False

            logger.info(f"📁 文件「{filename}」提取了 {len(text_content)} 字符，发送给 AI...")
            return self._reply_with_file(chat_name, sender, text_content, filename)

        except Exception as e:
            logger.error(f"[文件] ❌ 异常: {e}", exc_info=True)
            self._send_text("文件处理出错，请重试～", chat_name)
            return False

    def _extract_office_com_text(self, filepath, app_name):
        """通过 Office COM 自动化提取旧格式（.doc/.ppt/.xls）文本，失败则二进制兜底"""
        # 方法1: Office COM 自动化
        try:
            import comtypes.client
            app = comtypes.client.CreateObject(app_name)
            app.Visible = False

            if 'Word' in app_name:
                doc = app.Documents.Open(filepath)
                text = doc.Content.Text
                doc.Close()
            elif 'PowerPoint' in app_name:
                prs = app.Presentations.Open(filepath, WithWindow=False)
                slides = []
                for slide in prs.Slides:
                    for shape in slide.Shapes:
                        if shape.HasTextFrame:
                            slides.append(shape.TextFrame.TextRange.Text)
                text = '\n'.join(slides)
                prs.Close()
            elif 'Excel' in app_name:
                wb = app.Workbooks.Open(filepath)
                sheets = []
                for sheet in wb.Sheets:
                    used = sheet.UsedRange
                    if used:
                        rows = []
                        for row in used.Rows:
                            cells = []
                            for cell in row.Cells:
                                v = cell.Value
                                cells.append(str(v) if v is not None else '')
                            rows.append('\t'.join(cells))
                        sheets.append(
                            f"--- 工作表: {sheet.Name} ---\n" + '\n'.join(rows)
                        )
                text = '\n\n'.join(sheets)
                wb.Close()
            else:
                app.Quit()
                return None

            app.Quit()
            if text and text.strip():
                logger.debug(f"[文件] {app_name} COM 提取成功 ({len(text)} 字符)")
                return text.strip()
        except Exception as e:
            logger.debug(f"[文件] {app_name} COM 失败: {e}")

        # 方法2: 从二进制中提取可读文本（兜底方案）
        try:
            with open(filepath, 'rb') as f:
                data = f.read()
            text_parts = []
            buf = []
            for byte in data:
                if 32 <= byte < 127 or byte in (9, 10, 13):
                    buf.append(chr(byte))
                else:
                    if len(buf) >= 4:
                        text_parts.append(''.join(buf))
                    buf = []
            if len(buf) >= 4:
                text_parts.append(''.join(buf))
            result = '\n'.join(text_parts)
            if len(result) > 100:
                logger.debug(f"[文件] 二进制提取成功 ({len(result)} 字符)")
                return result
        except Exception as e:
            logger.debug(f"[文件] 二进制提取失败: {e}")

        return None

    def call_chat_ai(self, chat_id, user_msg, sender_name=None, is_group=False):
        headers = {
            "Authorization": f"Bearer {self.api_key}",
            "Content-Type": "application/json"
        }
        if is_group:
            decorated = f"群聊 - {sender_name}：{user_msg}" if sender_name else f"群聊：{user_msg}"
        else:
            decorated = f"私聊 - {sender_name}：{user_msg}" if sender_name else f"私聊：{user_msg}"
        current_time = time.strftime("%Y-%m-%d %H:%M:%S")
        messages = [
            {"role": "system", "content": self.system_prompt},
            {"role": "system", "content": f"当前时间：{current_time}"},
        ]
        for h in self._get_history(chat_id):
            if "time" in h:
                ts = h["time"]
                msg_content = f"[{ts}] {h['content']}"
            else:
                msg_content = h['content']
            messages.append({"role": h["role"], "content": msg_content})
        messages.append({"role": "user", "content": decorated})

        with self._model_lock:
            model = self.chat_model
            temp = self.chat_temperature
            top_p = self.chat_top_p

        # 上下文预算裁剪：文件全文/超长历史会撑爆模型上下文上限
        # （实测请求 272 万 token → API 400 "maximum context length"）。
        # 从最旧历史开始丢弃，保证单次请求不超模型上下文。
        messages = fit_messages_in_budget(
            messages, budget=getattr(self, "max_context_tokens", 100000))

        payload = {
            "model": model,
            "messages": messages,
            "temperature": temp,
            "top_p": top_p
        }
        last_exc = None
        for attempt in range(self.api_retry + 1):
            try:
                resp = requests.post(self.api_url, headers=headers, json=payload, timeout=self.api_timeout)
                if resp.status_code == 200:
                    data = resp.json()
                    choices = data.get("choices", [])
                    if choices and "message" in choices[0]:
                        reply = choices[0]["message"]["content"]
                    else:
                        logger.error(f"API返回异常 choices 为空: {data}")
                        return "API 返回数据异常，请检查模型名称或 API 状态"
                    reply = re.sub(r'^\[\d{4}-\d{2}-\d{2}\s\d{2}:\d{2}(:\d{2}|:xx)?\]\s*', '', reply)
                    self._add_history(chat_id, "user", decorated)
                    self._add_history(chat_id, "assistant", reply)
                    return reply
                else:
                    logger.warning(f"API错误 {resp.status_code}: {resp.text}")
                    return f"API 错误: {resp.status_code}"
            except requests.exceptions.RequestException as e:
                last_exc = e
                logger.error(f"请求失败 (第{attempt+1}次): {e}")
                if attempt < self.api_retry:
                    time.sleep(2 * (attempt + 1))
        return f"请求失败: {last_exc}"

    def _send_text(self, text, chat_id):
        try:
            cleaned_text = text.strip()
            self.wx.SendMsg(cleaned_text, chat_id)
            preview = cleaned_text[:50].replace('\n', ' ')
            logger.info(f"🤖 → [{chat_id}]: {preview}")
        except Exception as e:
            logger.error(f"发送失败: {e}")

    def fetch_models(self):
        url = self.api_url.replace("/chat/completions", "/models")
        headers = {"Authorization": f"Bearer {self.api_key}"}
        try:
            resp = requests.get(url, headers=headers, timeout=10)
            if resp.status_code == 200:
                return [m["id"] for m in resp.json().get("data", [])]
            else:
                logger.warning(f"获取模型列表失败: {resp.status_code}")
                return None
        except Exception as e:
            logger.error(f"请求模型列表异常: {e}")
            return None

    def process_new_messages(self):
        if self.paused:
            return
        if time.time() - self.last_reply_time < self.cooldown:
            return

        try:
            sessions = self.wx.GetSession()
            if not sessions:
                return

            for session in sessions:
                if hasattr(session, 'name'):
                    chat_name = session.name
                elif isinstance(session, str):
                    chat_name = session
                else:
                    continue
                if not chat_name:
                    continue

                self.wx.ChatWith(chat_name)
                time.sleep(0.2)

                msgs = self.wx.GetAllMessage()
                if not msgs:
                    continue

                logger.debug(f"[会话] {chat_name} 共 {len(msgs)} 条消息")
                recent_msgs = msgs[-10:] if len(msgs) >= 10 else msgs

                for msg in reversed(recent_msgs):
                    sender = getattr(msg, 'sender', None)
                    content = getattr(msg, 'content', '')
                    msg_type = getattr(msg, 'type', None)
                    msg_class = type(msg).__name__
                    if sender is None:
                        continue
                    if sender == "self" or 'Self' in str(type(msg)) or sender == self.nickname:
                        continue

                    msg_id = getattr(msg, 'id', None)
                    # 用消息 id 做去重 key（每条消息唯一，重发相同图片 id 也不同）
                    msg_key = f"{chat_name}_{msg_id}" if msg_id else f"{chat_name}_{sender}_{content}"

                    if msg_key in self.recent_msg_ids:
                        continue

                    # ── 调试日志（仅文件） ──
                    logger.debug(f"[MSG] chat={chat_name} sender={sender} class={msg_class} "
                                f"type={msg_type!r} content={content!r} "
                                f"id={msg_id[:12] if msg_id else '无'}")

                    # ========== 检测图片消息 ==========
                    is_image = isinstance(msg, ImageMessage)
                    # ====================================

                    if is_image:
                        logger.info(f"📷 收到 {sender} 的图片，开始识别...")
                        success = self._process_image(chat_name, sender, msg)
                        # 无论成败都标记已处理，防止重复处理
                        self.recent_msg_ids.add(msg_key)
                        if msg_id:
                            self._save_processed_id(msg_id)
                        if not success:
                            logger.warning("图片处理失败")
                        self.last_reply_time = time.time()
                        return

                    # ========== 检测文件消息 ==========
                    is_file = isinstance(msg, FileMessage)
                    # ====================================

                    if is_file:
                        # 跳过"上传中"的消息，等上传完成后的消息才处理
                        if '上传中' in str(content):
                            logger.debug(f"[文件] 文件上传中，等待完成消息...")
                            self.recent_msg_ids.add(msg_key)
                            if msg_id:
                                self._save_processed_id(msg_id)
                            continue

                        success = self._process_file(chat_name, sender, msg)
                        # 无论成败都标记已处理，防止重复处理
                        self.recent_msg_ids.add(msg_key)
                        if msg_id:
                            self._save_processed_id(msg_id)
                        if not success:
                            logger.warning("文件处理失败")
                        self.last_reply_time = time.time()
                        return

                # 没有图片/文件，处理最新文本消息
                latest = msgs[-1]
                sender = getattr(latest, 'sender', None)
                content = getattr(latest, 'content', '')
                msg_class = type(latest).__name__
                msg_type = getattr(latest, 'type', None)
                if sender is None or sender == "self" or 'Self' in str(type(latest)) or sender == self.nickname:
                    continue

                # 防止图片消息掉进文本处理
                if isinstance(latest, ImageMessage):
                    continue

                # 防止文件消息掉进文本处理
                if isinstance(latest, FileMessage):
                    continue

                msg_id = getattr(latest, 'id', None)
                msg_key = f"{chat_name}_{msg_id}" if msg_id else f"{chat_name}_{sender}_{content}"

                if msg_key in self.recent_msg_ids:
                    continue
                self.recent_msg_ids.add(msg_key)

                is_group = '群' in chat_name or '集团' in chat_name

                if is_group:
                    at_tag = f"@{self.nickname}"
                    if not content.startswith(at_tag) and at_tag not in content:
                        continue
                    question = content.replace(at_tag, "").strip()
                    if not question:
                        question = "你好呀～"
                else:
                    question = content

                logger.info(f"💬 [{chat_name}] {sender}: {question[:80]}")
                reply = self.call_chat_ai(chat_name, question, sender_name=sender, is_group=is_group)
                self._send_text(reply, chat_name)

                self.last_reply_time = time.time()
                return

        except Exception as e:
            logger.error(f"处理消息异常: {e}\n{traceback.format_exc()}")

    def run(self, stop_event=None, poll_interval=2.0):
        state = "暂停中，输入 resume 开始回复" if self.paused else "运行中"
        logger.info(f"✅ 小漓已启动（{state}）")
        while True:
            if stop_event is not None and stop_event.is_set():
                logger.info("🛑 引擎已停止")
                return
            try:
                self.process_new_messages()
            except Exception as e:
                logger.error(f"主循环异常: {e}\n{traceback.format_exc()}")
            # 可中断睡眠：stop 响应延迟 ≤100ms（控制台模式不传 stop_event，行为不变）
            remain = poll_interval
            while remain > 1e-9:
                if stop_event is not None and stop_event.is_set():
                    logger.info("🛑 引擎已停止")
                    return
                time.sleep(min(0.1, remain))
                remain -= 0.1


class Controller:
    def __init__(self, bot):
        self.bot = bot

    def start(self):
        threading.Thread(target=self._listen, daemon=True).start()

    def _listen(self):
        while True:
            try:
                cmd = input().strip().lower()
                if not cmd:
                    continue
                if cmd == "help":
                    print("""
可用命令：
  pause                     - 暂停自动回复
  resume                    - 恢复自动回复
  model                     - 交互式选择聊天模型
  model <名称>              - 直接切换聊天模型
  vision-model              - 交互式选择视觉模型
  vision-model <名称>       - 直接切换视觉模型
  chat-temp <值>            - 设置聊天模型温度 (0~2)
  chat-top-p <值>           - 设置聊天模型 top_p (0~1)
  vision-temp <值>          - 设置视觉模型温度 (0~2)
  clear                     - 清空全部对话历史
  clear <聊天ID>            - 清空指定聊天的历史
  del <聊天ID> <序号1> [序号2] [序号3-5] - 删除指定聊天中的消息
  memory <聊天ID>           - 查看指定聊天的历史消息（带序号）
  status                    - 查看当前状态（含模型信息、温度、top_p）
  quit                      - 退出程序
  help                      - 显示本帮助
""")
                elif cmd == "pause":
                    self.bot.paused = True
                    logger.info("⏸️  已暂停自动回复")
                elif cmd == "resume":
                    self.bot.paused = False
                    logger.info("▶️  已恢复自动回复")
                elif cmd == "model":
                    self._select_model("chat")
                elif cmd.startswith("model "):
                    new_model = cmd.split(" ", 1)[1].strip()
                    with self.bot._model_lock:
                        self.bot.chat_model = new_model
                    logger.info(f"🔄 聊天模型已切换为：{new_model}")
                elif cmd == "vision-model":
                    self._select_model("vision")
                elif cmd.startswith("vision-model "):
                    new_vision = cmd.split(" ", 1)[1].strip()
                    with self.bot._model_lock:
                        self.bot.vision_model = new_vision
                    logger.info(f"🔄 视觉模型已切换为：{new_vision}")
                elif cmd.startswith("chat-temp "):
                    try:
                        val = float(cmd.split(" ", 1)[1])
                        if 0 <= val <= 2:
                            self.bot.set_chat_temperature(val)
                        else:
                            logger.warning("温度值应在 0~2 之间")
                    except ValueError:
                        logger.warning("请输入有效的数字")
                elif cmd.startswith("chat-top-p "):
                    try:
                        val = float(cmd.split(" ", 1)[1])
                        if 0 <= val <= 1:
                            self.bot.set_chat_top_p(val)
                        else:
                            logger.warning("top_p 值应在 0~1 之间")
                    except ValueError:
                        logger.warning("请输入有效的数字")
                elif cmd.startswith("vision-temp "):
                    try:
                        val = float(cmd.split(" ", 1)[1])
                        if 0 <= val <= 2:
                            self.bot.set_vision_temperature(val)
                        else:
                            logger.warning("温度值应在 0~2 之间")
                    except ValueError:
                        logger.warning("请输入有效的数字")
                elif cmd == "clear":
                    self.bot.clear_history()
                elif cmd.startswith("clear "):
                    target = cmd.split(" ", 1)[1].strip()
                    self.bot.clear_history(target)
                elif cmd.startswith("del "):
                    parts = cmd.split()
                    if len(parts) < 3:
                        logger.info("用法: del <聊天ID> <序号1> [序号2] ... 或 del <聊天ID> <起始序号-结束序号>")
                        continue
                    chat_id = parts[1]
                    indices = []
                    for part in parts[2:]:
                        if '-' in part:
                            try:
                                start, end = map(int, part.split('-'))
                                if start > end:
                                    start, end = end, start
                                indices.extend(range(start, end+1))
                            except ValueError:
                                logger.warning(f"无效的范围格式: {part}")
                        else:
                            try:
                                indices.append(int(part))
                            except ValueError:
                                logger.warning(f"无效的序号: {part}")
                    if not indices:
                        continue
                    print(f"即将从聊天 '{chat_id}' 中删除序号: {sorted(set(indices))}")
                    confirm = input("确认删除？(y/n): ").strip().lower()
                    if confirm == 'y':
                        self.bot.delete_messages(chat_id, indices)
                    else:
                        logger.info("已取消删除")
                elif cmd.startswith("memory "):
                    target = cmd.split(" ", 1)[1].strip()
                    self._show_memory(target)
                elif cmd == "status":
                    self._show_status()
                elif cmd == "quit":
                    logger.info("👋 程序退出")
                    os._exit(0)
                else:
                    logger.info(f"❌ 未知命令: {cmd}")
            except (EOFError, KeyboardInterrupt):
                break

    def _show_status(self):
        total_msgs = sum(len(v) for v in self.bot.memory_db.values())
        chat_count = len(self.bot.memory_db)
        print(f"当前聊天模型：{self.bot.chat_model}")
        print(f"  聊天温度: {self.bot.chat_temperature}")
        print(f"  聊天 top_p: {self.bot.chat_top_p}")
        print(f"当前视觉模型：{self.bot.vision_model}")
        print(f"  视觉温度: {self.bot.vision_temp}")
        print(f"对话记忆：共 {chat_count} 个聊天，总计 {total_msgs} 条消息")
        if chat_count > 0:
            print("具体聊天对象：")
            for chat in self.bot.memory_db.keys():
                print(f"  - {chat}")
        print(f"当前状态：{'运行中' if not self.bot.paused else '已暂停'}")

    def _show_memory(self, target):
        matches = [chat for chat in self.bot.memory_db if target in chat]
        if not matches:
            logger.info(f"❌ 未找到包含 '{target}' 的聊天记录")
            return
        chat_key = matches[0]
        msgs = self.bot.memory_db[chat_key]
        print(f"📂 聊天对象: {chat_key}，共 {len(msgs)} 条消息：")
        for i, msg in enumerate(msgs, 1):
            role = "用户" if msg["role"] == "user" else "小漓"
            ts = msg.get("time", "未知时间")
            content = msg["content"]
            print(f"  {i}. [{ts}] {role}: {content}")

    def _select_model(self, model_type):
        was_paused = self.bot.paused
        self.bot.paused = True
        logger.info(f"⏸️  已暂停，正在获取模型列表...")
        models = self.bot.fetch_models()
        if not models:
            logger.error("无法获取模型列表")
            self.bot.paused = was_paused
            return
        for i, m in enumerate(models):
            print(f"  {i+1}. {m}")
        choice = input("请输入序号或模型名，输入 cancel 取消: ").strip()
        if choice.lower() == "cancel":
            logger.info("已取消")
            self.bot.paused = was_paused
            return
        try:
            idx = int(choice) - 1
            if 0 <= idx < len(models):
                new_model = models[idx]
            else:
                logger.error("序号超出范围")
                self.bot.paused = was_paused
                return
        except ValueError:
            new_model = choice
        with self.bot._model_lock:
            if model_type == "chat":
                self.bot.chat_model = new_model
                logger.info(f"🔄 聊天模型已切换为：{new_model}")
            else:
                self.bot.vision_model = new_model
                logger.info(f"🔄 视觉模型已切换为：{new_model}")
        ask = input("是否清空所有对话历史？(y/n): ").strip().lower()
        if ask == 'y':
            self.bot.clear_history()
        self.bot.paused = was_paused
        if not self.bot.paused:
            logger.info("▶️  已自动恢复回复")


if __name__ == "__main__":
    bot = WeChatBot(CONFIG)
    controller = Controller(bot)
    controller.start()
    bot.run()