#!/usr/bin/env python3
"""Probe the CMCC remote in an *unpaired* state.

The earlier conclusion (0x1812 characteristics are AccessDenied at the kernel
level) was reached while the device was paired, which is when BthLEEnum
creates a HID service node. With the pairing removed there is no such node, so
re-test whether a plain BLE central can reach the HOGP characteristics.

Read-only: nothing is written except the CCCD when --notify is passed.
"""
import argparse
import asyncio
import sys

from bleak import BleakClient, BleakScanner
from bleak.exc import BleakError

TARGET_MAC = "0C:F3:CC:63:10:E3"
TARGET_NAME = "CMCC_Voice_Remote"

HID_SERVICE = "00001812-0000-1000-8000-00805f9b34fb"
REPORT = "00002a4d-0000-1000-8000-00805f9b34fb"
REPORT_MAP = "00002a4b-0000-1000-8000-00805f9b34fb"
REPORT_REF = "00002908-0000-1000-8000-00805f9b34fb"
HID_INFO = "00002a4a-0000-1000-8000-00805f9b34fb"
PROTO_MODE = "00002a4e-0000-1000-8000-00805f9b34fb"
CCCD = "00002902-0000-1000-8000-00805f9b34fb"


def short(uuid: str) -> str:
    u = uuid.lower()
    if u.startswith("0000") and u.endswith("-0000-1000-8000-00805f9b34fb"):
        return "0x" + u[4:8].upper()
    return u


def hexs(b) -> str:
    return " ".join("%02X" % x for x in b)


async def find_target(timeout: float):
    print("scanning %.0fs ..." % timeout)
    seen = []
    devices = await BleakScanner.discover(timeout=timeout, return_adv=True)
    for addr, (dev, adv) in devices.items():
        name = dev.name or adv.local_name or ""
        seen.append((addr, name, adv.rssi))
        hit = (addr.upper() == TARGET_MAC.upper()) or (name == TARGET_NAME)
        if hit:
            print("  >> TARGET %s  %s  rssi=%d" % (addr, name or "-", adv.rssi))
            return dev
        if name:
            print("     %-28s %s rssi=%d" % (name, addr, adv.rssi))
    if not seen:
        print("  !! nothing found at all")
    return None


async def probe(dev, do_notify: bool, seconds: int):
    print("\nconnecting (unpaired)...")
    client = BleakClient(dev, timeout=20.0)
    try:
        await client.connect()
    except BleakError as exc:
        print("  !! connect failed: %s" % exc)
        return

    print("  connected=%s  mtu=%s" % (client.is_connected, getattr(client, "mtu_size", "?")))

    services = client.services
    print("\n=== services (%d) ===" % len(list(services)))
    has_hid = False
    for svc in services:
        tag = "   <== HID" if svc.uuid.lower() == HID_SERVICE else ""
        print("  [%s] %s%s" % (short(svc.uuid), svc.description, tag))
        if svc.uuid.lower() == HID_SERVICE:
            has_hid = True
        for ch in svc.characteristics:
            print("      chr %s h=0x%04X props=%s" % (
                short(ch.uuid), ch.handle, ",".join(ch.properties)))

    if not has_hid:
        print("\n!! 0x1812 absent even while unpaired -> the filter is not pairing-related")
        await client.disconnect()
        return

    print("\n=== HID service is visible. Reading what we can ===")
    for uuid, label in ((HID_INFO, "HID Information"), (REPORT_MAP, "Report Map"), (PROTO_MODE, "Protocol Mode")):
        try:
            val = await client.read_gatt_char(uuid)
            txt = hexs(val)
            if uuid == REPORT_MAP:
                print("  %-16s %d bytes" % (label, len(val)))
                for i in range(0, len(val), 28):
                    print("      " + hexs(val[i:i + 28]))
                open("report_map_unpaired.bin", "wb").write(bytes(val))
            else:
                print("  %-16s %s" % (label, txt))
        except Exception as exc:
            print("  %-16s !! %s: %s" % (label, type(exc).__name__, exc))

    hid = next((s for s in services if s.uuid.lower() == HID_SERVICE), None)
    reports = [c for c in hid.characteristics if c.uuid.lower() == REPORT]
    print("\n=== %d Report characteristics ===" % len(reports))
    refs = {}
    for ch in reports:
        ref_val = None
        for d in ch.descriptors:
            if d.uuid.lower() == REPORT_REF:
                try:
                    ref_val = bytes(await client.read_gatt_descriptor(d.handle))
                except Exception as exc:
                    ref_val = "err:%s" % type(exc).__name__
                break
        refs[ch.handle] = ref_val
        if isinstance(ref_val, bytes) and len(ref_val) >= 2:
            print("  h=0x%04X id=0x%02X type=%d  props=%s" % (
                ch.handle, ref_val[0], ref_val[1], ",".join(ch.properties)))
        else:
            print("  h=0x%04X ref=%s  props=%s" % (ch.handle, ref_val, ",".join(ch.properties)))

    if not do_notify:
        await client.disconnect()
        return

    print("\n=== subscribing to every notifiable Report for %ds ===" % seconds)
    print("    press buttons on the remote now\n")

    def make_cb(handle):
        def cb(_sender, data: bytearray):
            ref = refs.get(handle)
            rid = ref[0] if isinstance(ref, bytes) and len(ref) >= 2 else 0
            print("  [h=0x%04X rpt=0x%02X] %s" % (handle, rid, hexs(bytes(data))))
        return cb

    subscribed = 0
    for ch in reports:
        if "notify" not in ch.properties and "indicate" not in ch.properties:
            continue
        try:
            await client.start_notify(ch, make_cb(ch.handle))
            subscribed += 1
            print("  subscribed h=0x%04X" % ch.handle)
        except Exception as exc:
            print("  subscribe h=0x%04X !! %s: %s" % (ch.handle, type(exc).__name__, exc))

    print("\n  %d/%d subscribed" % (subscribed, len(reports)))
    if subscribed == 0:
        print("  !! nothing subscribed -> CCCD write refused (likely needs encryption)")
        await client.disconnect()
        return

    try:
        await asyncio.sleep(seconds)
    finally:
        for ch in reports:
            try:
                await client.stop_notify(ch)
            except Exception:
                pass
        await client.disconnect()
        print("\ndisconnected")


async def main():
    ap = argparse.ArgumentParser()
    ap.add_argument("--notify", action="store_true", help="subscribe and dump reports")
    ap.add_argument("--seconds", type=int, default=90)
    ap.add_argument("--scan", type=float, default=8.0)
    ap.add_argument("--wait", type=float, default=0.0, help="keep rescanning this long")
    args = ap.parse_args()

    deadline = asyncio.get_event_loop().time() + args.wait
    dev = await find_target(args.scan)
    while dev is None and args.wait > 0 and asyncio.get_event_loop().time() < deadline:
        print("  ... rescanning (press a button on the remote)")
        dev = await find_target(args.scan)

    if dev is None:
        print("\nno target. press a button on the remote to wake it, then re-run.")
        return 1
    await probe(dev, args.notify, args.seconds)
    return 0


if __name__ == "__main__":
    sys.exit(asyncio.run(main()))
