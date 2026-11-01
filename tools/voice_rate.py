# -*- coding: utf-8 -*-
"""按键边界 + 帧数曲线，同时抓，互不干扰。

设计要点：
  - AUDIO 0：不打印音频，串口不成为瓶颈，不影响接收行为
  - 边界用 # VOICE 1 / # VOICE 0（真实的按键报告），
    不用 STAT 里的 voice 字段 —— 那个是「300ms 没新帧就当语音结束」的推断值，
    音频一断就归零，会把断流误当成松手，上次就是这么被带偏的
  - 帧数用 STAT 的 cumulatives frames，只做累计计数，不受打印影响
"""
import re
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM11"
TOTAL = float(sys.argv[2]) if len(sys.argv) > 2 else 80
TAIL = 30.0
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

print("已关闭音频打印，只计数。")
print("请按住语音键，【连续说话 8 秒，中间不停顿】，然后松手。")
print()

t0 = time.time()
buf = b""
frames_now = 0
base = None
samples = []          # (t, frames)
first_press_t = None
first_press_f = None
last_release_t = None
last_release_f = None
rel_shown = False
link_info = ""
last_stat = 0.0
keys = []

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
                if first_press_t is not None:
                    last_release_t, last_release_f = tn, frames_now
                    if not rel_shown:
                        print("%7.3f  ----- 松手，继续观察 %d 秒 -----" % (tn, int(TAIL)))
                        rel_shown = True
        elif line.startswith("# KEY"):
            keys.append((tn, line))
            print("%7.3f  %s" % (tn, line))
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
if base is None:
    print("没读到 STAT，检查一下串口。")
    sys.exit(1)
if first_press_t is None:
    print("没抓到 # VOICE 1。请确认按的是语音键（0x3E）。")
    sys.exit(1)

print("基线 frames = %d" % base)
if link_info:
    print("实际链路参数  : %s" % link_info)
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
print("全程累计接收  : %d 条 = %d 帧 = %.2f 秒音频  <-- 真实覆盖"
      % (total, total // 3, total // 3 * FRAME_MS / 1000.0))
if hold > 0:
    print("按键时长 %.2f 秒理论应有 %d 帧，实际覆盖 %.0f%%"
          % (hold, int(hold * 50), 100.0 * total / (hold * 50)))
print()

# 从按下开始每 0.5 秒统计帧数增量
if first_press_t is not None:
    print("帧数累计曲线（从首次按下起，每 0.5 秒）：")
    t_end = max(s[0] for s in samples) if samples else first_press_t
    i = 0
    while True:
        a = first_press_t + i * 0.5
        if a > t_end:
            break
        b = a + 0.5
        vals = [f for t, f in samples if a <= t < b]
        if not vals:
            print("  %5.1fs   -" % (i * 0.5))
        else:
            lo, hi = min(vals), max(vals)
            tag = ""
            if last_release_t is not None and a <= last_release_t < b:
                tag = "  <-- 松手"
            print("  %5.1fs  %4d 条 (%3d 帧) %s%s"
                  % (i * 0.5, hi - lo, (hi - lo) // 3,
                     "#" * min(40, (hi - lo) // 4), tag))
        i += 1
        if i > 40:
            break
print("=" * 68)
