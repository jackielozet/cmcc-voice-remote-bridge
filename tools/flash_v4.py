#!/usr/bin/env python3
# -*- coding: utf-8 -*-
"""
v4 固件一键烧录：软件进下载模式 + 自动找到重新枚举的串口 + 烧录 merged.bin
=========================================================================
背景：ESP32-S3 用原生 USB-Serial/JTAG，进入下载模式后 USB 会重新枚举，
串口号会变（实测 COM11 -> COM12），所以不能写死端口。

流程：
  1. 往当前串口发 "BOOT"（固件里的命令，靠 RTC_CNTL_FORCE_DOWNLOAD_BOOT 重启进 ROM）
  2. 轮询串口列表，找 VID_303A（乐鑫）且不同于原端口的新端口
  3. esptool --before no-reset 直接烧 merged.bin 到 0x0

用法：
    python flash_v4.py COM11
    python flash_v4.py COM11 --bin ../esp/build_v4voice/xxx.ino.merged.bin
"""
import os
import sys
import time
import glob
import argparse
import subprocess

HERE = os.path.dirname(os.path.abspath(__file__))


def ports():
    try:
        from serial.tools import list_ports as lp
    except ImportError:
        print("缺 pyserial：python -m pip install pyserial -i https://mirrors.aliyun.com/pypi/simple/")
        sys.exit(2)
    return {p.device: p for p in lp.comports()}


def find_esp(exclude=None):
    """找乐鑫的串口（VID 303A = Espressif）"""
    for dev, p in ports().items():
        if exclude and dev == exclude:
            continue
        if p.vid == 0x303A:
            return dev
    return None


def send_boot(port):
    import serial
    s = serial.Serial(port, 115200, timeout=0.5)
    time.sleep(0.3)
    s.write(b"\n")
    time.sleep(0.2)
    s.write(b"BOOT\n")
    t0 = time.time()
    got = b""
    while time.time() - t0 < 2.0:
        d = s.read(256)
        if d:
            got += d
    s.close()
    return b"OK BOOT" in got


def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("port", help="板子当前串口，如 COM11")
    ap.add_argument("--bin", default=None, help="merged.bin 路径（默认自动找最新的）")
    ap.add_argument("--baud", type=int, default=460800)
    a = ap.parse_args()

    binpath = a.bin
    if binpath is None:
        # build 目录可能在仓库内，也可能在仓库的上一级（本地开发时的布局）
        roots = [os.path.dirname(HERE), os.path.dirname(os.path.dirname(HERE))]
        cands = []
        for r in roots:
            cands += glob.glob(os.path.join(r, "esp", "build_v4voice", "*.merged.bin"))
        if not cands:
            print("找不到 merged.bin，用 --bin 指定")
            return 2
        binpath = max(cands, key=os.path.getmtime)
    print("固件: %s (%d 字节)" % (binpath, os.path.getsize(binpath)))

    print("1) 让板子进下载模式（发 BOOT）...")
    ok = send_boot(a.port)
    print("   %s" % ("收到 OK BOOT" if ok else "没收到回显，仍然继续尝试"))

    print("2) 等 USB 重新枚举...")
    dev = None
    for _ in range(20):
        time.sleep(0.6)
        dev = find_esp()
        if dev:
            break
    if dev is None:
        print("   找不到乐鑫串口。请手动按住 BOOT 点一下 RST 再重试。")
        return 3
    print("   下载模式串口 = %s" % dev)

    print("3) 烧录...")
    cmd = [sys.executable, "-m", "esptool", "-p", dev, "-b", str(a.baud),
           "--before", "no-reset", "--after", "hard-reset",
           "write-flash", "0x0", binpath]
    rc = subprocess.call(cmd)
    if rc != 0:
        print("烧录失败 rc=%d" % rc)
        return rc

    print("4) 等板子重启...")
    for _ in range(20):
        time.sleep(0.6)
        back = find_esp()
        if back:
            print("   已重启，串口 = %s" % back)
            return 0
    print("   没等到重启后的串口，插拔一下 USB 看看")
    return 0


if __name__ == "__main__":
    sys.exit(main())
