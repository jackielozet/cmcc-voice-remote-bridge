import struct
from collections import defaultdict, Counter

D = open('btsnoop_hci.log','rb').read()
off, recs = 16, []
while off + 24 <= len(D):
    olen, ilen, flags, drops, ts = struct.unpack('>IIIIq', D[off:off+24]); off += 24
    p = D[off:off+ilen]; off += ilen
    recs.append((ts, flags, p, olen))

pkt = []
for i, (ts, flags, p, olen) in enumerate(recs):
    if not p or p[0] != 0x02 or len(p) < 11: continue
    l2 = struct.unpack('<H', p[5:7])[0]; cid = struct.unpack('<H', p[7:9])[0]
    if cid != 4: continue
    a = p[9:9+l2]
    if not a: continue
    pkt.append((i, ts, a[0], a))

OPS = {0x12:'Write Req', 0x52:'Write Cmd', 0x1B:'Notify', 0x1D:'Indicate',
       0x08:'MTU Req', 0x03:'MTU Resp', 0x10:'Read By Group Req'}
writes = [(i, ts, a) for i, ts, op, a in pkt if op in (0x12, 0x52)]
print('写操作共 %d 条' % len(writes))
cnt = Counter()
for i, ts, a in writes:
    h = struct.unpack('<H', a[1:3])[0]
    cnt[h] += 1
print('\n=== 各 handle 写入次数 ===')
for h, n in cnt.most_common():
    ex = next(a for i, ts, a in writes if struct.unpack('<H', a[1:3])[0] == h)
    print('  0x%04X  %3d 次   样例 %s' % (h, n, ex[3:].hex(' ')))

# 首个 0x33 通知之前最后 12 条写
first33 = None
for i, ts, op, a in pkt:
    if op == 0x1b and struct.unpack('<H', a[1:3])[0] == 0x33:
        first33 = i; break
print('\n=== 首个 0x33 通知(记录#%d)之前的最后 12 条写操作 ===' % first33)
prev = [w for w in writes if w[0] < first33][-12:]
for i, ts, a in prev:
    h = struct.unpack('<H', a[1:3])[0]
    print('  rec#%4d  handle 0x%04X  %s' % (i, h, a[3:].hex(' ')))

# 0x33 开始前后, 键盘报告 0x2F 的内容
print('\n=== 0x33 开始前后 20 条键盘报告(0x2F) ===')
k = [(i, a) for i, ts, op, a in pkt if op == 0x1b and struct.unpack('<H', a[1:3])[0] == 0x2F]
near = [x for x in k if abs(x[0]-first33) < 400]
for i, a in near[:24]:
    print('  rec#%4d  %s' % (i, a[3:].hex(' ')))
