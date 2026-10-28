# -*- coding: utf-8 -*-
"""自检 3：Win+L 处理（注册表锁屏策略 / 提权助手 / 看门狗）

验证：
1. 非管理员时 LockGuard 降级（返回 False），注册表不被弄脏
2. 提权助手 --lock-helper 在没权限时能干净退出，不残留 done 文件
3. 弹窗里"直接填 Win+L"能正确填成 Win+L（不按键盘，不会锁屏）
4. 钩子看门狗：不心跳 6 秒自动放行；有心跳一直拦着
全程不按真实按键，不会锁屏。
"""
import ctypes
import time
import winreg

try:
    ctypes.windll.shcore.SetProcessDpiAwareness(1)
except Exception:
    pass

import remote_ui as R

POL = r"Software\Microsoft\Windows\CurrentVersion\Policies\System"


def read_policy():
    try:
        with winreg.OpenKey(winreg.HKEY_CURRENT_USER, POL, 0, winreg.KEY_READ) as k:
            try:
                return winreg.QueryValueEx(k, "DisableLockWorkstation")[0]
            except FileNotFoundError:
                return None
    except FileNotFoundError:
        return None


base = read_policy()
print("[0] 初始 DisableLockWorkstation =", base, " 管理员 =",
      bool(ctypes.windll.shell32.IsUserAnAdmin()))

# ---- 1. 非管理员降级 ----
g = R.LockGuard()
ok = g.on()
print("[1] LockGuard.on() ->", ok, " 值 =", read_policy())
if not ctypes.windll.shell32.IsUserAnAdmin():
    assert ok is False, "非管理员不该成功"
    assert read_policy() == base, "降级时不该动注册表"
g.off()
print("[1] off() 后 值 =", read_policy())

# ---- 2. 助手模式 ----
rc = R.run_lock_helper(wait=1)
print("[2] run_lock_helper() 返回 =", rc, " done 文件存在 =",
      __import__("os").path.exists(R.LOCK_DONE))
assert read_policy() == base, "助手退出后注册表不干净"

# ---- 3/4. 弹窗 ----
app = R.App()
app.update_idletasks()
app.update()


class E:
    pass


def open_dlg():
    for k, c in app.cards.items():
        if isinstance(k, int) and c["label"] == "设置":
            e = E()
            e.x = (c["x1"] + c["x2"]) / 2
            e.y = (c["y1"] + c["y2"]) / 2
            app._on_click(e)
            break
    app.update_idletasks()
    dlg = None
    for w in app.winfo_children():
        if isinstance(w, R.Recorder):
            dlg = w
    dlg.update()
    print("[3] 屏蔽 =", dlg._shielded, " 禁锁屏 =", dlg._lock_on)
    print("    提示:", dlg.lbl_shield.cget("text"))
    print("    Win+L 按钮行可见 =", bool(dlg.row_winl.winfo_ismapped()))
    dlg._fill_winl()
    app.update_idletasks()
    print("[3] 直接填 Win+L ->", R.describe(*dlg.result),
          " 大字显示 =", dlg.lbl_rec.cget("text"))
    assert R.describe(*dlg.result) == "Win+L", "填出来的不是 Win+L"
    app.after(300, lambda: close_dlg(dlg))


def close_dlg(dlg):
    dlg.destroy()
    app.update_idletasks()
    print("[3] 关窗后 值 =", read_policy())
    assert read_policy() == base, "关窗后注册表不干净"
    app.after(200, watchdog_test)


def watchdog_test():
    hk = R.KeyShield()
    ok = hk.start()
    print("[4] 钩子装上 =", ok)
    if not ok:
        app.after(100, app.destroy)
        return
    t0 = time.time()
    while time.time() - t0 < 8:          # 故意不 touch，等看门狗
        app.update()
        time.sleep(0.05)
    print("[4] 不心跳 8 秒后 expired =", hk.expired, "（应 True，键盘已放行）")
    assert hk.expired, "看门狗没生效"

    hk2 = R.KeyShield()
    hk2.start()
    t0 = time.time()
    while time.time() - t0 < 8:          # 持续心跳，应该一直拦着
        app.update()
        hk2.touch()
        time.sleep(0.05)
    print("[4] 持续心跳 8 秒后 expired =", hk2.expired, "（应 False，仍在拦）")
    assert not hk2.expired, "有心跳却被放行了"
    hk2.stop()
    print("[4] 手动 stop 后 hook =", hk2._hook, "（应 None）")
    app.after(200, app.destroy)


app.after(700, open_dlg)
app.mainloop()
print("全部通过；收尾 DisableLockWorkstation =", read_policy())
