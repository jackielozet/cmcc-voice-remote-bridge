# -*- coding: utf-8 -*-
"""按住语音键连续说话，观察音频流什么时候断、松手后是否补发。

与 voice_probe.py 的区别：
  - 打开 AUDIO 1（需要逐帧时间戳，牺牲一点吞吐换可观测性）
  - 松手后继续观察 30 秒，确认遥控器是否还有补发
  - 按 0.5 秒分箱打印帧密度，直接看出"从哪一刻开始断流"
  - 音频时长按 40ms/帧 换算（G.722.1 8k 模式，40 字节一帧）

用法：
    python voice_long.py COM11
"""
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM11"
MAX_WAIT = float(sys.argv[2]) if len(sys.argv) > 2 else 70
TAIL_SEC = 30.0          # 松手后继续观察多久

FRAME_MS = 40.0          # 每帧 40ms 音频


def open_serial(p, tries=15):
    for _ in range(tries):
        try:
            return serial.Serial(p, 115200, timeout=0.1)
        except Exception:
            time.sleep(0.8)
    raise SystemExit("串口 %s 打不开" % p)


ser = open_serial(PORT)
time.sleep(1.0)
ser.reset_input_buffer()

# 打开音频输出
ser.write(b"AUDIO 1\n")
ser.flush()
warm = b""
t = time.time()
while time.time() - t < 0.8:
    d = ser.read(ser.in_waiting or 1)
    if d:
        warm += d
print("AUDIO 1 ->", (warm.decode("utf-8", "replace").strip().splitlines() or ["(无回应)"])[-1])
print()
print("请按住语音键，【连续说话 8 秒，中间不要停顿】，然后松手。")
print("松手后我会再盯 %d 秒。正在监听……" % int(TAIL_SEC))
print()

t0 = time.time()
buf = b""
frames = []            # 每个 A 帧的到达时刻
voice_on = None
voice_off = None
other = []             # 其他值得注意的行

while time.time() - t0 < MAX_WAIT:
    chunk = ser.read(ser.in_waiting or 1)
    if chunk:
        buf += chunk
    while b"\n" in buf:
        raw, buf = buf.split(b"\n", 1)
        line = raw.decode("utf-8", "replace").rstrip("\r")
        now = time.time() - t0

        if line.startswith("A "):
            frames.append(now)
        elif line.startswith("# VOICE"):
            s = line.strip()
            print("%7.3f  %s" % (now, s))
            if s.endswith("1") and voice_on is None:
                voice_on = now
            elif s.endswith("0") and voice_off is None:
                voice_off = now
                print("%7.3f  ----- 已松手，继续观察 %d 秒 -----" % (now, int(TAIL_SEC)))
        elif line.startswith("#") and ("GAP" in line or "KEY" in line or "END" in line):
            other.append((now, line))
            print("%7.3f  %s" % (now, line))

    if voice_off is not None and time.time() - t0 - voice_off > TAIL_SEC:
        break

ser.close()

print()
print("=" * 66)
if voice_on is None or voice_off is None:
    print("!! 边界没抓全：voice_on=%s voice_off=%s" % (voice_on, voice_off))
    print("   请确认按的是语音键，并且 AUDIO 已打开。")
    sys.exit(1)

hold = voice_off - voice_on
in_win = sum(1 for f in frames if voice_on <= f <= voice_off)
after = sum(1 for f in frames if f > voice_off)
print("按住时长        : %.2f 秒" % hold)
print("理论帧数        : %.0f 帧（50 帧/秒）" % (hold * 50))
print("按键期间收到    : %d 帧  = %.2f 秒音频  (覆盖率 %.0f%%)"
      % (in_win, in_win * FRAME_MS / 1000.0, 100.0 * in_win / max(1.0, hold * 50)))
print("松手后 %2d 秒内  : %d 帧  = %.2f 秒音频" % (int(TAIL_SEC), after, after * FRAME_MS / 1000.0))
print("总计            : %d 帧  = %.2f 秒音频" % (len(frames), len(frames) * FRAME_MS / 1000.0))
print()

# 分箱：从按下开始，每 0.5 秒一格，看密度变化
print("帧密度（每 0.5 秒一格，'#' 一个帧）：")
span_end = (max(frames) if frames else voice_on) - voice_on
n_bins = int(span_end / 0.5) + 1
for i in range(min(n_bins, 40)):
    a = voice_on + i * 0.5
    b = a + 0.5
    n = sum(1 for f in frames if a <= f < b)
    mark = "  <-- 松手" if a <= voice_off < b else ""
    print("  %5.1fs  %3d  %s%s" % (i * 0.5, n, "#" * n, mark))

print()
if after > 6:
    print("判定：松手后仍在补发 %d 帧 → 遥控器是「发得慢」，数据没丢。" % after)
elif in_win >= hold * 50 * 0.8:
    print("判定：实时收全，链路正常。")
elif n_bins > 0 and in_win > 0:
    # 找出断流时刻
    last_f = max((f for f in frames if f <= voice_off), default=voice_on)
    print("判定：按键 %.1f 秒，但音频只持续到第 %.1f 秒就断了，之后到松手一帧不来。"
          % (hold, last_f - voice_on))
    print("      说明遥控器推流有固定上限（约 %.1f 秒），不是链路收不过来。"
          % ((last_f - voice_on)))
print("=" * 66)
