"""BasicService —— 基础处理功能 Service（无 QWidget / Route 依赖）。

数据流：
  Route.collect_raw_state() → validate_and_normalize → 普通 options
  Preset JSON → normalize_preset → Route.apply_state()
"""
from __future__ import annotations

from typing import Any, Optional

from services.contracts.feature_descriptor import FeatureDescriptor, InputKind
from services.contracts.output_policy import OutputPolicy
from core.processors.basic_processor import default_basic_options
from .validation import ValidationResult

FEATURE_ID = "basic_process"
FEATURE_NAME = "基础处理"
FEATURE_ICON = "⚙"
FEATURE_DESCRIPTION = "格式转换 / 图片压缩 / 修改 DPI / 批量重命名，可任意组合"

_VALID_FORMATS = frozenset({"png", "jpg", "bmp", "webp"})
_COMPRESS_FORMATS = frozenset({"jpg", "webp"})
_VALID_COMPRESS_MODES = frozenset({"quality", "size"})
_VALID_PREFIX_MODES = frozenset({"custom", "keep"})

# 再导出，供 Route / 测试使用
__all__ = [
    "FEATURE_ID",
    "FEATURE_NAME",
    "FEATURE_ICON",
    "FEATURE_DESCRIPTION",
    "BasicService",
    "default_basic_options",
    "output_format_from_options",
]


def output_format_from_options(options: dict) -> str:
    """与历史 get_output_format 语义一致：未开格式转换返回空串。"""
    if options.get("enable_format"):
        fmt = str(options.get("output_format", "png") or "png").lower().strip()
        return fmt if fmt in _VALID_FORMATS else "png"
    return ""


class BasicService:
    """基础处理：校验 / 规范化 / 构建运行参数。"""

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
                service_factory=BasicService,
                supports_resume=True,
                supports_selected_load=False,
                preset_schema_version=1,
            )
        return self._descriptor

    @staticmethod
    def _make_processor():
        from core.processors.basic_processor import BasicProcessor
        return BasicProcessor()

    def create_processor(self):
        return self._make_processor()

    def default_state(self) -> dict[str, Any]:
        return default_basic_options()

    def normalize_preset(self, data: dict | None) -> dict[str, Any]:
        """加载预设时：合并默认值并应用联动约束（不抛错，尽量兼容旧 JSON）。"""
        raw = dict(data or {})
        result = self.validate_and_normalize(raw, strict=False)
        if result.ok and result.value is not None:
            return result.value
        # 极端坏数据：回退出厂默认
        return self.default_state()

    def validate_and_normalize(
        self, raw_state: dict | None, *, strict: bool = True
    ) -> ValidationResult:
        """规范化面板原始状态。

        联动规则（与历史 UI 一致）：
        - 开启压缩时强制 enable_format=True，且 output_format ∈ {jpg, webp}
        - quality 1~100；target_size_kb 1~100000；dpi 1~2400
        - prefix_mode ∈ {custom, keep}；digits 1~8；start_index 0~99999
        """
        if raw_state is None:
            if strict:
                return ValidationResult.failure("参数不能为空")
            raw_state = {}
        if not isinstance(raw_state, dict):
            return ValidationResult.failure("参数必须是字典")

        base = self.default_state()
        errors: list[str] = []

        def _bool(key: str, default: bool) -> bool:
            v = raw_state.get(key, default)
            if isinstance(v, bool):
                return v
            if v in (0, 1, "0", "1", "true", "false", "True", "False"):
                return str(v).lower() in ("1", "true")
            if strict and key in raw_state:
                errors.append(f"{key} 应为布尔值")
            return bool(v) if v is not None else default

        def _int(key: str, default: int, lo: int, hi: int) -> int:
            v = raw_state.get(key, default)
            try:
                n = int(v)
            except (TypeError, ValueError):
                if strict and key in raw_state:
                    errors.append(f"{key} 应为整数")
                n = default
            if n < lo or n > hi:
                if strict:
                    errors.append(f"{key} 应在 {lo}~{hi} 之间")
                n = max(lo, min(hi, n))
            return n

        enable_compress = _bool("enable_compress", base["enable_compress"])
        compress_mode = str(raw_state.get("compress_mode", base["compress_mode"]) or "quality").lower()
        if compress_mode not in _VALID_COMPRESS_MODES:
            if strict:
                errors.append("compress_mode 仅支持 quality / size")
            compress_mode = "quality"

        quality = _int("quality", base["quality"], 1, 100)
        target_size_kb = _int("target_size_kb", base["target_size_kb"], 1, 100000)

        enable_format = _bool("enable_format", base["enable_format"])
        fmt = str(raw_state.get("output_format", base["output_format"]) or "png").lower().strip()
        if fmt == "jpeg":
            fmt = "jpg"
        if fmt not in _VALID_FORMATS:
            if strict:
                errors.append(f"不支持的输出格式: {fmt}")
            fmt = "png"

        # 压缩联动：强制格式转换，且仅 JPG/WEBP
        if enable_compress:
            enable_format = True
            if fmt not in _COMPRESS_FORMATS:
                fmt = "jpg"

        enable_dpi = _bool("enable_dpi", base["enable_dpi"])
        dpi = _int("dpi", base["dpi"], 1, 2400)

        enable_rename = _bool("enable_rename", base["enable_rename"])
        prefix_mode = str(raw_state.get("prefix_mode", base["prefix_mode"]) or "custom").lower()
        if prefix_mode not in _VALID_PREFIX_MODES:
            if strict:
                errors.append("prefix_mode 仅支持 custom / keep")
            prefix_mode = "custom"
        prefix = str(raw_state.get("prefix", base["prefix"]) or "").strip()
        start_index = _int("start_index", base["start_index"], 0, 99999)
        digits = _int("digits", base["digits"], 1, 8)

        if errors:
            return ValidationResult.failure(*errors)

        return ValidationResult.success({
            "enable_compress": enable_compress,
            "compress_mode": compress_mode,
            "quality": quality,
            "target_size_kb": target_size_kb,
            "enable_format": enable_format,
            "output_format": fmt,
            "enable_dpi": enable_dpi,
            "dpi": dpi,
            "enable_rename": enable_rename,
            "prefix_mode": prefix_mode,
            "prefix": prefix,
            "start_index": start_index,
            "digits": digits,
        })

    def build_run_options(
        self, options: dict, output_policy: OutputPolicy | None = None
    ) -> dict[str, Any]:
        """构建批处理运行参数（补跨层字段；P3 将完整接入 OutputPolicy）。"""
        result = self.validate_and_normalize(options, strict=False)
        opts = dict(result.value or self.default_state())
        opts["_output_format"] = output_format_from_options(opts)
        # keep_matting 等与 Basic 无关，显式关闭避免脏数据
        opts.setdefault("keep_matting", False)
        if output_policy is not None:
            # 预留：跨层输出语义由编排层消费，此处不写入魔法键以外的内容
            pass
        return opts

    def load_selected(self, path: str | None) -> dict | None:
        """基础处理不支持选中回读。"""
        return None

    def format_start_log(self, options: dict) -> str | None:
        return None
