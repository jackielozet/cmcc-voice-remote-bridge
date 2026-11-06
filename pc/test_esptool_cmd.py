# -*- coding: utf-8 -*-
"""锁定 esptool 命令拼接：argv 里不许含程序名，前缀只能在 _run_esptool 加一次。

1.3.3 的真事故（2026-11-06 实测）：
    `_esptool_cmd` 里拼了 `list(et) + [...]`，`_run_esptool` 里又 `list(et) + list(argv)`，
    装了外部 esptool 的机器上实际执行的是
        esptool.EXE  esptool.EXE --chip esp32s3 --port COM10 ...
    esptool 把第一个参数当子命令，报 `No such command`、返回码 2。
    没装外部 esptool 的机器 `et` 为空，两处都拼不出东西 → 一切正常，
    这正是 1.3.1 / 1.3.3 在实测机上没暴露的原因。

跑法：python test_esptool_cmd.py   （不需要硬件，不启动 Tk）
"""
import sys
import types

sys.path.insert(0, r"C:\Users\lozet\WorkBuddy\2026-10-25-22-33-09\publish\pc")
import remote_ui as R

App = R.App
EXE = r"C:\Python313\Scripts\esptool.EXE"
FW = r"C:\tmp\cmcc_test.merged.bin"

fails = []


def check(name, cond, detail=""):
    if cond:
        print("  PASS  %s" % name)
    else:
        print("  FAIL  %s  %s" % (name, detail))
        fails.append(name)


class FakeUI:
    """_flash_print 走 ui.put 调度，这里同步执行即可。"""
    def put(self, fn):
        fn()


def new_app():
    obj = App.__new__(App)
    obj._flash_print = lambda s: None
    return obj


print("[1] _esptool_cmd 返回的 argv 不含程序名")
obj = new_app()
argv = obj._esptool_cmd("esp32s3", "COM10", "921600", FW)
check("argv 里没有 exe 路径", EXE not in argv, repr(argv))
check("argv 以 --chip 开头", argv[0] == "--chip", repr(argv[:3]))
check("argv 结尾是 write_flash 0x0 <fw>",
      argv[-3:] == ["write_flash", "0x0", FW], repr(argv[-3:]))
check("端口/波特率/芯片都在",
      all(x in argv for x in ("esp32s3", "COM10", "921600")), repr(argv))

print("[2] _run_esptool 加前缀只加一次（外部 esptool 存在时）")
obj = new_app()
seen = {}


def fake_exec(self, cmd, ui, flag, timeout=900.0):
    seen["cmd"] = list(cmd)
    return (0, "")


obj._exec_esptool = types.MethodType(fake_exec, obj)
obj._run_esptool(argv, [EXE], FakeUI(), 30.0)
cmd = seen.get("cmd", [])
check("程序路径只出现一次", cmd.count(EXE) == 1, repr(cmd))
check("第 0 项是程序、第 1 项是 --chip",
      cmd[:2] == [EXE, "--chip"], repr(cmd[:3]))

print("[3] 内嵌路径（et 为空）不拼任何前缀，argv 原样传")
obj = new_app()
seen.clear()
obj._exec_esptool_inproc = types.MethodType(
    lambda self, a, ui, t=900.0: (seen.setdefault("argv", list(a)), (0, ""))[1], obj)
obj._run_esptool(argv, [], FakeUI(), 30.0)
check("内嵌 argv 与原始 argv 一致", seen.get("argv") == list(argv), repr(seen.get("argv")))

print("[4] 带 before 参数时顺序不乱")
obj = new_app()
argv2 = obj._esptool_cmd("esp32s3", "COM10", "460800", FW, before="default_reset")
i_before = argv2.index("--before")
check("--before 在 write_flash 之前",
      i_before < argv2.index("write_flash"), repr(argv2))

print()
if fails:
    print("FAILED: %d 项 —— %s" % (len(fails), ", ".join(fails)))
    sys.exit(1)
print("ALL PASS")
