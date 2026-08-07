"""支持拖放的文件列表控件（缩略图列表的载体）。

P1 起拖放结果通过 urls_dropped 信号对外发布，由 FileListRoute 接入导入流程，
不再直接回调窗口私有方法，避免双重入口。
"""
from pathlib import Path

from PySide6.QtCore import Signal
from PySide6.QtWidgets import QListWidget, QAbstractItemView
from PySide6.QtGui import QDragEnterEvent, QDropEvent

from services.common.importing import IMAGE_EXTS


class DropListWidget(QListWidget):
    """支持拖放 / 粘贴、缩略图显示的文件列表"""

    urls_dropped = Signal(list)  # 拖入的 QUrl 列表（导入唯一入口）

    def __init__(self, parent=None, thumb_size=None):
        super().__init__(parent)
        # DropOnly：接受外部拖入，禁止列表内拖拽重排；NoDragDrop 会关闭 acceptDrops
        self.setDragDropMode(QAbstractItemView.DropOnly)
        self.setAcceptDrops(True)
        self.setSelectionMode(QAbstractItemView.ExtendedSelection)
        if thumb_size is not None:
            self.setIconSize(thumb_size)
        self.setSpacing(2)
        self.setToolTip(
            "支持拖放或 Ctrl+V 粘贴：图片 / 文件夹；"
            "HTML / DOCX / PDF 自动提取其中图片"
        )

    def dragEnterEvent(self, event: QDragEnterEvent):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dragMoveEvent(self, event):
        if event.mimeData().hasUrls():
            event.acceptProposedAction()
        else:
            event.ignore()

    def dropEvent(self, event: QDropEvent):
        self.urls_dropped.emit(list(event.mimeData().urls()))
        event.acceptProposedAction()

    def _is_image(self, path: str) -> bool:
        return Path(path).suffix.lower() in IMAGE_EXTS
