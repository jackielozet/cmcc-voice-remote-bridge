# -*- coding: utf-8 -*-
"""联机自检：连上设备 -> 读映射 -> 打印界面上的显示 -> 截图"""
import ctypes
import os
import sys

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

from PIL import ImageGrab

import remote_ui as R

HERE = os.path.dirname(os.path.abspath(__file__))
port = sys.argv[1] if len(sys.argv) > 1 else "COM11"

app = R.App()
app.cbo_port.delete(0, "end")
app.cbo_port.insert(0, port)
app.update_idletasks()
app.update()

app.toggle_conn()
app.after(int(sys.argv[2]) if len(sys.argv) > 2 else 2500, lambda: None)


def dump():
    print("\n界面显示（应等于设备里的映射）：")
    rows = []
    for k, c in app.cards.items():
        if isinstance(k, int):
            txt = app.canvas.itemcget(c["value"], "text")
            rows.append((c["y1"], c["label"], "%04X" % k, txt))
    rows.sort()
    for _, label, kid, txt in rows:
        flag = ""
        if txt == "—":
            flag = "   << 设备里没有这条绑定"
        print("  %-8s %s  %s%s" % (label, kid, txt, flag))

    x, y = app.winfo_rootx(), app.winfo_rooty()
    w, h = app.winfo_width(), app.winfo_height()
    p = os.path.join(HERE, "logs", "live.png")
    ImageGrab.grab(bbox=(x, y, x + w, y + h)).save(p)
    print("\n截图", p, (x, y, w, h))
    app.destroy()


app.after(600, lambda: (app.attributes("-topmost", True), app.lift(),
                        app.focus_force()))
app.after(2600, dump)
app.mainloop()
