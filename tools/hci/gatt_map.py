import struct
from collections import defaultdict, Counter

D = open('btsnoop_hci.log','rb').read()
off, recs = 16, []
while off + 24 <= len(D):
    olen, ilen, flags, drops, ts = struct.unpack('>IIIIq', D[off:off+24]); off += 24
    p = D[off:off+ilen]; off += ilen
    recs.append((ts, flags, p))

def att_pkts():
    for ts, flags, p in recs:
        if not p or p[0] != 0x02 or len(p) < 11:
            continue
        l2 = struct.unpack('<H', p[5:7])[0]
        cid = struct.unpack('<H', p[7:9])[0]
        if cid != 0x0004:
            continue
        a = p[9:9+l2]
        if len(a) >= 1:
            yield a

def u16(b, i): return struct.unpack('<H', b[i:i+2])[0]

UUID16 = {0x2800:'Primary Service',0x2801:'Secondary',0x2802:'Include',0x2803:'Char Decl',
          0x2900:'Ext Props',0x2901:'User Desc',0x2902:'CCCD',0x2903:'SCCD',
          0x2904:'Format',0x2905:'Aggregate',0x2908:'Report Ref',0x2A4D:'HID Report',
          0x2A4A:'HID Info',0x2A4B:'Report Map',0x2A4C:'HID Control',
          0x2A19:'Battery',0x180A:'DIS',0x1812:'HIDS',0x1800:'GAP',0x1801:'GATT',0x180F:'BAS'}
def uname(u):
    if u < 0x10000:
        return '%s(0x%04X)' % (UUID16.get(u, '?'), u)
    return 'vendor %08X-%04X-%04X' % (u>>32 & 0xFFFFFFFF, u>>16&0xFFFF, u&0xFFFF)

svc = {}      # 起始handle -> (结束, uuid)
chars = {}    # value handle -> (uuid, props, decl handle)
descs = {}    # handle -> uuid
seen = []

for a in att_pkts():
    op = a[0]
    if op == 0x11 and len(a) >= 2:      # Read By Group Type Response
        n = a[1]
        for i in range(2, len(a)-n+1, n):
            e = a[i:i+n]
            if len(e) < 4: break
            svc[u16(e,0)] = (u16(e,2), u16(e,4) if len(e)>=6 else 0)
    elif op == 0x09 and len(a) >= 2:    # Read By Type Response
        n = a[1]
        for i in range(2, len(a)-n+1, n):
            e = a[i:i+n]
            if len(e) < 2: break
            h = u16(e,0)
            rest = e[2:]
            if len(rest) == 2:          # descriptor/text
                descs[h] = u16(rest,0)
            elif len(rest) == 5:        # char decl: props + value handle + uuid16
                chars[u16(rest,1)] = (u16(rest,3), rest[0], h)
            elif len(rest) >= 16:
                pass

print('=== 服务 (handle 起点 -> 终点, UUID) ===')
for h in sorted(svc):
    e, u = svc[h]
    print('  0x%04X-0x%04X  %s' % (h, e, uname(u)))

print('\n=== 特征 (value handle -> UUID) ===')
for h in sorted(chars):
    u, props, decl = chars[h]
    print('  0x%04X  decl0x%04X  props=0x%02X  %s' % (h, decl, props, uname(u)))

print('\n=== 描述符 ===')
for h in sorted(descs):
    print('  0x%04X  %s' % (h, uname(descs[h])))
