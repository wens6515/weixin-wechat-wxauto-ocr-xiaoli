# -*- coding: utf-8 -*-
"""深层存档的键一致性：同一会话的 OCR 变体名不得把深层 jsonl 分裂成多份。

真机场景：同一个会话（如「“强盗”集团」）在不同帧里会被 OCR 读成
「强盗”集团」「"强盗"集团」「" 强盗 " 集团」等变体。

memory.json 的键经 _resolve_key 归一到首次写入的原文键（这层已修）；
但深层记忆是按会话名 percent-encode 出**文件名**——若写入路径不先把
变体名解析到已有键，变体名会另建一个 jsonl，于是：

* 记忆页「深层」计数只统计其中一个文件 → 数字偏小
* recall_memory 读的也只有一个文件 → 另一半历史「看起来丢了」
* delete_deep_message 按行号删的是另一个文件 → 删错

本组测试钉住：变体名与原文名读写的是同一个深层文件。
"""
import os
import sys
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from xiaoli_app.memory_store import MemoryStore

RAW = "“强盗”集团"      # 原文名（升级后的键形态）
VAR = "强盗”集团"        # 同一会话的 OCR 变体（前引号丢失）


def make_store(tmp):
    return MemoryStore(memory_file=os.path.join(tmp, "memory.json"),
                       cap_fn=lambda c: 5)


class TestDeepFileKeyConsistency(unittest.TestCase):
    def _fill(self, store, name, tag, n=6):
        for i in range(n):
            store.add(name, "user", f"{tag}-{i}", deep_enabled=True)

    def test_variant_name_does_not_split_deep_file(self):
        """原文名先写、变体名后写 → 只有一个深层文件、一份计数。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.load()
            self._fill(store, RAW, "raw")
            self._fill(store, VAR, "var")

            self.assertEqual(list(store.memory_db), [RAW],
                             "内存键应只有一个（等价匹配）")
            deep_dir = os.path.join(tmp, "memory_deep")
            files = sorted(os.listdir(deep_dir)) if os.path.isdir(deep_dir) else []
            self.assertEqual(len(files), 1,
                             f"深层文件应只有一个，实际 {files}")
            self.assertEqual(list(store.deep_count), [RAW],
                             f"深层计数键应只有一个，实际 {list(store.deep_count)}")
            self.assertEqual(store.count_deep(RAW), store.count_deep(VAR),
                             "两个名字应指向同一个深层文件")
            self.assertEqual(len(list(store.iter_deep(RAW))),
                             len(list(store.iter_deep(VAR))),
                             "两个名字应读到同一批深层消息")

    def test_reads_full_history_written_under_raw_name(self):
        """变体名必须能读到原文名下写进去的全部历史（不丢、不分裂）。"""
        with tempfile.TemporaryDirectory() as tmp:
            store = make_store(tmp)
            store.load()
            self._fill(store, RAW, "raw", n=8)   # cap=5 → 3 条溢出进深层
            deep = [m["content"] for m in store.iter_deep(VAR)]
            self.assertEqual(deep, ["raw-0", "raw-1", "raw-2"],
                             "变体名应读到原文名下归档的全部 3 条")
            self.assertEqual(store.count_deep(VAR), 3)

    def test_legacy_normalized_file_still_reachable(self):
        """升级前（键归一化时代）写下的深层文件名继续可读可追加。"""
        with tempfile.TemporaryDirectory() as tmp:
            from urllib.parse import quote
            store = make_store(tmp)
            store.load()
            legacy_name = "强盗集团"          # 旧归一化名
            deep_dir = os.path.join(tmp, "memory_deep")
            os.makedirs(deep_dir, exist_ok=True)
            legacy_path = os.path.join(deep_dir, quote(legacy_name, safe="") + ".jsonl")
            with open(legacy_path, "w", encoding="utf-8") as f:
                f.write('{"role": "user", "content": "旧历史"}\n')

            self.assertEqual(store.deep_path(RAW), legacy_path,
                             "新名文件不存在时应回退到旧归一化名文件")
            self.assertEqual(len(list(store.iter_deep(RAW))), 1)
            # 追加也落在旧文件上（不新开一份）
            store.append_deep(RAW, {"role": "user", "content": "新追加"})
            self.assertEqual(len(list(store.iter_deep(RAW))), 2)
            self.assertEqual(sorted(os.listdir(deep_dir)),
                             [os.path.basename(legacy_path)])


if __name__ == "__main__":
    unittest.main()
