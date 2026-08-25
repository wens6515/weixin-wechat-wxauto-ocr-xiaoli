import unittest
import importlib.util

# 加载被测模块（不依赖包路径）
spec = importlib.util.spec_from_file_location(
    "wechat_bot", r"D:\AI\小漓\xiaoli_desktop\wechat_bot.py")
wb = importlib.util.module_from_spec(spec)
spec.loader.exec_module(wb)
WeChatBot = wb.WeChatBot


class _CaptureWx:
    """捕获 send_text 的假后端"""
    def __init__(self):
        self.sent = []
    def send_text(self, chat, text):
        self.sent.append((chat, text))


def _make_bot():
    bot = WeChatBot.__new__(WeChatBot)
    bot.nickname = "小漓"
    bot._pending_placeholders = {}
    bot.wx = _CaptureWx()
    return bot


class TestSendTextSplit(unittest.TestCase):
    def test_plain_text_no_split(self):
        """不含空行：单条发送，行为零变化"""
        bot = _make_bot()
        bot._send_text("你好呀～今天过得怎么样？", "小明")
        self.assertEqual(bot.wx.sent, [("小明", "你好呀～今天过得怎么样？")])
        self.assertEqual(bot._pending_placeholders, {})

    def test_double_newline_split(self):
        """\n\n 拆成两段，逐段去首尾空白"""
        bot = _make_bot()
        bot._send_text("第一段内容\n\n第二段内容", "小明")
        self.assertEqual(bot.wx.sent, [("小明", "第一段内容"), ("小明", "第二段内容")])
        self.assertEqual(bot._pending_placeholders, {})

    def test_blank_line_with_spaces(self):
        """空行带空格（\n \n）：仍按空行拆分"""
        bot = _make_bot()
        bot._send_text("甲\n \n乙", "小明")
        self.assertEqual(bot.wx.sent, [("小明", "甲"), ("小明", "乙")])

    def test_consecutive_blank_lines(self):
        """\n\n\n 连续多空行：合并为一次拆分，不产生空段"""
        bot = _make_bot()
        bot._send_text("开头\n\n\n结尾", "小明")
        self.assertEqual(bot.wx.sent, [("小明", "开头"), ("小明", "结尾")])

    def test_placeholder_never_split(self):
        """占位消息：永不拆分，单条发送，计数 +1"""
        bot = _make_bot()
        bot._send_text("正在处理，请稍等～\n\n别急哦", "小明", placeholder=True)
        self.assertEqual(bot.wx.sent, [("小明", "正在处理，请稍等～\n\n别急哦")])
        self.assertEqual(bot._pending_placeholders, {"小明": 1})

    def test_pop_reset_once_after_split(self):
        """拆分多条后：占位计数只归零一次（pop 幂等）"""
        bot = _make_bot()
        bot._pending_placeholders["小明"] = 2
        bot._send_text("一段\n\n二段", "小明")
        self.assertEqual(bot.wx.sent, [("小明", "一段"), ("小明", "二段")])
        self.assertEqual(bot._pending_placeholders, {})

    def test_plain_text_resets_placeholder(self):
        """普通单条回复：占位归零（既有语义保留）"""
        bot = _make_bot()
        bot._pending_placeholders["小明"] = 1
        bot._send_text("回你啦", "小明")
        self.assertEqual(bot.wx.sent, [("小明", "回你啦")])
        self.assertEqual(bot._pending_placeholders, {})

    def test_leading_trailing_blank_lines(self):
        """首尾空行：不产生空段"""
        bot = _make_bot()
        bot._send_text("\n\n正文内容\n\n", "小明")
        self.assertEqual(bot.wx.sent, [("小明", "正文内容")])


if __name__ == "__main__":
    unittest.main()
