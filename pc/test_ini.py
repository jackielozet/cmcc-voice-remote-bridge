# -*- coding: utf-8 -*-
"""ini 配置自检：生成 / 解析 / 回写"""
import ctypes
import os
import sys

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import remote_ui as R

p = R.ini_path()
print("ini 路径:", p)

# 1) 无文件时自动生成
if os.path.exists(p):
    os.remove(p)
    print("已删除旧 ini，模拟首次启动")

app = R.App()
app.update_idletasks()
print("\n--- 生成后的 ini ---")
print(open(p, encoding="utf-8").read())

# 2) 解析回来，和界面上的显示比对
print("--- 界面显示 ---")
rows = []
for label, kid, _, _ in R.REMOTE:
    key = kid if kid else ("na", label)
    c = app.cards.get(key)
    if c:
        rows.append((c["y1"], label, app.canvas.itemcget(c["value"], "text")))
rows.sort()
for _, label, txt in rows:
    print("  %-8s %s" % (label, txt))

# 3) 改一项 + 回写
app.ini["设置"] = R.parse_action("Ctrl+Shift+P")
items = [(l, ) + app.ini[l] for l, _, _, _ in R.REMOTE if l in app.ini]
R.write_ini(p, items)
back, bad = R.read_ini(p)
print("\n--- 回写后重新解析 ---")
print("  设置 =", R.describe(*back["设置"]), " 无法识别行:", bad)
print("  总条数:", len(back))

app.destroy()
