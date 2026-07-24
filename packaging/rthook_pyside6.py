"""
PyInstaller runtime hook: 确保打包后能找到 Qt platform 插件。

PySide6 启动时需要 plugins/platforms/qwindows.dll。
部分环境下仅依赖默认搜索路径会失败，这里显式设置 QT_PLUGIN_PATH。
"""
import os
import sys


def _set_qt_plugin_path():
    # onedir: _MEIPASS 指向 _internal（或 contents 根）
    base = getattr(sys, "_MEIPASS", None)
    if not base:
        return

    candidates = [
        os.path.join(base, "PySide6", "plugins"),
        os.path.join(base, "plugins"),
        os.path.join(base, "qt6", "plugins"),
        os.path.join(base, "Qt6", "plugins"),
    ]
    for path in candidates:
        platforms = os.path.join(path, "platforms")
        if os.path.isdir(platforms):
            # 追加而非覆盖，兼容已有设置
            old = os.environ.get("QT_PLUGIN_PATH", "")
            if path not in old.split(os.pathsep):
                os.environ["QT_PLUGIN_PATH"] = (
                    path if not old else path + os.pathsep + old
                )
            # 部分 Qt 构建也认 QT_QPA_PLATFORM_PLUGIN_PATH
            os.environ.setdefault("QT_QPA_PLATFORM_PLUGIN_PATH", platforms)
            return


_set_qt_plugin_path()
