"""按键统计的离线测试：标签规则、计数落盘、按天汇总。不挂真正的事件监听。"""
import os
import sys
import json
import tempfile
import unittest

sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

from keystats import KeyStats, label_of, _char_names, TYPING, F_SHIFT, F_CMD, F_CTRL, F_FN

CHARS = _char_names()


class LabelTest(unittest.TestCase):
    def test_plain_typing_is_not_recorded_per_letter(self):
        self.assertEqual(label_of(0x00, 0, CHARS), TYPING)            # a
        self.assertEqual(label_of(0x00, F_SHIFT, CHARS), TYPING)      # A
        self.assertEqual(label_of(0x2C, 0, CHARS), TYPING)            # /

    def test_shortcuts_and_function_keys(self):
        self.assertEqual(label_of(0x08, F_CMD, CHARS), "cmd+c")
        self.assertEqual(label_of(0x15, F_CMD | F_SHIFT, CHARS), "shift+cmd+4")
        self.assertEqual(label_of(0x24, F_CMD, CHARS), "cmd+return")
        self.assertEqual(label_of(0x33, 0, CHARS), "backspace")
        self.assertEqual(label_of(0x7B, F_FN, CHARS), "left")         # 方向键自带 fn 位，不算进标签
        self.assertEqual(label_of(0x08, F_CTRL, CHARS), "ctrl+c")


class StoreTest(unittest.TestCase):
    def setUp(self):
        self.dir = tempfile.mkdtemp()
        self.s = KeyStats(self.dir)

    def test_record_query_flush(self):
        for _ in range(3):
            self.s.record("cmd+c", "kb")
        self.s.record("cmd+c", "tb")
        self.s.record(TYPING, "kb")
        q = self.s.query(1)
        self.assertEqual(q["total"], 5)
        self.assertEqual(q["tourbox"], 1)
        self.assertEqual(q["typing"], 1)
        self.assertEqual(q["top"][0], {"label": "cmd+c", "kb": 3, "tb": 1})
        self.assertEqual(sum(q["hours"]), 5)
        self.s.flush()
        with open(os.path.join(self.dir, f"{self.s.day}.json")) as f:
            self.assertEqual(json.load(f)["keys"]["cmd+c"], {"kb": 3, "tb": 1})
        # 重启后接着累计
        s2 = KeyStats(self.dir)
        s2.record("cmd+c", "kb")
        self.assertEqual(s2.query(1)["top"][0]["kb"], 4)

    def test_multi_day_sum(self):
        import time
        yday = time.strftime("%Y-%m-%d", time.localtime(time.time() - 86400))
        with open(os.path.join(self.dir, f"{yday}.json"), "w") as f:
            json.dump({"keys": {"cmd+v": {"kb": 7, "tb": 2}}, "hours": [0] * 24}, f)
        self.s.record("cmd+v", "kb")
        self.assertEqual(self.s.query(1)["total"], 1)
        q = self.s.query(7)
        self.assertEqual(q["total"], 10)
        self.assertEqual(len(q["daily"]), 7)
        self.assertEqual(q["daily"][-2]["total"], 9)


if __name__ == "__main__":
    unittest.main()
