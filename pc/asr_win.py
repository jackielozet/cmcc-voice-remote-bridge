# -*- coding: utf-8 -*-
"""CMCC 语音遥控器 —— 本地离线语音转文字（ASR）

链路：
    串口 "A <HEX>" 行
      -> 按 seq 拼帧（3 条通知 = 1 帧 48 字节）
      -> dewhite 还原成 40 字节 G.722.1 / Siren7 码流
      -> decode.exe 解成 16k 单声道 PCM
      -> sherpa-onnx Paraformer 识别成文字
      -> 剪贴板 + Ctrl+V 注入当前光标

设计要点：
1. 全程离线，不联网，不装驱动，不占麦克风设备。
2. 语音键仍由固件当普通键发出（默认 Win+H）。本模块装全局键盘钩子，
   勾了"语音键录音并直接输出文字"就把该组合吞掉，原绑定的按键功能
   被屏蔽，系统不再弹自己的语音输入；同时把这一路的起止当成本地录音
   的起止信号。取消勾选立刻恢复原绑定。
3. 识别在后台线程做，绝不阻塞 GUI。
"""
import ctypes
import os
import queue
import struct
import subprocess
import sys
import tempfile
import threading
import time
import wave
from ctypes import wintypes

# --------------------------------------------------------------- 协议常量 --
# 单条通知 20 字节，去掉前 4 字节头剩 16 字节载荷；3 条 = 48 字节
CHUNK_DATA = 16
CHUNK_HEAD = 4
FRAME_BYTES = 48
CODEC_BYTES = 40
WORDS = 20
SAMPLES_PER_FRAME = 320        # 20ms @ 16kHz
FRAME_MS = 20

IV_DEC_TABLE = [4, 16, 9, 8, 3, 11, 2, 6, 18, 7, 13, 17]
XOR_KEY = 0x0416

# 语句结束判定：这么多毫秒没有新帧就认为说完了。
# 不能太短：松手后设备还在吐尾巴帧（BLE 排队 + 固件缓冲），切早了这些尾巴
# 会被算到下一句开头，听起来就是"上一句最后一个字跑到下一句去了"。
SILENCE_MS = 500
# 最长一句，防止一直说话把缓冲撑爆
MAX_SECONDS = 20.0
# 尾部多留一点，末字（比如"天气"的"气"）要收全，切太紧会吞掉最后一个字
TAIL_KEEP_SEC = 0.40


def dewhite(frame48):
    """48 字节帧 -> 40 字节 G.722.1 码流（字节交换 + 去白化）"""
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


class Framer:
    """按 seq 分组，把 idx 0/1/2 三条通知拼成一帧。

    slots 必须按 seq 分组：一旦丢碎片（常态），只按 idx 存会把不同 seq 的
    碎片拼到一起，解出来是错乱的声音。
    """

    def __init__(self):
        self.slots = {}
        self.frames = 0
        self.bad = 0
        self.last_seq = None
        self.gap = 0

    def reset(self):
        self.slots = {}
        self.frames = 0
        self.bad = 0
        self.last_seq = None
        self.gap = 0

    def feed(self, payload):
        """喂一条通知载荷，拼齐时返回 48 字节帧，否则 None。"""
        if len(payload) < CHUNK_HEAD + CHUNK_DATA:
            self.bad += 1
            return None
        seq = payload[0] | (payload[1] << 8)
        idx = payload[2]
        if idx > 2:
            self.bad += 1
            return None
        if idx == 0:
            if self.last_seq is not None and seq != ((self.last_seq + 1) & 0xFFFF):
                self.gap += 1
            self.last_seq = seq
        if len(self.slots) > 200:
            self.slots.clear()
        s = self.slots.setdefault(seq, {})
        s[idx] = payload[CHUNK_HEAD:CHUNK_HEAD + CHUNK_DATA]
        if len(s) < 3:
            return None
        frame = s.get(0, b"") + s.get(1, b"") + s.get(2, b"")
        del self.slots[seq]
        self.frames += 1
        return frame


# ------------------------------------------------------------------ 解码 --
HERE = os.path.dirname(os.path.abspath(__file__))


