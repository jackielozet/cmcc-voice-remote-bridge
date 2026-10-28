#!/usr/bin/env python3
# UART sniffer for CMCC_Voice_Remote debug pads (GND + TX only, listen mode).
# ASCII-only output on purpose: Windows console is GBK and mangles non-ASCII.
#
# Usage:
#   python uart_capture.py list
#   python uart_capture.py scan COM6 [seconds_per_baud]
#   python uart_capture.py cap  COM6 115200 [seconds]
#
# Raw bytes always saved to uart_<port>_<baud>_<stamp>.bin
# Decoded text saved to   uart_<port>_<baud>_<stamp>.log

import sys
import os
import time
import datetime

BAUDS = [115200, 921600, 1000000, 1500000, 460800, 230400, 57600, 38400, 9600]

OK_BYTES = set(range(32, 127)) | {9, 10, 13}


def stamp():
    return datetime.datetime.now().strftime("%Y%m%d_%H%M%S")


def now():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]


def score(data):
    if not data:
        return 0.0, 0, 0
    printable = sum(1 for b in data if b in OK_BYTES)
    ratio = printable / len(data)
    newlines = data.count(10)
    best = cur = 0
    for b in data:
        if b in OK_BYTES:
            cur += 1
            if cur > best:
                best = cur
        else:
            cur = 0
    return ratio, newlines, best


def preview(data, limit=200):
    out = []
    for b in data[:limit]:
        out.append(chr(b) if (32 <= b < 127 or b in (9, 13)) else ".")
    return "".join(out).strip()


def do_list():
    try:
        from serial.tools import list_ports
    except ImportError:
        print("pyserial not installed. run: python -m pip install pyserial")
        return
    ports = list(list_ports.comports())
    if not ports:
        print("no serial ports found")
        return
    for p in ports:
        print("%-8s %-45s %s" % (p.device, p.description, p.hwid))


def do_scan(port, per_baud):
    import serial
    results = []
    print("scanning %s, %.1fs per baud rate" % (port, per_baud))
    for baud in BAUDS:
        try:
            ser = serial.Serial(port, baud, timeout=0.2)
        except Exception as exc:
            print("%-10s %7d  OPEN FAIL: %s" % (now(), baud, exc))
            continue
        buf = bytearray()
        t0 = time.time()
        while time.time() - t0 < per_baud:
            chunk = ser.read(4096)
            if chunk:
                buf.extend(chunk)
        try:
            ser.close()
        except Exception:
            pass
        ratio, newlines, best = score(bytes(buf))
        results.append((baud, len(buf), ratio, newlines, best, preview(bytes(buf))))
        print("%-10s %7d  bytes=%-6d printable=%5.1f%% lines=%-4d maxrun=%-4d"
              % (now(), baud, len(buf), ratio * 100, newlines, best))
        if buf:
            print("           sample: %r" % preview(bytes(buf), 120))

    print("")
    print("--- ranking (higher printable% and longer printable run wins) ---")

    def weight(item):
        _baud, _n, ratio, newlines, best, _s = item
        return ratio * 100 + min(best, 40) * 0.5 + min(newlines, 10)

    for baud, n, ratio, newlines, best, sample in sorted(results, key=weight, reverse=True):
        print("  %7d  n=%-6d printable=%5.1f%%  lines=%-4d maxrun=%-4d  %r"
              % (baud, n, ratio * 100, newlines, best, sample[:60]))
    print("")
    print("If one rate clearly stands out (printable > 90%), lock it:")
    print("  python uart_capture.py cap %s <baud> 600" % port)


def find_ch340():
    """Locate the CH340 adapter by USB identity, so a replug into another USB
    port (which renumbers the COM device) does not break the wait."""
    try:
        from serial.tools import list_ports
    except ImportError:
        return None
    for p in list_ports.comports():
        hwid = (p.hwid or "").upper()
        desc = (p.description or "").upper()
        if "1A86:7523" in hwid or "CH340" in desc:
            return p.device
    return None


