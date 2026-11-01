#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
链路截断归因实验（只读，不改固件、不改参数）

依据（固件 cmcc_s3_bridge_v4voice_20261031_2226.ino）：
  L239 handleAudio(): g_audioFrames++ 在最前面，早于 if(!g_audioDump) return;
     => STAT 的 frames 恒等于"桥从空口收到的通知条数"，不受 AUDIO 0/1 影响
  L245/L252/L283: g_audioDropped++ 只在 队列空 / 队列满 / 串口写不全 三处
     => dropped 就是"桥收到了但没能转给 PC"的量
  1 音频帧 = 3 条 20 字节通知（idx 0/1/2），48 字节帧，20ms

两种模式：
  --dump 1  开串口转储：可算 PC 实收率、拼帧、缺口分布、出 wav
  --dump 0  关串口转储：纯测空口天花板（只有桥收通知数）

因为跑次波动极大（实测同一配置 2.8s~6.7s 等价音频），单次对比无意义，
所以用 --interleave N 交替跑 N 对（A1/A0 交替），取中位数比较。

用法：
  python probe_rx_path.py COM11 --interleave 3 --hold 8
  python probe_rx_path.py COM11 --dump 1 --hold 8        # 单轮
"""
import sys
import os
import time
import argparse

import serial  # noqa

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
from voice_bridge_20261031_0115 import (  # noqa
    Framer, dewhite, find_decoder, beep, stamp, CODEC_BYTES,
)

NOTE_PER_FRAME = 3
SEC_PER_FRAME = 0.02


# ----------------------------------------------------------------- 基础 IO --
def drain(ser):
    try:
        n = ser.in_waiting
        if n:
            ser.read(n)
    except Exception:
        pass


def read_for(ser, dur):
    buf = b""
    t = time.time()
    while time.time() - t < dur:
        try:
            n = ser.in_waiting
        except Exception:
            break
        if n:
            buf += ser.read(n)
        else:
            time.sleep(0.01)
    return buf


def stat_once(ser, wait=1.0):
    drain(ser)
    try:
        ser.write(b"STAT\n")
    except Exception:
        return None
    buf = read_for(ser, wait)
    out = {}
    for line in buf.split(b"\n"):
        s = line.decode("utf-8", "replace").strip()
        if s.startswith("OK STAT"):
            for kv in s.split()[2:]:
                k, _, v = kv.partition("=")
                try:
                    out[k] = int(v)
                except ValueError:
                    pass
        elif s.startswith("OK LINK") and "itvl=" in s:
            try:
                out["itvl"] = int(s.split("itvl=")[1].split()[0])
                out["lat"] = int(s.split("lat=")[1].split()[0])
                out["to"] = int(s.split("to=")[1].split()[0])
            except (ValueError, IndexError):
                pass
    return out or None


def wait_connected(ser, timeout=30.0):
    t0 = time.time()
    while time.time() - t0 < timeout:
        st = stat_once(ser, wait=0.5)
        if st and st.get("conn") == 1:
            return st
    return None


# --------------------------------------------------------------- 单轮采集 --
def run_round(ser, dump, hold, post_idle, max_wait, seq_buckets=10):
    """跑一轮采集，返回指标 dict；没检测到按键返回 None"""
    try:
        ser.write(("AUDIO %d\n" % dump).encode())
        ser.write(b"AUDIOSUB 1\n")
    except Exception as e:
        print("# !! 发 AUDIO/AUDIOSUB 失败: %s" % e)
        return None
    time.sleep(0.15)

    base = stat_once(ser, wait=1.0)
    if not base:
        return None
    f0 = base.get("frames", 0)
    d0 = base.get("dropped", 0)

    framer = Framer()
    frames48 = []
    framer.on_frame = lambda f: frames48.append(f)

    seq_notes = {}          # seq -> 收到的 idx 集合大小
    n_a = 0
    held = False
    t_on = None
    t_off = None
    last_byte_t = None
    hinted = False
    line_buf = b""

    beep("ready")
    sys.stdout.flush()
    t_begin = time.time()
    try:
        while time.time() - t_begin < max_wait:
            try:
                waiting = ser.in_waiting
                chunk = ser.read(waiting if waiting else 1)
            except Exception as e:
                print("# !! 串口读失败: %s" % e)
                break
            now = time.time()
            if chunk:
                last_byte_t = now
                line_buf += chunk
                while b"\n" in line_buf:
                    line, line_buf = line_buf.split(b"\n", 1)
                    line = line.strip()
                    if not line:
                        continue
                    if line.startswith(b"A "):
                        n_a += 1
                        try:
                            payload = bytes.fromhex(
                                line[2:].decode("ascii", "ignore").strip())
                        except ValueError:
                            continue
                        if len(payload) >= 20:
                            seq = payload[0] | (payload[1] << 8)
                            idx = payload[2]
                            if idx <= 2:
                                seq_notes.setdefault(seq, set()).add(idx)
                        framer.feed(payload)
                    elif line.startswith(b"# VOICE 1"):
                        if not held:
                            held = True
                            t_on = now
                            framer.reset()
                            del frames48[:]
                            seq_notes.clear()
                            beep("pressed")
                    elif line.startswith(b"# VOICE 0"):
                        if held:
                            held = False
                            t_off = now
            if held and not hinted and t_on and (now - t_on) > hold:
                hinted = True
                beep("release")
                print("  # 已按住 %.0f 秒，可松手" % hold)
                sys.stdout.flush()
            if t_off is not None and last_byte_t and (now - last_byte_t) > post_idle:
                break
    except KeyboardInterrupt:
        print("\n# 手动中断本轮")

    if t_on is None:
        return None

    # 收尾：窗口边缘仍在到达的通知也要计入，否则会把"来晚的 A 行"误记成丢失
    flush_end = time.time() + 1.5
    while time.time() < flush_end:
        try:
            n = ser.in_waiting
            data = ser.read(n) if n else b""
        except Exception:
            break
        if data:
            line_buf += data
            while b"\n" in line_buf:
                line, line_buf = line_buf.split(b"\n", 1)
                if line.startswith(b"A "):
                    n_a += 1
                    try:
                        payload = bytes.fromhex(
                            line[2:].decode("ascii", "ignore").strip())
                    except ValueError:
                        continue
                    if len(payload) >= 20:
                        seq = payload[0] | (payload[1] << 8)
                        idx = payload[2]
                        if idx <= 2:
                            seq_notes.setdefault(seq, set()).add(idx)
                    framer.feed(payload)
        else:
            time.sleep(0.01)

    time.sleep(0.4)                      # 让 drainAudio() 把队列吐完
    end = stat_once(ser, wait=1.0)
    if not end:
        return None

    res = {
        "dump": dump,
        "hold": (t_off - t_on) if t_off else None,
        "bridge": end.get("frames", 0) - f0,
        "dropped": end.get("dropped", 0) - d0,
        "pc_lines": n_a,
        "assembled": framer.frames,
        "bad": framer.bad,
        "gap": framer.gap,
        "first_seq": framer.first_seq,
        "final_seq": framer.final_seq,
        "frames48": frames48,
        "seq_notes": dict(seq_notes),
    }

    # 缺口位置分布：把 seq 跨度均分 10 桶，统计每桶缺多少帧
    if framer.first_seq is not None and framer.final_seq is not None:
        span = ((framer.final_seq - framer.first_seq) & 0xFFFF) + 1
        res["span"] = span
        buckets = [0] * seq_buckets
        partial = 0
        for i in range(span):
            s = (framer.first_seq + i) & 0xFFFF
            if len(res["seq_notes"].get(s, ())) == NOTE_PER_FRAME:
                continue
            b = min(seq_buckets - 1, i * seq_buckets // max(span, 1))
            buckets[b] += 1
            if s in res["seq_notes"]:
                partial += 1
        res["buckets"] = buckets
        res["partial"] = partial
    return res


def summarize(tag, r):
    h = r["hold"] or 0
    eq = r["bridge"] / float(NOTE_PER_FRAME) * SEC_PER_FRAME
    print("  %-10s 按住 %5.2fs | 桥收 %4d 条 | dropped %2d | PC行 %4d | "
          "等价音频 %5.2fs | 效率 %.2f" %
          (tag, h, r["bridge"], r["dropped"], r["pc_lines"], eq,
           (eq / h) if h else 0))


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port")
    ap.add_argument("--dump", type=int, default=1, choices=(0, 1))
    ap.add_argument("--interleave", type=int, default=0,
                    help="交替跑 N 对（AUDIO1/AUDIO0），覆盖 --dump")
    ap.add_argument("--rounds", type=int, default=1,
                    help="同模式连跑 N 轮（配合 --dump 用）")
    ap.add_argument("--connpar", default=None,
                    help="可选：连接后先发 CONNPAR，格式 'itvl,lat,to' 如 '24,0,400'")
    ap.add_argument("--hold", type=float, default=8.0)
    ap.add_argument("--post-idle", type=float, default=2.5)
    ap.add_argument("--round-wait", type=float, default=40.0)
    ap.add_argument("--out", default=None)
    ap.add_argument("--decoder", default=None)
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
        return 2
    print("# 已打开 %s" % a.port)

    st = wait_connected(ser)
    if st is None:
        print("# !! 30s 没等到 conn=1，请按一下遥控器语音键唤醒后重跑")
        ser.close()
        return 2
    print("# 已连上  itvl=%s lat=%s to=%s" %
          (st.get("itvl"), st.get("lat"), st.get("to")))
    time.sleep(1.0)

    if a.connpar:
        try:
            it, la, to = [int(x) for x in a.connpar.split(",")]
        except Exception:
            print("# !! --connpar 格式应为 'itvl,lat,to'，例如 24,0,400")
            ser.close()
            return 2
        ser.write(("CONNPAR %d %d %d %d\n" % (it, it, la, to)).encode())
        print("# 已发 CONNPAR %d %d %d %d" % (it, it, la, to))
        got = None
        t0 = time.time()
        acc = b""
        while time.time() - t0 < 6.0:
            n = ser.in_waiting
            if n:
                acc += ser.read(n)
                if b"GAP CONNUPD" in acc:
                    got = acc.decode("utf-8", "replace")
                    break
            else:
                time.sleep(0.02)
        if got:
            for ln in got.splitlines():
                if "GAP CONNUPD" in ln:
                    print("# 生效: " + ln.strip())
        else:
            print("# !! 6s 未等到 GAP CONNUPD，参数可能被拒；STAT 回读确认：")
            cur = stat_once(ser, wait=1.0)
            print("#    当前 itvl=%s lat=%s to=%s" %
                  (cur.get("itvl") if cur else "?",
                   cur.get("lat") if cur else "?",
                   cur.get("to") if cur else "?"))
        time.sleep(0.8)

    plan = []
    if a.interleave:
        for i in range(a.interleave):
            plan.append(1)
            plan.append(0)
    else:
        plan.extend([a.dump] * max(1, a.rounds))

    print("# 共 %d 轮。每轮：三声上行短音 -> 按住说一句、坚持 %.0f 秒 -> "
          "两声中音后松手" % (len(plan), a.hold))

    results = []
    try:
        for i, dump in enumerate(plan, 1):
            print("")
            print("---- 第 %d/%d 轮：AUDIO %d（%s）----" %
                  (i, len(plan), dump, "开转储" if dump else "关转储"))
            r = run_round(ser, dump, a.hold, a.post_idle, a.round_wait)
            if r is None:
                print("  !! 本轮没检测到按键，跳过")
                continue
            results.append(r)
            summarize("AUDIO %d" % dump, r)
    except KeyboardInterrupt:
        print("\n# 手动中断")
    finally:
        ser.close()

    if not results:
        print("# 没有任何有效轮次")
        return 2

    # ---------------- 汇总 ----------------
    print("")
    print("================ 汇总 ================")
    for dump in (1, 0):
        rs = [r for r in results if r["dump"] == dump]
        if not rs:
            continue
        br = sorted(r["bridge"] for r in rs)
        eq = [x / float(NOTE_PER_FRAME) * SEC_PER_FRAME for x in br]
        med = eq[len(eq) // 2] if len(eq) % 2 else (eq[len(eq)//2 - 1] + eq[len(eq)//2]) / 2.0
        print("AUDIO %d : n=%d  桥收条数 %s  等价音频 %s  中位 %.2fs" %
              (dump, len(rs), br, ["%.2f" % x for x in eq], med))
        if dump == 1:
            print("           PC 实收率 %s" %
                  ["%.1f%%" % (100.0 * (r["bridge"] - r["dropped"]) / r["bridge"]
                               if r["bridge"] else 0) for r in rs])
            print("           拼帧 %s  缺口 %s" %
                  ([r["assembled"] for r in rs], [r["gap"] for r in rs]))

    # 缺口分布（仅 dump=1 的轮次）
    for r in results:
        if r["dump"] == 1 and "buckets" in r:
            b = r["buckets"]
            tot = sum(b)
            print("缺口分布(共%d帧缺，从左到右=流的头->尾)：" % tot)
            print("  " + "  ".join("%d" % x for x in b))
            print("  其中成组残缺(收到部分idx) %d 帧，完全没收到 %d 帧" %
                  (r["partial"], tot - r["partial"]))

    # 每一轮 dump=1 都单独出 wav（之前只存最后一轮，会把最长的那轮丢掉）
    exe = find_decoder(a.decoder)
    base = a.out or os.path.join(HERE, "take_probe_" + stamp())
    best = None
    idx = 0
    for r in results:
        if r["dump"] != 1 or not r["frames48"]:
            continue
        idx += 1
        out = "%s_r%d" % (base, idx)
        codec = b"".join(dewhite(f) for f in r["frames48"])
        with open(out + ".g7221", "wb") as fh:
            fh.write(codec)
        wav_path = out + ".wav"
        if exe:
            import subprocess
            subprocess.run([exe, out + ".g7221", wav_path, "16000"],
                           capture_output=True, text=True)
            if os.path.exists(wav_path):
                sec = len(codec) / CODEC_BYTES * 0.02
                print("# 第%d个 AUDIO1 轮 WAV: %s（%.2f 秒）" % (idx, wav_path, sec))
                if best is None or sec > best[0]:
                    best = (sec, wav_path)
    if best:
        beep("done")
        print("WAV_PATH=%s" % best[1])
    print("======================================")
    return 0


if __name__ == "__main__":
    sys.exit(main())
