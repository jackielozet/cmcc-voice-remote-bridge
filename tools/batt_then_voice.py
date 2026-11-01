# -*- coding: utf-8 -*-
"""先等链路建立 -> 读遥控器电量 -> 直接进入一轮录音统计。

为什么合成一个脚本：每次烧录后板子重启，链路要重新建，用户就得按一次键；
电量要在连接之后才能读，录音又要在按键之后测。分开跑要用户按三次，
合起来只需要一次连接按键 + 一次语音键。

电量来自遥控器的标准 Battery Service：
  0x180F / Battery Level 0x2A19，value handle 0x0010（由盒子 btsnoop 服务发现确认）
"""
import re
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM11"
WAIT_CONN = 100.0      # 最多等这么久让人按一下键建链
TOTAL = 80.0           # 建链后录音统计窗口
TAIL = 30.0            # 松手后继续观察时长
FRAME_MS = 20.0        # 每帧 20ms：盒子基准 257 帧 = 82240 采样 @16000Hz = 5.14 秒


def open_serial(p, tries=15):
    for _ in range(tries):
        try:
            return serial.Serial(p, 115200, timeout=0.05)
        except Exception:
            time.sleep(0.8)
    raise SystemExit("串口 %s 打不开" % p)


ser = open_serial(PORT)
time.sleep(0.9)
ser.reset_input_buffer()
ser.write(b"AUDIO 0\n")
ser.flush()
time.sleep(0.8)
ser.reset_input_buffer()
print("已关闭音频打印（不影响接收行为）。")
print("请先按遥控器任意键，把板子和遥控器连上……")
print()

# ---------------------------------------------------------------- 等连接 --
buf = b""
conn_deadline = time.time() + WAIT_CONN
connected = False
reports = 0
while time.time() < conn_deadline:
    ser.write(b"STAT\n")
    ser.flush()
    t_end = time.time() + 0.5
    while time.time() < t_end:
        d = ser.read(ser.in_waiting or 1)
        if d:
            buf += d
    while b"\n" in buf:
        raw, buf = buf.split(b"\n", 1)
        line = raw.decode("utf-8", "replace").strip()
        if line.startswith("# CB onConnect"):
            print("  链路建立：%s" % line)
        elif line.startswith("OK STAT"):
            m = re.search(r"conn=(\d+).*?reports=(\d+)", line)
            if m:
                if int(m.group(1)) == 1 and int(m.group(2)) > 0:
                    connected = True
                    reports = int(m.group(2))
    if connected:
        break

if not connected:
    ser.close()
    raise SystemExit("等了 %.0f 秒还没连上，先按键建链再重跑。" % WAIT_CONN)

print("  已连接，reports=%d" % reports)
time.sleep(1.0)          # 让服务发现/加密彻底完成再读电量

# ---------------------------------------------------------------- 读电量 --
buf = b""
for attempt in range(4):
    ser.write(b"BATT\n")
    ser.flush()
    t_end = time.time() + 1.5
    while time.time() < t_end:
        d = ser.read(ser.in_waiting or 1)
        if d:
            buf += d
    if b"OK BATT" in buf or b"ERR" in buf:
        break

print()
print("=" * 68)
batt_ok = False
for raw in buf.decode("utf-8", "replace").splitlines():
    line = raw.strip()
    if not line:
        continue
    if "BATT" in line or "ERR" in line or "0x180F" in line or "0x2A19" in line:
        print("  遥控器电量 -> %s" % line)
        if line.startswith("OK BATT"):
            batt_ok = True
if not batt_ok:
    print("  电量没读到，上面的日志是固件侧的回报。")
print("=" * 68)
print()

# ---------------------------------------------------------------- 录音 ----
print("接下来请按住语音键说话 5~8 秒再松手。")
print()

t0 = time.time()
buf = b""
frames_now = 0
base = None
samples = []
# 边界检测：首次 VOICE1 -> 末次 VOICE0，忽略连接初期虚假松手与中间抖动
first_press_t = None
first_press_f = None
last_release_t = None
last_release_f = None
rel_shown = False
link_info = ""          # 从 STAT 的 OK LINK 行抓取实际生效链路参数
last_stat = 0.0

