# -*- coding: utf-8 -*-
"""闭环：ini 里改成 F13 -> 保存 -> 设备应变成 F13 -> 改回 Ctrl+C -> 保存 -> 恢复"""
import ctypes
import sys

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import remote_ui as R

app = R.App()
app.cbo_port.delete(0, "end")
app.cbo_port.insert(0, sys.argv[1] if len(sys.argv) > 1 else "COM11")
app.update_idletasks()
app.toggle_conn()

KID = 0x20EB          # 设置键
state = {"phase": 0}


def dev_value():
    b = app.bindings.get(KID)
    return R.describe(b["out_type"], b["keys"], b["n"]) if b else "无"


def step():
    ph = state["phase"]
    if ph == 0:
        print("1) 保存前：界面=%s   设备=%s" % (app._card_value("设置"), dev_value()))
        print("   点保存（应把 ini 里的 F13 下发到设备）…")
        app.cmd_save()
        state["phase"] = 1
        app.after(3500, step)
    elif ph == 1:
        app.cmd_list(True)          # 强制以设备为准重读
        state["phase"] = 2
        app.after(1800, step)
    elif ph == 2:
        v = dev_value()
        print("2) 保存后：设备=%s   %s" % (v, "OK" if v == "F13" else "!! 没写进去"))
        app.ini["设置"] = R.parse_action("Ctrl+C")
        app.changed.add(KID)
        app._refresh_card(KID)
        print("3) 恢复成 Ctrl+C 并保存…")
        app.cmd_save()
        state["phase"] = 3
        app.after(3500, step)
    elif ph == 3:
        app.cmd_list(True)
        state["phase"] = 4
        app.after(1800, step)
    else:
        v = dev_value()
        print("4) 恢复后：设备=%s   %s" % (v, "已恢复" if v == "Ctrl+C" else "!! 恢复失败"))
        print("   ini 里现在：", app._card_value("设置"))
        app.destroy()


app.after(2800, step)
app.mainloop()
