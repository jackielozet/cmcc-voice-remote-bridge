#!/usr/bin/env python3
# Parse an HCI btsnoop capture and dump ATT-layer traffic.
import struct
import sys
import datetime

PATH = sys.argv[1] if len(sys.argv) > 1 else r'btsnoop_hci.log'

ATT_OP = {
    0x01: 'ERROR_RSP', 0x02: 'MTU_REQ', 0x03: 'MTU_RSP',
    0x04: 'FIND_INFO_REQ', 0x05: 'FIND_INFO_RSP',
    0x06: 'READ_BY_TYPE_REQ', 0x07: 'READ_BY_TYPE_RSP',
    0x08: 'READ_REQ', 0x09: 'READ_RSP',
    0x0A: 'READ_BLOB_REQ', 0x0B: 'READ_BLOB_RSP',
    0x0C: 'READ_MULTI_REQ', 0x0D: 'READ_MULTI_RSP',
    0x0E: 'READ_BY_GRP_TYPE_REQ', 0x0F: 'READ_BY_GRP_TYPE_RSP',
    0x10: 'WRITE_REQ', 0x11: 'WRITE_RSP',
    0x12: 'PREP_WRITE_REQ', 0x13: 'PREP_WRITE_RSP',
    0x14: 'EXEC_WRITE_REQ', 0x15: 'EXEC_WRITE_RSP',
    0x16: 'PREPARE_WRITE', 0x17: 'EXEC_WRITE',
    0x18: 'NOTIFY', 0x19: 'INDICATE',
    0x1A: 'CONF', 0x1B: 'NOTIFY', 0x1D: 'INDICATE',
    0x1E: 'SIGNED_WRITE', 0x1F: 'SIGNED_WRITE_RSP',
    0x52: 'WRITE_CMD', 0xD2: 'SIGNED_WRITE_CMD',
}

EPOCH_DELTA = 62168256000000000


def ts_str(ts):
    try:
        sec = (ts - EPOCH_DELTA) / 1e6
        return datetime.datetime.fromtimestamp(sec).strftime('%H:%M:%S.%f')[:-3]
    except Exception:
        return str(ts)


def hexs(b, limit=None):
    if limit:
        b = b[:limit]
    return ' '.join('%02X' % x for x in b)


def main():
    data = open(PATH, 'rb').read()
    if data[:8] != b'btsnoop\x00':
        print('not a btsnoop file')
        return
    ver, dl = struct.unpack('>II', data[8:16])
    print('btsnoop version=%d datalink=%d (%s)' % (ver, dl, 'H4-UART' if dl == 1002 else '?'))
    pos = 16
    recs = []
    while pos + 24 <= len(data):
        olen, ilen, flags, drops, ts = struct.unpack('>IIIIQ', data[pos:pos + 24])
        pos += 24
        if pos + ilen > len(data):
            break
        pkt = data[pos:pos + ilen]
        pos += ilen
        recs.append((flags, ts, pkt))
    print('records = %d' % len(recs))

    # connection handles we saw
    handles = {}
    att_events = []
    acl_frag = {}

    for flags, ts, pkt in recs:
        if not pkt:
            continue
        h4 = pkt[0]
        direction = (flags >> 0) & 0x01
        if h4 == 0x04 and len(pkt) >= 3:
            # HCI event; look for connection complete / LE meta
            evt = pkt[1]
            if evt == 0x3E and len(pkt) > 4:
                sub = pkt[3]
                if sub == 0x01 and len(pkt) >= 20:  # LE Connection Complete
                    h = struct.unpack('<H', pkt[4:6])[0] & 0x0FFF
                    mac = ':'.join('%02X' % b for b in reversed(pkt[9:15]))
                    handles[h] = mac
        elif h4 == 0x02 and len(pkt) >= 5:
            hf = struct.unpack('<H', pkt[1:3])[0]
            h = hf & 0x0FFF
            pb = (hf >> 12) & 0x03
            dlen = struct.unpack('<H', pkt[3:5])[0]
            body = pkt[5:5 + dlen]
            if pb == 0x02:
                acl_frag[h] = bytearray(body)
            elif pb == 0x01 and h in acl_frag:
                acl_frag[h] += body
            else:
                continue
            buf = acl_frag.get(h)
            if not buf or len(buf) < 4:
                continue
            l2len = struct.unpack('<H', buf[0:2])[0]
            cid = struct.unpack('<H', buf[2:4])[0]
            if cid != 0x0004:
                continue
            if len(buf) < 4 + l2len:
                continue  # still fragmented
            att = bytes(buf[4:4 + l2len])
            acl_frag[h] = bytearray()
            if not att:
                continue
            op = att[0]
            att_events.append((ts, h, direction, op, att))

    print('handles seen: ' + ', '.join('0x%03X=%s' % (h, m) for h, m in handles.items()))

    from collections import Counter
    cnt = Counter((op, d) for _, _, d, op, _ in att_events)
    print('\n=== ATT opcode counts (rx=from controller / tx=to controller) ===')
    for (op, d), n in sorted(cnt.items(), key=lambda x: -x[1]):
        print('  %-24s dir=%s  x%d' % (ATT_OP.get(op, '0x%02X' % op), 'rx' if d == 1 else 'tx', n))

    print('\n=== all ATT exchanges (opcode name, handle, len) ===')
    for ts, h, d, op, att in att_events:
        name = ATT_OP.get(op, '0x%02X' % op)
        extra = ''
        if op in (0x09, 0x0B, 0x0D, 0x05):
            extra = '  len=%d  %s' % (len(att), hexs(att[1:41]))
        elif op in (0x1B, 0x1D):
            hh = struct.unpack('<H', att[1:3])[0]
            extra = '  handle=0x%04X  value=%s' % (hh, hexs(att[3:]))
        elif op == 0x12:
            hh = struct.unpack('<H', att[1:3])[0]
            extra = '  handle=0x%04X  value=%s' % (hh, hexs(att[3:]))
        elif op == 0x08:
            hh = struct.unpack('<H', att[1:3])[0]
            extra = '  handle=0x%04X' % hh
        elif op == 0x01:
            extra = '  %s' % hexs(att)
        print('  %s h=0x%03X %-3s %-22s%s' % (ts_str(ts), h, 'rx' if d == 1 else 'tx', name, extra))


if __name__ == '__main__':
    main()
