#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CMCC 语音遥控器 -> PC 音频桥（v1, 2026-10-31 0115）
=================================================================
前半段：从 ESP32-S3 的 CDC 串口读固件打出的 "A <HEX>" 帧，
        按 idx(0/1/2) 拼成 48 字节帧，做 ICO 解白化 + 高低字节交换，
        得到标准 G.722.1 的 40 字节码流，落成 .raw。
后半段：调用 tools/libg7221/decode.exe 把 .raw 解成 .wav（该解码器已验证 SNR 29.9dB）。

固件每帧输出（单条 GATT 通知 20 字节，不含 report id）：
    seq(2, LE) | idx(0/1/2) | 00 | 16 字节载荷
3 条拼成 48 字节帧：前 40 字节 = 20 个 word 的码流，后 8 字节是元数据。

解白化（来自 libicocodec.so 的 ivDecTable，反汇编确认）：
    4 组、每组 3 个位置，新[a]=旧[c] 新[b]=旧[a] 新[c]=旧[b]
    然后 20 个 word 逐个 ^ 0x0416
    最后每个 word 高字节/低字节对调，才是 G.722.1 需要的 MSB-first 字节流

用法：
    python voice_bridge_20261031_0115.py list
    python voice_bridge_20261031_0115.py cap COM11 --auto
        # 按一下语音键，说一句，松手 —— 静默 1.5 秒后自动生成 wav
    python voice_bridge_20261031_0115.py cap COM11 --secs 30 --out take
        # 固定采 30 秒
