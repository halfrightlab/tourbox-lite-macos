#!/usr/bin/env python3
"""
TourBox -> macOS 全局键盘映射驱动（核心）

原理：TourBox 通过 USB CDC 串口上报单字节事件流，
按下 = code, 松开 = code | 0x80。本程序解析后，
用 Quartz CGEvent 注入到你配置的任意按键 / 组合键 / 媒体键 / 平滑滚动。

用法:
    python3 tourboxd.py            无界面运行（带界面请用 webui.py）
    python3 tourboxd.py --sniff    只打印原始字节（校准协议用）
    python3 tourboxd.py --port /dev/cu.usbmodemXXX
"""
import os
import sys
import json
import glob
import time
import argparse
import threading
import traceback

import serial

APP_DIR = os.path.dirname(os.path.abspath(__file__))
CONFIG_PATH = os.environ.get("TOURBOX_CONFIG", os.path.join(APP_DIR, "config.json"))
MAPPING_PATH = os.environ.get("TOURBOX_MAPPING", os.path.join(APP_DIR, "mappings.json"))

# 驱动注入的事件都打上这个标记（kCGEventSourceUserData），按键统计据此区分来源
INJECT_TAG = 0x70B0C5

# ---------- macOS 虚拟键码 ----------
# 真实 kVK_* 键码表（macOS 键码非字母序，必须硬编码）
VK = {
    "a": 0x00, "s": 0x01, "d": 0x02, "f": 0x03, "h": 0x04, "g": 0x05,
    "z": 0x06, "x": 0x07, "c": 0x08, "v": 0x09, "b": 0x0B, "q": 0x0C,
    "w": 0x0D, "e": 0x0E, "r": 0x0F, "y": 0x10, "t": 0x11,
    "o": 0x1F, "u": 0x20, "i": 0x22, "p": 0x23,
    "l": 0x25, "j": 0x26, "k": 0x28, "n": 0x2D, "m": 0x2E,
    "1": 0x12, "2": 0x13, "3": 0x14, "4": 0x15, "5": 0x17, "6": 0x16,
    "7": 0x1A, "8": 0x1C, "9": 0x19, "0": 0x1D,
    "f1": 0x7A, "f2": 0x78, "f3": 0x63, "f4": 0x76, "f5": 0x60,
    "f6": 0x61, "f7": 0x62, "f8": 0x64, "f9": 0x65, "f10": 0x6D,
    "f11": 0x67, "f12": 0x6F, "f13": 0x69, "f14": 0x6B, "f15": 0x71,
    "f16": 0x6A, "f17": 0x40, "f18": 0x4F, "f19": 0x50, "f20": 0x5A,
    "space": 0x31, "return": 0x24, "enter": 0x24, "escape": 0x35, "esc": 0x35,
    "delete": 0x33, "backspace": 0x33, "tab": 0x30, "capslock": 0x39,
    "up": 0x7E, "down": 0x7D, "left": 0x7B, "right": 0x7C,
    "pageup": 0x74, "pagedown": 0x79, "home": 0x73, "end": 0x77,
    "help": 0x72, "forwarddelete": 0x75, "clear": 0x47,
    # 修饰键本体：单独映射可当「按住」使用（如把某个键直接当 FN 键）
    "fn": 0x3F, "cmd": 0x37, "command": 0x37,
    "shift": 0x38, "alt": 0x3A, "option": 0x3A,
    "ctrl": 0x3B, "control": 0x3B,
    # 符号
    "-": 0x1B, "=": 0x18, "[": 0x21, "]": 0x1E, "\\": 0x2A,
    ";": 0x29, "'": 0x27, ",": 0x2B, ".": 0x2F, "/": 0x2C, "`": 0x32,
}

MOD_FLAGS = {
    "cmd": 0x00100000, "command": 0x00100000, "meta": 0x00100000,
    "shift": 0x00020000,
    "alt": 0x00080000, "option": 0x00080000, "opt": 0x00080000,
    "ctrl": 0x00040000, "control": 0x00040000,
    "fn": 0x00800000,
}

MOD_VK = {
    "cmd": 0x37, "command": 0x37, "meta": 0x37,
    "shift": 0x38,
    "alt": 0x3A, "option": 0x3A, "opt": 0x3A,
    "ctrl": 0x3B, "control": 0x3B,
    "fn": 0x3F,
}

# 媒体键不是普通键码：kVK_VolumeUp 当键盘事件发出去系统不认，
# 必须走 NSSystemDefined(subtype 8) 事件，值是 NX_KEYTYPE_*。
MEDIA_KEYS = {
    "volumeup": 0, "volumedown": 1, "mute": 7,
    "brightnessup": 2, "brightnessdown": 3,
    "play": 16, "playpause": 16, "next": 17, "previous": 18, "prev": 18,
    "fastforward": 19, "rewind": 20,
}

