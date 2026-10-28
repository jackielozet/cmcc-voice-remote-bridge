# -*- coding: utf-8 -*-
"""联机保存自检：连接 -> 读设备 -> 保存(写设备+写ini) -> 读回比对"""
import ctypes
import os
import sys

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import remote_ui as R

port = sys.argv[1] if len(sys.argv) > 1 else "COM11"
app = R.App()
app.cbo_port.delete(0, "end")
app.cbo_port.insert(0, port)
app.update_idletasks()

app.toggle_conn()


def step1():
    print("设备读到的条目 vs ini：")
    diff = 0
    for label, kid, _, _ in R.REMOTE:
        if not kid:
            continue
        b = app.bindings.get(kid)
        r = app.ini.get(label)
        d1 = R.describe(b["out_type"], b["keys"], b["n"]) if b else "无"
        d2 = R.describe(r[0], r[1], r[2]) if r else "无"
        if b and r and (b["out_type"], tuple(b["keys"]), b["n"]) != (r[0], tuple(r[1]), r[2]):
            print("  !! %-8s 设备=%s  ini=%s" % (label, d1, d2))
            diff += 1
    print("  不一致 %d 项" % diff)
    print("\n执行保存…")
    app.cmd_save()
    app.after(1800, step2)


def step2():
    p = R.ini_path()
    print("ini 修改时间:", __import__("time").strftime(
        "%H:%M:%S", __import__("time").localtime(os.path.getmtime(p))))
    data, bad = R.read_ini(p)
    print("ini 条数:", len(data), " 坏行:", bad)

    # 再从设备重新读一遍，确认跟 ini 一致
    app.cmd_list()
    app.after(1200, step3)


def step3():
    diff = 0
    for label, kid, _, _ in R.REMOTE:
        if not kid:
            continue
        b = app.bindings.get(kid)
        r = app.ini.get(label)
        if not b or not r:
            continue
        if (b["out_type"], tuple(b["keys"]), b["n"]) != (r[0], tuple(r[1]), r[2]):
            print("  !! 存完仍不一致 %-8s 设备=%s ini=%s"
                  % (label, R.describe(b["out_type"], b["keys"], b["n"]),
                     R.describe(*r)))
            diff += 1
    print("保存后不一致 %d 项  ->  %s" % (diff, "通过" if diff == 0 else "有问题"))
    app.destroy()


app.after(2600, step1)
app.mainloop()
