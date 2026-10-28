#!/usr/bin/env python3
"""Strict cross-check of the CMCC remote's HOGP report table vs its report map.

Parses the Android btsnoop HCI log, pairs ATT requests with responses, then
compares every Report characteristic's Report Reference descriptor against the
report IDs actually declared in the 224-byte report map.
"""
import struct
import sys
from collections import Counter

PATH = sys.argv[1] if len(sys.argv) > 1 else 'btsnoop_hci.log'

EPOCH_DELTA = 62168256000000000

REQ_OP = {0x02: 'MTU', 0x04: 'FIND_INFO', 0x06: 'READ_BY_TYPE',
          0x08: 'READ', 0x0A: 'READ_BLOB', 0x0C: 'READ_MULTI',
          0x10: 'WRITE', 0x12: 'WRITE_CMD', 0x52: 'CONFIG'}


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
    """Yield (ts, direction, payload) with direction 0=host->ctrl, 1=ctrl->host."""
    out = []
    frag = {}
    for flags, ts, pkt in recs:
        if not pkt or pkt[0] != 0x02 or len(pkt) < 5:
            continue
        hf = struct.unpack('<H', pkt[1:3])[0]
        handle = hf & 0x0FFF
        pb = (hf >> 12) & 0x03
        dlen = struct.unpack('<H', pkt[3:5])[0]
        body = pkt[5:5 + dlen]
        direction = 1 if (flags & 0x01) else 0

        if pb == 0x00:          # first non-flush fragment
            frag[handle] = bytearray(body)
            continue
        if pb == 0x01:          # continuation
            if handle in frag:
                frag[handle] += body
            continue
        if pb == 0x02:          # complete L2CAP PDU in one packet
            out.append((ts, direction, bytes(body)))
            continue

    # flush any lone first-fragment that had no continuation recorded
    for handle, buf in frag.items():
        if buf:
            out.append((0, 0, bytes(buf)))
    return out


def hexs(b):
    return ' '.join('%02X' % x for x in b)


