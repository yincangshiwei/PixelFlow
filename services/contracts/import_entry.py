"""ImportEntry —— 文件列表的唯一业务真相 DTO。

现状（重构前）列表状态散落在 QListWidgetItem 的 data role 中：
- ROLE_PATH      完整路径
- ROLE_REL_PATH  相对导入根目录的路径（保留目录结构用）

P1 起由 ImportCollection 以 ImportEntry 为真相，控件只是它的投影。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from enum import Enum


class ImportSource(Enum):
    """条目来源类型（用于展示与诊断，不参与去重）。"""

    LOCAL_FILE = "local_file"            # 单独添加的图片文件
    LOCAL_FOLDER = "local_folder"        # 添加文件夹递归导入
    DRAG_DROP = "drag_drop"              # 拖放导入
    PASTE_PATH = "paste_path"            # 粘贴文件/文件夹路径
    PASTE_BITMAP = "paste_bitmap"        # 粘贴剪贴板位图（落盘 PNG）
    HTML_EXTRACT = "html_extract"        # HTML 容器抽图
    DOCX_EXTRACT = "docx_extract"        # DOCX 容器抽图
    PDF_EXTRACT = "pdf_extract"          # PDF 容器抽图
    REMOTE_DOWNLOAD = "remote_download"  # HTML 远程图片下载落盘


def new_entry_id() -> str:
    """生成全局唯一条目 id（用于缩略图迟到结果校验等）。"""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class ImportEntry:
    """单个导入条目（不可变）。

    字段约定：
    - path          绝对路径（导入时即规范化），列表内按路径去重的依据
    - relative_path 相对导入根目录路径（"sub/a.png"，分隔符统一 "/"）；
                    单独添加的文件为 None（输出时平铺）
    - source        来源类型
    - content_hash  可选内容 sha1（粘贴/抽图等内容寻址场景使用）
    - entry_id      唯一 id；条目移除后，其异步结果（缩略图等）按此作废
    """

    path: str
    relative_path: str | None = None
    source: ImportSource = ImportSource.LOCAL_FILE
    content_hash: str = ""
    entry_id: str = field(default_factory=new_entry_id)

    def __post_init__(self):
        if not self.path:
            raise ValueError("ImportEntry.path 不能为空")
        if self.relative_path is not None:
            rel = self.relative_path.replace("\\", "/").strip("/")
            if rel != self.relative_path:
                object.__setattr__(self, "relative_path", rel or None)

    @property
    def dedupe_key(self) -> str:
        """去重键：绝对路径字符串。

        与既有列表去重语义一致（_insert_files 按完整路径判重）。
        URL/内容级去重发生在导入协调层（物化前），不在此处重复实现。
        """
        return self.path

    @property
    def file_name(self) -> str:
        idx = max(self.path.rfind("/"), self.path.rfind("\\"))
        return self.path[idx + 1:] if idx >= 0 else self.path
