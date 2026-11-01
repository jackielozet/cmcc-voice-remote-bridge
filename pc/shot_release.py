# -*- coding: utf-8 -*-
"""截发布用主图：启动 exe，等窗口稳定后整窗截图。

用法: python shot_release.py <exe路径> <输出图片名，不含扩展名> [宽高缩放系数]
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
EXE = os.path.abspath(sys.argv[1] if len(sys.argv) > 1
                      else os.path.join(HERE, "dist", "CMCC_RC_Keys", "CMCC_RC_Keys.exe"))
NAME = sys.argv[2] if len(sys.argv) > 2 else "release_main"

user32 = ctypes.windll.user32
user32.SetProcessDPIAware()

found = []


@ctypes.WINFUNCTYPE(ctypes.c_bool, wintypes.HWND, wintypes.LPARAM)
def _cb(h, _lp):
    n = user32.GetWindowTextLengthW(h)
    if n:
        buf = ctypes.create_unicode_buffer(n + 1)
        user32.GetWindowTextW(h, buf, n + 1)
        if buf.value.startswith("CMCC"):
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


def main():
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
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
            return 1

        # 窗口先到前台，再等模型/背景图那次重绘落定
        user32.SetForegroundWindow(hwnd)
        time.sleep(1.2)

        # 挪到原点并把高度压进工作区，否则窗口底边会落到任务栏里，截图带出任务栏
        l, t, r, b = rect(hwnd)
        w, h = r - l, b - t
        rw = wintypes.RECT()
        user32.SystemParametersInfoW(0x0030, 0, ctypes.byref(rw), 0)
        maxh = (rw.bottom - rw.top) - 40
        if h > maxh:
            k = maxh / float(h)
            user32.MoveWindow(hwnd, 0, 0, int(w * k), maxh, True)
        else:
            user32.MoveWindow(hwnd, 0, 0, w, h, True)
        time.sleep(3.0)

        # 只截客户区：GetWindowRect 含标题栏和圆角阴影，截进来会带出桌面残影
        cr = wintypes.RECT()
        user32.GetClientRect(hwnd, ctypes.byref(cr))
        org = wintypes.POINT(0, 0)
        user32.ClientToScreen(hwnd, ctypes.byref(org))
        cl, ct = org.x, org.y
        cw, ch = cr.right - cr.left, cr.bottom - cr.top
        img = ImageGrab.grab(bbox=(cl, ct, cl + cw, ct + ch))
        out = os.path.join(HERE, "logs", NAME + ".png")
        img.save(out)
        print("客户区 %dx%d @(%d,%d) -> %s" % (cw, ch, cl, ct, out))
        print("exe:", EXE)
        return 0
    finally:
        try:
            p.terminate()
        except Exception:
            pass


if __name__ == "__main__":
    sys.exit(main())
