# -*- coding: utf-8 -*-
"""验证「ini 优先」：手改 ini 里的一个键，连设备后界面应该显示 ini 的值，
   而不是设备原来的值；保存后设备才会变。"""
import ctypes
import os
import sys

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import remote_ui as R

p = R.ini_path()
data, _ = R.read_ini(p)
print("改前 设置 =", R.describe(*data["设置"]))

# 手动把 ini 里的「设置」改成 F13，模拟用户用记事本改配置
data["设置"] = R.parse_action("F13")
items = [(l,) + data[l] for l, _, _, _ in R.REMOTE if l in data]
R.write_ini(p, items)
print("已把 ini 里「设置」改成 F13")

app = R.App()
app.cbo_port.delete(0, "end")
app.cbo_port.insert(0, "COM11")
app.update_idletasks()
app.toggle_conn()


def check():
    c = app.cards[0x20EB]
    shown = app.canvas.itemcget(c["value"], "text")
    dev = app.bindings.get(0x20EB)
    devtxt = R.describe(dev["out_type"], dev["keys"], dev["n"]) if dev else "无"
    print("\n连上设备后：")
    print("  界面显示（应来自 ini）= %s" % shown)
    print("  设备里的值           = %s" % devtxt)
    print("  结论：%s" % ("ini 优先，通过" if shown == "F13" else "!! ini 被设备覆盖了"))
    print("\n状态栏：", app.lbl_ini.cget("text"))
    app.destroy()


app.after(2800, check)
app.mainloop()
