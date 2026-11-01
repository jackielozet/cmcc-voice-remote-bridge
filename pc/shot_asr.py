# -*- coding: utf-8 -*-
"""开发期自检：一次拍两张图 —— 未勾选 / 已勾选（麦克风键锁定）的界面。

不走串口，直接把 asr_on 置位再调 _sync_asr_lock()，纯看外观用。
用法: python shot_asr.py
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
    time.sleep(0.12)


def grab(name):
    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    img = ImageGrab.grab(bbox=(x, y, x + w, y + h))
    p = os.path.join(OUT, name + ".png")
    img.save(p)
    print("%-10s %dx%d -> %s" % (name, img.size[0], img.size[1], p))


grab("asr_off")

# 模拟"已勾选"：变量置真 -> 勾选框自己重绘；再走锁定逻辑
app.var_asr.set(True)
app.asr_on = True
app._asr_status("● 就绪，麦克风键已锁定 · 按住说话（模型 412 ms）", remote_ui.ACCENT)
app._sync_asr_lock()
for _ in range(6):
    app.update()
    time.sleep(0.1)
grab("asr_on")

# 再取消勾选，确认能还原
app.var_asr.set(False)
app.asr_on = False
app._asr_status(remote_ui.ASR_OFF_NOTE, remote_ui.TEXT_DIM)
app._sync_asr_lock()
for _ in range(6):
    app.update()
    time.sleep(0.1)
grab("asr_back")

app.destroy()
print("done")
