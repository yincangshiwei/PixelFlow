"""TransparentService —— 透明图处理功能 Service（无 QWidget / Route 依赖）。"""
from __future__ import annotations

from typing import Any, Optional

from services.contracts.feature_descriptor import FeatureDescriptor, InputKind
from services.contracts.output_policy import OutputPolicy
from core.processors.transparent_processor import default_transparent_options
from core.image_processor import rgba_to_hex
from .validation import ValidationResult

FEATURE_ID = "transparent_image"
FEATURE_NAME = "透明图处理"
FEATURE_ICON = "✂"
FEATURE_DESCRIPTION = "AI 抠图 → 去除透明边缘 → 按画布占比等比放置主体"

_VALID_FORMATS = frozenset({"png", "webp", "jpg", "jpeg"})
_VALID_DETAIL = frozenset({"normal", "subtle", "off"})
_VALID_POSITIONS = frozenset({
    "top_left", "top_center", "top_right",
    "middle_left", "center", "middle_right",
    "bottom_left", "bottom_center", "bottom_right",
})

__all__ = [
    "FEATURE_ID", "FEATURE_NAME", "FEATURE_ICON", "FEATURE_DESCRIPTION",
    "TransparentService", "default_transparent_options",
]


class TransparentService:
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
                service_factory=TransparentService,
                supports_resume=True,
                supports_selected_load=False,
                preset_schema_version=1,
            )
        return self._descriptor

    @staticmethod
    def _make_processor():
        from core.processors.transparent_processor import TransparentImageProcessor
        return TransparentImageProcessor()

    def create_processor(self):
        return self._make_processor()

    def default_state(self) -> dict[str, Any]:
        return default_transparent_options()

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

        def _bool(key, default):
            return bool(raw_state.get(key, default))

        def _int(key, default, lo, hi):
            try:
                n = int(raw_state.get(key, default))
            except (TypeError, ValueError):
                if strict and key in raw_state:
                    errors.append(f"{key} 应为整数")
                n = default
            if n < lo or n > hi:
                if strict:
                    errors.append(f"{key} 应在 {lo}~{hi}")
                n = max(lo, min(hi, n))
            return n

        enable_matting = _bool("enable_matting", base["enable_matting"])
        matting_model = str(raw_state.get("matting_model", base["matting_model"]) or "ben2")
        matting_refine = _bool("matting_refine", base["matting_refine"])
        keep_matting = _bool("keep_matting", False)
        if not enable_matting:
            keep_matting = False
            matting_refine = False

        enable_trim = _bool("enable_trim", base["enable_trim"])
        alpha_threshold = _int("alpha_threshold", base["alpha_threshold"], 0, 254)

        layout_enabled = raw_state.get("enable_layout")
        if layout_enabled is None:
            layout_enabled = bool(raw_state.get("enable_canvas", False))
        enable_layout = bool(layout_enabled)
        canvas_w = _int("canvas_w", base["canvas_w"], 1, 99999)
        canvas_h = _int("canvas_h", base["canvas_h"], 1, 99999)

        subject_percent = raw_state.get("subject_percent")
        if subject_percent is None and raw_state.get("enable_resize"):
            try:
                resize_w = max(1, int(raw_state.get("resize_w", 800)))
                resize_h = max(1, int(raw_state.get("resize_h", 800)))
                subject_percent = round(
                    min(resize_w / max(1, canvas_w), resize_h / max(1, canvas_h)) * 100
                )
            except (TypeError, ValueError):
                subject_percent = 80
        try:
            subject_percent = int(subject_percent if subject_percent is not None else 80)
        except (TypeError, ValueError):
            subject_percent = 80
        subject_percent = max(1, min(100, subject_percent))

        reserve_left = _int("reserve_left_percent", base["reserve_left_percent"], 0, 99)
        reserve_right = _int("reserve_right_percent", base["reserve_right_percent"], 0, 99)
        reserve_top = _int("reserve_top_percent", base["reserve_top_percent"], 0, 99)
        reserve_bottom = _int("reserve_bottom_percent", base["reserve_bottom_percent"], 0, 99)
        if reserve_left + reserve_right >= 100:
            if strict:
                errors.append("左侧预留与右侧预留之和必须小于 100%")
            else:
                reserve_left = base["reserve_left_percent"]
                reserve_right = base["reserve_right_percent"]
        if reserve_top + reserve_bottom >= 100:
            if strict:
                errors.append("顶部预留与底部预留之和必须小于 100%")
            else:
                reserve_top = base["reserve_top_percent"]
                reserve_bottom = base["reserve_bottom_percent"]

        subject_position = str(
            raw_state.get("subject_position", base["subject_position"]) or "center"
        )
        if subject_position not in _VALID_POSITIONS:
            if strict:
                errors.append("主体位置无效")
            subject_position = "center"

        detail = str(raw_state.get("detail_restore", base["detail_restore"]) or "normal")
        if detail not in _VALID_DETAIL:
            detail = "normal"

        try:
            canvas_color = rgba_to_hex(raw_state.get("canvas_color", base["canvas_color"]))
        except Exception:
            canvas_color = "#FFFFFF"

        fmt = str(raw_state.get("output_format", base["output_format"]) or "png").lower().strip()
        if fmt == "jpeg":
            fmt = "jpg"
        if fmt not in (_VALID_FORMATS - {"jpeg"}):
            if strict:
                errors.append(f"不支持的输出格式: {fmt}")
            fmt = "png"

        if errors:
            return ValidationResult.failure(*errors)
        return ValidationResult.success({
            "enable_matting": enable_matting,
            "matting_model": matting_model,
            "matting_refine": matting_refine,
            "keep_matting": keep_matting,
            "enable_trim": enable_trim,
            "alpha_threshold": alpha_threshold,
            "enable_layout": enable_layout,
            "canvas_w": canvas_w,
            "canvas_h": canvas_h,
            "canvas_color": canvas_color,
            "reserve_left_percent": reserve_left,
            "reserve_right_percent": reserve_right,
            "reserve_top_percent": reserve_top,
            "reserve_bottom_percent": reserve_bottom,
            "subject_percent": subject_percent,
            "subject_position": subject_position,
            "detail_restore": detail,
            "output_format": fmt,
        })

    def build_run_options(
        self, options: dict, output_policy: OutputPolicy | None = None
    ) -> dict[str, Any]:
        result = self.validate_and_normalize(options, strict=False)
        opts = dict(result.value or self.default_state())
        opts["_output_format"] = opts.get("output_format") or "png"
        # keep_matting 最终由 ActionBar 按输出区复选框覆盖；此处按 enable_matting 约束
        if not opts.get("enable_matting"):
            opts["keep_matting"] = False
        return opts

    def load_selected(self, path: str | None) -> dict | None:
        return None

    def format_start_log(self, options: dict) -> str | None:
        return None
