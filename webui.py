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
from tourboxd import Hub, check_permissions, acquire_single_instance

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
            body = HTML.encode()
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


HTML = r"""<!DOCTYPE html>
<html lang="zh-CN">
<head>
<meta charset="utf-8">
<title>TourBox 控制台</title>
<style>
  :root{
    --bg:#1c1c1e; --panel:#252528; --panel2:#2c2c30; --line:#3a3a3f;
    --text:#e8e8ea; --dim:#8b8b93; --accent:#4a9eff; --ok:#3ecf8e; --err:#ff6b6b;
  }
  *{box-sizing:border-box}
  body{margin:0;background:var(--bg);color:var(--text);
       font:13px/1.5 -apple-system,"PingFang SC",sans-serif;height:100vh;
       display:flex;flex-direction:column;overflow:hidden}
  header{display:flex;align-items:center;gap:12px;padding:10px 16px;
         border-bottom:1px solid var(--line);background:var(--panel)}
  header h1{font-size:13px;font-weight:600;margin:0;letter-spacing:.3px}
  header .sp{flex:1}
  header button{font-size:11px;padding:4px 10px;border-radius:5px;cursor:pointer;
                background:#3a3a40;border:none;color:var(--dim)}
  header button:hover{color:var(--text);background:#45454c}
  .dot{width:7px;height:7px;border-radius:50%;background:var(--dim)}
  .dot.on{background:var(--ok);box-shadow:0 0 6px var(--ok)}
  main{flex:1;display:flex;min-height:0}
  .left{width:46%;min-width:340px;display:flex;align-items:center;
        justify-content:center;padding:18px;border-right:1px solid var(--line)}
  .right{flex:1;overflow-y:auto;padding:14px 16px}
  .sec{font-size:11px;color:var(--dim);letter-spacing:.6px;margin:14px 0 8px;
       text-transform:uppercase}
  .sec:first-child{margin-top:0}
  .row{display:flex;align-items:center;gap:8px;padding:7px 10px;border-radius:7px;
       background:var(--panel2);margin-bottom:5px;transition:background .12s}
  .row:hover{background:#34343a}
  .row.active{outline:1.5px solid var(--accent)}
  .row .nm{width:84px;flex:none;font-size:12px}
  .row .byte{width:96px;flex:none;font-size:10px;color:var(--dim);font-family:ui-monospace,monospace}
  .row input{flex:1;min-width:0;background:#1e1e21;border:1px solid var(--line);color:var(--text);
             border-radius:5px;padding:5px 8px;font:12px ui-monospace,monospace;outline:none}
  .row input:focus{border-color:var(--accent)}
  .row input.bad{border-color:var(--err)}
  .row .learn{flex:none;font-size:11px;padding:4px 8px;border-radius:5px;cursor:pointer;
              background:#3a3a40;border:none;color:var(--dim)}
  .row .learn:hover{color:var(--text);background:#45454c}
  .row.learning{animation:pulse 1s infinite}
  @keyframes pulse{0%,100%{background:#2c2c30}50%{background:#4a3a1e}}
  .unbound{color:#666}
  svg{width:100%;height:auto;max-height:100%}
  .hot{fill:var(--accent)!important;filter:drop-shadow(0 0 8px var(--accent))}
  .pkey{cursor:pointer;transition:fill .1s}
  .pkey:hover{fill:#4a4a52}
  footer{padding:8px 16px;border-top:1px solid var(--line);background:var(--panel);
         font-size:11px;color:var(--dim);display:flex;gap:16px}
  #last.err{color:var(--err)}
  kbd{background:#33333a;border-radius:3px;padding:1px 5px;font-family:ui-monospace,monospace;font-size:10px}
</style>
</head>
<body>
<header>
  <span class="dot" id="dot"></span>
  <h1>TourBox 控制台</h1>
  <span style="color:var(--dim);font-size:11px" id="st">连接中…</span>
  <span class="sp"></span>
  <button id="release" title="万一某个键卡住（比如 fn），点这里立即全部松开">释放所有按键</button>
</header>
<main>
  <div class="left">
    <svg viewBox="0 0 420 400" id="dev">
      <path d="M105 40 Q60 40 52 100 L44 250 Q40 320 105 335 L315 335 Q380 320 376 250 L368 100 Q360 40 315 40 Z"
            fill="#2e2e33" stroke="#3f3f46" stroke-width="2"/>
      <rect class="pkey" data-id="scroll_up" x="88" y="72" width="17" height="27" rx="8" fill="#1a1a1d"/>
      <rect class="pkey" data-id="scroll_down" x="88" y="99" width="17" height="27" rx="8" fill="#1a1a1d"/>
      <rect class="pkey" data-id="horiz" x="152" y="64" width="86" height="20" rx="10" fill="#1a1a1d"/>
      <circle class="pkey" data-id="c1" cx="292" cy="80" r="13" fill="#1a1a1d"/>
      <circle class="pkey" data-id="c2" cx="320" cy="110" r="13" fill="#1a1a1d"/>
      <text x="330" y="78" fill="#6b6b73" font-size="9">C1</text>
      <text x="338" y="128" fill="#6b6b73" font-size="9">C2</text>
      <circle class="pkey" data-id="knob_cw" cx="195" cy="160" r="44" fill="#1a1a1d" stroke="#4a4a52" stroke-width="1.5"/>
      <circle class="pkey" data-id="knob_click" cx="195" cy="160" r="26" fill="#25252a"/>
      <circle cx="195" cy="160" r="9" fill="#3a3a42" pointer-events="none"/>
      <circle class="pkey" data-id="tour" cx="104" cy="245" r="19" fill="#1a1a1d"/>
      <text x="92" y="285" fill="#6b6b73" font-size="9">Tour</text>
      <rect class="pkey" data-id="long" x="228" y="212" width="26" height="54" rx="13" fill="#1a1a1d"/>
      <circle class="pkey" data-id="short" cx="290" cy="248" r="19" fill="#1a1a1d"/>
      <text x="248" y="292" fill="#6b6b73" font-size="9">长/短键</text>
      <text x="62" y="62" fill="#6b6b73" font-size="9">滚轮</text>
      <text x="176" y="52" fill="#6b6b73" font-size="9">横键</text>
      <text x="176" y="118" fill="#6b6b73" font-size="9">旋钮</text>
    </svg>
  </div>
  <div class="right">
    <div class="sec">按键映射</div>
    <div id="rows"></div>
    <div class="sec">写法</div>
    <div style="color:var(--dim);font-size:11.5px;line-height:1.9">
      · 单键：<kbd>fn</kbd>（按住 = 按住 FN，用于按住说话）、<kbd>backspace</kbd>、<kbd>return</kbd>，单键按住会自动连发<br>
      · 组合键：<kbd>cmd+shift+4</kbd>，按一下就完整触发一次；要按住组合键写 <kbd>hold:shift+space</kbd><br>
      · 媒体键：<kbd>volumeup</kbd> <kbd>volumedown</kbd> <kbd>mute</kbd> <kbd>playpause</kbd> <kbd>next</kbd> <kbd>previous</kbd> <kbd>brightnessup</kbd><br>
      · 平滑滚动：<kbd>scrollup</kbd> <kbd>scrolldown</kbd>；双击：<kbd>double:fn</kbd><br>
      · 旋钮 / 滚轮转动没有「松开」信号，不要映射成 <kbd>fn</kbd> 这类按住式按键<br>
      · 回车保存并立即生效；输入框清空再回车 = 取消映射；点「识别」后按设备上的实体键，可重新绑定位置
    </div>
  </div>
</main>
<footer>
  <span id="port"></span>
  <span id="last"></span>
</footer>
<script>
let STATE = null;
const rowsEl = document.getElementById('rows');
const lastEl = document.getElementById('last');
let selected = null;

const esc = s => String(s ?? '').replace(/[&<>"']/g, c => ({'&':'&amp;','<':'&lt;','>':'&gt;','"':'&quot;',"'":'&#39;'}[c]));
function say(text, err){ lastEl.textContent = text; lastEl.className = err ? 'err' : ''; }

function byteOf(phys){
  if(!STATE) return null;
  for(const [b,p] of Object.entries(STATE.mapping)) if(p===phys) return b;
  return null;
}
function specOf(pid, byte){
  const m = STATE.config.map || {};
  return m[pid] || (byte ? m[byte] : '') || '';
}

function render(){
  if(!STATE) return;
  // 用户正在输入时不重绘，避免把没保存的内容冲掉
  const focused = document.activeElement;
  if(focused && focused.tagName === 'INPUT' && rowsEl.contains(focused)) return;
  rowsEl.innerHTML = '';
  for(const [pid, info] of Object.entries(STATE.physical)){
    const byte = byteOf(pid);
    const row = document.createElement('div');
    row.className = 'row' + (selected===pid?' active':'');
    row.dataset.pid = pid;
    row.innerHTML = `
      <span class="nm">${esc(info.name)}</span>
      <span class="byte ${byte?'':'unbound'}">${esc(byte || '--')}</span>
      <input value="${esc(specOf(pid, byte))}" placeholder="${byte?'未设置':'待识别'}" data-pid="${pid}" ${byte?'':'disabled'}>
      <button class="learn" data-learn="${pid}">识别</button>`;
    rowsEl.appendChild(row);
  }
}

rowsEl.addEventListener('keydown', e=>{
  if(e.key!=='Enter' || !e.target.dataset.pid) return;
  const input = e.target;
  const pid = input.dataset.pid;
  const byte = byteOf(pid);
  // 物理键名下有单独映射就写物理键名，否则写字节名（保持和旧配置兼容）
  const key = (STATE.config.map||{})[pid] !== undefined ? pid : (byte || pid);
  fetch('/api/map',{method:'POST',headers:{'Content-Type':'application/json'},
    body:JSON.stringify({name:key, spec:input.value})})
    .then(r=>r.json()).then(res=>{
      if(!res.ok){ input.classList.add('bad'); say('没保存：'+res.error, true); return; }
      input.classList.remove('bad');
      say('已保存 '+STATE.physical[pid].name+' → '+(res.spec || '（无）'));
      input.blur(); load();
    });
});

rowsEl.addEventListener('click', e=>{
  const b = e.target.closest('[data-learn]');
  if(b){
    const pid = b.dataset.learn;
    fetch('/api/learn',{method:'POST',headers:{'Content-Type':'application/json'},
      body:JSON.stringify({physical:pid})}).then(()=>{
        document.querySelectorAll('.row').forEach(r=>r.classList.remove('learning'));
        b.closest('.row').classList.add('learning');
        say('请按下设备上「'+STATE.physical[pid].name+'」…');
      });
    return;
  }
  const row = e.target.closest('.row');
  if(row && e.target.tagName !== 'INPUT'){ selected = row.dataset.pid; render(); }
});

document.getElementById('release').onclick = ()=>{
  fetch('/api/release',{method:'POST',headers:{'Content-Type':'application/json'},body:'{}'})
    .then(()=>say('已释放所有按键'));
};

document.querySelectorAll('.pkey').forEach(el=>{
  el.addEventListener('click', ()=>{
    selected = el.dataset.id;
    render();
    const r = document.querySelector('.row[data-pid="'+selected+'"]');
    if(r) r.scrollIntoView({block:'nearest'});
  });
});

const flashLock = {};
function flash(phys){
  if(!phys) return;
  const now = Date.now();
  if(flashLock[phys] && now - flashLock[phys] < 150) return;  // 节流，防连点打爆渲染
  flashLock[phys] = now;
  const el = document.querySelector('.pkey[data-id="'+phys+'"]')
          || (phys==='knob_ccw' && document.querySelector('.pkey[data-id="knob_cw"]'));
  if(!el) return;
  el.classList.add('hot');
  setTimeout(()=>el.classList.remove('hot'), 140);
}

const STATUS_TEXT = {connected:'已连接', reconnecting:'重连中…', waiting:'等待设备接入…',
                     busy:'串口被占用（请退出 TourBox Console）', starting:'启动中…'};
function load(){
  fetch('/api/state').then(r=>r.json()).then(s=>{
    STATE = s; render();
    document.getElementById('dot').className = 'dot' + (s.status==='connected'?' on':'');
    document.getElementById('st').textContent = STATUS_TEXT[s.status] || s.status;
    document.getElementById('port').textContent = s.port || '';
  });
}
load();
setInterval(()=>{ if(!document.hidden) load(); }, 15000);

const es = new EventSource('/api/events');
es.onmessage = ev=>{
  const d = JSON.parse(ev.data);
  if(d.type==='key'){ flash(d.physical); }
  if(d.type==='learned'){ say('已绑定 '+d.byte_name+' → '+(STATE.physical[d.physical]||{}).name); load(); }
  if(d.type==='status'){ load(); }
  if(d.type==='notice'){ say(d.text); }
};
</script>
</body>
</html>
"""


def main():
    global HUB, PORT
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

    HUB = Hub(args.serial)
    HUB.start()

    def on_term(*_):
        # launchd 停止/重启服务时发 SIGTERM：先把按住的键松开再退，免得 fn 残留
        HUB.release_all()
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


if __name__ == "__main__":
    main()