# ---------- 协议表（TourBox Lite 实测校准，2026-10-30） ----------
# 单字节事件流：按下 = 低 7 位码，松开 = 码 | 0x80
# 规律：Lite 按下码 == Neo 按下码 & 0x7F（两型号同源，按下/松开位相反）
# 按钮类有按下/松开对；转盘与滚轮是脉冲码，不发松开。
PROTOCOL = {
    "K00":  0x00,   # 长键      Neo: BUTTON_7
    "K02":  0x02,   # 横键      Neo: BUTTON_3
    "K03":  0x03,   # 短键      Neo: BUTTON_6
    "K22":  0x22,   # C1        Neo: BUTTON_A
    "K23":  0x23,   # C2        Neo: BUTTON_B
    "K2A":  0x2A,   # Tour      Neo: BUTTON_11
    "K37":  0x37,   # 旋钮点击  Lite 独有
    "K0A":  0x0A,   # 滚轮点击
    # 脉冲类
    "SCROLL_DOWN":  0x09,
    "SCROLL_UP":    0x49,
    "WHEEL_CCW":    0x04,
    "WHEEL_CW":     0x44,
}

# 转盘/滚轮：转一格连发若干字节、没有松开码，只能「每 N 个脉冲触发一次」
PULSE_CONTROLS = {"SCROLL_DOWN", "SCROLL_UP", "WHEEL_CCW", "WHEEL_CW"}

# TourBox Lite 实测：字节代号 -> 物理键位置（界面「识别」可改写）
DEFAULT_MAPPING = {
    "K02": "horiz", "K2A": "tour", "K00": "long", "K03": "short",
    "K22": "c1", "K23": "c2", "K0A": "scroll_click", "K37": "knob_click",
    "WHEEL_CW": "knob_cw", "WHEEL_CCW": "knob_ccw",
    "SCROLL_UP": "scroll_up", "SCROLL_DOWN": "scroll_down",
}

DEFAULT_CONFIG = {
    # 每个控件映射到什么动作，写法见 README「映射写法」
    "map": {
        "K02":           "fn",            # 横键：按住说话
        "K00":           "backspace",     # 长键
        "K03":           "return",        # 短键
        "K22":           "cmd+c",         # C1
        "K23":           "cmd+v",         # C2
        "K2A":           "escape",        # Tour
        "K0A":           "space",         # 滚轮点击
        "K37":           "cmd+return",    # 旋钮点击：输入框里换行（回车发送、cmd+回车换行）
        "WHEEL_CW":      "right",
        "WHEEL_CCW":     "left",
        "SCROLL_UP":     "scrollup",
        "SCROLL_DOWN":   "scrolldown",
    },
    # 脉冲分频：每 N 个字节才触发一次动作（scrollup/scrolldown 自己按格计算，填 1）
    "divider": {
        "WHEEL_CW": 4, "WHEEL_CCW": 4,
        "SCROLL_UP": 1, "SCROLL_DOWN": 1,
    },
    "repeat_guard_ms": 0,     # 脉冲额外去抖（毫秒），0 = 关闭
    # 按住超过这么多秒仍没收到松开字节，判定丢包并强制释放。
    # 不要太短：按住 fn 长时间语音输入时会被误切断。
    "stuck_release_sec": 60,
    "repeat_delay": 0.42,     # 单键按住多久开始连发
    "repeat_interval": 0.032, # 连发间隔
    "stats": True,            # 按键统计（只记次数，不记内容），见 keystats.py
    "scroll": {
        "click_px": 130,
        "pulses_per_click": 8,
        "burst_gap": 0.045,
        "tick": 0.008,
        "decay": 0.12,
        "cap": 40,
        "boost_max": 1.8,
        "max_pending": 400,
    },
}


def _read_json(path):
    try:
        with open(path) as f:
            return json.load(f)
    except FileNotFoundError:
        return None


def write_json_atomic(path, obj):
    """先写临时文件再 rename，进程中途被杀也不会留下半个 JSON。"""
    tmp = f"{path}.tmp{os.getpid()}"
    with open(tmp, "w") as f:
        json.dump(obj, f, indent=2, ensure_ascii=False)
    os.replace(tmp, path)


def load_config():
    cfg = _read_json(CONFIG_PATH)
    if cfg is None:
        write_json_atomic(CONFIG_PATH, DEFAULT_CONFIG)
        print(f"[*] 已生成默认配置: {CONFIG_PATH}", flush=True)
        cfg = {}
    merged = {**DEFAULT_CONFIG, **cfg}
    merged["scroll"] = {**DEFAULT_CONFIG["scroll"], **(cfg.get("scroll") or {})}
    return merged


def load_mapping():
    m = _read_json(MAPPING_PATH)
    return dict(DEFAULT_MAPPING) if m is None else m


