#!/usr/bin/env python3
# Extract HID over GATT details: report map, report handles, key notifications.
import struct
import sys
import datetime
from collections import Counter

PATH = sys.argv[1] if len(sys.argv) > 1 else r'btsnoop_hci.log'
EPOCH_DELTA = 62168256000000000

ATT_OP = {
    0x01: 'ERROR_RSP', 0x02: 'MTU_REQ', 0x03: 'MTU_RSP',
    0x04: 'FIND_INFO_REQ', 0x05: 'FIND_INFO_RSP',
    0x06: 'READ_BY_TYPE_REQ', 0x07: 'READ_BY_TYPE_RSP',
    0x08: 'READ_REQ', 0x09: 'READ_RSP',
    0x0A: 'READ_BLOB_REQ', 0x0B: 'READ_BLOB_RSP',
    0x0C: 'READ_MULTI_REQ', 0x0D: 'READ_MULTI_RSP',
    0x0E: 'READ_BY_GRP_REQ', 0x0F: 'READ_BY_GRP_RSP',
    0x10: 'WRITE_REQ', 0x11: 'WRITE_RSP',
    0x12: 'WRITE_CMD', 0x13: 'PREP_WRITE_REQ',
    0x16: 'PREP_WRITE_RSP', 0x18: 'EXEC_WRITE_REQ', 0x19: 'EXEC_WRITE_RSP',
    0x1B: 'NOTIFY', 0x1D: 'INDICATE', 0x1E: 'CONF',
    0xD2: 'SIGNED_WRITE_CMD',
}


def ts_str(ts):
    try:
        return datetime.datetime.fromtimestamp((ts - EPOCH_DELTA) / 1e6).strftime('%H:%M:%S.%f')[:-3]
    except Exception:
        return str(ts)


def hexs(b):
    return ' '.join('%02X' % x for x in b)


def load(path):
    data = open(path, 'rb').read()
    pos = 16
    recs = []
    while pos + 24 <= len(data):
        olen, ilen, flags, drops, ts = struct.unpack('>IIIIQ', data[pos:pos + 24])
        pos += 24
        if pos + ilen > len(data):
            break
        recs.append((flags, ts, data[pos:pos + ilen]))
        pos += ilen
    return recs


def att_events(recs):
    out = []
    frag = {}
    for flags, ts, pkt in recs:
        if not pkt or pkt[0] != 0x02 or len(pkt) < 5:
            continue
        hf = struct.unpack('<H', pkt[1:3])[0]
        h = hf & 0x0FFF
        pb = (hf >> 12) & 0x03
        dlen = struct.unpack('<H', pkt[3:5])[0]
        body = pkt[5:5 + dlen]
        if pb == 0x02:
            frag[h] = bytearray(body)
        elif pb == 0x01 and h in frag:
            frag[h] += body
        else:
            continue
        buf = frag.get(h)
        if not buf or len(buf) < 4:
            continue
        l2len = struct.unpack('<H', buf[0:2])[0]
        cid = struct.unpack('<H', buf[2:4])[0]
        if cid != 0x0004 or len(buf) < 4 + l2len:
            continue
        att = bytes(buf[4:4 + l2len])
        frag[h] = bytearray()
        if att:
            out.append((ts, h, (flags & 1), att))
    return out


def main():
    recs = load(PATH)
    ev = att_events(recs)
    print('records=%d  att_events=%d' % (len(recs), len(ev)))

    # ---- 1. characteristic table (from READ_BY_TYPE_RSP on type 0x2803) ----
    chars = {}
    for ts, h, d, att in ev:
        if att[0] == 0x09 and len(att) > 2:
            entlen = att[1]
            body = att[2:]
            if entlen == 7:
                for i in range(0, len(body) - 6, 7):
                    hdl = struct.unpack('<H', body[i:i + 2])[0]
                    props = body[i + 2]
                    vhdl = struct.unpack('<H', body[i + 3:i + 5])[0]
                    uuid = struct.unpack('<H', body[i + 5:i + 7])[0]
                    chars[(hdl, vhdl)] = (props, uuid)
    print('\n=== characteristic declarations (handle -> value handle / uuid / props) ===')
    for (hdl, vhdl), (props, uuid) in sorted(chars.items()):
        print('  0x%04X -> val 0x%04X  uuid 0x%04X  props=0x%02X' % (hdl, vhdl, uuid, props))

    # ---- 2. report map: read responses for value handle of uuid 0x2A4B ----
    rm_handle = None
    for (hdl, vhdl), (props, uuid) in chars.items():
        if uuid == 0x2A4B:
            rm_handle = vhdl
    print('\n=== report map value handle = %s ===' % ('0x%04X' % rm_handle if rm_handle else 'NOT FOUND'))

    # collect (request handle -> assembled value) by pairing READ_REQ/READ_BLOB_REQ with responses
    reads = {}
    report_map = bytearray()
    seq = []
    for ts, h, d, att in ev:
        op = att[0]
        if op == 0x0A and len(att) >= 3:  # READ_REQ
            hdl = struct.unpack('<H', att[1:3])[0]
            seq.append(('req', hdl))
        elif op == 0x0C and len(att) >= 5:  # READ_BLOB_REQ
            hdl = struct.unpack('<H', att[1:3])[0]
            off = struct.unpack('<H', att[3:5])[0]
            seq.append(('blob', hdl, off))
        elif op == 0x09 and len(att) > 1:  # READ_RSP
            if seq:
                req = seq.pop(0)
                if req[0] == 'req':
                    reads.setdefault(req[1], []).append(bytes(att[1:]))
                else:
                    if req[1] == rm_handle:
                        if req[2] == 0:
                            report_map = bytearray()
                        report_map.extend(att[1:])

    if rm_handle and report_map:
        print('REPORT MAP (%d bytes):' % len(report_map))
        print(hexs(report_map))

    # ---- 3. notifications ----
    notif = [(ts, struct.unpack('<H', att[1:3])[0], bytes(att[3:])) for ts, h, d, att in ev if att[0] == 0x1B and len(att) > 3]
    print('\n=== notifications by handle ===')
    c = Counter(hdl for _, hdl, _ in notif)
    for hdl, n in sorted(c.items()):
        sample = [v for _, hh, v in notif if hh == hdl][:6]
        print('  handle 0x%04X  x%-4d  samples: %s' % (hdl, n, ' | '.join(hexs(s) for s in sample)))

    print('\n=== first 40 notifications (time / handle / value) ===')
    for ts, hdl, v in notif[:40]:
        print('  %s  0x%04X  %s' % (ts_str(ts), hdl, hexs(v)))


if __name__ == '__main__':
    main()
