# 参与贡献

欢迎 issue 和 PR。这个项目是围绕**一只特定型号的遥控器**做出来的，所以最有价值的贡献通常是：

- 你这只遥控器的**不同表现**（型号不同、固件版本不同、键码不同）
- 别的 BLE 遥控器/机顶盒配件的适配
- 语音通道（`0x6287` / `0xFC`）的突破
- 配置工具在非 100% 缩放 / 高 DPI 下的排版问题

---

## 提问前先自查

1. 读一遍 [docs/troubleshooting.md](docs/troubleshooting.md)，八成的问题都在里面；
2. 串口日志开 `RAW 1` 和 `SCANDUMP 1` 再复现一次；
3. 确认 `TARGET_ADDR` 是你自己那台的 MAC。

## 报 issue 请带上

| 项目 | 说明 |
|---|---|
| 遥控器型号 | 外壳标签上的型号 + 广播名（`SCANDUMP` 里能看到） |
| 固件版本 | 串口发 `INFO`，贴返回的那行 |
| 现象 | 精确到「按哪个键、期望什么、实际什么」 |
| 日志 | `RAW 1` / `SCANDUMP 1` 的输出，**贴文本不要截图** |
| 环境 | Windows 版本、Python 版本、ESP32-S3 板型 |

> MAC 地址可以只保留前三段（如 `0C:F3:CC:xx:xx:xx`），不影响排查。

## 代码风格

- Python：PEP 8，4 空格缩进，**不引入新依赖**（除了 `pyserial`，其它都要走可选导入）；
- 固件：Arduino 2 空格缩进，注释写「为什么」而不是「做了什么」，踩过的坑请留在代码旁边；
- 中文注释和中文 UI 文案都可以，这个项目就是中文优先。

## 提交前自检

```bash
python -m compileall -q pc tools
python pc/test_ini.py         # 不需要硬件
python pc/test_combo.py
python pc/test_shield3.py
```

需要硬件的自检（`test_live.py` / `test_save.py` / `test_roundtrip.py`）如果跑不了，
在 PR 描述里说明即可。

## PR

- 一个 PR 只做一件事，附带「怎么验证的」；
- 改了界面坐标就顺手跑 `python pc/check_layout.py`（引线端点是否还落在键上）；
- 改了固件协议就同步更新 `docs/protocol.md`，**命令表以文档为准**。

提交即表示同意以本项目 [MIT 许可证](LICENSE) 授权。
