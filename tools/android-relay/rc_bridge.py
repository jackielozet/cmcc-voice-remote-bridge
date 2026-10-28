#!/usr/bin/env python3
"""CMCC 语音遥控器 -> Windows 桥接（手机中继版）

链路:  遥控器 --BLE--> 手机 (Android HID host) --USB/ADB--> 本脚本 --> SendInput --> 任意 Windows 程序

为什么这么做:
  遥控器的 HID 描述符里有 3 个 Report Reference 与 Report Map 对不上
  (0xFD / 0xFA 不在表里, 0xFB 类型写反), 微软 mshidumdf 严格校验后拒绝初始化,
  设备管理器里稳定报"代码 10", 而且 0x1812 被 bthleenum.sys 内核独占,
  用户态读不到。绕开办法是让手机当 HID 主机 —— 手机已把它认成标准输入设备,
  `getevent` 免 root 就能把内核层按键事件流交给电脑。

两个必须注意的坑:
  1. getevent 的 stdout 接管道时是全缓冲, 必须用 adb shell -tt 强制 pty;
  2. 遥控器空闲会断链, Android 会销毁并重建 /dev/input/eventN,
     旧的 fd 变孤儿(不报错也收不到数据), 所以要监视节点变化并重启抓取。

用法:
    python rc_bridge.py --tap           # 只打印, 不注入(安全验证)
    python rc_bridge.py                 # 注入按键
"""
import argparse
import ctypes
import ctypes.wintypes as wt
import os
import queue
import re
import subprocess
import sys
import threading
import time

HERE = os.path.dirname(os.path.abspath(__file__))
ADB = os.path.join(HERE, "android", "platform-tools", "adb.exe")
if not os.path.exists(ADB):
    ADB = "adb"

DEVICE_HINT = "CMCC_Voice_Remote"

# ---------------------------------------------------------------- Linux -> VK
K = {
    "ESC": 1, "1": 2, "2": 3, "3": 4, "4": 5, "5": 6, "6": 7, "7": 8, "8": 9,
    "9": 10, "0": 11, "MINUS": 12, "EQUAL": 13, "BACKSPACE": 14, "TAB": 15,
    "ENTER": 28, "LEFTCTRL": 29, "SPACE": 57, "LEFTSHIFT": 42,
    "UP": 103, "DOWN": 108, "LEFT": 105, "RIGHT": 106,
    "HOME": 102, "END": 107, "PAGEUP": 104, "PAGEDOWN": 109,
    "INSERT": 110, "DELETE": 111,
    "MUTE": 113, "VOLUMEDOWN": 114, "VOLUMEUP": 115, "POWER": 116,
    "PAUSE": 119, "STOP": 128, "MENU": 139, "CALC": 140,
    "SLEEP": 142, "COFFEE": 152, "BACK": 158, "FORWARD": 159,
    "NEXTSONG": 163, "PLAYPAUSE": 164, "PREVIOUSSONG": 165, "STOPCD": 166,
    "RECORD": 167, "REWIND": 168, "PHONE": 169, "HOMEPAGE": 172,
    "REFRESH": 173, "EXIT": 174, "EDIT": 176, "SCROLLUP": 177,
    "SCROLLDOWN": 178, "PLAY": 207, "FASTFORWARD": 208, "SEARCH": 217,
    "BRIGHTNESSDOWN": 224, "BRIGHTNESSUP": 225,
    "MEDIA": 226, "SWITCHVIDEOMODE": 227,
    "KBDILLUMTOGGLE": 228, "KBDILLUMDOWN": 229, "KBDILLUMUP": 230,
    "SENDFILE": 231, "REPLY": 232, "FORWARDMAIL": 233, "SAVE": 234,
    "DOCUMENTS": 235, "BATTERY": 236, "BLUETOOTH": 237, "WLAN": 238,
    "UNKNOWN": 240, "VIDEO_NEXT": 241, "VIDEO_PREV": 242,
    "BRIGHTNESS_CYCLE": 243, "BRIGHTNESS_AUTO": 244, "DISPLAY_OFF": 245,
    "MICMUTE": 248,
}

