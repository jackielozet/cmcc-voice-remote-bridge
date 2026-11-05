# -*- coding: utf-8 -*-
"""校验固件 DEFAULTS[] 与上位机 default_items() 是否逐条一致。

这两处是「刷完即用」的两条独立兜底路径：
  · 固件那份：整片刷 merged.bin 清空 NVS，setup() 直接落它，不连电脑也生效
  · 上位机那份：本地没有 remotekey.ini 时由刷写成功的自动推送下发

任一处改了另一处没跟上，同一块板就会出现两套键位。所以改表之后跑一遍：

    python test_default_sync.py

退出码 0 = 一致，1 = 有差异（会逐条列出）。
"""
import glob
import os
import re
import sys

HERE = os.path.dirname(os.path.abspath(__file__))
sys.path.insert(0, HERE)

import remote_ui as R                                    # noqa: E402

FW_GLOB = os.path.join(HERE, "..", "firmware", "cmcc_s3_bridge_v4voice_*", "*.ino")


def newest_firmware():
    """取最新的一个 v4voice 固件源码（按目录名排，名字里带时间戳）。"""
    files = [f for f in glob.glob(FW_GLOB) if os.path.isfile(f)]
    if not files:
        return None
    return max(files, key=lambda f: os.path.basename(os.path.dirname(f)))


def parse_fw_defaults(path):
    """从 .ino 里抠出 DEFAULTS[] -> {id: (out_type, keys, n)}。"""
    txt = open(path, encoding="utf-8", errors="replace").read()
    start = txt.index("static const Bind DEFAULTS[]")
    end = txt.index("static const int DEFAULT_COUNT")
    seg = txt[start:end]
    out = {}
    for m in re.finditer(r"\{\s*(0x[0-9A-Fa-f]{4})\s*,\s*(\d+)\s*,\s*(\d+)\s*,\s*\{([^}]*)\}\s*\}",
                         seg):
        kid = int(m.group(1), 16)
        ot = int(m.group(2))
        n = int(m.group(3))
        keys = [int(x, 16) for x in re.findall(r"0x([0-9A-Fa-f]{1,2})", m.group(4))]
        keys = (keys + [0, 0, 0, 0])[:4]
        out[kid] = (ot, keys[:n], n)
    return out


def main():
    fw_path = newest_firmware()
    if not fw_path:
        print("找不到固件源码：%s" % FW_GLOB)
        return 1
    print("固件源码：%s" % os.path.relpath(fw_path, HERE))
    fw = parse_fw_defaults(fw_path)
    print("  DEFAULTS 条数：%d" % len(fw))

    py = {}
    for label, ot, keys, n in R.default_items():
        kid = next((k for l, k, _, _ in R.REMOTE if l == label), 0)
        if not kid:                      # TV 走红外，两边都不该有
            continue
        py[kid] = (ot, list(keys)[:n], n)
    print("  default_items 条数：%d（已排除红外键 TV）" % len(py))

    # 固件里那几条不在界面上的备用项（回车/Esc/退格/Delete 的裸 usage）不参与比对
    remote_ids = {k for _, k, _, _ in R.REMOTE if k}
    fw_cmp = {k: v for k, v in fw.items() if k in remote_ids}

    print("\n%-8s %-6s %-24s %-24s %s" % ("键", "id", "固件 DEFAULTS", "上位机默认", "判定"))
    print("-" * 86)
    bad = 0
    for label, kid, _, _ in R.REMOTE:
        if not kid:
            continue
        a, b = fw_cmp.get(kid), py.get(kid)
        f = lambda t: "缺失" if not t else (
            "不响应" if t[2] == 0 else "ot=%d %s (n=%d)" % (t[0], " ".join("%02X" % k for k in t[1]), t[2]))
        if a == b:
            verdict = "一致"
        else:
            verdict = "*** 不一致"
            bad += 1
        if a != b or "-v" in sys.argv:
            print("%-8s %04X   %-24s %-24s %s" % (label, kid, f(a), f(b), verdict))

    print("\n固件多出来、界面没有的备用项：%s"
          % ", ".join("%04X" % k for k in sorted(set(fw) - remote_ids)) or "无")
    if bad:
        print("\n%d 条不一致 —— 两条兜底路径会给出不同结果，请同步。" % bad)
        return 1
    print("\n全部一致（%d 条）。" % len(py))
    return 0


if __name__ == "__main__":
    sys.exit(main())
