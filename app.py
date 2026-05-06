"""
PixelFlow - 图像处理工作台
入口文件
"""
import sys
import traceback
from datetime import datetime
from config import APP_NAME, ICON_PATH, LOGS_DIR


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
