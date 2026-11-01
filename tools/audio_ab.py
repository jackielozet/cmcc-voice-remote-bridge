#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""A/B 对照：关掉串口输出 vs 开着串口输出，看 ESP32 分别收到多少条语音通知。

为什么要这么做：
  实测发现遥控器只发约 0.72 秒（55 帧）就停了，而且帧到达间隔约 14ms，
  比实时的 20ms 还快 —— 像是把一批缓冲冲完就断供。
  但历史数据里"关闭打印"时能收到 284 帧。所以必须先分清：
      A) 遥控器本来就停发        -> frames 无论开关打印都一样少
      B) 是串口输出把链路拖死了  -> 关打印时 frames 明显变多

  A 和 B 的修法完全不同，所以先量。

用法：
    python audio_ab.py COM11 --round 2
"""
import argparse
import re
import sys
import time

import serial

FRAMES_RE = re.compile(r"OK STAT frames=(\d+) dropped=(\d+)[^\n]*")


def send(ser, cmd):
    ser.write((cmd + "\n").encode())
    ser.flush()


def read_until(ser, pattern, timeout):
    """读到匹配 pattern 的行为止，返回 (匹配行, 期间所有行)"""
    rx = re.compile(pattern)
    lines = []
    end = time.time() + timeout
    buf = b""
    while time.time() < end:
        chunk = ser.read(ser.in_waiting or 1)
        if not chunk:
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").rstrip("\r")
            lines.append(line)
            if rx.search(line):
                return line, lines
    return None, lines


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default="COM11")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("--round", type=int, default=2, help="按几轮（每轮按一次语音键）")
    ap.add_argument("--wait", type=float, default=100, help="每轮等按键的上限（秒）")
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.05)
    ser.reset_input_buffer()

    # 清空历史计数：先读一次 STAT 作为基线
    send(ser, "STAT")
    line, _ = read_until(ser, r"OK STAT frames=", 3)
    base = int(FRAMES_RE.search(line).group(1)) if line else 0
    print("基线 frames=%d" % base)

    results = []
    for r in range(1, args.round + 1):
        # 先测关闭输出（未知量，最有价值），再测开着输出做对照。
        # 开着输出那一轮实测约 55 帧，所以先跑关闭那一轮。
        dump = 0 if r == 1 else 1
        label = "开着串口输出" if dump else "关闭串口输出"
        send(ser, "AUDIO %d" % dump)
        print("\n" + "=" * 58)
        print("第 %d 轮：%s（AUDIO %d）" % (r, label, dump))
        print("   >>> 请按住语音键说一句话，然后松手 <<<")
        print("=" * 58)
        sys.stdout.flush()

        line, _ = read_until(ser, r"KEY 103E DOWN", args.wait)
        if not line:
            print("!! 没等到语音键按下，跳过本轮")
            continue
        t_down = time.time()
        print("   已检测到按下…（说 4~5 秒再松手）")
        sys.stdout.flush()

        line, _ = read_until(ser, r"KEY 103E up", 30)
        t_up = time.time()
        if not line:
            print("!! 没等到抬起")
            continue
        hold = t_up - t_down
        print("   已松手，按住 %.2f 秒" % hold)

        # 等流彻底停（固件 300ms 判停），再读计数
        time.sleep(1.5)
        send(ser, "STAT")
        line, _ = read_until(ser, r"OK STAT frames=", 5)
        if not line:
            print("!! 读不到 STAT")
            continue
        m = FRAMES_RE.search(line)
        frames, dropped = int(m.group(1)), int(m.group(2))
        got = frames - base
        base = frames
        need = hold * 50 * 3                     # 50 帧/秒 × 3 条/帧
        print("   本轮通知 %d 条 = %d 帧 = %.2f 秒音频（理论需 %d 条 / %.2f 秒）"
              % (got, got // 3, got / 3 / 50.0, need, hold))
        print("   覆盖率 %.0f%%   丢帧计数 %d" % (100.0 * got / need if need else 0, dropped))
        results.append((label, hold, got, dropped))

    ser.close()

    print("\n" + "=" * 58)
    print("对照结果")
    print("%-16s %8s %10s %12s" % ("模式", "按住秒", "通知条数", "折算音频秒"))
    for label, hold, got, dropped in results:
        print("%-16s %8.2f %10d %12.2f" % (label, hold, got, got / 3 / 50.0))

    if len(results) >= 2:
        a, b = results[0][2], results[1][2]
        print()
        if b > a * 2:
            print("结论 B：关掉串口输出后通知数暴涨 %.1f 倍 —— 瓶颈在串口输出路径，"
                  "蓝牙链路本身能收。" % (b / max(1, a)))
        elif abs(a - b) <= max(a, b) * 0.3:
            print("结论 A：开关串口输出对通知数几乎没有影响 —— 是遥控器自己停发，"
                  "问题在链路/握手，不在串口。")
        else:
            print("结论：两种情况都有影响，需结合具体数值判断（差别不到 2 倍但超过 30%%）。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
