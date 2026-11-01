#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""按住语音键说一句话，量出"音频到底有没有实时到齐"。

判定标准很直接：
  VOICE 1（按下）到 VOICE 0（抬起）之间的 A 帧数，应该接近
  (抬起时刻 - 按下时刻) * 50。差得远，或者大量 A 帧落在 VOICE 0 之后，
  就说明数据滞后。lat=99 那版就是典型的重度滞后。

用法：
    python voice_probe.py COM11 --wait 120
"""
import argparse
import sys
import time

import serial


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?", default="COM11")
    ap.add_argument("--wait", type=float, default=120, help="总体等待上限（秒）")
    ap.add_argument("--baud", type=int, default=115200)
    ap.add_argument("-o", "--out", default="voice_probe.log")
    args = ap.parse_args()

    ser = serial.Serial(args.port, args.baud, timeout=0.1)
    ser.reset_input_buffer()

    log = open(args.out, "w", encoding="utf-8")
    t0 = time.time()
    events = []          # (相对秒, 类型, 内容)
    voice_on = None
    voice_off = None
    frames = []          # 每个 A 帧的到达时刻

    buf = b""
    while time.time() - t0 < args.wait:
        chunk = ser.read(ser.in_waiting or 1)
        if not chunk:
            if voice_off is not None and time.time() - t0 - voice_off[0] > 2.0:
                break    # 已经收到边界，多等 2 秒收尾就够了
            continue
        buf += chunk
        while b"\n" in buf:
            raw, buf = buf.split(b"\n", 1)
            line = raw.decode("utf-8", "replace").rstrip("\r")
            now = time.time() - t0
            log.write("%7.3f  %s\n" % (now, line))
            log.flush()

            if line.startswith("A "):
                frames.append(now)
            elif line.startswith("# VOICE"):
                events.append((now, line))
                print("%7.3f  %s" % (now, line))
                if " 1" in line[-2:] and voice_on is None:
                    voice_on = (now, len(frames))
                elif " 0" in line[-2:] and voice_off is None:
                    voice_off = (now, len(frames))
            elif line.startswith("#") and ("GAP" in line or "KEY" in line):
                print("%7.3f  %s" % (now, line))

    ser.close()
    log.close()

    print("\n" + "=" * 62)
    total = len(frames)
    print("A 帧总数: %d  (%.2f 秒音频 @50 帧/秒)" % (total, total / 50.0))
    if voice_on is None:
        print("!! 没抓到 VOICE 1（按下），请确认按的是语音键")
        return 1
    if voice_off is None:
        print("!! 没抓到 VOICE 0（抬起）")
        return 1

    t_press, idx_press = voice_on
    t_release, idx_release = voice_off
    hold = t_release - t_press
    in_window = idx_release - idx_press
    after = total - idx_release

    print("按住时长      : %.3f 秒" % hold)
    print("理论帧数      : %.0f" % (hold * 50))
    print("按键期间到达  : %d 帧" % in_window)
    print("抬起之后到达  : %d 帧   <- 这个数是滞后量的直接体现" % after)
    if in_window > 0:
        print("按键期间覆盖率: %.0f%%" % (100.0 * in_window / max(1.0, hold * 50)))

    if after <= 3 and in_window >= hold * 50 * 0.7:
        print("\n结论: 实时性正常，音频基本随说随到。")
    elif after > 10:
        print("\n结论: 仍有明显滞后，%d 帧是在松手之后才涌进来的。" % after)
    else:
        print("\n结论: 略有滞后但可接受。")
    return 0


if __name__ == "__main__":
    sys.exit(main())
