"""RunRequest —— 一次批处理请求的不可变快照。

由 Route 收集原始状态 → FeatureService 校验规范化 → 构建本对象，
BatchOrchestrator 只消费 RunRequest，不主动读取任何页面状态。

兼容期约定：options 仍为 dict，但 `_output_format` / `_overwrite` /
`_rel_path_map` 等跨层控制字段应逐步迁入 DTO，不再新增魔法键。
调用方构建 RunRequest 时必须传入 options 的副本（快照语义）。
"""
from __future__ import annotations

import uuid
from dataclasses import dataclass, field
from typing import Mapping, Tuple

from .output_policy import OutputPolicy


def new_job_id() -> str:
    """生成全局唯一任务 id。每次批处理（含续跑/重试）都应使用新 job_id。"""
    return uuid.uuid4().hex


@dataclass(frozen=True)
class RunRequest:
    """批处理请求快照。

    字段说明：
    - job_id     任务唯一 id；所有 JobEvent 携带它，用于迟到事件隔离
    - feature_id 功能 id（= FeatureDescriptor.id = preset_id）
    - kind       Worker 分派类型："image"（ProcessWorker）/ "file"（FileProcessWorker）
    - entries    输入快照 tuple[(绝对路径, 相对路径|None), ...]，顺序即默认序号
    - options    规范化后的功能参数（dict 副本）
    - output     输出策略快照
    - order_map  续跑/重试时保持原批次 1-based 序号（重命名等依赖）；
                 空映射表示按 entries 顺序取 i+1
    - resume     True 表示续跑/重试（沿用既有会话快照），False 表示全新整批
    """

    job_id: str
    feature_id: str
    kind: str
    entries: Tuple[Tuple[str, str | None], ...]
    options: dict
    output: OutputPolicy
    order_map: Mapping[str, int] = field(default_factory=dict)
    resume: bool = False

    def __post_init__(self):
        if not self.job_id:
            raise ValueError("RunRequest.job_id 不能为空")
        if not self.feature_id:
            raise ValueError("RunRequest.feature_id 不能为空")
        if self.kind not in ("image", "file"):
            raise ValueError(f"RunRequest.kind 非法: {self.kind!r}")
        if not isinstance(self.entries, tuple):
            # 统一转为 tuple，保证快照不可变语义
            object.__setattr__(self, "entries", tuple(self.entries))

    def file_list(self) -> list:
        """输入文件路径列表（保持快照顺序）。"""
        return [p for p, _ in self.entries]

    def order_for(self, path: str, fallback_index: int) -> int:
        """文件在批次中的 1-based 序号。

        与 Worker 的 `int(file_index_map.get(fpath, i + 1) or (i + 1))` 语义一致：
        order_map 命中则沿用原序号，否则按当前位置 fallback_index。
        """
        order = self.order_map.get(path) if self.order_map else None
        return int(order) if order else fallback_index
