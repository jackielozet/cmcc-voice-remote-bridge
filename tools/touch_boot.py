# -*- coding: utf-8 -*-
"""让桥接固件自己重启进 ROM 下载模式 —— 等效于手动按 BOOT 键，不用碰板子。

原理（cores/esp32/USBCDC.cpp:283 `_onLineCoding`）：
    if (reboot_enable && _bit_rate == 1200) usb_persist_restart(RESTART_BOOTLOADER);
其内部把 RTC_CNTL_OPTION1_REG 置 FORCE_DOWNLOAD_BOOT，下次复位直接进 ROM。
Arduino IDE 上传就是靠这条（注释原话："ArduinoIDE sends LineCoding with
1200bps baud to reset the device"）。

为什么不走 DTR/RTS？
    桥接固件是 TinyUSB 复合 USB，PID 恰好等于 ESP32-S3 硬件 USB-Serial/JTAG 的
    0x1001，esptool 会误判成硬件 JTAG 口、用 JTAG 控制请求去复位（TinyUSB 不响应）
    → "No serial data received"。而 USBCDC 的 DTR/RTS 状态机在本机实测**完全无效**
    （信号线回读正确、固件毫无反应）。1200bps 必中。

副作用：芯片复位后 USB 重新枚举，**COM 号会变**（实测 COM11 → COM10，
          接口从 MI_01 变 MI_00），随后 esptool 要用新口 + `--before no_reset`。

用法：
    python touch_boot.py COM11              # 只复位，打印新端口
    python touch_boot.py COM11 --flash 固件.merged.bin   # 复位并刷写

跑之前先关掉上位机 / 串口助手，任何占着该端口的程序都会让本脚本打不开。
"""
import subprocess
import sys
import time

import serial
import serial.tools.list_ports as lp

ESPTOOL = r"C:\Users\lozet\esptool\venv\Scripts\esptool.exe"


def snap():
    return {p.device: ((p.description or "")[:32], (p.location or ""),
                       (p.hwid or "")) for p in lp.comports()}


def show(tag):
    d = snap()
    print("[%s] 共 %d 个串口" % (tag, len(d)))
    for dev in sorted(d):
        print("   %-7s %-32s %s" % (dev, d[dev][0], d[dev][1]))
    return d


def is_esp(hwid):
    return "303A" in (hwid or "").upper() or "ESPRESSIF" in (hwid or "").upper()


def touch(port):
    """发 1200bps touch，返回重新枚举后的新端口号；失败返回 None。"""
    before = snap()
    want = (before.get(port, ("", "", ""))[1] or "").split(":")[0]
    print("[1] 以 115200 打开 %s，再把线路速率改成 1200 …" % port)
    try:
        s = serial.Serial(port, 115200, timeout=0.2)
    except Exception as exc:
        print("[!] 打不开 %s：%r" % (port, exc))
        print("    先关掉上位机 / 串口助手。")
        return None
    try:
        s.baudrate = 1200          # ← 触发 usb_persist_restart(RESTART_BOOTLOADER)
    except Exception:
        pass                       # 芯片此时已断开，报 OSError 22 属正常
    try:
        s.close()
    except Exception:
        pass

    print("[2] 等芯片进下载模式并重新枚举（最多 7 秒）…")
    old = set(before)
    for i in range(28):
        time.sleep(0.25)
        now = snap()
        if want:
            for dev, v in now.items():
                if (v[1] or "").split(":")[0] == want and is_esp(v[2]):
                    print("    +%.1fs → %s（同一 USB 位置 %s）" % (0.25 * (i + 1), dev, want))
                    return dev
        for dev, v in now.items():
            if dev not in old and is_esp(v[2]):
                print("    +%.1fs → 新出现的口 %s" % (0.25 * (i + 1), dev))
                return dev
    print("[!] 端口没有重新枚举，固件可能关掉了 1200bps 复位。")
    return None


def esptool(port, *args):
    print(">>> esptool --port %s %s" % (port, " ".join(args)))
    return subprocess.run([ESPTOOL, "--port", port] + list(args)).returncode


def main():
    args = [a for a in sys.argv[1:]]
    port = args[0] if args and not args[0].startswith("-") else "COM11"
    fw = None
    if "--flash" in args:
        i = args.index("--flash")
        if i + 1 >= len(args):
            print("[!] --flash 后面要跟固件路径"); return 2
        fw = args[i + 1]

    show("复位前")
    new = touch(port)
    if not new:
        return 2
    show("复位后")

    print("[3] 用 --before no_reset 连接 %s 验证" % new)
    esptool(new, "--before", "no_reset", "chip-id")
    if not fw:
        print("\n>>> 已可连接。刷写命令（先关掉上位机）：")
        print('    esptool --chip esp32s3 --port %s --baud 921600 --before no_reset '
              'write_flash 0x0 "%s"' % (new, "固件.merged.bin"))
        return 0

    print("\n[4] 开始刷写 %s" % fw)
    rc = esptool(new, "--chip", "esp32s3", "--baud", "921600",
                 "--before", "no_reset", "write_flash", "0x0", fw)
    print("返回码 %d" % rc)
    return rc


if __name__ == "__main__":
    sys.exit(main())
