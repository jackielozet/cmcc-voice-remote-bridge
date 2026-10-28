# CMCC 语音遥控器 → Windows 按键对照表（手机中继方案，已归档）

> **这篇是历史记录，不是当前用法。** 现在的主力方案是 ESP32-S3 桥（见 [README](../README.md)），
> 键码表在这里保留，是因为它是最早一批实测数据，Linux `input_event` 码与 HID usage 的对应关系仍有参考价值。
> 脚本在 [`tools/android-relay/`](../tools/android-relay/)。

- 设备：`CMCC_Voice_Remote`（讯飞 XFRCB22，RTL8762C）
- 链路：遥控器 --BLE--> vivo X21i（手机当 HID 主机）--USB/ADB--> `rc_bridge.py` --> SendInput --> Windows
- 整理时间：2026-10-26
- 所有键码均由手机 `getevent` 实测取得，非推测

## 一、映射表（Linux 键码）

| 遥控器按键 | Linux 键码 | Windows 输出 | 验证 |
|---|---|---|---|
| 上 | 103 | ↑ | ✅ |
| 下 | 108 | ↓ | ✅ |
| 左 | 105 | ← | ✅ |
| 右 | 106 | → | ✅ |
| 0 – 9 | 11,2,3,4,5,6,7,8,9,10 | 0 – 9 | ✅ |
| OK | 353 | Enter | ✅ |
| 返回 | 158 | Esc | ✅ |
| 主页 | 172 | Win 键（开始菜单） | ✅ |
| 菜单 | 127 | 右键菜单键（Apps） | ✅ |
| 删除 | 55 | Backspace（退格） | ✅ |
| 音量 + | 115 | 系统音量 + | ✅ |
| 音量 − | 114 | 系统音量 − | ✅ |
| 静音 | 113 | 系统静音 | ✅ |
| 频道 + | 104 | PageUp | ✅ |
| 频道 − | 109 | PageDown | ✅ |
| 语音键 | 63 | Win + H（Windows 语音键入） | ✅ |
| 电源 | 116 | **故意禁用**（防误触睡眠） | — |
| TV 键 | 无 | **无事件上报**，已放弃 | ❌ |

## 二、共用 linux 240 的三个键（靠扫描码区分）

这三个键在手机上都上报成 `linux=240 (KEY_UNKNOWN)`，单看键码无法区分，
脚本改为读取 `MSC_SCAN`（HID 扫描码）来分辨。

| 遥控器按键 | MSC_SCAN | 含义 | Windows 输出 | 验证 |
|---|---|---|---|---|
| `#` 键 | `0x0700CC` | Keyboard 页 usage 0xCC | 字符 `#`（UNICODE 注入） | ✅ |
| 设置键 | `0x0C00EB` | Consumer 页 usage 0xEB | **Ctrl + C**（复制） | ✅ |
| M 键 | `0x0C00F0` | Consumer 页 usage 0xF0 | **Ctrl + V**（粘贴） | ✅ |

## 三、实现要点（改键时注意）

| 项目 | 说明 |
|---|---|
| 组合键 | `send_combo([vk1, vk2])`，顺序按下、逆序抬起 |
| 字符键 | 用 `send_unicode("#")`，走 `KEYEVENTF_UNICODE`，与键盘布局无关 |
| 扩展键 | Win / 菜单键扫描码是 `0xE0xx`，必须带 `KEYEVENTF_EXTENDEDKEY`，否则系统直接丢弃 |
| 新增 240 键 | 在 `SCAN_NAME` 加名字，在 `SCAN_SPECIAL` 加动作 |
| 新增普通键 | 在 `CODE_VK` 里加 `键码: VK`，或在 `SPECIAL` 里加函数 |

## 四、已知限制

1. 手机是中转主机，会**同时消费**按键：按音量键手机音量也变，按主页键手机回桌面。不 root 无法屏蔽。
2. 语音键只是唤起 Windows 语音条，用的是**电脑麦克风**。遥控器自己的麦克风走私有通道 `0x6287`（需解 ATVV 协议），目前拿不到。
3. 手机必须插着 USB 并开启 USB 调试。
4. TV 键在手机内核层无任何事件，中继方案无法支持。
5. 电源键映射为 0，即不发送任何按键。

## 五、日常使用

```bash
cd tools\android-relay
python rc_bridge.py           # 注入模式
python rc_bridge.py --tap     # 只打印不注入（调试）
```

脚本会等待遥控器上线，掉线重连后自动重新挂载，无需干预。
