# tools/ —— 逆向与分析脚本

这些脚本**不是使用本项目的必需品**，它们记录了「这个遥控器是怎么被摸清楚的」，
方便同类设备（别的型号的 BLE 遥控器、机顶盒配件）照着走一遍。

共同点：

- 大多直接读**工作目录下的** `btsnoop_hci.log`，请在那个目录里执行，或把日志放过去；
- 抓 HCI 日志的方法见各子目录 README（安卓开启蓝牙 HCI 日志 → 拉 `btsnoop_hci.log`）；
- 脚本里的 MAC `0C:F3:CC:63:10:E3` 是作者那只遥控器，**换成你自己的**。

依赖：

```bash
pip install -r requirements.txt          # bleak / pyserial / pillow
```

---

## ble/ —— 直连抓取

用 [bleak](https://github.com/hbldh/bleak) 在 Windows/macOS/Linux 上直接当 BLE 中心，
不经过任何中间设备，适合「先确认这只遥控器到底发什么」。

| 脚本 | 作用 |
|---|---|
| `ble_hid.py` | 枚举 `0x1812` 下的全部特征与描述符，打印 UUID、属性、Report Reference |
| `ble_hid_capture.py` | 订阅全部可 notify 的特征，把每一次按键上报打出来（带时间戳） |
| `ble_notify.py` | 订阅指定特征（默认 `0x6287` 语音通道 + 电池），用于抓私有通道 |
| `ble_unpaired.py` | 在**未配对**状态下重新探测一遍，用于排除「配对后才被系统抢走」的可能 |
| `smp_probe.py` | 解析 `btsnoop` 里的广播/连接/SMP 过程，确认配对与加密发生在哪一步 |

关键结论：`ble_hid.py` 在 **Windows 上能读到全部特征、也能订阅**——
说明「Windows 收不到按键」不是权限问题，而是 HID 驱动层的校验问题。

---

## hci/ —— btsnoop / HCI 日志解析

真正的证据都在这里。安卓 `btsnoop_hci.log` 是标准 btsnoop 格式（24 字节包头 + 记录），
下面这些脚本都是自己解，不依赖 Wireshark（方便在没装 Wireshark 的机器上跑）。

| 脚本 | 作用 |
|---|---|
| `btsnoop_parse.py` | 通用解析：把 ATT 层请求/响应、通知按时间线打出来 |
| `gatt_map.py` | 从抓包里重建服务/特征表：handle 范围、UUID、属性 |
| `hid_extract.py` | 专门抽 HOGP：Report Map、各 Report 的 value handle、按键通知 |
| `verify_hid.py` | **本项目的关键证据**：把每个 Report Reference 描述符（0x2908）与 Report Map 里声明的 report ID / 类型逐项比对，输出不匹配清单 |
| `experiments/` | 语音通道攻关时的一次性分析脚本，见下 |

`verify_hid.py` 的输出就是「3 个 Report 对不上」的出处，也是 Windows 报代码 10 的根因。

### experiments/

一次性脚本，都读工作目录下的 `btsnoop_hci.log`，用来分析 `0xFC` 语音通道：

| 脚本 | 干了什么 | 结论 |
|---|---|---|
| `audio_probe.py` | 统计各 handle 的通知条数、长度、时间跨度 | 确认 `0xFC` 是音频流 |
| `stream_dump.py` | 按 seq/frag 还原分片流 | 分片顺序可还原 |
| `burst.py` | 按时间间隔切出「一次说话」的 burst | 单次 burst 与分片速率可测 |
| `frame_layout.py` | 逐偏移统计零占比与取值分布，猜帧头位置 | 帧头结构有规律，但字段含义未定 |
| `wr_scan.py` | 扫描写操作，找「控制命令」写在哪个 handle | 找到控制特征，但握手时序未解 |
| `adpcm_test.py` | 把负载按 IMA ADPCM 各种 order 解成 PCM 并算 RMS | **不是标准 IMA ADPCM**（熵 7.35 bit/sample），解出来是噪声 |

结论写在 [../docs/troubleshooting.md](../docs/troubleshooting.md) 最后一节。
订阅语音通道会**导致遥控器主动断链**，所以这部分没有进固件。

---

## uart/ —— 调试焊盘嗅探

| 脚本 | 作用 |
|---|---|
| `uart_capture.py` | 只用 GND + TX 两根线监听（不发送），自动扫常见波特率，把收到的字节存 bin + log |

用法：

```bash
python uart_capture.py list                # 列串口
python uart_capture.py scan COM6           # 每个波特率试一遍
python uart_capture.py read COM6 115200 60  # 指定波特率听 60 秒
```

对这只遥控器：焊盘上能听到启动日志，但按键不走串口（走蓝牙），
所以这条线只用来确认主控型号与启动过程。

---

## android-relay/ —— 手机中继方案（已归档）

| 脚本 | 作用 |
|---|---|
| `rc_bridge.py` | 安卓当 HID 主机，`getevent` 读键 → ADB → Windows `SendInput` 注入；支持 `--tap` 只打印 |

能用，但有硬伤：手机必须常插 USB 并开调试、按键会被手机**同时消费**（按音量键手机音量也变）、
遥控器麦克风拿不到。已被 ESP32 方案取代，留着仅供对照。
配套的键码表（Linux `input_event` 码）见 [../docs/keymap-android-relay.md](../docs/keymap-android-relay.md)。
