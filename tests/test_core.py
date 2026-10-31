"""离线测试：用假注入器验证解析与按键状态机，不会真的发出系统按键。"""
import os
import sys
import json
import time
import tempfile
import unittest

TMP = tempfile.mkdtemp()
os.environ["TOURBOX_CONFIG"] = os.path.join(TMP, "config.json")
os.environ["TOURBOX_MAPPING"] = os.path.join(TMP, "mappings.json")
sys.path.insert(0, os.path.dirname(os.path.dirname(os.path.abspath(__file__))))

import tourboxd
from tourboxd import parse_spec, Hub


class FakeInjector:
    MODIFIER_VKS = tourboxd.Injector.MODIFIER_VKS
    PHASE_BEGAN, PHASE_CHANGED, PHASE_ENDED = 1, 2, 4

    def __init__(self):
        self.log = []

    def tap(self, a): self.log.append(("tap", a.spec))
    def key_down(self, a, autorepeat=False): self.log.append(("down", a.spec, autorepeat))
    def key_up(self, a): self.log.append(("up", a.spec))
    def scroll_pixel(self, px, phase=0): self.log.append(("scroll", px, phase))


def make_hub():
    tourboxd.Injector = FakeInjector
    h = Hub()
    return h


class ParseTest(unittest.TestCase):
    def test_kinds(self):
        self.assertEqual(parse_spec("fn").kind, "key")
        self.assertTrue(parse_spec("fn").hold)
        self.assertFalse(parse_spec("fn").repeatable)
        self.assertTrue(parse_spec("backspace").repeatable)
        self.assertFalse(parse_spec("cmd+c").hold)
        self.assertTrue(parse_spec("hold:shift+space").hold)
        self.assertEqual(parse_spec("volumeup").kind, "media")
        self.assertEqual(parse_spec("scrollup").dir, -1)
        self.assertEqual(parse_spec("double:fn").inner.spec, "fn")
        self.assertEqual(parse_spec("CMD+Shift+4").vk, 0x15)

    def test_invalid(self):
        for bad in ["", "cmd+", "foo", "hyper+a", "scroll:x"]:
            with self.assertRaises(ValueError):
                parse_spec(bad)


class HubTest(unittest.TestCase):
    def setUp(self):
        for p in (os.environ["TOURBOX_CONFIG"], os.environ["TOURBOX_MAPPING"]):
            if os.path.exists(p):
                os.remove(p)
        self.h = make_hub()
        self.log = self.h.inj.log

    def test_fn_hold_and_release(self):
        self.h.handle(0x02)          # 横键按下
        self.h.handle(0x82)          # 松开
        self.assertEqual(self.log, [("down", "fn", False), ("up", "fn")])

    def test_combo_taps_on_press(self):
        self.h.handle(0x22)
        self.h.handle(0xA2)
        self.assertEqual(self.log, [("tap", "cmd+c")])

    def test_remap_while_held_releases_original(self):
        self.h.handle(0x02)
        self.h.set_map("K02", "escape")
        self.h.handle(0x82)
        self.assertEqual(self.log[-1], ("up", "fn"))

    def test_invalid_map_rejected(self):
        with self.assertRaises(ValueError):
            self.h.set_map("K02", "notakey")
        self.assertEqual(json.load(open(os.environ["TOURBOX_CONFIG"]))["map"]["K02"], "fn")

    def test_shared_target_refcount(self):
        self.h.set_map("K00", "fn")
        self.h.handle(0x00); self.h.handle(0x02)
        self.h.handle(0x80); self.h.handle(0x82)
        self.assertEqual(self.log, [("down", "fn", False), ("up", "fn")])

    def test_release_all(self):
        self.h.handle(0x02)
        self.h.release_all()
        self.assertEqual(self.log[-1], ("up", "fn"))
        self.h.handle(0x82)          # 迟到的松开不能再发一次 up
        self.assertEqual(self.log.count(("up", "fn")), 1)

    def test_knob_divider(self):
        for _ in range(8):
            self.h.handle(0x44)
        self.assertEqual(self.log, [("tap", "right")] * 2)

    def test_pump_and_repeat(self):
        self.h.start = None
        import threading
        threading.Thread(target=self.h.scroll_pump, daemon=True).start()
        threading.Thread(target=self.h.auto_repeat, daemon=True).start()
        for _ in range(5):
            self.h.handle(0x09)      # 滚轮向下一格
        self.h.handle(0x00)          # 长键 backspace 按住
        time.sleep(0.8)
        self.h.handle(0x80)
        time.sleep(0.6)
        scrolls = [e for e in self.log if e[0] == "scroll"]
        self.assertEqual(scrolls[0][2], 1)                 # Began
        self.assertEqual(scrolls[-1], ("scroll", 0, 4))    # Ended
        self.assertGreater(sum(e[1] for e in scrolls), 100)
        reps = [e for e in self.log if e[:2] == ("down", "backspace") and e[2]]
        self.assertGreater(len(reps), 5)
        self.assertEqual(self.log[-1] if self.log[-1][0] == "up" else
                         [e for e in self.log if e[0] == "up"][-1], ("up", "backspace"))


if __name__ == "__main__":
    unittest.main()
