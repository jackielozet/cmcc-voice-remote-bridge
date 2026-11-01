# -*- coding: utf-8 -*-
"""按键后长时跟踪：判断遥控器是「丢数据」还是「发得慢、松手后还在补发」。

背景：
  按下语音键 4~5 秒，我们只收到 68~70 帧（约 1.4 秒音频），而盒子能拿到 257 帧。
  但两边 seq 都是连续的、零缺口。零缺口意味着遥控器按顺序发、没有跳号，
  也就是说它没有「发了、我们没收到、重传失败丢弃」的情况。

  那剩下的可能只有两种：
    A. 遥控器把后面的音频丢了，再也不发 —— 丢数据
    B. 遥控器还在慢慢往外吐，只是我们统计得太早 —— 慢慢补发

  这两种的处置方案完全不同，必须用实测分开。
  做法：松手后继续读 STAT 的累计 frames，观察 30 秒内是否还在增长。
  全程 AUDIO 0（串口不打印音频），避免输出本身影响计数。
"""
import re
import sys
import time

import serial

PORT = sys.argv[1] if len(sys.argv) > 1 else "COM11"
TOTAL = int(sys.argv[2]) if len(sys.argv) > 2 else 100


def open_serial(p, tries=15):
    for i in range(tries):
        try:
            return serial.Serial(p, 115200, timeout=0.2)
        except Exception:
            time.sleep(0.8)
    raise SystemExit("串口 %s 打不开（被别的进程占用？）" % p)


ser = open_serial(PORT)
time.sleep(0.8)
ser.reset_input_buffer()


def cmd(c, wait=0.30):
    ser.write((c + "\n").encode())
    ser.flush()
    buf = b""
    t0 = time.time()
    while time.time() - t0 < wait:
        d = ser.read(ser.in_waiting or 1)
        if d:
            buf += d
    return buf.decode("utf-8", "replace")


# 热身：刚打开 CDC 时首字节常被线路设置冲掉，先空发一次
cmd("")
r = cmd("AUDIO 0", 0.5)
print("AUDIO 0 ->", (r.strip().splitlines() or ["(无输出)"])[-1])
time.sleep(0.3)

r = cmd("STAT", 0.6)
m = re.search(r"frames=(\d+).*?voice=(\d)", r, re.S)
if not m:
    print("STAT 解析失败，原始输出：")
    print(r)
    ser.close()
    raise SystemExit(1)
f0 = int(m.group(1))
print("基线 frames=%d voice=%d" % (f0, int(m.group(2))))
print()
print("请按住语音键说 4~5 秒再松手，然后什么都别做。")
print("松手后我会继续盯 30 秒，看累计帧数会不会还在涨。")
print()
print("  %6s  %7s  %6s  %6s  %s" % ("时间", "frames", "增量", "语音", "曲线"))

t0 = time.time()
last_f = f0
last_voice = 0
voice_start = None
voice_stop = None
mark_stop = None
rows = []

while time.time() - t0 < TOTAL:
    r = cmd("STAT", 0.12)
    m = re.search(r"frames=(\d+).*?voice=(\d)", r, re.S)
    if not m:
        time.sleep(0.15)
        continue
    f = int(m.group(1))
    v = int(m.group(2))
    t = time.time() - t0

    changed = (f != last_f) or (v != last_voice)
    if changed:
        delta = f - last_f
        bar = "#" * min(50, delta // 4)
        print("  %6.2f  %7d  %6d  %6s  %s"
              % (t, f, delta, "按下" if v else "松手", bar))
        rows.append((t, f, v))
        last_f = f
        last_voice = v

    if v and voice_start is None:
        voice_start = t
    if not v and voice_start is not None and voice_stop is None:
        voice_stop = t
        mark_stop = f
        print("  ----- 已松手，开始观察补发（30 秒）-----")

    # 松手后 30 秒就收工
    if mark_stop is not None and t - voice_stop > 30:
        break

    time.sleep(0.10)

r = cmd("STAT", 0.6)
m = re.search(r"frames=(\d+)", r)
final_f = int(m.group(1)) if m else last_f
ser.close()

print()
print("=" * 62)
if voice_start is None:
    print("整个窗口里没检测到语音键按下，请重跑。")
else:
    print("按下于 %.2f 秒" % voice_start)
    if voice_stop is None:
        print("窗口结束时仍按着（按住太久？）")
        dur = 0
        during = final_f - f0
        after = 0
    else:
        dur = voice_stop - voice_start
        during = mark_stop - f0
        after = final_f - mark_stop
        print("松手于 %.2f 秒，按住 %.2f 秒" % (voice_stop, dur))
    print()
    print("按键期间收到通知      : %5d 条  (%d 帧, %.2f 秒音频)"
          % (during, during // 3, during / 3 * 0.32))
    print("松手后 30 秒内又收到  : %5d 条  (%d 帧, %.2f 秒音频)"
          % (after, after // 3, after / 3 * 0.32))
    print("总计                  : %5d 条  = %d 帧 = %.2f 秒音频"
          % (final_f - f0, (final_f - f0) // 3, (final_f - f0) / 3 * 0.32))
    if dur > 0:
        print("按住的 %.1f 秒里理论上该有 %d 帧（50帧/秒）"
              % (dur, int(dur * 50)))
    print()
    if after > 6:
        print("结论：遥控器松手后仍在补发（%d 条），属于「发得慢」而不是「丢数据」。" % after)
        print("      处置方向是把接收端吞吐提上去，或者接受多等几秒。")
    else:
        print("结论：松手后基本不再来（仅 %d 条），遥控器确实丢弃了未发出的音频。" % after)
        print("      处置方向只剩提升链路吞吐这一条路。")
print("=" * 62)