def save_mapping(m):
    write_json_atomic(MAPPING_PATH, m)


# ---------- 映射写法解析 ----------
class Action:
    """
    解析后的动作。kind:
      key     普通键 / 组合键（mods + vk）
      media   媒体键（NX_KEYTYPE）
      scroll  平滑滚动（dir = ±1 按格，或 px 直接给像素）
      double  连按两下（inner 是被连按的 Action）
    hold = 按钮按住期间保持按下（单键默认 hold，组合键默认按一下就走完，
           写成 hold:shift+space 可强制按住）
    """
    __slots__ = ("kind", "mods", "vk", "media", "dir", "px", "inner", "hold", "spec")

    def __init__(self, kind, spec, **kw):
        self.kind = kind
        self.spec = spec
        self.mods = kw.get("mods", [])
        self.vk = kw.get("vk")
        self.media = kw.get("media")
        self.dir = kw.get("dir", 0)
        self.px = kw.get("px", 0.0)
        self.inner = kw.get("inner")
        self.hold = kw.get("hold", False)

    @property
    def is_modifier(self):
        return self.kind == "key" and not self.mods and self.vk in Injector.MODIFIER_VKS

    @property
    def repeatable(self):
        """按住连发只对普通单键有意义；修饰键（如 fn）连发会让输入法反复收到按下事件。"""
        return self.kind == "key" and not self.mods and not self.is_modifier


def parse_spec(spec):
    """把 'cmd+shift+4' / 'fn' / 'volumeup' / 'scrollup' / 'double:fn' 解析成 Action，非法则抛 ValueError。"""
    raw = (spec or "").strip()
    s = raw.lower()
    if not s:
        raise ValueError("空映射")
    if s.startswith("double:"):
        inner = parse_spec(s[len("double:"):])
        return Action("double", raw, inner=inner)
    force_hold = False
    if s.startswith("hold:"):
        force_hold = True
        s = s[len("hold:"):]
    if s in ("scrollup", "scrolldown"):
        return Action("scroll", raw, dir=-1 if s == "scrollup" else 1)
    if s.startswith("scroll:"):
        try:
            return Action("scroll", raw, px=float(s.split(":", 1)[1]))
        except ValueError:
            raise ValueError(f"滚动像素无效: {raw}")
    if s in MEDIA_KEYS:
        return Action("media", raw, media=MEDIA_KEYS[s])
    parts = [p.strip() for p in s.split("+")]
    if any(not p for p in parts):
        raise ValueError(f"写法有误: {raw}")
    *mods, key = parts
    for m in mods:
        if m not in MOD_FLAGS:
            raise ValueError(f"未知修饰键 '{m}'")
    if key not in VK:
        raise ValueError(f"未知按键 '{key}'")
    return Action("key", raw, mods=mods, vk=VK[key], hold=force_hold or not mods)


