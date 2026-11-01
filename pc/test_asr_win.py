# -*- coding: utf-8 -*-
"""asr_win 核心链路自测：G.722.1 码流 -> PCM -> 文字（不涉及注入和钩子）"""
import os
import sys
import time

sys.path.insert(0, os.path.dirname(os.path.abspath(__file__)))
import asr_win  # noqa: E402

PC = os.path.dirname(os.path.abspath(__file__))
CASES = [
    "take_rest3_20261101_r2.g7221",
    "take_lat0_continuous_20261101_r3.g7221",
    "take_rest3_20261101_r1.g7221",
    "take_itvl24_lat50_to400_20261101.g7221",
]

exe = asr_win.find_decoder()
print("解码器:", exe)
print("模型目录:", asr_win.default_model_dir())
print()

eng = asr_win.AsrEngine()
if not eng.available():
    print("!! 找不到模型，ASR 不可用")
    sys.exit(1)

t0 = time.time()
eng.load()
print("模型加载 %d ms\n" % eng.load_ms)

for name in CASES:
    p = os.path.join(PC, name)
    if not os.path.exists(p):
        print("%-44s 缺文件" % name)
        continue
    codec = open(p, "rb").read()
    t1 = time.time()
    pcm = asr_win.decode_codec(codec)
    dt_dec = time.time() - t1
    dur = len(pcm) / 2 / 16000.0
    t2 = time.time()
    text = eng.transcribe(asr_win.pcm_to_float(pcm))
    dt_asr = time.time() - t2
    print("%-42s %5.2fs  解码%5.0fms 识别%5.0fms" % (name, dur, dt_dec * 1000, dt_asr * 1000))
    print("    -> %s" % (text or "（空）"))

print("\n总耗时 %.2fs" % (time.time() - t0))
