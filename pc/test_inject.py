# -*- coding: utf-8 -*-
"""验证注入（剪贴板+Ctrl+V）和键盘钩子拦截，用自建窗口闭环检测。"""
import ctypes
import os
import sys
import time
import tkinter as tk

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr_win  # noqa: E402

VK_H, VK_LWIN = 0x48, 0x5B

root = tk.Tk()
root.title("asr_win 注入/钩子自测")
root.geometry("480x150+300+300")
ent = tk.Entry(root, width=50, font=("Microsoft YaHei UI", 11))
ent.pack(pady=20)
lbl = tk.Label(root, text="等待测试…", font=("Microsoft YaHei UI", 10))
lbl.pack()

result = {}


def log(s):
    print(s, flush=True)
    lbl.configure(text=s)


def t_clipboard():
    ok = asr_win.clipboard_set_text("剪贴板测试 ABC 123")
    got = asr_win.clipboard_get_text()
    log("剪贴板写=%s 读回=%r" % (ok, got))
    root.after(300, t_inject)


def t_inject():
    ent.delete(0, "end")
    root.lift()
    root.focus_force()
    ent.focus_set()
    root.update()
    time.sleep(0.2)
    ok = asr_win.inject_text("你好世界测试一二三")
    log("注入调用=%s, 等 Ctrl+V 生效…" % ok)
    root.after(900, t_check)


def t_check():
    got = ent.get()
    result["inject"] = got
    log("Entry 收到: %r  %s" % (got, "OK" if "你好世界" in got else "失败"))
    root.after(400, t_hook)


def t_hook():
    """装钩子，再用 SendInput 模拟 Win+H，看是否被吞并触发回调。"""
    cnt = {"d": 0, "u": 0}

    def on_d():
        cnt["d"] += 1

    def on_u():
        cnt["u"] += 1

    hk = asr_win.VoiceKeyHook(VK_H, {VK_LWIN}, on_d, on_u)
    ok = hk.start()
    log("钩子安装=%s" % ok)
    if not ok:
        result["hook"] = False
        root.after(300, finish)
        return

    time.sleep(0.3)
    # 模拟：Win down -> H down -> H up -> Win up
    seq = [asr_win._mk_key(VK_LWIN), asr_win._mk_key(VK_H),
           asr_win._mk_key(VK_H, True), asr_win._mk_key(VK_LWIN, True)]
    arr = (asr_win.INPUT * len(seq))(*seq)
    asr_win._u32.SendInput(len(seq), arr, ctypes.sizeof(asr_win.INPUT))
    time.sleep(0.5)

    asr_win.VoiceKeyHook  # 保持引用
    hk.stop()
    result["hook"] = (cnt["d"] == 1 and cnt["u"] == 1)
    log("钩子回调 down=%d up=%d -> %s" % (cnt["d"], cnt["u"],
                                          "OK" if result["hook"] else "失败"))
    root.after(400, finish)


def finish():
    print("\n=== 结果 ===")
    print("注入:", "OK" if "你好世界" in (result.get("inject") or "") else "失败")
    print("钩子:", "OK" if result.get("hook") else "失败")
    root.destroy()


root.after(400, t_clipboard)
root.mainloop()
