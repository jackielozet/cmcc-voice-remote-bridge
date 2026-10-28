import asyncio, sys, time, datetime

ADDR = "0C:F3:CC:63:10:E3"
SVC_HID = "00001812-0000-1000-8000-00805f9b34fb"
NAMES = {
    "00002a4a-0000-1000-8000-00805f9b34fb": "HIDInfo",
    "00002a4b-0000-1000-8000-00805f9b34fb": "ReportMap",
    "00002a4c-0000-1000-8000-00805f9b34fb": "HIDCtrlPoint",
    "00002a4d-0000-1000-8000-00805f9b34fb": "Report",
    "00002a4e-0000-1000-8000-00805f9b34fb": "ProtocolMode",
    "00002a22-0000-1000-8000-00805f9b34fb": "BootKbIn",
    "00002a32-0000-1000-8000-00805f9b34fb": "BootKbOut",
}
LOG = "hid_capture.log"
LISTEN = int(sys.argv[1]) if len(sys.argv) > 1 else 150

def ts(): return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

def out(m):
    print(m, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(m + "\n")
    except Exception: pass

def on_report(ch):
    def cb(sender, data):
        b = bytes(data)
        hexs = " ".join(f"{x:02X}" for x in b) if b else "<empty>"
        out(f"[{ts()}] REPORT {str(ch.uuid)[:8]} ch={ch.handle} len={len(b):2d}  {hexs}")
    return cb

async def find_device(timeout_total=90):
    from bleak import BleakClient, BleakScanner
    deadline = time.time() + timeout_total
    while time.time() < deadline:
        try:
            d = await BleakScanner.find_device_by_address(ADDR, timeout=6.0)
            if d:
                return d
        except Exception as e:
            out(f"[{ts()}] scan err: {type(e).__name__}: {e}")
        out(f"[{ts()}] not advertising yet, retry ...")
        await asyncio.sleep(2)
    return None

async def main():
    from bleak import BleakClient
    out("")
    out(f"=== {ts()} HID probe start, listen {LISTEN}s ===")

    dev = await find_device()
    if dev is None:
        out("device never appeared - press a key on the remote and rerun")
        return

    out(f"[{ts()}] found: {dev.name} {dev.address}")
    c = BleakClient(dev, timeout=25.0)
    await c.connect()
    out(f"[{ts()}] connected, mtu={c.mtu_size}")

    svcs = list(c.services)
    out(f"services visible: {len(svcs)}")
    for s in svcs:
        out(f"   SVC {s.uuid}  handle={s.handle}")

    hid = None
    for s in svcs:
        if str(s.uuid).lower().startswith("00001812"):
            hid = s
    if hid is None:
        out("!! 0x1812 STILL hidden - node disable did not free the service")
        await c.disconnect()
        return

    out(f"[{ts()}] HID service VISIBLE: {hid.uuid} handle={hid.handle}")
    chs = list(hid.characteristics)
    out(f"HID chars: {len(chs)}")
    for ch in chs:
        u = str(ch.uuid).lower()
        out(f"   CHR {u} [{NAMES.get(u,'?')}] props={ch.properties} handle={ch.handle}")
        for d in ch.descriptors:
            try:
                v = await c.read_gatt_descriptor(d)
                out(f"        desc {d.uuid} = {bytes(v).hex(' ').upper()}")
            except Exception as e:
                out(f"        desc {d.uuid} read fail: {type(e).__name__}")

    for ch in chs:
        u = str(ch.uuid).lower()
        if u.startswith("00002a4b"):
            v = await c.read_gatt_char(ch)
            b = bytes(v)
            out(f"[{ts()}] REPORT MAP ({len(b)} bytes):")
            out("   HEX: " + b.hex(" ").upper())
            out("   REPR: " + repr(b))

    for ch in chs:
        u = str(ch.uuid).lower()
        if u.startswith("00002a4a") or u.startswith("00002a4e"):
            try:
                v = await c.read_gatt_char(ch)
                out(f"[{ts()}] READ {NAMES.get(u)} -> {bytes(v).hex(' ').upper()}")
            except Exception as e:
                out(f"[{ts()}] READ {NAMES.get(u)} fail: {type(e).__name__}")

    subscribed = 0
    for ch in chs:
        if ch.properties & 0x10 or ch.properties & 0x20:
            try:
                await c.start_notify(ch, on_report(ch))
                out(f"[{ts()}] subscribed notify handle={ch.handle} uuid={str(ch.uuid)[:8]}")
                subscribed += 1
            except Exception as e:
                out(f"[{ts()}] subscribe handle={ch.handle} fail: {type(e).__name__}: {e}")

    out(f"[{ts()}] >>> {subscribed} report channel(s) armed. PRESS KEYS ON THE REMOTE NOW <<<")

    deadline = time.time() + LISTEN
    while c.is_connected and time.time() < deadline:
        await asyncio.sleep(0.3)

    out(f"[{ts()}] listen window over")
    try: await c.disconnect()
    except Exception: pass
    out(f"=== {ts()} end ===")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        out(f"FATAL {type(e).__name__}: {e}")
