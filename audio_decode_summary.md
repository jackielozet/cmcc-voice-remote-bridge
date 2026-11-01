# CMCC 语音遥控器 —— 音频解码方式汇总

> 整理时间：2026-10-31
> 数据来源：仓库内已实现代码 + 盒子 btsnoop/hidraw 实测，逐条核对，非推测。
> 涉及代码：`publish/pc/voice_bridge_20261031_0115.py`、`bt-snoop-new/box_voice_truth.py`、`tools/libg7221/decode_main.c`

---

## 一、整体数据流

```
遥控器(CMCC_Voice_Remote)
   │  BLE GATT notify, report id 0xFC
   ▼
ESP32-S3 桥接固件 (NiBLE central)
   │  20 字节/条 经 USB-CDC 串口打印 "A <HEX>"
   ▼
PC 端 Python (voice_bridge_*.py)
   │  按 idx 拼帧 → ICO 解白化 → 高低字节交换
   ▼
G.722.1 裸码流 (.g7221 / .raw, 40 字节/帧)
   │
   ▼
libg7221/decode.exe
   ▼
PCM WAV (16kHz, mono, 16bit)
```

**对照基准（盒子侧，不经过 ESP32）：**

```
盒子 Android HID 驱动 → /dev/hidraw1 → hidraw.raw (21 字节/条 FC 记录)
   → box_voice_truth.py (同一套拼帧+解白化)
   → decode.exe → box_hidraw.wav
```

两条路径共用同一套解白化与解码算法，区别只在**取原始字节的入口**（串口 20 字节 vs hidraw 21 字节）。

---

## 二、底层字节格式

### 2.1 ESP32 侧（串口打出的一条 GATT 通知，20 字节）

```
offset 0-1 : seq       uint16 LE   （每"帧"递增；同一 seq 出现 3 次对应 idx 0/1/2）
offset 2   : idx       0 / 1 / 2
offset 3   : 00        固定 0x00
offset 4-19: payload   16 字节载荷
```

固件 `handleAudio()` 收到 notify 后原样拷贝 20 字节入队，不在固件侧做任何解白化/拼接，全部交给 PC。

### 2.2 盒子 hidraw 侧（21 字节）

```
offset 0   : FC        report id（0xFC，Android 驱动多带的前缀）
offset 1-2 : seq       uint16 LE
offset 3   : idx       0 / 1 / 2
offset 4   : 00        固定 0x00
offset 5-20: payload   16 字节载荷
```

> **两种长度必须区分**：PC 端解析串口时按 20 字节切（无 report id）；解析 hidraw 时按 21 字节切（含 `FC`）。混用会整体错位 1 字节，整段音频报废。

---

## 三、拼帧逻辑

- **3 条通知拼成 1 帧**：相同 `seq` 的 idx 0、1、2 到齐后拼接。
- 拼接顺序：`payload0 + payload1 + payload2` = 48 字节。
- 48 字节中**前 40 字节 = 20 个 u16（小端）的码流**；后 8 字节是元数据（含 seq 等信息），解白化只取前 40 字节。
- 关键常量（`voice_bridge`）：`CHUNK_DATA=16`、`CHUNK_HEAD=4`、`FRAME_BYTES=48`、`CODEC_BYTES=40`、`WORDS=20`、`SAMPLES_PER_FRAME=320`。
- `Framer.reset()` 在**语音键按下**时清空，保证一句一文件，不把上一句尾巴带进来。

`seq` 语义（已用盒子和 ESP32 两侧实测双重确认）：

- `seq` 每"帧"递增 1，连续无缺口即表示一帧都没丢。
- 缺口只能由 seq 差判定（不是 idx 差）。

---

## 四、ICO 解白化算法

来源：`libicocodec.so`（遥控器 Android 端）反汇编得到的 `ivDecTable`，并用 Unicorn 直接跑库函数逐字节验证一致。

常量：

```python
IV_DEC_TABLE = [4, 16, 9,  8, 3, 11,  2, 6, 18,  7, 13, 17]
XOR_KEY      = 0x0416          # 遥控器 VID（0x0416，非巧合）
```

算法（`dewhite(frame48)`，逐字来自 `voice_bridge.py` / `box_voice_truth.py`，两处实现一致）：

