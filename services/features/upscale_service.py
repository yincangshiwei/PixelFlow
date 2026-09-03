"""UpscaleService —— 高清放大功能 Service（无 QWidget / Route 依赖）。

参数校验完全由引擎注册表的 ``ParamSpec`` schema 驱动：
新增放大引擎时本文件**不需要**为新参数写任何校验分支。

数据流：
  Route.collect_raw_state() → validate_and_normalize → 普通 options
  Preset JSON → normalize_preset → Route.apply_state()
"""
from __future__ import annotations

from typing import Any, Optional

from core.processors.upscale_processor import default_upscale_options
from core.upscale.engine_registry import (
    DEFAULT_ENGINE_ID,
    DEFAULT_OUTPUT_FORMAT,
    OUTPUT_FORMATS,
    all_engines_defaults,
    coerce_params,
    get_engine,
    resolve_engine_id,
)
from services.contracts.feature_descriptor import FeatureDescriptor, InputKind
from services.contracts.output_policy import OutputPolicy

from .validation import ValidationResult

FEATURE_ID = "upscale"
FEATURE_NAME = "高清放大"
FEATURE_ICON = "🔍"
FEATURE_DESCRIPTION = "AI 高清放大（DLSS 5 神经渲染 + 超分），支持多趟放大至 8×"

_VALID_FORMATS = frozenset({"png", "jpg", "jpeg", "webp"})
# 交由 Worker 现有保存逻辑做质量控制的格式
_LOSSY_FORMATS = frozenset({"jpg", "jpeg", "webp"})

__all__ = [
    "FEATURE_ID",
    "FEATURE_NAME",
    "FEATURE_ICON",
    "FEATURE_DESCRIPTION",
    "UpscaleService",
    "default_upscale_options",
    "active_engine_id",
    "active_params",
]


def active_engine_id(options: dict | None) -> str:
    return resolve_engine_id((options or {}).get("engine"))


def active_params(options: dict | None) -> dict[str, Any]:
    """取出当前引擎的参数（命名空间形态；兼容扁平旧数据）。"""
    opts = dict(options or {})
    engine_id = active_engine_id(opts)
    engines = opts.get("engines")
    if isinstance(engines, dict) and isinstance(engines.get(engine_id), dict):
        return dict(engines[engine_id])
    # 兼容：参数直接平铺在顶层
    engine = get_engine(engine_id)
    if engine is None:
        return {}
    return {p.key: opts[p.key] for p in engine.params if p.key in opts}


