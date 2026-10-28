#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
CMCC 语音遥控器 —— 按键映射设置（图形版 / 绿色主题 / 免安装）

配套固件：esp/cmcc_s3_bridge（ESP32-S3）

界面：遥控器照片做背景，每个物理键引一条线到左右两侧的文本框，
      框里显示当前绑定的动作。单击文本框即可改键（支持组合键）。

串口协议（行文本，115200）：
  INFO / LIST / SET <id> <outType> <n> <k...> / DEL <id> / SAVE / RESET / REBOOT
  设备主动上报： B <id> <type> <n> <k...>（绑定行）、# 开头的日志
"""
import ctypes
import os
import queue
import sys
import tempfile
import threading
import time
import tkinter as tk
from ctypes import wintypes
from tkinter import messagebox

try:
    import winreg
except ImportError:
    winreg = None

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

# ------------------------------------------------------------------ 配色 --
BG          = "#ffffff"     # 窗口底色：白
PANEL       = "#f1f8f4"     # 浅绿灰（工具栏按钮底）
CARD        = "#ffffff"     # 文本框底
CARD_HOVER  = "#e6f7ec"     # 悬停（浅绿）
CARD_NA     = "#f4f6f5"     # 无效键（TV）
BORDER      = "#a9dcbf"     # 普通边框（浅绿）
BORDER_HOT  = "#17a35a"     # 高亮/已改（主绿）
BORDER_NA   = "#b9c6bf"
TEXT        = "#123a2a"     # 主文字：深绿黑
TEXT_DIM    = "#6d8f7e"     # 次要文字
TEXT_HOT    = "#0d8f4c"
LEAD        = "#8ecfae"     # 引线（浅绿）
LEAD_HOT    = "#17a35a"
ACCENT      = "#17a35a"
WARN        = "#b8791a"
BTN         = "#17a35a"     # 主按钮底
BTN_FG      = "#ffffff"
BTN2        = "#eef7f2"     # 次按钮底
BTN2_FG     = "#2f6b4f"

# 程序声明了 DPI 感知，1 单位 = 1 物理像素。高 DPI 屏上所有尺寸整体放大。
Z = 1.42


def zp(x):
    return int(round(x * Z))


# ---- 画布布局（必须和 make_bg.py 里的算法一致）----
CARD_W   = zp(172)
CARD_H   = zp(27)
PAD_Y    = zp(8)
CROP_X0, CROP_X1 = 280, 520                    # 遥控器本体在原图上的横向范围
IMG_H    = zp(800 * 0.98 + 16) - PAD_Y * 2     # 遥控器在窗口里的高度
IMG_W    = int(round((CROP_X1 - CROP_X0) * IMG_H / 800.0))
WIN_W    = CARD_W * 2 + zp(16) * 2 + IMG_W
WIN_H    = zp(800 * 0.98 + 16)
IMG_K    = IMG_H / 800.0                       # 原图 -> 窗口 的缩放比
OX       = (WIN_W - IMG_W) // 2                # 图片左边距


APP_TITLE = "CMCC 遥控器 - 按键映射"

# ------------------------------------------------------------------ 键位 --
# 原图是 800x800，遥控器本体居中。下面是每个键在原图上的中心坐标。
# id = 0 表示这个键实测不上报（TV 键是红外直发，蓝牙里根本没它）。
REMOTE = [
    ("电源",   0x1066, 338,  67),
    ("TV",     0x0000, 460,  66),
    ("设置",   0x20EB, 339, 115),
    ("静音",   0x20E2, 458, 114),
    ("上",     0x1052, 400, 152),
    ("左",     0x1050, 340, 200),
    ("确定",   0x2041, 400, 200),
    ("右",     0x104F, 462, 200),
    ("下",     0x1051, 400, 250),
    ("菜单",   0x1065, 338, 291),
    ("返回",   0x2224, 458, 291),
    ("麦克风", 0x103E, 398, 310),
    ("音量+",  0x20E9, 335, 366),
    ("主页",   0x2223, 397, 366),
    ("频道+",  0x104E, 460, 366),
    ("音量-",  0x20EA, 335, 415),
    ("M",      0x20F0, 398, 415),
    ("频道-",  0x104B, 460, 415),
    ("1", 0x101E, 335, 473), ("2", 0x101F, 397, 473), ("3", 0x1020, 460, 473),
    ("4", 0x1021, 334, 524), ("5", 0x1022, 397, 524), ("6", 0x1023, 460, 524),
    ("7", 0x1024, 334, 574), ("8", 0x1025, 397, 574), ("9", 0x1026, 460, 574),
    ("#", 0x10CC, 334, 624), ("0", 0x1027, 397, 624), ("*", 0x1055, 461, 625),
]

# ------------------------------------------------------------------ HID --
MOD_CTRL, MOD_SHIFT, MOD_ALT, MOD_WIN = 0xE0, 0xE1, 0xE2, 0xE3

KEY_NAME = {
    0x04: "A", 0x05: "B", 0x06: "C", 0x07: "D", 0x08: "E", 0x09: "F",
    0x0A: "G", 0x0B: "H", 0x0C: "I", 0x0D: "J", 0x0E: "K", 0x0F: "L",
    0x10: "M", 0x11: "N", 0x12: "O", 0x13: "P", 0x14: "Q", 0x15: "R",
    0x16: "S", 0x17: "T", 0x18: "U", 0x19: "V", 0x1A: "W", 0x1B: "X",
    0x1C: "Y", 0x1D: "Z",
    0x1E: "1", 0x1F: "2", 0x20: "3", 0x21: "4", 0x22: "5",
    0x23: "6", 0x24: "7", 0x25: "8", 0x26: "9", 0x27: "0",
    0x28: "回车", 0x29: "Esc", 0x2A: "退格", 0x2B: "Tab", 0x2C: "空格",
    0x2D: "-", 0x2E: "=", 0x2F: "[", 0x30: "]", 0x31: "\\",
    0x33: ";", 0x34: "'", 0x35: "`", 0x36: ",", 0x37: ".", 0x38: "/",
    0x39: "CapsLock",
    0x3A: "F1", 0x3B: "F2", 0x3C: "F3", 0x3D: "F4", 0x3E: "F5", 0x3F: "F6",
    0x40: "F7", 0x41: "F8", 0x42: "F9", 0x43: "F10", 0x44: "F11", 0x45: "F12",
    0x49: "Insert", 0x4A: "Home", 0x4B: "PageUp", 0x4C: "Delete",
    0x4D: "End", 0x4E: "PageDown",
    0x4F: "→", 0x50: "←", 0x51: "↓", 0x52: "↑",
    0x54: "小键盘/", 0x55: "小键盘*", 0x56: "小键盘-", 0x57: "小键盘+",
    0x58: "小键盘回车", 0x65: "菜单键",
    0x68: "F13", 0x69: "F14", 0x6A: "F15", 0x6B: "F16",
}
KEY_USAGE = {v: k for k, v in KEY_NAME.items()}

MEDIA_NAME = {
    0x00E9: "音量 +", 0x00EA: "音量 -", 0x00E2: "静音",
    0x00CD: "播放/暂停", 0x00B7: "停止", 0x00B6: "上一曲", 0x00B5: "下一曲",
    0x00B3: "快进", 0x00B4: "快退",
    0x0196: "打开浏览器", 0x0224: "浏览器后退", 0x0225: "浏览器前进",
    0x0223: "浏览器主页", 0x0221: "搜索", 0x0192: "计算器", 0x0183: "媒体播放器",
}
MEDIA_USAGE = {v: k for k, v in MEDIA_NAME.items()}

MOD_LABEL = {MOD_CTRL: "Ctrl", MOD_SHIFT: "Shift", MOD_ALT: "Alt", MOD_WIN: "Win"}

# 文本框窄，太长的动作名换成短名，否则会和左边的键名撞在一起
SHORT = {
    "PageUp": "PgUp", "PageDown": "PgDn", "Insert": "Ins",
    "Backspace 退格": "退格", "空格": "空格", "小键盘回车": "小Enter",
    "小键盘/": "小/", "小键盘*": "小*", "小键盘-": "小-", "小键盘+": "小+",
    "菜单键": "菜单", "浏览器后退": "后退", "浏览器前进": "前进",
    "浏览器主页": "主页", "打开浏览器": "浏览器", "媒体播放器": "播放器",
    "播放/暂停": "播放", "上一曲": "上一曲", "下一曲": "下一曲",
}


def short(name):
    return SHORT.get(name, name)

# Tk keysym -> (usage, 是否需要 shift)
KEYSYM = {
    "space": 0x2C, "Return": 0x28, "KP_Enter": 0x58, "Escape": 0x29,
    "BackSpace": 0x2A, "Tab": 0x2B, "Delete": 0x4C, "Insert": 0x49,
    "Home": 0x4A, "End": 0x4D, "Prior": 0x4B, "Next": 0x4E,
    "Left": 0x50, "Right": 0x4F, "Up": 0x52, "Down": 0x51,
    "Caps_Lock": 0x39,
    "F1": 0x3A, "F2": 0x3B, "F3": 0x3C, "F4": 0x3D, "F5": 0x3E, "F6": 0x3F,
    "F7": 0x40, "F8": 0x41, "F9": 0x42, "F10": 0x43, "F11": 0x44, "F12": 0x45,
    "minus": 0x2D, "equal": 0x2E, "bracketleft": 0x2F, "bracketright": 0x30,
    "backslash": 0x31, "semicolon": 0x33, "apostrophe": 0x34, "grave": 0x35,
    "comma": 0x36, "period": 0x37, "slash": 0x38,
    # 上档符号：keysym 本身已经表示"要按 shift"
    "underscore": (0x2D, 1), "plus": (0x2E, 1), "braceleft": (0x2F, 1),
    "braceright": (0x30, 1), "bar": (0x31, 1), "colon": (0x33, 1),
    "quotedbl": (0x34, 1), "asciitilde": (0x35, 1), "less": (0x36, 1),
    "greater": (0x37, 1), "question": (0x38, 1),
    "exclam": (0x1E, 1), "at": (0x1F, 1), "numbersign": (0x20, 1),
    "dollar": (0x21, 1), "percent": (0x22, 1), "asciicircum": (0x23, 1),
    "ampersand": (0x24, 1), "asterisk": (0x25, 1), "parenleft": (0x26, 1),
    "parenright": (0x27, 1),
}

MODIFIER_KEYSYMS = {
    "Control_L", "Control_R", "Shift_L", "Shift_R",
    "Alt_L", "Alt_R", "Super_L", "Super_R", "Win_L", "Win_R",
    "Caps_Lock", "Num_Lock",
}


# ------------------------------------------------------- 系统热键屏蔽 --
# 录键时如果只是普通窗口接收按键，Win+L 会真把系统锁掉、Alt+Tab 会切窗口、
# Win+D 会回桌面——组合键根本录不全。所以录键期间装一个 WH_KEYBOARD_LL
# 低级键盘钩子，把按键全部吞掉（不传给系统和其它程序），只把事件交回本程序。
# 钩子只吞键盘，鼠标照常可用，所以"取消"按钮点得动。
WH_KEYBOARD_LL = 13
WM_KEYDOWN, WM_KEYUP = 0x0100, 0x0101
WM_SYSKEYDOWN, WM_SYSKEYUP = 0x0104, 0x0105
WM_QUIT = 0x0012
LLKHF_INJECTED = 0x10

# 虚拟键码 -> HID usage（只列会用到的；对不上就返回 None，不猜）
VK_USAGE = {}
for _i in range(26):                      # A-Z
    VK_USAGE[0x41 + _i] = 0x04 + _i
for _i in range(9):                       # 1-9
    VK_USAGE[0x31 + _i] = 0x1E + _i
VK_USAGE[0x30] = 0x27                     # 0
for _i in range(12):                      # F1-F12
    VK_USAGE[0x70 + _i] = 0x3A + _i
for _i in range(4):                       # F13-F16
    VK_USAGE[0x7C + _i] = 0x68 + _i
VK_USAGE.update({
    0x08: 0x2A, 0x09: 0x2B, 0x0D: 0x28, 0x14: 0x39, 0x1B: 0x29, 0x20: 0x2C,
    0x21: 0x4B, 0x22: 0x4E, 0x23: 0x4D, 0x24: 0x4A,
    0x25: 0x50, 0x26: 0x52, 0x27: 0x4F, 0x28: 0x51,
    0x2D: 0x49, 0x2E: 0x4C, 0x5D: 0x65,          # Insert Delete Apps
    0xBA: 0x33, 0xBB: 0x2E, 0xBC: 0x36, 0xBD: 0x2D, 0xBE: 0x37,
    0xBF: 0x38, 0xC0: 0x35, 0xDB: 0x2F, 0xDC: 0x31, 0xDD: 0x30, 0xDE: 0x34,
    0x6A: 0x55, 0x6B: 0x57, 0x6D: 0x56, 0x6E: 0x63, 0x6F: 0x54,   # 小键盘符号
})
VK_USAGE[0x60] = 0x62                     # 小键盘 0
for _i in range(1, 10):                   # 小键盘 1-9
    VK_USAGE[0x60 + _i] = 0x58 + _i

VK_MOD = {
    0xA0: MOD_SHIFT, 0xA1: MOD_SHIFT, 0x10: MOD_SHIFT,
    0xA2: MOD_CTRL, 0xA3: MOD_CTRL, 0x11: MOD_CTRL,
    0xA4: MOD_ALT, 0xA5: MOD_ALT, 0x12: MOD_ALT,
    0x5B: MOD_WIN, 0x5C: MOD_WIN,
}


class _KBDLLHOOKSTRUCT(ctypes.Structure):
    _fields_ = [("vkCode", wintypes.DWORD), ("scanCode", wintypes.DWORD),
                ("flags", wintypes.DWORD), ("time", wintypes.DWORD),
                ("dwExtraInfo", ctypes.c_void_p)]


_HOOKPROC = ctypes.WINFUNCTYPE(ctypes.c_ssize_t, ctypes.c_int,
                               ctypes.c_size_t, ctypes.c_size_t)


class KeyShield:
    """录键期间吞掉全部键盘输入，事件通过 queue 交给调用方轮询。

    用法：
        hk = KeyShield(); hk.start()
        ...
        while (…): hk.get()      # 取事件
        hk.stop()                # 务必调用，否则键盘一直没反应
    """

    def __init__(self):
        self.q = queue.Queue()
        self._hook = None
        self._proc = None
        self._tid = 0
        self._err = 0
        self._last = 0.0          # 最后一次心跳（弹窗还在收事件）
        self.expired = False      # 看门狗判定界面没反应、已经自动放行
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

    # ---- 钩子线程 ----
    def _proc_func(self, nCode, wParam, lParam):
        try:
            if nCode == 0:
                kb = ctypes.cast(lParam, ctypes.POINTER(_KBDLLHOOKSTRUCT)).contents
                self.q.put((wParam, kb.vkCode, kb.flags))
                return 1                       # 吞掉，不传给系统和别的程序
            return self._u32.CallNextHookEx(ctypes.c_void_p(self._hook), nCode,
                                            wParam, lParam)
        except Exception:
            # 钩子里绝不能抛异常，出事就放行，宁可漏拦也不能卡键盘
            return self._u32.CallNextHookEx(ctypes.c_void_p(self._hook), nCode,
                                            wParam, lParam)

    def _worker(self):
        self._tid = ctypes.windll.kernel32.GetCurrentThreadId()
        self._proc = _HOOKPROC(self._proc_func)     # 保住引用，否则回调被回收
        self._hook = self._u32.SetWindowsHookExW(WH_KEYBOARD_LL, self._proc, None, 0)
        if not self._hook:
            self._err = ctypes.get_last_error()
            return
        msg = wintypes.MSG()
        while self._u32.GetMessageW(ctypes.byref(msg), None, 0, 0) > 0:
            self._u32.TranslateMessage(ctypes.byref(msg))
            self._u32.DispatchMessageW(ctypes.byref(msg))

    # ---- 对外 ----
    def start(self):
        self._last = time.time()
        threading.Thread(target=self._worker, daemon=True).start()
        for _ in range(50):
            if self._hook or self._err:
                break
            time.sleep(0.02)
        if self._hook:
            threading.Thread(target=self._watchdog, daemon=True).start()
        return bool(self._hook)

    def touch(self):
        """弹窗每轮轮询调一次，表示"人还在这儿、界面还活着" """
        self._last = time.time()

    def _watchdog(self):
        """兜底：界面卡死/窗口没人管了就把键盘还回去，别把整台机器卡住。

        以前是固定 60 秒超时——用户开着弹窗慢慢想，超时后钩子悄悄卸掉，
        再按 Win+L 就真锁屏了。改成看心跳：只要弹窗还在轮询就一直拦。
        """
        while self._hook:
            time.sleep(1.0)
            if self._hook and time.time() - self._last > 6.0:
                self.expired = True
                self.stop()
                return

    def stop(self):
        # 先卸载再让消息循环退出；钩子卸载后键盘立刻恢复正常
        try:
            if self._hook:
                self._u32.UnhookWindowsHookEx(ctypes.c_void_p(self._hook))
                self._hook = None
        except Exception:
            pass
        try:
            if self._tid:
                self._u32.PostThreadMessageW(self._tid, WM_QUIT, 0, 0)
        except Exception:
            pass

    def get(self):
        """取一条事件 (是否按下, vk, flags)，没有则 None"""
        try:
            wParam, vk, flags = self.q.get_nowait()
        except queue.Empty:
            return None
        return (wParam in (WM_KEYDOWN, WM_SYSKEYDOWN), vk, flags)


# --------------------------------------------------- 录键时临时关掉锁屏 --
# Win+D / Win+E / Alt+Tab 都能被上面的低级钩子吞掉，唯独 Win+L 不行：
# 锁工作站是系统安全保留动作，钩子能收到 L 键、能拦住 L 键不进别的程序，
# 但拦不住"Win 按下 + L 按下 → 锁屏"这个判定（它在钩子之外）。
# 有效做法只有一个：录键期间把当前用户的"锁定工作站"策略临时置 1，
# 锁屏动作整个失效（Win+L、Ctrl+Alt+Del 里的"锁定"都灰掉），
# 关弹窗时立刻还原成原来的样子（原来没有这个值就删掉）。立即生效，不用重启。
LOCK_POLICY = r"Software\Microsoft\Windows\CurrentVersion\Policies\System"
LOCK_VALUE = "DisableLockWorkstation"


def lock_policy_on():
    """当前用户的"禁用锁定工作站"是否已经生效（读注册表，无需权限）"""
    if winreg is None:
        return False
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LOCK_POLICY, 0,
                            winreg.KEY_READ) as k:
            return int(winreg.QueryValueEx(k, LOCK_VALUE)[0]) == 1
    except (FileNotFoundError, OSError):
        return False


class LockGuard:
    """录键期间禁用 Win+L 锁屏，用完还原。"""

    # 万一程序被强杀（来不及走还原），下次启动按这个痕迹把策略改回去
    MARK = os.path.join(tempfile.gettempdir(), "cmcc_rc_lockguard.txt")

    def __init__(self):
        self._prev = 0
        self._had = False
        self._active = False

    @property
    def active(self):
        return self._active

    def _write_marker(self):
        try:
            with open(self.MARK, "w", encoding="ascii") as f:
                f.write("%d %d" % (1 if self._had else 0, int(self._prev)))
        except OSError:
            pass

    @classmethod
    def restore_stale(cls):
        """启动时调用：上次异常退出留下的禁用状态，恢复掉"""
        try:
            with open(cls.MARK, "r", encoding="ascii") as f:
                had, prev = [int(x) for x in f.read().split()]
        except (OSError, ValueError):
            return False
        g = cls()
        g._had, g._prev, g._active = bool(had), prev, True
        g.off()
        return True

    def on(self):
        if winreg is None:
            return False
        try:
            try:
                with winreg.OpenKey(winreg.HKEY_CURRENT_USER, LOCK_POLICY, 0,
                                    winreg.KEY_READ) as k:
                    self._prev = int(winreg.QueryValueEx(k, LOCK_VALUE)[0])
                    self._had = True
            except (FileNotFoundError, OSError):
                self._prev, self._had = 0, False
            self._write_marker()
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, LOCK_POLICY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                winreg.SetValueEx(k, LOCK_VALUE, 0, winreg.REG_DWORD, 1)
            self._active = True
        except OSError:
            self._active = False        # 被安全软件挡住就降级，不报错
        return self._active

    def off(self):
        if not self._active or winreg is None:
            try:
                os.remove(self.MARK)
            except OSError:
                pass
            return
        self._active = False
        ok = False
        try:
            with winreg.CreateKeyEx(winreg.HKEY_CURRENT_USER, LOCK_POLICY, 0,
                                    winreg.KEY_SET_VALUE) as k:
                if self._had:
                    winreg.SetValueEx(k, LOCK_VALUE, 0, winreg.REG_DWORD,
                                      int(self._prev))
                else:
                    try:
                        winreg.DeleteValue(k, LOCK_VALUE)
                    except (FileNotFoundError, OSError):
                        pass
            ok = True
        except OSError:
            pass
        if ok:                      # 没还原成功就留着痕迹，下次启动再试
            try:
                os.remove(self.MARK)
            except OSError:
                pass


# 提权助手：写这个策略需要管理员（Windows 保护 \Policies\ 分支，普通权限一律拒绝）。
# 主程序自己在管理员下跑就直接用 LockGuard；不是管理员时由用户点一下按钮，
# 提权起一个"助手进程"去改，助手一直等到主程序关掉弹窗（写 done 文件）再还原。
LOCK_DONE = os.path.join(tempfile.gettempdir(), "cmcc_rc_lockguard.done")
LOCK_HELPER_ARG = "--lock-helper"


def run_lock_helper(wait=300):
    """在管理员进程里跑：临时禁用锁屏 → 等主程序收工 → 还原 → 退出"""
    try:
        os.remove(LOCK_DONE)
    except OSError:
        pass
    g = LockGuard()
    if not g.on():          # 写不进去（还是没权限）就直接退出
        return 1
    try:
        end = time.time() + wait
        while time.time() < end:
            if os.path.exists(LOCK_DONE):
                break
            time.sleep(0.3)
    finally:
        g.off()
        try:
            os.remove(LOCK_DONE)
        except OSError:
            pass
    return 0



def describe(out_type, keys, n):
    """绑定 -> 人能读的动作名"""
    if n == 0:
        return "不响应"
    if out_type == 2:
        usage = keys[0] | (keys[1] << 8)
        return short(MEDIA_NAME.get(usage, "多媒体 0x%04X" % usage))
    mods, last = [], None
    for i in range(n):
        k = keys[i]
        if k == 0:          # 补齐用的空位，不算有效键
            continue
        if 0xE0 <= k <= 0xE7:
            mods.append(MOD_LABEL.get(k, "?"))
        else:
            last = k
    name = short(KEY_NAME.get(last, "0x%02X" % last)) if last is not None else ""
    return "+".join(mods + ([name] if name else [])) or "不响应"


# ------------------------------------------------------------------ ini 配置 --
# 配置文件就放在程序（exe / pyw）旁边，叫 remotekey.ini。
# 有它就读它，没它就按内置默认新建一份。格式：键名 = 动作，一行人话，能直接手改。
INI_NAME = "remotekey.ini"

MOD_FULL = {MOD_CTRL: "Ctrl", MOD_SHIFT: "Shift", MOD_ALT: "Alt", MOD_WIN: "Win"}
MOD_PARSE = {
    "ctrl": MOD_CTRL, "control": MOD_CTRL, "ctl": MOD_CTRL,
    "shift": MOD_SHIFT, "sht": MOD_SHIFT,
    "alt": MOD_ALT, "option": MOD_ALT,
    "win": MOD_WIN, "windows": MOD_WIN, "cmd": MOD_WIN, "super": MOD_WIN,
}
REV_SHORT = {v: k for k, v in SHORT.items()}

# 首次生成 ini 用的默认动作（与设备出厂默认一致）
DEFAULT_ACTIONS = {
    "电源": "Win+L", "TV": "", "设置": "Ctrl+C", "静音": "静音",
    "上": "↑", "左": "←", "确定": "回车", "右": "→", "下": "↓",
    "菜单": "菜单键", "返回": "Esc", "麦克风": "Win+H",
    "音量+": "音量 +", "音量-": "音量 -", "主页": "Win", "M": "Ctrl+V",
    "频道+": "PageDown", "频道-": "PageUp",
    "1": "1", "2": "2", "3": "3", "4": "4", "5": "5",
    "6": "6", "7": "7", "8": "8", "9": "9", "0": "0",
    "#": "Delete", "*": "退格",
}


def app_dir():
    """exe 所在目录（打包后）或脚本目录"""
    if getattr(sys, "frozen", False):
        return os.path.dirname(os.path.abspath(sys.executable))
    return os.path.dirname(os.path.abspath(__file__))


def ini_path():
    return os.path.join(app_dir(), INI_NAME)


def key_usage(name):
    """动作名 -> HID usage"""
    if name in KEY_USAGE:
        return KEY_USAGE[name]
    full = REV_SHORT.get(name, name)
    if full in KEY_USAGE:
        return KEY_USAGE[full]
    # 字母/数字大小写、方向箭头别名
    up = full.upper()
    alias = {"ENTER": "回车", "ESC": "Esc", "ESCAPE": "Esc", "BACKSPACE": "退格",
             "BS": "退格", "DEL": "Delete", "SPACE": "空格", "TAB": "Tab",
             "UP": "↑", "DOWN": "↓", "LEFT": "←", "RIGHT": "→",
             "PGUP": "PageUp", "PGDN": "PageDown"}
    if up in alias and alias[up] in KEY_USAGE:
        return KEY_USAGE[alias[up]]
    for k, v in KEY_USAGE.items():
        if k.upper() == up:
            return v
    return None


def parse_action(text):
    """'Win+L' -> (out_type, keys, n)；无法解析返回 None"""
    s = (text or "").strip()
    if not s or s in ("不响应", "无", "-", "none", "None"):
        return 1, [0, 0, 0, 0], 0

    # 多媒体键整串匹配（"音量 +" 本身带加号，必须先整体试）
    if s in MEDIA_USAGE:
        u = MEDIA_USAGE[s]
        return 2, [u & 0xFF, (u >> 8) & 0xFF, 0, 0], 2
    for nm, u in MEDIA_USAGE.items():
        if nm.replace(" ", "") == s.replace(" ", ""):
            return 2, [u & 0xFF, (u >> 8) & 0xFF, 0, 0], 2

    # 单键整串匹配（"小键盘+" 这种名字本身带加号，不能被当成组合键拆开）
    if s in KEY_USAGE:
        return 1, [KEY_USAGE[s], 0, 0, 0], 1
    if s in REV_SHORT and REV_SHORT[s] in KEY_USAGE:
        return 1, [KEY_USAGE[REV_SHORT[s]], 0, 0, 0], 1

    parts = [p.strip() for p in s.split("+") if p.strip()]
    if not parts:
        return None
    mods, main = [], None
    for p in parts:
        m = MOD_PARSE.get(p.lower())
        if m is not None:
            mods.append(m)
        else:
            main = p
    if main is None:
        if not mods:
            return None
        keys = (mods + [0, 0, 0, 0])[:4]
        return 1, keys, len(mods)
    u = key_usage(main)
    if u is None:
        return None
    keys = (mods + [u, 0, 0, 0])[:4]
    n = min(len(mods) + 1, 4)
    return 1, keys, n


def action_text(out_type, keys, n):
    """绑定 -> 写进 ini 的动作文本（用全名，方便手改）"""
    if n == 0:
        return ""
    if out_type == 2:
        u = keys[0] | (keys[1] << 8)
        return MEDIA_NAME.get(u, "0x%04X" % u)
    mods, last = [], None
    for i in range(n):
        k = keys[i]
        if k == 0:
            continue
        if 0xE0 <= k <= 0xE7:
            mods.append(MOD_FULL.get(k, "?"))
        else:
            last = k
    name = KEY_NAME.get(last, "0x%02X" % last) if last is not None else ""
    return "+".join(mods + ([name] if name else []))


def read_ini(path):
    """返回 {键名: (out_type, keys, n)}；文件不存在返回 None"""
    if not os.path.exists(path):
        return None
    data, bad = {}, []
    with open(path, "r", encoding="utf-8-sig", errors="replace") as f:
        for raw in f:
            line = raw.strip()
            if not line or line.startswith(";") or line.startswith("["):
                continue
            if "=" not in line:
                continue
            name, val = line.split("=", 1)
            name = name.strip()
            if not name:
                continue
            r = parse_action(val)
            if r is None:
                bad.append("%s = %s" % (name, val.strip()))
                continue
            data[name] = r
    return data, bad


def write_ini(path, items):
    """items: [(键名, out_type, keys, n)]，按给定顺序写"""
    lines = [
        "; CMCC 遥控器按键映射",
        "; 这个文件就在程序旁边，可以直接用记事本改，保存后重新打开程序生效。",
        ";",
        "; 写法：",
        ";   单个键      F5 / 回车 / Delete / ↑",
        ";   组合键      Win+L  Ctrl+C  Ctrl+Shift+Esc",
        ";   多媒体      音量 +  音量 -  静音  播放/暂停",
        ";   不响应      留空，或者写 不响应",
        ";",
        "[keys]",
    ]
    for name, ot, keys, n in items:
        lines.append("%s = %s" % (name, action_text(ot, keys, n)))
    tmp = path + ".tmp"
    with open(tmp, "w", encoding="utf-8") as f:
        f.write("\n".join(lines) + "\n")
    os.replace(tmp, path)


def default_items():
    """内置默认：按遥控器界面顺序"""
    out = []
    for label, kid, _, _ in REMOTE:
        r = parse_action(DEFAULT_ACTIONS.get(label, ""))
        if r is None:
            r = (1, [0, 0, 0, 0], 0)
        out.append((label,) + r)
    return out


# ------------------------------------------------------------------- 录键 --
class Recorder(tk.Toplevel):
    """点文本框后弹出：直接按下目标键（可组合），或从列表里挑"""

    def __init__(self, master, title, cur, on_ok):
        super().__init__(master)
        self.title(title)
        self.configure(bg=BG)
        self.resizable(False, False)
        self.transient(master)
        self.grab_set()
        self.on_ok = on_ok
        self.result = None
        self.mods = set()

        wrap = tk.Frame(self, bg=BG, padx=zp(22), pady=zp(16))
        wrap.pack(fill="both", expand=True)

        tk.Label(wrap, text="当前绑定", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9))).pack(anchor="w")
        self.lbl_cur = tk.Label(wrap, text=describe(cur["out_type"], cur["keys"], cur["n"])
                                if cur else "未绑定",
                                bg=BG, fg=TEXT, font=("Microsoft YaHei UI", zp(15), "bold"))
        self.lbl_cur.pack(anchor="w", pady=(0, zp(12)))

        box = tk.Frame(wrap, bg=PANEL, highlightbackground=BORDER, highlightthickness=zp(1))
        box.pack(fill="x")
        tk.Label(box, text="直接在键盘上按下要绑定的键", bg=PANEL, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9))).pack(pady=(zp(10), zp(2)))
        self.lbl_rec = tk.Label(box, text="等待按键…", bg=PANEL, fg=ACCENT,
                                font=("Microsoft YaHei UI", zp(20), "bold"))
        self.lbl_rec.pack(pady=(0, zp(4)))
        self.lbl_tip = tk.Label(box, text="支持组合键：按住 Ctrl / Shift / Alt / Win 再按主键",
                                bg=PANEL, fg=TEXT_DIM, font=("Microsoft YaHei UI", zp(8)))
        self.lbl_tip.pack(pady=(0, zp(2)))
        self.lbl_shield = tk.Label(box, text="", bg=PANEL, fg=TEXT_DIM,
                                   font=("Microsoft YaHei UI", zp(8)))
        self.lbl_shield.pack(pady=(0, zp(6)))

        # Win+L 是系统安全保留热键，键盘钩子拦不住；能挡住它的只有"禁用锁屏"策略，
        # 而那个策略写在受保护的 \Policies\ 分支下，必须管理员。所以给两条不折腾的路：
        #   ① 直接填 Win+L —— 不用按、绝不会锁屏（推荐）
        #   ② 管理员屏蔽 —— 弹一次 UAC，临时禁锁屏，随便怎么按
        self.row_winl = tk.Frame(box, bg=PANEL)
        self.row_winl.pack(pady=(0, zp(10)))
        for _txt, _cmd in (("直接填 Win+L", self._fill_winl),
                           ("管理员屏蔽 Win+L", self._elevate)):
            tk.Button(self.row_winl, text=_txt, command=_cmd, bg=BTN2, fg=BTN2_FG,
                      activebackground=CARD_HOVER, activeforeground=TEXT,
                      relief="flat", borderwidth=0,
                      font=("Microsoft YaHei UI", zp(8))).pack(side="left",
                                                               padx=zp(3), ipady=zp(1))

        tk.Label(wrap, text="或者从常用动作里选", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9))).pack(anchor="w", pady=(zp(12), zp(4)))

        pick = tk.Frame(wrap, bg=BG)
        pick.pack(fill="x")
        self.cbo = tk.StringVar(value="—")
        opts = ["—", "不响应"] + list(MEDIA_NAME.values()) + list(KEY_NAME.values())
        self.om = tk.OptionMenu(pick, self.cbo, *opts, command=self._pick)
        self.om.configure(bg=PANEL, fg=TEXT, activebackground=CARD_HOVER,
                          activeforeground=TEXT, highlightthickness=zp(1),
                          highlightbackground=BORDER, borderwidth=0,
                          font=("Microsoft YaHei UI", zp(9)), width=20, anchor="w")
        self.om["menu"].configure(bg="#ffffff", fg=TEXT, font=("Microsoft YaHei UI", zp(9)))
        self.om.pack(side="left")

        bar = tk.Frame(wrap, bg=BG)
        bar.pack(fill="x", pady=(zp(16), 0))
        tk.Button(bar, text="确定", width=8, command=self._ok,
                  bg=BTN, fg=BTN_FG, activebackground="#0f8b4c", activeforeground="#ffffff",
                  relief="flat", borderwidth=0,
                  font=("Microsoft YaHei UI", zp(9))).pack(side="right", padx=(zp(8), 0),
                                                           ipady=zp(2))
        tk.Button(bar, text="取消", width=8, command=self.destroy,
                  bg=BTN2, fg=BTN2_FG, activebackground=CARD_HOVER, activeforeground=TEXT,
                  relief="flat", borderwidth=0,
                  font=("Microsoft YaHei UI", zp(9))).pack(side="right", ipady=zp(2))

        self.bind("<KeyPress>", self._press)
        self.bind("<KeyRelease>", self._release)

        # 录键期间把键盘吞掉：Win+L / Alt+Tab / Win+D 这类系统热键不会被触发，
        # 组合键才录得全。钩子装不上就自动退回普通 Tk 接收（功能降级，不报错）。
        self.shield = KeyShield()
        self.lock = LockGuard()
        self._lock_on = False
        self._helper_owns_lock = False
        self._shielded = False
        if self.shield.start():
            self._shielded = True
            self._lock_on = self.lock.on()      # 管理员启动时能直接改
            self._set_shield_label()
            self._poll()
        else:
            self.lbl_shield.configure(text="提示：未能屏蔽系统热键，Win+L 这类组合请用下面的下拉")

        self.after(40, self._center)
        self.after(80, lambda: self.focus_force())

    def _set_shield_label(self):
        if self._lock_on:
            self.lbl_shield.configure(
                text="已屏蔽系统热键（含 Win+L，不会锁屏）：这里的按键不会传给系统")
            self.row_winl.pack_forget()
        else:
            self.lbl_shield.configure(
                text="已屏蔽 Win+D / Alt+Tab；Win+L 是系统保留键，钩子拦不住")

    # ---- Win+L 的两个出口 ----
    def _fill_winl(self):
        """不按键盘，直接把 Win+L 填进去——绝不会锁屏"""
        r = parse_action("Win+L")
        if r is None:
            return
        self.result = r
        self.lbl_rec.configure(text=describe(*r))
        self.cbo.set("—")

    def _helper_cmd(self):
        """提权起助手：打包成 exe 就重启自己，跑脚本就找 pythonw"""
        if getattr(sys, "frozen", False):
            return sys.executable, LOCK_HELPER_ARG
        py = sys.executable
        if os.path.basename(py).lower() in ("python.exe", "pythonw.exe"):
            pw = os.path.join(os.path.dirname(py), "pythonw.exe")
            if os.path.exists(pw):
                py = pw
        return py, '"%s" %s' % (os.path.abspath(__file__), LOCK_HELPER_ARG)

    def _elevate(self):
        try:
            os.remove(LOCK_DONE)
        except OSError:
            pass
        exe, params = self._helper_cmd()
        try:
            rc = ctypes.windll.shell32.ShellExecuteW(None, "runas", exe,
                                                     params, None, 0)
        except Exception:
            rc = 0
        if rc <= 32:            # 2 = 用户在 UAC 里点了"否"
            self.lbl_shield.configure(
                text="未提权，Win+L 仍会锁屏（用左边按钮直接填更省事）")
            return
        self.lbl_shield.configure(text="正在等管理员确认…")
        self._wait_lock(0)

    def _wait_lock(self, tries):
        if not self._shielded:
            return
        if lock_policy_on():
            self._lock_on = True
            self._helper_owns_lock = True
            self.row_winl.pack_forget()
            self._set_shield_label()
            return
        if tries >= 30:         # 约 9 秒
            self.lbl_shield.configure(
                text="提权没成功，Win+L 仍会锁屏（用左边按钮直接填更省事）")
            return
        self.after(300, lambda: self._wait_lock(tries + 1))

    # ---- 钩子事件处理（吞键模式下唯一的事件来源）----
    def _poll(self):
        if not self._shielded:
            return
        try:
            if not self.winfo_exists():
                return
        except Exception:
            return
        self.shield.touch()
        if self.shield.expired:                 # 界面曾卡住，钩子已自动放行
            self._shielded = False
            self.lock.off()
            self._lock_on = False
            self.lbl_shield.configure(text="键盘已恢复常通（本窗口仍可录键，但不再屏蔽热键）")
            return
        while True:
            ev = self.shield.get()
            if ev is None:
                break
            down, vk, flags = ev
            if down:
                self._vk_down(vk)
            else:
                self._vk_up(vk)
        self.after(25, self._poll)

    def _vk_down(self, vk):
        mod = VK_MOD.get(vk)
        if mod is not None:
            self.mods.add(mod)
            self._show_pending()
            return
        u = VK_USAGE.get(vk)
        if u is None:
            return
        self._commit(list(self.mods) + [u], "keyboard")

    def _vk_up(self, vk):
        mod = VK_MOD.get(vk)
        if mod is not None:
            self.mods.discard(mod)

    def destroy(self):
        try:
            if self._shielded:
                self._shielded = False
                self.shield.stop()
        except Exception:
            pass
        try:
            if self._helper_owns_lock:
                # 策略是提权助手改的，本进程没权限还原；写个 done 让它收工还原
                with open(LOCK_DONE, "w", encoding="ascii") as f:
                    f.write("1")
            else:
                self.lock.off()      # 无论怎么退，锁屏策略一定还原
        except Exception:
            pass
        self._lock_on = False
        try:
            super().destroy()
        except Exception:
            pass

    def _center(self):
        """居中到主窗口，别跑到屏幕外面去"""
        try:
            self.update_idletasks()
            w, h = self.winfo_width(), self.winfo_height()
            m = self.master
            x = m.winfo_rootx() + (m.winfo_width() - w) // 2
            y = m.winfo_rooty() + (m.winfo_height() - h) // 3
            sw, sh = self.winfo_screenwidth(), self.winfo_screenheight()
            x = max(8, min(x, sw - w - 8))
            y = max(8, min(y, sh - h - 64))
            self.geometry("+%d+%d" % (x, y))
        except Exception:
            pass

    # ---- 键盘采集 ----
    def _press(self, ev):
        ks = ev.keysym
        if ks in MODIFIER_KEYSYMS:
            if ks.startswith("Control"): self.mods.add(MOD_CTRL)
            elif ks.startswith("Shift"): self.mods.add(MOD_SHIFT)
            elif ks.startswith("Alt"):   self.mods.add(MOD_ALT)
            elif ks.startswith(("Super", "Win")): self.mods.add(MOD_WIN)
            self._show_pending()
            return

        usage, need_shift = self._lookup(ks, ev)
        if usage is None:
            return
        mods = set(self.mods)
        if need_shift or (ev.state & 0x0001):
            mods.add(MOD_SHIFT)
        self._commit(list(mods) + [usage], "keyboard")

    def _release(self, ev):
        ks = ev.keysym
        if ks.startswith("Control"): self.mods.discard(MOD_CTRL)
        elif ks.startswith("Shift"): self.mods.discard(MOD_SHIFT)
        elif ks.startswith("Alt"):   self.mods.discard(MOD_ALT)
        elif ks.startswith(("Super", "Win")): self.mods.discard(MOD_WIN)

    def _lookup(self, ks, ev):
        if len(ks) == 1:
            c = ks
            if "a" <= c <= "z":
                return 0x04 + (ord(c) - ord("a")), 0
            if "A" <= c <= "Z":
                return 0x04 + (ord(c) - ord("A")), 0      # 大写靠 state 判 shift
            if "1" <= c <= "9":
                return 0x1E + (ord(c) - ord("1")), 0
            if c == "0":
                return 0x27, 0
            return None, 0
        v = KEYSYM.get(ks)
        if v is None:
            return None, 0
        if isinstance(v, tuple):
            return v[0], v[1]
        return v, 0

    def _show_pending(self):
        names = [MOD_LABEL[m] for m in (MOD_CTRL, MOD_SHIFT, MOD_ALT, MOD_WIN) if m in self.mods]
        self.lbl_rec.configure(text="+".join(names + ["…"]) if names else "等待按键…")

    def _commit(self, keys, source):
        n = min(len(keys), 4)
        keys = (keys + [0, 0, 0, 0])[:4]
        self.result = (1, keys, n)
        self.lbl_rec.configure(text=describe(1, keys, n))
        self.cbo.set("—")

    # ---- 下拉选择 ----
    def _pick(self, sel):
        """下拉里选了动作：立刻把录键区显示成它，不然用户以为没选上"""
        sel = (sel or "").strip()
        if not sel or sel == "—":
            return
        r = parse_action(sel)
        if r is None:
            self.lbl_rec.configure(text="无法识别：" + sel)
            return
        self.result = r
        self.lbl_rec.configure(text=describe(*r) or "不响应")

    def _ok(self):
        # 下拉里的名字有键盘键（Esc/F5/回车）、多媒体键（音量 +）、还有"不响应"，
        # 统一交给 parse_action 解析，避免两边表不一致导致选完却存不进去。
        sel = self.cbo.get().strip()
        if sel and sel != "—":
            r = parse_action(sel)
            if r is not None:
                self.result = r
        if self.result is None:
            messagebox.showinfo("提示", "还没录到按键。先在键盘上按一个，或从下拉里选。", parent=self)
            return
        out_type, keys, n = self.result
        self.destroy()
        self.on_ok(out_type, keys, n)


# ------------------------------------------------------------------- 主窗 --
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.configure(bg=BG)
        self.resizable(False, False)

        LockGuard.restore_stale()   # 上次被强杀留下的"禁锁屏"痕迹，先还原

        self.ser = None
        self.rx_q = queue.Queue()
        self.bindings = {}          # 设备当前值 id -> dict(out_type, keys, n)
        self.ini = {}               # 配置源 键名 -> (out_type, keys, n)
        self.changed = set()        # 本次改过的 id
        self.cards = {}             # id -> dict(rect, text, line,...)
        self.hover = None
        self.ini_note = ""

        self._build()
        self._load_ini()
        self.refresh_ports()
        self._drain()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda e: self.destroy())

    def _on_close(self):
        if self.changed:
            r = messagebox.askyesnocancel(
                "有改动未保存",
                "有 %d 个键改了还没保存。\n\n现在保存到设备并写入 %s 吗？"
                % (len(self.changed), INI_NAME))
            if r is None:
                return
            if r:
                self.cmd_save()
                self.after(1500, self.destroy)
                return
        self.destroy()

    # ------------------------------------------------------------- ini ----
    def _load_ini(self):
        p = ini_path()
        res = read_ini(p)
        if res is None:
            items = default_items()
            try:
                write_ini(p, items)
                self.ini_note = "已新建 %s（默认映射）" % INI_NAME
            except Exception as exc:
                self.ini_note = "生成 ini 失败：%s" % exc
            self.ini = {n: (ot, k, c) for n, ot, k, c in items}
        else:
            data, bad = res
            self.ini = data
            self.ini_note = "%s · %d 条" % (INI_NAME, len(data))
            if bad:
                self.ini_note += "，%d 行无法识别" % len(bad)
        self._refresh_all()
        self._mark_dirty()

    def _refresh_all(self):
        for label, kid, _, _ in REMOTE:
            self._refresh_card(kid if kid else ("na", label))

    def _card_value(self, label):
        if label == "TV":
            return "红外键"
        r = self.ini.get(label)
        if r is None:
            return "—"
        ot, keys, n = r
        return describe(ot, keys, n)

    def _build(self):
        top = tk.Frame(self, bg=BG)
        top.pack(fill="x", padx=zp(14), pady=(zp(10), zp(4)))

        tk.Label(top, text="串口", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9))).pack(side="left")
        self.cbo_port = tk.Entry(top, width=9, bg=PANEL, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", justify="center",
                                 font=("Consolas", zp(10)))
        self.cbo_port.pack(side="left", padx=(zp(6), zp(6)), ipady=zp(3))
        tk.Button(top, text="扫端口", command=self.refresh_ports,
                  bg=BTN2, fg=BTN2_FG, relief="flat", activebackground=CARD_HOVER,
                  activeforeground=TEXT, borderwidth=0,
                  font=("Microsoft YaHei UI", zp(9))).pack(side="left")
        self.btn_conn = tk.Button(top, text="连接", command=self.toggle_conn, width=7,
                                  bg=BTN, fg=BTN_FG, relief="flat", borderwidth=0,
                                  activebackground="#0f8b4c", activeforeground="#ffffff",
                                  font=("Microsoft YaHei UI", zp(9)))
        self.btn_conn.pack(side="left", padx=zp(8))

        self.lbl_status = tk.Label(top, text="未连接", bg=BG, fg="#c0392b",
                                   font=("Microsoft YaHei UI", zp(9)))
        self.lbl_status.pack(side="left")

        self.lbl_hint = tk.Label(top, text="", bg=BG, fg=TEXT_DIM,
                                 font=("Microsoft YaHei UI", zp(9)))
        self.lbl_hint.pack(side="right")

        area = tk.Frame(self, bg=BG)
        area.pack(fill="both", expand=True, padx=0, pady=0)
        self.canvas = tk.Canvas(area, bg=BG, highlightthickness=0,
                                width=self._win_w(), height=self._win_h())
        self.canvas.pack()
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click)

        self._draw_background()
        self._layout()

        bar = tk.Frame(self, bg=BG)
        bar.pack(fill="x", padx=zp(14), pady=(zp(8), zp(12)))
        self.btn_save = tk.Button(bar, text="保存到设备", command=self.cmd_save,
                                  bg=BTN, fg=BTN_FG, relief="flat", borderwidth=0,
                                  activebackground="#0f8b4c", activeforeground="#ffffff",
                                  font=("Microsoft YaHei UI", zp(9)))
        self.btn_save.pack(side="left", ipadx=zp(6), ipady=zp(2))
        tk.Button(bar, text="从设备读取", command=lambda: self.cmd_list(True),
                  bg=BTN2, fg=BTN2_FG, relief="flat", borderwidth=0,
                  activebackground=CARD_HOVER, activeforeground=TEXT,
                  font=("Microsoft YaHei UI", zp(9))).pack(side="left", padx=zp(8),
                                                           ipadx=zp(6), ipady=zp(2))
        tk.Button(bar, text="恢复出厂映射", command=self.cmd_reset,
                  bg=BTN2, fg=WARN, relief="flat", borderwidth=0,
                  activebackground=CARD_HOVER, activeforeground=WARN,
                  font=("Microsoft YaHei UI", zp(9))).pack(side="left",
                                                           ipadx=zp(6), ipady=zp(2))
        self.lbl_dirty = tk.Label(bar, text="", bg=BG, fg=TEXT_DIM,
                                  font=("Microsoft YaHei UI", zp(8)))
        self.lbl_dirty.pack(side="right", padx=(zp(24), 0))

        # 窗口高度按实际需要算：画布 + 上下两条工具栏，否则底部按钮会被挤出去
        self.update_idletasks()
        self.geometry("%dx%d" % (WIN_W + zp(20), self.winfo_reqheight() + zp(4)))

    def _win_w(self):
        return WIN_W

    def _win_h(self):
        return WIN_H

    def _draw_background(self):
        """背景图（遥控器照片，白底）。找不到就用纯色。"""
        path = self._asset("bg.png")
        self.bg_img = None
        if path and os.path.exists(path):
            try:
                self.bg_img = tk.PhotoImage(file=path)
                w, h = self.bg_img.width(), self.bg_img.height()
                self.canvas.create_image(w // 2, h // 2, image=self.bg_img)
                return
            except Exception:
                self.bg_img = None
        self.canvas.configure(bg=BG)

    @staticmethod
    def _asset(name):
        base = getattr(sys, "_MEIPASS", os.path.dirname(os.path.abspath(__file__)))
        for d in (os.path.join(base, "assets"), base):
            p = os.path.join(d, name)
            if os.path.exists(p):
                return p
        return None

    # --------------------------------------------------------- 键位布局 --
    def _layout(self):
        # 按 x 坐标分列：在遥控器中线左边的键引到左列，右边的引到右列。
        # 两列内部再按 y 排序，这样引线只会在自己那半边活动，不会横穿遥控器。
        order = sorted(REMOTE, key=lambda k: (k[3], k[2]))
        cols = ([k for k in order if k[2] < 400],
                [k for k in order if k[2] >= 400])
        left = int(CARD_W / 2) + zp(10)
        right = WIN_W - int(CARD_W / 2) - zp(10)

        for ci, items in enumerate(cols):
            cx = left if ci == 0 else right
            side = -1 if ci == 0 else 1
            # 框优先对齐到自己那个键的 y，只在会撞到上一个框时才往下让。
            # 等距排会让上半部分（键很密）的引线斜得很难看。
            last = -1e9
            for i, (label, kid, kx, ky) in enumerate(items):
                ky_win = ky * IMG_K + PAD_Y
                fy = max(ky_win, last + CARD_H + zp(2))
                last = fy
                # 原图坐标 -> 窗口坐标（裁掉两侧白边后再缩放）
                self._make_card(label, kid,
                                (kx - CROP_X0) * IMG_K + OX,
                                ky_win,
                                cx, fy, side=side)

    def _make_card(self, label, kid, kx, ky, cx, cy, side):
        """一个键：锚点 + 引线 + 文本框。锚点落在键的一侧，线不压住键本身。"""
        na = (kid == 0)
        ax = kx + side * zp(17)
        self.canvas.create_oval(ax - zp(2.5), ky - zp(2.5), ax + zp(2.5), ky + zp(2.5),
                                fill=(LEAD if not na else BORDER_NA), outline="")

        edge = cx - side * (CARD_W / 2)
        line = self.canvas.create_line(ax, ky, edge, cy, fill=(LEAD if not na else BORDER_NA),
                                       width=max(1, zp(1)))

        x1, x2 = cx - CARD_W / 2, cx + CARD_W / 2
        y1, y2 = cy - CARD_H / 2, cy + CARD_H / 2
        rect = self.canvas.create_rectangle(x1, y1, x2, y2,
                                            fill=(CARD if not na else CARD_NA),
                                            outline=(BORDER if not na else BORDER_NA),
                                            width=max(1, zp(1)))
        name = self.canvas.create_text(x1 + zp(9), cy, text=label, anchor="w",
                                       fill=(TEXT_DIM if not na else BORDER_NA),
                                       font=("Microsoft YaHei UI", zp(9)))
        val = self.canvas.create_text(x2 - zp(9), cy, text=("红外键" if na else "—"), anchor="e",
                                      fill=(TEXT if not na else BORDER_NA),
                                      font=("Microsoft YaHei UI", zp(10), "bold"))
        self.cards[kid if kid else ("na", label)] = dict(
            rect=rect, name=name, value=val, line=line,
            kid=kid, label=label, x1=x1, y1=y1, x2=x2, y2=y2, na=na)

    # --------------------------------------------------------- 悬停/点击 --
    def _hit(self, x, y):
        for k, c in self.cards.items():
            if c["x1"] <= x <= c["x2"] and c["y1"] <= y <= c["y2"]:
                return k
        return None

    def _on_motion(self, ev):
        k = self._hit(ev.x, ev.y)
        if k == self.hover:
            return
        if self.hover is not None:
            self._paint(self.hover, False)
        self.hover = k
        if k is not None:
            c = self.cards[k]
            if not c["na"]:
                self._paint(k, True)
                self.canvas.configure(cursor="hand2")
            else:
                self.canvas.configure(cursor="arrow")
                self.lbl_hint.configure(text="TV 键实测不走蓝牙（红外直发），改不了")
        else:
            self.canvas.configure(cursor="")
            self.lbl_hint.configure(text="")

    def _on_click(self, ev):
        k = self._hit(ev.x, ev.y)
        if k is None:
            return
        c = self.cards[k]
        if c["na"]:
            messagebox.showinfo("TV 键",
                                "TV 键按下去遥控器不上报任何数据，连空白报告都没有，\n"
                                "实测是红外直发（电视控制区），蓝牙里没有这个键。\n\n"
                                "所以它没法映射到电脑上的任何动作。")
            return
        kid = c["kid"]
        Recorder(self, "改键：%s" % c["label"], self.bindings.get(kid), 
                 lambda t, ks, n: self._apply(kid, t, ks, n))

    def _paint(self, k, hot):
        c = self.cards[k]
        if c["na"]:
            return
        self.canvas.itemconfigure(c["rect"], fill=(CARD_HOVER if hot else CARD),
                                  outline=(BORDER_HOT if (hot or c["kid"] in self.changed) else BORDER),
                                  width=(2 if (hot or c["kid"] in self.changed) else 1))
        self.canvas.itemconfigure(c["line"], fill=(LEAD_HOT if hot else LEAD))

    # ------------------------------------------------------------- 串口 --
    def refresh_ports(self):
        """优先挑 ESP32-S3 的 USB CDC 口（VID 303A），其次最后一个串口"""
        ports, best = [], None
        if serial is not None:
            try:
                for p in serial.tools.list_ports.comports():
                    ports.append(p.device)
                    desc = (p.description or "") + (p.hwid or "")
                    if "303A" in desc.upper() or "ESPRESSIF" in desc.upper():
                        best = p.device
            except Exception:
                pass
        ports.sort()
        if ports:
            pick = best or ports[-1]
            if self.cbo_port.get() != pick:
                self.cbo_port.delete(0, "end")
                self.cbo_port.insert(0, pick)
        return ports

    def toggle_conn(self):
        if self.ser is not None:
            self.close_port()
            return
        if serial is None:
            messagebox.showerror("缺少依赖", "没有 pyserial，无法访问串口。")
            return
        port = self.cbo_port.get().strip()
        if not port:
            messagebox.showwarning("提示", "没填串口。先插上 ESP32-S3 的 USB。")
            return
        try:
            self.ser = serial.Serial(port, 115200, timeout=0.2)
        except Exception as exc:
            self.ser = None
            messagebox.showerror("打开失败", "%s\n\n可能被别的程序占着（串口监视器 / 旧进程）。" % exc)
            return
        self.lbl_status.configure(text="已连接 " + port, fg=ACCENT)
        self.btn_conn.configure(text="断开")
        threading.Thread(target=self._reader, daemon=True).start()
        self.send("INFO")
        self.after(150, lambda: self.cmd_list(False))

    def close_port(self):
        try:
            if self.ser:
                self.ser.close()
        except Exception:
            pass
        self.ser = None
        self.lbl_status.configure(text="未连接", fg="#c9553f")
        self.btn_conn.configure(text="连接")

    def _reader(self):
        buf = b""
        while self.ser is not None:
            try:
                chunk = self.ser.read(256)
            except Exception:
                break
            if not chunk:
                continue
            buf += chunk
            while b"\n" in buf:
                line, buf = buf.split(b"\n", 1)
                self.rx_q.put(line.decode("utf-8", "replace").strip("\r"))

    def send(self, cmd):
        if self.ser is None:
            messagebox.showwarning("未连接", "先连接设备。")
            return False
        try:
            self.ser.write((cmd + "\n").encode())
            return True
        except Exception as exc:
            messagebox.showerror("发送失败", str(exc))
            return False

    def _drain(self):
        try:
            while True:
                self._handle(self.rx_q.get_nowait())
        except queue.Empty:
            pass
        self.after(60, self._drain)

    def _handle(self, line):
        if not line or line.startswith("#"):
            return
        if line.startswith("B "):
            p = line.split()
            if len(p) < 4:
                return
            try:
                kid = int(p[1], 16)
                ot, n = int(p[2]), int(p[3])
                keys = [0, 0, 0, 0]
                for i in range(min(n, 4)):
                    if 4 + i < len(p):
                        keys[i] = int(p[4 + i], 16)
            except ValueError:
                return
            self.bindings[kid] = {"out_type": ot, "keys": keys, "n": n}
            return
        if line.strip() == "OK LIST":
            self._after_list()
            return
        if line.startswith("OK SET"):
            return

    def _refresh_card(self, key):
        c = self.cards.get(key)
        if not c:
            return
        if c["na"]:
            self.canvas.itemconfigure(c["value"], text="红外键")
            return
        self.canvas.itemconfigure(c["value"], text=self._card_value(c["label"]))
        hot = c["kid"] in self.changed
        self.canvas.itemconfigure(c["rect"],
                                  outline=(BORDER_HOT if hot else BORDER),
                                  width=(2 if hot else 1))

    # ------------------------------------------------------------- 动作 --
    def _apply(self, kid, out_type, keys, n):
        """改一个键：更新 ini（内存）+ 立即下发到设备（闪存要另外点保存）"""
        c = self.cards.get(kid)
        label = c["label"] if c else None
        if label:
            self.ini[label] = (out_type, keys, n)
        if n == 0:
            ok = self.send("SET %04X 1 0" % kid)
        else:
            arg = " ".join("%02X" % keys[i] for i in range(n))
            ok = self.send("SET %04X %d %d %s" % (kid, out_type, n, arg))
        if not ok:
            return
        self.bindings[kid] = {"out_type": out_type, "keys": keys, "n": n}
        self.changed.add(kid)
        self._refresh_card(kid)
        self._mark_dirty()

    def _mark_dirty(self):
        if self.changed:
            self.lbl_dirty.configure(text="● 有改动未保存 —— 点「保存到设备」", fg=WARN)
        else:
            self.lbl_dirty.configure(text=self.ini_note, fg=TEXT_DIM)

    def cmd_list(self, force=False):
        """读设备。force=False：ini 优先，只补 ini 缺的键；
        force=True：用设备的值覆盖 ini（点「从设备读取」时用）"""
        self._force_list = bool(force)
        self.bindings.clear()
        if force:
            self.changed.clear()
            self._mark_dirty()
        self.send("LIST")

    def _after_list(self):
        """设备把整张表报完了"""
        force = getattr(self, "_force_list", False)
        added = over = 0
        for label, kid, _, _ in REMOTE:
            if not kid:
                continue
            b = self.bindings.get(kid)
            if b is None:
                continue
            if label in self.ini and not force:
                continue                    # ini 优先，不动已有项
            self.ini[label] = (b["out_type"], b["keys"], b["n"])
            if force:
                over += 1
                self.changed.add(kid)
            else:
                added += 1
        self._refresh_all()
        if force:
            self.ini_note = "已按设备更新 %d 条（未保存）" % over
            self._mark_dirty()
        elif added:
            self.ini_note = "ini 优先，补入设备独有的 %d 条" % added
            self._mark_dirty()
        self._force_list = False

    def cmd_save(self):
        """保存：整套配置下发设备 + 写闪存 + 生成 remotekey.ini"""
        if self.ser is None:
            messagebox.showwarning("未连接", "先连接设备，保存要把配置写进遥控器桥。")
            return
        items = []
        for label, kid, _, _ in REMOTE:
            r = self.ini.get(label)
            if r is None:
                continue
            ot, keys, n = r
            items.append((label, ot, keys, n))
            if not kid:          # TV 是红外键，写进 ini 备查但不下发
                continue
            if n == 0:
                self.send("SET %04X 1 0" % kid)
            else:
                arg = " ".join("%02X" % keys[i] for i in range(n))
                self.send("SET %04X %d %d %s" % (kid, ot, n, arg))

        def do_save():
            self.send("SAVE")
            try:
                write_ini(ini_path(), items)
                self.ini_note = "%s · %d 条" % (INI_NAME, len(items))
            except Exception as exc:
                messagebox.showerror("写 ini 失败", str(exc))
            self.changed.clear()
            self._refresh_all()
            self._mark_dirty()
            self.lbl_status.configure(text="已保存", fg=ACCENT)

        self.after(350, do_save)      # 等这一串 SET 走完再发 SAVE

    def cmd_reset(self):
        if not messagebox.askokcancel("确认", "恢复出厂默认映射？当前所有学习结果会被覆盖。"):
            return
        self.send("RESET")
        self.after(400, lambda: self.cmd_list(True))


def main():
    # 提权助手模式：只临时关掉锁屏，等主程序关弹窗后还原，不碰界面
    if LOCK_HELPER_ARG in sys.argv:
        sys.exit(run_lock_helper())
    if serial is None:
        # pythonw 下没有控制台，不能用 print
        try:
            messagebox.showerror("缺少依赖", "没有 pyserial，请先执行：\n\npip install pyserial")
        except Exception:
            pass
        return
    # 让 Windows 按真实像素渲染，否则高 DPI 下整个界面被缩放、字发虚
    try:
        from ctypes import windll
        windll.shcore.SetProcessDpiAwareness(1)
    except Exception:
        pass
    App().mainloop()


if __name__ == "__main__":
    main()
