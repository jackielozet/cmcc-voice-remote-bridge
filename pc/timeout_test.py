# -*- coding: utf-8 -*-
"""验证 ASR 15 秒熔断：故意让识别卡死，看是否自动放弃并恢复。

跑法：python timeout_test.py
用 mainloop + after 调度，跟正式运行时一致（工作线程回调 after 才有效）。
"""
import os
import sys
import time

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import numpy as np
import remote_ui
import asr_win

remote_ui.ASR_TIMEOUT_MS = 3000          # 测试用 3 秒，正式 15 秒

asr_win.decode_codec = lambda codec: b"\x00\x00" * 16000
asr_win.prepare_audio = lambda a, sr=16000: (np.zeros(sr, dtype="float32"), "测试")
asr_win.inject_text = lambda t: True


class HungEngine:
    """永远卡住的引擎，模拟 onnxruntime 死循环。"""
    def __init__(self):
        self.reset_count = 0
        self.calls = 0

    def available(self):
        return True

    def load(self):
        return object()

    def reset(self):
        self.reset_count += 1

    def transcribe(self, s, rate=16000, waiter=None, lock_timeout=8.0):
        self.calls += 1
        time.sleep(120)
        return "不该出现"


class GoodEngine(HungEngine):
    def transcribe(self, s, rate=16000, waiter=None, lock_timeout=8.0):
        self.calls += 1
        time.sleep(0.05)
        return "你好世界"


app = remote_ui.App()
app.asr_framer = asr_win.Framer()
app.asr_on = True

hung = HungEngine()
good = GoodEngine()
res = {}


def feed(nbytes=6000):
    with app.asr_lock:
        app.asr_codec = bytearray(b"\x00" * nbytes)
    app.asr_last = 0.0


def s1_dispatch():
    print("== 第 1 句：引擎卡死 ==")
    app.asr_engine = hung
    feed()
    app._asr_tick()
    app.after(500, s1_check)


def s1_check():
    print("  派发 0.5s 后 busy=%s 状态=%s"
          % (app.asr_busy, app.lbl_asr.cget("text")))
    app.after(3200, s1_after)


def s1_after():
    st = app.lbl_asr.cget("text")
    print("  3.7s 后 busy=%s" % app.asr_busy)
    print("  状态=%s" % st)
    print("  引擎重置次数=%d  transcribe 调用=%d" % (hung.reset_count, hung.calls))
    res["timeout"] = (not app.asr_busy) and ("超时" in st) and hung.reset_count >= 1
    res["gen"] = app.asr_gen
    s2_dispatch()


def s2_dispatch():
    print("\n== 第 2 句：换正常引擎，验证立即恢复 ==")
    app.asr_engine = good
    feed()
    app._asr_tick()
    app.after(900, s2_check)


def s2_check():
    st = app.lbl_asr.cget("text")
    print("  busy=%s 状态=%s" % (app.asr_busy, st))
    res["recover"] = (not app.asr_busy) and ("你好世界" in st)
    s3_dispatch()


def s3_dispatch():
    print("\n== 第 3 句：超时后迟到的结果必须丢弃 ==")
    app.asr_engine = hung
    feed()
    app._asr_tick()
    res["gen3"] = app.asr_gen
    app.after(3400, s3_after)


def s3_after():
    old_gen = res["gen3"]
    app._asr_done("迟到的文字", None, 100, 2.0, "测试", old_gen)
    st = app.lbl_asr.cget("text")
    print("  注入旧代次结果后状态=%s" % st)
    print("  当前 gen=%d，旧 gen=%d" % (app.asr_gen, old_gen))
    res["late"] = ("迟到的文字" not in st)
    print("\n结果：熔断=%s 恢复=%s 迟到丢弃=%s"
          % (res.get("timeout"), res.get("recover"), res.get("late")))
    print("全部通过" if all(res.get(k) for k in ("timeout", "recover", "late"))
          else "有失败项")
    app.after(200, app.destroy)


app.after(300, s1_dispatch)
app.mainloop()
