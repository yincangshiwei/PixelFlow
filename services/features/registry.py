"""FeatureRegistry —— 功能描述符统一注册表（纯逻辑，无 Qt）。

壳、处理 Tab、预设与编排均读取同一注册表；注册只能有一个权威入口，
禁止在多处各自遍历处理器再拼菜单。
"""
from __future__ import annotations

from typing import Iterable, Iterator, Optional

from services.contracts.feature_descriptor import FeatureDescriptor


class FeatureRegistry:
    """有序、id 唯一的 FeatureDescriptor 容器。"""

    def __init__(self) -> None:
        self._items: list[FeatureDescriptor] = []
        self._by_id: dict[str, FeatureDescriptor] = {}

    def register(self, descriptor: FeatureDescriptor) -> FeatureDescriptor:
        if not isinstance(descriptor, FeatureDescriptor):
            raise TypeError("descriptor 必须是 FeatureDescriptor")
        if descriptor.id in self._by_id:
            raise ValueError(f"功能 id 重复注册: {descriptor.id}")
        self._items.append(descriptor)
        self._by_id[descriptor.id] = descriptor
        return descriptor

    def get(self, feature_id: str) -> Optional[FeatureDescriptor]:
        return self._by_id.get(feature_id)

    def require(self, feature_id: str) -> FeatureDescriptor:
        desc = self.get(feature_id)
        if desc is None:
            raise KeyError(f"未注册的功能 id: {feature_id}")
        return desc

    def all(self) -> list[FeatureDescriptor]:
        return list(self._items)

    def ids(self) -> list[str]:
        return [d.id for d in self._items]

    def __len__(self) -> int:
        return len(self._items)

    def __iter__(self) -> Iterator[FeatureDescriptor]:
        return iter(self._items)

    def __contains__(self, feature_id: object) -> bool:
        return isinstance(feature_id, str) and feature_id in self._by_id

    def extend(self, descriptors: Iterable[FeatureDescriptor]) -> None:
        for d in descriptors:
            self.register(d)
