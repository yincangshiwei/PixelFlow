"""功能目录 —— 统一构建 FeatureRegistry（权威注册入口，纯逻辑无 Qt/ui）。

顺序与历史菜单一致（高清放大插在基础处理之后）：
  transparent_image → basic_process → upscale → img2doc → image_overlay → metadata_edit

六功能均走独立 FeatureService；route_factory 由 UI 层注入。
"""
from __future__ import annotations

from dataclasses import replace
from typing import Any, Callable, Optional

from .basic_service import BasicService, FEATURE_ID as BASIC_ID
from .metadata_service import MetadataService, FEATURE_ID as METADATA_ID
from .overlay_service import OverlayService, FEATURE_ID as OVERLAY_ID
from .img2doc_service import Img2DocService, FEATURE_ID as IMG2DOC_ID
from .transparent_service import TransparentService, FEATURE_ID as TRANSPARENT_ID
from .upscale_service import UpscaleService, FEATURE_ID as UPSCALE_ID
from .registry import FeatureRegistry

# 完整菜单顺序 —— 测试与菜单基线
EXPECTED_FEATURE_ORDER = [
    TRANSPARENT_ID,
    BASIC_ID,
    UPSCALE_ID,
    IMG2DOC_ID,
    OVERLAY_ID,
    METADATA_ID,
]

# feature_id → Service 类（按菜单顺序构建）
_SERVICE_TYPES: list[tuple[str, type]] = [
    (TRANSPARENT_ID, TransparentService),
    (BASIC_ID, BasicService),
    (UPSCALE_ID, UpscaleService),
    (IMG2DOC_ID, Img2DocService),
    (OVERLAY_ID, OverlayService),
    (METADATA_ID, MetadataService),
]


def build_feature_registry(
    *,
    route_factories: Optional[dict[str, Callable]] = None,
) -> tuple[FeatureRegistry, dict[str, Any]]:
    """构建注册表与 feature_id → FeatureService 映射。

    :param route_factories: feature_id → Route 工厂（UI 注入；测试可省略）
    :return: (registry, services_by_id)
    """
    factories = dict(route_factories or {})

    registry = FeatureRegistry()
    services: dict[str, Any] = {}

    for fid, svc_cls in _SERVICE_TYPES:
        svc = svc_cls()
        desc = svc.descriptor
        rf = factories.get(fid)
        if rf is not None:
            desc = replace(desc, route_factory=rf)
        registry.register(desc)
        services[fid] = svc

    return registry, services


_default_registry: Optional[FeatureRegistry] = None
_default_services: Optional[dict[str, Any]] = None


def get_default_catalog(
    *,
    route_factories: Optional[dict[str, Callable]] = None,
    force_reload: bool = False,
) -> tuple[FeatureRegistry, dict[str, Any]]:
    """进程内单例目录（菜单 / 预设权威入口）。"""
    global _default_registry, _default_services
    if force_reload or _default_registry is None or _default_services is None:
        _default_registry, _default_services = build_feature_registry(
            route_factories=route_factories,
        )
    return _default_registry, _default_services


def reset_default_catalog() -> None:
    """测试用：清空单例。"""
    global _default_registry, _default_services
    _default_registry = None
    _default_services = None
