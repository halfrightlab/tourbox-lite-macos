"""
按键统计：每天在电脑上按了哪些键 / 快捷键，键盘和 TourBox 分开计数。

只记「每个键按了几次」和「每小时按了多少下」，不记顺序、不记内容：
普通打字（字母、数字、符号，含 shift）只算进一个「打字」总数，
所以从统计文件里还原不出你打过的任何一句话。
密码框会开启系统的安全输入，那期间系统本来就不让任何程序看到按键。

原理：在会话层挂一个只读的 CGEventTap（不拦截、不修改事件）。
驱动自己注入的按键带一个标记（kCGEventSourceUserData = INJECT_TAG），
统计时据此区分「键盘按的」和「TourBox 按的」。

数据：~/.tourbox/stats/YYYY-MM-DD.json，可用 TOURBOX_STATS 改目录。
"""
import os
import json
import time
import threading

from tourboxd import VK, INJECT_TAG, write_json_atomic

STATS_DIR = os.environ.get("TOURBOX_STATS", os.path.expanduser("~/.tourbox/stats"))
TYPING = "typing"           # 普通打字的汇总标签

# 键码 -> 统计用的名字（修饰键单独处理）
_NAMES = {
    0x24: "return", 0x4C: "return", 0x33: "backspace", 0x75: "forwarddelete",
    0x30: "tab", 0x31: "space", 0x35: "escape",
    0x7E: "up", 0x7D: "down", 0x7B: "left", 0x7C: "right",
    0x74: "pageup", 0x79: "pagedown", 0x73: "home", 0x77: "end",
}
_FKEYS = {0x7A: 1, 0x78: 2, 0x63: 3, 0x76: 4, 0x60: 5, 0x61: 6, 0x62: 7, 0x64: 8,
          0x65: 9, 0x6D: 10, 0x67: 11, 0x6F: 12, 0x69: 13, 0x6B: 14, 0x71: 15}
_FN_KEYCODES = {0x3F, 0xB3}       # Fn / 地球仪键

F_SHIFT, F_CTRL, F_ALT, F_CMD, F_FN = 0x20000, 0x40000, 0x80000, 0x100000, 0x800000


def _char_names():
    """可打印键的名字（a、1、/ …），只在组合键里用到，单按算打字。"""
    out = {}
    for name, vk in VK.items():
        if len(name) == 1 and vk not in out:
            out[vk] = name
    return out


def label_of(keycode, flags, chars):
    """把一次按键变成统计标签。返回 TYPING 表示普通打字。"""
    mods = [m for m, bit in (("ctrl", F_CTRL), ("alt", F_ALT), ("shift", F_SHIFT), ("cmd", F_CMD))
            if flags & bit]
    if keycode in _NAMES:
        name = _NAMES[keycode]
    elif keycode in _FKEYS:
        name = f"f{_FKEYS[keycode]}"
    elif keycode in chars:
        # 没有 cmd/ctrl/alt 的字符键就是打字（shift+字母 = 大写，也是打字）
        if not (flags & (F_CMD | F_CTRL | F_ALT)):
            return TYPING
        name = chars[keycode]
    else:
        if not mods or mods == ["shift"]:
            return TYPING        # 小键盘等
        name = f"vk{keycode}"
    return "+".join(mods + [name])


