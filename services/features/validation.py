"""功能参数校验结果（普通数据，无 UI 依赖）。"""
from __future__ import annotations

from dataclasses import dataclass, field
from typing import Any, Optional


@dataclass(frozen=True)
class ValidationResult:
    """validate_and_normalize 的返回值。"""

    ok: bool
    value: Optional[dict[str, Any]] = None
    errors: tuple[str, ...] = field(default_factory=tuple)

    @classmethod
    def success(cls, value: dict[str, Any]) -> "ValidationResult":
        return cls(ok=True, value=dict(value), errors=())

    @classmethod
    def failure(cls, *errors: str) -> "ValidationResult":
        msgs = tuple(str(e).strip() for e in errors if str(e).strip())
        if not msgs:
            msgs = ("参数校验失败",)
        return cls(ok=False, value=None, errors=msgs)
