import struct
from collections import Counter, defaultdict

D = open('btsnoop_hci.log','rb').read()
off, recs = 16, []
while off + 24 <= len(D):
    olen, ilen, flags, drops, ts = struct.unpack('>IIIIq', D[off:off+24]); off += 24
    p = D[off:off+ilen]; off += ilen
    recs.append((ts, flags, p))
base = recs[0][0]
print('records:', len(recs), ' 跨度 %.1f s' % ((recs[-1][0]-base)/1e6))

notif = defaultdict(list)
for ts, flags, p in recs:
    t = (ts - base)/1e6
    if not p or p[0] != 0x02 or len(p) < 13:
        continue
    # H4(1) + handle(2) + dlen(2) + l2cap_len(2) + cid(2) => 数据从 offset 9 开始
    l2 = struct.unpack('<H', p[5:7])[0]
    cid = struct.unpack('<H', p[7:9])[0]
    if cid != 0x0004:
        continue
    a = p[9:9+l2]
    if len(a) >= 3 and a[0] == 0x1b:
        ah = struct.unpack('<H', a[1:3])[0]
        notif[ah].append((t, a[3:]))

print('\n=== 通知 handle 汇总 ===')
for ah, lst in sorted(notif.items(), key=lambda kv: -len(kv[1])):
    lens = Counter(len(v) for _, v in lst)
    span = lst[-1][0]-lst[0][0]
    print('  0x%04X  %4d 条  长度%s  t=%.1f..%.1f (跨%.1fs)' % (
        ah, len(lst), dict(lens), lst[0][0], lst[-1][0], span))

# 重点: 20字节那条
for ah, lst in notif.items():
    if len(lst) > 100 and all(len(v) == 20 for _, v in lst):
        print('\n=== handle 0x%04X 前 8 包 ===' % ah)
        for t, v in lst[:8]:
            print('  %9.3f  %s' % (t, v.hex(' ')))
        print('  ...')
        for t, v in lst[-4:]:
            print('  %9.3f  %s' % (t, v.hex(' ')))
        # 包间隔
        iv = [lst[i+1][0]-lst[i][0] for i in range(len(lst)-1)]
        iv = [x for x in iv if x > 0]
        iv.sort()
        print('  包间隔中位数 %.1f ms  最小 %.1f  最大 %.1f' % (
            iv[len(iv)//2]*1000, iv[0]*1000, iv[-1]*1000))
        # 按 0.5s 窗口看密度
        buckets = Counter(int(t) for t, _ in lst)
        print('  每秒包数:', dict(sorted(buckets.items())))