class KeyStats:
    FLUSH_SEC = 60

    def __init__(self, stats_dir=STATS_DIR):
        self.dir = stats_dir
        os.makedirs(self.dir, exist_ok=True)
        self.lock = threading.Lock()
        self.day = None
        self.data = None
        self.dirty = False
        self.tap_ok = None
        self._chars = _char_names()
        self._prev_flags = 0
        self._roll()

    # ---- 存取 ----
    def _path(self, day):
        return os.path.join(self.dir, f"{day}.json")

    def _load(self, day):
        try:
            with open(self._path(day)) as f:
                d = json.load(f)
            d.setdefault("keys", {})
            d.setdefault("hours", [0] * 24)
            return d
        except (OSError, ValueError):
            return {"keys": {}, "hours": [0] * 24}

    def _roll(self):
        """跨天时先把昨天写盘，再换成今天的文件。"""
        today = time.strftime("%Y-%m-%d")
        if today != self.day:
            if self.day and self.dirty:
                self._write()
            self.day = today
            self.data = self._load(today)

    def _write(self):
        write_json_atomic(self._path(self.day), self.data)
        self.dirty = False

    def flush(self):
        with self.lock:
            if self.dirty:
                self._write()

    def record(self, label, source):
        """source: 'kb' 键盘 / 'tb' TourBox"""
        with self.lock:
            self._roll()
            k = self.data["keys"].setdefault(label, {"kb": 0, "tb": 0})
            k[source] = k.get(source, 0) + 1
            self.data["hours"][time.localtime().tm_hour] += 1
            self.dirty = True

    # ---- 查询 ----
    def query(self, days=1):
        days = max(1, min(int(days), 90))
        with self.lock:
            self._roll()
            today = json.loads(json.dumps(self.data))
        keys, hours, daily = {}, [0] * 24, []
        now = time.time()
        for i in range(days - 1, -1, -1):
            day = time.strftime("%Y-%m-%d", time.localtime(now - i * 86400))
            d = today if day == self.day else (self._load(day) if os.path.exists(self._path(day)) else None)
            total = 0
            if d:
                for label, c in d["keys"].items():
                    acc = keys.setdefault(label, {"kb": 0, "tb": 0})
                    acc["kb"] += c.get("kb", 0)
                    acc["tb"] += c.get("tb", 0)
                    total += c.get("kb", 0) + c.get("tb", 0)
                for h, n in enumerate(d["hours"][:24]):
                    hours[h] += n
            daily.append({"day": day, "total": total})
        top = sorted(({"label": l, **c} for l, c in keys.items()),
                     key=lambda x: x["kb"] + x["tb"], reverse=True)
        return {
            "days": days,
            "tap_ok": self.tap_ok,
            "total": sum(x["kb"] + x["tb"] for x in top),
            "tourbox": sum(x["tb"] for x in top),
            "typing": next((x["kb"] + x["tb"] for x in top if x["label"] == TYPING), 0),
            "top": top[:40],
            "hours": hours,
            "daily": daily,
        }

    # ---- 监听 ----
    def _on_event(self, proxy, etype, ev, refcon):
        Q = self.Q
        try:
            if etype in (Q.kCGEventTapDisabledByTimeout, Q.kCGEventTapDisabledByUserInput):
                Q.CGEventTapEnable(self.tap, True)
                return ev
            src = "tb" if Q.CGEventGetIntegerValueField(ev, Q.kCGEventSourceUserData) == INJECT_TAG else "kb"
            keycode = Q.CGEventGetIntegerValueField(ev, Q.kCGKeyboardEventKeycode)
            flags = Q.CGEventGetFlags(ev)
            if etype == Q.kCGEventKeyDown:
                if not Q.CGEventGetIntegerValueField(ev, Q.kCGKeyboardEventAutorepeat):
                    self.record(label_of(keycode, flags, self._chars), src)
            elif etype == Q.kCGEventFlagsChanged:
                # 修饰键只统计 Fn 单独按下（按住说话的次数），其余修饰键算在组合键里
                if keycode in _FN_KEYCODES and flags & F_FN and not self._prev_flags & F_FN:
                    self.record("fn", src)
                self._prev_flags = flags
        except Exception:
            pass
        return ev

    def _tap_loop(self):
        import Quartz as Q
        self.Q = Q
        mask = Q.CGEventMaskBit(Q.kCGEventKeyDown) | Q.CGEventMaskBit(Q.kCGEventFlagsChanged)
        self.tap = Q.CGEventTapCreate(Q.kCGSessionEventTap, Q.kCGTailAppendEventTap,
                                      Q.kCGEventTapOptionListenOnly, mask, self._on_event, None)
        if self.tap is None:
            self.tap_ok = False
            print("[stats] 无法监听按键：请在 系统设置 → 隐私与安全性 → 输入监控 里勾选当前 python3，"
                  "然后重启驱动", flush=True)
            try:
                Q.CGRequestListenEventAccess()
            except Exception:
                pass
            return
        self.tap_ok = True
        try:
            print(f"[perm] 输入监控(统计)={'已授权' if Q.CGPreflightListenEventAccess() else '未授权（辅助功能权限通常也够用）'}",
                  flush=True)
        except Exception:
            pass
        src = Q.CFMachPortCreateRunLoopSource(None, self.tap, 0)
        Q.CFRunLoopAddSource(Q.CFRunLoopGetCurrent(), src, Q.kCFRunLoopCommonModes)
        Q.CGEventTapEnable(self.tap, True)
        print(f"[stats] 按键统计已开启 → {self.dir}", flush=True)
        Q.CFRunLoopRun()

    def _flush_loop(self):
        while True:
            time.sleep(self.FLUSH_SEC)
            try:
                self.flush()
            except Exception as e:
                print(f"[stats] 写入失败: {e}", flush=True)

    def start(self):
        threading.Thread(target=self._tap_loop, name="keystats_tap", daemon=True).start()
        threading.Thread(target=self._flush_loop, name="keystats_flush", daemon=True).start()