def main():
    recs = load(PATH)
    ev = att_events(recs)
    print('ACL events: %d' % len(ev))
    print('opcode histogram: %s' % dict(Counter(hex(p[0]) for _, _, p in ev if p)))
    print()

    # --- pair READ / READ_BLOB requests with their responses ---------------
    reads = []              # (handle, offset, response_bytes)
    pending = None
    by_type = []            # (start, end, uuid, [(handle, value), ...])
    pending_bt = None

    for ts, direction, p in ev:
        if not p:
            continue
        op = p[0]
        if direction == 0:                      # host -> controller
            if op == 0x08 and len(p) >= 3:
                pending = ('read', struct.unpack('<H', p[1:3])[0], 0)
            elif op == 0x0A and len(p) >= 5:
                pending = ('read', struct.unpack('<H', p[1:3])[0],
                           struct.unpack('<H', p[3:5])[0])
            elif op == 0x06 and len(p) >= 7:
                pending_bt = (struct.unpack('<H', p[1:3])[0],
                              struct.unpack('<H', p[3:5])[0],
                              struct.unpack('<H', p[5:7])[0])
            elif op == 0x52 and len(p) >= 5:    # Write Request (CCCD)
                pass
        else:                                    # controller -> host
            if op == 0x09 and pending:           # READ_RSP
                reads.append((pending[1], 0, p[1:]))
                pending = None
            elif op == 0x0D and pending:         # READ_BLOB_RSP
                reads.append((pending[1], pending[2], p[1:]))
                pending = None
            elif op == 0x07 and pending_bt:      # READ_BY_TYPE_RSP
                start, end, uuid = pending_bt
                entries = []
                b = p[1:]
                i = 0
                while i < len(b):
                    ln = b[i]
                    if ln == 0 or i + 1 + ln > len(b):
                        break
                    entries.append(bytes(b[i + 1:i + 1 + ln]))
                    i += 1 + ln
                by_type.append((start, end, uuid, entries))
                pending_bt = None

    # --- characteristic declarations (UUID 0x2803) -------------------------
    decls = {}
    for start, end, uuid, entries in by_type:
        if uuid != 0x2803:
            continue
        for e in entries:
            if len(e) >= 5:
                props = e[0]
                vh = struct.unpack('<H', e[1:3])[0]
                u = struct.unpack('<H', e[3:5])[0]
                decls[vh] = (props, u)

    print('=== characteristic declarations found: %d ===' % len(decls))
    name = {0x2A4A: 'HID Information', 0x2A4B: 'Report Map',
            0x2A4C: 'HID Control Point', 0x2A4D: 'Report',
            0x2A4E: 'Protocol Mode'}
    for vh in sorted(decls):
        props, u = decls[vh]
        tag = name.get(u, '0x%04X' % u)
        print('  value_handle=0x%04X  props=0x%02X  uuid=0x%04X  %s' % (vh, props, u, tag))
    print()

    # --- report map reassembly (0x2A4B) ------------------------------------
    map_handle = next((vh for vh, (_, u) in decls.items() if u == 0x2A4B), None)
    report_map = b''
    if map_handle is not None:
        chunks = {}
        for h, off, data in reads:
            if h == map_handle:
                chunks[off] = data
        if 0 in chunks:
            report_map = chunks[0]
            off = len(report_map)
            while off in chunks:
                report_map += chunks[off]
                off = len(report_map)
        print('=== Report Map (%d bytes, handle 0x%04X) ===' % (len(report_map), map_handle))
        print(hexs(report_map))
        open('report_map_recheck.bin', 'wb').write(report_map)
        print()

    # --- report IDs declared in the map ------------------------------------
    declared = {}
    cur_id = 0
    i = 0
    d = report_map
    while i < len(d):
        prefix = d[i]
        if prefix == 0xFE:                      # long item
            if i + 2 >= len(d):
                break
            size = d[i + 1]
            i += 3 + size
            continue
        size = {0: 0, 1: 1, 2: 2, 3: 4}[prefix & 0x03]
        itype = (prefix >> 2) & 0x03
        tag = (prefix >> 4) & 0x0F
        val = int.from_bytes(d[i + 1:i + 1 + size], 'little') if size else 0
        if itype == 1 and tag == 0x8:           # Global / Report ID
            cur_id = val
            declared.setdefault(val, set())
        elif itype == 0 and tag == 0x8:         # Main / Input
            declared.setdefault(cur_id, set()).add('IN')
        elif itype == 0 and tag == 0x9:         # Main / Output
            declared.setdefault(cur_id, set()).add('OUT')
        elif itype == 0 and tag == 0xB:         # Main / Feature
            declared.setdefault(cur_id, set()).add('FEAT')
        i += 1 + size

    print('=== report IDs DECLARED in report map ===')
    for rid in sorted(declared):
        print('  0x%02X  %s' % (rid, '/'.join(sorted(declared[rid])) or '(no main item)'))
    print()

    # --- report reference descriptors (0x2908) -----------------------------
    reports = sorted(vh for vh, (_, u) in decls.items() if u == 0x2A4D)
    print('=== Report characteristics: %d ===' % len(reports))
    print()
    print('%-6s %-7s %-14s %-6s %-7s %s' % (
        'val', 'props', 'reference', 'mapID', 'mapDIR', 'verdict'))
    print('-' * 74)
    bad = 0
    for vh in reports:
        props = decls[vh][0]
        # Report Reference descriptor normally sits at value_handle + 1 (+2 for CCCD)
        ref = None
        for cand in (vh + 1, vh + 2, vh + 3):
            for h, off, data in reads:
                if h == cand and len(data) == 2:
                    ref = data
                    break
            if ref:
                break
        if ref is None:
            print('%-6X %-7s %-14s' % (vh, '0x%02X' % props, '<not read>'))
            continue
        rid, rtype = ref[0], ref[1]
        dirs = {1: 'IN', 2: 'OUT', 3: 'FEAT'}
        mdir = dirs.get(rtype, '?%d' % rtype)
        in_map = rid in declared
        dirmatch = in_map and (mdir in declared[rid])
        if not in_map:
            verdict = 'MISSING from map'
            bad += 1
        elif not dirmatch:
            verdict = 'DIR MISMATCH (map has %s)' % '/'.join(sorted(declared[rid]))
            bad += 1
        else:
            verdict = 'ok'
        print('%-6X %-7s 0x%02X/%-8s 0x%02X   %-7s %s' % (
            vh, '0x%02X' % props, rid, mdir, rid, mdir, verdict))
    print()
    print('MISMATCHED report characteristics: %d / %d' % (bad, len(reports)))


if __name__ == '__main__':
    main()
