#!/usr/bin/env python3
"""
TourBox 控制台 —— 带可视化界面的驱动

在 tourboxd.py 的驱动核心上加了本地 Web 界面：
  - 设备俯视图（可点击）
  - 每个按键的映射可视化配置（回车即生效）
  - 实时按键高亮
  - 按键识别向导（把字节绑到图形位置）

用法:
    python3 webui.py              # 启动并自动打开浏览器
    python3 webui.py --port 8777 --no-browser
"""
import os
import sys
import json
import time
import queue
import signal
import argparse
import threading
import webbrowser
from http.server import BaseHTTPRequestHandler, ThreadingHTTPServer

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tourboxd import Hub, check_permissions, acquire_single_instance, load_config
from urllib.parse import urlparse, parse_qs

# 物理按键定义：id -> 中文名，顺序即界面上列表顺序
PHYSICAL = {
    "horiz":        {"name": "横键", "kind": "button"},
    "tour":         {"name": "Tour", "kind": "button"},
    "long":         {"name": "长键", "kind": "button"},
    "short":        {"name": "短键", "kind": "button"},
    "c1":           {"name": "C1", "kind": "button"},
    "c2":           {"name": "C2", "kind": "button"},
    "scroll_click": {"name": "滚轮点击", "kind": "button"},
    "knob_click":   {"name": "旋钮点击", "kind": "button"},
    "knob_cw":      {"name": "旋钮 顺时针", "kind": "rotary"},
    "knob_ccw":     {"name": "旋钮 逆时针", "kind": "rotary"},
    "scroll_up":    {"name": "滚轮 向上", "kind": "rotary"},
    "scroll_down":  {"name": "滚轮 向下", "kind": "rotary"},
}

HUB = None
STATS = None
PORT = 8777


class Handler(BaseHTTPRequestHandler):
    def log_message(self, *a):
        pass

    def _json(self, obj, code=200):
        body = json.dumps(obj, ensure_ascii=False).encode()
        self.send_response(code)
        self.send_header("Content-Type", "application/json; charset=utf-8")
        self.send_header("Content-Length", str(len(body)))
        self.end_headers()
        self.wfile.write(body)

    def _local_only(self):
        """
        只接受来自本机控制台页面的请求。
        Host 校验挡 DNS rebinding；Origin 校验挡任意网页跨站 POST 改你的键位。
        """
        allowed = {f"127.0.0.1:{PORT}", f"localhost:{PORT}"}
        if self.headers.get("Host") not in allowed:
            self.send_error(403)
            return False
        origin = self.headers.get("Origin")
        if origin and origin.split("://", 1)[-1] not in allowed:
            self.send_error(403)
            return False
        return True

    def do_GET(self):
        if not self._local_only():
            return
        if self.path in ("/", "/index.html"):
            with open(HTML_PATH, "rb") as f:
                body = f.read()
            self.send_response(200)
            self.send_header("Content-Type", "text/html; charset=utf-8")
            self.send_header("Content-Length", str(len(body)))
            self.end_headers()
            self.wfile.write(body)
            return
        if self.path == "/api/state":
            self._json({
                "status": HUB.status,
                "port": HUB.port,
                "config": HUB.cfg,
                "mapping": HUB.mapping,
                "physical": PHYSICAL,
                "scroll_stats": HUB.scroll_stats(),
            })
            return
        if self.path.startswith("/api/stats"):
            if STATS is None:
                return self._json({"enabled": False})
            q = parse_qs(urlparse(self.path).query)
            try:
                days = int((q.get("days") or ["1"])[0])
            except ValueError:
                days = 1
            return self._json({"enabled": True, **STATS.query(days)})
        if self.path == "/api/events":
            self.send_response(200)
            self.send_header("Content-Type", "text/event-stream")
            self.send_header("Cache-Control", "no-cache")
            self.end_headers()
            q = queue.Queue(maxsize=200)
            HUB.subscribe(q)
            try:
                while True:
                    try:
                        ev = q.get(timeout=15)
                        self.wfile.write(f"data: {json.dumps(ev, ensure_ascii=False)}\n\n".encode())
                    except queue.Empty:
                        # 心跳，同时用于探测连接是否已断
                        self.wfile.write(b": ping\n\n")
                    self.wfile.flush()
            except Exception:
                return
            finally:
                # 必须回收，否则每次刷新页面都会泄漏一个线程
                HUB.unsubscribe(q)
                self.close_connection = True
            return
        self.send_error(404)

    def do_POST(self):
        if not self._local_only():
            return
        # 强制 JSON：跨站的「简单请求」发不出 application/json，进一步挡 CSRF
        if not (self.headers.get("Content-Type") or "").startswith("application/json"):
            return self._json({"ok": False, "error": "需要 application/json"}, 415)
        n = int(self.headers.get("Content-Length", 0))
        try:
            data = json.loads(self.rfile.read(n) if n else b"{}")
        except Exception:
            return self._json({"ok": False, "error": "JSON 无效"}, 400)

        if self.path == "/api/map":
            name = data.get("name")
            spec = (data.get("spec") or "").strip()
            if not name:
                return self._json({"ok": False, "error": "缺少 name"}, 400)
            try:
                HUB.set_map(name, spec)
            except ValueError as e:
                return self._json({"ok": False, "error": str(e)}, 400)
            return self._json({"ok": True, "name": name, "spec": spec})

        if self.path == "/api/learn":
            phys = data.get("physical")
            if phys not in PHYSICAL:
                return self._json({"ok": False, "error": "未知位置"}, 400)
            return self._json(HUB.set_learn(phys))

        if self.path == "/api/release":
            HUB.release_all("手动释放")
            return self._json({"ok": True})

        self.send_error(404)


