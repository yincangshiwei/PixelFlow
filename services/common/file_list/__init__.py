"""services.common.file_list —— 文件列表状态模型。

分层约定（见 TECHNICAL.md §1 分层与依赖规则）：
- ImportCollection 以 ImportEntry 为唯一业务真相（路径、相对路径、任务状态），
  不依赖任何 QWidget；QListWidget 只是它的投影。
- 控件侧投影（缩略图、状态前缀/颜色、预览）在 ui/routes/file_list。
"""
from .collection import (
    EntryState,
    ImportCollection,
    compute_display_name,
)

__all__ = [
    "EntryState",
    "ImportCollection",
    "compute_display_name",
]
