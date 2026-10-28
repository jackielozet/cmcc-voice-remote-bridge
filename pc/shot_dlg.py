# -*- coding: utf-8 -*-
"""弹窗截图（开发期自检）：开主窗 -> 打开"设置"键的录键弹窗 -> 截弹窗"""
import ctypes
import os
import time
from ctypes import wintypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

from PIL import ImageGrab

import remote_ui as R

HERE = os.path.dirname(os.path.abspath(__file__))
user32 = ctypes.windll.user32

app = R.App()
app.update_idletasks()
app.update()


class E:
    pass


for k, c in app.cards.items():
    if isinstance(k, int) and c["label"] == "设置":
        e = E()
        e.x = (c["x1"] + c["x2"]) / 2
        e.y = (c["y1"] + c["y2"]) / 2
        app._on_click(e)
        break

for _ in range(30):
    app.update_idletasks()
    app.update()
    time.sleep(0.05)

dlg = None
for w in app.winfo_children():
    if isinstance(w, R.Recorder):
        dlg = w
if dlg is None:
    print("没弹出窗口")
    raise SystemExit

hwnd = user32.FindWindowW(None, dlg.title())
if not hwnd:
    print("没找到弹窗句柄")
    raise SystemExit
user32.SetWindowPos(hwnd, -1, 0, 0, 0, 0, 0x0001 | 0x0002 | 0x0040)
user32.BringWindowToTop(hwnd)
time.sleep(0.6)
r = wintypes.RECT()
user32.GetWindowRect(hwnd, ctypes.byref(r))
img = ImageGrab.grab(bbox=(r.left, r.top, r.right, r.bottom))
os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)
p = os.path.join(HERE, "logs", "dlg.png")
img.save(p)
print("弹窗", r.left, r.top, r.right, r.bottom, "->", img.size, p)
app.destroy()
