# -*- coding: utf-8 -*-
"""启动界面 -> 按窗口矩形精确截图 -> 关闭（开发期自检用）

用法: python shot.py 名字 [等待秒] [点击画布坐标x,y]
"""
import ctypes
import os
import subprocess
import sys
import time
from ctypes import wintypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

from PIL import ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
PY = sys.executable
TITLE = "CMCC 遥控器 - 按键映射"

name = sys.argv[1] if len(sys.argv) > 1 else "shot"
wait = float(sys.argv[2]) if len(sys.argv) > 2 else 3.0
click = sys.argv[3] if len(sys.argv) > 3 else None

user32 = ctypes.windll.user32


def find_window():
    hwnd = user32.FindWindowW(None, TITLE)
    return hwnd or None


def rect_of(hwnd):
    r = wintypes.RECT()
    user32.GetWindowRect(hwnd, ctypes.byref(r))
    return r.left, r.top, r.right, r.bottom


def main():
    os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
    p = subprocess.Popen([PY, os.path.join(HERE, "remote_ui.py")], cwd=HERE)
    try:
        time.sleep(wait)
        hwnd = find_window()
        if not hwnd:
            print("没找到窗口")
            return
        # 强制激活并置顶：直接 SetForegroundWindow 会被 Windows 前台锁挡住，
        # 需要先把自己的输入队列 attach 到当前前台线程
        kernel32 = ctypes.windll.kernel32
        fore = user32.GetForegroundWindow()
        t_fore = user32.GetWindowThreadProcessId(fore, None)
        t_me = kernel32.GetCurrentThreadId()
        user32.AttachThreadInput(t_me, t_fore, True)
        user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
        user32.BringWindowToTop(hwnd)
        user32.SetForegroundWindow(hwnd)
        user32.AttachThreadInput(t_me, t_fore, False)
        time.sleep(1.0)

        if click:
            cx, cy = [int(v) for v in click.split(",")]
            l, t, r, b = rect_of(hwnd)
            # 画面元素坐标 -> 屏幕坐标（含标题栏和边框偏移，靠比例粗算）
            user32.SetCursorPos(l + cx, t + cy)
            time.sleep(0.3)
            user32.mouse_event(0x0002, 0, 0, 0, 0)   # left down
            user32.mouse_event(0x0004, 0, 0, 0, 0)   # left up
            time.sleep(1.2)

        l, t, r, b = rect_of(hwnd)
        img = ImageGrab.grab(bbox=(l, t, r, b))
        path = os.path.join(HERE, "logs", "%s.png" % name)
        img.save(path)
        print("窗口", l, t, r, b, "-> %dx%d" % (r - l, b - t))
        print("截图", path, img.size)
    finally:
        p.terminate()
        try:
            p.wait(3)
        except Exception:
            p.kill()


if __name__ == "__main__":
    main()
