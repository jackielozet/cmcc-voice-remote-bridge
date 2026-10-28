# -*- coding: utf-8 -*-
"""自检：从「常用动作」下拉里选任一项，点确定都应该能存住"""
import ctypes
import os
import sys

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import remote_ui as R

HERE = os.path.dirname(os.path.abspath(__file__))
os.makedirs(os.path.join(HERE, "logs"), exist_ok=True)

app = R.App()
app.update_idletasks()
app.update()


class E:
    pass


def find_recorder():
    for w in app.winfo_children():
        if isinstance(w, R.Recorder):
            return w
    return None


def open_dialog(label):
    for k, c in app.cards.items():
        if isinstance(k, int) and c["label"] == label:
            e = E()
            e.x = (c["x1"] + c["x2"]) / 2
            e.y = (c["y1"] + c["y2"]) / 2
            app._on_click(e)
            return True
    return False


def run():
    opts = ["—", "不响应"] + list(R.MEDIA_NAME.values()) + list(R.KEY_NAME.values())
    fail = []
    okn = 0
    # 让 on_ok 只记录，不真的去发串口
    for sel in opts:
        if sel == "—":
            continue
        if not open_dialog("音量+"):
            print("打不开弹窗")
            break
        app.update_idletasks()
        dlg = find_recorder()
        if dlg is None:
            print("弹窗没出现")
            break
        dlg.result = None
        dlg.cbo.set(sel)

        got = {}
        dlg.on_ok = lambda ot, ks, n, g=got: g.update(ot=ot, keys=ks, n=n)
        # 绕过 messagebox：先看能否解析出来
        if dlg.result is None:
            r = R.parse_action(sel)
            if r is None:
                fail.append((sel, "parse_action 返回 None"))
                dlg.destroy()
                continue
        dlg.result = dlg.result or R.parse_action(sel)
        try:
            dlg._ok()
        except Exception as exc:
            fail.append((sel, "点确定异常 %r" % exc))
            try:
                dlg.destroy()
            except Exception:
                pass
            continue
        if not got:
            fail.append((sel, "确定后没有提交结果"))
        else:
            okn += 1
        try:
            dlg.destroy()
        except Exception:
            pass

    print("下拉项总数 %d，成功提交 %d" % (len(opts) - 2, okn))
    if fail:
        print("失败 %d 项：" % len(fail))
        for s, why in fail[:20]:
            print("   ", s, "->", why)
    else:
        print("全部通过")
    app.destroy()


app.after(300, run)
app.mainloop()
