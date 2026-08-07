"""MetadataService —— 元数据编辑功能 Service（无 QWidget / Route 依赖）。"""
from __future__ import annotations

from pathlib import Path
from typing import Any, Optional

from services.contracts.feature_descriptor import FeatureDescriptor, InputKind
from services.contracts.output_policy import OutputPolicy
from core.processors.metadata_processor import default_metadata_options
from core.metadata_utils import (
    FIELD_KEYS, CONVERT_TARGETS, normalize_format_name, normalize_keywords,
    read_image_metadata,
)
from .validation import ValidationResult

FEATURE_ID = "metadata_edit"
FEATURE_NAME = "元数据编辑"
FEATURE_ICON = "🏷"
FEATURE_DESCRIPTION = (
    "批量修改图片标题、描述、作者、版权、标记，支持格式转换与 Excel 匹配"
)

_VALID_SOURCES = frozenset({"fixed", "excel", "filename"})

__all__ = [
    "FEATURE_ID",
    "FEATURE_NAME",
    "FEATURE_ICON",
    "FEATURE_DESCRIPTION",
    "MetadataService",
    "default_metadata_options",
]


class MetadataService:
    """元数据编辑：校验 / 规范化 / 选中回读数据 / 构建运行参数。"""

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
                service_factory=MetadataService,
                supports_resume=False,
                supports_selected_load=True,
                preset_schema_version=1,
            )
        return self._descriptor

    @staticmethod
    def _make_processor():
        from core.processors.metadata_processor import MetadataProcessor
        return MetadataProcessor()

    def create_processor(self):
        return self._make_processor()

    def default_state(self) -> dict[str, Any]:
        return default_metadata_options()

    def normalize_preset(self, data: dict | None) -> dict[str, Any]:
        raw = dict(data or {})
        result = self.validate_and_normalize(raw, strict=False)
        if result.ok and result.value is not None:
            return result.value
        return self.default_state()

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

        clear_all = bool(raw_state.get("clear_all", base["clear_all"]))
        enable_convert = bool(raw_state.get("enable_convert", base["enable_convert"]))
        target = normalize_format_name(
            raw_state.get("target_format", base["target_format"])
        ) or "jpg"
        if target == "jpeg":
            target = "jpg"
        if target == "tif":
            target = "tiff"
        if target not in CONVERT_TARGETS:
            if strict:
                errors.append(f"不支持的目标格式: {target}")
            target = "jpg"

        fields_in = raw_state.get("fields") or {}
        if not isinstance(fields_in, dict):
            if strict:
                errors.append("fields 应为字典")
            fields_in = {}

        fields_out: dict[str, Any] = {}
        base_fields = base["fields"]
        for key in FIELD_KEYS:
            conf = fields_in.get(key) or {}
            if not isinstance(conf, dict):
                conf = {}
            b = base_fields[key]
            source = str(conf.get("source", b["source"]) or "fixed").lower()
            if source not in _VALID_SOURCES:
                if strict:
                    errors.append(f"{key}.source 无效")
                source = "fixed"
            try:
                match_column = int(conf.get("match_column", b["match_column"]))
            except (TypeError, ValueError):
                match_column = b["match_column"]
            try:
                data_column = int(conf.get("data_column", b["data_column"]))
            except (TypeError, ValueError):
                data_column = b["data_column"]
            try:
                excel_row_start = int(conf.get("excel_row_start", b["excel_row_start"]))
            except (TypeError, ValueError):
                excel_row_start = b["excel_row_start"]
            match_column = max(1, min(100, match_column))
            data_column = max(1, min(100, data_column))
            excel_row_start = max(1, min(10000, excel_row_start))
            value = conf.get("value", b["value"])
            if value is None:
                value = ""
            value = str(value)
            if key == "keywords":
                value = normalize_keywords(value)
            fields_out[key] = {
                "enabled": bool(conf.get("enabled", b["enabled"])),
                "source": source,
                "value": value,
                "excel_file": str(conf.get("excel_file", b["excel_file"]) or "").strip(),
                "match_column": match_column,
                "data_column": data_column,
                "excel_row_start": excel_row_start,
            }

        if errors:
            return ValidationResult.failure(*errors)

        return ValidationResult.success({
            "clear_all": clear_all,
            "enable_convert": enable_convert,
            "target_format": target,
            "fields": fields_out,
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
        """读取图片元数据，返回 Route.apply_load_result 可消费的数据。"""
        import os

        if not path:
            return {"_load_note": "未读取", "_load_path": None, "_load_ok": False}
        if not os.path.exists(path):
            return {"_load_note": "文件不存在", "_load_path": None, "_load_ok": False}
        try:
            meta = read_image_metadata(path)
        except Exception as e:
            return {
                "_load_note": f"读取失败: {e}",
                "_load_path": None,
                "_load_ok": False,
            }

        name = Path(path).name
        true_fmt = meta.get("true_format") or "?"
        ext_fmt = meta.get("ext_format") or "?"
        note = f"已读取: {name}"
        if true_fmt != ext_fmt:
            note += f"  （扩展名 .{ext_fmt}，真实 {true_fmt}）"
        else:
            note += f"  （{true_fmt}）"
        if not meta.get("supported", True):
            note += "  — 此格式无元数据可写"

        fields = {}
        for key in FIELD_KEYS:
            val = meta.get(key, "") or ""
            if key == "keywords":
                val = normalize_keywords(val)
            fields[key] = {
                "enabled": bool(str(val).strip()),
                "source": "fixed",
                "value": val,
            }
        return {
            "_load_note": note,
            "_load_path": path,
            "_load_ok": True,
            "fields": fields,
        }

    def format_start_log(self, options: dict) -> str | None:
        return None
