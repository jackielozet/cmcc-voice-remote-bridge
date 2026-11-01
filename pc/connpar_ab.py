#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
连接参数 A/B 测试（免重烧）—— 验证「连接间隔是不是 BLE 吞吐杠杆」
=================================================================
关键修正：用 AUDIO 0（关串口打印）+ 轮询 STAT 的 frames 累计来测速。
之前版本用 AUDIO 1 抓串口帧，会被 hex 打印本身拖慢 BLE 接收（实测 26→10 通知/秒），
测出的速率是假的。STAT 的 frames 是固件内部计数器（每收一条 0xFC 通知 +1），
不受打印影响，才是真实吞吐。

用法（先确保遥控器已连上，看到固件 reports=9 / # GAP CONNECT）：
    python connpar_ab.py COM11 --itvl 6      # 小间隔 7.5ms（期望速率更高）
    python connpar_ab.py COM11 --itvl 16     # 大间隔 20ms（期望速率更低）
    python connpar_ab.py COM11               # 基线（不打 CONNPAR，用默认 itvl≈8/10ms）

判读：
  - 速率随 itvl 线性变化（小间隔更快、大间隔更慢）→ 间隔是杠杆，确认「每事件包数」相对固定。
  - 速率纹丝不动 → 限速死在 controller 调度，直接上 ESP-IDF 治本（方案 C）。
