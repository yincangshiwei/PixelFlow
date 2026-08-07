"""ChangelogRoute —— 版本日志页（P5：自 MainWindow._load_changelog 迁出）。

只读渲染 resources/CHANGELOG.md。
"""
from __future__ import annotations

from PySide6.QtWidgets import QTextBrowser, QVBoxLayout, QWidget

from config import RESOURCES_DIR


class ChangelogRoute(QWidget):
    """版本日志 Tab 内容区。"""

    def __init__(self, parent=None):
        super().__init__(parent)
        self.setObjectName("tab_content_group")
        lay = QVBoxLayout(self)
        lay.setContentsMargins(12, 12, 12, 12)
        lay.setSpacing(8)

        self.changelog_browser = QTextBrowser()
        self.changelog_browser.setOpenExternalLinks(False)
        self.changelog_browser.setReadOnly(True)
        lay.addWidget(self.changelog_browser, 1)

        self.reload()

    def reload(self) -> None:
        """读取 resources/CHANGELOG.md 并渲染。"""
        changelog_path = RESOURCES_DIR / "CHANGELOG.md"
        try:
            text = changelog_path.read_text(encoding="utf-8")
        except Exception:
            text = "_版本日志文件未找到_"
        self.changelog_browser.setMarkdown(text)
