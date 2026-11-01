#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""等板子重新连上遥控器，然后接着跑一轮 AUDIO 0 测量。

为什么要分两步：
  板子重启后是重新扫描，而遥控器只有在被按一下时才会广播。
  那一次按键被"建立连接"吃掉了，不能算作测量轮次。
  所以先等连接建立，再提示按第二轮。
"""
import subprocess
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM11"

ser = None
for attempt in range(15):
    try:
        ser = serial.Serial(PORT, 115200, timeout=0.2)
        break
    except Exception as exc:
        # 板子重启后 USB 重新枚举，句柄会短暂被系统占着，等一下就好
        print("  打开串口失败(%d/15): %s" % (attempt + 1, type(exc).__name__),
              flush=True)
        time.sleep(1.2)
if ser is None:
    print("!! 串口打不开，退出", flush=True)
    sys.exit(1)
ser.reset_input_buffer()
print("等待遥控器连接（请按一下遥控器任意键唤醒）…", flush=True)

end = time.time() + 150
buf = b""
connected = False
while time.time() < end:
    chunk = ser.read(ser.in_waiting or 1)
    if not chunk:
        continue
    buf += chunk
    while b"\n" in buf:
        raw, buf = buf.split(b"\n", 1)
        line = raw.decode("utf-8", "replace").rstrip("\r")
        if ("开始接收按键" in line or "CONNUPD" in line
                or "CONNUPD_REQ" in line or "CONNREQ" in line):
            print("  " + line, flush=True)
        if "开始接收按键" in line:
            connected = True
            break
    if connected:
        break

if not connected:
    print("!! 150 秒内没连上，退出", flush=True)
    ser.close()
    sys.exit(1)

# 多读几秒，把连接阶段剩下的日志（参数协商结果）打全
t = time.time() + 3
info = []
while time.time() < t:
    chunk = ser.read(ser.in_waiting or 1)
    if chunk:
        info.append(chunk.decode("utf-8", "replace"))
ser.close()

blob = "".join(info)
print("\n--- 连接阶段日志 ---")
for line in blob.splitlines():
    if line.strip():
        print("  " + line)

print("\n已连接。接着跑测量轮次。", flush=True)
sys.exit(subprocess.call([sys.executable, "audio_ab.py", PORT, "--round", "1", "--wait", "110"]))