def do_loop(port, baud):
    """Loopback self-test. Requires TXD and RXD on the adapter to be shorted
    with a jumper, with the remote NOT attached. Proves the adapter + driver
    receive path independently of the target board."""
    import serial
    payload = b"UART-LOOP-TEST-0123456789\r\n"
    print("loopback test on %s @ %d" % (port, baud))
    print("expect TXD and RXD on the adapter to be shorted together")
    try:
        ser = serial.Serial(port, baud, timeout=1.0, write_timeout=1.0)
    except Exception as exc:
        print("OPEN FAIL: %s" % exc)
        return
    time.sleep(0.1)
    try:
        ser.reset_input_buffer()
        ser.reset_output_buffer()
        wrote = ser.write(payload)
        ser.flush()
        print("wrote %d bytes: %r" % (wrote, payload))
        got = ser.read(len(payload) + 16)
        print("read  %d bytes: %r" % (len(got), got))
        if got:
            print("hex: %s" % " ".join("%02X" % b for b in got))
        if got.strip() == payload.strip():
            print("RESULT: PASS - adapter and receive path are working")
        elif got:
            print("RESULT: PARTIAL - got bytes back but they differ (check baud/line noise)")
        else:
            print("RESULT: FAIL - nothing came back, so either the jumper is not")
            print("        making contact, or this adapter's RX path is dead")
    finally:
        try:
            ser.close()
        except Exception:
            pass


def do_live(port, total_seconds, dwell):
    """Open the port ONCE and switch baud rates on the live handle.
    CH340 adapters are known to wedge on repeated open/close, so this avoids
    reopening between rates."""
    import serial
    order = [115200, 921600, 1500000, 1000000, 460800]
    try:
        ser = serial.Serial(port, order[0], timeout=0.2)
    except Exception as exc:
        print("OPEN FAIL: %s" % exc, flush=True)
        return
    print("opened %s once @ %d; switching baud on the live handle" % (port, order[0]), flush=True)
    t_end = time.time() + total_seconds
    i = 0
    try:
        while time.time() < t_end:
            baud = order[i % len(order)]
            i += 1
            remain = t_end - time.time()
            if remain <= 0:
                break
            this = min(dwell, remain)
            try:
                ser.baudrate = baud
                ser.reset_input_buffer()
            except Exception as exc:
                print("%-10s switch to %d FAILED: %s" % (now(), baud, exc), flush=True)
                time.sleep(0.5)
                continue
            print("%-10s listening %d for %.0fs" % (now(), baud, this), flush=True)
            buf = bytearray()
            t0 = time.time()
            while time.time() - t0 < this:
                chunk = ser.read(4096)
                if chunk:
                    buf.extend(chunk)
                    hexs = " ".join("%02X" % b for b in chunk[:64])
                    txt = "".join(chr(b) if (32 <= b < 127 or b in (9, 13)) else "." for b in chunk[:64])
                    print("%-10s %7d RX +%d: %s | %s" % (now(), baud, len(chunk), hexs, txt), flush=True)
            if buf:
                ratio, newlines, best = score(bytes(buf))
                tag = "uart_live_%d_%s" % (baud, stamp())
                with open(tag + ".bin", "wb") as fb:
                    fb.write(bytes(buf))
                print("%-10s %7d TOTAL %d bytes printable=%.1f%% lines=%d -> %s.bin"
                      % (now(), baud, len(buf), ratio * 100, newlines, tag), flush=True)
            else:
                print("%-10s %7d  (no data)" % (now(), baud), flush=True)
    finally:
        try:
            ser.close()
        except Exception:
            pass
    print("live session finished", flush=True)