# ---------- Quartz 注入 ----------
class Injector:
    # 修饰键在 CGEvent 层需要 flagsChanged 才能让 App 感知状态变化。
    # Fn(0x3F) 同样走 flagsChanged —— 豆包输入法监听的就是 [Fn] flagsChanged
    MODIFIER_VKS = {0x37, 0x38, 0x3A, 0x3B, 0x3F}
    # 键码 -> 它自己对应的修饰位，用于单独映射修饰键时兜底
    MOD_FLAG_OF = {0x37: 0x00100000, 0x38: 0x00020000,
                   0x3A: 0x00080000, 0x3B: 0x00040000, 0x3F: 0x00800000}

    # 手势相位：必须成套发 Began → Changed... → Ended，
    # 否则浏览器可能把它当成畸形事件而丢掉，表现就是「偶尔滚了没反应」
    PHASE_BEGAN = 1
    PHASE_CHANGED = 2
    PHASE_ENDED = 4

    def __init__(self):
        try:
            import Quartz
            self.Q = Quartz
        except ImportError:
            print("[!] 缺少 pyobjc-framework-Quartz，请 pip3 install -r requirements.txt")
            sys.exit(3)
        try:
            from AppKit import NSEvent
            self.NSEvent = NSEvent
        except ImportError:
            self.NSEvent = None

    def post_key(self, vk, down, flags=0, autorepeat=False):
        Q = self.Q
        ev = Q.CGEventCreateKeyboardEvent(None, vk, down)
        if ev is None:
            return
        Q.CGEventSetIntegerValueField(ev, Q.kCGEventSourceUserData, INJECT_TAG)
        # 按下修饰键本体时，务必把它的修饰位带上；
        # 否则事件会被系统解读为「该修饰键松开」。
        if down:
            flags |= self.MOD_FLAG_OF.get(vk, 0)
        if vk in self.MODIFIER_VKS:
            Q.CGEventSetType(ev, Q.kCGEventFlagsChanged)
            Q.CGEventSetFlags(ev, flags)
            # 修饰键必须投递到 HID 层，投到 Session 层修饰位不会生效（实测）
            Q.CGEventPost(Q.kCGHIDEventTap, ev)
            return
        if flags:
            Q.CGEventSetFlags(ev, flags)
        if autorepeat:
            Q.CGEventSetIntegerValueField(ev, Q.kCGKeyboardEventAutorepeat, 1)
        Q.CGEventPost(Q.kCGSessionEventTap, ev)

    def post_media(self, key_type):
        if self.NSEvent is None:
            print("[!] 媒体键需要 pyobjc-framework-Cocoa", flush=True)
            return
        for down in (True, False):
            flags = 0xA00 if down else 0xB00
            ev = self.NSEvent.otherEventWithType_location_modifierFlags_timestamp_windowNumber_context_subtype_data1_data2_(
                14, (0, 0), flags, 0, 0, None, 8, (key_type << 16) | flags, -1)
            self.Q.CGEventPost(self.Q.kCGHIDEventTap, ev.CGEvent())

    def scroll_pixel(self, px, phase=0):
        """
        像素级滚动 + 连续手势标记 + 手势相位。
        这四样齐了，应用才会把它当成一次真正的触控板手势：
          - UnitPixel     按像素而不是行理解增量
          - IsContinuous  走触控板代码路径，认 point delta
          - PointDelta    实际位移量
          - ScrollPhase   手势开始/持续/结束
        """
        Q = self.Q
        px = int(px)
        if not px and phase != self.PHASE_ENDED:
            return
        ev = Q.CGEventCreateScrollWheelEvent(None, Q.kCGScrollEventUnitPixel, 1, px)
        if ev is None:
            return
        try:
            Q.CGEventSetIntegerValueField(ev, Q.kCGScrollWheelEventIsContinuous, 1)
            if phase:
                Q.CGEventSetIntegerValueField(ev, Q.kCGScrollWheelEventScrollPhase, phase)
            Q.CGEventSetIntegerValueField(ev, Q.kCGScrollWheelEventPointDeltaAxis1, px)
            Q.CGEventSetIntegerValueField(ev, Q.kCGScrollWheelEventFixedPtDeltaAxis1, px << 16)
        except Exception:
            pass
        Q.CGEventPost(Q.kCGHIDEventTap, ev)

    def _flags(self, mods):
        f = 0
        for m in mods:
            f |= MOD_FLAGS[m]
        return f

    def tap(self, a):
        """按一下就走完整流程：修饰键按下 → 主键 → 松开 → 修饰键抬起。"""
        if a.kind == "media":
            self.post_media(a.media)
            return
        if a.kind != "key":
            return
        flags = 0
        for m in a.mods:
            flags |= MOD_FLAGS[m]
            self.post_key(MOD_VK[m], True, flags)
        self.post_key(a.vk, True, flags)
        self.post_key(a.vk, False, flags)
        for m in reversed(a.mods):
            flags &= ~MOD_FLAGS[m]
            self.post_key(MOD_VK[m], False, flags)

    def key_down(self, a, autorepeat=False):
        if a.kind == "media":
            if not autorepeat:
                self.post_media(a.media)
            return
        flags = 0
        if not autorepeat:
            for m in a.mods:
                # 修饰键按下必须带「累计修饰位」，传 0 会被系统解读为「松开」
                flags |= MOD_FLAGS[m]
                self.post_key(MOD_VK[m], True, flags)
        else:
            flags = self._flags(a.mods)
        self.post_key(a.vk, True, flags, autorepeat=autorepeat)

    def key_up(self, a):
        if a.kind != "key":
            return
        # 松开主键时不带修饰位，确保 up 事件一定被系统接受（否则按键会卡住）
        self.post_key(a.vk, False, 0)
        for m in reversed(a.mods):
            self.post_key(MOD_VK[m], False, 0)


# ---------- 串口 ----------
TOURBOX_VID = 0xCAFE


def find_port():
    """优先按 USB VID 认出 TourBox，避免误抢 Arduino 之类的其他串口设备。"""
    try:
        from serial.tools import list_ports
        for p in list_ports.comports():
            if p.vid == TOURBOX_VID or "tourbox" in (p.product or "").lower():
                return p.device
    except Exception:
        pass
    cands = [c for c in glob.glob("/dev/cu.usbmodem*") if "Bluetooth" not in c]
    tb = [c for c in cands if "TourBox" in c]
    return (tb or cands or [None])[0]


def open_serial(port):
    return serial.Serial(port, 115200, timeout=0.2,
                         bytesize=serial.EIGHTBITS,
                         parity=serial.PARITY_NONE,
                         stopbits=serial.STOPBITS_ONE,
                         rtscts=False, dsrdtr=False)


