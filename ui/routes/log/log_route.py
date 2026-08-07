"""LogRoute —— 后台日志页（P5：自 MainWindow 迁出）。

职责：
- 日志文本区 + 「清空日志」按钮
- 界面日志与落盘日志（AppLogManager）的统一写入入口：
  log()=界面+落盘；debug()=仅落盘；clear_current()=界面+当前分区落盘清空
"""
from __future__ import annotations

from PySide6.QtWidgets import (
    QHBoxLayout, QPushButton, QTextEdit, QVBoxLayout, QWidget,
)

from core.log_manager import AppLogManager


class LogRoute(QWidget):
    """后台日志 Tab 内容区。"""

    def __init__(self, *, log_manager: AppLogManager, parent=None):
        super().__init__(parent)
        self._log_manager = log_manager
        self._build_widget()

    def _build_widget(self) -> None:
        self.setObjectName("tab_content_group")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        self.log_text = QTextEdit()
        self.log_text.setReadOnly(True)
        lay.addWidget(self.log_text, 1)

        btn_row = QHBoxLayout()
        btn_row.addStretch()
        self.btn_clear_log = QPushButton("清空日志")
        self.btn_clear_log.clicked.connect(self.clear_current)
        btn_row.addWidget(self.btn_clear_log)
        lay.addLayout(btn_row)

    # ── 对外写入接口 ──

    def append(self, text: str) -> None:
        """仅追加界面日志（不落盘）。"""
        self.log_text.append(text)

    def log(self, text: str) -> None:
        """追加界面日志并写入当前功能日志分区。"""
        self.log_text.append(text)
        try:
            self._log_manager.write(text)
        except Exception:
            pass

    def debug(self, text: str) -> None:
        """仅写后台日志文件（不进界面）。"""
        try:
            self._log_manager.write(text)
        except Exception:
            pass

    def clear_text(self) -> None:
        """仅清空界面日志文本。"""
        self.log_text.clear()

    def clear_current(self) -> None:
        """清空界面日志与当前功能分区的 current.log。"""
        self.log_text.clear()
        try:
            self._log_manager.clear_current()
        except Exception:
            pass

    def switch_feature(self, feature_id: str, clear_current: bool = True) -> None:
        """切换日志功能分区（委托 AppLogManager）。"""
        self._log_manager.switch_feature(feature_id, clear_current=clear_current)
