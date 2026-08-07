"""OverlayService —— 图片叠加功能 Service（无 QWidget / Route 依赖）。"""
from __future__ import annotations

from typing import Any, Optional

from services.contracts.feature_descriptor import FeatureDescriptor, InputKind
from services.contracts.output_policy import OutputPolicy
from core.processors.overlay_processor import default_overlay_options
from .validation import ValidationResult

FEATURE_ID = "image_overlay"
FEATURE_NAME = "图片叠加"
FEATURE_ICON = "🎨"
FEATURE_DESCRIPTION = "在图片上叠加文本和图片元素，支持Excel数据、图片名称等数据源"

_VALID_FORMATS = frozenset({"png", "webp", "jpg", "jpeg"})
_VALID_SOURCES = frozenset({"fixed", "excel", "filename"})

__all__ = [
    "FEATURE_ID", "FEATURE_NAME", "FEATURE_ICON", "FEATURE_DESCRIPTION",
    "OverlayService", "default_overlay_options",
]


class OverlayService:
    feature_id = FEATURE_ID

    def __init__(self) -> None:
        self._descriptor: Optional[FeatureDescriptor] = None

    @property
    def descriptor(self) -> FeatureDescriptor:
        if self._descriptor is None:
            self._descriptor = FeatureDescriptor(
                id=FEATURE_ID,
                name=FEATURE_NAME,
                icon=FEATURE_ICON,
                description=FEATURE_DESCRIPTION,
                input_kind=InputKind.IMAGE,
                processor_factory=self._make_processor,
                service_factory=OverlayService,
                supports_resume=True,
                supports_selected_load=False,
                preset_schema_version=1,
            )
        return self._descriptor

    @staticmethod
    def _make_processor():
        from core.processors.overlay_processor import OverlayProcessor
        return OverlayProcessor()

    def create_processor(self):
        return self._make_processor()

    def default_state(self) -> dict[str, Any]:
        return default_overlay_options()

    def normalize_preset(self, data: dict | None) -> dict[str, Any]:
        result = self.validate_and_normalize(dict(data or {}), strict=False)
        return result.value if result.ok and result.value is not None else self.default_state()

    def validate_and_normalize(
        self, raw_state: dict | None, *, strict: bool = True
    ) -> ValidationResult:
        if raw_state is None:
            if strict:
                return ValidationResult.failure("参数不能为空")
            raw_state = {}
        if not isinstance(raw_state, dict):
            return ValidationResult.failure("参数必须是字典")

        base = self.default_state()
        errors: list[str] = []
        fmt = str(raw_state.get("output_format", base["output_format"]) or "png").lower().strip()
        if fmt == "jpeg":
            fmt = "jpg"
        if fmt not in _VALID_FORMATS - {"jpeg"}:
            if strict:
                errors.append(f"不支持的输出格式: {fmt}")
            fmt = "png"

        elements_in = raw_state.get("elements") or []
        if not isinstance(elements_in, list):
            if strict:
                errors.append("elements 应为列表")
            elements_in = []

        elements_out = []
        for i, el in enumerate(elements_in):
            if not isinstance(el, dict):
                if strict:
                    errors.append(f"elements[{i}] 应为字典")
                continue
            et = str(el.get("type", "text") or "text").lower()
            if et not in ("text", "image"):
                if strict:
                    errors.append(f"elements[{i}].type 无效")
                continue
            item = dict(el)
            item["type"] = et
            if et == "text":
                src = str(item.get("source", "fixed") or "fixed").lower()
                if src not in _VALID_SOURCES:
                    src = "fixed"
                item["source"] = src
                try:
                    item["font_size"] = max(8, min(200, int(item.get("font_size", 24))))
                except (TypeError, ValueError):
                    item["font_size"] = 24
                try:
                    item["x"] = int(item.get("x", 50))
                    item["y"] = int(item.get("y", 50))
                except (TypeError, ValueError):
                    item["x"], item["y"] = 50, 50
            else:
                try:
                    item["x"] = int(item.get("x", 100))
                    item["y"] = int(item.get("y", 100))
                    item["width"] = max(1, int(item.get("width", 200)))
                    item["height"] = max(1, int(item.get("height", 200)))
                except (TypeError, ValueError):
                    item.update({"x": 100, "y": 100, "width": 200, "height": 200})
            elements_out.append(item)

        custom_fonts = raw_state.get("custom_fonts") or {}
        if not isinstance(custom_fonts, dict):
            custom_fonts = {}

        if errors:
            return ValidationResult.failure(*errors)
        return ValidationResult.success({
            "elements": elements_out,
            "output_format": fmt,
            "custom_fonts": dict(custom_fonts),
        })

    def build_run_options(
        self, options: dict, output_policy: OutputPolicy | None = None
    ) -> dict[str, Any]:
        result = self.validate_and_normalize(options, strict=False)
        opts = dict(result.value or self.default_state())
        opts["_output_format"] = opts.get("output_format") or "png"
        opts.setdefault("keep_matting", False)
        return opts

    def load_selected(self, path: str | None) -> dict | None:
        return None

    def format_start_log(self, options: dict) -> str | None:
        return None
