"""
PixelFlow - 图像处理工作台
入口文件
"""
import os
import sys
import traceback
from datetime import datetime
from pathlib import Path
from config import APP_NAME, ICON_PATH, LOGS_DIR


def _setup_qt_plugin_path():
    """
    打包环境下确保 Qt 能找到 platforms/qwindows.dll。
    开发环境有 site-packages 路径，一般不需要。
    """
    if not getattr(sys, "frozen", False):
        return
    base = Path(getattr(sys, "_MEIPASS", Path(sys.executable).parent))
    for rel in (
        Path("PySide6") / "plugins",
        Path("plugins"),
        Path("qt6") / "plugins",
    ):
        plugins = base / rel
        if (plugins / "platforms").is_dir():
            os.environ.setdefault("QT_PLUGIN_PATH", str(plugins))
            os.environ.setdefault(
                "QT_QPA_PLATFORM_PLUGIN_PATH", str(plugins / "platforms")
            )
            break


def _setup_crash_log():
    """将未捕获异常写入 logs/app，便于排查闪退。"""
    log_dir = LOGS_DIR / "app" / "history"
    current_path = LOGS_DIR / "app" / "current.log"

    def exception_hook(exc_type, exc_value, exc_tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        header = f"\n{'=' * 60}\n[{datetime.now():%Y-%m-%d %H:%M:%S}] 未捕获异常\n"
        try:
            log_dir.mkdir(parents=True, exist_ok=True)
            current_path.parent.mkdir(parents=True, exist_ok=True)
            for path in (current_path, log_dir / f"{datetime.now():%Y-%m-%d}.log"):
                with open(path, "a", encoding="utf-8") as f:
                    f.write(f"{header}{msg}\n")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = exception_hook


def main():
    _setup_crash_log()
    _setup_qt_plugin_path()
    from PySide6.QtWidgets import QApplication
    from PySide6.QtGui import QIcon
    from ui.main_window import MainWindow

    app = QApplication(sys.argv)
    app.setApplicationName(APP_NAME)

    if ICON_PATH.exists():
        app.setWindowIcon(QIcon(str(ICON_PATH)))

    window = MainWindow()
    window.show()
    sys.exit(app.exec())


if __name__ == "__main__":
    main()
