"""
PixelFlow - 图像处理工作台
入口文件
"""
import sys
import traceback
from pathlib import Path
from config import APP_NAME, DATA_DIR, ICON_PATH


def _setup_crash_log():
    """打包后将未捕获异常写入 crash.log"""
    if not getattr(sys, 'frozen', False):
        return

    log_path = DATA_DIR / "crash.log"

    def exception_hook(exc_type, exc_value, exc_tb):
        msg = "".join(traceback.format_exception(exc_type, exc_value, exc_tb))
        try:
            with open(log_path, "a", encoding="utf-8") as f:
                f.write(f"\n{'=' * 60}\n{msg}\n")
        except Exception:
            pass
        sys.__excepthook__(exc_type, exc_value, exc_tb)

    sys.excepthook = exception_hook


def main():
    _setup_crash_log()
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