# ---------- 驱动中枢 ----------
class Hub:
    """串口事件中枢：解析字节 → 注入按键，同时把事件推给界面。"""

    MAX_EVENTS_PER_SEC = 40     # 注入速率上限，防事件风暴打爆系统事件队列

    def __init__(self, port=None, stats=None):
        self.fixed_port = port
        self.stats = stats              # KeyStats；媒体键和滚动不经过键盘事件，由这里直接计数
        self.port = None
        self.ser = None
        self.inj = Injector()
        self.mapping = load_mapping()   # byte_name -> physical_id
        self.listeners = []
        self.state_lock = threading.RLock()   # 保护按键状态：串口线程 / 守护 / 连发 / HTTP 都会碰
        self.held_since = {}            # 字节名 -> 按下时刻
        self.held_action = {}           # 字节名 -> 按下时生效的 Action（松开必须用同一个，防中途改映射卡键）
        self.active = {}                # 目标 spec -> 正在按住它的字节名集合（引用计数）
        self.repeat_last = {}           # 字节名 -> 上次连发时刻
        self.repeat_wake = threading.Event()
        self.pulses = {}
        self.last_fire = {}
        self.evt_window = []
        self.dropped = 0
        self.learn_target = None
        self.status = "starting"
        self.lookup = {}
        for name, code in PROTOCOL.items():
            self.lookup[code & 0x7F] = (name, True)
            self.lookup[(code & 0x7F) | 0x80] = (name, False)
        # 滚动状态
        self.scroll_lock = threading.RLock()
        self.scroll_wake = threading.Event()
        self.scroll_pending = 0.0
        self.scroll_frac = 0.0
        self.scroll_boost = 1.0
        self.scroll_in_gesture = False
        self.wheel_dir = 0
        self.wheel_n = 0
        self.wheel_ts = 0.0
        self.stat_last = 0.0
        self.stat_bursts = []
        self.stat_gaps = []
        self.reload_cfg()

    # ---- 配置 ----
    def reload_cfg(self):
        self.cfg = load_config()
        self._actions = {}
        for name, spec in (self.cfg.get("map") or {}).items():
            if not spec:
                continue
            try:
                self._actions[name] = parse_spec(spec)
            except ValueError as e:
                print(f"[!] 配置 {name}={spec!r} 无效：{e}，已忽略", flush=True)
        s = self.cfg["scroll"]
        self.CLICK_PX = float(s["click_px"])
        self.PULSES_PER_CLICK = int(s["pulses_per_click"])
        self.BURST_GAP = float(s["burst_gap"])
        self.SCROLL_TICK = float(s["tick"])
        self.SCROLL_DECAY = float(s["decay"])
        self.SCROLL_CAP = float(s["cap"])
        self.SCROLL_BOOST_MAX = float(s["boost_max"])
        self.SCROLL_MAX_PENDING = float(s["max_pending"])
        self.STUCK_RELEASE_SEC = float(self.cfg.get("stuck_release_sec", 60))
        self.REPEAT_DELAY = float(self.cfg.get("repeat_delay", 0.42))
        self.REPEAT_INTERVAL = float(self.cfg.get("repeat_interval", 0.032))

    def set_map(self, name, spec):
        """改一个映射并落盘。spec 为空表示取消映射。非法写法抛 ValueError。"""
        spec = (spec or "").strip()
        if spec:
            parse_spec(spec)
        cfg = _read_json(CONFIG_PATH) or dict(DEFAULT_CONFIG)
        cfg.setdefault("map", {})[name] = spec
        write_json_atomic(CONFIG_PATH, cfg)
        self.reload_cfg()

    def action_of(self, name):
        """按字节名查当前动作（物理键名有单独映射时以物理键为准）。"""
        phys = self.mapping.get(name)
        if phys and phys in self._actions:
            return self._actions[phys]
        return self._actions.get(name)

    # ---- 事件推送 ----
    def subscribe(self, q):
        self.listeners.append(q)

    def unsubscribe(self, q):
        try:
            self.listeners.remove(q)
        except ValueError:
            pass

    def publish(self, ev):
        for q in list(self.listeners):
            try:
                q.put_nowait(ev)
            except Exception:
                pass

    def set_learn(self, physical_id):
        self.learn_target = physical_id
        return {"ok": True, "learning": physical_id}

    # ---- 限流 / 释放 / 守护 ----
    def _rate_ok(self):
        now = time.time()
        self.evt_window = [t for t in self.evt_window if now - t < 1.0]
        if len(self.evt_window) >= self.MAX_EVENTS_PER_SEC:
            self.dropped += 1
            return False
        self.evt_window.append(now)
        return True

    def release_all(self, reason=""):
        """强制释放所有仍按住的键。卡键兜底 / 串口断开 / 退出时调用。"""
        with self.state_lock:
            for name, a in list(self.held_action.items()):
                if self.active.pop(a.spec, None) is not None:
                    try:
                        self.inj.key_up(a)
                    except Exception:
                        pass
            self.active.clear()
            self.held_action.clear()
            self.held_since.clear()
            self.repeat_last.clear()
        if reason:
            print(f"[!] 已强制释放所有按键（{reason}）", flush=True)
            self.publish({"type": "notice", "text": f"已释放按键：{reason}"})

    def watchdog(self):
        """按键按住超过阈值却没有松开事件时，强制释放（防 Fn 永久按住导致系统输入错乱）。"""
        while True:
            time.sleep(2)
            now = time.time()
            with self.state_lock:
                stuck = [n for n, t in self.held_since.items() if now - t > self.STUCK_RELEASE_SEC]
            if stuck:
                self.release_all(f"{stuck[0]} 按住超过 {self.STUCK_RELEASE_SEC:g}s，疑似丢包")

    def auto_repeat(self):
        """
        按住自动连发。
        我们自己合成的 keyDown 不会像物理按键那样由系统产生重复，
        所以这里按节奏补发带 autorepeat 标记的 keyDown。
        没有按住任何可连发的键时线程完全睡眠，不占 CPU。
        """
        while True:
            with self.state_lock:
                pending = [(n, t, self.held_action[n]) for n, t in self.held_since.items()
                           if self.held_action[n].repeatable]
            if not pending:
                self.repeat_wake.clear()
                self.repeat_wake.wait()
                continue
            time.sleep(0.01)
            now = time.time()
            for name, since, a in pending:
                if now - since < self.REPEAT_DELAY:
                    continue
                if now - self.repeat_last.get(name, 0) < self.REPEAT_INTERVAL:
                    continue
                with self.state_lock:
                    if name not in self.held_since:
                        continue
                    self.repeat_last[name] = now
                    self.inj.key_down(a, autorepeat=True)

    # ---- 平滑滚动 ----
    #
    # 为什么按「一格」而不是「一个脉冲」计距：
    #   实测这个滚轮转一格会发 1~13 个脉冲（均值 5.59），波动近 13 倍。
    #   按脉冲算距离，同样转一格有时走 9px 有时走 117px——手感不稳定的根源。
    #   所以先把一次连发的脉冲攒起来，整格只算一次固定位移（click_px）。
    def add_pulse(self, direction, name=None):
        """
        收到一个滚轮脉冲。direction: -1 向上，+1 向下
        一格的第一个脉冲到达就立刻结算整格距离开始滚（不等这一格发完，
        否则每格都有 burst_gap 的延迟，手感「反应慢」）；同一格后续脉冲只计数。
        """
        with self.scroll_lock:
            now = time.time()
            new_click = (not self.wheel_n or direction != self.wheel_dir
                         or now - self.wheel_ts > self.BURST_GAP
                         or self.wheel_n >= self.PULSES_PER_CLICK)
            if new_click:
                if self.wheel_n:
                    self.stat_bursts.append(self.wheel_n)
                    if len(self.stat_bursts) > 400:
                        del self.stat_bursts[:200]
                self.wheel_dir = direction
                self.wheel_n = 0
                self._commit_click(now)
                if self.stats:
                    self.stats.record("scrollup" if direction < 0 else "scrolldown", "tb")
                    if name:
                        self._count_button(name)
            self.wheel_n += 1
            self.wheel_ts = now
        self.scroll_wake.set()

    def add_scroll_px(self, px):
        with self.scroll_lock:
            lim = self.SCROLL_MAX_PENDING
            self.scroll_pending = max(-lim, min(lim, self.scroll_pending + px))
        self.scroll_wake.set()

    def _commit_click(self, now):
        """结算一格的距离。调用方必须已持有 scroll_lock。"""
        if self.stat_last:
            self.stat_gaps.append(max(0.0, now - self.stat_last))
            if len(self.stat_gaps) > 400:
                del self.stat_gaps[:200]
        self.stat_last = now
        # 连转加速：两格间隔很短时，这一格给得更多
        gap = self.stat_gaps[-1] if self.stat_gaps else 1.0
        if gap < 0.12:
            self.scroll_boost = min(self.scroll_boost + 0.10, self.SCROLL_BOOST_MAX)
        else:
            self.scroll_boost = 1.0
        p = self.scroll_pending + self.wheel_dir * self.CLICK_PX * self.scroll_boost
        lim = self.SCROLL_MAX_PENDING
        self.scroll_pending = max(-lim, min(lim, p))

    def scroll_stats(self):
        b, g = self.stat_bursts, self.stat_gaps
        return {
            "burst_avg": round(sum(b) / len(b), 2) if b else 0,
            "burst_max": max(b) if b else 0,
            "burst_min": min(b) if b else 0,
            "burst_samples": len(b),
            "gap_avg_ms": round(sum(g) / len(g) * 1000, 1) if g else 0,
            "click_px": self.CLICK_PX,
        }

    def scroll_pump(self):
        """
        平滑滚动泵。每 tick 吐一小步，每步走掉剩余量的 decay 比例，
        并成套发手势相位（Began → Changed… → Ended）。
        空闲时阻塞等待新脉冲，不再 125Hz 空转。
        """
        inj = self.inj
        while True:
            with self.scroll_lock:
                idle = abs(self.scroll_pending) < 0.5 and not self.scroll_in_gesture
                if idle:
                    self.scroll_wake.clear()
            if idle:
                self.scroll_wake.wait()
            time.sleep(self.SCROLL_TICK)
            n = 0
            ended = False
            with self.scroll_lock:
                p = self.scroll_pending
                if abs(p) < 0.5:
                    self.scroll_pending = 0.0
                    ended = self.scroll_in_gesture
                else:
                    step = max(-self.SCROLL_CAP, min(self.SCROLL_CAP, p * self.SCROLL_DECAY))
                    self.scroll_pending = p - step
                    self.scroll_frac += step
                    n = int(self.scroll_frac)
                    if n:
                        self.scroll_frac -= n
            if n:
                phase = inj.PHASE_CHANGED if self.scroll_in_gesture else inj.PHASE_BEGAN
                self.scroll_in_gesture = True
                inj.scroll_pixel(n, phase)
            elif ended:
                self.scroll_in_gesture = False
                self.scroll_frac = 0.0
                inj.scroll_pixel(0, inj.PHASE_ENDED)

    # ---- 串口主循环 ----
    def run(self):
        waiting_logged = False
        while True:
            port = self.fixed_port or find_port()
            if not port:
                if not waiting_logged:
                    print("[*] 等待 TourBox 接入…", flush=True)
                    waiting_logged = True
                self._set_status("waiting")
                time.sleep(2)
                continue
            try:
                self.ser = open_serial(port)
            except Exception as e:
                self._set_status("busy" if "busy" in str(e).lower() else "waiting")
                if not waiting_logged:
                    print(f"[!] 打开 {port} 失败: {e}", flush=True)
                    print("    Resource busy 的话先退出 TourBox Console：pkill -f 'TourBox Console'", flush=True)
                    waiting_logged = True
                time.sleep(2)
                continue
            waiting_logged = False
            self.port = port
            print(f"[*] 已连接 {port}", flush=True)
            self._set_status("connected")
            try:
                while True:
                    data = self.ser.read(64)
                    for b in data:
                        try:
                            self.handle(b)
                        except Exception:
                            # 单个事件出错不能拖垮整个读循环，否则驱动「活着但没反应」
                            traceback.print_exc()
            except (serial.SerialException, OSError) as e:
                print(f"[!] 串口中断: {e}", flush=True)
            # 串口断了立刻释放按键，否则 Fn 会一直卡在按下状态
            self.release_all("串口断开")
            self._set_status("reconnecting")
            try:
                self.ser.close()
            except Exception:
                pass
            time.sleep(1)

    def _set_status(self, st):
        if st != self.status:
            self.status = st
            self.publish({"type": "status", "status": st})

    def handle(self, b):
        hit = self.lookup.get(b)
        if not hit:
            self.publish({"type": "unknown", "byte": f"{b:02x}"})
            return
        name, down = hit

        # 学习模式：把下一个按下的字节绑到目标物理键
        target = self.learn_target
        if target and down:
            self.learn_target = None
            for k, v in list(self.mapping.items()):
                if v == target:
                    del self.mapping[k]
            self.mapping[name] = target
            save_mapping(self.mapping)
            self.publish({"type": "learned", "byte_name": name, "physical": target})
            return

        self.publish({"type": "key", "byte_name": name,
                      "physical": self.mapping.get(name), "down": down})

        if name in PULSE_CONTROLS:
            if down:
                self._pulse(name)
        elif down:
            self._count_button(name)
            self._press(name)
        else:
            self._release(name)

    def _count_button(self, name):
        """按物理键计数（转盘/滚轮按「格」或「次」算，不按原始脉冲算）。"""
        if self.stats:
            self.stats.record_button(self.mapping.get(name) or name)

    def _pulse(self, name):
        a = self.action_of(name)
        if a is None:
            return
        now = time.time() * 1000
        guard = self.cfg.get("repeat_guard_ms", 0)
        if guard and now - self.last_fire.get(name, 0) < guard:
            return
        if a.kind == "scroll":
            # 滚动走自己的缓冲泵，不过注入限流，否则会误杀脉冲
            if a.dir:
                self.add_pulse(a.dir, name)
            else:
                self.add_scroll_px(a.px)
            return
        div = max(1, int(self.cfg.get("divider", {}).get(name, 1)))
        self.pulses[name] = self.pulses.get(name, 0) + 1
        if self.pulses[name] % div:
            return
        self._count_button(name)
        if not self._rate_ok():
            return
        self.last_fire[name] = now
        self._fire(a)

    def _fire(self, a):
        if a.kind == "double":
            self._fire(a.inner)
            time.sleep(0.08)
            self._fire(a.inner)
        elif a.kind == "scroll":
            if a.dir:
                self.add_scroll_px(a.dir * self.CLICK_PX)
            else:
                self.add_scroll_px(a.px)
        else:
            if a.kind == "media" and self.stats:
                self.stats.record(a.spec.lower(), "tb")
            self.inj.tap(a)

    def _press(self, name):
        a = self.action_of(name)
        if a is None or not self._rate_ok():
            return
        if a.kind != "key" or not a.hold:
            # 组合键 / 媒体键 / 滚动 / 双击：按下即触发一次，松开无动作
            self._fire(a)
            return
        with self.state_lock:
            if name in self.held_since:
                return
            self.held_since[name] = time.time()
            self.held_action[name] = a
            self.repeat_last.pop(name, None)
            # 多个物理键映射到同一目标时按目标做引用计数：
            # 第一个按下才发 keyDown，全部松开才发 keyUp，否则目标键会卡住
            srcs = self.active.setdefault(a.spec, set())
            srcs.add(name)
            if len(srcs) == 1:
                self.inj.key_down(a)
        if a.repeatable:
            self.repeat_wake.set()

    def _release(self, name):
        # 松开绝不限流，否则按键会卡住
        with self.state_lock:
            if name not in self.held_since:
                return
            del self.held_since[name]
            a = self.held_action.pop(name)
            self.repeat_last.pop(name, None)
            srcs = self.active.get(a.spec, set())
            srcs.discard(name)
            if not srcs:
                self.active.pop(a.spec, None)
                self.inj.key_up(a)

    def start(self):
        """启动全部后台线程。串口线程若意外退出，整个进程退出交给 launchd 拉起。"""
        def guarded(fn):
            def run():
                try:
                    fn()
                except Exception:
                    traceback.print_exc()
                print(f"[!] 线程 {fn.__name__} 意外退出，进程即将重启", flush=True)
                self.release_all()
                os._exit(1)
            return run
        for fn in (self.run, self.watchdog, self.scroll_pump, self.auto_repeat):
            threading.Thread(target=guarded(fn), name=fn.__name__, daemon=True).start()


