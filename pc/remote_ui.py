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
import io
import os
import queue
import sys
import tempfile
import threading
import time
import re
import json
import subprocess
import shutil
import tkinter as tk
import tkinter.font as tkfont
from ctypes import wintypes
from tkinter import messagebox
from tkinter import filedialog

try:
    import winreg
except ImportError:
    winreg = None

try:
    import serial
    import serial.tools.list_ports
except ImportError:
    serial = None

# 本地离线语音转文字（可选功能：缺了只是没有语音，不影响改键）
_ASR_IMPORT_ERR = None
try:
    import asr_win
    ASR_TIMEOUT_EXC = asr_win.AsrTimeout
except Exception as _e:
    asr_win = None
    ASR_TIMEOUT_EXC = RuntimeError
    _ASR_IMPORT_ERR = str(_e)

# ------------------------------------------------------------------ 配色 --
BG          = "#ffffff"     # 窗口底色：白
PANEL       = "#f1f8f4"     # 浅绿灰（工具栏按钮底）
CARD        = "#ffffff"     # 文本框底
CARD_HOVER  = "#e6f7ec"     # 悬停（浅绿）
CARD_NA     = "#f4f6f5"     # 无效键（TV）
CARD_LOCK   = "#eef0ef"     # 被锁住的键（语音直出期间的麦克风键）
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

# 程序声明了 DPI 感知，1 单位 = 1 物理像素。
# Z 是全局缩放系数：启动时是下面这个基准值，用户拖窗口时按比例改。
Z = 1.42
Z_BASE = 1.42          # 用户拖拽缩放时的参照点


def zp(x):
    return int(round(x * Z))


# ---- 画布布局（必须和 make_bg.py 里的算法一致）----
# 这些值全部由 init_metrics() 按当前 Z 算出来，改窗口大小时会再算一次。
CARD_W = CARD_H = PAD_Y = 0
IMG_H = IMG_W = IMG_K = OX = 0
WIN_W = WIN_H = 0
CROP_X0, CROP_X1 = 280, 520                    # 遥控器本体在原图上的横向范围


def init_metrics():
    """按当前 Z 重算全部尺寸常量。窗口缩放时反复调用。"""
    global CARD_W, CARD_H, PAD_Y, IMG_H, IMG_W, WIN_W, WIN_H, IMG_K, OX
    CARD_W = zp(172)
    CARD_H = zp(27)
    PAD_Y = zp(8)
    IMG_H = zp(800 * 0.98 + 16) - PAD_Y * 2     # 遥控器在窗口里的高度
    IMG_W = int(round((CROP_X1 - CROP_X0) * IMG_H / 800.0))
    WIN_W = CARD_W * 2 + zp(16) * 2 + IMG_W
    WIN_H = zp(800 * 0.98 + 16)
    IMG_K = IMG_H / 800.0                       # 原图 -> 窗口 的缩放比
    OX = (WIN_W - IMG_W) // 2                   # 图片左边距


init_metrics()

SIDEBAR_W = zp(140)      # 左侧导航栏宽度（固定，不随窗口等比缩放）


APP_VER = "1.3.4"
APP_TITLE = "CMCC 遥控器 - 按键映射  v%s" % APP_VER

# 鼠标停到左下角版本号上时弹出的更新内容（新的写在最前面，一行一条）。
# 加版本时只往这里加一行，界面不用改。
APP_VER_LOG = [
    "20261106：v1.3.4-语音开麦指令 + 修复装了 esptool 的机器刷不了固件",
    "20261105：v1.3.3-刷完即用（固件内置完整键位）",
    "20261105：v1.3.2-刷完固件自动恢复按键映射",
    "20261104：v1.3.1-修复换机后刷不了固件",
    "20261104：v1.3.0-重构UI",
]

# 窗口最小客户区高度：按 768 高的屏幕算（768 - 任务栏 - 标题栏 ≈ 690）。
# 再往下拖，字和框就挤成一团了，干脆锁死。
MIN_CLIENT_H = 690

# 勾选语音直出时要锁住的键（麦克风键）
MIC_KID = 0x103E

# 勾选后的说明文字（多处共用，改文案只改这里）
# 单独占一行；窗口缩小时按实际宽度从上往下挑一条放得下的
ASR_OFF_NOTES = [
    "（本地离线识别，不用联网；勾选期间麦克风键原绑定功能被屏蔽）",
    "（本地离线识别，不用联网；勾选期间语音键原绑定被屏蔽）",
    "（本地离线识别，不用联网）",
]
ASR_OFF_NOTE = ASR_OFF_NOTES[0]

# 单句识别硬超时：超过这么久还卡在「识别中」就放弃这一句。
# onnxruntime 的 decode 是纯 C 调用，Python 杀不掉，所以这里是"放弃 + 重建识别器"，
# 不是真的终止那个线程 —— 只要后面的识别不再被它堵住就行。
ASR_TIMEOUT_S = 15.0
ASR_TIMEOUT_MS = int(ASR_TIMEOUT_S * 1000)

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

# 语音转文字时要拦截语音键：HID usage -> Windows 虚拟键码
HID_MOD_TO_VK = {0xE0: 0x11, 0xE1: 0x10, 0xE2: 0x12, 0xE3: 0x5B,   # Ctrl/Shift/Alt/Win
                 0xE4: 0x11, 0xE5: 0x10, 0xE6: 0x12, 0xE7: 0x5B}   # 右变体

_HID_SPECIAL_VK = {
    0x28: 0x0D, 0x29: 0x1B, 0x2A: 0x08, 0x2B: 0x09, 0x2C: 0x20,      # 回车 Esc 退格 Tab 空格
    0x2D: 0xBD, 0x2E: 0xBB, 0x2F: 0xDB, 0x30: 0xDD, 0x31: 0xDC,
    0x33: 0xBA, 0x34: 0xDE, 0x35: 0xC0, 0x36: 0xBC, 0x37: 0xBE, 0x38: 0xBF,
    0x39: 0x14,                                                       # CapsLock
    0x49: 0x2D, 0x4A: 0x24, 0x4B: 0x21, 0x4C: 0x2E, 0x4D: 0x23, 0x4E: 0x22,
    0x4F: 0x27, 0x50: 0x25, 0x51: 0x28, 0x52: 0x26,                   # 方向键
    0x65: 0x5D,                                                       # 菜单键
}


def hid_to_vk(u):
    """HID keyboard usage -> Windows VK。认不出返回 0。"""
    if 0x04 <= u <= 0x1D:                 # A-Z
        return 0x41 + (u - 0x04)
    if 0x1E <= u <= 0x26:                 # 1-9
        return 0x31 + (u - 0x1E)
    if u == 0x27:                         # 0
        return 0x30
    if 0x3A <= u <= 0x45:                 # F1-F12
        return 0x70 + (u - 0x3A)
    if 0x68 <= u <= 0x73:                 # F13-F24
        return 0x7C + (u - 0x68)
    return _HID_SPECIAL_VK.get(u, 0)

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

