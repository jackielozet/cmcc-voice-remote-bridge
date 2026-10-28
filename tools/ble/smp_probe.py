import struct, sys

def records(path):
    d = open(path,'rb').read()
    if d[:8] != b'btsnoop\x00':
        raise SystemExit('not btsnoop')
    off = 16
    out = []
    while off + 24 <= len(d):
        olen, ilen, flags, drops, ts, typ = struct.unpack('>IIIIqI', d[off:off+24])
        off += 24
        payload = d[off:off+olen]; off += olen
        out.append((ts, typ, payload))
    return out

recs = records('btsnoop_hci.log')
print('records:', len(recs))

ADV = {0x00:'ADV_IND(可连接/可扫描)',0x01:'ADV_DIRECT_IND(定向)',0x02:'ADV_SCAN_IND',
       0x03:'ADV_NONCONN_IND',0x04:'SCAN_RSP'}
seen_adv = {}
smp = []
conn = []

for ts, typ, p in recs:
    if not p: continue
    code = p[0]
    if code == 0x04 and len(p) >= 3:   # HCI Event
        ev = p[1]
        if ev == 0x3e and len(p) >= 4:
            sub = p[2]
            body = p[3:]
            if sub == 0x02 and len(body) >= 1:   # LE Advertising Report
                n = body[0]
                i = 1
                for _ in range(n):
                    if i+9 > len(body): break
                    etype = body[i]; atype = body[i+1]
                    addr = ':'.join('%02X'%x for x in body[i+2:i+8][::-1])
                    dlen = body[i+8]
                    data = body[i+9:i+9+dlen]
                    i += 9 + dlen
                    rssi = body[i] if i < len(body) else 0
                    i += 1
                    key = (etype, addr)
                    seen_adv[key] = seen_adv.get(key,0)+1
                    if key not in seen_adv or seen_adv[key] == 1:
                        print('  ADV etype=%02X %-22s %s len=%d rssi=%d  %s' % (
                            etype, ADV.get(etype,'?'), addr, dlen, rssi,
                            ' '.join('%02X'%x for x in data[:24])))
            elif sub == 0x01 and len(body) >= 10:
                st = body[0]; h = struct.unpack('<H', body[1:3])[0]
                atype = body[3]
                addr = ':'.join('%02X'%x for x in body[4:10][::-1])
                conn.append((st,h,atype,addr))
                print('  CONN status=%d handle=0x%04X addr=%s' % (st,h,addr))
            elif sub == 0x07:  # Data Length Change
                pass
    # ACL -> L2CAP -> SMP (cid 0x0006)
    if typ == 0x02 and len(p) >= 9:
        h = struct.unpack('<H', p[1:3])[0] & 0x0fff
        l2len = struct.unpack('<H', p[3:5])[0]
        cid = struct.unpack('<H', p[5:7])[0]
        if cid == 0x0006:
            smp.append(bytes(p[7:7+l2len]))

print('\n总广播记录种类:', len(seen_adv))
print('\n=== SMP 配对包 (%d) ===' % len(smp))
NAMES = {0x01:'Pairing Request',0x02:'Pairing Response',0x03:'Pairing Confirm',
         0x04:'Pairing Random',0x05:'Pairing Failed',0x0b:'Pairing Public Key',
         0x0c:'Pairing DHKey Check'}
IOCAP = {0:'DisplayOnly',1:'DisplayYesNo',2:'KeyboardOnly',3:'NoInputNoOutput',4:'KeyboardDisplay'}
for s in smp:
    if not s: continue
    op = s[0]
    if op in (0x01,0x02) and len(s) >= 7:
        print('  %-18s io_cap=%d(%s) oob=%d auth_req=0x%02X maxkey=%d initkey=0x%02X respkey=0x%02X' % (
            NAMES.get(op,'0x%02X'%op), s[1], IOCAP.get(s[1],'?'), s[2], s[3], s[4], s[5], s[6]))
        a = s[3]
        print('      auth: bond=%d mitm=%d sc=%d keypress=%d ctk=%d' % (
            a>>0&1, a>>2&1, a>>3&1, a>>4&1, a>>5&1))
    else:
        print('  %-18s %s' % (NAMES.get(op,'0x%02X'%op), ' '.join('%02X'%x for x in s[:12])))
