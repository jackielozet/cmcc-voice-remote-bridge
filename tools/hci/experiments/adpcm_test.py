import struct, math, wave
from collections import defaultdict

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

# 去掉 4 字节头, 只留 16 字节负载
payload = b''.join(v[4:] for v in lst)
print('负载 %d 字节' % len(payload))

# 标准 IMA ADPCM 解码 (4bit, step index table)
STEP = [7,8,9,10,11,12,13,14,16,17,19,21,23,25,28,31,34,37,41,45,50,55,60,66,73,80,88,97,
        107,118,130,143,157,173,190,209,230,253,279,307,337,371,408,449,494,544,598,658,
        724,796,876,963,1060,1166,1282,1411,1552,1707,1878,2066,2272,2499,2749,3024,
        3327,3660,4026,4428,4871,5358,5894,6484,7132,7845,8630,9493,10442,11487,12635,
        13899,15289,16818,18500,20350,22385,24623,27086,29794,32767]
IDX  = [-1,-1,-1,-1,2,4,6,8,-1,-1,-1,-1,2,4,6,8]

def decode_ima(data, pred=0, idx=0):
    out = []
    for b in data:
        for nib in (b & 0xF, b >> 4):      # 先低后高
            step = STEP[idx]
            diff = step >> 3
            if nib & 1: diff += step >> 2
            if nib & 2: diff += step >> 1
            if nib & 4: diff += step
            if nib & 8: pred -= diff
            else:       pred += diff
            pred = max(-32768, min(32767, pred))
            idx = max(0, min(88, idx + IDX[nib]))
            out.append(pred)
    return out

for order in ('low-first', 'high-first'):
    pcm = []
    pred = idx = 0
    for b in payload:
        nibs = (b & 0xF, b >> 4) if order == 'low-first' else (b >> 4, b & 0xF)
        for nib in nibs:
            step = STEP[idx]
            diff = step >> 3
            if nib & 1: diff += step >> 2
            if nib & 2: diff += step >> 1
            if nib & 4: diff += step
            pred = pred - diff if nib & 8 else pred + diff
            pred = max(-32768, min(32767, pred))
            idx = max(0, min(88, idx + IDX[nib]))
            pcm.append(pred)
    # 每 20ms 窗口 RMS
    win = 320
    env = []
    for i in range(0, len(pcm)-win, win):
        seg = pcm[i:i+win]
        r = math.sqrt(sum(x*x for x in seg)/win)
        env.append(r)
    print('\n[%s] 采样 %d 个 (16kHz 下 %.2f s)' % (order, len(pcm), len(pcm)/16000.0))
    print('  峰值 %d  RMS 均值 %.0f' % (max(abs(x) for x in pcm), sum(env)/len(env)))
    print('  前 30 个 20ms 窗口 RMS:')
    print('   ', ' '.join('%d' % e for e in env[:30]))
    print('  后 20 个窗口 RMS:')
    print('   ', ' '.join('%d' % e for e in env[-20:]))
    if order == 'low-first':
        with wave.open('adpcm_low.wav','wb') as w:
            w.setnchannels(1); w.setsampwidth(2); w.setframerate(16000)
            w.writeframes(struct.pack('<%dh' % len(pcm), *pcm))
