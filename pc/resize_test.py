# -*- coding: utf-8 -*-
"""开发期自检：把窗口拖小/拖大，拍照看等比缩放有没有错位。

用法: python resize_test.py
"""
import ctypes
import os
import sys
import time

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(2)
except Exception:
    pass

from PIL import ImageGrab

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)
os.chdir(HERE)

import remote_ui   # noqa: E402

OUT = os.path.join(HERE, "logs")
os.makedirs(OUT, exist_ok=True)

app = remote_ui.App()
app.update()
app.lift()
app.attributes("-topmost", True)
app.focus_force()
for _ in range(8):
    app.update()
    time.sleep(0.1)

bw, bh = app._base_wh
print("base", bw, bh, "minsize", app.minsize(), "maxsize", app.maxsize())


def grab(name):
    for _ in range(6):
        app.update()
        time.sleep(0.08)
    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    p = os.path.join(OUT, name + ".png")
    img.save(p)
    print("%-8s 窗口 %dx%d  实际 k=%.2f  Z=%.2f -> %s"
          % (name, w, h, h / float(bh), remote_ui.Z, p))


grab("rs_100")

for k, name in ((0.80, "rs_080"), (0.45, "rs_045"), (1.15, "rs_115")):
    app.geometry("%dx%d" % (int(bw * k), int(bh * k)))
    for _ in range(10):
        app.update()
        time.sleep(0.1)
    time.sleep(0.5)          # 等 260ms 防抖重建跑完
    grab(name)

app.destroy()
print("done")
