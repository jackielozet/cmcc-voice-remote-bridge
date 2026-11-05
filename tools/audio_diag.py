#!/usr/bin/env python3
"""音频流诊断：统计 0xFC 语音帧有没有断流、丢帧，定位"吞字"到底出在哪一层。

用法：
    python audio_diag.py COM11
    python audio_diag.py COM11 --secs 40

做四件事：
  1. 打开固件的 AUDIO 转储，抓一段时间（默认 30 秒，Ctrl+C 可提前结束）
  2. 按 seq 统计真实丢帧率
  3. 扫出所有 >200ms 的断流，标出时长和丢了多少帧
  4. 结束时读 STAT，跟固件自报的 dropped 对账

判读：
  · 出现 >500ms 断流  -> 上位机靠 500ms 静默切句，必然误判"说完了"，
                          表现就是吞字 / 上一句尾巴跑到下一句
  · 断流很干净但 dropped > 0 -> 桥侧队列或 USB TX 拥塞，跟空中无关
  · 两者都干净       -> 音频没问题，往 ASR 那边查
"""

import sys
import time
import argparse

import serial
import serial.tools.list_ports as list_ports

SILENCE_MS = 500          # 上位机 asr_win.SILENCE_MS
FRAME_MS = 20.0           # 一帧 20ms


def find_port():
    hits = [p.device for p in list_ports.comports()
            if "303A" in (p.hwid or "").upper()
            or "1A86" in (p.hwid or "").upper()]
    return hits


def parse_audio(line):
    """'A <40 字节 hex>' -> (seq, sub)；不是音频帧返回 None。"""
    body = line[2:].strip()
    if len(body) < 8:
        return None
    try:
        raw = bytes.fromhex(body)
    except ValueError:
        return None
    if len(raw) < 4:
        return None
    return raw[0] | (raw[1] << 8), raw[2]


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", nargs="?")
    ap.add_argument("--secs", type=float, default=30.0)
    ap.add_argument("--baud", type=int, default=115200)
    args = ap.parse_args()

    port = args.port
    if not port:
        hits = find_port()
        if len(hits) != 1:
            print("找到的串口：%s" % (hits or "无"))
            print("用法：python audio_diag.py COM11")
            return 2
        port = hits[0]

    print("串口 %s，准备抓 %.0f 秒" % (port, args.secs))
    print("请让遥控器保持连接，然后按住语音键正常说一句话。\n")

    ser = serial.Serial(port, args.baud, timeout=0.1)
    time.sleep(0.4)
    ser.reset_input_buffer()

    def cmd(text, wait=0.6):
        ser.write((text + "\n").encode())
        ser.flush()
        end = time.time() + wait
        got = []
        while time.time() < end:
            got.append(ser.read(4096))
        return b"".join(got).decode("utf-8", "replace")

    banner = cmd("AUDIO 1", 1.2)
    for ln in banner.splitlines():
        if ln.startswith("OK "):
            print("  %s" % ln.strip())

    # ---------------- 抓流 ----------------
    t0 = time.time()
    buf = b""
    events = []            # [(seq, t)]，同一 seq 只记第一次
    seen = set()
    voice_marks = []       # [(t, up/down)]
    n_audio = 0
    try:
        while time.time() - t0 < args.secs:
            chunk = ser.read(4096)
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                raw, buf = buf.split(b"\n", 1)
                line = raw.decode("utf-8", "replace").strip("\r")
                now = time.time() - t0
                if line.startswith("A "):
                    n_audio += 1
                    got = parse_audio(line)
                    if got is None:
                        continue
                    seq, _sub = got
                    if seq not in seen:
                        seen.add(seq)
                        events.append((seq, now))
                elif "# VOICE" in line:
                    state = "按下" if line.strip().endswith("1") else "松手"
                    voice_marks.append((now, state))
    except KeyboardInterrupt:
        print("\n（手动结束）")

    # 收尾：关转储 + 读 STAT
    tail = cmd("AUDIO 0", 0.4) + cmd("STAT", 1.0)
    ser.close()

    dur = time.time() - t0
    print("\n" + "=" * 62)
    print("抓取 %.1fs，音频行 %d 条，唯一帧 %d 帧（理论上限 %.0f 帧）"
          % (dur, n_audio, len(events), dur * 1000 / FRAME_MS))

    if len(events) < 2:
        print("\n没抓到音频帧。可能原因：遥控器没连上桥、语音键没按，或 AUDIO 转储没开。")
        print("先确认按语音键时板子蓝灯/红灯有反应，再重跑一次。")
        return 1

    # ---------------- 丢帧 ----------------
    lost = 0
    gaps = []
    for i in range(1, len(events)):
        prev_seq, prev_t = events[i - 1]
        seq, t = events[i]
        step = (seq - prev_seq) & 0xFFFF
        if step > 10000:                 # seq 回绕/复位，跳过
            continue
        if step > 1:
            n = step - 1
            lost += n
            gaps.append((prev_t, t, n))
    expect = len(events) + lost
    print("丢帧 %d 帧 / 期望 %d 帧 = %.1f%%"
          % (lost, expect, lost * 100.0 / max(expect, 1)))

    # ---------------- 断流 ----------------
    stalls = []
    for i in range(1, len(events)):
        prev_t = events[i - 1][1]
        t = events[i][1]
        dt = (t - prev_t) * 1000
        if dt >= 200:
            stalls.append((prev_t, dt))
    over500 = [s for s in stalls if s[1] >= SILENCE_MS]

    print("\n超过 200ms 的断流共 %d 次" % len(stalls))
    if stalls:
        stalls.sort(key=lambda x: -x[1])
        for t, dt in stalls[:10]:
            flag = "  <<< 超过 500ms，会被误判成一句说完" if dt >= SILENCE_MS else ""
            print("   t=%6.2fs  断了 %5.0fms%s" % (t, dt, flag))
        if len(stalls) > 10:
            print("   …另外 %d 次更短的" % (len(stalls) - 10))

    # ---------------- 固件自报 ----------------
    print("\n固件 STAT：")
    for ln in tail.splitlines():
        if ln.startswith("OK STAT"):
            print("  %s" % ln.strip())

    # ---------------- 结论 ----------------
    print("\n" + "-" * 62)
    if over500:
        print("结论：有 %d 次 >500ms 断流。这就是吞字的直接原因——" % len(over500))
        print("      上位机靠 500ms 静默判断『说完了』，断流一到就把一句切成两句，")
        print("      切的时候还会 reset 拼帧状态，半截帧直接丢掉。")
    elif lost * 100.0 / max(expect, 1) > 3:
        print("结论：丢帧率 %.1f%%，偏高但都是碎丢，没形成 >500ms 的整段断流。"
              % (lost * 100.0 / max(expect, 1)))
        print("      听起来是声音发闷/毛刺，不是整字消失。查 USB 线、主机负载。")
    elif stalls:
        print("结论：只有短断流（<500ms），不足以触发切句。")
        print("      若仍吞字，问题在 ASR 侧或解码，不在音频链路。")
    else:
        print("结论：音频流很干净，没有断流。串口打印不是原因，问题在 ASR 侧。")
    print("-" * 62)
    return 0


if __name__ == "__main__":
    sys.exit(main())
