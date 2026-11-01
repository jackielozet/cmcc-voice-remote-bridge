# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置（onedir 模式）
#
# 用法（在 pc/ 目录下）：
#   python -m PyInstaller --noconfirm --clean CMCC_RC_Keys.spec
# 产物：dist/CMCC_RC_Keys/ 整个目录拷到别的 Win10/Win11 机器即可直接用。
#
# 为什么用 onedir 而不是单文件：
#   语音识别要带 78MB 模型 + onnxruntime，单文件每次启动都要把上百 MB
#   解压到临时目录（慢 3~5 秒）。onedir 只初始化一次，启动快。
#
# 注意：datas 里的相对路径以 spec 所在目录为基准。不要用 --specpath 挪动 spec。

import os

from PyInstaller.utils.hooks import collect_all, collect_dynamic_libs

BASE = os.path.dirname(os.path.abspath(SPEC))
UP2 = os.path.normpath(os.path.join(BASE, '..', '..'))

# 语音识别模型（78MB）与 G.722.1 解码器，随包一起分发
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

# sherpa-onnx 的动态库（onnxruntime 等）必须完整收进去，
# 否则换台机器会报 DLL 缺失——这正是"不能因缺环境跑不起来"的关键。
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

hiddenimports += ['serial', 'serial.tools', 'serial.tools.list_ports',
                  'numpy', 'asr_win',
                  # 背景图要按窗口尺寸实时缩放，必须带 Pillow（含 tkinter 桥）
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
    [],
    exclude_binaries=True,
    name='CMCC_RC_Keys',
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=False,                     # onnxruntime 被 upx 压过容易崩，别压
    console=False,                 # 不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/app.ico',
    manifest='assets/app.manifest',   # 声明 DPI 感知，高 DPI 屏才不发虚
    version='assets/version_info.txt',  # 版本号写进 exe 属性（1.2.0）
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=False,
    name='CMCC_RC_Keys',
)
