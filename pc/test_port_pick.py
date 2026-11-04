# -*- coding: utf-8 -*-
"""用假的端口列表验证 _wait_boot_port 的换机自适应逻辑（不需要硬件）。

重点：下载模式叫 COM 几完全由 Windows 决定，换台电脑可能完全不同的号，
判定只能靠 USB 位置 / 接口号 / 新出现的乐鑫口。
"""
import sys

sys.path.insert(0, r"C:\Users\lozet\WorkBuddy\2026-10-25-22-33-09\publish\pc")
import remote_ui as R

App = R.App


def port(dev, loc, esp=True):
    return {"dev": dev, "loc": loc, "esp": esp, "ser": "", "desc": "", "hwid": ""}


def run_case(name, seq, start, expect):
    """seq: 每次调 _list_ports 依次返回的快照（最后一个重复）。"""
    calls = {"n": 0}

    def fake(self):
        i = min(calls["n"], len(seq) - 1)
        calls["n"] += 1
        return list(seq[i])

    old = App._list_ports
    App._list_ports = fake
    try:
        obj = App.__new__(App)
        before = seq[0]
        orig = next((i for i in before if i["dev"] == start), None)
        old_devs = {i["dev"] for i in before}
        got = obj._wait_boot_port(
            start, old_devs,
            obj._loc_key(orig) if orig else "",
            obj._loc_full(orig) if orig else "",
            timeout=0.6)
    finally:
        App._list_ports = old
    ok = "PASS" if got == expect else "FAIL"
    print("%s  %-34s 期望 %-6r 实际 %r" % (ok, name, expect, got))
    return got == expect


results = []

# 1) 本机：COM11(.1) --touch--> COM14(.0)，同一 USB 位置
results.append(run_case(
    "同位置换口号 COM11→COM14",
    [
        [port("COM11", "1-11:x.1")],                       # 快照
        [port("COM11", "1-11:x.1")],                       # touch 后还没枚举
        [port("COM14", "1-11:x.0")],                       # 枚举出来了（新号）
    ],
    "COM11", "COM14"))

# 2) 换台电脑：口号完全没关系（COM3 → COM7）
results.append(run_case(
    "换机后口号无规律 COM3→COM7",
    [
        [port("COM3", "2-1:x.1")],
        [port("COM3", "2-1:x.1")],
        [port("COM7", "2-1:x.0")],
    ],
    "COM3", "COM7"))

# 3) Windows 复用同一个号，只有接口号变了（.1 → .0）
results.append(run_case(
    "同口号只换接口 COM5(.1)→COM5(.0)",
    [
        [port("COM5", "1-9:x.1")],
        [port("COM5", "1-9:x.1")],
        [port("COM5", "1-9:x.0")],
    ],
    "COM5", "COM5"))

# 4) 原口号消失后，出现一个 location 读不到的新乐鑫口
results.append(run_case(
    "新口没有 location（靠消失判定）",
    [
        [port("COM3", "1-2:x.1")],
        [port("COM3", "1-2:x.1")],
        [port("COM9", "")],
    ],
    "COM3", "COM9"))

# 5) touch 没生效：还在 app 里，口号/接口都没变 → 必须返回 None
results.append(run_case(
    "没进下载模式（还在 app）",
    [
        [port("COM11", "1-11:x.1")],
        [port("COM11", "1-11:x.1")],
        [port("COM11", "1-11:x.1")],
    ],
    "COM11", None))

# 6) 插着两块 S3：另一块一直在，不能连错
results.append(run_case(
    "两块板：不能连到别人板上",
    [
        [port("COM3", "1-2:x.1"), port("COM8", "1-4:x.1")],
        [port("COM3", "1-2:x.1"), port("COM8", "1-4:x.1")],
        [port("COM8", "1-4:x.1")],                 # COM3 消失，COM8 还在
    ],
    "COM3", None))

# 7) 原口号消失后又原样回来（接口号没变）→ 只是重启回 app，不能认
#    这是 1.3.1 修的一个真实误判：兜底路径曾把「原口号自己回来」当成功
results.append(run_case(
    "原口消失后原样回来（不能认）",
    [
        [port("COM11", "1-11:x.1")],
        [],
        [port("COM11", "1-11:x.1")],
    ],
    "COM11", None))

print("\n%d/%d 通过" % (sum(results), len(results)))