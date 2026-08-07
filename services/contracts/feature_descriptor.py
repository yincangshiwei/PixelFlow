"""FeatureDescriptor —— 功能统一描述符。

壳、处理 Tab、预设与编排均读取同一描述符，不维护多份功能顺序。
描述符本身只承载元数据与工厂，不 import 任何 QWidget。
"""
from __future__ import annotations

from dataclasses import dataclass, field
from enum import Enum
from typing import Callable, Optional


class InputKind(Enum):
    """功能输入/执行形态，决定 Worker 分派与续跑能力。"""

    IMAGE = "image"
    # 逐张 process(img, options)（ProcessWorker）

    FILE = "file"
    # 逐文件 process_file(path, out_dir, options, index)（FileProcessWorker）

    BATCH_MERGED = "batch_merged"
    # process_batch(file_list, options, output_dir) 合并执行；
    # 与现状一致：is_batch_processor 功能不支持按文件续跑/重试


@dataclass(frozen=True)
class FeatureDescriptor:
    """一个功能的完整描述（id 即 preset_id，全局唯一）。"""

    id: str
    name: str
    icon: str = ""
    description: str = ""
    input_kind: InputKind = InputKind.IMAGE

    # 工厂：均为可空 callable，按需注入；描述符不持有实例
    processor_factory: Optional[Callable[[], object]] = None
    route_factory: Optional[Callable] = None      # P2 起：FeatureRoute 工厂
    service_factory: Optional[Callable] = None    # P2 起：FeatureService 工厂

    # 能力声明
    supports_resume: bool = True            # 逐文件处理器才可为 True
    supports_selected_load: bool = False    # 选中图片回读（如元数据编辑）
    capabilities: frozenset = field(default_factory=frozenset)
    preset_schema_version: int = 1

    def __post_init__(self):
        if not self.id or not str(self.id).strip():
            raise ValueError("FeatureDescriptor.id 不能为空")
        if not self.name or not str(self.name).strip():
            raise ValueError("FeatureDescriptor.name 不能为空")
        # 现状语义：批量合并功能（is_batch_processor）不支持按文件续跑
        if self.input_kind is InputKind.BATCH_MERGED and self.supports_resume:
            raise ValueError(
                f"功能 {self.id}: BATCH_MERGED 输入形态不支持按文件续跑，"
                "supports_resume 必须为 False"
            )

    def create_processor(self):
        """通过工厂创建处理器实例（image/file 功能分别返回
        BaseProcessor / BaseFileProcessor 子类实例）。"""
        if self.processor_factory is None:
            raise RuntimeError(f"功能 {self.id} 未注册 processor_factory")
        return self.processor_factory()
