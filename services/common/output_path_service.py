"""OutputPathService —— 输出路径应用服务（纯逻辑，无 Qt）。

面向 OutputSettingsRoute / ActionBarRoute 的薄门面：
校验与展示文案集中于此，路径语义本身由 contracts.OutputPolicy 承载
（四种模式矩阵、覆盖/保留结构的派生语义见 output_policy.py）。

与既有 MainWindow._resolve_output_dir / _resolve_file_overwrite /
_begin_process 的行为逐条对齐，不引入新语义。
"""
from __future__ import annotations

from typing import Iterable, Optional, Tuple

from services.contracts.output_policy import OutputPolicy, PathMode

# 与既有 _begin_process 的 mode_names 一致（展示用）
PATH_MODE_NAMES = ["桌面路径", "自定义路径", "原图路径(覆盖)", "原图路径(副本)"]


class OutputPathService:
    """输出路径校验 / 解析 / 展示（无状态）。"""

    def validate_for_start(self, policy: OutputPolicy) -> Optional[str]:
        """开始前校验，返回错误文案；None 表示通过。

        现状仅自定义路径模式要求目录非空（桌面模式空值回退桌面）。
        """
        if policy.path_mode is PathMode.CUSTOM and not (policy.root_dir or "").strip():
            return "请选择自定义输出目录"
        return None

    def resolve_output_dir(self, policy: OutputPolicy, first_src: str) -> str:
        """输出根目录。等价于 MainWindow._resolve_output_dir(file_list[0])。"""
        return policy.resolve_output_dir(first_src)

    def build_rel_path_map(
        self, policy: OutputPolicy, entries: Iterable[Tuple[str, Optional[str]]]
    ) -> dict:
        """保留目录结构时的 相对路径映射；未启用返回空 dict。"""
        return policy.build_rel_path_map(entries)

    @staticmethod
    def mode_display_name(path_mode) -> str:
        """路径模式展示名（与既有日志文案一致）。"""
        return PATH_MODE_NAMES[int(PathMode(path_mode))]
