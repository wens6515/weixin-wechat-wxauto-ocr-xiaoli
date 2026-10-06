# -*- coding: utf-8 -*-
"""文件文本提取（模块函数）：txt/docx/doc/pptx/ppt/xlsx/xls/pdf 纯文本
抽取 + Office COM 兜底 + 消息显示文件名解析。

从 wechat_bot.py 拆出（纯移动，零逻辑改动）；WeChatBot 的
_extract_file_text / _extract_file_display_name / _extract_office_com_text
保留为薄委托，实例级调用形态不变。
"""
from __future__ import annotations

import logging
import os
import re

logger = logging.getLogger("xiaoli")

# 文件名 token 提取（自 wechat_bot 迁入）：严格扩展名 + OCR 读花后缀的宽松兜底。

_FILE_TOKEN_RE = re.compile(
    r"[\w\u4e00-\u9fff][\w\u4e00-\u9fff\-.+()（）]*?"
    r"\.(?:docx?|xlsx?|pptx?|pdf|txt|md|html?|json|csv|zip|rar|7z|png|jpe?g|gif|mp4|mp3)",
    re.I)
# 兜底：已知扩展名一个都没匹配到（OCR 把后缀读花，真机事故 xlsx→xIsx）时，
# 用「字母开头的 2~5 位字母数字后缀」再扫一遍——主干 ≥2 字符、后缀首字符
# 必须是字母，避免把文件大小「19.6K」（后缀 6K 以数字开头）当成文件名。
# 只在文件卡片流程里用（strict 未命中才落到这里），误配由按名查找兜底。
_FILE_TOKEN_LOOSE_RE = re.compile(
    r"[\w\u4e00-\u9fff][\w\u4e00-\u9fff\-.+()（）]+?"
    r"\.(?:[A-Za-z][A-Za-z0-9]{1,4})(?![A-Za-z0-9])")

def _extract_file_name_token(text):
    """从 OCR 文本拆出干净文件名 token（含常见文档扩展名，去掉大小/图标字符）。

    '部门简介+纳新宣传.docx 20.1K W' → '部门简介+纳新宣传.docx'
    文件卡片 OCR 会把文件名、大小（20.1K 带小数点）、图标字符（W/P/?）读成
    一串——整串当文件名传给 os.path.splitext 时，20.1K 的小数点会被误当
    扩展名分隔符，导致主干匹配失败。先拆出真实文件名 token。

    OCR 换行会把文件名切成两半（真机 '…二轮面 试评分表.xlsx'）——token 只
    取到空格后那段是预期行为：_find_file_by_display_name 走主干子串匹配，
    片段命中全名，容忍这种截断。后缀读花（xlsx → xIsx）时走宽松后缀兜底。
    """
    toks = _FILE_TOKEN_RE.findall(text or "")
    if toks:
        return toks[0]
    toks = _FILE_TOKEN_LOOSE_RE.findall(text or "")
    return toks[0] if toks else None

def extract_file_text(filepath):
    """从文件中提取文本内容，支持纯文本、docx/doc（Office COM）、xlsx/xls、pdf（pypdf）。"""
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

    # .pdf（pypdf 纯 Python，零系统依赖；扫描件无文本层时提取为空走 None）
    if ext == '.pdf':
        try:
            from pypdf import PdfReader
            pages = []
            for page in PdfReader(filepath).pages:
                t = page.extract_text() or ""
                if t.strip():
                    pages.append(t.strip())
            text = '\n'.join(pages)
            return text if text.strip() else None
        except ImportError:
            logger.warning("[文件] 未安装 pypdf 库")
            return None
        except Exception as e:
            logger.warning(f"[文件] 读取 pdf 失败: {e}")
            return None

    # .doc（旧版 Word）
    if ext == '.doc':
        text = extract_office_com_text(filepath, 'Word.Application')
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
        text = extract_office_com_text(filepath, 'PowerPoint.Application')
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
        text = extract_office_com_text(filepath, 'Excel.Application')
        if text:
            return text
        return None

    # 未知扩展名（含 PDF，暂不支持解析）尝试按文本读取；二进制读取失败返回 None
    try:
        with open(filepath, 'r', encoding='utf-8') as f:
            text = f.read()
        if text.strip():
            logger.debug(f"[文件] 未知扩展名 .{ext}，按文本读取成功")
            return text
    except Exception:
        pass

    return None

def extract_file_display_name(msg):
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
        # 视觉后端兼容：content 即显示文件名（无 '文件\n' 前缀）
        if content and "\\n" not in content and "\n" not in content:
            # 纯文件名（不含换行/前缀）——视觉后端 file 消息格式。
            # 但消息区 OCR 可能把多条文件消息合并成一个文本块
            # （实测 '新宣传.docx 部门简介+纳新宣传.docx W'，文件图标被
            # OCR 成尾部杂字符）——整串当文件名必然匹配失败，需先拆出
            # 真实文件名（含常见文档扩展名的 token，取第一个）。
            name = content.strip()
            if name:
                return _extract_file_name_token(name) or name
    except Exception as e:
        logger.error(f"[文件] 提取文件名失败: {e}")
    return None

def extract_office_com_text(filepath, app_name):
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
