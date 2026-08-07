"""OutputPolicy —— 输出路径策略 DTO。

以现有 MainWindow._resolve_output_dir / _resolve_file_overwrite /
_begin_process 中 auto_folder、keep_structure 的语义为准，逐条固化：

- 四种路径模式：桌面 / 自定义 / 原图路径(覆盖原图) / 原图路径(另存副本)
- is_src_overwrite 仅表示「原图路径(覆盖原图)」，与「覆盖同名文件」无关
- 原图覆盖模式：file_overwrite 恒 True，不建自动子文件夹，不保留目录结构
- 原图副本模式：file_overwrite 恒 False（同名自动 _1/_2…，不覆盖原文件）
- 桌面/自定义：自动子文件夹 PixelFlow_output、保留目录结构、覆盖同名可配
"""
from __future__ import annotations

from dataclasses import dataclass
from enum import IntEnum
from pathlib import Path
from typing import Iterable, Tuple

AUTO_SUBFOLDER_NAME = "PixelFlow_output"


class PathMode(IntEnum):
    """输出路径模式（取值与现有 path_group.checkedId() 一致）。"""

    DESKTOP = 0        # 桌面路径
    CUSTOM = 1         # 自定义路径
    SRC_OVERWRITE = 2  # 原图路径 (覆盖原图)
    SRC_COPY = 3       # 原图路径 (另存副本)


def default_desktop_path() -> str:
    """桌面路径（与 _get_desktop_path 一致）。"""
    return str(Path.home() / "Desktop")


@dataclass(frozen=True)
class OutputPolicy:
    """一次批处理任务的输出策略快照（不可变）。

    root_dir 仅对 DESKTOP / CUSTOM 有意义；为空时按现状回退桌面路径。
    """

    path_mode: PathMode = PathMode.DESKTOP
    root_dir: str = ""
    auto_subfolder: bool = True   # 「在该路径下自动创建文件夹保存」勾选值
    keep_structure: bool = True   # 「保留目录结构」勾选值
    file_overwrite: bool = False  # 「覆盖同名文件」勾选值

    def __post_init__(self):
        # frozen dataclass：仅允许在初始化期规范化取值
        object.__setattr__(self, "path_mode", PathMode(self.path_mode))

    # ── 派生语义（与现有行为一一对应）──

    @property
    def src_overwrite(self) -> bool:
        """是否「原图路径(覆盖原图)」模式。"""
        return self.path_mode is PathMode.SRC_OVERWRITE

    def resolve_output_dir(self, src_path: str = "") -> str:
        """输出根目录。等价于 MainWindow._resolve_output_dir。

        原图路径模式取首个源文件所在目录，故需要 src_path。
        """
        if self.path_mode in (PathMode.DESKTOP, PathMode.CUSTOM):
            d = (self.root_dir or "").strip()
            return d if d else default_desktop_path()
        if not src_path:
            raise ValueError("原图路径模式需要提供 src_path（首个源文件路径）")
        return str(Path(src_path).parent)

    def resolve_file_overwrite(self) -> bool:
        """是否允许覆盖同名输出文件。等价于 MainWindow._resolve_file_overwrite。"""
        if self.src_overwrite:
            return True
        if self.path_mode in (PathMode.DESKTOP, PathMode.CUSTOM):
            return bool(self.file_overwrite)
        return False

    def effective_auto_subfolder(self) -> bool:
        """实际是否创建 PixelFlow_output 子文件夹。

        等价于 `chk_auto_folder.isChecked() and not is_src_overwrite`。
        """
        return bool(self.auto_subfolder) and not self.src_overwrite

    def effective_keep_structure(self) -> bool:
        """实际是否保留目录结构。

        等价于 `chk_keep_structure.isChecked() and not is_src_overwrite
        and mode_id in (0, 1)`。
        """
        return (
            bool(self.keep_structure)
            and not self.src_overwrite
            and self.path_mode in (PathMode.DESKTOP, PathMode.CUSTOM)
        )

    def build_rel_path_map(
        self, entries: Iterable[Tuple[str, str | None]]
    ) -> dict:
        """由 (path, rel_path) 输入快照构建 rel_path_map。

        未启用保留结构时返回空 dict（与 _begin_process 中
        `rel_map = {...} if keep_structure else {}` 一致）。
        """
        if not self.effective_keep_structure():
            return {}
        return {p: rel for p, rel in entries if rel}

    def plan_output_root(self, output_dir: str) -> Path:
        """Worker 实际写入的根目录（叠加 auto_subfolder 语义）。

        对应 ProcessWorker/FileProcessWorker 中
        `if self.auto_subfolder: out_dir = out_dir / "PixelFlow_output"`。
        """
        out = Path(output_dir)
        if self.effective_auto_subfolder():
            out = out / AUTO_SUBFOLDER_NAME
        return out
