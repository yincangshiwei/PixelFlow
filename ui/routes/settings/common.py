"""ui.routes.settings 公共辅助（P5）。

滚动容器包装、打开链接 / 目录等与具体子页无关的小工具。
"""
from __future__ import annotations

import subprocess
import sys
from pathlib import Path

from PySide6.QtWidgets import (
    QFileDialog, QFrame, QMessageBox, QScrollArea, QVBoxLayout, QWidget,
)


def get_desktop_path() -> str:
    return str(Path.home() / "Desktop")


def wrap_scroll(body: QWidget) -> QWidget:
    """把内容页包进透明滚动区。"""
    page = QWidget()
    outer = QVBoxLayout(page)
    outer.setContentsMargins(0, 0, 0, 0)
    scroll = QScrollArea()
    scroll.setWidgetResizable(True)
    scroll.setFrameShape(QFrame.NoFrame)
    scroll.setStyleSheet("background: transparent;")
    scroll.setWidget(body)
    outer.addWidget(scroll)
    return page


def open_url(parent: QWidget | None, url: str) -> None:
    """打开外部链接（浏览器），失败时提示手动打开。"""
    try:
        from PySide6.QtCore import QUrl
        from PySide6.QtGui import QDesktopServices
        QDesktopServices.openUrl(QUrl(url))
    except Exception:
        try:
            if sys.platform == "win32":
                os_start = getattr(__import__("os"), "startfile", None)
                if os_start:
                    os_start(url)
                else:
                    subprocess.Popen(["cmd", "/c", "start", url], shell=True)
            else:
                subprocess.Popen(["xdg-open", url])
        except Exception as e:
            QMessageBox.information(parent, "打开链接", f"请手动打开:\n{url}\n\n{e}")


def open_path(parent: QWidget | None, d: Path) -> None:
    """在系统文件管理器中打开目录。"""
    try:
        if sys.platform == "win32":
            subprocess.Popen(["explorer", str(d)])
        elif sys.platform == "darwin":
            subprocess.Popen(["open", str(d)])
        else:
            subprocess.Popen(["xdg-open", str(d)])
    except Exception as e:
        QMessageBox.warning(parent, "打开失败", str(e))


def browse_python_exe(parent: QWidget | None) -> str:
    """浏览选择 python.exe（默认桌面路径）。"""
    path, _ = QFileDialog.getOpenFileName(
        parent,
        "选择 python.exe",
        get_desktop_path(),
        "Python (python.exe);;所有文件 (*.*)",
    )
    return path