def app_dir():
    """程序所在目录：打包后是 exe 目录，开发时是脚本目录。"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return HERE


def search_roots():
    """资源搜索根目录，按优先级排列。

    打包后 models/ 和 decode.exe 跟 exe 放一起（也可被 PyInstaller 收进
    _MEIPASS）；开发时再兜底找仓库里的 asr_proto/models。
    """
    roots = [app_dir()]
    mp = getattr(sys, "_MEIPASS", None)
    if mp and mp not in roots:
        roots.append(mp)
    if HERE not in roots:
        roots.append(HERE)
    proj = os.path.dirname(os.path.dirname(HERE))
    if proj not in roots:
        roots.append(proj)
    # 开发时模型放在仓库根的 asr_proto/models 下
    up = os.path.dirname(proj)
    if up not in roots:
        roots.append(up)
    return roots


DECODER_REL = [
    ("decode.exe",),
    ("tools", "libg7221", "decode.exe"),
    ("libg7221", "decode.exe"),
]

MODEL_REL = [
    # 大模型优先（正确率明显高），没有就退回 small
    os.path.join("models", "sherpa-onnx-paraformer-zh-2023-09-14"),
    os.path.join("asr_proto", "models", "sherpa-onnx-paraformer-zh-2023-09-14"),
    os.path.join("sherpa-onnx-paraformer-zh-2023-09-14"),
    os.path.join("models", "sherpa-onnx-paraformer-zh-small-2024-03-09"),
    os.path.join("asr_proto", "models",
                 "sherpa-onnx-paraformer-zh-small-2024-03-09"),
    os.path.join("sherpa-onnx-paraformer-zh-small-2024-03-09"),
]


def find_decoder(override=None):
    if override and os.path.exists(override):
        return override
    for root in search_roots():
        for parts in DECODER_REL:
            p = os.path.join(root, *parts)
            if os.path.exists(p):
                return p
    return None


def decode_codec(codec_bytes, exe=None):
    """40N 字节 G.722.1 码流 -> int16 PCM bytes（16kHz 单声道）。

    走 decode.exe（已验证 SNR 29.9dB）。一句话调一次，开销约 30~60ms。
    """
    exe = exe or find_decoder()
    if not exe:
        raise FileNotFoundError("找不到 decode.exe（tools/libg7221/）")
    if not codec_bytes:
        return b""
    tmpdir = tempfile.mkdtemp(prefix="cmcc_asr_")
    raw = os.path.join(tmpdir, "in.g7221")
    wav = os.path.join(tmpdir, "out.wav")
    try:
        with open(raw, "wb") as f:
            f.write(codec_bytes)
        r = subprocess.run([exe, raw, wav, "16000"],
                           capture_output=True, text=True, timeout=20)
        if not os.path.exists(wav):
            raise RuntimeError("解码失败：%s" % (r.stderr or r.stdout)[:200])
        with wave.open(wav, "rb") as w:
            return w.readframes(w.getnframes())
    finally:
        for p in (raw, wav):
            try:
                os.remove(p)
            except OSError:
                pass
        try:
            os.rmdir(tmpdir)
        except OSError:
            pass


def pcm_to_float(pcm_bytes):
    """int16 bytes -> float32 numpy 数组（[-1,1]）"""
    import numpy as np
    a = np.frombuffer(pcm_bytes, dtype=np.int16).astype("float32")
    return a / 32768.0


# ------------------------------------------------------------ 音频预处理 --
# 遥控器每次开麦都会先来一下"尖峰 + 120ms 内衰完"的爆音；G.722.1 又是
# 16kbps 的强压缩，静音段送进去模型会瞎猜（实测吐出"喂喂喂""五"）。
# 所以送识别之前统一做：抠爆音 -> 掐首尾静音 -> 峰值归一化。

MIN_SPEECH_SEC = 0.25      # 处理后短于这个就当作没说话
HEAD_KEEP_SEC = 0.06       # 语音起点前留一点，别把首字声母切掉
# 开麦头噪声：实测每次按键开头都有 250~350ms 的"底噪 + 一下爆音"，
# 送进模型会被听成"喂/五/七"，必须整段剁掉（用户不可能在 300ms 内开口）。
HEAD_DROP_SEC = 0.30


def _frame_rms(a, sr, ms=20):
    """按 20ms 分帧算 RMS，返回 (数组, 每帧样本数)。"""
    import numpy as np
    hop = max(1, int(sr * ms / 1000))
    n = len(a) // hop
    if n <= 0:
        return np.zeros(0, dtype="float32"), hop
    x = a[:n * hop].reshape(n, hop)
    return np.sqrt((x ** 2).mean(axis=1)).astype("float32"), hop


def strip_head_click(a, sr):
    """剁掉开头那段噪声/爆音。返回 (新音频, 说明)。

    两步走：
      1) 先按固定时长剁头（HEAD_DROP_SEC）——设备每次开麦都有这一段，
         留着就会被识别成"喂/五/七"这种凭空多出来的字；
      2) 再看剩下开头有没有残余尖峰，有就把它置零（不动时间轴）。
    """
    import numpy as np
    if len(a) > int(sr * 0.7):
        cut = int(sr * HEAD_DROP_SEC)
        note = "剁头 %dms" % (HEAD_DROP_SEC * 1000)
    else:
        # 短句多剁一点比例，但别把整句吃光
        cut = min(int(len(a) * 0.25), int(sr * HEAD_DROP_SEC))
        note = "剁头 %dms" % (cut * 1000 / sr)
    out = a[cut:]
    rms, hop = _frame_rms(out, sr)
    if len(rms) >= 10:
        med = float(np.median(rms))
        pk = int(rms[:30].argmax())
        peak = float(rms[pk])
        if peak > 0.05 and peak > 6 * max(med, 1e-6):
            thr = max(3 * med, 0.02)
            i0, i1 = pk, pk
            while i0 > 0 and rms[i0 - 1] > thr:
                i0 -= 1
            while i1 < len(rms) - 1 and rms[i1 + 1] > thr and i1 - pk < 15:
                i1 += 1
            out = out.copy()
            out[i0 * hop:(i1 + 1) * hop] = 0.0
            note += "+置零爆音"
    return out, note


def vad_trim(a, sr):
    """掐掉首尾静音，让模型一上来就听到人声。返回 (新音频, 说明)。"""
    import numpy as np
    rms, hop = _frame_rms(a, sr)
    if len(rms) < 8:
        return a, "太短"
    lvl = float(np.percentile(rms, 90))          # 用高分位当"说话响度"
    thr = max(0.004, 0.12 * lvl)
    above = rms >= thr
    onset = None
    for i in range(len(rms) - 10):
        if int(above[i:i + 10].sum()) >= 5:      # 200ms 内有一半以上像人声
            onset = i
            break
    if onset is None:
        return None, "全是静音"
    last = len(rms) - 1
    while last > 0 and not above[last]:
        last -= 1
    s = max(0, onset * hop - int(sr * HEAD_KEEP_SEC))
    e = min(len(a), (last + 1) * hop + int(sr * TAIL_KEEP_SEC))
    return a[s:e], "裁到 %.2f~%.2fs" % (s / float(sr), e / float(sr))


def normalize(a, target=0.05, cap=30.0):
    """按 RMS 归一，不用峰值。

    峰值归一会被开头那下爆音带偏（爆音比人声大 20 倍），结果人声还是很小，
    模型就开始瞎猜。RMS 归一才是按"说话有多响"来定的。
    """
    import numpy as np
    if not len(a):
        return a, 1.0
    r = float(np.sqrt((a ** 2).mean()))
    if r < 1e-6:
        return a, 1.0
    g = min(target / r, cap)
    out = a * g
    peak = float(np.abs(out).max())
    if peak > 0.99:                              # 放大后别削波
        out *= 0.99 / peak
        g *= 0.99 / peak
    return out, g


def prepare_audio(a, sr=16000):
    """送识别前的统一处理。返回 (samples 或 None, 说明)。"""
    if a is None or not len(a):
        return None, "没有音频"
    a = a.astype("float32", copy=True)
    a -= float(a.mean())                     # 去直流，爆音多半带直流台阶
    a, n1 = strip_head_click(a, sr)
    a, n2 = vad_trim(a, sr)
    if a is None:
        return None, n2
    if len(a) < int(sr * MIN_SPEECH_SEC):
        return None, "有效人声只有 %.2fs" % (len(a) / float(sr))
    a, g = normalize(a)
    return a, "%s，%s，增益 x%.1f" % (n1, n2, g)


def debug_log(msg):
    """每次识别写一行到 asr_debug.log（排查用；超过 256KB 自动重建）。"""
    try:
        p = os.path.join(app_dir(), "asr_debug.log")
        if os.path.exists(p) and os.path.getsize(p) > 256 * 1024:
            os.remove(p)
        with open(p, "a", encoding="utf-8") as f:
            f.write("[%s] %s\n" % (time.strftime("%m-%d %H:%M:%S"), msg))
    except Exception:
        pass


# ------------------------------------------------------------------- ASR --
def default_model_dir():
    """按优先级找 paraformer 模型目录。"""
    for root in search_roots():
        for rel in MODEL_REL:
            p = os.path.join(root, rel)
            if os.path.exists(os.path.join(p, "tokens.txt")):
                return p
    return None


class AsrTimeout(RuntimeError):
    """识别超时（引擎卡死或锁被占太久）。"""


class _RecSlot:
    """一次加载出来的识别器 + 它自己的锁。

    为什么要包一层：onnxruntime 的 decode_stream() 是纯 C 调用，Python 这边
    既杀不掉也打断不了。一旦它卡住，那条线程会一直攥着锁不放。
    把「识别器 + 锁」装成整体，卡死后直接换一个新槽，卡住的旧线程爱攥多久
    攥多久，反正没人再用它 —— 后面的识别不会被堵死。
    """

    __slots__ = ("rec", "lock")

    def __init__(self, rec):
        self.rec = rec
        self.lock = threading.Lock()


class AsrEngine:
    """sherpa-onnx 离线识别器，懒加载 + 线程安全 + 可整体重置。"""

    def __init__(self, model_dir=None, threads=2):
        self.model_dir = model_dir or default_model_dir()
        self.threads = threads
        self._slot = None
        self._load_lock = threading.Lock()
        self.load_ms = 0
        self.reset_count = 0      # 被强制重置过几次（诊断用）

    @property
    def ready(self):
        return self._slot is not None

    def available(self):
        return self.model_dir is not None

    def load(self):
        slot = self._slot
        if slot is not None:
            return slot.rec
        with self._load_lock:
            if self._slot is not None:
                return self._slot.rec
            if not self.model_dir:
                raise FileNotFoundError("找不到 ASR 模型目录")
            import sherpa_onnx
            model = None
            for name in ("model.int8.onnx", "model.onnx"):
                p = os.path.join(self.model_dir, name)
                if os.path.exists(p):
                    model = p
                    break
            if not model:
                raise FileNotFoundError("模型目录里没有 .onnx")
            t0 = time.time()
            rec = sherpa_onnx.OfflineRecognizer.from_paraformer(
                paraformer=model,
                tokens=os.path.join(self.model_dir, "tokens.txt"),
                num_threads=self.threads,
                sample_rate=16000,
                feature_dim=80,
                decoding_method="greedy_search",
                provider="cpu",
            )
            self.load_ms = int((time.time() - t0) * 1000)
            self._slot = _RecSlot(rec)
            return rec

    def reset(self):
        """丢掉当前识别器。卡死的旧线程还攥着旧槽，不影响新建的这一份。"""
        with self._load_lock:
            self._slot = None
        self.reset_count += 1

    def transcribe(self, samples_float, rate=16000, waiter=None, lock_timeout=8.0):
        """识别。lock_timeout 只防"锁被占死"，真正的识别时长由调用方看门狗兜。

        waiter：可选，一个 threading.Event。设置后每 50ms 检查一次，
        被叫停就抛 AsrTimeout —— 用于 UI 的 15 秒熔断，让线程别再干等。
        """
        rec = self.load()
        slot = self._slot
        if slot is None:                     # 刚被 reset 掉，这份 rec 已作废
            raise AsrTimeout("识别器已重置，这一句丢弃")
        if samples_float is None or len(samples_float) < int(rate * 0.2):
            return ""            # 太短会触发 onnxruntime 的 reshape 报错
        if waiter is not None and waiter.is_set():
            raise AsrTimeout("识别超时，结果已丢弃")
        if not slot.lock.acquire(timeout=lock_timeout):
            raise AsrTimeout("识别器忙（上一句还卡着）")
        try:
            s = rec.create_stream()
            s.accept_waveform(rate, samples_float)
            rec.decode_stream(s)
            if waiter is not None and waiter.is_set():
                # 已经超时被判弃了，结果就不要了
                raise AsrTimeout("识别超时，结果已丢弃")
            return (s.result.text or "").strip()
        finally:
            try:
                slot.lock.release()
            except Exception:
                pass



# ------------------------------------------------------------------ 注入 --
_u32 = ctypes.WinDLL("user32", use_last_error=True)
_k32 = ctypes.WinDLL("kernel32", use_last_error=True)

CF_UNICODETEXT = 13
GMEM_MOVEABLE = 0x0002

_u32.OpenClipboard.argtypes = [wintypes.HWND]
_u32.OpenClipboard.restype = wintypes.BOOL
_u32.SetClipboardData.argtypes = [wintypes.UINT, wintypes.HANDLE]
_u32.SetClipboardData.restype = wintypes.HANDLE
_u32.GetClipboardData.argtypes = [wintypes.UINT]
_u32.GetClipboardData.restype = wintypes.HANDLE
_k32.GlobalAlloc.argtypes = [wintypes.UINT, ctypes.c_size_t]
_k32.GlobalAlloc.restype = wintypes.HGLOBAL
_k32.GlobalLock.argtypes = [wintypes.HGLOBAL]
_k32.GlobalLock.restype = ctypes.c_void_p
_k32.GlobalUnlock.argtypes = [wintypes.HGLOBAL]
_k32.GlobalFree.argtypes = [wintypes.HGLOBAL]


def clipboard_get_text():
    """读当前剪贴板文本；不是文本就返回 None。"""
    if not _u32.OpenClipboard(None):
        return None
    try:
        h = _u32.GetClipboardData(CF_UNICODETEXT)
        if not h:
            return None
        p = _k32.GlobalLock(h)
        if not p:
            return None
        try:
            return ctypes.wstring_at(p)
        finally:
            _k32.GlobalUnlock(h)
    finally:
        _u32.CloseClipboard()


def clipboard_set_text(text):
    """写剪贴板文本，成功返回 True。"""
    if not _u32.OpenClipboard(None):
        return False
    try:
        _u32.EmptyClipboard()
        data = text.encode("utf-16-le") + b"\x00\x00"
        h = _k32.GlobalAlloc(GMEM_MOVEABLE, len(data))
        if not h:
            return False
        p = _k32.GlobalLock(h)
        if not p:
            return False
        try:
            ctypes.memmove(p, data, len(data))
        finally:
            _k32.GlobalUnlock(h)
        if not _u32.SetClipboardData(CF_UNICODETEXT, h):
            _k32.GlobalFree(h)
            return False
        return True
    finally:
        _u32.CloseClipboard()


ULONG_PTR = ctypes.c_size_t if sys.maxsize > 2 ** 32 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wintypes.WORD), ("wScan", wintypes.WORD),
                ("dwFlags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ULONG_PTR)]


class MOUSEINPUT(ctypes.Structure):
    _fields_ = [("dx", wintypes.LONG), ("dy", wintypes.LONG),
                ("mouseData", wintypes.DWORD), ("dwFlags", wintypes.DWORD),
                ("time", wintypes.DWORD), ("dwExtraInfo", ULONG_PTR)]


class HARDWAREINPUT(ctypes.Structure):
    _fields_ = [("uMsg", wintypes.DWORD), ("wParamL", wintypes.WORD),
                ("wParamH", wintypes.WORD)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("mi", MOUSEINPUT), ("hi", HARDWAREINPUT)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wintypes.DWORD), ("u", _INPUTunion)]


INPUT_KEYBOARD = 1
KEYEVENTF_KEYUP = 0x0002
VK_CONTROL, VK_V = 0x11, 0x56

_u32.SendInput.argtypes = [wintypes.UINT, ctypes.POINTER(INPUT), ctypes.c_int]
_u32.SendInput.restype = wintypes.UINT


def _mk_key(vk, up=False):
    inp = INPUT()
    inp.type = INPUT_KEYBOARD
    inp.u.ki.wVk = vk
    inp.u.ki.wScan = 0
    inp.u.ki.dwFlags = KEYEVENTF_KEYUP if up else 0
    inp.u.ki.time = 0
    inp.u.ki.dwExtraInfo = 0
    return inp


def send_ctrl_v():
    """模拟 Ctrl+V。"""
    seq = [
        _mk_key(VK_CONTROL), _mk_key(VK_V),
        _mk_key(VK_V, True), _mk_key(VK_CONTROL, True),
    ]
    arr = (INPUT * len(seq))(*seq)
    return _u32.SendInput(len(seq), arr, ctypes.sizeof(INPUT)) == len(seq)


def inject_text(text, restore_delay=0.35):
    """把文本灌到当前光标处：剪贴板 + Ctrl+V，然后把原剪贴板恢复。"""
    text = (text or "").strip()
    if not text:
        return False
    old = clipboard_get_text()
    if not clipboard_set_text(text):
        return False
    time.sleep(0.03)                 # 给剪贴板一点时间落地
    ok = send_ctrl_v()
    if old is not None:
        def _restore():
            time.sleep(restore_delay)
            clipboard_set_text(old)
        threading.Thread(target=_restore, daemon=True).start()
    return ok


# ------------------------------------------------------------------ 钩子 --
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105

VK_LWIN, VK_RWIN = 0x5B, 0x5C
VK_SHIFT, VK_CONTROL, VK_MENU = 0x10, 0x11, 0x12
VK_LCONTROL, VK_RCONTROL = 0xA2, 0xA3
VK_LSHIFT, VK_RSHIFT = 0xA0, 0xA1
VK_LMENU, VK_RMENU = 0xA4, 0xA5
VK_CAPITAL = 0x14

MOD_VK = {VK_SHIFT, VK_CONTROL, VK_MENU, VK_LWIN, VK_RWIN,
          VK_LCONTROL, VK_RCONTROL, VK_LSHIFT, VK_RSHIFT,
          VK_LMENU, VK_RMENU}


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_size_t)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                               ctypes.c_size_t, ctypes.c_size_t)


class VoiceKeyHook:
    """全局键盘钩子：把语音键映射的主键（+修饰键）吞掉，转成本地回调。

    为什么不靠固件：语音键现在被固件当普通键发（默认 Win+H）。要"屏蔽原有
    功能"，在 PC 侧吞掉即可，不必重烧固件。

    代价：勾选期间涉及的修饰键（通常是 Win）会被一起吞掉，单独按 Win 不再
    弹开始菜单。取消勾选立刻恢复。
    """

    def __init__(self, target_vk, mods, on_down, on_up):
        self.target_vk = target_vk
        self.mods = set(mods)                 # 需要按住的修饰键（任一即算）
        self.on_down = on_down
        self.on_up = on_up
        self._pressed = set()
        self._hook = None
        self._proc = None
        self._tid = 0
        self._err = 0
        self._running = False
        self.down_count = 0
        self.up_count = 0
        self._u32 = ctypes.WinDLL("user32", use_last_error=True)
        u = self._u32
        u.SetWindowsHookExW.restype = ctypes.c_void_p
        u.SetWindowsHookExW.argtypes = [ctypes.c_int, _HOOKPROC, ctypes.c_void_p,
                                        wintypes.DWORD]
        u.UnhookWindowsHookEx.argtypes = [ctypes.c_void_p]
        u.CallNextHookEx.restype = ctypes.c_ssize_t
        u.CallNextHookEx.argtypes = [ctypes.c_void_p, ctypes.c_int,
                                     ctypes.c_size_t, ctypes.c_size_t]
        u.GetMessageW.argtypes = [ctypes.POINTER(wintypes.MSG), ctypes.c_void_p,
                                  wintypes.UINT, wintypes.UINT]
        u.PostThreadMessageW.argtypes = [wintypes.DWORD, wintypes.UINT,
                                         wintypes.WPARAM, wintypes.LPARAM]

    # --- 钩子回调 ---
    def _proc_func(self, nCode, wParam, lParam):
        try:
            if nCode == 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                vk = int(kb.vkCode)
                down = wParam in (WM_KEYDOWN, WM_SYSKEYDOWN)
                swallow = False

                if vk in MOD_VK:
                    # 语音键要用的修饰键：一律吞，避免系统和本程序状态错位
                    if self._need_mod(vk):
                        swallow = True
                        if down:
                            self._pressed.add(vk)
                        else:
                            self._pressed.discard(vk)
                elif vk == self.target_vk:
                    if down:
                        if self._mods_ok():
                            swallow = True
                            self._armed = True
                            self.down_count += 1
                            if self.on_down:
                                self.on_down()
                    else:
                        if self._armed:
                            swallow = True
                            self._armed = False
                            self.up_count += 1
                            if self.on_up:
                                self.on_up()
                if swallow:
                    return 1
            return self._u32.CallNextHookEx(ctypes.c_void_p(self._hook), nCode,
                                            wParam, lParam)
        except Exception:
            # 钩子里绝不能抛，出事就放行，宁可漏拦也别卡键盘
            try:
                return self._u32.CallNextHookEx(ctypes.c_void_p(self._hook), nCode,
                                                wParam, lParam)
            except Exception:
                return 0

    _armed = False

    def _need_mod(self, vk):
        if vk in self.mods:
            return True
        # L/R 变体：mods 里写通用键时也要认
        pair = {VK_SHIFT: (VK_LSHIFT, VK_RSHIFT),
                VK_CONTROL: (VK_LCONTROL, VK_RCONTROL),
                VK_MENU: (VK_LMENU, VK_RMENU),
                VK_LWIN: (VK_LWIN,), VK_RWIN: (VK_RWIN,)}
        for base, variants in pair.items():
            if base in self.mods and vk in variants:
                return True
            if vk in self.mods and base in variants:
                return True
        return False

    def _mods_ok(self):
        if not self.mods:
            return True
        for m in self.mods:
            if m in self._pressed:
                return True
            for v in {VK_LSHIFT: VK_SHIFT, VK_RSHIFT: VK_SHIFT,
                      VK_LCONTROL: VK_CONTROL, VK_RCONTROL: VK_CONTROL,
                      VK_LMENU: VK_MENU, VK_RMENU: VK_MENU}.get(m, m), m:
                if v in self._pressed:
                    return True
        return False

    def _on_down(self):
        self._armed = True
        if self.on_down:
            self.on_down()

    def _on_up(self):
        if self._armed:
            self._armed = False
            if self.on_up:
                self.on_up()

    # --- 生命周期 ---
    def _worker(self):
        self._tid = _k32.GetCurrentThreadId()
        self._proc = _HOOKPROC(self._proc_func)
        self._hook = self._u32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            self._err = ctypes.get_last_error()
            return
        self._running = True
        msg = wintypes.MSG()
        while self._u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._u32.TranslateMessage(ctypes.byref(msg))
            self._u32.DispatchMessageW(ctypes.byref(msg))
        self._running = False

    def start(self):
        if self.target_vk <= 0:
            return False
        self._pressed.clear()
        self._armed = False
        threading.Thread(target=self._worker, daemon=True).start()
        for _ in range(60):
            if self._hook or self._err:
                break
            time.sleep(0.02)
        return bool(self._hook)

    def stop(self):
        try:
            if self._hook:
                self._u32.UnhookWindowsHookEx(ctypes.c_void_p(self._hook))
        except Exception:
            pass
        self._hook = None
        if self._tid:
            try:
                self._u32.PostThreadMessageW(self._tid, 0x0012, 0, 0)  # WM_QUIT
            except Exception:
                pass
        self._tid = 0
        self._running = False
        self._pressed.clear()
        self._armed = False

    @property
    def running(self):
        return self._running
