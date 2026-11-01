#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
发 CONNPAR 改连接参数 + 端到端录音出 WAV（同一次串口会话，避免重开复位冲掉参数）

用法：
    python cap_connpar.py COM11 --itvl 24 --lat 50 --to 400 --secs 60 --out take_xxx
"""
import sys
import os
import time
import argparse

import serial  # noqa

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from voice_bridge_20261031_0115 import (  # noqa
    Framer, dewhite, find_decoder, beep, stamp,
    CHUNK_HEAD, CHUNK_DATA, CODEC_BYTES, SAMPLES_PER_FRAME,
)


def wait_connected(ser, timeout=25.0):
    itvl = None
    t0 = time.time()
    while time.time() - t0 < timeout:
        try:
            ser.write(b"STAT\n")
        except Exception:
            return None
        time.sleep(0.4)
        buf = b""
        tend = time.time() + 0.4
        while time.time() < tend:
            n = ser.in_waiting
            if n:
                buf += ser.read(n)
            else:
                time.sleep(0.02)
        for line in buf.split(b"\n"):
            s = line.decode("utf-8", "replace").strip()
            if s.startswith("OK LINK") and "itvl=" in s:
                try:
                    itvl = int(s.split("itvl=")[1].split()[0])
                except (ValueError, IndexError):
                    pass
        if b"conn=1" in buf:
            return itvl
    return None


def wait_line(ser, match, timeout=6.0):
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


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--itvl", type=int, default=24)
    ap.add_argument("--itvl-max", type=int, default=None,
                    help="连接间隔上限（CONNPAR 第二参）；不给定则等于 --itvl（固定值）")
    ap.add_argument("--lat", type=int, default=50)
    ap.add_argument("--to", type=int, default=400)
    ap.add_argument("--secs", type=float, default=60)
    ap.add_argument("--out", default=None)
    ap.add_argument("--decoder", default=None)
    ap.add_argument("--quiet", action="store_true")
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

    # 1) 等连上
    print("# 等待遥控器连上...")
    itvl = wait_connected(ser)
    if itvl is None:
        print("# !! 25s 没等到 conn=1，遥控可能没连上；仍继续，但可能抓空。")
    else:
        print("# 已连上，初始 itvl=%d (%.2fms)" % (itvl, itvl * 1.25))
        time.sleep(2.0)
        itvl2 = wait_connected(ser, timeout=6.0)
        if itvl2 and itvl2 != itvl:
            print("# 参数已更新 itvl %d -> %d" % (itvl, itvl2))
            itvl = itvl2

    # 2) 发 CONNPAR
    itvl_max = a.itvl_max if a.itvl_max is not None else a.itvl
    cmd = "CONNPAR %d %d %d %d\n" % (a.itvl, itvl_max, a.lat, a.to)
    ser.write(cmd.encode())
    print("# 已发 %s" % cmd.strip())
    ln = wait_line(ser, b"GAP CONNUPD", timeout=6.0)
    if ln:
        print("# 生效: " + ln.strip())
    else:
        # 兜底：直接 STAT 看当前 LINK 参数
        ser.write(b"STAT\n")
        sl = wait_line(ser, b"OK LINK", timeout=2.0)
        print("# 未等到 GAP CONNUPD，当前 LINK: " + (sl.strip() if sl else "（无）"))

    time.sleep(0.5)

    # 3) 拉起音频透传
    ser.write(b"AUDIO 1\n")
    ser.write(b"AUDIOSUB 1\n")
    time.sleep(0.15)
    print("# 已发 AUDIO 1 / AUDIOSUB 1")

    # 4) 采集（与 voice_bridge capture 同逻辑，复用 Framxer）
    raw_frames = []
    framer = Framer()
    framer.on_frame = lambda f: raw_frames.append(f)
    t_begin = time.time()
    t_end = t_begin + a.secs
    n_a_lines = 0
    line_buf = b""
    starts = []
    ends = []
    last_voice_ms = None
    voice_seen = False
    voice_off_t = None
    t_voice_on = None
    idle_gap = 2.5
    post_idle = 2.5
    post_max = 12.0
    hold_hint = 3.0
    hold_hinted = False

    print("# 现在按住语音键说一句话，说完松手即可。")
    beep("ready")
    sys.stdout.flush()

    try:
        while time.time() < t_end:
            waiting = ser.in_waiting
            chunk = ser.read(waiting if waiting else 1)
            now = time.time()
            if chunk:
                line_buf += chunk
                while b"\n" in line_buf:
                    line, line_buf = line_buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith(b"A "):
                        try:
                            payload = bytes.fromhex(line[2:].decode("ascii", "ignore").strip())
                        except ValueError:
                            continue
                        n_a_lines += 1
                        last_voice_ms = now
                        framer.feed(payload)
                    elif line.startswith(b"# VOICE 1"):
                        voice_seen = True
                        voice_off_t = None
                        t_voice_on = now
                        framer.reset()
                        del raw_frames[:]
                        print("=== 语音键按下，开始录音 ===")
                        beep("pressed")
                    elif line.startswith(b"# VOICE 0"):
                        voice_seen = True
                        voice_off_t = now
                        dur = (now - t_voice_on) if t_voice_on else 0.0
                        print("=== 语音键抬起（按住 %.2f 秒，已收 %d 帧），等残留 ===" %
                              (dur, framer.frames))
                    elif line.startswith(b"# A START"):
                        starts.append(framer.frames)
                        print("=== 检测到语音流开始 ===")
                    elif line.startswith(b"# OK LINK"):
                        print("  # " + line.decode("utf-8", "replace"))
                    elif line.startswith(b"# A END"):
                        ends.append(framer.frames)
                        print("=== 语音流暂停，累计 %d 帧 ===" % framer.frames)
                    elif not a.quiet:
                        s = line.decode("utf-8", "replace")
                        if ("DISCONNECT" in s or "ERR" in s or "CONNUPD" in s
                                or "INITIAL" in s or "CONNREQ" in s or "MTU" in s):
                            print("  # " + s)
            if (t_voice_on is not None and voice_off_t is None
                    and not hold_hinted and (now - t_voice_on) > hold_hint):
                hold_hinted = True
                beep("release")
                print("# 已按住 %.1f 秒，可以松手了" % hold_hint)
                sys.stdout.flush()
            if voice_off_t is not None:
                quiet = (last_voice_ms is None) or ((now - last_voice_ms) > post_idle)
                if quiet or ((now - voice_off_t) > post_max):
                    print("# 语音键已抬起，之后静默 %.1f 秒（或达上限 %.0f 秒），残留帧收取完毕" %
                          (post_idle, post_max))
                    break
            if voice_off_t is None and (not voice_seen) and last_voice_ms is not None \
                    and (now - last_voice_ms) > idle_gap:
                print("# 静默 %.1f 秒，采集结束" % idle_gap)
                break
    except KeyboardInterrupt:
        print("\n# 手动中断，用已收到的数据继续")
    finally:
        ser.close()

    print("# 通知 %d 条，拼出 %d 帧，坏包 %d，seq 缺口 %d 次" %
          (n_a_lines, framer.frames, framer.bad, framer.gap))
    if framer.first_seq is not None:
        span = ((framer.final_seq - framer.first_seq) & 0xFFFF) + 1
        print("# seq %d -> %d 跨 %d 帧，实收 %d 帧，%s" %
              (framer.first_seq, framer.final_seq, span, framer.frames,
               "完整无缺" if span == framer.frames else "少了 %d 帧" % (span - framer.frames)))
        if framer.first_t is not None:
            print("# 第一帧到达：距采集启动 %.2f 秒" % (framer.first_t - t_begin))

    if framer.frames == 0:
        print("# 没收到任何完整帧。")
        return 1

    out = a.out or os.path.join(HERE, "take_connpar_" + stamp())
    codec = b"".join(dewhite(f) for f in raw_frames)
    raw_path = out + ".g7221"
    with open(raw_path, "wb") as fh:
        fh.write(codec)
    print("# 码流已写 %s（%d 字节）" % (raw_path, len(codec)))

    wav_path = out + ".wav"
    exe = find_decoder(a.decoder)
    if exe:
        r = subprocess_run([exe, raw_path, wav_path, "16000"])
        if os.path.exists(wav_path):
            print("# 解码完成 %s" % wav_path)
            print("# 时长 %.2f 秒" % (len(codec) / CODEC_BYTES * 0.02))
            beep("done")
            return wav_path
        print("# 解码失败")
    else:
        print("# 找不到解码器")
    return None


def subprocess_run(cmd):
    import subprocess
    return subprocess.run(cmd, capture_output=True, text=True)


if __name__ == "__main__":
    sys.exit(0 if main() else 1)
