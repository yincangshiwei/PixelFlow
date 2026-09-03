"""services.features —— 功能级 Service（不依赖 Route / QWidget）。

六功能均走独立 Service（transparent / basic / upscale / img2doc / overlay / metadata），
统一注册入口为 catalog.build_feature_registry（FeatureDescriptor）。
"""
from .validation import ValidationResult
from .registry import FeatureRegistry

__all__ = [
    "ValidationResult",
    "FeatureRegistry",
]
