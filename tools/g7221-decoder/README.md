# decode.exe —— G.722.1 / Siren 7 裸码流解码器

把遥控器语音通道送来的 **G.722.1（Siren 7）裸码流** 解成 16 kHz 单声道 WAV。
PC 端语音识别链路里的一环：`0xFC` 通知 → 去白化/字节交换 → 40 字节帧 → **decode.exe** → PCM → 识别。

```
decode.exe in.raw out.wav [bitrate]
```

- `in.raw`：裸码流，每帧 20 字节（16 kbps 时）
- `out.wav`：16 kHz / 单声道 / 16 bit PCM
- `bitrate`：默认 `16000`（**是码率，不是采样率**；采样率固定 16 kHz）

## 它由两部分组成

| 部分 | 来源 | 许可证 |
|---|---|---|
| `decode_main.c` | 本项目编写（文件读写 + WAV 头 + 逐帧调用） | MIT（随本项目） |
| `libg722_1` `v0.2.0` | [freeswitch/libg7221](https://github.com/freeswitch/libg7221)（commit `7d35574`），Steve Underwood 改编自 Polycom 的 ITU-T G.722.1 参考代码 | **GPL-2.0** |

因此 **`decode.exe` 这个二进制整体按 GPL-2.0 分发**，与本项目其余部分的 MIT 许可分开。
仓库里提供完整对应源码，见下节。

## 重新构建（复现 `decode.exe`）

```bash
# 1. 取 libg722_1 v0.2.0
git clone --branch v0.2.0 https://github.com/freeswitch/libg7221.git
cd libg7221

# 2. 打上本项目的补丁（只导出两个调试用全局变量，不影响解码结果）
git apply /path/to/libg722_1-v0.2.0-debug-globals.patch

# 3. 编译静态库（Windows 下可用 MSYS2 / mingw-w64）
./autogen.sh && ./configure --enable-static --disable-shared
make -j

# 4. 链接 decode.exe（把 decode_main.c 放在同一目录）
gcc -O2 -o decode.exe decode_main.c \
    -I./include -L./src/.libs -lg722_1 \
    -DG722_1_USE_FIXED_POINT
```

产物约 52 KB。放到 exe 同目录的 `tools/libg7221/decode.exe`（打包后的绿色包内路径为
`_internal/tools/libg7221/decode.exe`），程序启动时按三个位置依次查找。

## 为什么单独做成一个进程

不是架构偏好，是许可证要求。libg722_1 的 `README` 原文：

> You should check for any licence conflicts with other code in your application - for example,
> use in a GPL'ed project might be problematic, unless you run the G.722.1 codec as a separate
> process, and your application communicates with it through pipes or sockets.

本项目正是这么做的：Python 侧用 `subprocess` 把码流写成临时文件、调 `decode.exe`、再读回 WAV，
两边只有文件句柄往来，没有链接关系。所以 GPL-2.0 只覆盖 `decode.exe` 本身，
不会传染主程序。

## G.722.1 的专利问题（重要）

G.722.1 不是自由专利技术。libg722_1 的 `COPYING` 原文：

> The 7kHz and 14kHz bandwidth (16k samples/second and 32k samples/second) codecs defined
> in G.722.1 require the licencing of patents from Polycom, in most countries. However, a royalty
> free licence is available from Polycom if certain fairly basic conditions are met.
> Please contact Polycom for their current licence conditions.

也就是说：**Polycom 持有相关专利，符合其基本条件者可获得免版税许可**。
本项目把该解码器作为可选组件随 PC 端发布，使用者若用于商业分发，
应自行向 Polycom 确认许可条件。详见 [`docs/third-party.md`](../../docs/third-party.md)。

## 验证

仓库根目录的 `audio_decode_summary.md` 记录了当时的验证过程与结果
（对同一段码流与真值比对，SNR 约 29.9 dB）；`asr_proto/` 下有对比脚本。
