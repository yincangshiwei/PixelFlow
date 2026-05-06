# -*- mode: python ; coding: utf-8 -*-
"""
PixelFlow PyInstaller 打包配置
用法: pyinstaller PixelFlow.spec
"""

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)

# 从 config.py 读取应用元信息
sys.path.insert(0, str(ROOT))
from config import APP_NAME
sys.path.pop(0)

_ICON = str(ROOT / 'resources' / 'app.ico')

a = Analysis(
    [str(ROOT / 'app.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / 'resources'), 'resources'),
        (str(ROOT / 'presets'), 'presets'),
    ],
    hiddenimports=[
        'core.processors.transparent_processor',
        'openpyxl',
        'openpyxl.cell',
        'openpyxl.utils',
        'xml.etree.ElementTree',
        'xml.etree',
        'xml',
        # python-pptx / python-docx 内部依赖
        'email',
        'email.mime',
        'email.mime.text',
        'email.mime.multipart',
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[],
    excludes=[
        'tkinter', 'unittest', 'html', 'http',
        'pydoc', 'doctest', 'difflib',
    ],
    noarchive=False,
    cipher=block_cipher,
)

pyz = PYZ(a.pure, a.zipped_data, cipher=block_cipher)

exe = EXE(
    pyz,
    a.scripts,
    [],
    exclude_binaries=True,
    name=APP_NAME,
    debug=False,
    bootloader_ignore_signals=False,
    strip=False,
    upx=True,
    console=False,
    disable_windowed_traceback=False,
    argv_emulation=False,
    target_arch=None,
    codesign_identity=None,
    entitlements_file=None,
    icon=_ICON,
)

coll = COLLECT(
    exe,
    a.binaries,
    a.datas,
    strip=False,
    upx=True,
    upx_exclude=[],
    name=APP_NAME,
    contents_directory='.',
)
