import asyncio, sys, datetime

ADDR = "0C:F3:CC:63:10:E3"
SVC_HID = "00001812-0000-1000-8000-00805f9b34fb"
NAMES = {
    "00002a4a-0000-1000-8000-00805f9b34fb": "HID Information",
    "00002a4b-0000-1000-8000-00805f9b34fb": "Report Map",
    "00002a4c-0000-1000-8000-00805f9b34fb": "HID Control Point",
    "00002a4d-0000-1000-8000-00805f9b34fb": "Report",
    "00002a4e-0000-1000-8000-00805f9b34fb": "Protocol Mode",
    "00002a22-0000-1000-8000-00805f9b34fb": "Boot Keyboard Input",
    "00002a32-0000-1000-8000-00805f9b34fb": "Boot Keyboard Output",
}

def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

async def main():
    from bleak import BleakClient
    c = BleakClient(ADDR, timeout=25.0)
    await c.connect()
    print(f"[{ts()}] connected mtu={c.mtu_size}", flush=True)

    svcs = c.services
    target = None
    for s in svcs:
        if str(s.uuid).lower().startswith("00001812"):
            target = s
    if target is None:
        print("HID service 0x1812 NOT found in service list", flush=True)
        return

    print(f"[{ts()}] HID service found: {target.uuid}  handle={target.handle}", flush=True)

    n = 0
    for ch in target.characteristics:
        n += 1
        u = str(ch.uuid).lower()
        label = NAMES.get(u, "?")
        print(f"   [CHR] {u}  {label}  props={ch.properties}  handle={ch.handle}", flush=True)

    print(f"total chars visible: {n}", flush=True)

    for ch in target.characteristics:
        u = str(ch.uuid).lower()
        if u.startswith("00002a4b") or u.startswith("00002a4a") or u.startswith("00002a4e"):
            try:
                v = await c.read_gatt_char(ch)
                print(f"   READ {NAMES.get(u,u)} -> {v.hex(' ').upper()}", flush=True)
            except Exception as e:
                print(f"   READ {NAMES.get(u,u)} FAILED: {type(e).__name__}: {e}", flush=True)

    await c.disconnect()

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except Exception as e:
        print(f"FATAL: {type(e).__name__}: {e}")
