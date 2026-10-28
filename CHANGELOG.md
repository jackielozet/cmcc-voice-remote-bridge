# 更新日志

格式参考 [Keep a Changelog](https://keepachangelog.com/zh-CN/1.1.0/)，版本号遵循语义化版本。

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