VK = {
    "ESC": 0x1B, "TAB": 0x09, "ENTER": 0x0D, "SPACE": 0x20,
    "BACKSPACE": 0x08, "DELETE": 0x2E, "INSERT": 0x2D,
    "UP": 0x26, "DOWN": 0x28, "LEFT": 0x25, "RIGHT": 0x27,
    "HOME": 0x24, "END": 0x23, "PAGEUP": 0x21, "PAGEDOWN": 0x22,
    "VOLUMEUP": 0xAF, "VOLUMEDOWN": 0xAE, "MUTE": 0xAD,
    "MEDIA": 0xB5, "PLAYPAUSE": 0xB3, "NEXTSONG": 0xB0, "PREVIOUSSONG": 0xB1,
    "STOPCD": 0xB2, "PLAY": 0xB3,
    "BACK": 0x1B, "FORWARD": 0xA7, "HOMEPAGE": 0x5B, "REFRESH": 0xA8,
    "SEARCH": 0xAA, "EXIT": 0x1B,
    "BRIGHTNESSUP": 0x6F, "BRIGHTNESSDOWN": 0x6F,
    "SLEEP": 0x5F, "POWER": 0, "COFFEE": 0x5F,
    "CALC": 0xB7, "MICMUTE": 0xAD,
    "1": 0x31, "2": 0x32, "3": 0x33, "4": 0x34, "5": 0x35,
    "6": 0x36, "7": 0x37, "8": 0x38, "9": 0x39, "0": 0x30,
}

# Linux 认不出名字的键(这台遥控器特有), 按 linux code 直接指定
# 353=KEY_SELECT(疑似 OK)  240=KEY_UNKNOWN(疑似语音)  127=KEY_COMPOSE(疑似菜单)  63=KEY_F5(未明)
CODE_VK = {
    55:  0x08,   # 遥控器"删除"键 -> Backspace(删光标前一个字, 符合遥控器习惯)
    353: 0x0D,   # -> Enter
    127: 0x5D,   # -> Apps 菜单键
}

EXT_VK = {0x26, 0x28, 0x25, 0x27, 0x24, 0x23, 0x21, 0x22, 0x2D, 0x2E,
          0xAF, 0xAE, 0xAD, 0xB0, 0xB1, 0xB2, 0xB3, 0xB4, 0xB5, 0xA6,
          0xA7, 0xA8, 0xAA, 0xAB, 0xAC, 0xB7, 0x5F, 0x6F,
          # Win / 菜单键的扫描码是 0xE0xx 扩展码, 少了这个标志系统直接丢弃
          0x5B, 0x5C, 0x5D}

# --------------------------------------------------------------- SendInput
INPUT_KEYBOARD = 1
KEYEVENTF_EXTENDEDKEY = 0x0001
KEYEVENTF_KEYUP = 0x0002
KEYEVENTF_SCANCODE = 0x0008
KEYEVENTF_UNICODE = 0x0004

ULONG_PTR = ctypes.c_ulonglong if ctypes.sizeof(ctypes.c_void_p) == 8 else ctypes.c_ulong


class KEYBDINPUT(ctypes.Structure):
    _fields_ = [("wVk", wt.WORD), ("wScan", wt.WORD), ("dwFlags", wt.DWORD),
                ("time", wt.DWORD), ("dwExtraInfo", ULONG_PTR)]


class _INPUTunion(ctypes.Union):
    _fields_ = [("ki", KEYBDINPUT), ("pad", ctypes.c_byte * 32)]


class INPUT(ctypes.Structure):
    _fields_ = [("type", wt.DWORD), ("u", _INPUTunion)]


