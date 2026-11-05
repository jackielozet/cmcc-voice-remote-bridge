# 第三方组件与开源许可

本项目自身代码以 **GPL-2.0** 发布（见 [`LICENSE`](../LICENSE)；1.3.2 起，之前为 MIT）。
发布物（ESP32 固件、PC 端绿色包）包含若干第三方组件，它们各自沿用原许可证，
另有两处需要特别注意的义务，都写在下面。

> 这份清单是**逐个核对打包产物**得来的，不是照抄依赖列表：
> PC 端的版本号取自 `_internal/*.dist-info/METADATA` 与 DLL 版本资源，
> 固件取自 `package.json` 与实际 include。核对方法见文末。

---

## 一、PC 端 CMCC_RC_Keys 1.3.1

| 组件 | 版本 | 许可证 | 在本项目里干什么 |
|---|---|---|---|
| [esptool](https://github.com/espressif/esptool) | 5.6.0 | **GPL-2.0-or-later** | 刷固件 / 读芯片型号（1.3.1 起内嵌，见下「2. esptool 内嵌带来的许可冲突」） |
| [rich](https://github.com/Textualize/rich) | 15.0.0 | MIT | esptool 的终端输出 |
| [click](https://github.com/pallets/click) / rich-click | 8.5.0 / 1.9.9 | BSD-3-Clause / MIT | esptool 的命令行解析 |
| [cryptography](https://github.com/pyca/cryptography) | 50.0.2 | Apache-2.0 OR BSD-3-Clause | esptool 的加密镜像支持 |
| [reedsolo](https://github.com/tomerfiliba-org/reedsolomon) | 1.7.0 | MIT | esptool 的 ECC 校验 |
| [sherpa-onnx](https://github.com/k2-fsa/sherpa-onnx) | 2.0.1 | Apache-2.0 | 离线语音识别框架（`sherpa_onnx/`） |
| [onnxruntime](https://github.com/microsoft/onnxruntime) | 1.28.2 | MIT | 推理运行时，随 sherpa-onnx 以 `onnxruntime.dll` 分发 |
| Paraformer-zh-small 模型权重 | 2024-03-09 | **未声明**（见下） | 语音识别模型 `model.int8.onnx`（78 MB） |
| [numpy](https://numpy.org/) | 2.5.4 | BSD-3-Clause | 音频重采样、归一化等数值运算 |
| [Pillow](https://python-pillow.org/) | 13.0.0 | MIT-CMU | 背景图按窗口尺寸实时缩放 |
| [pyserial](https://github.com/pyserial/pyserial) | 3.5 | BSD-3-Clause | 串口通信（读写映射表、收音频帧） |
| [Python](https://www.python.org/) | 3.13.17 | PSF-2.0 | 运行时（`python313.dll`） |
| Tcl/Tk | 8.6（随 Python） | BSD-style | Tkinter 图形界面 |
| [PyInstaller](https://pyinstaller.org/) | 6.22.3 | GPL-2.0-or-later **+ 例外条款** | 打包成 exe。其例外条款明确：打包出的程序不受 GPL 约束 |
| [libg722_1](https://github.com/freeswitch/libg7221) | v0.2.0 | **GPL-2.0** | G.722.1 / Siren 7 解码（`decode.exe`，独立进程） |
| G.722.1 / Siren 7 编解码标准 | — | ITU-T 标准，**Polycom 专利** | 遥控器语音通道的编码格式 |

### 1. 模型权重没有明确许可证

`sherpa-onnx-paraformer-zh-small-2024-03-09` 的 README 写明模型来自 ModelScope 的
[`crazyant/speech_paraformer_asr_nat-zh-cn-16k-common-vocab8358-onnx`](https://www.modelscope.cn/models/crazyant/speech_paraformer_asr_nat-zh-cn-16k-common-vocab8358-onnx)，
上游是 FunASR 的 Paraformer-zh。

核实结果，如实写：

- FunASR **工具包源码**是 MIT（[仓库](https://github.com/modelscope/FunASR)），但官方明确说明
  "预训练模型权重单独授权，以各模型卡为准"；
- 上述 ModelScope 模型卡的 `License` 字段查询结果为**空**（`GET /api/v1/models/...` 返回
  `"License": ""`），即上游未标注许可证。

因此本项目**不对该模型权重再授权**，只是随绿色包一起分发以便开箱可用。
使用者若用于商业场景，应自行向上游确认条款。不用语音功能时删掉
`_internal/models/` 即可，改键与按键映射照常工作。

### 2. `decode.exe` 是 GPL-2.0，靠"独立进程"隔离

`decode.exe` 静态链接了 **libg722_1**（GPL-2.0），所以这个二进制整体按 GPL-2.0 分发。
1.3.2 起本项目本身也是 GPL-2.0，两者一致；1.3.1 及更早的版本里，这一份是单独按 GPL-2.0 分发的。

libg722_1 的 README 原文给的就是这个办法：

> use in a GPL'ed project might be problematic, unless you run the G.722.1 codec as a separate
> process, and your application communicates with it through pipes or sockets.

本项目正是这么做的：Python 侧用 `subprocess` 写临时文件、调 `decode.exe`、读回 WAV，
两边只有文件句柄往来，没有链接关系。所以 GPL-2.0 只覆盖 `decode.exe` 本身。

**完整对应源码**在 [`tools/g7221-decoder/`](../tools/g7221-decoder/)：
`decode_main.c`（本项目编写）+ 对 libg722_1 v0.2.0 的补丁 + 构建步骤，
可复现出功能等价的 `decode.exe`。

### 3. G.722.1 涉及 Polycom 专利

libg722_1 的 `COPYING` 原文：

> The 7kHz and 14kHz bandwidth (16k samples/second and 32k samples/second) codecs defined
> in G.722.1 require the licencing of patents from Polycom, in most countries. However, a royalty
> free licence is available from Polycom if certain fairly basic conditions are met.
> Please contact Polycom for their current licence conditions.

即：**Polycom 持有相关专利，符合其基本条件者可获得免版税许可**。项目按"随程序提供、
默认不启用商业分发"的方式使用；若要商用，请先向 Polycom 确认许可条件。

### 4. esptool 内嵌与许可证的选择（1.3.1 起，1.3.2 已定案）

1.3.1 为了解决「目标机没装 esptool 就刷不了固件」，把 esptool 5.6.0 打进了 exe，
在**同一进程内** `import esptool` 调用。这跟 `decode.exe` 的处理方式不同：

| | `decode.exe`（1.2.0 起） | 内嵌 esptool（1.3.1 起） |
|---|---|---|
| 运行方式 | 独立进程，`subprocess` 调用、文件通信 | 与本程序同一进程、同一地址空间 |
| GPL 传染 | 不传染（聚合而非衍生） | **传染**：整个 exe 视为衍生作品 |
| 对仓库 LICENSE 的影响 | 无 | 需要仓库本身也是 GPL 兼容许可 |

**已定案（2026-11-05）**：仓库 `LICENSE` 从 MIT 改为 **GPL-2.0**，与内嵌的 esptool 一致。
源码本来就在这个仓库公开，满足 GPL 的源码提供义务；1.3.1 及更早的版本仍按 MIT 授权。

代码里仍保留了「用外部 esptool」的路径：`_resolve_esptool()` 优先找外部 `esptool.exe`
（`PATH`、程序所在目录、常见 pip 安装位置），只有找不到时才用内嵌那份。
如果以后要把 exe 退回宽松许可，把 esptool 单独打成 `esptool.exe` 用 `subprocess` 调用即可
（与 `decode.exe` 同款隔离），代价是 Release 多一个附件、程序要能定位到它。

---

## 二、ESP32 固件

| 组件 | 版本 | 许可证 | 用途 |
|---|---|---|---|
| [Arduino-ESP32](https://github.com/espressif/arduino-esp32) | 3.3.12 | LGPL-2.1-or-later | 板级封装、`BLEClient` / `BLEScan` 等 Arduino 风格 API |
| [ESP-IDF](https://github.com/espressif/esp-idf)（随 core 内置） | 随 3.3.12 | Apache-2.0 | NimBLE 主机栈、FreeRTOS、NVS、USB |
| [NimBLE](https://github.com/apache/mynewt-nimble) | 随 ESP-IDF | Apache-2.0 | BLE 协议栈（固件实际用的是 NimBLE，不是 Bluedroid） |
| [TinyUSB](https://github.com/hathach/tinyusb) | 随 ESP-IDF | MIT | USB 复合设备（HID 键盘 + 消费者控制 + CDC） |

固件是**静态编译进 bin 分发**的。Arduino-ESP32 采用 LGPL-2.1，本项目以"未修改的库 +
独立 sketch"方式使用，sketch 源码完整公开在本仓库 `firmware/` 下，符合 LGPL 对
"允许用户替换库并重新链接"的要求（用同一份 `arduino-cli` 命令即可重编，见
[`docs/build.md`](build.md)）。

---

## 三、只在开发/构建时用到（不随发布物分发）

`arduino-cli`、`esptool`、`gcc/mingw-w64`（编译 decode.exe）、`PyInstaller`、
`pytest` 等，只在构建机上使用。

---

## 四、怎么自己核对

```bash
# PC 端：看打包进去的包与版本
ls pc/dist/CMCC_RC_Keys/_internal/*.dist-info
grep -E "^(Name|Version|License)" \
     pc/dist/CMCC_RC_Keys/_internal/sherpa_onnx-2.0.1.dist-info/METADATA

# onnxruntime 版本（DLL 版本资源）
powershell -c "(Get-Item pc\dist\CMCC_RC_Keys\_internal\sherpa_onnx\lib\onnxruntime.dll).VersionInfo.FileVersion"

# 模型来源
cat pc/dist/CMCC_RC_Keys/_internal/models/*/README.md

# 模型许可证字段（返回 "License":"" 即为未声明）
curl -s "https://www.modelscope.cn/api/v1/models/crazyant/speech_paraformer_asr_nat-zh-cn-16k-common-vocab8358-onnx" | tr ',' '\n' | grep -i license

# 固件：Arduino core 版本与许可
grep license "%LOCALAPPDATA%/Arduino15/packages/esp32/hardware/esp32/3.3.12/package.json"
```

---

## 五、合规速查

| 你要做的事 | 需要注意 |
|---|---|
| 自用、改键、二次开发本项目代码 | GPL-2.0，随便用；**再分发时同样以 GPL-2.0 开放源码**并保留版权声明 |
| 把绿色包再分发给别人 | 连同本文件与 `LICENSE` 一起发；本项目与 `decode.exe` 都是 GPL-2.0，须一并提供源码（含 `tools/g7221-decoder/`） |
| 商业分发（卖硬件或卖软件） | GPL-2.0 允许商用，但**必须开源你修改后的代码**；另需先联系 Polycom 确认 G.722.1 专利许可，模型权重条款向 FunASR / ModelScope 上游确认 |
| 只做按键映射、不要语音 | 删掉 `_internal/models/`、`_internal/tools/`、`sherpa_onnx/`，就不涉及 GPL 与专利问题 |
