# -*- mode: python ; coding: utf-8 -*-
# PyInstaller 打包配置。用法：在 pc/ 目录下执行
#   python -m PyInstaller --noconfirm --clean CMCC_RC_Keys.spec
# 产物：dist/CMCC_RC_Keys.exe（单文件，约 11 MB）
#
# 注意：datas 里的 'assets' 是相对 spec 所在目录的路径。不要用 --specpath 把 spec
# 挪到别处，否则运行时找不到背景图（踩过）。

a = Analysis(
    ['remote_ui.py'],
    pathex=[],
    binaries=[],
    datas=[('assets', 'assets')],
    hiddenimports=[],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[],
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
    upx=True,
    upx_exclude=[],
    runtime_tmpdir=None,
    console=False,                 # 不要黑框
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon='assets/app.ico',
)