def do_wait(port, max_wait, total, dwell):
    """Poll until the port can actually be opened, then start a cycle capture.
    Used when the adapter has stalled and the operator is about to replug it."""
    import serial
    from serial.tools import list_ports
    t_end = time.time() + max_wait
    attempt = 0
    print("waiting up to %ds for the CH340 adapter to become usable" % max_wait, flush=True)
    while time.time() < t_end:
        attempt += 1
        present = [p.device for p in list_ports.comports()]
        cand = port if port in present else find_ch340()
        if cand is None:
            if attempt == 1 or attempt % 10 == 0:
                print("%-10s attempt %d: adapter not present (ports=%s)" % (now(), attempt, present), flush=True)
            time.sleep(2)
            continue
        if cand != port:
            print("%-10s port changed: %s -> %s" % (now(), port, cand), flush=True)
            port = cand
        try:
            probe = serial.Serial(cand, 115200, timeout=0.1)
            probe.close()
            print("%-10s PORT ALIVE after %d attempts -> starting capture" % (now(), attempt), flush=True)
            do_live(cand, total, dwell)
            return
        except Exception as exc:
            msg = str(exc).replace("\n", " ")[:90]
            if attempt == 1 or attempt % 10 == 0:
                print("%-10s attempt %d not ready: %s" % (now(), attempt, msg), flush=True)
            time.sleep(2)
    print("gave up after %ds without a usable port" % max_wait, flush=True)


def do_cycle(port, total_seconds, dwell):
    """Rotate through the most likely log baud rates, printing any byte that
    arrives. Use when the rate is unknown and a one-shot burst (boot log) is
    expected: a fixed wrong rate would miss it entirely."""
    import serial
    order = [115200, 1500000, 921600, 1000000, 460800]
    t_end = time.time() + total_seconds
    idx = 0
    print("cycle session: %ds total, %.0fs per baud, order=%s"
          % (total_seconds, dwell, order))
    while time.time() < t_end:
        baud = order[idx % len(order)]
        idx += 1
        remain = t_end - time.time()
        if remain <= 0:
            break
        this = min(dwell, remain)
        try:
            ser = serial.Serial(port, baud, timeout=0.2)
        except Exception as exc:
            print("%-10s %7d OPEN FAIL %s" % (now(), baud, exc), flush=True)
            time.sleep(1)
            continue
        print("%-10s %7d listening %.0fs" % (now(), baud, this), flush=True)
        buf = bytearray()
        t0 = time.time()
        while time.time() - t0 < this:
            chunk = ser.read(4096)
            if chunk:
                buf.extend(chunk)
                hexs = " ".join("%02X" % b for b in chunk[:48])
                print("%-10s %7d RX +%d bytes: %s" % (now(), baud, len(chunk), hexs), flush=True)
        try:
            ser.close()
        except Exception:
            pass
        if buf:
            ratio, newlines, best = score(bytes(buf))
            tag = "uart_cycle_%d_%s" % (baud, stamp())
            with open(tag + ".bin", "wb") as fb:
                fb.write(bytes(buf))
            print("%-10s %7d TOTAL %d bytes printable=%.1f%% lines=%d maxrun=%d -> %s.bin"
                  % (now(), baud, len(buf), ratio * 100, newlines, best, tag), flush=True)
        else:
            print("%-10s %7d  (no data)" % (now(), baud), flush=True)
    print("cycle session finished")


def do_lock(port, baud, seconds):
    """Lock one baud rate, print every chunk as hex+text in real time."""
    import serial
    try:
        ser = serial.Serial(port, baud, timeout=0.15)
    except Exception as exc:
        print("OPEN FAIL: %s" % exc, flush=True)
        return
    print("%-10s locked %s @ %d 8N1, %.0fs window" % (now(), port, baud, seconds), flush=True)
    base = "uart_lock_%d_%s" % (baud, stamp())
    bin_path = base + ".bin"
    t_end = time.time() + seconds
    total = 0
    with open(bin_path, "wb") as fb:
        try:
            while time.time() < t_end:
                chunk = ser.read(4096)
                if not chunk:
                    continue
                fb.write(chunk)
                fb.flush()
                total += len(chunk)
                hexs = " ".join("%02X" % b for b in chunk[:80])
                txt = "".join(chr(b) if (32 <= b < 127 or b in (9, 13)) else "." for b in chunk[:80])
                print("%-10s +%-4d %s | %s" % (now(), len(chunk), hexs, txt), flush=True)
        except KeyboardInterrupt:
            print("stopped by user", flush=True)
        finally:
            try:
                ser.close()
            except Exception:
                pass
    print("done. %d bytes -> %s" % (total, os.path.abspath(bin_path)), flush=True)


