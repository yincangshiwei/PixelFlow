"""
抠图（背景移除）模型管理与推理
"""
from core.matting.model_registry import (
    MATTING_MODELS,
    get_model_info,
    list_models,
)
from core.matting.model_manager import MattingModelManager, get_matting_manager
from core.matting.hardware import detect_hardware, HardwareInfo
from core.matting.inference import (
    remove_background,
    MattingError,
    shutdown_matting_workers,
    clear_model_cache,
)

__all__ = [
    "MATTING_MODELS",
    "get_model_info",
    "list_models",
    "MattingModelManager",
    "get_matting_manager",
    "detect_hardware",
    "HardwareInfo",
    "remove_background",
    "MattingError",
    "shutdown_matting_workers",
    "clear_model_cache",
]
