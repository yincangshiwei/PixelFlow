"""缩略图后台加载线程。

约定（见 TECHNICAL.md §1 Qt 线程与生命周期约定）：
- 任务以 (entry_id, path) 形式提交；结果携带 entry_id 回传
- 投影层按 entry_id 校验，条目已删除时的迟到结果被忽略
- 读图统一走 core.image_io（含 RAW→RGB），再转 QImage
"""
from PySide6.QtCore import Qt, QSize, QThread, Signal
from PySide6.QtGui import QImage, QPixmap, QIcon

from core.image_io import load_image_for_preview

#: 列表缩略图统一尺寸 48×48
THUMB_SIZE = QSize(48, 48)


def _pil_to_qimage(pil_img) -> QImage:
    """PIL Image → QImage（拷贝像素，不依赖原 buffer 生命周期）。"""
    if pil_img.mode not in ("RGB", "RGBA"):
        pil_img = pil_img.convert("RGBA" if "A" in pil_img.getbands() else "RGB")
    w, h = pil_img.size
    if pil_img.mode == "RGBA":
        data = pil_img.tobytes("raw", "RGBA")
        qimg = QImage(data, w, h, w * 4, QImage.Format_RGBA8888)
    else:
        data = pil_img.tobytes("raw", "RGB")
        qimg = QImage(data, w, h, w * 3, QImage.Format_RGB888)
    return qimg.copy()


class ThumbnailLoader(QThread):
    """后台加载缩略图"""

    loaded = Signal(str, str, QIcon)  # entry_id, path, icon

    def __init__(self, tasks: list[tuple[str, str]], parent=None):
        super().__init__(parent)
        self._tasks = list(tasks)

    def run(self):
        for entry_id, p in self._tasks:
            try:
                # 先试 Qt 直读（常规格式更快）；失败再走统一加载（含 RAW）
                img = QImage(p)
                if img.isNull():
                    pil = load_image_for_preview(p)
                    try:
                        img = _pil_to_qimage(pil)
                    finally:
                        pil.close()
                if not img.isNull():
                    scaled = img.scaled(
                        THUMB_SIZE, Qt.KeepAspectRatio, Qt.SmoothTransformation
                    )
                    pix = QPixmap.fromImage(scaled)
                    self.loaded.emit(entry_id, p, QIcon(pix))
            except Exception:
                pass