while time.time() - t0 < TOTAL:
    now = time.time() - t0
    if now - last_stat > 0.15:
        ser.write(b"STAT\n")
        ser.flush()
        last_stat = now
    d = ser.read(ser.in_waiting or 1)
    if d:
        buf += d
    while b"\n" in buf:
        raw, buf = buf.split(b"\n", 1)
        line = raw.decode("utf-8", "replace").strip()
        tn = time.time() - t0
        if line.startswith("# VOICE"):
            v = 1 if line.endswith("1") else 0
            print("%7.3f  VOICE %d" % (tn, v))
            if v == 1:
                if first_press_t is None:
                    first_press_t, first_press_f = tn, frames_now
            else:
                if first_press_t is not None:    # 只在已按下后计松手，过滤虚假 VOICE0
                    last_release_t, last_release_f = tn, frames_now
                    if not rel_shown:
                        print("%7.3f  ----- 松手，继续观察 %d 秒 -----" % (tn, int(TAIL)))
                        rel_shown = True
        elif line.startswith("OK STAT"):
            m = re.search(r"frames=(\d+)", line)
            if m:
                frames_now = int(m.group(1))
                if base is None:
                    base = frames_now
                samples.append((tn, frames_now))
        elif line.startswith("OK LINK"):
            link_info = line
    if last_release_t is not None and time.time() - t0 - last_release_t > TAIL:
        break

ser.close()

print()
print("=" * 68)
if first_press_t is None:
    print("没抓到 # VOICE 1，录音这一轮没测到。")
    sys.exit(1)
if base is None:
    print("没读到 STAT。")
    sys.exit(1)

if link_info:
    print("实际链路参数  : %s" % link_info)
else:
    print("实际链路参数  : (未抓到 OK LINK，连上后 STAT 才回报)")

if last_release_t is not None and last_release_t >= first_press_t:
    hold = last_release_t - first_press_t
    during = last_release_f - first_press_f
    after  = frames_now - last_release_f
    print("按住时长      : %.2f 秒（%.2f -> %.2f）" % (hold, first_press_t, last_release_t))
else:
    hold = (samples[-1][0] - first_press_t) if samples else 0
    during = frames_now - first_press_f
    after  = 0
    print("没抓到有效松手，按「按到窗口结束」估算，时长 %.2f 秒" % hold)

total = during + after
print("按键期间收到  : %d 条 = %d 帧 = %.2f 秒音频" % (during, during // 3, during // 3 * FRAME_MS / 1000.0))
print("松手后 %2d 秒内: %d 条 = %d 帧 = %.2f 秒音频" % (int(TAIL), after, after // 3, after // 3 * FRAME_MS / 1000.0))
print("----------------------------------------------------------------")
print("全程累计接收  : %d 条 = %d 帧 = %.2f 秒音频  <-- 这才是真实覆盖"
      % (total, total // 3, total // 3 * FRAME_MS / 1000.0))
if hold > 0:
    # 注意维度：total 是「通知条数」，覆盖率要用「帧数」比「帧数」，不能把条数当帧数除。
    # 每 3 条通知拼 1 帧；按键按住 hold 秒、满速 50 帧/秒 -> 理论应有 hold*50 帧。
    frames_got   = total / 3.0
    frames_want = hold * 50.0
    print("按键时长 %.2f 秒理论应有 %d 帧，实际覆盖 %.0f%%（%.1f 帧 / %.0f 帧）"
          % (hold, int(frames_want), 100.0 * frames_got / frames_want,
             frames_got, frames_want))
    # 关键诊断：把「每连接事件实际收到几个包」算出来，直接对比盒子（约 1.5 包/事件）
    m_itvl = re.search(r"\(([\d.]+)ms\)", link_info)
    if m_itvl:
        itvl_ms = float(m_itvl.group(1))
        ev_per_s = 1000.0 / itvl_ms
        pkt_per_ev = (total / hold) / ev_per_s
        print("接收速率      : %.1f 通知/秒 = %.3f 包/连接事件（盒子基准 ≈1.5 包/事件）"
              % (total / hold, pkt_per_ev))
print("=" * 68)