# 首次生成 ini 用的默认动作（与设备出厂默认一致，对应固件 migv=4）
DEFAULT_ACTIONS = {
    "电源": "Win+L", "TV": "", "设置": "Ctrl+C", "静音": "静音",
    "上": "↑", "左": "←", "确定": "回车", "右": "→", "下": "↓",
    "菜单": "Win", "返回": "Esc", "麦克风": "Win+H",
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


# ------------------------------------------------- 串口身份记忆（分级判定用）--
# 为什么单独一个文件：remotekey.ini 是「键名 = 动作」的行式映射表，
# 往里塞串口会把手改过的映射表搞乱，所以另存一份小的 json。
# 记的是**芯片序列号**（跟芯片走、不跟 USB 口走），换 USB 口也认得出同一块板。
PORT_PREF_NAME = "bridge_port.json"


def port_pref_path():
    return os.path.join(app_dir(), PORT_PREF_NAME)


def read_port_pref():
    try:
        with open(port_pref_path(), "r", encoding="utf-8") as fh:
            d = json.load(fh)
        return d if isinstance(d, dict) else {}
    except Exception:
        return {}


def write_port_pref(ser=None, port=None, desc=None):
    d = read_port_pref()
    if ser:
        d["ser"] = ser
    if port:
        d["port"] = port
    if desc:
        d["desc"] = desc
    try:
        with open(port_pref_path(), "w", encoding="utf-8") as fh:
            json.dump(d, fh, ensure_ascii=False, indent=2)
    except Exception:
        pass
    return d


def com_num(dev):
    """COM11 -> 11。用于按数字排序，别用字符串排（"COM6" > "COM11"）。"""
    m = re.search(r"(\d+)", dev or "")
    return int(m.group(1)) if m else 0


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
    """内置默认映射，按遥控器界面顺序返回 (键名, out_type, keys, n)。

    **必须与固件 DEFAULTS[]（cmcc_s3_bridge_v4voice_*.ino）逐条一致。**
    两处是两条独立的兜底路径：
      · 固件里那份 —— 整片刷 merged.bin 清空 NVS 后，setup() 直接落它，
        不连电脑、没有 ini 也已经可用；
      · 这里这份 —— 上位机发现本地没有 remotekey.ini 时用它下发。
    任一处改了另一处没跟上，「刷完即用」就会出现两套结果。
    改完可用 pc/test_default_sync.py 校验一致性。
    """
    out = []
    for label, kid, _, _ in REMOTE:
        r = parse_action(DEFAULT_ACTIONS.get(label, ""))
        if r is None:
            r = (1, [0, 0, 0, 0], 0)
        out.append((label,) + r)
    return out


# ------------------------------------------------------- 硬件信息字段释义 --
# 这些文案来自固件源码（不是在猜），硬件信息页每个字段旁的「?」
# 和鼠标悬停提示都读这张表。改文案只改这里。
HW_TIPS = {
    "mac":  "桥锁定的目标遥控器蓝牙地址（固件内部叫 pin，写在 NVS 里，一旦认定就不再重新搜索）。\n"
            "TARGET 行里的 cand/score/why 是它扫描时的候选与打分依据。",
    "cand": "本轮扫描选中的候选地址。全是 0 表示当前没在扫描、或者没选到。",
    "batt": "遥控器上报的电池电量百分比。\n读不到时会显示 -1 或空白，通常因为链路还没加密、或者根本没连上。",
    "conn": "BLE 链路是否已建立。桥连不上时这一项是「未连接」，其余指纹大多也会缺失。",
    "frames": "累计收到的 0xFC 音频通知条数，每条 20 字节。\n423 条 ≈ 141 帧音频 ≈ 2.8 秒。这个计数只要收到帧就涨，与「是否转储」无关。",
    "dropped": "序号跳变而被丢弃的帧数。0 表示一帧没丢。",
    "voice": "当前是否正在收语音：超过 300ms 没有新帧就判为「否」。",
    "reports": "解析出的 HID 输入报告（对应 GATT 特征）数量。音频、按键、鼠标各占一条。",
    "itvl": "连接间隔。单位 1.25ms，itvl=24 → 24×1.25 = 30ms。\n这是控制器回报的实际生效值，不是上位机请求的值（PC 上根本改不了连接间隔）。",
    "lat":  "从机延迟（Slave Latency）：遥控器允许连续跳过这么多个连接事件不回包。\nlat=50 时空闲状态大约 1.5 秒才握手一次 —— 这就是按键要先「戳」一下才醒的原因。",
    "to":   "监督超时。单位 10ms，to=500 → 5000ms。超过这么久收不到包就判定断链。",
    "mtu":  "ATT 有效载荷上限，247 字节。\n而音频通知只有 20 字节 —— 说明 20 字节是厂商固件写死的切片长度，不是 ATT 分片的结果。",
    "dle":  "数据长度扩展（DLE）协商结果，单包最多 251 字节。\n括号里是固件提示：Arduino 核心把本机 RX 锁在 27，所以这行只证明「协商到了」，不代表整条链路真用满了。",
    "fw_name": "固件自己上报的构建名（OK INFO 里的 name=）。\n这是判断「烧的是哪一版」唯一可靠的依据 —— ESP 镜像里的 app descriptor 在 Arduino 构建下恒为 arduino-lib-builder + git hash，从 bin 反推不出来。",
    "ver":  "映射表的协议版本，上位机据此决定怎么解析按键映射。\n注意：这个不是固件版本号。",
    "binds": "已绑定的按键数量。",
    "learn": "学习模式：只上报按键 id、不执行任何动作，用来录键。",
    "enabled": "桥的输出总开关。关掉时收到按键也不发，只回协议行，方便静默调试。",
    "dirty": "有改动还没写进 NVS 的标志。",
    "audio": "0xFC 音频转储开关。关着时串口上看不到 A 开头的音频行，但 STAT 里的 frames 仍会照常计数 —— 收到帧和转储出来是两件事。",
    "rid":  "HID Report ID 清单。0x01 键盘、0x02 鼠标、0x03 消费者控制（音量/媒体）、\n0xFC 音频裸管道、0xF8 语音键按下/抬起事件。",
    "role": "固件给每个报告判的用途：1=键盘 2=消费者 3=数据通道（不订阅）0=未识别。\n判据是 usage page 加报告长度；0xFB/0xF9/0xF8 都是 255 字节的伪装 Consumer 裸管道。",
    "scan": "扫描详单开关。置 1 会把扫到的每个广播都逐条打出来，排查「为什么没找到遥控器」时用。",
    "mapd": "Report Map 原文（224 字节）以及从中解析出的报告结构。\n它只说明「有哪些报告、各多少字节」，不含任何音频编码参数。",
}


class HoverTip:
    """轻量悬浮提示：鼠标停留 350ms 弹出，单击立刻弹出（3 秒后自动收起）。

    Tk 没有原生 tooltip，这里用无边框 Toplevel 手工摆位，
    并且夹在屏幕内，避免贴着窗口边缘时弹到屏幕外看不见。
    """

    _active = None

    def __init__(self, widget, text, delay=350):
        self.w = widget
        self.text = text
        self.delay = delay
        self._job = None
        self._tip = None
        widget.bind("<Enter>", self._on_enter, add="+")
        widget.bind("<Leave>", self._on_leave, add="+")
        widget.bind("<Button-1>", self._on_click, add="+")

    def _on_enter(self, _e=None):
        self._cancel()
        try:
            self._job = self.w.after(self.delay, self.show)
        except Exception:
            pass

    def _on_leave(self, _e=None):
        self._cancel()
        self.hide()

    def _on_click(self, _e=None):
        self._cancel()
        self.show()
        try:
            self._job = self.w.after(3000, self.hide)
        except Exception:
            pass

    def show(self):
        self._job = None
        if self._tip is not None or not self.text:
            return
        HoverTip.hide_all()
        try:
            x = self.w.winfo_rootx()
            y = self.w.winfo_rooty() + self.w.winfo_height() + zp(4)
            sw = self.w.winfo_screenwidth()
            sh = self.w.winfo_screenheight()
        except Exception:
            return
        tip = tk.Toplevel(self.w)
        tip.wm_overrideredirect(True)
        try:
            tip.attributes("-topmost", True)
        except Exception:
            pass
        tip.configure(bg=BORDER)
        tk.Label(tip, text=self.text, bg="#fffdf2", fg=TEXT, justify="left",
                 anchor="w", font=("Microsoft YaHei UI", zp(9)),
                 wraplength=zp(340), padx=zp(9), pady=zp(7)).pack(padx=1, pady=1)
        tip.update_idletasks()
        w, h = tip.winfo_reqwidth(), tip.winfo_reqheight()
        if x + w > sw - zp(6):
            x = max(zp(6), sw - w - zp(6))
        if y + h > sh - zp(6):                      # 下方放不下就翻到上面
            y = self.w.winfo_rooty() - h - zp(4)
        tip.geometry("+%d+%d" % (x, y))
        self._tip = tip
        HoverTip._active = self

    def hide(self):
        if self._tip is not None:
            try:
                self._tip.destroy()
            except Exception:
                pass
            self._tip = None
        if HoverTip._active is self:
            HoverTip._active = None

    def _cancel(self):
        if self._job is not None:
            try:
                self.w.after_cancel(self._job)
            except Exception:
                pass
            self._job = None

    @classmethod
    def hide_all(cls):
        if cls._active is not None:
            cls._active.hide()


# ------------------------------------------------------------------- 录键 --
class Recorder(tk.Toplevel):
    """点文本框后弹出：直接按下目标键。

    支持两种绑定：
      · 单独绑定修饰键：单击 Ctrl / Shift / Alt / Win 即可（按下后松开即生效）
      · 组合键：按住 Ctrl / Shift / Alt / Win 再按主键（如 Ctrl+C、Win+L）
    """

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
        self._cycle_mods = set()      # 本次按压周期里按下的所有修饰键
        self._plain_committed = False  # 本周期是否已绑定过主键（组合键）

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
        self.lbl_tip = tk.Label(box, text="可单独绑定 Ctrl/Shift/Alt/Win（按下松开即生效），也可组合主键如 Ctrl+C、Win+L",
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
            if not self.mods:
                self._cycle_mods = set()
                self._plain_committed = False
            self._cycle_mods.add(mod)
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
            self._on_mod_release(mod)

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
            if ks.startswith("Control"): m = MOD_CTRL
            elif ks.startswith("Shift"): m = MOD_SHIFT
            elif ks.startswith("Alt"):   m = MOD_ALT
            elif ks.startswith(("Super", "Win")): m = MOD_WIN
            else: m = None
            if m is not None:
                if not self.mods:
                    self._cycle_mods = set()
                    self._plain_committed = False
                self._cycle_mods.add(m)
                self.mods.add(m)
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
        if ks.startswith("Control"): self._on_mod_release(MOD_CTRL)
        elif ks.startswith("Shift"): self._on_mod_release(MOD_SHIFT)
        elif ks.startswith("Alt"):   self._on_mod_release(MOD_ALT)
        elif ks.startswith(("Super", "Win")): self._on_mod_release(MOD_WIN)

    def _on_mod_release(self, mod):
        """修饰键松开：若这组修饰键是单独按下的（没跟主键），就单独绑定它。"""
        self.mods.discard(mod)
        if self.mods or self._plain_committed:
            return
        keys = sorted(self._cycle_mods)
        self._cycle_mods = set()
        if keys:
            self._commit(keys, "keyboard")

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
        self._plain_committed = True
        n = min(len(keys), 4)
        keys = (keys + [0, 0, 0, 0])[:4]
        self.result = (1, keys, n)
        self.lbl_rec.configure(text=describe(1, keys, n))

    def _ok(self):
        if self.result is None:
            messagebox.showinfo("提示", "还没录到按键。先在键盘上按一个组合键，或单独按 Ctrl/Shift/Alt/Win。", parent=self)
            return
        out_type, keys, n = self.result
        self.destroy()
        self.on_ok(out_type, keys, n)


# --------------------------------------------------------------- 大勾选框 --
class BigCheck(tk.Frame):
    """放大版勾选框。

    Tk 原生 Checkbutton 的小方块只有 12px 左右，跟着字号也没法单独放大，
    看着像一行灰字。这里自己画一个方块 + 白色对勾，点方块或点文字都能勾选。
    对外仍用 BooleanVar，其它代码照旧 var.get()/set()。
    """

    def __init__(self, master, text, var, command=None, box=22):
        tk.Frame.__init__(self, master, bg=BG)
        self.var = var
        self.command = command
        self._box = box                      # 逻辑边长，实际按 zp() 缩放
        s = zp(box)
        self.cv = tk.Canvas(self, width=s, height=s, bg=BG,
                            highlightthickness=0, bd=0)
        self.cv.pack(side="left")
        self.lbl = tk.Label(self, text=text, bg=BG, fg=TEXT,
                            font=("Microsoft YaHei UI", zp(10), "bold"))
        self.lbl.pack(side="left", padx=(zp(8), 0))
        for w in (self.cv, self.lbl):
            w.bind("<Button-1>", self._click)
            w.configure(cursor="hand2")
        try:
            self._trace = var.trace_add("write", lambda *a: self.sync())
        except Exception:
            self._trace = None
        self.sync()

    def _click(self, ev=None):
        self.var.set(not self.var.get())
        if self.command:
            self.command()
        self.after(0, self.sync)             # 被外部回滚时也刷一次

    def sync(self):
        try:
            cv = self.cv
            cv.delete("all")
            s = zp(self._box)
            e = max(1, zp(1))
            on = bool(self.var.get())
            cv.create_rectangle(e, e, s - e, s - e,
                                fill=(ACCENT if on else "#ffffff"),
                                outline=(ACCENT if on else "#3f9670"),
                                width=max(2, zp(2)))
            if on:
                w = max(3, zp(2.4))          # 白色对勾，尽量粗，一眼能看见
                cv.create_line(s * 0.23, s * 0.52, s * 0.42, s * 0.72,
                               fill="#ffffff", width=w,
                               capstyle="round", joinstyle="round")
                cv.create_line(s * 0.42, s * 0.72, s * 0.78, s * 0.28,
                               fill="#ffffff", width=w,
                               capstyle="round", joinstyle="round")
        except Exception:
            pass


# ------------------------------------------------------------------- 主窗 --
class App(tk.Tk):
    def __init__(self):
        super().__init__()
        self.title(APP_TITLE)
        self.configure(bg=BG)
        self.resizable(True, True)      # 允许拖拽，内容等比缩放

        LockGuard.restore_stale()   # 上次被强杀留下的"禁锁屏"痕迹，先还原

        self.ser = None
        self._port_user_locked = False   # 用户手改过串口 -> 扫端口不再覆盖
        self._probing = False            # 后台握手中，避免重复开线程
        self.rx_q = queue.Queue()
        self.ui_q = queue.Queue()   # 子线程 -> 主线程的 UI 任务队列
        self.bindings = {}          # 设备当前值 id -> dict(out_type, keys, n)
        self.ini = {}               # 配置源 键名 -> (out_type, keys, n)
        self.changed = set()        # 本次改过的 id
        self.cards = {}             # id -> dict(rect, text, line,...)
        self.hover = None
        self.ini_note = ""

        # ---- 等比缩放状态 ----
        self._base_wh = None        # 首次构建出的基准尺寸（缩放参照）
        self._cur_k = 1.0           # 当前缩放比
        self._resize_job = None     # 防抖用
        self._building = False      # 重建中，忽略 Configure

        # ---- 语音转文字状态 ----
        self.asr_on = False
        self.var_asr = tk.BooleanVar(value=False)   # 语音方案页的勾选框（提前建好，避免未访问即触发）
        self._cap = []              # 硬件信息页抓取时的临时缓冲
        self._cap_active = False
        self._flash_running = False
        self._push_after_flash = False   # 刷写成功后是否要自动把映射写回设备
        self.asr_engine = None      # asr_win.AsrEngine
        self.asr_hook = None        # 吞掉语音键组合的钩子
        self.asr_framer = None
        self.asr_codec = bytearray()
        self.asr_last = 0.0         # 最后一次收到音频帧的时刻
        self.asr_busy = False       # 正在识别，别叠着跑
        self.asr_lock = threading.Lock()
        self.asr_count = 0          # 本次开启后识别过几句
        self._asr_poll = None
        self.asr_gen = 0            # 识别代次：超时作废后，晚到的结果一律丢弃
        self._asr_wd = None         # 15 秒熔断看门狗的 after id
        self._asr_waiter = None     # 传给引擎的"别要结果了"旗标
        self.asr_timeouts = 0       # 熔断过几次（诊断用）

        self._fit_z_to_screen()
        self._build_shell()
        self._build_pages()
        self._show_page(0)
        self._load_ini()
        self.refresh_ports()
        self._drain()
        self.protocol("WM_DELETE_WINDOW", self._on_close)
        self.bind("<Escape>", lambda e: self.destroy())
        # 左侧栏翻页：上/下方向键循环切换（焦点在输入框/文本框时不抢键）
        self.bind("<Up>", lambda e: self._page_step(-1))
        self.bind("<Down>", lambda e: self._page_step(+1))
        # 初始窗口尺寸 = 缩放基准；之后拖拽按高度比例重算 Z 并重建界面（内容等比缩放）
        sw = SIDEBAR_W + WIN_W + zp(20)
        sh = WIN_H + zp(170)
        self.geometry("%dx%d" % (sw, sh))
        self.resizable(True, True)
        # 锁住宽高比（和旧版一致）：这样拖任意一条边都只改大小、不改形状，
        # 内容才能干净地等比缩放，不会被拉扁。
        try:
            self.wm_aspect(sw, sh, sw, sh)
        except Exception:
            pass
        self.update_idletasks()
        self._base_wh = (sw, sh)
        self._base_z = Z
        self._cur_k = 1.0
        self._pending_k = 1.0
        self.bind("<Configure>", self._on_configure)

    # --------------------------------------------------------- 等比缩放 --
    def _fit_z_to_screen(self):
        """启动时按屏幕可用高度收 Z：换到 1080p 小屏也不会开在屏幕外面。"""
        global Z, SIDEBAR_W
        try:
            sh = self.winfo_screenheight()
        except Exception:
            return
        if not sh or sh <= 200:
            return
        avail = sh * 0.90
        est = WIN_H + zp(140)              # 画布 + 上下两条工具栏
        if est > avail > 0:
            Z = Z * (avail / est)
            init_metrics()
            SIDEBAR_W = zp(140)            # 侧栏宽按收过的 Z 重算，别用 import 时的旧值

    def _set_size_limits(self):
        """限制可拖范围。

        下限定成"768 高的屏幕也放得下"：再小字就糊成一团了，直接不让拖。
        上限压到屏幕工作区：窗口高过屏幕会让底部按钮跑到屏幕外，够不着。
        """
        if not self._base_wh:
            return
        bw, bh = self._base_wh
        if bh <= 0:
            return
        min_h = min(MIN_CLIENT_H, bh)                 # 768 屏的可用高度
        try:
            self.minsize(int(round(bw * min_h / float(bh))), min_h)
        except Exception:
            pass
        try:
            work_h = self._work_area_h()
            if work_h:
                max_h = max(min_h, min(int(work_h) - 48, int(bh * 2.6)))
                self.maxsize(int(round(bw * max_h / float(bh))), max_h)
        except Exception:
            pass

    @staticmethod
    def _work_area_h():
        """屏幕工作区高度（扣掉任务栏）。拿不到就返回 0。"""
        try:
            r = wintypes.RECT()
            SPI_GETWORKAREA = 0x0030
            if ctypes.windll.user32.SystemParametersInfoW(
                    SPI_GETWORKAREA, 0, ctypes.byref(r), 0):
                return r.bottom - r.top
        except Exception:
            pass
        return 0

    def _on_configure(self, ev):
        """窗口被拖动后按高度比例重算尺寸。260ms 防抖，拖动过程不狂重建。"""
        if ev.widget is not self or self._building or not self._base_wh:
            return
        bh = self._base_wh[1]
        h = self.winfo_height()
        if h < 200 or bh <= 0:
            return
        k = h / float(bh)
        if abs(k - self._cur_k) < 0.02:
            return
        self._pending_k = max(0.45, min(2.6, k))
        if self._resize_job:
            try:
                self.after_cancel(self._resize_job)
            except Exception:
                pass
        self._resize_job = self.after(260, self._do_resize)

    def _do_resize(self):
        self._resize_job = None
        k = getattr(self, "_pending_k", 1.0)
        if abs(k - self._cur_k) < 0.02:
            return
        self._apply_scale(k)

    def _apply_scale(self, k):
        """按 k 重算全部尺寸并重建界面（左侧栏 + 四页结构）。

        为什么要整个重建：tk 的字号是创建时定死的，改不了；只能按新 Z 重新量一遍。
        「状态」由 _snapshot_ui/_restore_ui 搬过去——串口连接、端口、当前页、
        日志内容、语音勾选、固件路径都保留，用户感觉只是窗口变大变小了。
        """
        global Z, SIDEBAR_W
        if self._building:
            return
        self._building = True
        try:
            HoverTip.hide_all()          # 重建前先收起悬浮提示，别留个孤儿窗口
            st = self._snapshot_ui()
            Z = self._base_z * k
            init_metrics()
            SIDEBAR_W = zp(140)          # 侧栏宽度也跟着等比走
            self._cur_k = k

            for w in self.winfo_children():
                w.destroy()
            self.pages = [None] * len(self.nav_spec)
            self.nav_btns = []
            self.cards.clear()
            self.hover = None

            self._build_shell()
            self._build_pages()
            self._show_page(int(st.get("page", 0)) % max(1, len(self.nav_spec)))
            self._restore_ui(st)
        except Exception:
            pass
        finally:
            self.after(80, self._end_building)

    # ---- 重建时的状态搬运 ----
    def _snapshot_ui(self):
        d = {"page": getattr(self, "_cur_page", 0)}

        def entry_text(attr):
            w = getattr(self, attr, None)
            try:
                return w.get()
            except Exception:
                return ""

        def box_text(attr):
            w = getattr(self, attr, None)
            try:
                return w.get("1.0", "end-1c")
            except Exception:
                return None

        def label_text(attr):
            w = getattr(self, attr, None)
            try:
                return w.cget("text")
            except Exception:
                return None

        d["port"] = entry_text("cbo_port")
        d["status"] = label_text("lbl_status")
        d["conn"] = label_text("btn_conn")
        d["fw"] = entry_text("flash_fw")
        d["log"] = box_text("flash_log")
        d["voice"] = box_text("voice_box")
        for key, attr in (("baud", "flash_baud"), ("autoscroll", "flash_autoscroll"),
                          ("wrap", "flash_wrap")):
            v = getattr(self, attr, None)
            try:
                d[key] = v.get()
            except Exception:
                pass
        try:
            d["asr"] = bool(self.var_asr.get())
        except Exception:
            pass
        return d

    def _restore_ui(self, d):
        try:
            if d.get("port"):
                self.cbo_port.delete(0, "end")
                self.cbo_port.insert(0, d["port"])
            if d.get("status"):
                self.lbl_status.configure(
                    text=d["status"], fg=(ACCENT if self.ser is not None else "#c9553f"))
            if d.get("conn"):
                self.btn_conn.configure(text=d["conn"])
        except Exception:
            pass
        # 语音勾选：BigCheck 挂的是同一个 var，trace 会自己重画
        try:
            self.var_asr.set(bool(d.get("asr", False)))
        except Exception:
            pass
        # 固件烧录页
        try:
            if d.get("fw"):
                self.flash_fw.delete(0, "end")
                self.flash_fw.insert(0, d["fw"])
            # 缩放会整页重建，重建后不能把 esptool 实测的结果退化成「未知」
            if (getattr(self, "_chip_name", "") and
                    getattr(self, "_chip_why", "") == "esptool 实测"):
                self._chip_label(self._chip_name, self._chip_why)
            else:
                self._update_chip()
        except Exception:
            pass
        for key, attr in (("baud", "flash_baud"), ("autoscroll", "flash_autoscroll"),
                          ("wrap", "flash_wrap")):
            v = getattr(self, attr, None)
            if v is not None and key in d:
                try:
                    v.set(d[key])
                except Exception:
                    pass
        try:
            self._flash_toggle_wrap()
        except Exception:
            pass
        # 日志 / 语音文本框（两者都是 disabled 态，写前要临时解锁）
        for txt, attr in ((d.get("log"), "flash_log"), (d.get("voice"), "voice_box")):
            if not txt:
                continue
            w = getattr(self, attr, None)
            if w is None:
                continue
            try:
                w.configure(state="normal")
                w.delete("1.0", "end")
                w.insert("end", txt)
                w.configure(state="disabled")
                if attr == "flash_log":
                    self._flash_n = txt.count("\n")
            except Exception:
                pass

    def _end_building(self):
        self._building = False

    # ------------------------------------------------------- 语音转文字 --
    def _asr_status(self, text, fg=None):
        try:
            self.lbl_asr.configure(text=text, fg=(fg or TEXT_DIM))
        except Exception:
            pass

    def toggle_asr(self):
        if self.var_asr.get():
            if not self._asr_start():
                self.var_asr.set(False)
        else:
            self._asr_stop()

    def _asr_start(self):
        if asr_win is None:
            messagebox.showerror(
                "语音功能不可用",
                "本地语音模块加载失败：\n%s\n\n改键功能不受影响。" % (_ASR_IMPORT_ERR or "未知"))
            return False
        if self.ser is None:
            messagebox.showwarning("未连接", "先连接设备，再开语音转文字。")
            return False

        eng = self.asr_engine or asr_win.AsrEngine()
        if not eng.available():
            messagebox.showerror(
                "缺少语音模型",
                "找不到识别模型（models/sherpa-onnx-paraformer-zh-small-2024-03-09）。\n\n"
                "把 models 目录放到程序旁边即可启用；改键功能不受影响。")
            return False
        if not asr_win.find_decoder():
            messagebox.showerror(
                "缺少解码器",
                "找不到 decode.exe（tools/libg7221/）。\n\n改键功能不受影响。")
            return False
        self.asr_engine = eng

        # 语音键当前绑定 -> 要吞掉的 VK（拦不住就只做识别，不屏蔽原键）
        vk, mods, why = self._voice_key_vk()
        if self.asr_hook:
            self.asr_hook.stop()
            self.asr_hook = None
        if vk:
            hk = asr_win.VoiceKeyHook(vk, mods, None, None)
            if hk.start():
                self.asr_hook = hk
            else:
                why = "键盘钩子安装失败（可能被杀软拦），系统语音输入不会被屏蔽"

        self.asr_framer = asr_win.Framer()
        with self.asr_lock:
            self.asr_codec = bytearray()
        self.asr_last = 0.0
        self.asr_count = 0
        self.asr_busy = False
        self.asr_gen += 1
        self.asr_timeouts = 0
        self._asr_disarm_watchdog()
        self.asr_on = True

        if not self.send("AUDIO 1"):
            self.asr_on = False
            return False

        if self._asr_poll:
            try:
                self.after_cancel(self._asr_poll)
            except Exception:
                pass
        self._asr_poll = self.after(150, self._asr_tick)

        threading.Thread(target=self._asr_warmup, daemon=True).start()

        tip = "就绪，麦克风键已锁定 · 按住说话" + (("（%s）" % why) if why else "")
        self._asr_status("● " + tip, ACCENT)
        self._sync_asr_lock()        # 勾上就把麦克风键锁灰、点不动
        return True

    def _asr_warmup(self):
        try:
            self.asr_engine.load()
            msg = "● 就绪，麦克风键已锁定 · 按住说话（模型 %d ms）" % self.asr_engine.load_ms
            self.after(0, lambda: self._asr_status(msg, ACCENT))
        except Exception as e:
            self.after(0, lambda: self._asr_status("● 模型加载失败：%s" % e, "#c9553f"))

    def _voice_key_vk(self):
        """读麦克风键的绑定，换算成要拦截的 VK。

        优先用设备实际回报的绑定（self.bindings），那才是按下时真会发出去的键；
        没连上设备时退回 ini 里的配置。
        返回 (vk, mods, 说明)；vk=0 表示拦不了（比如绑成多媒体键）。
        """
        dev = self.bindings.get(MIC_KID)
        if dev:
            ot, n = dev["out_type"], dev["n"]
            keys = list(dev["keys"])
            src = "设备"
        else:
            rec = self.ini.get("麦克风")
            if not rec:
                return 0, set(), "麦克风键没配置"
            ot, keys, n = rec
            src = "配置"
        if n == 0:
            return 0, set(), "麦克风键当前是「不响应」"
        if ot != 1:
            return 0, set(), "麦克风键绑的是多媒体键（%s），PC 侧拦不住多媒体键，" \
                             "请改成键盘组合" % src
        mods, main = set(), None
        for i in range(n):
            k = keys[i]
            if k == 0:
                continue
            if 0xE0 <= k <= 0xE7:
                m = HID_MOD_TO_VK.get(k)
                if m:
                    mods.add(m)
            else:
                main = k
        if main is None:
            return 0, set(), "麦克风键没绑主键"
        vk = hid_to_vk(main)
        if not vk:
            return 0, mods, "这个键不支持接管"
        return vk, mods, ""

    def _on_audio_line(self, line):
        """串口读到 "A <HEX>"（在读取线程里跑）。只拼帧，绝不碰 tkinter。"""
        if not self.asr_on or self.asr_framer is None:
            return
        try:
            payload = bytes.fromhex(line[2:].strip())
        except ValueError:
            return
        frame = self.asr_framer.feed(payload)
        if frame is None:
            return
        with self.asr_lock:
            self.asr_codec += asr_win.dewhite(frame)
        self.asr_last = time.time()

    def _asr_tick(self):
        """主线程轮询：静默超过 SILENCE_MS 就把这段送去识别。"""
        self._asr_poll = None
        if not self.asr_on:
            return
        try:
            now = time.time()
            with self.asr_lock:
                n = len(self.asr_codec)
            max_bytes = int(asr_win.CODEC_BYTES * 50 * asr_win.MAX_SECONDS)
            if n > max_bytes:
                # 说太久，截断保护
                with self.asr_lock:
                    self.asr_codec = bytearray()
            elif n and not self.asr_busy and \
                    now - self.asr_last > asr_win.SILENCE_MS / 1000.0:
                with self.asr_lock:
                    codec = bytes(self.asr_codec)
                    self.asr_codec = bytearray()
                # 半截碎片必须清掉：否则它的后半截到齐后会拼成"上一句的尾巴"，
                # 混进下一句的开头（听感就是上一句最后一个字被带了过来）
                if self.asr_framer:
                    self.asr_framer.reset()
                self.asr_busy = True
                self.asr_gen += 1
                gen = self.asr_gen
                self._asr_status("● 识别中…", WARN)
                self._asr_arm_watchdog(gen)      # 先挂看门狗（会新建 waiter）
                waiter = self._asr_waiter
                threading.Thread(target=self._asr_run, args=(codec, gen, waiter),
                                 daemon=True).start()
        finally:
            if self.asr_on:
                self._asr_poll = self.after(120, self._asr_tick)

    # ---- 15 秒熔断 ----
    def _asr_arm_watchdog(self, gen):
        """派发识别时挂一个看门狗；到点还没结束就放弃这一句。"""
        self._asr_disarm_watchdog()
        self._asr_waiter = threading.Event()
        self._asr_wd = self.after(ASR_TIMEOUT_MS, lambda: self._asr_timeout(gen))

    def _asr_disarm_watchdog(self):
        if self._asr_wd:
            try:
                self.after_cancel(self._asr_wd)
            except Exception:
                pass
            self._asr_wd = None

    def _asr_timeout(self, gen):
        self._asr_wd = None
        if gen != self.asr_gen or not self.asr_busy:
            return                       # 已经出结果了，这个看门狗来晚了
        self.asr_timeouts += 1
        self.asr_gen += 1                # 代次 +1：这条线程晚到的结果一律作废
        self.asr_busy = False
        if self._asr_waiter is not None:
            self._asr_waiter.set()       # 告诉还在 decode 的那条线程：结果别要了
        with self.asr_lock:
            self.asr_codec = bytearray()
        if self.asr_framer:
            self.asr_framer.reset()
        self.asr_last = 0.0
        # 关键：把卡死的识别器整份丢掉，下次识别重建一个新的，
        # 否则下一次还会排在同一把被占死的锁上，一直卡。
        try:
            if self.asr_engine is not None:
                self.asr_engine.reset()
        except Exception:
            pass
        if asr_win is not None:
            asr_win.debug_log("识别超时 %.0fs，已放弃这一句并重建识别器（第 %d 次）"
                              % (ASR_TIMEOUT_MS / 1000.0, self.asr_timeouts))
        self._asr_status(
            "● 识别超时（%.0f 秒）已放弃，可以继续说" % (ASR_TIMEOUT_MS / 1000.0),
            "#c9553f")

    def _asr_run(self, codec, gen=0, waiter=None):
        """后台线程：解码 + 预处理 + 识别，完成后回主线程注入。"""
        text, err, ms = "", None, 0
        note = ""
        samples = None
        try:
            t0 = time.time()
            pcm = asr_win.decode_codec(codec)
            if len(pcm) < 3200:                 # <100ms，别浪费算力
                raise RuntimeError("音频太短")
            # 抠开麦爆音 + 掐首尾静音 + 归一化，否则模型会对着噪声瞎猜
            samples, note = asr_win.prepare_audio(asr_win.pcm_to_float(pcm))
            if samples is None:
                raise RuntimeError("没听到说话声（%s）" % note)
            text = self.asr_engine.transcribe(samples, waiter=waiter)
            ms = int((time.time() - t0) * 1000)
        except ASR_TIMEOUT_EXC as e:
            err = "超时已放弃（%s）" % e
        except Exception as e:
            err = str(e)
        dur = len(codec) / float(asr_win.CODEC_BYTES) * asr_win.FRAME_MS / 1000.0
        if asr_win is not None:
            asr_win.debug_log("码流%.2fs 音频%.2fs %s → %s%s"
                              % (dur, len(samples) / 16000.0 if samples is not None
                                 else 0.0, note,
                                 ("「%s」" % text) if text else "(空)",
                                 ("  错误：%s" % err) if err else ""))
        try:
            self.after(0, lambda: self._asr_done(text, err, ms, dur, note, gen))
        except Exception:
            pass

    def _asr_done(self, text, err, ms, dur, note="", gen=0):
        if gen and gen != self.asr_gen:
            # 这句已经被 15 秒熔断判弃了，结果来晚了，别再注入
            asr_win.debug_log("丢弃超时后迟到的结果")
            return
        self._asr_disarm_watchdog()
        self.asr_busy = False
        if err:
            self._asr_status("● %s" % err, "#c9553f")
            return
        if not text:
            self._asr_status("● 没识别出文字（%.1fs 音频，可能没收到声音）" % dur, WARN)
            return
        # 语音转文字测试框：无论是否聚焦都记录识别结果
        self._voice_print(text)
        # 若测试框当前被聚焦，就只在框里显示，不再向系统注入（避免重复上屏）
        try:
            box_focused = (getattr(self, "voice_box", None) is not None
                           and str(self.voice_box) == str(self.focus_get()))
        except Exception:
            box_focused = False
        ok = True if box_focused else asr_win.inject_text(text)
        self.asr_count += 1
        short = text if len(text) <= 20 else text[:20] + "…"
        self._asr_status("● 已输入 %d 字：%s（%.1fs 音频 / %dms）%s" %
                         (len(text), short, dur, ms,
                          "" if ok else "  ※注入失败"), ACCENT if ok else "#c9553f")

    def _asr_stop(self):
        self.asr_on = False
        if self._asr_poll:
            try:
                self.after_cancel(self._asr_poll)
            except Exception:
                pass
            self._asr_poll = None
        if self.asr_hook:
            self.asr_hook.stop()
            self.asr_hook = None
        with self.asr_lock:
            self.asr_codec = bytearray()
        self.asr_framer = None
        self.asr_busy = False
        self.asr_gen += 1               # 关掉时进行中的识别结果一律作废
        self._asr_disarm_watchdog()
        if self._asr_waiter is not None:
            self._asr_waiter.set()
        if self.ser is not None:
            try:
                self.ser.write(b"AUDIO 0\n")   # 直接写，别走 send()（会弹框）
            except Exception:
                pass
        self._asr_status(getattr(self, "_off_note", ASR_OFF_NOTE), TEXT_DIM)
        try:
            self._sync_asr_lock()    # 解锁：麦克风键恢复原绑定功能
        except Exception:
            pass

    def _on_close(self):
        try:
            self._asr_stop()        # 必须先放掉键盘钩子，否则键盘一直被吞
        except Exception:
            pass
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

    # ----------------------------------------------------- 侧栏 + 多页框架 --
    def _build_shell(self):
        """顶部全局栏（串口/连接/状态，页2/3/4 共用）+ 左侧导航 + 右侧页面容器。"""
        # 顶部全局栏
        top = tk.Frame(self, bg=BG)
        top.pack(side="top", fill="x", padx=zp(12), pady=(zp(10), zp(6)))
        tk.Label(top, text="串口", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9))).pack(side="left")
        self.cbo_port = tk.Entry(top, width=9, bg=PANEL, fg=TEXT, insertbackground=TEXT,
                                 relief="flat", justify="center",
                                 font=("Consolas", zp(10)))
        self.cbo_port.pack(side="left", padx=(zp(6), zp(6)), ipady=zp(3))
        # 手动改过串口就记为「手选」，后面扫端口不再覆盖用户的选择
        self.cbo_port.bind("<KeyRelease>", self._on_port_typed)
        tk.Button(top, text="扫端口", command=lambda: self.refresh_ports(force=True),
                  bg=BTN2, fg=BTN2_FG, relief="flat", activebackground=CARD_HOVER,
                  activeforeground=TEXT, borderwidth=0,
                  font=("Microsoft YaHei UI", zp(9))).pack(side="left")
        self.btn_conn = tk.Button(top, text="连接", command=self.toggle_conn, width=7,
                                  bg=BTN, fg=BTN_FG, relief="flat", borderwidth=0,
                                  activebackground="#0f8b4c", activeforeground="#ffffff",
                                  font=("Microsoft YaHei UI", zp(9)))
        self.btn_conn.pack(side="left", padx=zp(8))
        self.lbl_status = tk.Label(top, text="未连接", bg=BG, fg="#c9553f",
                                   font=("Microsoft YaHei UI", zp(9)))
        self.lbl_status.pack(side="left")
        self.lbl_hint = tk.Label(top, text="", bg=BG, fg=TEXT_DIM,
                                 font=("Microsoft YaHei UI", zp(9)))
        self.lbl_hint.pack(side="right", padx=(0, zp(10)))

        # 主体：左栏 + 右侧内容
        body = tk.Frame(self, bg=BG)
        body.pack(side="top", fill="both", expand=True)

        self.side = tk.Frame(body, bg=PANEL, width=SIDEBAR_W)
        self.side.pack(side="left", fill="y")
        self.side.pack_propagate(False)
        self.nav_btns = []
        self.nav_spec = [
            ("硬件信息", self._page_hardware),
            ("按键映射", self._page_keys),
            ("语音方案", self._page_voice),
            ("固件烧录", self._page_flash),
        ]
        for i, (name, _b) in enumerate(self.nav_spec):
            bw = tk.Button(self.side, text=name, command=lambda idx=i: self._show_page(idx),
                           bg=(ACCENT if i == 0 else PANEL),
                           fg=("white" if i == 0 else TEXT),
                           activebackground=ACCENT, activeforeground="white",
                           relief="flat", borderwidth=0, anchor="w",
                           font=("Microsoft YaHei UI", zp(11), "bold"),
                           padx=zp(14), pady=zp(10))
            bw.pack(side="top", fill="x")
            self.nav_btns.append(bw)
        ver_lbl = tk.Label(self.side, text="CMCC_RC_Keys\nv%s" % APP_VER, bg=PANEL,
                           fg=TEXT_DIM, font=("Microsoft YaHei UI", zp(8)),
                           anchor="w", justify="left", cursor="hand2")
        ver_lbl.pack(side="bottom", fill="x", padx=zp(10), pady=zp(8))
        # 悬停/单击版本号 → 弹出更新内容（HoverTip 会自己判断下方放不下就翻到上面）
        self._ver_tip = HoverTip(ver_lbl, "\n".join(APP_VER_LOG))

        self.content = tk.Frame(body, bg=BG)
        self.content.pack(side="left", fill="both", expand=True)
        self.pages = [None] * len(self.nav_spec)
        self._cur_page = 0

    def _build_pages(self):
        for i, (_n, builder) in enumerate(self.nav_spec):
            if self.pages[i] is None:
                builder()

    def _show_page(self, idx):
        for i, f in enumerate(self.pages):
            if f is None:
                continue
            if i == idx:
                f.pack(fill="both", expand=True)
            else:
                f.pack_forget()
        for i, b in enumerate(self.nav_btns):
            on = (i == idx)
            b.configure(bg=(ACCENT if on else PANEL), fg=("white" if on else TEXT))
        self._cur_page = idx
        # 焦点若还停在被切走那页的输入框/文本框里，这里把焦点收回主窗口。
        # 否则上下键会被那个已隐藏的 Text 吃掉，表现为「点侧栏后方向键没反应」。
        self._drop_input_focus()
        if idx == 0:
            self._hw_on_show()
        elif idx == 3 and getattr(self, "flash_port_lbl", None) is not None:
            self.flash_port_lbl.configure(text=self.cbo_port.get() or "—")

    @staticmethod
    def _is_input_widget(w):
        return isinstance(w, (tk.Entry, tk.Text, tk.Listbox, tk.Spinbox))

    def _drop_input_focus(self):
        """把焦点从输入类控件收回主窗口（只在它确实占着焦点时才动）。"""
        try:
            w = self.focus_get()
        except Exception:
            return
        if w is not None and self._is_input_widget(w):
            try:
                self.focus_set()
            except Exception:
                pass

    def _page_step(self, delta):
        """上/下方向键在左侧栏四页之间循环切换。

        只有当焦点停在**当前可见**的输入框/文本框里时才不抢键（那里上下键是移光标、
        滚日志）；隐藏控件或焦点在主窗口时一律响应，避免出现「按了没反应」。
        """
        try:
            w = self.focus_get()
        except Exception:
            w = None
        if self._is_input_widget(w):
            try:
                if w.winfo_ismapped():
                    return None
            except Exception:
                return None
        n = len(getattr(self, "nav_spec", []) or [])
        if n <= 1 or not getattr(self, "pages", None):
            return None
        self._show_page((self._cur_page + delta) % n)
        return "break"

    # --------------------------------------------------- 页1：硬件信息 ----
    def _page_hardware(self):
        if self.pages[0] is not None:
            return
        f = tk.Frame(self.content, bg=BG)
        self.pages[0] = f
        head = tk.Frame(f, bg=BG, padx=zp(18), pady=zp(14))
        head.pack(fill="x")
        tk.Label(head, text="硬件信息", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", zp(14), "bold")).pack(side="left")
        tk.Button(head, text="重新抓取（需已连接桥）", command=self._hw_refresh,
                  bg=BTN2, fg=BTN2_FG, relief="flat", borderwidth=0,
                  activebackground=CARD_HOVER, activeforeground=TEXT,
                  font=("Microsoft YaHei UI", zp(9))).pack(side="right", ipadx=zp(6), ipady=zp(2))

        # 摘要区：每行若干「字段 值 ?」三连，? 悬停或点击给出该字段含义
        self.hw_box = tk.Frame(f, bg=PANEL)
        self.hw_box.pack(fill="x", padx=zp(18), pady=(0, zp(10)))
        self.hw_grid = tk.Frame(self.hw_box, bg=PANEL)
        self.hw_grid.pack(anchor="w", padx=zp(12), pady=zp(9))

        rawf = tk.Frame(f, bg=BG, padx=zp(18))
        rawf.pack(fill="both", expand=True, pady=(0, zp(14)))
        tk.Label(rawf, text="原始指纹（bridge_info.txt）", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9))).pack(anchor="w")
        self.hw_text = tk.Text(rawf, bg="#0f1c16", fg="#c8f0d8", relief="flat",
                               font=("Consolas", zp(9)), wrap="word", state="disabled",
                               height=10)
        self.hw_text.pack(side="left", fill="both", expand=True)
        scb = tk.Scrollbar(rawf, command=self.hw_text.yview)
        scb.pack(side="right", fill="y")
        self.hw_text.configure(yscrollcommand=scb.set)
        self._hw_load_file()

    def _hw_on_show(self):
        self._hw_load_file()

    def _hw_load_file(self):
        p = os.path.join(app_dir(), "bridge_info.txt")
        if os.path.isfile(p):
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    self._hw_render(fh.read())
                return
            except Exception:
                pass
        self._hw_set_message(
            "尚未抓取设备指纹。\n点右上「重新抓取」前请先用顶部「连接」连上 ESP32 桥。")
        self.hw_text.configure(state="normal")
        self.hw_text.delete("1.0", "end")
        self.hw_text.insert("end", "（暂无 bridge_info.txt）")
        self.hw_text.configure(state="disabled")

    # ---- 摘要区渲染：一行若干「字段 值 ?」，? 上挂 HW_TIPS ----
    def _hw_clear_rows(self):
        try:
            HoverTip.hide_all()
            for w in self.hw_grid.winfo_children():
                w.destroy()
        except Exception:
            pass

    def _hw_set_message(self, text):
        self._hw_clear_rows()
        try:
            tk.Label(self.hw_grid, text=text, bg=PANEL, fg=TEXT_DIM, anchor="w",
                     justify="left", font=("Microsoft YaHei UI", zp(9)),
                     wraplength=zp(540)).grid(row=0, column=0, sticky="w")
        except Exception:
            pass

    def _hw_set_rows(self, rows):
        """摘要区一律**单列**：每个字段占一行，横向只有「字段 / 值 / ?」三列。

        `rows` 仍是按语义分组传来（MAC+电量+连接 一组…），但这里顺序展平 ——
        分组只作数据组织，不再并排渲染。原因：并排时一行里只要有一个长值
        （比如报告映射），grid 的第 1 列就被它撑宽，同一行后面几组字段全被
        推到窗口外，只能把窗体拉很宽才看得全。

        cell 可以是 3 元组 (字段, 值, 提示键)，也可以是 4 元组
        (字段, 值, 提示键, True) —— 第 4 位 True 表示值自身带换行、需要多行显示。
        """
        self._hw_clear_rows()
        try:
            r = 0
            for cells in rows:
                for item in cells:
                    label, value, tip = item[0], item[1], item[2]
                    multi = len(item) > 3 and bool(item[3])
                    # 多行值的顶对齐，否则「?」会被拉到整块的垂直中间
                    anchor = "nw" if multi else "w"
                    tk.Label(self.hw_grid, text=label, bg=PANEL, fg=TEXT_DIM,
                             anchor="w", justify="left",
                             font=("Microsoft YaHei UI", zp(9))
                             ).grid(row=r, column=0, sticky=anchor,
                                    padx=(0, zp(8)), pady=zp(1))
                    # 值 + ? 装进同一个格子里：若把 ? 直接放第 2 列，它会排到
                    # 「本列最长的那个值」后面，短值的 ? 就被甩到很右边了。
                    holder = tk.Frame(self.hw_grid, bg=PANEL)
                    holder.grid(row=r, column=1, sticky=anchor,
                                padx=(0, zp(6)), pady=zp(1))
                    tk.Label(holder, text=value, bg=PANEL, fg=TEXT,
                             anchor="nw", justify="left",
                             font=("Consolas", zp(9))
                             ).pack(side="left", anchor="n")
                    q = tk.Label(holder, text="?", bg=PANEL, fg=BORDER_HOT,
                                 font=("Microsoft YaHei UI", zp(8), "bold"),
                                 cursor="hand2", padx=zp(2))
                    q.pack(side="left", anchor="n", padx=(zp(6), 0))
                    HoverTip(q, HW_TIPS.get(tip, ""))
                    r += 1
        except Exception:
            pass

    def _hw_render(self, text):
        info = self.parse_bridge_info(text)
        rows = []

        def push(*cells):
            row = [c for c in cells if c]
            if row:
                rows.append(row)

        def cell(label, value, tip, multi=False):
            if value is None or value == "":
                return None
            return (label, str(value), tip, multi)

        def flag(v, on="开", off="关"):
            return None if v is None else (on if v else off)

        b = info.get("batt")
        push(cell("遥控器 MAC", info.get("mac"), "mac"),
             cell("电量", ("%d%%" % b) if (b is not None and b >= 0) else None, "batt"),
             cell("连接", flag(info.get("conn"), "已连接", "未连接"), "conn"))
        if info.get("cand"):
            push(cell("扫描候选", info.get("cand"), "cand"),
                 cell("打分", info.get("score"), "cand"),
                 cell("依据", info.get("why"), "cand"))

        push(cell("连接间隔", (info["itvl"] + " (%.2fms)" % (int(info["itvl"]) * 1.25))
                  if info.get("itvl", "").isdigit() else info.get("itvl"), "itvl"),
             cell("从机延迟", info.get("lat"), "lat"),
             cell("监督超时", (info["to"] + "0ms") if info.get("to", "").isdigit() else info.get("to"), "to"))

        push(cell("MTU", info.get("mtu"), "mtu"),
             cell("数据长度扩展", ("%s/%s" % (info["dle_tx"], info["dle_rx"]))
                  if info.get("dle_tx") and info.get("dle_rx") else None, "dle"))

        push(cell("帧计数", info.get("frames"), "frames"),
             cell("丢帧", info.get("dropped"), "dropped"),
             cell("语音中", flag(info.get("voice"), "是", "否"), "voice"),
             cell("HID 报告数", info.get("rcount"), "reports"),
             cell("扫描详单", flag(info.get("scan")), "scan"))

        # 固件版本：只认设备自己上报的构建名（OK INFO name=）。
        # 老固件不上报就老实说没上报，本地镜像名只作参考，不冒充设备真实版本。
        dev_name = (info.get("ver_name") or "").strip()
        local_name = self._detect_fw_build_name()
        if dev_name:
            push(cell("固件版本", dev_name, "fw_name"))
        else:
            extra = ""
            if info.get("ver") is not None:
                extra = "协议 ver=%s" % info["ver"]
            if local_name:
                extra = (extra + "｜" if extra else "") + "本地镜像 %s" % local_name
            push(cell("固件版本", "设备未上报构建名（%s）" % (extra or "无"), "fw_name"))

        push(cell("协议版本", info.get("ver"), "ver"),
             cell("绑定数", info.get("binds"), "binds"),
             cell("桥使能", flag(info.get("enabled")), "enabled"))
        push(cell("学习模式", flag(info.get("learn")), "learn"),
             cell("未保存改动", flag(info.get("dirty"), "有", "无"), "dirty"),
             cell("音频转储", flag(info.get("audio")), "audio"))

        if info.get("reports"):
            push(cell("HID 报告", "  ".join(info["reports"]), "rid"))
        if info.get("map"):
            # 报告映射条目多（一块板七八个通道），一条一行会占掉太多纵向空间；
            # 每行 3 条既不会把列撑宽（单列布局下宽度本来就够），又能省行数。
            lines, buf = [], []
            for it in info["map"]:
                buf.append(it)
                if len(buf) >= 3:
                    lines.append("   ".join(buf))
                    buf = []
            if buf:
                lines.append("   ".join(buf))
            push(cell("报告映射", "\n".join(lines), "role", True))

        if rows:
            self._hw_set_rows(rows)
        else:
            self._hw_set_message("（未能解析指纹，见下方原始数据）")

        self.hw_text.configure(state="normal")
        self.hw_text.delete("1.0", "end")
        self.hw_text.insert("end", text)
        self.hw_text.configure(state="disabled")

    @staticmethod
    def parse_bridge_info(text):
        d = {"reports": [], "map": [], "ver_name": ""}
        for line in text.splitlines():
            s = line.strip()
            if s.startswith("OK TARGET"):
                m = re.search(r"pin=([0-9a-fA-F:]+)", s)
                if m:
                    d["mac"] = m.group(1)
                m = re.search(r"cand=([0-9a-fA-F:]+)", s)
                if m and m.group(1).replace(":", "").strip("0"):
                    d["cand"] = m.group(1)          # 全 0 表示没在扫，不显示
                m = re.search(r"score=(-?\d+)", s)
                if m:
                    d["score"] = int(m.group(1))
                m = re.search(r"why=(\S+)", s)
                if m:
                    d["why"] = m.group(1)
            elif s.startswith("OK BATT"):
                m = re.search(r"(-?\d+)", s)
                if m:
                    d["batt"] = int(m.group(1))     # -1 = 还没读到
            elif s.startswith("OK STAT"):
                for k in ("frames", "dropped", "conn", "reports", "voice"):
                    m = re.search(r"(?<![a-z])%s=(-?\d+)" % k, s)
                    if m:
                        d["rcount" if k == "reports" else k] = int(m.group(1))
            elif s.startswith("OK LINK"):
                for k in ("mtu", "itvl", "lat", "to"):
                    m = re.search(r"(?<![a-z])%s=(\d+)" % k, s)
                    if m:
                        d[k] = m.group(1)
            elif s.startswith("OK DLE"):
                m = re.search(r"max_tx_octets=(\d+)", s)
                if m:
                    d["dle_tx"] = m.group(1)
                m = re.search(r"max_rx_octets=(\d+)", s)
                if m:
                    d["dle_rx"] = m.group(1)
            elif s.startswith("OK SCANDUMP"):
                m = re.search(r"(\d+)", s)
                if m:
                    d["scan"] = int(m.group(1))
            elif s.startswith("OK INFO"):
                m = re.search(r"name=([^\s]+)", s)
                if m:
                    d["ver_name"] = m.group(1)
                for k in ("ver", "binds", "learn", "enabled", "connected", "dirty", "audio"):
                    m = re.search(r"%s=(\d+)" % k, s)
                    if m:
                        d[k] = int(m.group(1))
            elif s.startswith("R "):
                m = re.search(r"id=(0x[0-9A-Fa-f]+)", s)
                if m:
                    d["reports"].append(m.group(1))
            elif s.startswith("M "):
                m = re.search(r"id=(0x[0-9A-Fa-f]+)", s)
                if m:
                    role = re.search(r"role=(\d+)", s)
                    by = re.search(r"bytes=(\d+)", s)
                    rn = {"1": "键盘", "2": "消费者", "3": "数据", "0": "未知"}.get(
                        role.group(1) if role else "", "")
                    txt = m.group(1) + (":" + rn if rn else "")
                    if by:
                        txt += by.group(1) + "B"
                    d["map"].append(txt)
        return d

    @staticmethod
    def _mask_mac(mac):
        """（保留）MAC 打码工具；硬件信息页现已按需求显示完整 MAC，不再调用。"""
        parts = mac.split(":")
        if len(parts) == 6:
            return ":".join([parts[0], parts[1], parts[2], "**", "**", parts[5]])
        return mac

    def _detect_fw_build_name(self):
        """从程序所在目录（及其 firmware/ 子目录）里找最新的 *.merged.bin，
        取其构建名作为固件版本显示，例如 cmcc_s3_bridge_v4voice_20261031_2226。
        找不到时返回空串，由调用方回退到设备上报的 ver=。"""
        here = app_dir()
        roots = [here]
        p = here
        for _ in range(3):
            p = os.path.dirname(p)
            roots.append(p)
        cands = []
        for r in roots:
            if not os.path.isdir(r):
                continue
            try:
                for fn in os.listdir(r):
                    if fn.endswith(".merged.bin"):
                        cands.append(os.path.join(r, fn))
            except Exception:
                pass
            fw_dir = os.path.join(r, "firmware")
            if os.path.isdir(fw_dir):
                for _root, _dirs, _files in os.walk(fw_dir):
                    for fn in _files:
                        if fn.endswith(".merged.bin"):
                            cands.append(os.path.join(_root, fn))
        if not cands:
            return ""
        cands.sort(key=lambda x: os.path.getmtime(x), reverse=True)
        name = os.path.basename(cands[0])
        for suf in (".ino.merged.bin", ".merged.bin", ".ino.bin", ".bin", ".ino"):
            if name.endswith(suf):
                name = name[: -len(suf)]
                break
        return name

    def _hw_clear_display(self):
        try:
            self._hw_set_message("正在重新抓取设备指纹…")
            self.hw_text.configure(state="normal")
            self.hw_text.delete("1.0", "end")
            self.hw_text.insert("end", "（抓取中，请稍候…）")
            self.hw_text.configure(state="disabled")
        except Exception:
            pass

    def _hw_refresh(self):
        if self.ser is None:
            messagebox.showwarning("未连接", "先连接 ESP32 桥（顶部「连接」）再抓取。")
            return
        # 自动初始化：先清空旧指纹（避免残留陈旧隐私数据），再重新抓取
        self._hw_clear_display()
        self._cap = []
        self._cap_active = True
        threading.Thread(target=self._hw_capture, daemon=True).start()

    def _hw_capture(self):
        cmds = ["STAT", "TARGET", "INFO", "REPORTS", "MAPDUMP", "BATT", "SCANDUMP"]
        self._cap = []
        self._cap_active = True
        try:
            for c in cmds:
                if self.ser is None:
                    break
                try:
                    self.ser.write((c + "\n").encode())
                except Exception:
                    break
                time.sleep(1.0)
        finally:
            self._cap_active = False
        text = "\n".join(self._cap)
        try:
            with open(os.path.join(app_dir(), "bridge_info.txt"), "w", encoding="utf-8") as fh:
                fh.write(text)
        except Exception:
            pass
        # 子线程不能直接 self.after（会撞 "main thread is not in main loop"），
        # 一律走 ui_q 由主线程的 _drain 执行。
        self.ui_q.put(lambda: self._hw_render(text))

    # --------------------------------------------------- 页2：按键映射 ----
    def _page_keys(self):
        if self.pages[1] is not None:
            return
        f = tk.Frame(self.content, bg=BG)
        self.pages[1] = f
        area = tk.Frame(f, bg=BG)
        area.pack(fill="both", expand=True, padx=0, pady=0)
        self.canvas = tk.Canvas(area, bg=BG, highlightthickness=0,
                                width=self._win_w(), height=self._win_h())
        self.canvas.pack()
        self.canvas.bind("<Motion>", self._on_motion)
        self.canvas.bind("<Button-1>", self._on_click)
        self._draw_background()
        self._layout()

        bar = tk.Frame(f, bg=BG)
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

    # --------------------------------------------------- 页3：语音方案 ----
    def _page_voice(self):
        if self.pages[2] is not None:
            return
        f = tk.Frame(self.content, bg=BG)
        self.pages[2] = f
        pad = tk.Frame(f, bg=BG, padx=zp(20), pady=zp(18))
        pad.pack(fill="both", expand=True)
        self.chk_asr = BigCheck(pad, text="勾选后语音键录音并直接输出文字",
                                var=self.var_asr, command=self.toggle_asr, box=22)
        self.chk_asr.pack(anchor="w", pady=(0, zp(8)))
        self._off_note = self._pick_note(ASR_OFF_NOTES)
        self.lbl_asr = tk.Label(pad, text=self._off_note, anchor="w", bg=BG,
                                fg=TEXT_DIM, font=("Microsoft YaHei UI", zp(10)))
        self.lbl_asr.pack(anchor="w", pady=(0, zp(12)))
        info = ("本地离线语音识别链路：\n"
                "遥控器麦克风 → BLE(G.722.1/Siren7) → ESP32 桥 → 串口 → 本程序解码 "
                "→ sherpa-onnx 识别 → 直接输入文字。\n"
                "全程不联网；首次开启会加载识别模型（约 1~2 秒）。")
        tk.Label(pad, text=info, anchor="w", justify="left", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9)), wraplength=zp(540)
                 ).pack(anchor="w", pady=(0, zp(12)))
        tk.Label(pad, text="语音转文字测试 / 自检输出", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", zp(10), "bold")).pack(anchor="w", pady=(zp(6), zp(4)))
        boxf = tk.Frame(pad, bg=BG)
        boxf.pack(fill="both", expand=True)
        self.voice_box = tk.Text(boxf, bg="#0f1c16", fg="#c8f0d8", relief="flat",
                                 font=("Consolas", zp(9)), wrap="word",
                                 state="disabled", height=8)
        self.voice_box.pack(side="left", fill="both", expand=True)
        vscb = tk.Scrollbar(boxf, command=self.voice_box.yview)
        vscb.pack(side="right", fill="y")
        self.voice_box.configure(yscrollcommand=vscb.set)
        bbox = tk.Frame(pad, bg=BG)
        bbox.pack(fill="x", pady=(zp(8), 0))
        tk.Button(bbox, text="运行环境自检", command=self._run_diag,
                  bg=BTN2, fg=BTN2_FG, relief="flat", borderwidth=0,
                  activebackground=CARD_HOVER, activeforeground=TEXT,
                  font=("Microsoft YaHei UI", zp(9))
                  ).pack(side="left", ipadx=zp(6), ipady=zp(2))
        tk.Button(bbox, text="清空", command=self._voice_clear,
                  bg=BTN2, fg=BTN2_FG, relief="flat", borderwidth=0,
                  activebackground=CARD_HOVER, activeforeground=TEXT,
                  font=("Microsoft YaHei UI", zp(9))
                  ).pack(side="left", padx=zp(8), ipadx=zp(6), ipady=zp(2))

    def _voice_clear(self):
        try:
            self.voice_box.configure(state="normal")
            self.voice_box.delete("1.0", "end")
            self.voice_box.configure(state="disabled")
        except Exception:
            pass

    @staticmethod
    def _diag_mask(line):
        """隐私脱敏：本地用户名路径打码，避免自检输出泄露 C:\\Users\\xxx。"""
        m = re.search(r"(C:\\[Uu]sers\\[^\\]+)", line)
        if m:
            line = line.replace(m.group(1), "C:\\Users\\***")
        return line

    def _voice_print(self, s):
        box = getattr(self, "voice_box", None)
        if box is None:
            return
        try:
            box.configure(state="normal")
            if str(box.index("end-1c")) != "1.0":
                box.insert("end", "\n")
            box.insert("end", s)
            box.configure(state="disabled")
            box.see("end")
        except Exception:
            pass

    def _run_diag(self):
        try:
            p = write_diag()
            try:
                with open(p, "r", encoding="utf-8") as fh:
                    content = fh.read()
            except Exception:
                content = ""
            self._voice_print("==== 运行环境自检（%s）====" % p)
            for line in content.splitlines():
                self._voice_print(self._diag_mask(line))
            self._voice_print("")
            messagebox.showinfo("自检完成",
                                "环境自检结果已写入：\n%s\n（同时已显示在下方文本框，用户名已脱敏）" % p)
        except Exception as e:
            self._voice_print("[自检失败] %s" % e)
            messagebox.showerror("自检失败", str(e))

    # ------------------------------------------------- 页4：固件烧录 ------
    def _page_flash(self):
        if self.pages[3] is not None:
            return
        f = tk.Frame(self.content, bg=BG)
        self.pages[3] = f
        ctl = tk.Frame(f, bg=BG, padx=zp(18), pady=zp(14))
        ctl.pack(fill="x")
        tk.Label(ctl, text="固件烧录", bg=BG, fg=TEXT,
                 font=("Microsoft YaHei UI", zp(14), "bold")).pack(anchor="w", pady=(0, zp(10)))

        row1 = tk.Frame(ctl, bg=BG)
        row1.pack(fill="x", pady=(0, zp(6)))
        tk.Label(row1, text="固件文件", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9))).pack(side="left")
        self.flash_fw = tk.Entry(row1, bg=PANEL, fg=TEXT, relief="flat",
                                 font=("Consolas", zp(9)))
        self.flash_fw.pack(side="left", fill="x", expand=True, padx=zp(6), ipady=zp(3))
        self.flash_fw.bind("<KeyRelease>", lambda e: self._update_chip())
        dw = self._default_firmware()
        if dw:
            self.flash_fw.insert(0, dw)
        tk.Button(row1, text="浏览…", command=self._flash_browse,
                  bg=BTN2, fg=BTN2_FG, relief="flat", borderwidth=0,
                  activebackground=CARD_HOVER, activeforeground=TEXT,
                  font=("Microsoft YaHei UI", zp(9))
                  ).pack(side="left", padx=(zp(4), 0), ipadx=zp(6), ipady=zp(2))

        row2 = tk.Frame(ctl, bg=BG)
        row2.pack(fill="x", pady=(0, zp(6)))
        tk.Label(row2, text="端口", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9))).pack(side="left")
        self.flash_port_lbl = tk.Label(row2, text=self.cbo_port.get() or "—（顶部连接）",
                                       bg=BG, fg=TEXT, font=("Consolas", zp(9)))
        self.flash_port_lbl.pack(side="left", padx=zp(6))
        tk.Label(row2, text="芯片", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9))).pack(side="left", padx=(zp(14), 0))
        # 芯片栏只显示型号本体，识别依据放进悬浮提示 —— 带上「（esptool 实测）」
        # 会把这一行撑长，把右边的波特率挤出可视区（窗口窄时尤其明显）。
        self.flash_chip_lbl = tk.Label(row2, text="未知", bg=BG, fg=TEXT,
                                       width=9, anchor="w",
                                       font=("Consolas", zp(9)))
        self.flash_chip_lbl.pack(side="left", padx=zp(6))
        # 保留上一次的识别依据：整页重建（等比缩放）后要靠它判断是否该恢复实测结果
        self._chip_why = getattr(self, "_chip_why", "")
        self._chip_tip = HoverTip(self.flash_chip_lbl, "芯片型号自动识别")
        tk.Button(row2, text="检测芯片", command=self._flash_detect_chip,
                  bg=BTN2, fg=BTN2_FG, relief="flat", borderwidth=0,
                  activebackground=CARD_HOVER, activeforeground=TEXT,
                  font=("Microsoft YaHei UI", zp(8))
                  ).pack(side="left", ipadx=zp(4), ipady=zp(1))
        tk.Label(row2, text="波特率", bg=BG, fg=TEXT_DIM,
                 font=("Microsoft YaHei UI", zp(9))).pack(side="left", padx=(zp(14), 0))
        self.flash_baud = tk.StringVar(value="921600")
        om = tk.OptionMenu(row2, self.flash_baud, "921600", "460800", "230400", "115200")
        om.configure(bg=PANEL, fg=TEXT, relief="flat",
                     font=("Consolas", zp(9)), highlightthickness=0)
        om.pack(side="left", padx=zp(6))

        row3 = tk.Frame(ctl, bg=BG)
        row3.pack(fill="x", pady=(zp(4), zp(8)))
        tk.Button(row3, text="开始刷写", command=self._flash_start,
                  bg=BTN, fg=BTN_FG, relief="flat", borderwidth=0,
                  activebackground="#0f8b4c", activeforeground="#ffffff",
                  font=("Microsoft YaHei UI", zp(9))
                  ).pack(side="left", ipadx=zp(10), ipady=zp(2))
        tk.Button(row3, text="保存日志", command=self._flash_save,
                  bg=BTN2, fg=BTN2_FG, relief="flat", borderwidth=0,
                  activebackground=CARD_HOVER, activeforeground=TEXT,
                  font=("Microsoft YaHei UI", zp(9))
                  ).pack(side="left", padx=zp(8), ipadx=zp(6), ipady=zp(2))
        tk.Button(row3, text="清空", command=self._flash_clear,
                  bg=BTN2, fg=BTN2_FG, relief="flat", borderwidth=0,
                  activebackground=CARD_HOVER, activeforeground=TEXT,
                  font=("Microsoft YaHei UI", zp(9))
                  ).pack(side="left", ipadx=zp(6), ipady=zp(2))
        self.flash_autoscroll = tk.BooleanVar(value=True)
        tk.Checkbutton(row3, text="自动滚屏", variable=self.flash_autoscroll,
                       bg=BG, fg=TEXT_DIM, selectcolor=PANEL, activebackground=BG,
                       font=("Microsoft YaHei UI", zp(9))
                       ).pack(side="left", padx=zp(12))
        self.flash_wrap = tk.BooleanVar(value=True)
        tk.Checkbutton(row3, text="自动换行", variable=self.flash_wrap,
                       bg=BG, fg=TEXT_DIM, selectcolor=PANEL, activebackground=BG,
                       font=("Microsoft YaHei UI", zp(9)), command=self._flash_toggle_wrap
                       ).pack(side="left")

        logf = tk.Frame(f, bg=BG, padx=zp(18))
        logf.pack(fill="both", expand=True, pady=(0, zp(14)))
        self.flash_log = tk.Text(logf, bg="#0f1c16", fg="#c8f0d8", relief="flat",
                                 font=("Consolas", zp(9)), wrap="word",
                                 state="disabled", height=12)
        self.flash_log.pack(side="left", fill="both", expand=True)
        # 串口实时输出混在刷写日志里，给最常看的按键行和错误行上色，便于扫读
        self.flash_log.tag_configure("key", foreground="#5ddcae")
        self.flash_log.tag_configure("err", foreground="#ff8f80")
        lscb = tk.Scrollbar(logf, command=self.flash_log.yview)
        lscb.pack(side="right", fill="y")
        self.flash_log.configure(yscrollcommand=lscb.set)
        self._flash_print(
            "就绪。固件默认指向程序所在文件夹，点「浏览」从该目录选 .bin。\n"
            "点「开始刷写」即可，程序会自动让芯片进下载模式（1200bps touch，等效按 BOOT）。\n"
            "· 自动复位失败时才需要手动：按住 BOOT 不放 → 点一下 EN → 松开 BOOT。\n"
            "· 进下载模式后 COM 号会变（换台电脑就可能是别的号），程序按 USB 位置\n"
            "  自动认新端口，不用管它是 COM 几；\n"
            "· merged.bin 从 0x0 整片写入会清掉 NVS 里的按键映射，刷前先在按键页\n"
            "  点「从设备读取」把映射读出来备份。\n"
            "日志实时滚屏，可「保存日志」导出。\n")
        self._update_chip()

    # ---- 页4 辅助 ----
    def _default_firmware(self):
        """默认指向程序（.py / exe）所在文件夹，由用户再浏览选择 .bin。"""
        return app_dir()

    def _detect_chip(self, path):
        """从固件文件名尝试识别芯片型号，识别不到返回「未知」。"""
        p = (path or "").lower()
        if "esp32s3" in p:
            return "ESP32-S3"
        if "esp8266" in p:
            return "ESP8266"
        if "esp32" in p:
            return "ESP32"
        if "s3" in p:
            return "ESP32-S3"
        return "未知"

    # ESP 镜像头 chip_id → 芯片名（esptool/esptool 的 ESP_CHIP_ID_*）
    _CHIP_IDS = {0x0000: "ESP32", 0x0002: "ESP32-S2", 0x0005: "ESP32-C3",
                 0x0009: "ESP32-S3", 0x000C: "ESP32-C2", 0x000D: "ESP32-C6",
                 0x0010: "ESP32-H2", 0x0012: "ESP32-P4"}

    # 芯片名 → esptool --chip 参数
    _CHIP_ARGS = {"ESP32": "esp32", "ESP32-S2": "esp32s2", "ESP32-S3": "esp32s3",
                  "ESP32-C3": "esp32c3", "ESP32-C6": "esp32c6",
                  "ESP32-C2": "esp32c2", "ESP32-H2": "esp32h2",
                  "ESP32-P4": "esp32p4"}

    def _chip_from_image(self, path):
        """读固件镜像自带的芯片标识（ESP 镜像头里的 chip_id，偏移 12）。

        merged.bin 的 0x0 是 bootloader、0x10000 是 app，两段头里都有 chip_id，
        比按文件名猜可靠得多，也不用连设备。返回 (芯片名, 依据说明)。
        """
        try:
            if not path or not os.path.isfile(path):
                return "", ""
            with open(path, "rb") as fh:
                blob = fh.read(0x10018)
        except Exception:
            return "", ""
        for off, tag in ((0x10000, "镜像app段"), (0x0, "镜像头")):
            if len(blob) < off + 16:
                continue
            if blob[off] != 0xE9:          # ESP 镜像 magic
                continue
            cid = blob[off + 12] | (blob[off + 13] << 8)
            name = self._CHIP_IDS.get(cid)
            if name:
                return name, tag
        return "", ""

    def _chip_label(self, name, why=""):
        """写芯片栏：标签只放型号本体，依据走悬浮提示，避免撑长这一行。"""
        self._chip_name = name or ""
        self._chip_why = why or ""
        try:
            self.flash_chip_lbl.configure(text=name if name else "未知")
        except Exception:
            pass
        try:
            tip = ("当前芯片：%s\n识别依据：%s" % (name, why)) if name else \
                  ("点「检测芯片」用 esptool 直连读取型号。\n"
                   "未检测时会先按所选固件镜像头里的 chip_id 推断。")
            self._chip_tip.text = tip
        except Exception:
            pass

    def _update_chip(self):
        """芯片栏自动识别：优先读镜像头（准确），读不到再退回文件名，最后「未知」。

        识别不出来时不覆盖已有的 esptool 实测结果 —— 否则等比缩放整页重建、
        或固件路径暂时为空，都会把刚检测到的型号冲成「未知」。
        """
        try:
            path = self.flash_fw.get().strip()
            name, why = self._chip_from_image(path)
            if not name:
                guess = self._detect_chip(path)
                name = "" if guess == "未知" else guess
                why = "据文件名推断"
            if not name and getattr(self, "_chip_why", "") == "esptool 实测" \
                    and getattr(self, "_chip_name", ""):
                self._chip_label(self._chip_name, self._chip_why)
                return
            self._chip_label(name, why)
        except Exception:
            pass

    def _set_chip(self, name, why=""):
        """外部（esptool 实测）写回芯片型号。"""
        self._chip_label(name, why)

    def _chip_arg(self):
        name = getattr(self, "_chip_name", "") or self._detect_chip(
            self.flash_fw.get().strip())
        return self._CHIP_ARGS.get(name, "esp32s3")

    def _flash_browse(self):
        # 单击浏览：直接打开地址栏里当前显示的地址（文件夹或文件所在目录）
        cur = self.flash_fw.get().strip()
        if cur and os.path.isdir(cur):
            initialdir = cur
        elif cur and os.path.isfile(cur):
            initialdir = os.path.dirname(cur)
        else:
            initialdir = app_dir()
        p = filedialog.askopenfilename(
            title="选择固件 .bin", initialdir=initialdir,
            filetypes=[("固件镜像", "*.bin"), ("全部", "*.*")])
        if p:
            self.flash_fw.delete(0, "end")
            self.flash_fw.insert(0, p)
            self._update_chip()

    def _flash_detect_chip(self):
        """用 esptool 直连串口读芯片型号。

        USB 描述符只能看出是 Espressif 设备（303A:1001 覆盖 S3/C3/C6 等多款），
        分不出具体型号；要准确型号只能让芯片进下载模式问它本人——代价是复位一次、
        桥接中断几秒，所以做成手动按钮，并在结束后自动重连。
        """
        if getattr(self, "_chip_probing", False):
            return
        port = self.cbo_port.get().strip()
        if not port:
            messagebox.showwarning("未选端口", "先在顶部填入或扫描出串口。")
            return
        if not messagebox.askyesno(
                "检测芯片",
                "将用 esptool 直连 %s 读取芯片型号。\n"
                "程序会先发 1200bps touch 让芯片自动进下载模式（不用按 BOOT），\n"
                "期间串口被独占、遥控器桥接中断约 5~10 秒。\n"
                "检测完会自动找端口重连；若板子留在下载模式，拔插一次 USB 即可。\n\n"
                "继续？" % port):
            return
        reconnect = self.ser is not None
        if reconnect:
            self.close_port()
            time.sleep(0.4)
        self._chip_probing = True
        self._flash_print("\n=== 检测芯片：esptool %s（复位芯片）===\n" % port)
        threading.Thread(target=self._chip_probe_run,
                         args=(port, reconnect), daemon=True).start()

    # esptool 各版本报芯片型号的写法都不一样，逐个兜住：
    #   v5: "Chip type:          ESP32-S3 (QFN56) (revision v0.1)"
    #       "Detecting chip type... ESP32-S3" / "Connected to ESP32-S3 on COM6:"
    #   v4: "Chip is ESP32-S3 (revision 0.1)" / "Chip is ESP32-D0WDQ6 (revision 1.0)"
    _CHIP_PATS = (
        re.compile(r"^Chip type:\s+([A-Za-z0-9][\w\-]*)", re.M),
        re.compile(r"\bChip is\s+([A-Za-z0-9][\w\-]*)"),
        re.compile(r"Detecting chip type\.\.\.\s*([A-Za-z0-9][\w\-]*)"),
        re.compile(r"Connected to\s+([A-Za-z0-9][\w\-]*)\s+on\s+"),
    )

    @classmethod
    def _parse_chip(cls, txt):
        """从 esptool 输出里取芯片型号，取不到返回空串。"""
        for pat in cls._CHIP_PATS:
            m = pat.search(txt or "")
            if m:
                name = m.group(1).strip("-").upper()
                if name.startswith("ESP"):
                    return name
        return ""

    def _norm_chip(self, name):
        """把 ESP32-D0WDQ6 / ESP32-S3(QFN56) 之类归一成表里的规范名。"""
        name = (name or "").strip().upper()
        if not name:
            return ""
        if name in self._CHIP_ARGS:
            return name
        for known in sorted(self._CHIP_ARGS, key=len, reverse=True):
            if name.startswith(known):
                return known
        return "ESP32" if name.startswith("ESP32") else name

    @staticmethod
    def _esptool_fail_reason(txt):
        """把 esptool 的报错翻成一句人能直接照做的提示。"""
        t = (txt or "").lower()
        if "no serial data received" in t:
            return ("串口没有任何回应 —— 芯片没进下载模式（按住 BOOT 点一下 EN 再松手），"
                    "或该口是外置 USB 转串口、没有自动复位电路。")
        if ("access is denied" in t or "could not open port" in t
                or "permissionerror" in t):
            return "串口被占用 —— 先点「断开」，或关掉其它串口工具再试。"
        if "invalid choice" in t or "unrecognized" in t:
            return "esptool 版本太旧 —— 升级：pip install -U esptool"
        if "failed to connect" in t or "wrong boot mode" in t:
            return "握手失败 —— 手动进下载模式：按住 BOOT → 点 EN → 松开 BOOT。"
        return ""

    @staticmethod
    def _flash_fail_reason(txt):
        """把 esptool 刷写失败的报错翻成一句能直接照做的话。"""
        t = (txt or "").lower()
        if ("permissionerror" in t or "could not open port" in t
                or "port is busy" in t or "access is denied" in t):
            return ("端口被占用或不存在 —— 本程序若正连着会自动断开；仍报错就关掉"
                    "其它串口工具/残留进程，或点「扫端口」重新确认端口号。")
        if "no serial data received" in t:
            return ("芯片没有任何回应 —— 自动复位（1200bps touch）没生效。"
                    "手动进下载模式：按住 BOOT → 点一下 EN → 松开 BOOT，"
                    "再点「扫端口」确认新端口后重试。")
        if "failed to connect" in t or "wrong boot mode" in t:
            return ("连不上芯片 —— 手动进下载模式：按住 BOOT → 点 EN → 松开 BOOT。"
                    "另外该口若是固件自定义复合 USB 的 CDC 口，运行态刷不了。")
        if "does not fit" in t or "not enough space" in t or "out of bounds" in t:
            return "镜像超出 flash 范围 —— 检查芯片型号与 FlashSize 是否选对。"
        if "md5" in t or "checksum" in t or "verify failed" in t:
            return "写入校验失败 —— 多半是 USB 线/供电问题，换线或把波特率降到 460800 重试。"
        if "invalid choice" in t or "unrecognized" in t:
            return "esptool 版本不认这个命令 —— 升级：pip install -U esptool"
        return ""

    def _port_identity(self, dev):
        """返回该串口的身份（序列号 + 复合 USB 接口号 + 描述）。

        ESP32-S3 常把多个接口塞进一个复合 USB 设备，LOCATION 末尾的 :x.N
        就是接口号（N=0 即 PnP 里的 MI_00）。只有承载 USB-Serial-JTAG 的那个
        接口能被 esptool 下载，其余接口（固件自定义 CDC/HID）一律连不上，
        所以失败时把身份打出来，用户一眼能看出选错口了。
        """
        try:
            for i in self._list_ports():
                if i["dev"].upper() != (dev or "").strip().upper():
                    continue
                parts = []
                if i.get("ser"):
                    parts.append("序列号 %s" % i["ser"])
                m = re.search(r":x\.(\d+)$", i.get("loc") or "")
                if m:
                    parts.append("接口 MI_%02d" % int(m.group(1)))
                if i.get("desc"):
                    parts.append(i["desc"])
                return "，".join(parts)
        except Exception:
            pass
        return ""

    @staticmethod
    def _kill_quiet(proc):
        try:
            if proc.poll() is None:
                proc.kill()
        except Exception:
            pass

    def _chip_probe_run(self, port, reconnect):
        ui = self.ui_q          # 子线程一律走队列，直接碰 Tk 会崩
        chip, last = "", ""
        t0 = time.time()
        try:
            et = self._resolve_esptool()
            # 桥接板运行态是 TinyUSB 复合 USB，PID 也是 0x1001，esptool 会误判成
            # 硬件 USB-JTAG 口、发 JTAG 控制请求（TinyUSB 不响应）→ 永远连不上。
            # 先 1200bps touch 让它自己进 ROM 下载模式，端口号会变（号码不固定）。
            new_port = self._soft_enter_boot(port, ui)
            if new_port:
                port = new_port
                ui.put(lambda p=port: self._set_port(p))
            # v5 起 chip_id 改名 chip-id（旧名仅打 DEPRECATED 警告仍会跑），
            # 所以先试新名，报 invalid choice 才退回旧名。
            for sub in ("chip-id", "chip_id"):
                rc, txt = self._run_esptool(["--port", port, sub], et, ui, 75.0)
                last = txt
                chip = self._parse_chip(last)
                if chip or rc == -1:
                    break
                if not re.search(r"invalid choice|unrecognized|unknown command|no such",
                                 last, re.I):
                    break        # 命令本身有效，只是没连上，别再用旧名重试一遍
        except Exception as e:
            ui.put(lambda: self._flash_print("[检测异常] %s\n" % e))
        finally:
            self._chip_probing = False
        if chip:
            name = self._norm_chip(chip)
            ui.put(lambda: self._set_chip(name, "esptool 实测"))
            ui.put(lambda: self._flash_print("=== 芯片：%s ===\n" % name))
        else:
            why = self._esptool_fail_reason(last)
            if not last.strip():
                why = why or "esptool 没有任何输出。"
            if time.time() - t0 >= 70:
                why = (why + " " if why else "") + "（等待超时，已强制结束）"
            ident = self._port_identity(port)
            ui.put(lambda: self._flash_print(
                "[检测失败] 没读到芯片型号。%s\n" % (why or "")))
            if ident:
                ui.put(lambda: self._flash_print("  %s 身份：%s\n" % (port, ident)))
            ui.put(lambda: self._flash_print(
                "  自动复位（1200bps touch）没能让芯片进下载模式。\n"
                "  手动兜底：按住 BOOT → 点一下 EN → 松开 BOOT，再点「扫端口」后重试。\n"))
            ui.put(lambda: self._flash_print(
                "  提示：芯片栏已保留固件镜像里读到的型号，不影响按镜像刷写。\n"))
        # 检测完芯片大概率还停在下载模式（这块板软复位回不到 app），
        # 统一交给 _after_flash_reconnect 判断该重连还是该提示拔插。
        ui.put(lambda: self.after(800, self._after_flash_reconnect))

    def _flash_insert(self, text, tag=None):
        try:
            w = self.flash_log
            w.configure(state="normal")
            if tag:
                w.insert("end", text, tag)
            else:
                w.insert("end", text)
            # 串口输出会一直往这个框里灌，加个行数上限，否则跑一夜界面会卡死
            self._flash_n = getattr(self, "_flash_n", 0) + text.count("\n")
            if self._flash_n > 4500:
                try:
                    n = int(w.index("end-1c").split(".")[0])
                except Exception:
                    n = 0
                if n > 3800:
                    w.delete("1.0", "%d.0" % (n - 3000))
                    self._flash_n = 3000
            w.configure(state="disabled")
            if self.flash_autoscroll.get():
                w.see("end")
        except Exception:
            pass

    def _flash_rx(self, line):
        """把串口收到的一行实时镜像到页4 日志（按键报告 # KEY / 状态 OK / 映射 M 全都在）。

        以前 `_handle` 对 `#` 开头的行直接 return，而固件每次按键正是打
        `# KEY 0046 DOWN src=map usage=0x046 map=yes` —— 于是页4 一行实时输出都没有。
        `_handle` 由主线程的 `_drain` 调用，这里可以直接碰 Tk。
        音频行（`A `）在 `_reader` 里已旁路，不会进来刷屏。
        """
        try:
            if not line:
                return
            ts = time.strftime("%H:%M:%S") + (".%03d" % (int(time.time() * 1000) % 1000))
            tag = None
            if line.startswith("# KEY ") or line.startswith("L "):
                tag = "key"          # 按键响应，最常看的一行
            elif "ERROR" in line or line.startswith("[刷写失败]"):
                tag = "err"
            self._flash_insert("[%s] %s\n" % (ts, line), tag)
        except Exception:
            pass

    def _flash_print(self, s):
        self._flash_insert(s)

    def _flash_append(self, line):
        self.after(0, lambda: self._flash_insert(line + "\n"))

    def _flash_toggle_wrap(self):
        self.flash_log.configure(wrap=("word" if self.flash_wrap.get() else "none"))

    def _flash_clear(self):
        self.flash_log.configure(state="normal")
        self.flash_log.delete("1.0", "end")
        self.flash_log.configure(state="disabled")
        self._flash_n = 0

    def _flash_save(self):
        p = filedialog.asksaveasfilename(
            defaultextension=".log",
            filetypes=[("日志", "*.log"), ("文本", "*.txt"), ("全部", "*.*")],
            title="保存刷写日志")
        if not p:
            return
        try:
            txt = self.flash_log.get("1.0", "end")
            with open(p, "w", encoding="utf-8") as fh:
                fh.write(txt)
            self._flash_print("[日志已保存] %s\n" % p)
        except Exception as e:
            self._flash_print("[保存失败] %s\n" % e)

    def _resolve_esptool(self):
        """找**外部** esptool 程序：`PATH` → 程序目录 → 常见 pip 安装位置。

        返回命令前缀（`[exe]`），找不到返回 `[]` —— 空列表表示改用**进程内**
        esptool（打包进 exe 的那份），而不是拿 `sys.executable` 去当 Python 使。
        这一点是 1.3.0 的翻车点：单文件 exe 下 `sys.executable` 就是它自己，
        `[sys.executable, "-m", "esptool"]` 等于**再开一个上位机窗口**，
        既不刷机也不退出，只能等超时被杀。目标机上没装 esptool 时 100% 触发。

        只缓存真正找到文件的结果，用户中途安装后下次点击仍能重新发现。
        """
        cached = getattr(self, "_esptool_cached", None)
        if cached:
            return list(cached)
        # 排障用：强制走内嵌那条路（目标机没装 esptool 时就是这条路）
        if os.environ.get("CMCC_NO_EXT_ESPTOOL"):
            self._esptool_cached = []
            return []
        for name in ("esptool", "esptool.exe", "esptool.py"):
            pp = shutil.which(name)
            if pp:
                self._esptool_cached = [pp]
                return [pp]
        home = os.path.expanduser("~")
        local = os.environ.get("LOCALAPPDATA", "")
        roaming = os.environ.get("APPDATA", "")
        fixed = [
            os.path.join(app_dir(), "esptool.exe"),
            os.path.join(app_dir(), "esptool", "esptool.exe"),
            os.path.join(app_dir(), "venv", "Scripts", "esptool.exe"),
            os.path.join(home, "esptool", "venv", "Scripts", "esptool.exe"),
            os.path.join(local, "Programs", "esptool", "esptool.exe"),
        ]
        for root in (os.path.join(local, "Programs", "Python"),
                     os.path.join(roaming, "Python")):
            try:
                for d in sorted(os.listdir(root), reverse=True):
                    fixed.append(os.path.join(root, d, "Scripts", "esptool.exe"))
            except Exception:
                pass
        for p in fixed:
            try:
                if p and os.path.isfile(p):
                    self._esptool_cached = [p]
                    return [p]
            except Exception:
                continue
        # 没装外部 esptool：走进程内。只有「跑源码 + 当前解释器确实装了 esptool」
        # 这一种情况才用 -m，且必须不是 frozen（打包后 sys.executable 是 exe 自己）。
        if not getattr(sys, "frozen", False):
            try:
                import importlib.util
                if importlib.util.find_spec("esptool") is not None:
                    self._esptool_cached = [sys.executable, "-m", "esptool"]
                    return list(self._esptool_cached)
            except Exception:
                pass
        self._esptool_cached = []
        return []

    def _esptool_label(self, et):
        """日志里显示用的 esptool 名字。"""
        if et:
            return et[0]
        try:
            import esptool as _e
            return "内嵌 esptool %s" % getattr(_e, "__version__", "?")
        except Exception:
            return "内嵌 esptool"

    def _esptool_cmd(self, chip, port, baud, fw, before=None):
        """拼刷写参数 —— **返回的 argv 不含程序名**，程序前缀由 `_run_esptool` 统一加。

        1.3.3 的坑（2026-11-06 实测暴露）：这里原本是 `list(et) + [...]`，
        而 `_run_esptool` 里又 `list(et) + list(argv)` 拼了一次，于是装了外部 esptool
        的机器上实际执行的是 `esptool.EXE esptool.EXE --chip ...`，esptool 把第一个
        参数当成子命令，直接报 `No such command ...`、返回码 2。
        没装外部 esptool 的机器 `et` 为空，两处都拼不出东西 → 一直正常，
        这就是它为什么拖到 1.3.3 才暴露（1.3.1/1.3.3 的实测机都没装外部 esptool）。

        1.3.0 曾对「已进下载模式」的芯片强制 `--before no_reset`，实测反而是错的：
        这块板进下载模式后是 USB-Serial/JTAG 口，esptool 自己的 `USBJTAGSerialReset`
        是有效的（0.6 秒连上），而 no_reset 跳过握手直接发命令 → `Write timeout`。
        """
        cmd = ["--chip", chip, "--port", port, "--baud", baud]
        if before:
            cmd += ["--before", before]
        return cmd + ["write_flash", "0x0", fw]

    @staticmethod
    def _loc_key(info):
        """USB 物理位置前缀，如 '1-11:x.1' → '1-11'。同一根线重新枚举后前缀不变。"""
        return ((info or {}).get("loc") or "").split(":")[0]

    @staticmethod
    def _loc_full(info):
        """完整 USB 位置，如 '1-11:x.1'。末尾数字是复合设备的接口号：
        运行态 CDC 在 MI_01（.1），进 ROM 后 USB-Serial/JTAG 在 MI_00（.0）。
        所以「接口号变了」本身就是「换了模式」的硬证据。"""
        return (info or {}).get("loc") or ""

    @staticmethod
    def _touch_once(port):
        """发一次 1200bps touch。返回 True=已发出，None=端口打不开。"""
        try:
            s = serial.Serial(port, 115200, timeout=0.2)
        except Exception:
            return None
        try:
            try:
                s.write(b"\r\n")   # 先通一次数据，确认 CDC 端点真的开了
                s.flush()
            except Exception:
                pass
            time.sleep(0.35)
            s.baudrate = 1200      # ← 触发 usb_persist_restart(RESTART_BOOTLOADER)
        except Exception:
            pass                   # 芯片此刻断开重连，报 OSError 22 属正常
        try:
            time.sleep(0.25)
        except Exception:
            pass
        try:
            s.close()
        except Exception:
            pass
        return True

    def _soft_enter_boot(self, port, ui, tries=2):
        """用 1200bps touch 让固件自己重启进 ROM 下载模式 —— 等效按 BOOT 键。

        依据 `cores/esp32/USBCDC.cpp:283` 的 `_onLineCoding`：
            if (reboot_enable && _bit_rate == 1200) usb_persist_restart(RESTART_BOOTLOADER);
        内部把 `RTC_CNTL_OPTION1_REG` 置 `FORCE_DOWNLOAD_BOOT`，下次复位直接进 ROM。
        Arduino IDE 上传就是靠这条机制（注释里写着 "ArduinoIDE sends LineCoding
        with 1200bps baud to reset the device"）。

        **实测结论（2026-11-04）**：DTR/RTS 四步序列在这块板上完全无效（信号线
        回读正确、固件毫无反应）；1200bps 才有效，且设备会重新枚举。

        **口号不能假设**：下载模式叫 COM 几完全由 Windows 决定，本机是 11→14，
        换台电脑可能是 7→3。所以一律按「USB 物理位置 + 接口号变化 + 新出现的
        乐鑫口」来认，绝不写死号段，见 `_wait_boot_port`。

        返回下载模式下的端口号；没切成功返回 None（调用方退回 esptool 默认复位）。
        """
        before = self._list_ports()
        old = {i["dev"] for i in before}
        orig = next((i for i in before if i["dev"] == port), None)
        want_loc = self._loc_key(orig) if orig else ""
        orig_loc = self._loc_full(orig) if orig else ""

        for attempt in range(1, tries + 1):
            if attempt > 1:
                # 上一轮可能把口号换掉了，先按位置重新认一下再 touch
                now = self._list_ports()
                if port not in {i["dev"] for i in now}:
                    cand = [i for i in now if i["esp"]
                            and (not want_loc or self._loc_key(i) == want_loc)]
                    if cand:
                        port = cand[0]["dev"]
                ui.put(lambda a=attempt: self._flash_print(
                    "[软复位] 第 %d 次尝试重新复位…\n" % a))
                time.sleep(0.8)
            if self._touch_once(port) is None:
                ui.put(lambda p=port: self._flash_print(
                    "[软复位] 打不开 %s，改用 esptool 自带复位\n" % p))
                return None
            ui.put(lambda a=attempt: self._flash_print(
                "[软复位] 第 %d 次 1200bps touch 已发出，等端口重新枚举…\n" % a))

            newp = self._wait_boot_port(port, old, want_loc, orig_loc)
            if newp:
                ui.put(lambda o=port, n=newp: self._flash_print(
                    "[软复位] 已进下载模式：%s → %s（口号是 Windows 分的，不固定）\n"
                    % (o, n)))
                return newp
            ui.put(lambda: self._flash_print(
                "[软复位] 芯片重启了但没进下载模式（接口号没变，还在 app 里）\n"))
        ui.put(lambda: self._flash_print(
            "[软复位] 多次尝试仍未进下载模式，改用 esptool 自带复位\n"))
        return None

    def _wait_boot_port(self, port, old, want_loc, orig_loc, timeout=15.0):
        """等下载模式的端口，最多 timeout 秒；认不出来返回 None。

        判据（任一成立即算切过去了，不要求原口号先消失 —— 换台电脑时端口
        消失/重现的时序不一定能被 250ms 的轮询抓到）：
          ① 原口号还在，但**接口号变了**（`1-11:x.1` CDC → `1-11:x.0` USB-Serial/JTAG）
          ② 同一 USB 位置（`1-11`）上出现了**另一个**口号
          ③ 原口号已经消失，且冒出一个之前没见过的乐鑫口
        反例：原口号还在、接口号也没变 → 只是普通重启回 app，不算成功。

        ③ 必须挂在「原口号已消失」之下，否则插着两块 S3 时会连到别人板上。
        """
        t0 = time.time()
        gone = False
        while time.time() - t0 < timeout:
            now = self._list_ports()
            devs = {i["dev"] for i in now}
            cur = next((i for i in now if i["dev"] == port), None)
            if cur is None:
                gone = True
            elif orig_loc and self._loc_full(cur) != orig_loc:
                return port                      # ① 同口号、换了模式
            for i in now:
                if not i["esp"]:
                    continue
                if (want_loc and i["dev"] != port
                        and self._loc_key(i) == want_loc):
                    return i["dev"]              # ② 同位置新口号
                if gone and i["dev"] not in old:
                    return i["dev"]              # ③ 原口没了 + 新冒出的乐鑫口
            time.sleep(0.25)

        # 兜底：原口号确实没了，全场只剩一个乐鑫口 —— 且它要么是新冒出来的、
        # 要么在原 USB 位置上，才认。原口号自己回来但接口号没变时**不能认**，
        # 那只是重启回 app（① 已判过），认了就会拿一个还在 app 的口去刷。
        esp = [i for i in self._list_ports() if i["esp"]]
        if gone and len(esp) == 1:
            only = esp[0]
            if only["dev"] == port:
                if orig_loc and self._loc_full(only) != orig_loc:
                    return port
            elif only["dev"] not in old or (want_loc and self._loc_key(only) == want_loc):
                return only["dev"]
        return None

    def _flash_start(self):
        if getattr(self, "_flash_running", False):
            self._flash_print("[提示] 上一次刷写尚未结束。\n")
            return
        fw = self.flash_fw.get().strip()
        if not fw or not os.path.isfile(fw):
            messagebox.showerror("固件缺失", "请先选择固件 .bin 文件（含 merged.bin）。")
            return
        port = self.cbo_port.get().strip()
        if not port:
            messagebox.showwarning("未选端口",
                                   "先在顶部连接 ESP32 桥的串口，或手动在顶部填入端口。")
            return
        # 这些都要在主线程读（Tk 变量不能在子线程碰）
        et = self._resolve_esptool()
        chip = self._chip_arg()
        baud = self.flash_baud.get().strip()
        # 本程序自己开着串口时 esptool 必然打不开（PermissionError 13），
        # 必须先断开；端口句柄在 Windows 上释放略滞后，留一点时间给它。
        reconnect = self.ser is not None
        if reconnect:
            self.close_port()
            self._flash_print("[提示] 已断开上位机串口，交给 esptool 独占。\n")
            time.sleep(0.4)
        base = [i["dev"] for i in self._list_ports()]
        self._flash_running = True
        threading.Thread(target=self._flash_run,
                         args=(et, chip, port, baud, fw, reconnect, base),
                         daemon=True).start()

    def _exec_esptool(self, cmd, ui, flag, timeout=900.0):
        """跑一次**外部** esptool 程序，边跑边把输出推到日志。返回 (返回码, 全部输出)。

        返回码 -1 表示进程压根没起来（找不到 esptool / 启动异常），
        调用方据此区分「连不上芯片」和「环境没装好」。
        """
        try:
            proc = subprocess.Popen(cmd, stdout=subprocess.PIPE,
                                    stderr=subprocess.STDOUT, text=True,
                                    bufsize=1, creationflags=flag)
        except FileNotFoundError:
            ui.put(lambda: self._flash_print(
                "[错误] 找不到 esptool。请先安装：pip install -U esptool\n"))
            return (-1, "FileNotFoundError")
        except Exception as e:
            ui.put(lambda: self._flash_print("[启动失败] %s\n" % e))
            return (-1, str(e))
        out = []
        rc = None
        # 4MB @921600 正常 1~2 分钟，给 15 分钟兜底，避免卡死后再也点不动按钮
        killer = threading.Timer(timeout, self._kill_quiet, args=(proc,))
        killer.daemon = True
        killer.start()
        try:
            if proc.stdout:
                for line in proc.stdout:
                    out.append(line)
                    ui.put(lambda l=line: self._flash_insert(l.rstrip("\r\n") + "\n"))
            rc = proc.wait()
        except Exception:
            pass
        finally:
            killer.cancel()
        return (rc, "".join(out))

    def _run_esptool(self, argv, et, ui, timeout=900.0):
        """跑 esptool 的统一入口：有外部程序用外部，没有就用包里内嵌的那份。

        `argv` **不含程序名**（程序前缀在这里加），两条路返回同样的 `(返回码, 全部输出)`。
        """
        # 日志里把真正用的那个程序打出来：排障时一眼能看出走的是外部还是内嵌，
        # 1.3.3 那次「No such command」就是靠这行路径认出来的。
        ui.put(lambda a=argv, e=et: self._flash_print(
            ">>> " + (e[0] if e else "esptool(内嵌)") + " " + " ".join(a) + "\n"))
        if et:
            return self._exec_esptool(list(et) + list(argv), ui,
                                      getattr(subprocess, "CREATE_NO_WINDOW", 0),
                                      timeout)
        return self._exec_esptool_inproc(argv, ui, timeout)

    def _exec_esptool_inproc(self, argv, ui, timeout=900.0):
        """在本进程里跑内嵌的 esptool（目标机没装 Python / esptool 时走这条）。

        为什么不用子进程：单文件 exe 的 `sys.executable` 就是它自己，
        1.3.0 用 `[sys.executable, "-m", "esptool"]` 的结果是**又开一个上位机**——
        既不刷机也不退出，只能等 75 秒超时。这是 1.3.0 刷不了固件的直接原因。

        改成 import 进来调 `esptool._main()`（CLI 的真正入口，内部自己捕获异常、
        打印 "A fatal error occurred"、再 sys.exit）。注意不能用 `esptool.main()`：
        那个是库入口，`SerialTimeoutException` 会直接抛出来。

        esptool 是 GPL-2.0，随本程序分发后整个 exe 受 GPL-2.0 约束，见 docs/third-party.md。
        """
        try:
            import esptool as _esp
        except Exception as e:      # noqa: BLE001
            ui.put(lambda: self._flash_print(
                "[错误] 没有可用的 esptool：外部未安装，内嵌版也没打进包里。\n"
                "  修复：在装有 Python 的机器上 pip install -U esptool，"
                "或用带内嵌 esptool 的发布版。\n  (%s)\n" % e))
            return (-1, "no esptool")

        # 进程内的任务没法强杀：上一次超时放弃后它可能还在跑，再开一个会抢串口
        prev = getattr(self, "_et_thread", None)
        if prev is not None and prev.is_alive():
            ui.put(lambda: self._flash_print(
                "[提示] 上一次 esptool 还没结束（进程内无法中断），等它跑完再试。\n"))
            return (-1, "busy")

        class _W(io.TextIOBase):
            """收 esptool 的所有输出。rich 检测到 isatty=False 不会上色。"""

            def __init__(self):
                self.buf = ""
                self.lines = []

            def write(self, s):
                try:
                    self.buf += s
                    while "\n" in self.buf:
                        ln, self.buf = self.buf.split("\n", 1)
                        self.lines.append(ln)
                except Exception:
                    pass
                return len(s)

            def flush(self):
                try:
                    if self.buf:
                        self.lines.append(self.buf)
                        self.buf = ""
                except Exception:
                    pass

            def isatty(self):
                return False

            @property
            def encoding(self):
                return "utf-8"

            @property
            def errors(self):
                return "replace"

        w = _W()
        box = {}

        def _job():
            oo, oe, oa = sys.stdout, sys.stderr, sys.argv
            sys.stdout = sys.stderr = w
            sys.argv = ["esptool"] + list(argv)
            try:
                _esp._main()
                box["rc"] = 0
            except SystemExit as e:
                box["rc"] = (e.code if isinstance(e.code, int)
                             else (0 if e.code is None else 1))
            except BaseException as e:      # noqa: BLE001
                box["rc"] = 1
                try:
                    w.write("\n[异常] %s: %s\n" % (type(e).__name__, e))
                except Exception:
                    pass
            finally:
                try:
                    w.flush()
                except Exception:
                    pass
                sys.stdout, sys.stderr, sys.argv = oo, oe, oa

        th = threading.Thread(target=_job, daemon=True)
        self._et_thread = th
        th.start()
        idx = 0
        t0 = time.time()
        while True:
            while idx < len(w.lines):
                line = w.lines[idx]
                idx += 1
                ui.put(lambda l=line: self._flash_insert(l + "\n"))
            if not th.is_alive():
                w.flush()          # 把没换行结尾的最后一行也吐出来
                if idx >= len(w.lines):
                    break
            if time.time() - t0 > timeout:
                # 进程内没法强杀，只能不再等：esptool 自己的串口超时会给它收尾
                ui.put(lambda t=int(timeout): self._flash_print(
                    "[超时] esptool 已跑 %d 秒，先不等了（进程内无法强制结束）\n" % t))
                break
            time.sleep(0.05)
        return (box.get("rc", -1), "\n".join(w.lines))

    def _flash_run(self, et, chip, port, baud, fw, reconnect, base):
        ui = self.ui_q          # 子线程一律走队列，直接碰 Tk 会崩

        # ① 先让固件自己重启进 ROM 下载模式。
        #    桥接固件是 TinyUSB 复合 USB，PID 也是 0x1001，esptool 会误判成
        #    硬件 USB-JTAG 口、用 JTAG 控制请求去复位（TinyUSB 不响应），
        #    结果就是 "No serial data received"、只能手按 BOOT。
        new_port = self._soft_enter_boot(port, ui)
        if new_port:
            if new_port != port:
                port = new_port
                ui.put(lambda p=port: self._set_port(p))
            ui.put(lambda p=port: self._flash_print(
                "[刷写] 端口 %s，开始写入（无需按 BOOT）\n" % p))
        else:
            ui.put(lambda p=port: self._flash_print(
                "[刷写] 软复位没成功，仍用 %s 试一次 esptool 自带复位\n" % p))
        # 进下载模式后是 USB-Serial/JTAG 口，esptool 自己的 USBJTAGSerialReset
        # 有效（实测 0.6 秒连上）；反而 `--before no_reset` 会跳过握手 → Write timeout。
        argv = self._esptool_cmd(chip, port, baud, fw)
        rc, txt = self._run_esptool(argv, et, ui, 900.0)

        # 兜底重试：软复位成功、esptool 却仍连不上——多半是端口刚枚举出来
        # 还没就绪，或 Windows 竞态临时给了个旧口。再切一次模式重来。
        low = txt.lower()
        if (rc not in (0, -1)
                and ("no serial data" in low or "failed to connect" in low
                     or "write timeout" in low or "wrong boot mode" in low)):
            ui.put(lambda: self._flash_print(
                "[重试] 第一次没连上，重新复位一次再试…\n"))
            time.sleep(1.0)
            again = self._soft_enter_boot(port, ui)
            port = again or port
            ui.put(lambda p=port: self._set_port(p))
            argv = self._esptool_cmd(chip, port, baud, fw)
            rc, txt = self._run_esptool(argv, et, ui, 900.0)

        self._flash_running = False
        ui.put(lambda: self._flash_print(
            "--- 刷写进程结束，返回码 %s ---\n" % rc))
        if rc == 0:
            # 刷 merged.bin 从 0x0 写会把 NVS 清成 0xFF，设备回默认映射，
            # 而界面显示的是 remotekey.ini —— 两边不一致，用户按的键就不是他调好的。
            # 连回去之后自动把 ini 写回设备，见 _push_after_flash_run。
            self._push_after_flash = True
            ui.put(lambda: self._flash_print("=== 刷写成功 ===\n"))
        else:
            why = self._flash_fail_reason(txt)
            if why:
                ui.put(lambda: self._flash_print("[刷写失败] %s\n" % why))
        # 刷完芯片未必回得到 app（这块板的 USB-JTAG 不吃软复位），交给
        # _after_flash_reconnect 判断：回到 app 就重连，还停在下载模式就提示拔插。
        ui.put(lambda: self.after(900, self._after_flash_reconnect))

    @staticmethod
    def _is_app_port(info):
        """这个口是不是「固件在跑」的运行态口。

        桥接固件运行态是 TinyUSB CDC，出现在复合设备的接口 1（`1-11:x.1`）；
        ROM 下载模式是 USB-Serial/JTAG，接口 0（`1-11:x.0`）。拿不到 location
        时不敢下结论，按 app 口处理，交给后面的 INFO 握手去证伪。
        """
        loc = (info or {}).get("loc") or ""
        if not loc:
            return True
        return not loc.endswith(":x.0")

    def _after_flash_reconnect(self, tries=0):
        """刷写/检测结束：等芯片切回运行态再连，等不到才提示拔插。

        这块板（USB-Serial/JTAG）不吃软件复位，但**过几秒会自己切回 app**
        （实测 chip-id 后约 2–6 秒端口从 `x.0` 变回 `x.1`）。所以不急着下结论，
        先每 0.5 秒看一次，最多 12 秒。
        """
        infos = self._list_ports()
        app = [i for i in infos if i["esp"] and self._is_app_port(i)]
        if app:
            try:
                self.refresh_ports(force=True)
            except Exception:
                pass
            if self.cbo_port.get().strip() != app[0]["dev"]:
                self._set_port(app[0]["dev"])
            self.toggle_conn()
            if self._push_after_flash:
                # 连接时会先发 INFO / LIST，等它们走完再推映射，别抢串口
                self.after(1600, self._push_after_flash_run)
            return
        if tries < 24:                      # 12 秒
            if tries == 0:
                self._flash_print("[连接] 等芯片从下载模式切回固件…\n")
            self.after(500, lambda: self._after_flash_reconnect(tries + 1))
            return
        try:
            self.refresh_ports(force=True)
        except Exception:
            pass
        boot = [i for i in infos if i["esp"]]
        if self._push_after_flash:
            # 重连都没成功，就别挂着这个标志了，如实告诉用户要手工补一次
            self._push_after_flash = False
            self._flash_print(
                "[映射] 这次没能自动写回（还没连上），连上后点一次「保存到设备」。\n")
        if boot:
            self._flash_print(
                "[提示] 等了 12 秒，板子还停在下载模式（%s）。\n"
                "  请拔插一次 USB（或按一下 EN），再点「扫端口」就能连上。\n"
                % boot[0]["dev"])
        else:
            self._flash_print(
                "[提示] 没扫到 ESP32。拔插一次 USB 后点「扫端口」。\n")

    def _push_after_flash_run(self, tries=0):
        """刷写成功后，把 remotekey.ini 里整套映射写回设备并写闪存。

        刷 merged.bin 是从 0x0 整片写的，NVS 区被清成 0xFF，固件起来是出厂
        默认映射；而上位机界面显示的是本地 ini。两边一对不上，用户按遥控器
        就不是他调好的键位，得手动进「按键映射」页点一次「保存到设备」才恢复。
        这里刷完自动做掉。

        只由刷写成功（返回码 0）触发；检测芯片不碰。
        """
        if not self._push_after_flash:
            return
        # 连上之后 _hw_refresh 会连发 STAT/TARGET/INFO/REPORTS/MAPDUMP/BATT/SCANDUMP，
        # 每条间隔 1 秒、总共 7 秒。这期间往里插 SET 会把抓取缓冲搅乱，
        # 也可能让 MAPDUMP 的长输出和 OK SET 混在一起，所以等它抓完再推。
        if self._cap_active and tries < 40:          # 最多再等 20 秒
            self.after(500, lambda: self._push_after_flash_run(tries + 1))
            return
        self._push_after_flash = False
        if self.ser is None:
            self._flash_print("[映射] 没连上设备，跳过自动写回。\n")
            return

        # ini 优先；本地没有 ini（或一条都没解析出来）时用内置默认表兜底。
        # 内置表与固件 DEFAULTS 是同一套键位，所以两条路殊途同归：
        # 有 ini 就恢复你调好的，没 ini 就落回出厂默认，都不会出现「空表推下去」。
        table, source = self.ini, INI_NAME
        if not table:
            table = {n: (ot, k, c) for n, ot, k, c in default_items()}
            source = "内置默认表"

        items = []
        pushed = 0
        for label, kid, _, _ in REMOTE:
            r = table.get(label)
            if r is None:
                continue
            ot, keys, n = r
            items.append((label, ot, keys, n))
            if not kid:              # TV 是红外键，只记 ini，不下发
                continue
            pushed += 1
            if n == 0:
                self.send("SET %04X 1 0" % kid)
            else:
                arg = " ".join("%02X" % keys[i] for i in range(n))
                self.send("SET %04X %d %d %s" % (kid, ot, n, arg))

        if not items:
            self._flash_print(
                "[映射] 既没有 %s、内置默认表也是空的，跳过写回。\n" % INI_NAME)
            return

        self._flash_print(
            "[映射] 正在把 %d 条映射写回设备（来源：%s）…\n" % (pushed, source))

        def do_save():
            self.send("SAVE")
            self.changed.clear()
            self._refresh_all()
            self._mark_dirty()
            self.lbl_status.configure(
                text="已恢复 %d 条映射" % pushed, fg=ACCENT)
            self._flash_print(
                "[映射] 已写回并保存 %d 条，按键立即生效\n" % pushed)

        self.after(400, do_save)     # 等上面那串 SET 走完再发 SAVE

    def _build(self, fix_size=False):
        # 已废弃：UI 改为左侧栏 + 四页（_build_shell / _build_pages / _show_page）。
        # 保留签名以避免历史引用报错，实际界面由上面的分页方法构建。
        pass

    def _pick_note(self, cands):
        """挑一条能在当前窗口宽度里放得下的说明文字。

        字号按 zp() 取整，窗口越小字相对越宽，固定一条会在右边缘被切掉。
        """
        avail = WIN_W - (zp(14) + zp(22) + zp(8)) - zp(10)
        try:
            f = tkfont.Font(family="Microsoft YaHei UI", size=zp(9))
            for s in cands:
                if f.measure(s) <= avail:
                    return s
        except Exception:
            pass
        return cands[-1]

    def _win_w(self):
        return WIN_W

    def _win_h(self):
        return WIN_H

    def _draw_background(self):
        """背景：按当前 Z 从原图重绘，保证照片永远和引线框对得上。

        以前贴的是预先烤好的 bg.png（尺寸死锁在 Z=1.42），窗口一缩放，
        照片尺寸不变而引线框在缩，整屏就错位了。
        """
        self.bg_img = None
        if self._draw_bg_scaled():
            return
        # 退化路径：没有 Pillow 或原图缺失时沿用旧 bg.png（缩放后可能对不齐）
        path = self._asset("bg.png")
        if path and os.path.exists(path):
            try:
                self.bg_img = tk.PhotoImage(file=path)
                self.canvas.create_image(WIN_W // 2, WIN_H // 2, image=self.bg_img)
                return
            except Exception:
                self.bg_img = None
        self.canvas.configure(bg=BG)

    def _draw_bg_scaled(self):
        """用 Pillow 把 remote.jpg 按当前窗口尺寸裁切 + 缩放后画上去。"""
        src = self._asset("remote.jpg")
        if not src or IMG_W <= 0 or IMG_H <= 0:
            return False
        try:
            from PIL import Image, ImageTk
        except Exception:
            return False
        try:
            im = Image.open(src).convert("RGB")
            im = im.crop((CROP_X0, 0, CROP_X1, im.size[1]))
            im = im.resize((IMG_W, IMG_H), Image.LANCZOS)
            bg = Image.new("RGB", (WIN_W, WIN_H), (255, 255, 255))
            bg.paste(im, ((WIN_W - IMG_W) // 2, PAD_Y))
            self.bg_img = ImageTk.PhotoImage(bg)
            self.canvas.create_image(0, 0, anchor="nw", image=self.bg_img)
            return True
        except Exception:
            self.bg_img = None
            return False

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
        dot = self.canvas.create_oval(ax - zp(2.5), ky - zp(2.5), ax + zp(2.5), ky + zp(2.5),
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
            rect=rect, name=name, value=val, line=line, dot=dot,
            kid=kid, label=label, x1=x1, y1=y1, x2=x2, y2=y2, na=na,
            lock=False)

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
            if c["na"]:
                self.canvas.configure(cursor="arrow")
                self.lbl_hint.configure(text="TV 键实测不走蓝牙（红外直发），改不了")
            elif c.get("lock"):
                self.canvas.configure(cursor="arrow")
                self.lbl_hint.configure(
                    text="语音直出已开启，麦克风键已锁定；取消勾选后恢复原绑定")
            else:
                self._paint(k, True)
                self.canvas.configure(cursor="hand2")
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
        if c.get("lock"):
            # 勾了"语音键录音并直接输出文字"：禁止改键，也禁止弹改键小窗
            self.lbl_hint.configure(
                text="语音直出已开启，麦克风键已锁定；取消勾选后恢复原绑定")
            return
        kid = c["kid"]
        Recorder(self, "改键：%s" % c["label"], self.bindings.get(kid), 
                 lambda t, ks, n: self._apply(kid, t, ks, n))

    def _paint(self, k, hot):
        c = self.cards[k]
        if c["na"]:
            return
        if c.get("lock"):
            self._paint_lock(c)
            return
        self.canvas.itemconfigure(c["rect"], fill=(CARD_HOVER if hot else CARD),
                                  outline=(BORDER_HOT if (hot or c["kid"] in self.changed) else BORDER),
                                  width=(2 if (hot or c["kid"] in self.changed) else 1))
        self.canvas.itemconfigure(c["line"], fill=(LEAD_HOT if hot else LEAD))

    def _paint_lock(self, c):
        """锁定态：整块置灰，看起来就是"点不了"。"""
        canvas = self.canvas
        canvas.itemconfigure(c["rect"], fill=CARD_LOCK, outline=BORDER_NA, width=1)
        canvas.itemconfigure(c["name"], fill=BORDER_NA)
        canvas.itemconfigure(c["value"], fill=BORDER_NA)
        canvas.itemconfigure(c["line"], fill=BORDER_NA)
        if c.get("dot"):
            canvas.itemconfigure(c["dot"], fill=BORDER_NA)

    def _paint_unlock(self, c):
        """解除锁定：颜色还原。"""
        canvas = self.canvas
        canvas.itemconfigure(c["rect"], fill=CARD, outline=BORDER, width=1)
        canvas.itemconfigure(c["name"], fill=TEXT_DIM)
        canvas.itemconfigure(c["value"], fill=TEXT)
        canvas.itemconfigure(c["line"], fill=LEAD)
        if c.get("dot"):
            canvas.itemconfigure(c["dot"], fill=LEAD)

    def _sync_asr_lock(self):
        """语音直出开着 -> 锁死麦克风键；关掉 -> 还原原绑定功能。"""
        c = self.cards.get(MIC_KID)
        if not c:
            return
        c["lock"] = bool(self.asr_on)
        if c["lock"]:
            self._paint_lock(c)
        else:
            self._paint_unlock(c)
        self._refresh_card(MIC_KID)
        # 鼠标正停在这块上时，光标也要跟着变
        if self.hover == MIC_KID:
            self.canvas.configure(cursor=("arrow" if c["lock"] else "hand2"))

    # ------------------------------------------------------------- 串口 --
    def _list_ports(self):
        """枚举串口，带上「是不是乐鑫芯片」「USB 序列号」两个判据。

        注意 VID:PID=303A:1001 是 ESP32-S3 原生 USB-Serial-JTAG 的口，
        S3 / C3 / C6 全都这个号，**光看 VID 分不出是哪块板**，
        所以序列号（p.serial_number）才是识别同一块芯片的可靠依据。
        """
        out = []
        if serial is None:
            return out
        try:
            for p in serial.tools.list_ports.comports():
                desc = p.description or ""
                hwid = p.hwid or ""
                blob = (desc + " " + hwid).upper()
                ser = (getattr(p, "serial_number", "") or "").strip()
                out.append({
                    "dev": p.device,
                    "desc": desc,
                    "hwid": hwid,
                    "ser": ser,
                    "loc": (getattr(p, "location", "") or ""),
                    "esp": ("303A" in blob) or ("ESPRESSIF" in blob),
                })
        except Exception:
            pass
        out.sort(key=lambda i: com_num(i["dev"]))
        return out

    @staticmethod
    def _bridge_ping(dev, timeout=0.9):
        """跟串口上的固件握一次手：发 INFO，看是否回 OK INFO。

        只有我们的桥固件会回这一行，别的 ESP32 板子（哪怕同型号）不会，
        这是唯一能百分百分出「哪块是我们的桥」的判据。
        返回 (是否命中, 该口序列号)。
        """
        if serial is None:
            return False, ""
        try:
            s = serial.Serial(dev, 115200, timeout=0.1, write_timeout=0.3)
        except Exception:
            return False, ""
        ser = ""
        try:
            # DTR 必须拉高：这块 S3 走 USB CDC，DTR 为低时固件当主机没连上，
            # Serial 的输出会被直接丢掉，回包一个字节都收不到。
            # RTS 保持低，避免被当成复位/下载握手信号。
            try:
                s.dtr = True
                s.rts = False
            except Exception:
                pass
            try:
                s.reset_input_buffer()
            except Exception:
                pass
            s.write(b"INFO\r\n")
            t0 = time.time()
            buf = b""
            while time.time() - t0 < timeout:
                try:
                    chunk = s.read(128)
                except Exception:
                    break
                if chunk:
                    buf += chunk
                    if b"OK INFO" in buf:
                        return True, ser
            return False, ser
        except Exception:
            return False, ser
        finally:
            try:
                s.close()
            except Exception:
                pass

    def _probe_async(self, cands, prefer=None):
        """多个 ESP32 候选时，后台逐个握手定夺，不卡界面。

        `prefer` 是当前最可能正确的口（记忆/手选/枚举首选），排到队首先问，
        通常一次 0.1 秒就能确认，不必先等另一块板子超时 1 秒。
        """
        if getattr(self, "_probing", False) or self.ser is not None:
            return
        order = list(cands)
        if prefer:
            order.sort(key=lambda i: 0 if i["dev"] == prefer else 1)
        self._probing = True

        def work():
            hit = None
            try:
                for info in order:
                    if self.ser is not None:      # 用户中途自己连上了就收手
                        break
                    ok, _ = self._bridge_ping(info["dev"])
                    if ok:
                        hit = info
                        break
            finally:
                # 必须放 finally：探测线程若异常退出而不复位，界面会永久停在
                # 「正在确认」状态，之后每次扫端口都不再做握手
                self._probing = False
            if hit is not None:
                self.ui_q.put(lambda: self._on_probe_hit(hit))

        threading.Thread(target=work, daemon=True).start()

    def _on_probe_hit(self, info):
        # 用户手填过并且填的是候选之一，就别抢他的选择
        typed = self.cbo_port.get().strip()
        if getattr(self, "_port_user_locked", False) and typed:
            return
        self._set_port(info["dev"])
        write_port_pref(ser=info["ser"], port=info["dev"], desc=info["desc"])
        self._hint("已选 %s（桥接板已回应 OK INFO）" % info["dev"])

    def _set_port(self, dev):
        try:
            if self.cbo_port.get().strip() != dev:
                self.cbo_port.delete(0, "end")
                self.cbo_port.insert(0, dev)
        except Exception:
            pass

    def _on_port_typed(self, _ev=None):
        """用户手改串口 -> 标记为「手选」，后续扫端口不覆盖。"""
        self._port_user_locked = True

    def _remember_port(self, dev):
        """把成功连通的串口和芯片序列号记下来，下次优先认同一块板。"""
        for i in self._list_ports():
            if i["dev"] == dev:
                write_port_pref(ser=i["ser"], port=dev, desc=i["desc"])
                return
        write_port_pref(port=dev)

    def _hint(self, text):
        """顶部右侧的一次性提示（端口判定结果 / 热键说明都用它）。"""
        try:
            self.lbl_hint.configure(text=text)
        except Exception:
            pass

    def refresh_ports(self, force=False):
        """挑串口。五级判定，用来区分同机多块同型号 ESP32：

        ① 用户手选过且该口还在 —— 尊重（**点「扫端口」时作废，强制重判**）；
        ② 记住上次握手成功的芯片序列号 —— 跟芯片走，换 USB 口也认得出；
        ③ 唯一一个乐鑫口 —— 直接用它；
        ④ 多个乐鑫口 —— 先给临时值（取端口号最大的，通常是刚插的那块），
           同时后台逐个握手，只认回 OK INFO 的那块（同型号板不会回）；
        ⑤ 都不是乐鑫口 —— 按 COM 数字取最大的（不是字符串排序）。

        force=True 是用户主动点「扫端口」：必须清掉手选锁，否则用户手打过一次
        端口之后就永远停在旧值、看起来像「扫描没用」。
        """
        if force:
            self._port_user_locked = False
        infos = self._list_ports()
        devices = [i["dev"] for i in infos]
        if not devices:
            self._hint("没扫到串口，检查 USB 是直连还是仅供电")
            return []
        esp = [i for i in infos if i["esp"]]
        # 同一个复合 USB 设备可能同时挂着「运行态 CDC（.1）」和「下载模式
        # USB-Serial/JTAG（.0）」两个口，后者连上也没用（固件没在跑），
        # 所以挑口时优先 .1。
        app = [i for i in esp if self._is_app_port(i)]
        pref = read_port_pref()
        cur = self.cbo_port.get().strip()
        pick, why = None, ""
        ser_hit = False

        locked = bool(cur and getattr(self, "_port_user_locked", False)
                      and cur in devices)
        if locked:
            pick, why = cur, "手选"                            # ①
        if pick is None and pref.get("ser"):                   # ②
            for i in esp:
                if i["ser"] and i["ser"] == pref["ser"]:
                    pick, why, ser_hit = i["dev"], "序列号记忆", True
                    break
        if pick is None and len(app) == 1:                     # ③
            pick, why = app[0]["dev"], "唯一乐鑫口"
        if pick is None and not app and len(esp) == 1:
            pick, why = esp[0]["dev"], "乐鑫口（还在下载模式）"
        if pick is None:                                       # ④ / ⑤
            if pref.get("port") in devices:
                pick, why = pref["port"], "上次用过的口"
            elif app or esp:
                pick, why = (app or esp)[-1]["dev"], "多个乐鑫口，后台确认中"
            else:
                pick, why = devices[-1], "端口号最大"

        if self.ser is not None:
            # 已连接时改端口只会让界面和实际连接对不上，先提示断开
            self._hint("已连接 %s，要换口请先点「断开」" % (cur or "?"))
            return devices

        self._set_port(pick)
        self._hint("已选 %s（%s）" % (pick, why))

        # 手选的口不再探测（用户已经明确指定，别去串口上乱发 INFO）；
        # 序列号已认出也不用问。只有「多乐鑫口 + 毫无依据」才值得握手。
        if len(esp) > 1 and not ser_hit and not locked:
            self._probe_async(esp, pick)
        return devices

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
        self.lbl_hint.configure(text="")
        # 连上了就把这块芯片的序列号记住，下次插别的 USB 口也认得它
        self._remember_port(port)
        self._flash_print(
            "\n=== 已连接 %s —— 下面是串口实时输出，按一下遥控器会出现 # KEY 行 ===\n"
            % port)
        threading.Thread(target=self._reader, daemon=True).start()
        self.send("INFO")
        self.after(150, lambda: self.cmd_list(False))
        # 连接成功即自动初始化并重抓硬件信息（清空旧指纹 + 重新抓取，隐私不残留）
        self.after(300, self._hw_refresh)

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
                s = line.decode("utf-8", "replace").strip("\r")
                # 音频行走旁路：每秒上百条，塞 UI 队列会把界面拖死
                if s.startswith("A "):
                    self._on_audio_line(s)
                else:
                    self.rx_q.put(s)

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
        # 子线程不能直接碰 Tk（after 也不行，会撞 "main thread is not in main loop"），
        # 统一丢进队列，由这里在主线程执行。
        try:
            while True:
                fn = self.ui_q.get_nowait()
                try:
                    fn()
                except Exception:
                    pass
        except queue.Empty:
            pass
        self.after(60, self._drain)

    def _handle(self, line):
        # 硬件信息页「重新抓取」期间，把所有行收进临时缓冲，供解析用
        if self._cap_active:
            self._cap.append(line)
        if not line:
            return
        # 调试日志：串口收到的每一行都带时间戳镜像到页4，按键报告 R / OK STAT
        # 这些原本解析完就丢了，页4 看不到任何实时输出。
        self._flash_rx(line)
        if line.startswith("#"):
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
        if c.get("lock"):
            self._paint_lock(c)
            return
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


def write_diag():
    """--diag：把运行环境自检写到程序目录 diag.txt。

    换台电脑跑不起来时，让用户双击带 --diag 的快捷方式，把 diag.txt 发回来
    就能定位是缺 DLL、缺模型还是缺解码器，不用猜。
    """
    lines = ["CMCC_RC_Keys 环境自检", "=" * 40]
    lines.append("python      : %s" % sys.version.split()[0])
    lines.append("frozen      : %s" % bool(getattr(sys, "frozen", False)))
    lines.append("exe         : %s" % sys.executable)
    lines.append("_MEIPASS    : %s" % getattr(sys, "_MEIPASS", "-"))
    lines.append("cwd         : %s" % os.getcwd())
    lines.append("")
    lines.append("pyserial    : %s" % ("OK" if serial is not None else "MISSING"))
    # 刷固件要用 esptool：外部装了优先用外部，没装就用包里内嵌的
    try:
        import esptool as _esp
        lines.append("esptool     : 内嵌 %s" % getattr(_esp, "__version__", "?"))
    except Exception as exc:
        lines.append("esptool     : 内嵌不可用 - %s" % exc)
    _ext = None
    for _n in ("esptool", "esptool.exe"):
        _ext = _ext or shutil.which(_n)
    lines.append("esptool ext : %s" % (_ext or "未安装（将用内嵌）"))
    lines.append("bg.png      : %s" % (App._asset("bg.png") or "MISSING"))
    lines.append("ini         : %s" % ini_path())
    lines.append("")
    if asr_win is None:
        lines.append("asr_win     : IMPORT FAILED - %s" % _ASR_IMPORT_ERR)
    else:
        lines.append("asr_win     : OK")
        for r in asr_win.search_roots():
            lines.append("  root      : %s" % r)
        lines.append("  decoder   : %s" % (asr_win.find_decoder() or "MISSING"))
        md = asr_win.default_model_dir()
        lines.append("  model dir : %s" % (md or "MISSING"))
        if md:
            for f in ("model.int8.onnx", "tokens.txt"):
                p = os.path.join(md, f)
                lines.append("    %-18s %s" % (f, "OK" if os.path.exists(p) else "MISSING"))
        try:
            eng = asr_win.AsrEngine()
            eng.load()
            lines.append("  engine    : load OK (%d ms)" % eng.load_ms)
            lines.append("  timeout   : %.0f s（卡住就放弃这一句并重建识别器）"
                         % (ASR_TIMEOUT_MS / 1000.0))
            import numpy as np
            t = eng.transcribe(np.zeros(16000, dtype="float32"))
            lines.append("  selftest  : ran, text=%r" % t)
        except Exception as exc:
            lines.append("  engine    : FAIL - %s" % exc)
    text = "\n".join(lines)
    dst = os.path.join(app_dir(), "diag.txt")
    try:
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        dst = os.path.join(os.environ.get("TEMP", "."), "diag.txt")
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    try:
        print(text)
    except Exception:
        pass
    return dst


def write_esptool_selftest(port):
    """--selftest-esptool COMx：用**内嵌** esptool 读一次芯片型号，写进文本。

    目标机刷不了固件时，让用户跑这个，把结果发回来就能分清是「esptool 环境
    问题」还是「端口/硬件问题」。也用于发版前验证内嵌那份确实能跑。
    """
    out = ["CMCC_RC_Keys esptool 自检（进程内）", "=" * 40,
           "port        : %s" % port,
           "frozen      : %s" % bool(getattr(sys, "frozen", False)),
           "_MEIPASS    : %s" % getattr(sys, "_MEIPASS", "-"), ""]

    class _UI:
        def put(self, fn):
            try:
                fn()
            except Exception:
                pass

    class _Stub:
        def _flash_print(self, s):
            out.append(s.rstrip("\n"))

        def _flash_insert(self, s, tag=None):
            out.append(s.rstrip("\n"))

    stub = _Stub()
    t0 = time.time()
    try:
        rc, txt = App._exec_esptool_inproc(stub, ["--port", port, "chip-id"], _UI(), 90.0)
        out.append("")
        out.append("rc          : %r" % rc)
        out.append("耗时        : %.1f s" % (time.time() - t0))
        out.append("芯片型号    : %r" % App._parse_chip(txt))
        out.append("失败原因    : %r" % App._esptool_fail_reason(txt))
    except Exception as exc:
        out.append("EXCEPTION   : %s: %s" % (type(exc).__name__, exc))
    text = "\n".join(out)
    dst = os.path.join(app_dir(), "esptool_selftest.txt")
    try:
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    except Exception:
        dst = os.path.join(os.environ.get("TEMP", "."), "esptool_selftest.txt")
        with open(dst, "w", encoding="utf-8") as f:
            f.write(text + "\n")
    return dst


def main():
    if "--selftest-esptool" in sys.argv:
        i = sys.argv.index("--selftest-esptool")
        port = sys.argv[i + 1] if len(sys.argv) > i + 1 else ""
        p = write_esptool_selftest(port)
        try:
            messagebox.showinfo("自检完成", "esptool 自检结果已写入：\n%s" % p)
        except Exception:
            pass
        return
    if "--diag" in sys.argv:
        p = write_diag()
        try:
            messagebox.showinfo("自检完成", "环境自检结果已写入：\n%s" % p)
        except Exception:
            pass
        return
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
