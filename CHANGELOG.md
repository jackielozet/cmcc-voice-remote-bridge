# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

## [1.1.0] - 2026-10-29

固件去掉写死的设备信息，改为自动识别。**串口命令、NVS 映射表格式、`remotekey.ini` 格式全部未变**，
旧版可以直接覆盖升级，配置不用重做。旧固件 `firmware/cmcc_s3_bridge/` 原样保留。

### 新增

- 固件 v3（`firmware/cmcc_s3_bridge_v3auto_20261029_0051/`）：删掉写死的遥控器名字、MAC 和厂商指纹
  （`"LY RC"`），改为解析广播包打分 —— 设备地址 / 名字前缀 / 厂商 ID /
  HID Appearance `0x03C0`–`0x03C4` / 服务 UUID `0x1812`，取最高分连接
- 连上后自动把设备地址写入 NVS（`autopin`），之后开机按地址直连
- 读取 `0x2A4B` HID Report Map，按**顶层集合 + 位宽**判断每个 report 是键盘、消费者还是数据通道，
  订阅不再依赖固定 report id
- 读不到描述符时自动退回旧规则（只订 `0x01` / `0x03`），行为与 1.0.0 一致
- 新串口命令：`CFG`（查看/修改识别条件）、`TARGET`（查看/指定/清除已记住的地址）、
  `MAPDUMP`（打印描述符原文与分类结果）
- 文档 [`docs/auto-identify_20261029_0051.md`](docs/auto-identify_20261029_0051.md)：
  打分规则、分类算法、实测日志

### 变更

- README 烧录章节区分 v1 / v3 两个固件，并补上 S3 无法直接 `upload` 的原因与三种进下载模式的办法
- 「已知限制」中「MAC 硬编码」一条改为仅适用于 v1

### 文档

- 补充 1.0.0 漏掉的一个现象：`SAVE` 偶发 `ERR save` 是 NVS 瞬时空间不足，
  `REBOOT` 一次即恢复，不用重刷固件

### 已知问题

- v3 的自动识别只在这一种遥控器（讯飞 XFRCB22 / RTL8762C）上验证过。换型号能否识别，
  取决于它的广播里是否带 HID Appearance 或 `0x1812`；两者都没有时需要 `CFG any 1` 兜底
- 附近有多只 HID 设备时可能连错，误连会被 `autopin` 记住，需 `TARGET CLEAR`
- TV 键、语音通道的问题同 1.0.0

## [1.0.0] - 2026-10-28

首个公开版本。在此之前是本机自用的 0.x 原型，公开发布时按功能整理成下列条目。

### 新增

- 固件（`firmware/cmcc_s3_bridge`）：ESP32-S3 作为 BLE 中心连接 `CMCC_Voice_Remote`，
  以 USB 复合设备（HID 键盘 + 消费者控制 + CDC 串口）输出按键
- 映射表存 NVS，支持 `INFO/LIST/SET/DEL/SAVE/RESET/DEFAULTS/CLEAR` 命令，断电不丢
- 学习模式（`LEARN` 只上报 id）、原始帧打印（`RAW`）、广播打印（`SCANDUMP`）、
  音频帧转储（`AUDIO`，实验性）
- `BOOT` 命令：软件跳下载模式，烧录不必按 BOOT 键
- `migv` 迁移机制（当前 v2）：老版本升级后自动把关机键改成 Win+L、`#` 改成 Delete、录音键改成 Win+H
- 配置工具（`pc/remote_ui.py`）：遥控器照片当背景 + 引线到各键文本框，单击即可改键
- 录键弹窗支持组合键（最多 4 键）、常用动作下拉、录键期间屏蔽系统热键
- Windows 锁屏策略临时禁用（Win+L 拦不住，改为临时关策略），关窗/超时/强杀三种路径都会还原
- `remotekey.ini` 配置文件：同目录优先级高于设备，缺失时自动生成
- 逆向工具（`tools/`）：BLE 直连抓取、btsnoop/HCI 解析、Report Map 与 Report Reference 交叉校验、UART 嗅探
- 归档手机中继方案（`tools/android-relay/`）及其 Linux 键码表

### 已知问题

- TV 键在蓝牙侧零上报（遥控器走红外直发），无法映射
- 语音通道（`0x6287` / `0xFC`）未解开：订阅会导致遥控器断链，音频不是标准 IMA ADPCM
- 目标遥控器 MAC 硬编码在固件里，换设备需改一行重烧
- 打包出的 exe 会被 Windows 11 Smart App Control 拦截（按哈希查云端信誉），
  建议用 `pc/run.bat` 或 `pythonw` 启动