HTML_PATH = os.path.join(os.path.dirname(os.path.abspath(__file__)), "console.html")


def main():
    global HUB, STATS, PORT
    ap = argparse.ArgumentParser(description="TourBox 驱动 + 本地控制台")
    ap.add_argument("--port", type=int, default=8777, help="控制台端口")
    ap.add_argument("--serial", help="串口路径，默认按 USB VID 自动识别")
    ap.add_argument("--no-browser", action="store_true")
    args = ap.parse_args()
    PORT = args.port

    print(f"[*] 解释器 {os.path.realpath(sys.executable)}", flush=True)
    print(f"[*] 启动 {time.strftime('%Y-%m-%d %H:%M:%S')}", flush=True)
    check_permissions()
    _lock = acquire_single_instance()

    if load_config().get("stats", True):
        from keystats import KeyStats
        STATS = KeyStats()
        STATS.start()
    HUB = Hub(args.serial, stats=STATS)
    HUB.start()

    def on_term(*_):
        # launchd 停止/重启服务时发 SIGTERM：先把按住的键松开再退，免得 fn 残留
        HUB.release_all()
        if STATS:
            STATS.flush()
        os._exit(0)
    signal.signal(signal.SIGTERM, on_term)
    print(f"[*] 卡键守护：按住超过 {HUB.STUCK_RELEASE_SEC:g}s 无松开自动释放", flush=True)
    print(f"[*] 按住连发：{HUB.REPEAT_DELAY}s 后每 {HUB.REPEAT_INTERVAL}s 一次（修饰键不连发）", flush=True)

    srv = ThreadingHTTPServer(("127.0.0.1", PORT), Handler)
    srv.daemon_threads = True
    url = f"http://127.0.0.1:{PORT}/"
    print(f"[*] 控制台: {url}", flush=True)
    if not args.no_browser:
        threading.Timer(0.8, lambda: webbrowser.open(url)).start()
    try:
        srv.serve_forever()
    except KeyboardInterrupt:
        print("\n[*] 退出")
        HUB.release_all("程序退出")
        if STATS:
            STATS.flush()


if __name__ == "__main__":
    main()
