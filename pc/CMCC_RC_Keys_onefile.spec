# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 单文件（onefile）配置 —— 正式发布就用这一份（1.3.0 起）。
#
# 用法（在 pc/ 目录下）：
#   python -m PyInstaller --noconfirm --clean --distpath dist_onefile \
#       --workpath build_onefile CMCC_RC_Keys_onefile.spec
# 产物：dist_onefile/CMCC_RC_Keys.exe（单个 exe）
#
# 差异只在最后一步：binaries/datas 直接塞进 EXE（exclude_binaries=False），
# 不做 COLLECT。运行时全部解压到 %TEMP%\_MEIxxxx\。

import os

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

BASE = os.path.dirname(os.path.abspath(SPEC))
UP2 = os.path.normpath(os.path.join(BASE, '..', '..'))

MODEL_DIR = os.path.join(UP2, 'asr_proto', 'models',
                         'sherpa-onnx-paraformer-zh-small-2024-03-09')
DECODE_EXE = os.path.join(UP2, 'tools', 'libg7221', 'decode.exe')

datas = [('assets', 'assets')]
binaries = []
hiddenimports = []

if os.path.isdir(MODEL_DIR):
    datas.append((MODEL_DIR,
                  os.path.join('models',
                               'sherpa-onnx-paraformer-zh-small-2024-03-09')))
else:
    print('!! 警告：找不到模型目录，打包后语音功能不可用：%s' % MODEL_DIR)

if os.path.exists(DECODE_EXE):
    datas.append((DECODE_EXE, os.path.join('tools', 'libg7221')))
else:
    print('!! 警告：找不到 decode.exe，打包后语音功能不可用：%s' % DECODE_EXE)

try:
    _d, _b, _h = collect_all('sherpa_onnx')
    datas += _d
    binaries += _b
    hiddenimports += _h
except Exception as exc:
    print('!! collect_all(sherpa_onnx) 失败：%s' % exc)

try:
    binaries += collect_dynamic_libs('onnxruntime')
except Exception:
    pass

# 内嵌 esptool：目标机没装 Python / esptool 时也能刷固件。
# 1.3.0 曾用 [sys.executable, "-m", "esptool"] 兜底，单文件 exe 下等于再开一个
# 上位机（sys.executable 就是它自己），刷不了也没法退出。
# 注意：esptool 是 GPL-2.0，内嵌后整个 exe 按 GPL-2.0 分发，见 docs/third-party.md。
for _mod in ('esptool', 'esp_pylib', 'rich', 'rich_click', 'click',
             'reedsolo', 'intelhex', 'cryptography'):
    try:
        _d, _b, _h = collect_all(_mod)
        datas += _d
        binaries += _b
        hiddenimports += _h
    except Exception as exc:
        print('!! collect_all(%s) 失败：%s' % (_mod, exc))

hiddenimports += ['serial', 'serial.tools', 'serial.tools.list_ports',
                  'numpy', 'asr_win',
                  'PIL', 'PIL.Image', 'PIL.ImageTk']

a = Analysis(
    ['remote_ui.py'],
    pathex=[BASE],
    binaries=binaries,
    datas=datas,
    hiddenimports=hiddenimports,
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=['matplotlib', 'scipy', 'pandas', 'IPython', 'pytest', 'torch'],
    noarchive=False,
    optimize=0,
)
pyz = PYZ(a.pure)

exe = EXE(
    pyz,
    a.scripts,
    a.binaries,
    a.datas,
    [],
    name='CMCC_RC_Keys',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # onnxruntime 被 upx 压过容易崩，别压
    runtime_tmpdir=None,           # 解压到系统 %TEMP%
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/app.ico',
    manifest='assets/app.manifest',
    version='assets/version_info.txt',
    exclude_binaries=False,
)