```python
def dewhite(frame48):
    words = list(struct.unpack("<20H", frame48[:40]))   # 20 个 u16 小端
    for g in range(4):                                  # 4 组，每组 3 个位置循环置换
        a, b, c = IV_DEC_TABLE[3*g], IV_DEC_TABLE[3*g+1], IV_DEC_TABLE[3*g+2]
        oa, ob, oc = words[a], words[b], words[c]
        words[a], words[b], words[c] = oc, oa, ob       # new_a=old_c, new_b=old_a, new_c=old_b
    words = [x ^ 0x0416 for x in words]                 # 每 word 异或 VID
    out = bytearray(40)
    for i, w in enumerate(words):
        out[i*2]   = (w >> 8) & 0xFF                    # 高低字节交换（MSB-first）
        out[i*2+1] = w & 0xFF
    return bytes(out)
```

三步顺序固定：**① 位置置换 → ② 异或 0x0416 → ③ 高低字节交换**。

---

## 五、G.722.1 / Siren7 解码

`tools/libg7221/decode.exe`，用法 `decode.exe in.raw out.wav [bitrate]`。

关键参数（已读 `decode_main.c` 源码确认）：

| 项 | 值 | 来源 |
|---|---|---|
| 码率 `bit_rate` | 16000 bps（第三参传 `"16000"`） | `atoi(argv[3])`，缺省 16000 |
| 采样率 | **16000 Hz 硬编码**（`g722_1_decode_init(...,16000)`） | 源码写死 |
| 帧字节数 | `(16000/50)/8 = 40` 字节/帧 | `frame_bytes` |
| 每帧样本 | 320 样本（= 20 ms） | 输出 `MAX_FRAME_SIZE/2` |
| 声道 / 位深 | mono / 16 bit | WAV 头写死 |

> **历史误判纠正（重要）**：早先曾把"40 字节/帧"与"40 ms/帧、8 kHz"混淆。盒子基准文件 `box_hidraw.wav` 才是硬证据：
> `82240 采样 / 257 帧 = 320 样本/帧`，`82240 / 16000 = 5.14 秒`，即**每帧 20 ms、采样率 16 kHz**。凡"帧数→秒数"换算一律用 `帧数 × 0.02`。

`voice_bridge` 调用：

```python
codec = b"".join(dewhite(f) for f in raw_frames)   # 拼成裸码流
subprocess.run([exe, raw_path, wav_path, "16000"]) # 第三参 = 码率 16kbps
```

---

## 六、两条落地路径

| 路径 | 脚本 | 入口数据 | 作用 |
|---|---|---|---|
| PC 实时桥接 | `publish/pc/voice_bridge_20261031_0115.py` | ESP32 串口 `A <HEX>` | 实时抓按键语音 → wav |
| 盒子离线基准 | `bt-snoop-new/box_voice_truth.py` | `hidraw.raw` | 验证解码链路本身是否正确（已出 5.14s 清晰人声） |

`voice_bridge` 用法：

```
python voice_bridge_20261031_0115.py cap COM11 --auto     # 按一次语音键，松手后自动生成 wav
python voice_bridge_20261031_0115.py cap COM11 --secs 30 --out take
```

---

## 七、质量验证证据链（解码是对的）

| 验证项      | 结论                                            | 依据                             |
| -------- | --------------------------------------------- | ------------------------------ |
| 解白化逐字节正确 | 与 Unicorn 跑 Android 库的结果**逐字节一致**             | emu_dewhite.py 比对 257 帧，`True` |
| 解码保真度    | 纯 C 解码 vs ARM 库解码 **SNR 29.9 dB**             | 解码器自测                          |
| 实听       | 盒子基准 5.14s **清晰人声**                           | box_hidraw.wav 已听验             |
| 实播       | `pipe_play.exe -raw48` 播 257 帧，**0 错误**，扬声器出声 | 实播验证                           |

**结论**：解白化、字节序、采样率、解码器全部正确。当前 ESP32 链路的问题**不在解码**，而在"遥控器只肯推约 1 秒音频"（见语音采集排查日志），属采集层瓶颈。

---

## 八、关键坑 / 注意事项

1. **字节长度 20 vs 21**：串口入口无 report id（20B），hidraw 入口有 `FC`（21B）。解析器必须按来源分别处理。
2. **采样率参数**：`decode.exe` 第三参是**码率**不是采样率；采样率固定 16 kHz。传 `16000` 即 16kbps 模式。
3. **帧长误判**：曾误写 `FRAME_MS=40`，已改回 20。所有"秒数"换算用 0.02。
4. **解白化三步顺序**：置换 → 异或 → 字节交换，顺序错则整段噪音。
5. **seq 缺口判定**：缺口看 seq 差，不看 idx。连续即零丢帧。
6. **解码器位置**：脚本在三处候选路径查找 `decode.exe`（仓库自带 / 上一级 / 脚本旁），路径写错会找不到。
