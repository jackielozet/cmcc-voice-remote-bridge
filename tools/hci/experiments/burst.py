import struct
D = open('btsnoop_hci.log','rb').read()
off, recs = 16, []
while off + 24 <= len(D):
    olen, ilen, flags, drops, ts = struct.unpack('>IIIIq', D[off:off+24]); off += 24
    p = D[off:off+ilen]; off += ilen
    recs.append((ts, flags, p))

ev = []
for i, (ts, flags, p) in enumerate(recs):
    if not p or p[0] != 0x02 or len(p) < 11: continue
    l2 = struct.unpack('<H', p[5:7])[0]; cid = struct.unpack('<H', p[7:9])[0]
    if cid != 4: continue
    a = p[9:9+l2]
    if len(a) >= 3 and a[0] == 0x1b:
        ev.append((i, ts, struct.unpack('<H', a[1:3])[0], a[3:]))

v33 = [(i, ts, v) for i, ts, h, v in ev if h == 0x33]
# 按 seq 连续性切 burst
bursts = []
cur = []
prev = None
for i, ts, v in v33:
    s = struct.unpack('<H', v[0:2])[0]
    if prev is not None and (s != prev and s != prev+1):
        bursts.append(cur); cur = []
    cur.append((i, ts, s))
    prev = s
if cur: bursts.append(cur)

print('0x33 共 %d 条, 切成 %d 段 burst' % (len(v33), len(bursts)))
for b in bursts:
    t0, t1 = b[0][1], b[-1][1]
    dt = (t1-t0)/1e6
    nseq = b[-1][2]-b[0][2]+1
    print('  rec#%4d..%-4d  seq %3d..%-3d  %2d 记录  %.3f s  %.0f 记录/s  约 %.0f B/s'
          % (b[0][0], b[-1][0], b[0][2], b[-1][2], nseq, dt,
             nseq/dt if dt else 0, nseq*40/dt if dt else 0))

print('\n=== 每段 burst 前 6 条其它事件 ===')
for b in bursts:
    i0 = b[0][0]
    print(' burst@rec#%d:' % i0)
    for i, ts, h, v in ev:
        if i0-60 <= i < i0:
            print('    rec#%4d  h=0x%02X  %s' % (i, h, v.hex(' ')))
