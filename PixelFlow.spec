# -*- mode: python ; coding: utf-8 -*-
"""
PixelFlow PyInstaller 打包配置
用法: pyinstaller PixelFlow.spec

体积策略：
- 不使用 collect_all('PySide6')（会把 QML/WebEngine 等上百 MB 无关文件打进去）
- 只额外拷贝运行桌面程序必需的少量 Qt 插件目录：
    plugins/platforms   → qwindows.dll（缺了必崩）
    plugins/styles      → Windows 样式
    plugins/imageformats → 图标/图片解码（可选但建议保留）
    plugins/iconengines
- PySide6 主 DLL / pyd 仍由 PyInstaller 分析 import 自动收集
"""

import sys
from pathlib import Path

block_cipher = None
ROOT = Path(SPECPATH)

sys.path.insert(0, str(ROOT))
from config import APP_NAME
sys.path.pop(0)

_ICON = str(ROOT / 'resources' / 'app.ico')

# 仅保留启动与常规 UI 必需的插件子目录（体积通常几 MB～十几 MB，不是整包 Qt）
_ESSENTIAL_PLUGIN_SUBDIRS = (
    'platforms',      # qwindows.dll —— 必须
    'styles',         # 窗口样式
    'imageformats',   # png/ico/jpeg 等
    'iconengines',    # SVG 图标引擎（项目用了 svg 勾选图标）
    'generic',        # 部分平台辅助
)


def _pyside6_root() -> Path | None:
    try:
        import PySide6
        return Path(PySide6.__file__).resolve().parent
    except Exception:
        return None


def _collect_essential_qt_plugins():
    """
    返回 datas 列表: (src, dest)
    dest 统一为 PySide6/plugins/<subdir>，与 rthook / app 内 QT_PLUGIN_PATH 一致。
    """
    root = _pyside6_root()
    if root is None:
        return []
    plugins = root / 'plugins'
    if not plugins.is_dir():
        return []

    items = []
    for name in _ESSENTIAL_PLUGIN_SUBDIRS:
        src = plugins / name
        if src.is_dir():
            items.append((str(src), str(Path('PySide6') / 'plugins' / name)))
    return items


def _optional_dir_datas(src_name: str, dest_name: str):
    p = ROOT / src_name
    if p.is_dir():
        return [(str(p), dest_name)]
    return []


a = Analysis(
    [str(ROOT / 'app.py')],
    pathex=[str(ROOT)],
    binaries=[],
    datas=[
        (str(ROOT / 'resources'), 'resources'),
        *_optional_dir_datas('presets', 'presets'),
        # 只打必需 Qt 插件，不 collect_all
        *_collect_essential_qt_plugins(),
    ],
    hiddenimports=[
        'core.processors.transparent_processor',
        'core.processors.basic_processor',
        'core.processors.img2doc_processor',
        'core.processors.overlay_processor',
        'core.processors.metadata_processor',
        'openpyxl',
        'openpyxl.cell',
        'openpyxl.utils',
        'xml.etree.ElementTree',
        'xml.etree',
        'xml',
        'email',
        'email.mime',
        'email.mime.text',
        'email.mime.multipart',
        'PySide6.QtCore',
        'PySide6.QtGui',
        'PySide6.QtWidgets',
        'PySide6.QtNetwork',
        'PySide6.QtSvg',  # 若资源用到 svg
    ],
    hookspath=[],
    hooksconfig={},
    runtime_hooks=[str(ROOT / 'packaging' / 'rthook_pyside6.py')],
    excludes=[
        'tkinter', 'unittest', 'html',
        'pydoc', 'doctest', 'difflib',
        # 明确排除用不到的大型 Qt 模块（分析阶段减少误收集）
        'PySide6.Qt3DAnimation',
        'PySide6.Qt3DCore',
        'PySide6.Qt3DExtras',
        'PySide6.Qt3DInput',
        'PySide6.Qt3DLogic',
        'PySide6.Qt3DRender',
        'PySide6.QtBluetooth',
        'PySide6.QtCharts',
        'PySide6.QtDataVisualization',
        'PySide6.QtMultimedia',
        'PySide6.QtMultimediaWidgets',
        'PySide6.QtPositioning',
        'PySide6.QtQml',
        'PySide6.QtQuick',
        'PySide6.QtQuick3D',
        'PySide6.QtQuickWidgets',
        'PySide6.QtRemoteObjects',
        'PySide6.QtSensors',
        'PySide6.QtSerialPort',
        'PySide6.QtSql',
        'PySide6.QtTest',
        'PySide6.QtTextToSpeech',
        'PySide6.QtWebChannel',
        'PySide6.QtWebEngine',
        'PySide6.QtWebEngineCore',
        'PySide6.QtWebEngineWidgets',
        'PySide6.QtWebSockets',
        'PySide6.QtXml',
        'PySide6.QtPdf',
        'PySide6.QtPdfWidgets',
        'PySide6.QtHttpServer',
        'PySide6.QtWebView',
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
    # UPX 偶发破坏 Qt 插件 DLL
    upx=False,
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
    upx=False,
    upx_exclude=[],
    name=APP_NAME,
    contents_directory='.',
)
