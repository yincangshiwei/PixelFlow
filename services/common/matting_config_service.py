"""MattingConfigService —— 抠图模型配置门面（P5）。

对 ``core.matting``（model_manager / model_registry / hardware / inference）
的薄封装：配置中心「抠图模型」Route 经此读写模型设置，不直接散落调用
各 core 模块。纯委托，不含 UI、不 import PySide6。
"""
from __future__ import annotations

from pathlib import Path
from typing import Callable, Optional

from core.matting.hardware import HardwareInfo, detect_hardware, evaluate_model
from core.matting.inference import clear_model_cache
from core.matting.model_manager import get_matting_manager
from core.matting.model_registry import get_model_info, list_models


def format_gb(value: float) -> str:
    """格式化 GB 数值：8.0 → 8；4.5 → 4.5；非法 → ?"""
    try:
        v = float(value)
    except (TypeError, ValueError):
        return "?"
    if abs(v - round(v)) < 1e-6:
        return str(int(round(v)))
    return f"{v:.1f}"


class MattingConfigService:
    """抠图模型配置（默认模型 / 设备偏好 / 权重 / 下载）薄封装。"""

    def __init__(self, manager=None):
        self._mgr = manager if manager is not None else get_matting_manager()

    @property
    def manager(self):
        return self._mgr

    # ── 模型目录 ──

    def list_models(self):
        return list_models()

    def get_model_info(self, model_id: str):
        return get_model_info(model_id)

    # ── 默认模型 / 推理设备 ──

    def get_default_model_id(self) -> str:
        return self._mgr.get_default_model_id()

    def set_default_model_id(self, model_id: str) -> None:
        self._mgr.set_default_model_id(model_id)

    def get_device_preference(self) -> str:
        return self._mgr.get_device_preference()

    def set_device_preference(self, device: str) -> None:
        self._mgr.set_device_preference(device)

    # ── 权重状态 ──

    def is_ready(self, model_id: str) -> bool:
        return self._mgr.is_ready(model_id)

    def status_text(self, model_id: str) -> str:
        return self._mgr.status_text(model_id)

    def is_downloading(self, model_id: str) -> bool:
        return self._mgr.is_downloading(model_id)

    def default_local_dir(self, model_id: str) -> Path:
        return self._mgr.default_local_dir(model_id)

    def get_custom_path(self, model_id: str) -> str:
        return self._mgr.get_custom_path(model_id)

    def set_custom_path(self, model_id: str, path: str) -> None:
        self._mgr.set_custom_path(model_id, path)

    def open_model_folder(self, model_id: str) -> Path:
        return self._mgr.open_model_folder(model_id)

    def download_async(
        self,
        model_id: str,
        *,
        progress: Optional[Callable] = None,
        finished: Optional[Callable] = None,
    ) -> None:
        self._mgr.download_async(model_id, progress=progress, finished=finished)

    # ── 推理缓存 ──

    def clear_model_cache(self) -> None:
        clear_model_cache()

    # ── 硬件检测 ──

    def detect_hardware(self) -> HardwareInfo:
        return detect_hardware()

    def evaluate_model(self, model_id: str, hw: HardwareInfo):
        return evaluate_model(model_id, hw)


_default_service: Optional[MattingConfigService] = None


def get_matting_config_service() -> MattingConfigService:
    """进程内单例门面。"""
    global _default_service
    if _default_service is None:
        _default_service = MattingConfigService()
    return _default_service


__all__ = [
    "MattingConfigService",
    "get_matting_config_service",
    "format_gb",
]
