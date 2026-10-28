# 串口协议

配置工具（`pc/remote_ui.py`）与固件之间的全部通信都走 ESP32-S3 的 **USB CDC 串口**。

| 项 | 值 |
|---|---|
| 波特率 | 115200（USB CDC 下只是形式，实际不限速） |
| 数据格式 | 8N1 |
| 编码 | 纯 ASCII，**行文本**，每行以 `\n` 结尾 |
| 十六进制 | 不带 `0x` 前缀，不带空格 |
| 日志行 | 以 `#` 开头（客户端应忽略） |

客户端发**命令**，设备回**应答**。设备也会主动吐日志和事件，所以读取端必须按行解析、
遇到不认识的行直接丢。

---

## 一、数据结构

### 映射条目

一条映射 = 遥控器上的一个键 + 它要输出的动作。

| 字段 | 类型 | 说明 |
|---|---|---|
| `id` | 16 位 | 按键标识，`(源 << 12) | usage` |
| `outType` | 8 位 | `1` = 键盘输出，`2` = 消费者（多媒体）输出 |
| `n` | 8 位 | 有效键数量，1–4 |
| `keys[]` | 8 位 × 4 | HID usage；不足 4 个补 0 |

### 按键 id 的编码

```
id = (源 << 12) | usage
```

| 源 | 含义 | 例子 |
|---|---|---|
| `1` | 键盘报告（report 0x01）里的 usage | `0x1066` = 关机键，`0x10CC` = `#` 键 |
| `2` | 消费者报告（report 0x03）里的 usage | `0x20E9` = 音量+，`0x20EB` = 设置键 |

### 输出的两种类型

| outType | keys 含义 | 例子 |
|---|---|---|
| `1` | HID 键盘 usage 序列，`0xE0`–`0xE7` 是修饰键（Ctrl/Shift/Alt/Win） | `Win+L` → `E3 0F` |
| `2` | 16 位消费者 usage：`keys[0] | (keys[1] << 8)` | 音量+ → `E9 00` |

按下时按顺序按下、抬起时逆序抬起；组合键最多 4 个键。

---

## 二、命令

| 命令 | 参数 | 成功应答 | 说明 |
|---|---|---|---|
| `INFO` | — | `OK INFO ver=1 binds=33 learn=0 enabled=1 connected=1 dirty=0 audio=1` | `dirty=1` 表示内存里改过但没 `SAVE` |
| `LIST` | — | 若干 `B …` 行 + `OK LIST` | 列出全部非空映射 |
| `SET` | `<id> <outType> <n> <k1..k4>` | `OK SET <id>` | id 已存在则覆盖，否则追加 |
| `DEL` | `<id>` | `OK DEL <id>` | |
| `SAVE` | — | `OK SAVE` | 写进 NVS；不保存则断电丢失 |
| `RESET` | — | `OK RESET` | 恢复默认表**并立即保存** |
| `DEFAULTS` | — | `OK DEFAULTS` | 只恢复到内存，不保存 |
| `CLEAR` | — | `OK CLEAR` | 清空内存里的全部映射（不保存） |
| `REPORTS` | — | 若干 `R …` 行 + `OK REPORTS count=9` | 列出发现的 HID report 特征 |
| `LEARN` | `0` / `1` | `OK LEARN <0|1>` | 学习模式：只上报 `L <id>`，**不发送按键** |
| `RAW` | `0` / `1` | `OK RAW <0|1>` | 打印每个通知的原始字节 |
| `AUDIO` | `0` / `1` | `OK AUDIO <0|1>` | 打印 `0xFC` 语音帧（实验性） |
| `SCANDUMP` | `0` / `1` | `OK SCANDUMP <0|1>` | 打印扫描到的每一条 BLE 广播 |
| `ENABLE` | `0` / `1` | `OK ENABLE <0|1>` | 输出总开关；置 0 时会先松开所有按下的键 |
| `BOOT` | — | `OK BOOT` | 软件跳下载模式（之后 esptool 可免按 BOOT） |
| `REBOOT` | — | `OK REBOOT` | 软复位 |

### 应答行格式

```
B <id> <outType> <n> <k1> [k2] [k3] [k4]   # LIST 的一条映射
R <idx> id=0x01 type=1 h=0x0025 notify=1 sub=1   # REPORTS 的一行
L <id>                                     # 学习模式下的按键事件
# ...                                      # 日志，忽略
OK <CMD> [字段...]                          # 命令成功
ERR <原因>                                  # 命令失败
```

### 错误码

| 应答 | 含义 |
|---|---|
| `ERR args` | 参数缺失或格式不对 |
| `ERR n` | `n` 超过 4（`MAX_KEYS`） |
| `ERR keys` | 声明的 `n` 与实际给的键数不符 |
| `ERR full` | 映射表满（`MAX_BINDS` = 64） |
| `ERR notfound` | `DEL` 的 id 不存在 |
| `ERR save` | NVS 写入失败 |
| `ERR unknown` | 不认识的命令 |

---

## 三、设备主动上报

| 行 | 触发 | 说明 |
|---|---|---|
| `# KEY 1066 DOWN src=keyboard usage=0x066 map=yes` | 每次按键 | 30 ms 内同一批按键只打一行，未匹配的键一定打 |
| `# RAW h=0x0025 id=0x01 len=9 | 03 00 04 ...` | `RAW 1` | 原始通知字节 |
| `# ADV <mac> rssi=-60 aType=0 advType=0 conn=1 name='CMCC_Voice_Remote'` | `SCANDUMP 1` | 每条广播，另附 `# payload …` 原始负载 |
| `# 找到遥控器 … (mac=1 name=0 指纹=0)` | 扫描命中 | 三种识别方式命中情况 |
| `# t=… 连接尝试 1/2 -> <mac>` / `# 连接成功 耗时=… ms` | 连接过程 | |
| `# !! …` | 异常 | 例如链路没加密导致读不到 0x1812 |

---

## 四、典型交互

一次「读→改→存」的完整时序：

```
→ INFO
← OK INFO ver=1 binds=33 learn=0 enabled=1 connected=1 dirty=0 audio=1

→ LIST
← B 1066 1 2 E3 0F
← B 10CC 1 1 4C
← …
← OK LIST

→ SET 1066 1 2 E3 0B        # 电源键改绑 Win+H（示例）
← OK SET 1066

→ SAVE
← OK SAVE
```

想确认某个键在蓝牙侧的真实 id，最省事的办法：

```
→ LEARN 1
← OK LEARN 1
（按一下遥控器上的目标键）
← L 20EB
→ LEARN 0
```

`L` 后面的就是 id，把它填进 `SET` 即可。
