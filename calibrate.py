#!/usr/bin/env python3
"""
TourBox 协议校准器

按「固定顺序」依次按下设备上的每个控件，程序会按出现顺序记录字节，
结束后输出对照表，方便一次性生成完整映射。

用法:
    python3 calibrate.py
    然后依次按下每个按键 / 转动每个旋钮（每个控件动 1 次）
    按完 Ctrl+C 结束，程序打印结果
"""
import os
import sys
import time
import json
import serial

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
from tourboxd import find_port

PORT = sys.argv[1] if len(sys.argv) > 1 else find_port()

if not PORT:
    print("[!] 未找到串口设备")
    sys.exit(1)

print(f"[*] 打开 {PORT} @ 115200")
try:
    ser = serial.Serial(PORT, 115200, timeout=0.2,
                        bytesize=serial.EIGHTBITS,
                        parity=serial.PARITY_NONE,
                        stopbits=serial.STOPBITS_ONE,
                        rtscts=False, dsrdtr=False)
except Exception as e:
    print(f"[!] 打开失败: {e}")
    print("    Resource busy 请先退出 TourBox Console：")
    print("    pkill -f 'TourBox Console'")
    sys.exit(2)

print("""
[*] 校准模式启动。

    请按「固定顺序」依次操作每个控件，每个只动一次（按一下 / 转一格）：
      建议顺序：先按所有按钮，最后转动旋钮和滚轮各一格。

    屏幕会实时显示捕获到的字节。全部按完后 Ctrl+C 结束。
""")

order = []          # [(byte, timestamp)]
seen = set()
t0 = time.time()

try:
    while True:
        d = ser.read(64)
        if not d:
            continue
        for b in d:
            ts = time.time() - t0
            mark = "按下" if not (b & 0x80) else "松开"
            flag = ""
            if b not in seen:
                seen.add(b)
                flag = "   <<< 新控件"
            print(f"  [{ts:6.2f}s] {b:02x}  ({mark}){flag}", flush=True)
            order.append({"byte": b, "t": round(ts, 2), "kind": mark})
except KeyboardInterrupt:
    pass
finally:
    ser.close()

presses = [o for o in order if o["kind"] == "按下"]
print("\n" + "=" * 46)
print(f"捕获 {len(order)} 个字节，其中「按下」事件 {len(presses)} 个")
print("=" * 46)
print("\n按下码顺序（与你的操作顺序一一对应）:")
for i, p in enumerate(presses, 1):
    print(f"   {i:>2}. 0x{p['byte']:02x}")

with open("/tmp/tb_calibrate.json", "w") as f:
    json.dump(order, f, indent=2)
print("\n[*] 原始记录已存 /tmp/tb_calibrate.json")
