# -*- coding: utf-8 -*-
"""验证打包好的 exe：外部改窗口大小，看背景图和引线框有没有跟着等比缩放。

用法: python shot_exe_resize.py [exe路径]
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from PIL import ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
EXE = (sys.argv[1] if len(sys.argv) > 1
       else os.path.join(HERE, "dist", "CMCC_RC_Keys", "CMCC_RC_Keys.exe"))
TITLE = "CMCC 遥控器 - 按键映射"

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()


@ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
def _cb(h, _lp):
    n = user32.GetWindowTextLengthW(h)
    if n:
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        if buf.value.startswith(TITLE):
            found.append(h)
            return False
    return True


def find_window():
    found.clear()
    user32.EnumWindows(_cb, 0)
    return found[0] if found else None


def rect(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def foreground(hwnd):
    """把窗口拉到最前（SetForegroundWindow 会被前台锁挡，先 attach 输入队列）。"""
    k32 = ctypes.windll.kernel32
    fore = user32.GetForegroundWindow()
    t_fore = user32.GetWindowThreadProcessId(fore, None)
    t_me = k32.GetCurrentThreadId()
    user32.AttachThreadInput(t_me, t_fore, True)
    user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
    user32.BringWindowToTop(hwnd)
    user32.SetForegroundWindow(hwnd)
    user32.AttachThreadInput(t_me, t_fore, False)


found = []
p = subprocess.Popen([EXE], cwd=os.path.dirname(EXE))
try:
    hwnd = None
    for _ in range(60):
        time.sleep(0.5)
        hwnd = find_window()
        if hwnd:
            break
    if not hwnd:
        print("没找到窗口")
        sys.exit(1)

    time.sleep(2.0)
    l, t, r, b = rect(hwnd)
    w0, h0 = r - l, b - t
    print("原始窗口", w0, h0)

    for k, name in ((1.0, "exe_100"), (0.62, "exe_062"), (1.0, "exe_back")):
        nw, nh = int(w0 * k), int(h0 * k)
        user32.MoveWindow(hwnd, l, t, nw, nh, True)
        time.sleep(2.2)                    # 等 260ms 防抖重建 + 重绘
        foreground(hwnd)
        time.sleep(0.6)
        l2, t2, r2, b2 = rect(hwnd)
        img = ImageGrab.grab(bbox=(l2, t2, r2, b2))
        pth = os.path.join(HERE, "logs", name + ".png")
        img.save(pth)
        print("%-8s -> %dx%d  %s" % (name, r2 - l2, b2 - t2, pth))
finally:
    p.terminate()
    try:
        p.wait(3)
    except Exception:
        p.kill()
print("done")
