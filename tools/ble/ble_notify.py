import asyncio, sys, time, datetime

ADDR = "0C:F3:CC:63:10:E3"
CHR_VOICE = "00006487-3c17-d293-8e48-14fe2e4da212"   # 0x6287 svc, write+notify
CHR_BATT = "00002a19-0000-1000-8000-00805f9b34fb"    # control channel

DURATION = int(sys.argv[1]) if len(sys.argv) > 1 else 600
LOG = "ble_notify.log"

def ts():
    return datetime.datetime.now().strftime("%H:%M:%S.%f")[:-3]

def out(msg):
    print(msg, flush=True)
    try:
        with open(LOG, "a", encoding="utf-8") as f:
            f.write(msg + "\n")
    except Exception:
        pass

def dump(tag, data):
    b = bytes(data)
    hexs = " ".join(f"{x:02X}" for x in b) if b else "<empty>"
    txt = ""
    try:
        s = b.decode("utf-8")
        if s and all(31 < ord(c) < 127 for c in s):
            txt = f'  ascii="{s}"'
    except Exception:
        pass
    out(f"[{ts()}] {tag} len={len(b):3d}  {hexs}{txt}")

def on_voice(sender, data):
    dump("VOICE 0x6487", data)

def on_batt(sender, data):
    b = bytes(data)
    out(f"[{ts()}] BATT  0x2A19  {b[0] if b else '?'}%")

def on_disc(client):
    out(f"[{ts()}] !! DISCONNECTED")

async def heartbeat(client, deadline):
    while client.is_connected and time.time() < deadline:
        await asyncio.sleep(30)
        if client.is_connected and time.time() < deadline:
            out(f"[{ts()}] ..alive, waiting (mtu={getattr(client,'mtu_size','?')})")

async def main():
    from bleak import BleakClient
    out("")
    out(f"=== {ts()} start, duration {DURATION}s, notify: 0x6487 + 0x2A19(control) ===")
    deadline = time.time() + DURATION

    while time.time() < deadline:
        client = BleakClient(ADDR, disconnected_callback=on_disc, timeout=25.0)
        try:
            out(f"[{ts()}] connecting ...")
            await client.connect()
            out(f"[{ts()}] CONNECTED mtu={getattr(client,'mtu_size','?')}")

            await client.start_notify(CHR_VOICE, on_voice)
            out(f"[{ts()}] notify 0x6487  -> enabled")
            try:
                await client.start_notify(CHR_BATT, on_batt)
                out(f"[{ts()}] notify 0x2A19  -> enabled (control)")
            except Exception as e:
                out(f"[{ts()}] notify 0x2A19  -> failed: {e}")

            hb = asyncio.create_task(heartbeat(client, deadline))
            while client.is_connected and time.time() < deadline:
                await asyncio.sleep(0.3)
            hb.cancel()
            if client.is_connected:
                out(f"[{ts()}] window closed, disconnecting")
                try:
                    await client.stop_notify(CHR_VOICE)
                except Exception:
                    pass
                await client.disconnect()
                break
        except Exception as e:
            out(f"[{ts()}] error: {type(e).__name__}: {e}")
        finally:
            try:
                if client.is_connected:
                    await client.disconnect()
            except Exception:
                pass
        if time.time() < deadline:
            await asyncio.sleep(2)
    out(f"=== {ts()} end ===")

if __name__ == "__main__":
    try:
        asyncio.run(main())
    except KeyboardInterrupt:
        out("interrupted")
