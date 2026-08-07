"""Img2DocService —— 排版导出功能 Service（无 QWidget / Route 依赖）。"""
from __future__ import annotations

from typing import Any, Optional

from services.contracts.feature_descriptor import FeatureDescriptor, InputKind
from services.contracts.output_policy import OutputPolicy
from core.processors.img2doc_processor import default_img2doc_options
from .validation import ValidationResult

FEATURE_ID = "img2doc"
FEATURE_NAME = "图片排版导出"
FEATURE_ICON = "📑"
FEATURE_DESCRIPTION = "将多张图片按指定布局排版，导出为 PPT / PDF / Word"

_VALID_FORMATS = frozenset({"PPTX", "PDF", "DOCX"})
_VALID_ALIGN = frozenset({"left", "center", "right"})
_VALID_COMPRESS = frozenset({"JPEG", "WEBP"})

__all__ = [
    "FEATURE_ID", "FEATURE_NAME", "FEATURE_ICON", "FEATURE_DESCRIPTION",
    "Img2DocService", "default_img2doc_options",
]


class Img2DocService:
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
                input_kind=InputKind.BATCH_MERGED,
                processor_factory=self._make_processor,
                service_factory=Img2DocService,
                supports_resume=False,
                supports_selected_load=False,
                preset_schema_version=1,
            )
        return self._descriptor

    @staticmethod
    def _make_processor():
        from core.processors.img2doc_processor import Img2DocProcessor
        return Img2DocProcessor()

    def create_processor(self):
        return self._make_processor()

    def default_state(self) -> dict[str, Any]:
        return default_img2doc_options()

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

        def _num(key, default, lo, hi, cast=float):
            try:
                v = cast(raw_state.get(key, default))
            except (TypeError, ValueError):
                if strict and key in raw_state:
                    errors.append(f"{key} 数值无效")
                v = default
            return max(lo, min(hi, v))

        fmt = str(raw_state.get("format", base["format"]) or "PPTX").upper()
        if fmt not in _VALID_FORMATS:
            if strict:
                errors.append("format 仅支持 PPTX/PDF/DOCX")
            fmt = "PPTX"

        width_cm = _num("width_cm", base["width_cm"], 1, 200)
        height_cm = _num("height_cm", base["height_cm"], 1, 200)
        count_per_page = int(_num("count_per_page", base["count_per_page"], 1, 64, int))
        max_cols = int(_num("max_cols", base["max_cols"], 1, 16, int))
        keep_ratio = bool(raw_state.get("keep_ratio", base["keep_ratio"]))
        row_align = str(raw_state.get("row_align", base["row_align"]) or "left").lower()
        if row_align not in _VALID_ALIGN:
            row_align = "left"
        sort_mode = str(raw_state.get("sort_mode", base["sort_mode"]) or "default")
        group_by_folder = bool(raw_state.get("group_by_folder", base["group_by_folder"]))
        excel_path = str(raw_state.get("excel_path", "") or "")
        col_name = str(raw_state.get("col_name", "A") or "A").upper()
        col_val = str(raw_state.get("col_val", "B") or "B").upper()
        start_row = int(_num("start_row", base["start_row"], 1, 100000, int))
        compress_enabled = bool(raw_state.get("compress_enabled", False))
        compress_target_kb = int(_num("compress_target_kb", 500, 1, 100000, int))
        compress_format = str(raw_state.get("compress_format", "JPEG") or "JPEG").upper()
        if compress_format not in _VALID_COMPRESS:
            compress_format = "JPEG"

        layers = raw_state.get("overlay_layers") or []
        if not isinstance(layers, list):
            layers = []
        custom_fonts = raw_state.get("custom_fonts") or {}
        if not isinstance(custom_fonts, dict):
            custom_fonts = {}

        if errors:
            return ValidationResult.failure(*errors)
        return ValidationResult.success({
            "format": fmt,
            "width_cm": width_cm,
            "height_cm": height_cm,
            "count_per_page": count_per_page,
            "max_cols": max_cols,
            "keep_ratio": keep_ratio,
            "row_align": row_align,
            "sort_mode": sort_mode,
            "group_by_folder": group_by_folder,
            "excel_path": excel_path,
            "col_name": col_name,
            "col_val": col_val,
            "start_row": start_row,
            "compress_enabled": compress_enabled,
            "compress_target_kb": compress_target_kb,
            "compress_format": compress_format,
            "overlay_layers": list(layers),
            "custom_fonts": dict(custom_fonts),
        })

    def build_run_options(
        self, options: dict, output_policy: OutputPolicy | None = None
    ) -> dict[str, Any]:
        result = self.validate_and_normalize(options, strict=False)
        opts = dict(result.value or self.default_state())
        opts["_output_format"] = ""
        opts.setdefault("keep_matting", False)
        return opts

    def load_selected(self, path: str | None) -> dict | None:
        return None

    def format_start_log(self, options: dict) -> str | None:
        return None