def check_permissions():
    """
    打印辅助功能权限状态。常驻运行时没有终端隔着，事件被系统静默丢弃的话
    界面上什么都看不出来，所以启动时把状态写进日志。
    """
    try:
        from Quartz import CGPreflightPostEventAccess, CGRequestPostEventAccess
        ok = bool(CGPreflightPostEventAccess())
        print(f"[perm] 辅助功能(注入)={'已授权' if ok else '未授权'}", flush=True)
        if not ok:
            CGRequestPostEventAccess()
            print(f"[perm] 请在 系统设置 → 隐私与安全性 → 辅助功能 里勾选 {os.path.realpath(sys.executable)}",
                  flush=True)
        return ok
    except Exception as e:
        print(f"[perm] 无法检测权限: {e}", flush=True)
        return None


def acquire_single_instance():
    """单实例锁：多个驱动同时注入会让事件翻倍，直接把系统事件队列打爆。"""
    import fcntl
    fh = open(os.path.join(APP_DIR, ".tourbox.lock"), "w")
    try:
        fcntl.flock(fh, fcntl.LOCK_EX | fcntl.LOCK_NB)
    except OSError:
        print("[!] 已有实例在运行，本次退出（先执行 pkill -f webui.py）", flush=True)
        sys.exit(3)
    return fh


def sniff(port):
    ser = open_serial(port)
    print(f"[*] 嗅探 {port}，Ctrl+C 结束")
    try:
        while True:
            d = ser.read(64)
            if d:
                print(" ".join(f"{b:02x}" for b in d), flush=True)
    except KeyboardInterrupt:
        pass
    finally:
        ser.close()


def main():
    ap = argparse.ArgumentParser(description="TourBox 驱动（无界面）")
    ap.add_argument("--port", help="串口路径，默认自动识别")
    ap.add_argument("--sniff", action="store_true", help="只打印原始字节")
    args = ap.parse_args()

    if args.sniff:
        port = args.port or find_port()
        if not port:
            print("[!] 未找到 TourBox 串口设备")
            sys.exit(1)
        sniff(port)
        return

    check_permissions()
    _lock = acquire_single_instance()
    hub = Hub(args.port)
    hub.start()
    print("[*] 驱动就绪，Ctrl+C 退出", flush=True)
    try:
        while True:
            time.sleep(3600)
    except KeyboardInterrupt:
        hub.release_all("程序退出")


if __name__ == "__main__":
    main()