def _send(vk, up):
    scan = ctypes.windll.user32.MapVirtualKeyW(vk, 0)
    flags = KEYEVENTF_SCANCODE if scan else 0
    if vk in EXT_VK:
        flags |= KEYEVENTF_EXTENDEDKEY
    if up:
        flags |= KEYEVENTF_KEYUP
    inp = INPUT(type=INPUT_KEYBOARD)
    inp.u.ki = KEYBDINPUT(wVk=0 if scan else vk, wScan=scan, dwFlags=flags,
                          time=0, dwExtraInfo=0)
    ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_unicode(ch):
    """直接按字符发送, 与键盘布局无关。
    '#' 若走 Shift+3, 在非美式布局下会变成别的符号, 所以用 UNICODE 更保险。"""
    for up in (False, True):
        flags = KEYEVENTF_UNICODE | (KEYEVENTF_KEYUP if up else 0)
        inp = INPUT(type=INPUT_KEYBOARD)
        inp.u.ki = KEYBDINPUT(wVk=0, wScan=ord(ch), dwFlags=flags,
                              time=0, dwExtraInfo=0)
        ctypes.windll.user32.SendInput(1, ctypes.byref(inp), ctypes.sizeof(INPUT))


def send_combo(vks):
    """按顺序按下再逆序抬起，用于 Win+H 这类组合键"""
    for vk in vks:
        _send(vk, up=False)
    for vk in reversed(vks):
        _send(vk, up=True)


# 特殊动作: linux code -> 动作。按下触发一次, 抬起不重复触发
def _voice_key():
    send_combo([0x5B, 0x48])      # Win + H = Windows 语音键入


def _hash_key():
    send_unicode("#")


def _copy_key():
    send_combo([0x11, 0x43])      # Ctrl + C = 复制


def _paste_key():
    send_combo([0x11, 0x56])      # Ctrl + V = 粘贴


SPECIAL = {
    63: _voice_key,               # 语音键 -> Win+H
}

# linux 240 (KEY_UNKNOWN) 被多个键共用，单看键码分不开，必须靠 MSC_SCAN:
#   0x0700cc  Keyboard 页 usage 0xCC  -> "#" 键
#   0x0c00eb  Consumer 页 usage 0xEB  -> 设置键
#   0x0c00f0  Consumer 页 usage 0xF0  -> M 键
SCAN_NAME = {
    0x0700CC: "#键",
    0x0C00EB: "设置键",
    0x0C00F0: "M键",
}
SCAN_SPECIAL = {
    0x0700CC: _hash_key,
    0x0C00EB: _copy_key,          # 设置键 -> Ctrl+C
    0x0C00F0: _paste_key,         # M 键   -> Ctrl+V
}


# ------------------------------------------------------------ ADB / getevent
def sh(args, timeout=15):
    return subprocess.run([ADB] + args, capture_output=True, text=True,
                          encoding="utf-8", errors="replace", timeout=timeout)


def find_event_node():
    try:
        out = sh(["shell", "getevent -pl"]).stdout
    except Exception:
        return None, None
    cur = None
    for line in out.splitlines():
        m = re.match(r"add device \d+: (/dev/input/event\d+)", line)
        if m:
            cur = m.group(1)
            continue
        m = re.match(r'\s*name:\s+"(.*)"', line)
        if m and cur and DEVICE_HINT in m.group(1):
            return cur, m.group(1)
    return None, None


# 注意: 指定节点抓取时 getevent 不打印 "/dev/input/eventN:" 这一段,
# 不指定节点时才打印。所以设备名这段必须可选, 否则会全军覆没。
RE_EV = re.compile(
    r"\[\s*([0-9.]+)\]\s+(?:/dev/input/event\d+:\s+)?([0-9a-fA-F]{4})\s+"
    r"([0-9a-fA-F]{4})\s+([0-9a-fA-F]{8})")


def reader(proc, out_q, stop_evt):
    for line in proc.stdout:
        if stop_evt.is_set():
            break
        m = RE_EV.search(line)
        if m:
            out_q.put(m.groups())
    out_q.put(None)