def do_cap(port, baud, seconds):
    import serial
    base = "uart_%s_%d_%s" % (port.replace("/", "_").replace("\\", "_"), baud, stamp())
    bin_path = base + ".bin"
    log_path = base + ".log"
    print("opening %s @ %d 8N1" % (port, baud))
    ser = serial.Serial(port, baud, timeout=0.2)
    print("capturing %ds -> %s / %s" % (seconds, bin_path, log_path))
    print("press keys on the remote now (Ctrl-C to stop early)")
    line = bytearray()
    total = 0
    t_start = time.time()
    try:
        with open(bin_path, "wb") as fb, open(log_path, "w", encoding="utf-8", errors="replace") as fl:
            while time.time() - t_start < seconds:
                chunk = ser.read(4096)
                if not chunk:
                    continue
                fb.write(chunk)
                fb.flush()
                total += len(chunk)
                for b in chunk:
                    if b == 10:
                        text = bytes(line).decode("utf-8", errors="replace").rstrip("\r")
                        print("%-12s | %s" % (now(), text), flush=True)
                        fl.write("%s | %s\n" % (now(), text))
                        fl.flush()
                        line = bytearray()
                    else:
                        if len(line) < 4096:
                            line.append(b)
            if line:
                text = bytes(line).decode("utf-8", errors="replace").rstrip("\r")
                print("%-12s | %s  [no newline]" % (now(), text), flush=True)
                fl.write("%s | %s  [no newline]\n" % (now(), text))
    except KeyboardInterrupt:
        print("")
        print("stopped by user")
    finally:
        try:
            ser.close()
        except Exception:
            pass
    print("")
    print("done. %d bytes total" % total)
    print("raw : %s" % os.path.abspath(bin_path))
    print("text: %s" % os.path.abspath(log_path))


def main():
    args = sys.argv[1:]
    if not args or args[0] in ("-h", "--help", "help"):
        print(__doc__ or "")
        print("commands:")
        print("  list")
        print("  scan <port> [seconds_per_baud]")
        print("  cap  <port> <baud> [seconds]")
        return
    cmd = args[0].lower()
    if cmd == "list":
        do_list()
    elif cmd == "scan":
        if len(args) < 2:
            print("usage: python uart_capture.py scan COM6 [seconds_per_baud]")
            return
        per = float(args[2]) if len(args) > 2 else 4.0
        do_scan(args[1], per)
    elif cmd == "live":
        port = args[1] if len(args) > 1 else "COM3"
        total = int(args[2]) if len(args) > 2 else 120
        dwell = float(args[3]) if len(args) > 3 else 20.0
        do_live(port, total, dwell)
    elif cmd == "loop":
        port = args[1] if len(args) > 1 else "COM3"
        baud = int(args[2]) if len(args) > 2 else 115200
        do_loop(port, baud)
    elif cmd == "wait":
        if len(args) < 2:
            print("usage: python uart_capture.py wait COM3 [max_wait] [capture_seconds] [dwell]")
            return
        max_wait = int(args[2]) if len(args) > 2 else 600
        total = int(args[3]) if len(args) > 3 else 180
        dwell = float(args[4]) if len(args) > 4 else 20.0
        do_wait(args[1], max_wait, total, dwell)
    elif cmd == "cycle":
        if len(args) < 2:
            print("usage: python uart_capture.py cycle COM3 [seconds] [dwell]")
            return
        total = int(args[2]) if len(args) > 2 else 180
        dwell = float(args[3]) if len(args) > 3 else 20.0
        do_cycle(args[1], total, dwell)
    elif cmd == "lock":
        if len(args) < 3:
            print("usage: python uart_capture.py lock COM3 115200 [seconds]")
            return
        secs = int(args[3]) if len(args) > 3 else 90
        do_lock(args[1], int(args[2]), secs)
    elif cmd == "cap":
        if len(args) < 3:
            print("usage: python uart_capture.py cap COM6 115200 [seconds]")
            return
        secs = int(args[3]) if len(args) > 3 else 300
        do_cap(args[1], int(args[2]), secs)
    else:
        print("unknown command: %s" % cmd)


if __name__ == "__main__":
    main()