"""
import sys
import os
import time
import re
import argparse

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import serial  # noqa


def wait_line(ser, match, timeout=6.0):
    """读行直到某行包含 match，或超时。返回该行(str)或 None。"""
    t0 = time.time()
    buf = b""
    while time.time() - t0 < timeout:
        c = ser.read(1)
        if c:
            buf += c
            if b"\n" in buf:
                line, _, rest = buf.partition(b"\n")
                buf = rest
                if match in line:
                    return line.decode("utf-8", "replace")
    return None


def ensure_connected(ser):
    """发 STAT，确认遥控器已连接（conn=1）。"""
    ser.write(b"STAT\n")
    for _ in range(20):
        line = wait_line(ser, b"OK STAT", timeout=1.0)
        if line and "conn=1" in line:
            return True
        if line is None:
            break
    return False


def main():
    ap = argparse.ArgumentParser(description="连接参数 A/B 吞吐测试（免重烧，AUDIO0+STAT）")
    ap.add_argument("port", help="串口，如 COM11")
    ap.add_argument("--itvl", type=int, default=0,
                    help="连接间隔(单位1.25ms)，0=不打CONNPAR用默认")
    ap.add_argument("--lat", type=int, default=0, help="latency，默认0(让遥控器自己推)")
    ap.add_argument("--to", type=int, default=400, help="supervision timeout(单位10ms)")
    ap.add_argument("--secs", type=float, default=60, help="采集上限(秒)")
    a = ap.parse_args()

    ser = None
    for _ in range(10):
        try:
            ser = serial.Serial(a.port, 115200, timeout=0.2)
            break
        except serial.SerialException:
            time.sleep(0.5)
    if ser is None:
        print("# 打不开 %s" % a.port)
        return 1
    print("# 已打开 %s" % a.port)

    if not ensure_connected(ser):
        print("# 遥控器未连接。请先按遥控器任意键连上，再跑本脚本。")
        ser.close()
        return 1
    print("# 已确认遥控器连接。")

    if a.itvl > 0:
        cmd = "CONNPAR %d %d %d %d\n" % (a.itvl, a.itvl, a.lat, a.to)
        ser.write(cmd.encode())
        print("# 已发 %s" % cmd.strip())
        ln = wait_line(ser, b"GAP CONNUPD", timeout=6.0)
        if ln:
            print("# 生效: " + ln.strip())
        else:
            print("# 未在 6s 内收到 GAP CONNUPD（遥控器可能未接受），仍继续测。")
        time.sleep(0.3)
    else:
        print("# 基线：不打 CONNPAR，用固件默认参数(itvl≈8/10ms)。")

    # 关键：关串口打印，测真实 BLE 吞吐（AUDIO 1 抓帧会被 hex 打印拖慢，26→10 通知/秒）
    ser.write(b"AUDIO 0\n")
    time.sleep(0.1)
    print("# 已 AUDIO 0（关打印，测真实吞吐）。请按住语音键说 5~8 秒，松手。")

    def read_frames():
        ser.write(b"STAT\n")
        ln = wait_line(ser, b"OK STAT", timeout=1.0)
        if ln:
            m = re.search(r"frames=(\d+)", ln)
            if m:
                return int(m.group(1))
        return None

    t_begin = time.time()
    t_end = t_begin + a.secs
    line_buf = b""
    voice_on = None
    voice_off = None
    base_frames = None
    press_f = None
    release_f = None
    last_f = 0
    tail_gap = 0.35
    last_stat = 0.0
    try:
        while time.time() < t_end:
            # 每 0.5s 轮询 STAT，记录累计通知数（与打印无关，是真实吞吐）
            if time.time() - last_stat >= 0.5:
                f = read_frames()
                if f is not None:
                    if base_frames is None:
                        base_frames = f
                    last_f = f - base_frames
                last_stat = time.time()
            w = ser.in_waiting
            chunk = ser.read(w if w else 1)
            if chunk:
                line_buf += chunk
                while b"\n" in line_buf:
                    line, _, line_buf = line_buf.partition(b"\n")
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith(b"# VOICE 1"):
                        voice_on = time.time()
                        voice_off = None
                        f = read_frames()
                        press_f = (f - base_frames) if (f is not None and base_frames is not None) else last_f
                        print("=== 语音键按下 ===")
                    elif line.startswith(b"# VOICE 0"):
                        voice_off = time.time()
                        f = read_frames()
                        release_f = (f - base_frames) if (f is not None and base_frames is not None) else last_f
                        dur = (voice_off - voice_on) if voice_on else 0.0
                        during = (release_f - press_f) if (release_f is not None and press_f is not None) else 0
                        print("=== 语音键抬起（按住 %.2f 秒，期间收 %d 通知）===" % (dur, during))
                    elif line.startswith(b"# OK LINK"):
                        print("  # " + line.decode("utf-8", "replace"))
            if voice_off is not None and (time.time() - voice_off) > tail_gap:
                print("# 语音键已抬起，残留收完")
                break
    except KeyboardInterrupt:
        print("\n# 手动中断，用已收到的数据继续")
    finally:
        ser.close()

    hold = (voice_off - voice_on) if (voice_on and voice_off) else 0.0
    during = (release_f - press_f) if (release_f is not None and press_f is not None) else last_f
    frames = during if during > 0 else last_f

    print("=" * 64)
    print("A/B 结果 (itvl=%s)" % ("基线(默认)" if a.itvl == 0 else a.itvl))
    print("  按住时长      : %.2f 秒" % hold)
    print("  收到通知      : %d 条" % frames)
    if hold > 0 and frames > 0:
        rate_n = frames / hold
        print("  接收速率      : %.1f 通知/秒 (约 %.1f 帧/秒)" % (rate_n, rate_n / 3.0))
        print("  覆盖率        : %.0f%% (理论 %.0f 通知，按满速150通知/秒计)" %
              (100.0 * frames / (hold * 150), hold * 150))
        itvl_ms = (a.itvl * 1.25) if a.itvl > 0 else (8 * 1.25)
        ev_rate = 1000.0 / itvl_ms
        print("  事件率        : %.0f 事件/秒 -> %.3f 包/事件 (对比盒子 ~1.5)" %
              (ev_rate, rate_n / ev_rate))
    else:
        print("  (按住时长或帧数为0，本次无效，请重按一次 5~8 秒)")
    print("=" * 64)
    return 0


if __name__ == "__main__":
    sys.exit(main())
