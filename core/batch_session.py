"""
批处理会话：记录一次处理任务中每个文件的状态，支持续跑未完成 / 重试失败。

仅用于「逐文件」处理器；批量合并类（is_batch_processor）不支持按文件续跑。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Iterable


STATUS_PENDING = "pending"
STATUS_RUNNING = "running"
STATUS_SUCCESS = "success"
STATUS_FAILED = "failed"
STATUS_CANCELLED = "cancelled"  # 取消时正在处理、未得到完整结果的项


@dataclass
class FileJobState:
    path: str
    rel_path: str | None = None
    order: int = 0  # 原批次 1-based 序号（重命名续跑时保持一致）
    status: str = STATUS_PENDING
    error: str = ""
    output_path: str = ""

    @property
    def display_name(self) -> str:
        from pathlib import Path
        return Path(self.path).name


@dataclass
class BatchSession:
    """一次批处理任务的快照与进度。"""

    kind: str  # "image" | "file"
    processor_preset_id: str
    processor_name: str
    supports_resume: bool
    options: dict
    output_dir: str
    auto_subfolder: bool
    overwrite: bool  # 原图路径(覆盖原图) 模式
    file_overwrite: bool = False  # 桌面/自定义：覆盖同名输出文件
    keep_structure: bool = False
    path_mode_id: int = 0
    rel_path_map: dict = field(default_factory=dict)
    files: list[FileJobState] = field(default_factory=list)
    user_cancelled: bool = False

    def __post_init__(self):
        self._index: dict[str, FileJobState] = {f.path: f for f in self.files}

    @classmethod
    def create(
        cls,
        *,
        kind: str,
        processor_preset_id: str,
        processor_name: str,
        supports_resume: bool,
        options: dict,
        output_dir: str,
        auto_subfolder: bool,
        overwrite: bool,
        keep_structure: bool,
        path_mode_id: int,
        entries: list[tuple[str, str | None]],
        file_overwrite: bool | None = None,
    ) -> "BatchSession":
        jobs = [
            FileJobState(path=p, rel_path=rel or None, order=i + 1)
            for i, (p, rel) in enumerate(entries)
        ]
        # 未显式传入时：原图覆盖模式等同允许覆盖文件
        fo = overwrite if file_overwrite is None else bool(file_overwrite)
        return cls(
            kind=kind,
            processor_preset_id=processor_preset_id,
            processor_name=processor_name,
            supports_resume=supports_resume,
            options=dict(options or {}),
            output_dir=output_dir,
            auto_subfolder=auto_subfolder,
            overwrite=overwrite,
            file_overwrite=fo,
            keep_structure=keep_structure,
            path_mode_id=path_mode_id,
            rel_path_map={p: rel for p, rel in entries if rel},
            files=jobs,
        )

    def order_map(self) -> dict[str, int]:
        return {f.path: f.order for f in self.files if f.order > 0}

    def get(self, path: str) -> FileJobState | None:
        return self._index.get(path)

    def mark_running(self, path: str):
        job = self._index.get(path)
        # 不覆盖已有终态（成功/失败），避免取消竞态把完成项改坏
        if job and job.status in (STATUS_PENDING, STATUS_RUNNING, STATUS_CANCELLED):
            job.status = STATUS_RUNNING
            job.error = ""

    def mark_success(self, path: str, output_path: str = "", details: dict | None = None):
        job = self._index.get(path)
        if not job:
            return
        job.status = STATUS_SUCCESS
        job.error = ""
        job.output_path = output_path or ""

    def mark_failed(self, path: str, error: str = ""):
        job = self._index.get(path)
        if not job:
            return
        job.status = STATUS_FAILED
        job.error = (error or "").strip()
        job.output_path = ""

    def mark_cancelled_running(self):
        """取消时：把仍在 running 的项标为 cancelled，便于续跑时重做。"""
        self.user_cancelled = True
        for job in self.files:
            if job.status == STATUS_RUNNING:
                job.status = STATUS_CANCELLED
                job.error = "用户取消"

    def remove_paths(self, paths: Iterable[str]) -> int:
        """从会话移除指定路径（列表删除条目后同步；不再参与续跑）。

        与既有 main_window._on_files_removed 的逐条移除语义一致。
        返回实际移除条数。
        """
        dropped = set(paths)
        if not dropped:
            return 0
        before = len(self.files)
        self.files = [f for f in self.files if f.path not in dropped]
        for p in dropped:
            self._index.pop(p, None)
        return before - len(self.files)

    def reset_for_retry(self, paths: Iterable[str]):
        for p in paths:
            job = self._index.get(p)
            if not job:
                continue
            job.status = STATUS_PENDING
            job.error = ""
            job.output_path = ""

    def paths_with_status(self, *statuses: str) -> list[str]:
        want = set(statuses)
        return [f.path for f in self.files if f.status in want]

    def pending_paths(self) -> list[str]:
        """续跑：未跑完 + 取消中断的当前张。"""
        return self.paths_with_status(STATUS_PENDING, STATUS_CANCELLED)

    def failed_paths(self) -> list[str]:
        return self.paths_with_status(STATUS_FAILED)

    def count(self, *statuses: str) -> int:
        if not statuses:
            return len(self.files)
        want = set(statuses)
        return sum(1 for f in self.files if f.status in want)

    def summary(self) -> dict[str, int]:
        return {
            "total": len(self.files),
            "success": self.count(STATUS_SUCCESS),
            "failed": self.count(STATUS_FAILED),
            "pending": self.count(STATUS_PENDING),
            "cancelled": self.count(STATUS_CANCELLED),
            "running": self.count(STATUS_RUNNING),
        }

    def can_continue(self) -> bool:
        return self.supports_resume and bool(self.pending_paths())

    def can_retry_failed(self) -> bool:
        return self.supports_resume and bool(self.failed_paths())