class UpscaleService:
    """高清放大：校验 / 规范化 / 构建运行参数。"""

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
                service_factory=UpscaleService,
                supports_resume=True,
                supports_selected_load=False,
                preset_schema_version=1,
            )
        return self._descriptor

    @staticmethod
    def _make_processor():
        from core.processors.upscale_processor import UpscaleProcessor
        return UpscaleProcessor()

    def create_processor(self):
        return self._make_processor()

    def default_state(self) -> dict[str, Any]:
        return default_upscale_options()

    def normalize_preset(self, data: dict | None) -> dict[str, Any]:
        """加载预设：合并默认值，坏数据静默收敛，不抛错。"""
        result = self.validate_and_normalize(dict(data or {}), strict=False)
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

        engine_id = resolve_engine_id(raw_state.get("engine", base["engine"]))
        if strict and raw_state.get("engine") not in (None, engine_id):
            errors.append(f"未知的放大引擎: {raw_state.get('engine')!r}")

        # ── 各引擎参数按各自 schema 规范化（命名空间存放，切换引擎不丢参数）──
        raw_engines = raw_state.get("engines")
        if not isinstance(raw_engines, dict):
            raw_engines = {}
        engines_out: dict[str, dict[str, Any]] = {}
        for eid, defaults in all_engines_defaults().items():
            engine = get_engine(eid)
            raw_params = raw_engines.get(eid)
            if not isinstance(raw_params, dict):
                # 当前引擎允许参数平铺在顶层（兼容手写预设 / 旧数据）
                raw_params = dict(raw_state) if eid == engine_id else {}
            if engine is None:
                engines_out[eid] = dict(defaults)
                continue
            values, param_errors = coerce_params(engine, raw_params, strict=strict and eid == engine_id)
            if eid == engine_id:
                errors.extend(param_errors)
            engines_out[eid] = values

        # ── 引擎内联动约束 ──
        params = engines_out.get(engine_id, {})
        if engine_id == "dlss5" and params:
            if params.get("use_multi_pass"):
                target = float(params.get("target_scale", 4.0))
                if target <= 1.0:
                    if strict:
                        errors.append("多趟放大的目标倍数必须大于 1")
                    params["target_scale"] = 4.0

        # ── 输出格式 ──
        fmt = str(
            raw_state.get("output_format", base["output_format"]) or DEFAULT_OUTPUT_FORMAT
        ).lower().strip()
        if fmt not in _VALID_FORMATS:
            if strict:
                errors.append(f"不支持的输出格式: {fmt}")
            fmt = DEFAULT_OUTPUT_FORMAT

        try:
            quality = int(raw_state.get("quality", base["quality"]))
        except (TypeError, ValueError):
            if strict and "quality" in raw_state:
                errors.append("quality 应为整数")
            quality = int(base["quality"])
        if not 1 <= quality <= 100:
            if strict:
                errors.append("quality 应在 1~100 之间")
            quality = max(1, min(100, quality))

        if errors:
            return ValidationResult.failure(*errors)

        return ValidationResult.success({
            "engine": engine_id,
            "engines": engines_out,
            "output_format": fmt,
            "quality": quality,
        })

    def build_run_options(
        self, options: dict, output_policy: OutputPolicy | None = None
    ) -> dict[str, Any]:
        """构建批处理运行参数。

        有损格式（JPG/WEBP）复用 Worker 既有的保存质量通道：
        置 ``enable_compress`` + ``compress_mode="quality"``，Worker 即按 ``quality`` 保存；
        PNG 走无损，不启用压缩。
        """
        result = self.validate_and_normalize(options, strict=False)
        opts = dict(result.value or self.default_state())
        fmt = str(opts.get("output_format") or DEFAULT_OUTPUT_FORMAT).lower()
        opts["_output_format"] = "jpg" if fmt == "jpeg" else fmt
        # 与高清放大无关的跨层字段显式关闭，避免脏数据
        opts["keep_matting"] = False
        if opts["_output_format"] in _LOSSY_FORMATS:
            opts["enable_compress"] = True
            opts["compress_mode"] = "quality"
        else:
            opts["enable_compress"] = False
            opts.pop("compress_mode", None)
        return opts

    def load_selected(self, path: str | None) -> dict | None:
        """高清放大不支持选中图回读。"""
        return None

    def format_start_log(self, options: dict) -> str | None:
        """开始处理时的日志摘要（引擎 / 档位 / 输出格式）。"""
        opts = dict(options or {})
        engine_id = active_engine_id(opts)
        engine = get_engine(engine_id)
        params = active_params(opts)
        name = engine.name if engine is not None else engine_id
        bits = [f"放大引擎: {name}"]

        if engine_id == "dlss5" and params:
            from core.upscale.engine_registry import DLSS5_MODE_NAME
            if params.get("use_multi_pass"):
                from core.upscale.dlss5 import plan_passes
                modes, achieved = plan_passes(float(params.get("target_scale", 4.0)))
                bits.append(
                    f"多趟放大: 目标 {float(params.get('target_scale', 4.0)):g}× → "
                    + " + ".join(DLSS5_MODE_NAME.get(m, m) for m in modes)
                    + f"（实际 {achieved:g}×）"
                )
            else:
                bits.append(f"档位: {DLSS5_MODE_NAME.get(str(params.get('scale_mode')), '2× Performance')}")
            bits.append(
                f"NR: style={params.get('nr_style')} intensity={params.get('nr_intensity')} "
                f"preset={params.get('nr_preset')}"
            )
            bits.append(
                f"细节: structure={params.get('local_structure_strength')} "
                f"tone={params.get('local_tone_strength')} "
                f"skin={params.get('skin_structure_strength')}"
            )
            bits.append(f"DLSS 模型预设: {params.get('dlss_model_preset')}")

        bits.append(f"输出格式: {str(opts.get('output_format', 'png')).upper()}")
        return "  ·  ".join(bits)
