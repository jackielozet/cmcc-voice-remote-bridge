#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""对 voice_probe.log 做离线体检：查 seq 连续性 + 生成 wav。

要回答的就一个问题：
    按住 5.19 秒只拿到 167 帧（3.34 秒音频），到底是
    A) 遥控器本来就没发满（seq 连续，没有缺口）——那 3.34 秒就是它自己给的
    B) 中间丢了帧（seq 有缺口）——那问题在我们的接收/串口链路

A 和 B 的修法完全不同，所以必须先分清。
"""
import os
import re
import struct
import subprocess
import sys

IV_DEC_TABLE = [4, 16, 9, 8, 3, 11, 2, 6, 18, 7, 13, 17]
XOR_KEY = 0x0416
CODEC_BYTES = 40
WORDS = 20
SAMPLES_PER_FRAME = 320


def dewhite(frame48):
    words = list(struct.unpack("<20H", frame48[:CODEC_BYTES]))
    for g in range(4):
        a, b, c = IV_DEC_TABLE[3 * g], IV_DEC_TABLE[3 * g + 1], IV_DEC_TABLE[3 * g + 2]
        oa, ob, oc = words[a], words[b], words[c]
        words[a], words[b], words[c] = oc, oa, ob
    for i in range(WORDS):
        words[i] ^= XOR_KEY
    out = bytearray(CODEC_BYTES)
    for i, w in enumerate(words):
        out[i * 2] = (w >> 8) & 0xFF
        out[i * 2 + 1] = w & 0xFF
    return bytes(out)


def find_decoder():
    here = os.path.dirname(os.path.abspath(__file__))
    for p in (
        os.path.join(here, "libg7221", "decode.exe"),
        os.path.join(os.path.dirname(here), "tools", "libg7221", "decode.exe"),
        os.path.join(here, "..", "..", "tools", "libg7221", "decode.exe"),
    ):
        if os.path.exists(p):
            return os.path.abspath(p)
    return None


def main():
    src = sys.argv[1] if len(sys.argv) > 1 else "voice_probe.log"
    out_raw = sys.argv[2] if len(sys.argv) > 2 else "probe_out.raw"
    out_wav = os.path.splitext(out_raw)[0] + ".wav"

    pat = re.compile(r"^\s*([\d.]+)\s+A ([0-9A-Fa-f]+)\s*$")
    notes = []
    chunks = []
    for line in open(src, encoding="utf-8", errors="replace"):
        m = pat.match(line)
        if m:
            chunks.append((float(m.group(1)), bytes.fromhex(m.group(2))))
        elif "VOICE" in line or "KEY 103E" in line:
            notes.append(line.rstrip())

    print("通知条数: %d" % len(chunks))
    for n in notes:
        print("  " + n)

    # ---- 按 idx 拼帧 ----
    slots = {}
    frames = []            # (seq, ts, payload48)
    bad = 0
    for ts, d in chunks:
        if len(d) < 4 + 16:
            bad += 1
            continue
        seq = struct.unpack("<H", d[0:2])[0]
        idx = d[2]
        if idx > 2:
            bad += 1
            continue
        s = slots.setdefault(seq, {"t": ts})
        s[idx] = d[4:20]
        s["t%d" % idx] = ts
        if all(i in s for i in (0, 1, 2)):
            payload = s[0] + s[1] + s[2]
            frames.append((seq, s["t0"], s["t2"], payload))
            del slots[seq]

    print("\n拼出完整帧: %d   残缺字节组: %d   未拼完的 seq: %d"
          % (len(frames), bad, len(slots)))
    if not frames:
        print("!! 没有完整帧")
        return 1

    frames.sort(key=lambda x: x[1])

    # ---- seq 连续性 ----
    seqs = [f[0] for f in frames]
    gaps = []
    for a, b in zip(seqs, seqs[1:]):
        delta = (b - a) & 0xFFFF
        if delta != 1:
            gaps.append((a, b, delta - 1 if 0 < delta < 0x8000 else -1))
    print("\nseq 范围: %d .. %d" % (seqs[0], seqs[-1]))
    span = (seqs[-1] - seqs[0]) & 0xFFFF
    print("seq 跨度: %d  ->  %.2f 秒音频槽位" % (span + 1, (span + 1) / 50.0))
    print("缺口处数: %d" % len(gaps))
    lost = sum(g[2] for g in gaps if g[2] > 0)
    if gaps:
        print("  前 12 个缺口 (前seq -> 后seq, 丢帧数):")
        for g in gaps[:12]:
            print("    %5d -> %5d   丢 %s" % g)
    print("累计推断丢帧: %d (%.2f 秒)" % (lost, lost / 50.0))

    # ---- 到达节奏：每帧 t2 - t0 的间隔分布 ----
    import statistics
    dts = []
    for i in range(1, len(frames)):
        dt = frames[i][1] - frames[i - 1][1]
        if 0 <= dt < 5:
            dts.append(dt)
    if dts:
        print("\n帧到达间隔: 中位 %.1f ms, 最小 %.1f, 最大 %.1f"
              % (statistics.median(dts) * 1000, min(dts) * 1000, max(dts) * 1000))

    # ---- 落盘解码 ----
    with open(out_raw, "wb") as f:
        for _, _, _, p in frames:
            f.write(dewhite(p))
    dur = len(frames) * SAMPLES_PER_FRAME / 16000.0
    print("\n码流: %s (%d 帧 = %.2f 秒)" % (out_raw, len(frames), dur))

    exe = find_decoder()
    if exe is None:
        print("!! 找不到 decode.exe，跳过 wav")
        return 0
    r = subprocess.run([exe, out_raw, out_wav, "16000"],
                       capture_output=True, text=True)
    if os.path.exists(out_wav):
        print("wav : %s (%.2f 秒)" % (out_wav, dur))
        if r.stdout.strip():
            print("      " + r.stdout.strip().splitlines()[-1])
    else:
        print("!! 解码失败 rc=%d %s" % (r.returncode, r.stderr[:300]))
        return 1

    print("\n" + "=" * 60)
    if not gaps:
        print("结论 A: seq 完全连续 —— 遥控器就发了这么多，不是我们丢的。")
    elif lost <= 2:
        print("结论 A': 仅零星丢 %d 帧，基本可以认为遥控器自己就发这么多。" % lost)
    else:
        print("结论 B: seq 有 %d 帧缺口 —— 是接收链路丢的，需要继续查。" % lost)
    return 0


if __name__ == "__main__":
    sys.exit(main())