def node_ino(path):
    """节点身份。遥控器掉线重连时 eventN 会被销毁重建，路径名不变但 inode 会变，
    拿路径名比对是发现不了的，必须比对 inode。"""
    try:
        out = sh(["shell", "ls -i %s 2>/dev/null" % path], timeout=8).stdout.strip()
    except Exception:
        return None
    if not out:
        return None
    try:
        return out.split()[0]
    except IndexError:
        return None


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--tap", action="store_true", help="只打印，不注入")
    ap.add_argument("--seconds", type=float, default=0)
    args = ap.parse_args()

    devs = sh(["devices"]).stdout.strip().splitlines()
    if len(devs) < 2:
        print("!! 没找到 adb 设备，先把手机插上 USB 并允许调试")
        return 2

    names = {v: k for k, v in K.items()}
    print("模式: %s" % ("只打印(tap)" if args.tap else "注入按键"))
    print("遥控器一按就绪；按 Ctrl+C 结束\n")

    deadline = time.time() + args.seconds if args.seconds else 0
    node = None
    ino = None
    scan = 0          # 最近一次 MSC_SCAN, 用来区分共用 linux 240 的多个键
    proc = None
    q = queue.Queue()
    stop_evt = threading.Event()
    last_wait = 0.0

    def kill_capture():
        nonlocal proc, stop_evt
        if proc is not None:
            stop_evt.set()
            try:
                proc.kill()
            except Exception:
                pass
            proc = None

    while True:
        if deadline and time.time() > deadline:
            break

        cur_ino = node_ino(node) if node else None
        need_rediscover = (node is None or cur_ino is None or cur_ino != ino)

        if need_rediscover:
            if proc is not None:
                kill_capture()
                print("-- 节点已重建，重新挂载 --")
            cur, nm = find_event_node()
            if cur is None:
                node, ino = None, None
                if time.time() - last_wait > 10:
                    print(".. 等遥控器上线（按一下遥控器任意键）")
                    last_wait = time.time()
                time.sleep(1.2)
                continue
            node = cur
            ino = node_ino(node)
            scan = 0
            stop_evt = threading.Event()
            q = queue.Queue()
            print("== 已挂上 %s (%s, inode=%s) ==" % (nm, node, ino))
            # -tt 强制 pty; -t 输出十六进制码(-l 会输出符号名, 解析器不认)
            proc = subprocess.Popen([ADB, "shell", "-tt", "getevent -t %s" % node],
                                    stdout=subprocess.PIPE,
                                    stderr=subprocess.DEVNULL,
                                    text=True, encoding="utf-8",
                                    errors="replace", bufsize=1)
            threading.Thread(target=reader, args=(proc, q, stop_evt),
                             daemon=True).start()

        # 取事件
        try:
            item = q.get(timeout=1.0)
        except queue.Empty:
            continue
        if item is None:
            continue
        _ts, etype, ecode, eval_ = item
        if etype == "0004" and ecode == "0004":
            # EV_MSC / MSC_SCAN：记下最近一次的 HID 扫描码。
            # linux 240(KEY_UNKNOWN) 被好几个键共用，只能靠它区分。
            scan = int(eval_, 16)
            continue
        if etype != "0001":
            continue
        code = int(ecode, 16)
        val = int(eval_, 16)
        if val == 2:
            continue
        kname = names.get(code, "code%d" % code)
        if code == 240:
            kname = SCAN_NAME.get(scan, "UNKNOWN(%06x)" % scan)
        vk = VK.get(kname) or CODE_VK.get(code)
        if val == 1:
            print("%s  DOWN  %-14s linux=%-4d vk=%s" % (
                time.strftime("%H:%M:%S"), kname, code,
                ("0x%02X" % vk) if vk else "未映射"))
        else:
            print("%s  up    %s" % (time.strftime("%H:%M:%S"), kname))
        if args.tap:
            continue
        action = SPECIAL.get(code)
        if action is None and code == 240:
            action = SCAN_SPECIAL.get(scan)
        if action is not None:
            if val == 1:
                try:
                    action()
                except Exception as exc:
                    print("   !! 特殊动作失败: %s" % exc)
            continue
        if vk is None:
            continue
        try:
            _send(vk, up=(val == 0))
        except Exception as exc:
            print("   !! SendInput 失败: %s" % exc)

    if proc is not None:
        try:
            proc.kill()
        except Exception:
            pass
    print("\n结束")
    return 0


if __name__ == "__main__":
    sys.exit(main())
