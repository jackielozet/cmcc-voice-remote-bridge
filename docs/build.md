# 编译、烧录与发版

---

## 一、固件

### 环境

| 项 | 版本（本项目实测） |
|---|---|
| Arduino IDE / arduino-cli | 任意近期版本 |
| Arduino-ESP32 核心 | **3.3.12** |
| 开发板 | ESP32-S3（`esp32:esp32:esp32s3`） |

核心 3.x 才带 `USBHIDKeyboard` / `USBHIDConsumerControl` 这套 API，2.x 编译不过。

### 关键选项

| 选项 | 值 | 为什么 |
|---|---|---|
| USB CDC On Boot | **Enabled** | 否则 `Serial` 走 UART，配置工具连不上端口 |
| USB Mode | 默认 | TinyUSB 由 HID 库自动启用 |
| PSRAM / Flash Size | 默认 | 固件没用到 PSRAM |

等价命令行（照抄即可）：

```bash
FQBN="esp32:esp32:esp32s3:USBMode=default,CDCOnBoot=cdc"
arduino-cli compile --fqbn "$FQBN" firmware/cmcc_s3_bridge
arduino-cli upload  --fqbn "$FQBN" -p COM11 firmware/cmcc_s3_bridge
```

### 必改的一行

```cpp
static const char *TARGET_ADDR = "0c:f3:cc:63:10:e3";   // 改成你的
```

查自己的 MAC：串口终端 115200 连上后发 `SCANDUMP 1`，按一下遥控器任意键，
看到 `# ADV <地址> ... name='CMCC_Voice_Remote'` 就抄过来。

### 烧录（三种方式）

**A. IDE 直接上传** —— 最简单，插 OTG 口，选对 COM，点上传。

**B. 命令行 + 导出二进制**（不装 Arduino，给别人烧）

Arduino IDE 里 `项目 → 导出已编译的二进制文件`，会得到 `cmcc_s3_bridge.ino.merged.bin`，
一条命令烧到 0x0：

```bash
esptool --chip esp32s3 -p COM11 write-flash 0x0 cmcc_s3_bridge.ino.merged.bin
```

**C. 分段烧录**（只有 app 变了、不想动 bootloader 时）

| 偏移 | 文件 |
|---|---|
| `0x0` | `*.bootloader.bin` |
| `0x8000` | `*.partitions.bin` |
| `0xE000` | `boot_app0.bin`（在 Arduino-ESP32 核心的 `tools/partitions/` 下） |
| `0x10000` | `*.ino.bin` |

### 免按 BOOT 进下载模式

固件实现了 `BOOT` 命令：串口发 `BOOT`，它自己写 `RTC_CNTL_FORCE_DOWNLOAD_BOOT` 再复位，
端口会重新枚举成下载模式，随后 esptool 接上即可。省掉「按住 BOOT 再点 RST」那一步。

```bash
# 伪代码：先发命令，再等端口重新枚举，再烧
printf 'BOOT\n' > COM11 && sleep 2 && esptool --chip esp32s3 -p COM11 write-flash 0x0 merged.bin
```

---

## 二、PC 配置工具

### 运行

```bash
pip install -r pc/requirements.txt     # 只有 pyserial
python pc/remote_ui.py
```

Windows 下双击 `pc/run.bat` 也行（用 `pythonw` 启动，不带黑框）。

### 打包 exe

```bash
pip install -r pc/dev-requirements.txt  # pillow + pyinstaller
pc\build_exe.bat
```

产物 `pc\dist\CMCC_RC_Keys.exe`，单文件、约 11 MB。

打包注意两点（都踩过）：

1. **不要用 `--specpath`**：资源路径会按 spec 所在目录算，运行时找不到 `assets/bg.png`。
   本项目用 `--add-data "<绝对路径>\assets;assets"`；
2. **窗口标题栏图标**要在 `icon=` 里指定 `assets/app.ico`，否则是 Tk 默认羽毛。

### 自检

```bash
python pc/test_ini.py          # ini 往返，不需要硬件
python pc/test_combo.py        # 下拉项全量解析，不需要硬件
python pc/test_shield3.py      # 热键屏蔽 / Win+L 策略 / 看门狗，不需要硬件
python pc/test_live.py         # 需要接设备：读映射 + 截图
python pc/test_save.py         # 需要接设备：改 -> 存 -> 回读比对
```

GUI 脚本必须用**带 tkinter 的 Python**（官方安装包默认带）。CI 里只跑不需要硬件的部分。

---

## 三、发版流程

1. 改 `CHANGELOG.md`，`pc/remote_ui.py` 顶部的版本注释和固件 `MAP_VERSION`（若数据结构变了才升）
2. 打 tag：`git tag -a v1.0.0 -m "v1.0.0" && git push --tags`
3. 打包 exe（见上），算个哈希便于用户校验：
   ```powershell
   Get-FileHash .\pc\dist\CMCC_RC_Keys.exe -Algorithm SHA256
   ```
4. 在 GitHub Releases 里新建 release：附上 `CMCC_RC_Keys.exe` 和（可选）`merged.bin`，
   正文贴 CHANGELOG 对应段落 + SHA256
5. **exe 不进 git**：`.gitignore` 里已经排除，避免每次重打包都在历史里留一份 11 MB 二进制

> 发布出去的 exe 在别人的 Windows 11 上大概率会被 Smart App Control 拦（按哈希走云端信誉库，
> 未知文件一律拦）。Release 正文里写清楚三条路：用 `run.bat` / 自己打包 / 买签名证书。
> 详见 [troubleshooting.md](troubleshooting.md)。
