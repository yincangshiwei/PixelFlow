"""缩略图后台加载线程。

约定（见 TECHNICAL.md §1 Qt 线程与生命周期约定）：
- 任务以 (entry_id, path) 形式提交；结果携带 entry_id 回传
- 投影层按 entry_id 校验，条目已删除时的迟到结果被忽略
"""
from PySide6.QtCore import Qt, QSize, QThread, Signal
from PySide6.QtGui import QImage, QPixmap, QIcon

#: 列表缩略图统一尺寸 48×48
THUMB_SIZE = QSize(48, 48)


class ThumbnailLoader(QThread):
    """后台加载缩略图"""

    loaded = Signal(str, str, QIcon)  # entry_id, path, icon

    def __init__(self, tasks: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self._tasks = list(tasks)

    def run(self):
        for entry_id, p in self._tasks:
            try:
                img = QImage(p)
                if not img.isNull():
                    scaled = img.scaled(
                        THUMB_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                    pix = QPixmap.fromImage(scaled)
                    self.loaded.emit(entry_id, p, QIcon(pix))
            except Exception:
                pass
