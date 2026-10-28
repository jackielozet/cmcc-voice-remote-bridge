# -*- coding: utf-8 -*-
"""布局自检：框是否出界 / 同列是否重叠 / 引线端点是否对应正确的键"""
import ctypes

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import remote_ui as R

app = R.App()
app.update_idletasks()
app.update()

print("窗口 %dx%d  图片 %dx%d  缩放 %.3f  偏移 %d,%d\n"
      % (R.WIN_W, R.WIN_H, R.IMG_W, R.IMG_H, R.IMG_K, R.OX, R.PAD_Y))

rows, bad = [], []
for k, c in app.cards.items():
    if not isinstance(k, int):
        continue
    x1, y1, x2, y2 = c["x1"], c["y1"], c["x2"], c["y2"]
    co = app.canvas.coords(c["line"])
    rows.append((c["label"], k, x1, y1, x2, y2, co))
    if x1 < 0 or x2 > R.WIN_W or y1 < 0 or y2 > R.WIN_H:
        bad.append("出界: %s (%.0f,%.0f)-(%.0f,%.0f)" % (c["label"], x1, y1, x2, y2))

rows.sort(key=lambda r: (r[3], r[2]))
print("%-8s %-6s %-24s %s" % ("键", "id", "文本框 (x1,y1)-(x2,y2)", "引线 起点->终点"))
prev = None
for label, kid, x1, y1, x2, y2, co in rows:
    print("%-8s %04X   (%6.1f,%6.1f)-(%6.1f,%6.1f)   (%6.1f,%6.1f)->(%6.1f,%6.1f)"
          % (label, kid, x1, y1, x2, y2, co[0], co[1], co[2], co[3]))
    if prev and prev[2] == round(x1) and y1 < prev[5] - 0.5:
        bad.append("重叠: %s 与 %s" % (prev[0], label))
    prev = (label, kid, round(x1), y1, x2, y2)

print()
print("问题：" if bad else "检查通过：无出界、无重叠")
for b in bad:
    print("  -", b)

# 抽查：引线起点应等于该键在窗口里的坐标
print("\n抽查引线起点 vs 键坐标：")
for label, kid, kx, ky in R.REMOTE[:4] + R.REMOTE[-3:]:
    if kid == 0:
        continue
    c = app.cards.get(kid)
    if not c:
        continue
    co = app.canvas.coords(c["line"])
    ex = (kx - R.CROP_X0) * R.IMG_K + R.OX
    ey = ky * R.IMG_K + R.PAD_Y
    side = -1 if co[2] < co[0] else 1        # 终点在左侧说明是左列
    want = ex + side * R.zp(17)
    print("  %-6s 键(%.1f,%.1f) 引线起点(%.1f,%.1f) 期望x=%.1f  %s"
          % (label, ex, ey, co[0], co[1], want,
             "OK" if abs(co[0] - want) < 1.5 and abs(co[1] - ey) < 1.5 else "!! 不符"))

app.destroy()
