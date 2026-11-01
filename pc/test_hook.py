# -*- coding: utf-8 -*-
"""不开 GUI 也能测：剪贴板往返 + 键盘钩子拦截（SendInput 模拟 Win+H）"""
import ctypes
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr_win  # noqa: E402

VK_H, VK_LWIN = 0x48, 0x5B


def test_clipboard():
    ok_w = asr_win.clipboard_set_text("剪贴板往返测试 ABC 123")
    got = asr_win.clipboard_get_text()
    print("[剪贴板] 写=%s 读回=%r -> %s" %
          (ok_w, got, "OK" if got == "剪贴板往返测试 ABC 123" else "FAIL"))
    return got == "剪贴板往返测试 ABC 123"


def test_sendinput():
    n = asr_win.send_ctrl_v()
    print("[SendInput] Ctrl+V 发出=%s" % n)
    return bool(n)


def test_hook():
    cnt = {"d": 0, "u": 0}
    hk = asr_win.VoiceKeyHook(VK_H, {VK_LWIN},
                              lambda: cnt.__setitem__("d", cnt["d"] + 1),
                              lambda: cnt.__setitem__("u", cnt["u"] + 1))
    ok = hk.start()
    print("[钩子] 安装=%s" % ok)
    if not ok:
        return False
    time.sleep(0.35)
    seq = [asr_win._mk_key(VK_LWIN), asr_win._mk_key(VK_H),
           asr_win._mk_key(VK_H, True), asr_win._mk_key(VK_LWIN, True)]
    arr = (asr_win.INPUT * len(seq))(*seq)
    send = asr_win._u32.SendInput(len(seq), arr, ctypes.sizeof(asr_win.INPUT))
    time.sleep(0.45)
    hk.stop()
    print("[钩子] SendInput=%d  回调 down=%d up=%d -> %s" %
          (send, cnt["d"], cnt["u"],
           "OK" if (cnt["d"] == 1 and cnt["u"] == 1) else "FAIL"))
    return cnt["d"] == 1 and cnt["u"] == 1


def test_hook_nomod():
    """不带修饰键的目标键也要能拦（比如语音键映射成 F5）"""
    cnt = {"d": 0, "u": 0}
    hk = asr_win.VoiceKeyHook(0x74, set(),  # VK_F5
                              lambda: cnt.__setitem__("d", cnt["d"] + 1),
                              lambda: cnt.__setitem__("u", cnt["u"] + 1))
    if not hk.start():
        print("[钩子2] 安装失败")
        return False
    time.sleep(0.3)
    seq = [asr_win._mk_key(0x74), asr_win._mk_key(0x74, True)]
    arr = (asr_win.INPUT * len(seq))(*seq)
    asr_win._u32.SendInput(len(seq), arr, ctypes.sizeof(asr_win.INPUT))
    time.sleep(0.4)
    hk.stop()
    ok = cnt["d"] == 1 and cnt["u"] == 1
    print("[钩子2] F5 单独作为目标 down=%d up=%d -> %s" %
          (cnt["d"], cnt["u"], "OK" if ok else "FAIL"))
    return ok


if __name__ == "__main__":
    r = []
    r.append(("剪贴板", test_clipboard()))
    r.append(("SendInput", test_sendinput()))
    r.append(("钩子 Win+H", test_hook()))
    r.append(("钩子 无修饰键", test_hook_nomod()))
    print("\n=== 汇总 ===")
    for name, ok in r:
        print("  %-16s %s" % (name, "OK" if ok else "FAIL"))
    sys.exit(0 if all(o for _, o in r) else 1)