"""
import sys
import os
import time
import struct
import argparse
import subprocess
import datetime

# ---------------------------------------------------------------- 常量 --
IV_DEC_TABLE = [4, 16, 9, 8, 3, 11, 2, 6, 18, 7, 13, 17]
XOR_KEY = 0x0416

HERE = os.path.dirname(os.path.abspath(__file__))
ROOT = os.path.dirname(HERE)

# 解码器就近查找：仓库自带的 / 上一级的 / 放在脚本旁边的都认
DECODER_CANDIDATES = [
    os.path.join(ROOT, "tools", "libg7221", "decode.exe"),
    os.path.join(os.path.dirname(ROOT), "tools", "libg7221", "decode.exe"),
    os.path.join(HERE, "decode.exe"),
]


def find_decoder(override=None):
    if override:
        return override if os.path.exists(override) else None
    for p in DECODER_CANDIDATES:
        if os.path.exists(p):
            return p
    return None

# 单条通知 20 字节，去掉前 4 字节头后剩 16 字节载荷；3 条 = 48 字节
CHUNK_DATA = 16
CHUNK_HEAD = 4
FRAME_BYTES = 48
CODEC_BYTES = 40
WORDS = 20
SAMPLES_PER_FRAME = 320


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


# ------------------------------------------------------------ 解白化 --
def dewhite(frame48):
    """48 字节帧 -> 40 字节 G.722.1 码流（已做字节交换）"""
    words = list(struct.unpack("<20H", frame48[:CODEC_BYTES]))
    for g in range(4):
        a, b, c = IV_DEC_TABLE[3 * g], IV_DEC_TABLE[3 * g + 1], IV_DEC_TABLE[3 * g + 2]
        oa, ob, oc = words[a], words[b], words[c]
        words[a], words[b], words[c] = oc, oa, ob
    for i in range(WORDS):
        words[i] ^= XOR_KEY
    out = bytearray(CODEC_BYTES)
    for i, w in enumerate(words):
        out[i * 2] = (w >> 8) & 0xFF      # 高低字节对调
        out[i * 2 + 1] = w & 0xFF
    return bytes(out)


# ------------------------------------------------------------- 帧装配 --
class Framer:
    """按 idx 0/1/2 把 20 字节通知拼成 48 字节帧"""

    def __init__(self):
        self.slots = {}
        self.frames = 0
        self.bad = 0
        self.last_seq = None
        self.gap = 0
        self.on_frame = None
        self.first_seq = None      # 本次采集见到的第一个 idx=0 的 seq
        self.final_seq = None      # 最后一个 idx=0 的 seq
        self.first_t = None        # 第一帧到达时刻（相对采集起点）

    def reset(self):
        """语音键按下时清空，保证一句一文件，不会把上一句的尾巴带进来"""
        self.slots = {}
        self.frames = 0
        self.bad = 0
        self.last_seq = None
        self.gap = 0
        self.first_seq = None
        self.final_seq = None
        self.first_t = None

    def feed(self, payload):
        if len(payload) < CHUNK_HEAD + CHUNK_DATA:
            self.bad += 1
            return None
        seq = payload[0] | (payload[1] << 8)
        idx = payload[2]
        if idx > 2:
            self.bad += 1
            return None

        # seq 应当是连续的；不连续就记一次缺口（丢了通知）
        if idx == 0:
            if self.first_seq is None:
                self.first_seq = seq
                self.first_t = time.time()
            if self.last_seq is not None and seq != ((self.last_seq + 1) & 0xFFFF):
                self.gap += 1
            self.last_seq = seq
            self.final_seq = seq

        # 必须按 seq 分组拼帧。
        # 旧实现 self.slots 只按 idx 存、不按 seq 分组：一旦丢了某个碎片（我们丢 ~76%，很常见），
        # 就会把上一个 seq 残留的碎片和新 seq 的碎片拼进同一帧，解出来是错乱/别人的声音。
        # dewhite_probe.py 就是按 seq 分组的，所以它出的 cand_current.wav 才是昊哥本人的声音。
        if len(self.slots) > 200:
            self.slots.clear()   # 丢包严重时会有大量拼不齐的 seq，防堆积
        s = self.slots.setdefault(seq, {})
        s[idx] = payload[CHUNK_HEAD:CHUNK_HEAD + CHUNK_DATA]
        if len(s) < 3:
            return None
        frame = s.get(0, b"") + s.get(1, b"") + s.get(2, b"")
        del self.slots[seq]
        self.frames += 1
        if self.on_frame:
            self.on_frame(frame)
        return frame


# --------------------------------------------------------------- 串口 --
def list_ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        print("pyserial 未安装：python -m pip install pyserial -i https://mirrors.aliyun.com/pypi/simple/")
        return
    print("%-8s %-10s %s" % ("PORT", "VID:PID", "描述"))
    for p in lp.comports():
        vidpid = ""
        if p.vid is not None:
            vidpid = "%04X:%04X" % (p.vid, p.pid)
        print("%-8s %-10s %s" % (p.device, vidpid, p.description))


def beep(kind="ready"):
    """用 PC 蜂鸣器给昊哥发信号，省得盯着串口看。

    ready   = 三声上行短音：链路就绪，现在可以按语音键
    pressed = 一声高音：已检测到按下
    done    = 一声低长音：采集结束，wav 已生成
    """
    try:
        import winsound
    except Exception:
        return
    try:
        if kind == "ready":
            for f in (660, 880, 1180):
                winsound.Beep(f, 180)
        elif kind == "pressed":
            winsound.Beep(1200, 120)
        elif kind == "done":
            winsound.Beep(440, 600)
        elif kind == "release":
            # 两声同高中音：按住时长已到，可以松手了
            winsound.Beep(990, 150)
            time.sleep(0.12)
            winsound.Beep(990, 150)
    except Exception:
        pass


def wait_connected(ser, timeout=25.0):
    """轮询 STAT 等到 conn=1，并返回实际生效的连接间隔 itvl。

    必须等：刚开串口板子会复位重连，重连初期是 itvl=8（实测只有 1 秒级音频），
    遥控器随后才发起连接参数更新到 itvl=32（实测 3 秒级、覆盖率 ~100%）。
    不等就按键 = 踩在 8 上，白白少拿 2/3 的音频。
    """
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


def capture(port, secs, out_base, baud=115200, quiet=False, auto=False, decoder=None):
    """auto=True 时：收到 # A END 后再等 idle_gap 秒没新帧就收工，不必等满 secs。
    这样可以"按一下、说一句、松手"，脚本自己判断语音结束并生成 wav。"""
    import serial

    raw_frames = []
    framer = Framer()
    framer.on_frame = lambda f: raw_frames.append(f)

    t_begin = time.time()
    t_end = t_begin + secs
    n_a_lines = 0
    line_buf = b""
    starts = []
    ends = []
    last_voice_ms = None      # 最后一次收到音频通知的时间
    voice_seen = False        # 是否收到过语音键边界事件（固件支持才有）
    voice_off_t = None        # 语音键抬起的时刻
    t_voice_on = None
    # 遥控器的语音是分段的：说话中间停顿时它会把流切成几段，
    # 固件 300ms 没新帧就打 # A END。原来 1.5 秒就收工，只拿到第一段。
    # 放宽到 2.5 秒，把后面几段一起收进来（Framer 本来就是顺序拼接的）。
    idle_gap = 2.5
    # 抬起后还要等一小会儿，把在途的残留帧收干净再解码
    tail_gap = 0.35
    # 关键修正（2026-10-31 实测）：遥控器是"边录边缓冲、松手后才把音频倒出来"。
    # 原来一松手只等 0.35 秒就收工，拿到的是按下期间的涓流
    # （实测按住 3.55 秒只出 34 帧=0.68 秒），听起来就是一声噪音。
    # 改成：松手后继续收，直到"连续 post_idle 秒没有新帧"才算倒完。
    post_idle = 2.5        # 松手后静默这么久就认为音频倒完了
    post_max = 12.0        # 兜底：松手后最多再收这么久，防止遥控不发而卡死
    # 按住时长提示：实测按住 ≤3 秒覆盖率 ~100%，按到 5 秒只剩 ~50%
    # （遥控器侧倒流有自己的节奏/上限，按太久它不全给）。
    # 按住到点响两声提示松手，把每次采集都落在能拿全的窗口里。
    hold_hint = 3.0
    hold_hinted = False

    # Windows 上刚关闭的句柄不会立刻释放，连续开同一串口常报"拒绝访问"。
    # 这里重试几次，省得每次都要人工等一下再跑。
    ser = None
    last_err = None
    for _ in range(10):
        try:
            ser = serial.Serial(port, baud, timeout=0.2)
            break
        except serial.SerialException as e:
            last_err = e
            time.sleep(0.5)
    if ser is None:
        print("# 打不开 %s：%s" % (port, last_err))
        return None
    # 关键：确保固件在吐音频帧、且订阅了 0xFC 语音通道。
    # 上次若跑过 batt_then_voice（它会发 AUDIO 0 关打印），这里必须把状态拉回来，
    # 否则固件当前是 AUDIO 0，本脚本会一条 A 帧都收不到、直接抓空。
    ser.write(b"AUDIO 1\n")
    ser.write(b"AUDIOSUB 1\n")
    time.sleep(0.15)
    print("# 已打开 %s。已发送 AUDIO 1 / AUDIOSUB 1（拉起音频透传）。" % port)

    # 关键（2026-10-31 实测）：开串口会让板子复位重连，重连初期是 itvl=8，
    # 只有 1 秒级音频；遥控器随后才把连接参数更新到 itvl=32（3 秒级、~100% 覆盖）。
    # 以前这里直接提示按键，等于每次都踩在 8 上 —— 现在先等连上、等参数更新落地。
    print("# 等待遥控器连上、连接参数稳定（不要提前按）...")
    itvl = wait_connected(ser)
    if itvl is None:
        print("# !! 25 秒没等到 conn=1，遥控可能没连上；仍继续，但可能抓空。")
    else:
        print("# 遥控已连上，生效连接间隔 itvl=%d（%.2fms）" % (itvl, itvl * 1.25))
        if itvl < 32:
            print("# !! itvl=%d 偏低（实测 32 才能拿全语音），建议重新拔插/重连后再采。" % itvl)
        time.sleep(2.0)   # 给遥控器发起的参数更新留落地时间
        itvl2 = wait_connected(ser, timeout=6.0)
        if itvl2 and itvl2 != itvl:
            print("# 连接参数已更新：itvl %d -> %d（%.2fms）" % (itvl, itvl2, itvl2 * 1.25))
            itvl = itvl2

    print("# 现在按住语音键说一句话，说完松手即可，脚本自动出 wav。")
    # 蜂鸣提示：不用盯着串口，听到三声上行短音就可以按了
    beep("ready")
    sys.stdout.flush()
    if not auto:
        print("# 采集上限 %.0f 秒。" % secs)

    try:
        while time.time() < t_end:
            # 关键：不要 read(4096)。数据是一帧一帧到的，永远凑不满 4096，
            # 只能等 timeout（0.2s）才返回，等于每秒只读 5 次，白白让驱动缓冲积压。
            # 改成"有多少读多少"，没有就阻塞等 1 字节。
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
                        # 语音键按下：清空，这一句从零开始收
                        voice_seen = True
                        voice_off_t = None
                        t_voice_on = now
                        framer.reset()
                        del raw_frames[:]
                        print("=== 语音键按下，开始录音 ===")
                        beep("pressed")
                    elif line.startswith(b"# VOICE 0"):
                        # 语音键抬起：标记结束，再等 tail_gap 收残留
                        voice_seen = True
                        voice_off_t = now
                        dur = (now - t_voice_on) if t_voice_on else 0.0
                        print("=== 语音键抬起（按住 %.2f 秒，已收 %d 帧），等残留 ===" %
                              (dur, framer.frames))
                    elif line.startswith(b"# A START"):
                        starts.append(framer.frames)
                        print("=== 检测到语音流开始 (%s) ===" %
                              line.decode("utf-8", "replace"))
                    elif line.startswith(b"# OK LINK"):
                        # 真实生效的链路参数（itvl/lat/mtu），和覆盖率一起看
                        print("  # " + line.decode("utf-8", "replace"))
                    elif line.startswith(b"# A END"):
                        ends.append(framer.frames)
                        print("=== 语音流暂停，累计 %d 帧 ===" % framer.frames)
                    elif not quiet:
                        s = line.decode("utf-8", "replace")
                        if ("DISCONNECT" in s or "ERR" in s or "CONNUPD" in s
                                or "INITIAL" in s or "CONNREQ" in s or "MTU" in s):
                            print("  # " + s)
            # 按住到点：响两声提示松手（只在还没松手时提示一次）
            if (t_voice_on is not None and voice_off_t is None
                    and not hold_hinted and (now - t_voice_on) > hold_hint):
                hold_hinted = True
                beep("release")
                print("# 已按住 %.1f 秒，可以松手了" % hold_hint)
                sys.stdout.flush()
            # 松手后不能立刻收工：遥控器是松手后才把缓冲的音频倒出来的。
            # 要一直收到"连续 post_idle 秒没有新帧"为止（或撞上 post_max 上限）。
            if voice_off_t is not None:
                quiet = (last_voice_ms is None) or \
                        ((now - last_voice_ms) > post_idle)
                if quiet or ((now - voice_off_t) > post_max):
                    print("# 语音键已抬起，之后静默 %.1f 秒（或达上限 %.0f 秒），残留帧收取完毕"
                          % (post_idle, post_max))
                    break
            # 没有按键事件时（老固件），退回静默判定
            if voice_off_t is None and (not voice_seen) and auto \
                    and last_voice_ms is not None \
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
        print("# 没收到任何完整帧。检查：遥控器是否已连接（发 STAT 看 conn=1）、"
              "订阅是否打开（AUDIOSUB 1）、串口是否被别的程序占着。")
        return None

    # 语音段
    if starts and ends:
        print("# 语音段 %d 个：" % min(len(starts), len(ends)))
        for i, (s, e) in enumerate(zip(starts, ends)):
            print("   第%d段 帧 %d..%d = %d 帧 = %.2f 秒" %
                  (i + 1, s, e, e - s, (e - s) * 0.02))

    # 落盘
    codec = b"".join(dewhite(f) for f in raw_frames)
    raw_path = out_base + ".g7221"
    with open(raw_path, "wb") as fh:
        fh.write(codec)
    print("# 码流已写 %s（%d 字节）" % (raw_path, len(codec)))

    wav_path = out_base + ".wav"
    exe = find_decoder(decoder)
    if exe:
        r = subprocess.run([exe, raw_path, wav_path, "16000"],
                           capture_output=True, text=True)
        if os.path.exists(wav_path):
            print("# 解码完成 %s" % wav_path)
            print("# 时长 %.2f 秒" % (len(codec) / 40 * 0.02))
            beep("done")   # 一声低长音：可以来听了
            return wav_path
        print("# 解码失败：%s %s" % (r.stdout[-300:], r.stderr[-300:]))
    else:
        print("# 找不到解码器，试过这些位置：")
        for p in DECODER_CANDIDATES:
            print("#   %s" % p)
        print("# 用 --decoder <path> 指定")
    return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("mode", choices=["list", "cap"])
    ap.add_argument("port", nargs="?")
    ap.add_argument("--secs", type=float, default=60, help="采集上限（秒）")
    ap.add_argument("--out", default=None, help="输出文件名前缀")
    ap.add_argument("--quiet", action="store_true", help="不打印固件日志")
    ap.add_argument("--auto", action="store_true",
                    help="说过话后静默 1.5 秒自动收尾（推荐）")
    ap.add_argument("--decoder", default=None, help="decode.exe 路径")
    a = ap.parse_args()

    if a.mode == "list":
        list_ports()
        return 0
    if not a.port:
        print("需要串口名，例如 COM11")
        return 2
    out = a.out or os.path.join(HERE, "take_" + stamp())
    wav = capture(a.port, a.secs, out, quiet=a.quiet, auto=a.auto, decoder=a.decoder)
    return 0 if wav else 1


if __name__ == "__main__":
    sys.exit(main())
