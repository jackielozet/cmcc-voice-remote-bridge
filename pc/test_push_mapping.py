# -*- coding: utf-8 -*-
"""验证 _push_after_flash_run 的命令序列（桩对象，不碰真设备）。"""
import sys

HERE = r"C:\Users\lozet\WorkBuddy\2026-10-25-22-33-09\publish\pc"
sys.path.insert(0, HERE)
import remote_ui as R


class Lbl:
    def configure(self, **kw):
        if "text" in kw:
            print("[状态栏] %s" % kw["text"])


class FakeUI:
    def put(self, fn):
        try:
            fn()
        except Exception as e:
            print("[ui-err]", e)


class Probe(R.App):
    def __init__(self, ini, connected=True, cap_active=False, run_after=True):
        self.ui_q = FakeUI()
        self.sent = []
        self.ini = ini
        self.changed = {"电源"}
        self.ini_note = "ini 3 条"
        self._push_after_flash = True
        self._cap_active = cap_active
        self._run_after = run_after      # False = 只记录 after，不真执行
        self.after_calls = []
        self.ser = object() if connected else None
        self.lbl_status = Lbl()

    def __getattr__(self, name):      # 挡住 tk.Tk 的转发递归
        raise AttributeError(name)

    def send(self, cmd):
        self.sent.append(cmd)
        return True

    def after(self, ms, fn=None):
        self.after_calls.append(ms)
        if fn and self._run_after:
            fn()
        return "id"

    def _refresh_all(self):
        pass

    def _mark_dirty(self):
        print("[状态标记] changed=%r" % (self.changed or "空"))

    def _flash_print(self, s):
        sys.__stdout__.write(s)


def case(name, ini, connected=True, cap_active=False):
    print("\n" + "=" * 60)
    print("用例：%s" % name)
    p = Probe(ini, connected, cap_active)
    p._push_after_flash_run()
    print("发出 %d 条命令：" % len(p.sent))
    for c in p.sent:
        print("   ", c)
    print("SAVE 是不是最后一条：", bool(p.sent) and p.sent[-1] == "SAVE")
    print("标志已复位：", p._push_after_flash is False)


# 1) 正常：两个键，一个 0 键（无动作），一个 TV（不下发）
ini = {
    "电源": (1, [], 0),                    # n=0 -> SET .. 1 0
    "TV":   (1, [0x04], 1),                # kid=0 -> 只记不下发
    "设置": (2, [0x0C, 0xE9], 2),          # 普通多键
}
case("3 条映射，含 n=0 和 TV", ini)

# 2) 空 ini：不应该发任何东西
case("ini 为空", {})

# 3) 未连接：直接跳过
case("未连接", ini, connected=False)

# 4) 硬件抓取还没结束：应当不急着发，只挂个重试
print("\n" + "=" * 60)
print("用例：硬件抓取进行中（_cap_active=True）")
p = Probe(ini, cap_active=True, run_after=False)
p._push_after_flash_run()
print("发出 %d 条命令（应为 0）：" % len(p.sent))
print("排了重试：", p.after_calls, "（应为 [500]）")
print("标志还挂着，等重试：", p._push_after_flash is True)
