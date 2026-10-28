# 架构与实现要点

这篇文档记录**为什么这么设计**，以及几个踩过的坑。想直接跑起来看 [README 快速开始](../README.md#快速开始)。

---

## 一、三段链路，为什么最后只剩一段

这只遥控器一共试过三条路，前两条都被证伪或放弃：

| 方案 | 链路 | 结论 |
|---|---|---|
| 直连 Windows | 遥控器 --BLE--> Windows 蓝牙栈 | **不行**，代码 10，一个键收不到 |
| 手机中继 | 遥控器 --BLE--> 安卓（HID 主机）--ADB--> PC --SendInput--> Windows | 能用，但要手机常插、会重复消费按键（手机自己也响应音量/主页），已归档 |
| **ESP32 桥** | 遥控器 --BLE--> ESP32-S3 --USB HID--> Windows | **采用**，免驱、免装软件、断电保存映射 |

### Windows 为什么不行

不是驱动问题。Windows 的 HID over GATT 由用户态驱动 `mshidumdf.sys` 接管，它在初始化前会做严格校验：
每个 Report 特征的 **Report Reference 描述符**（0x2908，给出 report ID 和类型）必须能在
**Report Map**（0x2A4B）里找到对应的声明。

这只遥控器有 **9 个 Report 特征**，实测有 **3 个**的 Reference 与 Map 声明对不上。
`tools/hci/verify_hid.py` 就是干这个的：解析 `btsnoop` 日志，把 ATT 请求与响应配对，
再把 224 字节的 Report Map 解出来逐项比对。结果对不上 → 驱动拒绝初始化 → 设备管理器报代码 10。

而对一个通用 BLE 中心（安卓、ESP32）来说，`0x1812` 只是一组普通 GATT 特征，没有任何一方会做这种校验，
所以同一只遥控器在手机上按键上报完全正常。

---

## 二、固件侧

### 扫描与连接

```
每 2.5 秒一轮：
  被动扫描 1 秒（窗口 30ms / 周期 300ms，10% 占空比）
    ├─ 命中判定：广播名 == "CMCC_Voice_Remote"
    │            或 地址 == TARGET_ADDR
    │            或 广播负载指纹匹配
    ├─ 命中 → 记下设备，等扫描自然结束（不能在回调里 stop，见下）
    └─ 未命中 → 冷却后下一轮

连接：createClient → setMTU(247) → connect(addr, 5000ms) → 订阅 9 个 report
        失败立刻换新 client 重试一次（遥控器按键唤醒后只广播很短一段，趁热连）
```

两个坑：

1. **绝不能在 `onResult` 回调里调 `getScan()->stop()`**。回调跑在协议栈的 GAP 上下文里，
   在里面停扫描会把状态机卡住，之后 `esp_ble_gattc_open` 要等将近半分钟才发得出去（实测 31 秒才失败）。
   正确做法是只记下目标，让扫描自己跑完，回到 `loop()` 再连。
2. **必须按 handle 索引特征**，不能用 `getCharacteristics()`。这只遥控器 9 个 Report 特征的
   UUID 全是 `0x2A4D`，按 UUID 索引的 map 会互相覆盖只剩最后一个，表现是「只能收到键盘报告，
   音量/OK/返回这些消费者键完全收不到」。所以代码里用 `getCharacteristicsByHandle()`。

### 配对策略

**不主动发起配对**。曾经在连上后调过 `BLESecurity::startSecurity`，结果链路在 5.5 秒后被遥控器主动断掉；
原生是「连上就能用」，说明遥控器不希望被强制走安全流程。现在连上就直接订阅。

### 映射表存储

- 存在 NVS（`Preferences`），格式：`MAP_VERSION(4 字节) + MAX_BINDS × Bind 结构`
- `MAX_BINDS = 64`，`MAX_KEYS = 4`
- 读取时校验版本，不匹配就整体丢弃回落到默认表
- `SET` / `DEL` 只改内存并置 `dirty=1`，`SAVE` 才落盘，`INFO` 能看出来有没有未保存的改动
- 升级用 `migv` 计数器做一次性迁移（当前 v2）：
  `0x1066 → Win+L`、`0x10CC → Delete`、`0x103E → Win+H`。老用户刷完新固件不用手动改键

### USB 复合设备

TinyUSB 一个设备三个接口，共用一根线：

| 接口 | 类 | 用途 |
|---|---|---|
| HID 键盘 | boot keyboard，8 字节报告 | 普通键与组合键，`0xE0`–`0xE7` 修饰键 |
| HID 消费者 | consumer control | 音量/静音 |
| CDC | serial | 配置工具的命令通道 |

`console=False` 无关，`USB CDC On Boot` 必须启用，否则 `Serial` 走 UART，配置工具看不到端口。

---

## 三、配置工具侧（`pc/remote_ui.py`）

单文件 Tkinter，不依赖除了 pyserial 之外的库；打包成 exe 后同理。

| 模块 | 做法 |
|---|---|
| 背景 | 遥控器照片裁掉白边、合成白底后当 Canvas 底图（`make_bg.py` 生成，运行时不需要 Pillow） |
| 键位 | `REMOTE` 表写死每个键的 id 与照片坐标；`find_keys.py` 用连通域分析自动找中心点辅助校对 |
| 引线 | 按 x 坐标分左右两列，同列按 y 排序；框的上下边界贴齐该列第一个/最后一个键，避免斜跨 |
| 命中测试 | Canvas 上按矩形命中，悬停高亮 |
| 录键 | 弹窗里装 `WH_KEYBOARD_LL` 钩子，把按键全部吞掉，只把事件交回自己；因此 Win+D / Win+E / Alt+Tab 不会真的触发 |
| 钩子兜底 | 钩子线程带心跳看门狗：界面卡死 6 秒没打点就自动卸载钩子，避免把整台机器的键盘吞死 |
| Win+L | 系统安全保留动作，钩子拦不住。改用「临时禁用锁屏策略」：管理员助手进程写 `DisableLockWorkstation`，关窗/超时/被强杀三种路径都会还原 |
| 配置优先级 | 同目录 `remotekey.ini` 优先，没有则按固件默认生成一份；保存时同时写设备和 ini |
| 打包 | PyInstaller onefile，`--add-data assets`；注意 `--specpath` 会让资源相对路径失效 |

### 布局自检

`check_layout.py` 不是给人看的工具，是防回归的：它检查引线端点是否真的落在键上、
框有没有出界、同列是否重叠。改过坐标后跑一次，比肉眼看截图靠谱。
