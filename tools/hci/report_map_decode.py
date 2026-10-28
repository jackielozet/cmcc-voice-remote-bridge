#!/usr/bin/env python3
# Assemble the full HID Report Map from the btsnoop capture and decode it.
import struct
import sys

PATH = sys.argv[1] if len(sys.argv) > 1 else r'btsnoop_hci.log'

ATT_USAGE_PAGE = {
    0x01: 'Generic Desktop', 0x07: 'Keyboard/Keypad', 0x08: 'LED',
    0x09: 'Button', 0x0C: 'Consumer',
}
GD_USAGE = {0x01: 'Pointer', 0x02: 'Mouse', 0x06: 'Keyboard', 0x30: 'X', 0x31: 'Y', 0x38: 'Wheel'}
MAIN_ITEMS = {0x8: 'Input', 0x9: 'Output', 0xB: 'Feature', 0xA: 'Collection', 0xC: 'End Collection'}
GLOBAL_ITEMS = {0x0: 'Usage Page', 0x1: 'Logical Minimum', 0x2: 'Logical Maximum',
                0x7: 'Report Size', 0x8: 'Report ID', 0x9: 'Report Count',
                0xA: 'Push', 0xB: 'Pop', 0x5: 'Unit', 0x6: 'Unit Exponent'}
LOCAL_ITEMS = {0x0: 'Usage', 0x1: 'Usage Minimum', 0x2: 'Usage Maximum',
               0x3: 'Designator Index', 0x4: 'Designator Minimum',
               0x5: 'Designator Maximum', 0x7: 'String Index'}


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
            out.append((ts, h, flags & 1, att))
    return out


def decode_report_map(rm):
    lines = []
    i = 0
    rpt_id = None
    rpt_size = None
    rpt_count = None
    gm = []
    gmm = []
    usage_page = None
    indent = 0
    col_depth = 0
    while i < len(rm):
        b = rm[i]
        if b == 0xFE:  # long item
            if i + 1 >= len(rm):
                break
            ln = rm[i + 1]
            i += 2 + 1 + ln
            continue
        size_code = b & 0x03
        itype = (b >> 2) & 0x03
        tag = (b >> 4) & 0x0F
        sz = {0: 0, 1: 1, 2: 2, 3: 4}[size_code]
        raw = rm[i + 1:i + 1 + sz]
        i += 1 + sz
        val = 0
        for k, bb in enumerate(raw):
            val |= bb << (8 * k)
        key = (itype, tag)
        table = {0: MAIN_ITEMS, 1: GLOBAL_ITEMS, 2: LOCAL_ITEMS}.get(itype, {})
        name = table.get(tag, 'ERR type%d/tag0x%X' % (itype, tag))
        indent_s = '  ' * indent
        extra = ''
        if itype == 1 and tag == 0x0:
            usage_page = val
            extra = '  <- %s' % ATT_USAGE_PAGE.get(val, '0x%04X' % val)
        elif itype == 2 and tag == 0x0:
            if usage_page == 0x01:
                extra = '  <- %s' % GD_USAGE.get(val, '0x%04X' % val)
        elif itype == 1 and tag == 0x7:
            rpt_size = val
        elif itype == 1 and tag == 0x9:
            rpt_count = val
        elif itype == 1 and tag == 0x8:
            rpt_id = val
            extra = '  <<< REPORT ID 0x%02X' % val
        elif itype == 0 and tag in (0x8, 0x9, 0xB):
            bits = (rpt_size or 0) * (rpt_count or 0)
            extra = '  = %d x %d bit = %d bits / %d bytes' % (
                rpt_count or 0, rpt_size or 0, bits, (bits + 7) // 8)
            if tag == 0x8:
                extra += '   [rpt 0x%02X IN]' % (rpt_id or 0)
            elif tag == 0x9:
                extra += '   [rpt 0x%02X OUT]' % (rpt_id or 0)
        elif itype == 0 and tag == 0xA:
            col_depth += 1
            indent = col_depth
            extra = '  (collection type %d)' % val
        elif itype == 0 and tag == 0xC:
            col_depth = max(0, col_depth - 1)
            indent = col_depth
        lines.append('%s%-18s %s%s' % (indent_s, name, ('0x%X' % val) if sz else '', extra))
    return lines


def main():
    recs = load(PATH)
    ev = att_events(recs)
    rm = bytearray()
    started = False
    for ts, h, d, att in ev:
        if att[0] == 0x0B and len(att) > 20 and att[1] == 0x05 and att[2] == 0x01 and att[3] == 0x09:
            rm = bytearray(att[1:])
            started = True
        elif started and att[0] == 0x0D:
            rm.extend(att[1:])
    print('REPORT MAP: %d bytes' % len(rm))
    print(' '.join('%02X' % b for b in rm))
    print()
    print('=== decoded ===')
    for ln in decode_report_map(rm):
        print(ln)
    open('report_map.bin', 'wb').write(rm)
    print('\nraw saved to report_map.bin')


if __name__ == '__main__':
    main()
