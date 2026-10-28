import struct
from collections import Counter

D = open('btsnoop_hci.log','rb').read()
off, recs = 16, []
while off + 24 <= len(D):
    olen, ilen, flags, drops, ts = struct.unpack('>IIIIq', D[off:off+24]); off += 24
    p = D[off:off+ilen]; off += ilen
    recs.append((ts, flags, p))

lst = []
for ts, flags, p in recs:
    if not p or p[0] != 0x02 or len(p) < 11: continue
    l2 = struct.unpack('<H', p[5:7])[0]; cid = struct.unpack('<H', p[7:9])[0]
    if cid != 4: continue
    a = p[9:9+l2]
    if len(a) >= 3 and a[0] == 0x1b and struct.unpack('<H', a[1:3])[0] == 0x33:
        lst.append(a[3:])

n = len(lst)
print('包数 %d, 每包 %d 字节' % (n, len(lst[0])))
print('\n偏移  全零占比  不同值个数  最常见3个')
for i in range(20):
    col = [v[i] for v in lst]
    z = col.count(0)/n
    c = Counter(col).most_common(3)
    print('  %2d   %5.1f%%     %3d      %s' % (i, z*100, len(set(col)),
          ', '.join('%02X×%d' % kv for kv in c)))

# 假设 frag 循环 0,1,2: 检查每 60 字节内某偏移是否恒定
print('\n=== 按 frag 分组, 看同一个 frag 内各偏移的零占比 ===')
for frag in (0, 1, 2):
    grp = [v for v in lst if struct.unpack('<H', v[2:4])[0] == frag]
    if not grp: continue
    zs = ['%2d:%.0f%%' % (i, sum(1 for v in grp if v[i] == 0)/len(grp)*100) for i in range(20)]
    print('  frag%d (%d 包): %s' % (frag, len(grp), ' '.join(zs)))
