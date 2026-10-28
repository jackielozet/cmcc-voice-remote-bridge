import struct
from collections import Counter, defaultdict

D = open('btsnoop_hci.log','rb').read()
off, recs = 16, []
while off + 24 <= len(D):
    olen, ilen, flags, drops, ts = struct.unpack('>IIIIq', D[off:off+24]); off += 24
    p = D[off:off+ilen]; off += ilen
    recs.append((ts, flags, p))

notif = defaultdict(list)
for idx, (ts, flags, p) in enumerate(recs):
    if not p or p[0] != 0x02 or len(p) < 11:
        continue
    l2 = struct.unpack('<H', p[5:7])[0]
    cid = struct.unpack('<H', p[7:9])[0]
    if cid != 0x0004:
        continue
    a = p[9:9+l2]
    if len(a) >= 3 and a[0] == 0x1b:
        notif[struct.unpack('<H', a[1:3])[0]].append((idx, a[3:]))

for H in (0x33, 0x46):
    lst = notif.get(H, [])
    print('\n########## handle 0x%04X : %d 条 ##########' % (H, len(lst)))
    seqs = []
    for i, (idx, v) in enumerate(lst[:40]):
        s, f = struct.unpack('<HH', v[0:4])
        seqs.append((s, f))
        print('  %3d  seq=%5d frag=%d  %s' % (i, s, f, v[4:].hex(' ')))
    # frag 分布
    fragcnt = Counter(f for s, f in seqs)
    print('  前40条 frag 分布:', dict(fragcnt))

# 0x33 全量: 每条 seq 有几片
lst = notif.get(0x33, [])
by = defaultdict(int)
for idx, v in lst:
    s, f = struct.unpack('<HH', v[0:4])
    by[s] = max(by[s], f+1)
print('\n0x33 总 seq 数: %d  每 seq 片数分布: %s' % (len(by), dict(Counter(by.values()))))
ks = sorted(by)
print('seq 范围 %d..%d  连续性: 缺 %d 个' % (ks[0], ks[-1], (ks[-1]-ks[0]+1)-len(ks)))

# 负载熵
import math
data = b''.join(v[4:] for idx, v in lst)
c = Counter(data)
H_ = -sum(n/len(data)*math.log2(n/len(data)) for n in c.values())
print('总负载 %d 字节, 字节熵 %.2f bit/byte (8=完全随机)' % (len(data), H_))
nib = []
for b in data:
    nib.append(b & 0xF); nib.append(b >> 4)
print('低半字节均值 %.2f 高半字节均值 %.2f (ADPCM 通常明显偏低)' % (
    sum(nib[0::2])/len(nib[0::2]), sum(nib[1::2])/len(nib[1::2])))
